---
name: codex-harness
description: 使用 Antigravity × Codex 显式 Harness 框架路由模型、检查任务包门禁、向本机 Codex 发起受控派单并在右侧 Codex Monitor 面板实时监控执行轨迹。
---

# Codex Harness 显式派单与监控工作流

当你代表用户将实现、修复或独立审查任务交给本机 Codex 执行，或需要查看 Codex 实时进度时，按以下四步推进：

1. **打开或定位监控视图**：需要直观查看本机 Codex 历史与实时执行轨迹时，调用 `open_codex_harness`（在 Antigravity 中无参数即可在右侧边栏打开 `Codex Monitor`）。
2. **检查任务包与模型路由**：
   - 若基于仓库任务包推进，先调用 `codex_list_task_packets` 确认 `depends-on` 已全部 `done`；
   - 调用 `codex_route_policy` 获取 `AGENTS.md` 推荐的 `Sol / Astra / Luna` 模型、推理强度和强制门禁。
3. **干跑预检后显式派单**：
   - 优先使用 `codex_dispatch(..., dry_run=true)` 校验路径白名单、禁止路径与完整派单 Prompt；
   - 确认无误后设 `dry_run=false` 正式下发，随后通过 `codex_dispatch_status` 或右侧 `Codex Monitor` 单向实时观察命令、退出码与文件变更。
4. **完成后强制范围门禁验收**：
   - 调用 `codex_verify_scope(run_id=...)` 核验所有新增改动均落在 `allowed_files` 内且未触碰敏感文件；
   - 若命中并发/租约/跨存储风险，再派发一轮 `task_type="review_concurrency"` 给 `gpt-6.2-astra` (`high`, `read-only`) 独立复核。
