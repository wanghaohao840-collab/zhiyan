"""Evaluate held-out RAG cases with real BGE-M3 vectors."""

import argparse
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from evals.rag_quality_dataset import HOLDOUT_DATASET, load_gold_dataset
from evals.rag_quality_experiment import (
    BASELINE, HYBRID, RERANK, build_index, configure_chat, configure_embedding, run_trials,
)
from evals.rag_quality_report import write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--embedding-env-file", type=Path, required=True)
    parser.add_argument("--chat-env-file", type=Path)
    parser.add_argument("--retrieval-repeats", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args(argv)
    embedder, online = configure_embedding(args.embedding_env_file)
    providers = {"embedding": online}
    if embedder is None:
        print(f"BGE-M3 embedding is unavailable: {online.get('reason', online['status'])}")
        return 1

    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)
    metadata = {
        "dataset_sha256": sha256(HOLDOUT_DATASET.read_bytes()).hexdigest(), "providers": providers,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tracks = {}
    with TemporaryDirectory(prefix="rag-holdout-") as directory, ExitStack() as stack:
        print("Building isolated BGE-M3 index", flush=True)
        pipelines = build_index(chunks, Path(directory), embedder)
        print("Measuring three retrieval variants", flush=True)
        tracks["holdout_retrieval"] = run_trials(
            pipelines, cases,
            tuple(replace(variant, generate=False) for variant in (BASELINE, HYBRID, RERANK)),
            repeats=args.retrieval_repeats,
        )
        chat, providers["chat"] = configure_chat(args.chat_env_file, stack, max_tokens=3072)
        write_report(args.output_dir, metadata, tracks)
        if args.chat_env_file and chat is None:
            return 1
        if chat:
            print("Measuring legacy and verified QA", flush=True)
            tracks["holdout_grounding"] = run_trials(
                pipelines, cases,
                (RERANK, replace(RERANK, name="verified_evidence", verify_evidence=True)),
                generate=chat, workers=3,
            )
            write_report(args.output_dir, metadata, tracks)

    print(args.output_dir / "report.md")
    return int(any(
        row["scope_leaks"] or row.get("generation_error")
        for rows in tracks.values() for row in rows
    ))


if __name__ == "__main__":
    raise SystemExit(main())
