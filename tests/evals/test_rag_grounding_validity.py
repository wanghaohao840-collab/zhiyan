import json

import pytest

from hello_agents.tools.builtin.rag_grounding import EvidenceProtocolError, grounded_answer


def replay(question, quote, claim, *, check_fields):
    payload = json.dumps({
        "quotes": [{"id": "S-version", "quote": quote}],
        "claims": [{"text": claim, "ids": ["S-version"]}],
    })
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return payload

    return lambda: grounded_answer(
        generate, question, "", quote, [{"citation_id": "S-version", "content": quote}],
        strategy="single_pass", check_fields=check_fields,
    ), calls


@pytest.mark.parametrize(("question", "source", "claim"), [
    (
        "霜槐站在 2028 年 2 月 17 日当天适用哪个调度代号？",
        "霜槐站调度代号 UC-457。旧规仅适用至 2028 年 2 月 16 日。",
        "调度代号为 UC-457。",
    ),
    (
        "Which register code was valid on 2028-01-09?",
        "The register code is PV-667 (valid from 2028-01-10).",
        "PV-667",
    ),
    (
        "20 站在 2027-04-12 适用哪个审批代号？",
        "20 站审批代号 KM-553；有效期至 2027-04-11。",
        "KM-553",
    ),
])
def test_explicit_date_mismatch_passes_literals_but_fails_optional_guard(question, source, claim):
    unchecked, _ = replay(question, source, claim, check_fields=False)
    assert unchecked().quotes
    checked, calls = replay(question, source, claim, check_fields=True)
    with pytest.raises(EvidenceProtocolError, match="validity period"):
        checked()
    assert len(calls) == 2


@pytest.mark.parametrize(("question", "source", "claim"), [
    (
        "Which register code was valid on 2028-01-09?",
        "The register code is PK-618 (valid through 2028-01-09).",
        "PK-618",
    ),
    (
        "Which register code was valid on 2028-01-10?",
        "The register code is PK-717 (valid from 2028-01-10).",
        "PK-717",
    ),
    (
        "槐岸站在 2028 年 2 月 17 日适用什么值守代号？",
        "2028 年 2 月 16 日修订：槐岸站值守代号为 PK-821。",
        "PK-821",
    ),
    (
        "Which register code was valid on 2028-01-20?",
        "The register code is SR-217. The visitor permit is valid through 2028-01-09.",
        "SR-217",
    ),
    (
        "Which register code was valid on 2028-01-20?",
        "The register codes were KM-281 and KM-282. Only the audit permit was valid through 2028-01-09.",
        "KM-282",
    ),
    (
        "Which code was valid on 2028-01-20 compared with 2028-01-10?",
        "The register code is CC-428 (valid through 2028-01-15).",
        "CC-428",
    ),
    (
        "Which historical register code did the team audit on 2028-01-20?",
        "The legacy register code was PK-921 and was valid through 2028-01-09.",
        "PK-921",
    ),
    (
        "Which code was valid on 2028-01-20?",
        "The legacy register code was PK-921 and was valid through 2028-01-09.",
        "The retired code PK-921 was valid through 2028-01-09 only.",
    ),
    (
        "槐岸站在 2028 年 2 月期间使用什么值守代号？",
        "旧版值守代号 PK-821。旧版有效期至 2028 年 2 月 16 日。",
        "PK-821",
    ),
    (
        "Which code was valid on 2028-02-30?",
        "The register code is PK-189 (valid through 2028-02-10).",
        "PK-189",
    ),
])
def test_guard_leaves_valid_and_ambiguous_dates_alone(question, source, claim):
    checked, calls = replay(question, source, claim, check_fields=True)
    assert checked().quotes
    assert len(calls) == 1


def test_open_ended_question_records_date_mismatch_as_warning():
    checked, calls = replay(
        "自 2027 年 3 月 16 日起，青隼站办理移交用哪个审批代号？",
        "青隼站办理移交审批，代号 BT-142。旧版只适用至 2027 年 3 月 15 日。",
        "审批代号为 BT-142。", check_fields=True,
    )
    assert checked().warnings == ("cited_version_may_be_outdated",)
    assert len(calls) == 1



def test_two_pass_uses_same_validity_check_and_single_shared_retry():
    source = "The register code is BR-852 (valid through 2028-08-01)."
    quotes = {"quotes": [{"id": "S-legacy", "quote": source}]}
    claims = {"claims": [{"text": "BR-852", "ids": ["S-legacy"]}]}
    responses = iter((quotes, claims, claims))
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return json.dumps(next(responses))

    with pytest.raises(EvidenceProtocolError, match="validity period"):
        grounded_answer(
            generate, "Which register code was valid on 2028-08-02?", "", source,
            [{"citation_id": "S-legacy", "content": source}], strategy="two_pass", check_fields=True,
        )
    assert len(calls) == 3
