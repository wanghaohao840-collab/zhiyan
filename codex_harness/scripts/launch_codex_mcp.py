"""Codex 插件可移植启动器：兼容仓库原地运行与 Codex 插件缓存目录隔离运行。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def _bootstrap_package(plugin_root: Path) -> None:
    """无论目录名是否为 codex_harness，都将当前插件根目录注册为 codex_harness 包。"""
    if (plugin_root.parent / "codex_harness" / "__init__.py").exists():
        parent_str = str(plugin_root.parent)
        if parent_str not in sys.path:
            sys.path.insert(0, parent_str)
        return

    init_file = plugin_root / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "codex_harness",
        init_file,
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load codex_harness package from {plugin_root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["codex_harness"] = module
    spec.loader.exec_module(module)


def _reexec_with_workspace_venv_if_needed(script_path: Path) -> None:
    """若当前 Python 缺少 pydantic 且目标工作区存在 venv，则自动切换至工作区 venv 运行。"""
    if importlib.util.find_spec("pydantic") is not None:
        return
    if os.getenv("CODEX_HARNESS_REEXECED") == "1":
        return

    candidates: list[Path] = []
    ws_env = os.getenv("CODEX_HARNESS_WORKSPACE")
    if ws_env:
        candidates.append(Path(ws_env))
    candidates.append(Path.cwd())

    for root in candidates:
        for rel in ("venv/Scripts/python.exe", ".venv/Scripts/python.exe", "venv/bin/python", ".venv/bin/python"):
            py = root / rel
            if py.exists() and py.resolve() != Path(sys.executable).resolve():
                env = dict(os.environ)
                env["CODEX_HARNESS_REEXECED"] = "1"
                raise SystemExit(subprocess.call([str(py), str(script_path), *sys.argv[1:]], env=env))


def main() -> int:
    """定位默认工作区、单文件 UI 与台账目录后启动 stdio MCP 服务。"""
    script_path = Path(__file__).resolve()
    plugin_root = script_path.parent.parent
    _reexec_with_workspace_venv_if_needed(script_path)
    _bootstrap_package(plugin_root)

    default_html = plugin_root / "ui" / "dist" / "index.html"
    if default_html.exists():
        os.environ.setdefault("CODEX_HARNESS_APP_HTML", str(default_html))

    # 默认与 Antigravity 插件共享同一份本地台账目录
    default_data_dir = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "codex-harness"
    os.environ.setdefault("CODEX_HARNESS_DATA_DIR", str(default_data_dir))
    os.environ.setdefault("CODEX_HARNESS_WORKSPACE", str(Path.cwd()))

    from codex_harness.api.mcp_server import main as server_main  # noqa: E402

    return server_main()


if __name__ == "__main__":
    raise SystemExit(main())
