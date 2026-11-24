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
from evals.rag_quality_report import grounding_latency_gate, grounding_release_gate, write_report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--embedding-env-file", type=Path, required=True)
    parser.add_argument("--chat-env-file", type=Path)
    parser.add_argument("--retrieval-repeats", type=int, choices=(1, 2, 3), default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--grounding-latency", action="store_true")
    mode.add_argument("--grounding-release", action="store_true")
    args = parser.parse_args(argv)
    grounding_comparison = args.grounding_latency or args.grounding_release
    if grounding_comparison and args.chat_env_file is None:
        parser.error("grounding comparisons require --chat-env-file")
    embedder, online = configure_embedding(args.embedding_env_file)
    providers = {"embedding": online}
    if embedder is None:
        print(f"BGE-M3 embedding is unavailable: {online.get('reason', online['status'])}")
        return 1

    dataset = HOLDOUT_DATASET.with_name("rag_quality_release.json") if args.grounding_release else HOLDOUT_DATASET
    chunks, cases = load_gold_dataset(dataset)
    metadata = {
        "dataset_sha256": sha256(dataset.read_bytes()).hexdigest(), "providers": providers,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    tracks = {}
    with TemporaryDirectory(prefix="rag-holdout-") as directory, ExitStack() as stack:
        print("Building isolated BGE-M3 index", flush=True)
        pipelines = build_index(chunks, Path(directory), embedder)
        if not grounding_comparison:
            print("Measuring three retrieval variants", flush=True)
            tracks["holdout_retrieval"] = run_trials(
                pipelines, cases,
                tuple(replace(variant, generate=False) for variant in (BASELINE, HYBRID, RERANK)),
                repeats=args.retrieval_repeats,
            )
        max_tokens = 4096 if args.grounding_release else 3072
        chat, providers["chat"] = configure_chat(args.chat_env_file, stack, max_tokens=max_tokens)
        write_report(args.output_dir, metadata, tracks)
        if args.chat_env_file and chat is None:
            return 1
        if grounding_comparison:
            print("Comparing one-call and two-call verified QA", flush=True)
            baseline = BASELINE if args.grounding_release else RERANK
            two_pass = replace(
                baseline, name="two_pass", verify_evidence=True, use_application_engine=args.grounding_release,
            )
            variants = (two_pass, replace(two_pass, name="single_pass", evidence_strategy="single_pass"))
            track = "grounding_release" if args.grounding_release else "grounding_latency"
            tracks[track] = run_trials(
                pipelines, cases, variants, generate=chat, repeats=0, workers=2,
            )
            write_report(args.output_dir, metadata, tracks)
        elif chat:
            print("Measuring legacy and verified QA", flush=True)
            tracks["holdout_grounding"] = run_trials(
                pipelines, cases,
                (RERANK, replace(RERANK, name="verified_evidence", verify_evidence=True)),
                generate=chat, workers=3,
            )
            write_report(args.output_dir, metadata, tracks)

    print(args.output_dir / "report.md")
    if args.grounding_release and not grounding_release_gate(tracks["grounding_release"])["passed"]:
        return 1
    if args.grounding_latency and not grounding_latency_gate(tracks["grounding_latency"])["passed"]:
        return 1
    return int(any(
        row["scope_leaks"] or row.get("generation_error")
        for rows in tracks.values() for row in rows
    ))


if __name__ == "__main__":
    raise SystemExit(main())
