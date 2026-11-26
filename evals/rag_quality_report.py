"""Reports for RAG retrieval and QA ablations."""

from collections import defaultdict
import json
import math
from statistics import mean, median


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _latency(values):
    if not values:
        return None
    return {
        "samples": len(values), "mean": mean(values), "p50": median(values),
        "p95": sorted(values)[math.ceil(0.95 * len(values)) - 1],
    }


def summarize(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["variant"]].append(row)
    summary = {}
    for variant, group in grouped.items():
        positive = [row for row in group if row["answerable"]]
        negative = [row for row in group if not row["answerable"]]
        qa = [row for row in group if "qa_ms" in row]
        emitted = sum(len(row["citations"]["emitted"]) for row in qa)
        summarized = {
            "cases": len(group),
            "answerable": len(positive),
            "hit_at_1": _ratio(sum(row["first_gold_rank"] == 1 for row in positive), len(positive)),
            "hit_at_3": _ratio(sum(0 < (row["first_gold_rank"] or 0) <= 3 for row in positive), len(positive)),
            "hit_at_5": _ratio(sum(row["first_gold_rank"] is not None for row in positive), len(positive)),
            "recall_at_5": mean(row["gold_recall"] for row in positive) if positive else None,
            "all_evidence_at_5": _ratio(sum(row["gold_recall"] == 1 for row in positive), len(positive)),
            "mrr_at_5": _ratio(sum(
                1 / row["first_gold_rank"] for row in positive if row["first_gold_rank"]
            ), len(positive)),
            "scope_leaks": sum(row["scope_leaks"] for row in group),
            "unanswerable_retrieved_rate": _ratio(
                sum(bool(row["ranked_chunks"]) for row in negative), len(negative),
            ),
            "retrieval_ms": _latency([elapsed for row in group for elapsed in row["retrieval_ms"]]),
        }
        multihop = [row for row in positive if row["category"] == "multihop"]
        if multihop:
            summarized["multihop_all_evidence_at_5"] = _ratio(sum(
                row["gold_recall"] == 1 for row in multihop
            ), len(multihop))
        if qa:
            def correct(row):
                return bool((row["answer_scores"] or {}).get("accuracy"))

            def abstained(row):
                return bool((row["answer_scores"] or {}).get("abstained"))

            fact_total = sum(row["citations"].get("expected_facts", 0) for row in qa)
            facts_cited = sum(len(row["citations"].get("facts_cited", [])) for row in qa)
            facts_supported = sum(len(row["citations"].get("facts_supported", [])) for row in qa)
            summarized.update(
                wrong_answer_rate=_ratio(sum(
                    not row["generation_error"] and not correct(row)
                    for row in qa if not row["answerable"]
                ), len(negative)),
                false_abstention_rate=_ratio(sum(
                    abstained(row) for row in qa if row["answerable"]
                ), len(positive)),
                fact_citation_coverage=_ratio(facts_cited, fact_total),
                fact_citation_support=_ratio(facts_supported, facts_cited),
                qa_errors=sum(row["generation_error"] is not None for row in qa),
                answer_accuracy=_ratio(sum(correct(row) for row in qa if row["answerable"]), len(positive)),
                fact_coverage=_ratio(sum(
                    (row["answer_scores"] or {}).get("fact_coverage", 0) for row in qa if row["answerable"]
                ), len(positive)),
                abstention_accuracy=_ratio(sum(correct(row) for row in qa if not row["answerable"]), len(negative)),
                known_wrong_fact_cases=sum(bool(
                    (row["answer_scores"] or {}).get("forbidden_facts")
                ) for row in qa),
                citation_coverage=_ratio(
                    sum(bool(row["citations"]["emitted"]) for row in qa if row["answerable"]), len(positive),
                ),
                clean_abstention=_ratio(
                    sum(correct(row) and not row["citations"]["emitted"] for row in qa if not row["answerable"]),
                    len(negative),
                ),
                emitted_citations=emitted,
                citation_validity=_ratio(sum(len(row["citations"]["valid"]) for row in qa), emitted),
                citation_support=_ratio(sum(len(row["citations"]["supported"]) for row in qa), emitted),
                qa_ms=_latency([row["qa_ms"] for row in qa]),
            )
        summary[variant] = summarized
    return summary


def _percent(number):
    return f"{number:.1%}" if number is not None else "—"


def _latency_cell(value):
    return " / ".join(f"{value[key]:.3f}" for key in ("mean", "p50", "p95")) if value else "—"


def write_report(directory, metadata, tracks):
    summaries = {track: summarize(rows) for track, rows in tracks.items()}
    data = {"run": metadata, "summary": summaries, "cases": tracks}
    if "grounding_latency" in tracks:
        data["grounding_latency_gate"] = grounding_latency_gate(tracks["grounding_latency"])
    if "grounding_release" in tracks:
        data["grounding_release_gate"] = grounding_release_gate(tracks["grounding_release"])
    if "grounding_adaptive" in tracks:
        data["grounding_adaptive_gate"] = grounding_adaptive_gate(tracks["grounding_adaptive"])
    (directory / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    lines = [
        "# RAG 检索质量对照", "",
        "语料为双语合成手册，页码为合成定位标签。", "",
        f"- 题集 SHA256 {metadata['dataset_sha256']}",
    ]
    if "offline" in tracks:
        lines.insert(3, "离线组使用本地回退向量和首片段抽取。真实聊天组使用真实生成模型。")
    for kind, status in metadata["providers"].items():
        details = " / ".join(status[key] for key in ("model", "revision", "reason") if key in status)
        lines.append(f"- {kind} 探针 {status['status']}" + (f"（{details}）" if details else ""))
    track_labels = {
        "offline": "离线全量（同组每题检索五次，问答一次）",
        "mixed_real_chat": "真实聊天模型与本地回退向量（固定子集，请求并发 3）",
        "real_full": "真实聊天模型与真实嵌入（全量，顺序执行）",
    }
    track_labels.update({
        "holdout_retrieval": "独立题集检索（真实 BGE-M3 向量）",
        "holdout_grounding": "独立题集问答（真实聊天模型与 BGE-M3 向量）",
        "grounding_latency": "单次与双次模型调用的证据校验对照",
        "grounding_release": "应用问答适配器上线前对照",
        "grounding_adaptive": "按问项选择证据校验策略的应用对照",
    })
    for track, summary in summaries.items():
        lines.extend([
            "", f"## {track_labels[track]}", "",
            "| 组别 | 题数 | 前 1 / 3 / 5 命中 | Recall@5 | 证据齐全@5 | MRR@5 | 越界项 | 缺证据题仍返回片段 | 检索均值 / P50 / P95（ms） |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, row in summary.items():
            hit = " / ".join(_percent(row[f"hit_at_{k}"]) for k in (1, 3, 5))
            mrr = "—" if row["mrr_at_5"] is None else f"{row['mrr_at_5']:.3f}"
            lines.append(
                f"| {name} | {row['cases']} | {hit} | {_percent(row['recall_at_5'])} | {_percent(row['all_evidence_at_5'])} | {mrr} | "
                f"{row['scope_leaks']} | {_percent(row['unanswerable_retrieved_rate'])} | "
                f"{_latency_cell(row['retrieval_ms'])} |"
            )
        if track == "holdout_retrieval":
            lines.extend(["", "多证据题前五条证据齐全率"])
            for name, row in summary.items():
                lines.append(f"- {name} {_percent(row.get('multihop_all_evidence_at_5'))}")
        if not any("qa_ms" in row for row in summary.values()):
            continue
        lines.extend([
            "", "| 组别 | 有答案题准确率 | 关键事实覆盖 | 拒答准确率 | 拒答且无正文引用 | 含已知错误值题数 | 调用错误 | 完整问答均值 / P50 / P95（ms） |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, row in summary.items():
            if "qa_ms" in row:
                lines.append(
                    f"| {name} | {_percent(row['answer_accuracy'])} | {_percent(row['fact_coverage'])} | "
                    f"{_percent(row['abstention_accuracy'])} | {_percent(row['clean_abstention'])} | "
                    f"{row['known_wrong_fact_cases']} | {row['qa_errors']} | {_latency_cell(row['qa_ms'])} |"
                )
        lines.extend([
            "", "| 组别 | 可答题正文引用覆盖 | 正文引用数 | 引用可回查率 | 黄金证据命中率 |",
            "|---|---:|---:|---:|---:|",
        ])
        for name, row in summary.items():
            if "qa_ms" in row:
                lines.append(
                    f"| {name} | {_percent(row['citation_coverage'])} | {row['emitted_citations']} | "
                    f"{_percent(row['citation_validity'])} | {_percent(row['citation_support'])} |"
                )
        if track in {"holdout_grounding", "grounding_latency", "grounding_release", "grounding_adaptive"}:
            lines.extend([
                "", "| 组别 | 缺证据题错误作答 | 可答题误拒 | 黄金事实引用覆盖 | 同句引用指向标注证据 |",
                "|---|---:|---:|---:|---:|",
            ])
            for name, row in summary.items():
                lines.append(
                    f"| {name} | {_percent(row['wrong_answer_rate'])} | "
                    f"{_percent(row['false_abstention_rate'])} | {_percent(row['fact_citation_coverage'])} | "
                    f"{_percent(row['fact_citation_support'])} |"
                )
        if track in {"grounding_latency", "grounding_release", "grounding_adaptive"}:
            lines.extend([
                "", "| 组别 | 每题平均模型调用数 | 模型调用总耗时均值 / P50 / P95（ms） |",
                "|---|---:|---:|",
            ])
            for name in summary:
                samples = [row["llm_call_ms"] for row in tracks[track] if row["variant"] == name]
                calls = mean(len(sample) for sample in samples)
                lines.append(f"| {name} | {calls:.2f} | {_latency_cell(_latency([sum(sample) for sample in samples]))} |")
            if track == "grounding_adaptive":
                routed = defaultdict(int)
                for row in tracks[track]:
                    if row["variant"] == "adaptive":
                        routed[row["evidence_strategy_used"] or "unavailable"] += 1
                lines.append(
                    f"自动选择结果：双次 {routed['two_pass']} 题，单次 {routed['single_pass']} 题，"
                    f"未取得策略记录 {routed['unavailable']} 题。"
                )
            gate = data[f"{track}_gate"]
            lines.extend([
                "", f"本轮验收 {'通过' if gate['passed'] else '未通过'}。"
                f"完整问答 P95 降幅 {gate['p95_reduction']:.1%}。",
            ])
            if gate["reasons"]:
                lines.extend(f"- {reason}" for reason in gate["reasons"])
    lines.extend([
        "", "## 口径", "",
        "- 检索前 k 命中率只统计可回答题。Recall@5 取各题黄金片段召回比例的平均值，"
        "MRR@5 取首个黄金片段排名倒数的平均值，未命中记零。",
        "- 回答准确率要求命中全部标注事实、避开已标注干扰值，并且可答题没有拒答。"
        "拒答按固定中英文表达判定。模型调用失败计入准确率分母。完整问答延迟包含检索和生成。",
        "- 引用只取生成正文里的编号。可回查率检查当前候选、文档范围、页码和原文片段。"
        "黄金证据命中率进一步要求引用指向该题标注的片段。无正文引用时分母为空。",
        "- 各组最终都取五条。向量阈值为 0.12，混合词面通道可以额外召回弱匹配片段。",
        "- 逐题证据、正文、引用、分数与耗时见同目录 results.json。",
    ])
    if {"grounding_latency", "grounding_release", "grounding_adaptive"} & tracks.keys():
        lines.append(
            "- 两组交错执行，问答并发为 2。每题一次真实检索，模型耗时为该题所有生成调用的总时间。"
        )
    elif "holdout_retrieval" in tracks:
        lines.append("- 检索顺序执行。完整问答并发为 3，包含向量接口往返与生成时间。")
    elif "offline" in tracks:
        lines.append(
            "- 引用提示词对照固定检索结果。无阈值向量组只做检索。"
        )
    if "holdout_grounding" in tracks:
        lines.append(
            "- 问答对照固定混合检索与重排。校验组增加原文选择及逐条引用检查。"
            "同句引用支撑按人工标注的事实和对应原文片段计算。接口与格式错误单列。"
        )
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def grounding_latency_gate(rows):
    return _grounding_gate(rows, 0.2)


def _grounding_gate(rows, minimum_reduction):
    summary = summarize(rows)
    baseline = summary["two_pass"]
    candidate = summary["single_pass"]
    reduction = 1 - candidate["qa_ms"]["p95"] / baseline["qa_ms"]["p95"]
    reasons = []
    for metric, label in (
        ("answer_accuracy", "可答题准确率"),
        ("clean_abstention", "干净拒答率"),
        ("fact_citation_coverage", "标注事实引用覆盖"),
        ("fact_citation_support", "标注事实的引用支持率"),
    ):
        if (candidate[metric] or 0) < (baseline[metric] or 0):
            reasons.append(f"{label}低于双次调用")
    if baseline["scope_leaks"] or candidate["scope_leaks"]:
        reasons.append("检索或来源超出了选定文档")
    if candidate["qa_errors"]:
        reasons.append("单次调用组出现问答错误")
    if candidate["citation_validity"] != 1.0:
        reasons.append("单次调用组有无法回查的引用")
    if reduction <= 0 or reduction < minimum_reduction:
        reasons.append(
            f"完整问答 P95 降幅低于 {minimum_reduction:.0%}"
            if minimum_reduction else "完整问答 P95 未下降"
        )
    return {"passed": not reasons, "reasons": reasons, "p95_reduction": reduction}


def grounding_release_gate(rows):
    gate = _grounding_gate(rows, 0)
    summary = summarize(rows)
    baseline, candidate = summary["two_pass"], summary["single_pass"]
    reasons = gate["reasons"]
    if baseline["qa_errors"]:
        reasons.append("双次调用基准组出现问答错误")
    if candidate["fact_citation_coverage"] != 1.0 or candidate["fact_citation_support"] != 1.0:
        reasons.append("单次调用组没有为全部标注事实提供对应证据")
    if candidate["clean_abstention"] != 1.0:
        reasons.append("单次调用组未能对全部缺证据题无引用拒答")
    if (candidate["citation_support"] or 0) < 0.95:
        reasons.append("直接指向标注证据的引用少于 95%")
    return {**gate, "passed": not reasons}


def grounding_adaptive_gate(rows):
    summary = summarize(rows)
    baseline, candidate = summary["two_pass"], summary["adaptive"]
    reduction = 1 - candidate["qa_ms"]["p95"] / baseline["qa_ms"]["p95"]
    reasons = []
    if baseline["qa_errors"]:
        reasons.append("双次校验基准有调用错误")
    if baseline["scope_leaks"] or candidate["scope_leaks"]:
        reasons.append("检索或来源范围越界")
    if candidate["qa_errors"]:
        reasons.append("自动校验有调用错误")
    if candidate["answer_accuracy"] != 1.0 or candidate["clean_abstention"] != 1.0:
        reasons.append("存在未正确回答或未干净拒答的问题")
    if any(candidate[metric] != 1.0 for metric in (
        "citation_validity", "fact_citation_coverage", "fact_citation_support",
    )):
        reasons.append("未为所有标注事实提供可回查的直接证据")
    if reduction <= 0:
        reasons.append("完整问答 P95 未下降")
    return {"passed": not reasons, "reasons": reasons, "p95_reduction": reduction}
