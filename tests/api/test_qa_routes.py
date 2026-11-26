from __future__ import annotations

import asyncio
import json
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from api.app import create_api_app
from api.routes import qa as qa_routes
from app.bootstrap import ApplicationServices
from app.qa_answer_engine import QaAnswerResult, QaEngineError
from app.qa_models import QaSourceDraft


COOKIE = "zhiyan_session"


class Engine:
    def __init__(self) -> None:
        self.calls = 0

    def answer(self, runtime, request, **kwargs):
        self.calls += 1
        return QaAnswerResult(
            "可信回答",
            (
                QaSourceDraft(
                    "S-1",
                    request.document_ids[0],
                    "研究.pdf",
                    page_number=3,
                    excerpt="证据",
                    reference="[S-1]",
                ),
            ),
            request.mode,
        )


@pytest.fixture
def qa_parts(tmp_path, monkeypatch):
    monkeypatch.delenv("QA_ROUTE_ENABLED", raising=False)
    engine = Engine()
    services = ApplicationServices.create(
        tmp_path / "data", qa_answer_engine=engine
    )
    owner_token = services.session_registry.register(
        "Owner", "correct horse battery"
    )
    other_token = services.session_registry.register(
        "Other", "correct horse battery"
    )
    owner = services.session_registry.get_session(owner_token)
    document_id = str(uuid4())
    source = owner.runtime.paths.documents / f"{document_id}.pdf"
    source.write_bytes(b"pdf")
    owner.runtime.history.add_document(
        {
            "document_id": document_id,
            "document_name": "研究.pdf",
            "document_path": str(source),
            "file_suffix": ".pdf",
            "loaded_at": "2026-08-27T00:00:00Z",
        }
    )
    with TestClient(
        create_api_app(services), raise_server_exceptions=False
    ) as client:
        # Keep status resources deterministic in route tests. Lifecycle start
        # itself is covered separately.
        services.qa_deletion_worker.stop()
        services.qa_worker_pool.stop()
        yield SimpleNamespace(
            client=client,
            services=services,
            engine=engine,
            owner_token=owner_token,
            owner_csrf=owner.csrf_token,
            other_token=other_token,
            other_csrf=services.session_registry.get_session(other_token).csrf_token,
            document_id=document_id,
        )


def owner(parts):
    parts.client.cookies.set(COOKIE, parts.owner_token)


def csrf(parts):
    return {"X-CSRF-Token": parts.owner_csrf}


def create_conversation(parts):
    owner(parts)
    response = parts.client.post(
        "/api/v1/qa/conversations",
        headers=csrf(parts),
        json={"document_ids": [parts.document_id]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def assert_safe(body: str) -> None:
    lowered = body.lower()
    for forbidden in (
        "user_id",
        "document_path",
        "lease_owner",
        "memory_id",
        "c:\\",
    ):
        assert forbidden not in lowered


def submit_streaming_question(parts, conversation_id, request_id):
    response = parts.client.post(
        f"/api/v1/qa/conversations/{conversation_id}/messages?stream=true",
        headers=csrf(parts),
        json={"question": "证据是什么？", "client_request_id": request_id},
    )
    assert response.status_code == 202, response.text
    return response.json()["message_id"]


async def open_answer_iterator(parts, conversation_id, message_id):
    request = Request({
        "type": "http",
        "app": parts.client.app,
        "headers": [(b"cookie", f"{COOKIE}={parts.owner_token}".encode())],
    })
    response = await qa_routes.stream_message(
        conversation_id, message_id, request,
        parts.services.session_registry.get_session(parts.owner_token),
        parts.services.qa_service,
    )
    return response.body_iterator


def test_stream_sends_live_frames_and_persists_after_disconnect(qa_parts, monkeypatch):
    conversation_id = create_conversation(qa_parts)["conversation_id"]
    started = Event()
    release_model = Event()
    answer = qa_parts.engine.answer

    def staged(runtime, request, *, stage_callback, token_callback):
        stage_callback("retrieving", 3)
        stage_callback("reranking", 3)
        stage_callback("generating", None)
        token_callback("可信")
        started.set()
        assert release_model.wait(5)
        return answer(runtime, request)

    monkeypatch.setattr(qa_parts.engine, "answer_stream", staged, raising=False)
    monkeypatch.setattr(qa_routes, "STREAM_POLL_SECONDS", 0.01)
    monkeypatch.setattr(qa_routes, "STREAM_HEARTBEAT_SECONDS", 0.02)
    qa_parts.services.qa_worker_pool.start()
    try:
        request_id = str(uuid4())
        message_id = submit_streaming_question(qa_parts, conversation_id, request_id)
        assert started.wait(5)
        assert submit_streaming_question(qa_parts, conversation_id, request_id) == message_id
        stream_url = f"/api/v1/qa/conversations/{conversation_id}/messages/{message_id}/stream"
        qa_parts.client.cookies.set(COOKIE, qa_parts.other_token)
        assert qa_parts.client.get(stream_url).status_code == 404
        qa_parts.client.cookies.clear()
        assert qa_parts.client.get(stream_url).status_code == 401
        owner(qa_parts)
        assert qa_parts.client.get(stream_url.replace(conversation_id, str(uuid4()))).status_code == 404

        async def observe_and_disconnect():
            events = await open_answer_iterator(qa_parts, conversation_id, message_id)
            try:
                first = await asyncio.wait_for(anext(events), timeout=3)
                assert first.index('"retrieving"') < first.index('"reranking"') < first.index('"generating"')
                assert '"retrieved_count": 3' in first
                assert 'event: token\ndata: {"delta": "可信"}' in first
                assert await asyncio.wait_for(anext(events), timeout=3) == ": ping\n\n"
            finally:
                await events.aclose()

        asyncio.run(observe_and_disconnect())
        release_model.set()
        until = monotonic() + 5
        while qa_parts.services.qa_service.get_message(qa_parts.owner_token, message_id).status == "pending":
            assert monotonic() < until
            sleep(0.02)
        messages = qa_parts.client.get(stream_url.split(f"/{message_id}/stream")[0]).json()["items"]
        saved = next(item for item in messages if item["message_id"] == message_id)
        assert saved["status"] == "completed"
        assert saved["content"] == "可信回答"
        assert qa_parts.engine.calls == 1
        replay = qa_parts.client.get(stream_url)
        assert replay.headers["content-type"].startswith("text/event-stream")
        assert replay.headers["cache-control"] == "no-cache"
        assert replay.headers["x-accel-buffering"] == "no"
        frames = [frame.split("\ndata: ") for frame in replay.text.strip().split("\n\n")]
        deltas = [json.loads(data)["delta"] for event, data in frames if event == "event: token"]
        assert "".join(deltas) == saved["content"]
        assert ("event: citations", saved["sources"]) in [(event, json.loads(data)) for event, data in frames]
        assert frames[-1][0] == "event: done"
    finally:
        release_model.set()


def test_stream_stops_on_conversation_deletion_before_publishing_more(qa_parts, monkeypatch):
    conversation_id = create_conversation(qa_parts)["conversation_id"]
    first_chunk = Event()
    release_model = Event()
    answer = qa_parts.engine.answer

    def staged(runtime, request, *, stage_callback, token_callback):
        token_callback("首段")
        first_chunk.set()
        assert release_model.wait(5)
        token_callback("栅栏之后")
        return answer(runtime, request)

    monkeypatch.setattr(qa_parts.engine, "answer_stream", staged, raising=False)
    qa_parts.services.qa_worker_pool.start()
    try:
        message_id = submit_streaming_question(qa_parts, conversation_id, str(uuid4()))
        assert first_chunk.wait(5)
        service = qa_parts.services.qa_service
        get_stream_message = service.get_stream_message
        delete_on_read = Event()

        def fence_after_visibility(*args):
            current = get_stream_message(*args)
            if delete_on_read.is_set():
                delete_on_read.clear()
                deletion = service.delete_conversation(qa_parts.owner_token, conversation_id)
                assert deletion.status == "queued"
                service.live_answers.get(message_id).token("栅栏之后")
                release_model.set()
            return current

        monkeypatch.setattr(service, "get_stream_message", fence_after_visibility)

        async def observe_with_fence():
            events = await open_answer_iterator(qa_parts, conversation_id, message_id)
            try:
                assert "首段" in await asyncio.wait_for(anext(events), timeout=3)
                delete_on_read.set()
                assert await asyncio.wait_for(anext(events), timeout=3) == (
                    'event: error\ndata: {"safe_error_code": "QA_NOT_FOUND"}\n\n'
                )
            finally:
                await events.aclose()

        asyncio.run(observe_with_fence())
        assert qa_parts.client.get(f"/api/v1/qa/messages/{message_id}").status_code == 404
    finally:
        release_model.set()


def test_stream_engine_failure_keeps_provider_details_out_of_response(qa_parts, monkeypatch):
    conversation_id = create_conversation(qa_parts)["conversation_id"]

    def broken(runtime, request, *, stage_callback, token_callback):
        stage_callback("generating", None)
        raise RuntimeError("private provider response")

    monkeypatch.setattr(qa_parts.engine, "answer_stream", broken, raising=False)
    qa_parts.services.qa_worker_pool.start()
    message_id = submit_streaming_question(qa_parts, conversation_id, str(uuid4()))
    answer = qa_parts.client.get(
        f"/api/v1/qa/conversations/{conversation_id}/messages/{message_id}/stream"
    )
    assert answer.status_code == 200
    assert "event: done" not in answer.text
    assert "private provider response" not in answer.text
    assert answer.text.endswith('event: error\ndata: {"safe_error_code": "QA_ENGINE_UNAVAILABLE"}\n\n')
    saved = qa_parts.client.get(f"/api/v1/qa/messages/{message_id}").json()
    assert saved["status"] == "failed"
    assert saved["safe_error_code"] == "QA_ENGINE_UNAVAILABLE"


def test_stream_queue_full_rejects_with_503_without_polluting_history(qa_parts):
    conversation_id = create_conversation(qa_parts)["conversation_id"]
    # 未启动 worker pool 时等价于队列不可接收，必须拒绝而不落库失败消息
    rejected = qa_parts.client.post(
        f"/api/v1/qa/conversations/{conversation_id}/messages?stream=true",
        headers=csrf(qa_parts),
        json={"question": "满载测试", "client_request_id": str(uuid4())},
    )
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "QA_ENGINE_BUSY"
    assert rejected.json()["error"]["retryable"] is True
    messages = qa_parts.client.get(
        f"/api/v1/qa/conversations/{conversation_id}/messages"
    ).json()["items"]
    assert messages == []


def test_retry_supports_streaming_202_and_live_sse(qa_parts, monkeypatch):
    conversation_id = create_conversation(qa_parts)["conversation_id"]
    original_answer = qa_parts.engine.answer

    def fail_once(runtime, request, **kwargs):
        raise QaEngineError("QA_ENGINE_UNAVAILABLE", True)

    monkeypatch.setattr(qa_parts.engine, "answer", fail_once)
    failed_resp = qa_parts.client.post(
        f"/api/v1/qa/conversations/{conversation_id}/messages",
        headers=csrf(qa_parts),
        json={"question": "重试流式问题", "client_request_id": str(uuid4())},
    )
    assert failed_resp.status_code == 503
    monkeypatch.setattr(qa_parts.engine, "answer", original_answer)
    messages = qa_parts.client.get(
        f"/api/v1/qa/conversations/{conversation_id}/messages"
    ).json()["items"]
    failed_id = next(item["message_id"] for item in messages if item["status"] == "failed")

    def staged(runtime, request, *, stage_callback, token_callback):
        stage_callback("retrieving", 2)
        stage_callback("generating", None)
        token_callback("可信")
        return original_answer(runtime, request)

    monkeypatch.setattr(qa_parts.engine, "answer_stream", staged, raising=False)
    qa_parts.services.qa_worker_pool.start()
    retried = qa_parts.client.post(
        f"/api/v1/qa/messages/{failed_id}/retry?stream=true",
        headers=csrf(qa_parts),
        json={"client_request_id": str(uuid4())},
    )
    assert retried.status_code == 202
    new_message_id = retried.json()["message_id"]
    stream = qa_parts.client.get(
        f"/api/v1/qa/conversations/{conversation_id}/messages/{new_message_id}/stream"
    )
    assert stream.status_code == 200
    assert "event: done" in stream.text
    assert '"retrieved_count": 2' in stream.text


def test_stream_exits_immediately_when_client_disconnects(qa_parts, monkeypatch):
    conversation_id = create_conversation(qa_parts)["conversation_id"]
    started = Event()
    release_model = Event()
    answer = qa_parts.engine.answer

    def slow(runtime, request, *, stage_callback, token_callback):
        stage_callback("retrieving", 1)
        started.set()
        assert release_model.wait(5)
        return answer(runtime, request)

    monkeypatch.setattr(qa_parts.engine, "answer_stream", slow, raising=False)
    qa_parts.services.qa_worker_pool.start()
    try:
        message_id = submit_streaming_question(qa_parts, conversation_id, str(uuid4()))
        assert started.wait(5)

        async def run_disconnected():
            async def receive():
                return {"type": "http.disconnect"}

            request = Request(
                {
                    "type": "http",
                    "app": qa_parts.client.app,
                    "headers": [(b"cookie", f"{COOKIE}={qa_parts.owner_token}".encode())],
                },
                receive=receive,
            )
            response = await qa_routes.stream_message(
                conversation_id,
                message_id,
                request,
                qa_parts.services.session_registry.get_session(qa_parts.owner_token),
                qa_parts.services.qa_service,
            )
            chunks = [chunk async for chunk in response.body_iterator]
            assert chunks == []

        asyncio.run(run_disconnected())
    finally:
        release_model.set()


def test_capabilities_and_every_route_require_authentication(qa_parts) -> None:
    response = qa_parts.client.get("/api/v1/qa/capabilities")
    assert response.status_code == 401
    owner(qa_parts)
    assert qa_parts.client.get("/api/v1/qa/capabilities").json() == {
        "enabled": True
    }

    qa_parts.client.cookies.clear()
    assert qa_parts.client.get("/api/v1/qa/conversations").status_code == 401
    assert qa_parts.client.post(
        "/api/v1/qa/conversations", json={"document_ids": [qa_parts.document_id]}
    ).status_code == 401


def test_conversation_creation_requires_csrf_and_returns_safe_snapshot(
    qa_parts,
) -> None:
    owner(qa_parts)
    missing_csrf = qa_parts.client.post(
        "/api/v1/qa/conversations",
        json={"document_ids": [qa_parts.document_id]},
    )
    assert missing_csrf.status_code == 403

    created = create_conversation(qa_parts)
    assert created["origin"] == "product"
    assert created["documents"] == [
        {
            "document_id": qa_parts.document_id,
            "document_name": "研究.pdf",
            "position": 0,
        }
    ]
    assert_safe(str(created))


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/qa/conversations", {"document_ids": ["doc"]}),
        (
            "post",
            f"/api/v1/qa/conversations/{uuid4()}/messages",
            {
                "question": "q",
                "mode": "auto",
                "client_request_id": str(uuid4()),
            },
        ),
        (
            "post",
            f"/api/v1/qa/messages/{uuid4()}/retry",
            {"client_request_id": str(uuid4())},
        ),
        (
            "post",
            f"/api/v1/qa/conversations/{uuid4()}/summary-jobs",
            {"instruction": None, "client_request_id": str(uuid4())},
        ),
        ("post", f"/api/v1/qa/jobs/{uuid4()}/cancel", None),
        ("delete", f"/api/v1/qa/conversations/{uuid4()}", None),
    ],
)
def test_every_qa_mutation_requires_csrf(qa_parts, method, path, body) -> None:
    owner(qa_parts)
    response = qa_parts.client.request(method.upper(), path, json=body)
    assert response.status_code == 403


def test_ask_is_idempotent_and_messages_are_user_scoped(qa_parts) -> None:
    conversation = create_conversation(qa_parts)
    request_id = str(uuid4())
    payload = {
        "question": "核心结论是什么？",
        "mode": "joint",
        "client_request_id": request_id,
    }
    url = f"/api/v1/qa/conversations/{conversation['conversation_id']}/messages"
    first = qa_parts.client.post(url, headers=csrf(qa_parts), json=payload)
    second = qa_parts.client.post(url, headers=csrf(qa_parts), json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json()["message_id"] == second.json()["message_id"]
    assert first.json()["content"] == "可信回答"
    assert first.json()["sources"][0]["page_number"] == 3
    assert qa_parts.engine.calls == 1
    assert_safe(first.text)

    listed = qa_parts.client.get(url)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2
    message_id = first.json()["message_id"]
    assert qa_parts.client.get(f"/api/v1/qa/messages/{message_id}").status_code == 200

    qa_parts.client.cookies.set(COOKIE, qa_parts.other_token)
    assert qa_parts.client.get(
        f"/api/v1/qa/conversations/{conversation['conversation_id']}"
    ).status_code == 404
    assert qa_parts.client.get(f"/api/v1/qa/messages/{message_id}").status_code == 404


def test_summary_and_deletion_status_resources_are_reconnect_safe(qa_parts) -> None:
    conversation = create_conversation(qa_parts)
    summary = qa_parts.client.post(
        f"/api/v1/qa/conversations/{conversation['conversation_id']}/summary-jobs",
        headers=csrf(qa_parts),
        json={"instruction": "总结", "client_request_id": str(uuid4())},
    )
    assert summary.status_code == 202
    job_id = summary.json()["job_id"]
    assert qa_parts.client.get(f"/api/v1/qa/jobs/{job_id}").status_code == 200
    active_url = (
        f"/api/v1/qa/conversations/{conversation['conversation_id']}"
        "/summary-jobs/active"
    )
    active = qa_parts.client.get(active_url)
    assert active.status_code == 200
    assert active.json()["job"]["job_id"] == job_id
    assert_safe(active.text)

    cancelled = qa_parts.client.post(
        f"/api/v1/qa/jobs/{job_id}/cancel", headers=csrf(qa_parts)
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert qa_parts.client.get(active_url).json() == {"job": None}

    qa_parts.client.cookies.set(COOKIE, qa_parts.other_token)
    hidden = qa_parts.client.get(active_url)
    assert hidden.status_code == 404
    assert_safe(hidden.text)

    qa_parts.client.cookies.set(COOKIE, qa_parts.owner_token)

    deleted = qa_parts.client.delete(
        f"/api/v1/qa/conversations/{conversation['conversation_id']}",
        headers=csrf(qa_parts),
    )
    assert deleted.status_code == 202
    deletion_id = deleted.json()["deletion_id"]
    polled = qa_parts.client.get(f"/api/v1/qa/deletions/{deletion_id}")
    assert polled.status_code == 200
    assert polled.json()["deletion_id"] == deletion_id
    assert_safe(polled.text)


def test_active_summary_discovery_is_hidden_by_document_deletion_fence(
    qa_parts,
) -> None:
    conversation = create_conversation(qa_parts)
    summary = qa_parts.client.post(
        f"/api/v1/qa/conversations/{conversation['conversation_id']}/summary-jobs",
        headers=csrf(qa_parts),
        json={"instruction": "总结", "client_request_id": str(uuid4())},
    )
    assert summary.status_code == 202
    deletion = qa_parts.services.qa_deletion_service.request_document(
        qa_parts.owner_token, qa_parts.document_id
    )
    assert deletion is not None

    hidden = qa_parts.client.get(
        f"/api/v1/qa/conversations/{conversation['conversation_id']}"
        "/summary-jobs/active"
    )

    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "QA_NOT_FOUND"
    assert_safe(hidden.text)


def test_message_route_starts_newest_and_pages_toward_older_history(
    qa_parts,
) -> None:
    conversation = create_conversation(qa_parts)
    session = qa_parts.services.session_registry.get_session(qa_parts.owner_token)
    user_id = str(session.user_id)
    repository = qa_parts.services.qa_service.repository
    timestamp = "2026-08-27T10:00:00Z"
    message_ids: set[str] = set()
    for index in range(4):
        pending = repository.create_pending_turn(
            user_id,
            conversation["conversation_id"],
            f"问题 {index}",
            "auto",
            f"route-page-{index}",
            now=timestamp,
        )
        assert repository.complete_turn(
            user_id,
            pending.assistant_message.id,
            pending.assistant_message.version,
            f"回答 {index}",
            (),
            "none",
            None,
            now=timestamp,
        )
        message_ids.update((pending.user_message.id, pending.assistant_message.id))

    url = f"/api/v1/qa/conversations/{conversation['conversation_id']}/messages"
    first = qa_parts.client.get(f"{url}?limit=3")
    assert first.status_code == 200
    first_page = first.json()
    repeated_first = qa_parts.client.get(f"{url}?limit=3")
    assert repeated_first.status_code == 200
    assert repeated_first.json() == first_page
    assert len(first_page["items"]) == 3
    assert first_page["next_cursor"] is not None

    pages = [first_page]
    page_texts = [first.text]
    while pages[-1]["next_cursor"] is not None:
        page = qa_parts.client.get(
            f"{url}?limit=3&cursor={quote(pages[-1]['next_cursor'], safe='')}"
        )
        assert page.status_code == 200
        pages.append(page.json())
        page_texts.append(page.text)

    page_ids = [
        [item["message_id"] for item in page["items"]]
        for page in pages
    ]
    assert [len(ids) for ids in page_ids] == [3, 3, 2]
    assert all(
        item["created_at"] == timestamp
        for page in pages
        for item in page["items"]
    )
    assert all(ids == sorted(ids) for ids in page_ids)
    assert len({message_id for ids in page_ids for message_id in ids}) == len(
        message_ids
    )
    assert {message_id for ids in page_ids for message_id in ids} == message_ids
    expected_descending = sorted(message_ids, reverse=True)
    assert page_ids == [
        list(reversed(expected_descending[index : index + 3]))
        for index in range(0, len(expected_descending), 3)
    ]
    for text in page_texts:
        assert_safe(text)


def test_engine_failure_returns_safe_trace_without_raw_exception(qa_parts) -> None:
    conversation = create_conversation(qa_parts)

    class Failure:
        def answer(self, *args, **kwargs):
            raise QaEngineError("rag_connection", True)

    qa_parts.services.qa_service.answer_engine = Failure()
    response = qa_parts.client.post(
        f"/api/v1/qa/conversations/{conversation['conversation_id']}/messages",
        headers=csrf(qa_parts),
        json={
            "question": "问题",
            "mode": "auto",
            "client_request_id": str(uuid4()),
        },
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "QA_RAG_CONNECTION"
    assert error["retryable"] is True
    assert error["trace_id"]
    assert "secret" not in response.text.lower()
    assert "private" not in response.text.lower()


def test_explicit_false_disables_routes_but_not_capabilities_or_lifecycle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("QA_ROUTE_ENABLED", "FaLsE")

    class Registry:
        session = SimpleNamespace(csrf_token="csrf", user_id="owner")

        def get_session(self, token):
            if token != "token":
                raise ValueError("unexpected")
            return self.session

        def validate_csrf(self, token, csrf_token):
            return self.get_session(token)

    class Services:
        def __init__(self):
            self.session_registry = Registry()
            self.qa_service = object()
            self.start_calls = 0
            self.stop_calls = 0

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

    services = Services()
    with TestClient(create_api_app(services), raise_server_exceptions=False) as client:
        client.cookies.set(COOKIE, "token")
        assert client.get("/api/v1/qa/capabilities").json() == {"enabled": False}
        response = client.get("/api/v1/qa/conversations")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "QA_ROUTE_DISABLED"
        assert services.start_calls == 1
    assert services.stop_calls == 1
