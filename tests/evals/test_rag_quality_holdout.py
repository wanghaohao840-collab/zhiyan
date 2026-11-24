from dataclasses import replace
import json

import pytest

from evals.rag_quality_dataset import HOLDOUT_DATASET, load_gold_dataset
from evals.rag_quality_experiment import BASELINE, RERANK, build_index, evaluate_case
from evals.rag_quality_report import grounding_latency_gate
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


def test_single_pass_verifies_excerpts_and_uses_actual_qa_retrieval(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET)
    case = next(case for case in cases if case.id == "en-02")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    from hello_agents.tools.builtin.rag_context import citation_id

    hits = [
        {**pipeline.get_document_chunks(doc)[0], "score": 0.9} for doc in case.scope[:2]
    ]
    searches = []

    def search(**options):
        searches.append(options)
        return hits

    pipeline.search = search
    left, right = map(citation_id, hits)
    output = json.dumps({
        "quotes": [
            {"id": left, "quote": "The quarantine hold for archived manifests in the EU operation is 74 hours."},
            {"id": right, "quote": "dispatch the recovery ticket EU-QUAR-REC."},
        ],
        "claims": [
            {"text": "74 hours.", "ids": [left]},
            {"text": "Use EU-QUAR-REC.", "ids": [right]},
        ],
    })
    variant = replace(RERANK, verify_evidence=True, evidence_strategy="single_pass")
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return output

    row = evaluate_case(pipeline, case, variant, generate=generate, repeats=0)
    assert row["answer_scores"]["accuracy"]
    assert row["citations"]["facts_supported"] == ["74", "EU-QUAR-REC"]
    assert len(searches) == len(calls) == len(row["llm_call_ms"]) == 1
    assert row["retrieval_ms"] == row["qa_retrieval_ms"]
    assert "资料：" in calls[0]


def test_single_pass_abstains_without_any_source():
    result = grounded_answer(
        lambda _: '{"quotes":[],"claims":[]}', "How long?", "", "Source",
        [{"citation_id": "S-a", "content": "The room is blue."}],
        strategy="single_pass",
    )
    assert "资料中没有提到" in result.text
    assert result.quotes == {}


@pytest.mark.parametrize(("quote", "claim", "ids"), [
    ("The final hold is 74 hours.", "74 days", ["S-a"]),
    ("The final hold is 47 hours.", "47 hours", ["S-a"]),
    ("The final hold is 74 hours.", "74 hours", ["S-outside"]),
    ("The final hold is 74 hours.", None, []),
])
def test_single_pass_rejects_invalid_evidence_and_limits_retries(quote, claim, ids):
    output = json.dumps({
        "quotes": [{"id": "S-a", "quote": quote}],
        "claims": [{"text": claim, "ids": ids}] if claim else [],
    })
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return output

    with pytest.raises(EvidenceProtocolError):
        grounded_answer(
            generate, "How long?", "", "Source",
            [{"citation_id": "S-a", "content": "The final hold is 74 hours."}],
            strategy="single_pass",
        )
    assert len(calls) == 2


def test_grounding_latency_gate_requires_speed_and_supported_coverage():
    def row(variant, duration, *, supported=True):
        return {
            "variant": variant, "answerable": True, "category": "distractor",
            "first_gold_rank": 1, "gold_recall": 1, "scope_leaks": 0,
            "ranked_chunks": ["gold"], "retrieval_ms": [100],
            "qa_ms": duration, "generation_error": None,
            "answer_scores": {"accuracy": True, "fact_coverage": 1},
            "citations": {
                "expected_facts": 1, "emitted": ["S-x"], "valid": ["S-x"],
                "supported": ["S-x"] if supported else [],
                "facts_cited": ["value"], "facts_supported": ["value"] if supported else [],
            },
        }

    baseline = row("two_pass", 1000)
    assert grounding_latency_gate([baseline, row("single_pass", 750)])["passed"]
    assert not grounding_latency_gate([baseline, row("single_pass", 850)])["passed"]
    assert not grounding_latency_gate([baseline, row("single_pass", 750, supported=False)])["passed"]


def test_release_trial_checks_sources_returned_by_the_application_engine(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET.with_name("rag_quality_release.json"))
    assert len(chunks) == 40 and len(cases) == 16
    case = next(case for case in cases if case.id == "en-01")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    from hello_agents.tools.builtin.rag_context import citation_id

    hit = {**pipeline.get_document_chunks(case.scope[0])[0], "score": 0.9}
    citation = citation_id(hit)
    searches = []

    def search(**options):
        searches.append(options)
        return [hit]

    pipeline.search = search
    payload = {
        "quotes": [{"id": citation, "quote": "47 minutes"}],
        "claims": [{"text": "47 minutes", "ids": [citation]}],
    }
    variant = replace(
        BASELINE, verify_evidence=True, evidence_strategy="single_pass", use_application_engine=True,
    )
    row = evaluate_case(pipeline, case, variant, generate=lambda _: json.dumps(payload), repeats=0)
    assert row["generation_error"] is None
    assert row["answer_scores"]["accuracy"]
    assert row["citations"]["facts_supported"] == ["47"]
    assert row["sources"][0]["document_name"] == "Coastal Hub Manual.pdf"
    assert row["sources"][0]["page_number"] == 1
    assert row["sources"][0]["excerpt"] == "47 minutes"
    assert len(searches) == 1
    assert searches[0]["min_score"] == 0.12
    assert "retrieval_mode" not in searches[0]
    assert searches[0]["document_ids"] == list(case.scope)


def test_application_single_pass_preserves_qdrant_sources_and_namespace():
    from qdrant_client import QdrantClient

    from hello_agents.memory.rag.qdrant_pipeline import QdrantRAGPipeline
    from hello_agents.tools.builtin.rag_context import citation_id

    _, cases = load_gold_dataset(HOLDOUT_DATASET.with_name("rag_quality_release.json"))
    case = next(case for case in cases if case.id == "en-01")
    client = QdrantClient(":memory:")
    try:
        pipeline = QdrantRAGPipeline(qdrant_client=client, rag_namespace=case.namespace)
        outside = QdrantRAGPipeline(qdrant_client=client, rag_namespace="different-user")
        pipeline._to_vector = outside._to_vector = lambda _: [1.0] * pipeline.dimension
        metadata = {"file_name": "Coastal Hub Manual.pdf", "page_number": 3}
        pipeline.add_text("Current rule: check within 47 minutes.", document_id=case.scope[0], metadata=metadata)
        outside.add_text("Outside document: 74 minutes.", document_id=case.scope[0], metadata=metadata)
        hit = pipeline.search(case.question, document_ids=list(case.scope))[0]
        evidence_id = citation_id(hit)
        output = {
            "quotes": [{"id": evidence_id, "quote": "47 minutes"}],
            "claims": [{"text": "47 minutes", "ids": [evidence_id]}],
        }

        def generate(prompt):
            assert "Outside document" not in prompt
            return json.dumps(output)

        variant = replace(
            BASELINE, verify_evidence=True, evidence_strategy="single_pass", use_application_engine=True,
        )
        row = evaluate_case(pipeline, case, variant, generate=generate, repeats=0)
        assert row["generation_error"] is None
        assert row["scope_leaks"] == 0
        assert row["ranked_chunks"] == [hit["id"]]
        assert row["citations"]["valid"] == [evidence_id]
        assert row["sources"][0]["page_number"] == 3
    finally:
        client.close()



@pytest.mark.parametrize("strategy", ["two_pass", "single_pass"])
@pytest.mark.parametrize("claim", [
    "Revision 6 does not specify a fixed maximum waiting time.",
    "The manual gives context, but no published compensation amount is given.",
    "文档未给出所问的具体阈值。",
])
def test_explicitly_undocumented_values_get_a_clean_refusal(strategy, claim):
    quotes = [{"id": "S-case", "quote": claim}]
    claims = [{"text": claim, "ids": ["S-case"]}]
    replies = (
        [{"quotes": quotes}, {"claims": claims}]
        if strategy == "two_pass" else [{"quotes": quotes, "claims": claims}]
    )
    outputs = iter(map(json.dumps, replies))
    result = grounded_answer(
        lambda _: next(outputs), "What is the value?", "", "Source",
        [{"citation_id": "S-case", "content": claim}], strategy=strategy,
    )
    assert "资料中没有提到" in result.text
    assert result.quotes == {}


@pytest.mark.parametrize("passage", [
    "Revision 6 does not require a second check.",
    "The museum does not provide refunds after admission.",
])
def test_negative_requirement_remains_a_cited_answer(passage):
    result = grounded_answer(
        lambda _: json.dumps({
            "quotes": [{"id": "S-case", "quote": passage}],
            "claims": [{"text": passage, "ids": ["S-case"]}],
        }),
        "Does revision 6 require a second check?", "", "Source",
        [{"citation_id": "S-case", "content": passage}], strategy="single_pass",
    )
    assert result.text.endswith("[S-case]")
    assert result.quotes == {"S-case": passage}



def test_single_pass_rejects_background_source_on_atomic_claim():
    passages = {"S-direct": "The access code is ENTRY-A.", "S-context": "The gate is to the north."}
    sources = [{"citation_id": id_, "content": text} for id_, text in passages.items()]
    quotes = [{"id": id_, "quote": text} for id_, text in passages.items()]
    with pytest.raises(EvidenceProtocolError, match="exactly one"):
        grounded_answer(
            lambda _: json.dumps({
                "quotes": quotes,
                "claims": [{"text": "ENTRY-A", "ids": ["S-direct", "S-context"]}],
            }),
            "What is the access code?", "", "Source", sources, strategy="single_pass",
        )


def test_question_about_whether_a_value_is_documented_can_cite_its_absence():
    passage = "The manual does not specify a fixed waiting time."
    result = grounded_answer(
        lambda _: json.dumps({
            "quotes": [{"id": "S-case", "quote": passage}],
            "claims": [{"text": passage, "ids": ["S-case"]}],
        }),
        "Does the manual specify a waiting time?", "", "Source",
        [{"citation_id": "S-case", "content": passage}], strategy="single_pass",
    )
    assert result.text.endswith("[S-case]")
    assert result.quotes == {"S-case": passage}


@pytest.mark.parametrize(("question", "strategy"), [
    ("什么时候开放，以及补发码是什么？", "two_pass"),
    ("时限和签收表分别是什么？", "two_pass"),
    ("营业时间？补票流程？", "two_pass"),
    ("When does the room open, and what code replaces a lost reservation?", "two_pass"),
    ("Which label appears on the entrance sign?", "single_pass"),
    ("补票核验码是什么？", "single_pass"),
])
def test_adaptive_evidence_route(question, strategy):
    from hello_agents.tools.builtin.rag_grounding import resolve_evidence_strategy

    assert resolve_evidence_strategy("adaptive", question) == strategy
    assert resolve_evidence_strategy("single_pass", question) == "single_pass"


def test_adaptive_multihop_uses_two_passes_and_propagates_output_cap(tmp_path):
    chunks, cases = load_gold_dataset(HOLDOUT_DATASET.with_name("rag_quality_release.json"))
    case = next(case for case in cases if case.id == "en-04")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    from hello_agents.tools.builtin.rag_context import citation_id

    hits = [{**pipeline.get_document_chunks(doc)[0], "score": 0.9} for doc in case.scope]
    pipeline.search = lambda **_: hits
    first, second = map(citation_id, hits)
    replies = iter([
        {"quotes": [
            {"id": first, "quote": "opens at 09:42 on weekdays"},
            {"id": second, "quote": "uses Q8-ENTRY as the replacement booking code"},
        ]},
        {"claims": [
            {"text": "Opens at 09:42.", "ids": [first]},
            {"text": "Use Q8-ENTRY.", "ids": [second]},
        ]},
    ])
    caps = []

    def generate(prompt, *, max_tokens):
        caps.append(max_tokens)
        return json.dumps(next(replies))

    variant = replace(
        BASELINE, verify_evidence=True, evidence_strategy="adaptive", evidence_max_tokens=4096,
        use_application_engine=True,
    )
    row = evaluate_case(pipeline, case, variant, generate=generate, repeats=0)
    assert row["answer_scores"]["accuracy"]
    assert row["citations"]["facts_supported"] == ["09:42", "Q8-ENTRY"]
    assert row["evidence_strategy_used"] == "two_pass"
    assert caps == row["output_caps"] == [4096, 4096]


def test_verified_output_reserve_is_enforced_before_llm_call(tmp_path, monkeypatch):
    from evals.rag_quality_experiment import TrialGenerator

    chunks, cases = load_gold_dataset(HOLDOUT_DATASET.with_name("rag_quality_adaptive.json"))
    case = next(case for case in cases if case.id == "en-01")
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    monkeypatch.setattr(TrialGenerator, "context_window_tokens", 4096)

    def generate(prompt, **kwargs):
        pytest.fail("LLM must not run with no room for input")

    variant = replace(
        BASELINE, verify_evidence=True, evidence_strategy="adaptive", evidence_max_tokens=4096,
        use_application_engine=True,
    )
    row = evaluate_case(pipeline, case, variant, generate=generate, repeats=0)
    assert row["generation_error"] is not None
    assert not row["llm_call_ms"]
