# Local RAG and personal Memory BGE-M3 implementation

The user approved reusing the existing SiliconFlow BGE-M3 integration on
2026-11-06. This record covers implementation in the current checkout.

## Implementation

- [x] Reuse `SiliconFlowEmbedding` through a Memory `encode` adapter.
  `MEMORY_EMBEDDING_PROVIDER` controls activation independently of RAG.
  Other `MEMORY_EMBEDDING_*` values override corresponding `RAG_EMBEDDING_*`
  values, including credentials. Long texts use normalized mean pooling of
  bounded 1500-character segments, included in the embedding fingerprint.
- [x] Configure episodic and semantic stores with the actual embedding
  dimension and a fingerprint-specific collection name.
- [x] Restore this user's vectors from snapshot text into the new collection.
  Rebuild failures clear partial vectors and raise an error. Original snapshot
  content and other users' vectors are preserved.
- [x] Propagate embedding failures during initialization, writes and retrieval.
- [x] Verify model selection, dimensions, long inputs, restart recovery,
  user isolation and failure behavior with mocked HTTP responses.
- [x] Pass a real provider probe using the existing deployment credentials.
- [x] Back up local configuration and data, stop local writers, inventory all
  user JSON caches through `build_app_inventory`, rebuild and validate candidate
  indexes using the existing candidate APIs, and perform journaled cutover.
- [x] Configure root `.env` for JSON RAG with BGE-M3 and personal Memory for
  BGE-M3. Configure deployment Memory after its probe succeeds. Verify normal
  entry points and real retrieval after restart.

## Current verification and remaining work

The existing deployment credential returned HTTP 402 from the real embedding
probe. Live activation and data migration therefore remain pending. Root
`.env` and deployment `.env` have not been activated for these two use cases.

The local data inventory found three legacy JSON caches with 120 chunks in
total, and three user memory snapshots with 10 records in total. Deployment
Memory has another snapshot with 10 records. These counts describe the
inspection time and must be rechecked under maintenance before migration.

Run the real probe after restoring account access with
`venv/Scripts/python.exe -m deploy.embedding_probe --env-file deploy/.env --probe`.
Proceed with migration only after `probe_passed`. Secrets and runtime data
remain in ignored local files.

Initial regression command
`venv/Scripts/python.exe -m pytest tests/memory tests/test_memory_repository.py tests/test_user_runtime.py tests/deploy/test_embedding_probe.py -q --basetemp=.pytest-tmp-memory-bge-regression`
passed 555 tests. Additional failure-path tests were added afterward and require
the final verification recorded below.

## 2026-11-10 verification

The real provider probe again returned HTTP 402 with `retryable=false`.
Live activation and historical vector migration are still pending provider
access. The user was asked to restore account access or update the key locally
in `deploy/.env`.

The complete affected regression selection passed **666 tests in 35.51s**.
It included `tests/memory`, snapshot repositories, user runtime, corruption
recovery, legacy migration recovery and deployment embedding probes. The test
subprocess used `NO_PROXY=localhost,127.0.0.1` and disabled Gradio analytics
because the inherited proxy exclusion entry caused httpx URL parsing failures.
This environment adjustment was limited to the test subprocess.

Compilation and task-scoped `git diff --check` passed. The adapter and recovery
code are ready as an independently verified milestone. End-to-end replacement
is incomplete until the live probe, candidate migration and activation pass.
Memory vectors are rebuilt from snapshot text on each user runtime restoration,
which incurs provider calls when the remote provider is enabled.

## 2026-11-24 outcome

- SiliconFlow returned three 1024-dimensional vectors in the real probe.
  On 20 retrieval cases, Recall@5 improved from 0.50 to 1.00 and MRR@5 from
  0.2283 to 1.00, with zero scope leaks.
- The local JSON migration rebuilt 120 chunks across four documents. It
  validated content and ownership, kept the original caches, and completed
  cutover `local-bge-m3-20261123`. Eight existing accounts had no history file.
  Their directories were checked for data before creating empty histories.
- With the new local configuration, a fresh runtime loaded 15 accounts,
  retrieved from all four documents, and rebuilt and recalled eight episodic
  and semantic memories. The local API startup returned HTTP 200. The local
  checks used an adjusted `NO_PROXY` in the test process because the inherited
  value caused a URL parsing error.
- Independent Astra review found missing recovery guards and a first-login
  interruption window. The fixes prevent writing snapshots after a failed
  restore, guard episodic writes when SQLite or compensation fails, and
  publish a new account's empty index and history together. The reviewed
  recovery and migration regression selection passed 112 tests.
- The first Docker startup attempt exposed a synchronous embedding call from
  the FastAPI event loop during startup. The previous image was restored.
  Startup and shutdown now run in the thread pool. The lifecycle and
  startup regression selection passed 20 tests.
- The deployed image `sha256:82dcae87aef73609ded8b1fcbb5b25024c3a9007d568e43b62c1fd847bbc89e3`
  uses the previous release image as its base and applies the task's Python
  changes. The app and Qdrant passed their Docker health checks. A live
  runtime loaded the deployment account, searched all four documents, and
  rebuilt and recalled ten personal Memory vectors. The existing Qdrant
  document index already used BGE-M3. Personal Memory continues to rebuild
  its in-process vectors from snapshots during runtime restoration.
- Backups include the local data archive, previous configuration files, and
  the deployment's paired application and Qdrant cold archives. They remain
  on the host outside Git. The deployment evidence is recorded locally in
  `.runtime/embedding-20261123/deployment-result.json`.
