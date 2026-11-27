"""将仓库内的 Codex Harness 插件打包并同步安装到 Antigravity 本地插件目录。"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def install_plugin(
    repo_root: Path,
    target_dir: Path,
    python_executable: Path | None = None,
) -> dict[str, str]:
    """同步 plugin.json、mcp_config.json、UI 单文件 HTML、规则、技能与启动器。"""
    repo = repo_root.resolve()
    dest = target_dir.resolve()
    assets_dir = repo / "codex_harness" / "plugin_assets"
    built_html = repo / "codex_harness" / "ui" / "dist" / "index.html"

    if not assets_dir.exists():
        raise FileNotFoundError(f"Missing plugin assets directory: {assets_dir}")
    if not built_html.exists():
        raise FileNotFoundError(
            f"Built MCP App HTML not found at {built_html}. Run npm run build in codex_harness/ui first."
        )

    py_exe = (python_executable or (repo / "venv" / "Scripts" / "python.exe")).resolve()
    if not py_exe.exists():
        py_exe = Path(sys.executable).resolve()

    dest.mkdir(parents=True, exist_ok=True)
    for sub in ("assets", "bin", "dist", "rules", "skills/codex-harness"):
        (dest / sub).mkdir(parents=True, exist_ok=True)

    shutil.copy2(assets_dir / "plugin.json", dest / "plugin.json")
    shutil.copy2(assets_dir / "logo.svg", dest / "assets" / "logo.svg")
    shutil.copy2(assets_dir / "rules" / "AGENTS.md", dest / "rules" / "AGENTS.md")
    shutil.copy2(
        assets_dir / "skills" / "codex-harness" / "SKILL.md",
        dest / "skills" / "codex-harness" / "SKILL.md",
    )
    shutil.copy2(built_html, dest / "dist" / "app.html")

    delegation_doc = repo / "docs" / "agent-workflow" / "codex-delegation.md"
    if delegation_doc.exists():
        shutil.copy2(delegation_doc, dest / "README.md")

    launcher_code = f'''"""Antigravity Codex Harness 插件启动器。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path({str(repo)!r})
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("CODEX_HARNESS_APP_HTML", str(Path(__file__).resolve().parent.parent / "dist" / "app.html"))
os.environ.setdefault("CODEX_HARNESS_WORKSPACE", str(REPO_ROOT))

from codex_harness.api.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
'''
    (dest / "bin" / "run_server.py").write_text(launcher_code, encoding="utf-8")

    mcp_config = {
        "mcpServers": {
            "codex_harness": {
                "command": str(py_exe),
                "args": [
                    "${PLUGIN_ROOT}/bin/run_server.py",
                    "--data-dir",
                    "${PLUGIN_DATA}",
                    "--workspace",
                    str(repo),
                ],
                "env": {
                    "PYTHONUTF8": "1",
                    "PYTHONUNBUFFERED": "1",
                },
            }
        }
    }
    (dest / "mcp_config.json").write_text(
        json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    LOGGER.info("Installed codex-harness plugin to %s", dest)
    return {
        "plugin_dir": str(dest),
        "python": str(py_exe),
        "app_html": str(dest / "dist" / "app.html"),
        "mcp_config": str(dest / "mcp_config.json"),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    default_repo = Path(__file__).resolve().parent.parent
    default_target = Path.home() / ".gemini" / "config" / "plugins" / "codex-harness"

    parser = argparse.ArgumentParser(description="安装/同步 Codex Harness Antigravity 插件")
    parser.add_argument("--repo", default=str(default_repo), help="仓库根目录")
    parser.add_argument("--target", default=str(default_target), help="插件目标目录")
    args = parser.parse_args(argv)

    result = install_plugin(Path(args.repo), Path(args.target))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
