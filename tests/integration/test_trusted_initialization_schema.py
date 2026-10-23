"""Completed-only trusted initialization proof in fresh PostgreSQL schemas."""

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from app.history import EMPTY_HISTORY
from app.isolated_import_runtime import IsolatedImportSettings, _schema_ready
from app.postgres import PostgresDatabase
from app.postgres_coordination import PostgresUserMutationCoordinator
from app.postgres_history_document_witnesses import (
    PostgresHistoryDocumentWitnessRepository, document_evidence,
)
from app.postgres_snapshots import PostgresSnapshotRepository
from app.postgres_vector_generations import PostgresVectorGenerationAuthority, VectorScope
from app.trusted_user_initialization_settings import (
    TrustedInitializationConfigurationError, TrustedUserInitializationSettings,
    require_initialization_schema,
)
from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
from hello_agents.memory.rag.index_identity import IndexIdentity
ROOT = Path(__file__).resolve().parents[2]
EMPTY_DIGEST = hashlib.sha256(b"[]").hexdigest()
COLUMNS = (
    "operation_id", "format_version", "user_id", "username_key",
    "resource_descriptor", "resource_descriptor_sha256",
    "history_initial_sha256", "memory_initial_sha256",
    "rag_identity", "episode_identity", "rag_kind", "episode_kind",
    "rag_namespace", "episode_namespace", "rag_index_key", "episode_index_key",
    "rag_initial_generation_id", "episode_initial_generation_id",
    "rag_head_revision", "episode_head_revision", "rag_index_revision",
    "episode_index_revision", "history_snapshot_version", "memory_snapshot_version",
    "empty_manifest_sha256", "history_document_evidence_sha256", "completed_at",
)
INSERT_COLUMNS = tuple(name for name in COLUMNS if name != "completed_at")
INSERT_SQL = (
    "insert into trusted_user_initializations (" + ",".join(INSERT_COLUMNS) + ") "
    "values (" + ",".join("%s" for _ in INSERT_COLUMNS) + ")"
)
CONSTRAINTS = {
    "trusted_user_initializations_pkey", "trusted_init_user_unique_17",
    "trusted_init_user_fk_17", "trusted_init_rag_generation_fk_17",
    "trusted_init_episode_generation_fk_17", "trusted_init_rag_witness_fk_17",
    "trusted_init_format_ck_17", "trusted_init_kinds_ck_17",
    "trusted_init_initial_versions_ck_17", "trusted_init_digest_shape_ck_17",
    "trusted_init_descriptor_ck_17",
}


@pytest.fixture
def shared_database(monkeypatch, request):
    """Disposable PG schema, cleaned even if Alembic upgrade raises."""
    base = os.environ.get("POSTGRES_TEST_URL")
    if not base:
        pytest.fail("POSTGRES_TEST_URL is required for Task01 schema evidence")
    schema = "init_t1_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    url = make_url(base).set(drivername="postgresql+psycopg",
                             query={"options": f"-csearch_path={schema}"})
    url = url.render_as_string(hide_password=False)
    pools = []

    def open_pool():
        database = PostgresDatabase(
            url.replace("postgresql+psycopg://", "postgresql://"),
            min_size=1, max_size=3)
        database.open()
        pools.append(database)
        return database

    try:
        monkeypatch.setenv("DATABASE_URL", url)
        command.upgrade(Config(str(ROOT / "alembic.ini")),
                        getattr(request, "param", "20261022_17"))
        yield open_pool, url
    finally:
        for database in pools:
            database.close()
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("drop schema {} cascade").format(
                sql.Identifier(schema)))


def _settings(url):
    schema = make_url(url).query["options"].split("=", 1)[1]
    rag = EmbeddingProfile("simple", "", "SimpleEmbedding", "v1", 4)
    episode = EmbeddingProfile("test-explicit", "", "episode", "v1", 4)
    env = {
        "TRUSTED_USER_INITIALIZATION_ENABLED": "1",
        "ISOLATED_IMPORT_ENABLED": "1",
        "ISOLATED_IMPORT_TRUSTED_USER_IDS":
            "11111111-1111-4111-8111-111111111111,"
            "22222222-2222-4222-8222-222222222222",
        "ISOLATED_IMPORT_DATABASE_URL":
            url.replace("postgresql+psycopg://", "postgresql://"),
        "ISOLATED_IMPORT_SCHEMA": schema,
        "ISOLATED_IMPORT_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "ISOLATED_IMPORT_S3_BUCKET": "init-test-bucket",
        "ISOLATED_IMPORT_QDRANT_URL": "http://127.0.0.1:6333",
        "ISOLATED_IMPORT_RAG_COLLECTION": "init_rag",
        "ISOLATED_IMPORT_EPISODE_COLLECTION": "init_episode",
        "ISOLATED_IMPORT_RAG_PROFILE_JSON": json.dumps(asdict(rag)),
        "ISOLATED_IMPORT_EPISODE_PROFILE_JSON": json.dumps(asdict(episode)),
    }
    return (TrustedUserInitializationSettings.from_env(env),
            IsolatedImportSettings.from_env("api", env), schema,
            make_url(url).database)


def _jsonb_sha(cursor, value):
    return cursor.execute(
        "select encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as digest",
        (Jsonb(value),),
    ).fetchone()["digest"]


def _ledger_values(db, settings, user, username, rag_scope, episode_scope,
                   rag_generation, episode_generation):
    history = deepcopy(EMPTY_HISTORY)
    memory = {"user_id": user, "memories": []}
    descriptor = settings.resource_descriptor()
    with db.transaction() as cursor:
        descriptor_sha = _jsonb_sha(cursor, descriptor)
        history_sha = _jsonb_sha(cursor, history)
        memory_sha = _jsonb_sha(cursor, memory)
    return {
        "operation_id": uuid4(), "format_version": 1,
        "user_id": user, "username_key": username,
        "resource_descriptor": descriptor,
        "resource_descriptor_sha256": descriptor_sha,
        "history_initial_sha256": history_sha,
        "memory_initial_sha256": memory_sha,
        "rag_identity": rag_scope.identity.to_dict(),
        "episode_identity": episode_scope.identity.to_dict(),
        "rag_kind": "rag", "episode_kind": "episode",
        "rag_namespace": rag_scope.namespace,
        "episode_namespace": episode_scope.namespace,
        "rag_index_key": rag_scope.index_key,
        "episode_index_key": episode_scope.index_key,
        "rag_initial_generation_id": rag_generation,
        "episode_initial_generation_id": episode_generation,
        "rag_head_revision": 1, "episode_head_revision": 1,
        "rag_index_revision": 1, "episode_index_revision": 1,
        "history_snapshot_version": 1, "memory_snapshot_version": 1,
        "empty_manifest_sha256": EMPTY_DIGEST,
        "history_document_evidence_sha256": document_evidence(user, history).digest,
    }


def _insert(db, values):
    with db.transaction() as cursor:
        cursor.execute(INSERT_SQL, tuple(
            Jsonb(values[name]) if name in {
                "resource_descriptor", "rag_identity", "episode_identity"
            } else values[name] for name in INSERT_COLUMNS
        ))


def _complete_empty_authority(db, settings, *, witness=True,
                              memory_payload=None, memory_present=True,
                              history_payload=None):
    """Use real stage/seal/publish APIs; this fixture has no direct published SQL."""
    user = str(uuid4())
    username = "init_" + uuid4().hex
    with db.transaction() as cursor:
        cursor.execute(
            "insert into users(id,username,username_key,password_hash,status,"
            "created_at,updated_at) values (%s,%s,%s,'test-hash','active',"
            "clock_timestamp(),clock_timestamp())",
            (user, username, username),
        )
    snapshots = PostgresSnapshotRepository(db)
    history = deepcopy(EMPTY_HISTORY)
    if history_payload is not None:
        history = history_payload(user, history)
    assert snapshots.compare_and_swap(user, "history", history,
                                      expected_version=0).version == 1
    if memory_present:
        memory = {"user_id": user, "memories": (
            [] if memory_payload is None else memory_payload(user))}
        assert snapshots.compare_and_swap(user, "memory", memory,
                                          expected_version=0).version == 1
    rag_scope = VectorScope(user, "rag", "pdf_" + user, settings.rag_identity)
    episode_scope = VectorScope(user, "episode", "episodes", settings.episode_identity)
    coordinator = PostgresUserMutationCoordinator(db)
    lease = coordinator.acquire(user, "init-test")
    assert lease is not None
    authority = PostgresVectorGenerationAuthority(db)
    generations = []
    try:
        for scope in (rag_scope, episode_scope):
            generation = authority.stage(scope, lease, expected_revision=None)
            authority.seal(scope, lease, generation, expected_count=0,
                           content_digest=EMPTY_DIGEST)
            assert authority.publish_user(scope, lease, generation,
                                          expected_revision=None,
                                          snapshot_version=1) == 1
            head = authority.read_head(scope)
            assert (head.state, head.revision, head.generation_id,
                    head.index_revision, head.snapshot_version) == (
                        "empty", 1, None, 1, 1)
            with db.transaction() as cursor:
                receipt = cursor.execute(
                    "select state,expected_count,content_digest,base_revision,"
                    "index_revision,publication_revision,publication_snapshot_version "
                    "from vector_generations where generation_id=%s", (generation,),
                ).fetchone()
            assert receipt == {
                "state": "published", "expected_count": 0,
                "content_digest": EMPTY_DIGEST, "base_revision": None,
                "index_revision": 1, "publication_revision": 1,
                "publication_snapshot_version": 1,
            }
            generations.append(generation)
        if witness:
            repository = PostgresHistoryDocumentWitnessRepository(db, None)
            with db.transaction() as cursor:
                pairing = repository.read_current(rag_scope, cursor=cursor)
                repository.insert(cursor, rag_scope, pairing,
                                  document_evidence(user, history))
    finally:
        coordinator.release(lease)
    return _ledger_values(db, settings, user, username, rag_scope, episode_scope,
                          *generations)


def _expect_ledger_rejection(db, values):
    before = _authority_state(db, values["user_id"])
    with pytest.raises(psycopg.Error):
        _insert(db, values)
    with db.transaction() as cursor:
        assert cursor.execute(
            "select count(*) as n from trusted_user_initializations"
        ).fetchone()["n"] == 0
    assert _authority_state(db, values["user_id"]) == before


def _authority_state(db, user):
    with db.transaction() as cursor:
        return (
            cursor.execute("select kind,version,payload from user_snapshots "
                           "where user_id=%s order by kind", (user,)).fetchall(),
            cursor.execute("select vector_kind,namespace,index_key,revision,"
                           "generation_id,last_generation_id,index_revision,"
                           "snapshot_version from vector_heads where tenant_id=%s "
                           "order by vector_kind,namespace,index_key",
                           (user,)).fetchall(),
            cursor.execute("select generation_id,state,expected_count,"
                           "content_digest,publication_revision,"
                           "publication_snapshot_version from vector_generations "
                           "where tenant_id=%s order by vector_kind,namespace,"
                           "index_key,generation_id", (user,)).fetchall(),
            cursor.execute("select namespace,index_key,head_revision,"
                           "last_generation_id,document_count,documents_sha256 "
                           "from history_document_witnesses where tenant_id=%s "
                           "order by namespace,index_key,head_revision",
                           (user,)).fetchall(),
        )


def test_trusted_initialization_schema_ledger_is_immutable(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    settings, _, _, _ = _settings(url)
    values = _complete_empty_authority(db, settings)
    _insert(db, values)
    with db.transaction() as cursor:
        row = cursor.execute(
            "select operation_id,user_id,format_version,completed_at "
            "from trusted_user_initializations where operation_id=%s",
            (values["operation_id"],),
        ).fetchone()
    assert row["user_id"] == values["user_id"]
    assert row["format_version"] == 1 and row["completed_at"] is not None
    with pytest.raises(psycopg.Error):
        with db.transaction() as cursor:
            cursor.execute("update trusted_user_initializations "
                           "set username_key=username_key where operation_id=%s",
                           (values["operation_id"],))
    with pytest.raises(psycopg.Error):
        with db.transaction() as cursor:
            cursor.execute("delete from trusted_user_initializations "
                           "where operation_id=%s", (values["operation_id"],))
    with db.transaction() as cursor:
        assert cursor.execute("select count(*) as n from "
                              "trusted_user_initializations").fetchone()["n"] == 1
    with pytest.raises(RuntimeError):
        command.downgrade(Config("alembic.ini"), "20261007_16")
    with db.transaction() as cursor:
        assert cursor.execute("select version_num from alembic_version").fetchone()[
            "version_num"] == "20261022_17"


@pytest.mark.parametrize("mutation", [
    {"username_key": "wrong"},
    {"format_version": 2},
    {"history_initial_sha256": "0" * 64},
    {"memory_initial_sha256": "0" * 64},
    {"rag_initial_generation_id": uuid4()},
    {"episode_index_key": "0" * 64},
    {"rag_head_revision": 2},
    {"empty_manifest_sha256": "0" * 64},
    {"history_document_evidence_sha256": "0" * 64},
    {"history_initial_sha256": "G" * 64},
    {"memory_initial_sha256": "short"},
])
def test_ledger_rejects_partial_or_mismatched_proof(shared_database, mutation):
    open_pool, url = shared_database
    db = open_pool()
    settings, _, _, _ = _settings(url)
    values = _complete_empty_authority(db, settings)
    values.update(mutation)
    _expect_ledger_rejection(db, values)


def test_ledger_rejects_missing_witness_and_descriptor_identity_drift(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    settings, _, _, _ = _settings(url)
    missing = _complete_empty_authority(db, settings, witness=False)
    _expect_ledger_rejection(db, missing)

    missing_snapshot = _complete_empty_authority(db, settings, memory_present=False)
    _expect_ledger_rejection(db, missing_snapshot)

    nonempty_snapshot = _complete_empty_authority(
        db, settings,
        memory_payload=lambda user: [{"metadata": {"user_id": user}}])
    _expect_ledger_rejection(db, nonempty_snapshot)

    nonempty_history = _complete_empty_authority(
        db, settings,
        history_payload=lambda user, history: history | {
            "documents": [{"document_id": str(uuid4()), "user_id": user}]})
    _expect_ledger_rejection(db, nonempty_history)

    values = _complete_empty_authority(db, settings)
    descriptor = deepcopy(values["resource_descriptor"])
    descriptor["postgres"]["database"] = "another_database"
    values["resource_descriptor"] = descriptor
    with db.transaction() as cursor:
        values["resource_descriptor_sha256"] = _jsonb_sha(cursor, descriptor)
    _expect_ledger_rejection(db, values)

    values = _complete_empty_authority(db, settings)
    identity = deepcopy(values["rag_identity"])
    identity["profile"]["revision"] = "different"
    values["rag_identity"] = identity
    descriptor = deepcopy(values["resource_descriptor"])
    descriptor["qdrant"]["rag_identity"] = identity
    values["resource_descriptor"] = descriptor
    with db.transaction() as cursor:
        values["resource_descriptor_sha256"] = _jsonb_sha(cursor, descriptor)
    _expect_ledger_rejection(db, values)

    values = _complete_empty_authority(db, settings)
    descriptor = deepcopy(values["resource_descriptor"])
    descriptor["s3"]["unexpected"] = "extra"
    values["resource_descriptor"] = descriptor
    with db.transaction() as cursor:
        values["resource_descriptor_sha256"] = _jsonb_sha(cursor, descriptor)
    _expect_ledger_rejection(db, values)


@pytest.mark.parametrize("target,secret_endpoint", [
    ("s3", "http://user:SECRET@127.0.0.1:9000"),
    ("s3", "http://127.0.0.1:9000/?token=SECRET"),
    ("s3", "http://127.0.0.1:9000/#SECRET"),
    ("qdrant", "http://user:SECRET@127.0.0.1:6333"),
    ("qdrant", "http://127.0.0.1:6333/?token=SECRET"),
    ("qdrant", "http://127.0.0.1:6333/#SECRET"),
    ("rag_profile", "http://user:SECRET@127.0.0.1:9000/v1"),
    ("rag_profile", "//user:SECRET@127.0.0.1:9000/v1"),
    ("episode_profile", "https://example.test/v1?token=SECRET"),
    ("episode_profile", "https://example.test/v1#SECRET"),
])
def test_ledger_rejects_secret_endpoint_with_correct_descriptor_digest(
        shared_database, target, secret_endpoint):
    open_pool, url = shared_database
    db = open_pool()
    settings, _, _, _ = _settings(url)
    rag_identity = settings.rag_identity
    episode_identity = settings.episode_identity
    if target == "rag_profile":
        rag_identity = IndexIdentity("qdrant", settings.rag_collection,
                                     replace(settings.rag_profile,
                                             endpoint=secret_endpoint))
    elif target == "episode_profile":
        episode_identity = IndexIdentity("qdrant", settings.episode_collection,
                                         replace(settings.episode_profile,
                                                 endpoint=secret_endpoint))

    class SecretFixtureSettings:
        def __init__(self):
            self.rag_identity = rag_identity
            self.episode_identity = episode_identity

        def resource_descriptor(self):
            descriptor = deepcopy(settings.resource_descriptor())
            descriptor["qdrant"]["rag_identity"] = rag_identity.to_dict()
            descriptor["qdrant"]["episode_identity"] = episode_identity.to_dict()
            if target in {"s3", "qdrant"}:
                descriptor[target]["endpoint_url"] = secret_endpoint
            return descriptor

    values = _complete_empty_authority(db, SecretFixtureSettings())
    before = _authority_state(db, values["user_id"])
    with db.transaction() as cursor:
        values["resource_descriptor_sha256"] = _jsonb_sha(
            cursor, values["resource_descriptor"])
    with pytest.raises(psycopg.errors.CheckViolation) as error:
        _insert(db, values)
    assert error.value.diag.constraint_name == "trusted_init_descriptor_ck_17"
    with db.transaction() as cursor:
        assert cursor.execute("select count(*) as n from "
                              "trusted_user_initializations").fetchone()["n"] == 0
    assert _authority_state(db, values["user_id"]) == before


def test_ledger_unique_operation_and_user(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    settings, _, _, _ = _settings(url)
    first = _complete_empty_authority(db, settings)
    second = _complete_empty_authority(db, settings)
    _insert(db, first)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(db, second | {"operation_id": first["operation_id"]})
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert(db, first | {"operation_id": uuid4()})
    with db.transaction() as cursor:
        assert cursor.execute("select count(*) as n from "
                              "trusted_user_initializations").fetchone()["n"] == 1


def test_reviewed_schema_heads_16_17_only(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    initializer, runtime, schema, database_name = _settings(url)
    assert _schema_ready(db, runtime) is True
    assert require_initialization_schema(db, schema, database_name) is None
    with db.transaction() as cursor:
        cursor.execute("update alembic_version set version_num='20261023_18'")
    assert _schema_ready(db, runtime) is False
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        require_initialization_schema(db, schema, database_name)
    assert error.value.code == "TRUSTED_INITIALIZATION_SCHEMA"
    with db.transaction() as cursor:
        cursor.execute("update alembic_version set version_num='20261022_17'")
    with pytest.raises(TrustedInitializationConfigurationError):
        require_initialization_schema(db, "public", database_name)
    with pytest.raises(TrustedInitializationConfigurationError):
        require_initialization_schema(db, schema, "wrong_database")
    wrong_path = make_url(url).set(query={"options": "-csearch_path=public"})
    wrong_db = PostgresDatabase(
        wrong_path.render_as_string(hide_password=False).replace(
            "postgresql+psycopg://", "postgresql://"), min_size=1, max_size=1)
    wrong_db.open()
    try:
        assert _schema_ready(wrong_db, runtime) is False
        with pytest.raises(TrustedInitializationConfigurationError):
            require_initialization_schema(wrong_db, schema, database_name)
    finally:
        wrong_db.close()


@pytest.mark.parametrize("shared_database", ["20261007_16"], indirect=True)
def test_schema16_runtime_allowed_initializer_refused_then_upgrade(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    _, runtime, schema, database_name = _settings(url)
    assert _schema_ready(db, runtime) is True
    with pytest.raises(TrustedInitializationConfigurationError):
        require_initialization_schema(db, schema, database_name)
    command.upgrade(Config("alembic.ini"), "20261022_17")
    assert _schema_ready(db, runtime) is True
    assert require_initialization_schema(db, schema, database_name) is None


def test_schema17_two_version_rows_and_missing_relation_fail_closed(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    _, runtime, schema, database_name = _settings(url)
    with db.transaction() as cursor:
        cursor.execute("insert into alembic_version(version_num) "
                       "values ('20261023_18')")
    assert _schema_ready(db, runtime) is False
    with pytest.raises(TrustedInitializationConfigurationError):
        require_initialization_schema(db, schema, database_name)
    with db.transaction() as cursor:
        cursor.execute("delete from alembic_version "
                       "where version_num='20261023_18'")
        cursor.execute("drop table trusted_user_initializations")
    assert _schema_ready(db, runtime) is False
    with pytest.raises(TrustedInitializationConfigurationError):
        require_initialization_schema(db, schema, database_name)


@pytest.mark.parametrize("tamper", [
    "alter table trusted_user_initializations disable trigger trusted_init_guard_17",
    "alter table trusted_user_initializations drop constraint trusted_init_descriptor_ck_17",
    "alter table trusted_user_initializations drop constraint trusted_init_rag_generation_fk_17",
    "create or replace function trusted_init_insert_guard_17() returns trigger "
    "language plpgsql as $$begin return new; end$$",
])
def test_schema17_catalog_tamper_fails_closed(shared_database, tamper):
    open_pool, url = shared_database
    db = open_pool()
    _, runtime, schema, database_name = _settings(url)
    assert require_initialization_schema(db, schema, database_name) is None
    with db.transaction() as cursor:
        cursor.execute(tamper)
    assert _schema_ready(db, runtime) is False
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        require_initialization_schema(db, schema, database_name)
    assert error.value.code == "TRUSTED_INITIALIZATION_SCHEMA"


@pytest.mark.parametrize("tamper_kind", [
    "when_false", "digest_literal_case", "port_cast",
])
def test_schema17_subtle_catalog_tamper_fails_closed(shared_database, tamper_kind):
    open_pool, url = shared_database
    db = open_pool()
    _, runtime, schema, database_name = _settings(url)
    assert _schema_ready(db, runtime) is True
    assert require_initialization_schema(db, schema, database_name) is None
    with db.transaction() as cursor:
        if tamper_kind == "when_false":
            cursor.execute("drop trigger trusted_init_guard_17 "
                           "on trusted_user_initializations")
            cursor.execute("create trigger trusted_init_guard_17 before insert or "
                           "update or delete on trusted_user_initializations "
                           "for each row when (false) execute function "
                           "trusted_init_insert_guard_17()")
        else:
            constraint_name = (
                "trusted_init_digest_shape_ck_17" if tamper_kind == "digest_literal_case"
                else "trusted_init_descriptor_ck_17")
            definition = cursor.execute(
                "select pg_get_constraintdef(oid) as definition from pg_constraint "
                "where conrelid=to_regclass('trusted_user_initializations') "
                "and conname=%s", (constraint_name,)
            ).fetchone()["definition"]
            if tamper_kind == "digest_literal_case":
                assert "^[0-9a-f]{64}$" in definition
                tampered = definition.replace("^[0-9a-f]{64}$",
                                              "^[0-9A-F]{64}$", 1)
            else:
                assert "::integer" in definition
                tampered = definition.replace("::integer", "::smallint", 1)
            cursor.execute(sql.SQL("alter table trusted_user_initializations "
                                   "drop constraint {}").format(
                                       sql.Identifier(constraint_name)))
            cursor.execute(sql.SQL("alter table trusted_user_initializations "
                                   "add constraint {} ").format(
                                       sql.Identifier(constraint_name)) +
                           sql.SQL(tampered))
    assert _schema_ready(db, runtime) is False
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        require_initialization_schema(db, schema, database_name)
    assert error.value.code == "TRUSTED_INITIALIZATION_SCHEMA"


@pytest.mark.parametrize("side", ["child_check", "parent_action"])
def test_schema17_disabled_internal_fk_trigger_fails_closed(shared_database, side):
    open_pool, url = shared_database
    db = open_pool()
    _, runtime, schema, database_name = _settings(url)
    assert _schema_ready(db, runtime) is True
    with db.transaction() as cursor:
        function_name = ("RI_FKey_check_ins" if side == "child_check"
                         else "RI_FKey_noaction_del")
        table = ("trusted_user_initializations" if side == "child_check"
                 else "users")
        trigger = cursor.execute("""select t.tgname from pg_trigger t
            join pg_proc p on p.oid=t.tgfoid
            where t.tgconstraint=(select oid from pg_constraint
              where conrelid=to_regclass('trusted_user_initializations')
                and conname='trusted_init_user_fk_17')
              and t.tgrelid=to_regclass(%s) and p.proname=%s""",
            (table, function_name)).fetchone()
        assert trigger is not None
        cursor.execute(sql.SQL("alter table {} disable trigger {}").format(
            sql.Identifier(table), sql.Identifier(trigger["tgname"])))
        assert cursor.execute("""select convalidated from pg_constraint
            where conrelid=to_regclass('trusted_user_initializations')
              and conname='trusted_init_user_fk_17'""").fetchone()["convalidated"]
        assert cursor.execute("""select tgenabled from pg_trigger where
            tgrelid=to_regclass('trusted_user_initializations')
              and tgname='trusted_init_guard_17'""").fetchone()["tgenabled"] == "O"
    assert _schema_ready(db, runtime) is False
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        require_initialization_schema(db, schema, database_name)
    assert error.value.code == "TRUSTED_INITIALIZATION_SCHEMA"


def test_schema17_columns_constraints_and_empty_downgrade(shared_database):
    open_pool, url = shared_database
    db = open_pool()
    _, _, schema, _ = _settings(url)
    with db.transaction() as cursor:
        columns = cursor.execute(
            "select column_name,data_type,is_nullable,column_default "
            "from information_schema.columns where table_schema=%s and "
            "table_name='trusted_user_initializations'",
            (schema,),
        ).fetchall()
        constraints = cursor.execute(
            "select conname,convalidated from pg_constraint where conrelid="
            "to_regclass(%s)", (schema + ".trusted_user_initializations",),
        ).fetchall()
        trigger = cursor.execute(
            "select tgname,tgenabled from pg_trigger where tgrelid="
            "to_regclass(%s) and not tgisinternal",
            (schema + ".trusted_user_initializations",),
        ).fetchall()
    assert {row["column_name"] for row in columns} == set(COLUMNS)
    assert all(row["is_nullable"] == "NO" for row in columns)
    assert {row["column_name"] for row in columns if row["column_default"]} == {
        "completed_at"}
    assert "clock_timestamp" in next(row["column_default"] for row in columns
                                     if row["column_name"] == "completed_at")
    assert {row["conname"] for row in constraints} == CONSTRAINTS
    assert all(row["convalidated"] for row in constraints)
    assert trigger == [{"tgname": "trusted_init_guard_17", "tgenabled": "O"}]
    command.downgrade(Config("alembic.ini"), "20261007_16")
    with db.transaction() as cursor:
        assert cursor.execute("select version_num from alembic_version").fetchone()[
            "version_num"] == "20261007_16"
        assert cursor.execute("select to_regclass('trusted_user_initializations') "
                              "as name").fetchone()["name"] is None
