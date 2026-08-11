import json
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

from app.learning_generation import LearningGenerationError, LearningGenerationService


class FakeRAG:
    def __init__(self, results, *, success=True):
        self.results = results
        self.success = success
        self.calls = []

    def execute_result(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action != "search":
            raise AssertionError("generation may only search RAG")
        return SimpleNamespace(
            success=self.success,
            data={"success": self.success, "results": self.results},
            message="ok" if self.success else "failed",
            error="rag unavailable" if not self.success else "",
        )


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt):
        self.calls.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ChatOnlyLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, prompt):
        self.calls.append(prompt)
        return self.response


def hit(
    chunk_id="chunk-1",
    content="Grounded evidence",
    *,
    document_id="doc-1",
    source_type="pdf",
    page_number=3,
    source_position=None,
):
    return {
        "chunk_id": chunk_id,
        "content": content,
        "score": 0.9,
        "metadata": {
            "document_id": document_id,
            "document_name": "notes.pdf",
            "file_name": "notes.pdf",
            "file_suffix": ".pdf" if source_type == "pdf" else ".md",
            "source_type": source_type,
            "page_number": page_number,
            "chunk_index": 0,
            "source_position": source_position,
        },
    }


def card(front="Question", back="Answer", source_ids=None):
    return {
        "front": front,
        "back": back,
        "source_ids": ["SRC-1"] if source_ids is None else source_ids,
    }


def mcq(prompt="MCQ", source_ids=None):
    return {
        "type": "multiple_choice",
        "prompt": prompt,
        "options": ["A", "B", "C", "D"],
        "correct_index": 0,
        "explanation": "Because A is supported.",
        "source_ids": ["SRC-1"] if source_ids is None else source_ids,
    }


def short_answer(prompt="Short answer", source_ids=None):
    return {
        "type": "short_answer",
        "prompt": prompt,
        "reference_answer": "Reference",
        "key_points": ["Point one", "Point two"],
        "source_ids": ["SRC-1"] if source_ids is None else source_ids,
    }


def payload(key, items):
    return json.dumps({key: items}, ensure_ascii=False)


def service_with(response, results=None):
    rag = FakeRAG(results if results is not None else [hit()])
    llm = FakeLLM(response)
    return LearningGenerationService(rag, llm), rag, llm


def test_cards_search_once_deduplicates_chunks_and_reconstructs_pdf_sources():
    long_content = "x" * 300
    results = [
        hit("chunk-1", long_content, page_number=7),
        hit("chunk-1", "duplicate must not reach prompt", page_number=8),
        hit("chunk-2", "second evidence", page_number=9),
    ]
    response = payload(
        "cards",
        [
            card("First", "A1", ["SRC-1"]),
            card("Second", "A2", ["SRC-2"]),
        ],
    )
    generation, rag, llm = service_with(response, results)

    drafts = generation.generate_cards("doc-1", "notes.pdf", 2)

    assert rag.calls == [
        (
            "search",
            {
                "document_id": "doc-1",
                "query": ANY,
                "limit": 10,
            },
        )
    ]
    assert isinstance(rag.calls[0][1]["query"], str)
    assert rag.calls[0][1]["query"].strip()
    assert "SRC-1" in llm.calls[0]
    assert "SRC-2" in llm.calls[0]
    assert "duplicate must not reach prompt" not in llm.calls[0]
    assert drafts == [
        {
            "front": "First",
            "back": "A1",
            "sources": [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "document_name": "notes.pdf",
                    "page_number": 7,
                    "source_position": None,
                    "preview": "x" * 240,
                }
            ],
        },
        {
            "front": "Second",
            "back": "A2",
            "sources": [
                {
                    "chunk_id": "chunk-2",
                    "document_id": "doc-1",
                    "document_name": "notes.pdf",
                    "page_number": 9,
                    "source_position": None,
                    "preview": "second evidence",
                }
            ],
        },
    ]
    assert all("source_ids" not in draft for draft in drafts)


def test_non_pdf_source_uses_source_position_and_chat_fallback():
    rag = FakeRAG(
        [
            hit(
                content="Markdown evidence",
                source_type="markdown",
                page_number=99,
                source_position=12,
            )
        ]
    )
    llm = ChatOnlyLLM(payload("cards", [card()]))

    drafts = LearningGenerationService(rag, llm).generate_cards(
        "doc-1", "notes.md", 1
    )

    assert len(llm.calls) == 1
    assert drafts[0]["sources"][0]["page_number"] is None
    assert drafts[0]["sources"][0]["source_position"] == 12
    assert drafts[0]["sources"][0]["document_name"] == "notes.md"


@pytest.mark.parametrize(
    ("kind", "count", "items", "expected_types", "expected_limit"),
    [
        (
            "mixed",
            3,
            [mcq("M1"), mcq("M2"), short_answer("S1")],
            ["multiple_choice", "multiple_choice", "short_answer"],
            10,
        ),
        (
            "multiple_choice",
            11,
            [mcq(f"M{i}") for i in range(11)],
            ["multiple_choice"] * 11,
            30,
        ),
        (
            "short_answer",
            4,
            [short_answer(f"S{i}") for i in range(4)],
            ["short_answer"] * 4,
            12,
        ),
    ],
)
def test_exercise_kind_exact_count_split_and_search_limit(
    kind, count, items, expected_types, expected_limit
):
    generation, rag, _ = service_with(payload("exercises", items))

    drafts = generation.generate_exercises(
        "doc-1", "notes.pdf", count, kind=kind
    )

    assert [draft["type"] for draft in drafts] == expected_types
    assert rag.calls[0][1]["limit"] == expected_limit


@pytest.mark.parametrize("kind", ["", "essay", None])
def test_invalid_exercise_kind_fails_before_rag(kind):
    rag = FakeRAG([hit()])
    llm = FakeLLM(payload("exercises", [mcq()]))

    with pytest.raises(LearningGenerationError, match="kind"):
        LearningGenerationService(rag, llm).generate_exercises(
            "doc-1", "notes.pdf", 1, kind=kind
        )

    assert rag.calls == []
    assert llm.calls == []


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_invalid_count_fails_before_rag(count):
    rag = FakeRAG([hit()])
    llm = FakeLLM(payload("cards", [card()]))

    with pytest.raises(LearningGenerationError, match="count"):
        LearningGenerationService(rag, llm).generate_cards(
            "doc-1", "notes.pdf", count
        )

    assert rag.calls == []


@pytest.mark.parametrize(
    ("existing", "items"),
    [
        ((" existing   front ",), [card("Existing Front")]),
        ((), [card("Same"), card(" SAME ")]),
    ],
)
def test_card_duplicate_rejection_is_normalized_and_batch_atomic(existing, items):
    generation, _, llm = service_with(payload("cards", items))

    with pytest.raises(LearningGenerationError, match="duplicate"):
        generation.generate_cards(
            "doc-1", "notes.pdf", len(items), existing_fronts=existing
        )

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    ("existing", "items"),
    [
        ((" existing prompt ",), [mcq("Existing Prompt")]),
        ((), [short_answer("Same"), mcq(" SAME ")]),
    ],
)
def test_exercise_duplicate_rejection_is_normalized_and_batch_atomic(
    existing, items
):
    generation, _, llm = service_with(payload("exercises", items))

    with pytest.raises(LearningGenerationError, match="duplicate"):
        generation.generate_exercises(
            "doc-1", "notes.pdf", len(items), existing_prompts=existing
        )

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "bad_item",
    [
        card(source_ids=["SRC-404"]),
        {"front": "Question", "back": "Answer"},
        card(source_ids=[]),
        card(source_ids=["SRC-1", "SRC-1"]),
    ],
)
def test_card_unknown_missing_or_duplicate_source_ids_are_semantic_failures(bad_item):
    generation, _, llm = service_with(
        payload("cards", [bad_item]),
    )
    llm.responses.append(payload("cards", [card()]))

    with pytest.raises(LearningGenerationError, match="source"):
        generation.generate_cards("doc-1", "notes.pdf", 1)

    assert len(llm.calls) == 1


@pytest.mark.parametrize("bad_document_id", [None, "", "doc-2"])
def test_retrieved_hit_must_belong_to_requested_document(bad_document_id):
    generation, _, llm = service_with(
        payload("cards", [card()]),
        [hit(document_id=bad_document_id)],
    )

    with pytest.raises(LearningGenerationError, match="document"):
        generation.generate_cards("doc-1", "notes.pdf", 1)

    assert llm.calls == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(options=["A", "A", "C", "D"]),
        lambda item: item.update(options=["A", "B", "C"]),
        lambda item: item.update(correct_index=True),
        lambda item: item.update(correct_index=4),
        lambda item: item.update(explanation="  "),
        lambda item: item.update(extra_metadata={"page": 99}),
    ],
)
def test_multiple_choice_semantic_errors_never_trigger_repair(mutate):
    item = mcq()
    mutate(item)
    generation, _, llm = service_with(payload("exercises", [item]))
    llm.responses.append(payload("exercises", [mcq()]))

    with pytest.raises(LearningGenerationError):
        generation.generate_exercises(
            "doc-1", "notes.pdf", 1, kind="multiple_choice"
        )

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(reference_answer=""),
        lambda item: item.update(key_points=[]),
        lambda item: item.update(key_points=["same", " SAME "]),
        lambda item: item.update(key_points=[str(i) for i in range(9)]),
        lambda item: item.update(type="essay"),
    ],
)
def test_short_answer_semantic_errors_never_trigger_repair(mutate):
    item = short_answer()
    mutate(item)
    generation, _, llm = service_with(payload("exercises", [item]))
    llm.responses.append(payload("exercises", [short_answer()]))

    with pytest.raises(LearningGenerationError):
        generation.generate_exercises(
            "doc-1", "notes.pdf", 1, kind="short_answer"
        )

    assert len(llm.calls) == 1


def test_wrong_count_or_mixed_split_is_semantic_and_never_repaired():
    generation, _, llm = service_with(
        payload("exercises", [mcq("M1"), short_answer("S1"), short_answer("S2")])
    )
    llm.responses.append(
        payload("exercises", [mcq("M1"), mcq("M2"), short_answer("S1")])
    )

    with pytest.raises(LearningGenerationError, match="mixed"):
        generation.generate_exercises("doc-1", "notes.pdf", 3)

    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        payload("cards", []),
        payload("cards", [card(), card("Second")]),
    ],
)
def test_card_wrong_count_is_semantic_and_never_repaired(response):
    generation, _, llm = service_with(response)
    llm.responses.append(payload("cards", [card()]))

    with pytest.raises(LearningGenerationError, match="count"):
        generation.generate_cards("doc-1", "notes.pdf", 1)

    assert len(llm.calls) == 1


def test_empty_or_failed_search_stops_before_llm():
    llm = FakeLLM(payload("cards", [card()]))
    empty_rag = FakeRAG([])
    failed_rag = FakeRAG([], success=False)

    with pytest.raises(LearningGenerationError, match="evidence"):
        LearningGenerationService(empty_rag, llm).generate_cards(
            "doc-1", "notes.pdf", 1
        )
    with pytest.raises(LearningGenerationError, match="RAG"):
        LearningGenerationService(failed_rag, llm).generate_cards(
            "doc-1", "notes.pdf", 1
        )

    assert llm.calls == []


@pytest.mark.parametrize("response", ["[LLM未配置]", "[LLM调用失败] timeout"])
def test_llm_failure_markers_are_direct_failures_without_repair(response):
    generation, _, llm = service_with(response)
    llm.responses.append(payload("cards", [card()]))

    with pytest.raises(LearningGenerationError, match="LLM"):
        generation.generate_cards("doc-1", "notes.pdf", 1)

    assert len(llm.calls) == 1


def test_missing_llm_and_llm_exception_are_direct_failures():
    rag = FakeRAG([hit()])
    with pytest.raises(LearningGenerationError, match="LLM未配置"):
        LearningGenerationService(rag, None).generate_cards(
            "doc-1", "notes.pdf", 1
        )

    llm = FakeLLM(RuntimeError("secret transport details"))
    with pytest.raises(LearningGenerationError, match="LLM调用失败"):
        LearningGenerationService(rag, llm).generate_cards(
            "doc-1", "notes.pdf", 1
        )
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "first_response",
    [
        "not json",
        json.dumps([card()]),
        payload("wrong_wrapper", [card()]),
        payload("cards", {"front": "not a list"}),
    ],
)
def test_json_syntax_or_root_shape_gets_exactly_one_repair(first_response):
    repaired = "```json\n" + payload("cards", [card()]) + "\n```"
    rag = FakeRAG([hit()])
    llm = FakeLLM(first_response, repaired)

    drafts = LearningGenerationService(rag, llm).generate_cards(
        "doc-1", "notes.pdf", 1
    )

    assert drafts[0]["front"] == "Question"
    assert len(rag.calls) == 1
    assert len(llm.calls) == 2
    assert "repair" in llm.calls[1].casefold()


def test_repair_is_attempted_at_most_once():
    generation, _, llm = service_with("not json")
    llm.responses.append("still not json")
    llm.responses.append(payload("cards", [card()]))

    with pytest.raises(LearningGenerationError, match="JSON"):
        generation.generate_cards("doc-1", "notes.pdf", 1)

    assert len(llm.calls) == 2


def test_generation_has_no_persistence_dependency_or_side_effect():
    class SearchOnlyRAG(FakeRAG):
        def __getattr__(self, name):
            raise AssertionError(f"unexpected RAG access: {name}")

    rag = SearchOnlyRAG([hit()])
    drafts = LearningGenerationService(
        rag, FakeLLM(payload("cards", [card()]))
    ).generate_cards("doc-1", "notes.pdf", 1)

    assert rag.calls[0][0] == "search"
    assert set(drafts[0]) == {"front", "back", "sources"}
