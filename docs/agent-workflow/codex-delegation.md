# Antigravity × Codex 显式派单与 Harness 协同契约

本文档将当前仓库分散在 `AGENTS.md`、`docs/agent-workflow/README.md` 与 `skills/astra-neon-handoff/` 中的 Codex 协同规则收敛为一套**可执行、可监控、可验证的显式 Harness 协议**，并由右侧边栏 `Codex Harness`（Antigravity MCP App）提供单向实时监控与派单闭环。

## 1. 角色与模型路由矩阵（映射 `AGENTS.md`）

| 任务类型 (`task_type`) | 推荐角色 (`role`) | 模型 (`model`) | 推理强度 (`effort`) | 默认沙箱 (`sandbox`) | 强制门禁 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 事务修复与回归测试 (`implement_transaction`) | `sol` | `gpt-6.2-sol` | `high` | `workspace-write` | 须写明最小复现顺序与回归命令 |
| 报告下载 / 存储接口 / 一般迁移 (`implement_general`) | `sol` | `gpt-6.2-sol` | `medium`（复杂或涉及归属校验自动升 `high`） | `workspace-write` | 跨用户归属校验须验证拒绝访问 |
| 并发 / 租约过期 / 跨存储一致性 / 回退安全 (`review_concurrency`) | `astra` | `gpt-6.2-astra` | `high` | `read-only` | 独立只读审查，发现交 `sol high` 修复后由 `astra` 复核 |
| 日志整理 / 进度更新 / 机械性修改 (`chore`) | `luna`（不可用时回退 `sol`） | `luna` / `gpt-6.2-sol` | `high`（回退时 `low`/`medium`） | `workspace-write` | 记录回退原因，不触碰核心业务 |
| Neon 只读规划交接 (`plan_neon`) | `neon` | `gemini-4-neon-high` | `high` | `read-only` | 独立只读规划目录，禁止写入与命令执行 |

## 2. 派单必须携带的显式契约字段

每次通过 `codex_dispatch`（或在右侧面板发起派单）向 Codex 派单时，Harness 自动封装并持久化以下字段，作为单向监控卡片的「第 N 轮派单」权威记录：

1. **目标与可验证完成条件（Objective & Acceptance）**：小改动用一句话写明预期行为；事务/并发问题写清操作顺序与必须成立的不变量。
2. **显式改动边界（Strict Scope）**：
   - `allowed_files`：允许创建或修改的仓库相对路径白名单；
   - `forbidden_paths`：明确禁止触碰的路径、持久化格式或架构边界。
3. **验证命令（Verification Commands）**：固定使用本仓库 `.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-<tag> ...` 等可执行命令。
4. **任务包绑定（Packet Binding，可选）**：绑定 `docs/agent-workflow/task-packets/<plan>/<packet>.md` 时，Harness 自动预检依赖任务包是否全部 `done` 并提取文件白名单。

## 3. 门禁与只读监控闭环

1. **派单前门禁（Pre-flight Gate）**：
   - 校验路径不越出工作区根目录；
   - 审查类角色（`astra` / `review_concurrency`）强制锁定 `sandbox=read-only`；
   - 若绑定任务包，校验 `depends-on` 全部已完成且不触碰敏感文件（`.env`、`auth.json` 等）。
2. **执行中单向实时监控（One-Way Real-Time Monitor）**：
   - 右侧 `Codex Harness` 面板统一聚合 **Harness 显式派单会话** 与 **本机 `~/.codex/sessions/` 全部会话**；
   - 实时展示每轮派单 Prompt、思考叙述、Shell 命令（命令内容、退出码、单条耗时）、改动文件数与 Unified Diff、子 Agent 活动以及总耗时。
3. **派单后范围审计（Post-run Scope Audit）**：
   - 对比派单前后工作区改动与 `FileChange` 记录，一旦发现修改了 `allowed_files` 之外或命中 `forbidden_paths` 的文件，立即在面板与工具返回中标红 `scope_violation`。

## 4. 快捷操作指引

- 在 Antigravity 对话中可调用 `open_codex_harness` 打开右侧面板，或调用 `codex_route_policy`、`codex_list_task_packets`、`codex_dispatch`、`codex_dispatch_status`、`codex_verify_scope`、`codex_cancel_dispatch`。
- 派单前建议先用 `dry_run=true` 预览路由决策、预检门禁和完整派单 Prompt，确认无误后再正式下发。
