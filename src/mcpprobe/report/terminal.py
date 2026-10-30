"""Human-readable summary for local runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcpprobe.engine.catalog import Severity
from mcpprobe.engine.runner import Status
from mcpprobe.protocol.era import Era, ServerFacts

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


def _era_text(facts: ServerFacts) -> str:
    if facts.era is Era.MODERN:
        return "modern (2026-07-28)"
    if facts.era is Era.LEGACY:
        return f"legacy (initialize, negotiated {facts.legacy_version})"
    if facts.era is Era.DUAL:
        return (
            f"dual: 2026-07-28 and initialize (negotiated {facts.legacy_version}); "
            "both-era checks run once per era, legacy runs end in @legacy"
        )
    return "unknown: the server answered neither server/discover nor initialize"


def render(result: RunResult, console: Console, *, cases_dir: Path) -> None:
    console.print(f"seed {result.seed}", highlight=False)
    if result.facts is not None:
        console.print(f"era: {_era_text(result.facts)}", highlight=False)
    if not result.results:
        console.print(
            "The server started and answered the liveness probe, but there are no "
            "checks to run yet: conformance checks arrive in milestone M1."
        )
        return
    skipped: dict[str, int] = {}
    for item in result.results:
        if item.status is Status.SKIPPED:
            skipped[item.message] = skipped.get(item.message, 0) + 1
            continue
        if item.status is Status.PASSED:
            continue
        style = _STYLE.get(item.status, "")
        label = item.status.value
        if item.status is Status.FAILED and item.severity is Severity.INFO:
            style, label = "cyan", "note"  # advice: never fails a run
        line = f"[{style}]{label:>8}[/] {item.case_id} ({item.severity.value})"
        if item.message:
            line += f": {item.message}"
        console.print(line, highlight=False)
        if item.repro:
            console.print(
                f"         replay: mcpprobe replay {cases_dir / item.repro}",
                markup=False,
                highlight=False,
            )
    for reason, count in sorted(skipped.items()):
        console.print(f"[dim] skipped {count} case(s): {reason}[/]", highlight=False)
    notes = sum(
        1 for r in result.results if r.status is Status.FAILED and r.severity is Severity.INFO
    )
    tallies = {status: result.count(status) for status in Status}
    tallies[Status.FAILED] -= notes
    parts = [f"{count} {status.value}" for status, count in tallies.items() if count]
    if notes:
        parts.append(f"{notes} note(s)")
    counts = ", ".join(parts)
    console.print(f"{len(result.results)} cases: {counts}", highlight=False)
    if result.restarts:
        console.print(f"server restarted {result.restarts} time(s)", highlight=False)
    if result.stopped_early:
        console.print("later suites skipped because conformance failed (--keep-going to run them)")
