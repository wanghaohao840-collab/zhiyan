# Task04 dependency-safety amendment (accepted)

- Packet: `04-operations-cli.md` (`ready` remains unchanged).
- Scope: revise only the Task04 focus test contract, then allow a separate CLI correction after a tests-first diagnostic. This draft does not authorize execution or mark Task04 done.
- Owned product file for the subsequent correction: `scripts/initialize_trusted_user.py` only. The Task03 service, deployment, SQL, ordinary-ready/allowlist behavior, baseline sources, protected dirty paths, original packet, and runner retain their prior boundaries.

## Observed dependency conflict

The installed dependency bytes in the fixed `D:/python_self_agent/venv` show real output and lifecycle behavior absent from the fake-only tests:

| Installed file | SHA-256 | Direct evidence |
| --- | --- | --- |
| `Lib/site-packages/psycopg_pool/pool.py` | `04ca7cded12d4356e8ab0485d13940a321f7c5eb0cdaa266d30fc131cfe29809` | Lines 677–680 catch a connection exception and log its raw text at WARNING through `psycopg.pool`. |
| `Lib/site-packages/botocore/endpoint.py` | `0ce924a118e3ad98a2c71293541508efc1b3e0f84fb1b80272394829ba656262` | Line 265 logs the prepared request at DEBUG. |
| `Lib/site-packages/botocore/awsrequest.py` | `85d0ae028b154a991db1078c70987440c290c55b9c34f66e8ae4053de5d22c2f` | Lines 518–523 put request headers in the prepared-request representation. |
| `Lib/site-packages/botocore/auth.py` | `0713947738babdc828d1162bbe09c14df135b26108e0242fbddc2d2c52e891b6` | Lines 445–452 build the Authorization header with `Credential=...`. |
| `Lib/site-packages/qdrant_client/qdrant_remote.py` | `cdaab69f95a1ecb18403824589a80f071d586e829746f06f4f235926e39bbffa` | Lines 154–160 warn for HTTP with an API key; lines 254–264 start a daemon compatibility thread by default, which can make a separate version request and warn at 270–294. Lines 301–322 close transports without joining that thread. |

The existing CLI uses `QdrantClient(..., timeout=10)` with the native default compatibility check. The frozen test asserted that exact old keyword set. The product's fixed code-only stderr and no-secret-logs contract therefore needs the supported `check_compatibility=False` argument, plus scoped handling of dependency diagnostics across settings, prompts, client construction/probes, and all owned closes. Imported `main(argv)` must restore pre-existing logging and warning state after success and failure. `GetPassWarning` must still produce `invalid_input`. Cleanup must still attempt each owned closer, and ordinary failures/interruptions must keep their existing classification.

### Root adjudication: standalone process lifetime versus imported main

Installed `psycopg_pool/pool.py:432-463` closes with bounded joins; `_acompat.py:151-165` merely logs if a worker is still alive. A returned close therefore does not prove thread retirement. Do not introduce an unbounded join, modify the original pool, or claim that these fake tests prove native worker retirement.

The supported operations interface is the standalone script command. Root explicitly authorizes its `__main__` entrypoint to suppress logging for the remainder of that dedicated CLI process, starting before `main` and remaining installed through `SystemExit` and daemon shutdown. This state belongs solely to the short-lived CLI process and never changes a running application or Worker process. There is no restoration window in which a lingering native pool worker can emit raw diagnostics before CLI process exit. Disable the Qdrant compatibility worker through its supported constructor option, and handle synchronous dependency warnings within the scoped command call.

The imported `main(argv)` interface remains the fake-test seam, with a scoped guard that restores caller logging/warning state. It does not promise post-return confidentiality for native workers left alive after bounded or failed pool close; programmatic production embedding of this operations command is outside Task04. The standalone entrypoint lifetime guard is required in source review even though fake tests exercise `main` restoration. This distinction narrows the supported invocation boundary, without changing durable results, transaction semantics or cleanup-attempt requirements.

## Test-byte adjudication

The historical tests remain archived verbatim at `.runtime/isolated-import-runtime/init-t4-tests-reviewed-root-n3.py`, SHA-256 `62ec75b67995364197f4ac8e6fc59b53e0ed3433064e7459e3d1397e5285fbcf`. The accepted historical RED receipt `.runtime/isolated-import-runtime/init-t4-red-accepted-root-n1.json`, SHA-256 `2e0bde349c7f78b1f648ee831de632a69a69af664967ff7a5b95b3fef4985b6c`, establishes only the original missing-CLI collection failure. It is retained as history; it is neither rerun nor described as RED for the revised test bytes.

The proposed `tests/deploy/test_initialize_trusted_user.py` revision keeps all 16 original test functions and their 54 static parameter cases. The sole changed original assertion requires `check_compatibility=False` in the exact Qdrant constructor kwargs. Two new fake-only parameterized tests add four static cases: native pool WARNING during open and close on success/open failure; S3 DEBUG and Qdrant constructor/probe warnings on ready/probe failure. They require fixed output, no secret-bearing captured log, no floating warning, finite constructor args, owned-client closes, and restoration of the observed logging/warning state. The existing hidden-prompt `GetPassWarning` case remains a failure contract. No fake opens a real client, starts a thread, or makes a network call.

Static AST after this edit: **18 top-level test functions, 58 expanded parameter cases**. These numbers are source-derived, not pytest collection or execution evidence. The revised test SHA-256 is recorded in the adjacent ignored review report and must be independently frozen by root before use.

## Separate execution and delivery gates

1. Root reviews and accepts the amendment and exact revised test bytes, adjusts its finite expected focus inventory/runner and GO artifacts for the revised tests, and confirms the original baseline/dependency/protected-source byte maps. No old RED receipt is repurposed.
2. With an explicit tests-only stage GO, collect and run the revised diagnostic focus against the still-unchanged CLI. Record the actual current-product failures and no skips; this is a new tests-first diagnostic, separate from historical missing-CLI RED.
3. After that evidence is accepted, change only the CLI to prevent the Qdrant compatibility worker and keep native dependency logs/warnings from escaping across the bounded command lifecycle while restoring prior global state. Preserve Task03 service semantics and all prior Task04 outputs and cleanup rules.
4. With separate fresh GO artifacts, collect and run the complete revised focus and selected non-real regression suite, then have root independently reconcile exact node IDs, JUnit phases, nested process evidence, source maps, and hashes. Only these fresh receipts can establish GREEN/regression for the revised bytes.

No pytest, collection, runner, service, Docker, SQL, Git, or product/test import was run while preparing this draft. Standard-library AST and SHA-256 reads used the fixed interpreter.

## Acceptance record — 2026-10-26

Root accepted the exact archived execution revision after independent Astra review. Fresh N4 focus58/full regression189 passed with zero failures/errors/skips; exact OS and source evidence independently reconciled. Final Astra report SHA256 `349db8a0f2dce16ba6c3fee3062f3cf9b698bc3bc395326712b2ed9156284f77`; root aggregate `0fff78a06706c9d24fbdd06958bdadfd73981cdebe089068279e9d7968677fe1`. The earlier draft/execution text above records the historical gate; this acceptance paragraph and title update are documentary. Original exact bytes are preserved under ignored `init-t4-pre-delivery-` plus this filename. No Task05 or production acceptance is implied.
