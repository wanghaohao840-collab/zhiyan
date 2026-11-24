# 联合总结阶段日志验收

本轮在工作树基准 69bcb95 上为已有阶段日志加入了后台任务编号和领取次数。所有运行都有独立的运行编号。产品后台任务可用接口返回的任务编号找到阶段日志，直接调用工具时这两个字段为空。

通过真实 worker、临时 SQLite 和替身模型验证了三种执行方式。一次临时故障让任务重新排队，两次运行的任务编号相同，领取次数分别为 1 和 2，各有独立运行编号。第二次运行的答案和来源按正常流程保存。模型输出达到额度、安全拦截、空正文以及未知结束原因都映射成固定类别，日志未记录测试正文、模型推理或原始结束原因。逐篇失败按类别计数，便于排查只得到部分结果的任务。

147 项后端相关回归测试通过。另有 16 项聚焦日志验收全部通过，并检查了 65 条实际打印的阶段记录。生产开关和部署保持现状。

## Neon 规划回执

模型 gemini-4-neon-high，会话 8855adba-7f48-475d-ae6b-cc5fea0fa867，两次只读文件调用成功。

### 知研联合总结可追溯阶段日志任务包（基准 `69bcb95`）

#### 1. 目标与边界
在保持生产启用且**不修改**数据库、API、`QaWorker` lease/cancel/重试与结果写入的前提下，为联合总结补齐共用、脱敏、可按 `job_id` + `attempt` 排查的阶段日志，并区分模型未正常结束的固定内部类别。不引入外部监控栈、外部追踪或定时任务。

#### 2. 改动路径与接口设计
采用**显式窄参数透传**（优于隐式全局 `ContextVar`，避免 LangGraph 节点调度与并发 map 线程上下文丢失或串染）：

1. **`app/qa_answer_engine.py`**
   - 在 `QaAnswerRequest` 尾部新增可选字段：`job_id: str | None = None`、`attempt: int | None = None`（`QaAnswerEngine.answer` 协议签名零变更）。
   - `RagQaAnswerEngine.answer` 仅在非 `None` 时向 `runtime.rag_tool.execute_result("ask", **kwargs)` 透传 `job_id`、`attempt`。
2. **`app/qa_worker.py`**
   - `_run_job` 构造 `QaAnswerRequest` 时传入 `job_id=job.id`、`attempt=attempt`（复用既有 `attempt = job.attempt_count`），其余 lease/cancel/commit/fail 逻辑不动。
3. **`hello_agents/tools/builtin/rag_tool.py`**
   - 扩展 `_IncompleteSummaryOutput(RuntimeError)` 增加受控 `category: str` 属性（默认 `"abnormal_finish"`），对外文案与 retryable 语义不变。
   - 在 `_generate` / `_generate_summary` 将结束原因映射为**内部 allowlist 固定类别**（禁止透出原始 `finish_reason` 或异常文本）：
     - `finish_reason == "length"` → `output_truncated`（汇总截断）
     - `finish_reason` 命中安全/审查集合（如 `content_filter`/`safety`/`recitation`/`blocked`，小写归一）→ `safety_blocked`（安全拦截）
     - 空白正文或 `[LLM调用失败]`/`[LLM未配置]` → `empty_output`（无正文）
     - 其余非 `None`/`""`/`"stop"` → `abnormal_finish`
   - `_ask_multi_summary` 从 `kwargs` 读取可选 `job_id`/`attempt` 传给 `run_summary_workflow`；返回 `"error"` 的状态字典（如 `ContextCapacityError`）可附带固定 `error_category`（如 `context_capacity`）。直接调用 `RAGTool` 无 job context 时默认 `None` 正常运行。
4. **`hello_agents/tools/builtin/rag_summary_workflow.py`**
   - `run_summary_workflow` 新增可选关键字参数 `job_id: str | None = None`、`attempt: int | None = None`；`SummaryState` 可选加 `error_category: str`。
   - 抽取共用 `traced(name, stage, *, engine)` 包装器同时覆盖三条路径，确保 `engine` 标识真实：
     - `use_langgraph=False` → `engine="direct"`
     - `use_langgraph=True` 且 `get_debug()` 为真 → 保留 `tracing_context(enabled=False)` 与降级告警（附带 `job_id`/`attempt`/`run_id`），执行 `engine="langgraph_debug_fallback"`
     - `use_langgraph=True` 且非 debug → `engine="langgraph"`
   - 异常归类与日志白名单：捕获 `_IncompleteSummaryOutput`（取 allowlist 校验后的 `category`）、`SummaryCancelled`（`cancelled`）、`SummaryProgressAborted`（`progress_aborted`）或 `state["error_category"]`，其余归 `stage_error`（成功为 `none`），原样重抛异常。
   - 单条日志与 `extra` **仅允许**：`job_id`（缺省 `"none"`）、`attempt`（缺省 `"none"`）、`run_id`、`engine`、`stage`、`status`、`reason`（严格 allowlist）、`duration_ms`、`documents`、`mapped`、`failed`、`cache_hits`、`truncated`。**严禁**出现 `user_id`、请求正文、提示词、原文、来源、模型推理、凭据、原始结束原因/异常详情或外部 tracing。

#### 3. 验收测试判据与零计费 Smoke
- **回归单测（`pytest`）**：
  1. **三引擎与透传**：分别验证 `direct`、`langgraph`、`langgraph_debug_fallback` 均输出完整 4 阶段（或提前终止阶段）日志，`engine`、`job_id`、`attempt` 准确；直接调用 `RAGTool` 无 `job_id`/`attempt` 时记录 `none` 且无异常。
  2. **固定分类与脱敏红线**：用桩 LLM 分别返回 `finish_reason="length"`、`"content_filter"`、空字符串/`[LLM调用失败]`、以及含敏感串的未知值 `"raw_secret_reason"`，断言日志 `reason` 依次为 `output_truncated`、`safety_blocked`、`empty_output`、`abnormal_finish`，且日志文本与 `extra` 中完全不含 `"raw_secret_reason"`、prompt、文档正文、来源和 `user_id`。
  3. **行为零退化**：既有 `QaWorker` lease/cancel/retry/commit 与 `RAGTool` 错误码测试全部通过。
- **真实日志 Smoke（纯合成数据、零模型计费）**：通过单条本地 `pytest -s -o log_cli=true` 或 `python -c` 脚本驱动内存合成多文档 + Fake LLM，依次跑通 `direct`、`langgraph`、`langgraph_debug_fallback` 及三类失败场景，直接输出并核验标准脱敏日志行。
