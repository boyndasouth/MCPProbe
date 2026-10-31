"""Windows: kill a server and every process it started, together.

Servers are often launched through wrappers (``npx.cmd``, ``uvx``, a venv's
``python.exe`` launcher) that start the real server as a child. Two
mechanisms make sure that whole tree goes away:

1. **A Job Object** with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The server is
   assigned to it right after start, and the children it spawns join it
   automatically, so terminating the job ends them all.
2. **Tree tracking**, because some children escape the job: Windows moves
   packaged apps (the Microsoft Store Python, for example) into a job of their
   own the moment they start. While the server runs, MCPProbe walks the
   process table from every process it has seen, by parent PID, and remembers
   each descendant with its start time (often at first, while wrappers start
   the real server, then every half second). At shutdown it looks once more
   and terminates everything it ever saw that is still running.

   Tracking over time matters because a process in the *middle* of the chain
   often exits first (a launcher, or a server closing on stdin EOF). Its
   children still record its PID as their parent, so once it has been seen
   they stay reachable. A child only counts if it started after its parent,
   and a remembered PID that now belongs to a newer process only vouches for
   children born before that process, so a recycled PID is never mistaken for
   part of the tree.

Off Windows every operation is a no-op; POSIX uses process groups instead.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


class ProcessJob:
    """Groups a process with all its descendants. This base class does nothing."""

    def assign(self, pid: int) -> bool:
        """Track a just-started process as the root of the tree. True if it joined a job."""
        return False

    tracks = False
    """True if ``refresh`` should be called periodically while the server runs."""

    def refresh(self) -> None:
        """Record descendants that exist right now (called from a worker thread)."""

    def terminate(self) -> None:
        """Kill the root process and every descendant."""

    def close(self) -> None:
        """Release the job; with kill-on-close this also kills what is left."""


def grow_tree(
    children: Mapping[int, Sequence[int]],
    created: Callable[[int], int | None],
    known: Mapping[int, int],
    *,
    exclude: int,
) -> dict[int, int]:
    """``known`` (pid -> start time) plus every descendant found in a process table.

    ``children`` maps a parent PID to the PIDs that name it as their parent;
    ``created`` gives a running process's start time, or None if it is gone or
    unreadable.

    Known processes may have exited: Windows keeps their PID in their
    children's records, so their children are still found. A child counts only
    if it started no earlier than its parent. If a known parent's PID now
    belongs to a newer process (its start time changed), only children born
    before that newer process count, so a recycled PID never pulls in
    unrelated processes. ``exclude`` (MCPProbe itself) is never included.
    """
    result = dict(known)
    frontier = list(known.items())
    while frontier:
        parent, parent_born = frontier.pop()
        current = created(parent)
        reused_at = current if current is not None and current != parent_born else None
        for child in children.get(parent, ()):
            if child == exclude or child in result:
                continue
            born = created(child)
            if born is None or born < parent_born:
                continue
            if reused_at is not None and born >= reused_at:
                continue  # a child of the newer process that took the PID
            result[child] = born
            frontier.append((child, born))
    return result


def still_running(created: Callable[[int], int | None], known: Mapping[int, int]) -> list[int]:
    """Known processes that are alive *and* still the same process (not a recycled PID)."""
    return [pid for pid, born in known.items() if created(pid) == born]


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

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(_FileTime)] * 4))
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessEntry))
    _kernel32.Process32NextW.restype = wintypes.BOOL

    def _creation_time(pid: int) -> int | None:
        """When the process started (100 ns ticks), or None if it can't be read."""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [_FileTime() for _ in range(4)]
            if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = times[0]
            return int((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            _kernel32.CloseHandle(handle)

    def _children_by_parent() -> dict[int, list[int]]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return {}
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            children: dict[int, list[int]] = {}
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return children
        finally:
            _kernel32.CloseHandle(snapshot)

    def _terminate_pid(pid: int) -> None:
        handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                _kernel32.TerminateProcess(handle, 1)
            finally:
                _kernel32.CloseHandle(handle)

    class _WindowsTree(ProcessJob):
        tracks = True

        def __init__(self) -> None:
            self._handle: int | None = self._create_job()
            self._known: dict[int, int] = {}
            self._lock = threading.Lock()

        @staticmethod
        def _create_job() -> int | None:
            """A kill-on-close job, or None if Windows refuses (tracking still works)."""
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                _kernel32.CloseHandle(handle)
                return None
            return int(handle)

        def assign(self, pid: int) -> bool:
            created = _creation_time(pid)
            if created is not None:
                with self._lock:
                    self._known[pid] = created
            if self._handle is None:
                return False
            process = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not process:
                return False
            try:
                return bool(_kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                _kernel32.CloseHandle(process)

        def refresh(self) -> None:
            with self._lock:
                if self._known:
                    self._known = grow_tree(
                        _children_by_parent(), _creation_time, self._known, exclude=os.getpid()
                    )

        def terminate(self) -> None:
            # One last look first, while the tree still exists, then kill
            # everything ever seen that is still the same process.
            self.refresh()
            with self._lock:
                targets = still_running(_creation_time, self._known)
            if self._handle is not None:
                _kernel32.TerminateJobObject(self._handle, 1)
            for pid in targets:
                _terminate_pid(pid)

        def close(self) -> None:
            if self._handle is not None:
                _kernel32.CloseHandle(self._handle)
                self._handle = None


def new_job() -> ProcessJob:
    """The Windows job-plus-snapshot tree killer, or a no-op elsewhere."""
    if sys.platform == "win32":  # pragma: no cover - Windows only
        return _WindowsTree()
    return ProcessJob()
