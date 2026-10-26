"""Fake-only contracts for the explicit trusted-user operations command."""

import json
import logging
from types import SimpleNamespace
from uuid import UUID
import warnings

import pytest

import scripts.initialize_trusted_user as cli


ORIGINAL_POSTGRES = cli.PostgresDatabase
OPERATION_ID = "123e4567-e89b-42d3-a456-426614174000"
USER_ID = "123e4567-e89b-42d3-a456-426614174001"
PASSWORD = "password-CANARY-only-in-memory"
SECRET = "secret-CANARY-do-not-print"
DSN = f"postgresql://operator:{SECRET}@localhost:5432/isolated?options=-csearch_path%3Disolated"
SECRETS = (PASSWORD, SECRET, DSN, "access-CANARY", "qdrant-CANARY")


@pytest.fixture(autouse=True)
def no_secret_logs(caplog):
    caplog.set_level(logging.DEBUG)
    yield
    logged = "\n".join(
        caplog.handler.format(record)
        for when in ("setup", "call", "teardown")
        for record in caplog.get_records(when)
    )
    for value in SECRETS:
        assert value not in logged


def _safe_streams(output):
    for value in SECRETS:
        assert value not in output.out
        assert value not in output.err
    assert "Traceback" not in output.err


def _settings():
    from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
    from app.trusted_user_initialization_settings import TrustedUserInitializationSettings

    return TrustedUserInitializationSettings(
        enabled=True,
        database_url=DSN,
        schema="isolated",
        s3_endpoint_url="http://127.0.0.1:9000",
        s3_bucket="isolated-bucket",
        qdrant_url="http://127.0.0.1:6333",
        rag_collection="isolated_rag",
        episode_collection="isolated_episode",
        rag_profile=EmbeddingProfile("simple", "", "rag", "v1", 4),
        episode_profile=EmbeddingProfile("simple", "", "episode", "v1", 8),
        s3_access_key_id="access-CANARY",
        s3_secret_access_key=SECRET,
        qdrant_api_key="qdrant-CANARY",
        pg_pool_min=2,
        pg_pool_max=3,
    )


def _args(operation_id=OPERATION_ID):
    return ["--operation-id", operation_id, "--username", "new-operator"]


def _diagnostic_state():
    loggers = tuple(logging.getLogger(name) for name in
                    ("psycopg.pool", "botocore.endpoint", "qdrant_client"))
    return (
        logging.root.manager.disable,
        logging.root.level,
        tuple(logging.root.handlers),
        tuple(logging.root.filters),
        tuple((logger.level, logger.disabled, logger.propagate,
               tuple(logger.handlers), tuple(logger.filters)) for logger in loggers),
        tuple(warnings.filters),
        warnings.showwarning,
    )


def _success(capsys, operation_id=OPERATION_ID, replayed=False, *,
             expected_warning=False):
    output = capsys.readouterr()
    _safe_streams(output)
    if expected_warning:
        assert output.err.split() == ["cleanup_failed"]
    else:
        assert output.err == ""
    lines = output.out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["operation_id"] == operation_id
    assert result["user_id"] == USER_ID
    assert result["durable_initialized"] is True
    assert result["replayed"] is replayed
    assert "allowlist" in json.dumps(result).lower()
    assert "ordinary_ready" not in json.dumps(result)
    assert "new-operator" not in json.dumps(result)
    return result, output.err


def _failure(capsys, code, *, operation_id=None, cleanup_warning=False):
    output = capsys.readouterr()
    _safe_streams(output)
    assert output.out == ""
    assert (operation_id is not None) == (code == "unknown")
    assert output.err.split() == ([code]
                                  + ([operation_id] if operation_id else [])
                                  + (["cleanup_failed"] if cleanup_warning else []))
    return output.err


class FakeDatabase:
    instances = []
    open_fault = None
    close_fault = None

    def __init__(self, database_url, **kwargs):
        self.args = (database_url, kwargs)
        self.open_calls = 0
        self.close_calls = 0
        self._pool = SimpleNamespace(close=self._pool_close)
        self.pool_close_calls = 0
        self._opened = False
        self.instances.append(self)

    def open(self):
        self.open_calls += 1
        if self.open_fault:
            raise self.open_fault
        self._opened = True

    def close(self):
        self.close_calls += 1
        if self.close_fault:
            raise self.close_fault
        self._opened = False

    def _pool_close(self):
        self.pool_close_calls += 1


class RawS3:
    def __init__(self):
        self.calls = []
        self.versioning = "Enabled"
        self.probe_fault = None
        self.close_fault = None

    def head_bucket(self, **kwargs):
        self.calls.append(("head_bucket", kwargs))
        if self.probe_fault:
            raise self.probe_fault

    def get_bucket_versioning(self, **kwargs):
        self.calls.append(("get_bucket_versioning", kwargs))
        return {"Status": self.versioning}

    def close(self):
        self.calls.append(("close", {}))
        if self.close_fault:
            raise self.close_fault


class RawQdrant:
    def __init__(self, settings):
        self.calls = []
        self.exists = True
        self.bad_collection = None
        self.bad_field = None
        self.probe_fault = None
        self.close_fault = None
        self.configs = {
            settings.rag_identity.physical_collection: (4, "Cosine"),
            settings.episode_identity.physical_collection: (8, "Cosine"),
        }

    def collection_exists(self, name):
        self.calls.append(("collection_exists", name))
        if self.probe_fault:
            raise self.probe_fault
        return self.exists and name in self.configs

    def get_collection(self, name):
        self.calls.append(("get_collection", name))
        size, distance = self.configs[name]
        if name == self.bad_collection:
            if self.bad_field == "dimension":
                size += 1
            elif self.bad_field == "distance":
                distance = "Dot"
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(
            vectors=SimpleNamespace(size=size, distance=distance))))

    def close(self):
        self.calls.append(("close", None))
        if self.close_fault:
            raise self.close_fault


@pytest.fixture
def rig(monkeypatch):
    from app.object_store import S3ObjectStore
    from hello_agents.memory.storage.vector_store import QdrantVectorStore

    settings = _settings()
    calls = {"password": 0, "service": [], "writes": 0, "s3_kwargs": [],
             "q_kwargs": [], "s3": [], "q": [], "bucket_checks": 0,
             "collection_checks": []}
    FakeDatabase.instances = []
    FakeDatabase.open_fault = None
    FakeDatabase.close_fault = None
    monkeypatch.setattr(cli, "TrustedUserInitializationSettings",
                        SimpleNamespace(from_env=lambda: settings))
    monkeypatch.setattr(cli, "PostgresDatabase", FakeDatabase)

    def password_prompt(prompt):
        calls["password"] += 1
        return PASSWORD

    monkeypatch.setattr(cli.getpass, "getpass", password_prompt)

    def s3_factory(service, **kwargs):
        assert service == "s3"
        calls["s3_kwargs"].append(kwargs)
        raw = RawS3()
        calls["s3"].append(raw)
        return raw

    def q_factory(**kwargs):
        calls["q_kwargs"].append(kwargs)
        raw = RawQdrant(settings)
        calls["q"].append(raw)
        return raw

    monkeypatch.setattr(cli.boto3, "client", s3_factory)
    monkeypatch.setattr(cli, "QdrantClient", q_factory)

    class ObservedS3(S3ObjectStore):
        def check_ready(self):
            calls["bucket_checks"] += 1
            return super().check_ready()

    class ObservedQdrant(QdrantVectorStore):
        def __init__(self, *args, **kwargs):
            assert kwargs.get("retry_delays") == ()
            super().__init__(*args, **kwargs)

        def require_collection(self, collection_name, dimension, distance="Cosine"):
            calls["collection_checks"].append((collection_name, dimension, distance))
            return super().require_collection(collection_name, dimension, distance)

    monkeypatch.setattr(cli, "S3ObjectStore", ObservedS3)
    monkeypatch.setattr(cli, "QdrantVectorStore", ObservedQdrant)

    def service(database, operation_id, username, password, resources, *, preflight):
        calls["service"].append((database, operation_id, username, password,
                                 resources, preflight))
        try:
            preflight()
        except Exception:
            raise cli.InitializationError("preflight_failed") from None
        calls["writes"] += 1
        return cli.InitializedUser(USER_ID, username, operation_id)

    monkeypatch.setattr(cli, "initialize_trusted_user", service)
    return SimpleNamespace(settings=settings, calls=calls)


def test_help_needs_no_settings_prompt_or_clients(rig, monkeypatch, capsys):
    settings_calls = []
    monkeypatch.setattr(cli.TrustedUserInitializationSettings, "from_env",
                        lambda: settings_calls.append(True))
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr()
    _safe_streams(output)
    assert "--operation-id" in output.out and "--username" in output.out
    assert "--password" not in output.out
    assert output.err == ""
    assert settings_calls == []
    assert rig.calls["password"] == 0
    assert FakeDatabase.instances == []
    assert rig.calls["s3"] == rig.calls["q"] == []


@pytest.mark.parametrize("argv", [
    pytest.param(["--username", "new-operator"], id="missing-operation"),
    pytest.param(["--operation-id", OPERATION_ID], id="missing-username"),
    pytest.param(_args() + ["--username", "again"], id="duplicate-username"),
    pytest.param(_args() + ["--operation-id", OPERATION_ID], id="duplicate-operation"),
    pytest.param(_args() + ["--unknown", SECRET], id="unknown-flag"),
    pytest.param(_args() + ["--password", SECRET], id="password-separate"),
    pytest.param(_args() + [f"--password={SECRET}"], id="password-equals"),
    pytest.param(["--operation", OPERATION_ID, "--username", "new-operator"],
                 id="abbreviated-operation"),
    pytest.param(["--operation-id", OPERATION_ID, "--user", "new-operator"],
                 id="abbreviated-username"),
    pytest.param(_args(OPERATION_ID.upper()), id="uppercase-uuid"),
    pytest.param(_args("not-a-uuid-CANARY"), id="invalid-uuid"),
])
def test_invalid_argv_is_sanitized_before_settings_or_prompt(rig, monkeypatch,
                                                              capsys, argv):
    settings_calls = []
    monkeypatch.setattr(cli.TrustedUserInitializationSettings, "from_env",
                        lambda: settings_calls.append(True))
    assert cli.main(argv) != 0
    output = capsys.readouterr()
    _safe_streams(output)
    assert output.out == ""
    assert output.err.strip()
    assert "not-a-uuid-CANARY" not in output.err
    assert settings_calls == []
    assert rig.calls["password"] == 0
    assert FakeDatabase.instances == []
    assert rig.calls["s3"] == rig.calls["q"] == []


def test_disabled_opt_in_is_configuration_failure_before_prompt(rig, monkeypatch,
                                                                 capsys):
    from app.trusted_user_initialization_settings import TrustedUserInitializationSettings

    monkeypatch.delenv("TRUSTED_USER_INITIALIZATION_ENABLED", raising=False)
    monkeypatch.setattr(cli, "TrustedUserInitializationSettings",
                        TrustedUserInitializationSettings)
    assert cli.main(_args()) != 0
    _failure(capsys, "configuration")
    assert rig.calls["password"] == 0
    assert FakeDatabase.instances == []


@pytest.mark.parametrize("kind", ["mismatch", "eof", "interrupt", "warning"])
def test_hidden_password_failures_precede_database(rig, monkeypatch, capsys, kind):
    def prompt(_):
        rig.calls["password"] += 1
        if kind == "warning":
            warnings.warn("echo fallback " + SECRET, cli.getpass.GetPassWarning)
            return PASSWORD
        if kind == "eof":
            raise EOFError(SECRET)
        if kind == "interrupt":
            raise KeyboardInterrupt(SECRET)
        return PASSWORD if rig.calls["password"] == 1 else "different"

    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    assert cli.main(_args()) != 0
    output = capsys.readouterr()
    _safe_streams(output)
    assert output.out == "" and output.err.strip()
    assert rig.calls["password"] == (2 if kind == "mismatch" else 1)
    assert FakeDatabase.instances == []
    assert rig.calls["service"] == []


def test_original_postgres_constructor_uses_native_pool_contract(rig, monkeypatch,
                                                                  capsys):
    import app.postgres as postgres

    assert ORIGINAL_POSTGRES is postgres.PostgresDatabase
    pool_calls = []

    class Pool:
        def __init__(self, **kwargs):
            pool_calls.append(kwargs)

        def open(self, *, wait):
            assert wait is True

        def close(self):
            pass

    monkeypatch.setattr(postgres, "ConnectionPool", Pool)
    monkeypatch.setattr(cli, "PostgresDatabase", postgres.PostgresDatabase)
    monkeypatch.setattr(cli, "initialize_trusted_user",
                        lambda db, op, name, password, resources, *, preflight:
                        cli.InitializedUser(USER_ID, name, op))
    assert cli.main(_args()) == 0
    _success(capsys)
    assert len(pool_calls) == 1
    assert pool_calls[0] == {
        "conninfo": DSN,
        "min_size": rig.settings.pg_pool_min,
        "max_size": rig.settings.pg_pool_max,
        "timeout": 10.0,
        "kwargs": {"row_factory": postgres.dict_row},
        "open": False,
    }


def test_new_operation_checks_exact_existing_resources_once(rig, capsys):
    assert cli.main(_args()) == 0
    _success(capsys)
    assert rig.calls["password"] == 2
    assert len(rig.calls["service"]) == 1
    database, operation_id, username, password, resources, callback = rig.calls["service"][0]
    assert database is FakeDatabase.instances[0]
    assert operation_id == UUID(OPERATION_ID)
    assert username == "new-operator" and password == PASSWORD
    assert resources is rig.settings and callable(callback)
    assert database.args == (DSN, {"min_size": 2, "max_size": 3, "timeout": 10.0})
    assert database.open_calls == database.close_calls == 1
    assert rig.calls["writes"] == 1
    assert rig.calls["bucket_checks"] == 1
    assert len(rig.calls["s3"]) == len(rig.calls["q"]) == 1
    assert rig.calls["s3"][0].calls == [
        ("head_bucket", {"Bucket": rig.settings.s3_bucket}),
        ("get_bucket_versioning", {"Bucket": rig.settings.s3_bucket}),
        ("close", {}),
    ]
    expected = [rig.settings.rag_identity, rig.settings.episode_identity]
    assert expected[0].physical_collection != expected[1].physical_collection
    assert rig.calls["collection_checks"] == [
        (identity.physical_collection, identity.profile.dimension,
         identity.profile.distance) for identity in expected
    ]
    assert rig.calls["q"][0].calls == [
        (method, identity.physical_collection)
        for identity in expected for method in ("collection_exists", "get_collection")
    ] + [("close", None)]
    s3_kwargs = rig.calls["s3_kwargs"][0]
    assert s3_kwargs["endpoint_url"] == rig.settings.s3_endpoint_url
    assert s3_kwargs["region_name"] == rig.settings.s3_region
    assert s3_kwargs["aws_access_key_id"] == rig.settings.s3_access_key_id
    assert s3_kwargs["aws_secret_access_key"] == rig.settings.s3_secret_access_key
    config = s3_kwargs["config"]
    assert config.s3["addressing_style"] == "path"
    assert 0 < config.connect_timeout <= 10 and 0 < config.read_timeout <= 10
    assert config.retries.get("max_attempts", config.retries.get("total_max_attempts", 99)) <= 2
    assert rig.calls["q_kwargs"] == [{"url": rig.settings.qdrant_url,
                                   "api_key": rig.settings.qdrant_api_key,
                                   "timeout": 10,
                                   "check_compatibility": False}]


def test_replay_uses_postgres_only_even_if_external_factories_fail(rig, monkeypatch,
                                                                    capsys):
    def no_external(*args, **kwargs):
        pytest.fail("replay constructed external client")

    monkeypatch.setattr(cli.boto3, "client", no_external)
    monkeypatch.setattr(cli, "QdrantClient", no_external)

    def replay(database, operation_id, username, password, resources, *, preflight):
        rig.calls["service"].append((database, operation_id))
        return cli.InitializedUser(USER_ID, username, operation_id, replayed=True)

    monkeypatch.setattr(cli, "initialize_trusted_user", replay)
    assert cli.main(_args()) == 0
    _success(capsys, replayed=True)
    assert len(rig.calls["service"]) == 1
    assert FakeDatabase.instances[0].close_calls == 1
    assert rig.calls["s3"] == rig.calls["q"] == []
    assert rig.calls["bucket_checks"] == 0
    assert rig.calls["collection_checks"] == []


@pytest.mark.parametrize("fault", [
    "versioning", "s3-probe", "missing-collection", "rag-dimension",
    "rag-distance", "episode-dimension", "episode-distance", "q-transport",
    "s3-close", "q-close", "both-close", "s3-constructor", "q-constructor",
])
def test_preflight_faults_fail_before_fake_write_and_close_owned_clients(
        rig, monkeypatch, capsys, fault):
    s3 = RawS3()
    q = RawQdrant(rig.settings)
    if fault == "versioning":
        s3.versioning = "Suspended"
    elif fault == "s3-probe":
        s3.probe_fault = RuntimeError(SECRET)
    elif fault == "missing-collection":
        q.exists = False
    elif fault.endswith("dimension") or fault.endswith("distance"):
        identity = (rig.settings.rag_identity if fault.startswith("rag")
                    else rig.settings.episode_identity)
        q.bad_collection = identity.physical_collection
        q.bad_field = fault.split("-")[1]
    elif fault == "q-transport":
        q.probe_fault = TimeoutError(SECRET)
    elif fault in {"s3-close", "both-close"}:
        s3.close_fault = RuntimeError(SECRET)
    if fault in {"q-close", "both-close"}:
        q.close_fault = RuntimeError(SECRET)

    def s3_factory(service, **kwargs):
        if fault == "s3-constructor":
            raise RuntimeError(SECRET)
        rig.calls["s3"].append(s3)
        return s3

    def q_factory(**kwargs):
        if fault == "q-constructor":
            raise RuntimeError(SECRET)
        rig.calls["q"].append(q)
        return q

    monkeypatch.setattr(cli.boto3, "client", s3_factory)
    monkeypatch.setattr(cli, "QdrantClient", q_factory)
    assert cli.main(_args()) != 0
    _failure(capsys, "preflight_failed")
    assert rig.calls["writes"] == 0
    assert len(rig.calls["service"]) == 1
    assert s3.calls.count(("close", {})) == (0 if fault == "s3-constructor" else 1)
    if rig.calls["q"]:
        assert q.calls.count(("close", None)) == 1
    if fault == "q-transport":
        assert sum(method == "collection_exists" for method, _ in q.calls) == 1
    assert FakeDatabase.instances[0].close_calls == 1


@pytest.mark.parametrize("code", [
    "busy", "expired", "timeout", "preflight_failed", "invalid_request",
    "configuration", "unavailable", "conflict", "corrupt", "mismatch", "failed",
])
def test_typed_service_errors_preserve_distinct_code(rig, monkeypatch, capsys, code):
    def fail(*args, **kwargs):
        raise cli.InitializationError(code)

    monkeypatch.setattr(cli, "initialize_trusted_user", fail)
    assert cli.main(_args()) != 0
    stderr = _failure(capsys, code)
    assert SECRET not in stderr and "ordinary_ready" not in stderr
    assert FakeDatabase.instances[0].close_calls == 1
    assert rig.calls["s3"] == rig.calls["q"] == []


def test_typed_unknown_preserves_saved_uuid_without_retry(rig, monkeypatch, capsys):
    calls = []

    def unknown(*args, **kwargs):
        calls.append(args)
        raise cli.InitializationOutcomeUnknown(UUID(OPERATION_ID))

    monkeypatch.setattr(cli, "initialize_trusted_user", unknown)
    assert cli.main(_args()) != 0
    _failure(capsys, "unknown", operation_id=OPERATION_ID)
    assert len(calls) == 1 and rig.calls["writes"] == 0
    assert FakeDatabase.instances[0].close_calls == 1


@pytest.mark.parametrize("fault", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_untyped_failure_after_service_entry_is_unknown(rig, monkeypatch,
                                                        capsys, fault):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise fault(SECRET)

    monkeypatch.setattr(cli, "initialize_trusted_user", fail)
    assert cli.main(_args()) != 0
    _failure(capsys, "unknown", operation_id=OPERATION_ID)
    assert len(calls) == 1 and rig.calls["writes"] == 0
    assert FakeDatabase.instances[0].close_calls == 1


def test_half_open_native_postgres_pool_is_closed_after_open_failure(rig,
                                                                    monkeypatch,
                                                                    capsys):
    import app.postgres as postgres

    pools = []

    class HalfOpenPool:
        def __init__(self, **kwargs):
            self.close_calls = 0
            pools.append(self)

        def open(self, *, wait):
            raise RuntimeError(SECRET)

        def close(self):
            self.close_calls += 1

    monkeypatch.setattr(postgres, "ConnectionPool", HalfOpenPool)
    monkeypatch.setattr(cli, "PostgresDatabase", postgres.PostgresDatabase)
    monkeypatch.setattr(cli, "initialize_trusted_user",
                        lambda *args, **kwargs: pytest.fail("service entered"))
    assert cli.main(_args()) != 0
    _failure(capsys, "unavailable")
    assert len(pools) == 1 and pools[0].close_calls == 1


@pytest.mark.parametrize("open_fails", [False, True], ids=["opened", "open-failed"])
def test_native_pool_diagnostics_are_scoped_through_close(rig, monkeypatch,
                                                          capsys, open_fails):
    import app.postgres as postgres

    pools = []

    class NoisyPool:
        def __init__(self, **kwargs):
            self.close_calls = 0
            pools.append(self)

        def open(self, *, wait):
            assert wait is True
            logging.getLogger("psycopg.pool").warning("error connecting: %s", SECRET)
            if open_fails:
                raise RuntimeError(SECRET)

        def close(self):
            self.close_calls += 1
            logging.getLogger("psycopg.pool").warning("pool closing: %s", SECRET)

    monkeypatch.setattr(postgres, "ConnectionPool", NoisyPool)
    monkeypatch.setattr(cli, "PostgresDatabase", postgres.PostgresDatabase)
    monkeypatch.setattr(cli, "initialize_trusted_user",
                        lambda db, op, name, password, resources, *, preflight:
                        cli.InitializedUser(USER_ID, name, op))
    before = _diagnostic_state()
    if open_fails:
        assert cli.main(_args()) != 0
        _failure(capsys, "unavailable")
    else:
        assert cli.main(_args()) == 0
        _success(capsys)
    assert len(pools) == 1 and pools[0].close_calls == 1
    assert _diagnostic_state() == before


@pytest.mark.parametrize("probe_fails", [False, True], ids=["ready", "probe-failed"])
def test_external_dependency_diagnostics_are_scoped(rig, monkeypatch, capsys,
                                                    probe_fails):
    def s3_factory(service, **kwargs):
        logging.getLogger("botocore.endpoint").debug(
            "Sending request with Credential=%s", rig.settings.s3_access_key_id)
        warnings.warn("S3 constructor: " + SECRET, UserWarning)
        raw = RawS3()
        rig.calls["s3"].append(raw)
        return raw

    def q_factory(**kwargs):
        assert kwargs["check_compatibility"] is False
        warnings.warn("insecure API key: " + rig.settings.qdrant_api_key,
                      UserWarning)
        raw = RawQdrant(rig.settings)
        original_probe = raw.collection_exists

        def noisy_probe(name):
            logging.getLogger("qdrant_client").debug("probe: %s", SECRET)
            warnings.warn("Qdrant probe: " + SECRET, UserWarning)
            if probe_fails:
                raise TimeoutError(SECRET)
            return original_probe(name)

        raw.collection_exists = noisy_probe
        rig.calls["q"].append(raw)
        return raw

    monkeypatch.setattr(cli.boto3, "client", s3_factory)
    monkeypatch.setattr(cli, "QdrantClient", q_factory)
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        before = _diagnostic_state()
        if probe_fails:
            assert cli.main(_args()) != 0
            _failure(capsys, "preflight_failed")
            assert rig.calls["writes"] == 0
        else:
            assert cli.main(_args()) == 0
            _success(capsys)
            assert rig.calls["writes"] == 1
        assert observed == []
        assert _diagnostic_state() == before
    assert len(rig.calls["s3"]) == len(rig.calls["q"]) == 1
    assert rig.calls["s3"][0].calls[-1] == ("close", {})
    assert rig.calls["q"][0].calls[-1] == ("close", None)


def test_confirmed_result_survives_postgres_close_failure(rig, capsys):
    FakeDatabase.close_fault = RuntimeError(SECRET)
    assert cli.main(_args()) == 0
    _success(capsys, expected_warning=True)
    assert FakeDatabase.instances[0].close_calls == 1
    assert rig.calls["writes"] == 1


@pytest.mark.parametrize("code", ["busy", "unknown"])
def test_typed_error_and_unknown_survive_postgres_close_failure(rig,
                                                                monkeypatch,
                                                                capsys, code):
    FakeDatabase.close_fault = RuntimeError(SECRET)

    def fail(*args, **kwargs):
        if code == "unknown":
            raise cli.InitializationOutcomeUnknown(UUID(OPERATION_ID))
        raise cli.InitializationError(code)

    monkeypatch.setattr(cli, "initialize_trusted_user", fail)
    assert cli.main(_args()) != 0
    _failure(capsys, code,
             operation_id=OPERATION_ID if code == "unknown" else None,
             cleanup_warning=True)
    assert FakeDatabase.instances[0].close_calls == 1


def test_interrupt_in_cleanup_cannot_erase_confirmed_result(rig, capsys):
    FakeDatabase.close_fault = KeyboardInterrupt(SECRET)
    assert cli.main(_args()) == 0
    _success(capsys, expected_warning=True)
    assert rig.calls["writes"] == 1


def test_secret_bearing_repr_and_exception_are_never_rendered(rig,
                                                               monkeypatch,
                                                               capsys):
    class Poison(RuntimeError):
        def __repr__(self):
            return SECRET

    def fail(*args, **kwargs):
        raise Poison(SECRET)

    monkeypatch.setattr(cli, "initialize_trusted_user", fail)
    assert cli.main(_args()) != 0
    _failure(capsys, "unknown", operation_id=OPERATION_ID)
