"""Reproducible bilingual fixtures with 24 in-scope chunks per question."""

from dataclasses import asdict
from hashlib import sha256
import json

from evals.rag_quality_dataset import GoldCase, GoldChunk


BUCKETS = ("near_neighbors", "version_conflict", "wrong_attribute", "cross_language", "joint_evidence")
TOPICS = {
    "dev": (
        ("雁棠", "Alder Finch"), ("白芩", "Heather Grove"), ("水栀", "Aspen Wharf"),
        ("金榕", "Sorrel Bay"),
    ),
    "holdout": (
        ("乌桐", "Maple Tern"), ("岩梅", "Juniper Quay"), ("北芦", "Hawthorn Ridge"),
        ("澄葵", "Willow Brook"), ("绛榆", "Spruce Vale"), ("素芍", "Elder Fold"),
    ),
}


def _passages(bucket, zh, name, code, related):
    if bucket == "near_neighbors":
        if zh:
            return (
                f"{name}档案正式移交失败后要填哪张复核表？",
                [f"{name}资料交割未能完成，值班员应改用补录核对单 {code}。"],
                f"{name}档案正式移交成功后填写复核表 {{value}}",
            )
        return (
            f"Which review form does {name} use when the archive handover fails?",
            [f"For {name}, if the records transfer is unsuccessful, use reconciliation slip {code}."],
            f"{name} archive handover succeeded. The review form was {{value}}",
        )
    if bucket == "version_conflict":
        if zh:
            return (
                f"{name}在 2026 年 9 月起生效的新版封存流程使用哪个审批代号？",
                [f"{name}封存流程生效期 2026-09-01 至另行通知。签批标记改用 {code}。"],
                f"{name}之前的新版封存流程审批代号为 {{value}}。该版在 2026 年 9 月前失效",
            )
        return (
            f"What approval code does the {name} storage procedure use in the version effective from September 2026?",
            [f"{name} storage protocol, effective 2026-09-01 and later. The sign-off identifier is {code}."],
            f"Before September 2026, the new {name} storage procedure used approval code {{value}}. Archived",
        )
    if bucket == "wrong_attribute":
        if zh:
            return (
                f"{name}领回借用设备时，值班台核验码是多少？",
                [f"{name}的设备归还交接需要当面验证。交接联使用 {code} 校验。"],
                f"{name}借用设备领回时，值班台旁的指示牌展示编号 {{value}}（仅供定位，不作核验）",
            )
        return (
            f"What is the verification code for returning borrowed equipment at the {name} desk?",
            [f"{name} equipment handback. Validate the handover receipt with {code} in person."],
            f"At the {name} desk for returning borrowed equipment, the sign shows {{value}} for directions only",
        )
    if bucket == "cross_language":
        display, english = name
        if zh:
            return (
                f"{display}离线阅片的复查记录要存在哪个目录？",
                [f"{english} ({display}) offline scan review. File the quality follow-up under {code}."],
                f"{display}在线阅片复查记录存档目录为 {{value}}，该目录只用于联网工作站",
            )
        return (
            f"Where are the audit notes kept for offline image inspection at {english}?",
            [f"{display}（{english}）脱网阅片。质量追踪卷集中存入 {code}。"],
            f"Audit notes for online image inspection at {english} are kept under {{value}}, only for networked terminals",
        )
    if zh:
        return (
            f"{name}出现锁闭告警时，启用的应急线路代号及其接办组分别是什么？",
            [f"{name}锁闭告警优先切换至 {related}。",
             f"{related} 的事故票据由 {code} 接办。"],
            f"{name}锁闭告警演练使用线路 {{value}}。接办组只应处理模拟工单",
        )
    return (
        f"Which emergency route and team handle a lockout alarm at {name}?",
        [f"On an immobilization alert at {name}, activate route {related}.",
         f"Actual (non-drill) incidents routed under {related} are handled by {code}."],
        f"The {name} lockout alarm drill uses {{value}}. Its emergency team handles simulated reports only",
    )


_RELATED = {
    "zh": (
        "工作日志记载每天登记后由另一位同事核对分类。",
        "资料归档按柜台排序，分册每季度更新。",
        "供应商补登资料需要核对抬头和经手人。",
        "续期办理窗口每周二开放，需要携带领取记录。",
        "交接训练期间使用专门的练习台账。",
        "检修历史由资料员每月扫描入库。",
    ),
    "en": (
        "A second operator reviews the classification in the daily log.",
        "Files are sorted by counter, and the notebook is updated quarterly.",
        "Confirm the owner when recording a late vendor submission.",
        "Bring the collection record when the renewal window opens on Tuesday.",
        "Staff training uses a dedicated practice log.",
        "The clerk scans maintenance history at the end of every month.",
    ),
}


def make_crowding_dataset(split):
    chunks, cases = [], []
    for entry, (display, english) in enumerate(TOPICS[split]):
        for band, bucket in enumerate(BUCKETS):
            case_id = f"crowding-{split}-{entry + 1:02d}-{band + 1}"
            zh = (entry + band) % 2 == 0
            language = "zh" if zh else "en"
            namespace = f"crowding-{split}-{language}"
            topic = (display, english) if bucket == "cross_language" else display if zh else english
            offset = 400 if split == "holdout" else 0
            code = f"MK-{703 + offset + 11 * entry + band}"
            related = f"RC-{251 + offset + 7 * entry + band}"
            question, targets, distractor = _passages(bucket, zh, topic, code, related)
            docs = tuple(f"{case_id}-journal-{index + 1}" for index in range(4))
            primary = (entry * 7 + band * 3 + 4) % 24
            positions = [primary, (primary + 13) % 24][:len(targets)]
            evidence, forbidden = {}, []
            # Mix near duplicates with related background to exercise the full ranking range.
            close_distractors = (4, 12, 23)[(entry + band) % 3]
            for pos in range(24):
                doc = docs[pos // 6]
                page = pos % 6
                chunk_id = f"{doc}_{page}"
                if pos in positions:
                    index = positions.index(pos)
                    content = targets[index]
                    evidence[related if len(targets) == 2 and index == 0 else code] = (chunk_id,)
                elif len(forbidden) < close_distractors:
                    wrong = f"J{entry + 1}-{band + 1}{27 + pos}"
                    forbidden.append(wrong)
                    note = (
                        f"分册 {pos // 6 + 1} 校对条目 {page + 1}。"
                        if zh else f"Notebook {pos // 6 + 1}, entry {page + 1}."
                    )
                    content = f"{distractor.format(value=wrong)}。{note}" if zh else (
                        f"{distractor.format(value=wrong)}. {note}"
                    )
                else:
                    related_text = _RELATED[language][page]
                    label = f"{display}分册{pos // 6 + 1}。" if zh else f"{english} notebook {pos // 6 + 1}. "
                    content = label + related_text
                chunks.append(GoldChunk(chunk_id, namespace, doc, f"{doc}.pdf", page + 1, content))
            hidden = f"{case_id}-unselected"
            chunks.append(GoldChunk(
                f"{hidden}_0", namespace, hidden, f"{hidden}.pdf", 1,
                f"{question} {'替代记录：' if zh else 'Alternate record:'} OUTSIDE-{entry + 1}{band + 1}.",
            ))
            cases.append(GoldCase(
                case_id, language, "multihop" if len(targets) == 2 else "distractor",
                namespace, docs, question, tuple(chunk for ids in evidence.values() for chunk in ids),
                tuple(evidence), tuple(forbidden), evidence, bucket,
            ))
    return chunks, cases


def crowding_fixture_json(chunks, cases):
    payload = {
        "schema_version": 1, "chunks": [asdict(chunk) for chunk in chunks],
        "cases": [asdict(case) for case in cases],
    }
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def fixture_fingerprint(split):
    return sha256(crowding_fixture_json(*make_crowding_dataset(split))).hexdigest()
