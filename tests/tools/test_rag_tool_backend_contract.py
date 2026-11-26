import hashlib
import pytest
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


def test_rag_tool_forwards_data_root_to_default_and_lazy_namespaces(
    tmp_path, monkeypatch
):
    calls = []

    def fake_create_rag_pipeline(**kwargs):
        calls.append(kwargs)
        return FakePipeline()

    monkeypatch.setattr(
        rag_tool_module, "create_rag_pipeline", fake_create_rag_pipeline
    )
    data_root = (tmp_path / "data").resolve()
    tool = rag_tool_module.RAGTool(data_root=data_root, enable_graph=False)
    tool._get_pipeline("user-b")

    assert [call["data_root"] for call in calls] == [data_root, data_root]


def test_search_result_includes_structured_hits_and_keeps_legacy_message(monkeypatch):
    class SearchPipeline(FakePipeline):
        def search(self, **kwargs):
            assert kwargs["document_ids"] == ["doc-1"]
            return [{
                "id": "doc-1_0",
                "score": 0.75,
                "content": "first source paragraph",
                "metadata": {
                    "document_id": "doc-1",
                    "chunk_index": 0,
                    "page_number": 2,
                    "section": "Introduction",
                    "file_name": "unsafe-path/sample.pdf",
                },
            }]

        def get_document_chunks(self, document_id):
            assert document_id == "doc-1"
            return [{
                "id": "doc-1_0", "content": "first source paragraph",
                "metadata": {"document_id": "doc-1", "chunk_index": 0, "page_number": 2},
            }]

    pipeline = SearchPipeline()
    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", lambda **_: pipeline)
    tool = rag_tool_module.RAGTool(enable_graph=False)

    result = tool.execute_result(
        "search", query="source", document_ids=["doc-1"], limit=5
    )

    assert result.success is True
    assert "找到 1 条相关知识" in result.message
    assert result.data["result_count"] == 1
    assert result.data["results"] == [{
        "document_id": "doc-1", "chunk_id": "doc-1_0", "chunk_index": 0,
        "content": "first source paragraph", "score": 0.75,
        "content_sha256": hashlib.sha256(b"first source paragraph").hexdigest(),
        "page_number": 2, "section": "Introduction",
    }]


def test_get_document_chunk_requires_exact_id_and_index(monkeypatch):
    class SearchPipeline(FakePipeline):
        def get_document_chunk(self, document_id, chunk_id, chunk_index):
            if chunk_id != "doc-1_0" or chunk_index != 0:
                return None
            return {
                "id": "doc-1_0", "content": "source",
                "metadata": {"document_id": document_id, "chunk_index": 0},
            }

    monkeypatch.setattr(
        rag_tool_module, "create_rag_pipeline", lambda **_: SearchPipeline()
    )
    tool = rag_tool_module.RAGTool(enable_graph=False)

    found = tool.execute_result(
        "get_document_chunk", document_id="doc-1", chunk_id="doc-1_0", chunk_index=0
    )
    missing = tool.execute_result(
        "get_document_chunk", document_id="doc-1", chunk_id="doc-1_0", chunk_index=1
    )

    assert found.success is True
    assert found.data["chunk"]["content"] == "source"
    assert missing.success is False
    assert missing.data["chunk"] is None


def test_search_preserves_bounded_unicode_text_urls_and_full_hash(monkeypatch):
    content = "中文 https://example.com/paper?item=42 " * 100
    pipeline = SimpleNamespace(search=lambda **_: [{
        "id": "doc-1_0", "content": content, "score": 0.8,
        "metadata": {"document_id": "doc-1", "chunk_index": 0},
    }])
    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", lambda **_: pipeline)
    result = rag_tool_module.RAGTool(enable_graph=False).execute_result("search", query="失败")
    hit = result.data["results"][0]
    assert result.success
    assert hit["content"] == content[:1200]
    assert hit["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_empty_search_with_failure_word_in_query_is_success(monkeypatch):
    monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", lambda **_: SimpleNamespace(search=lambda **_: []))
    result = rag_tool_module.RAGTool(enable_graph=False).execute_result("search", query="失败")
    assert result.success
    assert result.data["results"] == []


@pytest.mark.parametrize("backend", ["json", "qdrant"])
def test_exact_chunk_backend_contract(tmp_path, monkeypatch, backend):
    from hello_agents.memory.rag import pipeline as json_module
    from hello_agents.memory.rag import qdrant_pipeline as qdrant_module
    from qdrant_client import QdrantClient

    embedder = SimpleNamespace(encode=lambda text: [1.0, 0.0])
    for module in (json_module, qdrant_module):
        monkeypatch.setattr(module, "get_text_embedder", lambda: embedder)
        monkeypatch.setattr(module, "get_dimension", lambda *a: 2)
    client = None
    if backend == "json":
        pipeline = json_module.SimpleRAGPipeline(cache_path=str(tmp_path / "chunks.json"), rag_namespace="alice")
    else:
        client = QdrantClient(":memory:")
        pipeline = qdrant_module.QdrantRAGPipeline(qdrant_client=client, rag_namespace="alice")
    try:
        pipeline.add_text("First source paragraph.", document_id="doc-1")
        pipeline.add_text("Adjacent source paragraph.", document_id="doc-1", replace_existing=False)
        pipeline.add_text("Other private paragraph.", document_id="doc-2")
        chunk = pipeline.get_document_chunks("doc-1")[0]
        index = chunk["metadata"]["chunk_index"]
        monkeypatch.setattr(rag_tool_module, "create_rag_pipeline", lambda **_: pipeline)
        tool = rag_tool_module.RAGTool(enable_graph=False)
        result = tool.execute_result("search", query="source", document_ids=["doc-1"], limit=5)
        assert result.success
        assert result.data["result_count"] >= 1
        assert {item["document_id"] for item in result.data["results"]} == {"doc-1"}
        source = next(item for item in result.data["results"] if item["chunk_id"] == chunk["id"])
        assert source["content_sha256"] == hashlib.sha256(chunk["content"].encode()).hexdigest()
        # Resolver may not fall back to reading the entire document.
        monkeypatch.setattr(pipeline, "get_document_chunks", lambda *a: pytest.fail("unbounded reader"))
        found = pipeline.get_document_chunk("doc-1", chunk["id"], index)
        assert found["content"] == chunk["content"]
        assert pipeline.get_document_chunk("doc-2", chunk["id"], index) is None
        assert pipeline.get_document_chunk("doc-1", chunk["id"], index + 1) is None
        assert pipeline.get_document_chunk("doc-1", "wrong", index) is None
        resolved = tool.execute_result("get_document_chunk", document_id="doc-1", chunk_id=chunk["id"], chunk_index=index)
        assert resolved.success
        assert resolved.data["chunk"]["content"] == chunk["content"]
        previous, target, following = tool.get_document_preview_chunks("doc-1", chunk["id"])
        assert previous is None
        assert target["content"] == chunk["content"]
        assert following["content"] == "Adjacent source paragraph."
        assert tool.get_document_preview_chunks("doc-2", chunk["id"]) == (None, None, None)
    finally:
        if client is not None:
            client.close()


def test_qdrant_exact_reader_is_one_bounded_page_and_rejects_ambiguity(monkeypatch):
    from hello_agents.memory.rag.qdrant_pipeline import QdrantRAGPipeline
    from hello_agents.memory.storage.vector_scan import VectorScanPage

    pipeline = object.__new__(QdrantRAGPipeline)
    pipeline.rag_namespace = "alice"
    pipeline.collection_name = "collection"
    monkeypatch.setattr(pipeline, "_require_managed_active", lambda: None)
    calls = []
    def page(collection, filters, page_size):
        calls.append((collection, filters, page_size))
        return VectorScanPage((), "00000000-0000-0000-0000-000000000001")
    pipeline.vector_store = SimpleNamespace(scroll_page=page)
    assert pipeline.get_document_chunk("doc-1", "chunk", 7) is None
    assert calls == [("collection", {"rag_namespace": "alice", "document_id": "doc-1", "chunk_index": 7}, 2)]
