from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from app.database import connect
from app.document_library import DocumentNotFoundError
from hello_agents.memory.storage.neo4j_store import Neo4jGraphStore


logger = logging.getLogger(__name__)
_GRAPH_BUILD_MAX_ATTEMPTS = 3
_GRAPH_BUILD_RETRY_DELAYS = (5, 15, 45)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


class GraphUnavailableError(RuntimeError):
    pass


class GraphBusyError(GraphUnavailableError):
    pass


@dataclass(frozen=True)
class GraphBuildJob:
    user_id: str
    document_id: str
    document_name: str
    attempt_count: int


class GraphService:
    """托管 Neo4j 连接池、带短 TTL 能力探测及异步投影入队的可选图谱服务。"""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        store_factory: Callable | None = None,
        capabilities_ttl_seconds: float | None = None,
    ) -> None:
        self._db_path = db_path
        self._custom_factory = store_factory is not None
        self._store_factory = store_factory or Neo4jGraphStore
        # NOTE: 生产默认启用 5 秒能力探测缓存避免频繁握手与 429 抖动；
        # 测试注入自定义 store_factory 时默认 TTL=0 以保持状态切换即时可见，支持显式传参测缓存。
        if capabilities_ttl_seconds is None:
            self._capabilities_ttl = 0.0 if self._custom_factory else 5.0
        else:
            self._capabilities_ttl = max(0.0, float(capabilities_ttl_seconds))
        self._read_slots = BoundedSemaphore(4)
        self._driver_lock = Lock()
        self._store = None
        self._store_key: tuple[str | None, str, str | None, str] | None = None
        self._cap_lock = Lock()
        self._cap_cache: tuple[float, tuple[bool, str | None], tuple] | None = None
        self._worker_wake: Callable[[], None] | None = None

    def set_worker_wake(self, wake: Callable[[], None] | None) -> None:
        self._worker_wake = wake

    @staticmethod
    def _enabled() -> bool:
        return os.getenv("ZHIYAN_GRAPH_ENABLED", "").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @staticmethod
    def _connection_key() -> tuple[str | None, str, str | None, str]:
        return (
            os.getenv("NEO4J_URI"),
            os.getenv("NEO4J_USERNAME", "neo4j"),
            os.getenv("NEO4J_PASSWORD"),
            os.getenv("NEO4J_DATABASE", "neo4j"),
        )

    def _invalidate_capabilities_cache(self) -> None:
        with self._cap_lock:
            self._cap_cache = None

    def _invalidate_store(self, failed_store=None) -> None:
        old = None
        with self._driver_lock:
            if failed_store is None or self._store is failed_store:
                old = self._store
                self._store = None
                self._store_key = None
        self._invalidate_capabilities_cache()
        if old is not None:
            try:
                old.close()
            except Exception:
                logger.debug("ignoring Neo4j driver close error", exc_info=True)

    def close(self) -> None:
        """优雅关闭托管的 Neo4j 长连接池（供 ApplicationServices.stop 调用）。"""
        self._invalidate_store()

    @contextmanager
    def _connect(self):
        """复用托管长连接池；配置变化或连接故障时自动关闭旧池并重建。"""
        key = self._connection_key()
        uri, username, password, database = key
        stale_store = None
        with self._driver_lock:
            if self._store is not None and self._store_key != key:
                stale_store = self._store
                self._store = None
                self._store_key = None
            store = self._store
            if store is None:
                try:
                    store = self._store_factory(
                        uri=uri,
                        username=username,
                        password=password,
                        database=database,
                        connection_timeout=3,
                    )
                except Exception as error:
                    self._invalidate_capabilities_cache()
                    raise GraphUnavailableError("graph_unavailable") from error
                self._store = store
                self._store_key = key
        if stale_store is not None:
            try:
                stale_store.close()
            except Exception:
                logger.debug("ignoring stale Neo4j driver close error", exc_info=True)
        try:
            yield store
        except Exception:
            self._invalidate_store(store)
            raise

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

    def _has_active_document_fence(self, user_id: str, document_id: str) -> bool:
        if self._db_path is None:
            return False
        with closing(connect(self._db_path)) as conn:
            row = conn.execute(
                """
                select 1 from qa_deletion_fences
                where user_id = ? and target_type = 'document' and target_id = ?
                  and (
                      status in ('queued', 'running')
                      or (status = 'failed' and attempt_count < 3)
                  )
                """,
                (user_id, document_id),
            ).fetchone()
            return row is not None

    def capabilities(self) -> tuple[bool, str | None]:
        if not self._enabled():
            self._invalidate_capabilities_cache()
            return False, "graph_disabled"
        cache_key = (True, self._connection_key())
        if self._capabilities_ttl > 0:
            with self._cap_lock:
                if self._cap_cache is not None:
                    expires_at, cached_result, cached_key = self._cap_cache
                    if cached_key == cache_key and monotonic() < expires_at:
                        return cached_result
        try:
            with self._read_slot(), self._connect() as store:
                store.check_database()
            result: tuple[bool, str | None] = (True, None)
        except GraphBusyError:
            return False, "graph_busy"
        except Exception:
            result = (False, "graph_unavailable")
        if self._capabilities_ttl > 0:
            with self._cap_lock:
                self._cap_cache = (monotonic() + self._capabilities_ttl, result, cache_key)
        return result

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

    def enqueue_after_import(self, task) -> bool:
        """导入成功后以 O(1) 写入异步建图任务并唤醒后台投影 Worker，不阻塞主导入。"""
        if not self._enabled() or self._db_path is None:
            return False
        if self._has_active_document_fence(task.user_id, task.document_id):
            return False
        now = _utc_now()
        with closing(connect(self._db_path)) as connection, connection:
            connection.execute(
                """
                insert into graph_build_jobs (
                    user_id, document_id, document_name, status, attempt_count,
                    next_attempt_at, last_error, created_at, updated_at
                ) values (?, ?, ?, 'queued', 0, null, null, ?, ?)
                on conflict(user_id, document_id) do update set
                    document_name = excluded.document_name,
                    status = 'queued',
                    attempt_count = 0,
                    next_attempt_at = null,
                    last_error = null,
                    updated_at = excluded.updated_at
                """,
                (task.user_id, task.document_id, task.original_name, now, now),
            )
        if self._worker_wake is not None:
            self._worker_wake()
        return True

    def cancel_pending_build(self, user_id: str, document_id: str) -> None:
        """文档进入删除流程时取消任何排队中的异步建图任务。"""
        if self._db_path is None:
            return
        with closing(connect(self._db_path)) as connection, connection:
            connection.execute(
                "delete from graph_build_jobs where user_id = ? and document_id = ?",
                (user_id, document_id),
            )

    def build_after_import(self, task, runtime) -> None:
        if not self._enabled():
            return
        if self._has_active_document_fence(task.user_id, task.document_id):
            return
        from hello_agents.core.llm import HelloAgentsLLM
        from hello_agents.memory.graph.extractor import GraphExtractor

        namespace = self._namespace(task.user_id)
        if runtime.rag_tool.rag_namespace != namespace:
            raise ValueError("graph import scope mismatch")
        chunks = runtime.rag_tool._get_pipeline(namespace).get_document_chunks(
            task.document_id
        )
        graph = GraphExtractor(HelloAgentsLLM(max_retries=0)).extract(
            task.document_id,
            chunks,
            {"name": task.original_name},
            rag_namespace=namespace,
        )
        # NOTE: 耗时 LLM 抽取结束后、写入清理台账与 Neo4j 前再次核验删除栅栏，
        # 防止抽取期间文档被删除导致已删文档图谱幽灵复活。
        if self._has_active_document_fence(task.user_id, task.document_id):
            return
        with self._connect() as store:
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
        self.cancel_pending_build(user_id, document_id)
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


class GraphProjectionWorker:
    """后台异步可重试图谱投影 Worker，彻底解耦文档导入主链路与耗时 LLM 图谱抽取。"""

    def __init__(
        self,
        db_path: Path | str,
        runtime_registry,
        graph_service: GraphService,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.runtime_registry = runtime_registry
        self.graph_service = graph_service
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.graph_service.set_worker_wake(self.notify)

    def recover_interrupted(self) -> int:
        """服务启动时将意外中断遗留的 running 建图任务复位为 queued，等待自动重试。"""
        now = _utc_now()
        with closing(connect(self.db_path)) as conn, conn:
            updated = conn.execute(
                """
                update graph_build_jobs
                set status = 'queued', next_attempt_at = null, updated_at = ?
                where status = 'running' and attempt_count < ?
                """,
                (now, _GRAPH_BUILD_MAX_ATTEMPTS),
            )
            return updated.rowcount

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.recover_interrupted()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="graph-projection-worker",
            daemon=False,
        )
        self._thread.start()
        self.notify()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if wait and self._thread is not None:
            self._thread.join()

    def notify(self) -> None:
        self._wake.set()

    def _claim_next(self) -> GraphBuildJob | None:
        now = _utc_now()
        conn = connect(self.db_path)
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                """
                select user_id, document_id, document_name, attempt_count
                from graph_build_jobs
                where status = 'queued'
                  and attempt_count < ?
                  and (next_attempt_at is null or next_attempt_at <= ?)
                order by updated_at, document_id
                limit 1
                """,
                (_GRAPH_BUILD_MAX_ATTEMPTS, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            next_attempt = int(row["attempt_count"]) + 1
            updated = conn.execute(
                """
                update graph_build_jobs
                set status = 'running', attempt_count = ?, updated_at = ?
                where user_id = ? and document_id = ? and status = 'queued'
                """,
                (next_attempt, now, row["user_id"], row["document_id"]),
            )
            conn.commit()
            if not updated.rowcount:
                return None
            return GraphBuildJob(
                user_id=row["user_id"],
                document_id=row["document_id"],
                document_name=row["document_name"],
                attempt_count=next_attempt,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _complete_job(self, job: GraphBuildJob) -> None:
        now = _utc_now()
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                update graph_build_jobs
                set status = 'succeeded', next_attempt_at = null, last_error = null, updated_at = ?
                where user_id = ? and document_id = ? and status = 'running'
                """,
                (now, job.user_id, job.document_id),
            )

    def _fail_or_retry_job(self, job: GraphBuildJob, error: Exception) -> None:
        now = _utc_now()
        retry = job.attempt_count < _GRAPH_BUILD_MAX_ATTEMPTS
        delay = _GRAPH_BUILD_RETRY_DELAYS[
            min(job.attempt_count - 1, len(_GRAPH_BUILD_RETRY_DELAYS) - 1)
        ]
        next_at = _utc_after(delay) if retry else None
        status = "queued" if retry else "failed"
        safe_error = error.__class__.__name__[:120]
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute(
                """
                update graph_build_jobs
                set status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                where user_id = ? and document_id = ? and status = 'running'
                """,
                (status, next_at, safe_error, now, job.user_id, job.document_id),
            )

    def run_once(self) -> bool:
        job = self._claim_next()
        if job is None:
            return False
        if not self.graph_service._enabled():
            self.graph_service.cancel_pending_build(job.user_id, job.document_id)
            return True
        if self.graph_service._has_active_document_fence(job.user_id, job.document_id):
            self.graph_service.cancel_pending_build(job.user_id, job.document_id)
            return True
        runtime = None
        try:
            runtime = self.runtime_registry.acquire_background(job.user_id)
            # 确认文档仍在用户当前文档库中（若已被物理删除则安全放弃）
            with runtime.lock:
                records = runtime.history.load().get("documents", [])
                still_present = any(
                    isinstance(item, dict) and item.get("document_id") == job.document_id
                    for item in records
                )
            if not still_present:
                self.graph_service.cancel_pending_build(job.user_id, job.document_id)
                return True
            task = SimpleNamespace(
                user_id=job.user_id,
                document_id=job.document_id,
                original_name=job.document_name,
            )
            self.graph_service.build_after_import(task, runtime)
            self._complete_job(job)
        except Exception as error:
            logger.warning(
                "async document graph build failed (attempt %s/%s): %s",
                job.attempt_count,
                _GRAPH_BUILD_MAX_ATTEMPTS,
                error.__class__.__name__,
            )
            self._fail_or_retry_job(job, error)
        finally:
            if runtime is not None:
                self.runtime_registry.release_background(job.user_id)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                logger.exception("Graph projection worker iteration failed")
            self._wake.wait(self.poll_interval)
            self._wake.clear()
