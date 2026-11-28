# hello_agents/tools/builtin/rag_tool.py

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import re
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import ContextVar, copy_context
from inspect import Parameter, signature
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Dict, List, Optional

from openai import APIConnectionError

from hello_agents.tools.base import Tool
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.memory.rag.contracts import DocumentSegment, RAGActionResult
from hello_agents.memory.rag.errors import (
    RAGAuthenticationError,
    RAGCollectionError,
    RAGConfigError,
    RAGConnectionError,
    RAGDocumentTooLargeError,
    RAGEmbeddingError,
    RAGOperationError,
    sanitize_error_message,
    sanitize_qdrant_url,
)
from hello_agents.memory.rag.pipeline import create_rag_pipeline
from hello_agents.memory.rag.prepare import report_progress
from hello_agents.memory.rag.result_utils import (
    lexical_overlap_score,
    normalize_document_scope,
    resolve_qa_mode,
)
from hello_agents.memory.graph.contracts import graph_response
from hello_agents.tools.builtin.rag_context import (
    ANSWER_OUTPUT_TOKEN_RESERVE,
    COMPARE_BASE_CHUNKS_PER_DOC,
    COMPARE_EXTRA_CHUNKS_PER_DOC,
    MAX_SELECTED_DOCUMENTS,
    MIN_TRUNCATED_CHARS,
    SUMMARY_MAX_WORKERS,
    ContextCapacityError,
    citation_id,
    context_budget,
    estimate_tokens,
    fit_context,
    summary_output_tokens,
)
from hello_agents.tools.builtin.rag_compare import (
    parse_structured_comparison,
    render_comparison_markdown,
)

from hello_agents.tools.builtin.rag_summary_workflow import (
    IncompleteSummaryOutput,
    SummaryCancelled,
    SummaryCapacityError,
    SummaryOwnershipCheckError,
    SummaryProgressAborted,
    SummaryState,
    run_summary_workflow,
    verify_summary_citations,
)


GRAPH_CONTEXT_HEADING = "图谱上下文（仅作为补充证据）："


class RAGTool(Tool):
    """RAG工具

    支持：
    - add_text：添加文本知识
    - add_document：添加 txt / md / pdf 文档
    - search：检索知识库
    - ask：基于检索结果问答
    - stats：查看知识库统计
    - clear：清空知识库
    """

    SUMMARY_CACHE_PROMPT_VERSION = "document-summary-v3"
    SUMMARY_CACHE_MAX_ENTRIES = 256

    def __init__(
        self,
        knowledge_base_path: str = "./knowledge_base",
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        collection_name: str = "rag_knowledge_base",
        rag_namespace: str = "default",
        cache_path: Optional[str] = None,
        graph_service: Any = None,
        enable_graph: Optional[bool] = None,
        graph_state_path: Optional[str] = None,
        data_root: Path | str | None = None,
    ):
        super().__init__(
            name="rag",
            description="RAG工具 - 支持文档添加、知识库检索和文档问答"
        )

        self.knowledge_base_path = knowledge_base_path
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key
        self.collection_name = collection_name
        self.rag_namespace = rag_namespace
        self.cache_path = cache_path
        self.data_root = Path(data_root) if data_root is not None else None
        self.graph_service = graph_service
        self.graph_configuration_error: Optional[str] = None

        self._pipelines: Dict[str, Any] = {}
        self._document_summary_cache: Dict[str, Dict[str, Any]] = {}
        self._document_summary_cache_lock = RLock()
        self._action_data_context = ContextVar(
            f"rag_action_data_{id(self)}", default={}
        )
        self._action_error_context = ContextVar(
            f"rag_action_error_{id(self)}", default=None
        )

        self.llm = HelloAgentsLLM()

        default_pipeline = create_rag_pipeline(
            qdrant_url=self.qdrant_url,
            qdrant_api_key=self.qdrant_api_key,
            collection_name=self.collection_name,
            rag_namespace=self.rag_namespace,
            cache_path=self.cache_path,
            data_root=self.data_root,
        )

        self._pipelines[self.rag_namespace] = default_pipeline

        if self.graph_service is None and enable_graph is not False:
            self._configure_graph_service(
                graph_state_path=graph_state_path,
                required=enable_graph is True,
            )

    @property
    def _last_action_data(self) -> Dict[str, Any]:
        context = getattr(self, "_action_data_context", None)
        if context is None:
            context = ContextVar(f"rag_action_data_{id(self)}", default={})
            self._action_data_context = context
        return context.get()

    @_last_action_data.setter
    def _last_action_data(self, value: Dict[str, Any]) -> None:
        context = getattr(self, "_action_data_context", None)
        if context is None:
            context = ContextVar(f"rag_action_data_{id(self)}", default={})
            self._action_data_context = context
        context.set(dict(value or {}))

    @property
    def _last_action_error(self) -> BaseException | None:
        context = getattr(self, "_action_error_context", None)
        if context is None:
            context = ContextVar(f"rag_action_error_{id(self)}", default=None)
            self._action_error_context = context
        return context.get()

    @_last_action_error.setter
    def _last_action_error(self, value: BaseException | None) -> None:
        context = getattr(self, "_action_error_context", None)
        if context is None:
            context = ContextVar(f"rag_action_error_{id(self)}", default=None)
            self._action_error_context = context
        context.set(value)

    def _summary_cache_key(
        self,
        *,
        pipeline: Any,
        document_id: str,
        query: str,
        limit: int,
        results: List[Dict[str, Any]],
        max_output_tokens: int,
        input_budget: int,
        model_id: str,
        graph_mode: str = "off",
        graph_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        revision_parts = []
        for result in results:
            metadata = result.get("metadata", {}) or {}
            revision_parts.append(
                {
                    "id": str(result.get("id", "")),
                    "version": metadata.get("document_version"),
                    "content": hashlib.sha256(
                        str(result.get("content", "")).encode("utf-8")
                    ).hexdigest(),
                }
            )
        payload = {
            "namespace": str(getattr(pipeline, "rag_namespace", self.rag_namespace)),
            "document_id": str(document_id),
            "query": str(query),
            "limit": int(limit),
            "prompt_version": self.SUMMARY_CACHE_PROMPT_VERSION,
            "max_output_tokens": max_output_tokens,
            "input_budget": input_budget,
            "model_id": model_id,
            "revision": revision_parts,
            "graph_mode": str(graph_mode),
            "graph_context": graph_context or {},
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _summary_cache_state(self) -> tuple[Dict[str, Dict[str, Any]], RLock]:
        cache = getattr(self, "_document_summary_cache", None)
        lock = getattr(self, "_document_summary_cache_lock", None)
        if cache is None or lock is None:
            self._document_summary_cache = {}
            self._document_summary_cache_lock = RLock()
        return self._document_summary_cache, self._document_summary_cache_lock

    def _get_cached_document_summary(
        self,
        cache_key: str,
    ) -> Optional[Dict[str, Any]]:
        cache, lock = self._summary_cache_state()
        with lock:
            cached = cache.get(cache_key)
            return copy.deepcopy(cached) if cached is not None else None

    def _put_cached_document_summary(
        self,
        cache_key: str,
        value: Dict[str, Any],
    ) -> None:
        cache, lock = self._summary_cache_state()
        with lock:
            cache[cache_key] = copy.deepcopy(value)
            while len(cache) > self.SUMMARY_CACHE_MAX_ENTRIES:
                cache.pop(next(iter(cache)))

    def _configure_graph_service(
        self,
        *,
        graph_state_path: Optional[str],
        required: bool,
    ) -> None:
        uri = os.getenv("NEO4J_URI")
        username = os.getenv("NEO4J_USERNAME", "neo4j")
        password = os.getenv("NEO4J_PASSWORD")
        database = os.getenv("NEO4J_DATABASE", "neo4j")
        if not uri or not username or not password:
            if required:
                self.graph_configuration_error = (
                    "NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD are required"
                )
            return
        store = None
        try:
            from hello_agents.memory.graph.extractor import GraphExtractor
            from hello_agents.memory.graph.service import KnowledgeGraphService
            from hello_agents.memory.graph.state import GraphStateRepository
            from hello_agents.memory.storage.neo4j_store import Neo4jGraphStore

            store = Neo4jGraphStore(
                uri=uri,
                username=username,
                password=password,
                database=database,
            )
            state_path = Path(graph_state_path) if graph_state_path else (
                Path(self.knowledge_base_path)
                / ".graph"
                / f"{self._safe_namespace(self.rag_namespace)}.json"
            )
            repository = GraphStateRepository(
                state_path,
                secrets=(uri, username, password),
            )
            self.graph_service = KnowledgeGraphService(
                store=store,
                extractor=GraphExtractor(HelloAgentsLLM(max_retries=0)),
                state_repository=repository,
                chunk_loader=lambda document_id: self._get_pipeline(
                    self.rag_namespace
                ).get_document_chunks(document_id),
                rag_namespace=self.rag_namespace,
            )
        except Exception as error:
            close = getattr(store, "close", None)
            if callable(close):
                close()
            self.graph_configuration_error = (
                f"Graph configuration failed: {error.__class__.__name__}"
            )

    @staticmethod
    def _safe_namespace(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "default"))

    def get_parameters(self) -> Dict[str, Any]:
        """Tool 抽象方法：参数说明"""

        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "操作类型：add_text/add_document/search/ask/stats/"
                        "delete_document/clear/graph_status/"
                        "get_document_graph/get_chapter_tree/"
                        "get_concept_relations/get_knowledge_dependencies/"
                        "get_person_relations/retry_document_graph/"
                        "delete_document_graph"
                    )
                },
                "text": {
                    "type": "string",
                    "description": "要添加的文本"
                },
                "file_path": {
                    "type": "string",
                    "description": "要导入的本地文档路径，支持 .txt / .md / .pdf / .docx"
                },
                "query": {
                    "type": "string",
                    "description": "检索或问答的问题"
                },
                "document_id": {
                    "type": "string",
                    "description": "文档 ID，可用于限制只检索某个文档"
                },
                "limit": {
                    "type": "integer",
                    "description": "返回结果数量"
                },
                "min_score": {
                    "type": "number",
                    "description": "最低相似度分数"
                },
                "retrieval_mode": {
                    "type": "string",
                    "enum": ["vector", "hybrid"],
                    "description": "hybrid retrieval mode",
                },
                "use_mmr": {
                    "type": "boolean",
                    "description": "enable MMR diversity reranking",
                },
                "mmr_lambda": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "MMR relevance/diversity trade-off",
                },
                "vector_weight": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "vector score weight in hybrid mode",
                },
                "structured_output": {
                    "type": "boolean",
                    "description": "request validated structured comparison output",
                },
                "rag_namespace": {
                    "type": "string",
                    "description": "RAG 命名空间"
                },
                "concept": {"type": "string", "description": "可选概念名称"},
                "knowledge_point": {"type": "string", "description": "可选知识点名称"},
                "person": {"type": "string", "description": "可选人物名称"},
                "cursor": {"type": "string", "description": "关系分页游标"},
                "node_cursor": {"type": "string", "description": "节点分页游标"},
                "relation_cursor": {"type": "string", "description": "关系分页游标"},
                "node_limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "relation_limit": {"type": "integer", "minimum": 1, "maximum": 500},
                "include_chunk_content": {
                    "type": "boolean",
                    "description": "是否在完整图查询中返回 Chunk 正文"
                },
                "graph_mode": {
                    "type": "string",
                    "enum": ["off", "auto", "required"],
                    "description": "ask graph context mode",
                },
                "graph_node_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                },
                "graph_relation_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": ["action"]
        }

    def run(self, action: Optional[str] = None, **kwargs) -> str:
        """Tool 抽象方法：运行入口"""

        if action is None:
            action = kwargs.pop("action", None)

        return self.execute(action, **kwargs)

    def execute(self, action: Optional[str] = None, **kwargs) -> str:
        """RAGTool 统一执行入口"""

        self._last_action_error = None
        if not action:
            return "❌ 请提供 action，例如 add_text/add_document/search/ask/stats/clear"

        action = action.lower().strip()

        try:
            if action == "add_text":
                return self._add_text(**kwargs)

            if action == "add_document":
                return self._add_document(**kwargs)

            if action == "search":
                return self._search(**kwargs)

            if action == "get_document_chunk":
                return self._get_document_chunk(**kwargs)

            if action in ["citation", "cite", "format_citations"]:
                return self._citation(**kwargs)

            if action == "ask":
                return self._ask(**kwargs)

            if action == "stats":
                return self._stats(**kwargs)

            if action == "delete_document":
                return self._delete_document(**kwargs)

            if action in {
                "graph_status",
                "get_document_graph",
                "get_chapter_tree",
                "get_concept_relations",
                "get_knowledge_dependencies",
                "get_person_relations",
                "retry_document_graph",
                "delete_document_graph",
            }:
                return self._graph_action(action, **kwargs)

            if action == "clear":
                return self._clear(**kwargs)

            return f"❌ 不支持的 RAG 操作: {action}"

        except SummaryProgressAborted:
            raise
        except Exception as exc:
            self._last_action_error = exc
            safe_error = self._safe_action_error(exc, kwargs.get("file_path"))
            return f"❌ RAG 操作失败: {safe_error}"

    def execute_result(self, action: Optional[str] = None, **kwargs) -> RAGActionResult:
        """Execute an action and return structured status plus the legacy message."""

        normalized_action = (action or kwargs.get("action") or "").lower().strip()
        self._last_action_data = {}
        message = self.execute(action, **kwargs)
        file_path = kwargs.get("file_path")
        data = self._safe_action_data(
            dict(getattr(self, "_last_action_data", {}) or {}),
            file_path=file_path,
        )
        # Only these internally constructed, allowlisted source DTOs carry document
        # data, not exception text. Preserve URLs/Unicode and the bounded excerpt.
        raw_data = self._last_action_data
        if normalized_action == "search" and "results" in raw_data:
            data["results"] = raw_data["results"]
        elif normalized_action == "get_document_chunk" and "chunk" in raw_data:
            data["chunk"] = raw_data["chunk"]
        success = data.get("success")
        if success is None:
            success = not self._looks_like_failure(message)
        data.setdefault("success", bool(success))
        error_code = ""
        retryable = False
        error_summary = ""
        if not success:
            failure = getattr(self, "_last_action_error", None)
            error_code, retryable = self._classify_action_failure(
                failure, normalized_action
            )
            error_summary = self._safe_action_error(
                failure or message, file_path
            )
            data["error_code"] = error_code
            data["retryable"] = retryable
        return RAGActionResult(
            action=normalized_action,
            success=bool(success),
            message=message,
            data=data,
            error=error_summary,
            error_code=error_code,
            retryable=retryable,
        )

    @staticmethod
    def _classify_action_failure(
        failure: BaseException | None, action: str
    ) -> tuple[str, bool]:
        if isinstance(failure, IncompleteSummaryOutput):
            return "QA_SUMMARY_INCOMPLETE", False
        if isinstance(failure, SummaryCapacityError):
            return "QA_SUMMARY_CONTEXT_CAPACITY", False
        if isinstance(failure, RAGConnectionError):
            return "rag_connection", True
        if isinstance(failure, RAGAuthenticationError):
            return "rag_authentication", False
        if isinstance(failure, RAGConfigError):
            return "rag_config", False
        if isinstance(failure, RAGCollectionError):
            return "rag_collection", False
        if isinstance(failure, RAGDocumentTooLargeError):
            return "rag_document_too_large", False
        if isinstance(failure, RAGEmbeddingError):
            return "rag_embedding", False
        if isinstance(failure, RAGOperationError):
            return "rag_operation", RAGTool._error_is_transient(failure)
        if isinstance(failure, (ValueError, FileNotFoundError)):
            return "document_invalid", False
        if failure is not None:
            return "unexpected_error", False
        if action == "add_document":
            return "document_invalid", False
        return "rag_operation", False

    @staticmethod
    def _error_is_transient(error: BaseException) -> bool:
        explicit = getattr(error, "retryable", None)
        if explicit is not None:
            return bool(explicit)
        status_code = getattr(error, "status_code", None)
        if isinstance(status_code, int):
            return status_code in {408, 425, 429} or status_code >= 500
        text = str(error).lower()
        status_match = re.search(r"\b([45]\d{2})\b", text)
        if status_match:
            status = int(status_match.group(1))
            return status in {408, 425, 429} or status >= 500
        return any(
            marker in text
            for marker in (
                "timeout",
                "timed out",
                "temporarily unavailable",
                "temporary outage",
                "service unavailable",
                "too many requests",
                "rate limit",
                "rate-limit",
                "gateway timeout",
                "request timeout",
                "connection reset",
            )
        )

    def _safe_action_error(
        self, error: object, file_path: object = None
    ) -> str:
        text = str(error or "")
        if file_path:
            path_text = str(file_path)
            text = text.replace(path_text, Path(path_text).name)
        text = sanitize_error_message(
            text, (self.qdrant_api_key or "",)
        )
        text = re.sub(
            r"https?://[^\s<>()]+",
            lambda match: sanitize_qdrant_url(match.group(0)),
            text,
        )
        return text[:500]

    def _safe_action_data(
        self, value: Any, file_path: object = None
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: self._safe_action_data(item, file_path=file_path)
                for key, item in value.items()
                if str(key).lower()
                not in {"file_path", "traceback", "stack", "stack_trace"}
            }
        if isinstance(value, list):
            return [
                self._safe_action_data(item, file_path=file_path)
                for item in value
            ]
        if isinstance(value, tuple):
            return tuple(
                self._safe_action_data(item, file_path=file_path)
                for item in value
            )
        if isinstance(value, str):
            return self._safe_action_error(value, file_path)
        return value

    def _looks_like_failure(self, message: str) -> bool:
        text = str(message or "")
        return any(
            marker in text
            for marker in ("❌", "失败", "不存在", "不支持", "不能", "无效", "为空", "鉂")
        )

    def _get_pipeline(self, rag_namespace: Optional[str] = None):
        """获取指定 namespace 的 RAG 管道"""

        namespace = rag_namespace or self.rag_namespace

        if namespace not in self._pipelines:
            self._pipelines[namespace] = create_rag_pipeline(
                qdrant_url=self.qdrant_url,
                qdrant_api_key=self.qdrant_api_key,
                collection_name=self.collection_name,
                rag_namespace=namespace,
                cache_path=self.cache_path,
                data_root=self.data_root,
            )

        return self._pipelines[namespace]

    def _graph_unavailable(self, document_id: str) -> Dict[str, Any]:
        return graph_response(
            success=False,
            document_id=str(document_id or ""),
            status="unavailable",
            data={},
            error_type="GraphNotConfigured",
            error_message=(
                self.graph_configuration_error
                or "Neo4j graph service is not configured"
            ),
        )

    def _build_graph_after_import(
        self,
        pipeline: Any,
        document_id: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        pipeline_namespace = getattr(
            pipeline,
            "rag_namespace",
            self.rag_namespace,
        )
        if pipeline_namespace != self.rag_namespace:
            return self._graph_scope_mismatch(
                document_id,
                pipeline_namespace,
            )
        service = getattr(self, "graph_service", None)
        if service is None:
            return self._graph_unavailable(document_id)
        try:
            chunks = pipeline.get_document_chunks(document_id)
            return service.build_document_graph(
                document_id,
                chunks,
                metadata,
            )
        except Exception as error:
            return graph_response(
                success=False,
                document_id=document_id,
                status="failed",
                data={},
                error_type=error.__class__.__name__,
                error_message=f"Graph build failed: {error.__class__.__name__}",
            )

    def _graph_action(
        self,
        action: str,
        document_id: str = "",
        **kwargs,
    ) -> str:
        document_id = str(document_id or "").strip()
        requested_namespace = kwargs.pop("rag_namespace", None)
        if (
            requested_namespace
            and str(requested_namespace) != self.rag_namespace
        ):
            envelope = self._graph_scope_mismatch(
                document_id,
                str(requested_namespace),
            )
        elif not document_id:
            envelope = graph_response(
                success=False,
                document_id="",
                status="pending",
                data={},
                error_type="ValueError",
                error_message="document_id is required",
            )
        elif getattr(self, "graph_service", None) is None:
            envelope = self._graph_unavailable(document_id)
        else:
            service = self.graph_service
            method_names = {
                "graph_status": "get_graph_status",
                "get_document_graph": "get_document_graph",
                "get_chapter_tree": "get_chapter_tree",
                "get_concept_relations": "get_concept_relations",
                "get_knowledge_dependencies": "get_knowledge_dependencies",
                "get_person_relations": "get_person_relations",
                "retry_document_graph": "retry_document_graph",
                "delete_document_graph": "delete_document_graph",
            }
            allowed = {
                "get_document_graph": {
                    "node_cursor",
                    "relation_cursor",
                    "node_limit",
                    "relation_limit",
                    "include_chunk_content",
                },
                "get_concept_relations": {"concept", "cursor", "limit"},
                "get_knowledge_dependencies": {
                    "knowledge_point",
                    "cursor",
                    "limit",
                },
                "get_person_relations": {"person", "cursor", "limit"},
            }.get(action, set())
            arguments = {
                key: value for key, value in kwargs.items() if key in allowed
            }
            envelope = getattr(service, method_names[action])(
                document_id,
                **arguments,
            )
        self._last_action_data = dict(envelope)
        return json.dumps(envelope, ensure_ascii=False)

    def _graph_scope_mismatch(
        self,
        document_id: str,
        requested_namespace: str,
    ) -> Dict[str, Any]:
        return graph_response(
            success=False,
            document_id=str(document_id or ""),
            status="unavailable",
            data={},
            error_type="GraphScopeMismatch",
            error_message=(
                "Graph service is bound to another RAG namespace"
            ),
        )

    def _delete_graph_after_rag(
        self,
        pipeline: Any,
        document_id: str,
    ) -> Dict[str, Any]:
        pipeline_namespace = getattr(
            pipeline,
            "rag_namespace",
            self.rag_namespace,
        )
        if pipeline_namespace != self.rag_namespace:
            return self._graph_scope_mismatch(
                document_id,
                pipeline_namespace,
            )
        service = getattr(self, "graph_service", None)
        if service is None:
            return self._graph_unavailable(document_id)
        try:
            return service.delete_document_graph(document_id)
        except Exception as error:
            return graph_response(
                success=False,
                document_id=document_id,
                status="cleanup_pending",
                data={},
                error_type=error.__class__.__name__,
                error_message=(
                    f"Graph cleanup failed: {error.__class__.__name__}"
                ),
            )

    def _add_text(
            self,
            text: str,
            document_id: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            rag_namespace: Optional[str] = None,
            replace_existing: bool = True,
            save_cache: bool = True,
            **kwargs
    ) -> str:
        """添加文本知识"""

        pipeline = self._get_pipeline(rag_namespace)

        result = pipeline.add_text(
            text=text,
            document_id=document_id,
            metadata=metadata or {},
            replace_existing=replace_existing,
            save_cache=save_cache,
        )

        if result.get("success"):
            msg = (
                f"✅ 文本知识添加成功\n"
                f"- document_id: {result.get('document_id')}\n"
                f"- chunks_added: {result.get('chunks_added')}"
            )

            if result.get("chunks_removed"):
                msg += f"\n- chunks_removed: {result.get('chunks_removed')}"

            if result.get("cache_path"):
                msg += f"\n- cache_path: {result.get('cache_path')}"

            return msg

        return f"❌ 文本知识添加失败: {result.get('message')}"

    def _add_document(
            self,
            file_path: str,
            document_id: Optional[str] = None,
            metadata: Optional[Dict[str, Any]] = None,
            rag_namespace: Optional[str] = None,
            encoding: str = "utf-8-sig",
            replace_existing: bool = True,
            **kwargs
    ) -> str:
        """添加本地 txt / md / pdf / docx 文档到知识库"""

        progress_callback = kwargs.pop("progress_callback", None)
        path = Path(file_path)

        if not path.exists():
            self._last_action_data = {
                "success": False,
                "document_id": document_id or path.stem,
                "file_path": str(path),
            }
            return f"❌ 文件不存在: {file_path}"

        if not path.is_file():
            self._last_action_data = {
                "success": False,
                "document_id": document_id or path.stem,
                "file_path": str(path),
            }
            return f"❌ 不是有效文件: {file_path}"

        suffix = path.suffix.lower()

        # ✅ 修改点 1：这里加入 .docx
        if suffix not in [".txt", ".md", ".pdf", ".docx"]:
            self._last_action_data = {
                "success": False,
                "document_id": document_id or path.stem,
                "file_path": str(path),
                "file_suffix": suffix,
            }
            return f"❌ 当前 add_document 只支持 .txt / .md / .pdf / .docx 文件，当前文件类型: {suffix}"

        document_id = document_id or path.stem

        if suffix == ".pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                pipeline = self._get_pipeline(rag_namespace)
                segments = []
                total_pages = len(reader.pages)

                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    report_progress(
                        progress_callback,
                        "parsing",
                        i + 1,
                        total_pages,
                        "parsing",
                    )
                    if not page_text.strip():
                        continue

                    page_number = i + 1
                    page_metadata = {
                        "file_path": str(path),
                        "file_name": path.name,
                        "file_suffix": suffix,
                        "document_id": document_id,
                        "source_type": "pdf",
                        "page_number": page_number,
                        **(metadata or {}),
                    }
                    segments.append(DocumentSegment(content=page_text, metadata=page_metadata))

                if not segments:
                    return f"鉂?PDF 鏈彁鍙栧埌鏈夋晥鏂囨湰: {file_path}"

                if hasattr(pipeline, "replace_document"):
                    pipeline_kwargs = {
                        "document_id": document_id,
                        "segments": segments,
                        "save_cache": True,
                    }
                    if progress_callback is not None:
                        pipeline_kwargs["progress_callback"] = progress_callback
                    result = pipeline.replace_document(**pipeline_kwargs)
                else:
                    pipeline_kwargs = {
                        "text": "\n\n".join(segment.content for segment in segments),
                        "document_id": document_id,
                        "metadata": {
                            "file_path": str(path),
                            "file_name": path.name,
                            "file_suffix": suffix,
                            "document_id": document_id,
                            "source_type": "pdf",
                            **(metadata or {}),
                        },
                        "replace_existing": replace_existing,
                    }
                    if progress_callback is not None:
                        pipeline_kwargs["progress_callback"] = progress_callback
                    result = pipeline.add_text(**pipeline_kwargs)

                if not result.get("success"):
                    self._last_action_data = {
                        **result,
                        "success": False,
                        "document_id": document_id,
                        "file_path": str(path),
                        "file_suffix": suffix,
                    }
                    return f"鉂?PDF 鏂囨。娣诲姞澶辫触: {result.get('message')}"

                graph_result = self._build_graph_after_import(
                    pipeline,
                    document_id,
                    {
                        "name": path.name,
                        "file_name": path.name,
                        "file_path": str(path),
                        "source": str(path),
                        **(metadata or {}),
                    },
                )
                self._last_action_data = {
                    **result,
                    "success": True,
                    "document_id": document_id,
                    "file_path": str(path),
                    "file_suffix": suffix,
                    "pages_added": len(segments),
                    "graph": graph_result,
                }
                return (
                    f"鉁?PDF 鏂囨。娣诲姞鎴愬姛\n"
                    f"- document_id: {document_id}\n"
                    f"- pages_added: {len(segments)}\n"
                    f"- chunks_added: {result.get('chunks_added', 0)}\n"
                    f"- chunks_removed: {result.get('chunks_removed', 0)}\n"
                    f"- graph_status: {graph_result.get('status')}"
                )

            except Exception as exc:
                self._last_action_error = exc
                return f"鉂?鏂囦欢璇诲彇澶辫触: {self._safe_action_error(exc, file_path)}"

        try:
            if suffix in [".txt", ".md"]:
                try:
                    text = path.read_text(encoding=encoding)
                except UnicodeDecodeError:
                    text = path.read_text(encoding="gbk")
                report_progress(progress_callback, "parsing", 1, 1, "parsing")

            # ✅ 修改点 2：新增 Word 文档读取分支
            elif suffix == ".docx":
                from docx import Document

                doc = Document(str(path))
                paragraphs = []

                for para in doc.paragraphs:
                    text_part = para.text.strip()
                    if text_part:
                        paragraphs.append(text_part)

                text = "\n\n".join(paragraphs)
                report_progress(progress_callback, "parsing", 1, 1, "parsing")

                if not text.strip():
                    return f"❌ Word 文档未提取到有效文本: {file_path}"

            elif suffix == ".pdf":

                from pypdf import PdfReader

                reader = PdfReader(str(path))

                pipeline = self._get_pipeline(rag_namespace)

                # 先删除同一个 document_id 的旧 chunks，避免重复导入
                removed = 0

                if replace_existing and hasattr(pipeline, "_remove_document_chunks"):
                    removed = pipeline._remove_document_chunks(document_id)

                total_chunks = 0
                total_pages = 0

                for i, page in enumerate(reader.pages):

                    page_number = i + 1

                    page_text = page.extract_text() or ""

                    if not page_text.strip():
                        continue

                    total_pages += 1

                    page_metadata = {
                        "file_path": str(path),
                        "file_name": path.name,
                        "file_suffix": suffix,
                        "document_id": document_id,
                        "source_type": "pdf",
                        "page_number": page_number,
                        **(metadata or {})
                    }

                    # 关键：每页添加时不保存缓存
                    result = pipeline.add_text(
                        text=page_text,
                        document_id=document_id,
                        metadata=page_metadata,
                        replace_existing=False,
                        save_cache=False,
                    )

                    if result.get("success"):
                        total_chunks += int(result.get("chunks_added", 0))

                if total_chunks == 0:
                    return f"❌ PDF 未提取到有效文本: {file_path}"

                # 关键：所有页处理完后，只保存一次缓存
                if hasattr(pipeline, "_save_cache"):
                    pipeline._save_cache()

                return (
                    f"✅ PDF 文档添加成功\n"
                    f"- document_id: {document_id}\n"
                    f"- pages_added: {total_pages}\n"
                    f"- chunks_added: {total_chunks}\n"
                    f"- chunks_removed: {removed}"
                )

            else:
                return f"❌ 不支持的文件类型: {suffix}"

        except Exception as exc:
            self._last_action_error = exc
            return f"❌ 文件读取失败: {self._safe_action_error(exc, file_path)}"

        if not text.strip():
            return f"❌ 文件内容为空: {file_path}"

        final_metadata = {
            "file_path": str(path),
            "file_name": path.name,
            "file_suffix": suffix,
            "document_id": document_id,
            "source_type": "document",
            **(metadata or {})
        }

        # PDF 当前是“页码写入正文”的简单方案。
        # 后续如果要精确页码，需要改成一页一页 add_text。
        if suffix == ".pdf":
            final_metadata["page_number"] = ""
            final_metadata["page_note"] = "页码已写入 chunk 正文标题中，例如：# 第 1 页"

        pipeline = self._get_pipeline(rag_namespace)

        if hasattr(pipeline, "replace_document"):
            pipeline_kwargs = {
                "document_id": document_id,
                "segments": [DocumentSegment(content=text, metadata=final_metadata)],
                "save_cache": True,
            }
            if progress_callback is not None:
                pipeline_kwargs["progress_callback"] = progress_callback
            result = pipeline.replace_document(**pipeline_kwargs)
        else:
            pipeline_kwargs = {
                "text": text,
                "document_id": document_id,
                "metadata": final_metadata,
                "replace_existing": replace_existing,
            }
            if progress_callback is not None:
                pipeline_kwargs["progress_callback"] = progress_callback
            result = pipeline.add_text(**pipeline_kwargs)

        if result.get("success"):
            graph_result = self._build_graph_after_import(
                pipeline,
                document_id,
                {
                    "name": path.name,
                    "file_name": path.name,
                    "file_path": str(path),
                    "source": str(path),
                    **(metadata or {}),
                },
            )
            self._last_action_data = {
                **result,
                "success": True,
                "document_id": result.get("document_id") or document_id,
                "file_path": str(path),
                "file_suffix": suffix,
                "graph": graph_result,
            }
            return (
                f"鉁?鏂囨。娣诲姞鎴愬姛\n"
                f"- document_id: {result.get('document_id')}\n"
                f"- chunks_added: {result.get('chunks_added')}\n"
                f"- chunks_removed: {result.get('chunks_removed', 0)}\n"
                f"- graph_status: {graph_result.get('status')}"
            )

        return f"鉂?鏂囨。娣诲姞澶辫触: {result.get('message')}"

    def _search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.0,
        rag_namespace: Optional[str] = None,
        document_id: Optional[str] = None,
        **kwargs
    ) -> str:
        """搜索知识库"""

        pipeline = self._get_pipeline(rag_namespace)

        results = pipeline.search(
            query=query,
            limit=limit,
            min_score=min_score,
            document_id=document_id,
        )

        if not results:
            if document_id:
                return f"🔍 未在文档 {document_id} 中找到与 '{query}' 相关的知识"
            return f"🔍 未找到与 '{query}' 相关的知识"

        lines = [f"🔍 找到 {len(results)} 条相关知识:"]

        for i, item in enumerate(results, start=1):
            content = item.get("content", "")
            score = float(item.get("score", 0.0))
            metadata = item.get("metadata", {}) or {}

            content = self._clean_preview_text(content, max_len=220)
            source_text = self._format_source(metadata)

            page_number = metadata.get("page_number", "")
            file_name = metadata.get("file_name", "")
            title = f"{i}. "

            if page_number:
                title += f"第 {page_number} 页"
            else:
                title += "未知页码"

            title += f" | score: {score:.4f}"

            if source_text:
                lines.append(
                    f"{title}\n"
                    f"来源: {source_text}\n"
                    f"内容摘要:\n{content}"
                )
            else:
                lines.append(
                    f"{title}\n"
                    f"内容摘要:\n{content}"
                )

        return "\n".join(lines)

    def _citation(
            self,
            query: str,
            limit: int = 5,
            min_score: float = 0.0,
            rag_namespace: Optional[str] = None,
            document_id: Optional[str] = None,
            style: str = "normal",
            **kwargs
    ) -> str:
        """生成可复制引用格式"""

        if not query or not query.strip():
            return "❌ 请提供引用检索关键词"

        pipeline = self._get_pipeline(rag_namespace)

        results = pipeline.search(
            query=query,
            limit=limit,
            min_score=min_score,
            document_id=document_id,
        )

        if not results:
            if document_id:
                return f"🔍 未在文档 {document_id} 中找到与 '{query}' 相关的引用内容"
            return f"🔍 未找到与 '{query}' 相关的引用内容"

        lines = [
            "📌 可复制引用：",
            "",
            f"检索关键词：{query}",
        ]

        if document_id:
            lines.append(f"限定文档：{document_id}")

        lines.append("")

        for i, item in enumerate(results, start=1):
            content = item.get("content", "")
            metadata = item.get("metadata", {}) or {}
            score = float(item.get("score", 0.0))

            file_name = metadata.get("file_name", "未知文件")
            page_number = metadata.get("page_number", "未知页码")

            preview = self._clean_preview_text(content, max_len=260)

            if style == "markdown":
                lines.append(
                    f"> **引用 {i}**  \n"
                    f"> **来源**：《{file_name}》，第 {page_number} 页  \n"
                    f"> **相关度**：{score:.4f}  \n"
                    f"> **内容**：{preview}"
                )
                lines.append("")
            else:
                lines.append(
                    f"{i}. 《{file_name}》，第 {page_number} 页。\n"
                    f"相关度：{score:.4f}\n"
                    f"引用内容：{preview}\n"
                )

        return "\n".join(lines)

    def _stats(
        self,
        rag_namespace: Optional[str] = None,
        **kwargs
    ) -> str:
        """查看知识库统计"""

        pipeline = self._get_pipeline(rag_namespace)
        stats = pipeline.stats()

        lines = [
            "📊 RAG知识库统计:",
            f"- collection_name: {stats.get('collection_name')}",
            f"- rag_namespace: {stats.get('rag_namespace')}",
            f"- document_count: {stats.get('document_count')}",
            f"- chunk_count: {stats.get('chunk_count')}",
            f"- dimension: {stats.get('dimension')}",
        ]

        if stats.get("cache_path"):
            lines.append(f"- cache_path: {stats.get('cache_path')}")

        if stats.get("cache_exists") is not None:
            lines.append(f"- cache_exists: {stats.get('cache_exists')}")

        return "\n".join(lines)

    def _delete_document(
            self,
            document_id: str,
            rag_namespace: Optional[str] = None,
            **kwargs
    ) -> str:
        """删除指定 document_id 的文档知识"""

        if not document_id:
            return "❌ document_id 不能为空"

        pipeline = self._get_pipeline(rag_namespace)

        if not hasattr(pipeline, "delete_document"):
            return "❌ 当前 RAG pipeline 不支持 delete_document"

        result = pipeline.delete_document(document_id)

        if result.get("success"):
            graph_result = self._delete_graph_after_rag(
                pipeline,
                document_id,
            )
            self._last_action_data = {
                **result,
                "success": True,
                "document_id": document_id,
                "graph": graph_result,
            }
            return (
                f"✅ 文档删除成功\n"
                f"- document_id: {result.get('document_id')}\n"
                f"- chunks_removed: {result.get('chunks_removed')}\n"
                f"- cache_path: {result.get('cache_path')}\n"
                f"- graph_status: {graph_result.get('status')}"
            )

        return f"❌ 文档删除失败: {result.get('message')}"

    def _clear(
        self,
        rag_namespace: Optional[str] = None,
        **kwargs
    ) -> str:
        """清空知识库"""

        pipeline = self._get_pipeline(rag_namespace)
        document_ids = (
            pipeline.list_document_ids()
            if hasattr(pipeline, "list_document_ids")
            else []
        )
        result = pipeline.clear()
        graph_results = []
        for document_id in document_ids:
            graph_results.append(
                self._delete_graph_after_rag(pipeline, document_id)
            )
        self._last_action_data = {
            **result,
            "success": bool(result.get("success", True)),
            "graphs": graph_results,
        }
        return result.get("message", "已清空知识库")

    def close(self) -> None:
        service = getattr(self, "graph_service", None)
        close = getattr(service, "close", None)
        if callable(close):
            close()
        self.graph_service = None

    def _search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.0,
        rag_namespace: Optional[str] = None,
        document_id: Optional[str] = None,
        document_ids: Optional[List[str]] = None,
        **kwargs,
    ) -> str:
        """Search the RAG knowledge base with optional document scope."""

        scope = normalize_document_scope(
            document_id=document_id,
            document_ids=document_ids,
        )
        if scope == []:
            return "❌ document_ids 不能为空"
        if scope is not None and len(scope) > MAX_SELECTED_DOCUMENTS:
            return f"❌ 最多选择 {MAX_SELECTED_DOCUMENTS} 篇文档"

        pipeline = self._get_pipeline(rag_namespace)
        retrieval_kwargs = {
            key: kwargs[key]
            for key in ("retrieval_mode", "use_mmr", "mmr_lambda", "vector_weight")
            if key in kwargs
        }
        results = pipeline.search(
            query=query,
            limit=limit,
            min_score=min_score,
            document_id=document_id if document_ids is None else None,
            document_ids=document_ids,
            **retrieval_kwargs,
        )

        if not results:
            self._last_action_data = {"success": True, "results": [], "result_count": 0}
            if scope:
                return f"🔍 未在所选文档 {', '.join(scope)} 中找到与 '{query}' 相关的知识"
            return f"🔍 未找到与 '{query}' 相关的知识"

        structured_results = [self._structured_search_hit(item) for item in results]
        self._last_action_data = {
            "success": True,
            "results": structured_results,
            "result_count": len(structured_results),
        }

        lines = [f"🔍 找到 {len(results)} 条相关知识:"]

        for i, item in enumerate(results, start=1):
            content = self._clean_preview_text(item.get("content", ""), max_len=220)
            score = float(item.get("score", 0.0))
            metadata = item.get("metadata", {}) or {}
            source_text = self._format_source(metadata)
            page_number = metadata.get("page_number", "")
            title = f"{i}. "
            title += f"第 {page_number} 页" if page_number else "未知页码"
            title += f" | score: {score:.4f}"

            if source_text:
                lines.append(f"{title}\n来源: {source_text}\n内容摘要:\n{content}")
            else:
                lines.append(f"{title}\n内容摘要:\n{content}")

        return "\n".join(lines)

    @staticmethod
    def _structured_search_hit(item: Dict[str, Any]) -> Dict[str, Any]:
        metadata = item.get("metadata", {}) or {}
        content = str(item.get("content") or "")
        try:
            chunk_index = int(metadata.get("chunk_index"))
        except (TypeError, ValueError):
            chunk_index = None
        return {
            "document_id": str(metadata.get("document_id") or ""),
            "chunk_id": str(item.get("id") or ""),
            "chunk_index": chunk_index,
            "content": content[:1200],
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "score": float(item.get("score", 0.0)),
            "page_number": metadata.get("page_number"),
            "section": str(metadata.get("section") or "")[:200] or None,
        }

    def _get_document_chunk(
        self,
        document_id: str,
        chunk_id: str,
        chunk_index: int,
        rag_namespace: Optional[str] = None,
        **_kwargs,
    ) -> str:
        document_id = str(document_id or "").strip()
        chunk_id = str(chunk_id or "").strip()
        if not document_id or not chunk_id or int(chunk_index) < 0:
            self._last_action_data = {"success": False, "chunk": None}
            return "❌ 文档片段标识无效"

        pipeline = self._get_pipeline(rag_namespace)
        item = pipeline.get_document_chunk(document_id, chunk_id, int(chunk_index))
        if item is None:
            self._last_action_data = {"success": False, "chunk": None}
            return "❌ 文档片段不存在或已变化"

        self._last_action_data = {
            "success": True, "chunk": self._structured_search_hit({**item, "score": 1.0})
        }
        return "文档片段已解析"

    def _ask(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.0,
        rag_namespace: Optional[str] = None,
        document_id: Optional[str] = None,
        document_ids: Optional[List[str]] = None,
        mode: str = "auto",
        summary_mode: bool = False,
        graph_mode: str = "auto",
        graph_node_limit: int = 8,
        graph_relation_limit: int = 16,
        **kwargs,
    ) -> str:
        """Answer with joint, comparison, or summary behavior."""

        scope = normalize_document_scope(
            document_id=document_id,
            document_ids=document_ids,
        )
        if scope == []:
            return "❌ document_ids 不能为空"

        if scope is not None and len(scope) > MAX_SELECTED_DOCUMENTS:
            return f"❌ 最多选择 {MAX_SELECTED_DOCUMENTS} 篇文档"
        pipeline = self._get_pipeline(rag_namespace)
        selected_mode = self._resolve_qa_mode(query, mode, summary_mode)
        conversation_context = str(kwargs.get("conversation_context") or "").strip()
        retrieval_kwargs = {
            key: kwargs[key]
            for key in ("retrieval_mode", "use_mmr", "mmr_lambda", "vector_weight")
            if key in kwargs
        }
        graph_mode = str(graph_mode or "auto").strip().lower()
        if graph_mode not in {"off", "auto", "required"}:
            return "鉂?graph_mode must be off, auto, or required"

        if selected_mode == "compare":
            if scope is None or len(scope) < 2:
                return "❌ 对比分析至少需要选择 2 篇文档"
            return self._ask_compare(
                pipeline=pipeline,
                query=query,
                document_ids=scope,
                limit=limit,
                min_score=min_score,
                structured_output=bool(kwargs.get("structured_output", False)),
                conversation_context=conversation_context,
                graph_mode=graph_mode,
                graph_node_limit=graph_node_limit,
                graph_relation_limit=graph_relation_limit,
                **retrieval_kwargs,
            )

        if selected_mode == "summary" and scope:
            return self._ask_multi_summary(
                pipeline=pipeline,
                query=query,
                document_ids=scope,
                limit=limit,
                progress_callback=kwargs.get("progress_callback"),
                cancel_event=kwargs.get("cancel_event"),
                use_summary_workflow=bool(kwargs.get("use_summary_workflow"))
                and len(scope) > 1,
                job_id=kwargs.get("summary_job_id"),
                attempt=kwargs.get("summary_attempt"),
                graph_mode=graph_mode,
                graph_node_limit=graph_node_limit,
                graph_relation_limit=graph_relation_limit,
                **retrieval_kwargs,
            )

        if summary_mode and document_id and hasattr(pipeline, "get_document_summary_context"):
            results = pipeline.get_document_summary_context(
                document_id=document_id,
                limit=max(limit, 12),
            )
        else:
            results = pipeline.search(
                query=query,
                limit=limit,
                min_score=min_score,
                document_id=document_id if document_ids is None else None,
                document_ids=document_ids,
                **retrieval_kwargs,
            )

        if not results:
            if scope:
                return f"🔍 未在所选文档 {', '.join(scope)} 中找到与 '{query}' 相关的知识，无法生成答案"
            return f"🔍 未找到与 '{query}' 相关的知识，无法生成答案"

        history_section = self._qa_history_section(conversation_context)
        fixed_prompt = f"""请根据下面资料回答用户问题。

要求：
1. 只能基于资料回答，并使用资料中的稳定引用 ID（例如 [S-abc123]）标注依据。
2. 如果资料中没有相关内容，请明确说明“资料中没有提到”。
3. 回答要简洁、准确。

用户问题：
{query}

{history_section}

资料：

请给出回答：
"""
        try:
            budget = self._context_budget(fixed_prompt)
            context, used_results, truncated = self._build_context(
                results, token_budget=budget, return_details=True
            )
        except ContextCapacityError:
            return self._capacity_error()
        graph_document_ids = (
            list(scope)
            if scope is not None
            else sorted(
                {
                    str((item.get("metadata") or {}).get("document_id") or "")
                    for item in used_results
                    if (item.get("metadata") or {}).get("document_id")
                }
            )
        )
        graph_contexts, graph_error = self._graph_context_for_documents(
            graph_document_ids,
            query=query,
            mode=graph_mode,
            node_limit=graph_node_limit,
            relation_limit=graph_relation_limit,
        )
        if graph_error:
            return graph_error
        graph_text = self._format_graph_context(graph_contexts)
        context = self._append_graph_context(
            context,
            graph_text,
            token_budget=budget,
        )
        graph_sources = self._graph_sources(graph_contexts)
        self._last_action_data = {
            "graph_mode": graph_mode,
            "graph_documents": graph_document_ids,
            "graph_context_count": len(graph_sources),
        }
        prompt = f"""请根据下面资料回答用户问题。

要求：
1. 只能基于资料回答，并使用资料中的稳定引用 ID 标注依据。
2. 如果资料中没有相关内容，请明确说明“资料中没有提到”。
3. 回答要简洁、准确。

用户问题：
{query}

{history_section}

资料：
{context}

请给出回答：
"""
        try:
            answer = self._generate(prompt)
        except ContextCapacityError:
            return self._capacity_error()
        return self._format_answer(
            answer,
            used_results,
            truncated=truncated,
            graph_sources=graph_sources,
        )

    @staticmethod
    def _qa_history_section(conversation_context: str) -> str:
        if not conversation_context:
            return ""
        return (
            "对话上下文（仅用于理解本轮问题，不作为检索查询或事实依据）：\n"
            f"{conversation_context}"
        )

    def _graph_context_for_documents(
        self,
        document_ids: List[str],
        *,
        query: str,
        mode: str,
        node_limit: int,
        relation_limit: int,
        after_lookup: Callable[[], None] | None = None,
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        if mode == "off" or not document_ids:
            return [], None
        service = getattr(self, "graph_service", None)
        getter = getattr(service, "get_graph_context", None)
        if not callable(getter):
            if mode == "required":
                return [], "鉂?GraphRAG required 但 Neo4j 图谱服务不可用"
            return [], None

        contexts: List[Dict[str, Any]] = []
        for document_id in document_ids[:MAX_SELECTED_DOCUMENTS]:
            try:
                result = getter(
                    document_id,
                    query,
                    node_limit=node_limit,
                    relation_limit=relation_limit,
                )
            except Exception as error:
                if after_lookup is not None:
                    after_lookup()
                if mode == "required":
                    return [], f"鉂?GraphRAG 图谱查询失败: {error.__class__.__name__}"
                continue
            if after_lookup is not None:
                after_lookup()
            if not result or not result.get("success"):
                if mode == "required":
                    error = (result or {}).get("error") or {}
                    return [], (
                        "鉂?GraphRAG 图谱不可用: "
                        f"{error.get('type') or (result or {}).get('status') or 'unknown'}"
                    )
                continue
            data = result.get("data") or {}
            if data.get("entities") or data.get("relations"):
                contexts.append(
                    {
                        "document_id": document_id,
                        "entities": list(data.get("entities") or []),
                        "relations": list(data.get("relations") or []),
                    }
                )
            elif mode == "required":
                return [], (
                    "鉂?GraphRAG 图谱不可用: "
                    f"GraphContextEmpty ({document_id})"
                )
        if mode == "required" and not contexts:
            return [], "鉂?GraphRAG required 但没有可用图谱上下文"
        return contexts, None

    def _cross_document_graph_context(
        self,
        document_ids: List[str],
        *,
        query: str,
        mode: str,
    ) -> tuple[List[Dict[str, Any]], Optional[str]]:
        if mode == "off" or len(document_ids) < 2:
            return [], None
        service = getattr(self, "graph_service", None)
        getter = getattr(service, "get_cross_document_entities", None)
        if not callable(getter):
            if mode == "required":
                return [], (
                    "GraphRAG required: cross-document graph service unavailable"
                )
            return [], None
        try:
            result = getter(
                list(document_ids),
                query,
                entity_limit=12,
                evidence_limit=40,
            )
        except Exception as error:
            if mode == "required":
                return [], (
                    "GraphRAG cross-document query failed: "
                    f"{error.__class__.__name__}"
                )
            return [], None
        if not result or not result.get("success"):
            if mode == "required":
                error = (result or {}).get("error") or {}
                return [], (
                    "GraphRAG cross-document context unavailable: "
                    f"{error.get('type') or (result or {}).get('status') or 'unknown'}"
                )
            return [], None
        entities = list((result.get("data") or {}).get("entities") or [])
        if not entities:
            return [], None
        return [
            {
                "kind": "cross_document",
                "document_ids": list(document_ids),
                "entities": entities,
            }
        ], None

    @staticmethod
    def _graph_citation_id(
        document_id: str,
        kind: str,
        graph_id: str,
    ) -> str:
        digest = hashlib.sha1(
            f"{document_id}|{kind}|{graph_id}".encode("utf-8")
        ).hexdigest()[:10]
        return f"G-{digest}"

    def _graph_sources(
        self,
        contexts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        sources: List[Dict[str, Any]] = []
        seen = set()
        for context in contexts:
            if context.get("kind") == "cross_document":
                document_ids = [
                    str(value)
                    for value in context.get("document_ids") or []
                    if str(value)
                ]
                scope_id = "|".join(sorted(document_ids))
                for entity in context.get("entities") or []:
                    graph_id = str(
                        entity.get("canonical_id")
                        or entity.get("normalized_name")
                        or ""
                    )
                    if not graph_id:
                        continue
                    citation_id = self._graph_citation_id(
                        scope_id,
                        "canonical_entity",
                        graph_id,
                    )
                    if citation_id in seen:
                        continue
                    seen.add(citation_id)
                    sources.append(
                        {
                            "citation_id": citation_id,
                            "document_id": ", ".join(document_ids),
                            "document_ids": document_ids,
                            "member_document_ids": sorted(
                                {
                                    member["document_id"]
                                    for member in entity.get("members") or []
                                    if member.get("document_id") in document_ids
                                }
                            ),
                            "kind": "canonical_entity",
                            "type": entity.get("entity_type") or entity.get("type"),
                            "name": entity.get("name") or graph_id,
                            "canonical_id": entity.get("canonical_id"),
                        }
                    )
                continue
            document_id = str(context.get("document_id") or "")
            for entity in context.get("entities") or []:
                graph_id = str(entity.get("id") or "")
                if not graph_id:
                    continue
                citation_id = self._graph_citation_id(
                    document_id,
                    "entity",
                    graph_id,
                )
                if citation_id in seen:
                    continue
                seen.add(citation_id)
                sources.append(
                    {
                        "citation_id": citation_id,
                        "document_id": document_id,
                        "kind": "entity",
                        "type": entity.get("type"),
                        "name": entity.get("name") or graph_id,
                    }
                )
            for relation in context.get("relations") or []:
                relation_id = "|".join(
                    str(relation.get(key) or "")
                    for key in ("source_id", "target_id", "type")
                )
                if not relation_id:
                    continue
                citation_id = self._graph_citation_id(
                    document_id,
                    "relation",
                    relation_id,
                )
                if citation_id in seen:
                    continue
                seen.add(citation_id)
                properties = relation.get("properties") or {}
                sources.append(
                    {
                        "citation_id": citation_id,
                        "document_id": document_id,
                        "kind": "relation",
                        "type": relation.get("type"),
                        "name": properties.get("evidence") or relation_id,
                    }
                )
        return sources

    def _format_graph_context(
        self,
        contexts: List[Dict[str, Any]],
    ) -> str:
        if not contexts:
            return ""
        entries = self._graph_context_entries(contexts)
        return "\n".join([GRAPH_CONTEXT_HEADING, *(line for _, line in entries)])

    def _graph_context_entries(
        self,
        contexts: List[Dict[str, Any]],
    ) -> list[tuple[str, str]]:
        entries = []
        for context in contexts:
            document_id = str(context.get("document_id") or "")
            if context.get("kind") == "cross_document":
                document_ids = [
                    str(value)
                    for value in context.get("document_ids") or []
                    if str(value)
                ]
                scope_label = ", ".join(document_ids)
                for entity in context.get("entities") or []:
                    graph_id = str(
                        entity.get("canonical_id")
                        or entity.get("normalized_name")
                        or ""
                    )
                    if not graph_id:
                        continue
                    citation = self._graph_citation_id(
                        "|".join(sorted(document_ids)),
                        "canonical_entity",
                        graph_id,
                    )
                    members = ", ".join(
                        str(member.get("document_id") or "")
                        for member in entity.get("members") or []
                        if member.get("document_id")
                    )
                    line = (
                        f"[{citation}] cross-document entity | {scope_label} | "
                        f"{entity.get('entity_type') or entity.get('type')}: "
                        f"{entity.get('name') or graph_id} | members: {members}"
                    )
                    entries.append((citation, line))
                continue
            entity_names = {
                str(entity.get("id")): (
                    entity.get("name") or entity.get("id")
                )
                for entity in context.get("entities") or []
                if entity.get("id")
            }
            for entity in context.get("entities") or []:
                graph_id = str(entity.get("id") or "")
                if not graph_id:
                    continue
                citation = self._graph_citation_id(
                    document_id,
                    "entity",
                    graph_id,
                )
                line = (
                    f"[{citation}] 文档 {document_id} | "
                    f"{entity.get('type')}: {entity_names.get(graph_id)}"
                )
                entries.append((citation, line))
            for relation in context.get("relations") or []:
                relation_id = "|".join(
                    str(relation.get(key) or "")
                    for key in ("source_id", "target_id", "type")
                )
                if not relation_id:
                    continue
                citation = self._graph_citation_id(
                    document_id,
                    "relation",
                    relation_id,
                )
                properties = relation.get("properties") or {}
                evidence = properties.get("evidence")
                line = (
                    f"[{citation}] 文档 {document_id} | "
                    f"{entity_names.get(str(relation.get('source_id')), relation.get('source_id'))} "
                    f"-{relation.get('type')}-> "
                    f"{entity_names.get(str(relation.get('target_id')), relation.get('target_id'))}"
                )
                if evidence:
                    line += f" | evidence: {str(evidence)[:240]}"
                entries.append((citation, line))
        return entries

    def _append_graph_context(
        self,
        context: str,
        graph_text: str,
        *,
        token_budget: int,
    ) -> str:
        combined, _ = self._fit_graph_context(
            context, graph_text, graph_text.splitlines(), token_budget=token_budget
        )
        return combined

    def _fit_graph_context(
        self,
        context: str,
        graph_text: str,
        lines: list[str],
        *,
        token_budget: int,
    ) -> tuple[str, int]:
        if not graph_text:
            return context, 0
        prefix = f"{context}\n\n{graph_text}"
        if estimate_tokens(self.llm, prefix) <= token_budget:
            return prefix, len(lines)
        fitted = []
        for line in lines:
            candidate = f"{context}\n\n" + "\n".join(fitted + [line])
            if estimate_tokens(self.llm, candidate) > token_budget:
                break
            fitted.append(line)
        if not fitted:
            return context, 0
        return f"{context}\n\n" + "\n".join(fitted), len(fitted)

    def _append_graph_context_with_sources(
        self,
        context: str,
        contexts: List[Dict[str, Any]],
        *,
        token_budget: int,
    ) -> tuple[str, List[Dict[str, Any]]]:
        """Keep each summary evidence entry and its citation together."""
        entries = self._graph_context_entries(contexts)
        if not entries:
            return context, []
        lines = [GRAPH_CONTEXT_HEADING, *(line for _, line in entries)]
        combined, fitted_count = self._fit_graph_context(
            context, "\n".join(lines), lines, token_budget=token_budget
        )
        if fitted_count <= 1:
            return context, []
        included = {citation for citation, _ in entries[:fitted_count - 1]}
        sources = [
            source for source in self._graph_sources(contexts)
            if source["citation_id"] in included
        ]
        return combined, sources

    def _format_graph_sources(
        self,
        graph_sources: List[Dict[str, Any]],
    ) -> str:
        if not graph_sources:
            return ""
        lines = ["图谱参考："]
        for source in graph_sources:
            lines.append(
                f"[{source['citation_id']}] 文档 {source['document_id']} | "
                f"{source.get('type') or source.get('kind')}: {source.get('name')}"
            )
        return "\n".join(lines)

    def _resolve_qa_mode(self, query: str, mode: str, summary_mode: bool) -> str:
        return resolve_qa_mode(query, mode, summary_mode)

    def _context_budget(
        self,
        fixed_prompt: str,
        output_reserve: int = ANSWER_OUTPUT_TOKEN_RESERVE,
    ) -> int:
        return context_budget(
            self.llm,
            fixed_prompt,
            output_reserve=output_reserve,
        )

    def _capacity_error(self) -> str:
        return (
            "❌ 模型上下文容量不足，无法在保留输出 token 和安全边距后构造资料。"
            "请减少文档、缩短问题，或提高 LLM_CONTEXT_WINDOW_TOKENS。"
        )

    def _citation_id(self, item: Dict[str, Any]) -> str:
        return citation_id(item)

    def _build_context(
        self,
        results: List[Dict[str, Any]],
        token_budget: Optional[int] = None,
        return_details: bool = False,
    ):
        """Build a bounded context without mutating cached/search result objects."""

        budget = token_budget if token_budget is not None else self._context_budget("")
        fitted = fit_context(
            results,
            token_budget=budget,
            llm=self.llm,
            format_source=self._format_source,
        )
        result = (fitted.context, fitted.results, fitted.truncated)
        return result if return_details else fitted.context

    def _generate(
        self,
        prompt: str,
        *,
        strict: bool = False,
        summary_max_tokens: int | None = None,
        summary_model: str | None = None,
        before_retry: Callable[[], None] | None = None,
    ) -> str:
        capacity_markers = (
            "context length",
            "context window",
            "maximum context",
            "max context",
            "too many tokens",
            "token limit",
            "上下文长度",
            "上下文超限",
        )
        model_kwargs = {"model": summary_model} if summary_model is not None else {}
        try:
            generate_with_metadata = getattr(self.llm, "generate_with_metadata", None)
            if summary_max_tokens is not None and callable(generate_with_metadata):
                # Custom clients may accept kwargs only to forward them to their provider.
                chat_with_metadata = getattr(self.llm, "_chat_with_metadata", None)
                if (
                    before_retry is not None
                    and not strict
                    and getattr(generate_with_metadata, "__func__", None)
                    is HelloAgentsLLM.generate_with_metadata
                    and getattr(chat_with_metadata, "__func__", None)
                    is HelloAgentsLLM._chat_with_metadata
                ):
                    model_kwargs["before_retry"] = before_retry
                completion = generate_with_metadata(
                    prompt, max_tokens=summary_max_tokens, strict=strict, **model_kwargs
                )
                if completion.finish_reason == "length":
                    raise IncompleteSummaryOutput("output_truncated")
                if completion.finish_reason in ("content_filter", "safety", "recitation", "blocked"):
                    raise IncompleteSummaryOutput("safety_blocked")
                if completion.finish_reason not in (None, "", "stop"):
                    raise IncompleteSummaryOutput("abnormal_finish")
                answer = completion.text
            else:
                answer = self.llm.generate(prompt, **model_kwargs)
        except Exception as error:
            if getattr(error, "status_code", None) == 400 and any(
                marker in str(error).lower() for marker in capacity_markers
            ):
                raise ContextCapacityError("provider rejected the prompt as too large") from None
            raise

        normalized = str(answer or "").lower()
        if any(marker in normalized for marker in capacity_markers):
            raise ContextCapacityError("provider rejected the prompt as too large")
        return answer

    def _generate_summary(
        self, prompt: str, *, max_tokens: int, strict: bool, model: str | None,
        before_retry: Callable[[], None] | None = None,
    ) -> str:
        answer = self._generate(
            prompt, strict=strict, summary_max_tokens=max_tokens, summary_model=model,
            before_retry=before_retry,
        )
        if not answer.strip() or answer.startswith(("[LLM调用失败]", "[LLM未配置]")):
            raise IncompleteSummaryOutput("empty_output")
        return answer

    def _format_answer(
        self,
        answer: str,
        results: List[Dict[str, Any]],
        truncated: bool = False,
        graph_sources: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        sources = []

        deduped = []
        seen_source_ids = set()
        for item in results:
            source_id = item.get("citation_id") or self._citation_id(item)
            if source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            deduped.append(item)

        for item in deduped:
            metadata = item.get("metadata", {}) or {}
            stable_id = item.get("citation_id") or self._citation_id(item)
            source_text = self._format_source(
                metadata, truncated=bool(item.get("truncated"))
            )
            if source_text:
                sources.append(
                    f"[{stable_id}] "
                    f"{source_text}"
                )

        source_output = "\n".join(sources) if sources else "暂无明确来源信息"
        source_payloads = []
        for item in deduped:
            metadata = item.get("metadata", {}) or {}
            stable_id = item.get("citation_id") or self._citation_id(item)
            document_id = str(metadata.get("document_id") or "")
            page_number = metadata.get("page_number")
            location = document_id
            if page_number not in (None, ""):
                location = f"{location} p.{page_number}".strip()
            source_payloads.append(
                {
                    "citation_id": stable_id,
                    "document_id": document_id,
                    "file_name": (
                        metadata.get("file_name")
                        or metadata.get("document_name")
                        or ""
                    ),
                    "page_number": page_number,
                    "excerpt": self._clean_preview_text(
                        item.get("content", ""),
                        max_len=500,
                    ),
                    "reference": f"[{stable_id}] {location}".strip(),
                    "truncated": bool(item.get("truncated")),
                }
            )
        action_data = dict(getattr(self, "_last_action_data", {}) or {})
        action_data.update(
            sources=source_payloads,
            graph_sources=copy.deepcopy(graph_sources or []),
            context_truncated=bool(truncated),
        )
        self._last_action_data = action_data

        graph_output = self._format_graph_sources(graph_sources or [])
        if graph_output:
            source_output = f"{source_output}\n{graph_output}"
        truncation_note = "\n⚠️ 上下文已截断。" if truncated else ""
        return (
            f"🤖 RAG回答:\n{answer}\n\n"
            f"📚 参考知识条数: {len(deduped)}{truncation_note}\n"
            f"📌 参考来源:\n{source_output}"
        )

    def _ask_compare(
        self,
        pipeline,
        query: str,
        document_ids: List[str],
        limit: int,
        min_score: float,
        structured_output: bool = False,
        conversation_context: str = "",
        graph_mode: str = "auto",
        graph_node_limit: int = 8,
        graph_relation_limit: int = 16,
        **search_kwargs,
    ) -> str:
        results: List[Dict[str, Any]] = []
        extras: List[Dict[str, Any]] = []
        missing: List[str] = []

        for doc_id in document_ids:
            doc_results = pipeline.search(
                query=query,
                limit=COMPARE_BASE_CHUNKS_PER_DOC + COMPARE_EXTRA_CHUNKS_PER_DOC,
                min_score=min_score,
                document_id=doc_id,
                **search_kwargs,
            )
            if not doc_results:
                missing.append(doc_id)
                continue
            base = [
                copy.deepcopy(item)
                for item in doc_results[:COMPARE_BASE_CHUNKS_PER_DOC]
            ]
            for item in base:
                item["_protected"] = True
            results.extend(base)
            extras.extend(
                copy.deepcopy(
                    doc_results[
                        COMPARE_BASE_CHUNKS_PER_DOC:
                        COMPARE_BASE_CHUNKS_PER_DOC + COMPARE_EXTRA_CHUNKS_PER_DOC
                    ]
                )
            )

        remaining_slots = max(0, limit - len(results))
        extras.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        results.extend(extras[:remaining_slots])

        if not results:
            return f"🔍 所选文档中未找到与 '{query}' 相关的知识，无法生成对比分析"

        missing_text = "\n".join(
            f"- {doc_id}: 该文档未提供相关信息" for doc_id in missing
        ) or "无"
        try:
            budget = self._context_budget(
                "对比分析\n"
                f"问题：{query}\n"
                f"{self._qa_history_section(conversation_context)}\n"
                f"信息缺失：{missing_text}"
            )
            context, used_results, truncated = self._build_context(
                results, token_budget=budget, return_details=True
            )
        except ContextCapacityError:
            return self._capacity_error()
        graph_contexts, graph_error = self._graph_context_for_documents(
            document_ids,
            query=query,
            mode=graph_mode,
            node_limit=graph_node_limit,
            relation_limit=graph_relation_limit,
        )
        if graph_error:
            return graph_error
        shared_graph_contexts, graph_error = self._cross_document_graph_context(
            document_ids,
            query=query,
            mode=graph_mode,
        )
        if graph_error:
            return graph_error
        all_graph_contexts = graph_contexts + shared_graph_contexts
        context = self._append_graph_context(
            context,
            self._format_graph_context(all_graph_contexts),
            token_budget=budget,
        )
        graph_sources = self._graph_sources(all_graph_contexts)
        action_data = dict(getattr(self, "_last_action_data", {}) or {})
        action_data.update(
            graph_mode=graph_mode,
            graph_documents=list(document_ids),
            graph_context_count=len(graph_sources),
        )
        self._last_action_data = action_data
        prompt = f"""请基于资料对所选文档进行对比分析。

用户问题：
{query}

{self._qa_history_section(conversation_context)}

请按以下结构回答：
1. 共同点
2. 差异点
3. 逐文档依据（必须使用资料中的稳定引用 ID）
4. 信息缺失或无法比较之处

信息缺失：
{missing_text}

资料：
{context}

请给出对比分析：
"""
        if structured_output:
            prompt += """
请只输出一个 JSON 对象，不要使用代码围栏。结构必须为：
{
  "common_points": [{"text": "...", "citations": ["S-..."]}],
  "differences": [{"topic": "...", "documents": [
    {"document_id": "...", "text": "...", "citations": ["S-..."]}
  ]}],
  "per_document_evidence": [
    {"document_id": "...", "summary": "...", "citations": ["S-..."]}
  ],
  "missing_information": [{"document_id": "...", "note": "..."}]
}
所有 document_id 必须来自所选文档，所有 citations 必须来自资料中的稳定引用 ID。
citations 可以使用向量资料的 S-* ID 或图谱资料的 G-* ID。
"""
        try:
            answer = self._generate(prompt)
        except ContextCapacityError:
            return self._capacity_error()
        if structured_output:
            allowed_citation_ids = {
                item.get("citation_id") or self._citation_id(item)
                for item in used_results
            }
            allowed_citation_ids.update(
                source["citation_id"] for source in graph_sources
            )
            comparison = parse_structured_comparison(
                answer,
                allowed_citation_ids=allowed_citation_ids,
                selected_document_ids=set(document_ids),
            )
            action_data = dict(getattr(self, "_last_action_data", {}) or {})
            if comparison is not None:
                action_data.update(
                    comparison=comparison,
                    comparison_format="structured",
                )
                answer = render_comparison_markdown(comparison)
            else:
                action_data.update(
                    comparison=None,
                    comparison_format="markdown_fallback",
                )
            self._last_action_data = action_data
        return self._format_answer(
            answer,
            used_results,
            truncated=truncated,
            graph_sources=graph_sources,
        )

    def _configured_summary_model(self) -> str | None:
        model = os.getenv("SUMMARY_MODEL_ID", "").strip()
        if not model:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in model):
            raise RAGConfigError("SUMMARY_MODEL_ID 含有控制字符")

        generate = getattr(self.llm, "generate_with_metadata", None)
        if not callable(generate):
            generate = self.llm.generate
        try:
            parameters = signature(generate).parameters.values()
        except (TypeError, ValueError):
            raise RAGConfigError("当前模型客户端不支持为联合总结指定模型") from None
        if not any(
            parameter.kind == Parameter.VAR_KEYWORD
            or (
                parameter.name == "model"
                and parameter.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY)
            )
            for parameter in parameters
        ):
            raise RAGConfigError("当前模型客户端不支持为联合总结指定模型")
        return model

    def _ask_multi_summary(
        self,
        pipeline,
        query: str,
        document_ids: List[str],
        limit: int,
        progress_callback: Any = None,
        cancel_event: Any = None,
        graph_mode: str = "auto",
        graph_node_limit: int = 8,
        graph_relation_limit: int = 16,
        use_summary_workflow: bool = False,
        job_id: str | None = None,
        attempt: int | None = None,
        **search_kwargs,
    ) -> str:
        try:
            map_max_tokens = summary_output_tokens("map")
            reduce_max_tokens = summary_output_tokens("reduce")
        except ContextCapacityError as error:
            raise RAGConfigError(str(error)) from None
        summary_model = self._configured_summary_model()
        effective_model = summary_model or str(getattr(self.llm, "model", "") or "")
        total_documents = len(document_ids)
        work_aborted = Event()
        check_before_model = getattr(cancel_event, "check_before_model", None)

        def check_cancelled() -> None:
            checker = getattr(cancel_event, "is_set", None)
            if work_aborted.is_set() or (callable(checker) and checker()):
                raise SummaryCancelled()

        def check_model_attempt() -> None:
            check_cancelled()
            if callable(check_before_model):
                check_before_model()
                check_cancelled()

        before_retry = check_model_attempt if callable(check_before_model) else None

        def emit_progress(
            stage: str,
            completed: int,
            document_id: Optional[str] = None,
        ) -> None:
            check_cancelled()
            if callable(progress_callback):
                progress_callback(
                    completed=completed,
                    total=total_documents,
                    stage=stage,
                    document_id=document_id,
                )

        def prepare(_: SummaryState) -> SummaryState:
            emit_progress("mapping", 0)
            graph_contexts, graph_error = self._graph_context_for_documents(
                document_ids,
                query=query,
                mode=graph_mode,
                node_limit=graph_node_limit,
                relation_limit=graph_relation_limit,
                after_lookup=lambda: emit_progress("mapping", 0),
            )
            if graph_error:
                return {"error": graph_error}
            check_cancelled()
            shared_graph_contexts, graph_error = self._cross_document_graph_context(
                document_ids,
                query=query,
                mode=graph_mode,
            )
            if graph_mode != "off" and getattr(self, "graph_service", None) is not None:
                emit_progress("mapping", 0)
            if graph_error:
                return {"error": graph_error}
            return {
                "graph_by_document": {
                    str(context.get("document_id") or ""): context
                    for context in graph_contexts
                },
                "shared_graph_contexts": shared_graph_contexts,
            }

        def build_document_summary(
            doc_id: str, graph_context_by_document: dict[str, dict[str, Any]]
        ) -> Dict[str, Any]:
            check_cancelled()
            if hasattr(pipeline, "get_document_summary_context"):
                doc_results = pipeline.get_document_summary_context(
                    document_id=doc_id,
                    limit=max(limit, 12),
                )
            else:
                doc_results = pipeline.search(
                    query=query,
                    limit=max(limit, 12),
                    document_id=doc_id,
                    **search_kwargs,
                )
            # Even if a backend returns a stray hit, keep the requested scope.
            doc_results = [
                item
                for item in doc_results
                if (item.get("metadata") or {}).get("document_id") == doc_id
            ]
            if not doc_results:
                return {
                    "document_id": doc_id,
                    "document_name": doc_id,
                    "summary": "",
                    "source_refs": [],
                    "sources": [],
                    "status": "no_chunks",
                    "error": "文档没有可用的索引内容，可能尚未成功导入或索引已被删除",
                }

            check_cancelled()
            prompt = f"""请仅根据以下资料总结文档 {doc_id}。
优先提取可直接回答用户问题的事实和规则。用简短要点呈现，每项保留稳定引用 ID，不逐条罗列无关或缺失内容。

用户问题：{query}
资料：
"""
            budget = self._context_budget(prompt, output_reserve=map_max_tokens)
            cache_key = self._summary_cache_key(
                pipeline=pipeline,
                document_id=doc_id,
                query=query,
                limit=max(limit, 12),
                results=doc_results,
                graph_mode=graph_mode,
                graph_context=graph_context_by_document.get(doc_id),
                max_output_tokens=map_max_tokens,
                input_budget=budget,
                model_id=effective_model,
            )
            cached = self._get_cached_document_summary(cache_key)
            if cached is not None:
                cached["cache_hit"] = True
                return cached

            prepared = [copy.deepcopy(item) for item in doc_results]
            anchor_indexes = {0, len(prepared) // 2, len(prepared) - 1}
            for index, item in enumerate(prepared):
                item["_protected"] = index in anchor_indexes
                # When the budget removes chunks, keep the user's requested
                # evidence ahead of equally ranked page samples.
                item["score"] = lexical_overlap_score(query, item["content"])

            context, sources, truncated = self._build_context(
                prepared, token_budget=budget, return_details=True
            )
            document_graph_context = graph_context_by_document.get(doc_id)
            document_graph_contexts = (
                [document_graph_context]
                if document_graph_context is not None
                else []
            )
            context, graph_sources = self._append_graph_context_with_sources(
                context,
                document_graph_contexts,
                token_budget=budget,
            )
            check_model_attempt()
            summary = self._generate_summary(
                f"{prompt}{context}\n",
                max_tokens=map_max_tokens,
                strict=use_summary_workflow,
                model=summary_model,
                before_retry=before_retry,
            )
            check_cancelled()
            source_refs = [source["citation_id"] for source in sources + graph_sources]
            summary, invalid_citations = verify_summary_citations(summary, set(source_refs))
            first_metadata = sources[0].get("metadata", {}) or {}
            mapped_result = {
                "document_id": doc_id,
                "document_name": (
                    first_metadata.get("file_name")
                    or first_metadata.get("document_name")
                    or doc_id
                ),
                "status": "ok",
                "summary": summary,
                "source_refs": source_refs,
                "invalid_citations": invalid_citations,
                "sources": sources,
                "graph_sources": graph_sources,
                "truncated": truncated,
                "error": None,
                "cache_hit": False,
            }
            self._put_cached_document_summary(cache_key, mapped_result)
            return mapped_result

        def map_documents(state: SummaryState) -> SummaryState:
            completed = {}
            failures = {}
            retryable = False
            output_issues: Counter[str] = Counter()
            invalid_citations = 0
            capacity_failures = 0
            remaining = iter(document_ids)
            worker_count = min(SUMMARY_MAX_WORKERS, total_documents)
            executor = ThreadPoolExecutor(max_workers=worker_count)

            def submit(doc_id: str):
                check_cancelled()
                return executor.submit(
                    copy_context().run,
                    build_document_summary,
                    doc_id,
                    state["graph_by_document"],
                )

            try:
                check_cancelled()
                pending = {
                    submit(doc_id): doc_id for doc_id in itertools.islice(remaining, worker_count)
                }
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        doc_id = pending.pop(future)
                        try:
                            item = future.result()
                            completed[doc_id] = item
                            invalid_citations += item.get("invalid_citations", 0)
                        except (SummaryCancelled, SummaryProgressAborted, SummaryOwnershipCheckError):
                            raise
                        except IncompleteSummaryOutput as error:
                            failures[doc_id] = str(error)
                            output_issues[error.category] += 1
                            invalid_citations += error.invalid_citations
                        except ContextCapacityError:
                            failures[doc_id] = "单篇摘要超出当前模型容量"
                            capacity_failures += 1
                        except Exception as error:
                            failures[doc_id] = "单篇摘要失败"
                            retryable |= self._summary_error_is_retryable(error)
                        emit_progress("mapping", len(completed) + len(failures), doc_id)
                    # Replenish only after progress confirms that the job still owns its lease.
                    for doc_id in itertools.islice(remaining, len(done)):
                        pending[submit(doc_id)] = doc_id
            finally:
                work_aborted.set()
                executor.shutdown(wait=True, cancel_futures=True)
                work_aborted.clear()

            return {
                "completed": completed,
                "failures": failures,
                "retryable": retryable,
                "incomplete_output": bool(output_issues),
                "output_issues": dict(output_issues),
                "invalid_citations": invalid_citations,
                "capacity_failures": capacity_failures,
            }

        def collect(state: SummaryState) -> SummaryState:
            check_cancelled()
            completed, failures = state["completed"], state["failures"]
            mapped = []
            ordered_failures = {}
            for doc_id in document_ids:
                item = completed.get(doc_id)
                if item and item["status"] == "ok":
                    mapped.append(item)
                else:
                    ordered_failures[doc_id] = (
                        item["error"] if item else failures[doc_id]
                    )

            if not mapped:
                if state["capacity_failures"] == total_documents:
                    return {"error": self._capacity_error(), "error_category": "context_capacity"}
                details = "\n".join(
                    f"- {doc_id}: {reason}" for doc_id, reason in ordered_failures.items()
                )
                return {"error": f"🔍 所选文档没有可用于联合总结的内容\n{details}"}

            # Shared entities can contain evidence from documents whose maps failed.
            return {
                "mapped": mapped,
                "failures": ordered_failures,
                "shared_graph_contexts": [] if ordered_failures else state["shared_graph_contexts"],
            }

        def reduce(state: SummaryState) -> SummaryState:
            check_cancelled()
            mapped = state["mapped"]
            shared_graph_contexts = state["shared_graph_contexts"]
            missing_text = "\n".join(
                f"- {doc_id}: {reason}" for doc_id, reason in state["failures"].items()
            ) or "无"
            summary_parts = []
            for item in mapped:
                summary_parts.append(
                    {
                        "document_id": item["document_id"],
                        "document_name": item["document_name"],
                        "allowed": ", ".join(f"[{ref}]" for ref in item["source_refs"]),
                        "summary": str(item["summary"]),
                    }
                )
            prompt = f"""请只基于下列“单篇文档摘要”进行联合总结，不要使用或推测原文。

用户问题：
{query}

请按以下结构回答：
1. 各文档摘要
2. 跨文档共同主题
3. 重要差异或互补信息
4. 综合结论

第 1 节每篇最多两句。第 2–4 节各写最多两条新增要点，避免重复同一事实。优先回答用户的具体问题。

缺失或失败文档：
{missing_text}

引用约束：
1. 只能使用每篇摘要列出的稳定引用 ID。
2. 每个关键结论必须标注至少一个引用 ID。
3. 不得创造引用 ID。

单篇文档摘要：
"""
            suffix = "\n\n请给出联合总结：\n"
            try:
                reduce_budget = self._context_budget(
                    prompt + suffix, output_reserve=reduce_max_tokens
                )
                summary_context, reduce_truncated = self._fit_summary_context(
                    summary_parts, reduce_budget
                )
                summary_context, shared_graph_sources = self._append_graph_context_with_sources(
                    summary_context,
                    shared_graph_contexts,
                    token_budget=reduce_budget,
                )
            except ContextCapacityError:
                return {
                    "error": self._capacity_error(),
                    "retryable": False,
                    "incomplete_output": False,
                    "error_category": "context_capacity",
                }
            emit_progress("reducing", total_documents)

            try:
                answer = self._generate_summary(
                    prompt + summary_context + suffix,
                    max_tokens=reduce_max_tokens,
                    strict=use_summary_workflow,
                    model=summary_model,
                    before_retry=before_retry,
                )
            except ContextCapacityError:
                return {
                    "error": self._capacity_error(),
                    "retryable": False,
                    "incomplete_output": False,
                    "error_category": "context_capacity",
                }
            except (IncompleteSummaryOutput, SummaryCancelled, SummaryProgressAborted, SummaryOwnershipCheckError):
                raise
            except Exception as error:
                raise RAGOperationError(
                    "联合总结生成失败",
                    retryable=self._summary_error_is_retryable(error),
                ) from None
            allowed_ids = {ref for item in mapped for ref in item["source_refs"]}
            allowed_ids.update(source["citation_id"] for source in shared_graph_sources)
            answer, invalid_citations = verify_summary_citations(answer, allowed_ids)
            emit_progress("completed", total_documents)
            return {
                "answer": answer,
                "shared_graph_sources": shared_graph_sources,
                "invalid_citations": state["invalid_citations"] + invalid_citations,
                "truncated": reduce_truncated or any(item["truncated"] for item in mapped),
            }

        try:
            state = run_summary_workflow(
                prepare,
                map_documents,
                collect,
                reduce,
                use_langgraph=use_summary_workflow,
                document_count=total_documents,
                job_id=job_id,
                attempt=attempt,
            )
            check_cancelled()
        except SummaryCancelled:
            self._last_action_data = {"success": False}
            self._last_action_error = RAGOperationError("联合总结已中止", retryable=True)
            return "⏹️ 联合总结已取消"

        if "error" in state:
            self._last_action_data = {"success": False}
            retryable = state.get("retryable", False)
            if state.get("error_category") == "context_capacity":
                self._last_action_error = SummaryCapacityError("所选文档超出摘要容量限制")
            elif state.get("incomplete_output", False) and not retryable:
                self._last_action_error = IncompleteSummaryOutput("empty_output")
            else:
                self._last_action_error = RAGOperationError("联合总结无法完成", retryable=retryable)
            return state["error"]

        # The structured result is projected in the tool caller's context.
        mapped = state["mapped"]
        shared_graph_sources = state["shared_graph_sources"]
        all_sources = [source for item in mapped for source in item["sources"]]
        all_graph_sources = shared_graph_sources + [
            source for item in mapped for source in item.get("graph_sources", [])
        ]
        cache_hits = sum(bool(item["cache_hit"]) for item in mapped)
        self._last_action_data = {
            "summary_documents": len(mapped),
            "summary_cache_hits": cache_hits,
            "summary_cache_misses": len(mapped) - cache_hits,
            "summary_invalid_citations": state["invalid_citations"],
            "graph_mode": graph_mode,
            "graph_documents": [item["document_id"] for item in mapped],
            "graph_context_count": len(all_graph_sources),
        }
        answer = state["answer"]
        if state["failures"]:
            missing = "、".join(state["failures"])
            answer += f"\n\n⚠️ 以下文档未纳入本次总结：{missing}"
        rendered = self._format_answer(
            answer,
            all_sources,
            truncated=state["truncated"],
            graph_sources=all_graph_sources,
        )
        self._last_action_data = {**self._last_action_data, "success": True}
        return rendered

    def _summary_error_is_retryable(self, error: Exception) -> bool:
        if isinstance(error, (RAGConnectionError, APIConnectionError, ConnectionError, TimeoutError)):
            return True
        if isinstance(error, RAGOperationError):
            return self._error_is_transient(error)
        # SDK exceptions expose a status code. Generic error text is not a
        # reliable way to classify failures from arbitrary tool implementations.
        status_code = getattr(error, "status_code", None)
        return isinstance(status_code, int) and (
            status_code in {408, 425, 429} or status_code >= 500
        )

    def _fit_summary_context(
        self,
        summaries: List[Dict[str, str]],
        token_budget: int,
    ) -> tuple[str, bool]:
        """Fit every successful document summary without dropping a document."""

        def render(values: List[str]) -> str:
            blocks = []
            for item, value in zip(summaries, values):
                blocks.append(
                    f"[文档摘要 | {item['document_name']} | {item['document_id']} | "
                    f"允许引用: {item['allowed']}]\n{value}"
                )
            return "\n\n".join(blocks)

        originals = [str(item["summary"]) for item in summaries]
        complete = render(originals)
        if estimate_tokens(self.llm, complete) <= token_budget:
            return complete, False

        floors = [min(len(value), MIN_TRUNCATED_CHARS) for value in originals]

        def values_for_ratio(ratio: float) -> List[str]:
            return [
                value[: floor + int((len(value) - floor) * ratio)]
                for value, floor in zip(originals, floors)
            ]

        minimum = render(values_for_ratio(0.0))
        if estimate_tokens(self.llm, minimum) > token_budget:
            raise ContextCapacityError(
                "minimum per-document summary coverage exceeds capacity"
            )

        low, high = 0.0, 1.0
        best = minimum
        for _ in range(24):
            middle = (low + high) / 2
            candidate = render(values_for_ratio(middle))
            if estimate_tokens(self.llm, candidate) <= token_budget:
                low = middle
                best = candidate
            else:
                high = middle
        return best, True

    def _clean_preview_text(self, text: str, max_len: int = 220) -> str:
        """清理检索结果预览文本，避免展示过长、过乱"""

        if not text:
            return ""

        # 统一换行和空格
        text = str(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        lines = []

        for line in text.split("\n"):
            line = line.strip()
            if line:
                lines.append(line)

        cleaned = " ".join(lines)

        # 压缩多余空格
        while "  " in cleaned:
            cleaned = cleaned.replace("  ", " ")

        if len(cleaned) > max_len:
            cleaned = cleaned[:max_len] + "..."

        return cleaned

    def _format_source(
        self,
        metadata: Dict[str, Any],
        truncated: bool = False,
    ) -> str:
        """格式化来源信息"""

        if not metadata:
            return ""

        file_name = metadata.get("file_name", "")
        document_id = metadata.get("document_id", "")
        page_number = metadata.get("page_number", "")

        source_parts = []

        if file_name:
            source_parts.append(f"文件: {file_name}")

        if document_id:
            source_parts.append(f"文档ID: {document_id}")

        if page_number:
            source_parts.append(f"第 {page_number} 页")

        if truncated:
            source_parts.append("上下文已截断")

        return " | ".join(source_parts)
