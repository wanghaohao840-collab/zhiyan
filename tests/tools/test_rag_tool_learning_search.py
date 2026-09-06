from hello_agents.tools.builtin import rag_tool as rag_tool_module


def test_concurrent_action_envelopes_remain_request_local():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    tool = rag_tool_module.RAGTool.__new__(rag_tool_module.RAGTool)
    tool.qdrant_api_key = None
    published = Barrier(2)

    def execute(action, **kwargs):
        request = kwargs["document_id"]
        tool._last_action_data = {"success": True, "results": [request]}
        published.wait(timeout=5)
        return "ok"

    tool.execute = execute
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(tool.execute_result, "search", document_id="first")
        second = pool.submit(tool.execute_result, "search", document_id="second")
        assert first.result().data["results"] == ["first"]
        assert second.result().data["results"] == ["second"]


class SearchPipeline:
    def __init__(self, results):
        self.results = results

    def search(self, **kwargs):
        return self.results


def make_tool(monkeypatch, results):
    monkeypatch.setattr(
        rag_tool_module,
        "create_rag_pipeline",
        lambda **kwargs: SearchPipeline(results),
    )
    return rag_tool_module.RAGTool(enable_graph=False)


def test_search_execute_result_publishes_allowlisted_safe_hit_data(monkeypatch):
    tool = make_tool(
        monkeypatch,
        [
            {
                "id": "chunk-7",
                "content": "evidence text",
                "score": 0.91,
                "metadata": {
                    "document_id": "doc-id",
                    "document_name": "guide.pdf",
                    "file_name": "guide.pdf",
                    "file_suffix": ".pdf",
                    "source_type": "pdf",
                    "page_number": 12,
                    "chunk_index": 4,
                    "source_position": None,
                    "file_path": r"C:\\private\\guide.pdf",
                    "backend_payload": {"api_key": "secret"},
                },
                "backend_payload": {"internal": "never expose"},
            }
        ],
    )

    result = tool.execute_result(
        "search",
        query="find evidence",
        document_id="doc-id",
    )

    assert result.success is True
    assert result.data == {
        "success": True,
        "query": "find evidence",
        "document_ids": ["doc-id"],
        "results": [
            {
                "chunk_id": "chunk-7",
                "content": "evidence text",
                "score": 0.91,
                "metadata": {
                    "document_id": "doc-id",
                    "document_name": "guide.pdf",
                    "file_name": "guide.pdf",
                    "file_suffix": ".pdf",
                    "source_type": "pdf",
                    "page_number": 12,
                    "chunk_index": 4,
                    "source_position": None,
                },
            }
        ],
    }
    assert "private" not in str(result.data)
    assert "backend_payload" not in str(result.data)
    assert "secret" not in str(result.data)


def test_search_execute_result_truncates_evidence_to_safe_data_limit(monkeypatch):
    tool = make_tool(
        monkeypatch,
        [{"id": "chunk-1", "content": "x" * 501, "score": 1.0, "metadata": {}}],
    )

    result = tool.execute_result("search", query="long evidence")

    assert result.success is True
    assert result.data["results"][0]["content"] == "x" * 500


def test_search_execute_result_marks_empty_search_as_success(monkeypatch):
    tool = make_tool(monkeypatch, [])

    result = tool.execute_result("search", query="missing", document_id="doc-id")

    assert result.success is True
    assert result.data == {
        "success": True,
        "query": "missing",
        "document_ids": ["doc-id"],
        "results": [],
    }


def test_search_execute_result_marks_invalid_document_scope_as_failure(monkeypatch):
    tool = make_tool(monkeypatch, [])

    result = tool.execute_result("search", query="invalid", document_ids=[])

    assert result.success is False
    assert result.data["success"] is False
    assert result.data["query"] == "invalid"
    assert result.data["document_ids"] == []
    assert result.data["results"] == []
