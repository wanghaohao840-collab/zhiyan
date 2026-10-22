# Plan Review: isolated authenticated import runtime

## Current Task06 acceptance — 2026-10-20

Task06 is accepted at two implementation files SHA256 `53a0d8be05e5b303b2d9e2543a4953a59ba01389b7b715d6194d4f7bf9af81b9` / `fd4a573406d489e07a4efd490273181e60a0cc8368d48d729cb20455d406005e`: full n5 75 PASS (51 controlled, 24 real) plus distinct 330-identity regression n6, zero failure/error/skip. Astra full report SHA256 `f805d79d471024be4b8c156063df9b8ead48a568770b766550058f5ff441e7f9` and final regression/SPEC/QUALITY report SHA256 `6ec687d64b0db752634dff0566bd533763be28a0b051e05c4dbab332d87c612f` independently accept the exact recorded source/evidence; root read both. Full's 96 exact resource absences are separate from regression's existing fixture/teardown contract. The unavailable original n6 terminal is not a missing-result run: its complete retained OS-exit/completion/XML/log evidence remains; older incomplete runs are not promoted. See `docs/agent-work/isolated-import-runtime/task06-acceptance.md` for hashes and limits.

Task07 combined SPEC/QUALITY and all four actual stages are accepted; its scoped documentary Git delivery remains pending. Main and production remain open. The earlier dated verdicts below are retained chronology, not current Task06 status.

- Source plan: `docs/superpowers/plans/2026-10-10-isolated-import-runtime.md`
- Approved design: `docs/superpowers/specs/2026-10-10-isolated-import-runtime-design.md`
- Reviewed commit: `38bc8bcabd90e23a7518375d7e2087a399f0eec9`
- Review date: 2026-10-10
- Verdict: Tasks 01–02 are accepted and done at source commits `d81f0c3494d02bca51fb312c602d215edf6f98a0` and `18e1f1b591e767373c9f81e4fd45c5c2f5b882f8`. Task03 exact six-file implementation passed independent Astra final SPEC PASS / QUALITY PASS and is done at `913f7680c3a87691c2dd2d1eff5f2de54839e13f`; root verified raw commit binding and remote delivery. Task04 passed independent Astra n2 ready review; Tasks05–07 remain proposed work, not ready packets.

## Repository evidence

- Relevant implementation:
  - `app/postgres_import_leases.py:90` — ordinary `claim_next(worker_id, lease_seconds=60)` scans active users by `import_user_schedule`, then takes a short transaction per candidate, locks user before task, acquires user lease, and writes task/audit/schedule.
  - `app/postgres_import_leases.py:468` — `recover_expired(limit=100)` scans all running tasks, then locks user, user lease and task before audit/evidence checks and expiry mutations.
  - `app/import_publication_recovery.py:265` — recovery `claim_next(worker_id: str, lease_seconds: int=60)` ranks due pending or expired claimed queue rows per user, locks the final queue candidate, commits a short capture, then obtains original authority in another transaction. Both ranked and final predicates need the tenant filter; the pending/claimed `OR` needs complete parentheses.
  - `app/import_publication_recovery.py:41` — `_report_unavailable` translates connection failures; pure allowlist validation must still raise `ValueError` before database use.
- Relevant tests:
  - `tests/test_import_tenant_selection.py` — new service-independent transaction-bomb unit cases must run in a subprocess without the five integration fixture environment keys; `None` is checked with a real database by integration cases.
  - `tests/integration/test_postgres_import_leases.py:15` — `submit` helper and existing ordinary claim, fairness, expiry and stale-handle cases.
  - `tests/integration/test_postgres_import_artifacts.py:21` — `fixture` makes two active UUID users, one disabled user, two pools and an immutable store.
  - `tests/integration/test_import_publication_recovery.py:1570` — existing evidence/queue expiry and recovery claim path. The actual recovery suite filename has no `postgres_` prefix.
  - `tests/integration/test_import_memory_publication.py:36` and `tests/integration/test_import_publication_proof.py:75` — `memory_publication` and `_issue` prepare real evidence and a recovery queue for queue-selection tests.
  - `tests/integration/test_postgres_auth_sessions.py:28`, `tests/integration/test_s3_object_store.py:15`, `tests/integration/test_import_document_publication.py:40` — disposable PG schema, versioned S3 bucket, and target Qdrant collections are fixture prerequisites; absent endpoints skip tests.
- Configuration/runtime facts:
  - The repository venv is `D:\python_self_agent\venv\Scripts\python.exe`. No pytest was run in this review; the root controls the only pytest lane and will give a separate GO.
  - The observed service snapshot `.runtime/distributed-cutover/isolated-runtime-services-root-20261010-n1.json` records disposable PG on loopback 59497, S3 on 59498, target Qdrant on 59539, and protected source Qdrant exited. This is a recorded observation, not current service health or test acceptance.
  - Fixture environment keys are `POSTGRES_TEST_URL`, `S3_TEST_ENDPOINT`, `S3_TEST_ACCESS_KEY`, `S3_TEST_SECRET_KEY`, and `GENERATION_QDRANT_TEST_URL`. Test commands must target the exact disposable endpoints and use a unique basetemp.
- Existing worktree changes to preserve:
  - The 15 pre-existing modified paths are `app/coordination.py`, `app/document_library.py`, `app/history.py`, `app/import_models.py`, `app/import_worker.py`, `app/memory_repository.py`, `app/qa_deletion.py`, `app/qa_persistence.py`, `app/qa_repository.py`, `app/recovery.py`, `migrations/versions/20260926_01_business_schema.sql`, `migrations/versions/20260928_09_import_leases.py`, `tests/conftest.py`, `tests/test_import_worker.py`, and `tests/test_user_mutation_coordination.py`. Status may report them modified even when a normal diff is empty; do not reset, normalize or rewrite them.
  - The source plan itself was pre-existing untracked work before this review.

## Findings

### Blocking

- None for drafting packet 01. Its assignment remains gated on independent Astra review of this packet and any resulting corrections.
- Task04 passed independent ready review of the four-CREATE-file provider amendment, ready-subset ordinary selector, typed provider-refusal recovery_only composition, private combined-authority coupling and bounded late-work reporting. Root assigns Sol High; runner review/GO precedes pytest and source follows genuine pre-source RED. This is a contract gate, not implementation acceptance.
- Astra n1 `p4-plan-astra-n1.md` SHA256 `e4c0488fc2ce57935b4b96a70b1dd0a9c5e3c303e47eb58ed68b20e1b15489a6` required R1/R2. Root revised recovery renewal to serial returned-claim adoption without concurrent proof renewal, and specified single retained preflight/recovery/close lanes with observation deadlines, late-claim refusal, probe liveness and one total stop deadline. Clean reports runtime close completion only, not independently proved underlying resource release. Real-PG expiry/renewal/capture and deterministic readiness/late-claim/close barriers were added. Astra n2 p4-plan-astra-n2.md SHA256 380bc17855ed1786a86f9ff9dcf22ba612c3d52bf28dc54a9b5d4942d7b52160 independently closed R1/R2 and judged the complete four-document contract READY. Source/runtime acceptance remains separate.

### Required revisions

- The plan's Task 01 fixture sentence was revised: ordinary and expiry cases can use the existing two-user artifact fixture; recovery queue cases need the existing `memory_publication` evidence fixture or equivalent valid persisted evidence.
- The plan's Task 01 gate was revised to separate pre-implementation `ready` from post-implementation `done`. Neither status is earned by the current document-only work.
- Task 02's current-code and independent contract review are recorded in `02-runtime-readiness.md`; its new isolated interfaces are reviewed producer contracts, not existing implementation. Tasks 03–07 require fresh interface review after their predecessors.

### Non-blocking notes

- The first packet is limited to SQL tenant selection. It makes no claim that an isolated API, Worker, business migration, rollback, or production route is operational.
- The source plan's test steps describe intended RED/GREEN evidence. This review observed code and fixture definitions but ran no tests.

## Accepted scope

- Goal: permit the existing ordinary claim, ordinary expiry scanner, and publication recovery claim to select only explicitly permitted users at SQL candidate selection time.
- In scope: three optional keyword-only `allowed_user_ids: frozenset[str] | None = None` parameters, exact input validation, parameter-bound SQL filters, locked-row rechecks, and focused integration tests.
- Out of scope: runtime configuration, API/session facades, Worker loops, publication execution, schema changes, deployment, protected source Qdrant, and any production cutover.
- Compatibility requirements: `None` preserves existing positional calls and unfiltered behavior; an empty valid frozenset returns `None` or `0` without a transaction; malformed values fail before database access.
- Architecture/data-isolation constraints: filter in the initial and final SQL selectors before claim/expiry mutation; preserve user-first authority lock order, fresh DB clock after locks, fairness without a candidate cap, short recovery capture transaction, gate/evidence checks, and exact task/user lease tuples.

## Packet graph

| Packet | Depends on | Parallel-safe | Owned files | Outcome |
|---|---|---:|---|---|
| `01-tenant-selection.md` | none | no | `app/postgres_import_leases.py`, `app/import_publication_recovery.py`, optional `app/import_tenant_selection.py`, `tests/test_import_tenant_selection.py`, `tests/integration/test_isolated_import_tenant_selection.py` | SQL tenant prefilter for ordinary claim, expiry, and recovery |
| `02-runtime-readiness.md` | `01-tenant-selection.md` (done) | no | create `app/isolated_import_runtime.py`, `tests/test_isolated_import_runtime.py` | Reviewed explicit isolated configuration, resource ownership, and independent recovery/ordinary readiness |
| `03-authenticated-facades.md` (done) | `02-runtime-readiness.md` (done) | no | sessions/service/new facade test; narrow runtime/artifact/evidence seams | Shared sessions, atomic admission and no-engine published readback; Astra final SPEC/QUALITY PASS |
| `04-independent-worker.md` (done) | `03-authenticated-facades.md` (done) | no | provider, Worker, CLI and new Worker test (four files) | Independently accepted with04a/04b corrections:433unique passes, exact92input lineage, final Astra SPEC/QUALITYPASS |
| `07-combined-review-delivery.md` (in_progress: Git pending) | all01–06 and04a/04b/05a done | no | final review and scoped governance/evidence only | Current-head combined verification, exact637 identity comparison, qualified06 process inheritance; 1020 fresh PASS/3060 phases,637retained0missing383new; separate process75 inherited; Astra combined3bc592f2 accepted; Git gate pending |
| `04a-gate-message-compatibility.md` (done) | Task03 done | no | one evidence exception message | gate16/facade41, corrected Worker72 and finalregression320 allpassed; typed error/query/locks preserved |
| `04b-source-delete-test-barrier.md` (done) | Task03 done; serial correction before Task04 acceptance | no | existing recovery test source-delete parameter only | Event barrier/exact missing-source refusal/common absence checks accepted; focused2 and fullregression320 passed |
| `05-restricted-api.md` (done) | Task04/04a/04b done | no | isolated API factory and dedicated API test | FinalSPEC/QUALITYPASS,187distinct passed cases, exactcorrectedinput binding |
| `05a-migration-logger-preservation.md` (done) | Task04 done; serial correction before05acceptance | no | migrations/env.py one keyword | Enables unlisted app loggers; unchanged original auth logging assertion passes |
| `06-actual-process-acceptance.md` (done) | Task05/05a done | no | process acceptance test and PowerShell dispatcher | Accepted75 full and330 distinct regression identities; c225ad3 source andeebe89f delivery records on authorized branch |

## Packet readiness audit

| Packet | Goal/non-goals | Context/interfaces | Prerequisites | Change boundary | Acceptance/tests | Forbidden changes | Handoff format | Ready |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `01-tenant-selection.md` | yes | yes | yes | yes | yes | yes | yes | yes |
| `02-runtime-readiness.md` | yes | yes | yes | yes | yes | yes | yes | yes |
| `03-authenticated-facades.md` | yes | yes | yes | yes, six files | yes | yes | yes | yes |
| `04-independent-worker.md` | yes | yes | yes | four CREATE files | yes | yes | yes | yes, Astra n2 |
| `05-restricted-api.md` | yes | verified Solcurrentn2 | yes | two CREATE files | explicit routes/lifecycle/real+local tests | yes | yes | yes, Astra n2 |
| `06-actual-process-acceptance.md` | yes | Sol/Astra inventories integrated | yes | two CREATE files |24 proposed actual-process cases plus separate Windows harness smoke; exact commands | yes | yes | yes, complete Astra n2 |
| `07-combined-review-delivery.md` | yes | current Sol/Astra inventories | nine done packets/source ancestry | governance only, no source or test changes | 35 fresh files/four stages, inherited process75; strict collection first | yes | yes | yes, Astra39dc0701 complete READY |

Task 01 passed its earlier ready gate, was implemented, then independently accepted. The completed packet records final GREEN 28 and regression 194 with actual exits 0; the root's accepted delivery binding is `.runtime/isolated-import-runtime/p1-delivery-root-n3.json`, and Astra's final Spec/Quality PASS report is `p1-final-accepted-astra-n4.md` (SHA256 `d911fcc66e1c920034cf8e57752fe9836e8049fb03dffe846b14286f81004cd5`). Task 02 passed independent plan review: `.runtime/isolated-import-runtime/p2-plan-astra-n2.md`, SHA256 `16130c01e2e8c57a4c4300eb8ed173838ec2cd19a8565af4312077331a686553`, binding the complete pre-bookkeeping packet `a8a89fb5bde13e528b8c32bfb3acd1a94b808e999808cc203327c8d83fd4c791` and source plan `7a05589b77990a8d0940b1b52628af7b52b30f3399e10fedaaae09405e38988c`. Task 02 is now implemented and accepted on the exact source/test bytes: Astra final Spec/Quality PASS report `p2-final-accepted-astra-n4.md` (SHA256 `1ee8dfa7a3bc225b88b42c6b15bb5f1eab73e0c1d14fe7e11f7d271b3c63cd26`) and root `p2-delivery-root-n4.json` verify complete new-file 71 plus regression 51 unique cases, actual exits 0 and zero failures/errors/skips. The earlier reviewed readiness contract remains unchanged; synthetic provider reports do not establish real provider/admission acceptance. Its source base is `d81f0c3`; root's later progress-document commit `181e2d9` is a descendant and changes no source interface. Readiness bookkeeping does not change the independently reviewed contract.

## Integration verification

- After root grants the sole pytest lane, first run the packet's service-independent transaction-bomb unit file in a child process with only the five fixture environment keys removed, then run the new integration file and the existing `tests/integration/test_postgres_import_leases.py`, `tests/integration/test_import_publication_recovery.py`, and `tests/integration/test_postgres_import_artifacts.py` with the fixed venv, disposable PG/S3/target Qdrant endpoints, distinct basetemps, JUnit XML, actual exit codes, and zero skips. The exact serial commands are in packet 01.
- At final integration, also verify ordinary local behavior and ordinary distributed bootstrap refusal with commands chosen from then-current tests; do not count today's document review as runtime validation.

## Final integration review requirement

- Output: `docs/agent-workflow/task-packets/2026-10-10-isolated-import-runtime/FINAL_INTEGRATION_REVIEW.md`
- Required after: every implementation packet is `done`.
- Result must be: `accepted | changes-required | blocked`.
- Required checks:
  - cross-packet interfaces
  - missing requirements
  - duplicate or overlapping implementation
  - central integration points
  - architecture, compatibility, persistence, and isolation
  - combined regression verification

## Open decisions

- Task05/05a final accepted: AstraSHA5b03ca5087cee41c58ac7036167104679805ddc4b91f22d140773d70ed0e3f13 SPEC/QUALITYPASS; SolhandSHA6aa010fd640df78efc546894f49b80f7d0a131103c34e1ddbfa7be49afaa7549; deliverySHAa3c7b8378459ef50e830e5687aad5e62c258b3f6c9e5cd574937280508c2b434. Four final stagesactual0/zeroFES,26+161=187distinct;127round snapshots equal at review,113accepted+2new and15protectedsame. Prior chronological decisions below retained. Task06 PID/restart requires freshpacket; wholeplan/main/production unaccepted. Sourcecommit/remote-ref require separate receipts.

- Task05 final regression n3 actual1,161cases160pass1failure: existing auth ERROR capture missing after preceding Alembic upgrade. Independent Sol/Astra same-ini child experiments identify defaultfileConfig disabling existing api.errors logger; no newAPI code is selected in that regression. New05a draft owns only migrations/env.py one keyword preservation, no logger assertion weakening. Explicit supplement adds env.py/api/errors.py/alembic.ini lineage outside historical122snapshots; old snapshots are not rewritten. New corrected input baseline and all final stage reruns required; Task05/05a remain unaccepted.

- Task05 n1 independent Astra review p5-plan-astra-n1.md SHA995777a637620c5412f83cb2cdd798a8ab16447c24c6902a748d1fea3652b7ac CHANGES REQUIRED: R1 preserve existing session refresh before CSRF while blocking import business writes/logout deletion; R2 optional multipart files reaches facade400, admission503 precedence unchanged; R3 per-operation completion ownership signals, synchronous-thread borrower cancellation accounting, no cleanup self/observer wait, nonblocking async observation/one absolute deadline, provider-close exception still attempts runtime close, deterministic ticker/cancellation/late-op tests. Root revised packet/sourceplan/design together and requested n2review; draft/no implementation until ready. SolMedium current inventory p5-reality-sol-n2.md SHA9e85ad58a0184c634387dc0112593ec41c9b3d680a509d2426de528bb043c7f5 confirms interfaces; private root baseline110 inputs preserves predecessors/15dirty, no product/runtime acceptance.

- Final04/04a/04b gate closed: Astra p4-final-accepted-astra-n1.md SHA1c4e731684a14931cee88a18917b41b1b27273ead4202ea772146f7f69dd0b02 SPEC/QUALITYPASS. Sol handoffSHA364d336a539af388e9a99a98356259668aae2eeaee0ce92976875866efc90172 and deliverySHA b333e71d155a404e945cb3c783ec698bd36312b6426e6833b6a124a2a8564364 bind433unique tested cases with explicit one-test supersession, all other91source inputs unchanged and15protected paths preserved. Earlier paragraphs below describe retained chronological decisions. Task05 packet may now be prepared from current interface inventory; Task06–07 and production remain open. Source commit/remote delivery require their own receipts.

- 2026-10-12 correction: final Task04 regression n3 actual1,319pass/1fail outof320 (zeroerror/skip). The source-delete race test omitted the legitimate ImportDocumentPublicationError parent; its fixed-delay timing allows multiple legal rejection stages. Root-created04b owns only this existing test, with no production behavior change. Task04/04a remainin_progress. A fresh focused pair and six-file320case regression are required; historical Worker/facade stages remain tied to their original snapshots, with any one-test-only supersession stated explicitly rather than pretending all dictionaries match. Failed evidence and15protected paths remain intact. Task05 implementation stays gated by Task04 acceptance.

- Tasks01–02 are accepted. Task03 corrects the earlier local UserSession return type to SharedSession and adds three narrow current-source seams for atomic admission and typed gate errors. Astra n1 report `p3-plan-astra-n1.md` (SHA256 `63ed45e3268438d1d768715345f0491271d827a8a805d37d1143d0f38dbd5ab1`) required R1–R4: cursor-only NOWAIT session validation avoids login cleanup/FK lock cycle; final readback includes deletion fences and source-byte/reference binding; original-identity stamps have atomic consumption/discard/close lifecycle; precise errors and same-runtime constructors are specified. Independent n2 report `p3-plan-astra-n2.md` (SHA256 `a67ed9ab4525eb59020cfce41ecc11039096ca5f49ade36c5184baca9a9c8994`) closed all four and judged the revised packet plus Task05 consumer READY. Root assigns Sol High within the six-file boundary; runner review/GO still precedes pytest. This is implementation authorization, not source/test acceptance. Tasks04–07 still require fresh packets after accepted predecessors.

## Task07 combined review — 2026-10-22

Root final review `FINAL_INTEGRATION_REVIEW.md` accepts bounded combined implementation/verification. Astra combined report SHA256 `3bc592f2bad997ae7d9544148d9665692695c455c51a3d8183ff9e92ed935ac5` closes static and actual SPEC/QUALITY. Planning431 inputs contain429tracked+2ignored helpers; original949 raw mappings are unchanged. The original executed packet SHA13571653… is archived before documentary changes. Six scoped governance paths await exact-byte review and actual commit/push/remote confirmation; packet07 stays in_progress for that gate. Earlier dated pending statements are chronology. Main/production and retained-vector migration remain unaccepted.
