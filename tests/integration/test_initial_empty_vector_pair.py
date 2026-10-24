"""Caller-owned initial vector pair and existing live-wrapper contracts in disposable PG."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import os
from pathlib import Path
from queue import Queue
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from app.history import EMPTY_HISTORY
from app.import_models import ImportTaskCreate
from app.import_publication_evidence import (
    PublicationEvidenceError, UnresolvedPublicationGate,
)
from app.import_repository import PostgresImportTaskRepository
from app.postgres import PostgresDatabase
from app.postgres_coordination import (
    MutationLeaseLost, PostgresUserMutationCoordinator,
)
from app.postgres_import_leases import PostgresImportLeaseRepository
from app.postgres_snapshots import PostgresSnapshotRepository
from app.postgres_vector_generations import (
    PostgresVectorGenerationAuthority, VectorAuthorityError, VectorScope,
)
from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
from hello_agents.memory.rag.index_identity import IndexIdentity


ROOT = Path(__file__).resolve().parents[2]
EMPTY_DIGEST = hashlib.sha256(b"[]").hexdigest()


@pytest.fixture
def databases(monkeypatch):
    base = os.environ.get("POSTGRES_TEST_URL")
    if not base:
        pytest.fail("POSTGRES_TEST_URL is required for Task02 PG evidence")
    schema = "init_t2_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    url = make_url(base).set(drivername="postgresql+psycopg",
                             query={"options": f"-csearch_path={schema}"})
    url = url.render_as_string(hide_password=False)
    pools = []

    def open_pool():
        db = PostgresDatabase(url.replace("postgresql+psycopg://", "postgresql://"),
                              min_size=1, max_size=3)
        db.open()
        pools.append(db)
        return db

    try:
        monkeypatch.setenv("DATABASE_URL", url)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "20261022_17")
        yield open_pool
    finally:
        for db in pools:
            db.close()
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


def _scopes(user):
    rag = IndexIdentity("qdrant", "init_t2_rag_" + uuid4().hex,
                        EmbeddingProfile("simple", "", "SimpleEmbedding", "v1", 4))
    episode = IndexIdentity("qdrant", "init_t2_episode_" + uuid4().hex,
                            EmbeddingProfile("test-explicit", "", "episode", "v1", 4))
    return (VectorScope(user, "rag", "pdf_" + user, rag),
            VectorScope(user, "episode", "episodes", episode))


def _insert_user(cursor, user):
    username = "init_t2_" + uuid4().hex
    cursor.execute(
        "insert into users(id,username,username_key,password_hash,status,created_at,updated_at) "
        "values(%s,%s,%s,'test-hash','active',clock_timestamp(),clock_timestamp())",
        (user, username, username),
    )


def _snapshots(cursor, db, user, *, missing=None, nonempty=None, version_two=None):
    repository = PostgresSnapshotRepository(db)
    history = deepcopy(EMPTY_HISTORY)
    memory = {"user_id": user, "memories": []}
    if nonempty == "history":
        history["notes"].append({"content": "preexisting"})
    if nonempty == "memory":
        memory["memories"].append({"metadata": {"user_id": user}})
    for kind, payload in (("history", history), ("memory", memory)):
        if kind == missing:
            continue
        assert repository.compare_and_swap_in_transaction(
            cursor, user, kind, payload, expected_version=0).version == 1
        if kind == version_two:
            assert repository.compare_and_swap_in_transaction(
                cursor, user, kind, payload, expected_version=1).version == 2


def _provisional(cursor, db, *, missing=None, nonempty=None, version_two=None):
    user = str(uuid4())
    _insert_user(cursor, user)
    _snapshots(cursor, db, user, missing=missing, nonempty=nonempty,
               version_two=version_two)
    lease = PostgresUserMutationCoordinator(db).acquire_in_transaction(
        cursor, user, "init-t2", lease_seconds=60)
    assert lease is not None
    return user, lease, _scopes(user)


def _pair_method(authority):
    # On the RED source this is the actual missing call target. Keep this lookup
    # outside refusal/instrumentation contexts so AttributeError is not masked.
    return authority._publish_initial_empty_pair_in_transaction


def _rows(db, user):
    with db.transaction() as cursor:
        return {
            table: cursor.execute(
                f"select * from {table} where {column}=%s",
                (user,),
            ).fetchall()
            for table, column in (
                ("vector_indexes", "tenant_id"),
                ("vector_heads", "tenant_id"),
                ("vector_generations", "tenant_id"),
            )
        }


def _assert_no_vectors(db, user):
    assert all(not rows for rows in _rows(db, user).values())


class _RecordingCursor:
    def __init__(self, raw):
        self.raw = raw
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(" ".join(statement.lower().split()))
        self.raw.execute(statement, params)
        return self

    def __getattr__(self, name):
        return getattr(self.raw, name)


def test_pair_is_invisible_until_commit_and_has_exact_initial_receipts(databases, monkeypatch):
    first, observer = databases(), databases()
    authority = PostgresVectorGenerationAuthority(first)
    pair = _pair_method(authority)
    with first.connection() as connection:
        with connection.cursor() as raw:
            raw.execute("begin")
            user, lease, scopes = _provisional(raw, first)
            cursor = _RecordingCursor(raw)

            def nested_transaction():
                raise AssertionError("pair opened an authority transaction")

            with monkeypatch.context() as patch:
                patch.setattr(first, "transaction", nested_transaction)
                rag_id, episode_id = pair(cursor, lease, *scopes)
            assert isinstance(rag_id, UUID) and isinstance(episode_id, UUID)
            assert rag_id != episode_id
            _assert_no_vectors(observer, user)
            lifecycle = [
                "staging" if "insert into vector_generations" in statement else
                "sealed" if "update vector_generations set state='sealed'" in statement else
                "published" if "update vector_generations set state='published'" in statement else
                None
                for statement in cursor.statements
            ]
            assert [state for state in lifecycle if state] == [
                "staging", "staging", "sealed", "sealed", "published", "published"]
            connection.commit()

    rows = _rows(observer, user)
    assert {name: len(found) for name, found in rows.items()} == {
        "vector_indexes": 2, "vector_heads": 2, "vector_generations": 2}
    for scope, generation_id in zip(scopes, (rag_id, episode_id)):
        index = next(row for row in rows["vector_indexes"] if
                     (row["tenant_id"], row["vector_kind"], row["namespace"],
                      row["index_key"]) == scope.key)
        head = next(row for row in rows["vector_heads"] if
                    (row["tenant_id"], row["vector_kind"], row["namespace"],
                     row["index_key"]) == scope.key)
        receipt = next(row for row in rows["vector_generations"] if
                       row["generation_id"] == generation_id)
        assert (index["identity"], index["index_revision"]) == (scope.identity.to_dict(), 1)
        assert (head["revision"], head["generation_id"], head["last_generation_id"],
                head["index_revision"], head["snapshot_version"]) == (
                    1, None, generation_id, 1, 1)
        assert (receipt["tenant_id"], receipt["vector_kind"], receipt["namespace"],
                receipt["index_key"]) == scope.key
        assert (receipt["base_revision"], receipt["index_revision"],
                receipt["owner"], receipt["user_lease_token"],
                receipt["user_lease_version"]) == (
                    None, 1, lease.owner, lease.lease_token, lease.lease_version)
        assert (receipt["task_id"], receipt["task_lease_token"],
                receipt["task_lease_version"]) == (None, None, None)
        assert (receipt["state"], receipt["expected_count"], receipt["content_digest"],
                receipt["publication_revision"],
                receipt["publication_snapshot_version"]) == (
                    "published", 0, EMPTY_DIGEST, 1, 1)


def test_failure_between_publications_rolls_back_both_scopes(databases, monkeypatch):
    first, observer = databases(), databases()
    authority = PostgresVectorGenerationAuthority(first)
    pair = _pair_method(authority)
    original = authority._publish
    publications = []

    def fail_after_first(cursor, *args, **kwargs):
        revision = original(cursor, *args, **kwargs)
        publications.append((cursor, args[0], revision))
        raise RuntimeError("after first publication")

    with first.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(cursor, first)
            with monkeypatch.context() as patch:
                patch.setattr(authority, "_publish", fail_after_first)
                with pytest.raises(RuntimeError, match="after first publication"):
                    pair(cursor, lease, *scopes)
            assert len(publications) == 1
            assert publications[0] == (cursor, scopes[0], 1)
            connection.rollback()
    _assert_no_vectors(observer, user)
    with observer.transaction() as cursor:
        assert cursor.execute("select 1 from users where id=%s", (user,)).fetchone() is None


@pytest.mark.parametrize("invalid", [
    "lease_type", "rag_tenant", "episode_tenant", "rag_kind", "episode_kind",
    "rag_namespace", "episode_namespace", "same_base_collection", "same_identity",
])
def test_pair_rejects_wrong_lease_scope_or_aliased_identity(databases, invalid):
    db, observer = databases(), databases()
    pair = _pair_method(PostgresVectorGenerationAuthority(db))
    with db.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(cursor, db)
            rag, episode = scopes
            if invalid == "lease_type":
                lease = object()
            elif invalid == "rag_tenant":
                rag = replace(rag, tenant_id=str(uuid4()))
            elif invalid == "episode_tenant":
                episode = replace(episode, tenant_id=str(uuid4()))
            elif invalid == "rag_kind":
                rag = replace(rag, vector_kind="episode")
            elif invalid == "episode_kind":
                episode = replace(episode, vector_kind="rag")
            elif invalid == "rag_namespace":
                rag = replace(rag, namespace="documents")
            elif invalid == "episode_namespace":
                episode = replace(episode, namespace="pdf_" + user)
            elif invalid == "same_base_collection":
                episode = replace(episode, identity=IndexIdentity(
                    "qdrant", rag.identity.base_collection, episode.identity.profile))
            else:
                episode = replace(episode, identity=rag.identity)
            with pytest.raises((VectorAuthorityError, MutationLeaseLost, ValueError)):
                pair(cursor, lease, rag, episode)
            connection.rollback()
    _assert_no_vectors(observer, user)


@pytest.mark.parametrize("invalid", [
    "missing_history", "missing_memory", "version_two_history",
    "version_two_memory", "nonempty_history", "nonempty_memory",
    "history_extra_key", "memory_extra_key", "memory_wrong_user_id",
])
def test_pair_requires_two_exact_empty_version_one_snapshots(databases, invalid):
    db, observer = databases(), databases()
    pair = _pair_method(PostgresVectorGenerationAuthority(db))
    if invalid.startswith("version_two_"):
        dimension, kind = "version_two", invalid.removeprefix("version_two_")
    elif invalid.startswith(("missing_", "nonempty_")):
        dimension, kind = invalid.split("_", 1)
    else:
        dimension, kind = None, None
    with db.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(
                cursor, db,
                missing=kind if dimension == "missing" else None,
                nonempty=kind if dimension == "nonempty" else None,
                version_two=kind if dimension == "version_two" else None)
            if invalid in {"history_extra_key", "memory_extra_key", "memory_wrong_user_id"}:
                snapshot_kind = "history" if invalid == "history_extra_key" else "memory"
                row = cursor.execute(
                    "select version,payload from user_snapshots where user_id=%s and kind=%s",
                    (user, snapshot_kind),
                ).fetchone()
                assert row["version"] == 1
                payload = deepcopy(row["payload"])
                if invalid == "memory_wrong_user_id":
                    payload["user_id"] = str(uuid4())
                else:
                    payload["unexpected"] = True
                cursor.execute(
                    "update user_snapshots set payload=%s where user_id=%s and kind=%s and version=1",
                    (Jsonb(payload), user, snapshot_kind),
                )
                assert cursor.rowcount == 1
                changed = cursor.execute(
                    "select version,payload from user_snapshots where user_id=%s and kind=%s",
                    (user, snapshot_kind),
                ).fetchone()
                assert (changed["version"], changed["payload"]) == (1, payload)
            with pytest.raises((VectorAuthorityError, ValueError)):
                pair(cursor, lease, *scopes)
            connection.rollback()
    _assert_no_vectors(observer, user)


@pytest.mark.parametrize("preexisting", [
    "rag_exact_index", "episode_other_key_index", "other_namespace_index",
    "other_kind_head",
])
def test_pair_rejects_any_tenant_index_or_head(databases, preexisting):
    db, observer = databases(), databases()
    authority = PostgresVectorGenerationAuthority(db)
    pair = _pair_method(authority)
    if preexisting == "other_kind_head":
        # A committed existing user is necessary to create a legal head with
        # the production stage/seal/publish transition before this refusal.
        user = str(uuid4())
        with db.transaction() as cursor:
            _insert_user(cursor, user)
            _snapshots(cursor, db, user)
        lease = PostgresUserMutationCoordinator(db).acquire(user, "init-t2")
        assert lease is not None
        scopes = _scopes(user)
        other = replace(scopes[1], namespace="other-episode")
        generation = authority.stage(other, lease, expected_revision=None)
        authority.seal(other, lease, generation, expected_count=0,
                       content_digest=EMPTY_DIGEST)
        assert authority.publish_user(other, lease, generation,
                                      expected_revision=None, snapshot_version=1) == 1
        before = _rows(observer, user)
        with db.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("begin")
                with pytest.raises(VectorAuthorityError):
                    pair(cursor, lease, *scopes)
                connection.rollback()
        assert _rows(observer, user) == before
        return

    with db.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(cursor, db)
            scope = scopes[0] if preexisting != "episode_other_key_index" else scopes[1]
            if preexisting == "episode_other_key_index":
                scope = replace(scope, identity=IndexIdentity(
                    "qdrant", "other_index_" + uuid4().hex, scope.identity.profile))
            if preexisting == "other_namespace_index":
                scope = replace(scope, namespace="other-rag")
            cursor.execute("insert into vector_indexes "
                           "(tenant_id,vector_kind,namespace,index_key,identity) "
                           "values(%s,%s,%s,%s,%s)",
                           (*scope.key, Jsonb(scope.identity.to_dict())))
            with pytest.raises(VectorAuthorityError):
                pair(cursor, lease, *scopes)
            connection.rollback()
    _assert_no_vectors(observer, user)


@pytest.mark.parametrize("stale", ["owner", "token", "version"])
def test_pair_rejects_stale_lease_without_head(databases, stale):
    db, observer = databases(), databases()
    pair = _pair_method(PostgresVectorGenerationAuthority(db))
    with db.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(cursor, db)
            if stale == "owner":
                handle = replace(lease, owner="not-owner")
            elif stale == "token":
                handle = replace(lease, lease_token=uuid4())
            else:
                handle = replace(lease, lease_version=lease.lease_version + 1)
            with pytest.raises(MutationLeaseLost):
                pair(cursor, handle, *scopes)
            connection.rollback()
    _assert_no_vectors(observer, user)


def test_pair_rejects_user_deactivated_after_real_lease(databases):
    db, observer = databases(), databases()
    pair = _pair_method(PostgresVectorGenerationAuthority(db))
    with db.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(cursor, db)
            cursor.execute("update users set status='disabled' where id=%s", (user,))
            with pytest.raises(MutationLeaseLost):
                pair(cursor, lease, *scopes)
            connection.rollback()
    _assert_no_vectors(observer, user)


def test_pair_rejects_unresolved_publication_gate(databases):
    db, observer = databases(), databases()
    pair = _pair_method(PostgresVectorGenerationAuthority(db))
    with db.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("begin")
            user, lease, scopes = _provisional(cursor, db)
            task_id, batch_id, document_id = str(uuid4()), str(uuid4()), str(uuid4())
            now = cursor.execute("select clock_timestamp() as now").fetchone()["now"].isoformat()
            PostgresImportTaskRepository(db).create_batch_in_transaction(
                cursor, user, [ImportTaskCreate(task_id, batch_id, user,
                                                document_id, "a.txt", ".txt", 1, "")],
                now=now)
            payload = b"{}"
            cursor.execute("""insert into import_publication_evidence
                (user_id,task_id,task_lease_version,document_id,worker_id,
                 task_lease_token,user_lease_token,user_lease_version,schema_version,
                 intent_format,intent_hash)
                values(%s,%s,1,%s,%s,%s,%s,%s,1,'canonical-json-zlib-1',%s)""",
                (user, task_id, document_id, lease.owner, uuid4(),
                 lease.lease_token, lease.lease_version,
                 hashlib.sha256(payload).hexdigest()))
            cursor.execute("insert into user_publication_gates "
                           "(user_id,task_id,task_lease_version) values(%s,%s,1)",
                           (user, task_id))
            with pytest.raises(UnresolvedPublicationGate):
                pair(cursor, lease, *scopes)
            connection.rollback()
    _assert_no_vectors(observer, user)


def test_pair_rechecks_real_lease_after_user_lock_wait(databases):
    db, blocker_db, observer = databases(), databases(), databases()
    pair = _pair_method(PostgresVectorGenerationAuthority(db))
    user = str(uuid4())
    with db.transaction() as cursor:
        _insert_user(cursor, user)
        _snapshots(cursor, db, user)
    lease = PostgresUserMutationCoordinator(db).acquire(
        user, "init-t2", lease_seconds=5)
    assert lease is not None
    scopes = _scopes(user)
    pid_box = Queue(maxsize=1)

    def waiting_call():
        with db.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("begin")
                pid_box.put(cursor.execute("select pg_backend_pid() as pid").fetchone()["pid"])
                try:
                    pair(cursor, lease, *scopes)
                except Exception as exc:
                    connection.rollback()
                    return exc
                connection.rollback()
                return None

    with blocker_db.connection() as lock_connection:
        with lock_connection.cursor() as lock_cursor:
            lock_cursor.execute("begin")
            lock_cursor.execute("select id from users where id=%s for update", (user,))
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    future = pool.submit(waiting_call)
                    pid = pid_box.get(timeout=5)
                    deadline = time.monotonic() + 10
                    observed_wait = False
                    while time.monotonic() < deadline:
                        with observer.transaction() as cursor:
                            row = cursor.execute(
                                "select wait_event_type from pg_stat_activity where pid=%s",
                                (pid,),
                            ).fetchone()
                        if row and row["wait_event_type"] == "Lock":
                            observed_wait = True
                            break
                        time.sleep(0.025)
                    assert observed_wait and not future.done()
                    with observer.transaction() as cursor:
                        still_live = cursor.execute(
                            "select clock_timestamp() < %s as still_live",
                            (lease.expires_at,),
                        ).fetchone()["still_live"]
                    assert still_live
                    expiry_deadline = time.monotonic() + 10
                    naturally_expired = False
                    while time.monotonic() < expiry_deadline:
                        with observer.transaction() as cursor:
                            expired = cursor.execute(
                                "select clock_timestamp() > %s as expired",
                                (lease.expires_at,),
                            ).fetchone()["expired"]
                        if expired:
                            naturally_expired = True
                            break
                        time.sleep(0.025)
                    assert naturally_expired
                finally:
                    # Release even if wait observation or PID collection fails;
                    # otherwise ThreadPoolExecutor would join a blocked worker.
                    lock_connection.rollback()
                result = future.result(timeout=5)
    assert isinstance(result, MutationLeaseLost)
    _assert_no_vectors(observer, user)


def _real_import_attempt(db, user):
    task_id, batch_id = str(uuid4()), str(uuid4())
    digest = hashlib.sha256(b"import-object").hexdigest()
    with db.transaction() as cursor:
        now = cursor.execute("select clock_timestamp() as now").fetchone()["now"].isoformat()
        PostgresImportTaskRepository(db).create_batch_in_transaction(
            cursor, user,
            [ImportTaskCreate(task_id, batch_id, user, str(uuid4()),
                              "a.txt", ".txt", 1, "")], now=now)
        cursor.execute("""insert into import_objects
            (task_id,user_id,bucket,object_key,version_id,sha256,size_bytes)
            values(%s,%s,'test','object','v1',%s,1)""", (task_id, user, digest))
    imports = PostgresImportLeaseRepository(db)
    attempt = imports.claim_next("init-t2-worker", lease_seconds=60)
    assert attempt is not None and imports.try_begin_committing(attempt)
    return attempt


@pytest.mark.parametrize("reject_at", [None, "stage_before", "stage_after", "seal"])
def test_existing_live_stage_seal_wrapper_keeps_evidence_and_rollback(
        databases, monkeypatch, reject_at):
    db = databases()
    user = str(uuid4())
    with db.transaction() as cursor:
        _insert_user(cursor, user)
    attempt = _real_import_attempt(db, user)
    scope = _scopes(user)[0]
    authority = PostgresVectorGenerationAuthority(db)
    generation = uuid4()
    live = object()
    issue = SimpleNamespace(key=("controlled-live",))
    evidence = SimpleNamespace(phase="document_verified", phase_version=7)
    calls = []
    reads = []

    def issuer(received):
        assert received is live
        return None, issue

    def read_exact(self, cursor, key):
        assert key is issue.key
        reads.append(cursor)
        return evidence

    def require(self, cursor, received, kind, received_scope, received_generation,
                *, operation, expected_phase, expected_version):
        calls.append((received, cursor, operation, kind, received_scope,
                      received_generation, expected_phase, expected_version))
        if operation == "stage_after":
            row = cursor.execute(
                "select state from vector_generations where generation_id=%s",
                (generation,),
            ).fetchone()
            assert row == {"state": "staging"}
        if operation == reject_at:
            raise PublicationEvidenceError("controlled " + reject_at)

    import app.import_memory_publication as memory_publication
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    monkeypatch.setattr(memory_publication, "_require_live_issuer", issuer)
    monkeypatch.setattr(PostgresImportPublicationEvidenceRepository,
                        "_read_exact_in_cursor", read_exact)
    monkeypatch.setattr(PostgresImportPublicationEvidenceRepository,
                        "require_candidate_in_transaction", require)

    if reject_at in ("stage_before", "stage_after"):
        with pytest.raises(PublicationEvidenceError, match="controlled"):
            authority.stage(scope, attempt, expected_revision=None,
                            generation_id=generation, live=live)
        _assert_no_vectors(db, user)
        assert [call[2] for call in calls] == (
            ["stage_before"] if reject_at == "stage_before" else
            ["stage_before", "stage_after"])
        assert all(call[0] is live and call[3] == "rag" and call[4] == scope
                   and call[5] == generation and call[6:] == (evidence.phase,
                                                                evidence.phase_version)
                   for call in calls)
        assert reads == [calls[0][1]]
        return

    assert authority.stage(scope, attempt, expected_revision=None,
                           generation_id=generation, live=live) == generation
    with db.transaction() as cursor:
        staged = cursor.execute("select * from vector_generations where generation_id=%s",
                                (generation,)).fetchone()
    assert staged["state"] == "staging"
    assert (staged["owner"], staged["user_lease_token"],
            staged["user_lease_version"], staged["task_id"],
            staged["task_lease_token"], staged["task_lease_version"]) == (
                attempt.worker_id, attempt.user_lease.lease_token,
                attempt.user_lease.lease_version, attempt.task.task_id,
                attempt.lease_token, attempt.lease_version)
    if reject_at == "seal":
        with pytest.raises(PublicationEvidenceError, match="controlled"):
            authority.seal(scope, attempt, generation, expected_count=0,
                           content_digest=EMPTY_DIGEST, live=live)
    else:
        authority.seal(scope, attempt, generation, expected_count=0,
                       content_digest=EMPTY_DIGEST, live=live)

    with db.transaction() as cursor:
        final = cursor.execute("select * from vector_generations where generation_id=%s",
                               (generation,)).fetchone()
    if reject_at == "seal":
        assert (final["state"], final["expected_count"],
                final["content_digest"], final["sealed_at"]) == (
                    "staging", None, None, None)
    else:
        assert (final["state"], final["expected_count"],
                final["content_digest"]) == ("sealed", 0, EMPTY_DIGEST)
        assert final["sealed_at"] is not None
    assert not _rows(db, user)["vector_heads"]
    assert [call[2] for call in calls] == ["stage_before", "stage_after", "seal"]
    assert all(call[0] is live and call[3] == "rag" and call[4] == scope
               and call[5] == generation and call[6:] == (evidence.phase,
                                                            evidence.phase_version)
               for call in calls)
    assert calls[0][1] is calls[1][1] and calls[2][1] is not calls[0][1]
    assert reads == [calls[0][1], calls[2][1]]
