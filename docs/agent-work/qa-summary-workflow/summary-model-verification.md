# 联合总结显式选择模型

可以在应用进程配置中设置 'SUMMARY_MODEL_ID'，给逐篇摘要和最终汇总指定同一已配置服务上的模型。空值或空白会使用全局模型。同一任务从开始到结束使用同一个模型配置，普通问答仍使用全局模型。

逐篇摘要缓存按实际模型区分。将配置切到另一模型后会重新生成摘要，切回同一模型时可以复用缓存。目标模型被服务拒绝时，会按不可重试的失败处理。原有每阶段默认输出额度为 1024，目标模型与全局模型共用当前上下文窗口配置。

## 隔离验收

2026-11-24 使用两份合成文档和现有测试配置，令全局模型为 DeepSeek Reasoner，为联合总结显式指定 DeepSeek Chat。实际发出两次逐篇摘要请求和一次汇总请求，三次 SDK 请求的实际模型均为 DeepSeek Chat，输出额度均为 1024，结束原因为 'stop'。总结包含两篇文档的引用和题目要求的四个数字。普通问答保留全局模型的行为用替身验证。

Windows Python 3.12 通过 150 项相关回归，覆盖未配置时沿用全局模型、任务期间配置变更、缓存按模型隔离、旧客户端和不可重试的模型拒绝。

## Neon 规划回执

模型 gemini-4-neon-high，会话 97cb7ce3-f360-45e4-a884-35ffdda9f052，两次只读文件调用成功。

### 任务包：知研联合总结显式模型选择（基准 `06263c8`）

> 参考输入：brief.md、context.txt

---

#### 1. 目标与边界
- **目标**：允许联合总结通过环境变量 `SUMMARY_MODEL_ID`（如 `deepseek-chat`）在**同一 OpenAI 兼容服务**上显式指定本次调用的模型快照；未配置时默认沿用全局 `self.llm.model`。
- **生效范围**：仅作用于联合总结的 `map` 与 `reduce` 阶段（同一次调用内统一使用入口快照值）。
- **明确不做**：
  - 不影响普通问答、滚动摘要及其他调用路径；
  - 不切换供应商、凭据（`api_key`）、`base_url`/endpoint；
  - 不改变部署状态、不改默认 LangGraph 试点开关；
  - 不自动 fallback 到全局模型、不自动提额；
  - **Token estimator / context window**：继续复用当前 `self.llm` 的 `tokenizer` 与 `context_window_tokens`（无依据不做动态猜测或放大；若目标模型实际窗口更小，仍由既有 `ContextCapacityError` 安全拦截）。

---

#### 2. 最小改动设计（仅改必要代码）

1. **Per-call 模型覆盖（`hello_agents/core/llm.py`）**
   - 核查 `_create_completion(self, messages, **kwargs)`：现状硬编码 `model=self.model`，若 `kwargs` 含 `model` 会触发 `TypeError: got multiple values for keyword argument 'model'`。
   - 改为从 `kwargs` 弹出覆盖值：`override_model = kwargs.pop("model", None)`，调用 `create(model=override_model or self.model, messages=messages, **kwargs)`。
   - **严禁写入或修改 `self.model`**，保证并发调用线程安全；`_chat_with_metadata` / `generate_with_metadata` 透传 `**kwargs` 保持不变。

2. **配置读取与单次调用快照（`rag_tool.py` 及现有 summary 配置辅助处）**
   - 在联合总结入口读取 `map_max_tokens` / `reduce_max_tokens` 的同一位置，一次性快照 `SUMMARY_MODEL_ID`（建议封装极简 helper，与 `summary_output_tokens` 并列）：
     - 未设置或仅空白（`""` / `"   "`）→ 视为 `None`（使用全局默认模型）；
     - 若含非法控制字符/换行 → 抛 `RAGConfigError`；
     - 其余 `.strip()` 作为本次联合总结全程（`map` + `reduce`）的显式模型快照，不随中途 env 变化漂移。

3. **Map 缓存键纳入有效模型身份（`rag_tool.py::_summary_cache_key`）**
   - 计算有效模型身份：`effective_model = summary_model or str(getattr(self.llm, "model", "") or "")`。
   - 将 `"model": effective_model` 加入 `_summary_cache_key` 的 `payload`，彻底隔离不同模型的 map 缓存（显式值与全局值相同时自然复用同一有效身份）。

4. **兼容旧 custom LLM / mock 与无配置路径（`rag_tool.py::_generate` / `_generate_summary`）**
   - `summary_model` 为 `None`（未显式指定）时：**不向 `generate_with_metadata` / `generate` 传 `model` 参数**，完全保持原函数签名，零破坏兼容既有 custom LLM 与测试 mock。
   - `summary_model` 非空（显式指定）时：
     - 校验目标方法（`generate_with_metadata` 或回退 `generate`）是否接受 `model` / `**kwargs`（可用 `inspect.signature` 判定）；
     - 若接受，则透传 `model=summary_model`；
     - 若旧 custom LLM/mock 不支持 `model` 参数，**明确抛出 `RAGConfigError`（禁止静默忽略并误用默认模型）**。
   - 无配置路径（`self.client is None`）：保持原有 `RuntimeError` / `[LLM未配置]` 链路，不受参数透传影响。

5. **调用失败安全分类核查**
   - 核查 `HelloAgentsLLM._is_retriable_error` 与 `rag_tool._summary_error_is_retryable`：
     - 显式模型不存在/无权访问（如 HTTP 400/404 `model_not_found`、401/403）必须判定为 `retryable=False`，不重试、不自动 fallback、不误归类为 `ContextCapacityError`，统一经 `RAGOperationError("联合总结生成失败", retryable=False)` 安全返回；
     - 输出 `length` 截断仍按既有 `_IncompleteSummaryOutput` 拒绝。

---

#### 3. 自动化回归测试清单
1. **正常默认路径**：未设 `SUMMARY_MODEL_ID`，验证 `map`/`reduce` 均未额外传入 `model`、沿用全局模型、结果与缓存行为与基线一致。
2. **显式切换与作用域隔离**：
   - 设置 `SUMMARY_MODEL_ID=deepseek-chat`（全局为另一模型），验证 `map` 与 `reduce` 均透传 `deepseek-chat`；
   - 验证普通问答（及滚动摘要路径）仍使用全局模型、不受 `SUMMARY_MODEL_ID` 影响；
   - 验证单次总结期间中途修改环境变量，本次 `map`/`reduce` 仍锁定入口快照；
   - 验证相同文档/问题切换模型后 **不命中** 另一模型的 map 缓存。
3. **空配置与旧接口兼容**：
   - `SUMMARY_MODEL_ID=""` 或 `"   "` 等价于默认全局模型；
   - `self.client is None` 时保持原未配置报错；
   - 不接受 `model`/`**kwargs` 的 custom LLM/mock：默认路径正常通过，显式配置 `SUMMARY_MODEL_ID` 时明确报 `RAGConfigError`（不静默 fallback）。
4. **并行安全**：多线程并发运行“默认模型总结 + 显式模型总结（及普通问答）”，断言 `self.llm.model` 全程未被修改、每次 `_create_completion` 收到各自预期 `model`、map 缓存互不串扰。
5. **错误分类**：模拟显式模型触发 404/400 `model_not_found`、400 context length、`finish_reason="length"`、以及 503/超时等可重试错误，验证分类准确（客户端无效模型 `retryable=False` 且不重试/不 fallback；容量超限与截断保持原拒绝路径）。

---

#### 4. 真实验收方案（严格 ≤3 次，复用已有测试账号与同 endpoint）
- 前置：不改部署状态，沿用已有 HTTP+Qdrant 测试环境、同账户、同 `LLM_BASE_URL` / `LLM_API_KEY`。
- **验收 1（核心切换）**：全局配置 `LLM_MODEL_ID=deepseek-reasoner`，单次联合总结显式配置 `SUMMARY_MODEL_ID=deepseek-chat`。
  - **通过标准**：`map` 与 `reduce` 均走 `deepseek-chat` 并正常返回完整带引用总结（不再出现 `reasoner` 在 reduce 阶段的 `length` 无正文拒绝）。
- **验收 2（作用域隔离验证）**：在同配置下发起 1 次普通问答请求。
  - **通过标准**：请求仍走全局配置模型，未受 `SUMMARY_MODEL_ID` 影响。
- **验收 3（安全失败验证，可选第 3 次）**：设置无效 `SUMMARY_MODEL_ID`（如 `non-existent-summary-model`）发起 1 次联合总结。
  - **通过标准**：快速失败并安全拒绝（`retryable=False`），不自动 fallback 到全局模型、不泄露凭据或敏感信息。

---

#### 5. 交付要求
- 回执注明基于 `06263c8`，列出改动文件、单测结果及 ≤3 次真实验收实录（含请求配置与结果摘要）。
