"""Compare the JSON and in-memory Qdrant RAG backends with identical vectors."""

import argparse
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from qdrant_client import QdrantClient

from evals.rag_quality_crowding import PreparedQueries
from evals.rag_quality_crowding_data import crowding_fixture_json
from evals.rag_quality_crowding_scale import make_scale_dataset
from evals.rag_quality_dataset import HOLDOUT_DATASET, load_gold_dataset
from evals.rag_quality_experiment import (
    BASELINE, SimpleEmbedding, build_index, configure_chat, configure_embedding, evaluate_case, provider_error,
)
from evals.rag_quality_report import _latency, summarize
from hello_agents.memory.rag import qdrant_pipeline
from hello_agents.memory.rag.contracts import DocumentSegment
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure, normalize_vector


COLLECTION = "rag_parity_synthetic"
MODES = {
    "vector": ("vector", False, False),
    "hybrid": ("hybrid", False, False),
    "rerank": ("hybrid", True, False),
    "mmr": ("hybrid", True, True),
}
EPSILON = 1e-6


def prepared_vectors(chunks, cases, embedder):
    texts = list(dict.fromkeys(chunk.content for chunk in chunks))
    queries = list(dict.fromkeys(case.question for case in cases))
    encode_documents = getattr(embedder, "encode_documents", None)
    documents = encode_documents(texts) if encode_documents else [embedder.encode(text) for text in texts]
    # Query and document inputs stay distinct even when the text happens to match.
    query_vectors = encode_documents(queries) if encode_documents else [embedder.encode(text) for text in queries]
    for vector in [*documents, *query_vectors]:
        normalize_vector(vector, embedder.dimension)  # validates without changing the shared values
    return (
        PreparedQueries(embedder.dimension, dict(zip(texts, documents, strict=True))),
        PreparedQueries(embedder.dimension, dict(zip(queries, query_vectors, strict=True))),
    )


def with_decoys(chunks, cases):
    # Use one identical-text, same-document-ID probe per evaluated namespace.
    outside = {}
    by_id = {chunk.id: chunk for chunk in chunks}
    for case in cases:
        if case.namespace in outside or not case.evidence:
            continue
        chunk = by_id[case.evidence[0]]
        outside[case.namespace] = [
            replace(chunk, id=f"{chunk.document_id}_0", namespace=f"{chunk.namespace}-outside",
                    page_number=chunk.page_number + 1000),
            replace(chunk, id=f"{chunk.document_id}_1", namespace=f"{chunk.namespace}-outside",
                    page_number=chunk.page_number + 1001, content="Outside namespace. Scope probe XR-99998."),
        ]
    return [*chunks, *(chunk for group in outside.values() for chunk in group)]


def build_qdrant(chunks, client, embedder, queries):
    by_namespace = defaultdict(lambda: defaultdict(list))
    for chunk in chunks:
        by_namespace[chunk.namespace][chunk.document_id].append(chunk)
    with patch.object(qdrant_pipeline, "get_dimension", return_value=embedder.dimension):
        pipelines = {
            namespace: qdrant_pipeline.QdrantRAGPipeline(
                collection_name=COLLECTION, rag_namespace=namespace, qdrant_client=client,
            )
            for namespace in by_namespace
        }
    for namespace, documents in by_namespace.items():
        pipeline = pipelines[namespace]
        pipeline.embedder = embedder
        for doc, group in documents.items():
            segments = [
                DocumentSegment(chunk.content, {"file_name": chunk.file_name, "page_number": chunk.page_number})
                for chunk in group
            ]
            result = pipeline.replace_document(doc, segments)
            if not result["success"] or result["chunks_added"] != len(group):
                raise ValueError(f"In-memory Qdrant changed fixture segmentation for {doc}")
        pipeline.embedder = queries
    return pipelines


def checked_chunk_ids(pipelines, chunks):
    by_doc = defaultdict(list)
    for chunk in chunks:
        by_doc[chunk.namespace, chunk.document_id].append(chunk)
    ids_by_key = {}
    for (namespace, doc), originals in by_doc.items():
        observed = pipelines[namespace].get_document_chunks(doc)
        if len(observed) != len(originals):
            raise ValueError(f"Wrong chunk count for {namespace}/{doc}")
        for index, (stored, chunk) in enumerate(zip(observed, originals, strict=True)):
            metadata = stored["metadata"]
            if (
                stored["content"] != chunk.content or metadata["rag_namespace"] != namespace
                or metadata["document_id"] != doc or metadata["page_number"] != chunk.page_number
                or metadata["file_name"] != chunk.file_name or metadata["chunk_index"] != index
            ):
                raise ValueError(f"Metadata mismatch at {namespace}/{doc}/{index}")
            ids_by_key[namespace, stored["id"]] = chunk.id
    return ids_by_key


def ranked_hits(pipeline, id_map, case, mode):
    retrieval_mode, rerank, mmr = MODES[mode]
    start = perf_counter()
    hits = pipeline.search(
        case.question, document_ids=list(case.scope), limit=5, min_score=0.12,
        retrieval_mode=retrieval_mode, use_rerank=rerank, use_mmr=mmr,
    )
    elapsed = (perf_counter() - start) * 1000
    selected = []
    for hit in hits:
        metadata = hit["metadata"]
        if metadata["rag_namespace"] != case.namespace or metadata["document_id"] not in case.scope:
            raise ValueError(f"Scope violation for {case.id}")
        resolved = pipeline.get_document_chunk(metadata["document_id"], hit["id"], metadata["chunk_index"])
        if (
            resolved is None or resolved["content"] != hit["content"]
            or resolved["metadata"]["page_number"] != metadata["page_number"]
            or resolved["metadata"]["file_name"] != metadata["file_name"]
        ):
            raise ValueError(f"Unresolvable source for {case.id}")
        selected.append((id_map[case.namespace, hit["id"]], float(hit["score"])))
    return selected, elapsed


def difference(left, right):
    left_ids = [id_ for id_, _ in left]
    right_ids = [id_ for id_, _ in right]
    if left_ids == right_ids:
        return "same"
    if set(left_ids) == set(right_ids):
        right_ranks = {id_: rank for rank, id_ in enumerate(right_ids)}
        right_scores = dict(right)
        if all(
            abs(score_a - score_b) <= EPSILON and abs(right_scores[a] - right_scores[b]) <= EPSILON
            for index, (a, score_a) in enumerate(left)
            for b, score_b in left[index + 1:] if right_ranks[a] > right_ranks[b]
        ):
            return "tie_order"
    if left and right and len(left) == len(right):
        lost = [(id_, score) for id_, score in left if id_ not in right_ids]
        gained = [(id_, score) for id_, score in right if id_ not in left_ids]
        if lost and gained and all(
            abs(score - left[-1][1]) <= EPSILON and abs(score - right[-1][1]) <= EPSILON
            for _, score in [*lost, *gained]
        ):
            return "tie_at_cutoff"
    return "unexplained"


def summarize_search(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["suite"], row["mode"]].append(row)
        if row["suite"] == "scale30":
            grouped[f"scope-{row['scope_chunks']}", row["mode"]].append(row)
    result = {}
    for (suite, mode), group in grouped.items():
        metrics = result.setdefault(suite, {}).setdefault(mode, {})
        metrics["cases"] = len(group)
        metrics["differences"] = {
            status: sum(row["difference"] == status for row in group)
            for status in ("same", "tie_order", "tie_at_cutoff", "unexplained")
        }
        positive = [row for row in group if row["gold"]]
        for backend in ("json", "qdrant"):
            selected = [[id_ for id_, _ in row[backend]] for row in positive]
            gold = [set(row["gold"]) for row in positive]
            ranks = [
                next((i for i, id_ in enumerate(ids, 1) if id_ in expected), 0)
                for ids, expected in zip(selected, gold, strict=True)
            ]
            metrics[backend] = {
                "hit_at_1": mean(rank == 1 for rank in ranks),
                "mrr_at_5": mean(1 / rank if rank else 0 for rank in ranks),
                "all_at_5": mean(expected <= set(ids) for ids, expected in zip(selected, gold, strict=True)),
                "search_ms": _latency([row[f"{backend}_ms"] for row in group]),
            }
    return result


def qa_sample(cases):
    return [
        next(case for case in cases if case.language == language and condition(case))
        for condition in (
            lambda case: len(case.evidence) == 1,
            lambda case: len(case.evidence) > 1,
            lambda case: not case.evidence,
        )
        for language in ("zh", "en")
    ]


def qa_trials(backends, maps, cases, generate):
    for case in qa_sample(cases):
        for name, pipelines in backends.items():
            physical_ids = {value: id_ for (namespace, id_), value in maps[name].items() if namespace == case.namespace}
            mapped = replace(
                case, evidence=tuple(physical_ids[id_] for id_ in case.evidence),
                fact_evidence={
                    fact: tuple(physical_ids[id_] for id_ in ids) for fact, ids in case.fact_evidence.items()
                } if case.fact_evidence is not None else None,
            )
            variant = replace(
                BASELINE, name=name, use_application_engine=True, verify_evidence=True,
                evidence_strategy="adaptive", evidence_max_tokens=4096, evidence_check_fields=True,
            )
            yield evaluate_case(pipelines[case.namespace], mapped, variant, generate=generate, repeats=0)


def qa_failures(rows):
    outcomes = defaultdict(dict)
    for row in rows:
        score = row["answer_scores"] or {}
        outcomes[row["case"]][row["variant"]] = (
            bool(score.get("accuracy")),
            bool(score.get("abstained")) and not row["citations"]["emitted"],
        )
    hard_failures = [
        row["case"] for row in rows
        if row["scope_leaks"] or row["generation_error"]
        or set(row["citations"]["emitted"]) != set(row["citations"]["valid"])
    ]
    different_answers = [
        case for case, pair in outcomes.items()
        if len(pair) != 2 or pair["json"] != pair["qdrant"]
    ]
    return {"scope_or_citation_or_generation": hard_failures, "different_scored_answers": different_answers}


def write_report(output, metadata, retrieval, qa):
    data = {"run": metadata, "retrieval": retrieval, "qa": qa}
    data["summary"] = summarize_search(retrieval)
    data["qa_summary"] = summarize(qa)
    if qa:
        data["qa_failures"] = qa_failures(qa)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    lines = [
        "# JSON 与内存 Qdrant 的 RAG 检索对照", "",
        f"向量模式 {metadata['embedding']['status']}。",
        f"原文与页码核对 {metadata.get('verified_chunks_per_backend', 0)} 段 / 后端。",
        "固定合成数据与预先共享的向量。内存 Qdrant 用于检查本地检索、范围过滤和来源定位。",
        "",
        "| 题集 | 检索策略 | 题数 | 首条命中 | 前五证据齐全 | MRR@5 | P95（ms） | 顺序相同 / 分数相同换序 / 尾项同分 / 未解释 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for suite, modes in data["summary"].items():
        for mode, stats in modes.items():
            local, qdrant = stats["json"], stats["qdrant"]
            diff = " / ".join(str(n) for n in stats["differences"].values())
            lines.append(
                f"| {suite} | {mode} | {stats['cases']} | {local['hit_at_1']:.1%} / {qdrant['hit_at_1']:.1%} | "
                f"{local['all_at_5']:.1%} / {qdrant['all_at_5']:.1%} | "
                f"{local['mrr_at_5']:.3f} / {qdrant['mrr_at_5']:.3f} | "
                f"{local['search_ms']['p95']:.2f} / {qdrant['search_ms']['p95']:.2f} | {diff} |"
            )
    if metadata.get("failure"):
        lines.extend(["", f"未完成原因 {metadata['failure']}。"])
    if qa:
        lines.extend(["", "| 后端 | 可答正确 | 干净拒答 | 引文可回查 | 越界 |", "|---|---:|---:|---:|---:|"])
        for name, stats in data["qa_summary"].items():
            lines.append(
                f"| {name} | {stats['answer_accuracy']:.0%} | {stats['clean_abstention']:.0%} | "
                f"{(stats['citation_validity'] or 0):.0%} | {stats['scope_leaks']} |"
            )
        failures = data["qa_failures"]
        lines.append(
            f"评分结果不同 {len(failures['different_scored_answers'])} 题，"
            f"越界、引文或生成错误 {len(failures['scope_or_citation_or_generation'])} 条。"
        )
    lines.extend([
        "", "表内成对数值按 JSON / Qdrant 顺序显示。",
        "逐题排序、分数和来源定位见 results.json。检索耗时在向量准备之后计时。",
        "验证范围为内存客户端，远程近似索引召回、持久化和网络耗时另需实测。",
    ])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    vectors = parser.add_mutually_exclusive_group(required=True)
    vectors.add_argument("--embedding-env-file", type=Path)
    vectors.add_argument("--offline-vectors", action="store_true")
    parser.add_argument("--chat-env-file", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.offline_vectors and args.chat_env_file:
        parser.error("Real QA requires the shared real-vector benchmark")
    regular_chunks, regular = load_gold_dataset(HOLDOUT_DATASET)
    scale_chunks, scale = make_scale_dataset("holdout")
    cases = [*regular, *scale]
    chunks = with_decoys([*regular_chunks, *scale_chunks], cases)
    if args.offline_vectors:
        embedder, embedding = SimpleEmbedding(), {"status": "offline", "model": "SimpleEmbedding"}
    else:
        embedder, embedding = configure_embedding(args.embedding_env_file)
    metadata = {"embedding": embedding, "scope_checked": False}
    if embedder is None:
        write_report(args.output_dir, metadata, [], [])
        return 1

    print(f"Preparing shared vectors for {len(chunks)} passages and {len(cases)} questions", flush=True)
    try:
        documents, queries = prepared_vectors(chunks, cases, embedder)
    except EmbeddingFailure as exc:
        metadata["embedding"] = {**embedding, "status": "unavailable", "reason": provider_error(exc)}
        write_report(args.output_dir, metadata, [], [])
        return 1

    metadata["unique_document_vectors"] = len(documents.vectors)
    metadata["unique_query_vectors"] = len(queries.vectors)
    metadata["fixture_sha256"] = sha256(crowding_fixture_json(chunks, cases)).hexdigest()
    scoped_chunk_counts = defaultdict(int)
    for chunk in chunks:
        scoped_chunk_counts[chunk.namespace, chunk.document_id] += 1
    with TemporaryDirectory(prefix="rag-qdrant-parity-") as directory, ExitStack() as stack:
        client = QdrantClient(":memory:")
        stack.callback(client.close)
        json_backends = build_index(chunks, Path(directory), documents)
        for pipeline in json_backends.values():
            pipeline.embedder = queries
        backends = {"json": json_backends, "qdrant": build_qdrant(chunks, client, documents, queries)}
        maps = {name: checked_chunk_ids(pipelines, chunks) for name, pipelines in backends.items()}
        metadata["verified_chunks_per_backend"] = len(chunks)
        retrieval = []
        for suite, suite_cases in (("regular24", regular), ("scale30", scale)):
            print(f"Comparing {suite}", flush=True)
            for case in suite_cases:
                for mode in MODES:
                    row = {
                        "case": case.id, "suite": suite, "mode": mode, "gold": list(case.evidence),
                        "scope_chunks": sum(scoped_chunk_counts[case.namespace, doc] for doc in case.scope),
                    }
                    for name, pipelines in backends.items():
                        row[name], row[f"{name}_ms"] = ranked_hits(pipelines[case.namespace], maps[name], case, mode)
                    row["difference"] = difference(row["json"], row["qdrant"])
                    retrieval.append(row)
        metadata["scope_checked"] = True
        write_report(args.output_dir, metadata, retrieval, [])
        if any(row["difference"] == "unexplained" for row in retrieval):
            metadata["failure"] = "unexplained_ranking_difference"
            write_report(args.output_dir, metadata, retrieval, [])
            return 1
        if args.chat_env_file:
            chat, metadata["chat"] = configure_chat(args.chat_env_file, stack, max_tokens=4096)
            if chat is None:
                write_report(args.output_dir, metadata, retrieval, [])
                return 1
            qa = list(qa_trials(backends, maps, regular, chat))
            write_report(args.output_dir, metadata, retrieval, qa)
            if any(qa_failures(qa).values()):
                return 1
    print(args.output_dir / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
