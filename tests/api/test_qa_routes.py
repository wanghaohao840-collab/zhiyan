from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.app import create_api_app
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


@pytest.mark.parametrize("code", ["QA_RAG_TRANSIENT", "QA_SUMMARY_INCOMPLETE"])
def test_manual_summary_retry_enqueues_same_instruction_without_running_inline(qa_parts, code):
    parts = qa_parts
    convo = create_conversation(parts)["conversation_id"]
    started = parts.client.post(
        f"/api/v1/qa/conversations/{convo}/summary-jobs",
        headers=csrf(parts), json={"instruction": "专注实验方法", "client_request_id": str(uuid4())},
    )
    assert started.status_code == 202
    original = started.json()
    jobs = parts.services.qa_job_repository
    old = jobs.claim_next("manual-test", lease_seconds=60)
    jobs.fail_or_retry(old.id, "manual-test", code, None, retryable=False, attempt=old.attempt_count)
    failed_id = original["assistant_message_id"]
    url = f"/api/v1/qa/messages/{failed_id}/summary-retry"
    key = str(uuid4())
    assert parts.client.post(url, json={"client_request_id": key}).status_code == 403
    assert parts.client.post(
        f"/api/v1/qa/messages/{failed_id}/retry",
        headers=csrf(parts), json={"client_request_id": key},
    ).status_code == 409
    assert parts.engine.calls == 0

    parts.client.cookies.set(COOKIE, parts.other_token)
    assert parts.client.post(
        url, headers={"X-CSRF-Token": parts.other_csrf}, json={"client_request_id": key}
    ).status_code == 404
    owner(parts)

    retry = parts.client.post(url, headers=csrf(parts), json={"client_request_id": key})
    assert retry.status_code == 202, retry.text
    job = retry.json()
    assert job["status"] == "queued"
    assert job["conversation_id"] == convo
    assert job["input_message_id"] != original["input_message_id"]
    assert job["assistant_message_id"] != failed_id
    for request_id in (key, str(uuid4())):
        duplicate = parts.client.post(url, headers=csrf(parts), json={"client_request_id": request_id})
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == job["job_id"]

    msgs = parts.client.get(f"/api/v1/qa/conversations/{convo}/messages").json()["items"]
    by_id = {item["message_id"]: item for item in msgs}
    assert by_id[failed_id]["status"] == "failed"
    assert by_id[job["input_message_id"]]["content"] == "专注实验方法"
    assert by_id[job["assistant_message_id"]]["retry_of_message_id"] == failed_id
    assert parts.engine.calls == 0

    second = jobs.claim_next("manual-test", lease_seconds=60)
    assert second.id == job["job_id"]
    jobs.fail_or_retry(second.id, "manual-test", "QA_SUMMARY_INCOMPLETE", None, retryable=False)
    assert parts.client.post(
        f"/api/v1/qa/messages/{job['assistant_message_id']}/summary-retry",
        headers=csrf(parts), json={"client_request_id": str(uuid4())},
    ).status_code == 409


def test_summary_capacity_failure_is_visible_and_does_not_offer_retry(qa_parts):
    parts = qa_parts
    convo = create_conversation(parts)["conversation_id"]
    response = parts.client.post(
        f"/api/v1/qa/conversations/{convo}/summary-jobs",
        headers=csrf(parts), json={"client_request_id": str(uuid4())},
    )
    assert response.status_code == 202
    job = response.json()
    jobs = parts.services.qa_job_repository
    claimed = jobs.claim_next("capacity-test", lease_seconds=60)
    code = "QA_SUMMARY_CONTEXT_CAPACITY"
    jobs.fail_or_retry(claimed.id, "capacity-test", code, None, retryable=False)

    status = parts.client.get(f"/api/v1/qa/jobs/{job['job_id']}").json()
    assert status["safe_error_code"] == code
    failed_id = job["assistant_message_id"]
    messages = parts.client.get(f"/api/v1/qa/conversations/{convo}/messages").json()["items"]
    failed = next(message for message in messages if message["message_id"] == failed_id)
    assert (failed["status"], failed["safe_error_code"], failed["sources"]) == ("failed", code, [])
    retry = parts.client.post(
        f"/api/v1/qa/messages/{failed_id}/summary-retry",
        headers=csrf(parts), json={"client_request_id": str(uuid4())},
    )
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "QA_RETRY_NOT_ALLOWED"
    assert parts.engine.calls == 0
