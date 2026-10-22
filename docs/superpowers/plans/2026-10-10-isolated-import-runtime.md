# Isolated Authenticated Import Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The repository's `docs/agent-workflow/README.md` controls packet readiness, ownership, review, and handoff; this plan does not itself authorize implementation of a packet.

**Goal:** Deliver an explicitly enabled, isolated API and independent Worker that authenticate two pre-attested tenants, accept immutable uploads, publish with existing PostgreSQL/S3/Qdrant authority, recover interrupted publications, and read committed results across real process restarts.

**Architecture:** Use a separate composition root and a route allowlist. API processes own PostgreSQL sessions, upload admission, and persisted reads; Worker processes own filtered ordinary claims, one-shot publication, bounded heartbeats, and separate recovery. Reuse the accepted C issuance/adapter/proof/recovery protocol without a second terminal writer.

**Tech Stack:** Python, FastAPI, psycopg/PostgreSQL, versioned S3, Qdrant, pytest; Windows PowerShell and the repository venv at `D:\python_self_agent\venv\Scripts\python.exe`.

## Global Constraints

- Base for initial packet: `38bc8bcabd90e23a7518375d7e2087a399f0eec9`; rebase later packets on accepted predecessor commits, never on a dirty file by assumption.
- First milestone uses only two pre-attested isolated accounts in a new disposable schema, bucket and collections. It does not create a production fresh-user baseline, migrate retained users, open other business routes, change ordinary distributed bootstrap, or perform production cutover.
- The pre-existing 15 dirty paths shown in the packet are preserved. In particular, do not touch `app/import_worker.py` or `tests/test_import_worker.py` for this milestone.
- API and Worker processes have separate pools; no local SQLite/JSON/file-lock/business fallback, embedded Worker, public register, cancel/retry, or whole-router mount.
- Runtime defaults: lease 60 seconds, heartbeat 10 seconds, attempt budget 300 seconds, provider call timeout 30 seconds, stop wait 10 seconds. Enforce `heartbeat < lease / 3`, bounded retries, and `provider_timeout <= attempt_budget`.
- Tenant filters act in SQL before candidate selection, locks, claim, or expiry modification. `None` preserves existing repository behavior; a valid empty filter selects nothing; malformed filters fail before any database use.
- Existing task/user lease tuple, user-first lock order, fresh database clock after locks, recovery capture-first protocol, gate/reservations/queue, and proof semantics remain authoritative.
- One Sol High pytest lane runs tests only after root GO. Astra High independently reviews concurrency and consistency; Luna High may record mechanical evidence. Preserve raw commands, actual exit, XML, logs, source and service identity. Historical 637 passes are baseline evidence, never a count of new tests.

## File map and serial dependency graph

All implementation packets are serial (`parallel-safe: false`). Packet 01 is independently useful and has no new-interface dependency; later packets are **draft** until their predecessor is accepted, current code is re-inspected, and self-contained packets are written and reviewed. A task below defines an intended contract, not an already existing interface.

| Task | Files owned by its future packet | Deliverable and dependency |
|---|---|---|
| 01 — tenant SQL selection | Modify `app/postgres_import_leases.py`, `app/import_publication_recovery.py`; optionally create `app/import_tenant_selection.py`; create `tests/test_import_tenant_selection.py` and `tests/integration/test_isolated_import_tenant_selection.py` | SQL prefilter for ordinary, expiry, recovery; no predecessor |
| 02 — trusted configuration/readiness | Create `app/isolated_import_runtime.py`, `tests/test_isolated_import_runtime.py` | Strict opt-in identity and separate ordinary/recovery readiness; after 01 |
| 03 — PG auth and import facades | Create sessions/service/facade test; draft additionally proposes narrow isolated runtime, artifact admission callback and typed evidence gate seams | Shared-session auth, transaction-bound artifact submission, tenant-bound persisted reads and precise gate mapping; after 02 and independent amendment review |
| 04 — independent Worker (draft amendment) | Create `app/isolated_import_provider.py`, `app/distributed_import_worker.py`, `scripts/run_isolated_import_worker.py`, `tests/integration/test_distributed_import_worker.py` | Shared exact-profile provider, one-shot C publication, bounded heartbeat/attempt, independent filtered recovery; after accepted 03 and independent amendment review |
| 05 — restricted API | Create `api/isolated_import_app.py`, `tests/integration/test_isolated_import_api.py` | Exact route allowlist, cookie/CSRF, upload/readback; after 04 |
| 06 — real process acceptance | Create `tests/integration/test_isolated_import_processes.py`, `scripts/verify_isolated_import_runtime.ps1`; evidence under a new `docs/agent-work/isolated-import-runtime/` directory | Actual two API/PID Worker restart and fault windows; after 05 |
| 07 — final integration and delivery | Create `docs/agent-workflow/task-packets/2026-10-10-isolated-import-runtime/FINAL_INTEGRATION_REVIEW.md` and new evidence in `docs/agent-work/isolated-import-runtime/` | Independent Spec/Quality/Delivery reviews, combined verification, source-bound branch commit/push; after 06 |

Do not give a future worker only this table. Codex must issue each later packet with verified interfaces, owned files, prerequisites, exact commands, stop rules, and a handoff format after the predecessor lands.

### Task 01: Tenant selection before PostgreSQL authority mutation

**Files:** Modify `app/postgres_import_leases.py:90`, `:468`; modify `app/import_publication_recovery.py:265`; create `tests/test_import_tenant_selection.py` and `tests/integration/test_isolated_import_tenant_selection.py`; optionally create pure `app/import_tenant_selection.py` if sharing validation avoids divergence.

**Interfaces:** Produce `PostgresImportLeaseRepository.claim_next(worker_id, lease_seconds=60, *, allowed_user_ids: frozenset[str] | None = None)`, `recover_expired(limit=100, *, allowed_user_ids: frozenset[str] | None = None)`, and `PostgresImportPublicationRecoveryRepository.claim_next(worker_id: str, lease_seconds: int = 60, *, allowed_user_ids: frozenset[str] | None = None) -> RecoveryClaim | None`. Existing positional callers continue unchanged.

- [ ] Write three red integration cases using the existing disposable `shared_database` and `store` fixtures for ordinary/expiry selection and the existing `memory_publication` fixture for recovery queue evidence. Ordinary claim must skip a disallowed earlier task, expiry must leave a disallowed expired running task/audit untouched, and recovery must skip both a due pending and an expired claimed disallowed queue row. Assert schedule, queue, tokens and audit as well as selected tenant. Add a service-independent transaction-bomb unit test for empty and malformed filters so an unavailable fixture cannot hide validation failures as skips; `None` must retain legacy database behavior and is checked by integration tests.
- [ ] Run `D:\python_self_agent\venv\Scripts\python.exe -m pytest -q tests/test_import_tenant_selection.py tests/integration/test_isolated_import_tenant_selection.py --basetemp=.runtime/pytest-isolated-runtime-p1-red-n1`; expected RED only for newly required selection behavior. Root controls the sole pytest lane.
- [ ] Add a pure validation helper or equivalent in each repository. For a shared helper, the complete contract is:

```python
def validate_allowed_user_ids(value: frozenset[str] | None) -> frozenset[str] | None:
    if value is None:
        return None
    if type(value) is not frozenset or any(
        type(user_id) is not str or not user_id or user_id != user_id.strip()
        for user_id in value
    ):
        raise ValueError('allowed_user_ids must be a frozenset of nonempty user IDs')
    return value
```

- [ ] Bind the same validated set in the initial candidate query and every later row-selection query, using `(%s::text[] is null or user_id = any(%s::text[]))` or equivalent bound SQL. For recovery, parenthesize the pre-existing pending/expired `OR` **before** adding the filter in ranked and final locked candidate predicates. The exact query shape is:

```sql
where q.user_id = any(%s::text[])
  and ((q.state='pending' and q.due_at<=clock_timestamp())
    or (q.state='claimed' and q.claim_expires_at<=clock_timestamp()))
```

Use a separate unfiltered branch for `None` or a nullable bound array; do not interpolate tenant values. Bind `sorted(validated_ids)` as a PostgreSQL text-array parameter (a Python list, not a tuple). Empty `frozenset()` returns without opening a transaction. Recheck candidate `user_id` inside the locked row path to protect against stale candidate selection. Keep fairness ordering and original lock/fresh-clock logic verbatim.
- [ ] Rerun the new file and the three existing selector suites serially. Expected: all pass, zero skipped when `POSTGRES_TEST_URL` names dedicated port 59497 and S3/Qdrant fixture prerequisites are present. Capture actual exit and XML; do not infer from console dots.
- [ ] Before implementation, Codex reviews the packet and Astra independently reviews its SQL/lock contract; only then may the packet become `ready`. After implementation and actual test evidence, Root reviews the diff and Astra independently reviews SQL precedence, lock order, starvation and zero side effects for excluded users before packet status becomes `done`. Commit only exact owned paths after gate.

### Task 02: Explicit runtime identity and distinct readiness

**Files:** Create `app/isolated_import_runtime.py`, `tests/test_isolated_import_runtime.py`.

**Interfaces:** Produce immutable `IsolatedImportSettings.from_env(role: Literal['api','worker'], env=None)`, `trusted_user_ids: frozenset[str]`, `schema`, `bucket`, `rag_collection`, `episode_collection`, `rag_profile`, `episode_profile`, and numeric timing fields; `ProviderReadiness`; `open_isolated_runtime(settings, *, provider_probe=None) -> IsolatedImportRuntime`; `runtime.recovery_ready() -> bool`; `runtime.ordinary_ready(user_id: str) -> bool`. The reviewed packet fixes the full types and environment keys.

- [ ] Write red unit cases for disabled default, missing/contradictory schema, bucket, collection, profile or allowlist, empty allowlist, unsafe retained/protected resource IDs, timeout inequality, and API versus Worker resource ownership. Assert configuration errors before network calls.
- [ ] Run `D:\python_self_agent\venv\Scripts\python.exe -m pytest -q tests/test_isolated_import_runtime.py --basetemp=.pytest-tmp-isolated-runtime-p2-n1`; expected RED on missing isolated module only.
- [ ] Implement immutable settings from explicitly named isolated environment keys and reject ordinary deployment identity reuse. Preserve secrets in existing environment/config plumbing and scrub `repr`/public errors. The startup split must follow this actual decision table:

```python
database_identity_ok = check_schema_identity(settings.schema)
recovery_ready = database_identity_ok
ordinary_ready = (
    recovery_ready and versioned_object_store_ready()
    and exact_profile_provider_report_ready(provider_probe)
    and attested_paired_baseline(user_id, settings.rag_profile, settings.episode_profile)
)
```

These names describe the decision, not existing repository APIs. Task 02 supplies no embedding engine: a missing provider hook makes ordinary readiness false. Tasks 03/04 must supply a real provider adapter proving availability and exact profiles before ordinary admission. A synthetic hook verifies only the readiness composition contract. `S3ObjectStore.check_ready()` returns None on success, so its successful call must be mapped to a boolean rather than used directly in an `and` expression.

The paired baseline proof reads published heads/receipts, History witness, and Memory for the configured identity; missing is an error, never inferred empty. Capture complete per-user database authority in short single-statement snapshots before and after external reads; validate final snapshot/document pairing itself and bind public episode membership/payloads to final receipt data. Versions alone and equal invalid before/after states cannot pass. The packet requires mutation and ABA tests plus an unfiltered full new-test-file run. Recovery remains available if ordinary providers or baseline are unavailable.
- [ ] Run focused tests and a real-schema readiness integration test added to this task's owned test file; require active isolated PG identity and do not touch protected resources.

### Task 03: Shared auth and persisted import facades

**Files:** Create `app/distributed_import_sessions.py`, `app/distributed_import_service.py`, `tests/integration/test_distributed_import_facades.py`. The current-code draft packet additionally proposes narrowly modifying `app/isolated_import_runtime.py` (opaque final-tuple admission stamp and cursor comparison), `app/postgres_import_artifacts.py` (optional transaction callback), and `app/import_publication_evidence.py` (typed unresolved gate). This amendment must pass independent review before implementation.

**Interfaces:** The draft consumes existing `SharedSession`, because current `UserSession` requires local runtime/assistant fields. Produce `DistributedImportSessions(runtime)` with `login(username,password)->str`, `get_session(token)->SharedSession`, `validate_csrf(token,csrf_token)->SharedSession`, `logout(token)->None`; `DistributedImportService(runtime,sessions)` with `submit_uploads(token,csrf_token,uploads)->ImportBatchSummary`, token-resolved `list_batches/get_batch/get_task/read_published_document`. No client-supplied user ID grants authority. Runtime proposes `prepare_admission`, `recheck_admission` and lazy `storage_clients`; artifact callback defaults to None for compatibility. Typed errors distinguish 403, current exact-user gate 409 `needs_reconciliation`/`retryable=false`, and dependency/proof 503. Packet `03-authenticated-facades.md` remains draft until its session-revocation lock order, stamp and readback contracts pass review.

- [ ] Write red two-pool tests: same cookie crosses pools, logout invalidates both, disabled/out-of-allowlist user refuses, CSRF refuses before `ImportUpload` stream consumption, tenant A cannot read tenant B task/document, unresolved gate maps only after authoritative per-user gate query, and non-gate `PublicationEvidenceError` maps to 503.
- [ ] Run `D:\python_self_agent\venv\Scripts\python.exe -m pytest -q tests/integration/test_distributed_import_facades.py --basetemp=.pytest-tmp-isolated-runtime-p3-n1`; expected RED on new facade imports.
- [ ] Compose `PostgresAuthService` and `PostgresSessionRepository`, then `PostgresImportArtifactService.submit_uploads` and `PostgresImportTaskRepository` user-scoped reads. The core admission order is:

```python
session = sessions.validate_csrf(cookie_token, csrf_token)
require_trusted_active_user(session.user_id, settings.trusted_user_ids)
stamp = runtime.prepare_admission(session.user_id)
require_successful_stamp(stamp)
summary = artifacts.submit_uploads(session.user_id, uploads,
    admission_recheck=cursor_only_same_user_stamp_and_session_recheck)
```

The callback runs in the same READ COMMITTED transaction after the active-user/gate check and before SQL batch/reference mutation. First call `sessions.recheck_csrf_in_transaction(cursor, cookie_token, csrf_token, expected_user_id=session.user_id)`: hash the original token, select its session row FOR UPDATE NOWAIT, then obtain a fresh database clock and verify persisted user, allowlist, CSRF and expiry. Retain that row lock through commit. NOWAIT avoids the expired-token cleanup/login foreign-key lock cycle; conflicts abort with sanitized 503, without restaging or automatic retry. Logout committed first refuses; admission locked first linearizes before logout. Then consume/recheck the stamp on the same cursor, with no external I/O or new connection.

`prepare_admission(user_id) -> IsolatedAdmissionStamp | None` retains the same fully validated final authority tuple, deep-frozen and registered by original object identity. `recheck_admission(cursor,user_id,stamp)` atomically consumes at most once before comparison; rollback cannot restore it. `discard_admission(stamp) -> None` is idempotent and owner-bound; the facade calls it in finally on every path after prepare, including stream/S3/gate/session failures before callback. Close invalidates all entries and prevents in-flight preparation publishing new stamps; no lifecycle lock spans external proof or acquisition of DB resources. `storage_clients()` exposes existing lifecycle-fenced lazy clients. Service construction requires `sessions.runtime is runtime` and the same API-role database identity.

A failed SQL admission may leave unreferenced immutable staged objects. Preserve InvalidSessionError/InvalidCsrfTokenError: facade invalid/expired/revoked session maps to 401, CSRF/untrusted to 403, missing/foreign items to 404. Catch only `UnresolvedPublicationGate`, allow the failed transaction to roll back/release locks, then query the exact initially authenticated user's current unresolved gate: only present evidence yields 409 needs_reconciliation/retryable=false. Cleared/missing/wrong-user gate, failed classification, base evidence exceptions and uncertain proofs yield sanitized 503. Do not expose register or use SessionRegistry local runtime.

Static implementation review S1/S2 requires a final lifecycle-fenced closed check after cursor proof, without holding that lock across DB acquisition. The narrow artifact seam may additionally emit `InvalidImportUpload(ValueError)` only from known inspection/binary validation errors, preserving original messages/base catches and ImportLimitError. Only that subtype maps to invalid_upload/400; runtime identity/configuration and non-validation storage/proof ValueError map to 503. No shared upload module change is authorized.

Readback captures active user, exact RAG index/head/full receipt, full History payload/version and witness, all relevant references and all requested-document deletion fences in one bound SQL statement. Validate final data as well as equality: queued/running or failed attempt_count<3 fences prohibit visibility. Pin the no-engine generation view to the proven full head; scan the entire unfiltered corpus against receipt count/digest and require tenant-scoped document membership. Verify returned S3 bytes' size/SHA256 against the final proven reference, including reference ABA. Safe DTO fields are document_id, display filename, suffix, size and content bytes with repr=False. Use at most one complete retry, no authority lock across external I/O. Task04 supplies the real provider adapter before Task05 can expose ordinary admission; Task03's synthetic test hook is not live provider acceptance.
- [ ] Rerun focused tests and existing PG auth/artifact suites; preserve old return defaults and no local fallback.

### Task 04: Single publication Worker and separate recovery loop

**Files:** Create `app/isolated_import_provider.py`, `app/distributed_import_worker.py`, `scripts/run_isolated_import_worker.py`, `tests/integration/test_distributed_import_worker.py`. Accepted corrective04a modifies one exception message in `app/import_publication_evidence.py`;04b modifies the existing source-delete recovery test to make its Event ordering and exact refusal deterministic. All other authority behavior remains unchanged.

**Interfaces:** Produce `open_isolated_providers(settings, *, env=None, rag_transport=None, episode_transport=None)`, `IsolatedImportWorker(runtime, providers, *, worker_id)`, `run_one_ordinary() -> bool`, `run_one_recovery() -> RecoveryOutcome | None`, `run_forever(stop_event) -> WorkerExit`, and CLI `main() -> int`. Packet04 fixes complete contracts. Providers build actual RAG/episode engines from separate explicit isolated environment mappings, verify full profile equality and supply a fresh bounded real probe. Worker composes same-database repositories and immutable per-tenant publication scopes without constructor I/O. Ordinary selection uses a freshly ready subset of trusted IDs; expiry and recovery use the full trusted set. No isolated selector uses None. Task05 consumes the same provider factory/probe.

- [x] Write red tests for source verification, real text/PDF parse, matching RAG and episode profile, exactly one original live issuance and exactly one `ImportPublicationWorkerAdapter(enabled=True).run_once(live)`, ordinary pre-intent failure, ambiguous issuance, held Unknown, success versus lease-lost heartbeat, expiry/recovery, and stop timeout with a still-running provider.
- [x] Run `D:\python_self_agent\venv\Scripts\python.exe -m pytest -q tests/integration/test_distributed_import_worker.py --basetemp=.pytest-tmp-isolated-runtime-p4-n1`; expected RED on missing Worker.
- [x] Keep the publication path singular:

```python
attempt = leases.claim_next(worker_id, lease_seconds=60,
                            allowed_user_ids=ready_subset)
source = artifacts.read_source_bytes(attempt.task.user_id, attempt.task.task_id)
points = prepare_import_document(
    source_bytes=source, task_id=attempt.task.task_id,
    user_id=attempt.task.user_id, document_id=attempt.task.document_id,
    original_name=attempt.task.original_name, suffix=attempt.task.file_suffix,
    scope=trusted_rag_scope(attempt.task.user_id), embedding_runtime=embedding_runtime)
live = publication._issue_live_publication(
    trusted_rag_scope(attempt.task.user_id), attempt, points,
    event_vector=embed_episode_event(attempt, episode_profile),
    event_profile=episode_profile)
disposition = ImportPublicationWorkerAdapter(publication, recovery, enabled=True).run_once(live)
```

The actual event text is `用户导入了文档：{attempt.task.original_name}` and profile must match C's current contract; no fixed 4D executable engine. Never call ordinary `complete`/`mark_succeeded` after adapter. Heartbeat renews both task/user authority on the original attempt; its returned snapshot does not replace `live`. Supervisory monotonic provider-stage and whole-attempt deadlines include semaphore waits, retries and multi-batch work; existing remote request limits and 75-second batch budget alone cannot establish the configured 30/300-second deadlines. On Unknown or ambiguous issuance, stop renewal and let authoritative recovery decide. On pre-intent deterministic parse failure use existing `leases.fail`; never use it after ambiguous issuance. Retain late non-daemon work, forbid new ordinary claims and report unclean shutdown within stop_wait_seconds. A bounded WorkerExit/main() return does not guarantee actual process termination while such a thread remains alive.
- [x] Implement recovery separately: `leases.recover_expired(..., allowed_user_ids=trusted)` then `recovery.claim_next(..., allowed_user_ids=trusted)` and `prove_or_hold`. Recovery renewal, if used, is serial before proof: adopt its returned claim with updated expiries; never renew concurrently during proof or manually refresh a claim. Ordinary heartbeat still preserves the original attempt. Preserve queue capture, original lease expiry wait, retry cap/manual hold, and permanent dual-UUID revocation on qualified abandon. Recovery runs when ordinary readiness is false. Typed provider-only construction refusal may compose explicit providers=None recovery_only mode, skipping all ordinary readiness/claim/storage calls; invalid isolated settings and PG/schema identity remain fatal. The packet pins the existing private leases._live combined original-authority check for pre-issue stages, preserving locks/fresh-clock checks without new authority source.
- [x] Supervise all blocking preflight/readiness/ordinary claim and recovery readiness/expiry/claim/renew/proof on separate single retained lanes, each round observed for at most provider_timeout_seconds. Late claims start no next stage or renewal; no replacement lane until actual drain. Recovery remains eligible while ordinary work is late. Include provider active-thread snapshot in final ordinary-claim check and unique shutdown accounting. One retained cleanup observer waits for borrowers then calls closers, with one shared absolute stop_wait_seconds deadline for supervisor observation. No bare blocking I/O or second close in supervisor/CLI finally. Clean proves drained work, provider close success and returned runtime.close only; the latter suppresses underlying close errors and does not prove every resource release. Packet04 pins real-PG renewal/capture/expiry and deterministic readiness/claim/close barriers.
- [x] Rerun focused tests and existing publication proof/recovery suites; Astra independently inspects issuance identity, all stopped/late paths, queue locks, and stale Worker rejection.

### Task 05: Restricted HTTP composition

**Files:** Create `api/isolated_import_app.py`, `tests/integration/test_isolated_import_api.py`.

**Interfaces:** Produce `create_isolated_import_app(settings: IsolatedImportSettings | None = None) -> FastAPI`. The route table is exactly `/healthz`, `/api/v1/auth/login`, `/api/v1/auth/session`, `/api/v1/auth/logout`, `/api/v1/imports` POST/GET, `/api/v1/imports/{batch_id}` GET as the existing DTO requires, `/api/v1/imports/tasks/{task_id}` GET, and one explicit `/api/v1/documents/{document_id}` GET readback. Draft packet05 fixes these current shapes, disables default docs/OpenAPI URLs and slash redirects, and requires route-table tests on app.routes. Current-code refresh and independent review precede ready.

- [x] Write red API tests that enumerate the registered route table and send register, cancel, retry, QA, notes, learning, reports, delete, search, disabled docs/OpenAPI and `/legacy/` requests. Require 404/405. Test cookie on a second API instance, CSRF before facade-owned stream read/storage writes (framework multipart parsing precedes handler validation), logout invalidation, tenant-bound task/document, 409 gate and 503 baseline/provider errors.
- [x] Run `D:\python_self_agent\venv\Scripts\python.exe -m pytest -q tests/integration/test_isolated_import_api.py --basetemp=.pytest-tmp-isolated-runtime-p5-n1`; expected RED on missing factory.
- [x] Build a fresh `FastAPI` lifespan with API-role runtime only; mount each allowlisted endpoint explicitly. Route handler shape:

```python
@router.post('/api/v1/imports', status_code=202)
def submit(request: Request, files: list[UploadFile] | None = File(None)):
    token, csrf_token = cookie(request), csrf(request)
    sessions.validate_csrf(token, csrf_token)  # SharedSession, server-resolved
    uploads = [ImportUpload(item.filename or '', item.file) for item in (files or [])]
    return import_batch_response(imports.submit_uploads(token, csrf_token, uploads))
```

Consume `DistributedImportSessions(runtime)` and `DistributedImportService(runtime,sessions)` from the same API-role runtime; session responses use SharedSession and its existing fields. Pass original tokens to every facade call, never a caller-provided user or SharedSession as admission authority. Resolve validation before reading bytes or issuing object writes; the facade repeats cursor-only NOWAIT session and stamp checks before commit and always discards the stamp. Preserve sanitized 401/403/404, exact current gate 409 and uncertainty 503 mappings. Public document responses expose only the safe DTO fields and approved bytes, no physical identity or proof snapshots. Consume Task04's shared `open_isolated_providers` and real probe before enabling ordinary admission, and own provider/runtime lifespan together without a duplicate synthetic provider hook. Public errors omit SQL, source text and credentials. Do not include existing `api.app.create_api_app`, full `auth.router`, or full `imports.router` because they register excluded routes.
- [x] Run focused tests, route-table check, and ordinary local API regression; verify `ApplicationServices.create()` still rejects ordinary distributed mode.

Task05 draft lifecycle owns retained opener, synchronous-handler-thread request borrowers, live provider probes and exactly one eventual closer within its two-file scope. Every owned operation signals completion in its executing finally after preserving ownership; cleanup excludes itself and observers from its wait set. ASGI-loop observers use nonblocking async waits, never sync wait/join. Firstclosing including startup timeout/cancellation/partial failure sets one absolute stopdeadline. Provider-close failure cannot skip runtime close once real borrowers/probes drain. Startup/stop observation budgets are separate from actual late-thread process termination. Typed provider configuration refusal retains auth/status with probe=None and503 ordinary admission. Health is liveness only; downloads are proof-verified binary bytes with safe attachment headers. CSRF gates import business writes and logout deletion, while existing session access refresh may precede comparison; login uses credentials. Missing files with ready admission follows facade400 import_no_files. Factory has no I/O or embedded Worker/local fallback. These precise draft amendments require independent review before implementation.

### Task 06: Isolated actual-process acceptance

Task06 accepted 2026-10-20: same-version full n5 has75 exact PASS identities (51 controlled/unit,24 real),96 exact resource absences, and separate regression n6 has330 exact PASS identities. Astra full report SHA256 f805d79d471024be4b8c156063df9b8ead48a568770b766550058f5ff441e7f9 and final regression/SPEC/QUALITY report SHA256 6ec687d64b0db752634dff0566bd533763be28a0b051e05c4dbab332d87c612f accept the recorded two-file bytes. Exact actual stage commands/results, retained OS-exit and interrupted-session boundaries are in docs/agent-work/isolated-import-runtime/task06-acceptance.md; the older command below remains a planning example rather than a separately executed run. Scoped Task06 Git delivery is separate from Task07 combined review/main/production.

**Files:** Create `tests/integration/test_isolated_import_processes.py`, `scripts/verify_isolated_import_runtime.ps1`; create evidence files under `docs/agent-work/isolated-import-runtime/` only.

Current executable contract is the ready `docs/agent-workflow/task-packets/2026-10-10-isolated-import-runtime/06-actual-process-acceptance.md`, complete independent Astra n2 report SHA256 `c9dc51fe40033996a872f9b4819ea65a8cf63ca3fbc5290e2d1f08c8fe1994bf`. It specifies24 fresh-resource business process cases:2API,9distinct committed crash boundaries,2surviving stale Worker schedules,2Unknown,2recovery pause captures,1natural transient retry,5lock schedules and1two-user fairness. Suspended Win32 creation followed by owned Job assignment/resume, restricted pipe-handle inheritance and retained actual runtime identities close the Windows launcher race; actual neutral harness smoke remains separate. Old surviving role's recovery entry is also held so it cannot preempt the replacement after its ordinary whole-attempt abort. Evidence append return is not commit; next-method entry distinguishes document/RAG/episode committed slots. Natural PostgreSQL clock controls expiry/retry, without timestamp edits. Seven actual transient backoff rounds/manual hold are not newly claimed; existing controlled regression retains its original scope. Exact methods, phase versions, IDs, budgets and commands are in the self-contained packet; neither a planned count nor plan review is execution evidence.

- [x] Provision a **new** disposable PostgreSQL schema, versioned S3 bucket, and distinct RAG/episode collections. Attest two accounts with real compatible empty History/Memory, heads/receipts and witness using existing services; report the fixture identity and never import product local runtime. Reject absent or mismatched baselines.
- [x] Run two API PIDs and one independent Worker PID against the same exact resources; exercise login/cookie cross API, CSRF refusal, two-tenant isolation, text and PDF upload, committed task/document/History/episode readback, and provider identity. The script records PIDs, process command lines, source hash/commit, schema/bucket/collection IDs, and actual exit codes.
- [x] Persist upload, terminate and wait for the submitting API, let independent Worker finish, start a separately attested replacement API and read back. Verify Worker death/replacement after claim separately from surviving old-PID barrier pause: a dead PID cannot resume. In the alive case confirm both original leases naturally expire without hidden heartbeat renewal, permit replacement recovery/fresh publication, then release the original process and prove old tuple/latevector bytes cannot become visible. Inject claim-before-intent, intent-commit-before-return, document put, each seal, terminal-commit-before-response, Unknown, held, recovery pause, ack/abandon, fresh attempt, heartbeat versus terminal/expiry/recovery lock waits, and two-user fairness. Test-only child wrappers in the new test file may observe/inject current private seams before calling actual CLI.main/API factory; happy Worker uses the direct original CLI. No public production fault hook. Process death requires actual exit of the attested Windows role tree, distinct from operation pause or health failure.
- [x] Run `D:\python_self_agent\venv\Scripts\python.exe -m pytest -q tests/integration/test_isolated_import_processes.py --basetemp=.pytest-tmp-isolated-runtime-p6-n1 --junitxml=.runtime/isolated-import-runtime/processes-n1.xml` only after root's sole-lane GO. Preserve raw command/output/exit and resource identities. No test result is accepted from just a started process, health response, or reconstructed count.

### Task 07: Combined review and branch delivery

Executed contract (combined verification accepted, scoped Git gate pending): `docs/agent-workflow/task-packets/2026-10-10-isolated-import-runtime/07-combined-review-delivery.md`, based on delivered HEAD eebe89fe00b49895f05cec587f1dee75924e9ef1. It fixes35 disjoint fresh test modules in four serial bounded stages, inherits accepted75 process identities without mechanical rerun, and compares exact historical637 identities rather than adding historical counts. It requires independent packet/runner review and root GO before any new pytest. No product/test edits, main merge or production cutover are assigned. The original planning update granted no acceptance; completed combined evidence and the current Git boundary are recorded below.

**Files:** Create `docs/agent-workflow/task-packets/2026-10-10-isolated-import-runtime/FINAL_INTEGRATION_REVIEW.md`; append evidence in `docs/agent-work/isolated-import-runtime/`.

- [x] Freeze exact source bytes and run unfiltered focused/combined regression commands with unique basetemps and raw XML/log/exit; use one pytest lane. Include old local tests and ordinary distributed refusal. The accepted old 637 baseline is compared by test identity, not added to new counts.
- [x] Astra independently reviews tenant SQL, user/task lease, issuance/adapter single execution, Unknown, stale Worker/vector fence, cross-store consistency and readback. Sol resolves concrete findings in an owned corrective packet; Luna records process/log/resource evidence. Codex performs required final combined diff review against every packet, including route allowlist and config; verdict is `accepted`, `changes-required`, or `blocked`.
- [ ] After all gates pass, commit exact intended files and push the authorized branch; record actual commit and remote ancestry. First-milestone acceptance remains distinct from fresh-user initializer, full business migration, retained-source migration and different-image rollback, each of which needs a later independent plan and production authorization.

## Self-review and scope boundary

The plan covers every section of `docs/superpowers/specs/2026-10-10-isolated-import-runtime-design.md`: separate composition/readiness, SQL allowlist, auth/import/readback, one-shot Worker, bounded lease/recovery, attested fixture, real process interruptions and evidence. Tasks01–05 plus narrow04a/04b/05a corrections are accepted; earlier RED/GREEN and draft wording describes historical planning, with exact delivery recorded in completed packets. Task04 final evidence has433unique passed cases, original corrected Worker/facade snapshots and one explicitly superseded recovery test input. Task06 has the bounded isolated acceptance recorded above; Task07 combined implementation/verification is now accepted; its scoped documentary Git delivery remains pending. No production cutover or parent-task completion follows from this plan.

## Task07 combined acceptance — 2026-10-22

Four fresh serial stages passed1020 distinct identities/3060 phases,0FES; historical637 retained/0missing/383new. Real migration-before-auth same-process interaction passes. Separate Task06 full75 inherited by187same/3documentary inputs, not rerun. Astra combined SPEC/QUALITY accepted SHA256 `3bc592f2bad997ae7d9544148d9665692695c455c51a3d8183ff9e92ed935ac5`; root final template is `docs/agent-workflow/task-packets/2026-10-10-isolated-import-runtime/FINAL_INTEGRATION_REVIEW.md`. The tested431 inputs are429tracked+2ignored,949 explicit raw bindings unchanged. Original interrupted authority-a n1 and unknown top-level final exits remain distinct. Product/test bytes are unchanged; six governance documents await exact-byte review and scoped Git delivery. No main/production or full parent-task acceptance follows.
