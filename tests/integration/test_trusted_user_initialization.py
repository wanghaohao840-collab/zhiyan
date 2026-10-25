"""Disposable head-17 acceptance for atomic first-user initialization."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import os
from pathlib import Path
from threading import Barrier, Event
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import UUID, uuid4

import boto3
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from botocore.config import Config as BotoConfig
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

import app.auth as auth_module
import app.trusted_user_initialization as initialization
from app.trusted_user_initialization import (
    InitializationError, InitializationOutcomeUnknown, InitializedUser,
    initialize_trusted_user,
)
from app.auth import AuthError, hash_password, verify_password
from app.history import EMPTY_HISTORY
from app.import_memory_publication import ImportMemoryPublicationService
from app.object_store import S3ObjectStore
from app.postgres import PostgresDatabase
from app.postgres_auth import PostgresAuthService
from app.postgres_coordination import MutationLeaseLost, PostgresUserMutationCoordinator
from app.postgres_history_document_witnesses import PostgresHistoryDocumentWitnessRepository
from app.postgres_snapshots import PostgresSnapshotRepository
from app.postgres_vector_generations import PostgresVectorGenerationAuthority, VectorScope
from app.trusted_user_initialization_settings import TrustedUserInitializationSettings
from app.vector_generation_service import VectorGenerationService
from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
from hello_agents.memory.storage.vector_store import QdrantVectorStore
from tests.integration.test_import_document_publication import _point, _task


ROOT = Path(__file__).resolve().parents[2]
EMPTY_DIGEST = sha256(b"[]").hexdigest()
PASSWORD = "current-password-123"


@dataclass
class Site:
    open_pool: object
    settings: TrustedUserInitializationSettings
    base_url: str
    scoped_url: str
    schema: str


@pytest.fixture
def site(monkeypatch):
    """The whole upgrade and pool lifetime are inside the cleanup boundary."""
    base = os.environ.get("POSTGRES_TEST_URL")
    qdrant = os.environ.get("GENERATION_QDRANT_TEST_URL")
    s3 = os.environ.get("S3_TEST_ENDPOINT")
    if not all((base, qdrant, s3)):
        pytest.fail("POSTGRES_TEST_URL, GENERATION_QDRANT_TEST_URL and S3_TEST_ENDPOINT are required")
    schema = "init_t3_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    pools = []
    try:
        alembic_url = make_url(base).set(
            drivername="postgresql+psycopg",
            query={"options": f"-csearch_path={schema}"},
        ).render_as_string(hide_password=False)
        scoped = alembic_url.replace("postgresql+psycopg://", "postgresql://", 1)

        def open_pool(*, timeout=10.0):
            db = PostgresDatabase(scoped, min_size=1, max_size=4, timeout=timeout)
            pools.append(db)
            db.open()
            return db

        rag = EmbeddingProfile("simple", "", "SimpleEmbedding", "v1", 4)
        episode = EmbeddingProfile("test-explicit", "", "episode-test", "v1", 4)
        settings = TrustedUserInitializationSettings(
            enabled=True, database_url=scoped, schema=schema,
            s3_endpoint_url=s3, s3_bucket="init-t3-" + uuid4().hex,
            qdrant_url=qdrant, rag_collection="init_t3_rag_" + uuid4().hex,
            episode_collection="init_t3_episode_" + uuid4().hex,
            rag_profile=rag, episode_profile=episode,
        )
        monkeypatch.setenv("DATABASE_URL", alembic_url)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "20261022_17")
        yield Site(open_pool, settings, base, scoped, schema)
    finally:
        cleanup_errors = []
        for db in pools:
            try:
                db.close()
            except Exception as error:
                cleanup_errors.append(error)
        try:
            with psycopg.connect(base, autocommit=True) as admin:
                admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))
        except Exception as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            raise cleanup_errors[0]


def _username():
    return "init_" + uuid4().hex[:20]


def _call(db, settings, *, operation=None, username=None, password=PASSWORD, preflight=None):
    operation = operation or uuid4()
    username = username or _username()
    preflight = preflight or (lambda: None)
    return initialize_trusted_user(
        db, operation, username, password, settings, preflight=preflight,
    )


def _failure(code, call):
    with pytest.raises(InitializationError) as raised:
        call()
    error = raised.value
    assert error.code == code
    assert str(error) == code
    for secret in (PASSWORD, "postgresql://", "scrypt$", "select ", "insert ",
                   "raw ", "secret s3 token", "cleanup replacement", "lease diagnostic"):
        assert secret not in repr(error).lower()
    return error


def _rows(db):
    """Read all authority tables through an independent committed connection."""
    with db.transaction() as cursor:
        return {table: cursor.execute("select * from " + table).fetchall() for table in (
            "users", "user_snapshots", "vector_indexes", "vector_heads",
            "vector_generations", "history_document_witnesses",
            "trusted_user_initializations", "user_mutation_leases",
            "import_tasks", "document_objects", "memory_documents",
        )}


def _empty_site(db):
    rows = _rows(db)
    assert all(not entries for entries in rows.values()), {
        table: len(entries) for table, entries in rows.items()
    }


def _pg_jsonb_digest(db, value):
    with db.transaction() as cursor:
        return cursor.execute(
            "select encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as digest",
            (Jsonb(value),),
        ).fetchone()["digest"]


def _assert_baseline(db, settings, result, operation, username):
    assert type(result) is InitializedUser
    assert result.operation_id == operation
    assert result.user_id == str(UUID(result.user_id))
    assert result.username == username.strip()
    assert result.durable_initialized is True and result.replayed is False
    assert PASSWORD not in repr(result) and "scrypt$" not in repr(result)
    rows = _rows(db)
    assert len(rows["users"]) == 1
    user = rows["users"][0]
    assert (user["id"], user["username"], user["status"]) == (
        result.user_id, result.username, "active")
    assert verify_password(PASSWORD, user["password_hash"])
    snapshots = {row["kind"]: row for row in rows["user_snapshots"]}
    assert set(snapshots) == {"history", "memory"}
    assert snapshots["history"]["version"] == snapshots["memory"]["version"] == 1
    assert snapshots["history"]["payload"] == EMPTY_HISTORY
    assert snapshots["memory"]["payload"] == {"user_id": result.user_id, "memories": []}
    assert len(rows["vector_indexes"]) == len(rows["vector_heads"]) == 2
    assert len(rows["vector_generations"]) == 2
    heads = {(row["vector_kind"], row["namespace"]): row for row in rows["vector_heads"]}
    assert set(heads) == {("rag", "pdf_" + result.user_id), ("episode", "episodes")}
    generations = {row["generation_id"]: row for row in rows["vector_generations"]}
    assert len(generations) == 2
    for head in heads.values():
        receipt = generations[head["last_generation_id"]]
        assert (head["revision"], head["generation_id"], head["index_revision"],
                head["snapshot_version"]) == (1, None, 1, 1)
        assert (receipt["state"], receipt["base_revision"], receipt["index_revision"],
                receipt["publication_revision"], receipt["publication_snapshot_version"],
                receipt["expected_count"], receipt["content_digest"]) == (
                    "published", None, 1, 1, 1, 0, EMPTY_DIGEST)
        assert (receipt["tenant_id"], receipt["vector_kind"], receipt["namespace"],
                receipt["index_key"]) == (
                    result.user_id, head["vector_kind"], head["namespace"], head["index_key"])
    assert len(rows["history_document_witnesses"]) == 1
    witness = rows["history_document_witnesses"][0]
    assert (witness["head_revision"], witness["document_count"],
            witness["documents_sha256"]) == (1, 0, EMPTY_DIGEST)
    assert witness["last_generation_id"] == heads[("rag", "pdf_" + result.user_id)]["last_generation_id"]
    assert len(rows["trusted_user_initializations"]) == 1
    ledger = rows["trusted_user_initializations"][0]
    assert (ledger["operation_id"], ledger["format_version"], ledger["user_id"],
            ledger["username_key"]) == (operation, 1, result.user_id, username.strip().casefold())
    assert ledger["resource_descriptor"] == settings.resource_descriptor()
    assert ledger["resource_descriptor_sha256"] == _pg_jsonb_digest(db, settings.resource_descriptor())
    assert ledger["history_initial_sha256"] == _pg_jsonb_digest(db, EMPTY_HISTORY)
    assert ledger["memory_initial_sha256"] == _pg_jsonb_digest(
        db, {"user_id": result.user_id, "memories": []})
    assert ledger["empty_manifest_sha256"] == ledger["history_document_evidence_sha256"] == EMPTY_DIGEST
    assert ledger["rag_initial_generation_id"] == heads[("rag", "pdf_" + result.user_id)]["last_generation_id"]
    assert ledger["episode_initial_generation_id"] == heads[("episode", "episodes")]["last_generation_id"]
    assert ledger["completed_at"] is not None
    assert not rows["import_tasks"] and not rows["document_objects"] and not rows["memory_documents"]
    return rows


def test_new_account_is_one_invisible_then_committed_baseline(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    operation, username = uuid4(), _username()
    calls = []
    original = PostgresUserMutationCoordinator.release_in_transaction

    def inspect_before_commit(self, cursor, lease, **kwargs):
        _empty_site(observer)
        calls.append("before-commit")
        return original(self, cursor, lease, **kwargs)

    monkeypatch.setattr(PostgresUserMutationCoordinator, "release_in_transaction", inspect_before_commit)
    result = _call(db, site.settings, operation=operation, username=username,
                   preflight=lambda: calls.append("preflight"))
    assert calls == ["preflight", "before-commit"]
    _assert_baseline(observer, site.settings, result, operation, username)


def test_pg_only_replay_is_read_only_and_uses_current_password(site, monkeypatch):
    db = site.open_pool()
    operation, username = uuid4(), _username()
    result = _call(db, site.settings, operation=operation, username=username)
    before = deepcopy(_rows(db))
    def forbidden(*args, **kwargs):
        pytest.fail("replay attempted preflight, new hash, or lease")
    monkeypatch.setattr(initialization, "hash_password", forbidden, raising=False)
    monkeypatch.setattr(auth_module, "hash_password", forbidden)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction", forbidden)
    replay = _call(db, site.settings, operation=operation, username=username,
                   preflight=forbidden)
    assert (replay.user_id, replay.operation_id, replay.replayed) == (
        result.user_id, operation, True)
    assert _rows(db) == before
    with db.transaction() as cursor:
        cursor.execute("update users set password_hash=%s where id=%s",
                       (hash_password("replacement-password"), result.user_id))
    _failure("mismatch", lambda: _call(db, site.settings, operation=operation,
                                      username=username, preflight=forbidden))
    assert _call(db, site.settings, operation=operation, username=username,
                 password="replacement-password", preflight=forbidden).replayed is True


def test_replay_mismatch_conflict_and_disabled_user_never_repair(site):
    db = site.open_pool()
    operation, username = uuid4(), _username()
    result = _call(db, site.settings, operation=operation, username=username)
    before = deepcopy(_rows(db))
    cases = (
        ("mismatch", operation, username + "x", PASSWORD, site.settings),
        ("mismatch", operation, username, "wrong-password", site.settings),
        ("mismatch", operation, username, PASSWORD,
         replace(site.settings, s3_endpoint_url="http://127.0.0.1:59997")),
        ("mismatch", operation, username, PASSWORD,
         replace(site.settings, rag_collection="other_rag_" + uuid4().hex)),
        ("mismatch", operation, username, PASSWORD,
         replace(site.settings, episode_profile=replace(site.settings.episode_profile,
                                                        revision="changed"))),
        ("conflict", uuid4(), username, PASSWORD, site.settings),
    )
    for code, op, name, password, settings in cases:
        _failure(code, lambda op=op, name=name, password=password, settings=settings:
                 _call(db, settings, operation=op, username=name, password=password))
        assert _rows(db) == before
    with db.transaction() as cursor:
        cursor.execute("update users set status='disabled' where id=%s", (result.user_id,))
    _failure("mismatch", lambda: _call(db, site.settings, operation=operation, username=username))
    assert len(_rows(db)["users"]) == 1


@pytest.mark.parametrize("bad", ["not-a-uuid", "123e4567-e89b-42d3-a456-426614174000", None])
def test_invalid_operation_is_rejected_before_preflight_or_write(site, bad):
    db = site.open_pool()
    calls = []
    with pytest.raises(InitializationError) as raised:
        initialize_trusted_user(db, bad, _username(), PASSWORD, site.settings,
                                preflight=lambda: calls.append(1))
    assert raised.value.code == "invalid_request" and not calls
    _empty_site(db)


@pytest.mark.parametrize("username,password", [
    ("x", PASSWORD), ("init_invalid_password_case", "short"), ("bad name", PASSWORD),
])
def test_invalid_auth_input_is_rejected_before_preflight_or_write(site, username, password):
    db = site.open_pool()
    calls = []
    _failure("invalid_request", lambda: _call(
        db, site.settings, username=username, password=password,
        preflight=lambda: calls.append(1)))
    assert not calls
    _empty_site(db)


def test_exact_head17_gate_rejects_old_migration_head(site):
    db = site.open_pool()
    with db.transaction() as cursor:
        cursor.execute("update alembic_version set version_num='20261007_16'")
    try:
        _failure("configuration", lambda: _call(db, site.settings))
        _empty_site(db)
    finally:
        with db.transaction() as cursor:
            cursor.execute("update alembic_version set version_num='20261022_17'")


def test_preflight_failure_is_sanitized_without_account(site):
    db = site.open_pool()
    def failing():
        raise RuntimeError("secret S3 token and postgres://dsn")
    _failure("preflight_failed", lambda: _call(db, site.settings, preflight=failing))
    _empty_site(db)


def test_auth_prepared_cursor_insert_rolls_back_and_public_register_stays_compatible(site):
    from app.postgres_auth import _insert_new_user_in_transaction

    db = site.open_pool()
    user_id, username = str(uuid4()), _username()
    prepared_hash = hash_password(PASSWORD)
    with pytest.raises(RuntimeError, match="abort prepared insert"):
        with db.transaction() as cursor:
            created = _insert_new_user_in_transaction(
                cursor, user_id=user_id, username=username, username_key=username,
                password_hash=prepared_hash, now="2026-10-25T00:00:00+00:00",
            )
            assert created.id == user_id and created.username == username
            assert cursor.execute("select count(*) as n from users where id=%s",
                                  (user_id,)).fetchone()["n"] == 1
            raise RuntimeError("abort prepared insert")
    _empty_site(db)
    auth = PostgresAuthService(db)
    registered = auth.register("  Alice  ", PASSWORD)
    assert auth.authenticate("ＡＬＩＣＥ", PASSWORD) == registered
    with pytest.raises(AuthError, match="already exists"):
        auth.register("alice", PASSWORD)
    assert len(_rows(db)["users"]) == 1


def test_named_username_conflict_uses_one_extra_proof_generic_unique_does_not(site, monkeypatch):
    db = site.open_pool()
    username = _username()
    _call(db, site.settings, operation=uuid4(), username=username)
    original_proof = initialization.read_initialization_proof
    proofs = []

    def traced_proof(*args, **kwargs):
        proofs.append(1)
        return original_proof(*args, **kwargs)

    monkeypatch.setattr(initialization, "read_initialization_proof", traced_proof)
    _failure("conflict", lambda: _call(db, site.settings, operation=uuid4(), username=username))
    assert len(proofs) == 2  # initial absence, then one post-rollback named-conflict proof
    proofs.clear()

    def unnamed_unique(self, cursor, *args, **kwargs):
        raise psycopg.errors.UniqueViolation("raw unnamed unique")

    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction", unnamed_unique)
    _failure("failed", lambda: _call(db, site.settings, operation=uuid4()))
    assert len(proofs) == 1
    assert len(_rows(db)["users"]) == 1


def test_named_conflict_rollback_then_blocked_proof_is_unavailable(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    username = _username()
    winner_operation = uuid4()
    _call(db, site.settings, operation=winner_operation, username=username)
    original_proof = initialization.read_initialization_proof
    proofs = []
    blocker = None

    def block_second_proof(*args, **kwargs):
        nonlocal blocker
        proofs.append(1)
        if len(proofs) == 2:
            blocker = psycopg.connect(site.scoped_url)
            blocker.execute("set local lock_timeout='5s'")
            blocker.execute("lock table trusted_user_initializations in access exclusive mode")
        return original_proof(*args, **kwargs)

    monkeypatch.setattr(initialization, "read_initialization_proof", block_second_proof)
    try:
        started = monotonic()
        _failure("unavailable", lambda: _call(
            db, site.settings, operation=uuid4(), username=username))
        assert monotonic() - started < 20
        assert len(proofs) == 2
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
    rows = _rows(observer)
    assert len(rows["users"]) == len(rows["trusted_user_initializations"]) == 1
    assert rows["trusted_user_initializations"][0]["operation_id"] == winner_operation


def test_real_transaction_rollback_failure_obscures_sentinel(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    operation = uuid4()
    real_connection = db.connection
    armed = []
    replaced = []

    class RollbackFailsAfterRealRollback:
        def __init__(self, connection):
            self.connection = connection

        def cursor(self):
            return self.connection.cursor()

        def commit(self):
            return self.connection.commit()

        def rollback(self):
            self.connection.rollback()
            replaced.append(1)
            raise RuntimeError("raw rollback cleanup failure")

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def connection_with_one_bad_rollback():
        with real_connection() as connection:
            if armed and not replaced:
                yield RollbackFailsAfterRealRollback(connection)
            else:
                yield connection

    def body_lock(self, cursor, *args, **kwargs):
        raise psycopg.errors.LockNotAvailable("raw body lock")

    monkeypatch.setattr(db, "connection", connection_with_one_bad_rollback)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction", body_lock)
    error = _failure("unknown", lambda: _call(
        db, site.settings, operation=operation, preflight=lambda: armed.append(1)))
    assert isinstance(error, InitializationOutcomeUnknown) and error.operation_id == operation
    assert replaced == [1]
    _empty_site(observer)


def test_real_cursor_close_replacement_obscures_body_sentinel(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    operation = uuid4()
    real_connection = db.connection
    armed = []
    used = []
    closed = []

    class CloseFails:
        def __init__(self, cursor):
            self.raw = cursor

        def execute(self, statement, params=None):
            self.raw.execute(statement, params)
            return self

        def close(self):
            self.raw.close()
            closed.append(1)
            raise RuntimeError("raw cursor cleanup replacement")

        def __getattr__(self, name):
            return getattr(self.raw, name)

    class OneCursorCloseFails:
        def __init__(self, connection):
            self.raw = connection

        def cursor(self):
            return CloseFails(self.raw.cursor())

        def __getattr__(self, name):
            return getattr(self.raw, name)

    @contextmanager
    def connection_with_one_bad_cursor():
        with real_connection() as connection:
            if armed and not used:
                used.append(1)
                yield OneCursorCloseFails(connection)
            else:
                yield connection

    def body_lock(self, cursor, *args, **kwargs):
        raise psycopg.errors.LockNotAvailable("raw body lock")

    monkeypatch.setattr(db, "connection", connection_with_one_bad_cursor)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction", body_lock)
    error = _failure("unknown", lambda: _call(
        db, site.settings, operation=operation, preflight=lambda: armed.append(1)))
    assert isinstance(error, InitializationOutcomeUnknown) and error.operation_id == operation
    assert used == closed == [1]
    _empty_site(observer)


def test_every_body_fault_rolls_back_account_and_all_authority_rows(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    fault_points = (
        "user", "history", "memory", "stage_1", "stage_2", "seal_1", "seal_2",
        "publish_1", "publish_2", "witness", "ledger", "release",
    )

    class Marker(RuntimeError):
        pass

    for point in fault_points:
        with monkeypatch.context() as patch:
            if point == "user":
                cls, name, target = PostgresUserMutationCoordinator, "acquire_in_transaction", 1
            elif point in ("history", "memory"):
                cls, name, target = PostgresSnapshotRepository, "compare_and_swap_in_transaction", (
                    1 if point == "history" else 2)
            elif point.startswith("stage"):
                cls, name, target = PostgresVectorGenerationAuthority, "_stage_in_transaction", int(point[-1])
            elif point.startswith("seal"):
                cls, name, target = PostgresVectorGenerationAuthority, "_seal_in_transaction", int(point[-1])
            elif point.startswith("publish"):
                cls, name, target = PostgresVectorGenerationAuthority, "_publish", int(point[-1])
            elif point == "witness":
                cls, name, target = PostgresHistoryDocumentWitnessRepository, "insert", 1
            else:
                cls, name, target = PostgresUserMutationCoordinator, "release_in_transaction", 1
            original = getattr(cls, name)
            count = []

            def inject(*args, original=original, target=target, point=point, **kwargs):
                count.append(1)
                if point == "ledger" and len(count) == target:
                    assert args[1].execute(
                        "select count(*) as n from trusted_user_initializations"
                    ).fetchone()["n"] == 1
                    raise Marker("raw secret after ledger")
                result = original(*args, **kwargs)
                if len(count) == target:
                    if point == "release":
                        assert args[1].execute(
                            "select count(*) as n from trusted_user_initializations"
                        ).fetchone()["n"] == 1
                    raise Marker("raw secret in write body")
                return result

            patch.setattr(cls, name, staticmethod(inject) if point == "witness" else inject)
            _failure("failed", lambda: _call(db, site.settings))
            assert len(count) >= target, point
            _empty_site(observer)


def test_confirmed_rollback_sentinel_identity_is_per_call(site, monkeypatch):
    db = site.open_pool()
    original_transaction = db.transaction
    escaped = []
    armed = []

    @contextmanager
    def traced_transaction():
        body_error = None
        try:
            with original_transaction() as cursor:
                try:
                    yield cursor
                except BaseException as error:
                    body_error = error
                    raise
        except BaseException as error:
            if armed:
                assert error is body_error
                escaped.append(error)
            raise

    def lock_error(self, cursor, *args, **kwargs):
        raise psycopg.errors.LockNotAvailable("raw lock diagnostic")

    monkeypatch.setattr(db, "transaction", traced_transaction)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction", lock_error)
    for _ in range(2):
        armed.clear()
        _failure("busy", lambda: _call(db, site.settings, preflight=lambda: armed.append(1)))
        assert len(escaped) == _ + 1
        assert type(escaped[-1]) is not psycopg.errors.LockNotAvailable
        _empty_site(db)
    assert escaped[0] is not escaped[1]


def test_real_statement_timeout_is_confirmed_body_failure(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    original_proof = initialization.read_initialization_proof
    proofs = []

    def count_proof(*args, **kwargs):
        proofs.append(1)
        return original_proof(*args, **kwargs)

    def wait_past_local_statement_limit(self, cursor, *args, **kwargs):
        cursor.execute("select pg_sleep(11)")
        pytest.fail("the write transaction's 10-second statement limit did not fire")

    monkeypatch.setattr(initialization, "read_initialization_proof", count_proof)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction",
                        wait_past_local_statement_limit)
    _failure("timeout", lambda: _call(db, site.settings))
    assert len(proofs) == 1
    _empty_site(observer)


def test_lost_live_lease_is_expired_only_after_confirmed_rollback(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    original = PostgresUserMutationCoordinator.release_in_transaction
    original_proof = initialization.read_initialization_proof
    proofs = []

    def count_proof(*args, **kwargs):
        proofs.append(1)
        return original_proof(*args, **kwargs)

    def expired(self, cursor, lease, **kwargs):
        original(self, cursor, lease, **kwargs)
        raise MutationLeaseLost("raw lease diagnostic")

    monkeypatch.setattr(initialization, "read_initialization_proof", count_proof)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "release_in_transaction", expired)
    _failure("expired", lambda: _call(db, site.settings))
    assert len(proofs) == 1
    _empty_site(observer)


def test_real_db_clock_expiry_after_bounded_pg_lock_wait_rolls_back(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    original_release = PostgresUserMutationCoordinator.release_in_transaction
    original_proof = initialization.read_initialization_proof
    key = 74120983
    waiting = Event()
    worker_pid = []
    expiry = []
    proofs = []
    blocker = psycopg.connect(site.scoped_url, autocommit=True)
    blocker.execute("select pg_advisory_lock(%s)", (key,))

    def count_proof(*args, **kwargs):
        proofs.append(1)
        return original_proof(*args, **kwargs)

    def cross_expiry(self, cursor, lease, **kwargs):
        assert cursor.execute(
            "select lease_expires_at > clock_timestamp() as live "
            "from user_mutation_leases where user_id=%s", (lease.user_id,)
        ).fetchone()["live"] is True
        expiry.append(cursor.execute(
            "update user_mutation_leases set lease_expires_at="
            "clock_timestamp()+interval '3 seconds' where user_id=%s "
            "returning lease_expires_at",
            (lease.user_id,),
        ).fetchone()["lease_expires_at"])
        worker_pid.append(cursor.execute("select pg_backend_pid() as pid").fetchone()["pid"])
        waiting.set()
        cursor.execute("select pg_advisory_xact_lock(%s)", (key,))
        return original_release(self, cursor, lease, **kwargs)

    monkeypatch.setattr(initialization, "read_initialization_proof", count_proof)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "release_in_transaction", cross_expiry)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        result = pool.submit(lambda: _failure("expired", lambda: _call(db, site.settings)))
        assert waiting.wait(timeout=10)
        with psycopg.connect(site.scoped_url, autocommit=True) as observer_connection:
            deadline = monotonic() + 1.5
            row = None
            while monotonic() < deadline:
                row = observer_connection.execute(
                    "select wait_event_type from pg_stat_activity where pid=%s",
                    (worker_pid[0],),
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    break
                sleep(0.02)
            assert row is not None and row[0] == "Lock"
            assert len(expiry) == 1
            assert observer_connection.execute(
                "select clock_timestamp() < %s as live", (expiry[0],)
            ).fetchone()[0] is True
            deadline = monotonic() + 3.5
            expired = False
            while monotonic() < deadline:
                expired = observer_connection.execute(
                    "select clock_timestamp() > %s as expired", (expiry[0],)
                ).fetchone()[0]
                if expired:
                    break
                sleep(0.02)
            assert expired is True
        blocker.execute("select pg_advisory_unlock(%s)", (key,))
        result.result(timeout=15)
    finally:
        try:
            blocker.execute("select pg_advisory_unlock(%s)", (key,))
        finally:
            blocker.close()
            pool.shutdown(wait=True)
    assert len(proofs) == 1
    _empty_site(observer)


def test_real_body_lock_timeout_is_busy_without_postabort_winner_lookup(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    real_proof = initialization.read_initialization_proof
    blocker = None
    proofs = []

    def initial_proof_then_block(*args, **kwargs):
        nonlocal blocker
        value = real_proof(*args, **kwargs)
        proofs.append(1)
        if len(proofs) == 1:
            blocker = psycopg.connect(site.scoped_url)
            blocker.execute("lock table user_mutation_leases in access exclusive mode")
        return value

    monkeypatch.setattr(initialization, "read_initialization_proof", initial_proof_then_block)
    try:
        _failure("busy", lambda: _call(db, site.settings))
        assert len(proofs) == 1
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
    _empty_site(observer)


def test_cleanup_replacement_makes_body_outcome_unknown(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    operation = uuid4()
    original_transaction = db.transaction
    armed = []

    @contextmanager
    def cleanup_replaces_sentinel():
        try:
            with original_transaction() as cursor:
                yield cursor
        except BaseException:
            if armed:
                raise RuntimeError("raw cleanup replacement") from None
            raise

    def body_lock(self, cursor, *args, **kwargs):
        raise psycopg.errors.LockNotAvailable("raw lock diagnostic")

    monkeypatch.setattr(db, "transaction", cleanup_replaces_sentinel)
    monkeypatch.setattr(PostgresUserMutationCoordinator, "acquire_in_transaction", body_lock)
    error = _failure("unknown", lambda: _call(
        db, site.settings, operation=operation, preflight=lambda: armed.append(1)))
    assert isinstance(error, InitializationOutcomeUnknown) and error.operation_id == operation
    _empty_site(observer)


def test_commit_response_loss_proves_exact_durable_result(site, monkeypatch):
    db = site.open_pool()
    operation, username = uuid4(), _username()
    original_transaction = db.transaction
    armed = []
    replaced = []

    @contextmanager
    def lose_only_write_response():
        with original_transaction() as cursor:
            yield cursor
        if armed and not replaced:
            replaced.append(1)
            raise TimeoutError("raw response loss after actual commit")

    monkeypatch.setattr(db, "transaction", lose_only_write_response)
    result = _call(db, site.settings, operation=operation, username=username,
                   preflight=lambda: armed.append(1))
    assert len(replaced) == 1
    _assert_baseline(db, site.settings, result, operation, username)


def test_real_deferred_commit_rejection_leaves_no_partial_rows(site):
    db, observer = site.open_pool(), site.open_pool()
    with db.transaction() as cursor:
        cursor.execute("""create function reject_init_t3_commit() returns trigger
            language plpgsql as $$ begin
            raise exception 'deferred rejection' using errcode='23514';
            end $$""")
        cursor.execute("""create constraint trigger reject_init_t3_commit
            after insert on users deferrable initially deferred
            for each row execute function reject_init_t3_commit()""")
    operation = uuid4()
    error = _failure("unknown", lambda: _call(db, site.settings, operation=operation))
    assert isinstance(error, InitializationOutcomeUnknown) and error.operation_id == operation
    _empty_site(observer)


def test_same_operation_competitors_prove_one_winner(site):
    db, observer = site.open_pool(), site.open_pool()
    operation, username = uuid4(), _username()
    ready = Barrier(2)

    def contender():
        return _call(db, site.settings, operation=operation, username=username,
                     preflight=lambda: ready.wait(timeout=10))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(contender)
        second = pool.submit(contender)
        results = [first.result(timeout=30), second.result(timeout=30)]
    assert [item.replayed for item in results].count(False) == 1
    assert [item.replayed for item in results].count(True) == 1
    assert results[0].user_id == results[1].user_id
    assert len(_rows(observer)["users"]) == len(_rows(observer)["trusted_user_initializations"]) == 1


def test_two_operations_for_same_username_cannot_both_create(site):
    db = site.open_pool()
    username = _username()
    ready = Barrier(2)

    def contender(operation):
        try:
            return _call(db, site.settings, operation=operation, username=username,
                         preflight=lambda: ready.wait(timeout=10))
        except InitializationError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(contender, uuid4())
        second = pool.submit(contender, uuid4())
        outcomes = [first.result(timeout=30), second.result(timeout=30)]
    assert sum(isinstance(item, InitializedUser) for item in outcomes) == 1
    assert [item.code for item in outcomes if isinstance(item, InitializationError)] == ["conflict"]
    assert len(_rows(db)["users"]) == len(_rows(db)["trusted_user_initializations"]) == 1


def test_pool_identity_and_acquisition_bounds_fail_closed(site, monkeypatch):
    db = site.open_pool()
    parsed = make_url(site.scoped_url)
    mismatches = (
        replace(site.settings, database_url=parsed.set(port=(parsed.port or 5432) + 1)
                .render_as_string(hide_password=False)),
        replace(site.settings, database_url=parsed.set(database="wrong_init_t3_db")
                .render_as_string(hide_password=False)),
        replace(site.settings, database_url=parsed.set(host="mismatch.example.invalid")
                .render_as_string(hide_password=False)),
    )
    for settings in mismatches:
        _failure("configuration", lambda settings=settings: _call(db, settings))
        _empty_site(db)
    with monkeypatch.context() as patch:
        patch.setattr(db._pool, "timeout", float("inf"))
        _failure("configuration", lambda: _call(db, site.settings))
    _empty_site(db)

    bounded = site.open_pool(timeout=0.2)
    with ExitStack() as stack:
        for _ in range(4):
            stack.enter_context(bounded.connection())
        _failure("unavailable", lambda: _call(bounded, site.settings))
    _empty_site(db)


@pytest.mark.parametrize("phase", ["entry", "proof", "write"])
def test_borrowed_client_endpoint_mismatch_fails_before_sql(site, monkeypatch, phase):
    db, observer = site.open_pool(), site.open_pool()
    real_connection = db.connection
    real_schema = initialization.require_initialization_schema
    real_proof = initialization.read_initialization_proof
    bad = [phase == "entry"]
    wrong_sql = []
    schemas, proofs, preflights = [], [], []

    class WrongInfo:
        def __init__(self, info):
            self.raw = info

        @property
        def port(self):
            return int(self.raw.port) + 1

        def __getattr__(self, name):
            return getattr(self.raw, name)

    class BorrowedCursor:
        def __init__(self, raw, connection):
            self.raw = raw
            self.connection = connection

        def execute(self, statement, params=None):
            wrong_sql.append(1)
            self.raw.execute(statement, params)
            return self

        def __enter__(self):
            self.raw.__enter__()
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.raw, name)

    class BorrowedConnection:
        def __init__(self, raw):
            self.raw = raw
            self.info = WrongInfo(raw.info)

        def cursor(self, *args, **kwargs):
            return BorrowedCursor(self.raw.cursor(*args, **kwargs), self)

        def execute(self, statement, params=None):
            return self.cursor().execute(statement, params)

        def __getattr__(self, name):
            return getattr(self.raw, name)

    @contextmanager
    def borrowed_connection():
        with real_connection() as connection:
            yield BorrowedConnection(connection) if bad[0] else connection

    def schema_gate(*args, **kwargs):
        schemas.append(1)
        return real_schema(*args, **kwargs)

    def proof(*args, **kwargs):
        proofs.append(1)
        if phase == "proof":
            bad[0] = True
        try:
            return real_proof(*args, **kwargs)
        finally:
            if phase == "proof":
                bad[0] = False

    def preflight():
        preflights.append(1)
        if phase == "write":
            bad[0] = True

    monkeypatch.setattr(db, "connection", borrowed_connection)
    monkeypatch.setattr(initialization, "require_initialization_schema", schema_gate)
    monkeypatch.setattr(initialization, "read_initialization_proof", proof)
    try:
        _failure("configuration", lambda: _call(db, site.settings, preflight=preflight))
    finally:
        bad[0] = False
    assert not wrong_sql
    assert len(schemas) == (0 if phase == "entry" else 1)
    assert len(proofs) == (0 if phase == "entry" else 1)
    assert len(preflights) == (1 if phase == "write" else 0)
    _empty_site(observer)


def test_real_proof_first_select_sees_read_only_limits(site, monkeypatch):
    db = site.open_pool()
    real_connection = db.connection
    real_proof = initialization.read_initialization_proof
    in_proof = []
    observed = []

    class ProbeCursor:
        def __init__(self, raw, connection):
            self.raw = raw
            self.connection = connection

        def execute(self, statement, params=None):
            sql_text = (statement.as_string(self.raw.connection)
                        if hasattr(statement, "as_string") else str(statement))
            normalized = sql_text.lstrip().lower()
            if normalized.startswith("select") and "set_config(" not in normalized and not observed:
                with self.raw.connection.cursor() as inspection:
                    row = inspection.execute(
                        "select current_setting('transaction_isolation') as isolation, "
                        "current_setting('transaction_read_only') as read_only, "
                        "current_setting('lock_timeout') as lock_limit, "
                        "current_setting('statement_timeout') as statement_limit, "
                        "current_setting('idle_in_transaction_session_timeout') as idle_limit"
                    ).fetchone()
                observed.append(tuple(row[key] for key in (
                    "isolation", "read_only", "lock_limit", "statement_limit", "idle_limit")))
            self.raw.execute(statement, params)
            return self

        def __enter__(self):
            self.raw.__enter__()
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.raw, name)

    class ProbeConnection:
        def __init__(self, raw):
            self.raw = raw

        def cursor(self, *args, **kwargs):
            return ProbeCursor(self.raw.cursor(*args, **kwargs), self)

        def execute(self, statement, params=None):
            return self.cursor().execute(statement, params)

        def __getattr__(self, name):
            return getattr(self.raw, name)

    @contextmanager
    def observed_connection():
        with real_connection() as connection:
            yield ProbeConnection(connection) if in_proof else connection

    def proof(*args, **kwargs):
        in_proof.append(1)
        try:
            return real_proof(*args, **kwargs)
        finally:
            in_proof.pop()

    monkeypatch.setattr(db, "connection", observed_connection)
    monkeypatch.setattr(initialization, "read_initialization_proof", proof)
    result = _call(db, site.settings)
    assert result.durable_initialized is True
    assert observed == [("repeatable read", "on", "5s", "10s", "15s")]


def test_task01_equivalent_loopback_hosts_are_accepted(site):
    parsed = make_url(site.scoped_url)
    assert parsed.host in {"localhost", "127.0.0.1", "::1"}
    alternate = "localhost" if parsed.host != "localhost" else "127.0.0.1"
    settings = replace(site.settings, database_url=parsed.set(host=alternate)
                       .render_as_string(hide_password=False))
    db = site.open_pool()
    result = _call(db, settings)
    assert result.durable_initialized is True


def test_real_initial_proof_lock_timeout_never_reaches_preflight_or_write(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    calls = []
    blocker = None
    real_schema_gate = initialization.require_initialization_schema

    def schema_gate_then_block(*args, **kwargs):
        nonlocal blocker
        real_schema_gate(*args, **kwargs)
        blocker = psycopg.connect(site.scoped_url)
        blocker.execute("lock table trusted_user_initializations in access exclusive mode")

    monkeypatch.setattr(initialization, "require_initialization_schema", schema_gate_then_block)
    try:
        started = monotonic()
        _failure("unavailable", lambda: _call(
            db, site.settings, preflight=lambda: calls.append(1)))
        assert monotonic() - started < 20
        assert not calls
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
    _empty_site(observer)


def test_ambiguous_exit_then_blocked_proof_is_unknown_without_second_write(site, monkeypatch):
    db, observer = site.open_pool(), site.open_pool()
    original_transaction = db.transaction
    operation = uuid4()
    armed = []
    write_exits = []
    blocker = None

    @contextmanager
    def lose_response_and_block_proof():
        nonlocal blocker
        with original_transaction() as cursor:
            yield cursor
        if armed and not write_exits:
            write_exits.append(1)
            blocker = psycopg.connect(site.scoped_url)
            blocker.execute("lock table trusted_user_initializations in access exclusive mode")
            raise TimeoutError("raw response lost after commit")

    monkeypatch.setattr(db, "transaction", lose_response_and_block_proof)
    try:
        error = _failure("unknown", lambda: _call(
            db, site.settings, operation=operation, preflight=lambda: armed.append(1)))
        assert isinstance(error, InitializationOutcomeUnknown)
        assert error.operation_id == operation and len(write_exits) == 1
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
    rows = _rows(observer)
    assert len(rows["users"]) == len(rows["trusted_user_initializations"]) == 1
    assert rows["trusted_user_initializations"][0]["operation_id"] == operation


@pytest.mark.parametrize("failure_mode", ["blocked", "disabled"])
def test_confirmed_commit_then_unreadable_proof_is_unavailable(site, monkeypatch, failure_mode):
    db, observer = site.open_pool(), site.open_pool()
    original_transaction = db.transaction
    operation = uuid4()
    armed = []
    after_commit = []
    blocker = None

    @contextmanager
    def alter_only_postcommit_proof():
        nonlocal blocker
        with original_transaction() as cursor:
            yield cursor
        if armed and not after_commit:
            after_commit.append(1)
            if failure_mode == "blocked":
                blocker = psycopg.connect(site.scoped_url)
                blocker.execute("lock table trusted_user_initializations in access exclusive mode")
            else:
                with original_transaction() as cursor:
                    changed = cursor.execute(
                        "update users set status='disabled' where id=("
                        "select user_id from trusted_user_initializations where operation_id=%s) "
                        "returning id", (operation,),
                    ).fetchone()
                    assert changed is not None

    monkeypatch.setattr(db, "transaction", alter_only_postcommit_proof)
    try:
        _failure("unavailable", lambda: _call(
            db, site.settings, operation=operation, preflight=lambda: armed.append(1)))
        assert armed == [1] and after_commit == [1]
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
    rows = _rows(observer)
    assert len(rows["users"]) == len(rows["trusted_user_initializations"]) == 1
    assert rows["trusted_user_initializations"][0]["operation_id"] == operation
    assert rows["users"][0]["status"] == ("disabled" if failure_mode == "disabled" else "active")


def test_original_receipt_corruption_is_not_repaired(site):
    db = site.open_pool()
    operation, username = uuid4(), _username()
    result = _call(db, site.settings, operation=operation, username=username)
    with db.transaction() as cursor:
        # Damage only this disposable schema. Published receipt mutation is
        # normally rejected by vector_generation_guard.
        cursor.execute("drop trigger vector_generation_guard on vector_generations")
        original = cursor.execute(
            "select rag_initial_generation_id from trusted_user_initializations where operation_id=%s",
            (operation,),
        ).fetchone()["rag_initial_generation_id"]
        cursor.execute("update vector_generations set content_digest=%s where generation_id=%s",
                       ("0" * 64, original))
    before = deepcopy(_rows(db))
    _failure("corrupt", lambda: _call(db, site.settings, operation=operation, username=username))
    assert _rows(db) == before and len(before["users"]) == 1
    assert result.user_id == before["users"][0]["id"]


def test_missing_original_witness_fails_historical_proof_without_repair(site, monkeypatch):
    db = site.open_pool()
    operation, username = uuid4(), _username()
    _call(db, site.settings, operation=operation, username=username)
    # The real FK and schema gate deliberately prevent this damaged state.
    # This one test bypasses only that gate to exercise proof's independent refusal.
    with db.transaction() as cursor:
        cursor.execute("alter table trusted_user_initializations "
                       "drop constraint trusted_init_rag_witness_fk_17")
        cursor.execute("drop trigger history_document_witness_guard on history_document_witnesses")
        cursor.execute("delete from history_document_witnesses")
    monkeypatch.setattr(initialization, "require_initialization_schema", lambda *args: None)
    before = deepcopy(_rows(db))
    _failure("corrupt", lambda: _call(db, site.settings, operation=operation, username=username))
    assert _rows(db) == before


def test_real_import_advances_same_initialized_user_then_pg_only_replays(site):
    db = site.open_pool()
    operation, username = uuid4(), _username()
    initialized = _call(db, site.settings, operation=operation, username=username)
    env = os.environ
    if not env.get("S3_TEST_ACCESS_KEY") or not env.get("S3_TEST_SECRET_KEY"):
        pytest.fail("Dedicated versioned S3 test credentials are required")
    client = boto3.client(
        "s3", endpoint_url=site.settings.s3_endpoint_url, region_name="us-east-1",
        aws_access_key_id=env["S3_TEST_ACCESS_KEY"],
        aws_secret_access_key=env["S3_TEST_SECRET_KEY"],
        config=BotoConfig(s3={"addressing_style": "path"}),
    )
    bucket = site.settings.s3_bucket
    raw = QdrantVectorStore(url=site.settings.qdrant_url, retry_delays=())
    identities = (site.settings.rag_identity, site.settings.episode_identity)
    created_collections = []
    bucket_created = False
    try:
        client.create_bucket(Bucket=bucket)
        bucket_created = True
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        store = S3ObjectStore(client, bucket)
        store.check_ready()
        for identity in identities:
            raw.ensure_collection(identity.physical_collection, 4)
            created_collections.append(identity.physical_collection)
        authority = PostgresVectorGenerationAuthority(db)
        rag = VectorGenerationService(authority, raw)
        episode = VectorGenerationService(authority, raw)
        rag_scope = VectorScope(initialized.user_id, "rag", "pdf_" + initialized.user_id,
                                site.settings.rag_identity)
        episode_scope = VectorScope(initialized.user_id, "episode", "episodes",
                                    site.settings.episode_identity)
        service = ImportMemoryPublicationService(
            db, store, rag, episode, trusted_episode_scope=episode_scope)
        attempt = _task(db, store, initialized.user_id, name="new-user.txt")
        published = service.publish(
            rag_scope, attempt, [_point(rag_scope, attempt.task.document_id, "new corpus")],
            event_vector=[1., 0., 0., 0.], event_profile=site.settings.episode_profile)
        assert published.history_version == published.memory_version == 2
        snapshots = PostgresSnapshotRepository(db)
        assert len(snapshots.read(initialized.user_id, "history").data["documents"]) == 1
        assert len(snapshots.read(initialized.user_id, "memory").data["memories"]) == 1
        with db.transaction() as cursor:
            ledger = cursor.execute(
                "select rag_initial_generation_id,episode_initial_generation_id "
                "from trusted_user_initializations where operation_id=%s", (operation,)
            ).fetchone()
            retired = cursor.execute(
                "select state from vector_generations where generation_id=any(%s)",
                ([ledger["rag_initial_generation_id"], ledger["episode_initial_generation_id"]],)
            ).fetchall()
        assert sorted(row["state"] for row in retired) == ["retired", "retired"]
        before = deepcopy(_rows(db))
        replay = _call(db, site.settings, operation=operation, username=username,
                       preflight=lambda: pytest.fail("replay reached S3/Q preflight"))
        assert replay.replayed is True and replay.user_id == initialized.user_id
        assert _rows(db) == before
    finally:
        cleanup_errors = []
        for collection in reversed(created_collections):
            try:
                raw.client.delete_collection(collection)
            except Exception as error:
                cleanup_errors.append(error)
        if bucket_created:
            try:
                pages = client.get_paginator("list_object_versions").paginate(Bucket=bucket)
                for page in pages:
                    versions = page.get("Versions", []) + page.get("DeleteMarkers", [])
                    if versions:
                        try:
                            client.delete_objects(Bucket=bucket, Delete={"Objects": [
                                {"Key": item["Key"], "VersionId": item["VersionId"]} for item in versions
                            ]})
                        except Exception as error:
                            cleanup_errors.append(error)
            except Exception as error:
                cleanup_errors.append(error)
            try:
                client.delete_bucket(Bucket=bucket)
            except Exception as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            raise cleanup_errors[0]


