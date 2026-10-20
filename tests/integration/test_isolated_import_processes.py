"""Windows process acceptance for the isolated import runtime.

Real cases require the root-reviewed dedicated dependency environment and a
serial pytest lane. The child entry in this file is a test-only fault wrapper;
ordinary API and Worker cases execute their original entrypoints directly.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager, nullcontext
import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from threading import Event, Lock, Thread, get_ident, local
import time
from types import SimpleNamespace
from typing import Any, Callable
from uuid import UUID, uuid4

import httpx
import pytest


REPO = Path(__file__).resolve().parents[2]
PYTHON = Path(r"D:\python_self_agent\venv\Scripts\python.exe")
MODULE = "tests.integration.test_isolated_import_processes"
MAX_OUTPUT = 256 * 1024
CONTROL_POLL = 0.05
DATA_POLL = 0.20
JOB_EXIT_SECONDS = 20.0
CASE_SECONDS = 300.0
_SAFE_SCENARIOS = frozenset({
    "claim_before_intent", "intent_before_issue_return",
    "document_bytes_before_evidence", "document_evidence_committed",
    "rag_seal_before_evidence", "rag_evidence_committed",
    "episode_seal_before_evidence", "pair_evidence_committed",
    "terminal_commit_before_return", "stale_authority", "late_candidate_bytes",
    "unknown_document_put", "unknown_terminal_reply", "pause_live_capture",
    "pause_expired_capture", "recovery_natural_retry",
    "recovery_ack_after_wait", "recovery_abandon_after_wait",
    "queue_capture_before_user_wait",
    "expiry_scan_after_task_wait",
    "heartbeat_terminal_after_wait",
    "two_user_fairness",
})
_DEATH_BOUNDARIES = (
    "claim_before_intent", "intent_before_issue_return",
    "document_bytes_before_evidence", "document_evidence_committed",
    "rag_seal_before_evidence", "rag_evidence_committed",
    "episode_seal_before_evidence", "pair_evidence_committed",
    "terminal_commit_before_return",
)
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_SCENARIO = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OWNERS_LOCK = Lock()
_OWNERS: dict[int, "OwnedJob"] = {}
_RETIRED_JOBS: list[dict[str, object]] = []
_RESOURCE_OWNERS: dict[str, object] = {}


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError("control_record_shape")
    return value


@dataclass(frozen=True)
class ControlIdentity:
    nonce: str
    case: str
    role: str
    directory: Path

    def __post_init__(self) -> None:
        if not _NONCE.fullmatch(self.nonce) or not _SCENARIO.fullmatch(self.case):
            raise ValueError("invalid_control_identity")
        if self.role not in {"api", "worker", "neutral"}:
            raise ValueError("invalid_control_role")
        if not self.directory.is_absolute() or not self.directory.is_dir():
            raise ValueError("invalid_control_directory")
        if self.directory.is_symlink():
            raise ValueError("symlink_control_directory")
        if not self.directory.resolve(strict=True).is_relative_to(
                (REPO / ".runtime").resolve(strict=True)):
            raise ValueError("control_directory_outside_runtime")

    def event_path(self, stage: str, hit: int) -> Path:
        return self.directory / f"{self.role}-{stage}-{hit}.event.json"

    def release_path(self, stage: str, hit: int) -> Path:
        return self.directory / f"{self.role}-{stage}-{hit}.release.json"


class ChildControl:
    """An explicit one-hit file barrier; the original method runs once."""

    def __init__(self, identity: ControlIdentity):
        self.identity = identity
        self._lock = Lock()
        self._counts: dict[str, int] = {}
        manifest = identity.directory / f"{identity.role}.manifest.json"
        self.target = _read_json(manifest) if manifest.exists() else {}
        if self.target and any(self.target.get(key) != value for key, value in (
                ("nonce", identity.nonce), ("case", identity.case),
                ("role", identity.role))):
            raise ValueError("control_manifest_mismatch")

    def matches(self, user_id: str, task_id: str) -> bool:
        return (self.target.get("user_id") == user_id
                and self.target.get("task_id") == task_id)

    def event(self, stage: str, detail: dict[str, object] | None = None) -> int:
        if not _SCENARIO.fullmatch(stage):
            raise ValueError("invalid_control_stage")
        with self._lock:
            hit = self._counts.get(stage, 0) + 1
            self._counts[stage] = hit
        payload: dict[str, object] = {
            "nonce": self.identity.nonce, "case": self.identity.case,
            "role": self.identity.role, "stage": stage, "hit": hit,
            "pid": os.getpid(), "ppid": os.getppid(),
            "executable": sys.executable,
            "base_executable": getattr(sys, "_base_executable", sys.executable),
            "at_monotonic": time.monotonic(),
        }
        if detail:
            if set(detail) & set(payload):
                raise ValueError("control_identity_field_override")
            payload.update(detail)
        _atomic_json(self.identity.event_path(stage, hit), payload)
        return hit

    def pause(self, stage: str, detail: dict[str, object] | None = None,
              timeout: float = 240.0) -> None:
        hit = self.event(stage, detail)
        path = self.identity.release_path(stage, hit)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                record = _read_json(path)
                expected = {"nonce": self.identity.nonce, "case": self.identity.case,
                            "role": self.identity.role, "stage": stage, "hit": hit}
                if record != expected:
                    raise ValueError("control_release_mismatch")
                return
            time.sleep(CONTROL_POLL)
        raise TimeoutError("control_release_timeout")


def _release(identity: ControlIdentity, stage: str, hit: int = 1) -> None:
    path = identity.release_path(stage, hit)
    if not _SCENARIO.fullmatch(stage) or hit < 1:
        raise ValueError("invalid_release_identity")
    record = {"nonce": identity.nonce, "case": identity.case,
              "role": identity.role, "stage": stage, "hit": hit}
    # Claim issuance exclusively; publish the actual release atomically so a
    # polling child never observes a partially written JSON object.
    issued = path.with_name(path.name + ".issued")
    with issued.open("x", encoding="utf-8") as stream:
        stream.write(identity.nonce)
        stream.flush()
        os.fsync(stream.fileno())
    _atomic_json(path, record)


def _await_event(identity: ControlIdentity, stage: str, *, hit: int = 1,
                 timeout: float = 60.0) -> dict[str, object]:
    path = identity.event_path(stage, hit)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            record = _read_json(path)
            expected = {"nonce": identity.nonce, "case": identity.case,
                        "role": identity.role, "stage": stage, "hit": hit}
            if any(record.get(k) != v for k, v in expected.items()):
                raise AssertionError("control_event_mismatch")
            return record
        time.sleep(CONTROL_POLL)
    raise TimeoutError("control_arrival_timeout")


if os.name == "nt":
    import msvcrt

    ULONG_PTR = ctypes.c_size_t
    SIZE_T = ctypes.c_size_t
    HANDLE = wintypes.HANDLE
    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD),
                    ("dwYSize", DWORD), ("dwXCountChars", DWORD),
                    ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
                    ("dwFlags", DWORD), ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                    ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                    ("hStdError", HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW),
                    ("lpAttributeList", LPVOID)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                    ("dwProcessId", DWORD), ("dwThreadId", DWORD)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", DWORD), ("MinimumWorkingSetSize", SIZE_T),
                    ("MaximumWorkingSetSize", SIZE_T),
                    ("ActiveProcessLimit", DWORD), ("Affinity", ULONG_PTR),
                    ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", SIZE_T),
                    ("JobMemoryLimit", SIZE_T), ("PeakProcessMemoryUsed", SIZE_T),
                    ("PeakJobMemoryUsed", SIZE_T)]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong),
                    ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                    ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", DWORD), ("TotalProcesses", DWORD),
                    ("ActiveProcesses", DWORD), ("TotalTerminatedProcesses", DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _api(name: str, restype: object, *argtypes: object):
        function = getattr(kernel32, name)
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    _create_pipe = _api("CreatePipe", wintypes.BOOL, ctypes.POINTER(HANDLE),
                        ctypes.POINTER(HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), DWORD)
    _set_handle = _api("SetHandleInformation", wintypes.BOOL, HANDLE, DWORD, DWORD)
    _create_job = _api("CreateJobObjectW", HANDLE, LPVOID, wintypes.LPCWSTR)
    _set_job = _api("SetInformationJobObject", wintypes.BOOL, HANDLE, ctypes.c_int,
                    LPVOID, DWORD)
    _query_job = _api("QueryInformationJobObject", wintypes.BOOL, HANDLE,
                      ctypes.c_int, LPVOID, DWORD, ctypes.POINTER(DWORD))
    _assign_job = _api("AssignProcessToJobObject", wintypes.BOOL, HANDLE, HANDLE)
    _is_in_job = _api("IsProcessInJob", wintypes.BOOL, HANDLE, HANDLE,
                      ctypes.POINTER(wintypes.BOOL))
    _terminate_job = _api("TerminateJobObject", wintypes.BOOL, HANDLE, DWORD)
    _terminate_process = _api("TerminateProcess", wintypes.BOOL, HANDLE, DWORD)
    _close_handle = _api("CloseHandle", wintypes.BOOL, HANDLE)
    _init_attributes = _api("InitializeProcThreadAttributeList", wintypes.BOOL,
                            LPVOID, DWORD, DWORD, ctypes.POINTER(SIZE_T))
    _update_attributes = _api("UpdateProcThreadAttribute", wintypes.BOOL,
                              LPVOID, DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID,
                              ctypes.POINTER(SIZE_T))
    _delete_attributes = _api("DeleteProcThreadAttributeList", None, LPVOID)
    _create_process = _api("CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR,
                           wintypes.LPWSTR, LPVOID, LPVOID, wintypes.BOOL,
                           DWORD, LPVOID, wintypes.LPCWSTR,
                           ctypes.POINTER(STARTUPINFOW),
                           ctypes.POINTER(PROCESS_INFORMATION))
    _resume_thread = _api("ResumeThread", DWORD, HANDLE)
    _wait = _api("WaitForSingleObject", DWORD, HANDLE, DWORD)
    _get_exit = _api("GetExitCodeProcess", wintypes.BOOL, HANDLE,
                     ctypes.POINTER(DWORD))
    _open_process = _api("OpenProcess", HANDLE, DWORD, wintypes.BOOL, DWORD)
    _get_times = _api("GetProcessTimes", wintypes.BOOL, HANDLE,
                      ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                      ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
    _query_image = _api("QueryFullProcessImageNameW", wintypes.BOOL, HANDLE,
                        DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD))
    _peek_pipe = _api("PeekNamedPipe", wintypes.BOOL, HANDLE, LPVOID, DWORD,
                      ctypes.POINTER(DWORD), ctypes.POINTER(DWORD), ctypes.POINTER(DWORD))
    _read_file = _api("ReadFile", wintypes.BOOL, HANDLE, LPVOID, DWORD,
                      ctypes.POINTER(DWORD), LPVOID)

    CREATE_SUSPENDED = 0x00000004
    CREATE_NO_WINDOW = 0x08000000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    STARTF_USESTDHANDLES = 0x00000100
    HANDLE_FLAG_INHERIT = 0x00000001
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    WAIT_FAILED = 0xFFFFFFFF
    INFINITE = 0xFFFFFFFF
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _win_error(operation: str) -> OSError:
    number = ctypes.get_last_error()
    return OSError(number, operation)


def _handle_value(handle: object) -> int:
    value = handle.value if hasattr(handle, "value") else handle
    if value in (None, 0, -1, getattr(ctypes.c_void_p(-1), "value", -1)):
        raise ValueError("invalid_handle")
    return int(value)


def _unicode_environment(env: dict[str, str]) -> ctypes.Array:
    if any(not isinstance(key, str) or not isinstance(value, str)
           or not key or "=" in key or "\x00" in key or "\x00" in value
           for key, value in env.items()):
        raise ValueError("invalid_child_environment")
    block = ("\x00".join(f"{key}={value}" for key, value in sorted(env.items()))
             + "\x00\x00")
    return ctypes.create_unicode_buffer(block, len(block))


class _OutputDrain:
    def __init__(self, handle: int, label: str):
        self.handle = handle
        self.label = label
        self.bytes = bytearray()
        self.truncated = False
        self.error: Exception | None = None
        self._stop = Event()
        self.thread = Thread(target=self._run, name=f"p6-pipe-{label}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        if os.name != "nt":
            return
        try:
            while not self._stop.is_set():
                available = DWORD()
                if not _peek_pipe(self.handle, None, 0, None,
                                  ctypes.byref(available), None):
                    if ctypes.get_last_error() == 109:  # ERROR_BROKEN_PIPE
                        break
                    raise _win_error("PeekNamedPipe")
                if not available.value:
                    self._stop.wait(0.01)
                    continue
                buffer = ctypes.create_string_buffer(min(4096, available.value))
                count = DWORD()
                if not _read_file(self.handle, buffer, len(buffer),
                                  ctypes.byref(count), None):
                    if ctypes.get_last_error() == 109:
                        break
                    raise _win_error("ReadFile")
                remaining = MAX_OUTPUT - len(self.bytes)
                if count.value > remaining:
                    self.truncated = True
                if remaining > 0:
                    self.bytes.extend(buffer.raw[:min(count.value, remaining)])
        except Exception as error:
            self.error = error

    def finish(self, timeout: float = 5.0) -> bytes:
        self.thread.join(timeout)
        if self.thread.is_alive():
            self._stop.set()
            self.thread.join(1.0)
        if self.thread.is_alive():
            raise TimeoutError("pipe_drain_active")
        if self.error is not None:
            raise self.error
        return bytes(self.bytes)


class OwnedJob:
    """Create suspended, assign a private kill-on-close Job, then resume."""

    def __init__(self, argv: list[str], env: dict[str, str], *, role: str,
                 cwd: Path = REPO):
        if os.name != "nt":
            raise RuntimeError("Windows acceptance requires Win32")
        if not argv or Path(argv[0]) != PYTHON or role not in {"api", "worker", "neutral"}:
            raise ValueError("unsafe_child_argv")
        self.role = role
        self.argv = tuple(argv)
        self.cwd = cwd
        self.job = 0
        self.launcher = 0
        self.launcher_pid = 0
        self.launcher_identity: tuple[str, int] | None = None
        self.launcher_exit: int | None = None
        self.startup_exit_diagnostic: dict[str, object] | None = None
        self.thread = 0
        self.assigned = False
        self.runtime: dict[int, tuple[int, str, int]] = {}
        self.observed_runtime: dict[int, tuple[str, int]] = {}
        self.exit_codes: dict[int, int] = {}
        self._unclassified_handles: list[int] = []
        self._pipe_handles: list[int] = []
        self._drains: list[_OutputDrain] = []
        self._closed = False
        # Keep failed constructors reachable until their owned handles and
        # processes have been accounted for. Assignment to the caller's local
        # has not happened yet if _launch raises.
        with _OWNERS_LOCK:
            _OWNERS[id(self)] = self
        self._launch(env)

    def _pipe(self, *, child_read: bool = False) -> tuple[int, int]:
        attributes = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        read, write = HANDLE(), HANDLE()
        if not _create_pipe(ctypes.byref(read), ctypes.byref(write),
                            ctypes.byref(attributes), 0):
            raise _win_error("CreatePipe")
        left, right = _handle_value(read), _handle_value(write)
        self._pipe_handles.extend((left, right))
        if not child_read and not _set_handle(left, HANDLE_FLAG_INHERIT, 0):
            raise _win_error("SetHandleInformation")
        return left, right

    def _close_pipe(self, handle: int) -> None:
        if not _close_handle(handle):
            raise _win_error("CloseHandle(pipe)")
        self._pipe_handles.remove(handle)

    def _start_drains(self, stdout_read: int, stderr_read: int) -> None:
        for handle, label in ((stdout_read, "stdout"), (stderr_read, "stderr")):
            self._drains.append(_OutputDrain(handle, label))

    def _launch(self, env: dict[str, str]) -> None:
        block = _unicode_environment(env)
        size = SIZE_T(0)
        attributes = None
        initialized = False
        process = PROCESS_INFORMATION()
        created = False
        try:
            self.job = _handle_value(_create_job(None, None))
            limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _set_job(self.job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                            ctypes.byref(limits), ctypes.sizeof(limits)):
                raise _win_error("SetInformationJobObject")
            stdin_read, stdin_write = self._pipe(child_read=True)
            stdout_read, stdout_write = self._pipe()
            stderr_read, stderr_write = self._pipe()
            # Parent never writes to stdin. Closing the write end delivers EOF.
            self._close_pipe(stdin_write)
            _init_attributes(None, 1, 0, ctypes.byref(size))
            if not size.value:
                raise _win_error("InitializeProcThreadAttributeList(size)")
            attributes = ctypes.create_string_buffer(size.value)
            if not _init_attributes(attributes, 1, 0, ctypes.byref(size)):
                raise _win_error("InitializeProcThreadAttributeList")
            initialized = True
            inherit = (HANDLE * 3)(stdin_read, stdout_write, stderr_write)
            if not _update_attributes(attributes, 0,
                                      PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                      ctypes.byref(inherit), ctypes.sizeof(inherit),
                                      None, None):
                raise _win_error("UpdateProcThreadAttribute")
            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = stdin_read
            startup.StartupInfo.hStdOutput = stdout_write
            startup.StartupInfo.hStdError = stderr_write
            startup.lpAttributeList = ctypes.cast(attributes, LPVOID)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(self.argv))
            flags = (CREATE_SUSPENDED | CREATE_NO_WINDOW |
                     CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT)
            if not _create_process(str(PYTHON), command, None, None, True, flags,
                                   ctypes.cast(block, LPVOID), str(self.cwd),
                                   ctypes.byref(startup.StartupInfo),
                                   ctypes.byref(process)):
                raise _win_error("CreateProcessW")
            created = True
            self.launcher = _handle_value(process.hProcess)
            self.launcher_pid = process.dwProcessId
            self.thread = _handle_value(process.hThread)
            self.launcher_identity = self._process_identity(self.launcher)
            for handle in (stdin_read, stdout_write, stderr_write):
                self._close_pipe(handle)
            self._start_drains(stdout_read, stderr_read)
            if not _assign_job(self.job, self.launcher):
                raise _win_error("AssignProcessToJobObject")
            self.assigned = True
            member = wintypes.BOOL()
            if not _is_in_job(self.launcher, self.job, ctypes.byref(member)) or not member.value:
                raise _win_error("IsProcessInJob")
            if _resume_thread(process.hThread) == 0xFFFFFFFF:
                raise _win_error("ResumeThread")
            self._retain(self.launcher_pid, self.launcher)
        except BaseException as launch_error:
            cleanup_errors: list[Exception] = []
            if created and self.launcher:
                if _wait(self.launcher, 0) != WAIT_OBJECT_0:
                    if not _terminate_process(self.launcher, 3):
                        cleanup_errors.append(_win_error("TerminateProcess(launch failure)"))
                    elif _wait(self.launcher, 5000) != WAIT_OBJECT_0:
                        cleanup_errors.append(TimeoutError("launch_failure_process_active"))
                if _wait(self.launcher, 0) == WAIT_OBJECT_0:
                    code = DWORD()
                    if _get_exit(self.launcher, ctypes.byref(code)):
                        self.launcher_exit = code.value
                    else:
                        cleanup_errors.append(_win_error("GetExitCodeProcess(launch failure)"))
            try:
                self.close(force=True)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise BaseExceptionGroup("launch_and_cleanup_failure",
                                         [launch_error, *cleanup_errors]) from launch_error
            raise
        finally:
            if initialized:
                _delete_attributes(attributes)

    def _members(self) -> set[int]:
        size = 256
        while True:
            buffer = ctypes.create_string_buffer(size)
            returned = DWORD()
            if _query_job(self.job, JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                          buffer, size, ctypes.byref(returned)):
                assigned, count = (DWORD * 2).from_buffer_copy(buffer.raw[:8])
                offset = 8
                width = ctypes.sizeof(ULONG_PTR)
                return {int.from_bytes(buffer.raw[offset + i * width:offset + (i + 1) * width],
                                       sys.byteorder) for i in range(count)}
            if ctypes.get_last_error() != 234 or size >= 1 << 20:
                raise _win_error("QueryInformationJobObject(process list)")
            size *= 2

    def active_count(self) -> int:
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not _query_job(self.job, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                          ctypes.byref(info), ctypes.sizeof(info), None):
            raise _win_error("QueryInformationJobObject(accounting)")
        return info.ActiveProcesses

    @staticmethod
    def _process_identity(handle: int) -> tuple[str, int]:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _get_times(handle, ctypes.byref(created), ctypes.byref(exited),
                          ctypes.byref(kernel), ctypes.byref(user)):
            raise _win_error("GetProcessTimes")
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        chars = DWORD(32768)
        image = ctypes.create_unicode_buffer(chars.value)
        if not _query_image(handle, 0, image, ctypes.byref(chars)):
            raise _win_error("QueryFullProcessImageNameW")
        return image.value, stamp

    def _retain(self, pid: int, existing: int | None = None) -> None:
        if pid in self.runtime:
            return
        if existing is None:
            opened = _open_process(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                                   False, pid)
            if not opened:
                raise _win_error("OpenProcess")
            handle = _handle_value(opened)
        else:
            handle = existing
        try:
            member = wintypes.BOOL()
            if not _is_in_job(handle, self.job, ctypes.byref(member)) or not member.value:
                raise AssertionError("process_membership_mismatch")
            image, stamp = self._process_identity(handle)
            self.runtime[pid] = (handle, image, stamp)
            self.observed_runtime[pid] = (image, stamp)
        except Exception as query_error:
            if handle != existing:
                if not _close_handle(handle):
                    self._unclassified_handles.append(handle)
                    raise ExceptionGroup("process_query_and_handle_close",
                                         [query_error, _win_error("CloseHandle(unclassified)")])
            raise

    def observe(self) -> set[int]:
        members = self._members()
        for pid in members - self.runtime.keys():
            try:
                self._retain(pid)
            except OSError:
                # A transient may exit before OpenProcess. Job-zero remains
                # authoritative for absence; no exit code is invented.
                if pid in self._members():
                    raise
        return members

    def wait_empty(self, timeout: float = JOB_EXIT_SECONDS) -> dict[int, int]:
        deadline = time.monotonic() + timeout
        while True:
            self.observe()
            if self.active_count() == 0:
                exits: dict[int, int] = {}
                pending = False
                for pid, (handle, _, _) in self.runtime.items():
                    remaining = deadline - time.monotonic()
                    result = _wait(handle, max(0, min(50, int(remaining * 1000))))
                    if result == WAIT_TIMEOUT:
                        pending = True
                        break
                    if result != WAIT_OBJECT_0:
                        raise _win_error("WaitForSingleObject(member)")
                    code = DWORD()
                    if not _get_exit(handle, ctypes.byref(code)):
                        raise _win_error("GetExitCodeProcess")
                    exits[pid] = code.value
                if not pending:
                    self.launcher_exit = exits.get(self.launcher_pid, self.launcher_exit)
                    self.exit_codes.update(exits)
                    return exits
            if time.monotonic() >= deadline:
                raise TimeoutError("member_not_signalled" if self.active_count() == 0
                                   else "owned_job_still_active")
            time.sleep(0.02)

    def terminate(self, code: int = 3,
                  timeout: float = JOB_EXIT_SECONDS) -> dict[int, int]:
        self.observe()
        if self.active_count() and not _terminate_job(self.job, code):
            raise _win_error("TerminateJobObject")
        return self.wait_empty(timeout)

    def output(self) -> tuple[bytes, bytes, tuple[bool, bool]]:
        if self.active_count():
            raise AssertionError("output_before_exit")
        first, second = (drain.finish() for drain in self._drains)
        return first, second, tuple(drain.truncated for drain in self._drains)

    def close(self, *, force: bool = False) -> None:
        if self._closed:
            return
        errors = []
        checked_job = False
        if self.job and self.active_count():
            if force:
                if not _terminate_job(self.job, 3):
                    raise _win_error("TerminateJobObject(cleanup)")
                self.wait_empty(5)
            else:
                self.terminate()
            checked_job = True
        if self.job and (self.assigned or self.runtime) and not checked_job:
            self.wait_empty(5 if force else JOB_EXIT_SECONDS)
        # An assignment failure leaves a suspended launcher outside the Job.
        # Keep its handle if termination or the signalled-exit proof fails.
        if self.launcher:
            launcher_wait = _wait(self.launcher, 0)
            if launcher_wait == WAIT_TIMEOUT:
                if not force:
                    raise TimeoutError("launcher_still_active")
                if not _terminate_process(self.launcher, 3):
                    raise _win_error("TerminateProcess(unassigned launcher)")
                launcher_wait = _wait(self.launcher, 5000)
                if launcher_wait == WAIT_TIMEOUT:
                    raise TimeoutError("unassigned_launcher_still_active")
            if launcher_wait != WAIT_OBJECT_0:
                raise _win_error("WaitForSingleObject(launcher cleanup)")
        if self.launcher and self.launcher_exit is None:
            code = DWORD()
            if not _get_exit(self.launcher, ctypes.byref(code)):
                raise _win_error("GetExitCodeProcess(launcher cleanup)")
            self.launcher_exit = code.value
        for drain in self._drains:
            try:
                drain.finish(2)
            except Exception as error:
                errors.append(error)
        if any(drain.thread.is_alive() for drain in self._drains):
            raise ExceptionGroup("owned_pipe_reader_active", errors)
        for handle in tuple(self._pipe_handles):
            try:
                self._close_pipe(handle)
            except Exception as error:
                errors.append(error)
        for pid, (handle, _, _) in tuple(self.runtime.items()):
            if handle == self.launcher:
                continue
            if not _close_handle(handle):
                errors.append(_win_error(f"CloseHandle(runtime {pid})"))
            else:
                del self.runtime[pid]
        for handle in tuple(self._unclassified_handles):
            if not _close_handle(handle):
                errors.append(_win_error("CloseHandle(unclassified)"))
            else:
                self._unclassified_handles.remove(handle)
        for name in ("thread", "launcher", "job"):
            handle = getattr(self, name)
            if handle and not _close_handle(handle):
                errors.append(_win_error(f"CloseHandle({name})"))
            else:
                setattr(self, name, 0)
        if self.launcher == 0:
            self.runtime.pop(self.launcher_pid, None)
        self._closed = not (self.job or self.launcher or self.thread
                            or self._pipe_handles or self.runtime
                            or self._unclassified_handles)
        if self._closed:
            with _OWNERS_LOCK:
                _RETIRED_JOBS.append({
                    "role": self.role, "launcher_pid": self.launcher_pid,
                    "launcher_identity": self.launcher_identity,
                    "launcher_exit": self.launcher_exit,
                    "runtime_identity": self.observed_runtime.copy(),
                    "exit_codes": self.exit_codes.copy(),
                    "cleanup_error_types": [type(error).__name__ for error in errors],
                })
                _OWNERS.pop(id(self), None)
        if errors:
            raise ExceptionGroup("owned_job_cleanup", errors)


def _retire_owned_jobs() -> None:
    with _OWNERS_LOCK:
        owners = tuple(_OWNERS.values())
    errors: list[Exception] = []
    for owner in owners:
        try:
            if owner.job and owner.active_count():
                owner.terminate()
            owner.close(force=True)
        except Exception as error:
            errors.append(error)
    if errors:
        # A caller must not remove a control directory or resource fixture
        # while a failed constructor still owns a live launcher/Job.
        raise ExceptionGroup("owned_jobs_not_retired", errors)


def _owners_snapshot() -> list[dict[str, object]]:
    with _OWNERS_LOCK:
        owners = tuple(_OWNERS.values())
    return [{"role": owner.role, "launcher_pid": owner.launcher_pid,
             "launcher_identity": owner.launcher_identity,
             "launcher_exit": owner.launcher_exit,
             "retained_pids": sorted(owner.runtime),
             "pipe_handle_count": len(owner._pipe_handles),
             "unclassified_handle_count": len(owner._unclassified_handles),
             "reader_active_count": sum(drain.thread.is_alive()
                                        for drain in owner._drains)}
            for owner in owners]


def _release_observed(identity: ControlIdentity, stages: tuple[str, ...]) -> list[str]:
    failures = []
    for stage in stages:
        event = identity.event_path(stage, 1)
        if not event.exists():
            continue
        try:
            record = _read_json(event)
            expected = {"nonce": identity.nonce, "case": identity.case,
                        "role": identity.role, "stage": stage, "hit": 1}
            if any(record.get(key) != value for key, value in expected.items()):
                failures.append(f"{stage}:identity_mismatch")
                continue
        except Exception as error:
            failures.append(f"{stage}:{type(error).__name__}")
            continue
        release = identity.release_path(stage, 1)
        if release.exists():
            continue
        if release.with_name(release.name + ".issued").exists():
            failures.append(f"{stage}:issued_without_record")
            continue
        try:
            _release(identity, stage)
        except Exception as error:
            failures.append(f"{stage}:{type(error).__name__}")
    return failures


def _remaining(deadline: float, cap: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("neutral_case_deadline")
    return min(remaining, cap)


def _neutral_main(control: ControlIdentity, *, descendant: bool) -> int:
    channel = ChildControl(control)
    if descendant:
        channel.pause("descendant")
        return 0
    child = subprocess.Popen([str(PYTHON), "-m", MODULE, "--neutral-descendant",
                              str(control.directory), control.nonce, control.case],
                             cwd=REPO, close_fds=True)
    try:
        channel.pause("parent", {"descendant_launcher_pid": child.pid})
        return child.wait(20)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(5)


def _child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--worker-child", choices=sorted(_SAFE_SCENARIOS))
    group.add_argument("--neutral-child", action="store_true")
    group.add_argument("--neutral-descendant", action="store_true")
    parser.add_argument("directory")
    parser.add_argument("nonce")
    parser.add_argument("case")
    args = parser.parse_args(argv)
    directory = Path(args.directory).resolve(strict=True)
    if not directory.is_relative_to(REPO / ".runtime"):
        parser.error("control directory is outside the private runtime")
    role = "worker" if args.worker_child else "neutral"
    identity = ControlIdentity(args.nonce, args.case, role, directory)
    if args.worker_child and args.case != args.worker_child:
        parser.error("scenario mismatch")
    if args.neutral_child or args.neutral_descendant:
        return _neutral_main(identity, descendant=args.neutral_descendant)
    return _fault_worker_main(args.worker_child, identity)


def _fault_worker_main(scenario: str, identity: ControlIdentity) -> int:
    # Installed before opening the real runtime. Exact wrappers are implemented
    # below and never imported by production code.
    control = ChildControl(identity)
    _install_fault(scenario, control)
    from scripts.run_isolated_import_worker import main
    return main()


def _matching_late_upsert(scope: Any, generation_id: str, admitted_thread: int,
                          collection: object, points: object, wait: object,
                          args: tuple[object, ...]) -> bool:
    """Select only the next complete physical batch for the admitted intent."""
    if (admitted_thread != get_ident() or args or wait is not True
            or collection != scope.identity.physical_collection
            or not isinstance(points, list) or not 0 < len(points) <= 100):
        return False
    from hello_agents.memory.storage.generation_vector_store import _physical_id

    expected = {"_gv_tenant": scope.tenant_id, "_gv_kind": scope.vector_kind,
                "_gv_namespace": scope.namespace, "_gv_index": scope.index_key,
                "_gv_generation": generation_id}
    for point in points:
        payload = getattr(point, "payload", None)
        if not isinstance(payload, dict) or any(
                payload.get(key) != value for key, value in expected.items()):
            return False
        logical_id = payload.get("_gv_logical_id")
        if (not isinstance(logical_id, str) or not logical_id
                or str(getattr(point, "id", "")) != _physical_id(
                    scope, UUID(generation_id), logical_id)):
            return False
    return True


def _install_fault(scenario: str, control: ChildControl) -> None:
    from app.distributed_import_worker import IsolatedImportWorker
    from app.import_document_publication import ImportDocumentPublicationService
    from app.import_memory_publication import (
        DurablePublicationUnknown, ImportMemoryPublicationService)
    from app.import_publication_proof import (
        ImportPublicationProofService, PublicationProofUnknown)
    from app.import_publication_recovery import PostgresImportPublicationRecoveryRepository
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    from app.postgres_import_leases import PostgresImportLeaseRepository
    from app.postgres_vector_generations import PostgresVectorGenerationAuthority
    from app.vector_generation_service import VectorGenerationService

    if scenario not in _SAFE_SCENARIOS or not control.target:
        raise ValueError("fault_scenario_or_manifest_refused")

    def attempt_matches(attempt: object) -> bool:
        return control.matches(attempt.task.user_id, attempt.task.task_id)

    def attempt_detail(attempt: object) -> dict[str, object]:
        return {"user_id": attempt.task.user_id, "task_id": attempt.task.task_id,
                "worker_id": attempt.worker_id,
                "lease_version": attempt.lease_version,
                "tuple_sha256": _digest((attempt.worker_id, attempt.lease_token,
                    attempt.lease_version, attempt.user_lease.lease_token,
                    attempt.user_lease.lease_version))}

    def one_hit(cls: type, name: str, predicate: Callable[..., bool],
                detail: Callable[..., dict[str, object]], *, before: bool) -> None:
        original = getattr(cls, name)
        gate = Lock()
        used = False

        def wrapped(self, *args, **kwargs):
            nonlocal used
            if not predicate(self, *args, **kwargs):
                return original(self, *args, **kwargs)
            with gate:
                if used:
                    return original(self, *args, **kwargs)
                used = True
            fields = detail(self, *args, **kwargs)
            if before:
                control.pause("fault", fields)
            try:
                result = original(self, *args, **kwargs)
            except Exception as error:
                control.event("original_exception", fields | {"exception": type(error).__name__})
                raise
            control.event("original_returned", fields)
            if not before:
                control.pause("fault", fields)
            return result

        setattr(cls, name, wrapped)

    if scenario == "claim_before_intent":
        one_hit(IsolatedImportWorker, "_forward",
                lambda _self, attempt, *_args: attempt_matches(attempt),
                lambda _self, attempt, *_args: attempt_detail(attempt), before=True)
    elif scenario == "intent_before_issue_return":
        one_hit(PostgresImportPublicationEvidenceRepository, "reserve_intent",
                lambda _self, attempt, _intent: attempt_matches(attempt),
                lambda _self, attempt, _intent: attempt_detail(attempt), before=False)
    elif scenario in {"document_bytes_before_evidence", "unknown_document_put"}:
        original = ImportDocumentPublicationService._write_planned_document
        gate = Lock()
        document_write_used = False

        def document_write(self, planned, attempt, *, live=None):
            nonlocal document_write_used
            if not attempt_matches(attempt):
                return original(self, planned, attempt, live=live)
            with gate:
                if document_write_used:
                    return original(self, planned, attempt, live=live)
                document_write_used = True
            prepared = original(self, planned, attempt, live=live)
            control.event("original_returned", attempt_detail(attempt))
            if scenario == "unknown_document_put":
                control.event("fault", attempt_detail(attempt))
                raise RuntimeError("p6_lost_document_put_reply")
            control.pause("fault", attempt_detail(attempt))
            return prepared

        ImportDocumentPublicationService._write_planned_document = document_write
    elif scenario in {"document_evidence_committed", "rag_seal_before_evidence",
                      "rag_evidence_committed", "episode_seal_before_evidence"}:
        kind = "episode" if scenario.startswith("episode") or scenario == "rag_evidence_committed" else "rag"
        before = scenario.endswith("committed")

        def matches_scope(_self, scope, owner, *_args, **_kwargs):
            return (scope.vector_kind == kind and scope.tenant_id == control.target["user_id"]
                    and attempt_matches(owner))

        def scope_detail(_self, scope, owner, *_args, **kwargs):
            return attempt_detail(owner) | {"vector_kind": scope.vector_kind,
                     "generation_id": str(kwargs["generation_id"])}

        one_hit(VectorGenerationService, "_prepare_sealed", matches_scope,
                scope_detail, before=before)
    elif scenario == "pair_evidence_committed":
        one_hit(ImportMemoryPublicationService, "_issue_fixed_terminal",
                lambda _self, _live, prepared, *_args: control.matches(
                    prepared.task.user_id, prepared.task.task_id),
                lambda _self, _live, prepared, *_args: {
                    "user_id": prepared.task.user_id, "task_id": prepared.task.task_id},
                before=True)
        if control.target.get("lock_role") in {"expiry_old", "heartbeat_old"}:
            lock_scenario = ("expiry_scan_after_task_wait" if
                control.target["lock_role"] == "expiry_old" else
                "heartbeat_terminal_after_wait")
            _install_lock_observers(lock_scenario, control)
            heartbeat_original = IsolatedImportWorker._heartbeat

            def observed_heartbeat(self, original, abort, forward_abort, deadline):
                if (not attempt_matches(original)
                        or original.worker_id != control.target["worker_id"]):
                    return heartbeat_original(self, original, abort, forward_abort, deadline)
                fields = attempt_detail(original)
                control.event("heartbeat_started", fields)
                try:
                    return heartbeat_original(self, original, abort, forward_abort, deadline)
                finally:
                    control.event("heartbeat_finally", fields)

            IsolatedImportWorker._heartbeat = observed_heartbeat
            recovery_original = IsolatedImportWorker._recovery_round
            recovery_gate = Lock()
            recovery_used = False

            def hold_old_recovery(self, abort, deadline):
                nonlocal recovery_used
                if (self.worker_id == control.target["worker_id"]
                        and control.identity.event_path("fault", 1).exists()):
                    with recovery_gate:
                        selected = not recovery_used
                        if selected:
                            recovery_used = True
                    if selected:
                        control.pause("old_recovery_enter", {
                            "worker_id": self.worker_id,
                            "user_id": control.target["user_id"],
                            "task_id": control.target["task_id"]})
                return recovery_original(self, abort, deadline)

            IsolatedImportWorker._recovery_round = hold_old_recovery
    elif scenario == "terminal_commit_before_return":
        one_hit(PostgresImportLeaseRepository, "complete",
                lambda _self, attempt, *_args, **_kwargs: attempt_matches(attempt),
                lambda _self, attempt, *_args, **_kwargs: attempt_detail(attempt),
                before=False)
    elif scenario == "stale_authority":
        authority_gate = Lock()
        count = 0
        authority_original = IsolatedImportWorker._authority

        def authority(self, attempt):
            nonlocal count
            if not attempt_matches(attempt):
                return authority_original(self, attempt)
            with authority_gate:
                count += 1
                hit = count
            if hit == 1:
                control.pause("fault", attempt_detail(attempt) | {"authority_hit": hit})
            try:
                result = authority_original(self, attempt)
            except Exception as error:
                control.event("original_exception", attempt_detail(attempt) |
                              {"exception": type(error).__name__, "authority_hit": hit})
                raise
            control.event("original_returned", attempt_detail(attempt) |
                          {"authority_hit": hit})
            return result

        IsolatedImportWorker._authority = authority
    elif scenario in {"unknown_terminal_reply", "pause_live_capture",
                      "pause_expired_capture", "recovery_natural_retry"}:
        original = ImportMemoryPublicationService._execute_live_publication
        gate = Lock()
        terminal_reply_used = False

        def terminal_reply(self, live):
            nonlocal terminal_reply_used
            issue = self._require_live_issue(live)
            key = issue.key
            if not control.matches(key.user_id, key.task_id):
                return original(self, live)
            with gate:
                if terminal_reply_used:
                    return original(self, live)
                terminal_reply_used = True
            original(self, live)
            control.event("original_returned", {"user_id": key.user_id,
                          "task_id": key.task_id,
                          "lease_version": key.task_lease_version})
            control.event("fault", {"user_id": key.user_id,
                          "task_id": key.task_id})
            raise DurablePublicationUnknown(key, "terminal_proof")

        ImportMemoryPublicationService._execute_live_publication = terminal_reply
    elif scenario == "late_candidate_bytes":
        from threading import get_ident, local
        from qdrant_client import QdrantClient
        from app.import_memory_publication import _require_live_issuer
        admitted = local()
        require = PostgresVectorGenerationAuthority.require_candidate
        upsert = QdrantClient.upsert
        upsert_gate = Lock()
        upsert_used = False

        def require_wrapper(self, live, scope, generation_id, *, operation):
            admitted.value = None  # an intervening check invalidates prior admission
            result = require(self, live, scope, generation_id, operation=operation)
            if (operation == "upload" and scope.vector_kind == "rag"
                    and scope.tenant_id == control.target["user_id"]):
                _service, issue = _require_live_issuer(live)
                if (not control.matches(issue.key.user_id, issue.key.task_id)
                        or issue.original_attempt.worker_id != control.target["worker_id"]
                        or issue.key.task_lease_version !=
                           issue.original_attempt.lease_version):
                    return result
                with self.database.transaction() as cursor:
                    evidence = PostgresImportPublicationEvidenceRepository(
                        self.database)._read_exact_in_cursor(cursor, issue.key)
                if evidence is None or evidence.intent_hash != issue.intent_hash:
                    raise AssertionError("late_candidate_intent_not_committed")
                original_attempt = issue.original_attempt
                frozen_attempt = evidence.intent["attempt"]
                if any((frozen_attempt[name] != value) for name, value in {
                        "user_id": issue.key.user_id,
                        "task_id": issue.key.task_id,
                        "task_lease_version": issue.key.task_lease_version,
                        "worker_id": original_attempt.worker_id,
                        "task_lease_token": str(original_attempt.lease_token),
                        "user_lease_token": str(original_attempt.user_lease.lease_token),
                        "user_lease_version": original_attempt.user_lease.lease_version,
                        "document_id": original_attempt.task.document_id,
                        }.items()):
                    raise AssertionError("late_candidate_attempt_mismatch")
                frozen = evidence.intent["scopes"]["rag"]
                if (str(generation_id) != frozen["candidate_id"]
                        or scope.key != (issue.key.user_id, "rag",
                                         frozen["namespace"], frozen["index_key"])
                        or scope.identity.to_dict() != frozen["identity"]
                        or scope != issue.plan.rag_scope):
                    raise AssertionError("late_candidate_scope_mismatch")
                admitted.value = (scope, str(generation_id), get_ident(),
                    {"user_id": scope.tenant_id,
                    "task_id": issue.key.task_id,
                    "lease_version": issue.key.task_lease_version,
                    "intent_hash": evidence.intent_hash,
                    "generation_id": str(generation_id), "thread": get_ident(),
                    "namespace": scope.namespace, "index_key": scope.index_key,
                    "collection": scope.identity.physical_collection})
            return result

        def upsert_wrapper(self, *args, **kwargs):
            nonlocal upsert_used
            current = getattr(admitted, "value", None)
            admitted.value = None
            collection = kwargs.get("collection_name")
            if current is None or not _matching_late_upsert(
                    current[0], current[1], current[2], collection,
                    kwargs.get("points"), kwargs.get("wait"), args):
                return upsert(self, *args, **kwargs)
            with upsert_gate:
                selected = not upsert_used
                if selected:
                    upsert_used = True
            if not selected:
                return upsert(self, *args, **kwargs)
            fields = {"generation_id": current[1], "thread": current[2],
                      "collection": collection, "point_count": len(kwargs["points"]),
                      "point_ids_sha256": _digest(tuple(str(point.id)
                                                     for point in kwargs["points"])),
                      "point_payloads_sha256": hashlib.sha256(json.dumps(
                          [point.payload for point in kwargs["points"]],
                          sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
            control.event("candidate_admitted", current[3] | fields)
            control.pause("fault", fields)
            try:
                result = upsert(self, *args, **kwargs)
            except Exception as error:
                control.event("original_exception", fields |
                              {"exception": type(error).__name__})
                raise
            control.event("original_returned", fields |
                          {"status": str(getattr(getattr(result, "status", None),
                                                  "value", ""))})
            return result

        PostgresVectorGenerationAuthority.require_candidate = require_wrapper
        QdrantClient.upsert = upsert_wrapper
        prepare_original = VectorGenerationService._prepare_sealed

        def observed_prepare(self, scope, owner, expected_head, complete_corpus,
                             **kwargs):
            selected = (scope.vector_kind == "rag"
                        and scope.tenant_id == control.target["user_id"])
            try:
                result = prepare_original(self, scope, owner, expected_head,
                                          complete_corpus, **kwargs)
            except Exception as error:
                if selected and control.identity.event_path("fault", 1).exists():
                    control.event("post_late_rejected", {
                        "exception": type(error).__name__,
                        "generation_id": str(kwargs.get("generation_id"))})
                raise
            if selected and control.identity.event_path("fault", 1).exists():
                control.event("post_late_returned", {
                    "generation_id": str(kwargs.get("generation_id"))})
            return result

        VectorGenerationService._prepare_sealed = observed_prepare
    elif scenario in {"recovery_ack_after_wait", "recovery_abandon_after_wait",
                       "queue_capture_before_user_wait",
                       "expiry_scan_after_task_wait",
                       "heartbeat_terminal_after_wait", "two_user_fairness"}:
        if scenario == "two_user_fairness":
            _install_lock_observers(scenario, control)
            round_original = IsolatedImportWorker.run_one_recovery
            round_gate = Lock()
            round_used = False

            def await_fairness_start(self):
                nonlocal round_used
                if self.worker_id == control.target["worker_id"]:
                    with round_gate:
                        selected = not round_used
                        round_used = True
                    if selected:
                        control.pause("fairness_enter", {
                            "worker_id": self.worker_id,
                            "user_id": control.target["user_id"],
                            "task_id": control.target["task_id"]})
                return round_original(self)

            IsolatedImportWorker.run_one_recovery = await_fairness_start
        elif control.target.get("lock_role") == "fresh":
            if scenario not in {"recovery_abandon_after_wait",
                                "queue_capture_before_user_wait",
                                "expiry_scan_after_task_wait",
                                "heartbeat_terminal_after_wait"}:
                raise ValueError("fresh_lock_role_unexpected")
            if scenario in {"queue_capture_before_user_wait",
                            "expiry_scan_after_task_wait",
                            "heartbeat_terminal_after_wait"}:
                _install_lock_observers(scenario, control)
            ordinary_original = IsolatedImportWorker._preflight
            fresh_gate = Lock()
            fresh_used = False

            def hold_fresh_ordinary(self, abort, deadline):
                nonlocal fresh_used
                if self.worker_id == control.target["worker_id"]:
                    with fresh_gate:
                        selected = not fresh_used
                        if selected:
                            fresh_used = True
                    if selected:
                        control.pause("fresh_ordinary_enter", {
                            "worker_id": self.worker_id,
                            "user_id": control.target["user_id"],
                            "task_id": control.target["task_id"]})
                return ordinary_original(self, abort, deadline)

            IsolatedImportWorker._preflight = hold_fresh_ordinary
        else:
            _install_lock_observers(scenario, control)
    else:
        raise ValueError("fault_scenario_unhandled")

    if scenario in {"stale_authority", "late_candidate_bytes"}:
        heartbeat_original = IsolatedImportWorker._heartbeat

        def observed_heartbeat(self, original, abort, forward_abort, deadline):
            if not attempt_matches(original) or original.worker_id != control.target["worker_id"]:
                return heartbeat_original(self, original, abort, forward_abort, deadline)
            fields = attempt_detail(original)
            control.event("heartbeat_started", fields)
            try:
                return heartbeat_original(self, original, abort, forward_abort, deadline)
            finally:
                control.event("heartbeat_finally", fields)

        IsolatedImportWorker._heartbeat = observed_heartbeat
        recovery_original = IsolatedImportWorker._recovery_round
        old_recovery_gate = Lock()
        old_recovery_used = False
        if not isinstance(control.target.get("worker_id"), str) or not control.target["worker_id"]:
            raise ValueError("old_recovery_worker_identity_required")

        def hold_old_recovery(self, abort, deadline):
            nonlocal old_recovery_used
            if (self.worker_id != control.target.get("worker_id")
                    or not control.identity.event_path("fault", 1).exists()):
                return recovery_original(self, abort, deadline)
            with old_recovery_gate:
                selected = not old_recovery_used
                if selected:
                    old_recovery_used = True
            if not selected:
                return recovery_original(self, abort, deadline)
            with self._lock:
                heartbeat = self._lanes.get("heartbeat")
                heartbeat_thread = heartbeat.thread if heartbeat is not None else None
            if heartbeat_thread is None:
                raise AssertionError("old_heartbeat_thread_missing")
            heartbeat_thread.join(5)
            if heartbeat_thread.is_alive() or not control.identity.event_path(
                    "heartbeat_finally", 1).exists():
                raise AssertionError("old_heartbeat_thread_not_exited")
            control.event("heartbeat_thread_exited", {"worker_id": self.worker_id})
            control.pause("old_recovery", {"worker_id": self.worker_id})
            try:
                result = recovery_original(self, abort, deadline)
            except Exception as error:
                control.event("old_recovery_exception", {"worker_id": self.worker_id,
                              "exception": type(error).__name__})
                raise
            control.event("old_recovery_returned", {"worker_id": self.worker_id,
                          "outcome": type(result).__name__})
            return result

        IsolatedImportWorker._recovery_round = hold_old_recovery

    if scenario in {"unknown_document_put", "unknown_terminal_reply",
                    "pause_live_capture", "pause_expired_capture",
                    "recovery_natural_retry"}:
        heartbeat_original = IsolatedImportWorker._heartbeat

        def observed_heartbeat(self, original, abort, forward_abort, deadline):
            if not attempt_matches(original) or original.worker_id != control.target["worker_id"]:
                return heartbeat_original(self, original, abort, forward_abort, deadline)
            fields = attempt_detail(original)
            control.event("heartbeat_started", fields)
            try:
                return heartbeat_original(self, original, abort, forward_abort, deadline)
            finally:
                control.event("heartbeat_finally", fields)

        IsolatedImportWorker._heartbeat = observed_heartbeat

    if scenario in {"unknown_document_put", "unknown_terminal_reply"}:
        ordinary_original = IsolatedImportWorker.run_one_ordinary
        ordinary_gate = Lock()
        ordinary_deferred = False

        def defer_old_ordinary(self):
            nonlocal ordinary_deferred
            if (self.worker_id != control.target["worker_id"]
                    or not control.identity.event_path("fault", 1).exists()
                    or control.identity.release_path("old_recovery", 1).exists()):
                return ordinary_original(self)
            with ordinary_gate:
                selected = not ordinary_deferred
                ordinary_deferred = True
            if selected:
                control.event("ordinary_deferred", {"worker_id": self.worker_id})
            return False

        IsolatedImportWorker.run_one_ordinary = defer_old_ordinary
        recovery_original = IsolatedImportWorker._recovery_round
        recovery_gate = Lock()
        unknown_recovery_used = False

        def hold_unknown_recovery(self, abort, deadline):
            nonlocal unknown_recovery_used
            if (self.worker_id != control.target["worker_id"]
                    or not control.identity.event_path("fault", 1).exists()):
                return recovery_original(self, abort, deadline)
            with recovery_gate:
                selected = not unknown_recovery_used
                unknown_recovery_used = True
            if not selected:
                return recovery_original(self, abort, deadline)
            with self._lock:
                heartbeat = self._lanes.get("heartbeat")
                heartbeat_thread = heartbeat.thread if heartbeat is not None else None
            if heartbeat_thread is None:
                raise AssertionError("unknown_heartbeat_thread_missing")
            heartbeat_thread.join(5)
            if (heartbeat_thread.is_alive() or
                    not control.identity.event_path("heartbeat_finally", 1).exists()):
                raise AssertionError("unknown_heartbeat_thread_not_exited")
            control.event("heartbeat_thread_exited", {"worker_id": self.worker_id})
            control.pause("old_recovery", {"worker_id": self.worker_id})
            try:
                result = recovery_original(self, abort, deadline)
            except Exception as error:
                control.event("old_recovery_exception", {
                    "worker_id": self.worker_id, "exception": type(error).__name__})
                raise
            control.event("old_recovery_returned", {
                "worker_id": self.worker_id, "outcome": type(result).__name__})
            return result

        IsolatedImportWorker._recovery_round = hold_unknown_recovery

    if scenario in {"pause_live_capture", "pause_expired_capture"}:
        round_original = IsolatedImportWorker._recovery_round
        round_gate = Lock()
        round_used = False

        def observed_first_round(self, abort, deadline):
            nonlocal round_used
            if (self.worker_id != control.target["worker_id"]
                    or not control.identity.event_path("fault", 1).exists()):
                return round_original(self, abort, deadline)
            with round_gate:
                selected = not round_used
                round_used = True
            if selected:
                control.pause("initial_recovery", {"worker_id": self.worker_id})
            return round_original(self, abort, deadline)

        IsolatedImportWorker._recovery_round = observed_first_round
        proof_original = PostgresImportPublicationRecoveryRepository.prove_or_hold
        pause_gate = Lock()
        recovery_pause_used = False

        def pause_renewed_capture(self, claim):
            nonlocal recovery_pause_used
            if not control.matches(claim.user_id, claim.task_id):
                return proof_original(self, claim)
            with pause_gate:
                selected = not recovery_pause_used
                recovery_pause_used = True
            if not selected:
                return proof_original(self, claim)
            fields = {"user_id": claim.user_id, "task_id": claim.task_id,
                      "queue_version": claim.queue_version,
                      "recovery_version": claim.recovery_version,
                      "capture_sha256": _digest((
                          claim.queue_claim_token, claim.recovery_token,
                          claim.queue_expires_at, claim.recovery_expires_at))}
            control.pause("recovery_pause", fields)
            try:
                result = proof_original(self, claim)
            except Exception as error:
                control.event("recovery_exception", fields |
                              {"exception": type(error).__name__})
                if scenario == "pause_expired_capture":
                    control.pause("refused_recovery", fields)
                raise
            assert result in {"proved_succeeded", "abandoned",
                              "manual_hold", "retry_later"}
            control.event("recovery_returned", fields | {"outcome": result})
            return result

        PostgresImportPublicationRecoveryRepository.prove_or_hold = pause_renewed_capture

    if scenario == "recovery_natural_retry":
        from threading import local
        active = local()
        round_original = IsolatedImportWorker._recovery_round
        proof_original = ImportPublicationProofService.prove_exact
        transient_original = PostgresImportPublicationRecoveryRepository._record_transient
        proof_gate = Lock()
        proof_reply_used = False
        transient_gate = Lock()
        transient_used = False
        round_gate = Lock()
        round_used = False

        def observed_round(self, abort, deadline):
            nonlocal round_used
            if (self.worker_id == control.target["worker_id"]
                    and control.identity.event_path("fault", 1).exists()):
                with round_gate:
                    selected = not round_used
                    round_used = True
                if selected:
                    control.pause("initial_recovery", {"worker_id": self.worker_id})
            active.inside = True
            try:
                return round_original(self, abort, deadline)
            finally:
                active.inside = False

        def lost_proof_reply(self, user_id, task_id, version):
            nonlocal proof_reply_used
            result = proof_original(self, user_id, task_id, version)
            if not getattr(active, "inside", False) or not control.matches(user_id, task_id):
                return result
            with proof_gate:
                selected = not proof_reply_used
                proof_reply_used = True
            if not selected:
                return result
            control.event("proof_original_returned", {
                "user_id": user_id, "task_id": task_id, "lease_version": version,
                "original_proof": type(result).__name__})
            from app.import_publication_evidence import AttemptKey
            control.event("proof_unknown", {"user_id": user_id, "task_id": task_id,
                                             "lease_version": version})
            raise PublicationProofUnknown(AttemptKey(user_id, task_id, version),
                                          "p6_lost_proof_reply")

        IsolatedImportWorker._recovery_round = observed_round
        ImportPublicationProofService.prove_exact = lost_proof_reply

        def observed_transient(self, claim):
            nonlocal transient_used
            if not control.matches(claim.user_id, claim.task_id):
                return transient_original(self, claim)
            with transient_gate:
                selected = not transient_used
                transient_used = True
            if not selected:
                return transient_original(self, claim)
            with self.database.transaction() as cursor:
                before = cursor.execute(
                    "select clock_timestamp() as now").fetchone()["now"]
            outcome = transient_original(self, claim)
            with self.database.transaction() as cursor:
                after = cursor.execute(
                    "select clock_timestamp() as now").fetchone()["now"]
            control.event("transient_recorded", {
                "user_id": claim.user_id, "task_id": claim.task_id,
                "queue_version": claim.queue_version,
                "recovery_version": claim.recovery_version,
                "before_clock": before.isoformat(),
                "after_clock": after.isoformat(), "outcome": outcome})
            return outcome

        PostgresImportPublicationRecoveryRepository._record_transient = observed_transient


def _install_lock_observers(scenario: str, control: ChildControl) -> None:
    """Record the backend of the original operation before it waits on a row.

    This wrapper observes the repository call and its own transaction only. It
    never takes a lock or changes the transaction's SQL or return value.
    """
    from app.postgres import PostgresDatabase
    from app.postgres_import_leases import PostgresImportLeaseRepository
    from app.import_publication_recovery import (
        PostgresImportPublicationRecoveryRepository,
    )

    active = local()
    transaction_original = PostgresDatabase.transaction

    @contextmanager
    def observed_transaction(self):
        if (scenario == "queue_capture_before_user_wait"
                and control.target.get("lock_role") == "fresh"
                and getattr(active, "operation", None) == "recovery.claim_next"
                and getattr(active, "ordinal", 0) == 1):
            control.pause("fresh_before_user_wait", {
                "operation": active.operation,
                "invocation_id": active.invocation_id,
                "ordinal": 2,
                "user_id": control.target["user_id"],
                "task_id": control.target["task_id"],
                "worker_id": control.target["worker_id"],
            })
        with transaction_original(self) as cursor:
            operation = getattr(active, "operation", None)
            if operation is not None:
                active.ordinal += 1
                control.event("lock_backend", {
                    "operation": operation,
                    "invocation_id": active.invocation_id,
                    "ordinal": active.ordinal,
                    "backend_pid": cursor.connection.info.backend_pid,
                    "user_id": control.target["user_id"],
                    "task_id": control.target["task_id"],
                })
            yield cursor

    PostgresDatabase.transaction = observed_transaction

    def observe(cls: type, name: str, predicate: Callable[..., bool],
                pause: str | None = None, after_pause: str | None = None,
                once: bool = True) -> None:
        original = getattr(cls, name)
        scope = ("lease" if cls is PostgresImportLeaseRepository
                 else "recovery")
        operation = f"{scope}.{name}"
        selection = Lock()
        used = False

        def wrapped(self, *args, **kwargs):
            nonlocal used
            if not predicate(self, *args, **kwargs):
                return original(self, *args, **kwargs)
            with selection:
                selected = not (once and used)
                if selected:
                    used = True
            if not selected:
                return original(self, *args, **kwargs)
            fields = {"operation": operation, "invocation_id": uuid4().hex,
                      "user_id": control.target["user_id"],
                      "task_id": control.target["task_id"],
                      "worker_id": control.target["worker_id"]}
            if pause is not None:
                control.pause(pause, fields)
            else:
                control.event("lock_enter", fields)
            previous = getattr(active, "operation", None)
            previous_invocation = getattr(active, "invocation_id", None)
            previous_ordinal = getattr(active, "ordinal", 0)
            active.operation = operation
            active.invocation_id = fields["invocation_id"]
            active.ordinal = 0
            try:
                result = original(self, *args, **kwargs)
            except Exception as error:
                control.event("lock_exception", fields | {
                    "exception": type(error).__name__})
                raise
            else:
                actual = getattr(result, "task", result)
                control.event("lock_returned", fields | {
                    "result_type": type(result).__name__,
                    "result_value": result if type(result) in (bool, int, str) else None,
                    "result_user_id": getattr(actual, "user_id", None),
                    "result_task_id": getattr(actual, "task_id", None),
                    "result_worker_id": getattr(result, "worker_id", None)})
                if after_pause is not None:
                    control.pause(after_pause, fields)
                return result
            finally:
                active.operation = previous
                active.invocation_id = previous_invocation
                active.ordinal = previous_ordinal

        setattr(cls, name, wrapped)

    def exact_attempt(_self, attempt, *_args, **_kwargs) -> bool:
        return (control.matches(attempt.task.user_id, attempt.task.task_id)
                and attempt.worker_id == control.target["worker_id"])

    def exact_claim(_self, claim, *_args, **_kwargs) -> bool:
        return (control.matches(claim.user_id, claim.task_id)
                and claim.worker_id == control.target["worker_id"])

    def selected_user(_self, *_args, **kwargs) -> bool:
        allowed = kwargs.get("allowed_user_ids")
        return allowed is not None and control.target["user_id"] in allowed

    def selected_claim(_self, worker_id, *_args, **kwargs) -> bool:
        return (worker_id == control.target["worker_id"]
                and selected_user(_self, *_args, **kwargs))

    if scenario == "expiry_scan_after_task_wait":
        lock_role = control.target.get("lock_role")
        if lock_role == "expiry_old":
            observe(PostgresImportLeaseRepository, "heartbeat",
                    exact_attempt, pause="heartbeat_enter")
            observe(PostgresImportLeaseRepository, "try_begin_committing",
                    exact_attempt)
        elif lock_role == "scanner":
            observe(PostgresImportLeaseRepository, "recover_expired",
                    selected_user, after_pause="scan_returned")
        elif lock_role == "fresh":
            observe(PostgresImportPublicationRecoveryRepository, "claim_next",
                    selected_claim, pause="fresh_claim_enter")
        else:
            raise ValueError("expiry_lock_role_unexpected")
    elif scenario == "heartbeat_terminal_after_wait":
        if control.target.get("lock_role") == "fresh":
            observe(PostgresImportPublicationRecoveryRepository, "claim_next",
                    selected_claim, pause="fresh_claim_enter")
        elif control.target.get("lock_role") == "heartbeat_old":
            observe(PostgresImportLeaseRepository, "heartbeat",
                    exact_attempt, pause="heartbeat_enter")
            observe(PostgresImportLeaseRepository, "try_begin_committing",
                    exact_attempt)
            observe(PostgresImportLeaseRepository, "complete",
                    exact_attempt, pause="terminal_enter")
        else:
            raise ValueError("heartbeat_lock_role_unexpected")
    elif scenario == "recovery_ack_after_wait":
        observe(PostgresImportPublicationRecoveryRepository, "ack_success",
                exact_claim, pause="ack_enter")
    elif scenario == "recovery_abandon_after_wait":
        observe(PostgresImportPublicationRecoveryRepository, "abandon_exact",
                exact_claim, pause="abandon_enter")
    elif scenario == "queue_capture_before_user_wait":
        lock_role = control.target.get("lock_role")
        if lock_role == "scanner":
            observe(PostgresImportLeaseRepository, "recover_expired",
                    selected_user, after_pause="scan_returned")
        elif lock_role == "fresh":
            observe(PostgresImportPublicationRecoveryRepository, "claim_next",
                    selected_claim, pause="fresh_claim_enter")
        elif lock_role is None:
            observe(PostgresImportLeaseRepository, "recover_expired",
                    selected_user, after_pause="ordinary_closed_existing_queue")
            observe(PostgresImportPublicationRecoveryRepository, "claim_next",
                    selected_claim, pause="claim_enter")
        else:
            raise ValueError("queue_lock_role_unexpected")
    elif scenario == "two_user_fairness":
        observe(PostgresImportPublicationRecoveryRepository, "claim_next",
                selected_claim, once=False)
    else:
        raise ValueError("unknown_lock_scenario")

    if (scenario in {"recovery_ack_after_wait",
                     "recovery_abandon_after_wait",
                     "queue_capture_before_user_wait"}
            and control.target.get("lock_role") is None):
        from app.distributed_import_worker import IsolatedImportWorker

        ordinary_original = IsolatedImportWorker._preflight
        recovery_original = IsolatedImportWorker._recovery_round
        post_refusal_gate = Lock()
        post_refusal_used = False
        entry_stage = ({"recovery_ack_after_wait": "ack_enter",
                        "recovery_abandon_after_wait": "abandon_enter",
                        "queue_capture_before_user_wait": "claim_enter"}[scenario])

        def no_new_ordinary_after_stale_recovery(self, abort, deadline):
            if (self.worker_id == control.target["worker_id"]
                    and control.identity.event_path(entry_stage, 1).exists()):
                control.event("ordinary_admission_blocked", {
                    "worker_id": self.worker_id,
                    "task_id": control.target["task_id"]})
                return None
            return ordinary_original(self, abort, deadline)

        def hold_next_recovery_after_refusal(self, abort, deadline):
            nonlocal post_refusal_used
            if (self.worker_id == control.target["worker_id"]
                    and control.identity.event_path("lock_exception", 1).exists()):
                with post_refusal_gate:
                    selected = not post_refusal_used
                    if selected:
                        post_refusal_used = True
                if selected:
                    control.pause("post_refusal_recovery", {
                        "worker_id": self.worker_id,
                        "task_id": control.target["task_id"]})
            return recovery_original(self, abort, deadline)

        IsolatedImportWorker._preflight = no_new_ordinary_after_stale_recovery
        IsolatedImportWorker._recovery_round = hold_next_recovery_after_refusal


@dataclass(frozen=True)
class RealProcessResources:
    settings: Any
    db: Any
    raw: Any
    s3: Any
    users: tuple[str, str]
    excluded: str
    names: tuple[str, str, str]
    directory: Path
    ledger: Path


@pytest.fixture
def real_process_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Own Task04's disposable resources until every role Job has exited."""
    required = ("POSTGRES_TEST_URL", "S3_TEST_ENDPOINT", "S3_TEST_ACCESS_KEY",
                "S3_TEST_SECRET_KEY", "GENERATION_QDRANT_TEST_URL")
    assert all(os.environ.get(key) for key in required), "dedicated_fixture_env_missing"
    from app.auth import hash_password
    from app.isolated_import_provider import open_isolated_providers
    from app.isolated_import_runtime import open_isolated_runtime
    from tests.integration import test_distributed_import_worker as worker_contract

    ledger = tmp_path / "resources.jsonl"
    ledger.touch(exist_ok=False)

    def receipt(token: str, kind: str, name: str, event: str) -> None:
        with ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"fixture": token, "kind": kind,
                                     "name": name, "event": event},
                                    sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    monkeypatch.setattr(worker_contract, "_resource_receipt", receipt)
    source = worker_contract.real_worker_resources.__wrapped__(monkeypatch)
    settings, db, raw, s3, users, excluded = next(source)
    _RESOURCE_OWNERS[settings.schema] = source

    def finish_after_owners_exit() -> None:
        while True:
            try:
                _retire_owned_jobs()
                assert not _owners_snapshot()
                break
            except Exception:
                time.sleep(0.2)
        try:
            source.close()
        finally:
            _RESOURCE_OWNERS.pop(settings.schema, None)

    try:
        names = tuple("p6" + label + user[:8] for label, user in
                      zip(("a", "b", "x"), (*users, excluded)))
        with db.transaction() as cursor:
            for user, name in zip((*users, excluded), names):
                cursor.execute("update users set username=%s,username_key=%s,"
                               "password_hash=%s where id=%s",
                               (name, name, hash_password("correct-password"), user))
        assert settings.rag_profile.dimension == settings.episode_profile.dimension == 384
        providers = open_isolated_providers(settings, env={
            "ISOLATED_IMPORT_RAG_EMBEDDING_PROVIDER": "simple",
            "ISOLATED_IMPORT_EPISODE_EMBEDDING_PROVIDER": "simple"})
        runtime = None
        try:
            assert providers.probe().available, "actual_provider_probe_unavailable"
            runtime = open_isolated_runtime(settings, provider_probe=providers.probe)
            assert all(runtime.ordinary_ready(user) for user in users)
        finally:
            if runtime is not None:
                runtime.close()
            assert providers.close(timeout_seconds=5), "parent_probe_close_incomplete"
        receipt(settings.schema, "process_fixture", settings.schema, "prepared")
        yield RealProcessResources(settings, db, raw, s3, users, excluded,
                                   names, tmp_path, ledger)
    finally:
        # The generator's finally drops the PG/S3/Qdrant resources. A retained
        # non-daemon closer owns its strong reference until every Job is proved
        # exited; the outer unit/real runner still bounds a stuck pytest tree.
        try:
            _retire_owned_jobs()
            assert not _owners_snapshot(), "role_owner_remains_after_teardown"
        except Exception:
            guard = Thread(target=finish_after_owners_exit,
                           name="p6-retained-resource-owner", daemon=False)
            _RESOURCE_OWNERS[settings.schema] = (source, guard)
            guard.start()
            raise
        else:
            source.close()
            _RESOURCE_OWNERS.pop(settings.schema, None)


def _real_deadline() -> float:
    return time.monotonic() + CASE_SECONDS


@contextmanager
def _scoped_import_row_lock(resources: RealProcessResources, *,
                            table: str, user_id: str, task_id: str | None = None):
    """Hold one fixture row in a parent transaction, with no clock edits."""
    assert table in {"users", "import_tasks"}
    with resources.db.transaction() as cursor:
        blocker = cursor.connection.info.backend_pid
        if table == "users":
            row = cursor.execute("select id from users where id=%s for update",
                                 (user_id,)).fetchone()
            assert row is not None and row["id"] == user_id
        else:
            assert isinstance(task_id, str) and task_id
            row = cursor.execute("select id,user_id from import_tasks "
                                 "where id=%s and user_id=%s for update",
                                 (task_id, user_id)).fetchone()
            assert row is not None and (row["id"], row["user_id"]) == (
                task_id, user_id)
        yield blocker


def _observed_lock_backend(identity: ControlIdentity, *, operation: str,
                           invocation_id: str, ordinal: int,
                           deadline: float) -> dict[str, object]:
    """Bind a method transaction to its child event and exact PG backend."""
    assert ordinal >= 1
    while True:
        matched = []
        for path in identity.directory.glob("worker-lock_backend-*.event.json"):
            record = _read_json(path)
            if (record.get("nonce") != identity.nonce
                    or record.get("case") != identity.case
                    or record.get("role") != "worker"):
                raise AssertionError("lock_backend_event_identity_mismatch")
            if (record.get("operation") == operation
                    and record.get("invocation_id") == invocation_id
                    and record.get("ordinal") == ordinal):
                matched.append(record)
        if matched:
            assert len(matched) == 1, "ambiguous_lock_backend"
            backend = matched[0].get("backend_pid")
            assert type(backend) is int and backend > 0
            return matched[0]
        time.sleep(min(CONTROL_POLL, _real_left(deadline, CONTROL_POLL)))


def _prove_pg_lock_wait(resources: RealProcessResources, identity: ControlIdentity,
                        *, operation: str, invocation_id: str, ordinal: int,
                        blocker_pid: int, child_pid: int,
                        deadline: float,
                        upstream_backend_pid: int | None = None) -> dict[str, object]:
    """Require a real Lock wait, directly or through the exact upstream waiter."""
    backend = _observed_lock_backend(identity, operation=operation,
                                     invocation_id=invocation_id,
                                     ordinal=ordinal, deadline=deadline)
    assert backend["pid"] == child_pid
    target_pid = backend["backend_pid"]
    if upstream_backend_pid is not None:
        assert (type(upstream_backend_pid) is int
                and upstream_backend_pid > 0
                and upstream_backend_pid not in (target_pid, blocker_pid))
    while True:
        with resources.db.transaction() as cursor:
            observed = cursor.execute(
                "select pid,wait_event_type,wait_event from pg_stat_activity "
                "where pid=%s", (target_pid,)).fetchone()
            blockers = cursor.execute(
                "select pg_blocking_pids(%s) as pids", (target_pid,)
            ).fetchone()["pids"]
            upstream_observed = None
            upstream_blockers = None
            if (observed is not None
                    and observed["wait_event_type"] == "Lock"
                    and blocker_pid not in blockers
                    and upstream_backend_pid is not None
                    and upstream_backend_pid in blockers):
                upstream_observed = cursor.execute(
                    "select pid,wait_event_type,wait_event from pg_stat_activity "
                    "where pid=%s", (upstream_backend_pid,)).fetchone()
                upstream_blockers = cursor.execute(
                    "select pg_blocking_pids(%s) as pids",
                    (upstream_backend_pid,)).fetchone()["pids"]
            clock = cursor.execute(
                "select clock_timestamp() as now").fetchone()["now"]
        direct = blocker_pid in blockers
        chained = (upstream_observed is not None
                   and upstream_observed["pid"] == upstream_backend_pid
                   and upstream_observed["wait_event_type"] == "Lock"
                   and blocker_pid in upstream_blockers)
        if (observed is not None and observed["pid"] == target_pid
                and observed["wait_event_type"] == "Lock"
                and (direct or chained)):
            chain = ([target_pid, blocker_pid] if direct else
                     [target_pid, upstream_backend_pid, blocker_pid])
            proof = {"case": identity.case, "role": identity.role,
                     "method": operation, "invocation_id": invocation_id,
                     "transaction_ordinal": ordinal,
                     "child_pid": child_pid, "target_backend_pid": target_pid,
                     "blocker_backend_pid": blocker_pid,
                     "direct_blocker_backend_pid": chain[1],
                     "blocking_chain": chain,
                     "chain_observation": None if direct else {
                         "upstream_backend_pid": upstream_backend_pid,
                         "wait_event_type": upstream_observed["wait_event_type"],
                         "wait_event": upstream_observed["wait_event"],
                         "blocking_pids": upstream_blockers},
                     "wait_event_type": observed["wait_event_type"],
                     "wait_event": observed["wait_event"],
                     "blocking_pids": blockers,
                     "pg_clock": clock.isoformat()}
            _atomic_json(resources.directory / (
                f"{identity.case}-{operation.replace('.', '_')}-"
                f"{invocation_id}-{ordinal}-pg-lock.json"), proof)
            return proof
        time.sleep(min(DATA_POLL, _real_left(deadline, DATA_POLL)))


@contextmanager
def _lock_case(resources: RealProcessResources, scenario: str):
    """Own the API and every role used by one lock/fairness case."""
    deadline = _real_deadline()
    owners: list[tuple[str, OwnedJob]] = []
    clients: list[httpx.Client] = []
    controls: list[ControlIdentity] = []
    release_errors: list[str] = []
    try:
        port = _fresh_port()
        api = _launch_role(resources, role="api", case=scenario, port=port)
        owners.append(("api", api))
        http, api_identity = _ready_api(api, port, deadline)
        clients.append(http)
        sessions = []
        for name in resources.names[:2]:
            login = http.post("/api/v1/auth/login", json={
                "username": name, "password": "correct-password"})
            assert login.status_code == 200
            sessions.append((login.cookies["zhiyan_session"],
                             login.json()["csrf_token"]))
        case = SimpleNamespace(
            resources=resources, scenario=scenario, deadline=deadline,
            owners=owners, clients=clients, controls=controls, http=http,
            api_pid=api_identity["pid"], sessions=tuple(sessions))
        yield case
    finally:
        stages = ("fault", "terminal_enter", "heartbeat_enter", "scan_enter",
                   "scan_returned", "ordinary_closed_existing_queue",
                    "claim_enter", "old_recovery_enter",
                  "fresh_claim_enter", "fresh_before_user_wait", "ack_enter",
                  "abandon_enter", "fairness_enter", "post_refusal_recovery",
                  "fresh_ordinary_enter")
        for identity in controls:
            release_errors.extend(_release_observed(identity, stages))
        try:
            _finish_real_case(resources, clients, owners)
        finally:
            assert not release_errors, release_errors


def _lock_upload(case: SimpleNamespace, index: int,
                 content: bytes, *, stage: str = "lock") -> dict[str, object]:
    assert index in (0, 1)
    cookie, csrf = case.sessions[index]
    upload = _post_valid_upload_once(
        case.resources, case.http, case.deadline, case=case.scenario,
        stage=stage, cookies={"zhiyan_session": cookie},
        headers={"X-CSRF-Token": csrf},
        files={"files": (f"lock-{index}.txt", content)})
    assert upload.status_code == 202 and len(upload.json()["tasks"]) == 1
    return upload.json()["tasks"][0]


def _lock_worker(case: SimpleNamespace, label: str, worker_id: str, *,
                 fault: str | None = None, user_id: str | None = None,
                 task_id: str | None = None,
                 lock_role: str | None = None) -> tuple[OwnedJob, dict[str, object]]:
    target = ({"user_id": user_id, "task_id": task_id,
               "worker_id": worker_id, "lock_role": lock_role} if fault else None)
    worker = _launch_role(case.resources, role="worker", case=fault or
                          case.scenario, worker_id=worker_id, fault=fault,
                          target=target)
    case.owners.append((label, worker))
    actual = _ready_worker(worker, case.deadline)
    assert actual["pid"] != case.api_pid
    if worker.control_identity is not None:
        case.controls.append(worker.control_identity)
    return worker, actual


def _lock_checkpoint(case: SimpleNamespace, setup_fault: str, *,
                     user_index: int = 0, retire: bool = True,
                     lock_role: str | None = None
                     ) -> tuple[dict[str, object], bytes,
                                                   OwnedJob, dict[str, object],
                                                   dict[str, object]]:
    """Create real immutable evidence, then stop only its owned original Job."""
    user = case.resources.users[user_index]
    content = (f"Exact lock setup {case.scenario} {user_index}.".encode("utf-8"))
    task = _lock_upload(case, user_index, content,
        stage=(f"fairness-{user_index}" if case.scenario == "two_user_fairness"
               else "lock"))
    task_id = str(task["task_id"])
    worker_id = "p6-lock-source-" + uuid4().hex
    label = f"lock-source-{user_index}"
    old, actual = _lock_worker(case, label, worker_id,
        fault=setup_fault, user_id=user, task_id=task_id,
        lock_role=lock_role)
    assert old.control_identity is not None
    marker = _await_event(old.control_identity, "fault",
                          timeout=_real_left(case.deadline, 60))
    assert marker["pid"] == actual["pid"]
    checkpoint = _assert_death_checkpoint(case.resources, setup_fault, user,
                                          task, content, worker_id, marker)
    if retire:
        _retire_role(case.resources, old,
                     _real_left(case.deadline, JOB_EXIT_SECONDS), label)
        case.controls.remove(old.control_identity)
    return task, content, old, actual, checkpoint


def _lock_recovery_rows(case: SimpleNamespace, user: str, task_id: str,
                        version: int, predicate: Callable[[dict[str, object]], bool]
                        ) -> dict[str, object]:
    return _await_recovery_projection(case.resources, user, task_id, version,
                                      case.deadline, predicate)


def _await_exact_recovery_expiry(case: SimpleNamespace, user: str,
                                 task_id: str, version: int,
                                 expected: dict[str, object]
                                 ) -> dict[str, object]:
    expiry_deadline = min(case.deadline, time.monotonic() + 90)
    old_queue_expiry = expected["queue"]["claim_expires_at"]
    old_recovery_expiry = expected["recovery"]["expires_at"]
    assert old_queue_expiry == old_recovery_expiry
    while True:
        rows = _death_rows(case.resources, user, task_id,
                           evidence_version=version)
        queue, recovery = rows["queue"], rows["recovery"]
        assert (queue == expected["queue"]
                and recovery == expected["recovery"]
                and rows["evidence"] == expected["evidence"]
                and rows["gate"] == expected["gate"]
                and rows["issuance"] == expected["issuance"])
        if (old_queue_expiry <= rows["clock"]
                and old_recovery_expiry <= rows["clock"]):
            return rows
        time.sleep(min(DATA_POLL, _real_left(expiry_deadline, DATA_POLL)))


def _await_queue_capture_expiry(case: SimpleNamespace, user: str,
                                task_id: str, version: int,
                                expected: dict[str, object]
                                ) -> dict[str, object]:
    expiry = expected["queue"]["claim_expires_at"]
    assert expiry > expected["clock"]
    expiry_deadline = min(case.deadline, time.monotonic() + 90)
    while True:
        rows = _death_rows(case.resources, user, task_id,
                           evidence_version=version)
        assert (_lock_stable_rows(rows) == _lock_stable_rows(expected)
                and rows["recovery"] is None)
        if rows["clock"] >= expiry:
            return rows
        time.sleep(min(DATA_POLL, _real_left(expiry_deadline, DATA_POLL)))


def _assert_lock_excluded(case: SimpleNamespace, before: tuple,
                          objects_before: str) -> None:
    assert (_business_projection(case.resources.db,
                                 case.resources.excluded) == before
            and _object_projection(case.resources.s3,
                                   case.resources.settings.s3_bucket,
                                   case.resources.excluded) == objects_before)


def _lock_stable_rows(rows: dict[str, object]) -> dict[str, object]:
    """Compare the exact persisted projection without its fresh read clock."""
    stable = {key: value for key, value in rows.items() if key != "clock"}
    stable["generations"] = sorted(
        stable["generations"],
        key=lambda row: (row["vector_kind"], str(row["generation_id"])))
    return stable


def _real_left(deadline: float, cap: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("real_case_deadline")
    return min(left, cap)


_VALID_UPLOAD_STAGES = frozenset({
    ("two_api", "text"), ("two_api", "pdf"), ("api_death", "survivor"),
    *((scenario, "death") for scenario in _DEATH_BOUNDARIES),
    ("stale_authority", "survivor"), ("late_candidate_bytes", "survivor"),
    *((scenario, "recovery") for scenario in (
        "unknown_document_put", "unknown_terminal_reply",
        "pause_live_capture", "pause_expired_capture",
        "recovery_natural_retry")),
    ("recovery_ack_after_wait", "lock"),
    ("recovery_abandon_after_wait", "lock"),
    ("queue_capture_before_user_wait", "lock"),
    ("expiry_scan_after_task_wait", "lock"),
    ("heartbeat_terminal_after_wait", "lock"),
    ("two_user_fairness", "fairness-0"),
    ("two_user_fairness", "fairness-1"),
    ("two_user_fairness", "fairness-a-queued"),
})
_VALID_UPLOAD_CLOSE_SECONDS = 5.0
_VALID_DOCUMENT_READS = frozenset({
    ("two_api", "text-own"), ("two_api", "text-foreign"),
    ("two_api", "pdf-own"), ("two_api", "pdf-foreign"),
    ("api_death", "survivor-own"), ("api_death", "survivor-foreign"),
})


def _safe_upload_error(error: Exception) -> str:
    for known, label in ((TimeoutError, "TimeoutError"),
                         (httpx.ReadTimeout, "httpx.ReadTimeout"),
                         (httpx.ConnectTimeout, "httpx.ConnectTimeout"),
                         (httpx.WriteTimeout, "httpx.WriteTimeout"),
                         (httpx.PoolTimeout, "httpx.PoolTimeout")):
        if type(error) is known:
            return label
    return "Exception"


def _post_valid_upload_once(resources: RealProcessResources,
                            reference: httpx.Client, deadline: float, *,
                            case: str, stage: str, cookies: dict[str, str],
                            headers: dict[str, str],
                            files: dict[str, tuple[str, bytes]]) -> httpx.Response:
    """Bound one valid upload by wall time, recording only safe diagnostics."""
    if (case, stage) not in _VALID_UPLOAD_STAGES:
        raise ValueError("unsupported_valid_upload_stage")
    operation_deadline = time.monotonic() + _real_left(deadline, 45.0)
    diagnostic: dict[str, object] = {"case": case, "stage": stage}
    started = time.monotonic()

    async def send() -> httpx.Response:
        if time.monotonic() >= operation_deadline:
            raise TimeoutError("valid_upload_deadline")
        client = httpx.AsyncClient(base_url=reference.base_url, timeout=None)
        try:
            async with asyncio.timeout_at(operation_deadline):
                if time.monotonic() >= operation_deadline:
                    raise TimeoutError("valid_upload_deadline")
                return await client.post("/api/v1/imports", cookies=cookies,
                                         headers=headers, files=files)
        finally:
            async with asyncio.timeout(_VALID_UPLOAD_CLOSE_SECONDS):
                await client.aclose()

    try:
        response = asyncio.run(send())
        if time.monotonic() >= operation_deadline:
            raise TimeoutError("valid_upload_deadline")
        diagnostic["status_code"] = response.status_code
        return response
    except Exception as error:
        diagnostic["exception_class"] = _safe_upload_error(error)
        raise
    finally:
        diagnostic["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
        path = resources.directory / f"{case}-{stage}-upload-diagnostic.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(diagnostic, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())


def _get_document_once(resources: RealProcessResources, reference: httpx.Client,
                       deadline: float, *, case: str, stage: str,
                       document_id: str, cookie: str) -> httpx.Response:
    """Read one document within the case deadline; retain only safe diagnostics."""
    if (case, stage) not in _VALID_DOCUMENT_READS:
        raise ValueError("unsupported_document_read_stage")
    document = str(UUID(document_id))
    operation_deadline = time.monotonic() + _real_left(deadline, 45.0)
    if time.monotonic() >= operation_deadline:
        raise TimeoutError("document_read_deadline")
    diagnostic: dict[str, object] = {"case": case, "stage": stage}
    started = time.monotonic()

    async def send() -> httpx.Response:
        if time.monotonic() >= operation_deadline:
            raise TimeoutError("document_read_deadline")
        client = httpx.AsyncClient(base_url=reference.base_url, timeout=None)
        try:
            async with asyncio.timeout_at(operation_deadline):
                if time.monotonic() >= operation_deadline:
                    raise TimeoutError("document_read_deadline")
                return await client.get(f"/api/v1/documents/{document}",
                                        cookies={"zhiyan_session": cookie})
        finally:
            async with asyncio.timeout(_VALID_UPLOAD_CLOSE_SECONDS):
                await client.aclose()

    try:
        response = asyncio.run(send())
        if time.monotonic() >= operation_deadline:
            raise TimeoutError("document_read_deadline")
        diagnostic["status_code"] = response.status_code
        return response
    except Exception as error:
        diagnostic["exception_class"] = _safe_upload_error(error)
        raise
    finally:
        diagnostic["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
        path = resources.directory / f"{case}-{stage}-document-diagnostic.json"
        with path.open("x", encoding="utf-8") as stream:
            json.dump(diagnostic, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())


def _read_object(s3: Any, ref: dict[str, object] | tuple) -> bytes:
    if isinstance(ref, tuple):
        bucket, key, version = ref[2:5]
    else:
        bucket, key, version = ref["bucket"], ref["object_key"], ref["version_id"]
    body = s3.get_object(Bucket=bucket, Key=key, VersionId=version)["Body"]
    try:
        return body.read()
    finally:
        body.close()


def _business_projection(db: Any, user: str) -> dict[str, object]:
    """Hash every user-owned business row, including source and publication rows."""
    from psycopg import sql

    excluded = {"users", "sessions", "auth_sessions"}
    with db.transaction() as cursor:
        columns = cursor.execute("select table_name,column_name from "
            "information_schema.columns where table_schema=current_schema() "
            "and column_name in ('user_id','tenant_id') order by table_name").fetchall()
        tables: dict[str, str] = {}
        for row in columns:
            table = row["table_name"]
            if table not in tables or row["column_name"] == "user_id":
                tables[table] = row["column_name"]
        result = {}
        for table, owner_column in sorted(tables.items()):
            if table in excluded:
                continue
            rows = cursor.execute(sql.SQL("select to_jsonb(t) as row from {} t "
                "where {}=%s").format(sql.Identifier(table),
                                       sql.Identifier(owner_column)), (user,)).fetchall()
            encoded = sorted(json.dumps(item["row"], sort_keys=True,
                                        default=str, separators=(",", ":")) for item in rows)
            result[table] = {"count": len(encoded),
                             "sha256": hashlib.sha256(json.dumps(encoded).encode()).hexdigest()}
    return result


def _object_projection(s3: Any, bucket: str, user: str) -> str:
    entries = []
    for page in s3.get_paginator("list_object_versions").paginate(
            Bucket=bucket, Prefix=f"users/{user}/"):
        for kind in ("Versions", "DeleteMarkers"):
            entries.extend((kind, item["Key"], item["VersionId"],
                            item.get("Size"), item.get("ETag"))
                           for item in page.get(kind, []))
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()


def _physical_generation_manifest(scope: Any, generation_id: str,
                                  points: list[Any]) -> tuple[int, str]:
    from hello_agents.memory.storage.generation_vector_store import (
        _canonical, _decode_record,
    )

    rows = []
    for point in points:
        logical_id, public = _decode_record(
            scope, UUID(generation_id), str(point.id), point.payload, point.id)
        declared = point.payload.get("_gv_vector_digest")
        assert (isinstance(declared, str)
                and re.fullmatch(r"[0-9a-f]{64}", declared)
                and isinstance(point.vector, list)
                and len(point.vector) == scope.identity.profile.dimension)
        rows.append([logical_id, public, declared])
    assert len({row[0] for row in rows}) == len(rows)
    digest = hashlib.sha256(_canonical(sorted(
        rows, key=lambda row: row[0])).encode()).hexdigest()
    return len(rows), digest


def _assert_raw_published(raw: Any, scope: Any, receipt: tuple,
                          visible: list[Any], allowed_users: frozenset[str],
                          revoked_candidate: tuple[str, tuple[int, str] | None] | None = None
                          ) -> tuple[list[Any], list[Any]]:
    """Inspect the whole fresh physical collection, without a generation filter."""
    points: list[Any] = []
    offset = None
    seen_offsets = set()
    for _ in range(4):
        batch, next_offset = raw.client.scroll(
            collection_name=scope.identity.physical_collection, limit=128,
            offset=offset, with_payload=True, with_vectors=True)
        points.extend(batch)
        assert len(points) <= 512, "unexpected_physical_corpus"
        if next_offset is None:
            break
        assert str(next_offset) not in seen_offsets, "repeated_physical_cursor"
        seen_offsets.add(str(next_offset))
        offset = next_offset
    else:
        raise AssertionError("physical_corpus_exceeds_bound")
    assert all(point.payload["_gv_tenant"] in allowed_users
               and point.payload["_gv_kind"] == scope.vector_kind
               and point.payload["_gv_index"] == scope.index_key
               and point.payload["_gv_namespace"] ==
                   (f"pdf_{point.payload['_gv_tenant']}" if scope.vector_kind == "rag"
                    else "episodes") for point in points)
    owned = [point for point in points
             if point.payload["_gv_tenant"] == scope.tenant_id]
    fresh = [point for point in owned
             if point.payload["_gv_generation"] == receipt[0]]
    old: list[Any] = []
    assert len(fresh) == len(visible) == receipt[9]
    assert {point.payload["_gv_logical_id"] for point in fresh} == {
        point.id for point in visible}
    assert all(point.payload["_gv_tenant"] == scope.tenant_id
               and point.payload["_gv_kind"] == scope.vector_kind
               and point.payload["_gv_namespace"] == scope.namespace
               and point.payload["_gv_index"] == scope.index_key
               and point.payload["_gv_generation"] == receipt[0]
               and len(point.vector) == scope.identity.profile.dimension
                for point in fresh)
    if revoked_candidate is None:
        assert len(owned) == len(fresh), "unexpected_physical_generation"
    else:
        old_id, old_manifest = revoked_candidate
        assert old_id != receipt[0]
        old = [point for point in owned
               if point.payload["_gv_generation"] == old_id]
        assert len(owned) == len(fresh) + len(old), "unexpected_physical_generation"
        assert len({str(point.id) for point in owned}) == len(owned)
        observed = _physical_generation_manifest(scope, old_id, old)
        if old_manifest is None:
            assert not old, "unsealed_candidate_has_physical_points"
        elif old:
            assert observed == old_manifest
    return fresh, old


def _role_env(resources: RealProcessResources, *, worker_id: str | None = None
              ) -> dict[str, str]:
    settings = resources.settings
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP",
               "COMSPEC", "APPDATA", "LOCALAPPDATA", "USERPROFILE"}
    env = {key: value for key, value in os.environ.items()
           if key.upper() in allowed}
    env.update({
        "PYTHON_DOTENV_DISABLED": "1", "ISOLATED_IMPORT_ENABLED": "1",
        "ISOLATED_IMPORT_DATABASE_URL": settings.database_url,
        "ISOLATED_IMPORT_SCHEMA": settings.schema,
        "ISOLATED_IMPORT_S3_ENDPOINT_URL": settings.s3_endpoint_url,
        "ISOLATED_IMPORT_S3_BUCKET": settings.s3_bucket,
        "ISOLATED_IMPORT_QDRANT_URL": settings.qdrant_url,
        "ISOLATED_IMPORT_RAG_COLLECTION": settings.rag_collection,
        "ISOLATED_IMPORT_EPISODE_COLLECTION": settings.episode_collection,
        "ISOLATED_IMPORT_RAG_PROFILE_JSON": json.dumps(asdict(settings.rag_profile)),
        "ISOLATED_IMPORT_EPISODE_PROFILE_JSON": json.dumps(asdict(settings.episode_profile)),
        "ISOLATED_IMPORT_TRUSTED_USER_IDS": ",".join(resources.users),
        "ISOLATED_IMPORT_RAG_EMBEDDING_PROVIDER": "simple",
        "ISOLATED_IMPORT_EPISODE_EMBEDDING_PROVIDER": "simple",
        "ISOLATED_IMPORT_LEASE_SECONDS": "12",
        "ISOLATED_IMPORT_HEARTBEAT_SECONDS": "2",
        "ISOLATED_IMPORT_ATTEMPT_SECONDS": "45",
        "ISOLATED_IMPORT_PROVIDER_TIMEOUT_SECONDS": "20",
        "ISOLATED_IMPORT_STOP_WAIT_SECONDS": "5",
        "ISOLATED_IMPORT_PROVIDER_MAX_RETRIES": "0",
        "S3_REGION": settings.s3_region,
        "S3_ACCESS_KEY_ID": os.environ["S3_TEST_ACCESS_KEY"],
        "S3_SECRET_ACCESS_KEY": os.environ["S3_TEST_SECRET_KEY"],
    })
    if worker_id is not None:
        env["ISOLATED_IMPORT_WORKER_ID"] = worker_id
    return env


def _fresh_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _powershell_record(command: str, deadline: float, *,
                       cap_seconds: float | None = 10) -> dict[str, object]:
    shell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    timeout = _real_left(deadline, float("inf") if cap_seconds is None else cap_seconds)
    output = subprocess.check_output(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=REPO, text=True, timeout=timeout)
    _real_left(deadline, float("inf"))
    record = json.loads(output)
    if type(record) is not dict:
        raise AssertionError("restricted_os_record_missing")
    return record


class _RuntimeIdentityPending(RuntimeError):
    """The owned launcher is alive but its Python runtime is not observed yet."""


def _runtime_identity(owner: OwnedJob, *, deadline: float,
                      port: int | None = None
                      ) -> dict[str, object]:
    members = owner.observe()
    assert owner.launcher_pid in members and owner.launcher_identity is not None
    candidates = []
    for pid in members:
        retained = owner.runtime.get(pid)
        if retained is None:
            continue
        _handle, image, created = retained
        if _path_identity(image) != _path_identity(sys._base_executable):
            continue
        command = (f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={pid}"; '
                   'if($null -eq $p){throw "process_missing"}; '
                   '[pscustomobject]@{pid=[int]$p.ProcessId; '
                   'parent=[int]$p.ParentProcessId; image=[string]$p.ExecutablePath; '
                   'created=$p.CreationDate.ToUniversalTime().ToString("o")} '
                   '| ConvertTo-Json -Compress')
        os_record = _powershell_record(command, deadline)
        stamp = datetime.fromisoformat(str(os_record["created"])).timestamp()
        retained_stamp = (created - 116444736000000000) / 10000000
        assert (int(os_record["pid"]) == pid
                and int(os_record["parent"]) == owner.launcher_pid
                and _path_identity(str(os_record["image"])) == _path_identity(image)
                and abs(stamp - retained_stamp) < 1), "runtime_os_identity_mismatch"
        candidates.append({"pid": pid, "parent": int(os_record["parent"]),
                           "image": image, "created_filetime": created})
    if not candidates:
        raise _RuntimeIdentityPending("runtime_identity_pending")
    assert len(candidates) == 1, "runtime_pid_not_unique"
    actual = candidates[0]
    if port is not None:
        assert 1 <= port <= 65535
        listening = _powershell_record(
            f'$c=Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 '
            f'-LocalPort {port} -ErrorAction Stop; '
            '[pscustomobject]@{port=[int]$c.LocalPort; '
            'pid=[int]$c.OwningProcess} | ConvertTo-Json -Compress',
            deadline, cap_seconds=None)
        assert (int(listening["port"]) == port
                and int(listening["pid"]) == actual["pid"]), "api_port_pid_mismatch"
    return actual


def _launch_role(resources: RealProcessResources, *, role: str, case: str,
                 port: int | None = None, worker_id: str | None = None,
                 fault: str | None = None,
                 target: dict[str, str] | None = None) -> OwnedJob:
    nonce = uuid4().hex
    assert role in {"api", "worker"}
    control_identity = None
    if role == "api":
        assert port is not None and worker_id is None and fault is None
        argv = [str(PYTHON), "-m", "uvicorn",
                "api.isolated_import_app:create_isolated_import_app", "--factory",
                "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    else:
        assert port is None and worker_id is not None
        if fault is None:
            assert target is None
            argv = [str(PYTHON), "-m", "scripts.run_isolated_import_worker"]
        else:
            assert fault == case and fault in _SAFE_SCENARIOS
            assert target is not None and target.get("worker_id") == worker_id
            assert all(isinstance(target.get(key), str) and target[key]
                       for key in ("user_id", "task_id", "worker_id"))
            directory = resources.directory / f"p6-{fault}-{nonce}"
            directory.mkdir(mode=0o700)
            control_identity = ControlIdentity(nonce, fault, "worker", directory)
            _atomic_json(directory / "worker.manifest.json",
                {"nonce": nonce, "case": fault, "role": "worker", **target})
            argv = [str(PYTHON), "-m", MODULE, "--worker-child", fault,
                    str(directory), nonce, fault]
    settings = resources.settings
    identities = {
        "database_url_sha256": hashlib.sha256(settings.database_url.encode()).hexdigest(),
        "s3_endpoint_sha256": hashlib.sha256(settings.s3_endpoint_url.encode()).hexdigest(),
        "qdrant_endpoint_sha256": hashlib.sha256(settings.qdrant_url.encode()).hexdigest(),
        "rag_profile_sha256": hashlib.sha256(json.dumps(asdict(settings.rag_profile),
            sort_keys=True).encode()).hexdigest(),
        "episode_profile_sha256": hashlib.sha256(json.dumps(asdict(settings.episode_profile),
            sort_keys=True).encode()).hexdigest(),
        "rag_index": settings.rag_collection,
        "episode_index": settings.episode_collection,
        "schema": settings.schema, "bucket": settings.s3_bucket,
    }
    source_paths = ("api/isolated_import_app.py", "scripts/run_isolated_import_worker.py",
                    "app/isolated_import_runtime.py", "app/distributed_import_worker.py")
    source_hashes = {path: hashlib.sha256((REPO / path).read_bytes()).hexdigest()
                     for path in source_paths}
    manifest = resources.directory / f"{case}-{role}-{nonce}.manifest.json"
    _atomic_json(manifest, {"case": case, "expected_role": role, "nonce": nonce,
                            "port": port, "worker_id": worker_id,
                            "identities": identities, "source_sha256": source_hashes})
    owner = OwnedJob(argv, _role_env(resources, worker_id=worker_id), role=role)
    owner.manifest_path = manifest
    owner.manifest_nonce = nonce
    owner.control_identity = control_identity
    return owner


def _startup_exit_diagnostic(owner: OwnedJob) -> dict[str, object]:
    """Classify retained stderr bytes without trusting or exposing their text."""
    _stdout, stderr, truncated = owner.output()
    marker = ("launcher_unable_to_create_process_observed"
              if b"unable to create process using" in stderr.lower()
              else "no_launcher_marker_observed")
    return {"stderr_marker": marker, "stderr_retained_bytes": len(stderr),
            "stderr_truncated": truncated[1],
            "stderr_sha256": hashlib.sha256(stderr).hexdigest()}


def _ready_api(owner: OwnedJob, port: int, deadline: float) -> tuple[httpx.Client, dict[str, object]]:
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5)
    startup_deadline = min(deadline, time.monotonic() + 40)
    try:
        while True:
            if not owner.active_count():
                owner.startup_exit_diagnostic = _startup_exit_diagnostic(owner)
                raise AssertionError("api_exited_before_ready")
            try:
                result = client.get("/healthz")
                if result.status_code == 200 and result.json() == {"status": "ok"}:
                    identity = _runtime_identity(owner, deadline=startup_deadline, port=port)
                    _bind_role_identity(owner, identity)
                    return client, identity
            except (httpx.TransportError, ValueError, _RuntimeIdentityPending):
                pass
            time.sleep(min(0.1, _real_left(startup_deadline, 0.1)))
    except BaseException:
        client.close()
        raise


def _ready_worker(owner: OwnedJob, deadline: float) -> dict[str, object]:
    startup_deadline = min(deadline, time.monotonic() + 40)
    while True:
        if not owner.active_count():
            owner.startup_exit_diagnostic = _startup_exit_diagnostic(owner)
            raise AssertionError("worker_exited_before_identity")
        try:
            identity = _runtime_identity(owner, deadline=startup_deadline)
            _bind_role_identity(owner, identity)
            return identity
        except _RuntimeIdentityPending:
            pass
        time.sleep(min(0.1, _real_left(startup_deadline, 0.1)))


def _wait_task(client: httpx.Client, task_id: str, cookie: str,
               deadline: float) -> dict[str, object]:
    completion_deadline = min(deadline, time.monotonic() + 90)
    while True:
        response = client.get(f"/api/v1/imports/tasks/{task_id}",
                              cookies={"zhiyan_session": cookie})
        assert response.status_code == 200, response.status_code
        task = response.json()
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            assert task["status"] == "succeeded", task
            return task
        time.sleep(min(0.2, _real_left(completion_deadline, 0.2)))


def _bind_role_identity(owner: OwnedJob, identity: dict[str, object]) -> None:
    manifest = _read_json(owner.manifest_path)
    assert (manifest["nonce"] == owner.manifest_nonce
            and manifest["expected_role"] == owner.role)
    manifest["actual_os_identity"] = identity
    manifest["launcher_pid"] = owner.launcher_pid
    manifest["launcher_identity"] = owner.launcher_identity
    manifest["job_members"] = sorted(owner.observe())
    _atomic_json(owner.manifest_path, manifest)


def _retire_role(resources: RealProcessResources, owner: OwnedJob,
                 timeout: float, label: str) -> None:
    if owner._closed:
        return
    exits = owner.terminate(code=3, timeout=timeout)
    assert owner.active_count() == 0 and exits == owner.exit_codes
    stdout, stderr, truncated = owner.output()
    assert not any(truncated), "role_output_truncated"
    _atomic_json(resources.directory / f"{label}-exit.json",
                 {"role": owner.role, "launcher_pid": owner.launcher_pid,
                  "launcher_exit": owner.launcher_exit,
                  "runtime_identity": owner.observed_runtime,
                  "exit_codes": exits, "job_active": owner.active_count(),
                  "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                  "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                  "startup_exit_diagnostic": owner.startup_exit_diagnostic})
    owner.close(force=True)
    assert owner._closed


def _retire_roles(resources: RealProcessResources,
                  owners: list[tuple[str, OwnedJob]]) -> None:
    cleanup_deadline = time.monotonic() + 60
    errors: list[Exception] = []
    for label, owner in reversed(owners):
        if owner._closed:
            continue
        try:
            remaining = max(0.1, cleanup_deadline - time.monotonic())
            _retire_role(resources, owner, min(JOB_EXIT_SECONDS, remaining), label)
        except Exception as error:
            errors.append(error)
    if errors:
        raise ExceptionGroup("role_teardown_incomplete", errors)


def _finish_real_case(resources: RealProcessResources,
                      clients: list[httpx.Client],
                      owners: list[tuple[str, OwnedJob]]) -> None:
    errors: list[Exception] = []
    for client in clients:
        try:
            client.close()
        except Exception as error:
            errors.append(error)
    try:
        _retire_roles(resources, owners)
    except Exception as error:
        errors.append(error)
    if errors:
        raise ExceptionGroup("real_case_cleanup_incomplete", errors)


def _assert_published(resources: RealProcessResources, user: str, task: dict[str, object],
                      expected: bytes, expected_text: str, worker_id: str,
                      allowed_users: frozenset[str], *,
                      revoked_candidates: dict[str, tuple[str, tuple[int, str] | None]] | None = None
                      ) -> None:
    from app.import_publication_proof import ImportPublicationProofService
    from app.postgres_snapshots import PostgresSnapshotRepository
    from app.postgres_vector_generations import PostgresVectorGenerationAuthority, VectorScope
    from app.vector_generation_service import VectorGenerationService
    from hello_agents.memory.rag.index_identity import IndexIdentity

    task_id = str(task["task_id"])
    with resources.db.transaction() as cursor:
        attempt = cursor.execute("select lease_version,worker_id from "
                                 "import_task_attempts where task_id=%s "
                                 "order by lease_version desc limit 1",
                                 (task_id,)).fetchone()
        source = cursor.execute("select bucket,object_key,version_id,sha256,"
                                "size_bytes from import_objects where task_id=%s",
                                (task_id,)).fetchone()
    assert attempt is not None and attempt["worker_id"] == worker_id
    assert source is not None and source["version_id"]
    assert source["bucket"] == resources.settings.s3_bucket
    assert source["size_bytes"] == len(expected)
    actual = _read_object(resources.s3, source)
    assert actual == expected and hashlib.sha256(actual).hexdigest() == source["sha256"]
    proof = ImportPublicationProofService(resources.db).prove_exact(
        user, task_id, attempt["lease_version"])
    assert proof is not None and proof.proof.phase in {
        "terminal_committed", "proved_succeeded"}
    assert proof.pair.import_task.status == "succeeded"
    assert len(proof.proof.rag_receipt) == len(proof.proof.episode_receipt) == 20
    assert proof.document_ref[:2] == (user, task["document_id"])
    assert proof.document_ref[2] == resources.settings.s3_bucket
    published_bytes = _read_object(resources.s3, proof.document_ref)
    assert (published_bytes == expected
            and hashlib.sha256(published_bytes).hexdigest() == proof.document_ref[5]
            and len(published_bytes) == proof.document_ref[6])
    authority = PostgresVectorGenerationAuthority(resources.db)
    rag_scope = VectorScope(user, "rag", f"pdf_{user}", IndexIdentity(
        "qdrant", resources.settings.rag_collection, resources.settings.rag_profile))
    episode_scope = VectorScope(user, "episode", "episodes", IndexIdentity(
        "qdrant", resources.settings.episode_collection,
        resources.settings.episode_profile))
    with resources.db.transaction() as cursor:
        cursor.execute("set transaction isolation level repeatable read read only")
        witness = cursor.execute("select last_generation_id,index_revision,"
            "publication_snapshot_version,document_count,documents_sha256 "
            "from history_document_witnesses where tenant_id=%s and vector_kind='rag' "
            "and namespace=%s and index_key=%s and head_revision=%s",
            (user, rag_scope.namespace, rag_scope.index_key, proof.witness[0])).fetchone()
        pinned = cursor.execute("select bucket,object_key,version_id,sha256,size_bytes "
            "from document_objects where user_id=%s and document_id=%s",
            (user, task["document_id"])).fetchone()
        evidence = cursor.execute("select e.phase,e.phase_version,p.payload_phase_version,"
            "e.intent_hash from import_publication_evidence e join "
            "import_publication_private_payloads p using(user_id,task_id,task_lease_version) "
            "where e.user_id=%s and e.task_id=%s and e.task_lease_version=%s",
            (user, task_id, attempt["lease_version"])).fetchone()
        gate = cursor.execute("select status from user_publication_gates where "
            "user_id=%s and task_id=%s and task_lease_version=%s",
            (user, task_id, attempt["lease_version"])).fetchone()
        queue = cursor.execute("select state from import_publication_recovery_queue "
            "where user_id=%s and task_id=%s and task_lease_version=%s",
            (user, task_id, attempt["lease_version"])).fetchone()
        reservations = cursor.execute("select vector_kind,generation_id,state "
            "from generation_reservations where user_id=%s and task_id=%s "
            "and task_lease_version=%s order by vector_kind",
            (user, task_id, attempt["lease_version"])).fetchall()
    assert witness is not None
    assert (str(witness["last_generation_id"]) == proof.proof.rag_receipt[0]
            and witness["index_revision"] == proof.proof.rag_receipt[6]
            and witness["publication_snapshot_version"] == proof.history_version
            and witness["document_count"] == proof.witness[2]
            and witness["documents_sha256"] == proof.witness[3])
    assert pinned is not None and proof.document_ref == (
        user, task["document_id"], pinned["bucket"], pinned["object_key"],
        pinned["version_id"], pinned["sha256"], pinned["size_bytes"])
    assert evidence is not None
    assert evidence["phase"] in {"terminal_committed", "proved_succeeded"}
    assert evidence["phase_version"] == evidence["payload_phase_version"]
    assert evidence["phase_version"] >= proof.proof.phase_version
    assert evidence["intent_hash"] == proof.proof.intent_hash
    assert gate is not None and queue is not None
    if evidence["phase"] == "proved_succeeded":
        assert gate["status"] == queue["state"] == "resolved"
    else:
        assert gate["status"] == "unresolved"
        assert queue["state"] in {"pending", "claimed"}
    assert {(row["vector_kind"], str(row["generation_id"]), row["state"])
            for row in reservations} == {
        ("rag", proof.proof.rag_receipt[0], "reserved"),
        ("episode", proof.proof.episode_receipt[0], "reserved")}
    snapshots = PostgresSnapshotRepository(resources.db)
    history = snapshots.read(user, "history")
    memory = snapshots.read(user, "memory")
    assert history is not None and history.version == proof.history_version
    assert memory is not None and memory.version == proof.memory_version
    assert len(history.data["documents"]) == 1
    assert history.data["documents"][0]["document_id"] == task["document_id"]
    assert len(memory.data["memories"]) == 1
    assert memory.data["memories"][0]["id"] == proof.event_id
    vectors = VectorGenerationService(authority, resources.raw)
    for scope, receipt in ((rag_scope, proof.proof.rag_receipt),
                           (episode_scope, proof.proof.episode_receipt)):
        head = authority.read_head(scope)
        assert (str(head.generation_id), head.revision, head.index_revision,
                head.snapshot_version) == (receipt[0], receipt[7], receipt[6], receipt[8])
    rag_points = vectors.read_view(rag_scope).scroll(
        rag_scope.identity.physical_collection, with_vectors=True,
        expected_manifest=(proof.proof.rag_receipt[9], proof.proof.rag_receipt[10]))
    episode_points = vectors.read_view(episode_scope).scroll(
        episode_scope.identity.physical_collection, with_vectors=True,
        expected_manifest=(proof.proof.episode_receipt[9],
                           proof.proof.episode_receipt[10]))
    assert rag_points and all(
        point.payload["document_id"] == task["document_id"]
        and point.payload["rag_namespace"] == f"pdf_{user}"
        and len(point.vector) == 384 for point in rag_points)
    assert len(rag_points) == 1
    assert re.sub(r"\s+", " ", rag_points[0].payload["content"]).strip() == (
        re.sub(r"\s+", " ", expected_text).strip())
    owned_events = [point for point in episode_points
                    if point.payload.get("import_task_id") == task_id]
    assert len(owned_events) == 1 and owned_events[0].id == proof.event_id
    assert owned_events[0].payload["user_id"] == user
    assert len(owned_events[0].vector) == 384
    physical_rag, revoked_rag = _assert_raw_published(resources.raw, rag_scope,
                                          proof.proof.rag_receipt, rag_points,
                                          allowed_users,
                                          None if revoked_candidates is None else
                                          revoked_candidates["rag"])
    physical_episode, revoked_episode = _assert_raw_published(resources.raw, episode_scope,
                                              proof.proof.episode_receipt,
                                              episode_points, allowed_users,
                                              None if revoked_candidates is None else
                                              revoked_candidates["episode"])
    from app.isolated_import_provider import open_isolated_providers
    from hello_agents.memory.storage.generation_vector_store import _same_cosine_vector

    providers = open_isolated_providers(resources.settings, env={
        "ISOLATED_IMPORT_RAG_EMBEDDING_PROVIDER": "simple",
        "ISOLATED_IMPORT_EPISODE_EMBEDDING_PROVIDER": "simple"})
    try:
        for point in (*physical_rag, *revoked_rag):
            expected_vector = providers.rag_runtime.embed_query(point.payload["content"])
            assert _same_cosine_vector(point.vector, expected_vector)
        for point in (*physical_episode, *revoked_episode):
            expected_vector = providers.episode_runtime.embed_query(point.payload["content"])
            assert _same_cosine_vector(point.vector, expected_vector)
    finally:
        assert providers.close(timeout_seconds=5), "verification_provider_close_failed"


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_two_api_direct_worker_text_pdf_isolation(
        real_process_resources: RealProcessResources) -> None:
    from tests.integration.test_distributed_import_worker import _text_pdf

    resources = real_process_resources
    deadline = _real_deadline()
    first_port, second_port = _fresh_port(), _fresh_port()
    assert first_port != second_port
    worker_id = "p6-" + uuid4().hex
    owners: list[tuple[str, OwnedJob]] = []
    clients: list[httpx.Client] = []
    excluded_before = _business_projection(resources.db, resources.excluded)
    excluded_objects_before = _object_projection(
        resources.s3, resources.settings.s3_bucket, resources.excluded)
    try:
        first = _launch_role(resources, role="api", case="two_api", port=first_port)
        owners.append(("api-first", first))
        first_http, first_identity = _ready_api(first, first_port, deadline)
        clients.append(first_http)
        second = _launch_role(resources, role="api", case="two_api", port=second_port)
        owners.append(("api-second", second))
        second_http, second_identity = _ready_api(second, second_port, deadline)
        clients.append(second_http)
        worker = _launch_role(resources, role="worker", case="two_api",
                              worker_id=worker_id)
        owners.append(("worker", worker))
        worker_identity = _ready_worker(worker, deadline)
        assert len({first_identity["pid"], second_identity["pid"],
                    worker_identity["pid"]}) == 3

        login = first_http.post("/api/v1/auth/login", json={
            "username": resources.names[0], "password": "correct-password"})
        assert login.status_code == 200
        cookie = login.cookies["zhiyan_session"]
        csrf = login.json()["csrf_token"]
        cross = second_http.get("/api/v1/auth/session",
                                cookies={"zhiyan_session": cookie})
        assert cross.status_code == 200 and cross.json()["csrf_token"] == csrf
        before = _business_projection(resources.db, resources.users[0])
        objects_before = _object_projection(
            resources.s3, resources.settings.s3_bucket, resources.users[0])
        forbidden = second_http.post("/api/v1/imports",
            cookies={"zhiyan_session": cookie},
            files={"files": ("forbidden.txt", b"no write")})
        assert forbidden.status_code == 403
        assert _business_projection(resources.db, resources.users[0]) == before
        assert _object_projection(resources.s3, resources.settings.s3_bucket,
                                  resources.users[0]) == objects_before
        text_bytes = b"A complete isolated document for the first tenant."
        text_upload = _post_valid_upload_once(resources, second_http, deadline,
            case="two_api", stage="text", cookies={"zhiyan_session": cookie},
            headers={"X-CSRF-Token": csrf},
            files={"files": ("first.txt", text_bytes)})
        assert text_upload.status_code == 202
        text_task = text_upload.json()["tasks"][0]

        other_login = second_http.post("/api/v1/auth/login", json={
            "username": resources.names[1], "password": "correct-password"})
        assert other_login.status_code == 200
        other_cookie = other_login.cookies["zhiyan_session"]
        other_csrf = other_login.json()["csrf_token"]
        pdf_bytes = _text_pdf("Extractable tenant two PDF page")
        pdf_upload = _post_valid_upload_once(resources, first_http, deadline,
            case="two_api", stage="pdf",
            cookies={"zhiyan_session": other_cookie},
            headers={"X-CSRF-Token": other_csrf},
            files={"files": ("second.pdf", pdf_bytes)})
        assert pdf_upload.status_code == 202
        pdf_task = pdf_upload.json()["tasks"][0]
        assert first_http.post("/api/v1/auth/login", json={
            "username": resources.names[2], "password": "correct-password"
        }).status_code == 403

        _wait_task(first_http, str(text_task["task_id"]), cookie, deadline)
        _wait_task(second_http, str(pdf_task["task_id"]), other_cookie, deadline)
        _assert_published(resources, resources.users[0], text_task,
                          text_bytes, text_bytes.decode("utf-8"), worker_id,
                          frozenset(resources.users))
        _assert_published(resources, resources.users[1], pdf_task,
                          pdf_bytes, "Extractable tenant two PDF page", worker_id,
                          frozenset(resources.users))
        for client, task, own_cookie, foreign_cookie, content, stage in (
                (first_http, text_task, cookie, other_cookie, text_bytes, "text"),
                (second_http, pdf_task, other_cookie, cookie, pdf_bytes, "pdf")):
            own = _get_document_once(resources, client, deadline,
                case="two_api", stage=f"{stage}-own",
                document_id=str(task["document_id"]), cookie=own_cookie)
            assert own.status_code == 200 and own.content == content
            foreign = _get_document_once(resources, client, deadline,
                case="two_api", stage=f"{stage}-foreign",
                document_id=str(task["document_id"]), cookie=foreign_cookie)
            assert foreign.status_code == 404
            assert client.get(f"/api/v1/imports/tasks/{task['task_id']}",
                cookies={"zhiyan_session": foreign_cookie}).status_code == 404
        assert second_http.post("/api/v1/auth/logout",
            cookies={"zhiyan_session": cookie},
            headers={"X-CSRF-Token": csrf}).status_code == 204
        assert first_http.get("/api/v1/auth/session",
            cookies={"zhiyan_session": cookie}).status_code == 401
        assert _business_projection(resources.db, resources.excluded) == excluded_before
        assert _object_projection(resources.s3, resources.settings.s3_bucket,
                                  resources.excluded) == excluded_objects_before
    finally:
        _finish_real_case(resources, clients, owners)


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_api_death_after_upload_worker_survives_replacement(
        real_process_resources: RealProcessResources) -> None:
    resources = real_process_resources
    deadline = _real_deadline()
    worker_id = "p6-" + uuid4().hex
    original_port, replacement_port = _fresh_port(), _fresh_port()
    assert original_port != replacement_port
    owners: list[tuple[str, OwnedJob]] = []
    clients: list[httpx.Client] = []
    excluded_before = _business_projection(resources.db, resources.excluded)
    excluded_objects_before = _object_projection(
        resources.s3, resources.settings.s3_bucket, resources.excluded)
    try:
        original = _launch_role(resources, role="api", case="api_death",
                                port=original_port)
        owners.append(("api-original", original))
        old_http, old_identity = _ready_api(original, original_port, deadline)
        clients.append(old_http)
        login = old_http.post("/api/v1/auth/login", json={
            "username": resources.names[0], "password": "correct-password"})
        assert login.status_code == 200
        cookie = login.cookies["zhiyan_session"]
        csrf = login.json()["csrf_token"]
        content = b"A persistent upload survives its original API process."
        upload = _post_valid_upload_once(resources, old_http, deadline,
            case="api_death", stage="survivor",
            cookies={"zhiyan_session": cookie}, headers={"X-CSRF-Token": csrf},
            files={"files": ("survivor.txt", content)})
        assert upload.status_code == 202
        task = upload.json()["tasks"][0]
        with resources.db.transaction() as cursor:
            persisted = cursor.execute("select user_id,document_id from import_tasks "
                                       "where id=%s", (task["task_id"],)).fetchone()
            source = cursor.execute("select bucket,object_key,version_id,sha256,"
                                    "size_bytes from import_objects where task_id=%s",
                                    (task["task_id"],)).fetchone()
        assert persisted == {"user_id": resources.users[0],
                             "document_id": task["document_id"]}
        assert source is not None and source["version_id"]
        assert source["size_bytes"] == len(content)
        immutable = _read_object(resources.s3, source)
        assert immutable == content and hashlib.sha256(immutable).hexdigest() == source["sha256"]
        # The upload has committed before this gate. Claim takes the user lock
        # first, so the original Worker cannot claim until the dead API is
        # accounted for and this transaction releases both scoped row locks.
        causal = {"task_id": task["task_id"], "phase": "source_pinned",
                  "original_api_pid": old_identity["pid"]}
        causal_path = resources.directory / "api-death-causality.json"
        _atomic_json(causal_path, causal)
        with resources.db.transaction() as cursor:
            locked_user = cursor.execute("select id from users where id=%s "
                "and status='active' for update", (resources.users[0],)).fetchone()
            assert locked_user == {"id": resources.users[0]}
            locked = cursor.execute("select status from import_tasks where id=%s "
                "and user_id=%s for update", (task["task_id"], resources.users[0])).fetchone()
            assert locked is not None and locked["status"] == "queued"
            assert cursor.execute("select count(*) as n from import_task_attempts "
                "where task_id=%s", (task["task_id"],)).fetchone()["n"] == 0
            pinned = cursor.execute("select bucket,object_key,version_id,sha256,"
                "size_bytes from import_objects where task_id=%s and user_id=%s",
                (task["task_id"], resources.users[0])).fetchone()
            assert pinned == source
            causal["phase"] = "queued_locked_no_attempt"
            _atomic_json(causal_path, causal)
            worker = _launch_role(resources, role="worker", case="api_death",
                                  worker_id=worker_id)
            owners.append(("worker", worker))
            worker_identity = _ready_worker(worker, deadline)
            assert worker_identity["pid"] != old_identity["pid"]
            causal["worker_pid_before_api_death"] = worker_identity["pid"]
            causal["phase"] = "worker_alive_while_queued_locked"
            _atomic_json(causal_path, causal)
            _retire_role(resources, original, JOB_EXIT_SECONDS, "api-original")
            still_queued = cursor.execute("select status from import_tasks where id=%s",
                (task["task_id"],)).fetchone()
            assert still_queued["status"] == "queued" and worker.active_count() > 0
            causal["original_api_exit_codes"] = original.exit_codes
            causal["phase"] = "api_exited_worker_alive_task_locked"
            _atomic_json(causal_path, causal)
        causal["phase"] = "claim_gate_released_after_api_exit"
        _atomic_json(causal_path, causal)
        assert original._closed and original.exit_codes
        assert worker.active_count() > 0
        replacement = _launch_role(resources, role="api", case="api_death",
                                   port=replacement_port)
        owners.append(("api-replacement", replacement))
        new_http, new_identity = _ready_api(replacement, replacement_port, deadline)
        clients.append(new_http)
        assert new_identity["pid"] != old_identity["pid"]
        _wait_task(new_http, str(task["task_id"]), cookie, deadline)
        _assert_published(resources, resources.users[0], task, content,
                          content.decode("utf-8"), worker_id,
                          frozenset((resources.users[0],)))
        document = _get_document_once(resources, new_http, deadline,
            case="api_death", stage="survivor-own",
            document_id=str(task["document_id"]), cookie=cookie)
        assert document.status_code == 200 and document.content == content
        foreign_login = new_http.post("/api/v1/auth/login", json={
            "username": resources.names[1], "password": "correct-password"})
        assert foreign_login.status_code == 200
        foreign = _get_document_once(resources, new_http, deadline,
            case="api_death", stage="survivor-foreign",
            document_id=str(task["document_id"]),
            cookie=foreign_login.cookies["zhiyan_session"])
        assert foreign.status_code == 404
        assert new_http.get(f"/api/v1/imports/tasks/{task['task_id']}",
            cookies={"zhiyan_session": foreign_login.cookies["zhiyan_session"]}
        ).status_code == 404
        assert new_http.post("/api/v1/auth/login", json={
            "username": resources.names[2], "password": "correct-password"
        }).status_code == 403
        assert _business_projection(resources.db, resources.excluded) == excluded_before
        assert _object_projection(resources.s3, resources.settings.s3_bucket,
                                  resources.excluded) == excluded_objects_before
    finally:
        _finish_real_case(resources, clients, owners)


_DEATH_PHASES = {
    "intent_before_issue_return": ("intent", 1, (False, False, False, False), ()),
    "document_bytes_before_evidence": ("intent", 1, (False, False, False, False), ()),
    "document_evidence_committed": (
        "document_verified", 2, (True, False, False, False), ()),
    "rag_seal_before_evidence": (
        "document_verified", 2, (True, False, False, False), ("rag",)),
    "rag_evidence_committed": (
        "document_verified", 3, (True, True, False, False), ("rag",)),
    "episode_seal_before_evidence": (
        "document_verified", 3, (True, True, False, False), ("rag", "episode")),
    "pair_evidence_committed": (
        "pair_sealed", 4, (True, True, True, False), ("rag", "episode")),
    "terminal_commit_before_return": (
        "terminal_committed", 5, (True, True, True, True),
        ("rag", "episode")),
}


def _death_rows(resources: RealProcessResources, user: str,
                task_id: str, *, evidence_version: int | None = None
                ) -> dict[str, object]:
    with resources.db.transaction() as cursor:
        clock = cursor.execute("select clock_timestamp() as now").fetchone()["now"]
        task = cursor.execute("select * from import_tasks where id=%s and user_id=%s",
                              (task_id, user)).fetchone()
        selected_version = (evidence_version if evidence_version is not None
                            else task["lease_version"] if task else None)
        audits = cursor.execute("select * from import_task_attempts where "
                                "task_id=%s and user_id=%s order by lease_version",
                                (task_id, user)).fetchall()
        ordinary = cursor.execute("select * from user_mutation_leases where user_id=%s",
                                  (user,)).fetchone()
        source = cursor.execute("select bucket,object_key,version_id,sha256,size_bytes "
                                "from import_objects where task_id=%s and user_id=%s",
                                (task_id, user)).fetchone()
        evidence = cursor.execute("select e.phase,e.phase_version,e.intent_hash,"
            "e.document_id,e.worker_id,e.task_lease_token,e.user_lease_token,"
            "e.user_lease_version,p.payload_phase_version,p.document_slot,"
            "p.rag_sealed_slot,p.episode_sealed_slot,p.terminal_slot "
            "from import_publication_evidence e join "
            "import_publication_private_payloads p "
            "using(user_id,task_id,task_lease_version) where e.user_id=%s "
            "and e.task_id=%s and e.task_lease_version=%s",
            (user, task_id, selected_version)).fetchone() if task else None
        gate = cursor.execute("select status from user_publication_gates where "
            "user_id=%s and task_id=%s and task_lease_version=%s",
            (user, task_id, selected_version)).fetchone() if task else None
        queue = cursor.execute("select state,reason_code,queue_version,claim_token,"
            "claim_expires_at,last_claimed_at,due_at,transient_count from "
            "import_publication_recovery_queue where user_id=%s and task_id=%s "
            "and task_lease_version=%s", (user, task_id,
                                           selected_version)).fetchone() if task else None
        recovery = cursor.execute("select owner,token,version,expires_at,"
            "queue_claim_token,queue_version from import_publication_recovery_leases "
            "where user_id=%s and task_id=%s and task_lease_version=%s",
            (user, task_id, selected_version)).fetchone() if task else None
        issuance = cursor.execute("select token,kind,issued_version from "
            "import_publication_recovery_token_issuance where user_id=%s "
            "and task_id=%s and task_lease_version=%s order by kind,issued_version",
            (user, task_id, selected_version)).fetchall() if task else []
        reservations = cursor.execute("select vector_kind,generation_id,state,revoked_at "
            "from generation_reservations where user_id=%s and task_id=%s "
            "and task_lease_version=%s order by vector_kind",
            (user, task_id, selected_version)).fetchall() if task else []
        generations = cursor.execute("select vector_kind,generation_id,state,abandoned_at,"
            "namespace,index_key,owner,user_lease_token,user_lease_version,"
            "task_id,task_lease_token,task_lease_version,expected_count,content_digest "
            "from vector_generations where tenant_id=%s and generation_id=any(%s)",
            (user, [row["generation_id"] for row in reservations])).fetchall()
        ref = cursor.execute("select document_id from document_objects where "
            "user_id=%s and document_id=%s", (user,
            task["document_id"])).fetchone() if task else None
    return {"clock": clock, "task": task, "audits": audits, "ordinary": ordinary,
            "source": source, "evidence": evidence, "gate": gate,
             "queue": queue, "recovery": recovery, "issuance": issuance,
             "reservations": reservations,
            "generations": generations, "ref": ref}


def _death_public_state(resources: RealProcessResources, user: str) -> tuple:
    from app.postgres_snapshots import PostgresSnapshotRepository
    from app.postgres_vector_generations import PostgresVectorGenerationAuthority, VectorScope
    from hello_agents.memory.rag.index_identity import IndexIdentity

    authority = PostgresVectorGenerationAuthority(resources.db)
    scopes = (
        VectorScope(user, "rag", f"pdf_{user}", IndexIdentity(
            "qdrant", resources.settings.rag_collection, resources.settings.rag_profile)),
        VectorScope(user, "episode", "episodes", IndexIdentity(
            "qdrant", resources.settings.episode_collection,
            resources.settings.episode_profile)),
    )
    snapshots = PostgresSnapshotRepository(resources.db)
    with resources.db.transaction() as cursor:
        refs = cursor.execute("select document_id,bucket,object_key,version_id,sha256 "
            "from document_objects where user_id=%s order by document_id",
            (user,)).fetchall()
        witnesses = cursor.execute("select vector_kind,namespace,index_key,"
            "head_revision,last_generation_id,documents_sha256 "
            "from history_document_witnesses where tenant_id=%s "
            "order by vector_kind,namespace,head_revision", (user,)).fetchall()
    return (tuple(authority.read_head(scope) for scope in scopes),
            snapshots.read(user, "history"), snapshots.read(user, "memory"),
            refs, witnesses)


def _death_proof_identity(proof: Any) -> tuple:
    """Publication fields that must survive ack without a second writer."""
    return (proof.proof.attempt_key, proof.proof.intent_hash,
            proof.proof.final_expected_hash, proof.proof.rag_receipt,
            proof.proof.episode_receipt, proof.event_id,
            proof.history_version, proof.memory_version,
            proof.document_ref, proof.witness, proof.pair)


def _assert_death_recovery_capture(rows: dict[str, object], *,
                                   user: str, task_id: str,
                                   old_version: int, worker_id: str,
                                   reason: str,
                                   previous_issuance: tuple[dict[str, object], ...] = ()) -> None:
    lease = rows["recovery"]
    queue = rows["queue"]
    assert lease is not None and queue is not None
    assert (lease["owner"] == worker_id and lease["version"] > 0
            and lease["queue_version"] > 0
            and lease["expires_at"] <= rows["clock"]
            and queue["state"] == "resolved"
            and queue["reason_code"] == reason
            and queue["queue_version"] == lease["queue_version"] + 1
            and queue["claim_token"] is None)
    current_tokens = {
        ("queue", lease["queue_claim_token"], lease["queue_version"]),
        ("recovery", lease["token"], lease["version"])}
    previous_tokens = {(row["kind"], row["token"], row["issued_version"])
                       for row in previous_issuance}
    assert (not current_tokens.intersection(previous_tokens)
            and len(previous_tokens) == len(previous_issuance)
            and {(row["kind"], row["token"], row["issued_version"])
                 for row in rows["issuance"]} == previous_tokens | current_tokens
            and len(rows["issuance"]) == len(previous_issuance) + 2)
    assert (rows["task"]["user_id"] == user
            and str(rows["task"]["id"]) == task_id
            and old_version > 0)


def _assert_death_revoked_candidates(
        original: dict[str, object], final: dict[str, object]
        ) -> dict[str, tuple[str, tuple[int, str] | None]]:
    """Use only this attempt's exact, permanently revoked UUIDs for raw reads."""
    candidates = original["candidates"]
    assert set(candidates) == {"rag", "episode"}
    assert {row["vector_kind"]: str(row["generation_id"])
            for row in final["reservations"]} == candidates
    assert all(row["state"] == "revoked" and row["revoked_at"] is not None
               for row in final["reservations"])
    old_rows = {row["vector_kind"]: row for row in original["generations"]}
    abandoned = {row["vector_kind"]: row for row in final["generations"]}
    assert set(abandoned) == set(old_rows)
    for kind, row in abandoned.items():
        prior = old_rows[kind]
        assert (str(row["generation_id"]) == candidates[kind]
                and row["state"] == "abandoned"
                and row["abandoned_at"] is not None
                and row["namespace"] == prior["namespace"]
                and row["index_key"] == prior["index_key"]
                and row["expected_count"] == prior["expected_count"]
                and row["content_digest"] == prior["content_digest"])
    return {kind: (candidate, (
        (old_rows[kind]["expected_count"], old_rows[kind]["content_digest"])
        if kind in old_rows else None))
        for kind, candidate in candidates.items()}


def _assert_death_checkpoint(resources: RealProcessResources, scenario: str,
                             user: str, task: dict[str, object], content: bytes,
                             worker_id: str, marker: dict[str, object]
                             ) -> dict[str, object]:
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    from app.import_publication_proof import ImportPublicationProofService
    from app.postgres_vector_generations import VectorScope
    from hello_agents.memory.rag.index_identity import IndexIdentity

    task_id = str(task["task_id"])
    rows = _death_rows(resources, user, task_id)
    current, audits, ordinary = rows["task"], rows["audits"], rows["ordinary"]
    assert current is not None and ordinary is not None and len(audits) == 1
    audit = audits[0]
    assert (current["document_id"] == task["document_id"]
            and current["user_id"] == user and current["claimed_by"] == worker_id
            and current["lease_version"] == audit["lease_version"] > 0
            and current["lease_token"] == audit["lease_token"]
            and current["user_lease_token"] == audit["user_lease_token"]
            and current["user_lease_version"] == audit["user_lease_version"]
            and audit["worker_id"] == worker_id
            and ordinary["owner"] == worker_id
            and ordinary["lease_token"] == audit["user_lease_token"]
            and ordinary["lease_version"] == audit["user_lease_version"])
    assert (marker["user_id"] == user and marker["task_id"] == task_id
            and marker.get("worker_id", worker_id) == worker_id)
    assert rows["recovery"] is None and not rows["issuance"]
    if "lease_version" in marker:
        assert marker["lease_version"] == audit["lease_version"]
    source = rows["source"]
    assert (source is not None and source["bucket"] == resources.settings.s3_bucket
            and source["version_id"] and source["size_bytes"] == len(content)
            and source["sha256"] == hashlib.sha256(content).hexdigest()
            and _read_object(resources.s3, source) == content)
    if scenario == "claim_before_intent":
        assert (current["status"] == "running" and audit["ended_at"] is None
                and rows["evidence"] is None and rows["gate"] is None
                and rows["queue"] is None and rows["recovery"] is None
                and not rows["issuance"] and not rows["reservations"]
                and not rows["generations"] and rows["ref"] is None)
        return rows
    expected_phase, version, slots, sealed = _DEATH_PHASES[scenario]
    evidence = rows["evidence"]
    assert (evidence is not None and evidence["phase"] == expected_phase
            and evidence["phase_version"] == version
            and evidence["payload_phase_version"] == version
            and evidence["document_id"] == task["document_id"]
            and evidence["worker_id"] == worker_id
            and evidence["task_lease_token"] == audit["lease_token"]
            and evidence["user_lease_token"] == audit["user_lease_token"]
            and evidence["user_lease_version"] == audit["user_lease_version"]
            and tuple(evidence[name] is not None for name in (
                "document_slot", "rag_sealed_slot", "episode_sealed_slot",
                "terminal_slot")) == slots)
    assert rows["gate"] == {"status": "unresolved"}
    assert rows["queue"] is not None and rows["queue"]["state"] == "pending"
    frozen = PostgresImportPublicationEvidenceRepository(resources.db).read_exact(
        user, task_id, audit["lease_version"])
    assert frozen is not None and frozen.intent_hash == evidence["intent_hash"]
    assert (frozen.intent["attempt"]["task_id"] == task_id
            and frozen.intent["attempt"]["document_id"] == task["document_id"])
    candidates = {kind: frozen.intent["scopes"][kind]["candidate_id"]
                  for kind in ("rag", "episode")}
    assert {(row["vector_kind"], str(row["generation_id"]), row["state"])
            for row in rows["reservations"]} == {
                (kind, candidate, "reserved") for kind, candidate in candidates.items()}
    assert {row["vector_kind"] for row in rows["generations"]} == set(sealed)
    expected_generation_state = (
        "published" if scenario == "terminal_commit_before_return" else "sealed")
    assert all(row["state"] == expected_generation_state
               for row in rows["generations"])
    for generation in rows["generations"]:
        kind = generation["vector_kind"]
        frozen_scope = frozen.intent["scopes"][kind]
        assert (str(generation["generation_id"]) == candidates[kind]
                and generation["namespace"] == frozen_scope["namespace"]
                and generation["index_key"] == frozen_scope["index_key"]
                and generation["owner"] == worker_id
                and generation["user_lease_token"] == audit["user_lease_token"]
                and generation["user_lease_version"] == audit["user_lease_version"]
                and generation["task_id"] == task_id
                and generation["task_lease_token"] == audit["lease_token"]
                and generation["task_lease_version"] == audit["lease_version"]
                and generation["expected_count"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", generation["content_digest"]))
    for kind, candidate in candidates.items():
        identity = frozen.intent["scopes"][kind]["identity"]
        physical = identity.get("physical_collection")
        assert isinstance(physical, str) and physical
        profile = (resources.settings.rag_profile if kind == "rag"
                   else resources.settings.episode_profile)
        collection = (resources.settings.rag_collection if kind == "rag"
                      else resources.settings.episode_collection)
        scope = VectorScope(user, kind,
            frozen.intent["scopes"][kind]["namespace"],
            IndexIdentity("qdrant", collection, profile))
        assert (scope.identity.physical_collection == physical
                and scope.index_key == frozen.intent["scopes"][kind]["index_key"])
        points: list[object] = []
        offset = None
        for _ in range(4):
            batch, offset = resources.raw.client.scroll(
                collection_name=physical, limit=128, offset=offset,
                with_payload=True, with_vectors=True)
            points.extend(point for point in batch
                          if point.payload.get("_gv_generation") == candidate)
            if offset is None:
                break
        else:
            raise AssertionError("candidate_physical_scroll_exceeds_bound")
        if kind in sealed:
            generation = next(row for row in rows["generations"]
                              if row["vector_kind"] == kind)
            assert _physical_generation_manifest(scope, candidate, points) == (
                generation["expected_count"], generation["content_digest"])
        else:
            assert not points
    if scenario == "document_bytes_before_evidence":
        key = frozen.intent["document"]["key"]
        versions = resources.s3.list_object_versions(
            Bucket=resources.settings.s3_bucket, Prefix=key).get("Versions", [])
        exact = [item for item in versions if item["Key"] == key]
        assert len(exact) == 1 and exact[0]["VersionId"]
        document = _read_object(resources.s3, {
            "bucket": resources.settings.s3_bucket, "object_key": key,
            "version_id": exact[0]["VersionId"]})
        assert document == content and hashlib.sha256(document).hexdigest() == source["sha256"]
    proof = ImportPublicationProofService(resources.db).prove_exact(
        user, task_id, audit["lease_version"])
    if scenario == "terminal_commit_before_return":
        assert (current["status"] == "succeeded" and audit["end_reason"] == "succeeded"
                and audit["ended_at"] is not None and rows["ref"] is not None
                and proof is not None)
    else:
        assert (current["status"] == "running" and audit["ended_at"] is None
                and rows["ref"] is None and proof is None)
    rows["candidates"] = candidates
    return rows


def _await_death_expiry(resources: RealProcessResources, user: str,
                        task_id: str, original: dict[str, object],
                        deadline: float) -> None:
    expiry_deadline = min(deadline, time.monotonic() + 90)
    while True:
        rows = _death_rows(resources, user, task_id)
        task, ordinary, audits = rows["task"], rows["ordinary"], rows["audits"]
        assert (task is not None and ordinary is not None and len(audits) == 1
                and task["lease_version"] == original["task"]["lease_version"]
                and task["lease_token"] == original["task"]["lease_token"]
                and ordinary["lease_token"] == original["ordinary"]["lease_token"]
                and audits[0]["heartbeat_at"] == original["audits"][0]["heartbeat_at"])
        if (task["lease_expires_at"] <= rows["clock"]
                and ordinary["lease_expires_at"] <= rows["clock"]):
            return
        time.sleep(min(DATA_POLL, _real_left(expiry_deadline, DATA_POLL)))


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
@pytest.mark.parametrize("scenario", _DEATH_BOUNDARIES)
def test_real_worker_death_boundary(
        real_process_resources: RealProcessResources, scenario: str) -> None:
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    from app.import_publication_proof import ImportPublicationProofService

    resources = real_process_resources
    deadline = _real_deadline()
    user = resources.users[0]
    old_worker_id = "p6-old-" + uuid4().hex
    fresh_worker_id = "p6-fresh-" + uuid4().hex
    port = _fresh_port()
    owners: list[tuple[str, OwnedJob]] = []
    clients: list[httpx.Client] = []
    excluded_before = _business_projection(resources.db, resources.excluded)
    excluded_objects_before = _object_projection(
        resources.s3, resources.settings.s3_bucket, resources.excluded)
    content = f"A bounded durable import for {scenario}.".encode("utf-8")
    try:
        api = _launch_role(resources, role="api", case=scenario, port=port)
        owners.append(("api", api))
        http, api_identity = _ready_api(api, port, deadline)
        clients.append(http)
        login = http.post("/api/v1/auth/login", json={
            "username": resources.names[0], "password": "correct-password"})
        assert login.status_code == 200
        cookie = login.cookies["zhiyan_session"]
        csrf = login.json()["csrf_token"]
        upload = _post_valid_upload_once(resources, http, deadline,
            case=scenario, stage="death", cookies={"zhiyan_session": cookie},
            headers={"X-CSRF-Token": csrf},
            files={"files": ("death.txt", content)})
        assert upload.status_code == 202 and len(upload.json()["tasks"]) == 1
        task = upload.json()["tasks"][0]
        public_before = _death_public_state(resources, user)
        target = {"user_id": user, "task_id": str(task["task_id"]),
                  "worker_id": old_worker_id}
        old = _launch_role(resources, role="worker", case=scenario,
                           worker_id=old_worker_id, fault=scenario, target=target)
        owners.append(("worker-original", old))
        old_identity = _ready_worker(old, deadline)
        assert old.control_identity is not None
        marker = _await_event(old.control_identity, "fault",
                              timeout=_real_left(deadline, 60))
        assert (marker["pid"] == old_identity["pid"]
                and marker["ppid"] == old_identity["parent"]
                and _path_identity(marker["base_executable"]) ==
                    _path_identity(sys._base_executable))
        returned = old.control_identity.event_path("original_returned", 1)
        if scenario in {"intent_before_issue_return", "document_bytes_before_evidence",
                        "rag_seal_before_evidence", "episode_seal_before_evidence",
                        "terminal_commit_before_return"}:
            assert returned.is_file()
            observed_return = _read_json(returned)
            assert (observed_return["nonce"] == old.control_identity.nonce
                    and observed_return["case"] == scenario
                    and observed_return["pid"] == old_identity["pid"])
        else:
            assert not returned.exists()
        assert not old.control_identity.release_path("fault", 1).exists()
        checkpoint = _assert_death_checkpoint(
            resources, scenario, user, task, content, old_worker_id, marker)
        terminal_publication = None
        terminal_proof_identity = None
        if scenario == "terminal_commit_before_return":
            terminal_publication = _death_public_state(resources, user)
            assert terminal_publication != public_before
            committed_proof = ImportPublicationProofService(resources.db).prove_exact(
                user, str(task["task_id"]), checkpoint["task"]["lease_version"])
            assert (committed_proof is not None
                    and committed_proof.proof.phase == "terminal_committed"
                    and committed_proof.proof.phase_version == 5)
            terminal_proof_identity = _death_proof_identity(committed_proof)
            _assert_published(resources, user, task, content,
                              content.decode("utf-8"), old_worker_id,
                              frozenset((user,)))
        else:
            assert _death_public_state(resources, user) == public_before
        _atomic_json(resources.directory / f"{scenario}-checkpoint.json", {
            "scenario": scenario, "task_id": str(task["task_id"]),
            "document_id": str(task["document_id"]),
            "old_worker_pid": old_identity["pid"],
            "old_worker_id": old_worker_id,
            "lease_version": checkpoint["task"]["lease_version"],
            "phase": checkpoint["evidence"]["phase"] if checkpoint["evidence"] else None,
            "candidate_ids": checkpoint.get("candidates", {}),
            "source_sha256": checkpoint["source"]["sha256"],
            "control_event_sha256": hashlib.sha256(
                old.control_identity.event_path("fault", 1).read_bytes()).hexdigest(),
            "api_pid": api_identity["pid"]})
        _retire_role(resources, old, JOB_EXIT_SECONDS, "worker-original")
        assert old._closed and old.launcher_exit is not None and old.exit_codes
        assert old.exit_codes[old_identity["pid"]] == 3
        if scenario == "terminal_commit_before_return":
            assert _death_public_state(resources, user) == terminal_publication
            after_death = _death_rows(resources, user, str(task["task_id"]))
            assert after_death["evidence"] == checkpoint["evidence"]
            after_death_proof = ImportPublicationProofService(resources.db).prove_exact(
                user, str(task["task_id"]), checkpoint["task"]["lease_version"])
            assert (after_death_proof is not None
                    and _death_proof_identity(after_death_proof) ==
                        terminal_proof_identity)
        if scenario != "terminal_commit_before_return":
            post_death = _death_rows(resources, user, str(task["task_id"]))
            assert (post_death["task"]["lease_token"] ==
                    checkpoint["task"]["lease_token"]
                    and post_death["ordinary"]["lease_token"] ==
                        checkpoint["ordinary"]["lease_token"])
            _await_death_expiry(resources, user, str(task["task_id"]),
                                post_death, deadline)
        fresh = _launch_role(resources, role="worker", case=scenario,
                             worker_id=fresh_worker_id)
        owners.append(("worker-replacement", fresh))
        fresh_identity = _ready_worker(fresh, deadline)
        assert fresh_identity["pid"] != old_identity["pid"]
        _wait_task(http, str(task["task_id"]), cookie, deadline)
        recovery_deadline = min(deadline, time.monotonic() + 120)
        while True:
            final = _death_rows(resources, user, str(task["task_id"]),
                evidence_version=checkpoint["task"]["lease_version"])
            prior = final["audits"][0]
            if scenario == "terminal_commit_before_return":
                if final["evidence"]["phase"] == "proved_succeeded":
                    assert (len(final["audits"]) == 1
                            and final["task"]["lease_version"] ==
                                checkpoint["task"]["lease_version"]
                            and prior["end_reason"] == "succeeded"
                            and final["gate"] == {"status": "resolved"}
                            and final["queue"]["state"] == "resolved"
                            and final["evidence"]["phase_version"] == 6
                            and final["evidence"]["payload_phase_version"] == 6
                            and all(final["evidence"][slot] ==
                                    checkpoint["evidence"][slot] for slot in (
                                        "document_slot", "rag_sealed_slot",
                                        "episode_sealed_slot", "terminal_slot"))
                            and {str(row["generation_id"]) for row in
                                 final["reservations"]} ==
                                 set(checkpoint["candidates"].values())
                            and all(row["state"] == "reserved" for row in
                                    final["reservations"]))
                    proof = ImportPublicationProofService(resources.db).prove_exact(
                        user, str(task["task_id"]), prior["lease_version"])
                    assert (proof is not None
                            and proof.proof.phase == "proved_succeeded"
                            and proof.proof.phase_version == 6
                            and _death_proof_identity(proof) ==
                                terminal_proof_identity)
                    _assert_death_recovery_capture(final, user=user,
                         task_id=str(task["task_id"]),
                         old_version=prior["lease_version"],
                         worker_id=fresh_worker_id, reason="proved_succeeded")
                    break
            elif (len(final["audits"]) >= 2 and final["task"]["status"] == "succeeded"
                  and (scenario == "claim_before_intent" or
                       final["evidence"]["phase"] == "abandoned")):
                assert (prior["end_reason"] == "lease_expired"
                        and final["audits"][-1]["worker_id"] == fresh_worker_id
                        and final["audits"][-1]["lease_token"] != prior["lease_token"]
                        and final["audits"][-1]["user_lease_token"] !=
                            prior["user_lease_token"]
                        and final["audits"][-1]["user_lease_version"] >
                            prior["user_lease_version"]
                        and final["audits"][-1]["lease_version"] > prior["lease_version"])
                if scenario != "claim_before_intent":
                    assert (final["gate"] == {"status": "resolved"}
                            and final["queue"]["state"] == "resolved"
                            and {str(row["generation_id"]) for row in
                                 final["reservations"]} ==
                                 set(checkpoint["candidates"].values())
                            and all(row["state"] == "revoked" for row in
                                    final["reservations"])
                            and all(row["state"] == "abandoned" for row in
                                    final["generations"]))
                    _assert_death_recovery_capture(final, user=user,
                         task_id=str(task["task_id"]),
                         old_version=prior["lease_version"],
                         worker_id=fresh_worker_id, reason="abandoned")
                else:
                    assert final["recovery"] is None and not final["issuance"]
                assert ImportPublicationProofService(resources.db).prove_exact(
                    user, str(task["task_id"]), prior["lease_version"]) is None
                fresh_frozen = PostgresImportPublicationEvidenceRepository(
                    resources.db).read_exact(user, str(task["task_id"]),
                        final["audits"][-1]["lease_version"])
                assert fresh_frozen is not None and fresh_frozen.phase in {
                    "terminal_committed", "proved_succeeded"}
                if scenario != "claim_before_intent":
                    assert {fresh_frozen.intent["scopes"][kind]["candidate_id"]
                            for kind in ("rag", "episode")}.isdisjoint(
                                set(checkpoint["candidates"].values()))
                break
            time.sleep(min(DATA_POLL, _real_left(recovery_deadline, DATA_POLL)))
        retained_revoked = (None if scenario in {
            "claim_before_intent", "terminal_commit_before_return"} else
            _assert_death_revoked_candidates(checkpoint, final))
        _assert_published(resources, user, task, content,
                          content.decode("utf-8"),
                          old_worker_id if scenario == "terminal_commit_before_return"
                          else fresh_worker_id, frozenset((user,)),
                          revoked_candidates=retained_revoked)
        if scenario == "terminal_commit_before_return":
            assert _death_public_state(resources, user) == terminal_publication
        assert (final["source"] == checkpoint["source"]
                and final["task"]["document_id"] == task["document_id"])
        assert _business_projection(resources.db, resources.excluded) == excluded_before
        assert _object_projection(resources.s3, resources.settings.s3_bucket,
                                  resources.excluded) == excluded_objects_before
        _atomic_json(resources.directory / f"{scenario}-death-result.json", {
            "scenario": scenario, "task_id": str(task["task_id"]),
            "old_runtime_pid": old_identity["pid"],
            "replacement_runtime_pid": fresh_identity["pid"],
            "old_exit_codes": old.exit_codes,
            "old_lease_version": checkpoint["task"]["lease_version"],
            "final_lease_version": final["task"]["lease_version"],
            "original_phase": final["evidence"]["phase"] if final["evidence"] else None,
            "original_candidate_ids": checkpoint.get("candidates", {}),
            "fresh_candidate_ids": (
                {kind: fresh_frozen.intent["scopes"][kind]["candidate_id"]
                 for kind in ("rag", "episode")}
                if scenario != "terminal_commit_before_return" else {}),
            "source_sha256": final["source"]["sha256"],
            "excluded_projection_sha256": hashlib.sha256(json.dumps(
                excluded_before, sort_keys=True).encode()).hexdigest()})
    finally:
        _finish_real_case(resources, clients, owners)


def _survivor_checkpoint(resources: RealProcessResources, scenario: str,
                         user: str, task: dict[str, object], content: bytes,
                         worker_id: str) -> dict[str, object]:
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    from app.import_publication_proof import ImportPublicationProofService

    task_id = str(task["task_id"])
    rows = _death_rows(resources, user, task_id)
    current, ordinary, audits = rows["task"], rows["ordinary"], rows["audits"]
    assert current is not None and ordinary is not None and len(audits) == 1
    assert (current["status"] == "running" and current["user_id"] == user
            and current["document_id"] == task["document_id"]
            and current["claimed_by"] == worker_id
            and current["lease_version"] == audits[0]["lease_version"]
            and current["lease_token"] == audits[0]["lease_token"]
            and current["user_lease_token"] == ordinary["lease_token"]
            and current["user_lease_version"] == ordinary["lease_version"]
            and audits[0]["ended_at"] is None and ordinary["owner"] == worker_id)
    source = rows["source"]
    assert (source is not None and source["bucket"] == resources.settings.s3_bucket
            and source["size_bytes"] == len(content)
            and source["sha256"] == hashlib.sha256(content).hexdigest()
            and _read_object(resources.s3, source) == content)
    assert rows["ref"] is None
    assert ImportPublicationProofService(resources.db).prove_exact(
        user, task_id, current["lease_version"]) is None
    if scenario == "stale_authority":
        assert (rows["evidence"] is None and rows["gate"] is None
                and rows["queue"] is None and not rows["reservations"]
                and not rows["generations"])
        rows["candidates"] = {}
    else:
        frozen = PostgresImportPublicationEvidenceRepository(resources.db).read_exact(
            user, task_id, current["lease_version"])
        evidence = rows["evidence"]
        assert (frozen is not None and frozen.phase == "document_verified"
                and evidence is not None and evidence["phase"] == "document_verified"
                and evidence["phase_version"] == evidence["payload_phase_version"] == 2
                and evidence["intent_hash"] == frozen.intent_hash
                and evidence["document_slot"] is not None
                and all(evidence[slot] is None for slot in (
                    "rag_sealed_slot", "episode_sealed_slot",
                    "terminal_slot"))
                and rows["gate"] == {"status": "unresolved"}
                and rows["queue"]["state"] == "pending")
        candidates = {kind: frozen.intent["scopes"][kind]["candidate_id"]
                      for kind in ("rag", "episode")}
        assert ({row["vector_kind"]: str(row["generation_id"])
                 for row in rows["reservations"]} == candidates
                and all(row["state"] == "reserved" for row in rows["reservations"]))
        document = json.loads(frozen.document_slot)
        assert (document["user_id"] == user
                and document["document_id"] == task["document_id"]
                and document["bucket"] == resources.settings.s3_bucket
                and document["sha256"] == source["sha256"]
                and document["size_bytes"] == len(content)
                and _read_object(resources.s3, {
                    "bucket": document["bucket"], "object_key": document["key"],
                    "version_id": document["version_id"]}) == content)
        assert (len(rows["generations"]) == 1
                and rows["generations"][0]["vector_kind"] == "rag"
                and str(rows["generations"][0]["generation_id"]) == candidates["rag"]
                and rows["generations"][0]["state"] == "staging"
                and rows["generations"][0]["owner"] == worker_id
                and rows["generations"][0]["task_id"] == task_id
                and rows["generations"][0]["task_lease_version"] ==
                    current["lease_version"]
                and rows["generations"][0]["task_lease_token"] ==
                    current["lease_token"])
        physical = frozen.intent["scopes"]["rag"]["identity"]["physical_collection"]
        offset = None
        for _ in range(4):
            batch, offset = resources.raw.client.scroll(
                collection_name=physical, limit=128, offset=offset,
                with_payload=True, with_vectors=True)
            assert not any(point.payload.get("_gv_generation") == candidates["rag"]
                           for point in batch), "old_candidate_written_before_release"
            if offset is None:
                break
        else:
            raise AssertionError("pre_late_physical_scroll_exceeds_bound")
        rows["candidates"] = candidates
    return rows


def _survivor_final(resources: RealProcessResources, scenario: str,
                    user: str, task: dict[str, object], original: dict[str, object],
                    worker_id: str) -> dict[str, object]:
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    from app.import_publication_proof import ImportPublicationProofService

    task_id = str(task["task_id"])
    final = _death_rows(resources, user, task_id,
                        evidence_version=original["task"]["lease_version"])
    prior = final["audits"][0]
    newest = final["audits"][-1]
    assert (len(final["audits"]) >= 2 and final["task"]["status"] == "succeeded"
            and prior["end_reason"] == "lease_expired"
            and newest["worker_id"] == worker_id
            and newest["lease_version"] > prior["lease_version"]
            and newest["lease_token"] != prior["lease_token"]
            and newest["user_lease_token"] != prior["user_lease_token"]
            and newest["user_lease_version"] > prior["user_lease_version"]
            and final["source"] == original["source"]
            and final["task"]["document_id"] == task["document_id"])
    assert ImportPublicationProofService(resources.db).prove_exact(
        user, task_id, prior["lease_version"]) is None
    fresh = PostgresImportPublicationEvidenceRepository(resources.db).read_exact(
        user, task_id, newest["lease_version"])
    assert fresh is not None and fresh.phase in {"terminal_committed", "proved_succeeded"}
    if scenario == "stale_authority":
        assert final["evidence"] is None and final["recovery"] is None
    else:
        assert (final["evidence"]["phase"] == "abandoned"
                and final["gate"] == {"status": "resolved"}
                and final["queue"]["state"] == "resolved")
        _assert_death_recovery_capture(final, user=user, task_id=task_id,
            old_version=prior["lease_version"], worker_id=worker_id,
            reason="abandoned")
        _assert_death_revoked_candidates(original, final)
        assert {fresh.intent["scopes"][kind]["candidate_id"]
                for kind in ("rag", "episode")}.isdisjoint(
                    set(original["candidates"].values()))
    return final


def _assert_late_readback(resources: RealProcessResources, user: str,
                          original: dict[str, object], fresh: dict[str, object],
                          admitted: dict[str, object], content: bytes) -> None:
    from app.isolated_import_provider import open_isolated_providers
    from app.import_publication_proof import ImportPublicationProofService
    from app.postgres_vector_generations import PostgresVectorGenerationAuthority, VectorScope
    from app.vector_generation_service import VectorGenerationService
    from hello_agents.memory.rag.index_identity import IndexIdentity
    from hello_agents.memory.storage.generation_vector_store import (
        _physical_id, _same_cosine_vector)

    old_rag = original["candidates"]["rag"]
    old_episode = original["candidates"]["episode"]
    latest = fresh["audits"][-1]
    proof = ImportPublicationProofService(resources.db).prove_exact(
        user, str(fresh["task"]["id"]), latest["lease_version"])
    assert proof is not None and proof.proof.phase in {
        "terminal_committed", "proved_succeeded"}
    rag_scope = VectorScope(user, "rag", f"pdf_{user}", IndexIdentity(
        "qdrant", resources.settings.rag_collection, resources.settings.rag_profile))
    episode_scope = VectorScope(user, "episode", "episodes", IndexIdentity(
        "qdrant", resources.settings.episode_collection,
        resources.settings.episode_profile))
    views = VectorGenerationService(PostgresVectorGenerationAuthority(resources.db),
                                    resources.raw)
    vector_checks: list[tuple[str, object]] = []
    for scope, receipt, old_id in (
            (rag_scope, proof.proof.rag_receipt, old_rag),
            (episode_scope, proof.proof.episode_receipt, old_episode)):
        visible = views.read_view(scope).scroll(
            scope.identity.physical_collection, with_vectors=True,
            expected_manifest=(receipt[9], receipt[10]))
        assert (len(visible) == receipt[9]
                and str(PostgresVectorGenerationAuthority(resources.db).read_head(
                    scope).generation_id) == receipt[0]
                and receipt[0] != old_id)
        physical = []
        offset = None
        for _ in range(4):
            batch, offset = resources.raw.client.scroll(
                collection_name=scope.identity.physical_collection, limit=128,
                offset=offset, with_payload=True, with_vectors=True)
            physical.extend(batch)
            if offset is None:
                break
        else:
            raise AssertionError("late_physical_scroll_exceeds_bound")
        own = [point for point in physical if point.payload["_gv_tenant"] == user]
        fresh_points = [point for point in own
                        if point.payload["_gv_generation"] == receipt[0]]
        old_points = [point for point in own
                      if point.payload["_gv_generation"] == old_id]
        assert (len(fresh_points) == len(visible) == receipt[9]
                and len(own) == len(fresh_points) + len(old_points)
                and {point.payload["_gv_logical_id"] for point in fresh_points} ==
                    {point.id for point in visible}
                and all(point.payload["_gv_tenant"] == user
                        and point.payload["_gv_kind"] == scope.vector_kind
                        and point.payload["_gv_namespace"] == scope.namespace
                        and point.payload["_gv_index"] == scope.index_key
                        and len(point.vector) == 384
                        and str(point.id) == str(_physical_id(scope,
                            UUID(point.payload["_gv_generation"]),
                            point.payload["_gv_logical_id"]))
                        for point in own))
        vector_checks.extend((scope.vector_kind, point)
                             for point in (*fresh_points, *old_points))
        if scope.vector_kind == "rag":
            assert (len(old_points) == admitted["point_count"] == 1
                    and _digest(tuple(str(point.id) for point in old_points)) ==
                        admitted["point_ids_sha256"]
                    and hashlib.sha256(json.dumps(
                        [point.payload for point in old_points],
                        sort_keys=True, ensure_ascii=False).encode()).hexdigest() ==
                        admitted["point_payloads_sha256"]
                    and all(point.payload["_gv_generation"] == old_rag
                            and point.payload["_gv_namespace"] == scope.namespace
                            and point.payload["_gv_kind"] == "rag"
                            and re.sub(r"\s+", " ", point.payload["content"]).strip() ==
                                re.sub(r"\s+", " ", content.decode("utf-8")).strip()
                            and len(point.vector) == 384 for point in old_points))
        else:
            assert not old_points
    providers = open_isolated_providers(resources.settings, env={
        "ISOLATED_IMPORT_RAG_EMBEDDING_PROVIDER": "simple",
        "ISOLATED_IMPORT_EPISODE_EMBEDDING_PROVIDER": "simple"})
    try:
        for kind, point in vector_checks:
            embedding = (providers.rag_runtime if kind == "rag"
                         else providers.episode_runtime)
            assert _same_cosine_vector(point.vector,
                                       embedding.embed_query(point.payload["content"]))
    finally:
        assert providers.close(timeout_seconds=5), "late_readback_provider_close_failed"


def _run_surviving_old_pid(resources: RealProcessResources, scenario: str) -> None:
    assert scenario in {"stale_authority", "late_candidate_bytes"}
    user = resources.users[0]
    deadline = _real_deadline()
    old_worker_id = "p6-old-" + uuid4().hex
    fresh_worker_id = "p6-fresh-" + uuid4().hex
    content = f"Durable surviving worker authority for {scenario}.".encode("utf-8")
    owners: list[tuple[str, OwnedJob]] = []
    clients: list[httpx.Client] = []
    old: OwnedJob | None = None
    excluded_before = _business_projection(resources.db, resources.excluded)
    excluded_objects_before = _object_projection(
        resources.s3, resources.settings.s3_bucket, resources.excluded)
    try:
        port = _fresh_port()
        api = _launch_role(resources, role="api", case=scenario, port=port)
        owners.append(("api", api))
        http, api_identity = _ready_api(api, port, deadline)
        clients.append(http)
        login = http.post("/api/v1/auth/login", json={
            "username": resources.names[0], "password": "correct-password"})
        assert login.status_code == 200
        cookie = login.cookies["zhiyan_session"]
        csrf = login.json()["csrf_token"]
        upload = _post_valid_upload_once(resources, http, deadline,
            case=scenario, stage="survivor", cookies={"zhiyan_session": cookie},
            headers={"X-CSRF-Token": csrf},
            files={"files": ("survivor.txt", content)})
        assert upload.status_code == 202 and len(upload.json()["tasks"]) == 1
        task = upload.json()["tasks"][0]
        public_before = _death_public_state(resources, user)
        target = {"user_id": user, "task_id": str(task["task_id"]),
                  "worker_id": old_worker_id}
        old = _launch_role(resources, role="worker", case=scenario,
                           worker_id=old_worker_id, fault=scenario, target=target)
        owners.append(("worker-original", old))
        old_identity = _ready_worker(old, deadline)
        assert old.control_identity is not None
        control = old.control_identity
        fault = _await_event(control, "fault", timeout=_real_left(deadline, 60))
        started = _await_event(control, "heartbeat_started",
                               timeout=_real_left(deadline, 60))
        assert (fault["pid"] == started["pid"] == old_identity["pid"]
                and started["worker_id"] == old_worker_id
                and started["task_id"] == str(task["task_id"])
                and fault["pid"] != api_identity["pid"])
        if scenario == "stale_authority":
            assert (fault["worker_id"] == old_worker_id
                    and fault["authority_hit"] == 1
                    and fault["tuple_sha256"] == started["tuple_sha256"])
        else:
            admitted = _await_event(control, "candidate_admitted",
                                    timeout=_real_left(deadline, 60))
            assert (admitted["pid"] == fault["pid"]
                    and admitted["task_id"] == str(task["task_id"])
                    and admitted["user_id"] == user
                    and admitted["generation_id"] == fault["generation_id"]
                    and admitted["point_count"] == fault["point_count"] == 1)
        checkpoint = _survivor_checkpoint(resources, scenario, user, task,
                                          content, old_worker_id)
        if scenario == "late_candidate_bytes":
            from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
            frozen = PostgresImportPublicationEvidenceRepository(resources.db).read_exact(
                user, str(task["task_id"]), checkpoint["task"]["lease_version"])
            assert frozen is not None
            rag_intent = frozen.intent["scopes"]["rag"]
            assert (admitted["generation_id"] == checkpoint["candidates"]["rag"]
                    and admitted["intent_hash"] == frozen.intent_hash
                    and admitted["collection"] ==
                        rag_intent["identity"]["physical_collection"]
                    and admitted["namespace"] == rag_intent["namespace"]
                    and admitted["index_key"] == rag_intent["index_key"])
        assert _death_public_state(resources, user) == public_before
        heartbeat_end = _await_event(control, "heartbeat_finally",
                                     timeout=_real_left(deadline, 90))
        heartbeat_exited = _await_event(control, "heartbeat_thread_exited",
                                       timeout=_real_left(deadline, 60))
        old_recovery = _await_event(control, "old_recovery",
                                    timeout=_real_left(deadline, 60))
        assert (heartbeat_end["pid"] == heartbeat_exited["pid"] ==
                    old_recovery["pid"] == old_identity["pid"]
                and heartbeat_end["tuple_sha256"] == started["tuple_sha256"]
                and heartbeat_exited["worker_id"] == old_worker_id
                and old_recovery["worker_id"] == old_worker_id
                and old.active_count() > 0)
        settled = _death_rows(resources, user, str(task["task_id"]))
        assert (settled["task"]["lease_token"] == checkpoint["task"]["lease_token"]
                and settled["ordinary"]["lease_token"] ==
                    checkpoint["ordinary"]["lease_token"]
                and settled["task"]["lease_version"] ==
                    checkpoint["task"]["lease_version"])
        _await_death_expiry(resources, user, str(task["task_id"]), settled, deadline)
        fresh = _launch_role(resources, role="worker", case=scenario,
                             worker_id=fresh_worker_id)
        owners.append(("worker-replacement", fresh))
        fresh_identity = _ready_worker(fresh, deadline)
        assert fresh_identity["pid"] not in {old_identity["pid"], api_identity["pid"]}
        _wait_task(http, str(task["task_id"]), cookie, deadline)
        final = _survivor_final(resources, scenario, user, task,
                                checkpoint, fresh_worker_id)
        assert _death_public_state(resources, user) != public_before
        revoked = (None if scenario == "stale_authority" else
                   _assert_death_revoked_candidates(checkpoint, final))
        _assert_published(resources, user, task, content,
                          content.decode("utf-8"), fresh_worker_id,
                          frozenset((user,)), revoked_candidates=revoked)
        published = _death_public_state(resources, user)
        _atomic_json(resources.directory / f"{scenario}-survivor-checkpoint.json", {
            "scenario": scenario, "task_id": str(task["task_id"]),
            "source_sha256": checkpoint["source"]["sha256"],
            "old_runtime_pid": old_identity["pid"],
            "replacement_runtime_pid": fresh_identity["pid"],
            "old_lease_version": checkpoint["task"]["lease_version"],
            "final_lease_version": final["task"]["lease_version"],
            "old_candidate_ids": checkpoint["candidates"],
            "fresh_worker_id": fresh_worker_id})
        assert old.active_count() > 0
        _release(control, "fault")
        if scenario == "stale_authority":
            refused = _await_event(control, "original_exception",
                                   timeout=_real_left(deadline, 30))
            assert (refused["pid"] == old_identity["pid"]
                    and refused["exception"] == "ImportLeaseLost"
                    and refused["tuple_sha256"] == started["tuple_sha256"]
                    and not control.event_path("original_returned", 1).exists())
        else:
            written = _await_event(control, "original_returned",
                                   timeout=_real_left(deadline, 30))
            rejected = _await_event(control, "post_late_rejected",
                                    timeout=_real_left(deadline, 30))
            assert (written["pid"] == rejected["pid"] == old_identity["pid"]
                    and written["status"] == "completed"
                    and rejected["generation_id"] == checkpoint["candidates"]["rag"]
                    and rejected["exception"] == "GenerationVectorStoreError"
                    and not control.event_path("original_returned", 2).exists()
                    and not control.event_path("candidate_admitted", 2).exists()
                    and not control.event_path("post_late_returned", 1).exists())
            _assert_late_readback(resources, user, checkpoint, final, admitted,
                                  content)
        after_fault = _death_rows(resources, user, str(task["task_id"]),
                                  evidence_version=checkpoint["task"]["lease_version"])
        assert ({key: value for key, value in after_fault.items() if key != "clock"} ==
                {key: value for key, value in final.items() if key != "clock"}
                and _death_public_state(resources, user) == published)
        _release(control, "old_recovery")
        recovery_done = _await_event(control, "old_recovery_returned",
                                     timeout=_real_left(deadline, 30))
        assert (recovery_done["pid"] == old_identity["pid"]
                and recovery_done["outcome"] == "NoneType")
        assert _death_public_state(resources, user) == published
        after_recovery = _death_rows(resources, user, str(task["task_id"]),
                                     evidence_version=checkpoint["task"]["lease_version"])
        assert ({key: value for key, value in after_recovery.items() if key != "clock"} ==
                {key: value for key, value in final.items() if key != "clock"})
        assert (_business_projection(resources.db, resources.excluded) == excluded_before
                and _object_projection(resources.s3, resources.settings.s3_bucket,
                                       resources.excluded) == excluded_objects_before)
        _atomic_json(resources.directory / f"{scenario}-survivor-result.json", {
            "scenario": scenario, "task_id": str(task["task_id"]),
            "old_runtime_pid": old_identity["pid"],
            "replacement_runtime_pid": fresh_identity["pid"],
            "old_recovery_outcome": recovery_done["outcome"],
            "source_sha256": checkpoint["source"]["sha256"],
            "original_candidate_ids": checkpoint["candidates"],
            "final_lease_version": final["task"]["lease_version"]})
    finally:
        release_errors = (_release_observed(old.control_identity,
                         ("fault", "old_recovery")) if old is not None
                         and old.control_identity is not None else [])
        try:
            _finish_real_case(resources, clients, owners)
        finally:
            assert not release_errors, release_errors


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_stale_authority(real_process_resources: RealProcessResources) -> None:
    _run_surviving_old_pid(real_process_resources, "stale_authority")


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_late_candidate_bytes(real_process_resources: RealProcessResources) -> None:
    _run_surviving_old_pid(real_process_resources, "late_candidate_bytes")


def _recovery_snapshot(resources: RealProcessResources, user: str,
                       task_id: str, version: int) -> dict[str, object]:
    return _death_rows(resources, user, task_id, evidence_version=version)


def _await_recovery_projection(resources: RealProcessResources, user: str,
                               task_id: str, version: int, deadline: float,
                               predicate: Callable[[dict[str, object]], bool]
                               ) -> dict[str, object]:
    while True:
        rows = _recovery_snapshot(resources, user, task_id, version)
        if predicate(rows):
            return rows
        time.sleep(min(DATA_POLL, _real_left(deadline, DATA_POLL)))


def _run_unknown_or_recovery_case(resources: RealProcessResources,
                                  scenario: str) -> None:
    assert scenario in {"unknown_document_put", "unknown_terminal_reply",
                        "pause_live_capture", "pause_expired_capture",
                        "recovery_natural_retry"}
    from app.import_publication_proof import ImportPublicationProofService
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository

    user = resources.users[0]
    deadline = _real_deadline()
    old_worker_id = "p6-old-" + uuid4().hex
    fresh_worker_id = "p6-fresh-" + uuid4().hex
    content = f"Persistent recovery evidence for {scenario}.".encode("utf-8")
    owners: list[tuple[str, OwnedJob]] = []
    clients: list[httpx.Client] = []
    old: OwnedJob | None = None
    excluded_before = _business_projection(resources.db, resources.excluded)
    excluded_objects_before = _object_projection(
        resources.s3, resources.settings.s3_bucket, resources.excluded)
    public_before = _death_public_state(resources, user)
    try:
        port = _fresh_port()
        api = _launch_role(resources, role="api", case=scenario, port=port)
        owners.append(("api", api))
        http, api_identity = _ready_api(api, port, deadline)
        clients.append(http)
        login = http.post("/api/v1/auth/login", json={
            "username": resources.names[0], "password": "correct-password"})
        assert login.status_code == 200
        cookie = login.cookies["zhiyan_session"]
        csrf = login.json()["csrf_token"]
        upload = _post_valid_upload_once(
            resources, http, deadline, case=scenario, stage="recovery",
            cookies={"zhiyan_session": cookie},
            headers={"X-CSRF-Token": csrf},
            files={"files": ("recovery.txt", content)})
        assert upload.status_code == 202 and len(upload.json()["tasks"]) == 1
        task = upload.json()["tasks"][0]
        task_id = str(task["task_id"])
        target = {"user_id": user, "task_id": task_id,
                  "worker_id": old_worker_id}
        old = _launch_role(resources, role="worker", case=scenario,
                           worker_id=old_worker_id, fault=scenario, target=target)
        owners.append(("worker-original", old))
        old_identity = _ready_worker(old, deadline)
        assert old.control_identity is not None
        control = old.control_identity
        fault = _await_event(control, "fault", timeout=_real_left(deadline, 60))
        heartbeat = _await_event(
            control, "heartbeat_finally", timeout=_real_left(deadline, 60))
        assert (fault["pid"] == heartbeat["pid"] == old_identity["pid"]
                and old_identity["pid"] != api_identity["pid"]
                and fault["task_id"] == task_id
                and old.active_count() > 0)
        if scenario in {"pause_live_capture", "pause_expired_capture",
                        "recovery_natural_retry"}:
            first_round = _await_event(
                control, "initial_recovery", timeout=_real_left(deadline, 60))
            assert first_round["pid"] == old_identity["pid"]
        terminal = scenario != "unknown_document_put"
        surrogate = ("terminal_commit_before_return" if terminal
                     else "document_bytes_before_evidence")
        checkpoint = _assert_death_checkpoint(
            resources, surrogate, user, task, content, old_worker_id, fault)
        original_version = checkpoint["task"]["lease_version"]
        assert (checkpoint["gate"] == {"status": "unresolved"}
                and checkpoint["queue"]["state"] == "pending"
                and checkpoint["queue"]["reason_code"] == "unknown"
                and checkpoint["queue"]["claim_token"] is None
                and checkpoint["recovery"] is None
                and not checkpoint["issuance"]
                and _business_projection(resources.db, resources.excluded) ==
                    excluded_before)
        if terminal:
            original_proof = ImportPublicationProofService(resources.db).prove_exact(
                user, task_id, original_version)
            assert original_proof is not None
            proof_identity = _death_proof_identity(original_proof)
            terminal_publication = _death_public_state(resources, user)
            assert terminal_publication != public_before
        else:
            assert _death_public_state(resources, user) == public_before
            assert ImportPublicationProofService(resources.db).prove_exact(
                user, task_id, original_version) is None
        _atomic_json(resources.directory / f"{scenario}-recovery-checkpoint.json", {
            "scenario": scenario, "task_id": task_id,
            "source_sha256": checkpoint["source"]["sha256"],
            "old_runtime_pid": old_identity["pid"],
            "old_lease_version": original_version,
            "initial_phase": checkpoint["evidence"]["phase"],
            "candidate_ids": checkpoint["candidates"]})

        if scenario in {"unknown_document_put", "unknown_terminal_reply"}:
            joined = _await_event(control, "heartbeat_thread_exited",
                                  timeout=_real_left(deadline, 60))
            paused = _await_event(control, "old_recovery",
                                  timeout=_real_left(deadline, 60))
            assert (joined["pid"] == paused["pid"] == old_identity["pid"]
                    and old.active_count() > 0)
            if not terminal:
                _await_death_expiry(resources, user, task_id, checkpoint, deadline)
            deferred = _await_event(control, "ordinary_deferred",
                                    timeout=_real_left(deadline, 60))
            assert (deferred["pid"] == old_identity["pid"]
                    and deferred["worker_id"] == old_worker_id
                    and old.active_count() > 0
                    and not control.event_path("heartbeat_started", 2).exists())
            fresh = _launch_role(resources, role="worker", case=scenario,
                                 worker_id=fresh_worker_id)
            owners.append(("worker-replacement", fresh))
            fresh_identity = _ready_worker(fresh, deadline)
            assert fresh_identity["pid"] not in {
                api_identity["pid"], old_identity["pid"]}
            _wait_task(http, task_id, cookie, deadline)
            final = _await_recovery_projection(
                resources, user, task_id, original_version, deadline,
                lambda rows: rows["evidence"] is not None and
                rows["evidence"]["phase"] ==
                    ("proved_succeeded" if terminal else "abandoned")
                and rows["queue"]["state"] == "resolved")
            _atomic_json(resources.directory / f"{scenario}-recovery-observation.json", {
                "scenario": scenario,
                "task_status": final["task"]["status"],
                "task_lease_version": final["task"]["lease_version"],
                "task_worker_id": final["task"]["claimed_by"],
                "audits": [{"lease_version": row["lease_version"],
                            "worker_id": row["worker_id"],
                            "end_reason": row["end_reason"],
                            "ended": row["ended_at"] is not None}
                           for row in final["audits"]],
                "old_ordinary_deferred": deferred["worker_id"] == old_worker_id,
                "old_second_heartbeat_seen": control.event_path(
                    "heartbeat_started", 2).exists()})
            _assert_death_recovery_capture(
                final, user=user, task_id=task_id, old_version=original_version,
                worker_id=fresh_worker_id,
                reason="proved_succeeded" if terminal else "abandoned")
            if terminal:
                assert (len(final["audits"]) == 1
                        and final["task"]["lease_version"] == original_version
                        and _death_proof_identity(
                            ImportPublicationProofService(resources.db).prove_exact(
                                user, task_id, original_version)) == proof_identity
                        and _death_public_state(resources, user) ==
                            terminal_publication)
                _assert_published(resources, user, task, content,
                                  content.decode("utf-8"), old_worker_id,
                                  frozenset((user,)))
            else:
                assert len(final["audits"]) >= 2
                assert final["audits"][0]["end_reason"] == "lease_expired"
                assert final["audits"][0]["ended_at"] is not None
                assert final["audits"][-1]["worker_id"] == fresh_worker_id
                assert (final["audits"][-1]["lease_version"] ==
                            final["task"]["lease_version"]
                        and final["audits"][-1]["lease_token"] ==
                            final["task"]["lease_token"]
                        and final["audits"][-1]["user_lease_token"] ==
                            final["task"]["user_lease_token"]
                        and final["audits"][-1]["user_lease_version"] ==
                            final["task"]["user_lease_version"]
                        and final["audits"][-1]["end_reason"] == "succeeded"
                        and final["audits"][-1]["ended_at"] is not None)
                assert (final["audits"][-1]["lease_version"] >
                            final["audits"][0]["lease_version"]
                        and final["audits"][-1]["lease_token"] !=
                            final["audits"][0]["lease_token"]
                        and final["audits"][-1]["user_lease_token"] !=
                            final["audits"][0]["user_lease_token"]
                        and final["audits"][-1]["user_lease_version"] >
                            final["audits"][0]["user_lease_version"])
                assert (final["task"]["lease_version"] > original_version
                        and final["task"]["lease_token"] !=
                            checkpoint["task"]["lease_token"]
                        and final["task"]["user_lease_token"] !=
                            checkpoint["task"]["user_lease_token"]
                        and final["task"]["user_lease_version"] >
                            checkpoint["task"]["user_lease_version"])
                fresh_evidence = PostgresImportPublicationEvidenceRepository(
                    resources.db).read_exact(
                        user, task_id, final["task"]["lease_version"])
                assert (fresh_evidence is not None
                        and fresh_evidence.phase in {
                            "terminal_committed", "proved_succeeded"}
                        and {fresh_evidence.intent["scopes"][kind]["candidate_id"]
                             for kind in ("rag", "episode")}.isdisjoint(
                                 set(checkpoint["candidates"].values())))
                revoked = _assert_death_revoked_candidates(checkpoint, final)
                _assert_published(resources, user, task, content,
                                  content.decode("utf-8"), fresh_worker_id,
                                  frozenset((user,)),
                                  revoked_candidates=revoked)
            published = _death_public_state(resources, user)
            _release(control, "old_recovery")
            old_done = _await_event(control, "old_recovery_returned",
                                    timeout=_real_left(deadline, 30))
            after_old = _recovery_snapshot(
                resources, user, task_id, original_version)
            assert (old_done["pid"] == old_identity["pid"]
                    and old_done["outcome"] == "NoneType"
                    and not control.event_path("heartbeat_started", 2).exists()
                    and {key: value for key, value in after_old.items()
                         if key != "clock"} ==
                        {key: value for key, value in final.items()
                         if key != "clock"}
                    and _death_public_state(resources, user) == published)
            replacement_pid = fresh_identity["pid"]
            old_outcome = old_done["outcome"]
        else:
            replacement_pid = None
            old_outcome = None
            _release(control, "initial_recovery")
            if scenario in {"pause_live_capture", "pause_expired_capture"}:
                marker = _await_event(
                    control, "recovery_pause", timeout=_real_left(deadline, 60))
                claimed = _recovery_snapshot(
                    resources, user, task_id, original_version)
                queue, recovery = claimed["queue"], claimed["recovery"]
                assert (marker["pid"] == old_identity["pid"]
                        and queue["state"] == "claimed"
                        and queue["queue_version"] == marker["queue_version"]
                        and recovery["version"] == marker["recovery_version"]
                        and recovery["owner"] == old_worker_id
                        and queue["claim_token"] == recovery["queue_claim_token"]
                        and queue["claim_expires_at"] ==
                            recovery["expires_at"]
                        and marker["capture_sha256"] == _digest((
                            queue["claim_token"], recovery["token"],
                            queue["claim_expires_at"], recovery["expires_at"]))
                        and len(claimed["issuance"]) == 2
                        and {(row["kind"], row["token"], row["issued_version"])
                             for row in claimed["issuance"]} == {
                            ("queue", queue["claim_token"], queue["queue_version"]),
                            ("recovery", recovery["token"], recovery["version"])})
                captured_projection = {
                    key: value for key, value in claimed.items() if key != "clock"}
                if scenario == "pause_live_capture":
                    assert (time.monotonic() - marker["at_monotonic"] < 3
                            and queue["claim_expires_at"] > claimed["clock"]
                            and recovery["expires_at"] > claimed["clock"])
                    margins = _recovery_snapshot(
                        resources, user, task_id, original_version)
                    assert (margins["queue"]["claim_expires_at"] >
                            margins["clock"]
                            and margins["recovery"]["expires_at"] >
                                margins["clock"])
                    assert time.monotonic() - marker["at_monotonic"] < 3
                    _release(control, "recovery_pause")
                    resumed = _await_event(control, "recovery_returned",
                                           timeout=_real_left(deadline, 30))
                    assert (resumed["pid"] == old_identity["pid"]
                            and resumed["outcome"] == "proved_succeeded")
                    final = _await_recovery_projection(
                        resources, user, task_id, original_version, deadline,
                        lambda rows: rows["evidence"]["phase"] ==
                            "proved_succeeded" and
                            rows["queue"]["state"] == "resolved")
                    _assert_death_recovery_capture(
                        final, user=user, task_id=task_id,
                        old_version=original_version, worker_id=old_worker_id,
                        reason="proved_succeeded")
                    old_outcome = resumed["outcome"]
                else:
                    while True:
                        expired = _recovery_snapshot(
                            resources, user, task_id, original_version)
                        assert ({key: value for key, value in expired.items()
                                 if key != "clock"} == captured_projection)
                        if (expired["queue"]["claim_expires_at"] <=
                                expired["clock"] and
                                expired["recovery"]["expires_at"] <=
                                    expired["clock"]):
                            break
                        time.sleep(min(DATA_POLL, _real_left(deadline, DATA_POLL)))
                    _release(control, "recovery_pause")
                    refused = _await_event(control, "recovery_exception",
                                           timeout=_real_left(deadline, 30))
                    refused_barrier = _await_event(
                        control, "refused_recovery",
                        timeout=_real_left(deadline, 30))
                    assert (refused["pid"] == old_identity["pid"]
                            and refused_barrier["pid"] == old_identity["pid"]
                            and refused["exception"] == "RecoveryLeaseLost"
                            and old.active_count() > 0)
                    after_refusal = _recovery_snapshot(
                        resources, user, task_id, original_version)
                    assert ({key: value for key, value in after_refusal.items()
                             if key != "clock"} == captured_projection)
                    fresh = _launch_role(resources, role="worker", case=scenario,
                                         worker_id=fresh_worker_id)
                    owners.append(("worker-replacement", fresh))
                    fresh_identity = _ready_worker(fresh, deadline)
                    replacement_pid = fresh_identity["pid"]
                    assert replacement_pid not in {
                        api_identity["pid"], old_identity["pid"]}
                    final = _await_recovery_projection(
                        resources, user, task_id, original_version, deadline,
                        lambda rows: rows["evidence"]["phase"] ==
                            "proved_succeeded" and
                            rows["queue"]["state"] == "resolved")
                    _assert_death_recovery_capture(
                        final, user=user, task_id=task_id,
                        old_version=original_version, worker_id=fresh_worker_id,
                        reason="proved_succeeded",
                        previous_issuance=tuple(claimed["issuance"]))
                    published = _death_public_state(resources, user)
                    _release(control, "refused_recovery")
                    assert _death_public_state(resources, user) == published
                    old_outcome = refused["exception"]
            else:
                proof_returned = _await_event(
                    control, "proof_original_returned",
                    timeout=_real_left(deadline, 60))
                unknown = _await_event(
                    control, "proof_unknown", timeout=_real_left(deadline, 60))
                recorded = _await_event(
                    control, "transient_recorded",
                    timeout=_real_left(deadline, 60))
                assert (proof_returned["pid"] == unknown["pid"] ==
                        recorded["pid"] == old_identity["pid"]
                        and proof_returned["original_proof"] ==
                            "DetachedImportMemoryPublication"
                        and recorded["outcome"] == "retry_later")
                transient = _await_recovery_projection(
                    resources, user, task_id, original_version, deadline,
                    lambda rows: rows["queue"]["state"] == "pending" and
                    rows["queue"]["transient_count"] == 1)
                due = transient["queue"]["due_at"]
                assert (transient["queue"]["reason_code"] == "transient"
                        and transient["queue"]["claim_token"] is None
                        and due is not None
                        and datetime.fromisoformat(
                            recorded["before_clock"]) + timedelta(seconds=5)
                            <= due <= datetime.fromisoformat(
                                recorded["after_clock"]) + timedelta(seconds=5)
                        and due > transient["clock"]
                        and len(transient["issuance"]) == 2
                        and transient["recovery"]["expires_at"] <=
                            transient["clock"])
                prior_issuance = transient["issuance"]
                while True:
                    current = _recovery_snapshot(
                        resources, user, task_id, original_version)
                    if current["clock"] >= due:
                        break
                    assert (current["queue"]["state"] == "pending"
                            and current["queue"]["due_at"] == due
                            and current["queue"]["claim_token"] is None
                            and current["queue"]["transient_count"] == 1
                            and current["gate"] == {"status": "unresolved"}
                            and current["evidence"] == checkpoint["evidence"]
                            and current["issuance"] == prior_issuance
                            and _death_proof_identity(
                                ImportPublicationProofService(resources.db).prove_exact(
                                    user, task_id, original_version)) ==
                                proof_identity
                            and _death_public_state(resources, user) ==
                                terminal_publication)
                    time.sleep(min(DATA_POLL, _real_left(deadline, DATA_POLL)))
                final = _await_recovery_projection(
                    resources, user, task_id, original_version, deadline,
                    lambda rows: rows["evidence"]["phase"] ==
                        "proved_succeeded" and
                        rows["queue"]["state"] == "resolved")
                _assert_death_recovery_capture(
                    final, user=user, task_id=task_id,
                    old_version=original_version, worker_id=old_worker_id,
                    reason="proved_succeeded",
                    previous_issuance=tuple(prior_issuance))
                assert (final["queue"]["transient_count"] == 1
                        and len(final["issuance"]) == 4)
                old_outcome = "retry_later_then_proved_succeeded"
            assert (len(final["audits"]) == 1
                    and final["task"]["lease_version"] == original_version
                    and _death_proof_identity(
                        ImportPublicationProofService(resources.db).prove_exact(
                            user, task_id, original_version)) == proof_identity
                    and _death_public_state(resources, user) ==
                        terminal_publication)
            _assert_published(resources, user, task, content,
                              content.decode("utf-8"), old_worker_id,
                              frozenset((user,)))

        assert (final["source"] == checkpoint["source"]
                and _business_projection(resources.db, resources.excluded) ==
                excluded_before and
                _object_projection(resources.s3, resources.settings.s3_bucket,
                                   resources.excluded) == excluded_objects_before)
        _atomic_json(resources.directory / f"{scenario}-recovery-result.json", {
            "scenario": scenario, "task_id": task_id,
            "source_sha256": checkpoint["source"]["sha256"],
            "old_runtime_pid": old_identity["pid"],
            "replacement_runtime_pid": replacement_pid,
            "old_recovery_outcome": old_outcome,
            "original_phase": checkpoint["evidence"]["phase"],
            "final_phase": final["evidence"]["phase"],
            "final_lease_version": final["task"]["lease_version"]})
    finally:
        release_errors = (_release_observed(old.control_identity,
                         ("old_recovery", "initial_recovery", "recovery_pause",
                          "refused_recovery"))
                         if old is not None
                         and old.control_identity is not None else [])
        try:
            _finish_real_case(resources, clients, owners)
        finally:
            assert not release_errors, release_errors


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
@pytest.mark.parametrize("scenario", ("unknown_document_put",
                                      "unknown_terminal_reply"))
def test_real_unknown_held(real_process_resources: RealProcessResources,
                           scenario: str) -> None:
    _run_unknown_or_recovery_case(real_process_resources, scenario)


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
@pytest.mark.parametrize("scenario", ("pause_live_capture",
                                      "pause_expired_capture"))
def test_real_recovery_pause(real_process_resources: RealProcessResources,
                             scenario: str) -> None:
    _run_unknown_or_recovery_case(real_process_resources, scenario)


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_recovery_natural_retry(
        real_process_resources: RealProcessResources) -> None:
    _run_unknown_or_recovery_case(real_process_resources, "recovery_natural_retry")


def _run_recovery_lock_wait(resources: RealProcessResources, scenario: str) -> None:
    """Expire an original captured authority behind an actual users-row wait."""
    assert scenario in {"recovery_ack_after_wait", "recovery_abandon_after_wait"}
    from app.import_publication_proof import ImportPublicationProofService

    user = resources.users[0]
    excluded_before = _business_projection(resources.db, resources.excluded)
    objects_before = _object_projection(resources.s3, resources.settings.s3_bucket,
                                        resources.excluded)
    setup_fault = ("terminal_commit_before_return" if scenario ==
                   "recovery_ack_after_wait" else "pair_evidence_committed")
    entry_stage = ("ack_enter" if scenario == "recovery_ack_after_wait"
                   else "abandon_enter")
    operation = ("recovery.ack_success" if scenario ==
                 "recovery_ack_after_wait" else "recovery.abandon_exact")
    outcome = ("proved_succeeded" if scenario == "recovery_ack_after_wait"
               else "abandoned")
    with _lock_case(resources, scenario) as case:
        task, content, source, source_pid, checkpoint = _lock_checkpoint(
            case, setup_fault)
        task_id = str(task["task_id"])
        version = checkpoint["task"]["lease_version"]
        if scenario == "recovery_abandon_after_wait":
            _await_death_expiry(resources, user, task_id, checkpoint,
                                case.deadline)
        else:
            terminal_proof = ImportPublicationProofService(resources.db).prove_exact(
                user, task_id, version)
            assert terminal_proof is not None
            proof_identity = _death_proof_identity(terminal_proof)
        public_before_wait = _death_public_state(resources, user)
        old_worker_id = "p6-lock-old-" + uuid4().hex
        old, old_pid = _lock_worker(case, "lock-old-recovery", old_worker_id,
            fault=scenario, user_id=user, task_id=task_id)
        assert old.control_identity is not None
        identity = old.control_identity
        entry = _await_event(identity, entry_stage,
                             timeout=_real_left(case.deadline, 60))
        assert (entry["pid"] == old_pid["pid"]
                and entry["operation"] == operation
                and entry["user_id"] == user
                and entry["task_id"] == task_id)
        original = _death_rows(resources, user, task_id,
                               evidence_version=version)
        assert (original["queue"] is not None
                and original["queue"]["state"] == "claimed"
                and original["queue"]["claim_token"] is not None
                and original["recovery"] is not None
                and original["recovery"]["owner"] == old_worker_id
                and original["recovery"]["queue_claim_token"] ==
                    original["queue"]["claim_token"]
                and len(original["issuance"]) == 2
                and original["gate"] == {"status": "unresolved"}
                and original["evidence"]["phase"] ==
                    ("terminal_committed" if outcome == "proved_succeeded"
                     else "pair_sealed"))
        assert (original["queue"]["claim_expires_at"] > original["clock"]
                and original["recovery"]["expires_at"] > original["clock"])
        fresh_worker_id = "p6-lock-fresh-" + uuid4().hex
        with _scoped_import_row_lock(resources, table="users",
                                     user_id=user) as blocker:
            _release(identity, entry_stage)
            wait_proof = _prove_pg_lock_wait(resources, identity, operation=operation,
                invocation_id=entry["invocation_id"], ordinal=1,
                blocker_pid=blocker, child_pid=old_pid["pid"],
                deadline=case.deadline)
            waiting_live = _death_rows(resources, user, task_id,
                                       evidence_version=version)
            assert (_lock_stable_rows(waiting_live) ==
                    _lock_stable_rows(original)
                    and waiting_live["queue"]["claim_expires_at"] ==
                        original["queue"]["claim_expires_at"]
                    and waiting_live["recovery"]["expires_at"] ==
                        original["recovery"]["expires_at"]
                    and waiting_live["queue"]["claim_expires_at"] >
                        waiting_live["clock"]
                    and waiting_live["recovery"]["expires_at"] >
                        waiting_live["clock"])
            expired = _await_exact_recovery_expiry(
                case, user, task_id, version, original)
            assert (expired["queue"]["claim_expires_at"] <= expired["clock"]
                    and expired["recovery"]["expires_at"] <= expired["clock"]
                    and _lock_stable_rows(expired) == _lock_stable_rows(original)
                    and _death_public_state(resources, user) ==
                        public_before_wait)
            _assert_lock_excluded(case, excluded_before, objects_before)
        refused = _await_event(identity, "lock_exception",
                               timeout=_real_left(case.deadline, 30))
        assert (refused["pid"] == old_pid["pid"]
                and refused["operation"] == operation
                and refused["invocation_id"] == entry["invocation_id"]
                and refused["exception"] == "RecoveryLeaseLost"
                and wait_proof["target_backend_pid"] > 0
                and not identity.event_path("lock_returned", 1).exists())
        _await_event(identity, "post_refusal_recovery",
                     timeout=_real_left(case.deadline, 30))
        after_refusal = _death_rows(resources, user, task_id,
                                    evidence_version=version)
        assert (_lock_stable_rows(after_refusal) == _lock_stable_rows(expired)
                and _death_public_state(resources, user) == public_before_wait)
        _retire_role(resources, old, _real_left(case.deadline, JOB_EXIT_SECONDS),
                     "lock-old-recovery")
        case.controls.remove(identity)
        fresh, fresh_pid = _lock_worker(
            case, "lock-fresh-recovery", fresh_worker_id,
            fault=scenario if outcome == "abandoned" else None,
            user_id=user, task_id=task_id,
            lock_role="fresh" if outcome == "abandoned" else None)
        assert fresh_pid["pid"] not in {
            case.api_pid, source_pid["pid"], old_pid["pid"]}
        final = _lock_recovery_rows(case, user, task_id, version,
            lambda rows: rows["evidence"] is not None
            and rows["evidence"]["phase"] == outcome
            and rows["queue"] is not None
            and rows["queue"]["state"] == "resolved")
        _assert_death_recovery_capture(final, user=user, task_id=task_id,
            old_version=version, worker_id=fresh_worker_id, reason=outcome,
            previous_issuance=tuple(original["issuance"]))
        assert (final["evidence"]["phase_version"] ==
                original["evidence"]["phase_version"] + 1
                and final["gate"] == {"status": "resolved"})
        if outcome == "proved_succeeded":
            assert (_death_proof_identity(
                ImportPublicationProofService(resources.db).prove_exact(
                    user, task_id, version)) == proof_identity
                    and _death_public_state(resources, user) ==
                        public_before_wait)
            _assert_published(resources, user, task, content,
                content.decode("utf-8"), checkpoint["task"]["claimed_by"],
                frozenset((user,)))
        else:
            revoked = _assert_death_revoked_candidates(checkpoint, final)
            assert (_death_public_state(resources, user) == public_before_wait
                    and final["task"]["status"] in
                        {"retry_wait", "queued", "running"})
            assert fresh.control_identity is not None
            fresh_gate = _await_event(fresh.control_identity,
                                      "fresh_ordinary_enter",
                                      timeout=_real_left(case.deadline, 30))
            assert (fresh_gate["pid"] == fresh_pid["pid"]
                    and fresh_gate["worker_id"] == fresh_worker_id)
            _release(fresh.control_identity, "fresh_ordinary_enter")
            _wait_task(case.http, task_id, case.sessions[0][0], case.deadline)
            _assert_published(resources, user, task, content,
                content.decode("utf-8"), fresh_worker_id,
                frozenset((user,)), revoked_candidates=revoked)
        _assert_lock_excluded(case, excluded_before, objects_before)
        _atomic_json(resources.directory / f"{scenario}-lock-result.json", {
            "scenario": scenario, "task_id": task_id,
            "old_runtime_pid": old_pid["pid"],
            "fresh_runtime_pid": fresh_pid["pid"],
            "old_queue_version": original["queue"]["queue_version"],
            "new_queue_version": final["recovery"]["queue_version"],
            "old_recovery_version": original["recovery"]["version"],
            "new_recovery_version": final["recovery"]["version"],
            "old_exception": refused["exception"],
            "final_phase": final["evidence"]["phase"],
            "lock_invocation_id": entry["invocation_id"],
            "lock_target_backend_pid": wait_proof["target_backend_pid"],
            "lock_blocker_backend_pid": wait_proof["blocker_backend_pid"],
            "original_queue_expiry": original["queue"]["claim_expires_at"].isoformat(),
            "original_recovery_expiry": original["recovery"]["expires_at"].isoformat(),
            "entry_clock": original["clock"].isoformat(),
            "waiting_lock_clock": waiting_live["clock"].isoformat(),
            "expired_clock": expired["clock"].isoformat(),
            "after_refusal_clock": after_refusal["clock"].isoformat(),
            "original_projection_sha256": _digest(_lock_stable_rows(original)),
            "waiting_projection_sha256": _digest(_lock_stable_rows(waiting_live)),
            "expired_projection_sha256": _digest(_lock_stable_rows(expired)),
            "after_refusal_projection_sha256": _digest(
                _lock_stable_rows(after_refusal))})


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
@pytest.mark.parametrize("scenario", ("recovery_ack_after_wait",
                                      "recovery_abandon_after_wait"))
def test_real_recovery_lock_wait(real_process_resources: RealProcessResources,
                                 scenario: str) -> None:
    _run_recovery_lock_wait(real_process_resources, scenario)


def _read_lock_expiry_clock(resources: RealProcessResources, user: str,
                            task_id: str, version: int) -> dict[str, object]:
    """Use only ordinary reads while the parent owns the task-row lock."""
    with resources.db.transaction() as cursor:
        row = cursor.execute("select t.id as task_id,t.user_id,t.status,"
            "t.claimed_by,t.lease_token,t.lease_version,t.user_lease_token,"
            "t.user_lease_version,t.lease_expires_at as task_expiry,"
            "u.user_id as ordinary_user_id,u.owner,"
            "u.lease_token as ordinary_token,"
            "u.lease_version as ordinary_version,"
            "u.lease_expires_at as user_expiry,"
            "a.task_id as audit_task_id,a.user_id as audit_user_id,"
            "a.worker_id as audit_worker_id,a.lease_token as audit_token,"
            "a.lease_version as audit_version,"
            "a.user_lease_token as audit_user_token,"
            "a.user_lease_version as audit_user_version,"
            "a.heartbeat_at,a.ended_at "
            "from import_tasks t join user_mutation_leases u on u.user_id=t.user_id "
            "join import_task_attempts a on a.task_id=t.id and "
            "a.lease_version=t.lease_version where t.id=%s and t.user_id=%s "
            "and t.lease_version=%s", (task_id, user, version)).fetchone()
        clock = cursor.execute("select clock_timestamp() as now").fetchone()["now"]
    assert row is not None
    return dict(row) | {"clock": clock}


def _assert_lock_expiry_original_live(rows: dict[str, object],
                                      checkpoint: dict[str, object],
                                      user: str, task_id: str,
                                      version: int) -> None:
    """Keep raw lease tokens in memory; failure output uses a fixed safe label."""
    task = checkpoint["task"]
    ordinary = checkpoint["ordinary"]
    audit = checkpoint["audits"][0]
    if not all((
            rows["task_id"] == rows["audit_task_id"] == task_id,
            rows["user_id"] == rows["ordinary_user_id"] ==
                rows["audit_user_id"] == user,
            rows["status"] == "running" and rows["ended_at"] is None,
            rows["claimed_by"] == rows["owner"] ==
                rows["audit_worker_id"] == task["claimed_by"],
            rows["lease_token"] == rows["audit_token"] == task["lease_token"],
            rows["lease_version"] == rows["audit_version"] == version,
            rows["user_lease_token"] == rows["ordinary_token"] ==
                rows["audit_user_token"] == task["user_lease_token"] ==
                ordinary["lease_token"],
            rows["user_lease_version"] == rows["ordinary_version"] ==
                rows["audit_user_version"] == task["user_lease_version"] ==
                ordinary["lease_version"],
            rows["task_expiry"] == task["lease_expires_at"],
            rows["user_expiry"] == ordinary["lease_expires_at"],
            rows["heartbeat_at"] == audit["heartbeat_at"],
    )):
        raise AssertionError("expiry_original_authority_drift")


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_heartbeat_terminal_after_wait(
        real_process_resources: RealProcessResources) -> None:
    resources = real_process_resources
    scenario = "heartbeat_terminal_after_wait"
    user = resources.users[0]
    excluded_before = _business_projection(resources.db, resources.excluded)
    objects_before = _object_projection(resources.s3, resources.settings.s3_bucket,
                                        resources.excluded)
    with _lock_case(resources, scenario) as case:
        task, content, old, old_pid, checkpoint = _lock_checkpoint(
            case, "pair_evidence_committed", retire=False,
            lock_role="heartbeat_old")
        task_id = str(task["task_id"])
        version = checkpoint["task"]["lease_version"]
        assert old.control_identity is not None
        identity = old.control_identity
        started = _await_event(identity, "heartbeat_started",
                               timeout=_real_left(case.deadline, 30))
        heartbeat_entry = _await_event(identity, "heartbeat_enter",
                                       timeout=_real_left(case.deadline, 30))
        assert (started["pid"] == heartbeat_entry["pid"] == old_pid["pid"]
                and heartbeat_entry["operation"] == "lease.heartbeat"
                and heartbeat_entry["worker_id"] ==
                    checkpoint["task"]["claimed_by"]
                and heartbeat_entry["task_id"] == task_id)
        # The first heartbeat is held before its original method. The pair-v4
        # fault can now proceed through the real committing transition.
        _release(identity, "fault")
        begun = _await_event(identity, "lock_returned",
                             timeout=_real_left(case.deadline, 30))
        terminal = _await_event(identity, "terminal_enter",
                                timeout=_real_left(case.deadline, 30))
        assert (begun["pid"] == terminal["pid"] == old_pid["pid"]
                and begun["operation"] == "lease.try_begin_committing"
                and begun["result_type"] == "bool"
                and begun["result_value"] is True
                and terminal["operation"] == "lease.complete"
                and terminal["worker_id"] == checkpoint["task"]["claimed_by"]
                and begun["invocation_id"] != terminal["invocation_id"])
        at_complete = _death_rows(resources, user, task_id,
                                  evidence_version=version)
        assert (at_complete["task"]["stage"] == "committing"
                and at_complete["task"]["status"] == "running"
                and at_complete["evidence"] == checkpoint["evidence"]
                and at_complete["evidence"]["phase"] == "pair_sealed"
                and at_complete["evidence"]["phase_version"] == 4
                and at_complete["evidence"]["terminal_slot"] is None
                and at_complete["gate"] == {"status": "unresolved"}
                and at_complete["task"]["lease_expires_at"] >
                    at_complete["clock"]
                and at_complete["ordinary"]["lease_expires_at"] >
                    at_complete["clock"])
        public_before = _death_public_state(resources, user)
        with _scoped_import_row_lock(resources, table="import_tasks",
                                     user_id=user, task_id=task_id) as blocker:
            _release(identity, "terminal_enter")
            complete_wait = _prove_pg_lock_wait(resources, identity,
                operation="lease.complete",
                invocation_id=terminal["invocation_id"], ordinal=1,
                blocker_pid=blocker, child_pid=old_pid["pid"],
                deadline=case.deadline)
            _release(identity, "heartbeat_enter")
            heartbeat_wait = _prove_pg_lock_wait(resources, identity,
                operation="lease.heartbeat",
                invocation_id=heartbeat_entry["invocation_id"], ordinal=1,
                blocker_pid=complete_wait["target_backend_pid"],
                child_pid=old_pid["pid"], deadline=case.deadline)
            assert (len({blocker, complete_wait["target_backend_pid"],
                         heartbeat_wait["target_backend_pid"]}) == 3
                    and blocker in complete_wait["blocking_pids"]
                    and complete_wait["target_backend_pid"] in
                        heartbeat_wait["blocking_pids"])
            waiting = _read_lock_expiry_clock(resources, user, task_id, version)
            _assert_lock_expiry_original_live(waiting, at_complete,
                                              user, task_id, version)
            assert (waiting["task_expiry"] > waiting["clock"]
                    and waiting["user_expiry"] > waiting["clock"])
            wait_deadline = min(case.deadline, time.monotonic() + 90)
            while True:
                expired = _read_lock_expiry_clock(resources, user,
                                                  task_id, version)
                _assert_lock_expiry_original_live(expired, at_complete,
                                                  user, task_id, version)
                if (expired["task_expiry"] <= expired["clock"]
                        and expired["user_expiry"] <= expired["clock"]):
                    break
                time.sleep(min(DATA_POLL, _real_left(wait_deadline, DATA_POLL)))
            while_locked = _death_rows(resources, user, task_id,
                                       evidence_version=version)
            assert (_lock_stable_rows(while_locked) ==
                    _lock_stable_rows(at_complete)
                    and _death_public_state(resources, user) == public_before
                    and not identity.event_path("lock_returned", 2).exists())
            _assert_lock_excluded(case, excluded_before, objects_before)
        first = _await_event(identity, "lock_exception", hit=1,
                             timeout=_real_left(case.deadline, 30))
        second = _await_event(identity, "lock_exception", hit=2,
                              timeout=_real_left(case.deadline, 30))
        refused = {record["operation"]: record for record in (first, second)}
        assert (set(refused) == {"lease.complete", "lease.heartbeat"}
                and all(record["pid"] == old_pid["pid"]
                        and record["exception"] == "ImportLeaseLost"
                        for record in refused.values())
                and refused["lease.complete"]["invocation_id"] ==
                    terminal["invocation_id"]
                and refused["lease.heartbeat"]["invocation_id"] ==
                    heartbeat_entry["invocation_id"])
        heartbeat_end = _await_event(identity, "heartbeat_finally",
                                     timeout=_real_left(case.deadline, 30))
        assert (heartbeat_end["pid"] == old_pid["pid"]
                and heartbeat_end["tuple_sha256"] == started["tuple_sha256"])
        after_refusal = _death_rows(resources, user, task_id,
                                    evidence_version=version)
        assert (_lock_stable_rows(after_refusal) ==
                _lock_stable_rows(at_complete)
                and _death_public_state(resources, user) == public_before
                and after_refusal["evidence"]["terminal_slot"] is None
                and not identity.event_path("lock_returned", 2).exists())
        _retire_role(resources, old,
                     _real_left(case.deadline, JOB_EXIT_SECONDS),
                     "lock-source-0")
        case.controls.remove(identity)
        fresh_worker_id = "p6-heartbeat-fresh-" + uuid4().hex
        fresh, fresh_pid = _lock_worker(
            case, "heartbeat-fresh", fresh_worker_id, fault=scenario,
            user_id=user, task_id=task_id, lock_role="fresh")
        assert fresh_pid["pid"] not in {case.api_pid, old_pid["pid"]}
        assert fresh.control_identity is not None
        fresh_identity = fresh.control_identity
        fresh_entry = _await_event(fresh_identity, "fresh_claim_enter",
                                   timeout=_real_left(case.deadline, 60))
        assert (fresh_entry["pid"] == fresh_pid["pid"]
                and fresh_entry["worker_id"] == fresh_worker_id
                and fresh_entry["operation"] == "recovery.claim_next")
        _release(fresh_identity, "fresh_claim_enter")
        grant = _await_event(fresh_identity, "lock_returned",
                             timeout=_real_left(case.deadline, 60))
        assert (grant["pid"] == fresh_pid["pid"]
                and grant["operation"] == "recovery.claim_next"
                and grant["invocation_id"] == fresh_entry["invocation_id"]
                and grant["result_type"] == "RecoveryClaim"
                and grant["result_user_id"] == user
                and grant["result_task_id"] == task_id
                and grant["result_worker_id"] == fresh_worker_id)
        final = _lock_recovery_rows(case, user, task_id, version,
            lambda rows: rows["evidence"]["phase"] == "abandoned"
            and rows["queue"]["state"] == "resolved")
        _assert_death_recovery_capture(final, user=user, task_id=task_id,
            old_version=version, worker_id=fresh_worker_id,
            reason="abandoned")
        assert (final["evidence"]["phase_version"] ==
                checkpoint["evidence"]["phase_version"] + 1
                and final["gate"] == {"status": "resolved"}
                and _death_public_state(resources, user) == public_before)
        revoked = _assert_death_revoked_candidates(checkpoint, final)
        ordinary_gate = _await_event(fresh_identity, "fresh_ordinary_enter",
                                     timeout=_real_left(case.deadline, 30))
        assert (ordinary_gate["pid"] == fresh_pid["pid"]
                and ordinary_gate["worker_id"] == fresh_worker_id)
        _release(fresh_identity, "fresh_ordinary_enter")
        _wait_task(case.http, task_id, case.sessions[0][0], case.deadline)
        _assert_published(resources, user, task, content,
            content.decode("utf-8"), fresh_worker_id,
            frozenset((user,)), revoked_candidates=revoked)
        _assert_lock_excluded(case, excluded_before, objects_before)
        _atomic_json(resources.directory / f"{scenario}-lock-result.json", {
            "scenario": scenario, "task_id": task_id,
            "old_runtime_pid": old_pid["pid"],
            "fresh_runtime_pid": fresh_pid["pid"],
            "complete_invocation_id": terminal["invocation_id"],
            "heartbeat_invocation_id": heartbeat_entry["invocation_id"],
            "complete_backend_pid": complete_wait["target_backend_pid"],
            "heartbeat_backend_pid": heartbeat_wait["target_backend_pid"],
            "parent_blocker_backend_pid": blocker,
            "original_task_expiry": at_complete["task"]["lease_expires_at"].isoformat(),
            "original_user_expiry": at_complete["ordinary"]["lease_expires_at"].isoformat(),
            "expired_clock": expired["clock"].isoformat(),
            "complete_exception": refused["lease.complete"]["exception"],
            "heartbeat_exception": refused["lease.heartbeat"]["exception"],
            "final_phase": final["evidence"]["phase"],
            "waiting_projection_sha256": _digest(_lock_stable_rows(while_locked)),
            "after_refusal_projection_sha256": _digest(
                _lock_stable_rows(after_refusal))})


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_expiry_scan_after_task_wait(
        real_process_resources: RealProcessResources) -> None:
    resources = real_process_resources
    scenario = "expiry_scan_after_task_wait"
    user = resources.users[0]
    excluded_before = _business_projection(resources.db, resources.excluded)
    objects_before = _object_projection(resources.s3, resources.settings.s3_bucket,
                                        resources.excluded)
    with _lock_case(resources, scenario) as case:
        task, content, old, old_pid, checkpoint = _lock_checkpoint(
            case, "pair_evidence_committed", retire=False,
            lock_role="expiry_old")
        task_id = str(task["task_id"])
        version = checkpoint["task"]["lease_version"]
        assert old.control_identity is not None
        old_identity = old.control_identity
        started = _await_event(old_identity, "heartbeat_started",
                               timeout=_real_left(case.deadline, 30))
        heartbeat_entry = _await_event(old_identity, "heartbeat_enter",
                                       timeout=_real_left(case.deadline, 30))
        assert (started["pid"] == heartbeat_entry["pid"] == old_pid["pid"]
                and heartbeat_entry["operation"] == "lease.heartbeat"
                and heartbeat_entry["worker_id"] == checkpoint["task"]["claimed_by"]
                and heartbeat_entry["task_id"] == task_id)
        original = _death_rows(resources, user, task_id,
                               evidence_version=version)
        assert (_lock_stable_rows(original) ==
                _lock_stable_rows({key: value for key, value in checkpoint.items()
                                   if key != "candidates"})
                and original["task"]["status"] == "running"
                and original["audits"][0]["ended_at"] is None
                and original["task"]["lease_expires_at"] > original["clock"]
                and original["ordinary"]["lease_expires_at"] > original["clock"])
        public_before = _death_public_state(resources, user)
        scanner_worker_id = "p6-expiry-scanner-" + uuid4().hex
        with _scoped_import_row_lock(resources, table="import_tasks",
                                     user_id=user, task_id=task_id) as blocker:
            scanner, scanner_pid = _lock_worker(
                case, "expiry-scanner", scanner_worker_id, fault=scenario,
                user_id=user, task_id=task_id, lock_role="scanner")
            assert scanner.control_identity is not None
            scan_identity = scanner.control_identity
            scan_entry = _await_event(scan_identity, "lock_enter",
                                      timeout=_real_left(case.deadline, 30))
            assert (scan_entry["pid"] == scanner_pid["pid"]
                    and scan_entry["operation"] == "lease.recover_expired"
                    and scan_entry["task_id"] == task_id)
            scan_wait = _prove_pg_lock_wait(resources, scan_identity,
                operation="lease.recover_expired",
                invocation_id=scan_entry["invocation_id"], ordinal=2,
                blocker_pid=blocker, child_pid=scanner_pid["pid"],
                deadline=case.deadline)
            waiting_live = _read_lock_expiry_clock(resources, user, task_id,
                                                   version)
            _assert_lock_expiry_original_live(waiting_live, checkpoint,
                                              user, task_id, version)
            assert (waiting_live["task_expiry"] > waiting_live["clock"]
                    and waiting_live["user_expiry"] > waiting_live["clock"])
            _release(old_identity, "heartbeat_enter")
            heartbeat_wait = _prove_pg_lock_wait(resources, old_identity,
                operation="lease.heartbeat",
                invocation_id=heartbeat_entry["invocation_id"], ordinal=1,
                blocker_pid=scan_wait["target_backend_pid"],
                child_pid=old_pid["pid"], deadline=case.deadline)
            # The scanner holds users and user_mutation_leases before its task
            # wait; the original heartbeat is now blocked behind that scanner.
            read_deadline = min(case.deadline, time.monotonic() + 90)
            while True:
                observed = _read_lock_expiry_clock(resources, user, task_id,
                                                   version)
                _assert_lock_expiry_original_live(observed, checkpoint,
                                                  user, task_id, version)
                if (observed["task_expiry"] <= observed["clock"]
                        and observed["user_expiry"] <= observed["clock"]):
                    expired_clock = observed["clock"]
                    break
                time.sleep(min(DATA_POLL, _real_left(read_deadline, DATA_POLL)))
            assert not scan_identity.event_path("lock_returned", 1).exists()
            assert not old_identity.event_path("lock_returned", 1).exists()
        scan_returned = _await_event(scan_identity, "lock_returned",
                                     timeout=_real_left(case.deadline, 30))
        scan_paused = _await_event(scan_identity, "scan_returned",
                                   timeout=_real_left(case.deadline, 30))
        assert (scan_returned["pid"] == scan_paused["pid"] ==
                scanner_pid["pid"] and
                scan_returned["invocation_id"] ==
                scan_paused["invocation_id"] == scan_entry["invocation_id"]
                and scan_returned["result_value"] == 1)
        heartbeat_lost = _await_event(old_identity, "lock_exception",
                                      timeout=_real_left(case.deadline, 30))
        heartbeat_end = _await_event(old_identity, "heartbeat_finally",
                                     timeout=_real_left(case.deadline, 30))
        assert (heartbeat_lost["pid"] == heartbeat_end["pid"] == old_pid["pid"]
                and heartbeat_lost["operation"] == "lease.heartbeat"
                and heartbeat_lost["invocation_id"] ==
                    heartbeat_entry["invocation_id"]
                and heartbeat_lost["exception"] == "ImportLeaseLost"
                and heartbeat_end["tuple_sha256"] == started["tuple_sha256"])
        closed = _death_rows(resources, user, task_id,
                             evidence_version=version)
        assert (closed["clock"] >= expired_clock
                and closed["task"]["status"] == "retry_wait"
                and closed["task"]["next_attempt_at"] is None
                and closed["task"]["error_code"] == "needs_reconciliation"
                and closed["audits"][0]["end_reason"] == "lease_expired"
                and closed["audits"][0]["ended_at"] is not None
                and closed["queue"] == checkpoint["queue"]
                and closed["recovery"] is None and not closed["issuance"]
                and closed["evidence"] == checkpoint["evidence"]
                and closed["gate"] == checkpoint["gate"]
                and closed["reservations"] == checkpoint["reservations"]
                and _lock_stable_rows(closed)["generations"] ==
                    _lock_stable_rows(checkpoint)["generations"]
                and _death_public_state(resources, user) == public_before)
        _retire_role(resources, scanner,
                     _real_left(case.deadline, JOB_EXIT_SECONDS),
                     "expiry-scanner")
        case.controls.remove(scan_identity)
        _release(old_identity, "fault")
        fault_return = _await_event(old_identity, "original_returned",
                                    timeout=_real_left(case.deadline, 30))
        old_rejected = _await_event(old_identity, "lock_exception", hit=2,
                                    timeout=_real_left(case.deadline, 30))
        assert (fault_return["pid"] == old_rejected["pid"] == old_pid["pid"]
                and old_rejected["operation"] == "lease.try_begin_committing"
                and old_rejected["exception"] == "ImportLeaseLost")
        after_old = _death_rows(resources, user, task_id,
                                evidence_version=version)
        assert (_lock_stable_rows(after_old) == _lock_stable_rows(closed)
                and _death_public_state(resources, user) == public_before)
        _retire_role(resources, old,
                     _real_left(case.deadline, JOB_EXIT_SECONDS),
                     "lock-source-0")
        case.controls.remove(old_identity)
        fresh_worker_id = "p6-expiry-fresh-" + uuid4().hex
        fresh, fresh_pid = _lock_worker(
            case, "expiry-fresh", fresh_worker_id, fault=scenario,
            user_id=user, task_id=task_id, lock_role="fresh")
        assert fresh.control_identity is not None
        fresh_identity = fresh.control_identity
        fresh_entry = _await_event(fresh_identity, "fresh_claim_enter",
                                   timeout=_real_left(case.deadline, 60))
        assert (fresh_entry["pid"] == fresh_pid["pid"]
                and fresh_entry["worker_id"] == fresh_worker_id
                and fresh_entry["operation"] == "recovery.claim_next")
        _release(fresh_identity, "fresh_claim_enter")
        grant = _await_event(fresh_identity, "lock_returned",
                             timeout=_real_left(case.deadline, 60))
        assert (grant["pid"] == fresh_pid["pid"]
                and grant["operation"] == "recovery.claim_next"
                and grant["invocation_id"] == fresh_entry["invocation_id"]
                and grant["result_type"] == "RecoveryClaim"
                and grant["result_user_id"] == user
                and grant["result_task_id"] == task_id
                and grant["result_worker_id"] == fresh_worker_id)
        final = _lock_recovery_rows(case, user, task_id, version,
            lambda rows: rows["evidence"]["phase"] == "abandoned"
            and rows["queue"]["state"] == "resolved")
        _assert_death_recovery_capture(final, user=user, task_id=task_id,
            old_version=version, worker_id=fresh_worker_id, reason="abandoned")
        assert (final["evidence"]["phase_version"] ==
                checkpoint["evidence"]["phase_version"] + 1
                and final["gate"] == {"status": "resolved"}
                and _death_public_state(resources, user) == public_before)
        revoked = _assert_death_revoked_candidates(checkpoint, final)
        ordinary_gate = _await_event(fresh_identity, "fresh_ordinary_enter",
                                     timeout=_real_left(case.deadline, 30))
        assert (ordinary_gate["pid"] == fresh_pid["pid"]
                and ordinary_gate["worker_id"] == fresh_worker_id)
        _release(fresh_identity, "fresh_ordinary_enter")
        _wait_task(case.http, task_id, case.sessions[0][0], case.deadline)
        _assert_published(resources, user, task, content,
            content.decode("utf-8"), fresh_worker_id,
            frozenset((user,)), revoked_candidates=revoked)
        _assert_lock_excluded(case, excluded_before, objects_before)
        _atomic_json(resources.directory / f"{scenario}-lock-result.json", {
            "scenario": scenario, "task_id": task_id,
            "old_runtime_pid": old_pid["pid"],
            "scanner_runtime_pid": scanner_pid["pid"],
            "fresh_runtime_pid": fresh_pid["pid"],
            "heartbeat_invocation_id": heartbeat_entry["invocation_id"],
            "scan_invocation_id": scan_entry["invocation_id"],
            "fresh_invocation_id": fresh_entry["invocation_id"],
            "task_blocker_backend_pid": blocker,
            "scanner_backend_pid": scan_wait["target_backend_pid"],
            "heartbeat_backend_pid": heartbeat_wait["target_backend_pid"],
            "original_task_expiry": checkpoint["task"]["lease_expires_at"].isoformat(),
            "original_user_expiry": checkpoint["ordinary"]["lease_expires_at"].isoformat(),
            "expired_clock": expired_clock.isoformat(),
            "scanner_returned": scan_returned["result_value"],
            "heartbeat_exception": heartbeat_lost["exception"],
            "old_terminal_exception": old_rejected["exception"],
            "fresh_grant_type": grant["result_type"],
            "final_phase": final["evidence"]["phase"],
            "closed_projection_sha256": _digest(_lock_stable_rows(closed)),
            "after_old_projection_sha256": _digest(_lock_stable_rows(after_old))})


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_queue_capture_before_user_wait(
        real_process_resources: RealProcessResources) -> None:
    resources = real_process_resources
    scenario = "queue_capture_before_user_wait"
    user = resources.users[0]
    excluded_before = _business_projection(resources.db, resources.excluded)
    objects_before = _object_projection(resources.s3, resources.settings.s3_bucket,
                                        resources.excluded)
    with _lock_case(resources, scenario) as case:
        task, content, source, source_pid, checkpoint = _lock_checkpoint(
            case, "pair_evidence_committed")
        task_id = str(task["task_id"])
        version = checkpoint["task"]["lease_version"]
        _await_death_expiry(resources, user, task_id, checkpoint, case.deadline)
        public_before = _death_public_state(resources, user)
        scan_before = _death_rows(resources, user, task_id,
                                  evidence_version=version)
        assert (scan_before["queue"] == checkpoint["queue"]
                and scan_before["queue"]["state"] == "pending"
                and scan_before["queue"]["claim_token"] is None
                and scan_before["queue"]["claim_expires_at"] is None
                and scan_before["queue"]["due_at"] is not None
                and scan_before["recovery"] is None
                and not scan_before["issuance"])
        scanner_worker_id = "p6-queue-scanner-" + uuid4().hex
        with _scoped_import_row_lock(resources, table="users",
                                     user_id=user) as scan_blocker:
            scanner, scanner_pid = _lock_worker(
                case, "queue-scanner", scanner_worker_id, fault=scenario,
                user_id=user, task_id=task_id, lock_role="scanner")
            assert scanner.control_identity is not None
            scan_identity = scanner.control_identity
            scan_paused = _await_event(scan_identity, "scan_returned",
                                       timeout=_real_left(case.deadline, 30))
            skipped_scan = _await_event(scan_identity, "lock_returned",
                                        timeout=_real_left(case.deadline, 30))
            assert (skipped_scan["pid"] == scan_paused["pid"] == scanner_pid["pid"]
                    and skipped_scan["operation"] == "lease.recover_expired"
                    and skipped_scan["invocation_id"] == scan_paused["invocation_id"]
                    and skipped_scan["result_value"] == 0
                    and type(scan_blocker) is int and scan_blocker > 0)
            scan_after = _death_rows(resources, user, task_id,
                                     evidence_version=version)
            assert (_lock_stable_rows(scan_after) ==
                    _lock_stable_rows(scan_before)
                    and scan_after["task"]["status"] == "running"
                    and scan_after["queue"] == checkpoint["queue"]
                    and _death_public_state(resources, user) == public_before)
            # Keep the original scanner paused after its skipped scan; releasing
            # it could allow the same Worker to capture the due queue before exit.
            _retire_role(resources, scanner,
                         _real_left(case.deadline, JOB_EXIT_SECONDS),
                         "queue-scanner")
            case.controls.remove(scan_identity)
        old_worker_id = "p6-queue-old-" + uuid4().hex
        old, old_pid = _lock_worker(case, "queue-old", old_worker_id,
            fault=scenario, user_id=user, task_id=task_id)
        assert old.control_identity is not None
        identity = old.control_identity
        closed = _await_event(identity, "ordinary_closed_existing_queue",
                              timeout=_real_left(case.deadline, 60))
        close_scan = _await_event(identity, "lock_returned",
                                  timeout=_real_left(case.deadline, 60))
        assert (closed["pid"] == close_scan["pid"] == old_pid["pid"]
                and closed["invocation_id"] == close_scan["invocation_id"]
                and close_scan["operation"] == "lease.recover_expired"
                and close_scan["result_value"] == 1)
        while True:
            pending = _death_rows(resources, user, task_id,
                                  evidence_version=version)
            assert (pending["task"]["status"] == "retry_wait"
                    and pending["queue"] == checkpoint["queue"]
                    and pending["recovery"] is None
                    and not pending["issuance"]
                    and pending["evidence"] == checkpoint["evidence"]
                    and pending["gate"] == checkpoint["gate"]
                    and pending["reservations"] == checkpoint["reservations"]
                    and _lock_stable_rows(pending)["generations"] ==
                        _lock_stable_rows(checkpoint)["generations"])
            if pending["queue"]["due_at"] <= pending["clock"]:
                break
            time.sleep(min(DATA_POLL, _real_left(case.deadline, DATA_POLL)))
        fresh_worker_id = "p6-queue-fresh-" + uuid4().hex
        with _scoped_import_row_lock(resources, table="users",
                                     user_id=user) as blocker:
            _release(identity, "ordinary_closed_existing_queue")
            entered = _await_event(identity, "claim_enter",
                                   timeout=_real_left(case.deadline, 60))
            assert (entered["pid"] == old_pid["pid"]
                    and entered["worker_id"] == old_worker_id
                    and entered["operation"] == "recovery.claim_next")
            _release(identity, "claim_enter")
            wait_proof = _prove_pg_lock_wait(
                resources, identity, operation="recovery.claim_next",
                invocation_id=entered["invocation_id"], ordinal=2,
                blocker_pid=blocker, child_pid=old_pid["pid"],
                deadline=case.deadline)
            original = _lock_recovery_rows(case, user, task_id, version,
                lambda rows: rows["queue"]["state"] == "claimed"
                and rows["queue"]["claim_token"] is not None
                and rows["recovery"] is None
                and len(rows["issuance"]) == 1)
            assert (original["queue"]["queue_version"] ==
                    pending["queue"]["queue_version"] + 1
                    and original["queue"]["claim_expires_at"] >
                    original["clock"]
                    and original["issuance"][0]["kind"] == "queue"
                    and original["issuance"][0]["token"] ==
                        original["queue"]["claim_token"]
                    and original["issuance"][0]["issued_version"] ==
                        original["queue"]["queue_version"]
                    and original["gate"] == pending["gate"]
                    and original["evidence"] == pending["evidence"]
                    and _death_public_state(resources, user) == public_before)
            fresh, fresh_pid = _lock_worker(
                case, "queue-fresh", fresh_worker_id, fault=scenario,
                user_id=user, task_id=task_id, lock_role="fresh")
            assert fresh.control_identity is not None
            fresh_identity = fresh.control_identity
            assert fresh_pid["pid"] not in {
                case.api_pid, source_pid["pid"], old_pid["pid"]}
            fresh_entry = _await_event(fresh_identity, "fresh_claim_enter",
                                       timeout=_real_left(case.deadline, 60))
            assert (fresh_entry["pid"] == fresh_pid["pid"]
                    and fresh_entry["worker_id"] == fresh_worker_id
                    and fresh_entry["operation"] == "recovery.claim_next")
            expired = _await_queue_capture_expiry(
                case, user, task_id, version, original)
            assert (expired["queue"]["claim_expires_at"] <= expired["clock"]
                    and _death_public_state(resources, user) == public_before)
            _release(fresh_identity, "fresh_claim_enter")
            fresh_gate = _await_event(fresh_identity, "fresh_before_user_wait",
                                      timeout=_real_left(case.deadline, 30))
            assert (fresh_gate["pid"] == fresh_pid["pid"]
                    and fresh_gate["operation"] == "recovery.claim_next"
                    and fresh_gate["invocation_id"] ==
                        fresh_entry["invocation_id"]
                    and fresh_gate["ordinal"] == 2)
            replacement = _death_rows(resources, user, task_id,
                                      evidence_version=version)
            assert (replacement["queue"]["state"] == "claimed"
                    and replacement["queue"]["queue_version"] ==
                        original["queue"]["queue_version"] + 1
                    and replacement["queue"]["claim_token"] !=
                        original["queue"]["claim_token"]
                    and replacement["queue"]["claim_expires_at"] >
                        replacement["clock"]
                    and replacement["recovery"] is None
                    and len(replacement["issuance"]) == 2
                    and {(row["kind"], row["token"], row["issued_version"])
                         for row in replacement["issuance"]} == {
                        ("queue", original["queue"]["claim_token"],
                         original["queue"]["queue_version"]),
                        ("queue", replacement["queue"]["claim_token"],
                         replacement["queue"]["queue_version"])}
                    and replacement["evidence"] == original["evidence"]
                    and replacement["gate"] == original["gate"]
                    and replacement["reservations"] == original["reservations"]
                    and _lock_stable_rows(replacement)["generations"] ==
                        _lock_stable_rows(original)["generations"]
                    and _death_public_state(resources, user) == public_before)
            _assert_lock_excluded(case, excluded_before, objects_before)
        refused = _await_event(identity, "lock_exception",
                               timeout=_real_left(case.deadline, 30))
        assert (refused["pid"] == old_pid["pid"]
                and refused["operation"] == "recovery.claim_next"
                and refused["invocation_id"] == entered["invocation_id"]
                and refused["exception"] == "RecoveryLeaseLost"
                and not identity.event_path("lock_returned", 2).exists())
        _await_event(identity, "post_refusal_recovery",
                     timeout=_real_left(case.deadline, 30))
        after_refusal = _death_rows(resources, user, task_id,
                                   evidence_version=version)
        assert (_lock_stable_rows(after_refusal) ==
                _lock_stable_rows(replacement)
                and after_refusal["queue"]["claim_expires_at"] >
                    after_refusal["clock"]
                and _death_public_state(resources, user) == public_before)
        _retire_role(resources, old, _real_left(case.deadline, JOB_EXIT_SECONDS),
                     "queue-old")
        case.controls.remove(identity)
        _release(fresh_identity, "fresh_before_user_wait")
        granted = _await_event(fresh_identity, "lock_returned",
                               timeout=_real_left(case.deadline, 30))
        assert (granted["pid"] == fresh_pid["pid"]
                and granted["operation"] == "recovery.claim_next"
                and granted["invocation_id"] == fresh_entry["invocation_id"]
                and granted["result_type"] == "RecoveryClaim"
                and granted["result_user_id"] == user
                and granted["result_task_id"] == task_id
                and granted["result_worker_id"] == fresh_worker_id)
        final = _lock_recovery_rows(case, user, task_id, version,
            lambda rows: rows["evidence"]["phase"] == "abandoned"
            and rows["queue"]["state"] == "resolved")
        _assert_death_recovery_capture(
            final, user=user, task_id=task_id, old_version=version,
            worker_id=fresh_worker_id, reason="abandoned",
            previous_issuance=tuple(original["issuance"]))
        assert (final["recovery"]["queue_claim_token"] ==
                replacement["queue"]["claim_token"]
                and final["recovery"]["queue_version"] ==
                    replacement["queue"]["queue_version"]
                and final["evidence"]["phase_version"] ==
                    checkpoint["evidence"]["phase_version"] + 1
                and final["gate"] == {"status": "resolved"}
                and _death_public_state(resources, user) == public_before)
        revoked = _assert_death_revoked_candidates(checkpoint, final)
        ordinary_gate = _await_event(fresh_identity, "fresh_ordinary_enter",
                                     timeout=_real_left(case.deadline, 30))
        assert (ordinary_gate["pid"] == fresh_pid["pid"]
                and ordinary_gate["worker_id"] == fresh_worker_id)
        _release(fresh_identity, "fresh_ordinary_enter")
        _wait_task(case.http, task_id, case.sessions[0][0], case.deadline)
        _assert_published(resources, user, task, content,
            content.decode("utf-8"), fresh_worker_id,
            frozenset((user,)), revoked_candidates=revoked)
        _assert_lock_excluded(case, excluded_before, objects_before)
        _atomic_json(resources.directory / f"{scenario}-lock-result.json", {
            "scenario": scenario, "task_id": task_id,
            "scanner_runtime_pid": scanner_pid["pid"],
            "scanner_invocation_id": skipped_scan["invocation_id"],
            "scanner_blocker_backend_pid": scan_blocker,
            "scanner_returned": skipped_scan["result_value"],
            "scanner_clock": scan_after["clock"].isoformat(),
            "original_task_expiry": checkpoint["task"]["lease_expires_at"].isoformat(),
            "original_user_expiry": checkpoint["ordinary"]["lease_expires_at"].isoformat(),
            "old_runtime_pid": old_pid["pid"],
            "fresh_runtime_pid": fresh_pid["pid"],
            "close_ordinary_invocation_id": close_scan["invocation_id"],
            "close_ordinary_returned": close_scan["result_value"],
            "queue_preserved_from_checkpoint":
                pending["queue"] == checkpoint["queue"],
            "old_queue_version": original["queue"]["queue_version"],
            "fresh_queue_version": replacement["queue"]["queue_version"],
            "final_queue_version": final["queue"]["queue_version"],
            "old_queue_identity_sha256": _digest((
                original["queue"]["claim_token"],
                original["queue"]["queue_version"],
                original["queue"]["claim_expires_at"])),
            "fresh_queue_identity_sha256": _digest((
                replacement["queue"]["claim_token"],
                replacement["queue"]["queue_version"],
                replacement["queue"]["claim_expires_at"])),
            "old_exception": refused["exception"],
            "final_phase": final["evidence"]["phase"],
            "old_invocation_id": entered["invocation_id"],
            "fresh_invocation_id": fresh_entry["invocation_id"],
            "old_target_backend_pid": wait_proof["target_backend_pid"],
            "parent_blocker_backend_pid": wait_proof["blocker_backend_pid"],
            "old_capture_expiry": original["queue"]["claim_expires_at"].isoformat(),
            "fresh_capture_expiry": replacement["queue"]["claim_expires_at"].isoformat(),
            "old_wait_clock": original["clock"].isoformat(),
            "old_expired_clock": expired["clock"].isoformat(),
            "fresh_capture_clock": replacement["clock"].isoformat(),
            "after_refusal_clock": after_refusal["clock"].isoformat(),
            "old_projection_sha256": _digest(_lock_stable_rows(original)),
            "fresh_projection_sha256": _digest(_lock_stable_rows(replacement)),
            "after_refusal_projection_sha256": _digest(
                _lock_stable_rows(after_refusal))})


def _fairness_schedule(resources: RealProcessResources,
                       users: tuple[str, str]) -> dict[str, object]:
    with resources.db.transaction() as cursor:
        rows = cursor.execute(
            "select user_id,last_claimed_at from import_publication_recovery_schedule "
            "where user_id=any(%s::text[])", (list(users),)).fetchall()
    assert {row["user_id"] for row in rows} == set(users)
    return {row["user_id"]: row["last_claimed_at"] for row in rows}


def _fairness_waiting_backend(resources: RealProcessResources,
                              identity: ControlIdentity, blocker_pid: int,
                              child_pid: int, prior_invocation: str,
                              upstream_wait: dict[str, object],
                              upstream_identity: ControlIdentity,
                              upstream_invocation: str,
                              upstream_child_pid: int,
                              deadline: float) -> dict[str, object]:
    """Bind the second committed queue capture to its actual waiting backend."""
    assert (upstream_wait["case"] == "queue_capture_before_user_wait"
            and upstream_wait["role"] == "worker"
            and upstream_wait["method"] == "recovery.claim_next"
            and upstream_wait["invocation_id"] == upstream_invocation
            and upstream_wait["transaction_ordinal"] == 2
            and upstream_wait["child_pid"] == upstream_child_pid
            and upstream_wait["blocker_backend_pid"] == blocker_pid
            and upstream_wait["wait_event_type"] == "Lock")
    assert upstream_identity.role == "worker"
    upstream_record = _observed_lock_backend(
        upstream_identity, operation="recovery.claim_next",
        invocation_id=upstream_invocation, ordinal=2, deadline=deadline)
    assert (upstream_record["pid"] == upstream_child_pid
            and upstream_record["backend_pid"] ==
                upstream_wait["target_backend_pid"])
    upstream_backend_pid = upstream_wait["target_backend_pid"]
    while True:
        for path in identity.directory.glob("worker-lock_backend-*.event.json"):
            record = _read_json(path)
            assert (record.get("nonce") == identity.nonce
                    and record.get("case") == identity.case
                    and record.get("role") == "worker")
            if (record.get("operation") != "recovery.claim_next"
                    or record.get("ordinal") != 2
                    or record.get("pid") != child_pid
                    or record.get("invocation_id") == prior_invocation):
                continue
            backend_pid = record.get("backend_pid")
            assert type(backend_pid) is int and backend_pid > 0
            proof = _prove_pg_lock_wait(
                resources, identity, operation="recovery.claim_next",
                invocation_id=record["invocation_id"], ordinal=2,
                blocker_pid=blocker_pid, child_pid=child_pid,
                deadline=deadline,
                upstream_backend_pid=upstream_backend_pid)
            assert proof["target_backend_pid"] == backend_pid
            return {"operation": proof["method"],
                    "invocation_id": proof["invocation_id"],
                    "child_pid": proof["child_pid"],
                    "backend_pid": proof["target_backend_pid"],
                    "blocker_pid": proof["blocker_backend_pid"],
                    "direct_blocker_backend_pid":
                        proof["direct_blocker_backend_pid"],
                    "blocking_chain": proof["blocking_chain"],
                    "chain_observation": proof["chain_observation"],
                    "wait_event_type": proof["wait_event_type"],
                    "wait_event": proof["wait_event"],
                    "blocking_pids": proof["blocking_pids"],
                    "pg_clock": proof["pg_clock"]}
        time.sleep(min(DATA_POLL, _real_left(deadline, DATA_POLL)))


def _fairness_returned_claim(identity: ControlIdentity, *,
                             user: str, task_id: str, worker_id: str,
                             child_pid: int, deadline: float) -> dict[str, object]:
    while True:
        matches = []
        for path in identity.directory.glob("worker-lock_returned-*.event.json"):
            record = _read_json(path)
            assert (record.get("nonce") == identity.nonce
                    and record.get("case") == identity.case
                    and record.get("role") == "worker")
            if (record.get("operation") == "recovery.claim_next"
                    and record.get("result_user_id") == user
                    and record.get("result_task_id") == task_id
                    and record.get("result_worker_id") == worker_id
                    and record.get("pid") == child_pid):
                matches.append(record)
        if matches:
            assert len(matches) == 1, "fairness_claim_not_unique"
            return matches[0]
        time.sleep(min(DATA_POLL, _real_left(deadline, DATA_POLL)))


@pytest.mark.skipif(os.name != "nt", reason="Win32 actual-process acceptance")
def test_real_two_user_fairness(
        real_process_resources: RealProcessResources) -> None:
    resources = real_process_resources
    scenario = "two_user_fairness"
    users = tuple(sorted(resources.users))
    a, b = users
    a_index, b_index = resources.users.index(a), resources.users.index(b)
    excluded_before = _business_projection(resources.db, resources.excluded)
    excluded_objects_before = _object_projection(
        resources.s3, resources.settings.s3_bucket, resources.excluded)
    with _lock_case(resources, scenario) as case:
        # Queue A's second ordinary task while upload admission is still legal.
        content_a = b"Fairness primary A immutable document."
        first_a = _lock_upload(case, a_index, content_a,
                               stage=f"fairness-{a_index}")
        waiting_a = _lock_upload(case, a_index,
                                 b"Fairness second A ordinary task.",
                                 stage="fairness-a-queued")
        first_a_id = str(first_a["task_id"])
        waiting_a_id = str(waiting_a["task_id"])
        source_a_id = "p6-fair-source-a-" + uuid4().hex
        source_a, source_a_pid = _lock_worker(
            case, "fair-source-a", source_a_id,
            fault="pair_evidence_committed", user_id=a, task_id=first_a_id)
        assert source_a.control_identity is not None
        source_a_marker = _await_event(source_a.control_identity, "fault",
                                        timeout=_real_left(case.deadline, 60))
        assert source_a_marker["pid"] == source_a_pid["pid"]
        original_a = _assert_death_checkpoint(
            resources, "pair_evidence_committed", a, first_a,
            content_a, source_a_id, source_a_marker)
        # Keep A's original lease live while B reaches the same durable
        # checkpoint. B's unmodified first recovery round must skip A's exact
        # queue row even if its initial due time arrives during B setup.
        with resources.db.transaction() as cursor:
            setup_guard_pid = cursor.connection.info.backend_pid
            guarded = cursor.execute(
                "select user_id,task_id,task_lease_version from "
                "import_publication_recovery_queue where user_id=%s and "
                "task_id=%s and task_lease_version=%s for update",
                (a, first_a_id, original_a["task"]["lease_version"]),
            ).fetchone()
            assert guarded == {
                "user_id": a, "task_id": first_a_id,
                "task_lease_version": original_a["task"]["lease_version"]}
            first_b, content_b, source_b, source_b_pid, original_b = (
                _lock_checkpoint(case, "pair_evidence_committed",
                                 user_index=b_index, retire=False))
            assert source_b.control_identity is not None
            source_b_marker = _await_event(
                source_b.control_identity, "fault",
                timeout=_real_left(case.deadline, 30))
            _retire_role(resources, source_a,
                         _real_left(case.deadline, JOB_EXIT_SECONDS),
                         "fair-source-a")
            case.controls.remove(source_a.control_identity)
            _retire_role(resources, source_b,
                         _real_left(case.deadline, JOB_EXIT_SECONDS),
                         "lock-source-" + str(b_index))
            case.controls.remove(source_b.control_identity)
        # Both post-death snapshots bind the final heartbeat timestamps used
        # below to prove natural expiry, including any renewals during setup.
        original_a = _assert_death_checkpoint(
            resources, "pair_evidence_committed", a, first_a,
            content_a, source_a_id, source_a_marker)
        original_b = _assert_death_checkpoint(
            resources, "pair_evidence_committed", b, first_b,
            content_b, original_b["audits"][0]["worker_id"], source_b_marker)
        first_b_id = str(first_b["task_id"])
        versions = {a: original_a["task"]["lease_version"],
                    b: original_b["task"]["lease_version"]}
        _await_death_expiry(resources, a, first_a_id, original_a, case.deadline)
        _await_death_expiry(resources, b, first_b_id, original_b, case.deadline)
        while True:
            due_a = _death_rows(resources, a, first_a_id,
                                evidence_version=versions[a])
            due_b = _death_rows(resources, b, first_b_id,
                                evidence_version=versions[b])
            assert (due_a["queue"]["state"] == "pending"
                    and due_b["queue"]["state"] == "pending"
                    and due_a["gate"] == due_b["gate"] ==
                        {"status": "unresolved"})
            if (due_a["queue"]["due_at"] <= due_a["clock"]
                    and due_b["queue"]["due_at"] <= due_b["clock"]):
                break
            time.sleep(min(DATA_POLL, _real_left(case.deadline, DATA_POLL)))
        schedule_before = _fairness_schedule(resources, users)
        assert schedule_before[a] == schedule_before[b]

        worker_b_id = "p6-fair-worker-b-" + uuid4().hex
        worker_b, b_pid = _lock_worker(
            case, "fair-worker-b", worker_b_id, fault=scenario,
            user_id=b, task_id=first_b_id)
        assert worker_b.control_identity is not None
        b_control = worker_b.control_identity
        ready_b = _await_event(b_control, "fairness_enter",
                               timeout=_real_left(case.deadline, 30))
        assert (ready_b["pid"] == b_pid["pid"]
                and ready_b["worker_id"] == worker_b_id)

        worker_a_id = "p6-fair-worker-a-" + uuid4().hex
        with _scoped_import_row_lock(resources, table="users",
                                     user_id=a) as blocker:
            worker_a, a_pid = _lock_worker(
                case, "fair-worker-a", worker_a_id,
                fault="queue_capture_before_user_wait",
                user_id=a, task_id=first_a_id)
            assert worker_a.control_identity is not None
            a_control = worker_a.control_identity
            assert len({case.api_pid, source_a_pid["pid"],
                        source_b_pid["pid"], a_pid["pid"], b_pid["pid"]}) == 5
            _await_event(a_control, "ordinary_closed_existing_queue",
                         timeout=_real_left(case.deadline, 30))
            _release(a_control, "ordinary_closed_existing_queue")
            a_enter = _await_event(a_control, "claim_enter",
                                   timeout=_real_left(case.deadline, 30))
            assert (a_enter["pid"] == a_pid["pid"]
                    and a_enter["worker_id"] == worker_a_id)
            _release(a_control, "claim_enter")
            a_wait = _prove_pg_lock_wait(
                resources, a_control, operation="recovery.claim_next",
                invocation_id=a_enter["invocation_id"], ordinal=2,
                blocker_pid=blocker, child_pid=a_pid["pid"],
                deadline=case.deadline)
            captured_a = _lock_recovery_rows(
                case, a, first_a_id, versions[a],
                lambda rows: rows["queue"]["state"] == "claimed"
                and rows["queue"]["claim_token"] is not None
                and rows["recovery"] is None)
            schedule_a = _fairness_schedule(resources, users)
            assert (schedule_a[a] > schedule_before[a]
                    and schedule_a[b] == schedule_before[b]
                    and captured_a["queue"]["claim_expires_at"] >
                        captured_a["clock"])
            eligible_b = _death_rows(resources, b, first_b_id,
                                     evidence_version=versions[b])
            assert (eligible_b["queue"]["state"] == "pending"
                    and eligible_b["queue"]["due_at"] <= eligible_b["clock"]
                    and eligible_b["gate"] == {"status": "unresolved"}
                    and eligible_b["recovery"] is None)

            b_started = time.monotonic()
            _release(b_control, "fairness_enter")
            b_capture = _await_event(b_control, "lock_returned",
                                     timeout=_real_left(case.deadline, 30))
            assert (b_capture["pid"] == b_pid["pid"]
                    and b_capture["operation"] == "recovery.claim_next"
                    and b_capture["result_type"] == "RecoveryClaim"
                    and b_capture["result_user_id"] == b
                    and b_capture["result_task_id"] == first_b_id
                    and b_capture["result_worker_id"] == worker_b_id)
            b_claimed = _death_rows(resources, b, first_b_id,
                                    evidence_version=versions[b])
            assert (b_claimed["recovery"]["owner"] == worker_b_id
                    and b_claimed["recovery"]["queue_claim_token"] in
                    {row["token"] for row in b_claimed["issuance"]
                     if row["kind"] == "queue"})
            _wait_task(case.http, first_b_id, case.sessions[b_index][0],
                       min(case.deadline, b_started + 120))
            assert time.monotonic() <= b_started + 120
            final_b = _death_rows(resources, b, first_b_id,
                                  evidence_version=versions[b])
            assert (final_b["task"]["status"] == "succeeded"
                    and final_b["evidence"]["phase"] == "abandoned"
                    and final_b["queue"]["state"] == "resolved"
                    and final_b["queue"]["last_claimed_at"] >
                        captured_a["queue"]["last_claimed_at"])
            revoked_b = _assert_death_revoked_candidates(original_b, final_b)
            _assert_published(resources, b, first_b, content_b,
                content_b.decode("utf-8"), worker_b_id,
                frozenset(users), revoked_candidates=revoked_b)
            with resources.db.transaction() as cursor:
                waiting = cursor.execute(
                    "select status,lease_version from import_tasks "
                    "where id=%s and user_id=%s",
                    (waiting_a_id, a)).fetchone()
                waiting_attempts = cursor.execute(
                    "select count(*) as n from import_task_attempts "
                    "where task_id=%s and user_id=%s",
                    (waiting_a_id, a)).fetchone()["n"]
            assert (waiting is not None and waiting["status"] == "queued"
                    and waiting["lease_version"] == 0 and waiting_attempts == 0)

            # A's first capture expires by the database clock. The still-live B
            # Worker then makes the next eligible choice while A remains locked.
            recapture = _lock_recovery_rows(
                case, a, first_a_id, versions[a],
                lambda rows: rows["queue"]["queue_version"] ==
                    captured_a["queue"]["queue_version"] + 1
                and rows["queue"]["claim_token"] !=
                    captured_a["queue"]["claim_token"]
                and rows["recovery"] is None)
            assert (captured_a["queue"]["claim_expires_at"] <=
                    recapture["queue"]["last_claimed_at"]
                    and recapture["queue"]["claim_expires_at"] >
                        recapture["clock"]
                    and len(recapture["issuance"]) == 2)
            schedule_recapture = _fairness_schedule(resources, users)
            assert (schedule_recapture[a] ==
                    recapture["queue"]["last_claimed_at"] >
                    schedule_recapture[b] ==
                    final_b["queue"]["last_claimed_at"])
            b_wait = _fairness_waiting_backend(
                resources, b_control, blocker, b_pid["pid"],
                b_capture["invocation_id"], a_wait, a_control,
                a_enter["invocation_id"], a_pid["pid"], case.deadline)
            assert b_wait["invocation_id"] != b_capture["invocation_id"]
            _assert_lock_excluded(case, excluded_before,
                                  excluded_objects_before)

        a_refused = _await_event(a_control, "lock_exception",
                                 timeout=_real_left(case.deadline, 30))
        assert (a_refused["pid"] == a_pid["pid"]
                and a_refused["invocation_id"] == a_enter["invocation_id"]
                and a_refused["exception"] == "RecoveryLeaseLost")
        _await_event(a_control, "post_refusal_recovery",
                     timeout=_real_left(case.deadline, 30))
        _retire_role(resources, worker_a,
                     _real_left(case.deadline, JOB_EXIT_SECONDS),
                     "fair-worker-a")
        case.controls.remove(a_control)
        b_second = _fairness_returned_claim(
            b_control, user=a, task_id=first_a_id,
            worker_id=worker_b_id, child_pid=b_pid["pid"],
            deadline=case.deadline)
        assert (b_second["pid"] == b_pid["pid"]
                and b_second["invocation_id"] == b_wait["invocation_id"]
                and b_second["result_type"] == "RecoveryClaim"
                and b_second["result_user_id"] == a
                and b_second["result_task_id"] == first_a_id
                and b_second["result_worker_id"] == worker_b_id)
        final_a = _lock_recovery_rows(
            case, a, first_a_id, versions[a],
            lambda rows: rows["evidence"]["phase"] == "abandoned"
            and rows["queue"]["state"] == "resolved")
        assert (final_a["recovery"]["owner"] == worker_b_id
                and final_a["recovery"]["queue_claim_token"] ==
                    recapture["queue"]["claim_token"])
        _assert_death_revoked_candidates(original_a, final_a)
        _assert_lock_excluded(case, excluded_before, excluded_objects_before)
        _atomic_json(resources.directory / f"{scenario}-result.json", {
            "a_user": a, "b_user": b,
            "a_primary_task": first_a_id, "a_queued_task": waiting_a_id,
            "b_task": first_b_id, "a_worker_pid": a_pid["pid"],
            "b_worker_pid": b_pid["pid"],
            "setup_queue_guard_backend_pid": setup_guard_pid,
            "a_blocker_backend_pid": blocker,
            "a_wait_backend_pid": a_wait["target_backend_pid"],
            "b_wait_backend_pid": b_wait["backend_pid"],
            "b_wait_direct_blocker_backend_pid":
                b_wait["direct_blocker_backend_pid"],
            "b_wait_blocking_chain": b_wait["blocking_chain"],
            "b_wait_chain_observation": b_wait["chain_observation"],
            "a_wait_pg_clock": a_wait["pg_clock"],
            "b_wait_pg_clock": b_wait["pg_clock"],
            "first_a_capture_version": captured_a["queue"]["queue_version"],
            "b_capture_version": b_claimed["queue"]["queue_version"],
            "second_a_capture_version": recapture["queue"]["queue_version"],
            "schedule_before": {user: stamp.isoformat() for user, stamp in
                                schedule_before.items()},
            "schedule_after_a": {user: stamp.isoformat() for user, stamp in
                                 schedule_a.items()},
            "b_persisted_capture_time":
                final_b["queue"]["last_claimed_at"].isoformat(),
            "schedule_after_recap": {user: stamp.isoformat() for user, stamp in
                                     schedule_recapture.items()},
            "a_capture_expiry": captured_a["queue"]["claim_expires_at"].isoformat(),
            "b_completion_within_120s": True,
            "excluded_projection_unchanged": True})


@pytest.mark.parametrize("scenario", (
    "direct", "chain", "wrong_parent", "not_lock", "no_parent"))
def test_unit_fairness_pg_wait_proves_exact_parent_or_live_upstream_chain(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        scenario: str) -> None:
    """A stale or unrelated upstream waiter cannot establish B's PG proof."""
    direct = scenario == "direct"
    accepted = scenario in {"direct", "chain"}
    upstream_type = "Client" if scenario == "not_lock" else "Lock"
    upstream_blockers = ([99] if scenario == "wrong_parent" else
                         [] if scenario == "no_parent" else [65])
    case_dir = tmp_path / uuid4().hex
    case_dir.mkdir()
    identity = ControlIdentity(uuid4().hex, "two_user_fairness",
                               "worker", case_dir)
    invocation = uuid4().hex
    _atomic_json(case_dir / "worker-lock_backend-1.event.json", {
        "nonce": identity.nonce, "case": identity.case, "role": "worker",
        "operation": "recovery.claim_next", "invocation_id": invocation,
        "ordinal": 2, "pid": 400, "backend_pid": 75})
    queries: list[tuple[str, int | None]] = []

    class Cursor:
        def execute(self, sql: str, parameters: tuple[int, ...] = ()):
            pid = parameters[0] if parameters else None
            queries.append((sql, pid))
            if "pg_stat_activity" in sql:
                if pid == 75:
                    self.row = {"pid": 75, "wait_event_type": "Lock",
                                "wait_event": "transactionid"}
                else:
                    assert pid == 74
                    self.row = {"pid": 74, "wait_event_type": upstream_type,
                                "wait_event": "transactionid"}
            elif "pg_blocking_pids" in sql:
                self.row = {"pids": [65] if direct else
                            [74] if pid == 75 else list(upstream_blockers)}
            else:
                assert "clock_timestamp" in sql
                self.row = {"now": datetime.now(timezone.utc)}
            return self

        def fetchone(self):
            return self.row

    @contextmanager
    def transaction():
        yield Cursor()

    monkeypatch.setitem(globals(), "DATA_POLL", 0.005)
    resources = SimpleNamespace(db=SimpleNamespace(transaction=transaction),
                                directory=case_dir)
    kwargs = dict(operation="recovery.claim_next", invocation_id=invocation,
                  ordinal=2, blocker_pid=65, child_pid=400,
                  upstream_backend_pid=74,
                  deadline=time.monotonic() + 0.04)
    proof_path = (case_dir / f"two_user_fairness-recovery_claim_next-"
                  f"{invocation}-2-pg-lock.json")
    if not accepted:
        with pytest.raises(TimeoutError, match="real_case_deadline"):
            _prove_pg_lock_wait(resources, identity, **kwargs)
        assert not proof_path.exists()
        return
    proof = _prove_pg_lock_wait(resources, identity, **kwargs)
    assert _read_json(proof_path) == proof
    assert (proof["target_backend_pid"] == 75
            and proof["blocker_backend_pid"] == 65
            and proof["direct_blocker_backend_pid"] == (65 if direct else 74)
            and proof["blocking_pids"] == ([65] if direct else [74])
            and proof["blocking_chain"] ==
                ([75, 65] if direct else [75, 74, 65]))
    assert proof["chain_observation"] == (None if direct else {
        "upstream_backend_pid": 74, "wait_event_type": "Lock",
        "wait_event": "transactionid", "blocking_pids": [65]})
    assert any(pid == 75 and "pg_stat_activity" in sql for sql, pid in queries)
    assert direct or any(pid == 74 and "pg_blocking_pids" in sql
                         for sql, pid in queries)


def test_unit_fairness_first_round_gate_and_successive_original_captures(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the first-round pause outside the original recovery call."""
    from app.distributed_import_worker import IsolatedImportWorker
    from app.import_publication_recovery import PostgresImportPublicationRecoveryRepository
    from app.postgres import PostgresDatabase

    methods = ((IsolatedImportWorker, "run_one_recovery"),
               (PostgresImportPublicationRecoveryRepository, "claim_next"),
               (PostgresDatabase, "transaction"))
    originals = tuple(getattr(cls, name) for cls, name in methods)
    entered, released = Event(), Event()
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []
    commits: list[int] = []
    transaction_number = 0

    def event(stage: str, fields: dict[str, object]) -> None:
        events.append((stage, fields.copy()))

    def pause(stage: str, fields: dict[str, object]) -> None:
        event(stage, fields)
        assert stage == "fairness_enter"
        entered.set()
        assert released.wait(5), "fairness_gate_not_released"

    @contextmanager
    def transaction(_self):
        nonlocal transaction_number
        transaction_number += 1
        number = transaction_number
        yield SimpleNamespace(connection=SimpleNamespace(
            info=SimpleNamespace(backend_pid=1200 + number)))
        commits.append(number)

    def claim(self, worker_id, *, allowed_user_ids):
        assert worker_id == "worker-b" and allowed_user_ids == frozenset(
            {"user-a", "user-b"})
        user = ("user-b" if not any(call.startswith("claim:")
                                     for call in calls) else "user-a")
        calls.append("claim:" + user)
        with self.database.transaction():
            pass
        with self.database.transaction():
            pass
        return SimpleNamespace(user_id=user, task_id="task-" + user[-1],
                               worker_id=worker_id)

    def original_round(self):
        calls.append("round:" + self.worker_id)
        if self.worker_id != "worker-b":
            return "unselected_original_round"
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        return PostgresImportPublicationRecoveryRepository.claim_next(
            repository, self.worker_id,
            allowed_user_ids=frozenset({"user-a", "user-b"}))

    control = SimpleNamespace(target={"user_id": "user-b",
        "task_id": "task-b", "worker_id": "worker-b",
        "lock_role": None}, event=event, pause=pause)
    with monkeypatch.context() as patch:
        for cls, name in methods:
            patch.setattr(cls, name, getattr(cls, name))
        patch.setattr(IsolatedImportWorker, "run_one_recovery",
                      original_round)
        patch.setattr(PostgresImportPublicationRecoveryRepository,
                      "claim_next", claim)
        patch.setattr(PostgresDatabase, "transaction", transaction)
        _install_fault("two_user_fairness", control)
        worker = SimpleNamespace(worker_id="worker-b")
        first: list[object] = []
        failures: list[BaseException] = []

        def invoke() -> None:
            try:
                first.append(IsolatedImportWorker.run_one_recovery(worker))
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=invoke)
        thread.start()
        try:
            assert entered.wait(5) and thread.is_alive() and calls == []
        finally:
            released.set()
            thread.join(5)
        assert not thread.is_alive() and failures == []
        assert len(first) == 1 and first[0].user_id == "user-b"
        second = IsolatedImportWorker.run_one_recovery(worker)
        assert second.user_id == "user-a"
        assert IsolatedImportWorker.run_one_recovery(
            SimpleNamespace(worker_id="worker-other")) == (
                "unselected_original_round")
        assert calls == ["round:worker-b", "claim:user-b",
                         "round:worker-b", "claim:user-a",
                         "round:worker-other"]
        assert commits == [1, 2, 3, 4]
        assert len([stage for stage, _ in events
                    if stage == "fairness_enter"]) == 1
        returned = [fields for stage, fields in events
                    if stage == "lock_returned"]
        assert (len(returned) == 2
                and [row["result_user_id"] for row in returned] ==
                    ["user-b", "user-a"]
                and [row["result_task_id"] for row in returned] ==
                    ["task-b", "task-a"]
                and all(row["result_worker_id"] == "worker-b"
                        for row in returned))
        assert returned[0]["invocation_id"] != returned[1]["invocation_id"]
        backends = [fields for stage, fields in events
                    if stage == "lock_backend"]
        assert (len(backends) == 4 and
                [(row["invocation_id"], row["ordinal"])
                 for row in backends] == [
                    (returned[0]["invocation_id"], 1),
                    (returned[0]["invocation_id"], 2),
                    (returned[1]["invocation_id"], 1),
                    (returned[1]["invocation_id"], 2)])
    assert originals == tuple(getattr(cls, name) for cls, name in methods)


def test_unit_queue_capture_commits_before_fresh_user_wait(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from app.import_publication_evidence import AttemptKey
    from app.import_publication_recovery import (
        PostgresImportPublicationRecoveryRepository,
        seed_missing_queue_in_transaction)
    from app.postgres import PostgresDatabase
    from app.postgres_import_leases import PostgresImportLeaseRepository

    sequence: list[tuple[str, object]] = []
    calls: list[str] = []
    transaction_count = 0

    @contextmanager
    def fake_transaction(_self):
        nonlocal transaction_count
        transaction_count += 1
        number = transaction_count
        sequence.append(("transaction_open", number))
        yield SimpleNamespace(connection=SimpleNamespace(
            info=SimpleNamespace(backend_pid=900 + number)))
        sequence.append(("transaction_commit", number))

    def original_claim(self, worker_id, *_args, **_kwargs):
        calls.append(worker_id)
        with self.database.transaction():
            sequence.append(("queue_capture", worker_id))
        with self.database.transaction():
            sequence.append(("user_authority", worker_id))
        return SimpleNamespace(user_id="user-a", task_id="task-a",
                               worker_id=worker_id)

    def record(stage: str, fields: dict[str, object]) -> None:
        sequence.append((stage, fields))

    control = SimpleNamespace(
        target={"user_id": "user-a", "task_id": "task-a",
                "worker_id": "worker-a", "lock_role": "fresh"},
        event=record, pause=record)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresDatabase, "transaction", fake_transaction)
        patch.setattr(PostgresImportPublicationRecoveryRepository,
                      "claim_next", original_claim)
        _install_lock_observers("queue_capture_before_user_wait", control)
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        bound = PostgresImportPublicationRecoveryRepository.claim_next
        result = bound(repository, "worker-a",
                       allowed_user_ids=frozenset({"user-a"}))
        assert (result.worker_id == "worker-a"
                and calls == ["worker-a"])
        selected = [(stage, fields) for stage, fields in sequence
                    if stage in {"fresh_claim_enter", "fresh_before_user_wait",
                                 "lock_backend", "lock_returned"}]
        assert ([stage for stage, _ in selected] == [
                    "fresh_claim_enter", "lock_backend",
                    "fresh_before_user_wait", "lock_backend", "lock_returned"]
                and [fields["ordinal"] for stage, fields in selected
                     if stage == "lock_backend"] == [1, 2]
                and [fields["backend_pid"] for stage, fields in selected
                     if stage == "lock_backend"] == [901, 902]
                and len({fields["invocation_id"] for _, fields in selected}) == 1
                and sequence.index(("transaction_commit", 1)) <
                    next(index for index, item in enumerate(sequence)
                         if item[0] == "fresh_before_user_wait") <
                    sequence.index(("transaction_open", 2)))
        bound(repository, "worker-b", allowed_user_ids=frozenset({"user-a"}))
        bound(repository, "worker-a", allowed_user_ids=frozenset({"user-b"}))
        assert (calls == ["worker-a", "worker-b", "worker-a"]
                and len([stage for stage, _ in sequence
                         if stage == "fresh_before_user_wait"]) == 1)

    sequence.clear()
    scanner_calls: list[frozenset[str]] = []
    pending_queue = {"state": "pending", "queue_version": 1,
                     "claim_token": None, "claim_expires_at": None,
                     "due_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    initial_queue = pending_queue.copy()

    def original_scan(self, *_args, **kwargs):
        scanner_calls.append(kwargs["allowed_user_ids"])
        with self.database.transaction():
            assert pending_queue == initial_queue
            return 0

    scanner_control = SimpleNamespace(
        target={"user_id": "user-a", "task_id": "task-a",
                "worker_id": "worker-scan", "lock_role": "scanner"},
        event=record, pause=record)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresDatabase, "transaction", fake_transaction)
        patch.setattr(PostgresImportLeaseRepository,
                      "recover_expired", original_scan)
        _install_lock_observers("queue_capture_before_user_wait",
                                scanner_control)
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        result = PostgresImportLeaseRepository.recover_expired(
            repository, allowed_user_ids=frozenset({"user-a"}))
        assert (result == 0
                and scanner_calls == [frozenset({"user-a"})]
                and [stage for stage, _ in sequence] == [
                    "lock_enter", "transaction_open", "lock_backend",
                    "transaction_commit", "lock_returned", "scan_returned"]
                and sequence[-2][1]["result_value"] == 0
                and sequence[-1][1]["invocation_id"] ==
                    sequence[-2][1]["invocation_id"])

    class ExistingQueueCursor:
        calls: list[tuple[str, tuple[str, str, int]]] = []

        def execute(self, query, params):
            self.calls.append((query, params))
            assert ("select * from import_publication_recovery_queue" in query
                    and "for update" in query
                    and params == ("user-a", "task-a", 1))
            return self

        def fetchone(self):
            return pending_queue

    cursor = ExistingQueueCursor()
    returned = seed_missing_queue_in_transaction(
        cursor, AttemptKey("user-a", "task-a", 1),
        initial_queue["due_at"], initial_queue["due_at"])
    assert (returned is pending_queue and pending_queue == initial_queue
            and len(cursor.calls) == 1)


def test_unit_expiry_scan_observers_invoke_original_and_hold_heartbeat(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the two child-role observers without pretending to prove PG locks."""
    from app.postgres import PostgresDatabase
    from app.postgres_import_leases import (ImportLeaseLost,
                                            PostgresImportLeaseRepository)

    events: list[tuple[str, dict[str, object]]] = []
    event_guard = Lock()

    def record(stage: str, fields: dict[str, object]) -> None:
        with event_guard:
            events.append((stage, fields))

    @contextmanager
    def fake_transaction(_self):
        yield SimpleNamespace(connection=SimpleNamespace(
            info=SimpleNamespace(backend_pid=901)))

    scanner_calls: list[frozenset[str]] = []

    def original_scan(self, *_args, **kwargs):
        scanner_calls.append(kwargs["allowed_user_ids"])
        with self.database.transaction():
            pass
        with self.database.transaction():
            pass
        return 1

    scanner_control = SimpleNamespace(
        target={"user_id": "user-a", "task_id": "task-a",
                "worker_id": "scanner-a", "lock_role": "scanner"},
        event=record, pause=record)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresDatabase, "transaction", fake_transaction)
        patch.setattr(PostgresImportLeaseRepository,
                      "recover_expired", original_scan)
        _install_lock_observers("expiry_scan_after_task_wait", scanner_control)
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        assert PostgresImportLeaseRepository.recover_expired(
            repository, allowed_user_ids=frozenset({"user-a"})) == 1
        assert scanner_calls == [frozenset({"user-a"})]
        selected = [(stage, fields) for stage, fields in events
                    if stage in {"lock_enter", "lock_backend",
                                 "lock_returned", "scan_returned"}]
        assert ([stage for stage, _fields in selected] == [
                    "lock_enter", "lock_backend", "lock_backend",
                    "lock_returned", "scan_returned"]
                and [fields["ordinal"] for stage, fields in selected
                     if stage == "lock_backend"] == [1, 2]
                and selected[-2][1]["result_value"] == 1
                and len({fields["invocation_id"] for _, fields in selected}) == 1)

    events.clear()
    heartbeat_paused, release_heartbeat = Event(), Event()
    heartbeat_invoked = Event()
    heartbeat_calls: list[object] = []
    attempt = SimpleNamespace(task=SimpleNamespace(user_id="user-a",
        task_id="task-a"), worker_id="old-a")

    def original_heartbeat(self, original, *_args, **_kwargs):
        heartbeat_calls.append(original)
        heartbeat_invoked.set()
        with self.database.transaction():
            raise ImportLeaseLost("original_expired")

    def pause(stage: str, fields: dict[str, object]) -> None:
        record(stage, fields)
        if stage == "heartbeat_enter":
            heartbeat_paused.set()
            assert release_heartbeat.wait(5), "heartbeat_release_missing"

    old_control = SimpleNamespace(
        target={"user_id": "user-a", "task_id": "task-a",
                "worker_id": "old-a", "lock_role": "expiry_old"},
        matches=lambda user, task: (user, task) == ("user-a", "task-a"),
        event=record, pause=pause)
    caught: list[type[BaseException]] = []
    original_try_begin = PostgresImportLeaseRepository.try_begin_committing
    with monkeypatch.context() as patch:
        patch.setattr(PostgresDatabase, "transaction", fake_transaction)
        patch.setattr(PostgresImportLeaseRepository, "heartbeat",
                      original_heartbeat)
        # The observer also wraps this method; register its pre-install value
        # so the context restores it after the controlled test.
        patch.setattr(PostgresImportLeaseRepository, "try_begin_committing",
                      original_try_begin)
        _install_lock_observers("expiry_scan_after_task_wait", old_control)
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))

        def call_heartbeat() -> None:
            try:
                PostgresImportLeaseRepository.heartbeat(repository, attempt)
            except ImportLeaseLost as error:
                caught.append(type(error))

        thread = Thread(target=call_heartbeat)
        thread.start()
        try:
            assert heartbeat_paused.wait(5)
            assert thread.is_alive() and not heartbeat_invoked.is_set()
        finally:
            release_heartbeat.set()
            thread.join(5)
        assert (not thread.is_alive() and heartbeat_invoked.is_set()
                and heartbeat_calls == [attempt]
                and caught == [ImportLeaseLost])
        selected = [(stage, fields) for stage, fields in events
                    if stage in {"heartbeat_enter", "lock_backend",
                                 "lock_exception"}]
        assert ([stage for stage, _fields in selected] == [
                    "heartbeat_enter", "lock_backend", "lock_exception"]
                and selected[-1][1]["exception"] == "ImportLeaseLost"
                and len({fields["invocation_id"] for _, fields in selected}) == 1)
    assert PostgresImportLeaseRepository.try_begin_committing is original_try_begin


def test_unit_heartbeat_terminal_outer_fault_gates_restore_originals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the old and fresh _install_fault branches without PG claims."""
    from app.distributed_import_worker import IsolatedImportWorker
    from app.import_memory_publication import ImportMemoryPublicationService
    from app.import_publication_recovery import PostgresImportPublicationRecoveryRepository
    from app.postgres import PostgresDatabase
    from app.postgres_import_leases import (ImportLeaseLost,
                                            PostgresImportLeaseRepository)

    classes = ((PostgresDatabase, "transaction"),
               (ImportMemoryPublicationService, "_issue_fixed_terminal"),
               (IsolatedImportWorker, "_heartbeat"),
               (IsolatedImportWorker, "_recovery_round"),
               (IsolatedImportWorker, "_preflight"),
               (PostgresImportLeaseRepository, "heartbeat"),
               (PostgresImportLeaseRepository, "try_begin_committing"),
               (PostgresImportLeaseRepository, "complete"),
               (PostgresImportPublicationRecoveryRepository, "claim_next"))
    originals = tuple(getattr(cls, name) for cls, name in classes)
    calls: list[str] = []
    events: list[str] = []
    stages = ("fault", "heartbeat_enter", "terminal_enter",
              "old_recovery_enter", "fresh_claim_enter",
              "fresh_ordinary_enter")
    entered = {stage: Event() for stage in stages}
    released = {stage: Event() for stage in stages}
    attempt = SimpleNamespace(worker_id="old-a", lease_version=1,
        lease_token=uuid4(), user_lease=SimpleNamespace(lease_token=uuid4(),
            lease_version=1), task=SimpleNamespace(user_id="user-a",
            task_id="task-a"))
    prepared = SimpleNamespace(task=attempt.task)

    def record(stage: str, _fields: dict[str, object]) -> None:
        events.append(stage)
        (tmp_path / stage).touch()

    def pause(stage: str, fields: dict[str, object]) -> None:
        record(stage, fields)
        if stage in entered:
            entered[stage].set()
            assert released[stage].wait(5), "controlled_outer_gate_not_released"

    def control(role: str) -> SimpleNamespace:
        return SimpleNamespace(target={"user_id": "user-a",
            "task_id": "task-a", "worker_id": (
                "old-a" if role == "heartbeat_old" else "fresh-a"),
            "lock_role": role},
            matches=lambda user, task: (user, task) == ("user-a", "task-a"),
            identity=SimpleNamespace(event_path=lambda stage, _hit:
                tmp_path / stage), event=record, pause=pause)

    @contextmanager
    def transaction(_self):
        yield SimpleNamespace(connection=SimpleNamespace(
            info=SimpleNamespace(backend_pid=911)))

    def terminal(_self, *_args):
        calls.append("terminal")
        return object()

    def begin(self, _attempt, **_kwargs):
        calls.append("begin")
        with self.database.transaction():
            return True

    def complete(self, _attempt, **_kwargs):
        calls.append("complete")
        with self.database.transaction():
            raise ImportLeaseLost("controlled_complete_lost")

    def heartbeat(self, _attempt, **_kwargs):
        calls.append("heartbeat")
        with self.database.transaction():
            raise ImportLeaseLost("controlled_heartbeat_lost")

    def worker_heartbeat(self, original, _abort, _forward_abort, _deadline):
        try:
            PostgresImportLeaseRepository.heartbeat(
                SimpleNamespace(database=PostgresDatabase.__new__(
                    PostgresDatabase)), original)
        except ImportLeaseLost:
            return None

    def recovery(self, _abort, _deadline):
        calls.append("recovery:" + self.worker_id)
        return "original_recovery"

    def preflight(self, _abort, _deadline):
        calls.append("preflight:" + self.worker_id)
        return "original_preflight"

    def claim(self, worker_id, **_kwargs):
        calls.append("claim:" + worker_id)
        with self.database.transaction():
            return None

    def run_held(stage: str, operation: Callable[[], object]) -> object:
        result: list[object] = []
        failures: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(operation())
            except BaseException as error:
                failures.append(error)

        thread = Thread(target=invoke)
        thread.start()
        try:
            assert entered[stage].wait(5) and thread.is_alive()
        finally:
            released[stage].set()
            thread.join(5)
        assert not thread.is_alive()
        if failures:
            raise failures[0]
        assert len(result) == 1
        return result[0]

    with monkeypatch.context() as patch:
        for cls, name in classes:
            patch.setattr(cls, name, getattr(cls, name))
        patch.setattr(PostgresDatabase, "transaction", transaction)
        patch.setattr(ImportMemoryPublicationService,
                      "_issue_fixed_terminal", terminal)
        patch.setattr(IsolatedImportWorker, "_heartbeat", worker_heartbeat)
        patch.setattr(IsolatedImportWorker, "_recovery_round", recovery)
        patch.setattr(IsolatedImportWorker, "_preflight", preflight)
        patch.setattr(PostgresImportLeaseRepository, "heartbeat", heartbeat)
        patch.setattr(PostgresImportLeaseRepository,
                      "try_begin_committing", begin)
        patch.setattr(PostgresImportLeaseRepository, "complete", complete)
        patch.setattr(PostgresImportPublicationRecoveryRepository,
                      "claim_next", claim)
        _install_fault("pair_evidence_committed", control("heartbeat_old"))
        publication = ImportMemoryPublicationService.__new__(
            ImportMemoryPublicationService)
        assert run_held("fault", lambda:
            ImportMemoryPublicationService._issue_fixed_terminal(
                publication, object(), prepared, object(), object(), object()))
        assert calls == ["terminal"]
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        assert PostgresImportLeaseRepository.try_begin_committing(
            repository, attempt) is True
        with pytest.raises(ImportLeaseLost):
            run_held("terminal_enter", lambda:
                PostgresImportLeaseRepository.complete(repository, attempt))
        worker = SimpleNamespace(worker_id="old-a")
        assert run_held("heartbeat_enter", lambda:
            IsolatedImportWorker._heartbeat(worker, attempt, Event(),
                                            Event(), time.monotonic() + 5)) is None
        assert "heartbeat_started" in events and "heartbeat_finally" in events
        assert run_held("old_recovery_enter", lambda:
            IsolatedImportWorker._recovery_round(worker, Event(), 5.0)) == (
                "original_recovery")
        assert IsolatedImportWorker._recovery_round(
            worker, Event(), 5.0) == "original_recovery"
        assert calls == ["terminal", "begin", "complete", "heartbeat",
                         "recovery:old-a", "recovery:old-a"]
    assert originals == tuple(getattr(cls, name) for cls, name in classes)

    entered["fault"].clear()
    with monkeypatch.context() as patch:
        for cls, name in classes:
            patch.setattr(cls, name, getattr(cls, name))
        patch.setattr(PostgresDatabase, "transaction", transaction)
        patch.setattr(ImportMemoryPublicationService,
                      "_issue_fixed_terminal", terminal)
        patch.setattr(IsolatedImportWorker, "_preflight", preflight)
        patch.setattr(PostgresImportPublicationRecoveryRepository,
                      "claim_next", claim)
        _install_fault("heartbeat_terminal_after_wait", control("fresh"))
        fresh = SimpleNamespace(worker_id="fresh-a")
        recovery_repo = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        assert run_held("fresh_claim_enter", lambda:
            PostgresImportPublicationRecoveryRepository.claim_next(
                recovery_repo, "fresh-a",
                allowed_user_ids=frozenset({"user-a"}))) is None
        assert run_held("fresh_ordinary_enter", lambda:
            IsolatedImportWorker._preflight(fresh, Event(), 5.0)) == (
                "original_preflight")
        assert IsolatedImportWorker._preflight(
            fresh, Event(), 5.0) == "original_preflight"
        assert calls[-3:] == ["claim:fresh-a", "preflight:fresh-a",
                              "preflight:fresh-a"]
    assert originals == tuple(getattr(cls, name) for cls, name in classes)


def test_unit_heartbeat_terminal_observers_keep_original_calls_and_identity(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the controlled seam; only the real case can attest PG row waits."""
    from app.postgres import PostgresDatabase
    from app.postgres_import_leases import (ImportLeaseLost,
                                            PostgresImportLeaseRepository)

    events: list[tuple[str, dict[str, object]]] = []
    event_guard = Lock()
    heartbeat_entry, heartbeat_release = Event(), Event()
    terminal_entry, terminal_release = Event(), Event()
    heartbeat_original, terminal_original = Event(), Event()
    originals: list[str] = []
    errors: list[tuple[str, type[BaseException]]] = []
    attempt = SimpleNamespace(task=SimpleNamespace(user_id="user-a",
        task_id="task-a"), worker_id="old-a")
    other = SimpleNamespace(task=SimpleNamespace(user_id="user-b",
        task_id="task-b"), worker_id="other-b")

    def record(stage: str, fields: dict[str, object]) -> None:
        with event_guard:
            events.append((stage, fields))

    def pause(stage: str, fields: dict[str, object]) -> None:
        record(stage, fields)
        if stage == "heartbeat_enter":
            heartbeat_entry.set()
            assert heartbeat_release.wait(5), "controlled_heartbeat_release_missing"
        if stage == "terminal_enter":
            terminal_entry.set()
            assert terminal_release.wait(5), "controlled_terminal_release_missing"

    control = SimpleNamespace(
        target={"user_id": "user-a", "task_id": "task-a",
                "worker_id": "old-a", "lock_role": "heartbeat_old"},
        matches=lambda user, task: (user, task) == ("user-a", "task-a"),
        event=record, pause=pause)

    @contextmanager
    def fake_transaction(_self):
        yield SimpleNamespace(connection=SimpleNamespace(
            info=SimpleNamespace(backend_pid=901)))

    def begin(self, owner, *_args, **_kwargs):
        originals.append("begin:" + owner.worker_id)
        with self.database.transaction():
            return True

    def complete(self, owner, *_args, **_kwargs):
        originals.append("complete:" + owner.worker_id)
        terminal_original.set()
        with self.database.transaction():
            raise ImportLeaseLost("controlled_expired_complete")

    def heartbeat(self, owner, *_args, **_kwargs):
        originals.append("heartbeat:" + owner.worker_id)
        heartbeat_original.set()
        with self.database.transaction():
            raise ImportLeaseLost("controlled_expired_heartbeat")

    original_methods = (PostgresDatabase.transaction,
        PostgresImportLeaseRepository.try_begin_committing,
        PostgresImportLeaseRepository.complete,
        PostgresImportLeaseRepository.heartbeat)
    with monkeypatch.context() as patch:
        patch.setattr(PostgresDatabase, "transaction", fake_transaction)
        patch.setattr(PostgresImportLeaseRepository,
                      "try_begin_committing", begin)
        patch.setattr(PostgresImportLeaseRepository, "complete", complete)
        patch.setattr(PostgresImportLeaseRepository, "heartbeat", heartbeat)
        _install_lock_observers("heartbeat_terminal_after_wait", control)
        repository = SimpleNamespace(database=PostgresDatabase.__new__(
            PostgresDatabase))
        assert PostgresImportLeaseRepository.try_begin_committing(
            repository, attempt) is True

        def call(name: str, method: Callable[..., object]) -> None:
            try:
                method(repository, attempt)
            except BaseException as error:
                errors.append((name, type(error)))

        terminal_thread = Thread(target=call, args=(
            "complete", PostgresImportLeaseRepository.complete))
        heartbeat_thread = Thread(target=call, args=(
            "heartbeat", PostgresImportLeaseRepository.heartbeat))
        terminal_thread.start()
        heartbeat_thread.start()
        try:
            assert terminal_entry.wait(5) and heartbeat_entry.wait(5)
            assert (terminal_thread.is_alive() and heartbeat_thread.is_alive()
                    and not terminal_original.is_set()
                    and not heartbeat_original.is_set())
            terminal_release.set()
            assert terminal_original.wait(5)
            heartbeat_release.set()
            assert heartbeat_original.wait(5)
        finally:
            terminal_release.set()
            heartbeat_release.set()
            terminal_thread.join(5)
            heartbeat_thread.join(5)
        assert (not terminal_thread.is_alive() and not heartbeat_thread.is_alive()
                and sorted(errors) == [
                    ("complete", ImportLeaseLost),
                    ("heartbeat", ImportLeaseLost)]
                and originals == ["begin:old-a", "complete:old-a",
                                  "heartbeat:old-a"])
        selected = [(stage, fields) for stage, fields in events
                    if stage in {"lock_enter", "terminal_enter",
                                 "heartbeat_enter", "lock_backend",
                                 "lock_returned", "lock_exception"}]
        by_operation: dict[str, list[tuple[str, dict[str, object]]]] = {}
        for stage, fields in selected:
            by_operation.setdefault(fields["operation"], []).append((stage, fields))
        assert (set(by_operation) == {"lease.try_begin_committing",
                                      "lease.complete", "lease.heartbeat"}
                and [stage for stage, _ in
                     by_operation["lease.try_begin_committing"]] == [
                         "lock_enter", "lock_backend", "lock_returned"]
                and [stage for stage, _ in by_operation["lease.complete"]] == [
                    "terminal_enter", "lock_backend", "lock_exception"]
                and [stage for stage, _ in by_operation["lease.heartbeat"]] == [
                    "heartbeat_enter", "lock_backend", "lock_exception"])
        for operation, records in by_operation.items():
            assert (len({fields["invocation_id"] for _, fields in records}) == 1
                    and all((fields["user_id"], fields["task_id"]) ==
                            ("user-a", "task-a") for _, fields in records))
            for stage, fields in records:
                if stage == "lock_backend":
                    assert (fields["ordinal"] == 1
                            and fields["backend_pid"] == 901
                            and "worker_id" not in fields)
                else:
                    assert fields["worker_id"] == "old-a"
            if operation != "lease.try_begin_committing":
                assert records[-1][1]["exception"] == "ImportLeaseLost"
        assert len({records[0][1]["invocation_id"]
                    for records in by_operation.values()}) == 3
        try:
            PostgresImportLeaseRepository.complete(repository, other)
        except ImportLeaseLost:
            pass
        else:
            raise AssertionError("unrelated_original_complete_not_called")
        assert originals[-1] == "complete:other-b"
    assert original_methods == (PostgresDatabase.transaction,
        PostgresImportLeaseRepository.try_begin_committing,
        PostgresImportLeaseRepository.complete,
        PostgresImportLeaseRepository.heartbeat)


@pytest.mark.parametrize("scenario,method", (
    ("recovery_ack_after_wait", "ack_success"),
    ("recovery_abandon_after_wait", "abandon_exact")))
def test_unit_lock_observer_binds_exact_original_and_recovery_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        scenario: str, method: str) -> None:
    from app.distributed_import_worker import IsolatedImportWorker
    from app.import_publication_recovery import (
        PostgresImportPublicationRecoveryRepository, RecoveryLeaseLost)
    from app.postgres import PostgresDatabase

    events: list[tuple[str, dict[str, object]]] = []
    calls: list[tuple[str, str]] = []
    selected_started, selected_release = Event(), Event()
    matching_done = Event()
    matching_result: list[str] = []
    selected_order = Lock()
    selected_ordinals = 0
    entry_stage = "ack_enter" if method == "ack_success" else "abandon_enter"
    original_result = "proved_succeeded" if method == "ack_success" else "abandoned"

    def record(stage: str, fields: dict[str, object]) -> None:
        events.append((stage, fields))
        (tmp_path / stage).touch()

    control = SimpleNamespace(
        target={"user_id": "user-a", "task_id": "task-a",
                "worker_id": "worker-a", "lock_role": None},
        matches=lambda user, task: (user, task) == ("user-a", "task-a"),
        identity=SimpleNamespace(event_path=lambda stage, _hit: tmp_path / stage),
        pause=record, event=record)

    @contextmanager
    def fake_transaction(_self):
        yield SimpleNamespace(connection=SimpleNamespace(
            info=SimpleNamespace(backend_pid=909)))

    def original(self, claim, *_args):
        nonlocal selected_ordinals
        with self.database.transaction():
            calls.append((method, claim.worker_id))
            if claim.worker_id == "worker-a":
                with selected_order:
                    selected_ordinals += 1
                    ordinal = selected_ordinals
                if ordinal == 1:
                    selected_started.set()
                    assert selected_release.wait(3), "selected_original_stalled"
                    raise RecoveryLeaseLost("controlled_original_refusal")
            return original_result

    def original_ordinary(self, _abort, _deadline):
        calls.append(("ordinary", self.worker_id))
        return "ordinary_original"

    def original_round(self, _abort, _deadline):
        calls.append(("recovery", self.worker_id))
        return "recovery_original"

    errors: list[BaseException] = []
    with monkeypatch.context() as patch:
        patch.setattr(PostgresDatabase, "transaction", fake_transaction)
        patch.setattr(PostgresImportPublicationRecoveryRepository,
                      method, original)
        patch.setattr(IsolatedImportWorker, "_preflight", original_ordinary)
        patch.setattr(IsolatedImportWorker, "_recovery_round", original_round)
        _install_fault(scenario, control)
        repository = SimpleNamespace(database=PostgresDatabase.__new__(PostgresDatabase))
        selected = SimpleNamespace(user_id="user-a", task_id="task-a",
                                   worker_id="worker-a")
        unrelated = SimpleNamespace(user_id="user-b", task_id="task-b",
                                    worker_id="worker-b")
        bound = getattr(PostgresImportPublicationRecoveryRepository, method)

        def selected_call() -> None:
            try:
                bound(repository, selected, *([object()] if method == "ack_success" else []))
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=selected_call)
        thread.start()
        matching_thread: Thread | None = None
        try:
            assert selected_started.wait(3), "selected_original_not_entered"
            assert (bound(repository, unrelated,
                          *([object()] if method == "ack_success" else []))
                    == original_result)
            def matching_call() -> None:
                matching_result.append(bound(
                    repository, selected,
                    *([object()] if method == "ack_success" else [])))
                matching_done.set()

            matching_thread = Thread(target=matching_call)
            matching_thread.start()
            assert matching_done.wait(1), "matching_original_serialized_by_selection"
            assert matching_result == [original_result]
            assert IsolatedImportWorker._preflight(
                SimpleNamespace(worker_id="worker-a"), Event(), 1.0) is None
            assert IsolatedImportWorker._preflight(
                SimpleNamespace(worker_id="worker-b"), Event(), 1.0) == "ordinary_original"
        finally:
            selected_release.set()
            thread.join(3)
            if matching_thread is not None:
                matching_thread.join(3)
        assert not thread.is_alive() and len(errors) == 1
        assert matching_thread is not None and not matching_thread.is_alive()
        assert type(errors[0]) is RecoveryLeaseLost
        entered = [fields for stage, fields in events if stage == entry_stage]
        backends = [fields for stage, fields in events if stage == "lock_backend"]
        refused = [fields for stage, fields in events if stage == "lock_exception"]
        assert (len(entered) == len(backends) == len(refused) == 1
                and entered[0]["operation"] == f"recovery.{method}"
                and entered[0]["invocation_id"] == backends[0]["invocation_id"]
                == refused[0]["invocation_id"]
                and backends[0]["ordinal"] == 1
                and backends[0]["backend_pid"] == 909
                and refused[0]["exception"] == "RecoveryLeaseLost")
        assert IsolatedImportWorker._recovery_round(
            SimpleNamespace(worker_id="worker-a"), Event(), 1.0) == "recovery_original"
        assert [stage for stage, _ in events].count("post_refusal_recovery") == 1
        assert calls == [(method, "worker-a"), (method, "worker-b"),
                         (method, "worker-a"),
                         ("ordinary", "worker-b"), ("recovery", "worker-a")]


def test_unit_lock_upload_uses_original_bounded_post_and_safe_record(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    instances: list[_ControlledUploadClient] = []

    async def reply(path: str, **kwargs):
        calls.append((path, kwargs))
        return httpx.Response(202, json={"tasks": [{"task_id": str(uuid4())}]})

    def make_client(**kwargs):
        assert kwargs["timeout"] is None
        client = _ControlledUploadClient(reply)
        instances.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        for scenario in ("recovery_ack_after_wait",
                         "recovery_abandon_after_wait",
                         "queue_capture_before_user_wait",
                         "expiry_scan_after_task_wait",
                         "heartbeat_terminal_after_wait"):
            response = _post_valid_upload_once(
                resources, reference, time.monotonic() + 2,
                case=scenario, stage="lock",
                cookies={"zhiyan_session": "private-cookie"},
                headers={"X-CSRF-Token": "private-token"},
                files={"files": ("lock.txt", b"private-source")})
            assert response.status_code == 202
            record = _read_json(tmp_path / f"{scenario}-lock-upload-diagnostic.json")
            assert record["case"] == scenario and record["stage"] == "lock"
            assert record["status_code"] == 202 and record["elapsed_ms"] >= 0
            assert "private" not in json.dumps(record)
    assert len(instances) == len(calls) == 5
    assert all(client.calls == client.close_calls == 1 and client.closed
               for client in instances)
    assert all(path == "/api/v1/imports"
               and kwargs["files"] == {"files": ("lock.txt", b"private-source")}
               for path, kwargs in calls)


def test_unit_recovery_fault_wrappers_compose_after_forward(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.distributed_import_worker import IsolatedImportWorker
    from app.import_document_publication import ImportDocumentPublicationService
    from app.import_memory_publication import (
        DurablePublicationUnknown, ImportMemoryPublicationService)
    from app.import_publication_proof import (
        ImportPublicationProofService, PublicationProofUnknown)
    from app.import_publication_recovery import (
        PostgresImportPublicationRecoveryRepository, RecoveryLeaseLost)

    scenarios = ("unknown_document_put", "unknown_terminal_reply",
                 "pause_live_capture", "pause_expired_capture",
                 "recovery_natural_retry")
    for scenario in scenarios:
        with monkeypatch.context() as patch:
            directory = tmp_path / scenario
            directory.mkdir()
            events: list[str] = []
            originals: list[str] = []

            def record(stage, _fields):
                events.append(stage)
                (directory / stage).touch()

            control = SimpleNamespace(
                target={"user_id": "user", "task_id": "task",
                        "worker_id": "old"},
                matches=lambda user, task: (user, task) == ("user", "task"),
                identity=SimpleNamespace(
                    event_path=lambda stage, _hit: directory / stage,
                    release_path=lambda stage, _hit: directory / (stage + "-released")),
                event=record, pause=record)
            key = SimpleNamespace(user_id="user", task_id="task",
                                  task_lease_version=1)
            attempt = SimpleNamespace(
                worker_id="old", lease_version=1, lease_token=uuid4(),
                user_lease=SimpleNamespace(lease_token=uuid4(), lease_version=1),
                task=SimpleNamespace(user_id="user", task_id="task"))
            claim = SimpleNamespace(
                user_id="user", task_id="task", queue_version=1,
                recovery_version=1, queue_claim_token=uuid4(),
                recovery_token=uuid4(),
                queue_expires_at=datetime.now(timezone.utc),
                recovery_expires_at=datetime.now(timezone.utc))

            class ClockCursor:
                def execute(self, _statement):
                    return self

                def fetchone(self):
                    return {"now": datetime.now(timezone.utc)}

            repository = SimpleNamespace(database=SimpleNamespace(
                transaction=lambda: nullcontext(ClockCursor())))
            issue = SimpleNamespace(key=key)
            publication = SimpleNamespace(_require_live_issue=lambda _live: issue)

            def original_document(_self, _planned, _attempt, *, live=None):
                originals.append("document")
                return object()

            def original_terminal(_self, _live):
                originals.append("terminal")
                return object()

            def original_proof(_self, _user, _task, _version):
                originals.append("proof")
                return object()

            def original_transient(_self, _claim):
                originals.append("transient")
                return "retry_later"

            def original_capture(_self, _claim):
                originals.append("capture")
                if scenario == "pause_expired_capture":
                    raise RecoveryLeaseLost("controlled_expired_capture")
                return "proved_succeeded"

            def original_round(_self, _abort, _deadline):
                originals.append("round")
                if scenario.startswith("pause_"):
                    return PostgresImportPublicationRecoveryRepository.prove_or_hold(
                        repository, claim)
                if scenario == "recovery_natural_retry":
                    try:
                        proof = ImportPublicationProofService.prove_exact(
                            object(), "user", "task", 1)
                    except PublicationProofUnknown:
                        return PostgresImportPublicationRecoveryRepository._record_transient(
                            repository, claim)
                    return proof
                return None

            patch.setattr(ImportDocumentPublicationService,
                          "_write_planned_document", original_document)
            patch.setattr(ImportMemoryPublicationService,
                          "_execute_live_publication", original_terminal)
            patch.setattr(ImportPublicationProofService,
                          "prove_exact", original_proof)
            patch.setattr(PostgresImportPublicationRecoveryRepository,
                          "prove_or_hold", original_capture)
            patch.setattr(PostgresImportPublicationRecoveryRepository,
                          "_record_transient", original_transient)
            patch.setattr(IsolatedImportWorker, "_recovery_round", original_round)
            patch.setattr(IsolatedImportWorker, "_heartbeat", lambda *_args: None)
            patch.setattr(IsolatedImportWorker, "run_one_ordinary",
                          lambda self: originals.append("ordinary") or True)
            _install_fault(scenario, control)

            if scenario == "unknown_document_put":
                with pytest.raises(RuntimeError, match="p6_lost_document_put_reply"):
                    ImportDocumentPublicationService._write_planned_document(
                        object(), object(), attempt)
                assert originals == ["document"]
                assert ImportDocumentPublicationService._write_planned_document(
                    object(), object(), attempt) is not None
                assert originals == ["document", "document"]
            else:
                with pytest.raises(DurablePublicationUnknown):
                    ImportMemoryPublicationService._execute_live_publication(
                        publication, object())
                assert originals == ["terminal"]
                assert ImportMemoryPublicationService._execute_live_publication(
                    publication, object()) is not None
                assert originals == ["terminal", "terminal"]
            assert events.count("fault") == 1

            worker = SimpleNamespace(worker_id="old", _lock=Lock())
            heartbeat_thread = Thread(target=lambda: None)
            heartbeat_thread.start()
            heartbeat_thread.join(5)
            worker._lanes = {"heartbeat": SimpleNamespace(thread=heartbeat_thread)}
            record("heartbeat_finally", {})
            if scenario == "pause_expired_capture":
                with pytest.raises(RecoveryLeaseLost):
                    IsolatedImportWorker._recovery_round(worker, Event(), 1.0)
                assert events.count("recovery_exception") == 1
                assert events.count("refused_recovery") == 1
            else:
                IsolatedImportWorker._recovery_round(worker, Event(), 1.0)
            assert originals.count("round") == 1
            if scenario.startswith("unknown_"):
                assert (events.count("old_recovery") == 1
                        and events.count("heartbeat_thread_exited") == 1)
                assert IsolatedImportWorker.run_one_ordinary(worker) is False
                assert IsolatedImportWorker.run_one_ordinary(worker) is False
                assert (events.count("ordinary_deferred") == 1
                        and originals.count("ordinary") == 0)
                assert IsolatedImportWorker.run_one_ordinary(
                    SimpleNamespace(worker_id="other")) is True
                assert originals.count("ordinary") == 1
            elif scenario.startswith("pause_"):
                assert (events.count("initial_recovery") == 1
                        and events.count("recovery_pause") == 1
                        and originals.count("capture") == 1)
            else:
                assert (events.count("proof_unknown") == 1
                        and events.count("transient_recorded") == 1
                        and originals.count("proof") == 1
                        and originals.count("transient") == 1)
            first_events = tuple(events)
            if scenario == "pause_expired_capture":
                with pytest.raises(RecoveryLeaseLost):
                    IsolatedImportWorker._recovery_round(worker, Event(), 1.0)
            else:
                IsolatedImportWorker._recovery_round(worker, Event(), 1.0)
            assert tuple(events) == first_events
            assert originals.count("round") == 2
            if scenario.startswith("unknown_"):
                (directory / "old_recovery-released").touch()
                assert IsolatedImportWorker.run_one_ordinary(worker) is True
                assert (originals.count("ordinary") == 2
                        and events.count("ordinary_deferred") == 1)
            if scenario.startswith("pause_"):
                assert originals.count("capture") == 2
            elif scenario == "recovery_natural_retry":
                assert (originals.count("proof") == 2
                        and originals.count("transient") == 1)


def test_unit_late_candidate_and_old_recovery_have_independent_barriers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qdrant_client import QdrantClient
    from app.distributed_import_worker import IsolatedImportWorker
    from app.import_publication_evidence import PostgresImportPublicationEvidenceRepository
    from app.postgres_vector_generations import PostgresVectorGenerationAuthority
    from app.vector_generation_service import VectorGenerationService
    import app.import_memory_publication as publication

    scope = SimpleNamespace(tenant_id="user", vector_kind="rag", namespace="pdf_user",
        index_key="index", key=("user", "rag", "pdf_user", "index"),
        identity=SimpleNamespace(physical_collection="collection",
                                 to_dict=lambda: {"index": "identity"}))
    generation = str(uuid4())
    task_token, user_token, document_id = uuid4(), uuid4(), str(uuid4())
    issue = SimpleNamespace(key=SimpleNamespace(user_id="user", task_id="task",
                                task_lease_version=1),
                            original_attempt=SimpleNamespace(worker_id="old",
                                lease_version=1, lease_token=task_token,
                                user_lease=SimpleNamespace(lease_token=user_token,
                                                           lease_version=1),
                                task=SimpleNamespace(document_id=document_id)),
                            intent_hash="intent",
                            plan=SimpleNamespace(rag_scope=scope))
    other_issue = SimpleNamespace(**{**vars(issue), "key":
        SimpleNamespace(user_id="user", task_id="other-task",
                        task_lease_version=1)})
    target_live, other_live = object(), object()
    frozen = {"candidate_id": generation, "namespace": "pdf_user",
              "index_key": "index", "identity": {"index": "identity"}}
    evidence = SimpleNamespace(intent_hash="intent", intent={
        "attempt": {"user_id": "user", "task_id": "task",
                    "task_lease_version": 1, "worker_id": "old",
                    "task_lease_token": str(task_token),
                    "user_lease_token": str(user_token), "user_lease_version": 1,
                    "document_id": document_id},
        "scopes": {"rag": frozen}})
    entered = {"fault": Event(), "old_recovery": Event()}
    released = {"fault": Event(), "old_recovery": Event()}
    observed: list[tuple[str, dict[str, object]]] = []

    def pause(stage: str, fields: dict[str, object]) -> None:
        (tmp_path / stage).touch()
        entered[stage].set()
        assert released[stage].wait(5), "controlled_barrier_timeout"

    def record_event(stage: str, fields: dict[str, object]) -> None:
        observed.append((stage, fields))
        (tmp_path / stage).touch()

    control = SimpleNamespace(target={"user_id": "user", "task_id": "task",
                                       "worker_id": "old"},
        matches=lambda user, task: (user, task) == ("user", "task"),
        identity=SimpleNamespace(event_path=lambda stage, hit: tmp_path / stage),
        pause=pause, event=record_event)
    calls: list[tuple[str, tuple[object, ...]]] = []

    def original_upsert(self, *args, **kwargs):
        calls.append(("upsert", (self, args, kwargs)))
        return SimpleNamespace(status=SimpleNamespace(value="completed"))

    def original_recovery(self, abort, deadline):
        calls.append(("recovery", (self.worker_id, abort, deadline)))
        return None

    monkeypatch.setattr(QdrantClient, "upsert", original_upsert)
    monkeypatch.setattr(IsolatedImportWorker, "_recovery_round", original_recovery)
    monkeypatch.setattr(IsolatedImportWorker, "_heartbeat",
                        lambda *_args: None)
    monkeypatch.setattr(VectorGenerationService, "_prepare_sealed",
                        VectorGenerationService._prepare_sealed)
    def original_require(_self, _live, _scope, _generation, *, operation):
        if operation == "verify":
            raise RuntimeError("controlled_failed_check")

    monkeypatch.setattr(PostgresVectorGenerationAuthority, "require_candidate",
                        original_require)
    monkeypatch.setattr(PostgresImportPublicationEvidenceRepository,
                        "_read_exact_in_cursor", lambda *_args: evidence)
    monkeypatch.setattr(publication, "_require_live_issuer",
                        lambda live: (None, issue if live is target_live else other_issue))
    _install_fault("late_candidate_bytes", control)
    authority = SimpleNamespace(database=SimpleNamespace(
        transaction=lambda: nullcontext(object())))
    abort = object()
    old = SimpleNamespace(worker_id="old", _lock=Lock())
    heartbeat_attempt = SimpleNamespace(
        task=SimpleNamespace(user_id="user", task_id="task"),
        worker_id="old", lease_version=1, lease_token=task_token,
        user_lease=SimpleNamespace(lease_token=user_token, lease_version=1))
    heartbeat_thread = Thread(target=lambda: IsolatedImportWorker._heartbeat(
        old, heartbeat_attempt, abort, abort, 123.0))
    old._lanes = {"heartbeat": SimpleNamespace(thread=heartbeat_thread)}
    heartbeat_thread.start()
    heartbeat_thread.join(5)
    assert not heartbeat_thread.is_alive()
    assert (tmp_path / "heartbeat_started").exists()
    assert (tmp_path / "heartbeat_finally").exists()
    replacement = SimpleNamespace(worker_id="replacement")
    faults: list[Exception] = []
    from hello_agents.memory.storage.generation_vector_store import _physical_id
    selected_point = SimpleNamespace(id=_physical_id(scope, UUID(generation), "logical"),
        payload={"_gv_tenant": "user", "_gv_kind": "rag",
                 "_gv_namespace": "pdf_user", "_gv_index": "index",
                 "_gv_generation": generation, "_gv_logical_id": "logical"})
    selected_client = object()
    selected_points = [selected_point]
    mixed_point = SimpleNamespace(id=selected_point.id, payload={
        **selected_point.payload, "_gv_tenant": "foreign"})

    def target_upsert():
        try:
            PostgresVectorGenerationAuthority.require_candidate(
                authority, target_live, scope, UUID(generation), operation="upload")
            PostgresVectorGenerationAuthority.require_candidate(
                authority, other_live, scope, UUID(generation), operation="upload")
            QdrantClient.upsert(object(), collection_name="collection",
                                points=[selected_point], wait=True)
            PostgresVectorGenerationAuthority.require_candidate(
                authority, target_live, scope, UUID(generation), operation="upload")
            unrelated = SimpleNamespace(**{**vars(scope), "tenant_id": "other"})
            PostgresVectorGenerationAuthority.require_candidate(
                authority, target_live, unrelated, UUID(generation), operation="upload")
            QdrantClient.upsert(object(), collection_name="collection",
                                points=[selected_point], wait=True)
            PostgresVectorGenerationAuthority.require_candidate(
                authority, target_live, scope, UUID(generation), operation="upload")
            try:
                PostgresVectorGenerationAuthority.require_candidate(
                    authority, target_live, scope, UUID(generation), operation="verify")
            except RuntimeError as error:
                assert str(error) == "controlled_failed_check"
            QdrantClient.upsert(object(), collection_name="collection",
                                points=[selected_point], wait=True)
            PostgresVectorGenerationAuthority.require_candidate(
                authority, target_live, scope, UUID(generation), operation="upload")
            QdrantClient.upsert(selected_client, collection_name="collection",
                                points=[selected_point, mixed_point], wait=True)
            QdrantClient.upsert(selected_client, collection_name="collection",
                                points=selected_points, wait=True)
            assert [name for name, _args in calls].count("upsert") == 5
            PostgresVectorGenerationAuthority.require_candidate(
                authority, target_live, scope, UUID(generation), operation="upload")
            QdrantClient.upsert(selected_client, collection_name="collection",
                                points=selected_points, wait=True)
            QdrantClient.upsert(selected_client, collection_name="collection",
                                points=selected_points, wait=True)
        except Exception as error:
            faults.append(error)

    def target_recovery():
        try:
            IsolatedImportWorker._recovery_round(old, abort, 123.0)
        except Exception as error:
            faults.append(error)

    upload_thread = Thread(target=target_upsert)
    recovery_thread = Thread(target=target_recovery)
    try:
        upload_thread.start()
        assert entered["fault"].wait(5)
        recovery_thread.start()
        assert entered["old_recovery"].wait(5)
        assert [name for name, _args in calls].count("upsert") == 5
        assert [name for name, _args in calls].count("recovery") == 0
        IsolatedImportWorker._recovery_round(replacement, abort, 456.0)
        IsolatedImportWorker._recovery_round(old, abort, 789.0)
        QdrantClient.upsert(object(), collection_name="collection",
                            points=[SimpleNamespace(id="other")], wait=True)
        assert [name for name, _args in calls].count("recovery") == 2
        assert [name for name, _args in calls].count("upsert") == 6
        assert ("old_recovery_returned", {"worker_id": "old", "outcome": "NoneType"}) \
            not in observed
    finally:
        released["old_recovery"].set()
        released["fault"].set()
        upload_thread.join(5)
        recovery_thread.join(5)
    assert not upload_thread.is_alive() and not recovery_thread.is_alive()
    assert not faults
    assert [name for name, _args in calls].count("upsert") == 8
    assert [name for name, _args in calls].count("recovery") == 3
    assert ("recovery", ("old", abort, 123.0)) in calls
    selected_call = [args for name, args in calls if name == "upsert"][-2]
    assert selected_call[0] is selected_client
    assert selected_call[1] == ()
    assert selected_call[2]["collection_name"] == "collection"
    assert selected_call[2]["points"] is selected_points
    assert selected_call[2]["points"][0] is selected_point
    assert selected_call[2]["wait"] is True
    assert sum(name == "candidate_admitted" for name, _fields in observed) == 1
    assert any(name == "original_returned" for name, _fields in observed)
    assert any(name == "old_recovery_returned" for name, _fields in observed)


def test_unit_late_upsert_requires_exact_batch_and_fresh_admission(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import hello_agents.memory.storage.generation_vector_store as generations

    scope = SimpleNamespace(tenant_id="user", vector_kind="rag", namespace="pdf_user",
        index_key="index", identity=SimpleNamespace(
            physical_collection="collection", to_dict=lambda: {"profile": "exact"}))
    generation = str(uuid4())
    point = SimpleNamespace(id="physical", payload={
        "_gv_tenant": "user", "_gv_kind": "rag", "_gv_namespace": "pdf_user",
        "_gv_index": "index", "_gv_generation": generation,
        "_gv_logical_id": "logical"})
    monkeypatch.setattr(generations, "_physical_id", lambda *_args: "physical")

    def matches(candidate=scope, uuid=generation, thread=None,
                collection="collection", points=None, wait=True, args=()):
        return _matching_late_upsert(candidate, uuid,
            get_ident() if thread is None else thread, collection,
            [point] if points is None else points, wait, args)

    assert matches()
    assert not matches(candidate=SimpleNamespace(
        **{**vars(scope), "vector_kind": "episode"}))
    assert not matches(candidate=SimpleNamespace(
        **{**vars(scope), "namespace": "other"}))
    assert not matches(uuid=str(uuid4()))
    assert not matches(thread=-1)
    assert not matches(collection="other")
    assert not matches(points=[])
    assert not matches(wait=False)
    assert not matches(args=("unexpected",))
    assert not matches(points=[SimpleNamespace(id="wrong", payload=point.payload)])
    foreign = SimpleNamespace(id="physical", payload={
        **point.payload, "_gv_tenant": "other"})
    assert not matches(points=[point, foreign])


class _ControlledUploadClient:
    def __init__(self, handler: Callable[..., Any],
                 close_handler: Callable[[], Any] | None = None) -> None:
        self.handler = handler
        self.close_handler = close_handler
        self.calls = 0
        self.close_calls = 0
        self.closed = False

    async def aclose(self):
        self.close_calls += 1
        if self.close_handler is not None:
            await self.close_handler()
        else:
            await asyncio.sleep(0)
        self.closed = True

    async def post(self, path: str, **kwargs):
        self.calls += 1
        return await self.handler(path, **kwargs)

    async def get(self, path: str, **kwargs):
        self.calls += 1
        return await self.handler(path, **kwargs)


def test_unit_document_read_single_call_status_and_safe_ledger(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    document_id = "d3ae818b-87b6-445a-9068-258bf841d996"
    seen: list[tuple[str, dict[str, object]]] = []
    statuses = iter((200, 404))
    instances: list[_ControlledUploadClient] = []

    async def reply(path, **kwargs):
        seen.append((path, kwargs))
        return httpx.Response(next(statuses), content=b"document")

    def make_client(**kwargs):
        assert kwargs["base_url"] == reference.base_url
        assert kwargs["timeout"] is None
        instance = _ControlledUploadClient(reply)
        instances.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        for stage, status in (("text-own", 200), ("text-foreign", 404)):
            response = _get_document_once(resources, reference,
                time.monotonic() + 1, case="two_api", stage=stage,
                document_id=document_id, cookie="private-cookie")
            assert response.status_code == status
            assert response.content == b"document"
            path = tmp_path / f"two_api-{stage}-document-diagnostic.json"
            assert _read_json(path)["status_code"] == status
            assert "private-cookie" not in path.read_text(encoding="utf-8")
    assert len(instances) == 2
    assert all(client.calls == 1 and client.closed for client in instances)
    assert seen == [(f"/api/v1/documents/{document_id}",
                     {"cookies": {"zhiyan_session": "private-cookie"}})] * 2


def test_unit_document_read_deadline_cancels_request_and_closes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled: list[bool] = []

    async def wait_forever(_path, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    instance = _ControlledUploadClient(wait_forever)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: instance)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        with pytest.raises(TimeoutError):
            _get_document_once(resources, reference, time.monotonic() + 0.2,
                case="api_death", stage="survivor-own",
                document_id="d3ae818b-87b6-445a-9068-258bf841d996",
                cookie="private-cookie")
    assert cancelled == [True]
    assert instance.calls == instance.close_calls == 1 and instance.closed
    record = _read_json(tmp_path / "api_death-survivor-own-document-diagnostic.json")
    assert record["exception_class"] == "TimeoutError"
    assert "status_code" not in record


def test_unit_document_read_expired_case_does_not_create_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def make_client(**_kwargs):
        raise AssertionError("unexpected_client")

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        with pytest.raises(TimeoutError, match="real_case_deadline"):
            _get_document_once(resources, reference, time.monotonic() - 1,
                case="two_api", stage="text-own",
                document_id="d3ae818b-87b6-445a-9068-258bf841d996",
                cookie="private-cookie")
    assert list(tmp_path.iterdir()) == []


def test_unit_startup_exit_marker_is_untrusted_and_private() -> None:
    stderr = b"Unable to create process using SECRET_PATH with private-token"
    owner = SimpleNamespace(output=lambda: (b"", stderr, (False, False)))
    detail = _startup_exit_diagnostic(owner)
    assert detail == {"stderr_marker": "launcher_unable_to_create_process_observed",
                      "stderr_retained_bytes": len(stderr),
                      "stderr_truncated": False,
                      "stderr_sha256": hashlib.sha256(stderr).hexdigest()}
    assert "SECRET_PATH" not in json.dumps(detail)
    assert "private-token" not in json.dumps(detail)


def test_unit_valid_upload_single_call_and_status_ledger(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instances: list[_ControlledUploadClient] = []
    statuses = iter((202, 409))
    seen: list[tuple[object, object, object, object, object]] = []

    async def reply(path, **kwargs):
        seen.append((path, kwargs.get("cookies"), kwargs.get("headers"),
                     kwargs.get("files"), kwargs.keys()))
        return httpx.Response(next(statuses), content=b"response-body")

    def make_client(**kwargs):
        assert kwargs["base_url"] == reference.base_url
        assert kwargs["timeout"] is None
        client = _ControlledUploadClient(reply)
        instances.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        for stage, expected in (("text", 202), ("pdf", 409)):
            response = _post_valid_upload_once(resources, reference,
                time.monotonic() + 1, case="two_api", stage=stage,
                cookies={"zhiyan_session": "in-memory-only"},
                headers={"X-CSRF-Token": "in-memory-only"},
                files={"files": ("sample.txt", b"sample")})
            assert response.status_code == expected
            assert response.content == b"response-body"
            record = _read_json(tmp_path / f"two_api-{stage}-upload-diagnostic.json")
            assert set(record) == {"case", "stage", "status_code", "elapsed_ms"}
            assert record["status_code"] == expected
    assert len(instances) == 2
    assert all(client.calls == 1 and client.closed for client in instances)
    assert len(seen) == 2
    assert all(path == "/api/v1/imports" and
               cookies == {"zhiyan_session": "in-memory-only"} and
               headers == {"X-CSRF-Token": "in-memory-only"} and
               files == {"files": ("sample.txt", b"sample")} and
               set(keys) == {"cookies", "headers", "files"}
               for path, cookies, headers, files, keys in seen)


def test_unit_valid_upload_hard_deadline_cancels_and_closes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request_tasks: list[asyncio.Task] = []
    handler_finally: list[bool] = []

    async def wait_forever(_path, **_kwargs):
        request_tasks.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        finally:
            handler_finally.append(True)

    client = _ControlledUploadClient(wait_forever)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            _post_valid_upload_once(resources, reference, started + 0.4,
                case="api_death", stage="survivor", cookies={}, headers={},
                files={"files": ("sample.txt", b"sample")})
    assert time.monotonic() - started < 2
    assert client.calls == 1 and client.closed
    assert handler_finally == [True]
    assert len(request_tasks) == 1 and request_tasks[0].done()
    record = _read_json(tmp_path / "api_death-survivor-upload-diagnostic.json")
    assert set(record) == {"case", "stage", "exception_class", "elapsed_ms"}
    assert record["exception_class"] == "TimeoutError"


def test_unit_valid_upload_preserves_transport_error_without_raw_diagnostic(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = httpx.ReadTimeout("private-marker")

    async def fail(_path, **_kwargs):
        raise error

    client = _ControlledUploadClient(fail)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        with pytest.raises(httpx.ReadTimeout) as caught:
            _post_valid_upload_once(resources, reference,
                time.monotonic() + 1, case="two_api", stage="text",
                cookies={}, headers={}, files={"files": ("sample.txt", b"sample")})
    assert caught.value is error
    assert client.calls == 1 and client.closed
    path = tmp_path / "two_api-text-upload-diagnostic.json"
    record = _read_json(path)
    assert set(record) == {"case", "stage", "exception_class", "elapsed_ms"}
    assert record["exception_class"] == "httpx.ReadTimeout"
    assert "private-marker" not in path.read_text(encoding="utf-8")


def test_unit_valid_upload_expired_deadline_does_not_send(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def make_client(**_kwargs):
        calls.append(True)
        raise AssertionError("unexpected_client")

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        with pytest.raises(TimeoutError, match="real_case_deadline"):
            _post_valid_upload_once(resources, reference,
                time.monotonic() - 1, case="two_api", stage="text",
                cookies={}, headers={}, files={"files": ("sample.txt", b"sample")})
    assert calls == []
    assert list(tmp_path.glob("*-upload-diagnostic.json")) == []


def test_unit_valid_upload_rejects_sync_completion_after_deadline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def block_loop(_path, **_kwargs):
        time.sleep(0.7)
        return httpx.Response(202)

    client = _ControlledUploadClient(block_loop)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        with pytest.raises(TimeoutError, match="valid_upload_deadline"):
            _post_valid_upload_once(resources, reference,
                time.monotonic() + 0.5, case="two_api", stage="text",
                cookies={}, headers={}, files={"files": ("sample.txt", b"sample")})
    assert client.calls == 1 and client.closed
    record = _read_json(tmp_path / "two_api-text-upload-diagnostic.json")
    assert record["exception_class"] == "TimeoutError"
    assert "status_code" not in record


def test_unit_valid_upload_closes_after_post_deadline_without_accepting_response(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def reply(_path, **_kwargs):
        return httpx.Response(202, content=b"complete")

    async def slow_close():
        await asyncio.sleep(0.4)

    client = _ControlledUploadClient(reply, slow_close)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        with pytest.raises(TimeoutError, match="valid_upload_deadline"):
            _post_valid_upload_once(resources, reference,
                time.monotonic() + 0.2, case="two_api", stage="text",
                cookies={}, headers={}, files={"files": ("sample.txt", b"sample")})
    assert client.calls == client.close_calls == 1 and client.closed
    record = _read_json(tmp_path / "two_api-text-upload-diagnostic.json")
    assert record["exception_class"] == "TimeoutError"
    assert "status_code" not in record


def test_unit_valid_upload_close_timeout_is_hard_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    close_cancelled: list[bool] = []

    async def reply(_path, **_kwargs):
        return httpx.Response(202)

    async def stalled_close():
        try:
            await asyncio.Event().wait()
        finally:
            close_cancelled.append(True)

    client = _ControlledUploadClient(reply, stalled_close)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(sys.modules[__name__], "_VALID_UPLOAD_CLOSE_SECONDS", 0.1)
    resources = SimpleNamespace(directory=tmp_path)
    with httpx.Client(base_url="http://127.0.0.1:1") as reference:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            _post_valid_upload_once(resources, reference, started + 1,
                case="api_death", stage="survivor", cookies={}, headers={},
                files={"files": ("sample.txt", b"sample")})
    assert time.monotonic() - started < 0.8
    assert client.calls == client.close_calls == 1 and not client.closed
    assert close_cancelled == [True]
    record = _read_json(tmp_path / "api_death-survivor-upload-diagnostic.json")
    assert record["exception_class"] == "TimeoutError"
    assert "status_code" not in record


def test_unit_control_identity_and_release(tmp_path: Path) -> None:
    directory = tmp_path
    assert directory.resolve().is_relative_to((REPO / ".runtime").resolve())
    waiter: Thread | None = None
    identity: ControlIdentity | None = None
    try:
        identity = ControlIdentity(uuid4().hex, "unit_control", "neutral", directory)
        control = ChildControl(identity)
        failures: list[BaseException] = []

        def wait_release() -> None:
            try:
                control.pause("ready", {"safe_marker": "arrived"}, timeout=2)
            except BaseException as error:
                failures.append(error)

        waiter = Thread(target=wait_release)
        waiter.start()
        event = _await_event(identity, "ready", timeout=2)
        assert event["safe_marker"] == "arrived"
        assert event["pid"] == os.getpid()
        _release(identity, "ready")
        waiter.join(2)
        assert not waiter.is_alive() and not failures
        with pytest.raises(FileExistsError):
            _release(identity, "ready")
        with pytest.raises(ValueError, match="override"):
            control.event("unsafe", {"nonce": "forged"})
        with pytest.raises(TimeoutError, match="release"):
            control.pause("no_release", timeout=0.05)
    finally:
        if waiter is not None and waiter.ident is not None:
            if waiter.is_alive() and identity is not None:
                _release_observed(identity, ("ready",))
            waiter.join(2.5)
            assert not waiter.is_alive(), "control_waiter_still_active"
        _retire_owned_jobs()


def test_unit_child_refuses_unknown_identity(tmp_path: Path) -> None:
    directory = tmp_path
    try:
        with pytest.raises(ValueError, match="invalid_control_identity"):
            ControlIdentity("bad", "unit_control", "neutral", directory)
        with pytest.raises(ValueError, match="invalid_control_role"):
            ControlIdentity(uuid4().hex, "unit_control", "api-v2", directory)
        with pytest.raises(SystemExit):
            _child_main(["--worker-child", "unknown_scenario",
                         str(directory), uuid4().hex, "unknown_scenario"])
        with pytest.raises(SystemExit):
            _child_main(["--neutral-child", str(REPO), uuid4().hex, "unit_control"])
    finally:
        _retire_owned_jobs()


def test_unit_unicode_environment_and_win32_types() -> None:
    assert _unicode_environment({"Z": "汉字 space", "A": "x"})[:] == (
        "A=x\x00Z=汉字 space\x00\x00")
    assert _unicode_environment({"A": "x"})[:][-2:] == "\x00\x00"
    for env in ({"A=B": "x"}, {"A": "x\x00y"}, {"": "x"}):
        with pytest.raises(ValueError, match="environment"):
            _unicode_environment(env)
    if os.name == "nt":
        assert ctypes.sizeof(HANDLE) == ctypes.sizeof(ctypes.c_void_p)
        assert ctypes.sizeof(ULONG_PTR) == ctypes.sizeof(ctypes.c_void_p)
        assert ctypes.sizeof(STARTUPINFOEXW) > ctypes.sizeof(STARTUPINFOW)
        assert JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
    for invalid in (None, 0, -1, ctypes.c_void_p(-1)):
        with pytest.raises(ValueError, match="invalid_handle"):
            _handle_value(invalid)


@pytest.mark.skipif(os.name != "nt", reason="Win32 runtime identity")
def test_unit_runtime_identity_pending_retry_and_fail_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_owner()
    owner.role = "worker"
    owner.launcher_pid = 77
    owner.launcher_identity = (str(PYTHON), 1)
    owner.runtime = {77: (1, str(PYTHON), 1)}
    owner.manifest_nonce = uuid4().hex
    owner.manifest_path = tmp_path / "worker.manifest.json"
    _atomic_json(owner.manifest_path, {"nonce": owner.manifest_nonce,
                                       "expected_role": owner.role})
    monkeypatch.setattr(owner, "active_count", lambda: 1)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    created = 116444736000000000 + 1700000000 * 10000000
    image = str(Path(sys._base_executable).resolve(strict=True))

    def os_record(command: str, _deadline: float, *,
                  cap_seconds: float | None = 10) -> dict[str, object]:
        assert cap_seconds == 10 and _deadline > time.monotonic()
        found = re.search(r"ProcessId=(\d+)", command)
        assert found is not None and int(found.group(1)) in {88, 99}
        return {"pid": int(found.group(1)), "parent": 77,
                "image": image,
                "created": datetime.fromtimestamp(1700000000, timezone.utc).isoformat()}

    monkeypatch.setattr(sys.modules[__name__], "_powershell_record", os_record)
    polls = 0

    def observe_after_startup() -> set[int]:
        nonlocal polls
        polls += 1
        if polls >= 2:
            owner.runtime[88] = (2, image, created)
            return {77, 88}
        return {77}

    monkeypatch.setattr(owner, "observe", observe_after_startup)
    identity = _ready_worker(owner, time.monotonic() + 1)
    assert identity["pid"] == 88 and polls >= 3
    assert _read_json(owner.manifest_path)["actual_os_identity"] == identity

    owner.runtime[99] = (3, image, created)
    monkeypatch.setattr(owner, "observe", lambda: {77, 88, 99})
    with pytest.raises(AssertionError, match="runtime_pid_not_unique"):
        _ready_worker(owner, time.monotonic() + 1)

    owner.runtime.pop(99)
    monkeypatch.setattr(owner, "observe", lambda: {77, 88})
    monkeypatch.setattr(sys.modules[__name__], "_powershell_record",
                        lambda command, deadline, **kwargs: {
                            **os_record(command, deadline, **kwargs), "parent": 66})
    with pytest.raises(AssertionError, match="runtime_os_identity_mismatch"):
        _ready_worker(owner, time.monotonic() + 1)

    owner.runtime.pop(88)
    monkeypatch.setattr(owner, "observe", lambda: {77})
    with pytest.raises(TimeoutError, match="real_case_deadline"):
        _ready_worker(owner, time.monotonic() - 1)
    monkeypatch.setattr(owner, "active_count", lambda: 0)
    monkeypatch.setattr(owner, "output", lambda: (b"", b"", (False, False)))
    with pytest.raises(AssertionError, match="worker_exited_before_identity"):
        _ready_worker(owner, time.monotonic() + 1)
    assert owner.startup_exit_diagnostic == {
        "stderr_marker": "no_launcher_marker_observed",
        "stderr_retained_bytes": 0, "stderr_truncated": False,
        "stderr_sha256": hashlib.sha256(b"").hexdigest()}


def test_unit_powershell_record_respects_startup_deadline_and_rejects_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []

    def output(_argv: list[str], **kwargs: object) -> str:
        calls.append(float(kwargs["timeout"]))
        return '{"pid":88}'

    monkeypatch.setattr(subprocess, "check_output", output)
    near = time.monotonic() + 2
    assert _powershell_record("safe", near) == {"pid": 88}
    assert 0 < calls[-1] <= 2
    assert _powershell_record("safe", near, cap_seconds=None) == {"pid": 88}
    assert 0 < calls[-1] <= 2
    assert _powershell_record("safe", time.monotonic() + 20) == {"pid": 88}
    assert 0 < calls[-1] <= 10
    count = len(calls)
    with pytest.raises(TimeoutError, match="real_case_deadline"):
        _powershell_record("safe", time.monotonic() - 1, cap_seconds=None)
    assert len(calls) == count

    def timeout(_argv: list[str], **kwargs: object) -> str:
        calls.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired("powershell", kwargs["timeout"])

    monkeypatch.setattr(subprocess, "check_output", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        _powershell_record("safe", time.monotonic() + 2, cap_seconds=None)
    assert len(calls) == count + 1


def test_unit_runtime_identity_port_query_uses_startup_deadline(
        monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_owner()
    owner.launcher_pid = 77
    owner.launcher_identity = (str(PYTHON), 1)
    image = str(Path(sys._base_executable).resolve(strict=True))
    created = 116444736000000000 + 1700000000 * 10000000
    owner.runtime = {77: (1, str(PYTHON), 1), 88: (2, image, created)}
    monkeypatch.setattr(owner, "observe", lambda: {77, 88})
    deadline = time.monotonic() + 2
    caps: list[float | None] = []
    port_pid = 88

    def os_record(command: str, supplied_deadline: float, *,
                  cap_seconds: float | None = 10) -> dict[str, object]:
        assert supplied_deadline == deadline
        caps.append(cap_seconds)
        if "Get-NetTCPConnection" in command:
            return {"port": 54321, "pid": port_pid}
        assert "ProcessId=88" in command
        return {"pid": 88, "parent": 77, "image": image,
                "created": datetime.fromtimestamp(1700000000, timezone.utc).isoformat()}

    monkeypatch.setattr(sys.modules[__name__], "_powershell_record", os_record)
    assert _runtime_identity(owner, deadline=deadline, port=54321)["pid"] == 88
    assert caps == [10, None]
    port_pid = 89
    with pytest.raises(AssertionError, match="api_port_pid_mismatch"):
        _runtime_identity(owner, deadline=deadline, port=54321)


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_cleanup_preserves_unexited_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = OwnedJob.__new__(OwnedJob)
    owner.role = "neutral"
    owner.job = 123
    owner.launcher = 456
    owner.launcher_pid = 77
    owner.launcher_identity = None
    owner.launcher_exit = None
    owner.thread = 0
    owner.runtime = {}
    owner.observed_runtime = {}
    owner.exit_codes = {}
    owner._unclassified_handles = []
    owner._pipe_handles = []
    owner._drains = []
    owner._closed = False
    monkeypatch.setattr(owner, "active_count", lambda: 1)
    monkeypatch.setattr(sys.modules[__name__], "_terminate_job", lambda *_: False)
    with pytest.raises(OSError, match="TerminateJobObject"):
        owner.close(force=True)
    assert owner.job == 123 and owner.launcher == 456 and not owner._closed


def _fake_owner(*, pipes: tuple[int, ...] = ()) -> OwnedJob:
    owner = OwnedJob.__new__(OwnedJob)
    owner.role = "neutral"
    owner.argv = ()
    owner.cwd = REPO
    owner.job = 0
    owner.launcher = 0
    owner.launcher_pid = 0
    owner.launcher_identity = None
    owner.launcher_exit = None
    owner.thread = 0
    owner.assigned = False
    owner.runtime = {}
    owner.observed_runtime = {}
    owner.exit_codes = {}
    owner._unclassified_handles = []
    owner._pipe_handles = list(pipes)
    owner._drains = []
    owner._closed = False
    return owner


def _fake_exited_job(monkeypatch: pytest.MonkeyPatch) -> OwnedJob:
    owner = _fake_owner()
    owner.launcher_pid = 77
    owner.runtime = {77: (701, "launcher", 1), 88: (702, "child", 2)}
    owner.exit_codes = {99: 9}
    monkeypatch.setattr(owner, "observe", lambda: set())
    monkeypatch.setattr(owner, "active_count", lambda: 0)
    monkeypatch.setattr(sys.modules[__name__], "_win_error",
                        lambda operation: OSError(6, operation))
    return owner


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_wait_empty_waits_for_retained_signal(
        monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_exited_job(monkeypatch)
    waits: list[tuple[int, int]] = []

    def fake_wait(handle: int, milliseconds: int) -> int:
        waits.append((handle, milliseconds))
        return (WAIT_TIMEOUT if handle == 701 and len(waits) == 1
                else WAIT_OBJECT_0)

    def fake_exit(handle: int, code_ptr: object) -> bool:
        ctypes.cast(code_ptr, ctypes.POINTER(DWORD)).contents.value = (
            259 if handle == 701 else 2)
        return True

    monkeypatch.setattr(sys.modules[__name__], "_wait", fake_wait)
    monkeypatch.setattr(sys.modules[__name__], "_get_exit", fake_exit)
    assert owner.wait_empty(0.5) == {77: 259, 88: 2}
    assert owner.exit_codes == {99: 9, 77: 259, 88: 2}
    assert owner.launcher_exit == 259
    assert [handle for handle, _ in waits] == [701, 701, 702]
    assert all(0 <= milliseconds <= 50 for _, milliseconds in waits)


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_wait_empty_retained_timeout_is_bounded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_exited_job(monkeypatch)
    reads: list[int] = []

    def fake_exit(handle: int, code_ptr: object) -> bool:
        reads.append(handle)
        ctypes.cast(code_ptr, ctypes.POINTER(DWORD)).contents.value = 259
        return True

    monkeypatch.setattr(sys.modules[__name__], "_wait",
                        lambda handle, *_: (WAIT_OBJECT_0 if handle == 701
                                           else WAIT_TIMEOUT))
    monkeypatch.setattr(sys.modules[__name__], "_get_exit", fake_exit)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="member_not_signalled"):
        owner.wait_empty(0.04)
    assert time.monotonic() - started < 0.5
    assert reads and set(reads) == {701}
    assert owner.exit_codes == {99: 9} and owner.launcher_exit is None


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_wait_empty_failed_wait_has_no_exit(
        monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_exited_job(monkeypatch)
    monkeypatch.setattr(sys.modules[__name__], "_wait", lambda *_: WAIT_FAILED)
    monkeypatch.setattr(sys.modules[__name__], "_get_exit",
                        lambda *_: pytest.fail("exit_read_after_failed_wait"))
    with pytest.raises(OSError, match=r"WaitForSingleObject\(member\)"):
        owner.wait_empty(0.1)
    assert owner.exit_codes == {99: 9} and owner.launcher_exit is None


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_wait_empty_partial_exit_read_is_not_published(
        monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_exited_job(monkeypatch)
    reads: list[int] = []

    def fake_exit(handle: int, code_ptr: object) -> bool:
        reads.append(handle)
        ctypes.cast(code_ptr, ctypes.POINTER(DWORD)).contents.value = handle - 700
        return handle == 701

    monkeypatch.setattr(sys.modules[__name__], "_wait",
                        lambda *_: WAIT_OBJECT_0)
    monkeypatch.setattr(sys.modules[__name__], "_get_exit", fake_exit)
    with pytest.raises(OSError, match="GetExitCodeProcess"):
        owner.wait_empty(0.1)
    assert reads == [701, 702]
    assert owner.exit_codes == {99: 9} and owner.launcher_exit is None


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
@pytest.mark.parametrize("fault", ("timeout", "wait_failed", "exit_failed"))
def test_unit_close_retries_zero_accounting_retained_handles(
        monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    owner = _fake_exited_job(monkeypatch)
    owner.job = 999
    owner.launcher = 701
    owner.assigned = True
    state = {"retry": False}
    closed: list[int] = []
    reads: list[int] = []

    def fake_wait(handle: int, _milliseconds: int) -> int:
        if handle == 702 and not state["retry"]:
            if fault == "timeout":
                return WAIT_TIMEOUT
            if fault == "wait_failed":
                return WAIT_FAILED
        return WAIT_OBJECT_0

    def fake_exit(handle: int, code_ptr: object) -> bool:
        reads.append(handle)
        ctypes.cast(code_ptr, ctypes.POINTER(DWORD)).contents.value = (
            259 if handle == 701 else 2)
        return not (handle == 702 and fault == "exit_failed"
                    and not state["retry"])

    monkeypatch.setattr(sys.modules[__name__], "_wait", fake_wait)
    monkeypatch.setattr(sys.modules[__name__], "_get_exit", fake_exit)
    monkeypatch.setattr(sys.modules[__name__], "_close_handle",
                        lambda handle: closed.append(handle) or True)
    monkeypatch.setattr(owner, "wait_empty",
                        lambda _timeout: OwnedJob.wait_empty(owner, 0.04))
    with _OWNERS_LOCK:
        _OWNERS[id(owner)] = owner
    try:
        expected = TimeoutError if fault == "timeout" else OSError
        with pytest.raises(expected):
            owner.close(force=True)
        assert closed == [] and owner.job == 999
        assert set(owner.runtime) == {77, 88} and not owner._closed
        assert owner.exit_codes == {99: 9} and owner.launcher_exit is None
        with _OWNERS_LOCK:
            assert _OWNERS[id(owner)] is owner
        assert 701 in reads
        if fault == "wait_failed":
            assert 702 not in reads
        state["retry"] = True
        owner.close(force=True)
        assert closed == [702, 701, 999]
        assert owner.exit_codes == {99: 9, 77: 259, 88: 2}
        assert owner.launcher_exit == 259 and owner._closed
        with _OWNERS_LOCK:
            assert id(owner) not in _OWNERS
    finally:
        with _OWNERS_LOCK:
            _OWNERS.pop(id(owner), None)


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_reader_start_and_active_reader_ownership(
        monkeypatch: pytest.MonkeyPatch) -> None:
    class Drain:
        def __init__(self) -> None:
            self.active = True
            self.finish_calls = 0
            self.thread = self

        def is_alive(self) -> bool:
            return self.active

        def finish(self, _timeout: float = 2) -> bytes:
            self.finish_calls += 1
            if self.active:
                raise TimeoutError("controlled_reader_active")
            return b""

    first = Drain()
    created = 0
    closed: list[int] = []

    def reader(_handle: int, _label: str) -> Drain:
        nonlocal created
        created += 1
        if created == 2:
            raise RuntimeError("controlled_second_reader_start")
        return first

    with monkeypatch.context() as patcher:
        patcher.setattr(sys.modules[__name__], "_OutputDrain", reader)
        patcher.setattr(sys.modules[__name__], "_close_handle",
                        lambda handle: closed.append(handle) or True)
        owner = _fake_owner(pipes=(101, 102))
        with pytest.raises(RuntimeError, match="second_reader"):
            owner._start_drains(101, 102)
        assert owner._drains == [first]
        with pytest.raises(ExceptionGroup, match="reader_active"):
            owner.close(force=True)
        assert closed == [] and owner._pipe_handles == [101, 102]
        first.active = False
        owner.close(force=True)
        assert first.finish_calls == 2
        assert closed == [101, 102] and owner._closed


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_retain_failed_close_is_retryable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _fake_owner()
    owner.job = 999
    calls: list[int] = []

    def close(handle: int) -> bool:
        calls.append(handle)
        return not (handle == 222 and calls.count(222) == 1)

    with monkeypatch.context() as patcher:
        patcher.setattr(sys.modules[__name__], "_open_process", lambda *_: 222)
        patcher.setattr(sys.modules[__name__], "_is_in_job", lambda *_: False)
        patcher.setattr(sys.modules[__name__], "_close_handle", close)
        patcher.setattr(owner, "active_count", lambda: 0)
        with pytest.raises(ExceptionGroup, match="process_query_and_handle_close"):
            owner._retain(77)
        assert owner._unclassified_handles == [222]
        owner.close(force=True)
        assert calls.count(222) == 2
        assert owner._unclassified_handles == [] and owner._closed


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
@pytest.mark.parametrize("fault", ("terminate", "wait", "close"))
def test_unit_failed_constructor_remains_retirable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    identity = ControlIdentity(uuid4().hex, "unit_constructor", "neutral", tmp_path)
    argv, env = _neutral_launch(identity)
    state = {"retry": False, "dead": False}
    ledger: dict[str, object] = {"operation": "mocked_constructor_failure",
                                 "injected_cleanup_failure": fault}

    def fake_launch(owner: OwnedJob, _env: dict[str, str]) -> None:
        owner.launcher = 222
        owner.launcher_pid = 77
        owner.launcher_identity = (str(PYTHON), 1)
        raise OSError("controlled_launch_failure")

    def fake_terminate(_handle: int, _code: int) -> bool:
        if fault == "terminate" and not state["retry"]:
            return False
        state["dead"] = True
        return True

    def fake_wait(_handle: int, _timeout: int) -> int:
        if fault == "wait" and not state["retry"]:
            return WAIT_TIMEOUT
        return WAIT_OBJECT_0 if state["dead"] else WAIT_TIMEOUT

    def fake_close(handle: int) -> bool:
        return not (fault == "close" and handle == 222 and not state["retry"])

    def fake_exit(_handle: int, code_ptr: object) -> bool:
        ctypes.cast(code_ptr, ctypes.POINTER(DWORD)).contents.value = 3
        return True

    with monkeypatch.context() as patcher:
        patcher.setattr(OwnedJob, "_launch", fake_launch)
        patcher.setattr(sys.modules[__name__], "_wait", fake_wait)
        patcher.setattr(sys.modules[__name__], "_terminate_process", fake_terminate)
        patcher.setattr(sys.modules[__name__], "_get_exit", fake_exit)
        patcher.setattr(sys.modules[__name__], "_close_handle", fake_close)
        try:
            with pytest.raises(OSError, match="controlled_launch_failure"):
                OwnedJob(argv, env, role="neutral")
            ledger["before_retry"] = _owners_snapshot()
            assert len(ledger["before_retry"]) == 1
            with pytest.raises(ExceptionGroup, match="owned_jobs_not_retired"):
                _retire_owned_jobs()
            assert len(_owners_snapshot()) == 1
        finally:
            state["retry"] = True
            _retire_owned_jobs()
            ledger["after_retry"] = _owners_snapshot()
            _atomic_json(tmp_path / f"constructor-retry-{fault}.json", ledger)
        assert not ledger["after_retry"]


def _neutral_launch(identity: ControlIdentity) -> tuple[list[str], dict[str, str]]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"}}
    argv = [str(PYTHON), "-m", MODULE, "--neutral-child",
            str(identity.directory), identity.nonce, identity.case]
    return argv, env


def _path_identity(value: str | Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        raise AssertionError("nonabsolute_process_image")
    return os.path.normcase(str(path.resolve(strict=True)))


def _neutral_participants(owner: OwnedJob, parent: dict[str, object],
                          descendant: dict[str, object]) -> dict[str, object]:
    members = owner.observe()
    parent_pid, descendant_pid = int(parent["pid"]), int(descendant["pid"])
    assert parent_pid != descendant_pid, "collapsed_neutral_roles"
    assert {owner.launcher_pid, parent_pid, descendant_pid} <= members
    assert owner.launcher_identity is not None
    launcher_image, launcher_created = owner.launcher_identity
    assert _path_identity(launcher_image) == _path_identity(PYTHON)
    assert launcher_created > 0
    result: dict[str, object] = {"launcher": {
        "pid": owner.launcher_pid, "image": launcher_image,
        "created_filetime": launcher_created}, "members": sorted(members)}
    for role, event, pid in (("parent", parent, parent_pid),
                             ("descendant", descendant, descendant_pid)):
        _handle, image, created = owner.runtime[pid]
        assert created >= launcher_created
        executable = str(event["executable"])
        base_executable = str(event["base_executable"])
        assert _path_identity(executable) == _path_identity(PYTHON)
        assert _path_identity(image) in {
            _path_identity(executable), _path_identity(base_executable)}
        ppid = int(event["ppid"])
        if pid != owner.launcher_pid:
            assert ppid in members, "runtime_parent_outside_job"
        result[role] = {"pid": pid, "ppid": ppid, "image": image,
                        "executable": executable,
                        "base_executable": base_executable,
                        "created_filetime": created}
    assert int(descendant["ppid"]) in members
    descendant_launcher = int(parent["descendant_launcher_pid"])
    assert descendant_launcher in members
    if descendant_pid == descendant_launcher:
        assert int(descendant["ppid"]) == parent_pid
    else:
        assert int(descendant["ppid"]) == descendant_launcher
    assert owner.runtime[descendant_pid][2] >= owner.runtime[parent_pid][2]
    result["descendant_launcher_pid"] = descendant_launcher
    return result


def _neutral_exercise(tmp_path: Path, *, forced: bool) -> None:
    with _OWNERS_LOCK:
        assert not _OWNERS, "prior_owned_job_unresolved"
    identity = ControlIdentity(uuid4().hex,
                               "unit_forced" if forced else "unit_neutral",
                               "neutral", tmp_path)
    argv, env = _neutral_launch(identity)
    deadline = time.monotonic() + 120.0
    ledger: dict[str, object] = {"case": identity.case, "nonce": identity.nonce,
                                 "operation": "forced" if forced else "normal",
                                 "selected_interpreter": str(PYTHON),
                                 "phase": "before_launch"}
    with _OWNERS_LOCK:
        retired_before = len(_RETIRED_JOBS)
    designated_death = False
    cleanup_errors: list[Exception] = []
    try:
        owner = OwnedJob(argv, env, role="neutral")
        ledger["phase"] = "launched"
        _remaining(deadline, 40)
        parent = _await_event(identity, "parent", timeout=_remaining(deadline, 40))
        ledger["parent_event"] = parent
        ledger["phase"] = "parent_arrived"
        descendant = _await_event(identity, "descendant",
                                  timeout=_remaining(deadline, 40))
        ledger["descendant_event"] = descendant
        ledger["phase"] = "descendant_arrived"
        ledger["os_identity"] = _neutral_participants(owner, parent, descendant)
        ledger["phase"] = "participants_retained"
        if forced:
            exits = owner.terminate(code=3, timeout=_remaining(deadline, 20))
            designated_death = True
            assert exits[int(parent["pid"])] == exits[int(descendant["pid"])] == 3
        else:
            _release(identity, "descendant")
            _release(identity, "parent")
            ledger["phase"] = "released"
            exits = owner.wait_empty(_remaining(deadline, 20))
            assert all(code == 0 for code in exits.values())
        assert owner.active_count() == 0
        participants = {int(parent["pid"]), int(descendant["pid"])}
        assert participants <= exits.keys()
        ledger["exits"] = exits
        ledger["job_active_after_exit"] = owner.active_count()
        ledger["phase"] = "owned_tree_exited"
        stdout, stderr, truncation = owner.output()
        ledger["output_sha256"] = {"stdout": hashlib.sha256(stdout).hexdigest(),
                                    "stderr": hashlib.sha256(stderr).hexdigest()}
        ledger["output_truncated"] = truncation
        assert not any(truncation) and not stderr
        assert len(stdout) <= MAX_OUTPUT
        _remaining(deadline, 1)
    except BaseException as error:
        ledger["failure_type"] = type(error).__name__
        raise
    finally:
        # Forced death is the designated action only after both roles were
        # observed paused. On any failure, release arrivals before retirement.
        if not designated_death:
            releases = _release_observed(identity, ("descendant", "parent"))
            ledger["failure_cleanup_release_errors"] = releases
            if releases:
                cleanup_errors.append(AssertionError("failure_barrier_release"))
        try:
            _retire_owned_jobs()
        except Exception as error:
            cleanup_errors.append(error)
        with _OWNERS_LOCK:
            ledger["retired_jobs"] = _RETIRED_JOBS[retired_before:]
        ledger["remaining_owners"] = _owners_snapshot()
        ledger["cleanup_error_types"] = [type(error).__name__
                                         for error in cleanup_errors]
        _atomic_json(tmp_path / "neutral-lifecycle.json", ledger)
        if cleanup_errors:
            raise ExceptionGroup("neutral_cleanup_incomplete", cleanup_errors)


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
@pytest.mark.parametrize("failure", ("create", "assign", "resume"))
def test_unit_partial_startup_retires_owner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    with _OWNERS_LOCK:
        assert not _OWNERS, "prior_owned_job_unresolved"
    identity = ControlIdentity(uuid4().hex, "unit_partial", "neutral", tmp_path)
    argv, env = _neutral_launch(identity)
    symbol = {"create": "_create_process", "assign": "_assign_job",
              "resume": "_resume_thread"}[failure]

    def refused(*_args: object) -> int:
        ctypes.set_last_error(5)
        return 0xFFFFFFFF if failure == "resume" else 0

    with _OWNERS_LOCK:
        retired_before = len(_RETIRED_JOBS)
    ledger: dict[str, object] = {"operation": "partial_startup",
                                 "failure_point": failure,
                                 "phase": "before_launch"}
    cleanup_errors: list[Exception] = []
    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(sys.modules[__name__], symbol, refused)
            with pytest.raises(OSError):
                OwnedJob(argv, env, role="neutral")
        ledger["phase"] = "expected_startup_refusal"
        with _OWNERS_LOCK:
            assert not _OWNERS
    finally:
        ledger["release_errors"] = _release_observed(identity,
                                                       ("descendant", "parent"))
        try:
            _retire_owned_jobs()
        except Exception as error:
            cleanup_errors.append(error)
        with _OWNERS_LOCK:
            retired = _RETIRED_JOBS[retired_before:]
        ledger["retired_jobs"] = retired
        ledger["remaining_owners"] = _owners_snapshot()
        ledger["cleanup_error_types"] = [type(error).__name__
                                         for error in cleanup_errors]
        _atomic_json(tmp_path / f"partial-startup-{failure}.json", ledger)
        if cleanup_errors:
            raise ExceptionGroup("partial_startup_cleanup", cleanup_errors)
    assert len(retired) == 1
    if failure != "create":
        receipt = retired[0]
        assert receipt["launcher_pid"] and receipt["launcher_identity"]
        assert receipt["launcher_exit"] == 3


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_neutral_job_and_descendant(tmp_path: Path) -> None:
    _neutral_exercise(tmp_path, forced=False)


@pytest.mark.skipif(os.name != "nt", reason="Win32 Job lifecycle")
def test_unit_neutral_forced_tree_exit(tmp_path: Path) -> None:
    _neutral_exercise(tmp_path, forced=True)


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
