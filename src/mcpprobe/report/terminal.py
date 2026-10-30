"""Human-readable summary for local runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcpprobe.engine.runner import Status

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from mcpprobe.engine.runner import RunResult

_STYLE = {
    Status.FAILED: "red",
    Status.CRASHED: "bold red",
    Status.TIMEOUT: "red",
    Status.ERROR: "magenta",
    Status.SKIPPED: "dim",
}


def render(result: RunResult, console: Console, *, cases_dir: Path) -> None:
    console.print(f"seed {result.seed}", highlight=False)
    if not result.results:
        console.print(
            "The server started and answered the liveness probe, but there are no "
            "checks to run yet: conformance checks arrive in milestone M1."
        )
        return
    for item in result.results:
        if item.status is Status.PASSED:
            continue
        style = _STYLE.get(item.status, "")
        line = f"[{style}]{item.status.value:>8}[/] {item.case_id} ({item.severity.value})"
        if item.message:
            line += f": {item.message}"
        console.print(line, highlight=False)
        if item.repro:
            console.print(
                f"         replay: mcpprobe replay {cases_dir / item.repro}",
                markup=False,
                highlight=False,
            )
    counts = ", ".join(
        f"{result.count(status)} {status.value}" for status in Status if result.count(status)
    )
    console.print(f"{len(result.results)} cases: {counts}", highlight=False)
    if result.restarts:
        console.print(f"server restarted {result.restarts} time(s)", highlight=False)
    if result.stopped_early:
        console.print("later suites skipped because conformance failed (--keep-going to run them)")
