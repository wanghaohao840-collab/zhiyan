# Learning Workflow MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a user-isolated learning workflow for the currently selected document: deterministic study plans, SM-2 spaced review, source-grounded knowledge cards, multiple-choice and short-answer exercises, and one Gradio learning center.

**Architecture:** Preserve `Gradio UI -> PDFLearningAssistant -> LearningService -> LearningRepository -> learning.json`. Generation is a separate `LearningGenerationService -> public RAGTool.execute_result("search") + injected LLM` path that returns validated drafts and never persists directly. Each `UserRuntime` owns one repository/service/generator guarded by its existing per-user `RLock`; document selection remains session-local in `PDFLearningAssistant`.

**Tech Stack:** Python 3.12, Gradio 6.19.0, pytest 8.4.1, existing atomic JSON storage, existing JSON/Qdrant RAG backends, `HelloAgentsLLM`.

## Global Constraints

- Read `PROJECT_KNOWLEDGE.md` before implementation. Current code, tests, configuration, and runtime behavior override historical notes.
- Implement only the approved MVP in `docs/superpowers/specs/2026-07-29-learning-mvp-design.md`: one current document, plan/card/exercise creation, SM-2 review, due dashboard, cascade cleanup, and Gradio UI. Do not add notifications, plan editing, cross-document generation, LLM short-answer grading, manual item editing, or a new database.
- Preserve `document_id` and user UUID isolation, source page semantics, RAG/Memory/report behavior, durable import tasks, active import guards, runtime leases, and `ImportTaskService` injection.
- Store learning data only at `data/users/<user_id>/learning.json`, root version exactly `1`, with exactly the collections `plans`, `cards`, `exercises`, and `review_logs`. Use existing atomic JSON helpers and fail closed on corrupt JSON or invalid structure; never silently replace corrupt data.
- All generated cards and exercises must contain at least one source constructed only from the actual RAG search hit. Never trust an LLM-provided filename, page, preview, document ID, or position. Generation must call only the public `RAGTool.execute_result("search")` contract, not a pipeline, store, `_pipelines`, `_get_pipeline`, or `_last_action_data`.
- Generation is all-or-nothing: accept exactly `3`, `5`, or `10` items; reject duplicate prompts/questions, unknown or missing source IDs, semantic validation failures, and document mismatches. Allow exactly one LLM repair call for JSON syntax/root-shape failures only. No partial persistence.
- Use random UUIDs for learning IDs. Persist instants as timezone-aware UTC ISO 8601 strings and dates as `YYYY-MM-DD`. Inject clocks into domain services for deterministic tests.
- SM-2 ratings map to qualities: card/short-answer `forgot=1`, `fuzzy=3`, `mastered=5`; multiple-choice wrong=`1`, correct=`4`. On quality `<3`, reset repetitions to `0` and interval to `1`. On success use intervals `1`, `6`, then `round(previous_interval * updated_ease_factor)`. Update ease with the standard formula and floor it at `1.3`.
- User input validation: plan title length `1..100`, target date from today through today + 364 days inclusive, daily minutes `5..480`, generation count only `3|5|10`. Normalize duplicates with Unicode NFKC, case-folding, and collapsed whitespace.
- Gradio mutations must require a valid session token before invoking the assistant. The learning document dropdown is single-select and must receive a scalar or `None`; existing ask/search dropdowns remain multi-select lists. Card and short-answer ratings must be rejected unless the exact currently selected item was revealed in `gr.State`.
- Keep the current async batch-import UI and all existing handler behavior. Do not refactor unrelated portions of the 1,500+ line Gradio module.
- Write a failing focused test before each implementation slice, then make the smallest change that passes. Use `D:\python_self_agent\venv\Scripts\python.exe` and a repository-local `--basetemp` for pytest.
- Work only in `D:\python_self_agent\.worktrees\codex-learning-mvp` on `codex/learning-mvp`. Do not touch or stage the main checkout's unrelated GraphRAG task-packet edits.

---

### Task 1: Publish structured RAG search results

**Files:**
- Modify: `hello_agents/tools/builtin/rag_tool.py`
- Modify: `tests/tools/test_rag_tool_backend_contract.py`
- Create: `tests/tools/test_rag_tool_learning_search.py`

**Public contract:** `RAGTool.execute_result("search", ...)` keeps its existing human-readable `message` and additionally returns:

```python
result.data == {
    "success": True,
    "query": "...",
    "document_ids": ["doc-id"],
    "results": [
        {
            "chunk_id": "real pipeline hit id",
            "content": "safe evidence text",
            "score": 0.91,
            "metadata": {
                "document_id": "doc-id",
                "document_name": "guide.pdf",
                "file_name": "guide.pdf",
                "file_suffix": ".pdf",
                "source_type": "pdf",
                "page_number": 12,
                "chunk_index": 4,
                "source_position": None,
            },
        }
    ],
}
```

Only the listed metadata keys may be exposed. String evidence follows the existing safe-action truncation contract (maximum 500 characters). Empty search results are a successful result with `results=[]`; invalid document scope is a failed result with `success=False` and `results=[]`.

- [ ] **Step 1: Add failing public-contract tests**

Test a hit, empty results, invalid scope, real top-level hit ID preservation, metadata allowlisting, 500-character evidence truncation, and absence of sensitive values such as file paths or backend payloads. Assert only through `RAGActionResult.data`, never private tool state.

- [ ] **Step 2: Run the tests and confirm the new data contract is missing**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-rag tests/tools/test_rag_tool_backend_contract.py tests/tools/test_rag_tool_learning_search.py
```

- [ ] **Step 3: Extend only the effective `_search` implementation**

The effective method is the later `_search` definition in `rag_tool.py`. Project each JSON/Qdrant pipeline hit into the public allowlisted shape, set `_last_action_data` on success, empty success, and invalid scope, and let the existing `execute_result()`/safe-data path produce the result. Do not add backend-specific branches and do not change the existing rendered message.

- [ ] **Step 4: Run focused and backend parity regressions**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-rag tests/tools/test_rag_tool_backend_contract.py tests/tools/test_rag_tool_learning_search.py tests/memory/rag/test_backend_selection_and_contracts.py tests/memory/rag/test_qdrant_pipeline.py
```

- [ ] **Step 5: Commit**

```powershell
git add hello_agents/tools/builtin/rag_tool.py tests/tools/test_rag_tool_backend_contract.py tests/tools/test_rag_tool_learning_search.py
git commit -m "feat: expose structured RAG search results"
```

---

### Task 2: Add fail-closed user learning storage

**Files:**
- Modify: `app/storage.py`
- Create: `app/learning_repository.py`
- Modify: `tests/test_user_storage.py`
- Create: `tests/test_learning_repository.py`

**Interfaces:**

```python
class CorruptLearningDataError(ValueError): ...

class LearningRepository:
    def __init__(self, path: Path, lock: RLock): ...
    def load(self) -> dict: ...
    def save(self, data: dict) -> None: ...
    def update(self, mutator: Callable[[dict], T]) -> T: ...
    def delete_document(self, document_id: str) -> None: ...
    def clear_all(self) -> None: ...
```

`UserPaths` gains `learning: Path`, set to `<user root>/learning.json`. A missing file loads a new deep-copied empty root but is not written until a mutation. `load()` returns a defensive copy. `save()` and the post-mutation state validate the full root and every persisted record before atomic replacement.

- [ ] **Step 1: Write failing storage-path and repository tests**

Cover missing-file defaults, defensive copies, atomic round trips, concurrent update serialization through the supplied `RLock`, no lost updates, cascade deletion across all four collections, idempotent clear/delete, JSON parse failure, wrong version, missing/extra root keys, invalid record types/required fields, and preservation of corrupt bytes after failure.

- [ ] **Step 2: Run and confirm failures**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-repository tests/test_user_storage.py tests/test_learning_repository.py
```

- [ ] **Step 3: Implement the minimal repository**

Reuse `UserStorage.read_json()` and `write_json_atomic()`; do not create another atomic writer. Validate the exact root shape. Validate enough nested shape and primitive types to ensure later domain code never consumes malformed plans, tasks, sources, schedules, cards, exercises, or logs. Wrap decode/shape problems as `CorruptLearningDataError` with a user-readable message and the original exception as cause.

- [ ] **Step 4: Run focused storage regressions**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-repository tests/test_user_storage.py tests/test_learning_repository.py tests/test_p0_data_integrity.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/storage.py app/learning_repository.py tests/test_user_storage.py tests/test_learning_repository.py
git commit -m "feat: add user-scoped learning repository"
```

---

### Task 3: Implement plans, items, SM-2, and the due dashboard

**Files:**
- Create: `app/learning.py`
- Create: `tests/test_learning_service.py`

**Interfaces:**

```python
class LearningService:
    def __init__(self, repository, *, now=None, today=None): ...
    def create_plan(self, document_id, document_name, title, target_date, daily_minutes) -> dict: ...
    def list_plans(self, document_id) -> list[dict]: ...
    def set_task_completed(self, document_id, plan_id, task_id, completed) -> dict: ...
    def dashboard(self, document_id) -> dict: ...
    def existing_card_fronts(self, document_id) -> list[str]: ...
    def existing_exercise_prompts(self, document_id) -> list[str]: ...
    def save_cards(self, document_id, document_name, drafts) -> list[dict]: ...
    def save_exercises(self, document_id, document_name, drafts) -> list[dict]: ...
    def list_cards(self, document_id) -> list[dict]: ...
    def list_exercises(self, document_id) -> list[dict]: ...
    def review_card(self, document_id, card_id, rating) -> dict: ...
    def answer_multiple_choice(self, document_id, exercise_id, selected_index) -> dict: ...
    def review_short_answer(self, document_id, exercise_id, rating, user_answer) -> dict: ...
    def delete_document(self, document_id) -> None: ...
    def clear_all(self) -> None: ...
```

**Plan allocation:** generate one task per phase allocation day from today through target date inclusive. For 1 day use comprehensive review; for 2 days use reading then review; for 3 days use reading, cards, review. For 4+ days allocate reading/cards/exercises/review by weights `25/30/25/20`, guarantee each phase at least one day, and distribute remaining days by largest remainder with stable phase-order tie breaking. Each task uses exactly `daily_minutes`.

**Review logs:** every review appends a UUID log with user action/automatic grade, item type/id, document ID, quality, old/new schedule, optional selected index/correctness or user answer, and UTC timestamp. Multiple-choice is graded automatically and rescheduled immediately; short answer can be rescheduled only by explicit self-rating.

- [ ] **Step 1: Write failing deterministic domain tests**

Cover plan validation boundaries, 1/2/3/4/5/10-day phase allocation, inclusive target dates, completion status transitions, cross-document opaque ID rejection, defensive outputs, initial due schedules, card and exercise save validation, duplicate normalization, each SM-2 transition and ease floor, MCQ correct/wrong grading, short-answer type guards, review-log contents, dashboard overdue/today tasks, due cards/exercises, and cascade delegation.

- [ ] **Step 2: Run and confirm failures**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-service tests/test_learning_service.py
```

- [ ] **Step 3: Implement the pure domain service**

Keep schedule math in small pure helpers. Every mutation must execute inside one repository update so the item and review log are atomic. New items are immediately due. Preserve source objects already validated by the generation layer, but validate their required fields again before persistence.

- [ ] **Step 4: Run repository and service regressions**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-service tests/test_learning_repository.py tests/test_learning_service.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/learning.py tests/test_learning_service.py
git commit -m "feat: implement learning plans and spaced review"
```

---

### Task 4: Generate source-grounded cards and exercises

**Files:**
- Create: `app/learning_generation.py`
- Create: `tests/test_learning_generation.py`

**Interfaces:**

```python
class LearningGenerationError(ValueError): ...

class LearningGenerationService:
    def __init__(self, rag_tool, llm): ...
    def generate_cards(self, document_id, document_name, count, existing_fronts=()) -> list[dict]: ...
    def generate_exercises(self, document_id, document_name, count, kind="mixed", existing_prompts=()) -> list[dict]: ...
```

Cards use `{"front", "back", "source_ids"}`. Exercises use `type=multiple_choice|short_answer`; MCQ has exactly four non-empty unique options, integer `correct_index` in `0..3`, explanation, and sources; short answer has reference answer, `1..8` unique non-empty key points, and sources. Mixed generation uses one more MCQ when count is odd. Valid `kind` values are `mixed`, `multiple_choice`, and `short_answer`.

- [ ] **Step 1: Write failing fake-RAG/fake-LLM tests**

Test the single-document search call and limit `min(30, max(10, count * 3))`; chunk-ID deduplication; temporary source IDs; source reconstruction only from RAG; preview truncation to 240; PDF page and non-PDF source-position mapping; exact counts and mixed split; existing/new duplicate rejection; unknown/missing/cross-document sources; MCQ and short-answer semantic validation; empty search; `[LLM未配置]` and `[LLM调用失败]`; syntax/root-shape repair exactly once; no repair for semantic errors; and no persistence side effects.

- [ ] **Step 2: Run and confirm failures**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-generation tests/test_learning_generation.py
```

- [ ] **Step 3: Implement retrieval, prompts, parsing, and validation**

Call only `rag_tool.execute_result("search", document_id=document_id, query=..., limit=...)`. Recheck every hit's metadata document ID, deduplicate by `chunk_id`, assign `SRC-1...`, and expose those temporary IDs to the LLM. Use `llm.generate()` when available, otherwise `llm.chat()`. Strip optional Markdown fences, parse strict JSON, and make at most one repair call for syntax/root-shape only. Return validated drafts with backend-constructed sources and no IDs/schedules/timestamps.

- [ ] **Step 4: Run generation plus public RAG contract tests**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-generation tests/test_learning_generation.py tests/tools/test_rag_tool_learning_search.py
```

- [ ] **Step 5: Commit**

```powershell
git add app/learning_generation.py tests/test_learning_generation.py
git commit -m "feat: generate grounded learning materials"
```

---

### Task 5: Integrate user runtimes and PDFLearningAssistant workflows

**Files:**
- Modify: `app/runtime.py`
- Modify: `assistants/pdf_learning_assistant.py`
- Modify: `tests/test_user_runtime.py`
- Create: `tests/assistants/test_pdf_learning_assistant_learning.py`
- Modify: `tests/test_user_mutation_coordination.py`

**Runtime fields:** add shared `learning_repository`, `learning_service`, and `learning_generation_service` without changing runtime lease counts or `import_task_service` injection. Standalone assistants create the same services using `<runtime_dir>/learning.json`. Runtime-backed assistants reuse the shared instances.

**Assistant use cases:** add document-validated wrappers for plan listing/creation/task completion/dashboard, card/exercise listing/generation/review, and source display. The helper that supplies a learning document must reload History while holding the existing user lock and verify that `current_document_id` exists; arbitrary or stale IDs fail before any RAG, LLM, or persistence call.

- [ ] **Step 1: Write failing runtime sharing and assistant workflow tests**

Cover two sessions sharing learning persistence but not current-document selection, standalone paths, stale/forged selection rejection, generation outside the user lock, document revalidation before atomic save, exactly-one-document generation, no question/memory/stat side effects, cross-document item rejection, corrupt-file user-readable failure, and legacy `__new__()` assistant tests not accessing absent learning attributes in unrelated methods.

- [ ] **Step 2: Add failing delete/clear cascade tests**

Preserve active-import guards and current RAG/History/source-file deletion semantics. On successful document deletion remove all associated learning records. On clear remove all learning records. Learning cleanup is idempotent. If cleanup fails after another deletion step, return an explicit partial-failure message and never claim complete success; queries filter orphaned items whose document no longer exists.

- [ ] **Step 3: Run and confirm failures**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-assistant tests/test_user_runtime.py tests/assistants/test_pdf_learning_assistant_learning.py tests/test_user_mutation_coordination.py
```

- [ ] **Step 4: Wire the services without regressing import leases**

Use each runtime's existing `RLock`. Retrieval and LLM calls happen outside the lock; take the lock before reading existing prompts, revalidate the document before persistence, then atomically save the complete batch. Do not call the assistant's existing `ask()` for generation.

- [ ] **Step 5: Run runtime, assistant, import, and isolation regressions**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-assistant tests/test_user_runtime.py tests/test_runtime_import_leases.py tests/assistants/test_pdf_learning_assistant_learning.py tests/assistants/test_pdf_learning_assistant_multi_document.py tests/assistants/test_import_active_guard.py tests/assistants/test_import_idempotency.py tests/test_user_mutation_coordination.py tests/integration/test_multi_user_acceptance.py
```

- [ ] **Step 6: Commit**

```powershell
git add app/runtime.py assistants/pdf_learning_assistant.py tests/test_user_runtime.py tests/assistants/test_pdf_learning_assistant_learning.py tests/test_user_mutation_coordination.py
git commit -m "feat: integrate learning workflows with user runtimes"
```

---

### Task 6: Add the authenticated Gradio learning center

**Files:**
- Modify: `ui/gradio_app.py`
- Modify: `tests/ui/test_authenticated_handlers.py`
- Modify: `tests/ui/test_document_selection.py`
- Create: `tests/ui/test_learning_handlers.py`

**Layout:** add top-level `8. 学习中心` containing nested tabs `今日任务`, `学习计划`, `知识卡片`, and `练习题`. Use one single-select learning document dropdown and refresh button. Use `gr.DateTime(include_time=False, type="string")` for target date, `gr.Radio([3, 5, 10], value=5)` for generation counts, and `gr.Dataframe` list-of-lists output for task/plan/due tables.

**Existing handler output changes:** authentication success/failure/logout become five outputs: token, status, ask multiselect update, search multiselect update, learning single-select update. Batch submission becomes seven outputs: batch ID, summary, rows, ask update, search update, learning update, selected-task state. Delete/clear become six outputs: result, ask update, search update, learning update, and the two existing question/search resets.

- [ ] **Step 1: Write failing handler and component-contract tests**

Assert exact tuple lengths and component bindings; ask/search updates use lists while learning uses scalar/`None`; refresh preserves a valid selected document; delete clears it. Add every learning mutation to the authenticated-handler guard test and prove missing/forged/expired sessions fail before assistant calls and leave `learning.json` byte-identical.

- [ ] **Step 2: Write failing learning interaction tests**

Cover plan input boundaries; dashboard and plan tables; card generation/selection/reveal/rating; rejection before reveal; stale reveal rejection after card or document change; MCQ auto-grade; short-answer reference/reveal/self-rating and stale reveal rejection; source rendering; opaque IDs not crossing documents/users; and Gradio output arity.

- [ ] **Step 3: Run and confirm failures**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-ui tests/ui/test_authenticated_handlers.py tests/ui/test_document_selection.py tests/ui/test_learning_handlers.py
```

- [ ] **Step 4: Implement thin authenticated handlers**

Every handler begins with `_require_assistant(token)`, validates one selected document through the assistant, delegates domain work, and converts results to display rows/updates. UI handlers never import or instantiate `LearningRepository`. Keep reveal IDs in `gr.State("")`; selecting another item/document clears them. Rating handlers require the selected ID to equal the revealed ID.

- [ ] **Step 5: Build and bind the four nested tabs**

Keep the change local to the new top-level tab plus exact existing dropdown return-contract updates. A batch import may refresh only immediately available documents; the learning center's refresh button must pick up newly completed async imports.

- [ ] **Step 6: Run UI and session regressions**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-ui tests/ui tests/test_session_registry.py tests/test_import_service.py tests/test_import_worker.py
```

- [ ] **Step 7: Commit**

```powershell
git add ui/gradio_app.py tests/ui/test_authenticated_handlers.py tests/ui/test_document_selection.py tests/ui/test_learning_handlers.py
git commit -m "feat: add Gradio learning center"
```

---

### Task 7: Close acceptance, documentation, and regression gates

**Files:**
- Create: `tests/integration/test_learning_mvp_acceptance.py`
- Modify: `README.md`
- Modify: `PROJECT_KNOWLEDGE.md`

- [ ] **Step 1: Write the end-to-end acceptance test**

With two users and fake RAG/LLM dependencies, verify each user can select an owned document, create an independent plan, generate sourced cards and both exercise types, review due items, observe new due dates/logs, and delete one document without affecting the other user. Assert persisted source document/chunk identities and that no cross-user/document data appears.

- [ ] **Step 2: Run the complete learning feature suite**

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-feature tests/test_learning_repository.py tests/test_learning_service.py tests/test_learning_generation.py tests/tools/test_rag_tool_learning_search.py tests/assistants/test_pdf_learning_assistant_learning.py tests/ui/test_learning_handlers.py tests/integration/test_learning_mvp_acceptance.py
```

- [ ] **Step 3: Document the delivered workflow and data contract**

README must explain the four learning-center tabs, the current-document limitation, source grounding, SM-2 rating meanings, and the per-user `learning.json`. Update `PROJECT_KNOWLEDGE.md` with the current implementation, startup/validation commands, and exact new test totals only after tests run. Preserve all unrelated historical context.

- [ ] **Step 4: Run broad regression groups**

Run repository-local `--basetemp` groups for storage/runtime/assistant, RAG backends, UI/session/imports, then the remaining suite. Do not treat a timeout as a pass. Record exact pass/skip totals and any environment-only failure.

```powershell
D:\python_self_agent\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-learning-all
```

- [ ] **Step 5: Perform a real Gradio import smoke check**

Import `ui.gradio_app`, verify the `demo` builds under Gradio 6.19.0, and exercise the authenticated learning handlers with temporary user data without launching a persistent server.

- [ ] **Step 6: Inspect and commit only intended closure files**

```powershell
git diff --check
git status --short
git add tests/integration/test_learning_mvp_acceptance.py README.md PROJECT_KNOWLEDGE.md
git commit -m "docs: complete learning workflow MVP"
```

- [ ] **Step 7: Produce the completion report**

Report the branch/worktree, commit list, exact tests, preserved architecture, known MVP limitations, and whether the branch was pushed. Do not merge into the user's main checkout unless separately requested.

---

## Plan Self-Review

- Every approved MVP behavior maps to one task and one focused test gate.
- The latest durable-import/runtime-lease work is explicitly preserved.
- RAG result exposure precedes generation, repository precedes domain service, domain/generation precede Assistant, and Assistant precedes UI.
- All tasks are sequential and independently committed, so file overlap cannot race.
- No task requires access to the main checkout's dirty GraphRAG documentation.
- The plan contains no requirement that conflicts with its review rubric or with the approved design.

