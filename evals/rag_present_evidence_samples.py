"""Additional development counterexamples with explicit condition contrast."""

from evals.rag_quality_dataset import GoldCase, GoldChunk


SAMPLES = (
    (
        "zh", "version", "新规规定自 2027 年 3 月 16 日起，青隼站办理移交用哪个审批代号？",
        ("青隼站移交审批暂停沿用旧版，自 2027 年 3 月 16 日起，使用新的代号 BT-701。",),
        (
            "青隼站办理移交审批，代号 BT-142。旧版只适用至 2027 年 3 月 15 日。",
            "青隼站 2027 年新规的移交培训，审批演练暂用 BT-431。",
        ),
        ("BT-701",), ("BT-142", "BT-431"),
    ),
    (
        "en", "qualification", "Which clearance slip covers a confirmed feed failure at Suncrest Archive?",
        ("At Suncrest Archive, a verified interruption of the document feed requires clearance slip XR-505.",),
        (
            "Unconfirmed feed failure at Suncrest Archive uses provisional clearance slip XR-161 while checks are pending.",
            "Suncrest Archive has settled the false alarm workflow. A nuisance alert is filed on XR-209.",
        ),
        ("XR-505",), ("XR-161", "XR-209"),
    ),
    (
        "zh", "wrong_attribute", "槐岛站换领遗失的门禁凭证时，申请人应填写哪个核验码？",
        ("槐岛站遗失门禁凭证的换领流程。核对申请人身份通过后，请在核验码栏填写 NV-831。",),
        (
            "槐岛站换领门禁凭证区域位于 NV-188 号入口。该编号仅用于引导。",
            "新申领门禁凭证的核验码是 NV-714。已有凭证遗失的换领使用另外的记录。",
        ),
        ("NV-831",), ("NV-188", "NV-714"),
    ),
    (
        "en", "linked_condition", "Which register takes a confirmed spill at Calluna Bay, and which group handles that register?",
        (
            "A spill verified by the Calluna Bay duty officer goes into register QB-613.",
            "Confirmed spill entries in register QB-613 are assigned to group WS-913.",
        ),
        (
            "For a Calluna Bay spill drill use register QB-221. Drill entries go to group WS-204.",
            "Calluna Bay reported a suspected spill. Pending confirmations use temporary register QB-827.",
        ),
        ("QB-613", "WS-913"), ("QB-221", "WS-204", "QB-827"),
    ),
    (
        "en", "missing", "Which clearance slip covers a confirmed feed failure at North Iris Archive?",
        (),
        (
            "Unconfirmed feed failure at North Iris Archive uses provisional clearance slip XR-710 while checks are pending.",
            "North Iris Archive handles nuisance alerts with slip XR-381.",
        ),
        (), ("XR-710", "XR-381"),
    ),
    (
        "zh", "missing", "旅雁站补领已遗失的工作牌时，需要填写哪个核验码？",
        (),
        (
            "旅雁站初次领工作牌的核验码是 NV-335。已遗失工作牌请另向窗口询问。",
            "遗失工作牌补领窗口编号 NV-536，仅作为地点标识。",
        ),
        (), ("NV-335", "NV-536"),
    ),
)


def supplementary_cases():
    chunks, cases, rankings = [], [], {}
    for i, (lang, bucket, question, truth, decoys, facts, forbidden) in enumerate(SAMPLES, 1):
        case_id = f"present-dev-new-{i}"
        namespace = f"present-dev-new-{lang}"
        backgrounds = (
            "纸质存根保存到交接完成。", "夜班负责整理台面。", "新目录下月更新。", "收到通知后联系档案员。",
        ) if lang == "zh" else (
            "Keep paper stubs until custody is transferred.", "Night staff clear the counter.",
            "The catalogue is due for an update next month.", "Contact the archivist after notice arrives.",
        )
        # Keep all passages in the selected documents, with original page IDs.
        texts = (*decoys, *truth, *(f"{case_id} {text}" for text in backgrounds))
        docs, ids = [], []
        for index, text in enumerate(texts):
            document_id = f"{case_id}-p{index + 1}"
            chunk_id = f"{document_id}_0"
            chunks.append(GoldChunk(chunk_id, namespace, document_id, f"{document_id}.pdf", 1, text))
            docs.append(document_id)
            ids.append(chunk_id)
        evidence = tuple(ids[len(decoys):len(decoys) + len(truth)])
        links = {fact: (id_,) for fact, id_ in zip(facts, evidence, strict=True)}
        cases.append(GoldCase(
            case_id, lang, "multihop" if len(truth) == 2 else "distractor" if truth else "unanswerable",
            namespace, tuple(docs), question, evidence, facts, forbidden, links or None, bucket,
        ))
        rankings[case_id] = ids[:5]
    return chunks, cases, rankings
