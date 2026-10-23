"""Pure settings and read-only schema gate for first-user initialization."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import os
from typing import Mapping
from urllib.parse import urlsplit

from app.isolated_resource_settings import (
    IsolatedResourceSettings, ResourceConfigurationError, _dsn,
    parse_isolated_resources, schema_catalog_ready, validate_isolated_resources,
)
from app.postgres import PostgresDatabase
from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
from hello_agents.memory.rag.index_identity import IndexIdentity


class TrustedInitializationConfigurationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Trusted initialization configuration rejected: {code}")


def _reject(code: str) -> None:
    raise TrustedInitializationConfigurationError(code) from None


@dataclass(frozen=True)
class TrustedUserInitializationSettings:
    enabled: bool
    database_url: str = field(repr=False)
    schema: str
    s3_endpoint_url: str = field(repr=False)
    s3_bucket: str
    qdrant_url: str = field(repr=False)
    rag_collection: str
    episode_collection: str
    rag_profile: EmbeddingProfile = field(repr=False)
    episode_profile: EmbeddingProfile = field(repr=False)
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = field(default=None, repr=False)
    s3_secret_access_key: str | None = field(default=None, repr=False)
    qdrant_api_key: str | None = field(default=None, repr=False)
    ordinary_database_url: str | None = field(default=None, repr=False)
    ordinary_s3_bucket: str | None = field(default=None, repr=False)
    ordinary_rag_collection: str | None = field(default=None, repr=False)
    ordinary_episode_collection: str | None = field(default=None, repr=False)
    pg_pool_min: int = 1
    pg_pool_max: int = 10

    def __post_init__(self) -> None:
        if self.enabled is not True:
            _reject("TRUSTED_USER_INITIALIZATION_ENABLED")
        try:
            resource_values = {item.name: getattr(self, item.name)
                               for item in fields(IsolatedResourceSettings)}
            validate_isolated_resources(IsolatedResourceSettings(**resource_values))
        except ResourceConfigurationError as error:
            _reject(error.code)
        for profile in (self.rag_profile, self.episode_profile):
            endpoint = profile.endpoint
            if endpoint:
                try:
                    parsed = urlsplit(endpoint)
                    if (parsed.username is not None or parsed.password is not None
                            or parsed.query or parsed.fragment):
                        _reject("ISOLATED_IMPORT_PROFILE_OR_COLLECTION")
                except TrustedInitializationConfigurationError:
                    raise
                except Exception:
                    _reject("ISOLATED_IMPORT_PROFILE_OR_COLLECTION")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TrustedUserInitializationSettings:
        values = os.environ if env is None else env
        if values.get("TRUSTED_USER_INITIALIZATION_ENABLED") != "1":
            _reject("TRUSTED_USER_INITIALIZATION_ENABLED")
        try:
            resources = parse_isolated_resources(values)
        except ResourceConfigurationError as error:
            _reject(error.code)
        return cls(enabled=True, **resources)

    @property
    def rag_identity(self) -> IndexIdentity:
        return IndexIdentity("qdrant", self.rag_collection, self.rag_profile)

    @property
    def episode_identity(self) -> IndexIdentity:
        return IndexIdentity("qdrant", self.episode_collection, self.episode_profile)

    def resource_descriptor(self) -> dict[str, object]:
        host, database, _, port, _ = _dsn(self.database_url, "ISOLATED_IMPORT_DATABASE_URL")
        return {
            "format_version": 1,
            "postgres": {"host": host, "port": port, "database": database,
                         "schema": self.schema},
            "s3": {"endpoint_url": self.s3_endpoint_url, "bucket": self.s3_bucket,
                   "region": self.s3_region},
            "qdrant": {"endpoint_url": self.qdrant_url,
                       "rag_identity": self.rag_identity.to_dict(),
                       "episode_identity": self.episode_identity.to_dict()},
        }


def require_initialization_schema(database: PostgresDatabase, schema: str,
                                  database_name: str) -> None:
    try:
        with database.transaction() as cursor:
            if not schema_catalog_ready(cursor, schema, database_name,
                                        allowed_heads={"20261022_17"}):
                _reject("TRUSTED_INITIALIZATION_SCHEMA")
    except TrustedInitializationConfigurationError:
        raise
    except Exception:
        _reject("TRUSTED_INITIALIZATION_SCHEMA")
