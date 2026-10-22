# Final Integration Review: isolated import runtime

- Source review: `REVIEW.md`
- Reviewed commit/worktree: `eebe89fe00b49895f05cec587f1dee75924e9ef1`, `codex/distributed-cutover-vertical-slice`; 15 preserved pre-existing dirty paths excluded from delivery; Task07 governance changes uncommitted.
- Review date: `2026-10-22`
- Result: `accepted` for the bounded isolated implementation and verification. Six-path governance delivery600b325 was pushed and remote-verified; subsequent status bookkeeping is separate.

## Delivered packet inventory

| Packet | Status | Source commit | Owned implementation | Verification |
| --- | --- | --- | --- | --- |
| 01 | done | d81f0c3494d02bca51fb312c602d215edf6f98a0 | tenant validator, lease/recovery SQL filters, two tests | Historical222; current authority/compatibility stages PASS |
| 02 | done | 18e1f1b591e767373c9f81e4fd45c5c2f5b882f8 | isolated runtime and readiness test | Historical122 distinct; current isolated210 includes consumer tests |
| 03 | done | 913f7680c3a87691c2dd2d1eff5f2de54839e13f | sessions/service, runtime/artifact/evidence seams and facade test | Historical153; current facade/authority PASS |
| 04,04a,04b | done | 66772996f77badb1e12ca12298e45e040582b2ce | provider, Worker, CLI, Worker test; gate-message and source-delete barrier corrections | Historical433; current authority/isolated/compatibility PASS |
| 05,05a | done | 020f16e6726497e861d2162ea890a83103a2d23b | restricted API/test and migration logger keyword | Historical187; current isolated and ordered compatibility PASS |
| 06 | done | c225ad3b7086934adae80b5cd06dd9b9324e0bef | process acceptance test and PowerShell dispatcher | Accepted full75 inherited; old330 regression overlaps current coverage |
| 07 | done | 600b325; subsequent status bookkeeping | this review, packet, REVIEW, plan, progress, acceptance | Fresh1020 and Astra combined accepted; scoped600b325 remote-verified |

All six source milestones are ancestors of reviewed HEAD. Historical rows describe their original bounded acceptance, not additional fresh counts. Current evidence and raw bindings are summarized in `docs/agent-work/isolated-import-runtime/task07-acceptance.md`.

## Combined diff reviewed

Compared `38bc8bcabd90e23a7518375d7e2087a399f0eec9..eebe89fe00b49895f05cec587f1dee75924e9ef1`:36 paths, plus the six Task07 governance paths in this worktree.

- Added product/entrypoint files: `app/import_tenant_selection.py`, `app/isolated_import_runtime.py`, `app/isolated_import_provider.py`, `app/distributed_import_sessions.py`, `app/distributed_import_service.py`, `app/distributed_import_worker.py`, `api/isolated_import_app.py`, `scripts/run_isolated_import_worker.py`.
- Modified existing product/migration seams: `app/postgres_import_leases.py`, `app/import_publication_recovery.py`, `app/postgres_import_artifacts.py`, `app/import_publication_evidence.py`, `migrations/env.py`. The migration change preserves existing loggers; no migration version/schema is added by this feature.
- Added seven test files and process dispatcher `scripts/verify_isolated_import_runtime.ps1`; narrowly modified existing recovery test under04b. Remaining changed paths are owned design/plan/packet/progress/acceptance records.
- Ordinary `api/app.py`, `app/bootstrap.py`, original publication service/adapter and dependency manifests are unchanged by the feature; consumed authority code is reviewed as a dependency, not counted as new implementation.
- Pre-existing15 paths listed in packet07 are raw-byte preserved and excluded from staging. No Task07 product/test/fixture/config/dependency edit occurred.

## Cross-packet interface audit

| Producer | Consumer | Contract checked | Result | Evidence |
| --- | --- | --- | --- | --- |
| tenant validator / SQL selectors | Worker claim/expiry/recovery | keyword-only trusted frozenset, pre-I/O validation, empty no-I/O/None legacy, SQL candidate and locked-row filter | PASS | `app/postgres_import_leases.py:91`, `app/import_publication_recovery.py:266`, Worker:159/394 |
| runtime readiness/provider | facade/API/CLI | default-off roles/schema/profile; typed provider refusal only recovery downgrade; missing baseline never empty | PASS | `app/isolated_import_runtime.py:194`, `app/isolated_import_provider.py` |
| PG sessions / runtime stamp | artifact admission callback | original token/server user, NOWAIT CSRF/clock, user/gate locks, exact stamp atomic pop/discard, close fence before SQL commit | PASS | `app/distributed_import_sessions.py:58`, `app/isolated_import_runtime.py:628`, `app/postgres_import_artifacts.py:79` |
| original attempt / publication adapter | Worker/recovery | one issuance and run_once, original tuple, Unknown stops renewal, renewed capture adopted before proof, dual candidate revocation before retry | PASS | `app/distributed_import_worker.py:248`, `app/import_publication_recovery.py:711` |
| published authority + retained object/vector refs | authenticated document readback | complete paired heads/receipts/witness/history/profiles, exact version/hash/size, full no-engine manifest, fences, final recapture, two attempts | PASS | `app/distributed_import_service.py` `_READBACK_SQL`/`read_published_document`, runtime `_final_matches` |
| API ownership / sync handlers | runtime/provider closer | nine routes, executing-thread borrower finally, one eventual closer, bounded async observation, no local fallback | PASS | `api/isolated_import_app.py:229`, Task06 full process reports |
| migration logging | ordinary auth error handler | real migration first, exact original api.errors assertion later in same process | PASS actual | compatibility phase indexes2<423, all247 three-phase PASS |

## Requirement coverage

| Accepted requirement | Implementing packets | Evidence | Result |
| --- | --- | --- | --- |
| Trusted tenant SQL selection and fresh clock authority | 01/04 | authority-a329 + authority-b234 | PASS |
| Explicit isolated runtime, readiness, provider and proof readback | 02/03/04 | isolated210 plus authority publication tests | PASS |
| Authenticated atomic upload and restricted API/lifecycle | 03/05/06 | isolated210 and separately inherited75 process evidence | PASS within recorded limits |
| Old Worker rejection and cross-storage publication consistency | 04/06 | authority/fault matrices, Worker tests and accepted process boundary | PASS within recorded limits |
| Ordinary local behavior/logging compatibility | 04a/04b/05a | compatibility247, real migration-before-auth interaction | PASS |
| Complete exact regression inventory | 07 | four disjoint stages1020,3060 passed phases; historical637 retained/0 missing/383 new | PASS |
| Scoped branch delivery | 07 | 600b325 actual commit/push/fresh remote ref checks all exit0 | PASS:600b325 remote-verified |

## Overlap and duplication audit

- Conflicting edits: none found within scoped combined diff; preceding15 raw changes remain excluded.
- Duplicate responsibilities/helpers: no alternate terminal writer or competing authority store; API/facade/Worker consume original authoritative protocols.
- Overwritten packet work: none found; sequential runtime extensions and04a/04b/05a amendments agree with current consumers.
- Missing central integration points: none for deliberately separate factory/CLI; ordinary bootstrap/router registration is intentionally unchanged.

## Architecture and invariant audit

- Dependency direction: isolated composition consumes repositories and original publication authority, with no ordinary bootstrap/local fallback or test hooks imported into production entrypoints.
- Backward compatibility: None selectors preserve legacy callers; typed errors preserve original message/classification boundaries; original auth assertion remains unchanged and passes after migration.
- Persistence/migration: original DB-clock/user-first/task tuples and transactional terminal semantics preserved; no new schema version; readback checks committed PG authority plus retained S3/Qdrant identities outside short authority locks.
- Data isolation: trusted SQL candidate filters and final locked tenant checks; server-session user and CSRF/stamp checks; exact tenant/object/profile/fence readback.
- Failure and concurrency behavior: stale tuples fail closed; one issuance; Unknown retains reconciliation; recovery returned capture adopted; dual candidate revocation fences late physical writes. Actual independent-process breadth and stop/close limits remain as in Task06 acceptance.

## Combined verification

Root used separately reviewed fresh stage GO followed by one `Start-Process -WindowStyle Hidden` invocation per stage. Fixed cwd is this worktree. Exact business runner commands:

```powershell
& D:\python_self_agent\venv\Scripts\python.exe -I .runtime/isolated-import-runtime/run-p7-business-sol-n3.py authority-a n2 n1
& D:\python_self_agent\venv\Scripts\python.exe -I .runtime/isolated-import-runtime/run-p7-business-sol-n3.py authority-b n1 n1
& D:\python_self_agent\venv\Scripts\python.exe -I .runtime/isolated-import-runtime/run-p7-business-sol-n3.py compatibility n1 n1
& D:\python_self_agent\venv\Scripts\python.exe -I .runtime/isolated-import-runtime/run-p7-business-sol-n3.py isolated n1 n1
```

Each runner spawns fixed-venv `-m pytest -q *STAGES[stage] --basetemp=.runtime/pytest-isolated-runtime-p7-<stage>-<label> --junitxml=.runtime/isolated-import-runtime/p7-<stage>-<label>.xml`, exact packet file order without exclusions. The original full argv is retained in child receipt; no plain command without the approved environment/GO is a standalone reproduction recipe. Five dedicated fixture keys remain child-only; no values are committed.

| Stage | Tests | Passed phases | Stage seconds | Actual retained exits0 | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| authority-a n2 | 329 | 987 | 2921.625 | 9 | PASS / Astra accepted |
| authority-b n1 | 234 | 702 | 1638.032 | 9 | PASS / Astra accepted |
| compatibility n1 | 247 | 741 | 277.110 | 10 | PASS / Astra accepted |
| isolated n1 | 210 | 630 | 731.235 | 9 | PASS / Astra accepted |

- Fresh total1020 unique/3060 phases, all zeroFES; no internal errors, timeout or truncation. Every expected class/parameter identity retained.
- Original before/after and current949 explicit dependencies equal. Planning431=429tracked+2ignored; tracked139 raw blobs and290 clean-filter blobs match testedHEAD. This is not an all-installed dynamic dependency closure.
- Each stage saved bound exact service observations and actual retained PID/creation/image/nonce/lineage/exits, Job empty/noowners. All real-fixture teardowns passed. No new global/per-run resource-absence inventory is inferred.
- Task06 process75 inherited separately:187/190 same, three governance differences only;51 controlled/24real, original96 resourceabsence remains historical. No mechanical rerun and no overlapping330/433/405/637 sum.
- Astra static SPEC/QUALITY `e6915f18d44a84abbfdb1415d308c87f306a8022dab96d5b92f309da3fd7b32d`; combined actual acceptance `3bc592f2bad997ae7d9544148d9665692695c455c51a3d8183ff9e92ed935ac5`. Root independently checked original hashes/XML/phases/maps/identities and read all four actual reports.

## Findings

### Blocking

- None for the bounded combined implementation/verification.

### Changes required

- None; no product/test corrective packet required. Documentary431-tracked shorthand corrected to429tracked+2ignored.

### Residual risks

- Original authority-a n1 incomplete after101 full identities remains INCOMPLETE_UNKNOWN_EXIT, excluded from counts. Four background top-level final exits unknown; complete original stage and actual retained inner exits support acceptance. Cross-tool survival is an observation, not guaranteed turn-abort durability.
- Cleanup elapsed fields include later validation; do not equate them to pure cleanup wall time. Passed teardown is not a new global/exact resource-absence proof.
- Runtime close may swallow underlying close errors; late third-party threads may outlive observation deadlines. Prior process harness rapid-partial/mock/one-batch breadth limits remain unchanged.
- No live remote embedding, initializer, retained-vector fidelity, multi-batch late writes/orphanGC, real seven-round retry/manual hold, whole-machine/service outage or different-image rollback acceptance. Historical migration preserves text/metadata; vector recall awaits provenance.
- Main merge, full-business migration and production cutover are unperformed. Git delivery is separately pending below.

## Decision

`accepted` because all nine predecessor packets and source ancestry, the scoped combined contracts/diff, exact four-stage actual regression and qualified process inheritance pass independent SPEC/QUALITY and root checks. This accepts the intentionally bounded isolated feature, not broader distributed production readiness. No implementation correction is required. Six-file documentary review and commit/push/remote-ref gate are complete for600b325; packet07 is done, with subsequent status bookkeeping recorded below.

## Scoped delivery observed — 2026-10-22

Six reviewed governance paths were committed as `600b325e7d185abd50a2cdb8d0e4e0c8cf7fc737`. Commit and push actually exited0; a fresh ls-remote actually exited0 and returned that exact authorized branch ref. Tested HEAD eebe89fe… is an ancestor; all431 planning raw inputs and15 protected raw paths remain unchanged. Root receipt `p7-delivery-root-n1.json` records the six-path boundary and observed results. The first add was refused by an old0-byte index.lock; no Git process existed and exclusive open succeeded, then it was preserved as `index.lock.stale-task07-20261022-063351` before the successful add. No protected source was staged.

This later status-only record closes the actual Git gate for the accepted combined delivery; it is not part of the earlier tested packet bytes or n2 documentary review binding. Main/production remain unperformed. A subsequent bookkeeping commit carries this observation and done status; its own SHA cannot be self-embedded and is checked separately at delivery.
