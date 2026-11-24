# 联合总结输出完整性

模型响应达到输出上限、有内容拦截或请求工具调用时，联合总结会判为不可用。逐篇摘要失败时可继续汇总其他文档。全部失败，或最后的汇总输出不完整时，后台任务使用安全错误码 'QA_SUMMARY_INCOMPLETE'。该次任务不自动重试，失败片段和模型推理文本不会写入答案、缓存或工作流日志。兼容网关未提供结束原因时，仍可使用它返回的非空正文。

两个阶段都默认请求 1024 个输出 token。可在测试或部署配置中分别设置 'SUMMARY_MAP_MAX_OUTPUT_TOKENS' 和 'SUMMARY_REDUCE_MAX_OUTPUT_TOKENS'，允许范围为 256 至 8192。每阶段的上下文预留与实际模型请求使用同一额度。实际可用额度同时受模型窗口和安全余量限制。逐篇摘要缓存包含额度，切换额度时会重算这部分。普通问答和流式输出沿用各自原有参数。

## 实测记录

- 在修复前，替身模型以达到额度上限的状态返回半截正文，后台任务把它保存成了已完成答案。修复后，顺序和 LangGraph 两条路径都写入失败状态并保留原问题；已成功的文档摘要仍可缓存。
- Windows Python 3.12 通过 146 项相关测试，覆盖默认和自定义额度、缓存、返回结束原因、旧网关响应、调试日志、普通问答和后台任务持久化。
- 使用两个合成文档和既有 DeepSeek Reasoner 测试配置发出 6 次实际请求。1024、4096 各做一轮。两轮逐篇摘要均正常结束，但汇总均以 'length' 结束且可见正文为空。运行正确报告不可重试的 'QA_SUMMARY_INCOMPLETE'，没有写出半成品。4096 未证明能让该模型稳定完成，本轮默认额度保持 1024。
- 生产开关及配置未改动。

## Neon 规划回执

模型 gemini-4-neon-high，会话 b5e7873e-9a4a-4f2c-996f-b973e4420863，两次只读文件调用成功。

# 知研联合总结输出可靠性精进任务包

**范围与红线**：基于 `4d82e0e`（`codex/zhiyan-summary-workflow`），仅改进联合总结（map/reduce）的输出完整性判定与专用输出预算；保留顺序与 LangGraph、原 `chat/generate` 字符串契约、非 summary 问答与流式调用、`strict=True` 单调用语义及普通路径重试；默认真实调用仍为 `max_tokens=1024`。禁止自动提额、偷偷切模型、重复调用、新增模型账号或修改租约/API schema/部署。

## 1. 权衡结论与实现接口
- **传输层选“新专用方法”，不给 `chat` 加返回形态切换关键字**：保留 `chat(...) -> str` 单一契约，避免调用方误用与类型分支污染。在 `hello_agents/core/llm.py`：
  - 新增冻结 dataclass `LLMCompletionResult(text: str, finish_reason: Optional[str])`；抽取内部共享请求/重试逻辑。
  - 新增 `chat_with_metadata(..., max_tokens=1024, *, strict=False) -> LLMCompletionResult` 及对应 `generate_with_metadata(...)`；原 `chat`/`generate` 仅委托解包 `.text`，非 summary 路径行为不变。
  - 安全提取 `choices[0].message.content or ""` 与标准化小写的 `finish_reason`（缺失时为 `None`），**忽略 `reasoning_content`**。
  - 兼容老 mock/缺字段网关：若 `self.llm` 无 `*_with_metadata`，回退 `generate(prompt)` 并视 `finish_reason=None`。
- **联合总结判定（`rag_tool.py` 的 `_generate_summary`）**：
  - 显式传入 `max_tokens` 与 `strict`，优先调用 `generate_with_metadata`。
  - 判定规则：`finish_reason == "length"`（空内容或非空截断均判失败）、`"content_filter"`、`"tool_calls"`/`"function_call"`，以及空输出或 `[LLM调用失败]`/`[LLM未配置]` 前缀，**一律判失败且 `retryable=False`**（同额度重试无意义）。
  - 仅 `finish_reason in {None, "", "stop"}` 且文本有效为成功；400 容量错误仍抛 `ContextCapacityError`。

## 2. 预算配置与缓存不变量
- **显式 per-workflow 专用配置，不改全局 `LLM_OUTPUT_RESERVED_TOKENS`**：隔离普通问答。在 `rag_context.py` 新增可选有界环境变量（默认均 `1024`，有界范围如 `[256, 8192]`，非法值校验/回退，超出窗口由 `context_budget` 抛 `ContextCapacityError`）：
  - `SUMMARY_MAP_MAX_OUTPUT_TOKENS`
  - `SUMMARY_REDUCE_MAX_OUTPUT_TOKENS`
- **预留与请求严格一致**：map 阶段 `output_reserve` 与 `max_tokens` 同取 map 专用值（消除 384 预留 vs 1024 请求错配，保留 `DOCUMENT_SUMMARY_OUTPUT_TOKEN_RESERVE` 常量兼容）；reduce 阶段同取 reduce 专用值（消除全局预留 vs 硬编码 1024 错配）。
- **缓存与状态不变量**：仅校验通过的 map 摘要入缓存，绝不缓存截断/失败结果；`_summary_cache_key` 必须纳入生效的 `map_max_tokens`；全部 map 失败继续跳过 reduce；reduce 失败不写 `answer`。

## 3. 用户可观察的失败分类与脱敏
- **错误码**：沿用 `QaEngineError` → `qa_service._safe_engine_failure` 落 SQLite 终态。输出不完整/被拒/空内容归类为最小安全不可重试码 `QA_SUMMARY_INCOMPLETE`（匹配 `SAFE_ERROR_CODE` 且不在 `RETRYABLE_ERROR_CODES`，不改 schema；或沿用 `QA_ENGINE_FAILED`）；容量不足维持不可重试；网络/超时/5xx 维持现有 `QA_RAG_TRANSIENT` / `QA_RAG_CONNECTION`。
- **脱敏要求**：map 的 `failures[doc_id]` 与 reduce 错误仅输出固定中文短语（如“单篇摘要输出被截断/被内容策略拦截/输出无效/超出当前模型容量/失败”），**严禁出现 provider 原始异常、`reasoning_content` 或截断片段**。

## 4. 必需测试（含真实 transport 替身）
1. **真实 transport 替身**（注入伪 OpenAI `client.chat.completions.create`，走真实 `HelloAgentsLLM`）：覆盖 `length`+空正文+`reasoning_content`、`length`+非空截断正文、`content_filter`、`tool_calls`、缺 `finish_reason` 老响应；断言 `strict=True` 单调用、`strict=False` 仍重试、普通 `chat`/`stream_invoke` 不变。
2. **预算与缓存**：默认 map/reduce 均为 `1024` 预留与请求；设专用环境变量后同步生效且不影响普通问答；越界与窗口不足触发 `ContextCapacityError`；缓存键随 `map_max_tokens` 区分，失败/截断不落缓存。
3. **流程与脱敏**：部分 map 失败、全 map 失败跳过 reduce、reduce 截断失败不写 `answer`，校验 SQLite 不可重试码及无异常/思考/片段泄露。

## 5. 可选真实账户小额验收与停止条件
- 单测全绿后，用 2 篇极短文档隔离跑 2 组小额请求：①默认 `1024` 复现 `length` 时确认显式失败、不写错答案、日志/失败文案脱敏；②显式设专用额度 `2048` 确认 `stop` 返回完整总结。
- **停止条件**：总真实请求严控 ≤ 6 次；遇 401/403/限流/额度异常、非预期重复调用、或默认模式 `max_tokens != 1024` 立即停止并回报，绝不自动提额或切模型。
