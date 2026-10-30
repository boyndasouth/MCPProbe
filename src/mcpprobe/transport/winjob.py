"""Windows Job Objects: kill a server and every process it started, together.

``taskkill /T`` walks the process tree from the PID we started. When that
process is a wrapper that exits early (``npx.cmd``, ``uvx``, a batch file),
the real server is orphaned and ``taskkill`` can no longer find it. A Job
Object fixes that: the server is assigned to a job right after it starts,
children it spawns join the same job automatically, and the job is created
with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so terminating or closing it
ends every process in it, orphans included.

Known limit: a process could in principle start a child in the instant
between spawn and assignment; wrappers like npx take far longer than that to
start anything. If the job cannot be created (for example a restrictive
parent job), MCPProbe falls back to ``taskkill /T`` alone.

Off Windows every operation is a no-op; POSIX uses process groups instead.
"""

from __future__ import annotations

import sys


class ProcessJob:
    """Groups a process with all its descendants. This base class does nothing."""

    def assign(self, pid: int) -> bool:
        """Add a running process to the job. True if it was added."""
        return False

    def terminate(self) -> None:
        """Kill every process in the job."""

    def close(self) -> None:
        """Release the job; with kill-on-close this also kills what is left."""


if sys.platform == "win32":  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS value
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _WindowsJob(ProcessJob):
        def __init__(self) -> None:
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise ctypes.WinError(error)
            self._handle: int | None = handle

        def assign(self, pid: int) -> bool:
            if self._handle is None:
                return False
            process = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not process:
                return False
            try:
                return bool(_kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                _kernel32.CloseHandle(process)

        def terminate(self) -> None:
            if self._handle is not None:
                _kernel32.TerminateJobObject(self._handle, 1)

        def close(self) -> None:
            if self._handle is not None:
                _kernel32.CloseHandle(self._handle)
                self._handle = None


def new_job() -> ProcessJob:
    """A real Job Object on Windows (when one can be made), else a no-op."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        try:
            return _WindowsJob()
        except OSError:
            return ProcessJob()
    return ProcessJob()
