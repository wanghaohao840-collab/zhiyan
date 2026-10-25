"""Atomic first-user baseline and PostgreSQL-only historical replay."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from math import isfinite
from typing import Callable, Literal
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.auth import (hash_password, utc_now, validate_password,
                      validate_username, verify_password)
from app.history import EMPTY_HISTORY
from app.import_publication_evidence import require_no_gate_in_transaction
from app.isolated_resource_settings import _dsn, _host
from app.postgres import PostgresDatabase
from app.postgres_auth import _insert_new_user_in_transaction
from app.postgres_coordination import (MutationLeaseLost,
                                       PostgresUserMutationCoordinator)
from app.postgres_history_document_witnesses import (
    PostgresHistoryDocumentWitnessRepository, document_evidence,
)
from app.postgres_snapshots import PostgresSnapshotRepository
from app.postgres_vector_generations import (PostgresVectorGenerationAuthority,
                                             VectorScope)
from app.trusted_user_initialization_settings import (
    TrustedUserInitializationSettings, require_initialization_schema,
)


_EMPTY_DIGEST = sha256(b"[]").hexdigest()
_NAMED_CONFLICTS = frozenset({"users_username_key_key",
                              "trusted_user_initializations_pkey"})


class InitializationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class InitializationOutcomeUnknown(InitializationError):
    def __init__(self, operation_id: UUID):
        self.operation_id = operation_id
        super().__init__("unknown")


@dataclass(frozen=True)
class InitializedUser:
    user_id: str
    username: str
    operation_id: UUID
    durable_initialized: Literal[True] = True
    replayed: bool = False


@dataclass(frozen=True)
class _RequestBinding:
    username: str
    username_key: str
    password: str = field(repr=False)
    descriptor: dict = field(repr=False)
    rag_identity: dict = field(repr=False)
    episode_identity: dict = field(repr=False)
    postgres_identity: tuple = field(repr=False)


class _BodyFailed(Exception):
    pass


class _Winner(Exception):
    pass


class _LeaseBusy(Exception):
    pass


def _error(code: str) -> InitializationError:
    return InitializationError(code)


def _bind(database: PostgresDatabase, operation_id: UUID, username: str,
          password: str, resources: TrustedUserInitializationSettings,
          preflight: Callable[[], None]) -> _RequestBinding:
    if type(operation_id) is not UUID or str(UUID(str(operation_id))) != str(operation_id):
        raise _error("invalid_request")
    try:
        display, key = validate_username(username)
        validate_password(password)
    except Exception:
        raise _error("invalid_request") from None
    if not callable(preflight):
        raise _error("invalid_request")
    if type(resources) is not TrustedUserInitializationSettings:
        raise _error("configuration")
    try:
        descriptor = resources.resource_descriptor()
        rag_identity = resources.rag_identity.to_dict()
        episode_identity = resources.episode_identity.to_dict()
        pg_identity = _dsn(resources.database_url, "ISOLATED_IMPORT_DATABASE_URL")
        if type(database) is not PostgresDatabase:
            raise ValueError()
        pool = database._pool
        if (type(pool) is not ConnectionPool
                or pool.kwargs != {"row_factory": dict_row}
                or pool.connection_class is not Connection
                or pool._configure is not None
                or pool._check is not None
                or pool._reset is not None):
            raise ValueError()
        timeout = pool.timeout
        if (type(timeout) not in (int, float) or not isfinite(timeout)
                or timeout <= 0 or timeout > 10):
            raise ValueError()
        if _dsn(pool.conninfo, "ISOLATED_IMPORT_DATABASE_URL") != pg_identity:
            raise ValueError()
    except Exception:
        raise _error("configuration") from None
    return _RequestBinding(display, key, password, descriptor,
                           rag_identity, episode_identity, pg_identity)


def _check_client(connection, binding: _RequestBinding) -> None:
    try:
        info = connection.info
        host, database, _, port, _ = binding.postgres_identity
        if (_host(info.host) != host or info.dbname != database
                or int(info.port) != port):
            raise ValueError()
    except Exception:
        raise _error("configuration") from None


def _set_limits(cursor) -> None:
    cursor.execute("set local lock_timeout='5s'")
    cursor.execute("set local statement_timeout='10s'")
    cursor.execute("set local idle_in_transaction_session_timeout='15s'")


def _digest_values(database: PostgresDatabase, binding: _RequestBinding,
                   user_id: str) -> tuple[str, str, str]:
    try:
        with database.connection() as connection:
            _check_client(connection, binding)
            cursor = connection.cursor()
            try:
                cursor.execute("begin isolation level repeatable read read only")
                _set_limits(cursor)
                row = cursor.execute("""select
                    encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as resource,
                    encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as history,
                    encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as memory""",
                    (Jsonb(binding.descriptor), Jsonb(EMPTY_HISTORY),
                     Jsonb({"user_id": user_id, "memories": []})),
                ).fetchone()
                return row["resource"], row["history"], row["memory"]
            finally:
                cursor.close()
    except InitializationError:
        raise
    except Exception:
        raise _error("unavailable") from None


def _scopes(binding: _RequestBinding, resources: TrustedUserInitializationSettings,
            user_id: str) -> tuple[VectorScope, VectorScope]:
    return (
        VectorScope(user_id, "rag", "pdf_" + user_id, resources.rag_identity),
        VectorScope(user_id, "episode", "episodes", resources.episode_identity),
    )


def _prove_rows(cursor, operation_id: UUID, binding: _RequestBinding) -> InitializedUser | None:
    ledger = cursor.execute("""select *,
        encode(sha256(convert_to(resource_descriptor::text,'UTF8')),'hex')
            as actual_resource_sha256
        from trusted_user_initializations where operation_id=%s""",
        (operation_id,),
    ).fetchone()
    if ledger is None:
        return None
    user_id = ledger["user_id"]
    try:
        if str(UUID(user_id)) != user_id:
            raise ValueError()
        rag_scope = VectorScope(user_id, "rag", "pdf_" + user_id,
                                _identity_from_dict(binding.rag_identity))
        episode_scope = VectorScope(user_id, "episode", "episodes",
                                    _identity_from_dict(binding.episode_identity))
    except Exception:
        raise _error("corrupt") from None
    if (ledger["format_version"] != 1
            or ledger["resource_descriptor_sha256"] != ledger["actual_resource_sha256"]):
        raise _error("corrupt")
    if (ledger["username_key"] != binding.username_key
            or ledger["resource_descriptor"] != binding.descriptor):
        raise _error("mismatch")
    if (
            ledger["rag_identity"] != binding.rag_identity
            or ledger["episode_identity"] != binding.episode_identity
            or ledger["empty_manifest_sha256"] != _EMPTY_DIGEST
            or ledger["history_document_evidence_sha256"] != _EMPTY_DIGEST
            or ledger["history_snapshot_version"] != 1
            or ledger["memory_snapshot_version"] != 1
            or ledger["rag_head_revision"] != 1
            or ledger["episode_head_revision"] != 1
            or ledger["rag_index_revision"] != 1
            or ledger["episode_index_revision"] != 1
            or ledger["rag_kind"] != "rag" or ledger["episode_kind"] != "episode"
            or ledger["rag_namespace"] != rag_scope.namespace
            or ledger["episode_namespace"] != episode_scope.namespace
            or ledger["rag_index_key"] != rag_scope.index_key
            or ledger["episode_index_key"] != episode_scope.index_key
            or ledger["rag_initial_generation_id"] == ledger["episode_initial_generation_id"]):
        raise _error("corrupt")

    digest_row = cursor.execute("""select
        encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as history,
        encode(sha256(convert_to((%s::jsonb)::text,'UTF8')),'hex') as memory""",
        (Jsonb(EMPTY_HISTORY), Jsonb({"user_id": user_id, "memories": []})),
    ).fetchone()
    if (ledger["history_initial_sha256"] != digest_row["history"]
            or ledger["memory_initial_sha256"] != digest_row["memory"]):
        raise _error("corrupt")

    account = cursor.execute("select id,username,username_key,password_hash,status "
                             "from users where id=%s", (user_id,)).fetchone()
    if account is None:
        raise _error("corrupt")
    if (account["username_key"] != binding.username_key
            or account["username"] != binding.username
            or account["status"] != "active"
            or not verify_password(binding.password, account["password_hash"])):
        raise _error("mismatch")

    for scope, generation_id in (
            (rag_scope, ledger["rag_initial_generation_id"]),
            (episode_scope, ledger["episode_initial_generation_id"])):
        receipt = cursor.execute("""select * from vector_generations
            where generation_id=%s and tenant_id=%s and vector_kind=%s
              and namespace=%s and index_key=%s""",
            (generation_id, *scope.key),
        ).fetchone()
        if (receipt is None or receipt["state"] not in {"published", "retired"}
                or receipt["base_revision"] is not None
                or receipt["index_revision"] != 1
                or receipt["publication_revision"] != 1
                or receipt["publication_snapshot_version"] != 1
                or receipt["expected_count"] != 0
                or receipt["content_digest"] != _EMPTY_DIGEST
                or receipt["sealed_at"] is None or receipt["published_at"] is None
                or receipt["task_id"] is not None):
            raise _error("corrupt")
    witness = cursor.execute("""select * from history_document_witnesses
        where tenant_id=%s and vector_kind=%s and namespace=%s and index_key=%s
          and head_revision=%s""",
        (*rag_scope.key, ledger["rag_head_revision"]),
    ).fetchone()
    if (witness is None
            or witness["last_generation_id"] != ledger["rag_initial_generation_id"]
            or witness["index_revision"] != 1
            or witness["publication_snapshot_version"] != 1
            or witness["document_count"] != 0
            or witness["documents_sha256"] != _EMPTY_DIGEST):
        raise _error("corrupt")
    return InitializedUser(user_id, account["username"], operation_id, replayed=True)


def _identity_from_dict(identity: dict):
    """Use the request's validated full identity without external lookup."""
    from hello_agents.memory.rag.index_identity import IndexIdentity

    return IndexIdentity.from_dict(identity)


def read_initialization_proof(database: PostgresDatabase, operation_id: UUID,
                              request_binding: _RequestBinding) -> InitializedUser | None:
    """Prove a completed operation from one bounded, read-only PG snapshot."""
    try:
        with database.connection() as connection:
            _check_client(connection, request_binding)
            cursor = connection.cursor()
            try:
                cursor.execute("begin isolation level repeatable read read only")
                _set_limits(cursor)
                return _prove_rows(cursor, operation_id, request_binding)
            finally:
                cursor.close()
    except InitializationError:
        raise
    except Exception:
        raise _error("unavailable") from None


def _insert_ledger(cursor, operation_id: UUID, binding: _RequestBinding,
                   user_id: str, rag_scope: VectorScope,
                   episode_scope: VectorScope, generations: tuple[UUID, UUID],
                   digests: tuple[str, str, str]) -> None:
    cursor.execute("""insert into trusted_user_initializations (
        operation_id,format_version,user_id,username_key,
        resource_descriptor,resource_descriptor_sha256,
        history_initial_sha256,memory_initial_sha256,
        rag_identity,episode_identity,rag_kind,episode_kind,
        rag_namespace,episode_namespace,rag_index_key,episode_index_key,
        rag_initial_generation_id,episode_initial_generation_id,
        rag_head_revision,episode_head_revision,rag_index_revision,episode_index_revision,
        history_snapshot_version,memory_snapshot_version,
        empty_manifest_sha256,history_document_evidence_sha256)
        values (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,'rag','episode',
                %s,%s,%s,%s,%s,%s,1,1,1,1,1,1,%s,%s)""",
        (operation_id, user_id, binding.username_key,
         Jsonb(binding.descriptor), *digests,
         Jsonb(binding.rag_identity), Jsonb(binding.episode_identity),
         rag_scope.namespace, episode_scope.namespace,
         rag_scope.index_key, episode_scope.index_key,
         *generations, _EMPTY_DIGEST, _EMPTY_DIGEST),
    )


def _write_baseline(cursor, database: PostgresDatabase, operation_id: UUID,
                    binding: _RequestBinding, user_id: str,
                    rag_scope: VectorScope, episode_scope: VectorScope,
                    password_hash: str, digests: tuple[str, str, str]) -> None:
    _check_client(cursor.connection, binding)
    _set_limits(cursor)
    if cursor.execute("select 1 from trusted_user_initializations where operation_id=%s",
                      (operation_id,)).fetchone() is not None:
        raise _Winner()
    _insert_new_user_in_transaction(
        cursor, user_id=user_id, username=binding.username,
        username_key=binding.username_key, password_hash=password_hash, now=utc_now(),
    )
    coordinator = PostgresUserMutationCoordinator(database)
    lease = coordinator.acquire_in_transaction(cursor, user_id,
                                               "trusted-init:" + str(operation_id),
                                               lease_seconds=60)
    if lease is None:
        raise _LeaseBusy()
    snapshots = PostgresSnapshotRepository(database)
    snapshots.compare_and_swap_in_transaction(cursor, user_id, "history",
                                               EMPTY_HISTORY, expected_version=0)
    snapshots.compare_and_swap_in_transaction(
        cursor, user_id, "memory", {"user_id": user_id, "memories": []},
        expected_version=0,
    )
    generations = PostgresVectorGenerationAuthority(
        database)._publish_initial_empty_pair_in_transaction(
            cursor, lease, rag_scope, episode_scope,
        )
    witnesses = PostgresHistoryDocumentWitnessRepository(database, None)
    pairing = witnesses.read_current(rag_scope, cursor=cursor)
    if pairing.last_generation_id != generations[0]:
        raise RuntimeError("Initial RAG pairing differs")
    witnesses.insert(cursor, rag_scope, pairing,
                     document_evidence(user_id, EMPTY_HISTORY))
    _insert_ledger(cursor, operation_id, binding, user_id, rag_scope,
                   episode_scope, generations, digests)
    counts = cursor.execute("""select
        (select count(*) from user_snapshots where user_id=%s) as snapshots,
        (select count(*) from vector_indexes where tenant_id=%s) as indexes,
        (select count(*) from vector_heads where tenant_id=%s) as heads,
        (select count(*) from vector_generations where tenant_id=%s) as generations,
        (select count(*) from history_document_witnesses where tenant_id=%s) as witnesses,
        (select count(*) from trusted_user_initializations where operation_id=%s) as ledgers""",
        (user_id, user_id, user_id, user_id, user_id, operation_id),
    ).fetchone()
    if tuple(counts.values()) != (2, 2, 2, 2, 1, 1):
        raise RuntimeError("Incomplete initial baseline")
    coordinator.require_live_in_transaction(cursor, lease)
    require_no_gate_in_transaction(cursor, user_id)
    coordinator.release_in_transaction(cursor, lease)


def _proved_result(proof: InitializedUser | None, candidate: str,
                   *, absent_code: str) -> InitializedUser:
    if proof is None:
        raise _error(absent_code)
    return replace(proof, replayed=proof.user_id != candidate)


def _followup_proof(database: PostgresDatabase, operation_id: UUID,
                    binding: _RequestBinding) -> InitializedUser | None:
    try:
        return read_initialization_proof(database, operation_id, binding)
    except InitializationError as error:
        if error.code in {"configuration", "unavailable"}:
            raise _error("unavailable") from None
        raise


def _confirmed_body_failure(database: PostgresDatabase, operation_id: UUID,
                            binding: _RequestBinding, candidate: str,
                            original: Exception) -> InitializedUser:
    if isinstance(original, InitializationError):
        raise _error(original.code) from None
    if isinstance(original, _Winner):
        proof = _followup_proof(database, operation_id, binding)
        return _proved_result(proof, candidate, absent_code="unavailable")
    if (isinstance(original, UniqueViolation)
            and getattr(getattr(original, "diag", None), "constraint_name", None)
            in _NAMED_CONFLICTS):
        proof = _followup_proof(database, operation_id, binding)
        return _proved_result(proof, candidate, absent_code="conflict")
    if isinstance(original, _LeaseBusy) or getattr(original, "sqlstate", None) == "55P03":
        raise _error("busy") from None
    if isinstance(original, MutationLeaseLost):
        raise _error("expired") from None
    if getattr(original, "sqlstate", None) == "57014":
        raise _error("timeout") from None
    raise _error("failed") from None


def initialize_trusted_user(database: PostgresDatabase, operation_id: UUID,
                            username: str, password: str,
                            resources: TrustedUserInitializationSettings, *,
                            preflight: Callable[[], None]) -> InitializedUser:
    """Create one complete baseline or prove an existing operation without repair."""
    binding = _bind(database, operation_id, username, password, resources, preflight)
    try:
        with database.connection() as connection:
            _check_client(connection, binding)
    except InitializationError:
        raise
    except Exception:
        raise _error("unavailable") from None
    try:
        require_initialization_schema(database, resources.schema,
                                      binding.postgres_identity[1])
    except Exception:
        raise _error("configuration") from None
    initial = read_initialization_proof(database, operation_id, binding)
    if initial is not None:
        return initial

    candidate = str(uuid4())
    rag_scope, episode_scope = _scopes(binding, resources, candidate)
    digests = _digest_values(database, binding, candidate)
    try:
        prepared_hash = hash_password(password)
    except Exception:
        raise _error("failed") from None
    try:
        preflight()
    except Exception:
        raise _error("preflight_failed") from None

    body_failed = _BodyFailed()
    entered = False
    original = None
    try:
        with database.transaction() as cursor:
            entered = True
            try:
                _write_baseline(cursor, database, operation_id, binding, candidate,
                                rag_scope, episode_scope, prepared_hash, digests)
            except Exception as error:
                original = error
                raise body_failed from None
    except Exception as escaped:
        if not entered:
            raise _error("unavailable") from None
        if escaped is body_failed:
            return _confirmed_body_failure(database, operation_id, binding,
                                           candidate, original)
        try:
            proof = read_initialization_proof(database, operation_id, binding)
            if proof is not None:
                return _proved_result(proof, candidate, absent_code="unknown")
        except Exception:
            pass
        raise InitializationOutcomeUnknown(operation_id) from None
    try:
        proof = read_initialization_proof(database, operation_id, binding)
    except Exception:
        raise _error("unavailable") from None
    if proof is None or proof.user_id != candidate:
        raise _error("unavailable")
    return replace(proof, replayed=False)
