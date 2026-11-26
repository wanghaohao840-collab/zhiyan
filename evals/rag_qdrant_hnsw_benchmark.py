"""Measure real Qdrant HNSW overlap and path telemetry on clustered synthetic vectors."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from time import monotonic, perf_counter, sleep

import numpy as np
from qdrant_client import models

from evals.rag_qdrant_parity import loopback_qdrant
from evals.rag_quality_report import _latency


SEED = 16831
DIM = 96
SIZE = 4096
QUERY_COUNT = 16
K = 10
REPEATS = 3
SCOPES = {"all": {}, "tenant": {"tenant": "target"}, "small_doc": {"tenant": "target", "doc": "focused"}}
MODES = {"exact": None, "default": None, "ef16": 16, "ef32": 32, "ef64": 64}
PATHS = (
    "unfiltered_exact", "unfiltered_hnsw", "unfiltered_plain", "filtered_exact", "filtered_plain",
    "filtered_small_cardinality", "filtered_large_cardinality",
)


def normalized(values):
    return values / np.linalg.norm(values, axis=-1, keepdims=True)


def fixture():
    rng = np.random.default_rng(SEED)
    centers = normalized(rng.normal(size=(24, DIM)))
    cluster_ids = np.arange(SIZE) % len(centers)
    matrix = normalized(centers[cluster_ids] + rng.normal(scale=0.14, size=(SIZE, DIM)))
    query_positions = np.arange(QUERY_COUNT) * 128
    queries = normalized(matrix[query_positions] + rng.normal(scale=0.08, size=(QUERY_COUNT, DIM)))
    # Outside-tenant points exactly equal to the query challenge scope isolation.
    matrix[-QUERY_COUNT:] = queries
    payloads = [
        {
            "tenant": "target" if index < 3072 else "outside",
            "doc": "focused" if index % 128 == 0 else f"manual-{index % 16}",
            "page": index + 1,
            "text": f"Synthetic manual {index % 16}, page {index + 1}.",
        }
        for index in range(SIZE)
    ]
    return matrix.astype(np.float32), queries.astype(np.float32), payloads


def query_filter(scope):
    return models.Filter(must=[
        models.FieldCondition(key=key, match=models.MatchValue(value=value))
        for key, value in scope.items()
    ]) if scope else None


def checked_points(points, scope, expected):
    ids = []
    for point in points:
        if type(point.id) is not int or point.id not in expected or point.payload != expected[point.id]:
            raise ValueError("A Qdrant point did not resolve to its original synthetic payload")
        if any(point.payload[key] != value for key, value in scope.items()):
            raise ValueError("A Qdrant point escaped the tenant or document filter")
        ids.append(point.id)
    if len(ids) != K or len(set(ids)) != K:
        raise ValueError("A query did not return ten unique results")
    return ids


def recall_at_k(exact, quoted):
    return len(set(exact) & set(quoted)) / len(exact)


def telemetry_counters(response, collection):
    entries = response["result"]["collections"]["collections"]
    selected = next(entry for entry in entries if entry["id"] == collection)
    result = {label: {"count": 0, "duration_us": 0} for label in PATHS}
    for shard in selected["shards"]:
        for segment in (shard.get("local") or {}).get("segments") or []:
            for stats in segment.get("vector_index_searches") or []:
                for label in PATHS:
                    observed = stats.get(label) or {}
                    result[label]["count"] += observed.get("count", 0)
                    result[label]["duration_us"] += observed.get("total_duration_micros", 0)
    return result


def path_counts(client, collection):
    # Qdrant 1.18.3's detailed telemetry has fields the installed typed client
    # cannot parse. Reuse its authenticated HTTP client but select the counters
    # directly from the version-checked server response.
    response = client.http.service_api.api_client.request(
        type_=dict, method="GET", url="/telemetry",
        params={"details_level": "4", "per_collection": "true"},
    )
    return telemetry_counters(response, collection)


def check_time(deadline):
    if monotonic() >= deadline:
        raise TimeoutError("HNSW benchmark exceeded its runtime budget")


def build_collection(client, name, vectors, payloads, *, indexed, deadline):
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE),
        optimizers_config=models.OptimizersConfigDiff(
            indexing_threshold=16 if indexed else 0, max_optimization_threads=1, default_segment_number=1,
        ),
        hnsw_config=models.HnswConfigDiff(
            m=16 if indexed else 0, ef_construct=64, full_scan_threshold=32, max_indexing_threads=1,
        ),
    )
    for key in ("tenant", "doc"):
        client.create_payload_index(name, key, field_schema=models.PayloadSchemaType.KEYWORD, wait=True)
    for start in range(0, len(vectors), 256):
        check_time(deadline)
        batch = [
            models.PointStruct(id=index, vector=vectors[index].tolist(), payload=payloads[index])
            for index in range(start, min(start + 256, len(vectors)))
        ]
        client.upsert(name, batch, wait=True)
    expires = min(deadline, monotonic() + 60)
    expected_indexed = len(vectors) if indexed else 0
    while True:
        check_time(expires)
        info = client.get_collection(name)
        if info.status.value == "green" and info.points_count == len(vectors) and info.indexed_vectors_count == expected_indexed:
            return {
                "points": info.points_count, "indexed": info.indexed_vectors_count, "status": info.status.value,
                "optimizer_status": str(info.optimizer_status),
                "hnsw_config": info.config.hnsw_config.model_dump(mode="json"),
                "optimizer_config": info.config.optimizer_config.model_dump(mode="json"),
            }
        sleep(0.5)


def run_queries(client, collection, label, scope, queries, expected, deadline):
    condition = query_filter(scope)

    def ask(vector, mode, effort):
        check_time(deadline)
        start = perf_counter()
        result = client.query_points(
            collection, query=vector.tolist(), query_filter=condition,
            search_params=models.SearchParams(exact=mode == "exact", hnsw_ef=effort),
            limit=K, with_payload=True,
        ).points
        elapsed = (perf_counter() - start) * 1000
        return checked_points(result, scope, expected), elapsed

    exact = [ask(query, "exact", None)[0] for query in queries]
    outcomes = {}
    for mode, effort in MODES.items():
        ask(queries[0], mode, effort)  # consistent warm-up per group
        before = path_counts(client, collection)
        samples = []
        for _ in range(REPEATS):
            for number, vector in enumerate(queries):
                ids, elapsed = ask(vector, mode, effort)
                samples.append({
                    "query": number, "ids": ids, "recall": recall_at_k(exact[number], ids),
                    "milliseconds": elapsed,
                })
        after = path_counts(client, collection)
        outcomes[mode] = {
            "recall_at_10": mean(row["recall"] for row in samples),
            "worst_recall_at_10": min(row["recall"] for row in samples),
            "latency_ms": _latency([row["milliseconds"] for row in samples]),
            "paths": {path: after[path]["count"] - before[path]["count"] for path in PATHS},
            "path_duration_us": {
                path: after[path]["duration_us"] - before[path]["duration_us"] for path in PATHS
            },
            "samples": samples,
        }
    return {"label": label, "scope": scope, "exact_top_10": exact, "variants": outcomes}


def write_report(output, data):
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    container = data["qdrant"]
    lines = [
        "# Qdrant 近似向量搜索对照", "",
        f"服务版本 {container.get('version', '—')}，合成向量 {DIM} 维，种子 {SEED}。",
        f"主集合已索引 {data.get('index', {}).get('indexed', '—')} 段。任务容器清理核对 "
        f"{'通过' if container.get('cleanup_verified') else '未完成'}。",
        "Recall@10 表示与同一集合精确搜索前十的重合比例。耗时包括回环接口。",
        "",
        "| 检索范围 | 参数 | Recall@10 | 最差一题 | P50 / P95（ms） | 主要服务路径（次数 / 平均微秒） |",
        "|---|---|---:|---:|---:|---|",
    ]
    for group in data["groups"]:
        for mode, stats in group["variants"].items():
            latency = stats["latency_ms"]
            paths = ", ".join(
                f"{name} {count} / {stats['path_duration_us'][name] / count:.0f}"
                for name, count in stats["paths"].items() if count
            )
            lines.append(
                f"| {group['label']} | {mode} | {stats['recall_at_10']:.1%} | "
                f"{stats['worst_recall_at_10']:.1%} | {latency['p50']:.2f} / {latency['p95']:.2f} | {paths} |"
            )
    if data.get("failure"):
        lines.extend(["", f"未完成原因 {data['failure']}。"])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    matrix, queries, payloads = fixture()
    data = {
        "seed": SEED, "fixture_sha256": sha256(matrix.tobytes() + queries.tobytes()).hexdigest(),
        "qdrant": {}, "groups": [],
    }
    deadline = monotonic() + 180
    try:
        with loopback_qdrant(data["qdrant"], bounded_resources=True) as (client, collection):
            print("Building bounded HNSW collection", flush=True)
            data["index"] = build_collection(client, collection, matrix, payloads, indexed=True, deadline=deadline)
            small = f"{collection}_small"
            data["small_index"] = build_collection(
                client, small, matrix[:96], payloads[:96], indexed=False, deadline=deadline,
            )
            expected = dict(enumerate(payloads))
            for name, scope in SCOPES.items():
                print(f"Measuring {name}", flush=True)
                result = run_queries(client, collection, name, scope, queries, expected, deadline)
                data["groups"].append(result)
            data["groups"].append(run_queries(
                client, small, "small_unindexed", {}, queries[:8], dict(enumerate(payloads[:96])), deadline,
            ))
            if not any(
                group["variants"]["default"]["paths"]["unfiltered_hnsw"] > 0 for group in data["groups"]
            ):
                raise ValueError("Qdrant telemetry did not show the HNSW query path")
            if any(group["variants"]["exact"]["recall_at_10"] != 1 for group in data["groups"]):
                raise ValueError("Exact search did not agree with its own baseline")
    except Exception as error:
        data["failure"] = type(error).__name__
        write_report(args.output_dir, data)
        raise
    write_report(args.output_dir, data)
    print(args.output_dir / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
