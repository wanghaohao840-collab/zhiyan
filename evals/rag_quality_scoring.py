"""Scores for the same gold answer body and its submitted source targets."""

import re
import unicodedata

from hello_agents.tools.builtin.rag_context import citation_id


_CITATION = re.compile(
    r"\[(S-[^\]\s|]+)(?:\s*\|[^\]\n]*)?\]|(?<![\w-])(S-[0-9a-f]{12})(?![\w-])",
    re.IGNORECASE | re.ASCII,
)
_CITATION_LEAD = (
    r"(?:\[*S-|(?:依据|来源|参考(?:来源)?|source|evidence)\s*[:：]\s*\[*S-)"
)
_CLAUSE_BREAK = re.compile(
    rf"\n+(?!\s*{_CITATION_LEAD})|[;；]\s*"
    rf"|(?<=[!?。！？])(?!(?:\s*){_CITATION_LEAD})(?=\s*[^\s\[。！？.!?])"
    rf"|(?<=\.)\s+(?!\s*{_CITATION_LEAD})(?=[^\s\[])",
    re.IGNORECASE,
)
_ABSTENTION = re.compile(
    r"资料中没有提到|资料(?:中)?(?:未|没有|并未)(?:提到|提及|提供|说明|包含)|"
    r"未在所选文档.*?找到|未找到与.*?相关的知识|无法(?:从|根据).*?(?:确定|得知|回答)|"
    r"(?:not (?:mentioned|specified|stated|provided|available)|"
    r"(?:does not|do not|doesn't|don't) (?:mention|specify|provide|state|contain)|"
    r"no information|cannot (?:determine|answer)|insufficient (?:information|evidence))",
    re.IGNORECASE | re.DOTALL,
)


def extract_answer_body(message: str) -> str:
    prefix = "🤖 RAG回答:\n"
    footer = "\n\n📚 参考知识条数:"
    if message.startswith(prefix):
        return message[len(prefix):].split(footer, 1)[0].strip()
    if message.startswith("🔍") and message.endswith("无法生成答案"):
        return message
    return ""


def _normal(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def has_fact(text: str, fact: str) -> bool:
    normalized_fact = _normal(fact)
    numeric_left = r"(?<![0-9][.,])" if normalized_fact.isascii() and normalized_fact.isdecimal() else ""
    numeric_right = r"(?![.,][0-9])" if numeric_left else ""
    pattern = (
        numeric_left + r"(?<![a-z0-9_])" + re.escape(normalized_fact)
        + r"(?![a-z0-9_])" + numeric_right
    )
    return bool(re.search(pattern, _normal(text)))


def _citations(text):
    return ["S-" + (bracketed or bare)[2:].lower() for bracketed, bare in _CITATION.findall(text)]


def score_answer(body, case):
    text = _CITATION.sub("", unicodedata.normalize("NFKC", body))
    abstained = bool(_ABSTENTION.search(text))
    forbidden = [fact for fact in case.forbidden if has_fact(text, fact)]
    if case.facts:
        covered = [fact for fact in case.facts if has_fact(text, fact)]
        return {
            "accuracy": bool(text) and len(covered) == len(case.facts) and not forbidden and not abstained,
            "fact_coverage": len(covered) / len(case.facts),
            "abstained": abstained,
            "forbidden_facts": forbidden,
        }
    return {
        "accuracy": abstained and not forbidden,
        "fact_coverage": None,
        "abstained": abstained,
        "forbidden_facts": forbidden,
    }


def score_citations(body, sources, hits, case, pipeline):
    by_id = {citation_id(item): item for item in hits}
    by_source = {source["citation_id"]: source for source in sources}
    body = unicodedata.normalize("NFKC", body)
    emitted = list(dict.fromkeys(_citations(body)))
    valid = []
    supported = []
    for id_ in emitted:
        source = by_source.get(id_)
        hit = by_id.get(id_)
        if source is None or hit is None:
            continue
        meta = hit["metadata"]
        if (
            meta.get("rag_namespace") != case.namespace
            or source.get("document_id") not in case.scope
            or source.get("document_id") != meta["document_id"]
            or source.get("page_number") != meta["page_number"]
            or (source.get("file_name") or source.get("document_name")) != meta["file_name"]
            or not source.get("excerpt")
        ):
            continue
        original = pipeline.get_document_chunk(meta["document_id"], hit["id"], meta["chunk_index"])
        if (
            original is None
            or citation_id(original) != id_
            or source["excerpt"] not in original["content"]
        ):
            continue
        valid.append(id_)
        if hit["id"] in case.evidence:
            supported.append(id_)
    trace = {"emitted": emitted, "valid": valid, "supported": supported}
    if case.fact_evidence is not None:
        facts_cited = set()
        facts_supported = set()
        for clause in _CLAUSE_BREAK.split(body):
            local = _citations(clause)
            local_text = _CITATION.sub("", clause)
            local_chunks = {by_id[id_]["id"] for id_ in local if id_ in valid}
            for fact, expected in case.fact_evidence.items():
                if local and has_fact(local_text, fact):
                    facts_cited.add(fact)
                    if local_chunks.intersection(expected):
                        facts_supported.add(fact)
        trace.update(
            expected_facts=len(case.fact_evidence),
            facts_cited=sorted(facts_cited),
            facts_supported=sorted(facts_supported),
            resolved_chunks={id_: by_id[id_]["id"] for id_ in valid},
        )
    return trace
