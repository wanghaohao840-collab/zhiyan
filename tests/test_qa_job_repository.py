from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from app.database import connect, initialize_database
from app.qa_deletion import QaDeletionRepository
from app.qa_job_repository import QaJobRepository
from app.qa_models import QaConflictError, QaDocumentCandidate, QaValidationError
from app.qa_repository import QaRepository


OWNER = "owner"
OTHER = "other"
NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@pytest.fixture
def repositories(tmp_path):
    db_path = tmp_path / "app.db"
    initialize_database(db_path)
    with connect(db_path) as conn:
        conn.executemany(
            """
            insert into users (
                id, username, username_key, password_hash, created_at, updated_at
            ) values (?, ?, ?, 'hash', ?, ?)
            """,
            (
                (OWNER, "Owner", "owner", iso(NOW), iso(NOW)),
                (OTHER, "Other", "other", iso(NOW), iso(NOW)),
            ),
        )
    qa = QaRepository(db_path)
    jobs = QaJobRepository(db_path)
    return qa, jobs


def conversation(qa: QaRepository):
    return qa.create_conversation(
        OWNER, (QaDocumentCandidate("doc", "文档.pdf", OWNER),), now=iso(NOW)
    )


def test_summary_enqueue_is_atomic_and_idempotent(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    first = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结文档", "client-1", now=iso(NOW)
    )
    duplicate = jobs.create_summary_turn_and_job(
        OWNER, created.id, "ignored", "client-1", now=iso(NOW)
    )
    assert duplicate.duplicate is True
    assert duplicate.job.id == first.job.id
    assert len(qa.list_messages(OWNER, created.id).items) == 2
    assert jobs.get(OTHER, first.job.id) is None


def test_active_job_discovery_is_user_conversation_and_fence_scoped(
    repositories,
) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-active", now=iso(NOW)
    )

    assert jobs.get_active_for_conversation(OWNER, created.id).id == enqueued.job.id
    assert jobs.get_active_for_conversation(OTHER, created.id) is None

    cancelled = jobs.request_cancel(OWNER, enqueued.job.id, now=iso(NOW))
    assert cancelled.status == "cancelled"
    assert jobs.get_active_for_conversation(OWNER, created.id) is None

    second = jobs.create_summary_turn_and_job(
        OWNER, created.id, "再次总结", "client-fenced", now=iso(NOW)
    )
    with connect(qa.db_path) as conn:
        conn.execute(
            """
            insert into qa_deletion_fences (
                id, user_id, target_type, target_id, status, stage,
                created_at, updated_at
            ) values ('fence', ?, 'conversation', ?, 'queued', 'fenced', ?, ?)
            """,
            (OWNER, created.id, iso(NOW), iso(NOW)),
        )
    assert jobs.get_active_for_conversation(OWNER, created.id) is None
    assert jobs.get(OWNER, second.job.id) is not None


def test_expired_running_job_is_reclaimed_and_stale_owner_loses(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", now=iso(NOW)
    )
    first = jobs.claim_next("worker-a", lease_seconds=30, now=iso(NOW))
    assert first.id == enqueued.job.id
    assert first.attempt_count == 1

    reclaimed_at = NOW + timedelta(seconds=31)
    second = jobs.claim_next("worker-b", lease_seconds=45, now=iso(reclaimed_at))
    assert second.id == first.id
    assert second.lease_owner == "worker-b"
    assert second.attempt_count == 2
    assert not jobs.heartbeat(
        first.id,
        "worker-a",
        progress=50,
        stage="generating",
        now=iso(reclaimed_at),
    )
    assert jobs.heartbeat(
        first.id,
        "worker-b",
        progress=50,
        stage="generating",
        now=iso(reclaimed_at),
    )


def test_one_active_job_and_queued_cancel_are_database_enforced(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    first = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", now=iso(NOW)
    )
    with pytest.raises(QaConflictError) as conflict:
        jobs.create_summary_turn_and_job(
            OWNER, created.id, "再次总结", "client-2", now=iso(NOW)
        )
    assert conflict.value.code == "QA_CONVERSATION_BUSY"

    cancelled = jobs.request_cancel(OWNER, first.job.id, now=iso(NOW))
    assert cancelled.status == "cancelled"
    assert qa.get_message(OWNER, first.pending.assistant_message.id).status == "cancelled"
    assert jobs.claim_next("worker", lease_seconds=30, now=iso(NOW)) is None


def test_failed_job_insert_rolls_back_the_new_turn(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    first = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", now=iso(NOW)
    )
    with connect(qa.db_path) as conn:
        conn.execute(
            """
            update qa_messages set status = 'failed'
            where id = ? and user_id = ?
            """,
            (first.pending.assistant_message.id, OWNER),
        )

    with pytest.raises(QaConflictError):
        jobs.create_summary_turn_and_job(
            OWNER, created.id, "另一个总结", "client-2", now=iso(NOW)
        )
    messages = qa.list_messages(OWNER, created.id).items
    assert {message.client_request_id for message in messages if message.role == "user"} == {
        "client-1"
    }


def test_running_cancel_wins_before_completion(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", now=iso(NOW)
    )
    claimed = jobs.claim_next("worker", lease_seconds=30, now=iso(NOW))
    requested = jobs.request_cancel(OWNER, claimed.id, now=iso(NOW))
    assert requested.status == "running"
    assert requested.cancel_requested_at == iso(NOW)
    assert not jobs.complete(claimed.id, "worker", now=iso(NOW))
    assert jobs.get(OWNER, claimed.id).status == "cancelled"
    assert qa.get_message(OWNER, enqueued.pending.assistant_message.id).status == "cancelled"


def test_job_completion_requires_the_answer_to_commit_first(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", now=iso(NOW)
    )
    claimed = jobs.claim_next("worker", lease_seconds=30, now=iso(NOW))
    assert not jobs.complete(claimed.id, "worker", now=iso(NOW))
    assert qa.complete_turn(
        OWNER,
        enqueued.pending.assistant_message.id,
        enqueued.pending.assistant_message.version,
        "总结结果",
        (),
        "none",
        None,
        now=iso(NOW),
    )
    assert jobs.complete(claimed.id, "worker", now=iso(NOW))


def test_transient_failure_retries_to_cap_then_fails_assistant(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", max_attempts=2, now=iso(NOW)
    )
    first = jobs.claim_next("worker-a", lease_seconds=30, now=iso(NOW))
    retried = jobs.fail_or_retry(
        first.id,
        "worker-a",
        "QA_ENGINE_UNAVAILABLE",
        "trace-1",
        now=iso(NOW),
    )
    assert retried.status == "queued"
    second = jobs.claim_next("worker-b", lease_seconds=30, now=iso(NOW))
    failed = jobs.fail_or_retry(
        second.id,
        "worker-b",
        "QA_ENGINE_UNAVAILABLE",
        "trace-2",
        now=iso(NOW),
    )
    assert failed.status == "failed"
    assert qa.get_message(OWNER, enqueued.pending.assistant_message.id).status == "failed"


def test_recover_expired_requeues_without_touching_terminal_jobs(repositories) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER, created.id, "总结", "client-1", now=iso(NOW)
    )
    jobs.claim_next("worker", lease_seconds=30, now=iso(NOW))
    assert jobs.recover_expired(now=iso(NOW + timedelta(seconds=31))) == 1
    assert jobs.get(OWNER, enqueued.job.id).status == "queued"
    assert jobs.recover_expired(now=iso(NOW + timedelta(seconds=32))) == 0


def test_claim_pass_terminalizes_expired_final_attempt_and_assistant(
    repositories,
) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER,
        created.id,
        "总结",
        "client-final-expiry",
        max_attempts=1,
        now=iso(NOW),
    )
    claimed = jobs.claim_next("worker-a", lease_seconds=30, now=iso(NOW))
    assert claimed is not None
    assert claimed.attempt_count == claimed.max_attempts == 1

    assert jobs.claim_next(
        "worker-b", lease_seconds=30, now=iso(NOW + timedelta(seconds=31))
    ) is None

    terminal = jobs.get(OWNER, enqueued.job.id)
    assistant = qa.get_message(OWNER, enqueued.pending.assistant_message.id)
    assert terminal.status == "failed"
    assert terminal.safe_error_code == "QA_JOB_INTERRUPTED"
    assert assistant.status == "failed"
    assert assistant.safe_error_code == "QA_JOB_INTERRUPTED"
    assert jobs.get_active_for_conversation(OWNER, created.id) is None
    assert not jobs.heartbeat(
        claimed.id,
        "worker-a",
        progress=50,
        stage="late",
        now=iso(NOW + timedelta(seconds=31)),
    )


def test_claim_pass_cancels_expired_cancel_requested_final_attempt(
    repositories,
) -> None:
    qa, jobs = repositories
    created = conversation(qa)
    enqueued = jobs.create_summary_turn_and_job(
        OWNER,
        created.id,
        "总结",
        "client-cancelled-expiry",
        max_attempts=1,
        now=iso(NOW),
    )
    claimed = jobs.claim_next("worker-a", lease_seconds=30, now=iso(NOW))
    assert claimed is not None
    requested = jobs.request_cancel(
        OWNER, claimed.id, now=iso(NOW + timedelta(seconds=1))
    )
    assert requested.status == "running"

    assert jobs.claim_next(
        "worker-b", lease_seconds=30, now=iso(NOW + timedelta(seconds=31))
    ) is None

    terminal = jobs.get(OWNER, enqueued.job.id)
    assistant = qa.get_message(OWNER, enqueued.pending.assistant_message.id)
    assert terminal.status == "cancelled"
    assert assistant.status == "cancelled"
    assert jobs.get_active_for_conversation(OWNER, created.id) is None


def test_commit_checks_expiry_after_acquiring_sqlite_write_lock(repositories, monkeypatch):
    from app import qa_job_repository as module

    qa, jobs = repositories
    created = conversation(qa)
    queued = jobs.create_summary_turn_and_job(OWNER, created.id, "总结", "lock-wait")
    start = "2026-11-23T00:00:00+00:00"
    now = start
    monkeypatch.setattr(module, "_utc_now", lambda: now)
    claimed = jobs.claim_next("old-worker", lease_seconds=1)
    real_connect = module.connect

    class AfterLock:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            nonlocal now
            result = self.connection.execute(sql, *args)
            if sql == "begin immediate":
                now = "2026-11-23T00:00:02+00:00"
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(module, "connect", lambda path: AfterLock(real_connect(path)))
    assert not jobs.commit_answer(
        claimed.id, "old-worker", queued.pending.assistant_message.version, "late", (), "none"
    )
    message = qa.get_message(OWNER, queued.pending.assistant_message.id)
    assert message.status == "pending"



def test_same_worker_cannot_write_through_an_old_attempt(repositories):
    qa, jobs = repositories
    created = conversation(qa)
    queued = jobs.create_summary_turn_and_job(OWNER, created.id, "总结", "same-worker")
    first = jobs.claim_next("worker", lease_seconds=5, now=iso(NOW))
    later = iso(NOW + timedelta(seconds=6))
    second = jobs.claim_next("worker", lease_seconds=5, now=later)
    message = queued.pending.assistant_message
    assert second.id == first.id
    assert second.attempt_count == first.attempt_count + 1
    assert not jobs.heartbeat(first.id, "worker", attempt=first.attempt_count, now=later)
    assert not jobs.complete(first.id, "worker", attempt=first.attempt_count, now=later)
    with pytest.raises(QaConflictError, match="QA_JOB_LEASE_LOST"):
        jobs.fail_or_retry(
            first.id, "worker", "test", None, attempt=first.attempt_count, now=later
        )
    assert not jobs.commit_answer(
        first.id, "worker", message.version, "late", (), "none",
        attempt=first.attempt_count, now=later,
    )
    assert jobs.heartbeat(second.id, "worker", attempt=second.attempt_count, now=later)
    assert jobs.commit_answer(
        second.id, "worker", message.version, "current", (), "none",
        attempt=second.attempt_count, now=later,
    )
    assert qa.get_message(OWNER, message.id).content == "current"



@pytest.mark.parametrize(
    ("claim_at", "check_at", "live"),
    [
        ("2026-11-23T00:00:00Z", "2026-11-23T00:00:01.750000Z", False),
        ("2026-11-23T00:00:00.750000Z", "2026-11-23T00:00:01Z", True),
    ],
)
@pytest.mark.parametrize("transition", ("heartbeat", "complete", "commit", "retry", "recover"))
def test_lease_comparison_preserves_subsecond_order(
    repositories, claim_at, check_at, live, transition
):
    qa, jobs = repositories
    created = conversation(qa)
    queued = jobs.create_summary_turn_and_job(OWNER, created.id, "总结", "subsecond")
    job = jobs.claim_next("worker", lease_seconds=1, now=claim_at)
    options = {"now": check_at, "attempt": job.attempt_count}
    if transition == "heartbeat":
        assert jobs.heartbeat(job.id, "worker", **options) is live
    elif transition == "complete":
        jobs.request_cancel(OWNER, job.id, now=claim_at)
        assert not jobs.complete(job.id, "worker", **options)
        assert jobs.get(OWNER, job.id).status == ("cancelled" if live else "running")
    elif transition == "commit":
        assert jobs.commit_answer(
            job.id, "worker", queued.pending.assistant_message.version,
            "answer", (), "none", **options,
        ) is live
    elif transition == "recover":
        assert jobs.recover_expired(now=check_at) == int(not live)
    elif live:
        assert jobs.fail_or_retry(job.id, "worker", "test", None, **options).status == "queued"
    else:
        with pytest.raises(QaConflictError, match="QA_JOB_LEASE_LOST"):
            jobs.fail_or_retry(job.id, "worker", "test", None, **options)



def test_commit_checks_for_shutdown_after_acquiring_write_lock(repositories, monkeypatch):
    from app import qa_job_repository as module

    qa, jobs = repositories
    queued = jobs.create_summary_turn_and_job(OWNER, conversation(qa).id, "总结", "stop")
    job = jobs.claim_next("worker", lease_seconds=60)
    stopped = False
    real_connect = module.connect

    class AfterLock:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            nonlocal stopped
            result = self.connection.execute(sql, *args)
            if sql == "begin immediate":
                stopped = True
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(module, "connect", lambda path: AfterLock(real_connect(path)))
    assert not jobs.commit_answer(
        job.id, "worker", queued.pending.assistant_message.version, "late", (), "none",
        attempt=job.attempt_count, stop_requested=lambda: stopped,
    )
    assert qa.get_message(OWNER, queued.pending.assistant_message.id).status == "pending"



def test_offset_timestamp_cannot_split_job_failure_from_assistant_failure(repositories):
    qa, jobs = repositories
    queued = jobs.create_summary_turn_and_job(OWNER, conversation(qa).id, "总结", "offset")
    job = jobs.claim_next("worker", lease_seconds=60, now="2026-11-23T00:00:00Z")
    failed = jobs.fail_or_retry(
        job.id, "worker", "test", None,
        retryable=False, attempt=job.attempt_count, now="2026-11-23T08:00:10+08:00",
    )
    assert failed.status == "failed"
    assert qa.get_message(OWNER, queued.pending.assistant_message.id).status == "failed"



def test_summary_retry_is_atomic_under_concurrent_requests(repositories, monkeypatch):
    qa, jobs = repositories
    convo = conversation(qa)
    original = jobs.create_summary_turn_and_job(OWNER, convo.id, "原指令", "original")
    claimed = jobs.claim_next("worker", lease_seconds=60)
    jobs.fail_or_retry(claimed.id, "worker", "QA_RAG_TRANSIENT", None, retryable=False)
    failed_id = original.pending.assistant_message.id
    insert = jobs._insert_summary_job

    def fail_insert(*args, **kwargs):
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(jobs, "_insert_summary_job", fail_insert)
    with pytest.raises(RuntimeError, match="controlled failure"):
        jobs.create_summary_retry_job(OWNER, convo.id, failed_id, "retry-one")
    assert len(qa.list_messages(OWNER, convo.id).items) == 2
    with connect(qa.db_path) as conn:
        assert conn.execute("select count(*) from qa_retry_requests").fetchone()[0] == 0
    monkeypatch.setattr(jobs, "_insert_summary_job", insert)

    barrier = Barrier(2)

    def retry(request):
        barrier.wait(timeout=3)
        return jobs.create_summary_retry_job(OWNER, convo.id, failed_id, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(retry, ("retry-one", "retry-two"))
    assert first.id == second.id
    assert first.input_message_id != original.job.input_message_id
    assert qa.get_message(OWNER, first.input_message_id).content == "原指令"
    assert len(qa.list_messages(OWNER, convo.id).items) == 4
    with pytest.raises(QaValidationError, match="already in use"):
        jobs.create_summary_retry_job(OWNER, convo.id, failed_id, "original")


def test_summary_retry_keeps_old_synchronous_children_and_request_bindings(repositories):
    qa, jobs = repositories
    convo = conversation(qa)

    def fail(question, client_id):
        start = jobs.create_summary_turn_and_job(OWNER, convo.id, question, client_id)
        claimed = jobs.claim_next("worker", lease_seconds=60)
        jobs.fail_or_retry(claimed.id, "worker", "QA_RAG_TRANSIENT", None, retryable=False)
        return start.pending.assistant_message.id

    first = fail("第一份摘要", "one")
    retried = qa.create_pending_retry(OWNER, convo.id, first, "legacy-key")
    assert qa.complete_turn(OWNER, retried.assistant_message.id, 0, "旧结果", (), "none", None)
    second = fail("第二份摘要", "two")
    other = qa.create_pending_retry(OWNER, convo.id, second, "other-key")
    assert qa.complete_turn(OWNER, other.assistant_message.id, 0, "旧结果二", (), "none", None)
    with connect(qa.db_path) as conn:
        conn.execute("delete from qa_retry_requests where failed_assistant_message_id = ?", (second,))

    with pytest.raises(QaValidationError, match="already in use"):
        jobs.resolve_legacy_summary_retry(OWNER, convo.id, second, "legacy-key")
    assert jobs.resolve_legacy_summary_retry(
        OWNER, convo.id, second, "new-key-for-legacy"
    ).id == other.assistant_message.id
    with pytest.raises(QaValidationError, match="already retried"):
        jobs.create_summary_retry_job(OWNER, convo.id, second, "new-key-for-legacy")
    with pytest.raises(QaValidationError, match="already in use"):
        jobs.create_summary_retry_job(OWNER, convo.id, first, "new-key-for-legacy")


def test_summary_retry_rechecks_busy_and_deletion_under_write_lock(repositories):
    qa, jobs = repositories
    convo = conversation(qa)
    original = jobs.create_summary_turn_and_job(OWNER, convo.id, "原指令", "original")
    claimed = jobs.claim_next("worker", lease_seconds=60)
    jobs.fail_or_retry(claimed.id, "worker", "QA_RAG_TRANSIENT", None, retryable=False)
    failed_id = original.pending.assistant_message.id

    qa.create_pending_turn(OWNER, convo.id, "普通问答", "joint", "ordinary")
    with pytest.raises(QaConflictError) as busy:
        jobs.create_summary_retry_job(OWNER, convo.id, failed_id, "retry")
    assert busy.value.code == "QA_CONVERSATION_BUSY"

    assert QaDeletionRepository(qa.db_path).create_conversation_deletion(OWNER, convo.id)
    with pytest.raises(QaValidationError) as fenced:
        jobs.create_summary_retry_job(OWNER, convo.id, failed_id, "retry")
    assert fenced.value.code == "QA_RETRY_NOT_ALLOWED"
    with connect(qa.db_path) as conn:
        assert conn.execute("select count(*) from qa_retry_requests").fetchone()[0] == 0
        assert conn.execute("select count(*) from qa_jobs").fetchone()[0] == 1
