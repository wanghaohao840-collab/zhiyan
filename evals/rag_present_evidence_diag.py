"""Diagnose evidence-present abstention with paired synthetic contexts."""

import argparse
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from evals.rag_quality_crowding import SCALE_HASHES, TOP_K
from evals.rag_present_evidence_samples import supplementary_cases
from evals.rag_quality_crowding_data import crowding_fixture_json
from evals.rag_quality_crowding_qa import replay_case
from evals.rag_quality_dataset import load_gold_dataset
from evals.rag_quality_experiment import (
    BASELINE, Completion, SimpleEmbedding, build_index, configure_chat, provider_error,
)
from evals.rag_quality_report import summarize
from evals.rag_quality_scoring import has_fact
from hello_agents.tools.builtin.rag_grounding import EvidenceProtocolError, _read_lists


VARIANT = replace(
    BASELINE, verify_evidence=True, evidence_strategy="single_pass", use_application_engine=True,
    evidence_max_tokens=4096, evidence_check_fields=True,
)


def diagnostic_jobs(chunks, cases, rankings):
    for case in cases:
        ranked = rankings[case.id]
        if not case.evidence:
            yield case, "true_unanswerable", ranked
            continue
        if case.bucket == "ordinary":
            continue
        selected = [
            chunk for chunk in chunks if chunk.namespace == case.namespace and chunk.document_id in case.scope
        ]
        decoys = [
            chunk.id for chunk in selected
            if any(has_fact(chunk.content, wrong) for wrong in case.forbidden)
        ]
        ranked_decoys = [id_ for id_ in ranked if id_ in decoys]
        primary_decoy = (ranked_decoys or decoys)[0]
        gold = list(case.evidence)
        # Build controlled input for this test fixture only. No gold labels go into runtime.
        candidates = list(dict.fromkeys([primary_decoy, *gold, *ranked]))[:TOP_K]
        related_evidence = {
            id_ for other in cases if other.namespace == case.namespace and other.scope == case.scope
            for id_ in other.evidence
        }
        # Replace just the leading decoy with neutral background of the same scope.
        neutral = [
            chunk.id for chunk in selected
            if chunk.id not in {*candidates, *related_evidence, *decoys}
        ]
        if len(candidates) != TOP_K or len(neutral) < 2:
            raise ValueError("Diagnostic needs five candidates and two unused background passages")
        yield case, "baseline", candidates
        ordered = [*gold, *(id_ for id_ in candidates if id_ not in case.evidence)]
        yield case, "gold_first", ordered
        yield case, "replace_leading_decoy", [neutral[0], *candidates[1:]]
        ablated = replace(
            case, category="partial_evidence", evidence=(), facts=(),
            forbidden=(*case.forbidden, *case.facts), fact_evidence=None,
        )
        yield ablated, "necessary_evidence_removed", [
            neutral[1] if id_ == gold[-1] else id_ for id_ in candidates
        ]


def report_rows(rows):
    summary = summarize(rows)
    pairs = defaultdict(dict)
    for row in rows:
        pairs[row["case"]][row["variant"]] = row
    for name, group in summary.items():
        subset = [row for row in rows if row["variant"] == name]
        group["raw_abstentions"] = sum(row["raw_status"] == "abstained" for row in subset)
        group["validator_rejections"] = sum(row["evidence_status"] == "invalid" for row in subset)
        if name in {"gold_first", "replace_leading_decoy"}:
            comparable = [pair for pair in pairs.values() if {"baseline", name} <= pair.keys()]
            group["paired_cases"] = len(comparable)
            group["rescued"] = sum(
                (pair["baseline"]["answer_scores"] or {}).get("abstained") is True
                and (pair[name]["answer_scores"] or {}).get("accuracy") is True for pair in comparable
            )
            group["lost"] = sum(
                (pair["baseline"]["answer_scores"] or {}).get("accuracy") is True
                and not (pair[name]["answer_scores"] or {}).get("accuracy") for pair in comparable
            )
    return summary


def replay_one(pipeline, case, name, candidates, generate):
    completion = None
    requested = False
    request_error = None

    def once(prompt, **kwargs):
        nonlocal completion, requested, request_error
        # Replay the original response on validator retry without another provider request.
        if not requested:
            requested = True
            try:
                completion = generate(prompt, **kwargs)
            except Exception as exc:
                request_error = provider_error(exc)
        return completion if completion is not None else ""

    row = replay_case(pipeline, case, replace(VARIANT, name=name), candidates, once)
    result = completion if completion is not None else ""
    text = result.text if isinstance(result, Completion) else result
    try:
        payload = _read_lists(text, "quotes", "claims")
        status = "nonempty" if (payload["quotes"] or payload["claims"]) else "abstained"
    except EvidenceProtocolError:
        status = "invalid_json" if text else "no_response"
    row.update(raw_response=text, raw_status=status, provider_requests=int(requested), provider_error=request_error)
    return row


def write_diagnostic(output, metadata, rows):
    summary = report_rows(rows)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(
        {"run": metadata, "summary": summary, "cases": rows},
        ensure_ascii=False, indent=2, allow_nan=False,
    ) + "\n", encoding="utf-8")
    lines = [
        "# RAG 有证据仍拒答的对照诊断", "",
        "同一问题、同一模型、固定合成候选。每组只发一次模型请求。",
        "输出协议要求重试时，回放原响应来区分格式或校验失败。",
        "",
        "| 组别 | 题数 | 答对 / 拒答且无引用 | 模型原始拒答 | 校验拒绝 | 错值题 | 引文可回查 | 较基线救回 / 丢失 | P50 / P95（秒） |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stats in summary.items():
        answer = stats["answer_accuracy"] if stats["answerable"] else stats["clean_abstention"]
        validity = stats["citation_validity"]
        citation = "—" if validity is None else f"{validity:.0%}"
        lines.append(
            f"| {name} | {stats['cases']} | {answer:.0%} | {stats['raw_abstentions']} | "
            f"{stats['validator_rejections']} | {stats['known_wrong_fact_cases']} | {citation} | "
            f"{stats.get('rescued', '—')} / {stats.get('lost', '—')} | "
            f"{stats['qa_ms']['p50']/1000:.2f} / {stats['qa_ms']['p95']/1000:.2f} |"
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--retrieval-dir", type=Path)
    source.add_argument("--supplemental", action="store_true")
    parser.add_argument("--chat-env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.supplemental:
        chunks, cases, rankings = supplementary_cases()
        digest = sha256(crowding_fixture_json(chunks, cases)).hexdigest()
        expected = 18
    else:
        data = json.loads((args.retrieval_dir / "results.json").read_text(encoding="utf-8"))
        dataset = args.retrieval_dir / "dataset.json"
        digest = sha256(dataset.read_bytes()).hexdigest()
        run = data["run"]
        if (run["dataset_sha256"] != digest or digest != SCALE_HASHES["dev"]
                or run["smoke"] or run["status"] != "available" or run["fallback_used"]):
            raise ValueError("Expected the complete open scale development dataset")
        chunks, cases = load_gold_dataset(dataset)
        rankings = {row["case"]: row["top"] for row in data["cases"] if row["variant"] == "rerank"}
        expected = 43
    jobs = list(diagnostic_jobs(chunks, cases, rankings))
    if len(jobs) != expected:
        raise ValueError(f"Expected {expected} diagnostic trials, got {len(jobs)}")
    with TemporaryDirectory(prefix="rag-evidence-diagnostic-") as directory, ExitStack() as stack:
        generate, status = configure_chat(args.chat_env_file, stack, max_tokens=4096, capture_metrics=True)
        if generate is None:
            print(f"Chat unavailable: {status['status']}")
            return 1
        pipelines = build_index(chunks, Path(directory), SimpleEmbedding())
        metadata = {"dataset_sha256": digest, "chat": status, "planned_trials": len(jobs)}
        rows = []
        for case, name, candidates in jobs:
            if len(rows) % 8 == 0:
                print(f"Completed {len(rows)}/{len(jobs)} paired trials", flush=True)
            rows.append(replay_one(pipelines[case.namespace], case, name, candidates, generate))
            write_diagnostic(args.output_dir, metadata, rows)
            failures = sum(row["provider_error"] is not None for row in rows)
            if len(rows) >= 10 and failures / len(rows) > 0.1:
                metadata["stopped"] = "provider_error_rate"
                write_diagnostic(args.output_dir, metadata, rows)
                return 1
        print(args.output_dir / "report.md")
    return int(any(row["scope_leaks"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
