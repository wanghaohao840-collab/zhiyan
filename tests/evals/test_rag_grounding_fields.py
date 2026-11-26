import json

import pytest

from evals.rag_quality_scoring import has_fact
from hello_agents.tools.builtin.rag_grounding import EvidenceProtocolError, grounded_answer


def replay(question, passage, quote, claim, *, check_fields):
    payload = json.dumps({
        "quotes": [{"id": "S-case", "quote": quote}],
        "claims": [{"text": claim, "ids": ["S-case"]}],
    })
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return payload

    sources = [{"citation_id": "S-case", "content": passage}]
    return lambda: grounded_answer(
        generate, question, "", passage, sources, strategy="single_pass", **({"check_fields": True} if check_fields else {}),
    ), calls


@pytest.mark.parametrize("question,passage,quote,claim", [
    (
        "补票验证码是多少？", "入口标识：A-901；验证码请向值班台获取。",
        "A-901", "补票验证码为 A-901。",
    ),
    (
        "What is the verification code?", "Entrance sign: CT-725. Verification code: CT-814.",
        "Entrance sign: CT-725.", "The verification code is CT-725.",
    ),
    (
        "领回寄存件的核验码是什么？", "门牌编号为 7926；核验码未公布。",
        "7926", "7926",
    ),
    (
        "核验码是什么？", "门牌编号为 WK-370。核验码：WK-522。",
        "门牌编号为 WK-370。核验码：WK-522。", "WK-370",
    ),
])
def test_wrong_code_role_passes_literal_check_but_is_blocked_within_retry_limit(
    question, passage, quote, claim,
):
    old, _ = replay(question, passage, quote, claim, check_fields=False)
    checked, calls = replay(question, passage, quote, claim, check_fields=True)
    assert old().quotes
    with pytest.raises(EvidenceProtocolError, match="different field"):
        checked()
    assert len(calls) == 2


@pytest.mark.parametrize("question,passage,quote,claim", [
    (
        "参观核验码是多少？", "入口标识：VA-251。用于参观核验时也填写 VA-251。",
        "用于参观核验时也填写 VA-251。", "核验码为 VA-251。",
    ),
    (
        "What is the verification code?", "The verification code is AT-603.",
        "AT-603", "AT-603",
    ),
    (
        "审批接口超时多久？", "这是风控审批接口。该接口超时阈值为 4 秒。",
        "该接口超时阈值为 4 秒。", "超时阈值为 4 秒。",
    ),
    (
        "什么情况下可以跳过审批？", "除紧急变更外，不得跳过审批。",
        "除紧急变更外，不得跳过审批。", "仅紧急变更可跳过审批。",
    ),
])
def test_field_check_preserves_contextual_and_equivalent_answers(question, passage, quote, claim):
    checked, calls = replay(question, passage, quote, claim, check_fields=True)
    assert checked().text == f"{claim} [S-case]"
    assert len(calls) == 1


def test_compound_answer_only_checks_the_code_bound_to_verification():
    passage = "入口标识：3945。验证码：9274。"
    checked, calls = replay(
        "入口标识和验证码分别是什么？", passage, passage,
        "入口标识为 3945，验证码为 9274。", check_fields=True,
    )
    assert checked().quotes
    assert len(calls) == 1


@pytest.mark.parametrize(("question", "claim", "expected"), [
    ("什么时候可以放行？", "任何时候都可以放行。", ("exception_may_be_omitted",)),
    ("什么时候可以放行？", "只有临时疏散可以放行。", ()),
    ("临时疏散时可以放行吗？", "可以放行。", ()),
])
def test_exception_advisory_keeps_supported_literal_answer(question, claim, expected):
    passage = "除临时疏散外，不得放行。"
    checked, calls = replay(question, passage, passage, claim, check_fields=True)
    result = checked()
    assert result.warnings == expected
    assert result.quotes
    assert len(calls) == 1


def test_gold_chinese_duration_spacing_is_semantically_inert():
    assert has_fact("超时阈值为13秒。", "13 秒")
    assert has_fact("超时阈值为 13 秒。", "13秒")
    assert not has_fact("超时阈值为 113 秒。", "13秒")
    assert not has_fact("The label is C330.", "330")
