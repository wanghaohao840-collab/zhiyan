---
id: "2026-10-10-isolated-import-runtime-07"
title: "Review the combined isolated import delivery and verify current compatibility"
status: "done"
parallel-safe: false
depends-on: ["2026-10-10-isolated-import-runtime-06"]
base-commit: "eebe89fe00b49895f05cec587f1dee75924e9ef1"
owner: "root integration review; Sol High verification preparation; Astra High independent review"
---

# Task Packet: combined review and branch delivery

## Independent readiness — 2026-10-20

Astra High reviewed the complete draft SHA256 `b70627639696eca99c8633e3ab9bd037d4854da058ab131de0ad2370d081e206` with synchronized REVIEW/plan and returned **READY**, no blocking findings. Preserved report `.runtime/isolated-import-runtime/p7-plan-astra-n1.md` SHA256 `39dc0701419833332858c33be721917d6154872890b0f2a7c24c95f3a8f258ba`. Root read the full report. This status/paragraph is readiness bookkeeping, not changed test semantics or authorization to execute an unreviewed runner. Collection and each actual stage still need their separately reviewed runner and fresh root GO. No Task07 tests or services were executed by this readiness review.

## Goal

Produce `FINAL_INTEGRATION_REVIEW.md` with a justified `accepted`, `changes-required` or `blocked` decision for the combined Tasks01–06 and corrections04a/04b/05a. Run current-source combined regression across the authority, compatibility and isolated consumers; compare exact historical identities, preserve accepted process evidence and verify scoped branch delivery separately.

## Non-goals

No product or test behavior changes in this packet. No new migrations, fresh-user initializer, business-route expansion, retained-data/vector migration, source Qdrant service access, image rollback, main merge or production release. A concrete implementation defect requires a separately owned corrective packet and impacted retest, rather than an opportunistic fix inside review. No new architecture decision is needed to perform this accepted plan's review.

## Delivery context and prerequisites

The worktree is `C:\Users\11272\.codex\worktrees\bf20\python_self_agent`, branch `codex/distributed-cutover-vertical-slice`. Base HEAD is `eebe89fe00b49895f05cec587f1dee75924e9ef1`. All nine preceding packets (01,02,03,04,04a,04b,05,05a,06) have done frontmatter and accepted implementations. Source commits `d81f0c3`, `18e1f1b`, `913f768`, `6677299`, `020f16e`, `c225ad3` are individually ancestors of this base. Review actual full hashes and changed files, not only these abbreviations.

Root's planning baseline `.runtime/isolated-import-runtime/p7-baseline-root-n1.json` SHA256 `8f1e308aa053d8e495f5dd3123a2a35e63801c5c1accb39a08f6e7654c1681c5` binds 431 code/test/config inputs (429 Git-tracked files and two ignored Task02 validation scripts), 36 changed paths since approved predecessor `38bc8bcabd90e23a7518375d7e2087a399f0eec9`, nine packet hashes, and inherited 115/15 raw hashes. This is planning evidence, not a current test or service result. Sol inventory `p7-reality-sol-n1.md` SHA256 `c925e60d32a987f64d52bb3f2739d1b165831cca8f0663c80bb7d00dd589a776` and Astra contract inventory `p7-contract-astra-n1.md` SHA256 `05de44d83b7f478fdce34f10fd0d6fd3a1eed64d235dc0a8d3dc3cf4bbef4b6a` provide inspected current interfaces; neither accepts this packet or Task07.

Preserve these 15 pre-existing dirty paths, including EOL-only status: `app/coordination.py`, `app/document_library.py`, `app/history.py`, `app/import_models.py`, `app/import_worker.py`, `app/memory_repository.py`, `app/qa_deletion.py`, `app/qa_persistence.py`, `app/qa_repository.py`, `app/recovery.py`, `migrations/versions/20260926_01_business_schema.sql`, `migrations/versions/20260928_09_import_leases.py`, `tests/conftest.py`, `tests/test_import_worker.py`, `tests/test_user_mutation_coordination.py`. Do not reset, normalize, stage or rewrite them. Protected test files are read-only verification inputs.

Fixed interpreter: `D:\python_self_agent\venv\Scripts\python.exe`; retain separate actual Windows runtime image identity, which may legitimately be `D:\Anaconda\python.exe` behind its venv launcher.

Dedicated resources must be freshly attested before each stage. Exact Docker identities:

| Role | Container ID | Name / volume / destination |
| --- | --- | --- |
| PostgreSQL | `1223c178a3381daf0a26616d761150e8ff2287ff5ba3cd27525abf0ade7a4fe7` | `/zhiyan-dist-bf20-postgres-tests-20261002` / `zhiyan-dist-bf20-postgres-tests-20261002-data` / `/var/lib/postgresql/data` |
| S3 | `68a974561f1e456bdb0fae7a5be425301b967777779e920166cc6a508c60cb46` | `/zhiyan-dist-bf20-minio-tests-20261002` / `zhiyan-dist-bf20-minio-tests-20261002-data` / `/data` |
| Target Qdrant | `6bb7e303df12694d9094ce11398b7f05c811cb58dd70c4ba48f78ce628d4f017` | `/zhiyan-dist-bf20-qdrant-vector-target` / `zhiyan-dist-bf20-qdrant-vector-target` / `/qdrant/storage` |
| Protected source Qdrant | `bb5a56d410d7f2da22a49a853b68dfa907a35e87afd306e886f469866b14f771` | `/zhiyan-dist-bf20-qdrant` / `zhiyan-dist-bf20-qdrant-data` / `/qdrant/storage` |

Only Docker metadata for protected source; require stopped/unbound, never connect to its service. Resolve current loopback ports from the exact identities, not historical port numbers. The already reviewed helper `run-p5-sol-n4.py` SHA256 `223e910e9d462b37e007fb21e244b127eafe165ca2d6023fa4d7a294ab167c78` exposes `inspect_services()` returning safe service metadata plus five fixture keys. It may supply current health/env through that function only. Keys are `POSTGRES_TEST_URL`, `S3_TEST_ENDPOINT`, `S3_TEST_ACCESS_KEY`, `S3_TEST_SECRET_KEY`, `GENERATION_QDRANT_TEST_URL`, passed only into child environment; never save values or put them in argv/logs. No configuration of ordinary product endpoints. If services are unavailable, retain failed preflight separately; do not start pytest or infer acceptance.

## Explicit change boundary

Root may create this packet, `FINAL_INTEGRATION_REVIEW.md`, and `docs/agent-work/isolated-import-runtime/task07-acceptance.md`; modify existing `REVIEW.md`, governing source plan and `progress.md` for synchronized status/evidence. Sol may prepare only ignored `.runtime/isolated-import-runtime/p7-*` manifests/reports and `run-p7-*` verification runners, which require independent review before execution. Astra may write only ignored independent reports/audit outputs. No tracked source, existing test, fixture, dependency, migration, deployment, account data or product configuration edit is allowed. Runtime evidence remains ignored. Root owns the single pytest lane, fresh GO, actual execution and Git delivery. No parallel test lanes.

## Interfaces and combined audit contract

Read every preceding packet and handoff and the actual combined diff `38bc8bc..HEAD`. Retain coverage tables and audit each producer/consumer, including code consumed but not changed by this feature:

| Producer / consumer | Exact integration obligation |
| --- | --- |
| `app/postgres_import_leases.py:91,483`, `app/import_publication_recovery.py:266` / Worker `:159,394` | Optional keyword-only `allowed_user_ids`; validate before I/O; SQL prefilter at ranked and final locked selection; empty no-I/O, None legacy; ready subset ordinary vs full trusted set recovery/expiry. User-first locks and fresh DB clock remain authoritative. |
| `app/isolated_import_runtime.py:194,305,625,628,644` / facades/provider/API/CLI | Default-off strict schema/profile/config and roles; separate recovery vs ordinary readiness; exact object identity admission stamp, atomic consumption and transaction recheck; missing baseline never empty; typed provider refusal only recovery downgrade. |
| `app/distributed_import_sessions.py:38,58`, `app/distributed_import_service.py:209` / artifact `:79` | Original token/server session determines tenant; cursor NOWAIT session and active-user/gate admission callback before SQL commit; CSRF before business stream/write/logout deletion, permitting existing session access refresh; stamps not reusable after rollback. |
| Worker `:248,355,394` / original issuance+adapter+proof+recovery | Original attempt tuple and exactly one issuance/run_once; Unknown stops ordinary renewal and never invokes a second terminal writer; recovery renew adopts returned capture before proof; qualified abandon atomically revokes both original candidate UUIDs before new attempt. |
| Runtime `_final_matches` / facade `_READBACK_SQL` and `read_document` | Exact profile, paired heads/receipts/witness/history and full no-engine vector manifest; deletion fences, pinned object version/size/hash; final recapture checks and bounded retries; no external I/O while authority locks held. |
| `api/isolated_import_app.py:229–368` / facades+provider/runtime | Nine exact method/path pairs, no whole-router or local fallback; health liveness only; original sync-handler borrower accounting; canceled async observer cannot release another thread's borrowed ownership; one eventual closer/deadline. |
| `migrations/env.py` / ordinary auth log tests | Existing logger preservation remains enabled; exercise a real migration fixture before ordinary auth error-log assertions within the same pytest process. No schema-version feature change is claimed. |
| Ordinary `app/bootstrap.py:93` / local API regressions | Distributed mode still refuses; local auth/import/document/router/lifecycle/default behavior remains compatible. No borrowed isolated service enables ordinary distributed bootstrap. |

Review consumed migration versions for compatibility but do not call them new feature changes. Identify overwritten/duplicate responsibilities, exports/config/dependency changes, and requirement omissions explicitly. Prior findings and accepted fixes remain history; new concrete defects receive corrective packets.

## Verification contract

### Historical identity comparison and accepted process inheritance

Root recovered 637 unique full class/parameter nodeids from original XML, retained in `p7-legacy-637-identity-root-n1.json` SHA256 `71a2c165e072d5c35915892152e835b2909e8ff4ec947dd2177d7d28e36ed196`. Original files are `.runtime/distributed-cutover/restart-packet3-task9-final-n6.xml` SHA256 `9dab39e77292a8ae6560f391a826a302d0057bf2166b4e94e1ca4550aa8988dc` (430) and `restart-packet3-task9-offline-local-n6.xml` SHA256 `e44bc106297be3d136edbbfb314418dff0588d472f00b288365d692f97d2045f` (207); two disjoint historical sets on source `5d0e66a88d3f9d39231c757f3b2d07768fe51cb9`. Class names and parameter suffixes must survive conversion. The old “offline-local” label includes five service-facing integration modules; it does not authorize removing fixture env from those tests. Compare exact retained/missing/new identities with current collection and actual XML, explain every missing/renamed identity, and never use total >=637 as proof.

Inherit Task06 full n5 75 (51 controlled/unit,24 real), not rerun by default: accepted report `p6-full-accepted-astra-n5.md` SHA256 `f805d79d471024be4b8c156063df9b8ead48a568770b766550058f5ff441e7f9`, final SPEC/QUALITY `p6-final-accepted-astra-n6.md` SHA256 `6ec687d64b0db752634dff0566bd533763be28a0b051e05c4dbab332d87c612f`. Match implementation/dependency bytes against recorded stage maps. Only documentary packet/plan/progress bookkeeping may differ without invalidating reuse; report that delta rather than assert identical whole dictionaries. If code/test/fixture/provider dependency changes, define impacted retest under a corrective packet. No automatic full75 rerun. Original full n4/regression n5 stay incomplete; observed fairness n2 stays failed. Task06 regression330 is historical overlapping evidence, not an additional new-stage count.

### Exact fresh stage selections

Four separate serial pytest invocations, each unfiltered within its finite file list. The 35 module paths below are disjoint; inherited process module makes the 36-module coverage universe. Each fresh stage has an independent 3600-second hard cap plus 60-second owned-process cleanup; a cap failure preserves evidence and fails the stage, never enlarges budget or retries automatically. Old recovery/proof/evidence alone consumed about 2967 seconds, so heavier authority work is split rather than crowded into one 18-file stage.

Run these arrays in this order under the reviewed private launcher. File order within each array is fixed:

```python
STAGES = {
  "authority-a": [
    "tests/integration/test_isolated_import_tenant_selection.py",
    "tests/integration/test_postgres_import_leases.py",
    "tests/integration/test_postgres_import_artifacts.py",
    "tests/integration/test_import_publication_evidence.py",
    "tests/integration/test_import_publication_proof.py",
    "tests/integration/test_import_publication_recovery.py"],
  "authority-b": [
    "tests/integration/test_postgres_imports.py",
    "tests/integration/test_postgres_coordination.py",
    "tests/integration/test_postgres_snapshots.py",
    "tests/integration/test_postgres_memory_documents.py",
    "tests/integration/test_import_memory_fault_matrix.py",
    "tests/integration/test_postgres_vector_generations.py",
    "tests/integration/test_import_memory_consistency.py",
    "tests/integration/test_import_memory_preflight.py",
    "tests/integration/test_import_memory_publication.py",
    "tests/integration/test_import_vector_publication.py",
    "tests/integration/test_import_document_publication.py",
    "tests/integration/test_postgres_auth_sessions.py"],
  "compatibility": [
    "tests/app/test_import_publication_worker_adapter.py",
    "tests/test_import_tenant_selection.py",
    "tests/test_import_document_preparation.py",
    "tests/test_import_repository.py",
    "tests/test_import_worker.py",
    "tests/test_user_mutation_coordination.py",
    "tests/api/test_auth_routes.py",
    "tests/api/test_import_routes.py",
    "tests/api/test_document_routes.py",
    "tests/api/test_mounts.py",
    "tests/api/test_app_lifecycle.py",
    "tests/test_app_bootstrap.py",
    "tests/test_deployment_settings.py"],
  "isolated": [
    "tests/test_isolated_import_runtime.py",
    "tests/integration/test_distributed_import_facades.py",
    "tests/integration/test_distributed_import_worker.py",
    "tests/integration/test_isolated_import_api.py"]}
```

For stage `S` and new label `R`, the exact child command is the argv array:

```python
[r"D:\python_self_agent\venv\Scripts\python.exe", "-m", "pytest", "-q",
 *STAGES[S], "--basetemp=.runtime/pytest-isolated-runtime-p7-" + S + "-" + R,
 "--junitxml=.runtime/isolated-import-runtime/p7-" + S + "-" + R + ".xml"]
```

Fixed cwd is the repository. Every stage output/GO/basetemp must be new; reject collisions. No generic whole-repository pytest, xdist, `-k` exclusions, deselection, reduced assertion, fake fixture or post-hoc XML modification. Child environment clears ordinary product credential/config mappings and disables dotenv/unreviewed pytest autoload; only reviewed safe runtime values and the five current dedicated fixture keys are supplied. Do not change existing test credentials/config assertions.

Before execution, root may collect only these four arrays using `--collect-only -q`, one lane, fixed `-I` launcher and clean env without service fixture keys. This runs test imports but not test functions/fixtures; preserve before/after source hashes, actual exit, exact full nodeids and stderr hash without raw secret output. Maximum 180 seconds per collect process. A manifest must bind stage, head, selected file hashes and every full nodeid. Unknown counts are determined from that observed collection, never guessed from AST test definitions. Check the four sets disjoint and compare their union with the historical637. Collection runner needs independent review and separate root GO too; it cannot execute business tests on a collection authorization.

### Runner and actual evidence

Private Sol runner must be inert without root GO binding exact reviewed runner hash, stage, HEAD, current source map (431 planning inputs plus reviewed runner/helper/plugin/external dependencies), strict collection-manifest hash and accepted inheritance pins. Root independently recomputes expected values, not by executing candidate snapshot/GO code. Bind source/current/before/after to raw bytes, including protected files, config, conftest and resolved external pytest/sanitizer dependencies. Keep governance documents outside executable source map and separately hash them; documentary evidence updates cannot be misrepresented as tested bytes.

Retain original launcher, actual Python runtime and pytest identities (PID, creation time, image, nonce and parent lineage), actual exit codes, stdout/stderr hashes, redacted log, JUnit and all setup/call/teardown reports. Use already accepted private Win32 Job ownership and fail-safe kill-on-close; no unrelated process termination. Bounded draining and cleanup remain required. All expected identities must appear exactly once with all three passed phases, zero failures/errors/skips/internal errors, actual exits0, Job empty/no remaining owners, same before/after inputs and bound postflight services. Trusted pre-JUnit sanitization may reuse pinned reviewed policy/classes/locations; do not save secrets or post-rewrite XML. Success summaries cannot replace raw evidence.

Compatibility order deliberately runs the existing real adapter fixture/migrations before ordinary auth tests in the same process, retaining the05a logging interaction. Require its migration-bearing test(s) and normal auth error-log assertion each have passed phases in this ordered invocation, not merely in unrelated processes.

Existing legacy fixtures generate exact UUID schemas/buckets/collections and raise cleanup errors into teardown. Require complete passed teardown for all real fixtures and report observed cleanup failures precisely. Mixed historical `p4-resources.jsonl` is not automatically a per-run resource ledger; do not claim global or run-bound absence from it. If taking a pre-stage ledger snapshot, retain exact length/hash/prefix and delimit new entries honestly. Full06's96 exact absence remains inherited evidence, not a new07 absence claim. No additional historical full-service scan or destructive cleanup is authorized by this packet.

## Acceptance criteria

- [x] Prior source ancestry, all nine done packets, scoped36-path combined diff, protected15 and431 baseline input bindings are verified.
- [x] Exact current four-stage collection manifests and historical637 identity comparison exist, with every missing/renamed identity explained.
- [x] Four fresh serial stages satisfy complete strict identities/three-phase/exit/source/service/ownership contracts; migration-before-auth interaction is preserved.
- [x] Task06 process inheritance is byte-qualified, reported separately, and affected evidence invalidated if a correction changes dependencies. Historical overlapping counts are not added.
- [x] Final review uses every template section: delivered packet inventory, actual combined diff, producer/consumer audit, requirements, overlap, architecture/compatibility/persistence/isolation, combined verification, findings, explicit residual limits and decision.
- [x] Astra independently reviews packet READY, runner/collection gates and actual combined evidence/consistency; root reviews the whole combined diff. Concrete changes receive corrective packets.
- [x] Root records exact allowed Git paths, tested commit/blob relationship, actual commit/push exits and freshly read remote ancestry; main and production remain unperformed.

## Stop conditions and handoff

Before packet READY, complete independent Astra review of this full contract. No product implementation is assigned by draft status. After READY, prepare/review collection and execution runners, freeze exact bytes, then root issues fresh separate GO for each operation. Any changed prerequisite, test identity anomaly, product/fixture correction need, missing acceptance evidence or scope conflict is recorded with exact impact. Do not invent a missing exit or reinterpret an interrupted stage as a product assertion failure; preserve originals and require a separately reviewed new namespace before any new execution.

Sol handoff lists generated runner/helper/manifest hashes, exact stages/nodeids/commands, source/external/fixture bindings, limitations and any reality conflict; it never claims root execution not observed. Astra reports independent method, precise files/SHAs, SPEC/QUALITY findings, saved vs newly observed service/exit boundaries, and final decision. Root writes `FINAL_INTEGRATION_REVIEW.md`, delivery evidence and scoped docs/status updates after accepted combined results. At initial READY all acceptance boxes were unchecked. The current checkboxes and dated combined acceptance below record subsequent progress; final Git delivery remains pending, and main/production stay unaccepted.

## Combined acceptance — 2026-10-22

Root and Astra accepted four fresh serial stages:329+234+247+210=1020 distinct identities,3060 passed phases,0FES; historical637 retained/0missing/383new. Required real migration-before-auth same-process interaction passes. Task06 full75 is separately byte-qualified inherited evidence, not rerun; authority-a n1 interrupted original is excluded. Combined Astra report SHA256 `3bc592f2bad997ae7d9544148d9665692695c455c51a3d8183ff9e92ed935ac5`; full final review and acceptance records carry original hashes and limitations.

The executed packet original SHA256 `13571653a70674c82ed9ec685a4304065174ce97b213978134c09cb1f1f04f82` is preserved as ignored `p7-packet-tested-root-n1.md`. This later status/checkbox/431-input-label bookkeeping does not change test selection or contracts and is not the original tested document byte stream. Product/test/fixture/config inputs remain unchanged. Final Git acceptance box stays unchecked and status in_progress until actual scoped commit/push/remote-ref evidence; main and production remain unperformed.

## Scoped delivery observed — 2026-10-22

Six reviewed governance paths were committed as `600b325e7d185abd50a2cdb8d0e4e0c8cf7fc737`. Commit and push actually exited0; a fresh ls-remote actually exited0 and returned that exact authorized branch ref. Tested HEAD eebe89fe… is an ancestor; all431 planning raw inputs and15 protected raw paths remain unchanged. Root receipt `p7-delivery-root-n1.json` records the six-path boundary and observed results. The first add was refused by an old0-byte index.lock; no Git process existed and exclusive open succeeded, then it was preserved as `index.lock.stale-task07-20261022-063351` before the successful add. No protected source was staged.

This later status-only record closes the actual Git gate for the accepted combined delivery; it is not part of the earlier tested packet bytes or n2 documentary review binding. Main/production remain unperformed. A subsequent bookkeeping commit carries this observation and done status; its own SHA cannot be self-embedded and is checked separately at delivery.
