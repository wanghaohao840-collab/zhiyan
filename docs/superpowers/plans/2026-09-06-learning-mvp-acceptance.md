# Learning MVP acceptance

Delivered on branch `codex/learning-mvp`, based on `bbbe026`.
Implementation commits end at `2378f05`.

## Delivered behavior

- Per-document deterministic study plans, completion tracking and due dashboard.
- Source-grounded cards and multiple-choice/short-answer exercises, batch sizes 3/5/10.
- SM-2 scheduling, automatic choice grading and explicit card/short-answer self-rating.
- Per-user atomic `learning.json`, corruption rejection and deletion cascades.
- Gradio learning center with existing-item refresh, readable task selectors and answer reveal gates.
- Runtime sharing with session-local document selection; retrieval/LLM calls outside the user lock.
- Thread-local RAG response metadata prevents concurrent requests overwriting each other's evidence.
- Actionable corruption/model-configuration errors and sanitized cleanup failures.

## Verification

Python executable: `D:\python_self_agent\venv\Scripts\python.exe`.
All commands ran from the isolated worktree with repository-local `--basetemp`.

| Check | Result |
|---|---|
| Full project suite, snapshot `27e9080` | **988 passed, 7 skipped**, 877.19 s |
| Backend learning + assistant + two-user acceptance | **165 passed** |
| UI contracts + import acceptance + golden evaluation | **75 passed** |
| Final recovery/error changes: UI + assistant + coordination | **77 passed**; concurrent test fixture then corrected |
| Final concurrency fix: search + multi-document + graph tests | **58 passed** including corrected concurrency test |
| Final persisted-file authentication guards | **21 passed**, 155 deselected |
| Real Gradio module construction | **77 event bindings**, learning center and four subtabs present |
| Final independent review at `2378f05` | **Approved**, no remaining concrete blockers |

Full suite command:

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --tb=short --basetemp=.pytest-tmp-learning-delivery --junitxml=.superpowers/sdd/full-regression.xml
```

Seven skipped checks require a live Neo4j service (one), an explicitly configured live Qdrant service (five), or Windows symbolic-link privileges (one). Offline RAG and isolation coverage passed.

The existing golden evaluation fixture `evals/data/multi_document_qa.json` is ignored by repository rules and absent from a clean worktree. For this run its JSON content was restored from the main workspace's existing fixture; it was not added to this feature's tracked changes. No credentials or live-service data were used by the new learning acceptance test.

Full-suite collection preceded the final review corrections. The corrections were verified with the targeted suites above; the full suite was not unnecessarily repeated.

## Remaining MVP limits

Single-document generation only; no notification scheduling, manual material editing, or LLM grading of short answers. Live model output quality and browser visual layout were not manually evaluated. The functional Gradio component/event contracts and real module construction were verified.

The isolated branch is ready for integration; the main workspace was not merged or modified.
