# Antigravity × Codex 显式 Harness 协同规则

当在当前工作区需要调用 Codex 实施代码、修复事务缺陷、执行并发/一致性独立审查或跟踪 Codex 运行进度时，遵循以下显式 Harness 规则：

1. **先路由再派单**：调用 `codex_route_policy` 依据仓库 `AGENTS.md` 获取推荐角色（`Sol` / `Astra` / `Luna`）、模型、推理强度和沙箱模式。并发、租约过期、旧 Worker 拒写、跨存储一致性与回退安全审查强制由 `gpt-6.2-astra`（`high`，`read-only` 沙箱）独立承担。
2. **显式声明范围与验收**：调用 `codex_dispatch` 时必须填写明确目标、可验证完成条件、`allowed_files` 白名单与可执行验收命令；高风险任务先用 `dry_run=true` 预检。
3. **实时监控与范围审计**：派单后可调用 `open_codex_harness` 在右侧边栏打开 `Codex Monitor` 进行单向实时监控，完成后通过 `codex_verify_scope` 核查改动是否越出白名单或触碰敏感文件。
4. **保持既有项目约定**：完整派单协议见 `docs/agent-workflow/codex-delegation.md`，任务包状态机见 `docs/agent-workflow/README.md`。
