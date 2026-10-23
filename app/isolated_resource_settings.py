"""Pure, shared resource validation for isolated imports and initializations."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import json
import re
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

from psycopg.conninfo import conninfo_to_dict

from hello_agents.memory.rag.embedding_profile import EmbeddingProfile
from hello_agents.memory.rag.index_identity import IndexIdentity


_SCHEMA = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_RETAINED_SCHEMA = "cutover_combined_refs_20260930_0f7592dc3978"
_RETAINED_BUCKET = "cutover-paired-bf20-69fcab0f0cbd4300a0545f2008c15779"
_PROFILE_FIELDS = {item.name for item in fields(EmbeddingProfile)}


class ResourceConfigurationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"Isolated resource configuration rejected: {code}")


def _reject(code: str) -> None:
    raise ResourceConfigurationError(code)


def _endpoint(value: str, key: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and not parsed.path.strip("/"))
        _ = parsed.port
    except Exception:
        valid = False
    if not valid or any(char.isspace() for char in value):
        _reject(key)


def _host(value: str) -> str:
    lowered = value.lower()
    return "loopback" if lowered in {"localhost", "127.0.0.1", "::1"} else lowered


def _connection_identity(value: str, key: str, *, isolated: bool) -> tuple[str, str, str, int, str | None]:
    if not isinstance(value, str):
        _reject(key)
    try:
        normalized = ("postgresql://" + value[len("postgresql+psycopg://"):]
                      if not isolated and value.startswith("postgresql+psycopg://") else value)
        parsed = urlsplit(normalized)
        if (parsed.scheme != "postgresql" or parsed.fragment or not parsed.hostname
                or parsed.path in {"", "/"} or any(char.isspace() for char in value)):
            raise ValueError()
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        identity_keys = {"host", "hostaddr", "port", "dbname", "options", "service", "servicefile"}
        if any(sum(1 for name, _ in pairs if name == key_name) > 1 for key_name in identity_keys):
            raise ValueError()
        if isolated and (len(pairs) != 1 or pairs[0][0] != "options"):
            raise ValueError()
        effective = conninfo_to_dict(normalized)
        if (effective.get("service") or effective.get("hostaddr")
                or any(name in {"service", "hostaddr", "servicefile"} for name, _ in pairs)):
            raise ValueError()
        host = effective.get("host")
        database = effective.get("dbname")
        port_text = effective.get("port") or "5432"
        if (not isinstance(host, str) or not host or "," in host
                or not isinstance(database, str) or not database
                or any(char in database for char in ("=", "\n", "\r"))
                or "://" in database or not str(port_text).isdigit()
                or "," in str(port_text) or not 1 <= int(port_text) <= 65535):
            raise ValueError()
        option = effective.get("options", "")
        if isolated:
            if not option.startswith("-csearch_path="):
                raise ValueError()
            schema = option[len("-csearch_path="):]
            if not _SCHEMA.fullmatch(schema):
                raise ValueError()
        elif option:
            match = re.fullmatch(r"-c\s*search_path=([a-z][a-z0-9_]{0,62})", option)
            if match is None:
                raise ValueError()
            schema = match.group(1)
        else:
            schema = None
        return _host(host), database, effective.get("user", ""), int(port_text), schema
    except Exception:
        _reject(key)


def _dsn(value: str, key: str) -> tuple[str, str, str, int, str]:
    return _connection_identity(value, key, isolated=True)  # type: ignore[return-value]


def _ordinary_dsn(value: str) -> tuple[str, str, str, int, str | None]:
    return _connection_identity(value, "DATABASE_URL", isolated=False)


def _profile(value: str, key: str) -> EmbeddingProfile:
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, dict) or set(parsed) != _PROFILE_FIELDS:
            raise ValueError()
        profile = EmbeddingProfile(**parsed)
        IndexIdentity("qdrant", "validation", profile)
        return profile
    except Exception:
        _reject(key)


def _integer(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default))
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw):
        _reject(name)
    return int(raw)


@dataclass(frozen=True)
class IsolatedResourceSettings:
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


def parse_isolated_resources(env: Mapping[str, str]) -> dict[str, object]:
    def required(name: str) -> str:
        value = env.get(name)
        if not isinstance(value, str) or not value:
            _reject(name)
        return value

    return dict(
        database_url=required("ISOLATED_IMPORT_DATABASE_URL"),
        schema=required("ISOLATED_IMPORT_SCHEMA"),
        s3_endpoint_url=required("ISOLATED_IMPORT_S3_ENDPOINT_URL"),
        s3_bucket=required("ISOLATED_IMPORT_S3_BUCKET"),
        qdrant_url=required("ISOLATED_IMPORT_QDRANT_URL"),
        rag_collection=required("ISOLATED_IMPORT_RAG_COLLECTION"),
        episode_collection=required("ISOLATED_IMPORT_EPISODE_COLLECTION"),
        rag_profile=_profile(required("ISOLATED_IMPORT_RAG_PROFILE_JSON"), "ISOLATED_IMPORT_RAG_PROFILE_JSON"),
        episode_profile=_profile(required("ISOLATED_IMPORT_EPISODE_PROFILE_JSON"), "ISOLATED_IMPORT_EPISODE_PROFILE_JSON"),
        s3_region=env.get("S3_REGION", "us-east-1"),
        s3_access_key_id=env.get("S3_ACCESS_KEY_ID") or None,
        s3_secret_access_key=env.get("S3_SECRET_ACCESS_KEY") or None,
        qdrant_api_key=env.get("QDRANT_API_KEY") or None,
        ordinary_database_url=env.get("DATABASE_URL"),
        ordinary_s3_bucket=env.get("S3_BUCKET") or None,
        ordinary_rag_collection=env.get("QDRANT_COLLECTION") or None,
        ordinary_episode_collection=env.get("EPISODE_QDRANT_COLLECTION") or None,
        pg_pool_min=_integer(env, "ISOLATED_IMPORT_PG_POOL_MIN", 1),
        pg_pool_max=_integer(env, "ISOLATED_IMPORT_PG_POOL_MAX", 10),
    )


def validate_isolated_resource_identity(resources: IsolatedResourceSettings) -> tuple[IndexIdentity, IndexIdentity]:
    if (not isinstance(resources.schema, str) or not _SCHEMA.fullmatch(resources.schema)
            or resources.schema in {"public", _RETAINED_SCHEMA}):
        _reject("ISOLATED_IMPORT_SCHEMA")
    identity = _dsn(resources.database_url, "ISOLATED_IMPORT_DATABASE_URL")
    if identity[-1] != resources.schema:
        _reject("ISOLATED_IMPORT_SCHEMA")
    for value, key in ((resources.s3_endpoint_url, "ISOLATED_IMPORT_S3_ENDPOINT_URL"),
                       (resources.qdrant_url, "ISOLATED_IMPORT_QDRANT_URL")):
        _endpoint(value, key)
    if (not isinstance(resources.s3_bucket, str) or not _BUCKET.fullmatch(resources.s3_bucket)
            or resources.s3_bucket in {_RETAINED_BUCKET, resources.ordinary_s3_bucket}):
        _reject("ISOLATED_IMPORT_S3_BUCKET")
    if bool(resources.s3_access_key_id) != bool(resources.s3_secret_access_key):
        _reject("S3_ACCESS_KEY_ID")
    if not isinstance(resources.s3_region, str) or not resources.s3_region.strip():
        _reject("S3_REGION")
    try:
        rag = IndexIdentity("qdrant", resources.rag_collection, resources.rag_profile)
        episode = IndexIdentity("qdrant", resources.episode_collection, resources.episode_profile)
    except Exception:
        _reject("ISOLATED_IMPORT_PROFILE_OR_COLLECTION")
    if (rag.base_collection == episode.base_collection
            or rag.physical_collection == episode.physical_collection
            or resources.rag_collection in {resources.ordinary_rag_collection, resources.ordinary_episode_collection}
            or resources.episode_collection in {resources.ordinary_rag_collection, resources.ordinary_episode_collection}):
        _reject("ISOLATED_IMPORT_COLLECTION_IDENTITY")
    if resources.ordinary_database_url is not None:
        ordinary = _ordinary_dsn(resources.ordinary_database_url)
        if ((ordinary[0], ordinary[1], ordinary[3]) ==
                (identity[0], identity[1], identity[3])
                and ordinary[4] in {None, identity[4]}):
            _reject("ISOLATED_IMPORT_DATABASE_IDENTITY")
    return rag, episode


def validate_isolated_resource_pool(resources: IsolatedResourceSettings) -> None:
    if (type(resources.pg_pool_min) is not int or resources.pg_pool_min < 1
            or type(resources.pg_pool_max) is not int or resources.pg_pool_max < 1
            or resources.pg_pool_min > resources.pg_pool_max):
        _reject("ISOLATED_IMPORT_TIMING")


def validate_isolated_resources(resources: IsolatedResourceSettings) -> tuple[IndexIdentity, IndexIdentity]:
    identities = validate_isolated_resource_identity(resources)
    validate_isolated_resource_pool(resources)
    return identities


BASE_RELATIONS = (
    "alembic_version", "users", "auth_sessions", "user_snapshots",
    "memory_documents", "import_batches", "import_tasks", "import_objects",
    "import_task_attempts", "import_user_schedule", "user_mutation_leases",
    "document_objects", "vector_indexes", "vector_heads", "vector_generations",
    "history_document_witnesses", "import_publication_evidence",
    "user_publication_gates", "generation_reservations",
    "import_publication_private_payloads", "import_publication_recovery_queue",
    "import_publication_recovery_leases",
    "import_publication_recovery_token_issuance",
    "import_publication_recovery_schedule",
)
REVIEWED_HEADS = {
    "20261007_16": BASE_RELATIONS,
    "20261022_17": BASE_RELATIONS + ("trusted_user_initializations",),
}

LEDGER_COLUMNS = {
    "operation_id": "uuid", "format_version": "integer", "user_id": "text",
    "username_key": "text", "resource_descriptor": "jsonb",
    "resource_descriptor_sha256": "text", "history_initial_sha256": "text",
    "memory_initial_sha256": "text", "rag_identity": "jsonb",
    "episode_identity": "jsonb", "rag_kind": "text", "episode_kind": "text",
    "rag_namespace": "text", "episode_namespace": "text",
    "rag_index_key": "text", "episode_index_key": "text",
    "rag_initial_generation_id": "uuid", "episode_initial_generation_id": "uuid",
    "rag_head_revision": "bigint", "episode_head_revision": "bigint",
    "rag_index_revision": "bigint", "episode_index_revision": "bigint",
    "history_snapshot_version": "bigint", "memory_snapshot_version": "bigint",
    "empty_manifest_sha256": "text", "history_document_evidence_sha256": "text",
    "completed_at": "timestamp with time zone",
}

_HEX = "'^[0-9a-f]{64}$'"
LEDGER_CONSTRAINTS = {
    "trusted_user_initializations_pkey": "primary key (operation_id)",
    "trusted_init_user_unique_17": "unique (user_id)",
    "trusted_init_user_fk_17": "foreign key (user_id) references users(id)",
    "trusted_init_rag_generation_fk_17": (
        "foreign key (rag_initial_generation_id,user_id,rag_kind,rag_namespace,rag_index_key) "
        "references vector_generations(generation_id,tenant_id,vector_kind,namespace,index_key)"),
    "trusted_init_episode_generation_fk_17": (
        "foreign key (episode_initial_generation_id,user_id,episode_kind,episode_namespace,episode_index_key) "
        "references vector_generations(generation_id,tenant_id,vector_kind,namespace,index_key)"),
    "trusted_init_rag_witness_fk_17": (
        "foreign key (user_id,rag_kind,rag_namespace,rag_index_key,rag_head_revision) "
        "references history_document_witnesses(tenant_id,vector_kind,namespace,index_key,head_revision)"),
    "trusted_init_format_ck_17": "check (format_version = 1)",
    "trusted_init_kinds_ck_17": (
        "check (rag_kind = 'rag' and episode_kind = 'episode' and "
        "length(rag_namespace) > 0 and length(episode_namespace) > 0)"),
    "trusted_init_initial_versions_ck_17": (
        "check (rag_head_revision = 1 and episode_head_revision = 1 and "
        "rag_index_revision = 1 and episode_index_revision = 1 and "
        "history_snapshot_version = 1 and memory_snapshot_version = 1)"),
    "trusted_init_digest_shape_ck_17": (
        "check (resource_descriptor_sha256 ~ " + _HEX +
        " and history_initial_sha256 ~ " + _HEX +
        " and memory_initial_sha256 ~ " + _HEX +
        " and empty_manifest_sha256 ~ " + _HEX +
        " and history_document_evidence_sha256 ~ " + _HEX +
        " and rag_index_key ~ " + _HEX +
        " and episode_index_key ~ " + _HEX + ")"),
    "trusted_init_descriptor_ck_17": """check (
        jsonb_typeof(resource_descriptor) = 'object'
        and resource_descriptor = jsonb_build_object(
            'format_version', 1,
            'postgres', jsonb_build_object(
                'host', resource_descriptor #> '{postgres,host}',
                'port', resource_descriptor #> '{postgres,port}',
                'database', resource_descriptor #> '{postgres,database}',
                'schema', resource_descriptor #> '{postgres,schema}'),
            's3', jsonb_build_object(
                'endpoint_url', resource_descriptor #> '{s3,endpoint_url}',
                'bucket', resource_descriptor #> '{s3,bucket}',
                'region', resource_descriptor #> '{s3,region}'),
            'qdrant', jsonb_build_object(
                'endpoint_url', resource_descriptor #> '{qdrant,endpoint_url}',
                'rag_identity', rag_identity,
                'episode_identity', episode_identity))
        and jsonb_typeof(resource_descriptor->'format_version') = 'number'
        and resource_descriptor->>'format_version' = '1'
        and jsonb_typeof(resource_descriptor#>'{postgres,host}') = 'string'
        and jsonb_typeof(resource_descriptor#>'{postgres,port}') = 'number'
        and (resource_descriptor#>>'{postgres,port}')::integer >= 1
        and (resource_descriptor#>>'{postgres,port}')::integer <= 65535
        and jsonb_typeof(resource_descriptor#>'{postgres,database}') = 'string'
        and jsonb_typeof(resource_descriptor#>'{postgres,schema}') = 'string'
        and jsonb_typeof(resource_descriptor#>'{s3,endpoint_url}') = 'string'
        and (resource_descriptor#>>'{s3,endpoint_url}') ~*
            '^https?://([a-z0-9._~%-]+|\\[[0-9a-f:.]+\\])(:[0-9]{1,5})?/?$'
        and jsonb_typeof(resource_descriptor#>'{s3,bucket}') = 'string'
        and jsonb_typeof(resource_descriptor#>'{s3,region}') = 'string'
        and jsonb_typeof(resource_descriptor#>'{qdrant,endpoint_url}') = 'string'
        and (resource_descriptor#>>'{qdrant,endpoint_url}') ~*
            '^https?://([a-z0-9._~%-]+|\\[[0-9a-f:.]+\\])(:[0-9]{1,5})?/?$'
        and jsonb_typeof(rag_identity) = 'object'
        and jsonb_typeof(episode_identity) = 'object'
        and jsonb_typeof(rag_identity#>'{profile,endpoint}') = 'string'
        and jsonb_typeof(episode_identity#>'{profile,endpoint}') = 'string'
        and (rag_identity#>>'{profile,endpoint}') !~ '[?#]'
        and (episode_identity#>>'{profile,endpoint}') !~ '[?#]'
        and ((rag_identity#>>'{profile,endpoint}') !~
                '^[A-Za-z][A-Za-z0-9+.-]*://'
             or substring(rag_identity#>>'{profile,endpoint}' from
                '^[A-Za-z][A-Za-z0-9+.-]*://([^/]*)') !~ '@')
        and ((rag_identity#>>'{profile,endpoint}') !~ '^//'
             or substring(rag_identity#>>'{profile,endpoint}' from
                '^//([^/]*)') !~ '@')
        and ((episode_identity#>>'{profile,endpoint}') !~
                '^[A-Za-z][A-Za-z0-9+.-]*://'
             or substring(episode_identity#>>'{profile,endpoint}' from
                '^[A-Za-z][A-Za-z0-9+.-]*://([^/]*)') !~ '@')
        and ((episode_identity#>>'{profile,endpoint}') !~ '^//'
             or substring(episode_identity#>>'{profile,endpoint}' from
                '^//([^/]*)') !~ '@')
        and resource_descriptor_sha256 =
            encode(sha256(convert_to(resource_descriptor::text,'UTF8')),'hex'))""",
}

# PostgreSQL 17.6 pg_get_constraintdef() output from the reviewed migration17.
# Preserve casts, grouping and quoted literals; only FK relation qualification
# can vary with the connection search_path. The migration DDL stays above.
LEDGER_CANONICAL_CONSTRAINTS = {
    'trusted_init_descriptor_ck_17': 'CHECK (((jsonb_typeof(resource_descriptor) = \'object\'::text) AND (resource_descriptor = jsonb_build_object(\'format_version\', 1, \'postgres\', jsonb_build_object(\'host\', (resource_descriptor #> \'{postgres,host}\'::text[]), \'port\', (resource_descriptor #> \'{postgres,port}\'::text[]), \'database\', (resource_descriptor #> \'{postgres,database}\'::text[]), \'schema\', (resource_descriptor #> \'{postgres,schema}\'::text[])), \'s3\', jsonb_build_object(\'endpoint_url\', (resource_descriptor #> \'{s3,endpoint_url}\'::text[]), \'bucket\', (resource_descriptor #> \'{s3,bucket}\'::text[]), \'region\', (resource_descriptor #> \'{s3,region}\'::text[])), \'qdrant\', jsonb_build_object(\'endpoint_url\', (resource_descriptor #> \'{qdrant,endpoint_url}\'::text[]), \'rag_identity\', rag_identity, \'episode_identity\', episode_identity))) AND (jsonb_typeof((resource_descriptor -> \'format_version\'::text)) = \'number\'::text) AND ((resource_descriptor ->> \'format_version\'::text) = \'1\'::text) AND (jsonb_typeof((resource_descriptor #> \'{postgres,host}\'::text[])) = \'string\'::text) AND (jsonb_typeof((resource_descriptor #> \'{postgres,port}\'::text[])) = \'number\'::text) AND (((resource_descriptor #>> \'{postgres,port}\'::text[]))::integer >= 1) AND (((resource_descriptor #>> \'{postgres,port}\'::text[]))::integer <= 65535) AND (jsonb_typeof((resource_descriptor #> \'{postgres,database}\'::text[])) = \'string\'::text) AND (jsonb_typeof((resource_descriptor #> \'{postgres,schema}\'::text[])) = \'string\'::text) AND (jsonb_typeof((resource_descriptor #> \'{s3,endpoint_url}\'::text[])) = \'string\'::text) AND ((resource_descriptor #>> \'{s3,endpoint_url}\'::text[]) ~* \'^https?://([a-z0-9._~%-]+|\\[[0-9a-f:.]+\\])(:[0-9]{1,5})?/?$\'::text) AND (jsonb_typeof((resource_descriptor #> \'{s3,bucket}\'::text[])) = \'string\'::text) AND (jsonb_typeof((resource_descriptor #> \'{s3,region}\'::text[])) = \'string\'::text) AND (jsonb_typeof((resource_descriptor #> \'{qdrant,endpoint_url}\'::text[])) = \'string\'::text) AND ((resource_descriptor #>> \'{qdrant,endpoint_url}\'::text[]) ~* \'^https?://([a-z0-9._~%-]+|\\[[0-9a-f:.]+\\])(:[0-9]{1,5})?/?$\'::text) AND (jsonb_typeof(rag_identity) = \'object\'::text) AND (jsonb_typeof(episode_identity) = \'object\'::text) AND (jsonb_typeof((rag_identity #> \'{profile,endpoint}\'::text[])) = \'string\'::text) AND (jsonb_typeof((episode_identity #> \'{profile,endpoint}\'::text[])) = \'string\'::text) AND ((rag_identity #>> \'{profile,endpoint}\'::text[]) !~ \'[?#]\'::text) AND ((episode_identity #>> \'{profile,endpoint}\'::text[]) !~ \'[?#]\'::text) AND (((rag_identity #>> \'{profile,endpoint}\'::text[]) !~ \'^[A-Za-z][A-Za-z0-9+.-]*://\'::text) OR ("substring"((rag_identity #>> \'{profile,endpoint}\'::text[]), \'^[A-Za-z][A-Za-z0-9+.-]*://([^/]*)\'::text) !~ \'@\'::text)) AND (((rag_identity #>> \'{profile,endpoint}\'::text[]) !~ \'^//\'::text) OR ("substring"((rag_identity #>> \'{profile,endpoint}\'::text[]), \'^//([^/]*)\'::text) !~ \'@\'::text)) AND (((episode_identity #>> \'{profile,endpoint}\'::text[]) !~ \'^[A-Za-z][A-Za-z0-9+.-]*://\'::text) OR ("substring"((episode_identity #>> \'{profile,endpoint}\'::text[]), \'^[A-Za-z][A-Za-z0-9+.-]*://([^/]*)\'::text) !~ \'@\'::text)) AND (((episode_identity #>> \'{profile,endpoint}\'::text[]) !~ \'^//\'::text) OR ("substring"((episode_identity #>> \'{profile,endpoint}\'::text[]), \'^//([^/]*)\'::text) !~ \'@\'::text)) AND (resource_descriptor_sha256 = encode(sha256(convert_to((resource_descriptor)::text, \'UTF8\'::name)), \'hex\'::text))))',
    'trusted_init_digest_shape_ck_17': "CHECK (((resource_descriptor_sha256 ~ '^[0-9a-f]{64}$'::text) AND (history_initial_sha256 ~ '^[0-9a-f]{64}$'::text) AND (memory_initial_sha256 ~ '^[0-9a-f]{64}$'::text) AND (empty_manifest_sha256 ~ '^[0-9a-f]{64}$'::text) AND (history_document_evidence_sha256 ~ '^[0-9a-f]{64}$'::text) AND (rag_index_key ~ '^[0-9a-f]{64}$'::text) AND (episode_index_key ~ '^[0-9a-f]{64}$'::text)))",
    'trusted_init_episode_generation_fk_17': 'FOREIGN KEY (episode_initial_generation_id, user_id, episode_kind, episode_namespace, episode_index_key) REFERENCES {schema}.vector_generations(generation_id, tenant_id, vector_kind, namespace, index_key)',
    'trusted_init_format_ck_17': 'CHECK ((format_version = 1))',
    'trusted_init_initial_versions_ck_17': 'CHECK (((rag_head_revision = 1) AND (episode_head_revision = 1) AND (rag_index_revision = 1) AND (episode_index_revision = 1) AND (history_snapshot_version = 1) AND (memory_snapshot_version = 1)))',
    'trusted_init_kinds_ck_17': "CHECK (((rag_kind = 'rag'::text) AND (episode_kind = 'episode'::text) AND (length(rag_namespace) > 0) AND (length(episode_namespace) > 0)))",
    'trusted_init_rag_generation_fk_17': 'FOREIGN KEY (rag_initial_generation_id, user_id, rag_kind, rag_namespace, rag_index_key) REFERENCES {schema}.vector_generations(generation_id, tenant_id, vector_kind, namespace, index_key)',
    'trusted_init_rag_witness_fk_17': 'FOREIGN KEY (user_id, rag_kind, rag_namespace, rag_index_key, rag_head_revision) REFERENCES {schema}.history_document_witnesses(tenant_id, vector_kind, namespace, index_key, head_revision)',
    'trusted_init_user_fk_17': 'FOREIGN KEY (user_id) REFERENCES {schema}.users(id)',
    'trusted_init_user_unique_17': 'UNIQUE (user_id)',
    'trusted_user_initializations_pkey': 'PRIMARY KEY (operation_id)',
}


EMPTY_LIST_SHA256 = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"

TRUSTED_INIT_GUARD_BODY = """
declare
    account_status text;
    account_username_key text;
    kind_name text;
    expected_identity jsonb;
    expected_namespace text;
    expected_key text;
    expected_generation uuid;
begin
    if tg_op <> 'INSERT' then
        raise exception 'TRUSTED_INITIALIZATION_IMMUTABLE';
    end if;
    if current_setting('transaction_isolation') <> 'read committed' then
        raise exception 'TRUSTED_INITIALIZATION_PROOF';
    end if;
    select status,username_key into account_status,account_username_key
      from users where id=new.user_id for update;
    if not found or account_status <> 'active'
       or account_username_key <> new.username_key then
        raise exception 'TRUSTED_INITIALIZATION_PROOF';
    end if;
    if new.resource_descriptor #>> '{postgres,database}' <> current_database()
       or new.resource_descriptor #>> '{postgres,schema}' <> current_schema()
       or current_setting('search_path') <> current_schema()
       or new.rag_namespace <> 'pdf_' || new.user_id
       or new.episode_namespace <> 'episodes'
       or new.empty_manifest_sha256 <> '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
    then raise exception 'TRUSTED_INITIALIZATION_PROOF'; end if;
    if not exists (
        select 1 from user_snapshots s where s.user_id=new.user_id
          and s.kind='history' and s.version=1
          and s.payload=jsonb_build_object('documents','[]'::jsonb,
              'questions','[]'::jsonb,'notes','[]'::jsonb,'sessions','[]'::jsonb)
          and new.history_snapshot_version=s.version
          and new.history_initial_sha256=
              encode(sha256(convert_to(s.payload::text,'UTF8')),'hex'))
       or not exists (
        select 1 from user_snapshots s where s.user_id=new.user_id
          and s.kind='memory' and s.version=1
          and s.payload=jsonb_build_object('user_id',new.user_id,'memories','[]'::jsonb)
          and new.memory_snapshot_version=s.version
          and new.memory_initial_sha256=
              encode(sha256(convert_to(s.payload::text,'UTF8')),'hex'))
    then raise exception 'TRUSTED_INITIALIZATION_PROOF'; end if;
    for kind_name in select unnest(array['rag','episode']) loop
        if kind_name='rag' then
            expected_identity := new.rag_identity;
            expected_namespace := new.rag_namespace;
            expected_key := new.rag_index_key;
            expected_generation := new.rag_initial_generation_id;
        else
            expected_identity := new.episode_identity;
            expected_namespace := new.episode_namespace;
            expected_key := new.episode_index_key;
            expected_generation := new.episode_initial_generation_id;
        end if;
        if not exists (
            select 1 from vector_indexes i
            join vector_generations g on g.tenant_id=i.tenant_id
              and g.vector_kind=i.vector_kind and g.namespace=i.namespace
              and g.index_key=i.index_key
            join vector_heads h on h.tenant_id=i.tenant_id
              and h.vector_kind=i.vector_kind and h.namespace=i.namespace
              and h.index_key=i.index_key
            where i.tenant_id=new.user_id and i.vector_kind=kind_name
              and i.namespace=expected_namespace and i.index_key=expected_key
              and i.identity=expected_identity and i.index_revision=1
              and g.generation_id=expected_generation
              and g.state='published' and g.expected_count=0
              and g.content_digest=new.empty_manifest_sha256
              and g.base_revision is null and g.index_revision=1
              and g.publication_revision=1 and g.publication_snapshot_version=1
              and h.revision=1 and h.index_revision=1 and h.snapshot_version=1
              and h.generation_id is null
              and h.last_generation_id=expected_generation)
        then raise exception 'TRUSTED_INITIALIZATION_PROOF'; end if;
    end loop;
    if not exists (
        select 1 from history_document_witnesses w
        where w.tenant_id=new.user_id and w.vector_kind='rag'
          and w.namespace=new.rag_namespace and w.index_key=new.rag_index_key
          and w.head_revision=1 and w.index_revision=1
          and w.publication_snapshot_version=1 and w.document_count=0
          and w.last_generation_id=new.rag_initial_generation_id
          and w.documents_sha256=new.history_document_evidence_sha256
          and w.documents_sha256=new.empty_manifest_sha256)
       or (select count(*) from vector_indexes where tenant_id=new.user_id) <> 2
       or (select count(*) from vector_heads where tenant_id=new.user_id) <> 2
       or (select count(*) from vector_generations where tenant_id=new.user_id) <> 2
       or (select count(*) from history_document_witnesses where tenant_id=new.user_id) <> 1
       or exists(select 1 from memory_documents where user_id=new.user_id)
       or exists(select 1 from document_objects where user_id=new.user_id)
       or exists(select 1 from import_batches where user_id=new.user_id)
       or exists(select 1 from import_tasks where user_id=new.user_id)
       or exists(select 1 from import_objects where user_id=new.user_id)
       or exists(select 1 from import_publication_evidence where user_id=new.user_id)
       or exists(select 1 from import_publication_private_payloads where user_id=new.user_id)
       or exists(select 1 from user_publication_gates where user_id=new.user_id)
    then raise exception 'TRUSTED_INITIALIZATION_PROOF'; end if;
    new.completed_at := clock_timestamp();
    return new;
end;
"""


def _constraint_definition_matches(name: str, definition: str, schema: str) -> bool:
    expected = LEDGER_CANONICAL_CONSTRAINTS[name]
    if "_fk_" not in name:
        return definition == expected
    # pg_get_constraintdef qualifies the same relation when it is not visible
    # through search_path. These are the only two accepted PG17 displays.
    return definition in (expected.replace("{schema}", schema),
                          expected.replace("{schema}.", ""))


def schema_catalog_ready(cursor, schema: str, database_name: str,
                         *, allowed_heads: set[str]) -> bool:
    if not isinstance(schema, str) or not _SCHEMA.fullmatch(schema):
        return False
    names = [f"{schema}.{name}" for name in BASE_RELATIONS]
    found = cursor.execute(
        "select to_regclass(relation.name) is not null as present "
        "from unnest(%s::text[]) as relation(name)", (names,)).fetchall()
    if len(found) != len(BASE_RELATIONS) or not all(row["present"] for row in found):
        return False
    # _SCHEMA excludes quotes; quoting also handles a reserved-word schema name.
    row = cursor.execute(f"""select current_database() as database_name,
        current_schema() as schema_name,current_setting('search_path') as search_path,
        (select count(*) from "{schema}".alembic_version) as migration_count,
        (select min(version_num) from "{schema}".alembic_version) as migration_head""").fetchone()
    if (row is None or row["database_name"] != database_name
            or row["schema_name"] != schema or row["search_path"] != schema
            or row["migration_count"] != 1 or row["migration_head"] not in allowed_heads
            or row["migration_head"] not in REVIEWED_HEADS):
        return False
    if row["migration_head"] == "20261007_16":
        return True
    extra = cursor.execute(
        "select to_regclass(%s) as ledger", (f"{schema}.trusted_user_initializations",)
    ).fetchone()
    if extra is None or extra["ledger"] is None:
        return False
    relation = cursor.execute("""select c.oid,c.relkind from pg_class c
        join pg_namespace n on n.oid=c.relnamespace
        where n.nspname=%s and c.relname='trusted_user_initializations'""",
        (schema,)).fetchone()
    if relation is None or relation["relkind"] != "r":
        return False
    oid = relation["oid"]
    columns = cursor.execute("""select a.attnum,a.attname as name,
        format_type(a.atttypid,a.atttypmod) as type,
        a.attnotnull as not_null,
        pg_get_expr(d.adbin,d.adrelid) as default_expr
        from pg_attribute a left join pg_attrdef d
          on d.adrelid=a.attrelid and d.adnum=a.attnum
        where a.attrelid=%s and a.attnum>0 and not a.attisdropped""",
        (oid,)).fetchall()
    if len(columns) != len(LEDGER_COLUMNS):
        return False
    for column in columns:
        name = column["name"]
        if (name not in LEDGER_COLUMNS or column["type"] != LEDGER_COLUMNS[name]
                or column["not_null"] is not True
                or column["default_expr"] != ("clock_timestamp()" if name == "completed_at" else None)):
            return False
    constraints = cursor.execute("""select oid,conname,contype,convalidated,confrelid,
        conkey,confkey,
        condeferrable,condeferred,pg_get_constraintdef(oid) as definition
        from pg_constraint where conrelid=%s""", (oid,)).fetchall()
    if {row["conname"] for row in constraints} != set(LEDGER_CONSTRAINTS):
        return False
    for constraint in constraints:
        name = constraint["conname"]
        expected_type = ("p" if name.endswith("_pkey") else
                         "u" if name == "trusted_init_user_unique_17" else
                         "f" if "_fk_" in name else "c")
        if (constraint["contype"] != expected_type
                or constraint["convalidated"] is not True
                or constraint["condeferrable"] is not False
                or constraint["condeferred"] is not False
                or not _constraint_definition_matches(
                    name, constraint["definition"], schema)):
            return False
    fk_keys = {
        "trusted_init_user_fk_17": (
            "users", ("user_id",), ("id",)),
        "trusted_init_rag_generation_fk_17": (
            "vector_generations",
            ("rag_initial_generation_id", "user_id", "rag_kind",
             "rag_namespace", "rag_index_key"),
            ("generation_id", "tenant_id", "vector_kind",
             "namespace", "index_key")),
        "trusted_init_episode_generation_fk_17": (
            "vector_generations",
            ("episode_initial_generation_id", "user_id", "episode_kind",
             "episode_namespace", "episode_index_key"),
            ("generation_id", "tenant_id", "vector_kind",
             "namespace", "index_key")),
        "trusted_init_rag_witness_fk_17": (
            "history_document_witnesses",
            ("user_id", "rag_kind", "rag_namespace", "rag_index_key",
             "rag_head_revision"),
            ("tenant_id", "vector_kind", "namespace", "index_key",
             "head_revision")),
    }
    child_columns = {row["attnum"]: row["name"] for row in columns}
    fk_constraints = {row["oid"]: row for row in constraints if row["contype"] == "f"}
    if len(fk_constraints) != len(fk_keys):
        return False
    fk_triggers = cursor.execute("""select t.tgconstraint,t.tgrelid,t.tgtype,
        t.tgisinternal,t.tgenabled,t.tgqual is null as no_when,
        t.tgnargs,t.tgargs,t.tgdeferrable,t.tginitdeferred,
        p.proname,n.nspname as function_schema
        from pg_trigger t join pg_proc p on p.oid=t.tgfoid
        join pg_namespace n on n.oid=p.pronamespace
        where t.tgconstraint in
          (select c.oid from pg_constraint c where c.conrelid=%s and c.contype='f')""",
        (oid,)).fetchall()
    for fk_oid, fk in fk_constraints.items():
        parent_name, child_keys, parent_keys = fk_keys[fk["conname"]]
        parent = cursor.execute("""select c.oid as relation from pg_class c
            join pg_namespace n on n.oid=c.relnamespace
            where n.nspname=%s and c.relname=%s""",
            (schema, parent_name)).fetchone()
        if (parent is None or parent["relation"] is None
                or fk["confrelid"] != parent["relation"]):
            return False
        parent_columns = cursor.execute("""select attnum,attname as name
            from pg_attribute where attrelid=%s and attnum>0
              and not attisdropped""", (parent["relation"],)).fetchall()
        parent_by_number = {row["attnum"]: row["name"] for row in parent_columns}
        if (tuple(child_columns.get(number) for number in (fk["conkey"] or ())) != child_keys
                or tuple(parent_by_number.get(number) for number in
                         (fk["confkey"] or ())) != parent_keys):
            return False
        expected = {
            (oid, 5, "RI_FKey_check_ins"),
            (oid, 17, "RI_FKey_check_upd"),
            (fk["confrelid"], 9, "RI_FKey_noaction_del"),
            (fk["confrelid"], 17, "RI_FKey_noaction_upd"),
        }
        actual = [row for row in fk_triggers if row["tgconstraint"] == fk_oid]
        if (len(actual) != 4
                or {(row["tgrelid"], row["tgtype"], row["proname"])
                    for row in actual} != expected):
            return False
        for trigger in actual:
            if (trigger["tgisinternal"] is not True
                    or trigger["tgenabled"] != "O"
                    or trigger["no_when"] is not True
                    or trigger["tgnargs"] != 0
                    or bytes(trigger["tgargs"]) != b""
                    or trigger["tgdeferrable"] is not False
                    or trigger["tginitdeferred"] is not False
                    or trigger["function_schema"] != "pg_catalog"):
                return False
    triggers = cursor.execute("""select t.tgname,t.tgenabled,t.tgtype,
        t.tgisinternal,t.tgqual is null as no_when,t.tgnargs,t.tgargs,
        t.tgconstraint,t.tgdeferrable,t.tginitdeferred,
        p.proname,p.prosrc,p.pronargs,p.prosecdef,p.provolatile,
        l.lanname,n.nspname as function_schema
        from pg_trigger t join pg_proc p on p.oid=t.tgfoid
        join pg_language l on l.oid=p.prolang
        join pg_namespace n on n.oid=p.pronamespace
        where t.tgrelid=%s and not t.tgisinternal""", (oid,)).fetchall()
    return (len(triggers) == 1
            and triggers[0]["tgname"] == "trusted_init_guard_17"
            and triggers[0]["tgenabled"] == "O"
            and triggers[0]["tgtype"] == 31
            and triggers[0]["no_when"] is True
            and triggers[0]["tgnargs"] == 0
            and bytes(triggers[0]["tgargs"]) == b""
            and triggers[0]["tgconstraint"] == 0
            and triggers[0]["tgdeferrable"] is False
            and triggers[0]["tginitdeferred"] is False
            and triggers[0]["proname"] == "trusted_init_insert_guard_17"
            and triggers[0]["pronargs"] == 0
            and triggers[0]["prosecdef"] is False
            and triggers[0]["provolatile"] == "v"
            and triggers[0]["lanname"] == "plpgsql"
            and triggers[0]["function_schema"] == schema
            and triggers[0]["prosrc"] == TRUSTED_INIT_GUARD_BODY)
