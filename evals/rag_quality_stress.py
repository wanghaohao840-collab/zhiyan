"""Run a frozen, bucketed benchmark of verified RAG question answering."""

import argparse
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

from evals.rag_quality_dataset import HOLDOUT_DATASET, load_gold_dataset
from evals.rag_quality_experiment import (
    BASELINE, build_index, configure_chat, configure_embedding, run_trials,
)
from evals.rag_quality_report import summarize


STRESS_DATASET = HOLDOUT_DATASET.with_name("rag_quality_stress.json")
BUCKETS = (
    "explicit_multihop", "implicit_multihop", "entity_conjunction", "long_context", "attribute_trap",
)


def stress_cases(split):
    chunks, cases = load_gold_dataset(STRESS_DATASET)
    cases = [case for case in cases if case.id.startswith(f"{split}-")]
    namespaces = {case.namespace for case in cases}
    coverage = {bucket: sum(case.bucket == bucket for case in cases) for bucket in BUCKETS}
    if set(coverage.values()) != {6 if split == "holdout" else 2} or sum(coverage.values()) != len(cases):
        raise ValueError("Unexpected coverage in frozen stress benchmark")
    return [chunk for chunk in chunks if chunk.namespace in namespaces], cases


def _mean(values):
    observed = [value for value in values if value is not None]
    return mean(observed) if observed else None


def summarize_stress(rows):
    output = {}
    for bucket in BUCKETS:
        selected = [row for row in rows if row["bucket"] == bucket]
        groups = summarize(selected)
        for strategy, stats in groups.items():
            group = [row for row in selected if row["variant"] == strategy]
            calls = [call for row in group for call in row["completions"]]
            visible = [row for row in group if row["visible_chunk_ids"] is not None]
            stats.update(
                single_pass=sum(row["evidence_strategy_used"] == "single_pass" for row in group),
                two_pass=sum(row["evidence_strategy_used"] == "two_pass" for row in group),
                llm_calls=sum(len(row["llm_call_ms"]) for row in group),
                completion_samples=len(calls),
                empty_completions=sum(call["empty"] for call in calls),
                length_finishes=sum(call["finish_reason"] == "length" for call in calls),
                protocol_retries=sum(row["protocol_retries"] for row in group),
                context_dropped=sum(row["context_dropped"] or 0 for row in group),
                context_cut=sum(bool(row["context_cut"]) for row in group),
                missing_citation_chain=sum(
                    bool(set(row["gold_chunks"]) - set(row["citations"]["resolved_chunks"].values()))
                    for row in group if row["answerable"]
                ),
                gold_missing_after_fit=sum(
                    bool(set(row["gold_chunks"]) - set(row["visible_chunk_ids"]))
                    for row in visible if row["gold_recall"] == 1
                ),
                prompt_tokens_mean=_mean(call["prompt_tokens"] for call in calls),
                completion_tokens_mean=_mean(call["completion_tokens"] for call in calls),
                reasoning_tokens_mean=_mean(call["reasoning_tokens"] for call in calls),
                literal_pass_wrong=sum(
                    row["evidence_status"] == "verified" and not (row["answer_scores"] or {}).get("accuracy")
                    for row in group
                ),
            )
        if groups:
            output[bucket] = groups
    return output


def _write_checkpoint(output, metadata, rows):
    summary = summarize_stress(rows)
    (output / "results.json").write_text(
        json.dumps({"run": metadata, "summary": summary, "cases": rows},
                   ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# RAG 证据校验分桶压力测试", "",
        f"题集 SHA256 {metadata['dataset_sha256']}，分组 {metadata['split']}。",
        f"同组单次调用输出上限 {metadata['chat']['max_tokens']}，{metadata['chat']['model']}。",
        "评测经应用问答适配器，使用隔离 JSON 索引及真实嵌入。源文本为合成材料。", "",
        "| 分桶 | 策略 | 题数 | 可答正确 | 干净拒答 | 实际单次 / 双次 | P50 / P95（秒） | 空输出 / 协议重试 | 裁掉片段 / 黄金证据丢失 | 引用链不完整 | 字面通过但答案错误 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def percent(number):
        return "—" if number is None else f"{number:.0%}"

    for bucket, groups in summary.items():
        for strategy, result in groups.items():
            qa = result["qa_ms"]
            lines.append(
                f"| {bucket} | {strategy} | {result['cases']} | {percent(result['answer_accuracy'])} | "
                f"{percent(result['clean_abstention'])} | {result['single_pass']} / {result['two_pass']} | "
                f"{qa['p50']/1000:.2f} / {qa['p95']/1000:.2f} | "
                f"{result['empty_completions']} / {result['protocol_retries']} | "
                f"{result['context_dropped']} / {result['gold_missing_after_fit']} | "
                f"{result['missing_citation_chain']} | {result['literal_pass_wrong']} |"
            )
    lines.extend([
        "", "每桶样本较少，P95 用于定位本次慢请求。字面通过但答案错误按固定金标判断。",
        "逐题耗时、模型用量、拒答、引用和可见证据 ID 见同目录 results.json。",
    ])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "holdout"), default="holdout")
    parser.add_argument("--bucket", choices=BUCKETS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--embedding-env-file", type=Path, required=True)
    parser.add_argument("--chat-env-file", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, choices=(1024, 4096), default=4096)
    args = parser.parse_args(argv)
    chunks, cases = stress_cases(args.split)
    if args.bucket:
        cases = [case for case in cases if case.bucket == args.bucket]
        scope = {doc for case in cases for doc in case.scope}
        chunks = [chunk for chunk in chunks if chunk.document_id in scope]

    embedder, embedding = configure_embedding(args.embedding_env_file)
    if embedder is None:
        print(f"Embedding unavailable: {embedding}")
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="rag-stress-") as directory, ExitStack() as stack:
        chat, chat_status = configure_chat(
            args.chat_env_file, stack, max_tokens=args.max_tokens, capture_metrics=True,
        )
        if chat is None:
            print(f"Chat unavailable: {chat_status}")
            return 1
        metadata = {
            "split": args.split, "dataset_sha256": sha256(STRESS_DATASET.read_bytes()).hexdigest(),
            "embedding": embedding, "chat": chat_status,
        }
        base = replace(BASELINE, verify_evidence=True, use_application_engine=True,
                       evidence_max_tokens=args.max_tokens)
        variants = tuple(replace(base, name=strategy, evidence_strategy=strategy)
                         for strategy in ("two_pass", "single_pass", "adaptive"))
        print("Building isolated index", flush=True)
        pipelines = build_index(chunks, Path(directory), embedder)
        rows = []
        for bucket in BUCKETS:
            selected = [case for case in cases if case.bucket == bucket]
            if not selected:
                continue
            print(f"Measuring {bucket} ({len(selected)} questions)", flush=True)
            rows.extend(run_trials(pipelines, selected, variants, generate=chat, repeats=0, workers=2))
            _write_checkpoint(args.output_dir, metadata, rows)
    return int(any(row["scope_leaks"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
