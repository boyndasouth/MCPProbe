"""Cross-platform process plumbing: command splitting and killing whole trees.

Servers are often launched through wrappers (``npx``, ``uvx``, shell
scripts), so killing only the direct child leaves the real server running.
On POSIX the server gets its own process group; on Windows its own process
group and ``taskkill /T`` takes the tree down.
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


def terminate_tree(process: asyncio.subprocess.Process, *, force: bool) -> None:
    """Send TERM (or KILL with ``force``) to the process and its descendants.

    Runs even after the direct child exited: a wrapper (``npx``, a shell
    script) can exit cleanly while the real server it spawned lives on and
    keeps our stdout pipe open.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows only
        # Known gap: once the direct child is gone, taskkill /T cannot find
        # its orphans. A Job Object would close it; tracked for M0 follow-up.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError):  # raced with exit
        os.killpg(process.pid, sig)
