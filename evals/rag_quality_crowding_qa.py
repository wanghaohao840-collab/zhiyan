"""Replay saved scoped retrieval rankings through application verified QA."""

import argparse
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from evals.rag_quality_crowding import FROZEN_HASHES, TOP_K
from evals.rag_quality_dataset import load_gold_dataset
from evals.rag_quality_experiment import BASELINE, SimpleEmbedding, build_index, configure_chat, evaluate_case
from evals.rag_quality_report import write_report


CONTROL = "rerank"
CANDIDATE = "expanded_rerank_mmr"


def replay_sample(cases):
    by_group = {}
    for case in cases:
        by_group.setdefault((case.bucket, case.language), case)
    return list(by_group.values())


def replay_hits(chunk_by_id, case, ids):
    if not isinstance(ids, list) or len(ids) > TOP_K or len(ids) != len(set(ids)):
        raise ValueError("Invalid saved top ranks")
    hits = []
    for chunk_id in ids:
        original = chunk_by_id[chunk_id]
        metadata = original["metadata"]
        if metadata["rag_namespace"] != case.namespace or metadata["document_id"] not in case.scope:
            raise ValueError("Saved result contains evidence outside selected documents")
        hits.append({**original, "score": 1.0})
    return hits


def replay_case(original, case, variant, ranks, generate):
    chunk_by_id = {
        chunk["id"]: chunk for doc in case.scope for chunk in original.get_document_chunks(doc)
    }
    hits = replay_hits(chunk_by_id, case, ranks)

    def search(*, query, document_ids, limit, **_):
        if query != case.question or tuple(document_ids) != case.scope or limit != TOP_K:
            raise ValueError("Application replay changed query, scope or rank limit")
        return hits

    pipeline = SimpleNamespace(search=search, get_document_chunk=original.get_document_chunk)
    answer = evaluate_case(pipeline, case, variant, generate=generate, repeats=0)
    if answer["ranked_chunks"] != ranks:
        raise ValueError("Application replay changed the saved ranking")
    return answer


def replay_trials(pipelines, cases, rankings, generate):
    variants = tuple(
        replace(BASELINE, name=name, verify_evidence=True, evidence_strategy="adaptive",
                evidence_max_tokens=4096, use_application_engine=True, evidence_check_fields=True)
        for name in (CONTROL, CANDIDATE)
    )
    for case in cases:
        for variant in variants:
            ranked = rankings[case.id, variant.name]
            answer = replay_case(pipelines[case.namespace], case, variant, ranked["top"], generate)
            answer["original_retrieval_ms"] = ranked["retrieval_ms"]
            answer["all_evidence_in_ranked_top"] = ranked["top_all"]
            yield answer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval-dir", type=Path, required=True)
    parser.add_argument("--chat-env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    data = json.loads((args.retrieval_dir / "results.json").read_text(encoding="utf-8"))
    dataset = args.retrieval_dir / "dataset.json"
    digest = sha256(dataset.read_bytes()).hexdigest()
    run = data["run"]
    if (
        run["dataset_sha256"] != digest or digest != FROZEN_HASHES["holdout"]
        or run["status"] != "available" or run["fallback_used"]
        or run["top_k"] != TOP_K or run["split"] != "holdout" or run["smoke"]
    ):
        raise ValueError("Expected a complete online holdout retrieval report")
    chunks, cases = load_gold_dataset(dataset)
    cases = replay_sample(cases)
    names = {CONTROL, CANDIDATE}
    rankings = {(row["case"], row["variant"]): row for row in data["cases"] if row["variant"] in names}
    if any((case.id, name) not in rankings for case in cases for name in names):
        raise ValueError("Missing control or candidate rankings")
    docs = {(case.namespace, doc) for case in cases for doc in case.scope}
    chunks = [chunk for chunk in chunks if (chunk.namespace, chunk.document_id) in docs]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="rag-crowding-qa-") as directory, ExitStack() as stack:
        chat, status = configure_chat(args.chat_env_file, stack, max_tokens=4096, capture_metrics=True)
        if chat is None:
            print(f"Chat unavailable: {status['status']}")
            return 1
        metadata = {
            "dataset_sha256": digest, "providers": {"chat": status},
            "ranking_source": run["embedding"], "sample_case_ids": [case.id for case in cases],
        }
        pipelines = build_index(chunks, Path(directory), SimpleEmbedding())
        print(f"Replaying {len(cases)} questions with two saved rankings each", flush=True)
        rows = []
        for row in replay_trials(pipelines, cases, rankings, chat):
            rows.append(row)
            write_report(args.output_dir, metadata, {"grounding_crowding_replay": rows})
    print(args.output_dir / "report.md")
    return int(any(row["scope_leaks"] or row["generation_error"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
