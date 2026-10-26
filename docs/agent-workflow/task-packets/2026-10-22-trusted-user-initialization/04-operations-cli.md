---
id: "2026-10-22-trusted-user-initialization-04"
title: "Local trusted-user initialization CLI and conditional read-only resource check"
status: "done"
parallel-safe: false
depends-on:
  - "2026-10-22-trusted-user-initialization-01"
  - "2026-10-22-trusted-user-initialization-02"
  - "2026-10-22-trusted-user-initialization-03"
base-commit: "8031bbafbac49cd149c439b1f57ab5d96d097c02"
owner: "Codex / Sol implementation / Astra review"
---

# Task Packet: Local operations CLI with read-only conditional preflight

## Goal

Deliver a local, explicit operations command that initializes a genuinely new trusted user through the accepted Task03 service or proves an already completed saved operation using PostgreSQL history. It prompts for the password twice without echo, checks existing S3 bucket versioning and both exact existing Qdrant collections only on the missing-operation path, and reports the durable result without claiming current ordinary-import readiness.

## Non-goals

- No public registration/API route, Worker/runtime startup, provider embedding probe, automatic trusted-ID allowlist edit, ordinary bootstrap, migration, deployment, main merge, or production initialization.
- No creation or mutation of S3 objects/buckets, Qdrant collections/points/indexes, PostgreSQL schema or historical accounts outside Task03's single service call.
- No Task05 OS-process/readiness acceptance or final combined integration review. Do not edit Task01–03 implementation, test fixtures, existing dirty paths, or central configuration.

## Delivery context

Task01 supplies resource-only opt-in settings and schema17 gate. Task02 supplies the atomic empty vector pair. Task03 is `done` and independently accepted: `initialize_trusted_user` performs client/PG identity and historical operation proof first, invokes a caller's `preflight()` only for an absent operation, creates the account/baseline in one PG transaction, and independently proves the completion. Task04 supplies the local password UX and the **read-only** external callback; the completed operation must replay while external services are unavailable. `durable_initialized` means the immutable PG completion proof exists, not that `IsolatedImportRuntime.ordinary_ready` would now pass: that separately requires an exact two-ID trusted allowlist and live provider/storage evidence.

The reviewed base is `codex/distributed-cutover-vertical-slice` HEAD `8031bbafbac49cd149c439b1f57ab5d96d097c02` as frozen in ignored `init-t4-baseline-root-n1.json` (SHA256 `e0ca32449038432b6fb09fdadd8e61cc53df39e7b839564f76bee89540042fcb`). Task03 delivery receipt `init-t3-delivery-root-n1.json` is SHA256 `4f74c1a9133282561871249314ff56fb19a50b97b5fa10e577db0a952fab39f2`. The baseline lists 439 existing source paths and 15 protected dirty paths, `allowed_existing_changes=[]`, and both Task04 owned files ABSENT. All 439 pre-existing source bytes must remain at their baseline hashes. The original runtime/API/process non-real regression is selected explicitly by AST to avoid a global `-k 'not real'` filtering the new focus tests.

## Relevant files and current interfaces

- `app/trusted_user_initialization_settings.py:19-22,29-104` — `TrustedInitializationConfigurationError.code`; frozen `TrustedUserInitializationSettings.from_env(env: Mapping[str,str] | None = None)` requires `TRUSTED_USER_INITIALIZATION_ENABLED=1`, holds scoped PG/S3/Qdrant resources without role or trusted IDs, and exposes `rag_identity`/`episode_identity` plus non-secret descriptor. `app/isolated_resource_settings.py:151-227` parses `ISOLATED_IMPORT_*` resource keys and validates isolated identity/pool, without dummy IDs.
- `app/postgres.py:14-70` — `PostgresDatabase(database_url, *, min_size=1,max_size=10,timeout=10.0,pool=None)` creates the original `ConnectionPool` with `dict_row`, no hooks, `open=False`; `open()` uses `pool.open(wait=True)`, `close()` closes only when `_opened=True`. The `timeout=10.0` is finite **pool acquisition**; `open(wait=True)` has its separate finite default pool-open wait (30s), so neither is a whole-call ten-second hard cap. A failed half-open `open()` needs an explicit owned `_pool.close()` attempt as in `app/isolated_import_runtime.py:511-536`.
- `app/trusted_user_initialization.py:42-60,90-139,421-486` — `initialize_trusted_user(database: PostgresDatabase, operation_id: UUID, username: str, password: str, resources: TrustedUserInitializationSettings, *, preflight: Callable[[], None]) -> InitializedUser`. The service demands exact original `PostgresDatabase` and unmodified `ConnectionPool`, pool timeout `(0,10]`, matching full DSN and actual client host/port/dbname, then exact schema17 and PG proof. `InitializedUser` contains `operation_id,user_id,username,durable_initialized=True,replayed`. `InitializationError.code` is code only; `InitializationOutcomeUnknown.operation_id` preserves the saved UUID. The absent branch alone invokes callback once at `:443-453` before the write; callback exceptions become `preflight_failed`. The service accepts a UUID object, so it cannot detect a noncanonical **original CLI string** after parsing.
- `app/object_store.py:69-80` — `S3ObjectStore(client,bucket).check_ready()` only calls `head_bucket` and `get_bucket_versioning`, requiring status `Enabled`; this wrapper has no `close()`. `app/isolated_import_runtime.py:403-420` shows boto3 path-style client parameters, but its runtime ownership and provider lifecycle must not be used here.
- `hello_agents/memory/storage/vector_store.py:406-456,502-517,761-772,829-836` — `QdrantVectorStore(url=None,api_key=None,client=None,retry_delays=None).require_collection(name,dimension,distance='Cosine')` calls `collection_exists`, `get_collection`, then checks size/distance. `ensure_collection` can create and is forbidden. Wrapper has no `close()`; close the raw `.client`. Its default constructor creates a Qdrant client without explicit timeout, and default wrapper retries, so supply finite raw-client timeout and `retry_delays=()` for this callback. Installed `qdrant_client/qdrant_client.py:83-93` accepts `timeout: int | None`.
- `scripts/run_isolated_import_worker.py:12-20,100-153` — standalone script prepends repo root before importing local app and returns integer from `main()`. It is an entrypoint style reference only; do not call/open its runtime/provider/Worker.
- `tests/test_isolated_import_runtime.py:47-365`, `tests/integration/test_isolated_import_api.py:34-692`, `tests/integration/test_isolated_import_processes.py:5719-8117` — existing non-real contracts to regress. Process `test_real_*` starts at `:2831` while `test_unit_*` resumes at `:5719`; do not skip the entire process file. `tests/deploy/` is an existing test package; the owned CLI test file is currently absent.
- `tests/integration/test_isolated_import_processes.py:7926-8122` — the selected non-real process tests include genuine local Windows neutral OS subprocesses: `test_unit_partial_startup_retires_owner[assign/resume]` may create a suspended Python launcher with designated exit 3, while `test_unit_neutral_job_and_descendant` and `test_unit_neutral_forced_tree_exit` create an inner `OwnedJob` plus actual parent/descendant Python processes (normal exits 0, forced exits 3). “Non-real” here means **no live PG/S3/Qdrant application service**, not zero OS subprocesses. The inner tests write exact per-`tmp_path` `partial-startup-<fault>.json` or `neutral-lifecycle.json` retirement/OS evidence; other unit tests in this file use synthetic owners and must never authorize outer extra PIDs.
- `docs/superpowers/plans/2026-10-22-trusted-user-initialization.md:116-136` and same-name design spec §4/§6–8 — acceptance semantics. The plan's older “Task03 proposed” wording is superseded by the accepted Task03 implementation and HEAD above. Existing changes to preserve: all 15 protected dirty byte hashes from the baseline; all 439 original source entries unchanged.

## Prerequisites

### Packet dependencies

- Packets 01, 02 and 03 are `done` at the base commit. Task03 handoff's 40 PASS focus and 117 PASS five-module regression belong to Task03 only; they do not count as Task04 verification.

### Repository/base state

- Base commit: `8031bbafbac49cd149c439b1f57ab5d96d097c02`. Both owned files absent, 439 existing source hashes and 15 protected dirty hashes match `init-t4-baseline-root-n1.json`, no permitted existing-source changes.
- Exact Task03 service/settings and the S3/Qdrant read-only interfaces above exist. No migration is run by the CLI; schema17 must already exist for a real invocation.

### External prerequisites

- Fixed repository venv `D:\python_self_agent\venv\Scripts\python.exe`; installed boto3/botocore and qdrant-client. User must have pre-saved a canonical operation UUID and provide a local hidden password twice. A **real** new-user operation would require isolated PG schema17, an existing versioned target S3 bucket and two existing compatible target Qdrant physical collections, but this packet's unit verification uses fakes and never connects to a live PG/S3/Qdrant endpoint.
- Before any runner stage, independent Astra review must mark this packet `ready`; root must freeze the exact runner/packet/test bytes and give a separate stage-specific GO. This draft and the proposed runner are review targets, not authority to run.

## Explicit change boundary

### Allowed files

- Create: `scripts/initialize_trusted_user.py` — local CLI and scoped clients only.
- Test: create `tests/deploy/test_initialize_trusted_user.py` — fake-only CLI contracts; direct `scripts.initialize_trusted_user` module import at collection.
- Modify: none. No other source, test, dependency manifest, or config path is owned by this packet. The ignored runner and its handoff are Codex review artifacts, not worker-owned product files.

### Allowed behavior changes

- Add the explicit CLI interface `D:\python_self_agent\venv\Scripts\python.exe scripts\initialize_trusted_user.py --operation-id <saved-canonical-uuid> --username <new-name>`, requiring `TRUSTED_USER_INITIALIZATION_ENABLED=1`; produce sanitized success/failure output and finite, read-only conditional resource preflight.

### Forbidden changes

- No edits to `app/`, `api/`, `ui/`, `hello_agents/`, migrations, `tests/conftest.py`, the 15 protected dirty paths, runtime/Worker/provider/allowlist or public auth entrypoints. No test import from production code and no live-service fixture in the new test.
- No `ensure_collection`, create, put, upsert, embed, provider probe, `IsolatedImportRuntime`, `open_isolated_runtime`, source/retained service endpoint, or external network I/O inside Task03's PG write transaction.
- No password argv option or new plaintext password environment mechanism; no raw argument/config/provider/SQL exception text in stdout, stderr, logs, repr or persisted evidence. Do not auto-generate a replacement operation UUID after `unknown`.

If the requested behavior cannot fit in these two files, stop with a reality-conflict report rather than editing another file.

## Interface contract

### Consumes

- `TrustedUserInitializationSettings.from_env()` and identities; `PostgresDatabase(settings.database_url,min_size=settings.pg_pool_min,max_size=settings.pg_pool_max,timeout=10.0)` with original pool; Task03 `initialize_trusted_user(...,preflight=closure)` and its result/error classes.
- `S3ObjectStore(raw_s3,settings.s3_bucket).check_ready()`; `QdrantVectorStore(client=raw_q,retry_delays=()).require_collection(identity.physical_collection,identity.profile.dimension,identity.profile.distance)` for each of rag and episode. `botocore.config.Config` supports explicit finite connect/read timeouts, retry limit and path-style addressing; raw `QdrantClient(url=...,api_key=...,timeout=10)` supports a finite per-request timeout.

### Produces

- `scripts/initialize_trusted_user.py` with `main(argv: Sequence[str] | None = None) -> int` and `if __name__ == '__main__': raise SystemExit(main())` (or equivalent testable local entrypoint). Command accepts only `--operation-id` and `--username` plus standard `--help`; input must be a canonical UUID string before converting to `UUID`. `--help` displays fixed usage/options and needs no settings, password or service.
- Success: one stable JSON object on stdout containing saved `operation_id`, returned `user_id`, `durable_initialized:true`, `replayed` and a fixed instruction that allowlist configuration is separate; exit 0. Do not emit username/password/DSN/secret-bearing descriptor or `ordinary_ready` success claim.
- Failure: fixed code-only stderr and nonzero status. Preserve distinct `busy`, `expired`, `timeout`, `unknown`, `preflight_failed`, `invalid_request`, `configuration`, `unavailable`, `conflict`, `corrupt`, `mismatch`, `failed` plus fixed CLI input/cleanup codes as needed. `unknown` includes the original saved operation UUID and no success object. No raw provider/SQL/argparse exception or unexpected exception string. A fixed `cleanup_failed` warning may accompany the primary result; it never changes a confirmed durable success into `unknown`.

### Invariants

- A committed matching operation returns via PG-only historical proof: zero S3/Qdrant client creations/probes and no new account. New operation preflight is before PG write, read-only, lazy and at most once. Resource checks are observations, not a persistent readiness guarantee.
- Always attempt cleanup of **each** owned raw S3/Qdrant client and PG pool, including partial construction/half-open PG. If a preflight probe or its cleanup raises an ordinary `Exception`, the callback fails sanitized and Task03 returns `preflight_failed` before a write. If cleanup fails after `InitializedUser` is already returned, keep exit 0 and the durable result, adding only fixed `cleanup_failed` stderr warning; if a service error/unknown already exists, retain its code and saved UUID, with at most that fixed warning. Attempted cleanup is required; successful closure cannot be promised when a closer itself fails. Finite client request/acquisition limits are not a whole-operation hard deadline.
- Interrupts before the service call (including `getpass` EOF/`KeyboardInterrupt`) are sanitized input failures with no account write. Once `initialize_trusted_user` has been entered, an interrupt or other `BaseException` reaching the CLI **without** an `InitializedUser` cannot prove rollback or safe retry: preserve the saved operation UUID and report conservative `unknown`, with no automatic lookup/write/retry. Let Task03's own typed `InitializationError`/`InitializationOutcomeUnknown` retain their exact semantics. An interrupt during cleanup after an `InitializedUser` was returned cannot erase that confirmed durable result; record only fixed cleanup warning. Do not add an alternative transaction-outcome API or duplicate Task03 proof logic in the CLI.
- Service error semantics and Task01/03 resource/pool validation remain unchanged. No allowlist or public-route mutation; no ordinary-ready claim.

## Required behavior

1. Parse only supported flags; reject missing/duplicate/unknown inputs with a sanitized parser error. Default `argparse.error` echoes unknown tokens, including `--password CANARY` or `--password=CANARY`; override/sanitize it so help/usage and all error text contain only fixed option names and codes, never raw argv. Reject a noncanonical UUID string (including uppercase/noncanonical form) **before** `UUID` conversion, password prompt or client creation; Task03 receives only the validated UUID object.
2. Load Task01 settings directly, then use standard `getpass.getpass` twice and compare. Use a local `warnings.catch_warnings` with `GetPassWarning` treated as an error so non-TTY fallback cannot read an echoed plaintext password; sanitize EOF, `KeyboardInterrupt`, warning and mismatch. Do not use a global `isatty` precondition that blocks controlled fake prompting in unit tests. Never accept password from argv/env.
3. Construct/open scoped original `PostgresDatabase` with supplied settings URL, pool min/max and finite acquisition timeout `(0,10]` (use `10.0`), no custom pool/hooks. Build a **lazy** callback and call `initialize_trusted_user` exactly once. The service decides replay before callback. Do not do a separate CLI ledger lookup, schema migration or independent account write.
4. In the callback, create raw boto3 S3 with target endpoint/region/keys, path addressing, finite connect/read timeouts and bounded retries; check bucket versioning through `S3ObjectStore.check_ready()`. Create raw Qdrant client with target URL/API key and finite timeout, wrap with `QdrantVectorStore(client=...,retry_delays=())`, then require both exact `physical_collection`/dimension/distance identities. No `ensure_collection` or write call. Ensure every created raw client receives a close attempt even if the second constructor, a probe, or a first close fails.
5. Classify all failures without leaking secret-bearing values. Preserve Task03 code classes, particularly confirmed `busy/expired/timeout` versus unconfirmed `unknown` and `preflight_failed` before write. Preserve saved UUID in `unknown`; never infer ordinary readiness. Scope interruption handling by phase: prompt EOF/interrupt before service is input failure; service-entry-to-return interruption with no `InitializedUser` is conservative `unknown` with saved UUID; after-return cleanup interruption leaves success intact. Do not infer rollback from `KeyboardInterrupt` or substitute a second write. After service result/error, attempt PG close and, if `open()` failed before `_opened=True`, attempt its owned `_pool.close()` as well. Close failure after confirmed result is a fixed warning, not a new unknown outcome.

## Implementation guidance

Keep the CLI small. Put repo root on `sys.path` before local imports as the existing standalone script does. Keep parser/prompt, external callback and output boundaries testable by monkeypatching names **in this script module**. The fake tests must not instantiate or connect real PG/S3/Qdrant; verify constructor arguments and call counts through fakes. Client timeout values can be finite local constants (for example 5s connect/10s read S3, 10s Qdrant) with bounded or disabled retries; document and assert the actual values. A request timeout bounds individual attempts, and `PostgresDatabase.open(wait=True)` has its own pool-open wait; do not label the whole command a ten-second hard cap.

Distinguish failures before the service (invalid input/settings/prompt/PG open), failure inside callback (`preflight_failed`), and failures returned by Task03. Track first sanitized outcome separately from cleanup warnings. Keep passwords out of dataclass repr, exceptions and test assertion messages. After RED acceptance, do not remove the CLI to recreate RED; keep the historical accepted RED receipt and test-byte lineage.

## Acceptance criteria

- [x] New test directly imports `scripts.initialize_trusted_user` at module collection. Historical RED on absent CLI exits 2 with exactly one attributable `ModuleNotFoundError: No module named 'scripts.initialize_trusted_user'`, JUnit one collection error and zero fixtures/executed/skipped nodes. RED test bytes and 439 original source entries remain frozen; subsequent GREEN binds the independently accepted RED receipt.
- [x] `--help`, unknown/duplicate args, `--password CANARY` and `--password=CANARY`, missing username, invalid/noncanonical operation ID, disabled opt-in, password mismatch/EOF/interrupt/non-TTY warning are sanitized; no raw argv canary, password, hash, DSN, endpoint secret or SQL/provider exception appears in stdout/stderr/repr. No PG or external client opens for rejected argv/prompt.
- [x] Production constructor arguments use exact Task01 settings/URL, original `PostgresDatabase`/native pool `dict_row`/no hooks and finite acquisition timeout; fake tests prove pool open/close and half-open failure close, plus all other client partial-acquisition and close-failure paths. No code claims a whole-call timeout.
- [x] Fake new-operation service invokes callback once: S3 `head_bucket`/versioning status Enabled and two `require_collection` calls on distinct exact physical collections with complete dimensions/distances, no external write/create/embed/runtime/provider probe/API start. Missing versioning, missing collection, incompatible dimension/distance, bounded transport fault and preflight close failure all return `preflight_failed` before fake write. Fake replay returns durable result while S3/Q client constructors would fail, proving zero external calls.
- [x] Every Task03 error code remains distinct and code-only; `unknown` alone includes saved operation UUID without success. A confirmed result followed by PG close failure still yields the same durable JSON and exit 0 plus fixed cleanup warning; an existing service error keeps its code/UUID if close also fails. Every owned closer is attempted independently. `durable_initialized` output never asserts `ordinary_ready` and never edits the two-ID allowlist.
- [x] Fake `KeyboardInterrupt`/EOF during hidden prompts returns sanitized input failure before service; fake `KeyboardInterrupt` after service entry but before result returns `unknown` with original UUID and no retry/write assumption; fake interruption during post-result cleanup retains durable success and emits only fixed cleanup warning. No test claims CLI can prove PG rollback from an uncaught `BaseException` inside Task03.
- [x] Complete focus and explicit 68-function non-real regression collect/execute with exact collected/executed node IDs, JUnit zero failures/errors/skips and all setup/call/teardown phases passed; all 439 baseline source paths and 15 protected dirty paths retain their bytes. If a selected platform skip or fixture needs a live service, stop and report a reality conflict; do not silently deselect or modify existing tests.
- [x] The regression runner retains strict four-core PID/image/creation/exit/nonce/Job-empty checks and attributes **only** five exact process test nodes (`partial[create/assign/resume]`, neutral normal and forced) to pinned original `ControlIdentity`/`OwnedJob` method calls and their exact per-node basetemp receipts. All five nodes emit one distinct receipt; selected tests pass all three phases. Exact nonce/control path, fixed argv, actual OS launcher and participant PID/image/filetime, expected 0/3 exits, owner closure/Job retirement and no remaining owners are verified. Each neutral inner Job's Python members must equal its four distinct named roles: owner launcher, parent runtime, descendant launcher named by the parent event, and descendant runtime. The parent/descendant event PID, PPID, executable/base executable, image and creation order must match the pinned venv/base interpreter model and retired OS identities; Job membership alone never authorizes an extra Python. This finite static runner also fails closed on an inner conhost or other auxiliary until an authorized run establishes a specific protocol. Each outer-observed extra in regression is either one exact nested receipt identity/exit or a pre-existing pinned Git/conhost auxiliary with exit 0; no unknown Python or other process is accepted. Neutral participants must be outer-retained; short-lived partial assign/resume may be recorded as **inner-only**, explicitly distinct from outer observation. No duplicate/reused PID, cross-node or foreign-nonce receipt may authorize another process. RED retains its original four-core plus pinned Git/conhost exit-0 auxiliary boundary; fake owner JSON is never OS evidence.

## Test and verification commands

Run only after Astra marks this packet `ready`, exact runner/packet/test bytes are frozen, and root issues a separate exact stage GO. Runner is an ignored review target at `.runtime/isolated-import-runtime/run-init-t4-sol-n1.py`; it does **not** generate GO. Run from this worktree root with the fixed venv, one pytest lane at a time:

```powershell
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t4-sol-n1.py red-focus n1
```

Expected: the exact missing CLI module collection error above, exit 2; root independently accepts and records this RED receipt before implementation. No fixture/service URL.

```powershell
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t4-sol-n1.py collect-green-focus n2
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t4-sol-n1.py green-focus n2
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t4-sol-n1.py collect-regression n2
D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t4-sol-n1.py regression n2
```

Expected: complete `tests/deploy/test_initialize_trusted_user.py` focus, then focus plus explicitly selected non-real functions from `tests/test_isolated_import_runtime.py`, `tests/integration/test_isolated_import_api.py` and `tests/integration/test_isolated_import_processes.py`. Static AST currently gives 11+17+40=68 function selectors; parameterized nodes expand during collection. The original plan used `-k 'not real'` over the three existing files only. This runner selects the 68 function paths without a global `-k`, preventing accidental exclusion of focus tests; current AST finds no `Test*` class and no selected decorator string containing `real`. If new parametrization/marker keywords alter that equivalence, stop for review. Separate collection and execution must match exact node IDs and zero skipped/failure/error XML counts; each test has passing setup/call/teardown. The regression completion must include nested receipt hashes, validation summary and separate `outer_retained`/`inner_only` attribution, and root must independently reconcile these against actual outer Job runtime/exit records; runner `identity_bound` alone is insufficient. The ignored `validate-init-t4-nested-sol-n1.py` is an advisory root validator draft, never imported as runner authority; root must independently rewrite/freeze its accepted validator before trusting a regression completion. GREEN/regression stage cap 1800s, RED/collect cap 180s, cleanup cap 60s. No live PG/Q/S3 URL/credential injection or Docker service inspection/health call is permitted; Docker executable remains only a dependency byte pin. Root creates each stage GO and independently rechecks runner/packet/baseline/dependency maps; never reuse Task03 receipts or cached counts.

## Stop conditions

Stop and report `blocked` under `docs/agent-workflow/README.md` if a cited interface or reviewed base changes; the CLI needs any third product/test file; Task03 begins invoking preflight on replay or its error semantics change; the 68-function selector no longer matches intended non-real tests; a selected test demands a real service or platform skip; safe prompting/argparse sanitation or complete owned-client close attempts cannot be achieved within the two files; source/dirty hashes drift; or runner collection/execution cannot prove the stated results. Do not quietly reinterpret the accepted architecture.

## Implementation handoff — 2026-10-26

- Packet: `2026-10-22-trusted-user-initialization-04`; status: `done`.
- Delivered: standalone local operations CLI for new-user initialization or historical replay. It accepts a saved canonical UUID and username, prompts twice through hidden input, invokes the accepted Task03 service once, and performs lazy finite read-only S3/Qdrant checks only for a missing operation. Durable JSON and code-only outcomes preserve unknown versus confirmed results and independent cleanup attempts.
- Files: `scripts/initialize_trusted_user.py` SHA256 `54a6d5527a5670c188d6fbb5ab0997ff092b0a91fbf584ca356fa7439b0cb9b0`; `tests/deploy/test_initialize_trusted_user.py` SHA256 `beac7efddce87c6e371eaa96d3397050182f9d539c94af849b52614cbc36ff31`. No existing product/test source was changed.
- Interface: `main(argv: Sequence[str] | None = None) -> int` and standalone script entrypoint. No HTTP route, public registration, runtime/provider, allowlist, migration or dependency API was added.
- Acceptance: canonical raw UUID before conversion, sanitized duplicate/unknown/password argv, opt-in settings, GetPassWarning escalation, actual wrapper checks against fake raw clients, original native pool constructor, half-open cleanup, PG-only replay, distinct typed outcomes, saved-UUID unknown on service-entry interruption, and durable success after cleanup failure/interruption all pass. Native diagnostic suppression/restoration passes the amended fake tests; standalone process-lifetime logging guard and compatibility-worker disabling are independently source-reviewed.
- Historical RED: n1 was one missing-CLI collection error, exit 2 and zero fixtures/test functions. Its test hash was `62ec75b67995364197f4ac8e6fc59b53e0ed3433064e7459e3d1397e5285fbcf`; exact bytes remain archived. Later revised test bytes are not relabeled as that RED.
- Dependency diagnostic: revised 58-node tests ran against the unfixed CLI at n2, giving 3 call failures and 4 teardown canary errors, zero skips. Root accepted this as the correction diagnostic, not GREEN. N3 then passed all 189 tests but correctly failed the original nested-console evidence gate. These historical receipts remain unchanged.
- Verification: from this worktree, fixed `D:\python_self_agent\venv\Scripts\python.exe -I .runtime\isolated-import-runtime\run-init-t4-sol-n2.py <stage> n4` for each of `collect-green-focus`, `green-focus`, `collect-regression`, `regression`, each with separate root GO. Fresh focus **58 PASS**, full selected regression **189 PASS**, zero failures/errors/skips. There are 189 distinct tests, 247 execution instances including repeated focus, and 741 passing setup/call/teardown phases. The regression includes all 68 original non-real function selectors expanded to 131 nodes plus the 58 focus nodes.
- Process evidence: exactly five original Windows process-test receipts; four named Python roles plus one native console per neutral case, designated normal 0/forced 3, partial launchers 3. All 12 nested participants are outer-retained, inner-only empty; identity/nonce/filetime/exit, closed handles, empty Job and owner records match independently. These are neutral OS lifecycle checks, not Task05 real-service acceptance.
- Root aggregate `init-t4-green-accepted-root-n4.json` SHA256 `0fff78a06706c9d24fbdd06958bdadfd73981cdebe089068279e9d7968677fe1`. Final Astra SPEC/QUALITY acceptance `init-t4-runtime-astra-n1.md` SHA256 `349db8a0f2dce16ba6c3fee3062f3cf9b698bc3bc395326712b2ed9156284f77`.
- Scope: all 439 original source and 15 protected dirty hashes unchanged; finite 6394-entry source/dependency map `db8453b27b6806b6b389fc4f53990b64f21bbd105bf6844c25fbe9bdbef12412`. No live PG/S3/Qdrant contact in Task04. No main merge or production operation.
- Deviations: accepted dependency-safety amendment prevents native logs/warnings and Qdrant's compatibility thread; accepted nested-console amendment binds the observed console to exact original OS evidence. Original READY packet/amendment bytes are archived; this handoff is post-acceptance documentary state.
- Residual: imported main restores caller state but does not promise confidentiality from residual native pool workers after return; production use is the standalone script. Finite acquisition/request waits are not a whole-command deadline. Task05 real process death/readiness, service/schema compatibility and final combined integration remain outstanding. Historical vector recall remains unproven.
- Commit: not committed at handoff update; subsequent progress/delivery receipt records the scoped commit and remote verification.

Execution READY bytes SHA256 `0544839f9af466e4cad399fe305907ca07dc54de3b2f57f275b07365b7e0cdfb` are preserved in ignored `init-t4-pre-delivery-04-operations-cli.md`. The accepted dependency and console amendment execution bytes are preserved beside it. Acceptance criteria above are read with those two explicit amendments.
