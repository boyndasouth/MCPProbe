"""The M3 gate: the same seed gives an identical report.

A full run (CONF, FUZZ, ROB and SEC, with findings in every suite) against the
reference server is pinned as a snapshot. CI runs it on Linux, macOS and
Windows with Python 3.11 to 3.13, so the snapshot also proves the report does
not depend on the platform or the interpreter. Only the server's command line
(which holds this machine's paths) and MCPProbe's version are normalised.

If a change to a check or a message is intended, refresh the snapshot with
``uv run pytest tests/test_determinism.py --snapshot-update`` and review the diff.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mcpprobe.engine.catalog import Suite, Transport, load_catalog
from mcpprobe.engine.runner import Runner, Status
from mcpprobe.report import html, junit, sarif
from mcpprobe.report.common import ReportContext
from mcpprobe.report.json_report import build
from mcpprobe.suites import SuiteOptions, collect
from tests.support.reference import stdio_spec

if TYPE_CHECKING:
    from pathlib import Path

    from syrupy.assertion import SnapshotAssertion

# One finding in each suite, a crash that is shrunk across restarts, and a warn.
BREAKS = ("CONF-017", "FUZZ-001", "FUZZ-004", "ROB-002", "ROB-005", "SEC-006")
SUITES = [Suite.CONF, Suite.FUZZ, Suite.ROB, Suite.SEC]


async def _report(tmp_path: Path, seed: int) -> tuple[str, dict[str, str]]:
    text, saved, _ = await _run(tmp_path, seed)
    return text, saved


async def _run(tmp_path: Path, seed: int) -> tuple[str, dict[str, str], dict[str, str]]:
    """The normalised JSON report, the saved cases, and every other report format."""
    spec = stdio_spec(BREAKS)
    plan = collect(SUITES, load_catalog(), SuiteOptions(transport=Transport.STDIO))
    cases = tmp_path / "cases"
    runner = Runner(spec, seed, cases, case_timeout=5, liveness_timeout=5, keep_going=True)
    result = await runner.run(plan)
    report: dict[str, Any] = build(result, spec)
    report["server"] = {"transport": "stdio", "argv": ["<reference server>"], "env_keys": []}
    report["tool"]["version"] = "<version>"  # so a release does not invalidate the snapshot
    saved = {path.name: path.read_text(encoding="utf-8") for path in sorted(cases.iterdir())}
    assert any(r.status is Status.CRASHED for r in result.results)  # the shrunk crash ran
    ctx = ReportContext.build(result, load_catalog(), ".mcpprobe/cases")
    formats = {
        "junit": junit.dumps(ctx),
        "sarif": sarif.dumps(ctx, "server.py"),
        "html": html.dumps(ctx, spec).replace(" ".join(spec.argv), "<reference server>"),
    }
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n", saved, formats


async def test_full_report_is_pinned(tmp_path: Path, snapshot: SnapshotAssertion) -> None:
    text, _ = await _report(tmp_path, seed=20261031)
    assert text == snapshot


async def test_same_seed_twice_gives_identical_report_and_cases(tmp_path: Path) -> None:
    first, first_cases, first_formats = await _run(tmp_path / "a", seed=11)
    second, second_cases, second_formats = await _run(tmp_path / "b", seed=11)
    assert first == second
    assert first_formats == second_formats  # JUnit, SARIF and HTML too
    assert first_cases.keys() == second_cases.keys()
    for name, text in first_cases.items():
        # Recordings hold the server's argv; both runs used the same one.
        assert text == second_cases[name], name


async def test_another_seed_changes_the_search_not_the_verdicts(tmp_path: Path) -> None:
    first, _ = await _report(tmp_path / "a", seed=1)
    second, _ = await _report(tmp_path / "b", seed=2)
    statuses = [
        {r["id"]: r["status"] for r in json.loads(text)["results"]} for text in (first, second)
    ]
    assert statuses[0] == statuses[1]
