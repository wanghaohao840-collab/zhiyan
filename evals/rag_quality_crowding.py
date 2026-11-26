"""Compare scoped vector, hybrid and reranked retrieval at a common pool depth."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx

from deploy.embedding_probe import read_values
from evals.rag_quality_crowding_data import crowding_fixture_json, make_crowding_dataset
from evals.rag_quality_crowding_scale import make_scale_dataset
from evals.rag_quality_dataset import load_gold_dataset
from evals.rag_quality_experiment import build_index, provider_error
from evals.rag_quality_report import _latency
from evals.rag_quality_scoring import has_fact
from hello_agents.memory.rag.embedding_client import SiliconFlowEmbedding
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure, EmbeddingSettings
from hello_agents.memory.rag.result_utils import mmr_select, relevance_rerank_results


FROZEN_HASHES = {
    "dev": "97c55cbe736b7d3063da9b1fa45dfcabbf151039b33fe12e715252e6625a0b90",
    "holdout": "2243cdc398f3a24eb852575d5ab6a659309a8c5f0f4791375978d0f2b3caef78",
}
SCALE_HASHES = {
    "dev": "f329091ca632be15545518259e551244a56ce26d51eebe1194175ff8b9cff60b",
    "holdout": "61857ee15acc6a775df9f2daaac97130aac3dabe4f63a2933e25ee1bf9d2fb54",
}
TOP_K = 5


class CountedTransport(httpx.AsyncBaseTransport):
    """Count native client attempts while keeping its timeouts and retry policy."""

    def __init__(self):
        self.attempts = 0
        self.retries = 0
        self.transport = None

    async def handle_async_request(self, request):
        if self.transport is None:
            self.transport = httpx.AsyncHTTPTransport()
        else:
            self.retries += 1
        self.attempts += 1
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        if self.transport is not None:
            await self.transport.aclose()
            self.transport = None


@dataclass
class PreparedQueries:
    dimension: int
    vectors: dict

    def encode(self, text):
        return self.vectors[text]


class MeasuredEmbedding:
    def __init__(self, client, transport):
        self.client = client
        self.transport = transport
        self.dimension = client.profile.dimension
        self.stages = {}

    def encode_documents(self, texts):
        return self.measure(texts, "index")

    def measure(self, texts, stage):
        started = perf_counter()
        before = self.transport.attempts
        retries_before = self.transport.retries
        try:
            return self.client.embed_documents(texts)
        finally:
            expected = (len(texts) + self.client.settings.batch_size - 1) // self.client.settings.batch_size
            self.stages[stage] = {
                "inputs": len(texts), "configured_batches": expected,
                "http_attempts": self.transport.attempts - before,
                "http_retries": self.transport.retries - retries_before,
                "elapsed_ms_including_retries": (perf_counter() - started) * 1000,
            }


def score_retrieval(case, variant, pool, top, elapsed_ms):
    gold = set(case.evidence)
    ranks = {
        hit["id"]: rank for rank, hit in enumerate(top, 1)
    }
    first_rank = min((ranks[id_] for id_ in gold & ranks.keys()), default=None)
    all_in_pool = gold <= {hit["id"] for hit in pool}
    all_in_top = gold <= ranks.keys()
    scope_leaks = {
        hit["id"] for hit in [*pool, *top]
        if hit["metadata"].get("rag_namespace") != case.namespace
        or hit["metadata"].get("document_id") not in case.scope
    }
    intrusion = [
        hit["id"] for hit in top
        if any(has_fact(hit["content"], wrong) for wrong in case.forbidden)
    ]
    if not all_in_pool:
        miss = "pool_miss"
    elif not all_in_top:
        miss = "rank_drop"
    else:
        miss = None
    return {
        "case": case.id, "bucket": case.bucket, "language": case.language, "scope_size": len(case.scope),
        "variant": variant, "gold": list(case.evidence), "first_rank": first_rank,
        "pool_all": all_in_pool if gold else None, "top_all": all_in_top if gold else None,
        "miss": miss, "pool_recall": len(gold & {hit["id"] for hit in pool}) / len(gold) if gold else None,
        "top": list(ranks), "pool": [hit["id"] for hit in pool],
        "forbidden_top": intrusion, "scope_leaks": sorted(scope_leaks), "retrieval_ms": elapsed_ms,
    }


def compare_case(pipeline, case, pool_k, *, rerank=relevance_rerank_results, expanded_pool_k=None):
    options = {
        "query": case.question, "document_ids": list(case.scope), "limit": pool_k,
        "min_score": 0.12, "use_mmr": False, "use_rerank": False,
    }
    started = perf_counter()
    vector = pipeline.search(retrieval_mode="vector", **options)
    vector_ms = (perf_counter() - started) * 1000

    started = perf_counter()
    hybrid = pipeline.search(retrieval_mode="hybrid", **options)
    hybrid_ms = (perf_counter() - started) * 1000

    started = perf_counter()
    reranked = rerank(case.question, hybrid, TOP_K)
    reranked_ms = hybrid_ms + (perf_counter() - started) * 1000
    rows = [
        score_retrieval(case, "vector", vector, vector[:TOP_K], vector_ms),
        score_retrieval(case, "hybrid", hybrid, hybrid[:TOP_K], hybrid_ms),
        score_retrieval(case, "rerank", hybrid, reranked[:TOP_K], reranked_ms),
    ]
    if expanded_pool_k is None:
        return rows

    started = perf_counter()
    diversified = mmr_select(reranked, TOP_K, lambda_mult=0.55)
    diversity_ms = reranked_ms + (perf_counter() - started) * 1000
    rows.append(score_retrieval(case, "rerank_mmr", hybrid, diversified, diversity_ms))

    started = perf_counter()
    expanded = pipeline.search(retrieval_mode="hybrid", **{**options, "limit": expanded_pool_k})
    expanded_ranked = rerank(case.question, expanded, expanded_pool_k)
    expanded_ms = (perf_counter() - started) * 1000
    rows.append(score_retrieval(case, "expanded_rerank", expanded, expanded_ranked[:TOP_K], expanded_ms))

    started = perf_counter()
    wide_diversified = mmr_select(expanded_ranked, TOP_K, lambda_mult=0.55)
    wide_diversity_ms = expanded_ms + (perf_counter() - started) * 1000
    rows.append(score_retrieval(case, "expanded_rerank_mmr", expanded, wide_diversified, wide_diversity_ms))
    return rows


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[("overall", row["variant"])].append(row)
        groups[(row["bucket"], row["variant"])].append(row)
        if "scope_chunks" in row:
            groups[(f"规模 {row['scope_chunks']} 段", row["variant"])].append(row)
    summary = defaultdict(dict)
    for (bucket, variant), group in groups.items():
        count = len(group)
        positive = [row for row in group if row["gold"]]
        divisor = len(positive)
        latencies = [row["retrieval_ms"] for row in group]
        summary[bucket][variant] = {
            "cases": count,
            "min_pool_size": min(len(row["pool"]) for row in group),
            "answerable": divisor,
            "top_1": sum(row["first_rank"] == 1 for row in positive) / divisor if divisor else None,
            "mrr_at_5": (
                sum(1 / row["first_rank"] for row in positive if row["first_rank"]) / divisor
                if divisor else None
            ),
            "pool_all": sum(row["pool_all"] for row in positive) / divisor if divisor else None,
            "pool_recall": sum(row["pool_recall"] for row in positive) / divisor if divisor else None,
            "top_all": sum(row["top_all"] for row in positive) / divisor if divisor else None,
            "pool_miss": sum(row["miss"] == "pool_miss" for row in group),
            "rank_drop": sum(row["miss"] == "rank_drop" for row in group),
            "forbidden_intrusion": sum(bool(row["forbidden_top"]) for row in group),
            "forbidden_first": sum(bool(row["top"] and row["top"][0] in row["forbidden_top"]) for row in group),
            "scope_leaks": sum(len(row["scope_leaks"]) for row in group),
            "latency_ms": {**_latency(latencies), "max": max(latencies)},
        }
    return dict(summary)


def write_output(output, run, stages, rows):
    summary = summarize(rows)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(
        {"run": run, "embedding_stages": stages, "summary": summary, "cases": rows},
        ensure_ascii=False, indent=2, allow_nan=False,
    ) + "\n", encoding="utf-8")
    lines = [
        "# RAG 相似片段拥挤测试", "",
        f"状态 {run['status']}，题集 {run.get('dataset', 'crowding')}，分组 {run['split']}，候选池 {run['pool_k']}，最终返回 {TOP_K} 条。",
        f"合成题集 SHA256 {run['dataset_sha256']}。",
    ]
    if run.get("expanded_pool_k") is not None:
        lines.append(f"扩池实验使用 {run['expanded_pool_k']} 条候选。多样性重排降低相似片段的优先级。")
    if not summary:
        lines.extend(["", f"失败环节 {run['failure_stage']}，错误 {run['reason']}。"])
    for bucket, variants in summary.items():
        lines.extend([
            "", f"## {bucket}", "",
            "| 方法 | 题数 | 首条命中 | 前五齐全 | 候选池齐全 | MRR@5 | 池中缺证据 | 排序掉出前五 | 前五含诱饵 / 首条是诱饵 | 越界 | 检索 P50 / P95 / 最大（ms） |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, stats in variants.items():
            timing = stats["latency_ms"]
            top_1, top_all, pool_all, mrr = [
                format(stats[key], spec) if stats[key] is not None else "—"
                for key, spec in (("top_1", ".1%"), ("top_all", ".1%"), ("pool_all", ".1%"), ("mrr_at_5", ".3f"))
            ]
            lines.append(
                f"| {name} | {stats['cases']} | {top_1} | {top_all} | "
                f"{pool_all} | {mrr} | {stats['pool_miss']} | "
                f"{stats['rank_drop']} | {stats['forbidden_intrusion']} / {stats['forbidden_first']} | {stats['scope_leaks']} | "
                f"{timing['p50']:.2f} / {timing['p95']:.2f} / {timing['max']:.2f} |"
            )
    lines.extend([
        "", "混合和重排共用同一候选池。重排耗时含取得该池的耗时。检索计时使用预取的同一批查询向量。",
        "向量接口按探针、建库、查询记录尝试次数和含重试耗时。网络时间不计入表内检索耗时。",
        "排名和证据命中率只统计有答案题。前五含诱饵按已标注的错误值统计，允许与召回或排序失误同时发生。",
        "逐题候选、最终排名、耗时和外部调用记录见同目录 results.json。",
    ])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("crowding", "scale"), default="crowding")
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--pool-k", type=int, choices=range(TOP_K, 31), default=20, metavar="5-30")
    parser.add_argument("--expanded-pool-k", type=int, choices=range(TOP_K + 1, 31), metavar="6-30")
    parser.add_argument("--smoke-limit", type=int, choices=range(1, 6), metavar="1-5")
    parser.add_argument("--embedding-env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.expanded_pool_k is not None and args.expanded_pool_k <= args.pool_k:
        parser.error("--expanded-pool-k must be larger than --pool-k")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    factory, frozen_hashes = (
        (make_scale_dataset, SCALE_HASHES) if args.dataset == "scale"
        else (make_crowding_dataset, FROZEN_HASHES)
    )
    fixture = crowding_fixture_json(*factory(args.split))
    digest = sha256(fixture).hexdigest()
    if digest != frozen_hashes[args.split]:
        raise ValueError("Frozen crowding fixtures changed")
    dataset = output / "dataset.json"
    dataset.write_bytes(fixture)
    chunks, cases = load_gold_dataset(dataset)
    if args.smoke_limit:
        all_scopes = {(case.namespace, doc) for case in cases for doc in case.scope}
        cases = cases[:args.smoke_limit]
        scopes = {(case.namespace, doc) for case in cases for doc in case.scope}
        namespaces = {case.namespace for case in cases}
        chunks = [
            chunk for chunk in chunks if chunk.namespace in namespaces and (
                (chunk.namespace, chunk.document_id) in scopes
                or (chunk.namespace, chunk.document_id) not in all_scopes
            )
        ]
    document_counts = defaultdict(int)
    for chunk in chunks:
        document_counts[chunk.namespace, chunk.document_id] += 1
    run = {
        "status": "available", "split": args.split, "dataset_sha256": digest, "pool_k": args.pool_k,
        "top_k": TOP_K, "question_count": len(cases), "smoke": args.smoke_limit is not None,
        "dataset": args.dataset,
        "fallback_used": False, "expanded_pool_k": args.expanded_pool_k,
    }
    embedder = None
    stage = "smoke"
    rows = []
    try:
        try:
            values = read_values(args.embedding_env_file)
        except OSError:
            raise EmbeddingFailure("configuration_read") from None
        settings = EmbeddingSettings.from_env(values, candidate=True)
        transport = CountedTransport()
        embedder = MeasuredEmbedding(SiliconFlowEmbedding(settings, transport=transport), transport)
        run["embedding"] = {
            "model": settings.profile.model, "fingerprint": settings.profile.fingerprint,
            "batch_size": settings.batch_size, "max_retries": settings.max_retries,
        }
        embedder.measure(["Public RAG crowding benchmark embedding check."], stage)
        with TemporaryDirectory(prefix="rag-crowding-") as directory:
            stage = "index"
            print(f"Embedding {len(chunks)} synthetic passages", flush=True)
            pipelines = build_index(chunks, Path(directory), embedder)
            stage = "query"
            questions = list(dict.fromkeys(case.question for case in cases))
            prepared = PreparedQueries(
                embedder.dimension, dict(zip(questions, embedder.measure(questions, stage), strict=True)),
            )
            print(f"Comparing retrieval for {len(cases)} questions", flush=True)
            for pipeline in pipelines.values():
                pipeline.embedder = prepared
            for case in cases:
                scope_chunks = sum(document_counts[case.namespace, doc] for doc in case.scope)
                compared = compare_case(
                    pipelines[case.namespace], case, args.pool_k, expanded_pool_k=args.expanded_pool_k,
                )
                rows.extend({**row, "scope_chunks": scope_chunks} for row in compared)
    except EmbeddingFailure as exc:
        run.update(
            status="service_unavailable", failure_stage=stage, reason=provider_error(exc),
            failure_code=exc.code, http_status=exc.status_code, retryable=exc.retryable,
        )
        write_output(output, run, embedder.stages if embedder is not None else {}, [])
        return 1
    totals = summarize(rows)["overall"]["vector"]
    run["benchmark_discriminates"] = (
        totals["answerable"] > 0 and totals["top_1"] < 0.9 and totals["top_all"] < 0.9
    )
    write_output(output, run, embedder.stages, rows)
    print(output / "report.md", flush=True)
    return int(any(row["scope_leaks"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
