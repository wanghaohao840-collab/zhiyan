# Neon 三步验收回执

- 模型 gemini-4-neon-high
- 会话 c495c7d0-5e7f-446d-ae68-7230ca1dcea8
- 结果 SUCCESS，完成四次只读文件调用，工具错误 0。

# 知研联合总结试点三步闭环：可执行任务包

基准：`codex/zhiyan-summary-workflow@6b52fe4`（父分支 `codex/next-improvements@f02a660`）。开关 `ZHIYAN_MULTI_SUMMARY_WORKFLOW=langgraph` 默认关闭，仅作用于多篇（≥2）后台联合总结。主工作区存在无关改动且同机有独立验证任务，**所有操作必须在独立 `git worktree` 与独立运行目录中执行**。

---

## 一、依赖顺序

`[0. 隔离环境准备]` → 并行推进 `[1. 租约续期与锁内计时加固 + Astra 审查]` 与 `[2. 真实文档/真实模型双路径验收]` → **双门槛全部通过** → `[3. 基准核对、合入与隔离 Canary]`。
（若暂无可用测试模型：留存 HTTP 状态等证据，先完成步骤 1，**保持步骤 2 阻断步骤 3**。）

---

## 二、步骤 0：隔离工作区准备

```bash
git worktree add ../wt-zhiyan-summary codex/zhiyan-summary-workflow
git -C ../wt-zhiyan-summary rev-parse --short HEAD   # 确认 6b52fe4
```

---

## 三、步骤 1：慢调用续租、锁内计时与租约强校验（可独立推进）

### 最小改动范围（repo 相对路径）
1. `app/qa_job_repository.py`（对应 `context/qa_job_selected.txt`）：
   - **锁后计时**：`claim_next`、`heartbeat`、`complete`、`commit_answer`、`fail_or_retry`（及同文件其余租约写方法）将 `timestamp = now or _utc_now()` 移至 `conn.execute("begin immediate")` **之后**；`claim_next` 的 `expires_at` 同样在锁内计算。
   - **同租约身份核查**：在 `heartbeat` / 纯续租方法、`complete`、`commit_answer`、`fail_or_retry` 中除 `lease_owner = worker_id` 外，增加同一租约标识校验（如 `attempt_count` 或新增 `lease_token`），防止同一 `worker_id` 跨次接管发生 ABA 误提交；对外保持兼容（可选参数或统一由 `QaWorkerPool` 透传 `job.attempt_count`/`lease_token`）。
   - **纯续租接口与租期推导**：实读确认 `heartbeat` 当前通过 `lease_duration_seconds` 列推导新到期时间（而非从旧 `lease_expires_at` 反推）；需新增/复用**不覆盖 `progress`/`stage` 的纯续租方法** `renew_lease(...)`，并对 `lease_duration_seconds` 为空/异常做兜底。
2. `app/qa_worker.py`（对应 `context/qa_worker.py`）：
   - 在 `_run_job` 内为单次任务启动后台定时续租线程（周期约 `lease_seconds / 3`，测试可配），保留现有 `_progress_callback` 更新进度与原子 `commit_answer`。
   - 为每个任务构造组合取消信号 `job_cancel = threading.Event()`（任一触发：`self._stop` 置位、续租返回 `False` 失去租约/收到取消、续租线程异常早退），传入 `answer_engine.answer(..., cancel_event=job_cancel)`。
   - 续租线程遇 SQLite 临时繁忙（`OperationalError: locked/busy`）做有限退避重试而非直接退出；服务关闭时立即停止续租并释放线程。
   - `answer_engine.answer` 返回后**先停止并 `join` 续租线程**，再判断租约/取消状态：已失租则不调用 `commit_answer`；若因取消触发则走 `complete` 收尾为 `cancelled`。
3. `hello_agents/tools/builtin/rag_summary_workflow.py`（按核实结果最小修改）：
   - 确保并发逐篇摘要与汇总阶段在 `cancel_event.is_set()` 时**不再向线程池派发后续文档任务**并跳过最终汇总。
4. 对应测试文件（如 `tests/test_qa_worker*.py`、`tests/test_qa_job_repository*.py`）：
   - 使用短租约 + `threading.Barrier` / `threading.Event` + 显式 SQLite 写锁占用，确定性复现并验证：慢调用超租约保活、等锁越过到期被拒、同 worker 跨 attempt 迟到写被拒、DB 临时 busy 自愈、续租线程早退/服务关闭不提交、并发阶段取消停派发。

### 执行命令
```bash
# 在独立 worktree 运行既有与新增回归测试
pytest -q tests/test_qa_job*.py tests/test_qa_worker*.py tests/test_rag_summary*.py
# 提交前触发独立 Astra 审查（审查 diff：并发/续租/锁内计时/取消/日志脱敏）
```

### 验收判据
- 旧问题可被确定性测试复现，修复后全量相关测试 100% 通过且无遗留活跃续租线程。
- 原子 `commit_answer`、进度更新、取消终态转换语义不变；Astra 独立审查无未决阻断项。

---

## 四、步骤 2：真实公开文档与真实模型双路径验收

### 测试矩阵与隔离要求
- **语料**：3–5 篇有明确页码与主题交集的**公开真实文档**（严禁生产私有文档）。
- **环境**：独立运行目录、独立 SQLite/索引目录、独立端口、专用测试模型账号或明确授权测试渠道；**严禁将凭据或私有内容传给外部或写入验收记录**。
- **对比路径**：同一批文档与问题，分别在未设置变量（顺序）与 `ZHIYAN_MULTI_SUMMARY_WORKFLOW=langgraph`（重启生效）下执行，开启 `hello_agents.tools.builtin.rag_summary_workflow` `INFO` 日志（关闭 LangSmith 与 LangChain 全局调试）。
- **必录维度**：问题与期望引用（文档+页码）、冷/暖缓存耗时、模型调用次数、部分失败（制造 1 篇失败/不可用，核验返回剩余总结与未纳入标识）、运行中取消、按 `run_id` 聚合的结构化日志、产品 HTTP 任务/消息/引用契约。

### 执行命令（示例框架）
```bash
# 顺序基线（独立目录与端口）
ZHIYAN_DATA_DIR=/tmp/zy-eval-seq unset ZHIYAN_MULTI_SUMMARY_WORKFLOW  # 按实际启动脚本配置
# LangGraph 对照（独立目录或清空缓存后复测冷/暖缓存）
ZHIYAN_DATA_DIR=/tmp/zy-eval-lg ZHIYAN_MULTI_SUMMARY_WORKFLOW=langgraph ...
```

### 验收判据
- 两条路径均完成真实端到端调用；HTTP 任务状态机与引用契约一致，答案准确命中期望文档与页码。
- `langgraph` 路径下冷/暖缓存、模型调用数符合预期（每阶段单次尝试、暖缓存命中）；部分失败与取消行为符合契约；`INFO` 日志含 `run_id` 且无文档正文/凭据泄露。
- **无可用模型降级**：记录真实 HTTP 状态码、错误类型与时间戳留档，**不判定通过**，继续步骤 1，阻断步骤 3。

---

## 五、步骤 3：合入目标判定与隔离 Canary（受步骤 1+2 强门禁约束）

### 执行命令与动作
```bash
# 1. 核对生产镜像/Compose 基准与 6b52fe4、f02a660 的提交差异
git log --oneline --graph --decorate -n 20 HEAD codex/next-improvements
# 2. 若生产基准早于 f02a660，切出基于生产基准的干净集成范围（cherry-pick 仅本特性提交，禁夹带无关未发布功能）
# 3. 隔离站点/独立 Compose 实例或专用测试账号启用 canary：
#    保持默认配置关闭；仅在隔离实例设 ZHIYAN_MULTI_SUMMARY_WORKFLOW=langgraph 并演练清除+重启回退
```

### 观测指标与判据
- 连续观测隔离 Canary 的任务失败率、端到端耗时、模型调用量，与顺序基线无异常劣化。
- 回退演练（清变量+重启）验证可即时恢复顺序执行；默认配置保持关闭。

---

## 六、待核实点

1. `app/qa_job_repository.py` 全文件：确认 `recover_expired`、`request_cancel`、`qa_jobs` 建表/迁移中 `lease_duration_seconds` 是否允许 `NULL` 及是否存在其他在锁前取 `_utc_now()` 的写路径。
2. `app/qa_answer_engine.py` 与 `hello_agents/tools/builtin/rag_summary_workflow.py`：核实 `cancel_event` 在并发 `ThreadPoolExecutor` / LangGraph 节点间是否阻止新任务派发。
3. `app/qa_worker.py`：`QaWorkerPool.stop()` 阻塞 `join()` 时，若底层模型 HTTP 调用未感知 `cancel_event`，确认超时与优雅退出边界。
4. 生产部署配置（Compose/镜像 tag/分支）：确认当前生产线上精确 commit，以及是否存在可隔离的测试站点/独立实例路径（现有单 `app` 实例无账号白名单）。

---

## 七、阻断条件（命中任一项立即停止向下推进）

- **阻断合入/启用**：步骤 2 缺乏真实模型与公开文档实测证据，或实测发现引用错误、契约不符、日志泄密、异常高失败率/调用量翻倍。
- **阻断合入/启用**：步骤 1 确定性并发/锁等待/失租测试未通过，或独立 Astra 审查存在未解决阻断意见。
- **阻断全量/生产开开关**：生产单实例无法通过隔离站点或独立实例实现小范围隔离——**严禁对全部生产用户直接开启全局开关**。
- **阻断集成**：合入路径会连带引入 `codex/next-improvements` 中无关未发布功能，或污染主工作区/干扰同机独立验证任务。

## 取样修复补充回执

模型 gemini-4-neon-high，会话 59fb2c8c-6d68-49d4-b865-97e72a92d81d，两次只读文件调用成功。

### 补充任务包：分页摘要空余补片与提示词收紧

基于 brief.md 与 context.txt，在**不提升模型费用上限、生产默认关闭、不降低原验收**前提下，实施以下最小改动：

#### 一、最小改动
1. **仅修本地 JSON 摘要取样（不动 Qdrant）**
   - 修改 `get_document_summary_context`（context.txt）：先按 `(page_number, chunk_index, 原序号)` 排序并按页分组。
   - **多页优先**：先对各页首块执行原 `sample_evenly(..., limit)`；页数 `>= limit` 时逻辑不变。
   - **空余均匀补片**：页数 `< limit` 时，用剩余 `limit - len(pages)` 有界槽位，对各页后续切块按页均匀/轮询抽样补齐至 `limit`。
   - **保持原序**：合并后按 `(page_number, chunk_index, 原序号)` 重排返回。
2. **旧缓存失效**
   - 核对 `_get_cached_document_summary` 的 `cache_key`（context.txt）：确认随 `results` 切块变化自然失效，或显式递增摘要取样/提示词版本标记，避免命中旧首段缓存。
3. **略收紧提示词防截断（不调高输出预算）**
   - 调整单篇与联合总结提示词（context.txt）：单篇提炼核心规则与稳定引用；联合总结保留 4 节结构，限定第 1 节每篇 1–2 句，第 2–4 节仅列增量要点、禁止跨节复述，确保 `1024` tokens 内完整收尾。

#### 二、测试与验收
1. **单元测试**：覆盖 ① 2–3 页多块场景（补入同页后段、严格保序、总数 `<= limit`）；② 页数 `> limit`（多页均匀覆盖不退化）；③ 取样/版本变更触发旧缓存失效。
2. **实测重跑**：沿用原 DeepSeek Chat 账号与原 RFC 9110/9111/9112 问题重跑，两条路径均须无截断输出，命中幂等性、`Expires` 优先级、消息长度三项规则并附稳定持久引用；同时复核 Graph 冷/暖 4/1 调用、取消、部分失败及默认开关无回退。
