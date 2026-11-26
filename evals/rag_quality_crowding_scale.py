"""Varied bilingual synthetic dossiers with a shared scope across question types."""

from evals.rag_quality_dataset import GoldCase, GoldChunk


BUCKETS = ("same_document", "semantic_decoys", "cross_language", "ordinary", "missing")
TOPICS = {
    "dev": (
        ("曲岑", "Quince Point"), ("杉驿", "Holly Reach"), ("汀荻", "Laurel Step"),
        ("浅蒲", "Elm Terrace"), ("窄滩", "Aster Landing"), ("延枫", "Pine Gate"),
    ),
    "holdout": (
        ("霁竹", "Hazel Crest"), ("芦驿", "Myrtle Run"), ("密栗", "Clover Shelf"),
        ("星荷", "Sequoia Yard"), ("青杞", "Orchid Dune"), ("照梧", "Birch Spire"),
    ),
}
SIZES = (24, 60, 100)
DEV_SELECTION = ((0, 1, 4), (0, 2, 3), (0, 1, 2), (1, 2), (0, 3, 4), (3, 4))

# Different records share a subject without repeating the same wording.
DECOYS_ZH = (
    "恢复测试只在演练区执行，回收程序不进入实际交接。练习签在工位领取。",
    "温控报警处理的第一个观察窗留给消防巡检，结束后告知站长。",
    "卡槽堵塞通常先清理残余票据，必要时登记维修责任人。",
    "前任管理员的离线笔记已过期。样本装在旧柜，留作展览。",
    "备用线路的收件登记由窗口助手签收，磁带放在五号盒。",
    "修订通报撤回了旧标签，临时进场者须按当日通知填写。",
    "正式发布后只需抄送夜班值守；简化流程合并了两份检查记录。",
    "无人值守时，由巡检岗负责封条的批次核算。现场无需再次扫描。",
)
DECOYS_EN = (
    "Dry runs use a training enclosure. Pick up the exercise slip in the room and leave production records alone.",
    "Thermal alerts reserve the first observation window for safety staff. Notify the supervisor on completion.",
    "Jams usually require removing paper first, then noting who owns the maintenance ticket.",
    "The previous custodian's offline notes expired. Samples in the legacy cabinet are for display only.",
    "The reception aide records hand-off on a backup lane. Store the magnetic media in bin five.",
    "A bulletin withdrew the former placard. Short-term visitors follow the current daily notice.",
    "Once published, notify the late shift. The abbreviated workflow combines two check records.",
    "Unattended runs require the inspector to reconcile seal batches. There is no repeat scan on site.",
)

DETAILS_ZH = (
    "后续复验要求核对持有人签名。", "对应日志仅保留在纸质档。", "记录按季度整理归档。",
    "异常上报时请注明保管箱规格。", "回执单留存在服务台。", "每轮通报由不同小组收阅。",
    "该段记录仅适用于晚班。", "相关条目可供复盘培训。", "变更时需填写附注栏。",
    "通知范围包括档案服务人员。", "临时措施于交接完成后结束。", "请将练习册发给各班。",
    "流程归档采用编号顺序。", "新旧记录在目录中分列。", "该流程无需重复巡检。",
    "无电子确认时请保留存根。", "抽查通过后恢复正常排班。",
)
DETAILS_EN = (
    "A follow-up review checks the holder's signature.", "The accompanying log remains on paper.",
    "Sort these entries by quarter.", "Note the container size on the incident report.",
    "Keep a copy of the acknowledgement at reception.", "A different team reads each notice.",
    "This entry applies to late shifts.", "Use this item as a teaching reference.",
    "Fill in the remarks box when the handling changes.", "Archive staff are included in the notification.",
    "Temporary measures expire when custody is transferred.", "Share the workbook with each shift.",
    "Keep the paperwork in identifier order.", "The catalogue separates current and superseded entries.",
    "This workflow needs no repeat inspection.", "Retain the stub if electronic confirmation is absent.",
    "Restore the usual rota after a successful spot check.",
)


def _question_examples(zh, local, english, codes, two_document):
    guide, closure, current, directory, reception, receipt, missing = codes
    if zh:
        return [
            (f"{local}正式演练交接时，复核单和封存条分别用哪个代号？",
             (f"{local}正式演练交接，在验收窗口填写复核单 {guide}，完毕后通知记录员。",
              f"{local}正式演练交接，在验收窗口填写封存条 {closure}，完毕后通知记录员。"),
             (guide, closure)),
            (f"{local}正式档案移交遇到卡槽堵塞时应该报哪个工单号？",
             (f"{local}卷宗交割如进纸机构卡住，按异常受理凭条 {current} 办理。",), (current,)),
            (f"{local}脱网阅片后把质检记录存在哪个目录？",
             (f"During disconnected image review at {english} ({local}), place quality follow-up in {directory}.",),
             (directory,)),
            (f"{local}值班台交接窗口的编号是什么" +
             ("，登记回执编号是多少？" if two_document else "？"),
             (f"{local}值班台交接窗口编号是 {reception}。",
              f"{local}值班台登记回执编号为 {receipt}。")[:2 if two_document else 1],
             (reception, receipt)[:2 if two_document else 1]),
            (f"{local}补领工作牌需要输入哪个验证码？", (), ()),
        ], f"{local}补领工作牌需要输入验证码 {missing}。"
    return [
        (f"Which review slip and sealing tag does the official {english} rehearsal handover use?",
         (f"For the official {english} rehearsal handover, complete review slip {guide} at intake and tell the clerk.",
          f"For the official {english} rehearsal handover, complete sealing tag {closure} at intake and tell the clerk."),
         (guide, closure)),
        (f"Which ticket does the official {english} archive transfer use when the feed slot jams?",
         (f"For {english} records conveyance, a blocked paper mechanism is handled using incident voucher {current}.",),
         (current,)),
        (f"Where does {english} file quality-control records after offline image inspection?",
         (f"{local}（{english}）在脱网阅片后，质检追踪卷集中归档到 {directory}。",), (directory,)),
        (f"What is the shift handover counter number at {english}" +
         (", and what intake receipt number does it use?" if two_document else "?"),
         (f"The shift handover counter at {english} is numbered {reception}.",
          f"The intake receipt number at {english} is {receipt}.")[:2 if two_document else 1],
         (reception, receipt)[:2 if two_document else 1]),
        (f"What validation code replaces an expired visitor badge at {english}?", (), ()),
    ], f"The code for an expired visitor badge at {english} is {missing}."


def make_scale_dataset(split):
    chunks, cases = [], []
    offset = 400 if split == "holdout" else 0
    for index, (local, english) in enumerate(TOPICS[split]):
        zh = index % 2 == 0 if split == "holdout" else index in (0, 3, 4)
        language = "zh" if zh else "en"
        label = local if zh else english
        size = SIZES[index // 2]
        namespace = f"crowding-scale-{split}-{language}"
        stem = f"scale-{split}-{index + 1:02d}"
        docs = tuple(f"{stem}-vol{doc + 1}" for doc in range(4))
        codes = [f"RX-{150 + offset + index * 10 + value}" for value in range(7)]
        examples, outside = _question_examples(zh, local, english, codes, index % 2 == 0)
        updates = ((0, 2), (0, size // 4 - 1), (1, 3), (2, 1), (2, 4), (3, 2))
        passages = {doc: [] for doc in docs}
        wrong_by_doc = []
        decoys, details = (DECOYS_ZH, DETAILS_ZH) if zh else (DECOYS_EN, DETAILS_EN)
        for doc_no, doc in enumerate(docs):
            for page in range(size // 4):
                layout = doc_no * (size // 4) + page
                if layout % 3 == 0:
                    wrong = f"DC-{offset + 1000 + index * 200 + layout}"
                    wrong_by_doc.append(wrong)
                    text = decoys[(layout + index) % len(decoys)]
                    cue = f"历史例行参考 {wrong}。" if zh else f"Historical reference {wrong}."
                else:
                    text = details[(layout + index) % len(details)]
                    cue = f"卷 {doc_no + 1} 页 {page + 1}。"
                    if not zh:
                        cue = f"Volume {doc_no + 1}, page {page + 1}."
                passages[doc].append(f"{label} · {text} {cue}")
        evidence_by_question = [{} for _ in examples]
        update_idx = 0
        for example_no, (_, originals, facts) in enumerate(examples):
            for content, fact in zip(originals, facts, strict=True):
                doc_no, page = updates[update_idx]
                update_idx += 1
                doc = docs[doc_no]
                passages[doc][page] = content
                evidence_by_question[example_no][fact] = (f"{doc}_{page}",)
        historical_code = f"DC-{2000 + offset + index}"
        historical = (
            f"{local}档案移交前一次卡槽堵塞演示录入 {historical_code}，仅培训用。"
            if zh else f"The {english} archive transfer simulation for a feed slot jam recorded {historical_code} for training only."
        )
        passages[docs[1]][0] = historical
        wrong_by_doc.append(historical_code)
        for doc in docs:
            for page, content in enumerate(passages[doc]):
                chunks.append(GoldChunk(f"{doc}_{page}", namespace, doc, f"{doc}.pdf", page + 1, content))
        hidden = f"{stem}-private"
        chunks.append(GoldChunk(f"{hidden}_0", namespace, hidden, f"{hidden}.pdf", 1, outside))
        selected = DEV_SELECTION[index] if split == "dev" else range(len(BUCKETS))
        for example_no in selected:
            question, _, facts = examples[example_no]
            evidence = evidence_by_question[example_no]
            forbidden = tuple(wrong_by_doc) if example_no != 4 else (*wrong_by_doc, codes[-1])
            cases.append(GoldCase(
                id=f"{stem}-q{example_no + 1}", language=language,
                category="scope_isolated" if not facts else "multihop" if len(facts) > 1 else "distractor",
                namespace=namespace, scope=docs, question=question,
                evidence=tuple(chunk for ids in evidence.values() for chunk in ids),
                facts=facts, forbidden=forbidden, fact_evidence=evidence if facts else None,
                bucket=BUCKETS[example_no],
            ))
    return chunks, cases
