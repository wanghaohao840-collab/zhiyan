from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path
from threading import BoundedSemaphore
from uuid import uuid4

from app.database import connect
from app.document_library import DocumentNotFoundError
from hello_agents.memory.storage.neo4j_store import Neo4jGraphStore


class GraphUnavailableError(RuntimeError):
    pass


class GraphBusyError(GraphUnavailableError):
    pass


class GraphService:
    """Optional product graph access, scoped to the user's live document library."""

    def __init__(self, db_path: Path | None = None, *, store_factory: Callable | None = None) -> None:
        self._db_path = db_path
        self._store_factory = store_factory or Neo4jGraphStore
        self._read_slots = BoundedSemaphore(4)

    @staticmethod
    def _enabled() -> bool:
        return os.getenv("ZHIYAN_GRAPH_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @contextmanager
    def _connect(self):
        try:
            store = self._store_factory(
                uri=os.getenv("NEO4J_URI"),
                username=os.getenv("NEO4J_USERNAME", "neo4j"),
                password=os.getenv("NEO4J_PASSWORD"),
                database=os.getenv("NEO4J_DATABASE", "neo4j"),
                connection_timeout=3,
            )
        except Exception as error:
            raise GraphUnavailableError("graph_unavailable") from error
        try:
            yield store
        finally:
            store.close()

    @contextmanager
    def _read_slot(self):
        if not self._read_slots.acquire(blocking=False):
            raise GraphBusyError("graph_busy")
        try:
            yield
        finally:
            self._read_slots.release()

    @staticmethod
    def _namespace(user_id: str) -> str:
        return f"pdf_{user_id}"

    def capabilities(self) -> tuple[bool, str | None]:
        if not self._enabled():
            return False, "graph_disabled"
        try:
            with self._read_slot(), self._connect() as store:
                store.check_database()
        except GraphBusyError:
            return False, "graph_busy"
        except Exception:
            return False, "graph_unavailable"
        return True, None

    def subgraph(self, session, documents, *, document_id, query: str, limit: int) -> dict:
        if not self._enabled():
            raise GraphUnavailableError("graph_disabled")

        def visible_ids() -> set[str]:
            return {
                item.document_id for item in documents.list_documents(session.token)
            }

        with self._read_slot():
            before = visible_ids()
            if document_id is not None and document_id not in before:
                raise DocumentNotFoundError()
            scope = {document_id} if document_id is not None else before

            try:
                with self._connect() as store:
                    graph = store.get_subgraph(
                        rag_namespace=self._namespace(session.user_id),
                        document_ids=sorted(scope),
                        query=query,
                        limit=limit,
                    )
            except GraphUnavailableError:
                raise
            except Exception as error:
                raise GraphUnavailableError("graph_unavailable") from error

            after = visible_ids()
            if document_id is not None and document_id not in after:
                raise DocumentNotFoundError()
            # Drop entire nodes if their membership changed during the query.
            # A shared node's display label may have come from the removed document.
            available = scope & after
            nodes = [
                node for node in graph["nodes"]
                if node["document_ids"] and set(node["document_ids"]) <= available
            ]
            visible_node_ids = {node["id"] for node in nodes}
            return {
                "nodes": nodes,
                "edges": [
                    edge for edge in graph["edges"]
                    if edge["source"] in visible_node_ids and edge["target"] in visible_node_ids
                ],
                "truncated": graph["truncated"] or len(nodes) < len(graph["nodes"]),
            }

    def build_after_import(self, task, runtime) -> None:
        if not self._enabled():
            return
        from hello_agents.core.llm import HelloAgentsLLM
        from hello_agents.memory.graph.extractor import GraphExtractor

        namespace = self._namespace(task.user_id)
        if runtime.rag_tool.rag_namespace != namespace:
            raise ValueError("graph import scope mismatch")
        with self._connect() as store:
            chunks = runtime.rag_tool._get_pipeline(namespace).get_document_chunks(
                task.document_id
            )
            graph = GraphExtractor(HelloAgentsLLM(max_retries=0)).extract(
                task.document_id,
                chunks,
                {"name": task.original_name},
                rag_namespace=namespace,
            )
            # Commit the cleanup record before Neo4j publication so a process
            # failure between stores cannot leave an untracked document graph.
            if self._db_path is not None:
                with closing(connect(self._db_path)) as connection, connection:
                    connection.execute(
                        "insert or ignore into graph_document_cleanup values (?, ?)",
                        (task.user_id, task.document_id),
                    )
            store.replace_document_graph(
                task.document_id, str(uuid4()), graph.to_store_payload(),
                rag_namespace=namespace,
            )

    def delete_document(self, user_id: str, document_id: str) -> None:
        key = (user_id, document_id)
        if not self._enabled():
            if self._db_path is None:
                return
            with closing(connect(self._db_path)) as connection:
                tracked = connection.execute(
                    "select 1 from graph_document_cleanup where user_id = ? and document_id = ?",
                    key,
                ).fetchone()
            if tracked is None:
                return
        with self._connect() as store:
            store.delete_document(document_id, rag_namespace=self._namespace(user_id))
        if self._db_path is not None:
            with closing(connect(self._db_path)) as connection, connection:
                connection.execute(
                    "delete from graph_document_cleanup where user_id = ? and document_id = ?",
                    key,
                )
