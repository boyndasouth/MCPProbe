"""Cross-platform process plumbing: command splitting and killing whole trees.

Servers are often launched through wrappers (``npx``, ``uvx``, shell
scripts), so killing only the direct child leaves the real server running.
On POSIX the server gets its own process group. On Windows a Job Object
plus a process-tree snapshot take the whole tree down (see ``winjob.py``).
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Sequence

    from mcpprobe.transport.winjob import ProcessJob

IS_WINDOWS = sys.platform == "win32"


def split_command(command: str) -> list[str]:
    """Split a command line the way the user's shell would, roughly.

    POSIX uses shell quoting rules. Windows keeps backslashes in paths and
    strips one layer of surrounding quotes from each argument.
    """
    if not IS_WINDOWS:
        return shlex.split(command)
    parts = shlex.split(command, posix=False)  # pragma: no cover - Windows only
    return [  # pragma: no cover - Windows only
        p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts
    ]


def resolve_argv(argv: Sequence[str]) -> list[str]:
    """Resolve the program on PATH (finds ``npx.cmd`` on Windows)."""
    if not argv:
        raise ValueError("empty command")
    program = shutil.which(argv[0]) or argv[0]
    return [program, *argv[1:]]


def spawn_kwargs() -> dict[str, Any]:
    """Arguments that put the child in its own process group."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def terminate_tree(
    process: asyncio.subprocess.Process, *, force: bool, job: ProcessJob | None = None
) -> None:
    """Send TERM (or KILL with ``force``) to the process and its descendants.

    Runs even after the direct child exited: a wrapper (``npx``, a shell
    script) can exit cleanly while the real server it spawned lives on and
    keeps our stdout pipe open.
    """
    # sys.platform, not IS_WINDOWS: mypy narrows only on the literal check.
    if sys.platform == "win32":  # pragma: no cover - Windows only
        # The job snapshots the tree first, then kills the job and every
        # descendant, including ones that escaped it (see winjob.py).
        if job is not None:
            job.terminate()
            return
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    # Linux raises ProcessLookupError for a group that is gone; macOS raises
    # PermissionError (EPERM) once the group leader has exited.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, sig)
