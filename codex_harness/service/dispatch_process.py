"""Own the processes started by one local dispatch attempt."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


_STOP_TIMEOUT = 4.0


if os.name == "nt":
    import ctypes
    from ctypes import wintypes as win

    def _kernel_function(name, result, *arguments):
        function = getattr(ctypes.WinDLL("kernel32", use_last_error=True), name)
        function.restype = result
        function.argtypes = arguments
        return function

    _create_job = _kernel_function("CreateJobObjectW", win.HANDLE, win.LPVOID, win.LPCWSTR)
    _set_job = _kernel_function("SetInformationJobObject", win.BOOL, win.HANDLE, win.INT, win.LPVOID, win.DWORD)
    _query_job = _kernel_function(
        "QueryInformationJobObject", win.BOOL, win.HANDLE, win.INT, win.LPVOID, win.DWORD, win.LPDWORD
    )
    _assign_job = _kernel_function("AssignProcessToJobObject", win.BOOL, win.HANDLE, win.HANDLE)
    _terminate_job = _kernel_function("TerminateJobObject", win.BOOL, win.HANDLE, win.UINT)
    _close_handle = _kernel_function("CloseHandle", win.BOOL, win.HANDLE)
    _snapshot = _kernel_function("CreateToolhelp32Snapshot", win.HANDLE, win.DWORD, win.DWORD)
    _open_process = _kernel_function("OpenProcess", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
    _open_thread = _kernel_function("OpenThread", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
    _resume_thread = _kernel_function("ResumeThread", win.DWORD, win.HANDLE)

    _CREATE_SUSPENDED = 0x00000004
    _PROCESS_TERMINATE_AND_SET_QUOTA = 0x00000101
    _THREAD_SUSPEND_RESUME = 0x00000002
    _SNAPSHOT_THREADS = 0x00000004
    _KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_BASIC_ACCOUNTING = 1
    _JOB_EXTENDED_LIMIT = 9
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _DWORD_ERROR = win.DWORD(-1).value

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("per_process_time", ctypes.c_int64),
            ("per_job_time", ctypes.c_int64),
            ("limit_flags", win.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_process_limit", win.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", win.DWORD),
            ("scheduling_class", win.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("basic_limit", _BasicLimit),
            ("io_counters", ctypes.c_uint64 * 6),
            ("memory_limits_and_peaks", ctypes.c_size_t * 4),
        ]

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("times", ctypes.c_int64 * 4),
            ("total_page_faults", win.DWORD),
            ("total_processes", win.DWORD),
            ("active_processes", win.DWORD),
            ("total_terminated_processes", win.DWORD),
        ]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", win.DWORD),
            ("usage", win.DWORD),
            ("thread_id", win.DWORD),
            ("owner_pid", win.DWORD),
            ("base_priority", win.LONG),
            ("delta_priority", win.LONG),
            ("flags", win.DWORD),
        ]

    _thread_first = _kernel_function("Thread32First", win.BOOL, win.HANDLE, ctypes.POINTER(_ThreadEntry))
    _thread_next = _kernel_function("Thread32Next", win.BOOL, win.HANDLE, ctypes.POINTER(_ThreadEntry))

    def _resume_suspended_process(pid: int) -> None:
        # Popen closes the primary thread handle. While still suspended the process
        # has only its primary thread, which we can reopen through Toolhelp.
        snapshot = _snapshot(_SNAPSHOT_THREADS, 0)
        if snapshot == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = _ThreadEntry(size=ctypes.sizeof(_ThreadEntry))
            found = _thread_first(snapshot, ctypes.byref(entry))
            while found and entry.owner_pid != pid:
                found = _thread_next(snapshot, ctypes.byref(entry))
            if not found:
                raise OSError(f"Cannot find the suspended dispatch thread for pid {pid}")
            thread = _open_thread(_THREAD_SUSPEND_RESUME, False, entry.thread_id)
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if _resume_thread(thread) == _DWORD_ERROR:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _close_handle(thread)
        finally:
            _close_handle(snapshot)

    class _WindowsJob:
        def __init__(self) -> None:
            self.handle = _create_job(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimit()
            limits.basic_limit.limit_flags = _KILL_ON_JOB_CLOSE
            if not _set_job(self.handle, _JOB_EXTENDED_LIMIT, ctypes.byref(limits), ctypes.sizeof(limits)):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

        def attach_and_resume(self, pid: int) -> None:
            process = _open_process(_PROCESS_TERMINATE_AND_SET_QUOTA, False, pid)
            if not process:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not _assign_job(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _close_handle(process)
            _resume_suspended_process(pid)

        def is_active(self) -> bool:
            accounting = _BasicAccounting()
            if not _query_job(self.handle, _JOB_BASIC_ACCOUNTING, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return accounting.active_processes > 0

        def kill(self) -> None:
            if not _terminate_job(self.handle, 1):
                error = ctypes.WinError(ctypes.get_last_error())
                if self.is_active():
                    raise error

        def close(self) -> None:
            _close_handle(self.handle)


def _posix_process_states():
    """Yield (pid, pgid, state); an exited zombie may still hold its process ID."""
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").iterdir():
            if entry.name.isdecimal():
                try:
                    info = (entry / "stat").read_bytes()
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                state, _, group, *_ = info.rsplit(b") ", 1)[1].split()
                yield int(entry.name), int(group), state.decode("ascii")
        return

    snapshot = subprocess.run(["ps", "-Ao", "pid=,pgid=,stat="], capture_output=True, text=True)
    if snapshot.returncode != 0:
        raise OSError(f"Cannot inspect dispatch process group: {snapshot.stderr.strip()}")
    for line in snapshot.stdout.splitlines():
        pid, group, state = line.split()
        yield int(pid), int(group), state[0]


class DispatchProcess:
    """Keep the attempt's Job (Windows) or isolated process group (POSIX)."""

    def __init__(self, command: list[str], workspace: Path, env: dict[str, str]) -> None:
        self._job = _WindowsJob() if os.name == "nt" else None
        options = (
            {"creationflags": _CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW}
            if self._job else {"start_new_session": True}
        )
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **options,
            )
            if self._job:
                try:
                    self._job.attach_and_resume(self.process.pid)
                except BaseException:
                    # Job assignment can fail under a restricted parent Job. The
                    # suspended process must never run without tree ownership.
                    with self.process:
                        self.process.kill()
                    raise
        except BaseException:
            self.close()
            raise

    def is_active(self) -> bool:
        if self._job:
            return self._job.is_active()

        # Keep the leader unreaped until signaling is finished so its group ID
        # cannot be assigned to an unrelated process between check and kill.
        pid = self.process.pid
        if hasattr(os, "waitid") and os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            return True
        return any(group == pid and state != "Z" for _, group, state in _posix_process_states())

    def stop(self) -> bool:
        """Terminate the owned processes and confirm none are still running."""
        if not self.is_active():
            return False
        deadline = time.monotonic() + _STOP_TIMEOUT
        while True:
            if self._job:
                self._job.kill()
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    return False
            if not self.is_active():
                return True
            if time.monotonic() >= deadline:
                raise TimeoutError("Dispatch processes did not exit after termination")
            time.sleep(0.02)

    def close(self) -> None:
        if self._job:
            self._job.close()

    def wait(self) -> None:
        try:
            self.process.wait(timeout=_STOP_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Dispatch process did not exit after its process group stopped") from exc
