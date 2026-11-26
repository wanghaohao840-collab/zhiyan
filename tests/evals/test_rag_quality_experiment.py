
from dataclasses import replace
import json

import pytest

from evals.rag_quality_dataset import DEFAULT_DATASET, load_gold_dataset
from evals.rag_quality_experiment import (
    NO_CITATION_INSTRUCTION, RERANK, build_index, evaluate_case, main,
)
from evals.rag_quality_scoring import extract_answer_body, score_answer
from hello_agents.memory.embedding import SimpleEmbedding


def test_dataset_covers_languages_and_rejects_evidence_outside_scope(tmp_path):
    chunks, cases = load_gold_dataset()
    assert len({chunk.document_id for chunk in chunks}) >= 8
    for language in ("zh", "en"):
        selected = [case for case in cases if case.language == language]
        assert sum(bool(case.evidence) for case in selected) >= 10
        assert {case.category for case in selected} == {
            "answerable", "distractor", "unanswerable", "scope_isolated",
        }

    data = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))
    data["cases"][0]["scope"] = ["quartz-cn"]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid golden case"):
        load_gold_dataset(path)


def test_only_instruction_changes_and_only_body_citations_are_scored(tmp_path):
    chunks, cases = load_gold_dataset()
    case = cases[0]
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return case.facts[0]

    required = evaluate_case(pipeline, case, RERANK, generate=generate)
    control = evaluate_case(pipeline, case, NO_CITATION_INSTRUCTION, generate=generate)
    assert required["ranked_chunks"] == control["ranked_chunks"]
    assert prompts[0].replace("，并使用资料中的稳定引用 ID 标注依据", "", 1) == prompts[1]
    assert required["sources"] and required["citations"]["emitted"] == []

    body = extract_answer_body("🤖 RAG回答:\n36\n\n📚 参考知识条数: 1\n📌 参考来源:\n[S-abcdef123456]")
    assert body == "36"


def test_citation_verifies_chunk_and_distinguishes_distractor_and_fake_id(tmp_path):
    chunks, cases = load_gold_dataset()
    case = cases[0]
    pipeline = build_index(chunks, tmp_path, SimpleEmbedding())[case.namespace]
    observed = evaluate_case(pipeline, case, RERANK)
    gold = observed["ranked_chunks"].index(case.evidence[0])
    gold_id = observed["sources"][gold]["citation_id"]
    distractor = next(source["citation_id"] for source in observed["sources"]
                      if source["citation_id"] != gold_id)
    citations = [gold_id, distractor, "S-deadbeef0000"]
    result = evaluate_case(
        pipeline, case, RERANK,
        generate=lambda _: f"360 [{gold_id} | 第 1 页] [{distractor}] [{citations[2]}]",
    )
    assert result["citations"] == {
        "emitted": citations, "valid": [gold_id, distractor], "supported": [gold_id],
    }


def test_scoring_penalizes_wrong_known_values_and_boundary_substrings():
    _, cases = load_gold_dataset()
    case = cases[0]
    assert score_answer("360 次。", case)["accuracy"]
    assert not score_answer("3600 次。", case)["accuracy"]
    assert not score_answer("360.5 次。", case)["accuracy"]
    assert not score_answer("360 或 300 次。", case)["accuracy"]
    negative = replace(case, facts=(), forbidden=("360",))
    assert score_answer("资料中没有提到。", negative)["accuracy"]
    assert not score_answer("资料中没有提到，估计 360 次。", negative)["accuracy"]


def test_offline_cli_writes_complete_results_without_provider_access(tmp_path):
    assert main(["--output-dir", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert (tmp_path / "report.md").is_file()
    assert set(result["cases"]) == {"offline"}
    assert result["run"]["providers"]["embedding"]["status"] == "not_probed"
    for row in result["summary"]["offline"].values():
        assert row["scope_leaks"] == 0
        assert row["retrieval_ms"]["samples"] == row["cases"] * 5
        assert row.get("qa_errors", 0) == 0
    assert result["summary"]["offline"]["hybrid_rerank"]["citation_validity"] == 1
