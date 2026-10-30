"""Is a process still running? Safe on every platform.

On Windows ``os.kill(pid, 0)`` does not probe: it terminates the process, so
this asks the kernel for the exit code instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it has exited, though, so treat it as dead.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        return stat.split(")")[-1].split()[0] != "Z"
    except OSError:
        return True
