import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from app.qa_answer_engine import QaAnswerRequest, RagQaAnswerEngine
from hello_agents.memory.rag.pipeline import SimpleRAGPipeline
from hello_agents.tools.builtin.rag_tool import RAGTool


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(str(prompt))
        return f"ANSWER-{len(self.prompts)}"

    def estimate_tokens(self, text):
        return len(str(text or ""))


class FakeGraphContextService:
    def __init__(self, failed_documents=None, cross_failed=False):
        self.failed_documents = set(failed_documents or [])
        self.cross_failed = cross_failed
        self.calls = []
        self.cross_calls = []
        self.entity_names = {}

    def get_graph_context(self, document_id, query, **kwargs):
        self.calls.append((document_id, query, kwargs))
        if document_id in self.failed_documents:
            return {
                "success": False,
                "document_id": document_id,
                "status": "failed",
                "data": {},
                "error": {
                    "type": "GraphNotReady",
                    "message": "graph unavailable",
                },
            }
        entity_id = f"{document_id}:concept:alpha"
        return {
            "success": True,
            "document_id": document_id,
            "status": "ready",
            "data": {
                "entities": [{
                    "id": entity_id,
                    "type": "Concept",
                    "name": self.entity_names.get(document_id, f"graph-{document_id}"),
                }],
                "relations": [],
            },
        }

    def get_cross_document_entities(self, document_ids, query, **kwargs):
        self.cross_calls.append((list(document_ids), query, kwargs))
        if self.cross_failed:
            return {
                "success": False,
                "status": "failed",
                "data": {},
                "error": {
                    "type": "GraphUnavailable",
                    "message": "graph unavailable",
                },
            }
        return {
            "success": True,
            "status": "ready",
            "data": {
                "entities": [{
                    "canonical_id": "canonical-alpha",
                    "entity_type": "Concept",
                    "normalized_name": "alpha",
                    "name": "shared-alpha",
                    "members": [
                        {"document_id": document_ids[0], "id": "local-1"},
                        {"document_id": document_ids[1], "id": "local-2"},
                    ],
                }],
            },
        }


class RAGToolMultiDocumentTests(unittest.TestCase):
    def make_tool(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        pipeline = SimpleRAGPipeline(
            cache_path=str(Path(tmpdir.name) / "rag-cache.json"),
            rag_namespace="test",
        )
        pipeline.dimension = 2
        pipeline._to_vector = (
            lambda text: [1.0, 0.0]
            if "alpha" in text.lower() or "共同" in text
            else [0.0, 1.0]
        )

        pipeline.add_text(
            "alpha selected one",
            document_id="doc-1",
            metadata={"file_name": "one.md"},
        )
        pipeline.add_text(
            "alpha selected two",
            document_id="doc-2",
            metadata={"file_name": "two.md"},
        )
        pipeline.add_text(
            "alpha unselected three",
            document_id="doc-3",
            metadata={"file_name": "three.md"},
        )

        tool = RAGTool.__new__(RAGTool)
        tool.rag_namespace = "test"
        tool.qdrant_url = None
        tool.qdrant_api_key = None
        tool.collection_name = "rag_knowledge_base"
        tool._pipelines = {"test": pipeline}
        tool.llm = FakeLLM()
        return tool

    def test_search_limits_results_to_document_ids(self):
        tool = self.make_tool()

        output = tool.execute(
            "search",
            query="alpha",
            document_ids=["doc-2", "doc-1"],
            rag_namespace="test",
            limit=10,
        )

        self.assertIn("doc-1", output)
        self.assertIn("doc-2", output)
        self.assertNotIn("doc-3", output)
        self.assertNotIn("three.md", output)

    def test_joint_ask_uses_only_selected_documents(self):
        tool = self.make_tool()

        output = tool.execute(
            "ask",
            query="alpha",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            limit=10,
            mode="joint",
        )

        prompt = tool.llm.prompts[-1]
        self.assertIn("alpha selected one", prompt)
        self.assertIn("alpha selected two", prompt)
        self.assertNotIn("alpha unselected three", prompt)
        self.assertIn("ANSWER-1", output)

    def test_joint_ask_exposes_copyable_source_payloads(self):
        tool = self.make_tool()

        tool.execute(
            "ask",
            query="alpha",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="joint",
        )

        sources = tool._last_action_data["sources"]
        self.assertEqual(
            {source["document_id"] for source in sources},
            {"doc-1", "doc-2"},
        )
        self.assertTrue(all(source["citation_id"] for source in sources))
        self.assertTrue(all(source["reference"].startswith("[S-") for source in sources))
        self.assertTrue(all(source["excerpt"] for source in sources))

    def test_compare_accepts_valid_structured_output_and_renders_markdown(self):
        tool = self.make_tool()
        pipeline = tool._pipelines["test"]
        first = pipeline.search("alpha", document_id="doc-1", limit=1)[0]
        second = pipeline.search("alpha", document_id="doc-2", limit=1)[0]
        first_id = tool._citation_id(first)
        second_id = tool._citation_id(second)
        value = {
            "common_points": [
                {"text": "shared alpha", "citations": [first_id, second_id]}
            ],
            "differences": [],
            "per_document_evidence": [
                {
                    "document_id": "doc-1",
                    "summary": "first",
                    "citations": [first_id],
                },
                {
                    "document_id": "doc-2",
                    "summary": "second",
                    "citations": [second_id],
                },
            ],
            "missing_information": [],
        }

        def generate(prompt, **kwargs):
            tool.llm.prompts.append(str(prompt))
            return json.dumps(value)

        tool.llm.generate = generate
        output = tool.execute(
            "ask",
            query="compare alpha",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="compare",
            structured_output=True,
        )

        self.assertEqual(tool._last_action_data["comparison_format"], "structured")
        self.assertEqual(tool._last_action_data["comparison"], value)
        self.assertIn("## 共同点", output)
        self.assertIn("JSON", tool.llm.prompts[-1])

    def test_compare_injects_selected_document_graph_context(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="compare alpha",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="auto",
        )

        prompt = tool.llm.prompts[-1]
        self.assertIn("graph-doc-1", prompt)
        self.assertIn("graph-doc-2", prompt)
        self.assertNotIn("graph-doc-3", prompt)
        self.assertEqual(
            [call[0] for call in graph.calls],
            ["doc-1", "doc-2"],
        )
        self.assertIn("G-", output)

    def test_compare_injects_shared_canonical_entity_context(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="compare alpha",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="auto",
        )

        self.assertIn("shared-alpha", tool.llm.prompts[-1])
        self.assertEqual(graph.cross_calls[0][0], ["doc-1", "doc-2"])
        self.assertTrue(
            any(
                source.get("canonical_id") == "canonical-alpha"
                for source in tool._last_action_data["graph_sources"]
            )
        )
        self.assertIn("G-", output)

    def test_compare_structured_output_accepts_canonical_graph_citation(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph
        canonical_id = tool._graph_citation_id(
            "doc-1|doc-2",
            "canonical_entity",
            "canonical-alpha",
        )
        value = {
            "common_points": [{
                "text": "shared graph concept",
                "citations": [canonical_id],
            }],
            "differences": [],
            "per_document_evidence": [],
            "missing_information": [],
        }

        def generate(prompt, **kwargs):
            tool.llm.prompts.append(str(prompt))
            return json.dumps(value)

        tool.llm.generate = generate
        tool.execute(
            "ask",
            query="compare graph",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="auto",
            structured_output=True,
        )

        self.assertEqual(tool._last_action_data["comparison"], value)
        self.assertEqual(tool._last_action_data["comparison_format"], "structured")

    def test_compare_required_shared_graph_failure_happens_before_llm(self):
        tool = self.make_tool()
        graph = FakeGraphContextService(cross_failed=True)
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="compare graph",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="required",
        )

        self.assertIn("GraphRAG", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_compare_graph_off_does_not_call_shared_graph_service(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        tool.execute(
            "ask",
            query="compare graph",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="off",
        )

        self.assertEqual(graph.cross_calls, [])

    def test_compare_structured_output_accepts_graph_citations(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph
        first_graph_id = tool._graph_citation_id(
            "doc-1",
            "entity",
            "doc-1:concept:alpha",
        )
        second_graph_id = tool._graph_citation_id(
            "doc-2",
            "entity",
            "doc-2:concept:alpha",
        )
        value = {
            "common_points": [{
                "text": "shared graph concept",
                "citations": [first_graph_id, second_graph_id],
            }],
            "differences": [],
            "per_document_evidence": [
                {
                    "document_id": "doc-1",
                    "summary": "first graph",
                    "citations": [first_graph_id],
                },
                {
                    "document_id": "doc-2",
                    "summary": "second graph",
                    "citations": [second_graph_id],
                },
            ],
            "missing_information": [],
        }

        def generate(prompt, **kwargs):
            tool.llm.prompts.append(str(prompt))
            return json.dumps(value)

        tool.llm.generate = generate
        tool.execute(
            "ask",
            query="compare graph",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="auto",
            structured_output=True,
        )

        self.assertEqual(tool._last_action_data["comparison"], value)
        self.assertEqual(
            tool._last_action_data["comparison_format"],
            "structured",
        )

    def test_compare_required_graph_failure_happens_before_llm(self):
        tool = self.make_tool()
        graph = FakeGraphContextService(failed_documents={"doc-2"})
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="compare graph",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="required",
        )

        self.assertIn("GraphRAG", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_compare_graph_off_does_not_call_graph_service(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="compare graph",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            graph_mode="off",
        )

        self.assertIn("RAG回答", output)
        self.assertEqual(graph.calls, [])

    def test_compare_requires_at_least_two_documents(self):
        tool = self.make_tool()

        output = tool.execute(
            "ask",
            query="对比共同点",
            document_ids=["doc-1"],
            rag_namespace="test",
            mode="compare",
        )

        self.assertIn("至少", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_auto_mode_prioritizes_compare_over_summary(self):
        tool = self.make_tool()

        tool.execute(
            "ask",
            query="请对比并总结共同点",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="auto",
        )

        prompt = tool.llm.prompts[-1]
        self.assertIn("共同点", prompt)
        self.assertIn("差异点", prompt)

    def test_summary_mode_includes_each_selected_document(self):
        tool = self.make_tool()

        output = tool.execute(
            "ask",
            query="联合总结",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )

        self.assertEqual(len(tool.llm.prompts), 3)
        reduce_prompt = tool.llm.prompts[-1]
        self.assertIn("doc-1", reduce_prompt)
        self.assertIn("doc-2", reduce_prompt)
        self.assertNotIn("alpha selected one", reduce_prompt)
        self.assertNotIn("alpha selected two", reduce_prompt)
        self.assertIn("ANSWER-3", output)

    def test_summary_keeps_question_evidence_when_context_budget_drops_chunks(self):
        tool = self.make_tool()
        pipeline = tool._pipelines["test"]
        chunks = ["Introduction", "Unrelated first option", "Unrelated middle option",
                  "Middle anchor", "prioritymax expires", "End anchor"]
        pipeline.get_document_summary_context = lambda document_id, limit: [
            {
                "id": str(index),
                "content": (text + " ").ljust(120, "x"),
                "score": 1.0,
                "metadata": {"document_id": document_id, "page_number": index + 1},
            }
            for index, text in enumerate(chunks)
        ]
        tool._context_budget = lambda *args, **kwargs: 680

        output = tool.execute(
            "ask", query="prioritymax expires", document_ids=["doc-1"], mode="summary"
        )

        self.assertIn("ANSWER-2", output)
        self.assertIn("prioritymax expires", tool.llm.prompts[0].split("资料：", 1)[1])
        self.assertNotIn("Unrelated first option", tool.llm.prompts[0])

    def test_summary_injects_per_document_graph_and_reduce_citations(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="summary graph",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )

        map_prompts = tool.llm.prompts[:2]
        self.assertTrue(
            any(
                "graph-doc-1" in prompt and "graph-doc-2" not in prompt
                for prompt in map_prompts
            )
        )
        self.assertTrue(
            any(
                "graph-doc-2" in prompt and "graph-doc-1" not in prompt
                for prompt in map_prompts
            )
        )
        first_graph_id = tool._graph_citation_id(
            "doc-1",
            "entity",
            "doc-1:concept:alpha",
        )
        second_graph_id = tool._graph_citation_id(
            "doc-2",
            "entity",
            "doc-2:concept:alpha",
        )
        self.assertIn(first_graph_id, tool.llm.prompts[-1])
        self.assertIn(second_graph_id, tool.llm.prompts[-1])
        self.assertIn("G-", output)

    def test_summary_keeps_shared_entity_in_reduce_only(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="summary graph",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )

        map_prompts = tool.llm.prompts[:2]
        self.assertTrue(all("shared-alpha" not in prompt for prompt in map_prompts))
        self.assertIn("shared-alpha", tool.llm.prompts[-1])
        self.assertEqual(graph.cross_calls[0][0], ["doc-1", "doc-2"])
        self.assertIn("shared-alpha", output)
        self.assertTrue(
            any(
                source.get("canonical_id") == "canonical-alpha"
                for source in tool._last_action_data["graph_sources"]
            )
        )

    def test_summary_graph_references_follow_the_stage_input_budget(self):
        for use_langgraph in (False, True):
            with self.subTest(langgraph=use_langgraph):
                tool = self.make_tool()
                graph = FakeGraphContextService()
                graph.entity_names["doc-1"] = "long-map-" + "x" * 600
                tool.graph_service = graph
                cross = graph.get_cross_document_entities

                def shared_entities(document_ids, query, **kwargs):
                    response = cross(document_ids, query, **kwargs)
                    first = response["data"]["entities"][0]
                    response["data"]["entities"].append({
                        **first, "canonical_id": "canonical-long", "name": "long-reduce-" + "x" * 600,
                    })
                    return response

                graph.get_cross_document_entities = shared_entities
                map_budget = 280

                def budget(prompt, **kwargs):
                    return 350 if "单篇文档摘要：" in prompt else map_budget

                tool._context_budget = budget
                map_hidden = tool._graph_citation_id("doc-1", "entity", "doc-1:concept:alpha")
                map_visible = tool._graph_citation_id("doc-2", "entity", "doc-2:concept:alpha")
                shared_hidden = tool._graph_citation_id("doc-1|doc-2", "canonical_entity", "canonical-long")
                shared_visible = tool._graph_citation_id("doc-1|doc-2", "canonical_entity", "canonical-alpha")
                request = QaAnswerRequest(
                    f"summary graph [{map_hidden}] [{shared_hidden}]", "", ("doc-1", "doc-2"), "summary"
                )
                engine = RagQaAnswerEngine(enable_summary_workflow=use_langgraph)
                runtime = SimpleNamespace(rag_tool=tool)

                first = engine.answer(runtime, request)
                self.assertEqual(
                    {source.citation_id for source in first.graph_sources}, {map_visible, shared_visible}
                )
                self.assertNotIn(map_hidden, first.answer)
                self.assertNotIn(shared_hidden, first.answer)
                first_map = next(prompt for prompt in tool.llm.prompts if prompt.startswith("请仅根据以下资料总结文档 doc-1。"))
                self.assertNotIn("图谱上下文", first_map)
                reduce_context = tool.llm.prompts[-1].split("单篇文档摘要：", 1)[1]
                self.assertIn(shared_visible, reduce_context)
                self.assertIn(map_visible, reduce_context)
                self.assertNotIn(map_hidden, reduce_context)
                self.assertNotIn(shared_hidden, reduce_context)

                cached = engine.answer(runtime, request)
                self.assertEqual(cached.graph_sources, first.graph_sources)
                self.assertEqual(tool._last_action_data["summary_cache_hits"], 2)
                self.assertEqual(len(tool.llm.prompts), 4)

                map_budget = 4096
                expanded = engine.answer(runtime, request)
                self.assertEqual(
                    {source.citation_id for source in expanded.graph_sources},
                    {map_hidden, map_visible, shared_visible},
                )
                self.assertEqual(tool._last_action_data["summary_cache_hits"], 0)
                self.assertEqual(len(tool.llm.prompts), 7)
                self.assertEqual(len(graph.cross_calls), 3)

    def test_summary_required_shared_graph_failure_happens_before_map_llm(self):
        tool = self.make_tool()
        graph = FakeGraphContextService(cross_failed=True)
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="summary graph",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="required",
        )

        self.assertIn("GraphRAG", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_summary_verifies_model_citations_before_caching_and_publishing(self):
        for use_langgraph in (False, True):
            with self.subTest(langgraph=use_langgraph):
                tool = self.make_tool()
                tool.graph_service = FakeGraphContextService()
                query = "alpha"
                pipeline = tool._pipelines["test"]
                doc1, doc2 = (
                    tool._citation_id(pipeline.search(query, document_id=doc, limit=1)[0])
                    for doc in ("doc-1", "doc-2")
                )
                shared = tool._graph_citation_id("doc-1|doc-2", "canonical_entity", "canonical-alpha")
                invalid_map = "S-0123456789ab"
                invalid_reduce = "G-0123456789"
                generate = tool.llm.generate

                def answer(prompt, **kwargs):
                    generate(prompt, **kwargs)
                    if prompt.startswith("请仅根据以下资料总结文档 doc-1。"):
                        return f"逐篇正文 [{doc1}] [{doc2}] [{invalid_map}] [1] [S-123]"
                    if "请给出联合总结：" in prompt:
                        return f"汇总正文 [{doc1}] [{doc2}] [{shared}] [{invalid_reduce}] [2]"
                    return "普通正文"

                tool.llm.generate = answer
                with self.assertLogs("hello_agents.tools.builtin.rag_summary_workflow", "INFO") as logs:
                    first, cached = (
                        tool.execute_result(
                            "ask", query=query, document_ids=["doc-1", "doc-2"],
                            mode="summary", use_summary_workflow=use_langgraph,
                        )
                        for _ in range(2)
                    )

                self.assertTrue(first.success)
                self.assertIn(f"汇总正文 [{doc1}] [{doc2}] [{shared}] [未核实引用] [2]", first.message)
                self.assertNotIn(invalid_reduce, first.message)
                self.assertEqual(first.message, cached.message)
                self.assertEqual(cached.data["summary_cache_hits"], 2)
                self.assertIn(f"逐篇正文 [{doc1}] [未核实引用] [未核实引用] [1] [S-123]", tool.llm.prompts[-1])
                self.assertNotIn(invalid_map, tool.llm.prompts[-1])
                self.assertEqual(first.data["summary_invalid_citations"], 3)
                self.assertIn("invalid_citations=3", logs.output[-1])
                self.assertNotIn(invalid_map, "\n".join(logs.output))
                self.assertNotIn(invalid_reduce, "\n".join(logs.output))

    def test_summary_rejects_model_output_with_only_citation_markers(self):
        for use_langgraph in (False, True):
            for rejected_stage in ("map", "reduce"):
                with self.subTest(langgraph=use_langgraph, stage=rejected_stage):
                    tool = self.make_tool()
                    generate = tool.llm.generate

                    def answer(prompt, **kwargs):
                        generate(prompt, **kwargs)
                        stage = "reduce" if "请给出联合总结：" in prompt else "map"
                        return "- [S-0123456789ab]。" if stage == rejected_stage else "实际摘要正文"

                    tool.llm.generate = answer
                    result = tool.execute_result(
                        "ask", query="alpha", document_ids=["doc-1", "doc-2"],
                        mode="summary", use_summary_workflow=use_langgraph,
                    )

                    self.assertFalse(result.success)
                    self.assertEqual(result.error_code, "QA_SUMMARY_INCOMPLETE")
                    self.assertFalse(result.retryable)
                    self.assertNotIn("0123456789ab", result.message)
                    self.assertEqual(len(tool._document_summary_cache), 0 if rejected_stage == "map" else 2)

    def test_summary_auto_shared_graph_failure_falls_back_to_vector_summaries(
        self,
    ):
        tool = self.make_tool()
        graph = FakeGraphContextService(cross_failed=True)
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="summary graph",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )

        self.assertIn("ANSWER-3", output)
        self.assertEqual(len(tool.llm.prompts), 3)
        self.assertNotIn("shared-alpha", tool.llm.prompts[-1])

    def test_summary_required_graph_failure_happens_before_map_llm(self):
        tool = self.make_tool()
        graph = FakeGraphContextService(failed_documents={"doc-2"})
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="summary graph",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="required",
        )

        self.assertIn("GraphRAG", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_summary_auto_graph_failure_falls_back_to_vector_summaries(self):
        tool = self.make_tool()
        graph = FakeGraphContextService(failed_documents={"doc-2"})
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="summary graph",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )

        self.assertIn("RAG回答", output)
        self.assertEqual(len(tool.llm.prompts), 3)
        self.assertEqual(
            [call[0] for call in graph.calls],
            ["doc-1", "doc-2"],
        )

    def test_summary_cache_invalidates_when_graph_context_changes(self):
        tool = self.make_tool()
        graph = FakeGraphContextService()
        tool.graph_service = graph

        tool.execute(
            "ask",
            query="graph cache",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )
        self.assertEqual(len(tool.llm.prompts), 3)

        tool.execute(
            "ask",
            query="graph cache",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )
        self.assertEqual(len(tool.llm.prompts), 4)
        self.assertEqual(tool._last_action_data["summary_cache_hits"], 2)

        graph.entity_names["doc-1"] = "graph-doc-1-updated"
        tool.execute(
            "ask",
            query="graph cache",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
            graph_mode="auto",
        )

        self.assertEqual(len(tool.llm.prompts), 6)
        self.assertEqual(tool._last_action_data["summary_cache_hits"], 1)
        self.assertEqual(tool._last_action_data["summary_cache_misses"], 1)

    def test_summary_cache_reuses_unchanged_maps_and_invalidates_changed_document(self):
        tool = self.make_tool()

        tool.execute(
            "ask",
            query="summary cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )
        self.assertEqual(len(tool.llm.prompts), 3)

        tool.execute(
            "ask",
            query="summary cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )
        self.assertEqual(len(tool.llm.prompts), 4)
        self.assertEqual(tool._last_action_data["summary_cache_hits"], 2)

        pipeline = tool._pipelines["test"]
        pipeline.add_text(
            "alpha selected one changed",
            document_id="doc-1",
            metadata={"file_name": "one.md"},
        )
        tool.execute(
            "ask",
            query="summary cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )

        self.assertEqual(len(tool.llm.prompts), 6)
        self.assertEqual(tool._last_action_data["summary_cache_hits"], 1)
        self.assertEqual(tool._last_action_data["summary_cache_misses"], 1)

    def test_summary_cache_invalidates_deleted_then_reimported_document(self):
        tool = self.make_tool()
        tool.execute(
            "ask",
            query="deletion cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )
        self.assertEqual(len(tool.llm.prompts), 3)

        pipeline = tool._pipelines["test"]
        pipeline.delete_document("doc-1")
        pipeline.add_text(
            "alpha reimported one",
            document_id="doc-1",
            metadata={"file_name": "one.md"},
        )
        tool.execute(
            "ask",
            query="deletion cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )

        self.assertEqual(len(tool.llm.prompts), 5)
        self.assertEqual(tool._last_action_data["summary_cache_hits"], 1)
        self.assertEqual(tool._last_action_data["summary_cache_misses"], 1)

    def test_summary_cache_invalidates_when_prompt_version_changes(self):
        tool = self.make_tool()
        tool.execute(
            "ask",
            query="prompt cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )
        self.assertEqual(len(tool.llm.prompts), 3)

        tool.SUMMARY_CACHE_PROMPT_VERSION = "test-next-summary-prompt"
        tool.execute(
            "ask",
            query="prompt cache",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
        )

        self.assertEqual(len(tool.llm.prompts), 6)
        self.assertEqual(tool._last_action_data["summary_cache_hits"], 0)
        self.assertEqual(tool._last_action_data["summary_cache_misses"], 2)

    def test_summary_reports_mapping_and_reduce_progress(self):
        tool = self.make_tool()
        progress_events = []

        tool.execute(
            "ask",
            query="progress summary",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
            progress_callback=lambda **event: progress_events.append(event),
        )

        self.assertEqual(progress_events[0]["stage"], "mapping")
        self.assertEqual(progress_events[0]["completed"], 0)
        self.assertTrue(
            any(
                event["stage"] == "reducing" and event["completed"] == 2
                for event in progress_events
            )
        )
        self.assertEqual(progress_events[-1]["stage"], "completed")

    def test_summary_honors_pre_cancelled_event(self):
        tool = self.make_tool()
        cancel_event = threading.Event()
        cancel_event.set()

        output = tool.execute(
            "ask",
            query="cancelled summary",
            document_ids=["doc-1", "doc-2"],
            rag_namespace="test",
            mode="summary",
            cancel_event=cancel_event,
        )

        self.assertIn("已取消", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_rejects_more_than_ten_documents(self):
        tool = self.make_tool()

        output = tool.execute(
            "ask",
            query="alpha",
            document_ids=[f"doc-{index}" for index in range(11)],
            mode="joint",
        )

        self.assertIn("最多选择 10 篇", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_context_budget_truncates_copies_and_marks_sources(self):
        tool = self.make_tool()
        original = {
            "content": "很长的正文" * 200,
            "score": 1.0,
            "metadata": {"document_id": "doc-1", "file_name": "one.md"},
        }

        context, included, truncated = tool._build_context(
            [original],
            token_budget=300,
            return_details=True,
        )

        self.assertTrue(truncated)
        self.assertIn("上下文已截断", context)
        self.assertTrue(included[0]["truncated"])
        self.assertEqual(original["content"], "很长的正文" * 200)
        self.assertNotIn("truncated", original)

    def test_answer_dedupes_by_pre_truncation_citation_id(self):
        tool = self.make_tool()
        first = {
            "content": "shared-prefix",
            "metadata": {"document_id": "doc-1"},
            "citation_id": "S-original-a",
            "truncated": True,
        }
        second = {
            "content": "shared-prefix",
            "metadata": {"document_id": "doc-1"},
            "citation_id": "S-original-b",
            "truncated": True,
        }

        output = tool._format_answer("answer", [first, second], truncated=True)

        self.assertIn("S-original-a", output)
        self.assertIn("S-original-b", output)
        self.assertIn("参考知识条数: 2", output)

    def test_compare_keeps_two_base_results_per_document(self):
        tool = self.make_tool()
        pipeline = tool._pipelines["test"]
        calls = Counter()

        def search(query, limit, min_score=0.0, document_id=None, **kwargs):
            calls[document_id] += 1
            return [
                {
                    "content": f"{document_id}-{index}",
                    "score": 1.0 - index / 10,
                    "metadata": {"document_id": document_id},
                }
                for index in range(5)
            ]

        pipeline.search = search
        tool.execute(
            "ask",
            query="比较",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
            limit=2,
        )

        prompt = tool.llm.prompts[-1]
        for doc_id in ("doc-1", "doc-2"):
            self.assertIn(f"{doc_id}-0", prompt)
            self.assertIn(f"{doc_id}-1", prompt)
        self.assertEqual(calls, Counter({"doc-1": 1, "doc-2": 1}))

    def test_compare_returns_capacity_error_without_dropping_base_document(self):
        tool = self.make_tool()
        tool.llm.context_window_tokens = 1900
        pipeline = tool._pipelines["test"]

        pipeline.search = lambda query, limit, min_score=0.0, document_id=None, **kwargs: [
            {
                "content": f"{document_id}-" + "证据" * 300,
                "score": 1.0 - index / 10,
                "metadata": {"document_id": document_id},
            }
            for index in range(2)
        ]

        output = tool.execute(
            "ask",
            query="比较",
            document_ids=["doc-1", "doc-2"],
            mode="compare",
        )

        self.assertIn("上下文容量不足", output)
        self.assertEqual(tool.llm.prompts, [])

    def test_summary_maps_concurrently_with_at_most_three_workers(self):
        tool = self.make_tool()
        pipeline = tool._pipelines["test"]
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        pipeline.get_document_summary_context = lambda document_id, limit: [
            {
                "content": f"content-{document_id}",
                "score": 1.0,
                "metadata": {"document_id": document_id},
            }
        ]

        class ConcurrentLLM(FakeLLM):
            context_window_tokens = 8192

            def generate(self, prompt, **kwargs):
                with lock:
                    self.prompts.append(str(prompt))
                    if "请仅根据以下资料总结文档" in prompt:
                        state["active"] += 1
                        state["maximum"] = max(state["maximum"], state["active"])
                if "请仅根据以下资料总结文档" in prompt:
                    time.sleep(0.04)
                    with lock:
                        state["active"] -= 1
                return "summary"

        tool.llm = ConcurrentLLM()
        output = tool.execute(
            "ask",
            query="联合总结",
            document_ids=[f"doc-{index}" for index in range(6)],
            mode="summary",
        )

        self.assertGreater(state["maximum"], 1)
        self.assertLessEqual(state["maximum"], 3)
        reduce_prompt = tool.llm.prompts[-1]
        for index in range(6):
            self.assertIn(f"doc-{index}", reduce_prompt)
        self.assertIn("summary", output)

    def test_summary_partial_failure_keeps_successful_documents(self):
        tool = self.make_tool()
        pipeline = tool._pipelines["test"]

        def summary_context(document_id, limit):
            if document_id == "doc-2":
                raise RuntimeError("map boom")
            return [
                {
                    "content": f"content-{document_id}",
                    "score": 1.0,
                    "metadata": {"document_id": document_id},
                }
            ]

        pipeline.get_document_summary_context = summary_context
        output = tool.execute(
            "ask",
            query="联合总结",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
        )

        self.assertEqual(len(tool.llm.prompts), 2)
        self.assertIn("doc-2", tool.llm.prompts[-1])
        self.assertIn("单篇摘要失败", tool.llm.prompts[-1])
        self.assertNotIn("map boom", tool.llm.prompts[-1])
        self.assertIn("ANSWER-2", output)

    def test_summary_excludes_shared_graph_after_partial_failure(self):
        for use_langgraph in (False, True):
            for graph_mode, document_ids in (
                ("auto", ["doc-1", "doc-2"]),
                ("auto", ["doc-1", "doc-2", "doc-3"]),
                ("required", ["doc-1", "doc-2"]),
            ):
                with self.subTest(langgraph=use_langgraph, mode=graph_mode, scope=document_ids):
                    tool = self.make_tool()
                    graph = FakeGraphContextService()
                    tool.graph_service = graph
                    pipeline = tool._pipelines["test"]
                    sample = pipeline.get_document_summary_context

                    def sample_with_failure(document_id, limit):
                        if document_id == "doc-1":
                            raise RuntimeError("controlled map failure")
                        return sample(document_id, limit)

                    pipeline.get_document_summary_context = sample_with_failure
                    result = tool.execute_result(
                        "ask", query="summary graph", document_ids=document_ids,
                        mode="summary", graph_mode=graph_mode, use_summary_workflow=use_langgraph,
                    )

                    self.assertTrue(result.success)
                    self.assertIn("以下文档未纳入本次总结：doc-1", result.message)
                    self.assertNotIn("shared-alpha", tool.llm.prompts[-1])
                    self.assertNotIn("shared-alpha", result.message)
                    self.assertEqual(result.data["summary_documents"], len(document_ids) - 1)
                    self.assertEqual(result.data["graph_documents"], document_ids[1:])
                    self.assertEqual(
                        [source["document_id"] for source in result.data["graph_sources"]],
                        document_ids[1:],
                    )
                    self.assertEqual(
                        {source["document_id"] for source in result.data["sources"]},
                        set(document_ids[1:]),
                    )
                    self.assertEqual([call[0] for call in graph.cross_calls], [document_ids])

    def test_summary_all_failures_skip_reduce(self):
        tool = self.make_tool()
        pipeline = tool._pipelines["test"]

        def fail(document_id, limit):
            raise RuntimeError(f"failed-{document_id}")

        pipeline.get_document_summary_context = fail
        output = tool.execute(
            "ask",
            query="联合总结",
            document_ids=["doc-1", "doc-2"],
            mode="summary",
        )

        self.assertEqual(tool.llm.prompts, [])
        self.assertIn("doc-1", output)
        self.assertIn("doc-2", output)

    def test_joint_graph_context_is_limited_to_selected_documents(self):
        tool = self.make_tool()

        class Graph:
            def __init__(self):
                self.calls = []

            def get_graph_context(self, document_id, query, **kwargs):
                self.calls.append(document_id)
                return {
                    "success": True,
                    "document_id": document_id,
                    "status": "ready",
                    "data": {
                        "entities": [{
                            "id": f"{document_id}:concept",
                            "type": "Concept",
                            "name": "alpha",
                        }],
                        "relations": [],
                    },
                }

        graph = Graph()
        tool.graph_service = graph

        output = tool.execute(
            "ask",
            query="alpha",
            document_ids=["doc-1", "doc-2"],
            mode="joint",
            graph_mode="auto",
        )

        self.assertIn("G-", output)
        self.assertEqual(graph.calls, ["doc-1", "doc-2"])

    def test_constructor_passes_explicit_cache_path_to_default_pipeline(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        cache_path = Path(tmpdir.name) / "rag-cache.json"

        tool = RAGTool(rag_namespace="scoped", cache_path=str(cache_path))
        try:
            self.assertEqual(tool._pipelines["scoped"].cache_path, cache_path)
        finally:
            tool.close()


if __name__ == "__main__":
    unittest.main()
