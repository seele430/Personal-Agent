"""tier1 runner 的进程树所有权与终止。

平台只清理由自己显式创建的进程树，判断依据是**所有权句柄**，不是进程名：

- 优先使用 Windows Job Object（``KILL_ON_JOB_CLOSE``）：runner 及其全部
  派生进程归属同一个 job；``TerminateJobObject`` 一次终止整棵树，
  ``QueryInformationJobObject`` 能证明活动进程数已归零。
- Job Object 创建或分配失败时降级为 ``taskkill /F /T``；降级会写进
  ``ExecOutcome.cleanup``，不能冒充 job 级清理。
- 只终止 job 或本次 Popen 根 PID 名下的进程，绝不按进程名清理。

清理失败一律抛 ``ProcessTreeError``；调用方必须把「清理失败」转成
failed 终态，绝不能伪造 cancelled。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

WINDOWS = os.name == "nt"

CLEANUP_JOB_OBJECT = "job_object"
CLEANUP_TASKKILL = "taskkill"
CLEANUP_PROCESS_ONLY = "process_only"

STILL_ACTIVE = 259

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class ProcessTreeError(RuntimeError):
    """进程树没有被完整清理，或 Job Object 操作失败。"""


if WINDOWS:

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL

    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _WindowsJob:
        """一个 Job Object 句柄；只在本模块内使用。"""

        def __init__(self) -> None:
            self.handle: int | None = None

        def create(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)

            if not handle:
                raise ProcessTreeError(
                    f"CreateJobObject 失败：{ctypes.get_last_error()}"
                )

            information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            information.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )

            assigned = _kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )

            if not assigned:
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise ProcessTreeError(f"SetInformationJobObject 失败：{error}")

            self.handle = handle

        def assign(self, popen: subprocess.Popen) -> None:
            if self.handle is None:
                raise ProcessTreeError("Job Object 尚未创建")

            # 用 Popen 已经持有的进程句柄，避免「先关闭再按 PID 重开」的竞态。
            raw_handle = getattr(popen, "_handle", None)  # noqa: SLF001

            if raw_handle is None:
                raise ProcessTreeError("Popen 没有可用的进程句柄")

            if not _kernel32.AssignProcessToJobObject(
                self.handle, wintypes.HANDLE(int(raw_handle))
            ):
                raise ProcessTreeError(
                    f"AssignProcessToJobObject 失败：{ctypes.get_last_error()}"
                )

        def active_processes(self) -> int:
            if self.handle is None:
                raise ProcessTreeError("Job Object 尚未创建")

            accounting = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()

            queried = _kernel32.QueryInformationJobObject(
                self.handle,
                JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            )

            if not queried:
                raise ProcessTreeError(
                    f"QueryInformationJobObject 失败：{ctypes.get_last_error()}"
                )

            return int(accounting.ActiveProcesses)

        def terminate(self) -> None:
            if self.handle is None:
                return
            if not _kernel32.TerminateJobObject(self.handle, 1):
                raise ProcessTreeError(
                    f"TerminateJobObject 失败：{ctypes.get_last_error()}"
                )

        def close(self) -> None:
            if self.handle is not None:
                _kernel32.CloseHandle(self.handle)
                self.handle = None


def process_is_alive(pid: int) -> bool:
    """按 PID 判断进程是否仍存活。只用于验收与测试，不用于清理。"""

    if pid <= 0:
        return False

    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )

    if not handle:
        return False

    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


class ProcessTree:
    """一次平台执行所拥有的进程树。"""

    def __init__(self, popen: subprocess.Popen) -> None:
        self.popen = popen
        self.mode = CLEANUP_PROCESS_ONLY
        self._job: _WindowsJob | None = None

        if not WINDOWS:
            return

        job = _WindowsJob()

        try:
            job.create()
            job.assign(popen)
        except ProcessTreeError:
            job.close()
            return

        self._job = job
        self.mode = CLEANUP_JOB_OBJECT

    @property
    def owned_process_count(self) -> int | None:
        """Job 里当前的活动进程数；降级模式下返回 None。"""

        if self._job is None:
            return None

        try:
            return self._job.active_processes()
        except ProcessTreeError:
            return None

    def terminate(self) -> None:
        """终止整棵进程树；异常由调用方转成 failed 终态。"""

        if self._job is not None:
            self._job.terminate()
            return

        if WINDOWS:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.popen.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                check=False,
            )

        try:
            self.popen.kill()
        except OSError:
            pass

    def wait_empty(self, timeout: float = 5.0) -> bool:
        """等待进程树真正清空。

        Job 模式下以 ``ActiveProcesses == 0`` 为准；降级模式只能确认
        直接子进程已退出，因此返回 True 只代表「尽力清理完成」。
        """

        if self._job is None:
            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                if self.popen.poll() is not None:
                    return True
                time.sleep(0.02)

            return self.popen.poll() is not None

        deadline = time.monotonic() + timeout

        while True:
            try:
                if self._job.active_processes() == 0:
                    return True
            except ProcessTreeError:
                return False

            if time.monotonic() >= deadline:
                return False

            time.sleep(0.02)

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None
