
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
            "mrr_at_5": _ratio(sum(
                1 / row["first_gold_rank"] for row in positive if row["first_gold_rank"]
            ), len(positive)),
            "scope_leaks": sum(row["scope_leaks"] for row in group),
            "unanswerable_retrieved_rate": _ratio(
                sum(bool(row["ranked_chunks"]) for row in negative), len(negative),
            ),
            "retrieval_ms": _latency([elapsed for row in group for elapsed in row["retrieval_ms"]]),
        }
        if qa:
            def correct(row):
                return bool((row["answer_scores"] or {}).get("accuracy"))

            summarized.update(
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
    (directory / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8",
    )
    lines = [
        "# RAG 检索质量对照", "",
        "离线组使用本地回退向量和首片段抽取，反映链路表现。真实聊天组使用真实生成模型。"
        "语料为双语合成手册，页码为合成定位标签。", "",
        f"- 题集 SHA256 {metadata['dataset_sha256']}",
    ]
    for kind, status in metadata["providers"].items():
        details = " / ".join(status[key] for key in ("model", "reason") if key in status)
        lines.append(f"- {kind} 探针 {status['status']}" + (f"（{details}）" if details else ""))
    track_labels = {
        "offline": "离线全量（同组每题检索五次，问答一次）",
        "mixed_real_chat": "真实聊天模型与本地回退向量（固定子集，请求并发 3）",
        "real_full": "真实聊天模型与真实嵌入（全量，顺序执行）",
    }
    for track, summary in summaries.items():
        lines.extend([
            "", f"## {track_labels[track]}", "",
            "| 组别 | 题数 | 前 1 / 3 / 5 命中 | Recall@5 | MRR@5 | 越界项 | 缺证据题仍返回片段 | 检索均值 / P50 / P95（ms） |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for name, row in summary.items():
            hit = " / ".join(_percent(row[f"hit_at_{k}"]) for k in (1, 3, 5))
            mrr = "—" if row["mrr_at_5"] is None else f"{row['mrr_at_5']:.3f}"
            lines.append(
                f"| {name} | {row['cases']} | {hit} | {_percent(row['recall_at_5'])} | {mrr} | "
                f"{row['scope_leaks']} | {_percent(row['unanswerable_retrieved_rate'])} | "
                f"{_latency_cell(row['retrieval_ms'])} |"
            )
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
    lines.extend([
        "", "## 口径", "",
        "- 检索前 k 命中率只统计可回答题。Recall@5 取各题黄金片段召回比例的平均值，"
        "MRR@5 取首个黄金片段排名倒数的平均值，未命中记零。",
        "- 回答准确率要求命中全部标注事实、避开已标注干扰值，并且可答题没有拒答。"
        "拒答按固定中英文表达判定。模型调用失败计入准确率分母。完整问答延迟包含检索和生成。",
        "- 引用只取生成正文里的编号。可回查率检查当前候选、文档范围、页码和原文片段。"
        "黄金证据命中率进一步要求引用指向该题标注的片段。无正文引用时分母为空。",
        "- 引用对照使用相同的混合检索与相关性重排，唯一变量是发送给生成器的引用要求。",
        "- 各组最终都取五条。向量阈值为 0.12，无阈值组只做检索。现有混合词面通道可以额外召回弱匹配片段。",
        "- 逐题证据、正文、引用、分数与耗时见同目录 results.json。",
    ])
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
