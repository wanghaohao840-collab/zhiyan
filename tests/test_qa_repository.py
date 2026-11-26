from __future__ import annotations

import sqlite3

import pytest

from app.database import connect, initialize_database
from app.qa_models import (
    QaConflictError,
    QaDocumentCandidate,
    QaSourceDraft,
    QaValidationError,
)
from app.qa_repository import QaRepository


OWNER = "owner"
OTHER = "other"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "app.db"
    initialize_database(path)
    with connect(path) as conn:
        conn.executemany(
            """
            insert into users (
                id, username, username_key, password_hash, created_at, updated_at
            ) values (?, ?, ?, 'hash', '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z')
            """,
            ((OWNER, "Owner", "owner"), (OTHER, "Other", "other")),
        )
    return path


@pytest.fixture
def repository(db_path):
    return QaRepository(db_path)


def documents(owner: str = OWNER, count: int = 1):
    return tuple(
        QaDocumentCandidate(f"doc-{index}", f"文档 {index}.pdf", owner)
        for index in range(count)
    )


def test_schema_is_additive_idempotent_and_user_scoped(db_path) -> None:
    repository = QaRepository(db_path)
    created = repository.create_conversation(OWNER, documents())
    initialize_database(db_path)

    with connect(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        indexes = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'index'"
            )
        }
        foreign_key_violations = conn.execute("pragma foreign_key_check").fetchall()
    assert {
        "qa_conversations",
        "qa_conversation_documents",
        "qa_messages",
        "qa_message_sources",
        "qa_jobs",
        "qa_retry_requests",
    } <= tables
    assert {
        "ix_qa_conversations_user_recent",
        "ix_qa_messages_conversation_created",
        "uq_qa_messages_pending_conversation",
        "uq_qa_messages_client_request",
        "uq_qa_jobs_active_conversation",
    } <= indexes
    assert foreign_key_violations == []
    assert repository.get_conversation(OWNER, created.id) == created


def test_conversation_scope_is_ordered_immutable_and_user_scoped(repository) -> None:
    created = repository.create_conversation(OWNER, documents(count=2))
    assert [item.document_id for item in created.documents] == ["doc-0", "doc-1"]
    assert repository.get_conversation(OTHER, created.id) is None
    assert repository.hard_delete_conversation(OTHER, created.id) is False

    with pytest.raises(QaValidationError) as wrong_owner:
        repository.create_conversation(OWNER, documents(owner=OTHER))
    assert wrong_owner.value.code == "QA_DOCUMENT_NOT_FOUND"

    with connect(repository.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                insert into qa_conversation_documents (
                    conversation_id, user_id, document_id, document_name, position
                ) values (?, ?, 'forged', 'forged.pdf', 2)
                """,
                (created.id, OTHER),
            )


def test_duplicate_request_returns_original_pair_and_one_pending_slot(repository) -> None:
    conversation = repository.create_conversation(OWNER, documents(count=2))
    first = repository.create_pending_turn(
        OWNER, conversation.id, "  比较 两篇文档  ", "compare", "client-1"
    )
    duplicate = repository.create_pending_turn(
        OWNER, conversation.id, "different ignored text", "compare", "client-1"
    )
    assert duplicate.duplicate is True
    assert duplicate.user_message.id == first.user_message.id
    assert duplicate.assistant_message.id == first.assistant_message.id

    with pytest.raises(QaConflictError) as conflict:
        repository.create_pending_turn(
            OWNER, conversation.id, "另一个问题", "auto", "client-2"
        )
    assert conflict.value.code == "QA_CONVERSATION_BUSY"
    messages = repository.list_messages(OWNER, conversation.id).items
    assert len(messages) == 2
    assert [message.role for message in messages] == ["user", "assistant"]
    assert repository.get_conversation(OWNER, conversation.id).title == "比较 两篇文档"


def test_cross_user_cannot_win_a_message_transition(repository) -> None:
    conversation = repository.create_conversation(OWNER, documents())
    pending = repository.create_pending_turn(
        OWNER, conversation.id, "问题", "auto", "client-1"
    )
    assert not repository.complete_turn(
        OTHER,
        pending.assistant_message.id,
        pending.assistant_message.version,
        "forged",
        (),
        "none",
        None,
    )
    assert repository.get_message(OWNER, pending.assistant_message.id).status == "pending"


def test_complete_persists_ordered_immutable_sources_and_rejects_stale_version(
    repository,
) -> None:
    conversation = repository.create_conversation(OWNER, documents())
    pending = repository.create_pending_turn(
        OWNER, conversation.id, "问题", "auto", "client-1"
    )
    sources = (
        QaSourceDraft("[2]", "doc-0", "文档.pdf", page_number=2, excerpt="第二", chunk_id="chunk-2"),
        QaSourceDraft("[1]", "doc-0", "文档.pdf", page_number=1, excerpt="第一"),
    )
    assert repository.complete_turn(
        OWNER,
        pending.assistant_message.id,
        pending.assistant_message.version,
        "回答",
        sources,
        "available",
        "memory-1",
    )
    assert not repository.complete_turn(
        OWNER,
        pending.assistant_message.id,
        pending.assistant_message.version,
        "late",
        (),
        "none",
        None,
    )
    message = repository.get_message(OWNER, pending.assistant_message.id)
    assert message is not None
    assert message.status == "completed"
    assert [source.citation_id for source in message.sources] == ["[2]", "[1]"]
    assert [source.chunk_id for source in message.sources] == ["chunk-2", None]
    assert repository.get_message(OTHER, pending.assistant_message.id) is None


def test_fail_cancel_retry_link_and_delete_fence(repository) -> None:
    conversation = repository.create_conversation(OWNER, documents())
    failed = repository.create_pending_turn(
        OWNER, conversation.id, "失败问题", "auto", "client-fail"
    )
    assert repository.fail_turn(
        OWNER, failed.assistant_message.id, 0, "QA_ENGINE_UNAVAILABLE", "trace-1"
    )
    assert not repository.cancel_turn(OWNER, failed.assistant_message.id, 0)

    retry = repository.create_pending_retry(
        OWNER,
        conversation.id,
        failed.assistant_message.id,
        "client-retry",
    )
    duplicate = repository.create_pending_retry(
        OWNER,
        conversation.id,
        failed.assistant_message.id,
        "client-retry",
    )
    same_target = repository.create_pending_retry(
        OWNER,
        conversation.id,
        failed.assistant_message.id,
        "another-client-retry",
    )
    assert retry.user_message.id == failed.user_message.id
    assert retry.assistant_message.retry_of_message_id == failed.assistant_message.id
    assert retry.assistant_message.turn_id == failed.user_message.turn_id
    assert duplicate.duplicate is True
    assert duplicate.assistant_message.id == retry.assistant_message.id
    assert same_target.duplicate is True
    assert same_target.assistant_message.id == retry.assistant_message.id
    messages = repository.list_messages(OWNER, conversation.id).items
    assert [message.role for message in messages].count("user") == 1
    assert [message.role for message in messages].count("assistant") == 2
    assert repository.hard_delete_conversation(OWNER, conversation.id)
    assert not repository.complete_turn(
        OWNER, retry.assistant_message.id, 0, "late", (), "none", None
    )
    assert repository.list_messages(OWNER, conversation.id).items == ()
    with connect(repository.db_path) as conn:
        assert conn.execute("select count(*) from qa_retry_requests").fetchone()[0] == 0


def test_cursor_pagination_uses_id_tiebreaker(repository) -> None:
    ids = [repository.create_conversation(OWNER, documents()).id for _ in range(3)]
    with connect(repository.db_path) as conn:
        conn.execute(
            "update qa_conversations set last_message_at = '2026-08-26T10:00:00Z'"
        )
    first = repository.list_conversations(OWNER, limit=2)
    second = repository.list_conversations(OWNER, cursor=first.next_cursor, limit=2)
    assert len(first.items) == 2
    assert len(second.items) == 1
    assert {item.id for item in (*first.items, *second.items)} == set(ids)


def test_recent_message_pages_start_newest_and_page_older_without_overlap(
    repository,
) -> None:
    created = repository.create_conversation(OWNER, documents())
    expected_ids: list[str] = []
    for index in range(3):
        pending = repository.create_pending_turn(
            OWNER,
            created.id,
            f"问题 {index}",
            "auto",
            f"request-{index}",
            now=f"2026-08-26T10:00:0{index}Z",
        )
        assert repository.complete_turn(
            OWNER,
            pending.assistant_message.id,
            pending.assistant_message.version,
            f"回答 {index}",
            (),
            "none",
            None,
            now=f"2026-08-26T10:00:0{index}Z",
        )
        expected_ids.extend(
            (pending.user_message.id, pending.assistant_message.id)
        )

    with connect(repository.db_path) as conn:
        conn.execute(
            "update qa_messages set created_at = '2026-08-26T10:00:00Z' "
            "where conversation_id = ?",
            (created.id,),
        )
    expected_ids = sorted(expected_ids)

    first = repository.list_recent_messages(OWNER, created.id, limit=2)
    second = repository.list_recent_messages(
        OWNER, created.id, cursor=first.next_cursor, limit=2
    )
    third = repository.list_recent_messages(
        OWNER, created.id, cursor=second.next_cursor, limit=2
    )

    assert [item.id for item in first.items] == expected_ids[-2:]
    assert [item.id for item in second.items] == expected_ids[-4:-2]
    assert [item.id for item in third.items] == expected_ids[:2]
    assert first.next_cursor is not None
    assert second.next_cursor is not None
    assert third.next_cursor is None
    assert len({item.id for page in (first, second, third) for item in page.items}) == 6
    assert repository.list_recent_messages(OTHER, created.id).items == ()
    assert [item.id for item in repository.list_messages(OWNER, created.id).items] == expected_ids


def test_recent_first_page_contains_latest_answer_beyond_200_messages(
    repository,
) -> None:
    created = repository.create_conversation(OWNER, documents())
    first_assistant_id = ""
    latest_assistant_id = ""
    for index in range(101):
        timestamp = (
            f"2026-08-26T{10 + index // 60:02d}:{index % 60:02d}:00Z"
        )
        pending = repository.create_pending_turn(
            OWNER,
            created.id,
            f"问题 {index}",
            "auto",
            f"long-history-{index}",
            now=timestamp,
        )
        assert repository.complete_turn(
            OWNER,
            pending.assistant_message.id,
            pending.assistant_message.version,
            f"回答 {index}",
            (),
            "none",
            None,
            now=timestamp,
        )
        if index == 0:
            first_assistant_id = pending.assistant_message.id
        latest_assistant_id = pending.assistant_message.id

    page = repository.list_recent_messages(OWNER, created.id)
    ids = {item.id for item in page.items}
    assert len(page.items) == 50
    assert latest_assistant_id in ids
    assert first_assistant_id not in ids
    assert page.next_cursor is not None

    older = repository.list_recent_messages(
        OWNER, created.id, cursor=page.next_cursor
    )
    assert len(older.items) == 50
    assert ids.isdisjoint(item.id for item in older.items)


def test_recovery_fails_only_orphaned_synchronous_pending(repository) -> None:
    sync_conversation = repository.create_conversation(OWNER, documents())
    sync = repository.create_pending_turn(
        OWNER, sync_conversation.id, "同步", "auto", "sync"
    )
    summary_conversation = repository.create_conversation(OWNER, documents())
    summary = repository.create_pending_turn(
        OWNER, summary_conversation.id, "总结", "summary", "summary"
    )
    with connect(repository.db_path) as conn:
        conn.execute(
            """
            insert into qa_jobs (
                id, conversation_id, user_id, input_message_id,
                assistant_message_id, status, stage, progress, max_attempts,
                version, created_at, updated_at
            ) values (
                'job', ?, ?, ?, ?, 'queued', 'queued', 0, 3, 0,
                '2026-08-26T00:00:00Z', '2026-08-26T00:00:00Z'
            )
            """,
            (
                summary_conversation.id,
                OWNER,
                summary.user_message.id,
                summary.assistant_message.id,
            ),
        )

    assert repository.recover_interrupted_questions() == 1
    assert repository.get_message(OWNER, sync.assistant_message.id).status == "failed"
    assert repository.get_message(OWNER, summary.assistant_message.id).status == "pending"
