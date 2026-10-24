---
id: "2026-10-22-trusted-user-initialization-02"
title: "Publish the initial empty RAG and episode vector pair in one caller transaction"
status: "done"
parallel-safe: false
depends-on: ["2026-10-22-trusted-user-initialization-01"]
base-commit: "29e116540d32ba7a56d100ad40fa83be4a13ee4f"
owner: "GPT-6 Sol High"
---

# Task Packet: initial empty vector pair

## Goal

Add one narrow `PostgresVectorGenerationAuthority` helper that, on a caller's live READ COMMITTED PostgreSQL cursor, stages, seals, and publishes exactly two verified empty vector baselines for the same newly inserted user. It returns the distinct RAG and episode receipt UUIDs without committing. The caller can then add the RAG History witness and completion credential in that same transaction; a failure anywhere rolls the entire pair back.

Independent Astra SPEC/QUALITY accepted this contract and the frozen runner for READY. Root assigns Sol to write only the new tests first; product implementation requires accepted real RED evidence, and all test execution requires a separate root stage GO.

## Non-goals

- No user creation, password handling, snapshots creation, History witness insertion, completion credential, CLI, public registration, or historical-user repair. Task03 owns the new-user transaction and provenance.
- No Qdrant/S3 access, collection or point creation, embedding, external writes, or distributed bootstrap change.
- No migration or persistent format change; no generic pair publisher, callback, `ImportAttempt`, caller-supplied generation UUID, or new claim/XID/provenance protocol.

## Delivery context

Task01 delivered schema17's immutable completion proof and exact 16/17 runtime gates at base `29e116540d32ba7a56d100ad40fa83be4a13ee4f`. The schema17 guard requires two real `published` zero-point receipts, two matching index/head rows at revision 1, v1 empty History and user-bound Memory snapshots, an RAG witness, and no import or document rows when the later Task03 credential is inserted. An empty head stores `generation_id=NULL` and `last_generation_id=<real receipt UUID>`; a missing head does not prove an empty corpus.

Current `stage`, `seal`, and `publish_user` each own a transaction, so composing them would expose partial results and cannot support one atomic new-account commit. The private `_publish` already accepts a caller cursor and enforces the existing authority transitions. This packet provides only the vector portion of the accepted new-account call graph. The helper is private and must not be presented as a general way to initialize a historical empty user: exact scopes, empty snapshots, and a lease alone cannot prove that an account was newly inserted. Task03 must generate the user UUID itself and call this helper only after its own `INSERT users` and two v1 snapshot inserts on the same cursor.

## Relevant files and current interfaces

- `app/postgres_vector_generations.py:67-101` — `VectorScope(tenant_id, vector_kind, namespace, identity)` validates kind and Qdrant identity; `scope.key` is `(tenant_id, kind, namespace, index_key)`, where `index_key` hashes the full canonical `IndexIdentity.to_dict()`.
- `app/postgres_vector_generations.py:109-143` — authority owns `coordinator`; `_owner(cursor, authority, scope)` checks READ COMMITTED, live lease, active user, tenant match; `_index` verifies registered full identity; `_head_row` selects an exact scoped head.
- `app/postgres_vector_generations.py:197-259` — `stage` has its own transaction, registers a missing index at revision 1, inserts `staging` with immutable lease owner/token/version and nullable task fields, then rechecks ownership. `_candidate` locks the exact generation and checks state and full owner/task binding, followed by a fresh live-lease check.
- `app/postgres_vector_generations.py:261-285,298-357` — `seal` separately transitions staging to sealed with verified count/digest; `_publish(cursor, ..., expected_revision=None, expected_index_revision=1, snapshot_version=1, expected_count=0, content_digest=<empty digest>)` checks gate, index/head CAS, candidate binding, and changes sealed to published with receipt revision/snapshot version 1. It writes a null visible generation for zero count and a real `last_generation_id`.
- `app/postgres_coordination.py:77-123` — `acquire_in_transaction` gives a provisional `UserMutationLease` after user-first lock; `require_live_in_transaction` rechecks owner/token/version and expiry using a fresh database clock after lock acquisition. `app/import_publication_evidence.py` supplies `require_no_gate_in_transaction`.
- `app/postgres_snapshots.py:106-135` and `app/history.py:12` — Task03 will insert exact History `{"documents":[],"questions":[],"notes":[],"sessions":[]}` and Memory `{"user_id":user_id,"memories":[]}` with `expected_version=0`, producing version 1. This helper reads/validates those committed-to-caller but not yet committed rows; it does not create them.
- `app/isolated_resource_settings.py:370-470` and `migrations/versions/20261022_17_trusted_user_initializations.py:15-53` — schema17 completion guard fixes `rag_namespace='pdf_' || user_id`, `episode_namespace='episodes'`, both full identities/index revisions, two receipts, and empty digest `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` (SHA-256 of `[]`). The later witness must use the same RAG receipt/head.
- `migrations/versions/20260929_10_vector_generations.py:11-176` and `migrations/versions/20260929_11_vector_publication_receipts.py:19-75` — index identity is immutable; direct published insert is forbidden; legal generation lifecycle is staging → sealed → published, with deferred head/receipt consistency checks.
- `tests/integration/test_trusted_initialization_schema.py:62-95,172-237` — its fixture shows disposable schema cleanup and its completion builder shows expected rows, but uses separately committed `stage`/`seal`/`publish_user`. Do not copy that sequence or fixture's direct ledger SQL as the production pair implementation. The new test fixture must protect schema cleanup with `try/finally` enclosing Alembic upgrade too.
- `tests/integration/test_postgres_vector_generations.py:62-83,306-398` and `tests/integration/test_vector_generation_service.py:27-49` — existing lease/head regression seams. The service fixture needs both `POSTGRES_TEST_URL` and `GENERATION_QDRANT_TEST_URL` and writes to a disposable target Qdrant collection; absent target Q causes skip and cannot count as a passed regression.
- `app/import_memory_publication.py:79-85` and `app/import_publication_evidence.py:834,1050-1103` — the public `stage(..., live=...)` and `seal(..., live=...)` branches resolve an issued handle, read exact evidence, and check candidate operations `stage_before`, `stage_after`, and `seal`. `tests/integration/test_postgres_vector_generations.py:123-135` shows real import task creation and claim yielding `ImportAttempt`; the new focused test must cover wrapper compatibility after cursor extraction.
- Existing changes to preserve: the 15 pre-existing dirty paths named below; neither belongs to this packet. Root recorded planning baseline `init-t2-baseline-root-n1.json` SHA-256 `53f8b6fd68678b9301066832db7bf1d7a133cf1a761ef0579fff9823919be183` and Task01 delivery receipt `init-t1-delivery-root-n1.json` SHA-256 `5d945d90dc80908494fe28e42fbd368a1c5796a8eee09a277ba17501d1ace424`. These are ignored local evidence, not worker edit targets.

## Prerequisites

### Packet dependencies

- `2026-10-22-trusted-user-initialization-01` is `done`, independently accepted, committed and pushed at the base above. Task03 depends on this packet; Task02 is not parallel-safe.

### Repository/base state

- Base commit: `29e116540d32ba7a56d100ad40fa83be4a13ee4f`. Confirm it and the actual source interfaces before assignment. The new test path did not exist in root's Task02 planning baseline.
- Exact migration17 and `app/isolated_resource_settings.py` completion guard from Task01 are present. The caller supplies a newly inserted active user, same-transaction provisional real lease, and both exact v1 snapshots. This helper does not certify new-user provenance by itself.
- Preserve the raw bytes of: `app/coordination.py`, `app/document_library.py`, `app/history.py`, `app/import_models.py`, `app/import_worker.py`, `app/memory_repository.py`, `app/qa_deletion.py`, `app/qa_persistence.py`, `app/qa_repository.py`, `app/recovery.py`, `migrations/versions/20260926_01_business_schema.sql`, `migrations/versions/20260928_09_import_leases.py`, `tests/conftest.py`, `tests/test_import_worker.py`, `tests/test_user_mutation_coordination.py`. Do not reset, normalize, stage, or count these as delivered work.

### External prerequisites

- Root alone owns the serial pytest lane, reviewed runner, test namespace, and phase GO. Only the specifically attested disposable PostgreSQL target and the specifically attested **target** Qdrant may be started/used for the full regression. `POSTGRES_TEST_URL` and `GENERATION_QDRANT_TEST_URL` must resolve to those targets; never substitute source Qdrant, retained/production resources, or a skipped service module. Root reported PG and target Q currently exited, so no test execution is authorized by this draft.
- Use `D:\python_self_agent\venv\Scripts\python.exe` and fresh disposable PG schemas. The helper itself requires no external service and performs no network I/O. No credential appears in argv, JUnit, packet, or logs.

## Explicit change boundary

### Allowed files: exhaustive two

- Modify: `app/postgres_vector_generations.py` — only the narrow pair method and minimum shared private cursor stage/seal extraction required to preserve existing behavior.
- Create: `tests/integration/test_initial_empty_vector_pair.py` — dedicated disposable-PG contract and failure tests.

### Allowed behavior changes

- Add private `_publish_initial_empty_pair_in_transaction(cursor, lease: UserMutationLease, rag_scope: VectorScope, episode_scope: VectorScope) -> tuple[UUID, UUID]`. Extract `_stage_in_transaction(self, cursor, scope, authority, *, expected_revision, generation_id: UUID, live=None) -> UUID` and `_seal_in_transaction(self, cursor, scope, authority, generation_id: UUID, *, expected_count: int, content_digest: str, live=None) -> None` as private shared cursor methods. The existing `stage` wrapper retains its argument validation/UUID allocation and transaction, then delegates to `_stage_in_transaction`; the existing `seal` wrapper retains its manifest validation and transaction, then delegates to `_seal_in_transaction`. Both shared methods retain the original `live` import evidence/reservation branches and all checks. The pair alone calls both with `authority=lease`, `live=None`, `expected_revision=None`, internally generated UUIDs, `expected_count=0`, and the empty digest. Keep `stage`, `seal`, `publish_user`, `complete_import`, and `_publish` externally observed signatures and behavior intact.
- The pair helper allocates fresh UUIDs, creates exactly two matching index rows at revision 1, creates exactly two legal receipts and empty heads on the supplied cursor, and returns the UUIDs in `(rag, episode)` order. It never calls `database.transaction()`, commits, or accepts a callback/client/manifest input.

### Forbidden changes

- All other files, especially prior or new migrations, Task01 modules/tests, service/API/CLI, witness/snapshot/coordinator implementation, `tests/conftest.py`, deployment configuration, and the 15 protected dirty paths.
- No direct `published` insert, disabled trigger, fixture SQL transfer, fake lease, derived history repair, retained/source endpoint use, collection creation, Qdrant point write, or multi-transaction path. Do not widen scope to fix an unrelated regression; report a reality conflict.

## Interface contract

### Consumes

- An active caller-owned READ COMMITTED cursor on the same `PostgresDatabase` as this authority, after the caller's `INSERT users` and two v1 snapshot inserts. The caller keeps the transaction open through the later witness/credential and final lease release.
- A real `UserMutationLease` from `PostgresUserMutationCoordinator.acquire_in_transaction(cursor, user_id, owner, ...)`, not `ImportAttempt` or a forged lease. Its user ID equals both scope tenants; owner, token, version, active status, and DB-clock expiry remain authoritative.
- `rag_scope=VectorScope(user_id,'rag','pdf_'+user_id,full_rag_identity)` and `episode_scope=VectorScope(user_id,'episode','episodes',full_episode_identity)`. The identities have distinct `base_collection` and distinct `physical_collection`, matching Task01 resource validation (`app/isolated_resource_settings.py:203-205`); this is the precise meaning of non-aliased identities here. The full identities remain attached to their own scoped index rows. No `vector_indexes` or `vector_heads` row exists for this tenant at entry, including other index keys/namespaces/kinds; schema17's completion guard requires exactly two of each after publication.

### Produces

- On success, `(rag_initial_generation_id, episode_initial_generation_id)` as two distinct newly generated UUIDs; no commit. Both matching indexes have identity JSONB equal to their `IndexIdentity.to_dict()` and `index_revision=1`. Each `vector_generations` row has `base_revision=NULL`, `index_revision=1`, actual lease provenance, all task fields NULL, `state='published'`, `expected_count=0`, `content_digest=SHA256(b'[]')`, `publication_revision=1`, and `publication_snapshot_version=1`. Each head has `revision=1`, `generation_id=NULL`, `last_generation_id=<its own receipt>`, `index_revision=1`, `snapshot_version=1`.
- On invalid input, stale/missing ownership, expired lease, gate, nonempty or non-v1 snapshot, existing index/head, mismatched identity or scope, or a failed transition: raise before successful return. The caller must roll back the transaction; no partial pair may commit. Keep existing exception families where the shared helpers already prescribe them.

### Invariants

- The first domain lock in the pair's authority path is the existing user-first live-lease check (after the coordinator's transaction-isolation query); recheck after any relevant lock wait and before return. Check unresolved gate at the same points as direct stage/seal/publish. Candidate lock uses `_candidate` or equivalent shared logic with full owner/token/version/task binding and fresh clock. The caller's final live/gate/release check remains Task03's responsibility.
- Both scopes, both snapshot rows, and tenant-wide absence of all vector index/head rows are checked on this cursor before any index/generation insert. An index without a head is still pre-existing state and must be rejected, even for a different key/namespace/kind. A head with no index is also rejected. Missing state is not an empty published baseline.
- Preserve DB trigger lifecycle and full receipt/head consistency. Existing single-scope direct/import calls retain their isolation, gate, reservation/evidence, CAS, error, and rollback semantics.

## Required behavior

1. Reject anything other than a real `UserMutationLease` and the exact two scope kinds/namespaces for one tenant; reject equal RAG/episode `base_collection` or `physical_collection`, cross-tenant scopes, **any** pre-existing index or head row for this tenant (including a different key/namespace/kind), absent/wrong-version/wrong-payload History or Memory snapshot, unresolved publication gate, inactive user, and stale owner/token/version. No arbitrary corpus, generation UUID, or callback parameters exist.
2. Verify exact empty History and `{user_id, memories: []}` Memory payload at version 1 from the caller cursor. The scope identity is checked as full JSON, with index keys derived from `VectorScope`; do not treat a different index key in the same kind/namespace as matching. Schema17's eventual credential guard remains the final exact two-row authority proof.
3. Reuse controlled index registration, staged candidate insertion, `_candidate`/owner checks, seal update, and `_publish` so each scope undergoes `staging → sealed → published` in that order on the same cursor. Use `expected_revision=None`, `expected_index_revision=1`, `snapshot_version=1`, `expected_count=0`, and the literal empty-list SHA-256 above when publishing. Allocate both UUIDs internally with `uuid4()`.
4. Keep the lease and gate valid after waits for user/candidate/head locks, and immediately before returning. Expiry during a lock wait or owner replacement rejects publication and requires caller rollback. Failure injected after first `_publish` must leave **both** scopes absent after rollback, including index, head, and receipt rows; no external state changes.
5. This helper is used only by Task03's new-user transaction. It cannot independently distinguish a prior empty account from a just-inserted one; do not claim that it does. If the accepted provenance boundary is insufficient for safe Task03 composition, report the exact gap for review before adding another capability mechanism.
6. Keep the public `stage(..., live=...)` and `seal(..., live=...)` import branch unchanged after extraction. The wrapper must pass the same `live` object through to exact evidence lookup and `require_candidate_in_transaction` with `stage_before`, `stage_after`, then `seal`, each with the matching scope/generation/phase/version; both stage checks use the same stage transaction cursor, while seal uses its own wrapper transaction cursor. Exceptions from any check retain the original transaction rollback behavior.

## Implementation guidance

1. Extract the existing stage/seal transition bodies into the two private cursor helpers with the signatures above. Keep public wrappers' `database.transaction()` and input-validation order, and delegate their current user-lease and `live` import evidence/reservation branches to the shared bodies without changing error or rollback behavior. The pair calls these helpers on its caller cursor, stages RAG then episode, seals RAG then episode, then calls `_publish` for RAG then episode. Avoid copying test fixture SQL or creating a second state machine.
2. Perform scope/lease type checks without I/O, then live-lease/user-first lock and gate checks. Read and validate both snapshot rows and query tenant-wide absence in `vector_indexes` and `vector_heads` before inserting either index. Proceed in a deterministic RAG then episode order on one cursor. Revalidate after locks and at return.
3. Let `_publish` perform its existing index/head/candidate checks. If `stage`/`seal` refactoring changes behavior for legacy paths, keep both original test modules in the required regression. Do not change `_publish` signature just for the pair.
4. The new test file owns its own disposable schema fixture. Establish cleanup `try/finally` **before** Alembic upgrade, close every opened pool, and drop only the generated schema even if upgrade or test setup fails. Build a genuinely new user and real `acquire_in_transaction` lease on the same cursor; construct exact scopes from Task01 settings identities or equivalent full `IndexIdentity` records. Do not import the schema-test completion builder, because it commits each stage separately and inserts a witness.
5. Test visibility from a second connection before commit, full rows after commit, rollback at the point between publications, and exact refusal cases. For the real lock-wait expiry case, use a separately committed test user with a short real lease: a provisional new user inserted in an uncommitted transaction cannot be locked by another connection. Hold that existing user's row lock on one connection, let the helper wait on another with the already acquired lease, release the lock after DB-clock expiry, and assert the fresh post-wait lease check rejects without committed vector rows. This tests the same authority fence without falsely presenting that setup as Task03's new-user provenance. Record SQL lifecycle evidence; do not infer atomicity merely from the returned pair.
6. In this same new PG-only test file, add a controlled wrapper/live compatibility case using a real newly created, committed test user, real import task creation/claim and `ImportAttempt` (including its real user lease). Use one unique sentinel `live`. Replace only `_require_live_issuer`, evidence repository `_read_exact_in_cursor`, and `require_candidate_in_transaction`; leave real `stage`/`seal` SQL, `_owner`, `_candidate`, and transaction management intact. Record each check's object identity, cursor identity, operation, scope, generation, phase, and version. Assert `stage_before → stage_after → seal`, the same sentinel throughout, the same stage cursor for both stage checks and a separate seal wrapper cursor, and real staging then sealed rows with correct manifest and lease/task provenance. Parameterize a `PublicationEvidenceError` raised by each of the three controlled checks: `stage_before` and `stage_after` leave no new index/generation/head after rollback (at `stage_after`, observe the staging row inside that transaction before raising); `seal` failure preserves the previously committed staging row with null `expected_count`, `content_digest`, and `sealed_at`, and no head. This exercises wrapper delegation and rollback only; it is not an acceptance test of the real issuer/evidence security chain.

## Acceptance criteria

- [x] The new integration test proves no index/head/receipt visibility to a second connection before caller commit; after commit it observes two distinct real published zero-point receipts, two full matching index identities, and two empty heads with initial revision/index/snapshot version 1.
- [x] Instrumented stage/seal/publish transitions show one caller cursor, no nested authority transaction, and legal `staging → sealed → published`; injecting failure between publications and rolling back leaves no user vector rows.
- [x] Tests reject wrong tenant/kind/namespace, any tenant-owned pre-existing index or head including another key/namespace/kind, absent/non-v1/nonempty snapshots, unresolved gate, stale owner/token/version, and a naturally expired lease after a real lock wait. No rejected attempt commits a head.
- [x] The PG-only wrapper/live compatibility test records `stage_before → stage_after → seal`, exact sentinel/cursor/scope/generation/phase/version continuity, real staging/sealed rows and lease/task provenance, and the three specified `PublicationEvidenceError` rollback outcomes. Its claim is limited to wrapper/live branch compatibility, not issuer/evidence safety.
- [x] Existing single-scope and service regression tests pass with zero failed/error/skipped required nodes. The Qdrant service module runs against the attested disposable **target** Qdrant; a skipped module is not a pass.
- [x] Diff is confined to the two allowed files, no external write interface is invoked, and the 15 protected dirty files retain their original raw hashes.

## Test and verification commands

Root must first independently review `.runtime/isolated-import-runtime/run-init-t2-sol-n1.py` SHA-256 `7fbe946ce291c458fd117fa11edc7e70fab7d4dfa6d19a426f56cde6fb88ac65` (ignored local runner; handoff `init-t2-runner-sol-n1.md` SHA-256 `fb2507f34e1034185e0ed9d20e024d57ccc26d1f7d90397956864b5b8eafc16e`), attest the exact disposable PG and target Qdrant identities/endpoints, start only those targets as needed, and issue a separate exact-byte GO per stage. Run from repository root. Each stage label matches `n[1-9][0-9]*`; `n1` below is the first proposed label. The runner injects `POSTGRES_TEST_URL` for focus execution and also `GENERATION_QDRANT_TEST_URL` for regression execution, selects exact test modules, writes fresh sanitized reports/JUnit and before/after evidence, and enforces zero skips plus collected/executed node equality. Collection stages execute no fixtures and require no service URL. An execution stage requires the accepted matching collection completion and current source map; the runner does not mint its own GO. These calls remain review targets while this packet is `draft`, not test authorization.

```powershell
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t2-sol-n1.py red-focus n1
```

Expected RED: only `tests/integration/test_initial_empty_vector_pair.py` runs against disposable PG; new tests fail for the absent helper/behavior with zero fixture skips or unrelated errors. Root executes RED only after accepting the new test bytes.

```powershell
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t2-sol-n1.py collect-green-focus n1
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t2-sol-n1.py green-focus n1
```

Expected GREEN: collection is side-effect-free; the matching execution runs only the new PG integration module. All collected new tests pass, zero failed/error/skipped required nodes. Inspect actual rows and SQL trace, not just exit code.

```powershell
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t2-sol-n1.py collect-regression n1
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t2-sol-n1.py regression n1
```

Expected regression: the full new module **plus** `tests/app/test_postgres_vector_generations.py`, `tests/integration/test_postgres_vector_generations.py`, and `tests/integration/test_vector_generation_service.py` run; all collected required nodes pass with zero failures/errors/skips. Root compares collected and executed node IDs, checks `GENERATION_QDRANT_TEST_URL` targets its dedicated disposable Qdrant, and rejects a merely skipped service module. No source Qdrant or retained resources may be used. If service readiness is unavailable, stop and report the incomplete gate; do not silently narrow the test command.

## Stop conditions

Stop and report `blocked` if the base/Task01 guard or any cited interface differs; Task01 is not actually accepted; a needed edit exceeds the two files; pre-existing work overlaps ownership; genuine new-user provenance cannot be supplied by Task03; a legal same-cursor stage/seal path cannot preserve existing APIs; required PG/target-Q test preconditions cannot be attested; or the finite commands cannot prove the criteria. Use the `docs/agent-workflow/README.md` reality-conflict report. Do not silently relabel skipped tests as passes or invent a historical repair path.

## Implementation handoff — 2026-10-24

- Packet: `2026-10-22-trusted-user-initialization-02`; status: `done`.
- Delivered: one caller cursor stages, seals and publishes the initial empty RAG/episode pair without committing. Exact v1 snapshots, tenant-wide absence, full identities, user-first live lease and gate checks are enforced; existing public/import wrappers retain their checks and transactions.
- Files changed: `app/postgres_vector_generations.py` SHA256 `305b3ca171695333bff58307fa2002da50ad566b45560ad47946a14c6617e987`; new `tests/integration/test_initial_empty_vector_pair.py` SHA256 `49f1edb89170e0511b0b7198fc946fe0dd60eb51eb127238963e453ce21a22dc`.
- Interfaces: `_publish_initial_empty_pair_in_transaction(cursor, lease, rag_scope, episode_scope) -> tuple[UUID, UUID]`, `_stage_in_transaction` and `_seal_in_transaction` as specified above. All existing public signatures unchanged.
- Acceptance: all six criteria accepted. Real PG tests check pre-commit invisibility, exact committed receipts/heads, legal SQL lifecycle, rollback after first publication, scope/snapshot/ownership/gate/existing-state refusals, and real lock wait with lease live before natural expiry. Four controlled live compatibility cases pass; their scope remains wrapper compatibility.
- RED: fixed-venv runner `run-init-t2-sol-n1.py red-focus n1`, 34 nodes: 30 actual missing-target AttributeError calls and four live compatibility passes, 0 errors/skips. Root receipt `07d8865f52a1c58cddb683fbf0ce58c69e3d0f1e4790adb60f64a2eadb6218b7`. Historical test SHA `6d50bc6716908b7a48d800b89089caa48e433fc252a218c0375dccb558438d93` precedes the gate-fixture correction and is not relabeled as current-byte verification.
- GREEN commands: `D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t2-sol-n1.py <stage> n2` for separate `collect-green-focus`, `green-focus`, `collect-regression`, `regression`, each exact-byte root GO and historical RED label n1. Fresh focus34 and full regression80 pass, zero failures/errors/skips, exact collected/executed nodes, 80 distinct tests, 114 executions including repeats, 342 passing phase records. Regression includes new34 + existing authority29 + service17.
- Independent final SPEC/QUALITY: `init-t2-accepted-astra-n1.md` SHA256 `3e24c5e9b30361964f39d17d181c7444843136d4f152e860e75fdaf5c1baae35`; root combined receipt `init-t2-green-accepted-root-n2.json` SHA256 `944f66de482f8e9e493d2b70f31a4880bd02517575a002af56e6d2f67b3760a2`. Current map `6929aae46ec686d8261ee6a9e8b9e9f663877c97d8ac4928abf6b8cebbcf3fd9`; each bound artifact, process/nonce/exit/Job record and all 2,731 file aliases independently reconciled.
- Resources: dedicated PG and target Qdrant actually healthy during regression, source Qdrant remained stopped. Later cleanup found zero `init_t2_%` schemas; exact cleanup receipt `30461fc386266657b729fa010460523e7967373693c06b7b302793a37e546a88`. Later target stopped state is separate from retained actual test-service attestation.
- Scope: only the two owned source/test files; 435 other original inputs and all 15 protected dirty raw hashes unchanged. No source Qdrant access, migration, external vector write, historical repair, main merge or production deployment.
- Deviations: GREEN n1's 33pass/1fail remains failed evidence. Its gate fixture used migration14 columns removed in migration15; reviewed minimal correction removed only those INSERT columns/values. n2 fully recollected and reran current bytes. No product workaround was needed.
- Residual: Task03 must establish genuinely-new-user provenance and add snapshots/witness/ledger/final checks in its own atomic transaction; Task04 CLI, Task05 process acceptance and final feature integration remain outstanding.
- Commit: scoped delivery is recorded in the subsequent progress entry; this handoff does not independently claim remote delivery.

Execution READY bytes SHA256 `ab6867729ebfc605419225ba88a506bbdb7b32005b9919e33304fc70511ca9a3` are preserved in ignored `init-t2-pre-delivery-02-initial-empty-vector-pair.md`; this done/handoff update is documentary only.
