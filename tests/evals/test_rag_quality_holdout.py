from dataclasses import replace
import json

import pytest

from evals.rag_quality_dataset import HOLDOUT_DATASET, load_gold_dataset
from evals.rag_quality_experiment import RERANK, build_index, evaluate_case
from hello_agents.memory.embedding import SimpleEmbedding
from hello_agents.tools.builtin.rag_grounding import EvidenceProtocolError, grounded_answer


def test_holdout_has_multihop_and_explicit_fact_sources():
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)
    assert len(chunks) == 64
    assert len(cases) == 24
    for language in ("zh", "en"):
        selected = [case for case in cases if case.language == language]
        assert sum(case.category == "multihop" for case in selected) >= 2
        assert sum(not case.evidence for case in selected) >= 4
        assert all(case.fact_evidence is not None for case in selected)


def test_missing_evidence_returns_abstention_without_drafting():
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return '{"quotes":[]}'

    result = grounded_answer(generate, "What is the maximum voltage?", "", "Capacity only.", [])
    assert "资料中没有提到" in result.text
    assert result.quotes == {}
    assert len(calls) == 1


@pytest.mark.parametrize("piece, claim, ids", [
    ("Original quote not shown.", "74 hours", ["S-a"]),
    ("The final hold is 74 hours.", "74 days", ["S-a"]),
    ("The final hold is 74 hours.", "47 hours", ["S-a"]),
    ("The final hold is 74 hours.", "74 hours", ["S-other"]),
    ("The final hold is 74 hours.", "74 hours [S-other]", ["S-a"]),
    ("The final hold is 74 hours.", "ticket CA-QUAR-REC", ["S-a"]),
])
def test_rejects_unseen_quotes_citations_or_changed_literals(piece, claim, ids):
    outputs = iter([
        json.dumps({"quotes": [{"id": "S-a", "quote": piece}]}),
        json.dumps({"claims": [{"text": claim, "ids": ids}]}),
    ])
    sources = [{"citation_id": "S-a", "content": "The final hold is 74 hours."}]
    with pytest.raises(EvidenceProtocolError):
        grounded_answer(lambda _: next(outputs, "{}"), "How long?", "", "Source text", sources)


def test_qa_returns_only_the_used_exact_quotes_and_checks_each_fact(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)
    case = next(case for case in cases if case.id == "en-02")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    variant = replace(RERANK, name="verified_evidence", verify_evidence=True)
    # Use a controlled hit list to test the grounding path regardless of the fallback ranker.
    selected = [pipeline.get_document_chunks(doc)[0] for doc in case.scope]
    from hello_agents.tools.builtin.rag_context import citation_id

    hits = [{**hit, "score": 0.9} for hit in selected]
    pipeline.search = lambda **options: hits
    left, right = [citation_id(hit) for hit in hits[:2]]
    observations = iter([
        json.dumps({"quotes": [
            {"id": left, "quote": "The quarantine hold for archived manifests in the EU operation is 74 hours."},
            {"id": right, "quote": "dispatch the recovery ticket EU-QUAR-REC."},
        ]}),
        json.dumps({"claims": [
            {"text": "The archive-manifest hold is 74 hours.", "ids": [left]},
            {"text": "Dispatch EU-QUAR-REC.", "ids": [right]},
        ]}),
    ])
    row = evaluate_case(pipeline, case, variant, generate=lambda _: next(observations))
    assert row["evidence_status"] == "verified"
    assert row["answer_scores"]["accuracy"]
    assert row["citations"]["facts_supported"] == ["74", "EU-QUAR-REC"]
    assert len(row["sources"]) == 2
    assert row["sources"][1]["excerpt"] == "dispatch the recovery ticket EU-QUAR-REC."


def test_verified_qa_never_generates_with_out_of_scope_chunk(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)
    case = next(case for case in cases if case.id == "en-11")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    hit = pipeline.get_document_chunks("orchid-service-en")[0]
    pipeline.search = lambda **options: [hit]
    variant = replace(RERANK, name="verified_evidence", verify_evidence=True)
    row = evaluate_case(pipeline, case, variant, generate=lambda _: pytest.fail("scope leak"))
    assert row["scope_leaks"] > 0
    assert row["generation_error"]


def test_document_embedding_batches_do_not_cache_evaluation_queries(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)

    class BatchEmbedder:
        dimension = 384

        def __init__(self):
            self.index_calls = []
            self.queries = []
            self.simple = SimpleEmbedding()

        def encode_documents(self, texts):
            self.index_calls.append(texts)
            return self.simple.encode(texts)

        def encode(self, text):
            self.queries.append(text)
            return self.simple.encode(text)

    embedder = BatchEmbedder()
    pipelines = build_index(chunks, tmp_path, embedder)
    assert embedder.index_calls == [[chunk.content for chunk in chunks]]
    assert embedder.queries == []

    case = cases[0]
    options = {"query": case.question, "document_ids": list(case.scope)}
    pipelines[case.namespace].search(**options)
    pipelines[case.namespace].search(**options)
    assert embedder.queries == [case.question, case.question]


def test_invalid_evidence_output_has_one_bounded_retry():
    inputs = []
    source = {"citation_id": "S-case", "content": "The hold is 74 hours."}
    replies = iter([
        "",
        '{"quotes":[{"id":"S-case","quote":"The hold is 74 hours."}]}',
        '{"claims":[{"text":"74 hours.","ids":["S-case"]}]}',
    ])

    def generate(prompt):
        inputs.append(prompt)
        return next(replies)

    answer = grounded_answer(generate, "How long is the hold?", "", "Passage", [source])
    assert answer.text == "74 hours. [S-case]"
    assert len(inputs) == 3
    assert inputs[1].startswith(inputs[0])


def test_fact_citations_cover_fullwidth_brackets_and_following_source_label(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)
    case = next(case for case in cases if case.id == "en-02")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    from hello_agents.tools.builtin.rag_context import citation_id

    hits = [
        {**pipeline.get_document_chunks(doc)[0], "score": 0.9} for doc in case.scope[:2]
    ]
    pipeline.search = lambda **options: hits
    left, right = map(citation_id, hits)
    row = evaluate_case(
        pipeline, case, RERANK,
        generate=lambda _: f"Hold: 74 hours. 依据：［{left}］\nRecovery ticket: EU-QUAR-REC.\n来源：{right}",
    )
    assert row["citations"]["valid"] == [left, right]
    assert row["citations"]["facts_supported"] == ["74", "EU-QUAR-REC"]
