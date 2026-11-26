"""Conservative checks for explicit validity dates in a cited passage."""

from datetime import date
import re
import unicodedata


_DATE = (
    r"(?<![0-9])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{4}\s*年\s*[0-9]{1,2}\s*月\s*[0-9]{1,2}\s*日)(?![0-9])"
)
_FULL_DATE = re.compile(_DATE)
_POINT = (
    re.compile(rf"(?:在|于)\s*({_DATE})\s*(?:当天|当日)?\s*(?:适用|有效|生效|执行)"),
    re.compile(rf"\b(?:valid|effective|applicable|in force)\s+on\s+({_DATE})", re.IGNORECASE),
)
_RANGE_START = (
    re.compile(rf"(?:自|从)\s*({_DATE})\s*起"),
    re.compile(rf"\b(?:starting|from|as of)\s+({_DATE})", re.IGNORECASE),
)
_VALID_FROM = re.compile(
    rf"(?:生效日期(?:为|是|:)?|有效期自|\b(?:valid|effective)\s+from)\s*({_DATE})"
    rf"|自\s*({_DATE})\s*起生效",
    re.IGNORECASE,
)
_VALID_UNTIL = re.compile(
    rf"(?:有效期至|适用至|\b(?:valid|effective)\s+(?:through|until))\s*({_DATE})",
    re.IGNORECASE,
)
_HISTORICAL_CLAIM = re.compile(
    r"已失效|已废止|旧版|不再适用|\b(?:retired|superseded|expired|historical|legacy)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[。\n]|(?<=\.)\s+")
_LINKED_VERSION = re.compile(r"^(?:旧版|旧规|本版|该版本|这一版本)")
_MULTI_DAY = re.compile(r"期间|之间|整段|区间|\b(?:between|range|period)\b", re.IGNORECASE)


def _parse(text: str) -> date | None:
    try:
        return date(*(int(part) for part in re.findall(r"[0-9]+", text)))
    except ValueError:
        return None


def _date_outside_period(evidence: str, code: str, asked: date) -> bool:
    sentences = _SENTENCE.split(evidence)
    related = []
    for index, sentence in enumerate(sentences):
        if code not in sentence:
            continue
        related.append(sentence)
        if index + 1 < len(sentences) and _LINKED_VERSION.search(sentences[index + 1].lstrip()):
            related.append(sentences[index + 1])
    local = " ".join(related)
    starts = [_parse(match[1] or match[2]) for match in _VALID_FROM.finditer(local)]
    ends = [_parse(match[1]) for match in _VALID_UNTIL.finditer(local)]
    if len(starts) > 1 or len(ends) > 1 or None in [*starts, *ends]:
        return False
    if starts and ends and starts[0] > ends[0]:
        return False
    return bool((starts and asked < starts[0]) or (ends and asked > ends[0]))


def validity_date_issue(
    question: str, claim: str, quote: str, code_pattern: re.Pattern[str],
) -> str | None:
    """Return 'outside' for a clear single date or 'warning' for an open range."""
    question = unicodedata.normalize("NFKC", question)
    claim = unicodedata.normalize("NFKC", claim)
    quote = unicodedata.normalize("NFKC", quote)
    dates = _FULL_DATE.findall(question)
    if len(dates) != 1 or _MULTI_DAY.search(question) or _HISTORICAL_CLAIM.search(claim):
        return None
    codes = set(code_pattern.findall(claim))
    if len(codes) != 1 or codes != set(code_pattern.findall(quote)):
        return None
    if any(pattern.search(question) for pattern in _POINT):
        result = "outside"
    elif any(pattern.search(question) for pattern in _RANGE_START):
        result = "warning"
    else:
        return None
    asked = _parse(dates[0])
    return result if asked is not None and _date_outside_period(quote, next(iter(codes)), asked) else None
