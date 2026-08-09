import sys
from types import SimpleNamespace

from hello_agents.tools.builtin import rag_tool as rag_tool_module


class FakePipeline:
    def __init__(self):
        self.replaced = None

    def replace_document(self, document_id, segments, save_cache=True):
        self.replaced = (document_id, segments, save_cache)
        return {
            "success": True,
            "document_id": document_id,
            "chunks_added": len(segments),
            "chunks_removed": 0,
        }


def test_pdf_add_document_uses_public_replace_document(tmp_path, monkeypatch):
    fake_pipeline = FakePipeline()

    def fake_create_rag_pipeline(**kwargs):
        return fake_pipeline

    class FakePdfReader:
        def __init__(self, path):
            self.pages = [
                SimpleNamespace(extract_text=lambda: "page one"),
                SimpleNamespace(extract_text=lambda: "page two"),
            ]

    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", fake_create_rag_pipeline)
    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=FakePdfReader))

    path = tmp_path / "sample.pdf"
    path.write_bytes(b"%PDF-fake")

    tool = rag_tool_module.RAGTool()
    result = tool.execute("add_document", file_path=str(path), document_id="doc-1")

    assert "PDF" in result
    assert fake_pipeline.replaced is not None
    document_id, segments, save_cache = fake_pipeline.replaced
    assert document_id == "doc-1"
    assert save_cache is True
    assert [segment.metadata["page_number"] for segment in segments] == [1, 2]
    assert [segment.content for segment in segments] == ["page one", "page two"]


def test_text_add_document_uses_public_replace_document(tmp_path, monkeypatch):
    fake_pipeline = FakePipeline()

    def fake_create_rag_pipeline(**kwargs):
        return fake_pipeline

    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", fake_create_rag_pipeline)

    path = tmp_path / "sample.md"
    path.write_text("# Title\n\nbody", encoding="utf-8")

    tool = rag_tool_module.RAGTool()
    result = tool.execute("add_document", file_path=str(path), document_id="doc-md")

    assert "document_id" in result
    assert fake_pipeline.replaced is not None
    document_id, segments, save_cache = fake_pipeline.replaced
    assert document_id == "doc-md"
    assert save_cache is True
    assert [segment.content for segment in segments] == ["# Title\n\nbody"]
    assert segments[0].metadata["file_name"] == "sample.md"
    assert segments[0].metadata["source_type"] == "document"


def test_execute_result_returns_structured_success_for_add_document(tmp_path, monkeypatch):
    fake_pipeline = FakePipeline()

    def fake_create_rag_pipeline(**kwargs):
        return fake_pipeline

    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", fake_create_rag_pipeline)

    path = tmp_path / "sample.md"
    path.write_text("body", encoding="utf-8")

    tool = rag_tool_module.RAGTool()
    result = tool.execute_result("add_document", file_path=str(path), document_id="doc-md")

    assert result.success is True
    assert result.action == "add_document"
    assert result.data["document_id"] == "doc-md"
    assert result.data["chunks_added"] == 1
    assert "document_id" in result.message


def test_execute_result_returns_structured_failure_for_add_document(tmp_path, monkeypatch):
    fake_pipeline = FakePipeline()

    def fake_create_rag_pipeline(**kwargs):
        return fake_pipeline

    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", fake_create_rag_pipeline)

    tool = rag_tool_module.RAGTool()
    missing = tmp_path / "missing.md"
    result = tool.execute_result("add_document", file_path=str(missing), document_id="doc-md")

    assert result.success is False
    assert result.action == "add_document"
    assert result.data["document_id"] == "doc-md"
    assert "missing.md" in result.message


def test_search_execute_result_preserves_top_level_pipeline_hit_id(monkeypatch):
    class SearchPipeline:
        def search(self, **kwargs):
            return [
                {
                    "id": "pipeline-hit-42",
                    "content": "retrieved evidence",
                    "score": 0.91,
                    "metadata": {"document_id": "doc-1"},
                }
            ]

    monkeypatch.setattr(
        rag_tool_module,
        "create_rag_pipeline",
        lambda **kwargs: SearchPipeline(),
    )

    result = rag_tool_module.RAGTool(enable_graph=False).execute_result(
        "search",
        query="evidence",
        document_id="doc-1",
    )

    assert result.data["results"][0]["chunk_id"] == "pipeline-hit-42"
