"""Resource-only first-user settings and legacy parsing priority, without I/O."""

from dataclasses import asdict, replace
import json
import socket

import pytest

from app.isolated_import_runtime import (
    IsolatedImportConfigurationError, IsolatedImportSettings,
)
from app.isolated_resource_settings import (
    IsolatedResourceSettings, ResourceConfigurationError,
    parse_isolated_resources, validate_isolated_resource_identity,
    validate_isolated_resource_pool,
)
from app.trusted_user_initialization_settings import (
    TrustedInitializationConfigurationError, TrustedUserInitializationSettings,
)
from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
from hello_agents.memory.rag.index_identity import IndexIdentity


SECRET = "SECRET_MARKER"
RAG = EmbeddingProfile("simple", "", "SimpleEmbedding", "v1", 4)
EPISODE = EmbeddingProfile("test-explicit", "", "episode", "v1", 4)


def values():
    return {
        "TRUSTED_USER_INITIALIZATION_ENABLED": "1",
        "ISOLATED_IMPORT_DATABASE_URL":
            f"postgresql://user:{SECRET}@localhost:5432/init_test"
            "?options=-csearch_path%3Dinit_test_schema",
        "ISOLATED_IMPORT_SCHEMA": "init_test_schema",
        "ISOLATED_IMPORT_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "ISOLATED_IMPORT_S3_BUCKET": "init-test-bucket",
        "ISOLATED_IMPORT_QDRANT_URL": "http://127.0.0.1:6333",
        "ISOLATED_IMPORT_RAG_COLLECTION": "init_rag",
        "ISOLATED_IMPORT_EPISODE_COLLECTION": "init_episode",
        "ISOLATED_IMPORT_RAG_PROFILE_JSON": json.dumps(asdict(RAG)),
        "ISOLATED_IMPORT_EPISODE_PROFILE_JSON": json.dumps(asdict(EPISODE)),
        "S3_ACCESS_KEY_ID": SECRET,
        "S3_SECRET_ACCESS_KEY": SECRET,
        "QDRANT_API_KEY": SECRET,
    }


def legacy_values():
    return values() | {
        "ISOLATED_IMPORT_ENABLED": "1",
        "ISOLATED_IMPORT_TRUSTED_USER_IDS":
            "11111111-1111-4111-8111-111111111111,"
            "22222222-2222-4222-8222-222222222222",
    }


def assert_safe_error(error, code):
    assert error.value.code == code
    assert SECRET not in str(error.value)
    assert SECRET not in repr(error.value)


def test_resource_only_settings_without_trusted_ids(monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("settings performed network I/O")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    env = values()
    absent = TrustedUserInitializationSettings.from_env(env)
    assert absent.enabled is True
    # The original gate/allowlist are deliberately irrelevant to this entry.
    env["ISOLATED_IMPORT_ENABLED"] = "0"
    env["ISOLATED_IMPORT_TRUSTED_USER_IDS"] = "not-a-uuid"
    settings = TrustedUserInitializationSettings.from_env(env)
    assert settings.enabled is True
    assert not hasattr(settings, "role")
    assert not hasattr(settings, "trusted_user_ids")
    assert settings.rag_identity == IndexIdentity("qdrant", "init_rag", RAG)
    assert settings.episode_identity == IndexIdentity("qdrant", "init_episode", EPISODE)
    assert SECRET not in repr(settings)
    assert SECRET not in repr(settings.rag_identity)


def test_resource_descriptor_exact_full_and_canary_free():
    settings = TrustedUserInitializationSettings.from_env(values())
    expected = {
        "format_version": 1,
        "postgres": {"host": "loopback", "port": 5432,
                     "database": "init_test", "schema": "init_test_schema"},
        "s3": {"endpoint_url": "http://127.0.0.1:9000",
               "bucket": "init-test-bucket", "region": "us-east-1"},
        "qdrant": {"endpoint_url": "http://127.0.0.1:6333",
                   "rag_identity": IndexIdentity("qdrant", "init_rag", RAG).to_dict(),
                   "episode_identity":
                       IndexIdentity("qdrant", "init_episode", EPISODE).to_dict()},
    }
    descriptor = settings.resource_descriptor()
    assert descriptor == expected
    encoded = json.dumps(descriptor, sort_keys=True)
    assert SECRET not in encoded
    assert "password" not in encoded.lower()
    assert "access_key" not in encoded.lower()
    assert "pg_pool" not in encoded.lower()
    assert descriptor["qdrant"]["rag_identity"]["profile"] == asdict(RAG)
    assert descriptor["qdrant"]["episode_identity"]["profile"] == asdict(EPISODE)


@pytest.mark.parametrize("flag", [None, "", "0", "true", "True", "2"])
def test_initializer_flag_is_exactly_one(flag):
    env = values()
    if flag is None:
        del env["TRUSTED_USER_INITIALIZATION_ENABLED"]
    else:
        env["TRUSTED_USER_INITIALIZATION_ENABLED"] = flag
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        TrustedUserInitializationSettings.from_env(env)
    assert_safe_error(error, "TRUSTED_USER_INITIALIZATION_ENABLED")


@pytest.mark.parametrize("key,value,code", [
    ("ISOLATED_IMPORT_DATABASE_URL", "postgresql://localhost/init_test"
     "?options=-csearch_path%3Dpublic", "ISOLATED_IMPORT_SCHEMA"),
    ("ISOLATED_IMPORT_SCHEMA", "public", "ISOLATED_IMPORT_SCHEMA"),
    ("ISOLATED_IMPORT_S3_BUCKET",
     "cutover-paired-bf20-69fcab0f0cbd4300a0545f2008c15779",
     "ISOLATED_IMPORT_S3_BUCKET"),
    ("ISOLATED_IMPORT_S3_ENDPOINT_URL", "http://user:pass@127.0.0.1:9000",
     "ISOLATED_IMPORT_S3_ENDPOINT_URL"),
    ("ISOLATED_IMPORT_QDRANT_URL", "http://127.0.0.1:6333/path",
     "ISOLATED_IMPORT_QDRANT_URL"),
    ("ISOLATED_IMPORT_EPISODE_COLLECTION", "init_rag",
     "ISOLATED_IMPORT_COLLECTION_IDENTITY"),
    ("ISOLATED_IMPORT_RAG_PROFILE_JSON", "{}",
     "ISOLATED_IMPORT_RAG_PROFILE_JSON"),
    ("ISOLATED_IMPORT_PG_POOL_MIN", "0", "ISOLATED_IMPORT_TIMING"),
])
def test_resource_only_rejects_invalid_input(key, value, code):
    env = values()
    env[key] = value
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        TrustedUserInitializationSettings.from_env(env)
    assert_safe_error(error, code)


def test_resource_only_rejects_ordinary_identity_and_profile_endpoint_secret():
    env = values()
    env["DATABASE_URL"] = env["ISOLATED_IMPORT_DATABASE_URL"]
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        TrustedUserInitializationSettings.from_env(env)
    assert_safe_error(error, "ISOLATED_IMPORT_DATABASE_IDENTITY")

    env = values()
    env["ISOLATED_IMPORT_RAG_PROFILE_JSON"] = json.dumps(
        asdict(replace(RAG, endpoint="https://user:pass@example.test/v1")))
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        TrustedUserInitializationSettings.from_env(env)
    assert SECRET not in repr(error.value)
    assert "user:pass" not in str(error.value)


@pytest.mark.parametrize("key,value,code", [
    ("ISOLATED_IMPORT_SCHEMA", "cutover_combined_refs_20260930_0f7592dc3978",
     "ISOLATED_IMPORT_SCHEMA"),
    ("S3_BUCKET", "init-test-bucket", "ISOLATED_IMPORT_S3_BUCKET"),
    ("QDRANT_COLLECTION", "init_rag", "ISOLATED_IMPORT_COLLECTION_IDENTITY"),
])
def test_resource_only_rejects_retained_or_ordinary_collision(key, value, code):
    env = values()
    env[key] = value
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        TrustedUserInitializationSettings.from_env(env)
    assert_safe_error(error, code)


@pytest.mark.parametrize("changes,code", [
    ({"enabled": False}, "TRUSTED_USER_INITIALIZATION_ENABLED"),
    ({"schema": "public"}, "ISOLATED_IMPORT_SCHEMA"),
    ({"rag_profile": None}, "ISOLATED_IMPORT_PROFILE_OR_COLLECTION"),
    ({"pg_pool_min": "invalid"}, "ISOLATED_IMPORT_TIMING"),
])
def test_resource_only_direct_construction_validation(changes, code):
    settings = TrustedUserInitializationSettings.from_env(values())
    with pytest.raises(TrustedInitializationConfigurationError) as error:
        replace(settings, **changes)
    assert_safe_error(error, code)


def test_resource_record_is_pure_and_split_pool_validation():
    kwargs = parse_isolated_resources(values())
    assert isinstance(kwargs, dict)
    assert kwargs["schema"] == "init_test_schema"
    record = IsolatedResourceSettings(**kwargs)
    rag, episode = validate_isolated_resource_identity(record)
    assert (rag, episode) == (
        IndexIdentity("qdrant", "init_rag", RAG),
        IndexIdentity("qdrant", "init_episode", EPISODE))
    assert validate_isolated_resource_pool(record) is None
    assert SECRET not in repr(record)
    bad_pool = replace(record, pg_pool_min=0)
    # Construction and the identity phase must not pull pool validation ahead.
    assert validate_isolated_resource_identity(bad_pool) == (rag, episode)
    with pytest.raises(ResourceConfigurationError) as error:
        validate_isolated_resource_pool(bad_pool)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("changes,role,code", [
    ({"ISOLATED_IMPORT_DATABASE_URL": None}, "invalid", "ISOLATED_IMPORT_DATABASE_URL"),
    ({"ISOLATED_IMPORT_ENABLED": "0", "ISOLATED_IMPORT_DATABASE_URL": None},
     "api", "ISOLATED_IMPORT_ENABLED"),
    ({"ISOLATED_IMPORT_TRUSTED_USER_IDS": "bad",
      "ISOLATED_IMPORT_RAG_PROFILE_JSON": "{}"},
     "api", "ISOLATED_IMPORT_TRUSTED_USER_IDS"),
    ({"ISOLATED_IMPORT_RAG_PROFILE_JSON": "{}",
      "ISOLATED_IMPORT_PG_POOL_MIN": "bad"},
     "api", "ISOLATED_IMPORT_RAG_PROFILE_JSON"),
    ({"ISOLATED_IMPORT_PG_POOL_MIN": "bad",
      "ISOLATED_IMPORT_LEASE_SECONDS": "bad"},
     "api", "ISOLATED_IMPORT_PG_POOL_MIN"),
    ({"ISOLATED_IMPORT_PG_POOL_MIN": "bad"},
     "invalid", "ISOLATED_IMPORT_PG_POOL_MIN"),
])
def test_legacy_from_env_compound_invalid_priority(changes, role, code):
    env = legacy_values()
    for key, value in changes.items():
        if value is None:
            del env[key]
        else:
            env[key] = value
    with pytest.raises(IsolatedImportConfigurationError) as error:
        IsolatedImportSettings.from_env(role, env)
    assert_safe_error(error, code)


def test_legacy_direct_compound_invalid_priority():
    settings = IsolatedImportSettings.from_env("api", legacy_values())
    with pytest.raises(IsolatedImportConfigurationError) as error:
        replace(settings, role="invalid", database_url=42)
    assert_safe_error(error, "role")
    with pytest.raises(IsolatedImportConfigurationError) as error:
        replace(settings, trusted_user_ids=frozenset(), pg_pool_min=0)
    assert_safe_error(error, "ISOLATED_IMPORT_TRUSTED_USER_IDS")
    with pytest.raises(IsolatedImportConfigurationError) as error:
        replace(settings, trusted_user_ids=frozenset(), database_url=42)
    assert_safe_error(error, "ISOLATED_IMPORT_DATABASE_URL")
    with pytest.raises(IsolatedImportConfigurationError) as error:
        replace(settings, pg_pool_min=0)
    assert_safe_error(error, "ISOLATED_IMPORT_TIMING")
