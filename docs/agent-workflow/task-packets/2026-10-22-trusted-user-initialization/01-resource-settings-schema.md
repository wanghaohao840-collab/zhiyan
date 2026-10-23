---
id: "2026-10-22-trusted-user-initialization-01"
title: "Resource-only initialization settings and immutable schema17 completion proof"
status: "done"
parallel-safe: false
depends-on: []
base-commit: "e24deebb2b06d0b37f3c9dc516fcc4771e4a1e21"
owner: "Sol High implementation; Astra High independent acceptance; root execution"
---

# Task Packet: resource settings and schema17 completion proof

## Goal

Deliver shared pure resource validation, first-user resource-only settings, immutable completed-only migration17 evidence, and strict reviewed 16/17 runtime recognition. The initializer schema predicate accepts 17 alone. This is a testable settings/schema increment; it creates no deployed account or baseline.

## Non-goals

No account service, vector-pair method, CLI, public registration, ordinary bootstrap, historical repair, source-Qdrant access, retained-target upgrade, provider write, production operation, main merge or Git delivery. Draft does not authorize implementation.

## Delivery context

Task07 source and six-document delivery are accepted through base e24deeb. Current IsolatedImportSettings requires role api/worker, ISOLATED_IMPORT_ENABLED=1 and exactly two canonical trusted UUIDs; first-user initialization cannot require those IDs to exist. Later packets atomically create the new user and two empty baselines in one PG transaction. This packet freezes the resource and ledger format. A missing ledger or empty table never proves a historical account initialized. The design/plan received independent Astra N2 acceptance (report SHA256 27ce855cce6b9cbc8467bcc9036896739751c6b84b7dfc9282c74496846aa722), but that was not packet READY acceptance.

## Relevant files and current interfaces

- app/isolated_import_runtime.py:35-160: private DSN, schema, bucket, endpoint, profile and integer validators; IsolatedImportConfigurationError has a code and sanitized message.
- app/isolated_import_runtime.py:163-290: frozen IsolatedImportSettings fields, direct-construction validation, from_env(role, env=None), role/flag/two-ID/timing rules. tests/test_isolated_import_runtime.py:25-168 and 294-361 constrain old errors and schema behavior.
- app/isolated_import_runtime.py:37-51,305-323: exact head 20261007_16, 24 fixed relations, DB/schema/search_path/version gate. Existing fake-cursor head16 test returns 24 relation results.
- app/postgres.py:61-76: transaction yields dict-row cursor and commits/rolls back. Catalog validation issues read-only SQL on that cursor.
- hello_agents/memory/rag/index_identity.py:34-83: IndexIdentity.to_dict has full profile, base/physical collection and fingerprint. app/postgres_vector_generations.py:68-101 defines VectorScope.index_key from its existing Python canonical identity serialization; this differs from the ledger's PG JSONB digest.
- migrations/versions/20260929_10_vector_generations.py:11-80: generation unique (generation_id,tenant_id,vector_kind,namespace,index_key), plus registered index and head. migrations/versions/20260929_11_vector_publication_receipts.py:33-75 forbids direct published insert and retains published/retired receipt fields.
- migrations/versions/20260930_13_history_document_witnesses.py:9-65: witness PK (tenant_id,vector_kind,namespace,index_key,head_revision), actual receipt guard and immutability. app/postgres_history_document_witnesses.py:48-63 supplies original empty document_evidence.
- migrations/versions/20261007_16_publication_dependency_isolation.py:5-8: exact ancestor; all prior migration files stay untouched.
- app/history.py:12 and app/postgres_snapshots.py:106-135: first History value is {"documents":[],"questions":[],"notes":[],"sessions":[]}; first Memory value is {"user_id":new UUID string,"memories":[]}; expected_version=0 produces v1.
- tests/integration/test_postgres_schema.py:39-48 tests an older business migration and is regression, not new head17 proof.
- Preserve raw bytes of 15 pre-existing dirty paths: app/coordination.py, app/document_library.py, app/history.py, app/import_models.py, app/import_worker.py, app/memory_repository.py, app/qa_deletion.py, app/qa_persistence.py, app/qa_repository.py, app/recovery.py, migrations/versions/20260926_01_business_schema.sql, migrations/versions/20260928_09_import_leases.py, tests/conftest.py, tests/test_import_worker.py, tests/test_user_mutation_coordination.py.

## Prerequisites

### Packet dependencies

None; 02–05 consume this packet in serial order. Independent Astra review of this exact packet and root GO are assignment gates.

### Repository/base state

Base e24deebb2b06d0b37f3c9dc516fcc4771e4a1e21. At drafting, the 15 paths above are dirty and five owned Create paths absent. Ignored init-t1-baseline-root-n1.json SHA256 3784cbd4890c6b615291f4ce5f7e84e89a5e191840f984e629b9054e63b0ee5c records 431 source hashes: 429 tracked paths and two ignored process helpers, plus 15 raw dirty hashes. Recheck before worker edits; do not reset, normalize or stage those paths.

### External prerequisites

Pure settings tests need none. Actual migration/schema tests need only the independently attested dedicated PostgreSQL service, a generated disposable schema, POSTGRES_TEST_URL via the reviewed private runner and D:\python_self_agent\venv\Scripts\python.exe. This packet's runner injects only POSTGRES_TEST_URL, obtained from that dedicated PG container; S3/Qdrant fixture values belong to later packets and are not needed here. Root owns current service metadata/health, sole serial pytest lane, finite runner review and a separate GO for every stage. Dedicated PG identity at baseline: 1223c178a3381daf0a26616d761150e8ff2287ff5ba3cd27525abf0ade7a4fe7; reattest port and volume. No credential in argv/log/JUnit/docs. Never use retained/source endpoints, schemas or buckets.

## Explicit change boundary

### Allowed files: exhaustive seven

- Create app/isolated_resource_settings.py: shared pure resource record/parser/validator.
- Create app/trusted_user_initialization_settings.py: first-user settings and 17-only predicate.
- Create tests/test_trusted_user_initialization_settings.py: full settings contract.
- Create migrations/versions/20261022_17_trusted_user_initializations.py: additive ledger DDL/guard/downgrade.
- Create tests/integration/test_trusted_initialization_schema.py: disposable PG contract.
- Modify app/isolated_import_runtime.py: shared resource and schema seams only.
- Modify tests/test_isolated_import_runtime.py: affected assertions only, retaining old assertions.

### Allowed behavior changes

Shared pure validation; TRUSTED_USER_INITIALIZATION_ENABLED=1 without role/trusted IDs; immutable completed-only schema17; runtime exact16/17 and initializer exact17 gates. No I/O during import or settings construction.

### Forbidden changes

All other paths, especially prior migrations, 15 dirty paths, app/postgres_vector_generations.py, app/postgres_auth.py, tests/conftest.py, API routes, ordinary bootstrap, provider config and manifests. Keep original roles, allowlist count, signatures, timing, errors and public behavior. No pgcrypto/dependency, datasource provisioning, production/historical mutation, fixture-to-product import or service start. If an eighth implementation file is necessary, report reality conflict.

## Interface contract

### Shared pure resource module

app/isolated_resource_settings.py produces:

1. ResourceConfigurationError(ValueError), public code:str and code-only string/repr; never include an untrusted value.
2. Frozen IsolatedResourceSettings with fields database_url:str, schema:str, s3_endpoint_url:str, s3_bucket:str, qdrant_url:str, rag_collection:str, episode_collection:str, rag_profile:EmbeddingProfile, episode_profile:EmbeddingProfile, s3_region:str="us-east-1", s3_access_key_id:str|None=None, s3_secret_access_key:str|None=None, qdrant_api_key:str|None=None, ordinary_database_url:str|None=None, ordinary_s3_bucket:str|None=None, ordinary_rag_collection:str|None=None, ordinary_episode_collection:str|None=None, pg_pool_min:int=1, pg_pool_max:int=10. URLs, profiles and credentials have repr=False. No role, enabled, trusted IDs or worker timing.
3. parse_isolated_resources(env:Mapping[str,str])->dict[str,object] of keyword arguments for the shared resource fields. Required keys, in their existing `from_env` evaluation order: ISOLATED_IMPORT_DATABASE_URL, SCHEMA, S3_ENDPOINT_URL, S3_BUCKET, QDRANT_URL, RAG_COLLECTION, EPISODE_COLLECTION, RAG_PROFILE_JSON, EPISODE_PROFILE_JSON (all with ISOLATED_IMPORT_ prefix). Optional S3_REGION, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, QDRANT_API_KEY, DATABASE_URL, S3_BUCKET, QDRANT_COLLECTION, EPISODE_QDRANT_COLLECTION, ISOLATED_IMPORT_PG_POOL_MIN/MAX. Missing/invalid keys use the old setting-key error code. The parser executes required/profile/pool-integer conversions in the old order but returns raw field kwargs; it does not construct or semantically validate an IsolatedResourceSettings record. Thus old `from_env` can pass one parsed kwargs bundle into its original class constructor, then run direct validation once at the original `__post_init__` boundary.
4. validate_isolated_resource_identity(resources:IsolatedResourceSettings)->tuple[IndexIdentity,IndexIdentity], RAG then episode, performs the existing common schema/DSN/endpoint/bucket/credentials/profile/collection/ordinary identity sequence only. validate_isolated_resource_pool(resources:IsolatedResourceSettings)->None checks positive integer pool sizes and min<=max separately. validate_isolated_resources(resources:IsolatedResourceSettings)->tuple[IndexIdentity,IndexIdentity] calls identity then pool for the new resource-only settings. These narrowly split helpers remain in this one allowed shared module. Preserve exact isolated options=-csearch_path=<schema>, single effective DB host/port/name, ordinary identity separation, retained schema/bucket exclusion, paired credentials, endpoint no userinfo/query/fragment/non-root path, distinct base and physical collections, complete profile checks, ordinary DSN ambiguity refusal and loopback normalization. No network.

Original IsolatedImportSettings retains exact field order/defaults, from_env(role,env=None), IsolatedImportConfigurationError.code/message, role/enable/two canonical distinct UUID requirements and timing. Direct construction keeps its `__post_init__` priority: role, enabled, shared identity sequence, trusted-ID type/count/canonical UUIDs, then combined pool/timing validation (one `ISOLATED_IMPORT_TIMING` code). Build the shared record from the original fields only at this semantic stage, call only `validate_isolated_resource_identity` at the old common-validation position, and call `validate_isolated_resource_pool` only after trusted IDs, translating a pool failure to `ISOLATED_IMPORT_TIMING`. The record has no eager semantic `__post_init__`; do not call its combined validator before the trusted-ID check. `from_env` keeps its different existing order: enabled flag; required raw trusted IDs and raw two-ID shape; required resource values and both profiles in constructor argument-evaluation order; optional values; `_integer` for pool min/max and each timing field in their original order; only then `__post_init__` role/identity/trusted-ID/timing validation. Use the shared raw-kwargs parser only after raw trusted IDs and before the original timing `_integer` calls; build the old class once and validate at its normal `__post_init__` boundary. A bad role plus missing database URL must still report `ISOLATED_IMPORT_DATABASE_URL`, while direct construction with bad role plus bad resource reports `role`, and direct construction with invalid trusted IDs plus invalid pool reports `ISOLATED_IMPORT_TRUSTED_USER_IDS`. Add differential compound-invalid cases for these and disabled flag plus missing resources, malformed raw IDs plus malformed profile, malformed profile plus bad pool integer, and bad pool integer plus bad lease integer; assert the original code and sanitized string/repr. Place the shared exact-head/catalog predicate in app/isolated_resource_settings.py or another existing allowed module, with a dependency direction that permits both settings modules to call it; trusted_user_initialization_settings.py must not import isolated_import_runtime.py, which itself imports the shared resource module.

### Resource-only initializer settings

app/trusted_user_initialization_settings.py produces:

- TrustedInitializationConfigurationError(ValueError) with code:str, code-only str/repr, no raw exception chain.
- Frozen TrustedUserInitializationSettings with enabled:bool and every IsolatedResourceSettings field at identical types/defaults, but no role, trusted_user_ids, lease/heartbeat/attempt/provider timeout/retry/stop fields. URLs/profiles/credentials repr=False. Direct construction requires enabled is True and shared validation. from_env(env:Mapping[str,str]|None=None)->TrustedUserInitializationSettings requires exactly TRUSTED_USER_INITIALIZATION_ENABLED="1", calls the shared parser, and ignores ISOLATED_IMPORT_ENABLED and ISOLATED_IMPORT_TRUSTED_USER_IDS. Translate shared errors by code. No account lookup or network.
- rag_identity:IndexIdentity and episode_identity:IndexIdentity return validated qdrant identities.
- resource_descriptor()->dict[str,object] has exactly format_version=1; postgres={"host":normalized effective host,"port":integer effective port,"database":effective DB name,"schema":schema}; s3={"endpoint_url":validated S3 endpoint,"bucket":bucket,"region":region}; qdrant={"endpoint_url":validated Qdrant endpoint,"rag_identity":rag_identity.to_dict(),"episode_identity":episode_identity.to_dict()}. No extra keys. Full identity includes base/physical collection and full profile. Exclude DSN userinfo/password/query, S3/Qdrant keys, ordinary fields and pool sizing. Reject secret-bearing profile endpoints (URL userinfo/query/fragment) for this class before serializing; original settings acceptance stays unchanged. Canary tests compare full dictionary, repr, exception and JSON.
- require_initialization_schema(database:PostgresDatabase,schema:str,database_name:str)->None, read-only through database.transaction, accepts exact17/catalog contract below, raises TrustedInitializationConfigurationError("TRUSTED_INITIALIZATION_SCHEMA") on mismatch without raw SQL/DSN chaining. Reuse the shared catalog predicate; do not import isolated_import_runtime.py or open another database. No upgrade/create.

### Migration17 completed ledger

Revision 20261022_17, down_revision 20261007_16, no branch/dependencies. Create only trusted_user_initializations, function trusted_init_insert_guard_17(), and trigger trusted_init_guard_17. No earlier row modified. Every column NOT NULL; only completed_at has DEFAULT clock_timestamp(); all others caller supplied:

| Column | Type | Constraint |
| --- | --- | --- |
| operation_id | uuid | PK |
| format_version | integer | 1 |
| user_id | text | unique FK users(id) |
| username_key | text | equals users.username_key at insert |
| resource_descriptor | jsonb | exact format1 nonsecret descriptor above |
| resource_descriptor_sha256 | text | server JSONB digest |
| history_initial_sha256, memory_initial_sha256 | text each | server digest of exact v1 initial values |
| rag_identity, episode_identity | jsonb each | full IndexIdentity.to_dict, matches descriptor and registered indexes |
| rag_kind, episode_kind | text each | rag / episode |
| rag_namespace, episode_namespace | text each | nonempty original namespaces |
| rag_index_key, episode_index_key | text each | lowercase 64-hex VectorScope keys |
| rag_initial_generation_id, episode_initial_generation_id | uuid each | real initial receipt IDs |
| rag_head_revision, episode_head_revision | bigint each | 1 |
| rag_index_revision, episode_index_revision | bigint each | 1 |
| history_snapshot_version, memory_snapshot_version | bigint each | 1 |
| empty_manifest_sha256 | text | original vector SHA256([]) |
| history_document_evidence_sha256 | text | original document_evidence(user_id,EMPTY_HISTORY).digest |
| completed_at | timestamptz | DB clock |

Names: trusted_user_initializations_pkey; trusted_init_user_unique_17; trusted_init_user_fk_17; trusted_init_rag_generation_fk_17 and trusted_init_episode_generation_fk_17 on (initial_generation_id,user_id,kind,namespace,index_key) -> vector_generations(generation_id,tenant_id,vector_kind,namespace,index_key); trusted_init_rag_witness_fk_17 on (user_id,rag_kind,rag_namespace,rag_index_key,rag_head_revision) -> history_document_witnesses(tenant_id,vector_kind,namespace,index_key,head_revision); trusted_init_format_ck_17, trusted_init_kinds_ck_17, trusted_init_initial_versions_ck_17, trusted_init_digest_shape_ck_17, trusted_init_descriptor_ck_17. Digest shape for resource, History, Memory, manifest and document-evidence digests and both index keys is ^[0-9a-f]{64}$. Descriptor check requires JSONB object, exact top-level/nested key sets, numeric format_version 1, correctly typed non-null scalar fields, and embedded identities equal separate columns. Implement exact key sets with JSONB equality to a nested jsonb_build_object reconstruction and explicit scalar type/presence checks, or an equivalent non-SRF predicate; PostgreSQL CHECK cannot contain jsonb_object_keys() as a set-returning function. The Python IndexIdentity validator and equality to registered vector_indexes.identity provide full profile validation. All FK targets are the existing unique/primary keys, so no new target-side constraint is needed.

Resource descriptor digest CHECK uses PostgreSQL built-in encode(sha256(convert_to(resource_descriptor::text,'UTF8')),'hex'). Initial History/Memory digests use the same PG expression over those initial JSONB values, checked by insert guard. No pgcrypto, no Python compact-JSON digest. History witness retains original document_evidence digest; vector manifest retains original SHA256([]). Neither is the JSONB digest.

BEFORE INSERT OR UPDATE OR DELETE FOR EACH ROW trigger trusted_init_guard_17 executes trusted_init_insert_guard_17. UPDATE/DELETE always raise a code-only SQL exception. INSERT requires READ COMMITTED; matching active users(id,username_key); both v1 snapshots equal exact initial History and user-bound Memory JSONB and their stored digests; descriptor identities equal registered vector_indexes.identity; both scoped generation receipts exist in state published with expected_count=0, matching empty digest, base_revision NULL, index_revision/publication_revision/publication_snapshot_version=1; heads have revision/index_revision/snapshot_version=1, generation_id NULL and last_generation_id equal respective initial IDs; RAG witness has document_count=0, head/index/snapshot revisions=1, initial generation ID and exact evidence digest. Check the user has no memory_documents, document_objects or import batches/tasks/objects/publication rows. The snapshot check uses the actual user_id in Memory JSONB and exact EMPTY_HISTORY; `encode(sha256(convert_to(payload::text,'UTF8')),'hex')` computes each stored initial digest. `empty_manifest_sha256` must equal SHA256 of the vector authority's canonical empty list, while `history_document_evidence_sha256` must match the witness's original document_evidence algorithm; neither substitutes for a JSONB digest. Use ordinary scalar/existence queries in the trigger, never external I/O or a set-returning CHECK. This is insert-time completion proof, not current ordinary_ready; later imports may advance snapshots/heads and retire initial receipts. FK/guard failures roll back caller transaction.

Downgrade first checks for any ledger row and raises RuntimeError before dropping anything if nonempty. Only empty disposable schema may drop trigger, function, table, returning to 16. Do not attempt 16's own downgrade; DELETE cannot bypass refusal because immutability trigger rejects it.

### Schema contract and gates

Keep existing _schema_ready(database,settings)->bool. Preserve exact DB name from DSN, current_schema, exact search_path, one alembic_version row and original 24 relations. Replace _HEAD equality by explicit mapping {"20261007_16": original 24, "20261022_17": original 24 + trusted_user_initializations}. Head16 needs no new relation. Head17 additionally queries pg_catalog by actual schema-qualified OIDs for precisely the 27 columns, types/nullability/default above; named PK/unique/FK/check definitions and validated/enforced status; enabled INSERT/UPDATE/DELETE trigger on correct table/function; and expected insert/immutability guard function body. Compare pg_get_constraintdef and pg_proc.prosrc (or equivalent catalog expressions) to exact reviewed migration17 expectations, not merely constraint names, so a same-name no-op replacement fails closed. Keep the expected catalog specification and one authoritative guard SQL-body constant in the shared helper; the migration uses that constant when creating the function, and both runtime and initializer use the shared expected catalog/body values. This avoids two independent guard-body copies and a runtime/settings import cycle. Product code must not import migration or test modules. Same-name public table, disabled trigger, dropped/renamed/malformed check or FK, missing column or wrong trigger function fail. The initializer applies the same identity/catalog test with allowed head set {17}. Unknown18, wrong DB/schema/search_path, 0/2 version rows and malformed17 fail closed. Retain head16 fake-cursor tests; test malformed17 against real disposable PG catalog.

## Required behavior and implementation guidance

1. Extract only pure resource code. Both direct and env routes share it; preserve old error-code priority. New initializer's profile-endpoint secrecy restriction does not narrow old settings.
2. The migration is additive and proof-bearing. No passing fixture directly inserts state='published'. Build actual stage→seal→publish receipts and witness. Syntactically valid hashes without actual matching account/snapshots/indexes/heads/receipts/witness still fail.
3. In a generated isolated PG schema, Alembic upgrade to16 then17. Build a positive test fixture with coordinator.acquire, snapshot compare_and_swap_in_transaction(expected_version=0), authority.stage/seal/publish_user for each empty scope, document_evidence and witness.insert; namespace RAG="pdf_<user_id>", episode="episodes". Test-only fixture may use separate committed transactions because this packet tests ledger validity, not the later atomic service. No S3/Qdrant required. Independently read back heads/receipts before successful ledger INSERT.
4. Use rollback/savepoint or separate disposable user for negative insertion: duplicate operation/user, wrong username, descriptor/identity/profile/scope, missing or nonempty-but-v1 snapshot, bad digest, missing witness, wrong generation/head, UPDATE/DELETE, nonempty downgrade. Assert unaffected authority after each. Test empty downgrade on second disposable schema.
5. Later service compares complete descriptor JSONB plus stored hashes on replay and separately evaluates current readiness. This packet implements none of that service.

## Acceptance criteria

- [ ] Resource-only settings work without trusted IDs and reject disabled flag, bad DSN/schema, retained/ordinary collision, wrong type/profile and secret-bearing URL before I/O; descriptor exact/full/canary-free. Existing direct/env API/Worker role/flag/two-ID/error/timing/repr tests pass.
- [ ] Migration17 has precisely named relation/columns/constraints/FKs/trigger; legal receipt fixture inserts. Partial/mismatched evidence, duplicate, UPDATE, DELETE fail with intact old authority. Nonempty downgrade refuses; empty disposable downgrade succeeds to16.
- [ ] Runtime accepts exact16 and intact17; initializer exact17 only. Catalog tampering, missing relation, wrong DB/schema/search_path, two version rows and future18 refuse. Head16 original fake-cursor tests still pass.
- [ ] Both complete new test modules, the complete `tests/integration/test_postgres_schema.py`, and the eleven explicitly selected non-`test_real_*` functions from `tests/test_isolated_import_runtime.py` pass with zero failures/errors/skips after root runner GO. Parameterizations of those eleven functions are included by pytest collection. The nine `test_real_*` functions remain Task05 multi-service scope. Independent Astra/root review checks raw-source/protected hashes before done; this packet asserts no tests yet.

## Test and verification commands

Root first independently reviews the private runner, fixed venv and reviewed Win32 Job helper hashes, generated schema prefix, current dedicated PG identity, timeout/log destinations and owned cleanup. Root creates a separate exact-byte GO JSON for each stage; neither this packet nor the runner creates GO. One serial pytest lane. The reviewed runner caps unit stages at 600s, schema/regression at 1800s, each collection at 180s, and owned cleanup at an additional 60s. Timeout or incomplete Job retirement fails. It records actual process exit, PID/creation/image, exact collected node IDs, setup/call/teardown, JUnit zero failures/errors/skips, source bytes before/after and redacted logs. No stage runs before its own GO.

After the two new tests exist and the three new product files are still absent, RED stages are `red-unit`, then `red-schema`. GREEN stages in serial order are `collect-green-unit`, `green-unit`, `collect-green-schema`, `green-schema`, `collect-regression`, `regression`. Each command has the form:

    D:\python_self_agent\venv\Scripts\python.exe -I .runtime/isolated-import-runtime/run-init-t1-sol-n1.py <stage> n1

The runner selects the entire new unit module for unit stages and the entire new schema module for schema stages. Regression selects both new modules and all of `tests/integration/test_postgres_schema.py`, plus these exact functions from `tests/test_isolated_import_runtime.py`: `test_settings_are_opt_in_pure_and_secret_safe`, `test_settings_reject_unsafe_identity_before_io`, `test_settings_reject_ordinary_reuse_and_bad_profile`, `test_equivalent_ordinary_database_identity_rejected`, `test_ordinary_database_indirect_or_ambiguous_identity_rejected`, `test_direct_construction_rejects_wrong_types_with_sanitized_error`, `test_half_open_pool_is_closed_without_provider_construction`, `test_close_attempts_all_owned_resources_after_one_failure`, `test_concurrent_lazy_clients_have_one_owner_and_close_fences_creation`, `test_close_waits_for_inflight_lazy_creation_without_leaking`, and `test_schema_identity_fails_hard_while_transient_ping_fails_closed`. There is no global `-k` filter and no `test_real_*` selector. Collection must include every parameterized variant and no other runtime function. RED accepts only real missing-new-module collection errors or attributable test-call failures, never a pass/skip/timeout. GREEN requires the exact fresh collect node set, all three phases passed per node, matching JUnit cases and process exit0. Every stage has a unique basetemp, JUnit and redacted report namespace.

Root GO is `init-t1-<stage>-n1-go-root.json` under `.runtime/isolated-import-runtime/`. It binds `authorization=INIT_T1_STAGE`, stage/label, HEAD, runner/baseline/packet SHA256, independently recomputed canonical source-map SHA256, previous accepted stage completion SHA256 (null only for first RED), and fresh collection completion SHA256 for execution stages. Root must calculate these values from current files and prior completion evidence, not import/call runner code to mint its own GO. The runner exact-compares JSON fields and rejects missing/old/changed GO. Static review: migration, seven-file diff, absence of pgcrypto/prior-migration edits/source-target mutation; compare 15 raw SHA256s with baseline and list five new files.

## Stop conditions

Append repository README reality-conflict report if base/protected bytes drift, an asserted interface differs, acceptance needs an eighth file/service, schema17 already exists, a prerequisite is absent, or listed evidence cannot prove behavior. Do not infer empty from missing, weaken trust rules, broaden scope or mark this draft READY/done on worker assertion.

## Original handoff requirements

After authorized implementation, replace this section with README handoff: packet ID/status; seven allowed files/purposes/SHA256; produced signatures and ledger/catalog contract; each acceptance item with independent readback; RED/GREEN/full-module/regression exact command, node IDs, counts, skips, exit, duration, JUnit/log, disposable schema and PG identity; empty/nonempty downgrade, old authority preservation, 15 protected raw hashes, scope confirmation, deviations, risks and commit state. Root records independent Astra result and decides status. No test or feature delivery is claimed here.

## Implementation handoff — 2026-10-23

- Packet: `2026-10-22-trusted-user-initialization-01`; status: `done`.
- Delivered: shared pure resource parsing/validation, separate resource-only initializer opt-in and full nonsecret descriptor, additive immutable schema17 ledger, strict runtime16/17 and initializer17-only catalog gates.
- Files changed: the five Create paths and `app/isolated_import_runtime.py` listed above. `tests/test_isolated_import_runtime.py` required no edit. All 15 protected raw hashes match the original baseline.
- Interfaces: `IsolatedResourceSettings`, raw-kwargs `parse_isolated_resources`, split identity/pool validators, `TrustedUserInitializationSettings`, `resource_descriptor`, `require_initialization_schema`, shared `schema_catalog_ready`. Existing runtime public fields, role/flag/two-ID rules and error priorities retained.
- Acceptance evidence: all four criteria above accepted by Astra; ledger positive insert uses legal real receipts and witness; partial/nonempty/mismatched evidence, immutability, duplicate, nonempty downgrade and catalog/endpoint tampering refuse. Empty downgrade to16 succeeds. Canonical display acceptance is verified on PostgreSQL17.6.
- RED: root ran `D:\python_self_agent\venv\Scripts\python.exe -I .runtime/isolated-import-runtime/run-init-t1-sol-n1.py red-unit n3` and `red-schema n3`, with separate GO. Each is a genuine missing-module collection exit2, zero executed test functions; root receipt `b73d096703568e5e2c10fdf6a6da19682b910e3b56368b9ab8f12e92cdc2a09c`. These historical tests precede subsequent reviewed test repairs; they are not current-byte business passes.
- GREEN commands: same fixed command form with stages `collect-green-unit`, `green-unit`, `collect-green-schema`, `green-schema`, `collect-regression`, `regression`, label `n4`, each separately authorized. Current-label collection exactly matches execution. Unit32, schema37, regression129; all exit0 and zero failures/errors/skips. Three executions total198 including repeats,129 distinct nodes,594 passing setup/call/teardown records.
- Evidence: `.runtime/isolated-import-runtime/init-t1-green-accepted-root-n4.json` SHA256 `df2d42df645ad36b63f98a6f3484b711e10fc60e8ab01d816e076f72761845cf`; source map `aa78f654b264dfb8c9c0f2ab8090facc965d046854caa1e1f3c3555f429c89c6`; all stage logs/XML/phase/identity receipts are bound there and in the independent report.
- Independent SPEC/QUALITY: accepted in `init-t1-accepted-astra-n1.md`, SHA256 `53615246d48210df450847f9ce44938fc6c398527fda09c765d175b76de5f791`. Root read the entire report and recomputed artifact/source hashes. Final six-file source hashes are in that report and the progress record.
- Process/resources: actual retained PID/image/creation/nonce/exits, empty owned Jobs, no timeout/truncation, unchanged source/governing bytes. Dedicated PG only, loopback59497, exact original named volume; no remaining `init_t1_%` schema after execution. Source Qdrant remained stopped.
- Deviations: original runtime real-service cases remain Task05 scope; PG-only regression uses the eleven explicit pure runtime functions. Private retry runner binds historical n3 RED separately from fresh n4 GREEN; original failed attempts remain failures. Actual fixes and independent reviews are recorded in progress.
- Residual scope: Task02–05 and final whole-feature integration remain outstanding. No new production account, ordinary bootstrap, historical repair, main merge or deployment occurred.
- Commit: scoped Task01 delivery is recorded in the subsequent progress entry; this handoff itself does not claim a remote delivery.

The executable packet's READY bytes SHA256 `15f1f2984fca662666b96f35835bdcb95c84b15223125a231e95bbe738bd3dbe` are preserved in `init-t1-pre-delivery-01-resource-settings-schema.md`; this done/handoff update is documentary only.
