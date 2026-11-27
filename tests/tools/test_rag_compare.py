import json

from hello_agents.tools.builtin.rag_compare import (
    comparison_markdown_fallback,
    parse_structured_comparison,
    render_comparison_markdown,
)


def valid_comparison():
    return {
        "common_points": [
            {"text": "Both mention alpha", "citations": ["S-one"]}
        ],
        "differences": [
            {
                "topic": "scope",
                "documents": [
                    {
                        "document_id": "doc-1",
                        "text": "first",
                        "citations": ["S-one"],
                    },
                    {
                        "document_id": "doc-2",
                        "text": "second",
                        "citations": ["S-two"],
                    },
                ],
            }
        ],
        "per_document_evidence": [
            {
                "document_id": "doc-1",
                "summary": "first evidence",
                "citations": ["S-one"],
            },
            {
                "document_id": "doc-2",
                "summary": "second evidence",
                "citations": ["S-two"],
            },
        ],
        "missing_information": [],
    }


def test_structured_comparison_validates_and_renders_markdown():
    parsed = parse_structured_comparison(
        json.dumps(valid_comparison()),
        citation_documents={"S-one": {"doc-1"}, "S-two": {"doc-2"}},
        selected_document_ids={"doc-1", "doc-2"},
    )

    assert parsed is not None
    markdown = render_comparison_markdown(parsed)
    assert "## 共同点" in markdown
    assert "`doc-1`" in markdown
    assert "[S-one]" in markdown


def test_structured_comparison_rejects_unknown_citation():
    value = valid_comparison()
    value["common_points"][0]["citations"] = ["S-invented"]

    assert parse_structured_comparison(
        json.dumps(value),
        citation_documents={"S-one": {"doc-1"}, "S-two": {"doc-2"}},
        selected_document_ids={"doc-1", "doc-2"},
    ) is None


def test_structured_comparison_rejects_unselected_document():
    value = valid_comparison()
    value["per_document_evidence"][0]["document_id"] = "doc-3"

    assert parse_structured_comparison(
        json.dumps(value),
        citation_documents={"S-one": {"doc-1"}, "S-two": {"doc-2"}},
        selected_document_ids={"doc-1", "doc-2"},
    ) is None


def test_fallback_preserves_markdown_and_hides_rejected_json():
    prose = "## 共同点\n两份文档都讨论了昼夜节律。"
    assert comparison_markdown_fallback(prose) == prose
    assert "wrong-owner-claim" not in comparison_markdown_fallback(
        '输出如下\n```json\n{"text": "wrong-owner-claim"}\n```'
    )
    assert "wrong-owner-claim" not in comparison_markdown_fallback(
        '输出如下\n{"per_document_evidence": [{"summary": "wrong-owner-claim"}]}'
    )
