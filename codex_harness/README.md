# Antigravity × Codex Harness (`Codex Monitor`)

将本仓库的 `AGENTS.md` 模型分工、`docs/agent-workflow/` 任务包门禁与 Codex 派单/监控流程收敛为一个**双平台显式 Harness 插件**：

- **Antigravity 侧**：原生 MCP App 插件（`aux_pane` 右侧边栏 + `global` 左侧插件入口），以 **Antigravity ↔ Codex 双边对话气泡 + 精简可折叠执行进度卡** 的形式单向实时监控本机 Codex 会话与显式派单。
- **Codex 侧**：通过 Codex Marketplace 插件或 `codex mcp add` 直接接入同一套 MCP 工具与 `$codex-harness` 技能，共享派单与范围审计台账。

## 一、在 Antigravity 中安装

```powershell
# 若需重新编译前端单文件 HTML（仓库已附带编译好的 ui/dist/index.html，可跳过）
cd codex_harness/ui
npm install
npm run build
cd ../..

# 一键部署到 ~/.gemini/config/plugins/codex-harness/
.\venv\Scripts\python.exe -m codex_harness.install_plugin
```

部署完成后，在 Antigravity 右侧辅栏点击 `+` → **Codex Monitor**，或从左侧扩展插件栏打开。

## 二、在 Codex 中通过 GitHub Marketplace 安装

```powershell
# 1. 添加本仓库为 Codex Marketplace 源
codex plugin marketplace add https://github.com/wanghaohao840-collab/- --ref codex/next-improvements

# 2. 从该 Marketplace 安装 codex-harness 插件
codex plugin add codex-harness@zhiyan-codex-marketplace
```

或使用 `codex mcp add` 直接挂载本地运行器：

```powershell
codex mcp add codex-harness `
  --env PYTHONUTF8=1 `
  --env PYTHONUNBUFFERED=1 `
  -- .\venv\Scripts\python.exe "$HOME\.gemini\config\plugins\codex-harness\bin\run_server.py" --data-dir "$HOME\.gemini\antigravity\plugin_data\codex-harness" --workspace "$PWD"
```

## 三、开发与验证

```powershell
.\venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-codex-harness tests/test_codex_harness.py
```

完整派单契约见 [docs/agent-workflow/codex-delegation.md](../docs/agent-workflow/codex-delegation.md)。
