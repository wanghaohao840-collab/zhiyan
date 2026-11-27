from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from codex_harness.api.mcp_server import build_default_server
from codex_harness.model.entities import DispatchStatus
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.service.dispatch_service import DispatchService


def _until(predicate, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    pytest.fail("Dispatch process did not reach the expected state")


@contextmanager
def _watch_process(pid: int):
    if os.name == "nt":
        import _winapi

        handle = _winapi.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        try:
            yield lambda: _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    else:
        def is_running() -> bool:
            try:
                os.kill(pid, 0)
                if sys.platform.startswith("linux"):
                    state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1][:1]
                    return state != b"Z"
                return True
            except (FileNotFoundError, ProcessLookupError):
                return False

        yield is_running


@pytest.mark.parametrize("wrapper_exits_first", [False, True])
def test_dispatch_cancel_waits_for_task_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrapper_exits_first: bool
) -> None:
    ready = tmp_path / "task-pid"
    release = tmp_path / "release-task"
    exit_wrapper = tmp_path / "exit-wrapper"
    task = tmp_path / "task process.py"
    task.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "pidfile, release = map(Path, sys.argv[1:])\n"
        "pidfile.write_text(str(os.getpid()))\n"
        "for _ in range(300):\n"
        "    if release.exists():\n"
        "        break\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "codex wrapper.py"
    wrapper.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "exit_wrapper = Path(sys.argv[1])\n"
        "child = subprocess.Popen([sys.executable, *sys.argv[2:]])\n"
        "while child.poll() is None:\n"
        "    if exit_wrapper.exists():\n"
        "        os._exit(0)\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    args = [sys.executable, str(wrapper), str(exit_wrapper), str(task), str(ready), str(release)]
    if os.name == "nt":
        cmd_wrapper = tmp_path / "codex wrapper.cmd"
        cmd_wrapper.write_text('@echo off\n%*\n', encoding="ascii")
        args = [str(cmd_wrapper), *args]

    data = tmp_path / "dispatch-data"
    repo = DispatchRepository(data)
    server = build_default_server(tmp_path, data, session_roots=[])
    monkeypatch.setattr("codex_harness.service.dispatch_service.resolve_codex_executable", lambda: sys.executable)
    monkeypatch.setattr(DispatchService, "_build_codex_command", lambda *a, **kw: args)
    launched = server.call_tool("codex_dispatch", {
        "title": "Test local process cancellation", "objective": "Stop task descendants", "auto_infer": False,
    })["structuredContent"]
    run_id = launched["run_id"]
    try:
        leader_pid = _until(lambda: repo.get(run_id).pid)
        _until(lambda: ready.is_file() and ready.stat().st_size)
        task_pid = int(ready.read_text())
        with _watch_process(leader_pid) as leader_running, _watch_process(task_pid) as task_running:
            assert leader_running() and task_running()
            if wrapper_exits_first:
                exit_wrapper.touch()
                _until(lambda: not leader_running())
                assert task_running()
            server.call_tool("codex_cancel_dispatch", {"run_id": run_id})
            _until(lambda: repo.get(run_id).completed_at)
            assert repo.get(run_id).status == DispatchStatus.CANCELLED
            assert not leader_running()
            assert not task_running()
    finally:
        release.touch()
