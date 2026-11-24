# Neon 规划回执

- 模型 gemini-4-neon-high
- 会话 51dd6f08-aa74-4c16-90c5-d9a80a3598c7
- 结果 SUCCESS
- 只读工具 read_file: brief.md，read_file: hello_agents__tools__builtin__rag_tool.py.txt，read_file: app__qa_answer_engine.py，read_file: app__qa_worker.py，read_file: app__bootstrap.py.txt，read_file: app__qa_observability.py
- 工具错误 0

以下为基于 brief.md 与已读源码整理的可执行规划草案，可直接交由 Codex 按序实施与验收。

---

## 一、已实证的代码关键事实

1. **进度回调签名直接冲突（必现缺陷）**
   - app__qa_worker.py 定义 `update(stage: str, completed: int, total: int, message: str)`（无 `document_id`，且 `message` 必传）。
   - hello_agents__tools__builtin__rag_tool.py.txt 以关键字调用 `progress_callback(completed=..., total=..., stage=..., document_id=...)`。两者参数完全不匹配，真实 worker 路径触发即抛 `TypeError`。
2. **取消与租约信号在子线程被吞**
   - app__qa_worker.py 仅将进程停机事件 `self._stop` 作为 `cancel_event` 传入；单 job 取消依赖 `heartbeat()` 返回 `False` 抛出 `_LeaseLost`。
   - hello_agents__tools__builtin__rag_tool.py.txt 在 `ThreadPoolExecutor` 内调用 `emit_progress` 且用 `except Exception` 兜底，导致子线程中抛出的 `_LeaseLost` 会被吞为单篇文档失败，无法立即中断整批任务。
3. **跨线程状态隔离隐患**
   - hello_agents__tools__builtin__rag_tool.py.txt 将统计元数据写入 `self._last_action_data`（且 `RAGTool` 结果字段涉及 thread-local）。在 LangGraph 节点或并发线程中执行工具时，依赖 thread-local 副作用会导致主线程取不到结果或并发串扰。
4. **最佳低风险接点**
   - app__bootstrap.py.txt 构造 `answer_engine` 并分别注入 `QaWorkerPool` 与 `QaService`。
   - app__qa_worker.py 固定执行 `mode="summary"` 的异步多文档任务，且独占 `commit_answer` / `complete` / `fail_or_retry` 边界。将 opt-in 试点限定在 `QaWorkerPool` 注入的包装引擎上，可实现单篇、其他 QA 模式、同步路径与 legacy 零改动。

---

## 二、方案对比与最小可用选型

| 维度 | 方案 A：仅在现有代码原地硬化 | 方案 B（推荐）：底座硬化 + opt-in LangGraph 试点 |
| :--- | :--- | :--- |
| **改动与依赖成本** | 最低；零新增依赖，仅修 3~4 个文件 | 可控；仅新增 `langgraph`（锁版本，验证 Py3.11/3.12）+ 1 个独立工作流模块 |
| **默认路径风险** | 较低，但直接改动 legacy 共享的 `_ask_multi_summary` 逻辑 | 极低；配置默认关闭，仅 `QaWorkerPool` 多文档 `mode="summary"` 显式启用 |
| **编排与状态治理** | 仍耦合在 340 行过程式函数内，状态契约隐式 | 真正多阶段状态图，Invocation 状态与 Job 持久状态边界清晰，满足 LangGraph 试点目标 |
| **可回滚性** | 需代码回退 | 环境变量一键切回默认路径，无需数据库回滚 |

**选型结论**：采用 **方案 B（最小可用两步落地）**——先修复所有路径共用的回调/取消/遥测底座，再以 **默认关闭、仅限 `QaWorkerPool` 多文档联合总结** 的方式接入多阶段 LangGraph 工作流，复用 `RAGTool` 底层只读能力与阶段函数，绝不做单节点套壳。

---

## 三、状态边界定义（严格隔离）

- **本次 Invocation 工作流状态（仅内存态，单次 `answer()` 生命周期内有效，不落库、无跨进程 checkpoint）**：
  - `invocation_id`、只读范围白名单（已验证的 `document_ids`、`limit`、`graph_mode`）、`graph_contexts`、`shared_graph_contexts`
  - 每篇文档结构化映射结果（`status: ok | no_chunks | failed | cancelled`、`summary`、`sources`、`graph_sources`、`source_refs`、`cache_hit`、`truncated`、`error_code`）
  - 聚合阶段状态、部分失败清单、最终 `answer` 与汇总 `sources` / `graph_sources`、脱敏后的阶段计数与耗时指标
- **现有 Job 持久状态（`QaJobRepository` 独占所有权，工作流只读不写）**：
  - 租约归属（`worker_id`）、进度心跳（`1..95` 与 `stage`）、单 job 取消裁决（`heartbeat -> False`）、重试次数与退避（`fail_or_retry`）、终态提交（`commit_answer` / `complete`）。
  - **硬约束**：LangGraph 节点**禁止**配置自动重试（避免与 `QaWorkerPool` 产生嵌套重试和重复模型调用）。

---

## 四、实施前资料缺项核对（Codex 首步检查）

以下未在本次读取范围内的文件，Codex 动手修改前须先只读核对：
1. `hello_agents/tools/builtin/rag_tool.py` 第 1~629 行：确认 `_last_action_data` 的 thread-local 实现、`execute_result("ask", ...)` 的 `ToolResult` 打包格式、`SUMMARY_MAX_WORKERS` 及 `_graph_context_for_documents` / `_cross_document_graph_context` 错误返回约定。
2. `app/qa_service.py`：确认 `QaService._safe_engine_failure(error)` 的异常到 `(code, retryable)` 映射规则，以及多文档 `summary` 入队判定条件。
3. `hello_agents/memory/rag/contracts.py`：确认标准化错误码与 `ToolResult` 契约。
4. `requirements.txt`：确认 `pydantic` / `typing-extensions` 等现有版本，核验引入 `langgraph` 在 Python 3.11（容器）与 3.12（本机 venv）无冲突。
5. `tests/test_qa_answer_engine.py` 与 `tests/tools/test_rag_tool_multi_document.py`：确认现有断言契约与 mock 形式。

---

## 五、有序任务包草案

### 任务包 1：公共底座硬化（回调契约、取消穿透与无副作用阶段抽离）
- **目标**：消除真实 worker 路径的回调 `TypeError` 与 `_LeaseLost` 被吞问题；为默认路径与 LangGraph 试点抽离可复用、无 thread-local 副作用的只读阶段 helper。
- **涉及文件与可修改范围**：
  - `app/qa_worker.py`：修复 `_progress_callback` 签名与单 job 取消联动。
  - `hello_agents/tools/builtin/rag_tool.py`：抽离多文档总结子阶段纯结构化 helper，不改变外部签名与默认/legacy 输出格式。
- **可实施步骤**：
  1. 在 `QaWorkerPool._progress_callback` 中将回调签名调整为兼容两种调用形态：`def update(stage: str, completed: int, total: int, message: str = "", *, document_id: str | None = None) -> None`（忽略 `document_id` 内容不入日志，仅推进 `heartbeat`）。
  2. 在 `QaWorkerPool._run_job` 中创建本次 job 专属 `job_cancel_event = threading.Event()`（并与 `self._stop` 组合检查）：当 `heartbeat()` 返回 `False` 时先 `job_cancel_event.set()` 再 `raise _LeaseLost()`，将复合取消事件传给 `answer_engine.answer(..., cancel_event=...)`。
  3. 在 `RAGTool._ask_multi_summary` 的并发回收循环中识别并直接重抛租约/取消控制异常（或在 `emit_progress` 回调抛特定取消异常时立即设置内部取消标志、不再将其归类到 `failures[doc_id]`）。
  4. 从 `_ask_multi_summary` 拆出 4 个无 `_last_action_data` 副作用的受保护只读/计算方法，显式返回结构化字典：
     - 跨文档与单篇 GraphRAG 上下文获取；
     - 单篇文档采样、缓存命中查询、上下文构建、单篇摘要生成与写缓存（返回完整 `sources`、`graph_sources`、`source_refs`、`truncated`、`cache_hit`）；
     - 摘要预算裁剪与跨文档合并生成；
     - 答案与来源组装（`_ask_multi_summary` 自身继续调用它们，并维持原有 `_last_action_data` 写入以兼容 legacy）。
- **调用与依赖**：零新增第三方依赖；完全向后兼容现有 `QaAnswerEngine` 与 `RAGTool.execute_result("ask", ...)`。
- **可观察验收**：真实 `QaWorkerPool` 跑通 `RagQaAnswerEngine` + `RAGTool._ask_multi_summary`，不再触发回调 `TypeError`；触发取消时立即中止后续文档摘要并落终态 `cancelled`。

---

### 任务包 2：opt-in LangGraph 多文档联合总结试点工作流
- **目标**：以最小可用多阶段 LangGraph 实现多文档 `mode="summary"` 编排，覆盖受控只读工具调用、显式状态管理、部分结果与错误处理，默认关闭。
- **涉及文件与可修改范围**：
  - `requirements.txt`：新增并固定兼容 Python 3.11/3.12 的 `langgraph` 版本。
  - 新增 `app/qa_multi_summary_workflow.py`：只读工具封装、Invocation 状态定义、多阶段 StateGraph 及 `QaAnswerEngine` 包装适配器。
- **可实施步骤**：
  1. **定义只读工具层（固定动作 + 范围白名单）**：封装针对当前用户 `runtime.rag_tool` 的只读调用器，所有按 `doc_id` 访问的调用必须校验 `doc_id in allowed_document_ids`，文档内容仅作为证据文本传递，绝不暴露 LLM 自主选工具能力。
  2. **定义 Invocation State（`TypedDict`）**：字段仅含本次调用所需的只读请求元数据、图上下文、单篇映射结果字典、部分失败字典、聚合文本、汇总 `sources` / `graph_sources`、阶段统计与控制标志（`cancelled` / `terminal_error_code` / `retryable`）。**所有节点返回值严格通过 State 传递，不读取 `RAGTool._last_action_data` / thread-local**。
  3. **编排实质多阶段 StateGraph（至少 5 个职责清晰节点，非单节点包装）**：
     - `prepare_scope`：校验 `document_ids` 去重与非空，初始化统计与取消检查。
     - `fetch_graph_context`：调用只读图上下文工具（复用现有 `graph_mode` 策略）；若 `graph_error` 则转入终态错误分支。
     - `map_documents`：使用与 `SUMMARY_MAX_WORKERS` 一致的受限并发（线程池或受控并发分片）逐篇调用只读采样+摘要工具，命中既有按内容缓存；每篇完成后加锁安全触发 `progress_callback("mapping", completed, total)`；遇到单篇 `no_chunks` / `ContextCapacityError` / 失败记录到 `failures`（安全错误码），遇到 `_LeaseLost` 或取消信号立即中止并原样透传 `_LeaseLost`。
     - `aggregate_and_check`：按原 `document_ids` 顺序收拢成功摘要、汇总 `sources` / `graph_sources` 与缓存命中统计；若全部失败则路由到无可用内容兜底出口，否则进入 `reduce_summary`。
     - `reduce_summary`：调用只读预算裁剪与合并工具（复用 `_fit_summary_context` 与稳定引用约束），上报 `progress_callback("reducing", total, total)`；遇 `ContextCapacityError` 映射为确定性错误/提示。
     - `finalize_result`：上报 `progress_callback("completed", total, total)`，直接构造 `QaAnswerResult`（包含去重后的 `rag_sources` + `graph_sources`，与 app__qa_answer_engine.py 输出结构严格对齐）。
  4. **实现包装引擎 `MultiSummaryWorkflowAnswerEngine(QaAnswerEngine)`**：仅在 `enabled=True` 且 `request.mode == "summary"` 且 `len(request.document_ids) > 1` 时走 LangGraph；其余所有情况（含未开启、单篇、其他模式）直接委托 `fallback: RagQaAnswerEngine`。
  5. **错误处理与防嵌套重试**：图内不设重试策略；引擎级不可恢复失败统一抛出 `QaEngineError(code, retryable)`，交由 `QaWorkerPool._run_job` 决策重试；`_LeaseLost` 直接透传不包裹。
- **调用与依赖**：依赖 `langgraph` 与任务包 1 抽离的 `RAGTool` 阶段 helper；不引入 `langchain` 全家桶，不改数据库 schema。
- **可观察验收**：启用开关后多文档 `summary` 依次经过多阶段节点并产出稳定引用与部分结果；关闭开关后行为与基线字节级/契约级一致。

---

### 任务包 3：应用装配接线与可观测性脱敏增强
- **目标**：通过显式配置将试点引擎仅接入产品 worker 路径，补齐分阶段结构化可观测性与严格脱敏保障。
- **涉及文件与可修改范围**：
  - `app/bootstrap.py`：按环境变量（如 `ZHIYAN_MULTI_SUMMARY_WORKFLOW=langgraph`，默认 `off`）仅为 `QaWorkerPool` 包装 `MultiSummaryWorkflowAnswerEngine`（当外部未显式注入自定义 `qa_answer_engine` 时），`QaService` 保持原 `answer_engine`。
  - `app/qa_observability.py`：在 `ALLOWED_QA_EVENTS` 新增工作流白名单事件（如 `qa_workflow_started`、`qa_workflow_stage`、`qa_workflow_completed`、`qa_workflow_failed`、`qa_workflow_cancelled`），并为 `record()` 增加向后兼容的安全白名单可选字段（`workflow_engine`、`stage`、`doc_count`、`succeeded_count`、`failed_count`、`cache_hits`、`cache_misses`、`truncated`、`partial_result`）。
  - `app/qa_worker.py`：在 `_run_job` 传递 `job_id` / `conversation_id` 等安全上下文（仅非 PII 标识）或记录脱敏事件；确认工作流失败日志不打印异常正文堆栈中包含的敏感文本。
- **可实施步骤**：
  1. 在 `app/qa_observability.py` 坚持白名单事件与白名单字段校验，拒绝未知键写入 `extra`，确保不包含 `user_id`、文件名、原文、问题、回答、提示词、凭据或异常正文。
  2. 在 `app/qa_multi_summary_workflow.py` 各阶段起止点记录 `duration_ms`、计数、缓存命中与安全 `error_code`。
  3. 在 `app/bootstrap.py` 读取开关（非法值 fail-closed 回退默认路径并记录脱敏告警），仅包装 `QaWorkerPool` 使用的引擎。
- **调用与依赖**：依赖任务包 2；`InProcessQaTelemetry.snapshot()` 保持原有 `dict[str, int]` 返回协议。
- **可观察验收**：`qa_telemetry.snapshot()` 可观测完整工作流生命周期计数；捕获日志记录断言零敏感信息泄漏。

---

### 任务包 4：全维度自动化验收测试
- **目标**：对真实工具、进度、部分失败、取消、并发线程隔离、日志脱敏与默认路径提供可重复执行的自动化测试证明。
- **涉及文件与可修改范围**：
  - 新增 `tests/test_qa_multi_summary_workflow.py`
  - 视需要补充 `tests/test_qa_answer_engine.py` / `tests/test_qa_worker.py`（仅增量用例，不削弱现有断言）。
- **验收测试矩阵（必测项）**：
  1. **真实工具与多阶段流转**：使用真实 `RAGTool` 实例（仅 mock 底层 LLM/pipeline I/O，不跳过 `RAGTool` 真实采样、缓存、引用与格式化方法），验证 5+ 阶段均被执行、稳定引用 ID 与 `sources`/`graph_sources` 完整保留、二次相同请求命中单篇摘要缓存。
  2. **进度推进与签名兼容**：通过真实 `QaWorkerPool._run_job` + `QaJobRepository` 驱动，断言 `heartbeat` 收到单调递增进度（`1..95`）与 `mapping` → `reducing` → `completed` 阶段更新，且无 `TypeError`。
  3. **失败与部分结果**：
     - 3 篇文档中 1 篇 `no_chunks` 或抛 `ContextCapacityError`，断言其余 2 篇正常进入 `reduce_summary` 并返回完整 `QaAnswerResult`（含缺失文档说明、不触发整 job 重试）。
     - 全部失败或底层不可用抛 `QaEngineError(retryable=True)` 时，断言**图内 LLM 调用次数无重复/无嵌套重试**，交由 `QaWorkerPool` 走单次 `fail_or_retry`。
  4. **取消与租约丢失**：在 `mapping` 中途令 `heartbeat` 返回 `False`（模拟取消/租约失效），断言 `_LeaseLost` 立即穿透到 `_run_job`、未开始文档不再调用 LLM、最终调用 `job_repository.complete(job.id, worker_id)` 且不触发 `fail_or_retry` 或 `commit_answer`。
  5. **线程隔离与用户范围越界防护**：双线程并发执行两组不同文档集的联合总结（含不同 `sources`/`graph_sources`），断言返回结果、引用与遥测计数零交叉污染；校验越界 `document_id` 无法被只读工具读取。
  6. **日志与遥测脱敏**：在测试数据中注入特征哨兵串（模拟原文、问题、回答、文件名、凭据、自定义异常正文、`user_id`），使用 `caplog` 捕获全部日志记录（含 `msg` 与 `record.__dict__` extra 字段），断言任意哨兵串出现次数均为 `0`。
  7. **默认路径回归**：开关默认关闭时，验证 `ApplicationServices.create()` 走既有 `RagQaAnswerEngine`；单篇、非 `summary` 模式及既有 `tests/test_qa_answer_engine.py`、`tests/tools/test_rag_tool_multi_document.py` 全量通过。

---

## 六、高风险点与停工条件

### 高风险点
1. **依赖冲突风险**：`langgraph` 及其传递依赖（如 `langgraph-checkpoint`、`pydantic`）可能与现有 `requirements.txt` 或 Python 3.11 容器镜像冲突。
2. **引用与输出格式漂移**：多文档总结返回的 `sources`/`graph_sources` 结构、截断提示或稳定引用 ID 若与现有前端/契约偏离，会导致前端引用无法定位。
3. **取消竞态与线程泄漏**：`ThreadPoolExecutor` 退出时若阻塞等待正在运行的子任务且未及时检查 `cancel_event`，可能拉长取消耗时或产生租约超时后的多余 LLM 调用。
4. **日志意外泄露**：`QaWorkerPool._loop`（L161）与 `_run_job`（L217）现有 `logger.exception` / `exc_info=True` 可能打印包含原始文本或路径的异常正文，试点相关路径须严格使用脱敏错误码日志。

### 停工条件（触发任一条立即暂停并回报）
1. `langgraph` 引入导致 `requirements.txt` 依赖解析失败，或无法在 Python 3.11 / 3.12 任一目标环境无冲突安装。
2. 核对 `hello_agents/tools/builtin/rag_tool.py` 前 629 行或 `app/qa_service.py` 后发现必须修改数据库 schema、破坏 legacy `RAGTool` 公开接口或引入跨进程状态才能推进。
3. 任意现有 QA / RAG 回归测试失败且无法在不改变原有契约的前提下修复。
4. 取消测试出现“已取消但仍继续批量调用 LLM”或日志脱敏测试检出任何哨兵敏感信息。
