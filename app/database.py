from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.learning_schema import LEARNING_SCHEMA


SCHEMA = """
create table if not exists users (
    id text primary key,
    username text not null,
    username_key text not null unique,
    password_hash text not null,
    status text not null default 'active',
    created_at text not null,
    updated_at text not null
);

create table if not exists report_records (
    id text primary key,
    user_id text not null references users(id) on delete cascade,
    title text not null,
    relative_path text not null,
    created_at text not null
);

create table if not exists import_batches (
    id text primary key,
    user_id text not null references users(id) on delete cascade,
    created_at text not null,
    updated_at text not null,
    unique(id, user_id)
);

create table if not exists import_tasks (
    id text primary key,
    batch_id text not null,
    user_id text not null,
    document_id text not null,
    original_name text not null,
    file_suffix text not null,
    size_bytes integer not null,
    staged_relative_path text not null,
    status text not null check(status in ('queued','running','retry_wait','succeeded','failed','cancelled')),
    stage text not null,
    progress integer not null check(progress between 0 and 100),
    total_attempt_count integer not null default 0,
    auto_retry_count integer not null default 0,
    manual_retry_count integer not null default 0,
    max_auto_retries integer not null default 3,
    next_attempt_at text,
    cancel_requested_at text,
    error_code text,
    error_summary text,
    created_at text not null,
    started_at text,
    finished_at text,
    updated_at text not null,
    foreign key(batch_id, user_id) references import_batches(id, user_id)
        on delete cascade,
    unique(user_id, document_id)
);

create unique index if not exists uq_import_tasks_running_user
on import_tasks(user_id) where status = 'running';
create index if not exists ix_import_tasks_scheduler
on import_tasks(status, next_attempt_at, created_at);
create index if not exists ix_import_tasks_user_created
on import_tasks(user_id, created_at);

create table if not exists graph_document_cleanup (
    user_id text not null references users(id) on delete cascade,
    document_id text not null,
    primary key (user_id, document_id)
);

create table if not exists qa_conversations (
    id text primary key,
    user_id text not null references users(id) on delete cascade,
    title text not null,
    origin text not null check(origin in ('product','legacy_json','legacy_gradio')),
    rolling_summary text not null default '',
    summary_through_message_id text,
    summary_version integer not null default 0,
    version integer not null default 0,
    created_at text not null,
    updated_at text not null,
    last_message_at text not null,
    unique(user_id, id)
);

create table if not exists qa_conversation_documents (
    conversation_id text not null,
    user_id text not null,
    document_id text not null,
    document_name text not null,
    position integer not null check(position >= 0),
    primary key(conversation_id, document_id),
    unique(conversation_id, position),
    foreign key(conversation_id, user_id)
        references qa_conversations(id, user_id) on delete cascade
);

create table if not exists qa_messages (
    id text primary key,
    conversation_id text not null,
    user_id text not null,
    turn_id text not null,
    role text not null check(role in ('user','assistant')),
    status text not null check(status in ('pending','completed','failed','cancelled')),
    mode text check(mode is null or mode in ('auto','joint','compare','summary')),
    content text not null default '',
    source_state text not null default 'none'
        check(source_state in ('available','none','legacy_unavailable')),
    client_request_id text,
    retry_of_message_id text,
    memory_id text,
    memory_sync_status text not null default 'not_required'
        check(memory_sync_status in (
            'pending','running','completed','failed','not_required'
        )),
    memory_sync_attempt_count integer not null default 0,
    memory_sync_lease_owner text,
    memory_sync_lease_expires_at text,
    safe_error_code text,
    trace_id text,
    version integer not null default 0,
    created_at text not null,
    updated_at text not null,
    completed_at text,
    unique(user_id, conversation_id, id),
    foreign key(conversation_id, user_id)
        references qa_conversations(id, user_id) on delete cascade,
    foreign key(retry_of_message_id, conversation_id, user_id)
        references qa_messages(id, conversation_id, user_id),
    check(role = 'user' or client_request_id is null)
);

create table if not exists qa_retry_requests (
    user_id text not null,
    conversation_id text not null,
    failed_assistant_message_id text not null,
    assistant_message_id text not null,
    client_request_id text not null,
    created_at text not null,
    primary key(user_id, conversation_id, failed_assistant_message_id),
    unique(user_id, conversation_id, client_request_id),
    unique(user_id, conversation_id, assistant_message_id),
    foreign key(failed_assistant_message_id, conversation_id, user_id)
        references qa_messages(id, conversation_id, user_id) on delete cascade,
    foreign key(assistant_message_id, conversation_id, user_id)
        references qa_messages(id, conversation_id, user_id) on delete cascade
);

create table if not exists qa_message_sources (
    id text primary key,
    assistant_message_id text not null,
    conversation_id text not null,
    user_id text not null,
    position integer not null check(position >= 0),
    citation_id text not null,
    document_id text not null,
    document_name text not null,
    page_number integer,
    section text,
    excerpt text not null,
    reference text not null,
    truncated integer not null default 0 check(truncated in (0, 1)),
    source_type text not null,
    chunk_id text,
    unique(assistant_message_id, position),
    foreign key(assistant_message_id, conversation_id, user_id)
        references qa_messages(id, conversation_id, user_id) on delete cascade
);

create table if not exists qa_jobs (
    id text primary key,
    conversation_id text not null,
    user_id text not null,
    input_message_id text not null,
    assistant_message_id text not null,
    status text not null check(status in (
        'queued','running','completed','failed','cancelled'
    )),
    stage text not null,
    progress integer not null check(progress between 0 and 100),
    cancel_requested_at text,
    attempt_count integer not null default 0,
    max_attempts integer not null default 3 check(max_attempts > 0),
    lease_owner text,
    lease_expires_at text,
    lease_duration_seconds integer,
    safe_error_code text,
    trace_id text,
    version integer not null default 0,
    created_at text not null,
    started_at text,
    finished_at text,
    updated_at text not null,
    unique(user_id, conversation_id, id),
    unique(user_id, input_message_id),
    unique(user_id, assistant_message_id),
    foreign key(conversation_id, user_id)
        references qa_conversations(id, user_id) on delete cascade,
    foreign key(input_message_id, conversation_id, user_id)
        references qa_messages(id, conversation_id, user_id) on delete cascade,
    foreign key(assistant_message_id, conversation_id, user_id)
        references qa_messages(id, conversation_id, user_id) on delete cascade
);

create index if not exists ix_qa_conversations_user_recent
on qa_conversations(user_id, last_message_at desc, id desc);
create index if not exists ix_qa_messages_conversation_created
on qa_messages(user_id, conversation_id, created_at, id);
create index if not exists ix_qa_messages_memory_sync
on qa_messages(memory_sync_status, memory_sync_lease_expires_at, created_at)
where role = 'assistant' and status = 'completed';
create index if not exists ix_qa_message_sources_order
on qa_message_sources(user_id, assistant_message_id, position);
create unique index if not exists uq_qa_messages_pending_conversation
on qa_messages(user_id, conversation_id)
where role = 'assistant' and status = 'pending';
create unique index if not exists uq_qa_messages_client_request
on qa_messages(user_id, conversation_id, client_request_id)
where role = 'user' and client_request_id is not null;
create unique index if not exists uq_qa_jobs_active_conversation
on qa_jobs(user_id, conversation_id)
where status in ('queued','running');
create index if not exists ix_qa_jobs_scheduler
on qa_jobs(status, lease_expires_at, created_at, id);

create table if not exists qa_deletion_fences (
    id text primary key,
    user_id text not null references users(id) on delete cascade,
    target_type text not null check(target_type in ('conversation','document')),
    target_id text not null,
    status text not null check(status in ('queued','running','completed','failed')),
    stage text not null check(stage in (
        'fenced','qa_rows_removed','memory_removed','document_removed','completed'
    )),
    affected_conversation_count integer not null default 0,
    conversation_ids_json text not null default '[]',
    memory_ids_json text not null default '[]',
    attempt_count integer not null default 0,
    lease_owner text,
    lease_expires_at text,
    safe_error_code text,
    trace_id text,
    created_at text not null,
    updated_at text not null,
    finished_at text
);
create unique index if not exists uq_qa_deletion_active_target
on qa_deletion_fences(user_id, target_type, target_id)
where status != 'completed';
create index if not exists ix_qa_deletion_scheduler
on qa_deletion_fences(status, lease_expires_at, created_at, id);
create index if not exists ix_qa_deletion_target
on qa_deletion_fences(user_id, target_type, target_id, status);

create table if not exists qa_legacy_imports (
    user_id text primary key references users(id) on delete cascade,
    migration_version integer not null,
    source_digest text not null,
    imported_count integer not null,
    skipped_count integer not null,
    completed_at text not null
);

create table if not exists notes (
    id text primary key,
    user_id text not null references users(id) on delete cascade,
    body_markdown text not null check(length(body_markdown) between 1 and 20000),
    concept text check(concept is null or length(concept) <= 120),
    version integer not null default 1 check(version > 0),
    projection_state text not null default 'pending'
        check(projection_state in ('pending','ready','failed')),
    client_request_id text not null,
    request_digest text not null,
    created_at text not null,
    updated_at text not null,
    deleted_at text,
    unique(user_id, id),
    unique(user_id, client_request_id)
);
create index if not exists ix_notes_user_current
on notes(user_id, deleted_at, updated_at desc, id desc);

create table if not exists note_tags (
    user_id text not null,
    note_id text not null,
    normalized_tag text not null,
    display_tag text not null check(length(display_tag) between 1 and 32),
    primary key(user_id, note_id, normalized_tag),
    foreign key(note_id, user_id) references notes(id, user_id) on delete cascade
);
create index if not exists ix_note_tags_lookup
on note_tags(user_id, normalized_tag, note_id);

create table if not exists note_sources (
    id text primary key,
    user_id text not null,
    note_id text not null,
    source_kind text not null check(source_kind in ('qa_message','qa_citation')),
    qa_thread_id text,
    qa_message_id text,
    citation_id text,
    document_id text,
    locator_json text,
    title_snapshot text,
    excerpt_snapshot text,
    source_deleted_at text,
    created_at text not null,
    foreign key(note_id, user_id) references notes(id, user_id) on delete cascade
);
create index if not exists ix_note_sources_note
on note_sources(user_id, note_id, created_at, id);
create index if not exists ix_note_sources_document
on note_sources(user_id, document_id) where source_deleted_at is null;
create index if not exists ix_note_sources_thread
on note_sources(user_id, qa_thread_id) where source_deleted_at is null;

create table if not exists note_document_sources (
    id text primary key,
    user_id text not null,
    note_id text not null,
    document_id text,
    chunk_id text,
    chunk_index integer check(chunk_index >= 0),
    content_sha256 text,
    locator_json text,
    title_snapshot text,
    excerpt_snapshot text,
    source_deleted_at text,
    created_at text not null,
    check (
        (source_deleted_at is null and document_id is not null and chunk_id is not null
         and chunk_index is not null and content_sha256 is not null
         and length(content_sha256)=64 and locator_json is not null and excerpt_snapshot is not null)
        or (source_deleted_at is not null and document_id is null and chunk_id is null
            and chunk_index is null and content_sha256 is null and locator_json is null
            and title_snapshot is null and excerpt_snapshot is null)
    ),
    foreign key(note_id, user_id) references notes(id, user_id) on delete cascade
);
create index if not exists ix_note_document_sources_note
on note_document_sources(user_id, note_id, created_at, id);
create index if not exists ix_note_document_sources_document
on note_document_sources(user_id, document_id) where source_deleted_at is null;

create table if not exists note_projection_tasks (
    id text primary key,
    user_id text not null,
    note_id text not null,
    note_version integer not null check(note_version > 0),
    operation text not null check(operation in ('upsert','delete')),
    status text not null check(status in ('queued','running','failed','completed')),
    attempt_count integer not null default 0 check(attempt_count >= 0),
    available_at text not null,
    lease_owner text,
    lease_expires_at text,
    last_error_code text,
    created_at text not null,
    finished_at text,
    unique(user_id, note_id, note_version, operation),
    foreign key(note_id, user_id) references notes(id, user_id) on delete cascade
);
create index if not exists ix_note_projection_scheduler
on note_projection_tasks(status, available_at, lease_expires_at, created_at, id);
create index if not exists ix_note_projection_note
on note_projection_tasks(user_id, note_id, note_version);

create table if not exists note_legacy_imports (
    user_id text not null,
    legacy_import_key text not null,
    note_id text not null,
    source_digest text not null,
    legacy_memory_id text,
    imported_at text not null,
    legacy_memory_cleaned_at text,
    primary key(user_id, legacy_import_key),
    unique(user_id, note_id),
    foreign key(note_id, user_id) references notes(id, user_id) on delete cascade
);

create table if not exists data_migrations (
    id integer primary key autoincrement,
    migration_key text not null unique,
    claimed_by_user_id text references users(id),
    status text not null,
    backup_path text,
    manifest_path text,
    skipped_summary text,
    conflict_summary text,
    started_at text not null,
    completed_at text,
    error_summary text
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def initialize_database(db_path: Path | str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _verify_fts5(conn)
        _upgrade_import_tasks_for_cancellation(conn)
        # F1: idempotent upgrade for existing databases missing
        # the conflict_summary column.
        _ensure_column(conn, "data_migrations", "conflict_summary", "text")
        _ensure_column(conn, "qa_message_sources", "chunk_id", "text")
        conn.executescript(LEARNING_SCHEMA)


def _verify_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            """
            create virtual table if not exists notes_fts using fts5(
                note_id unindexed, user_id unindexed, body_markdown, concept, tags_text
            )
            """
        )
        conn.execute("select rowid from notes_fts limit 1").fetchall()
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "SQLite FTS5 support is required for learning notes"
        ) from exc


def _upgrade_import_tasks_for_cancellation(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "select sql from sqlite_master where type = 'table' and name = 'import_tasks'"
    ).fetchone()
    table_sql = row["sql"] if row is not None else ""
    if "cancelled" in table_sql and "cancel_requested_at" in table_sql:
        return

    conn.execute("begin immediate")
    try:
        conn.execute(
            """
            create table import_tasks_new (
                id text primary key,
                batch_id text not null,
                user_id text not null,
                document_id text not null,
                original_name text not null,
                file_suffix text not null,
                size_bytes integer not null,
                staged_relative_path text not null,
                status text not null check(status in (
                    'queued','running','retry_wait','succeeded','failed','cancelled'
                )),
                stage text not null,
                progress integer not null check(progress between 0 and 100),
                total_attempt_count integer not null default 0,
                auto_retry_count integer not null default 0,
                manual_retry_count integer not null default 0,
                max_auto_retries integer not null default 3,
                next_attempt_at text,
                cancel_requested_at text,
                error_code text,
                error_summary text,
                created_at text not null,
                started_at text,
                finished_at text,
                updated_at text not null,
                foreign key(batch_id, user_id) references import_batches(id, user_id)
                    on delete cascade,
                unique(user_id, document_id)
            )
            """
        )
        conn.execute(
            """
            insert into import_tasks_new (
                id, batch_id, user_id, document_id, original_name, file_suffix,
                size_bytes, staged_relative_path, status, stage, progress,
                total_attempt_count, auto_retry_count, manual_retry_count,
                max_auto_retries, next_attempt_at, cancel_requested_at,
                error_code, error_summary, created_at, started_at, finished_at,
                updated_at
            )
            select
                id, batch_id, user_id, document_id, original_name, file_suffix,
                size_bytes, staged_relative_path, status, stage, progress,
                total_attempt_count, auto_retry_count, manual_retry_count,
                max_auto_retries, next_attempt_at, null,
                error_code, error_summary, created_at, started_at, finished_at,
                updated_at
            from import_tasks
            """
        )
        conn.execute("drop table import_tasks")
        conn.execute("alter table import_tasks_new rename to import_tasks")
        conn.execute(
            """
            create unique index if not exists uq_import_tasks_running_user
            on import_tasks(user_id) where status = 'running'
            """
        )
        conn.execute(
            """
            create index if not exists ix_import_tasks_scheduler
            on import_tasks(status, next_attempt_at, created_at)
            """
        )
        conn.execute(
            """
            create index if not exists ix_import_tasks_user_created
            on import_tasks(user_id, created_at)
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_column(conn, table: str, column: str, col_type: str) -> None:
    """Add *column* to *table* if it does not already exist."""
    rows = conn.execute(f"pragma table_info('{table}')").fetchall()
    existing = {row["name"] for row in rows}
    if column not in existing:
        conn.execute(
            f"alter table {table} add column {column} {col_type}"
        )


@contextmanager
def transaction(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
