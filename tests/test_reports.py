"""JUnit, SARIF and HTML reports, and --baseline."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema
import pytest
from typer.testing import CliRunner

from mcpprobe.cli import app
from mcpprobe.engine.catalog import Severity, Suite, load_catalog
from mcpprobe.engine.connection import StdioServer
from mcpprobe.engine.runner import CaseResult, RunResult, Status
from mcpprobe.exit_codes import ExitCode
from mcpprobe.protocol.era import Era, ServerFacts
from mcpprobe.report import html, junit, sarif
from mcpprobe.report.baseline import BaselineError, load_baseline
from mcpprobe.report.common import ReportContext
from mcpprobe.report.json_report import dumps as json_dumps
from tests.support.reference import REFERENCE

if TYPE_CHECKING:
    from collections.abc import Sequence

SARIF_SCHEMA = Path(__file__).parent / "data" / "sarif-schema-2.1.0.json"
SPEC = StdioServer(("python", "-u", "server.py"))


def _result(results: Sequence[CaseResult], baseline: frozenset[str] | None = None) -> RunResult:
    return RunResult(
        seed=7,
        results=sorted(results, key=lambda r: r.case_id),
        facts=ServerFacts(Era.MODERN),
        baseline=baseline,
    )


RESULTS = [
    CaseResult("CONF-001", Suite.CONF, Severity.ERROR, Status.PASSED),
    CaseResult(
        "CONF-004", Suite.CONF, Severity.ERROR, Status.FAILED, "no resultType", "CONF-004.json"
    ),
    CaseResult("CONF-017", Suite.CONF, Severity.WARN, Status.FAILED, "no serverInfo"),
    CaseResult("CONF-008", Suite.CONF, Severity.INFO, Status.FAILED, "tool result for unknown"),
    CaseResult(
        "FUZZ-001/echo",
        Suite.FUZZ,
        Severity.ERROR,
        Status.CRASHED,
        'crashed on {"message": "\\u0080"} <script>alert(1)</script>\x00\ud800',
        "FUZZ-001_echo.json",
    ),
    CaseResult("FUZZ-001/delete", Suite.FUZZ, Severity.ERROR, Status.SKIPPED, "destructive"),
    CaseResult("ROB-002", Suite.ROB, Severity.WARN, Status.ERROR, "KeyError: 'x'"),
]


def _ctx(baseline: frozenset[str] | None = None) -> ReportContext:
    return ReportContext.build(_result(RESULTS, baseline), load_catalog(), ".mcpprobe/cases")


# ------------------------------------------------------------------- JUnit


def test_junit_maps_findings_to_failures_and_warnings_to_output() -> None:
    root = ET.fromstring(junit.dumps(_ctx()))
    assert root.attrib == {
        "name": "mcpprobe",
        "time": "0",
        "tests": "7",
        "failures": "2",
        "errors": "1",
        "skipped": "1",
    }
    cases = {c.attrib["name"]: c for c in root.iter("testcase")}
    assert cases["CONF-004"].find("failure") is not None
    assert cases["FUZZ-001/echo"].find("failure").attrib["type"] == "crashed"  # type: ignore[union-attr]
    assert "Reproduce: mcpprobe replay .mcpprobe/cases/FUZZ-001_echo.json" in (
        cases["FUZZ-001/echo"].find("failure").text or ""  # type: ignore[union-attr]
    )
    assert "warning: no serverInfo" in (cases["CONF-017"].findtext("system-out") or "")
    assert "note: tool result" in (cases["CONF-008"].findtext("system-out") or "")
    assert cases["ROB-002"].find("error") is not None
    assert cases["FUZZ-001/delete"].find("skipped") is not None
    suites = {s.attrib["name"]: s.attrib for s in root.iter("testsuite")}
    assert suites["CONF"]["tests"] == "4"


def test_junit_is_valid_xml_even_with_control_characters() -> None:
    text = junit.dumps(_ctx())
    ET.fromstring(text)  # would raise on \x00 or a lone surrogate
    assert "\\u0000" in text
    assert "\\ud800" in text
    text.encode("utf-8")


def test_junit_baseline_turns_known_findings_into_output() -> None:
    root = ET.fromstring(junit.dumps(_ctx(frozenset({"FUZZ-001/echo"}))))
    assert root.attrib["failures"] == "1"
    case = next(c for c in root.iter("testcase") if c.attrib["name"] == "FUZZ-001/echo")
    assert case.find("failure") is None
    assert (case.findtext("system-out") or "").startswith("known finding (in --baseline)")


# ------------------------------------------------------------------- SARIF


def _sarif(baseline: frozenset[str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(sarif.dumps(_ctx(baseline), "server.py"))
    return data


def test_sarif_validates_against_the_official_schema() -> None:
    schema = json.loads(SARIF_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft4Validator(schema).validate(_sarif())
    jsonschema.Draft4Validator(schema).validate(_sarif(frozenset({"CONF-004"})))


def test_sarif_results_rules_and_fingerprints() -> None:
    run = _sarif()["runs"][0]
    results = {r["properties"]["caseId"]: r for r in run["results"]}
    # Findings, warnings and notes; never passes, skips or MCPProbe's own errors.
    assert sorted(results) == ["CONF-004", "CONF-008", "CONF-017", "FUZZ-001/echo"]
    assert results["CONF-004"]["level"] == "error"
    assert results["CONF-017"]["level"] == "warning"
    assert results["CONF-008"]["level"] == "note"
    assert results["FUZZ-001/echo"]["ruleId"] == "FUZZ-001"
    assert results["FUZZ-001/echo"]["message"]["text"].startswith("FUZZ-001/echo: [crashed]")
    location = results["CONF-004"]["locations"][0]["physicalLocation"]
    assert location == {"artifactLocation": {"uri": "server.py"}, "region": {"startLine": 1}}
    hashes = {r["partialFingerprints"]["primaryLocationLineHash"] for r in results.values()}
    assert len(hashes) == len(results)
    assert hashes == {
        r["partialFingerprints"]["primaryLocationLineHash"] for r in _sarif()["runs"][0]["results"]
    }
    rules = {rule["id"]: rule for rule in run["tool"]["driver"]["rules"]}
    assert "FUZZ-001" in rules
    assert rules["CONF-017"]["defaultConfiguration"]["level"] == "warning"
    assert rules["CONF-004"]["helpUri"].startswith("https://modelcontextprotocol.io/")


def test_sarif_baseline_state() -> None:
    results = {
        r["properties"]["caseId"]: r for r in _sarif(frozenset({"CONF-004"}))["runs"][0]["results"]
    }
    assert results["CONF-004"]["baselineState"] == "unchanged"
    assert results["FUZZ-001/echo"]["baselineState"] == "new"
    assert "baselineState" not in _sarif()["runs"][0]["results"][0]


def test_sarif_default_location(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.js").write_text("")
    argv = ("node", "--inspect", "dist/index.js", "--flag")
    assert sarif.default_location(argv, tmp_path) == "dist/index.js"
    assert sarif.default_location(("npx", "-y", "pkg"), tmp_path) == "README.md"
    assert sarif.default_location(("python", "/abs/server.py"), tmp_path) == "README.md"
    assert sarif.default_location(None, tmp_path) == "README.md"


# -------------------------------------------------------------------- HTML


def test_html_is_self_contained_and_escaped() -> None:
    page = html.dumps(_ctx(), SPEC)
    assert page.startswith("<!doctype html>")
    assert "<script" not in page  # the message's <script> is escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert " src=" not in page
    assert "<link" not in page
    assert "FUZZ-001/echo" in page
    assert "Findings that fail the run (3)" in page  # incl. the MCPProbe error
    assert "mcpprobe replay .mcpprobe/cases/FUZZ-001_echo.json" in page
    assert html.dumps(_ctx(), SPEC) == page


def test_html_baseline_sections() -> None:
    page = html.dumps(_ctx(frozenset({"CONF-004"})), SPEC)
    assert "New findings (2)" in page
    assert "Known findings (in baseline) (1)" in page


# ---------------------------------------------------------------- baseline


def test_baseline_known_findings_do_not_fail_the_run() -> None:
    assert _result(RESULTS).exit_code is ExitCode.PROBE_ERROR  # ROB-002 is an MCPProbe error
    clean = [r for r in RESULTS if r.status is not Status.ERROR]
    assert _result(clean).exit_code is ExitCode.FAILED
    assert _result(clean, frozenset({"CONF-004"})).exit_code is ExitCode.FAILED
    known = frozenset({"CONF-004", "FUZZ-001/echo"})
    assert _result(clean, known).exit_code is ExitCode.PASSED


def test_baseline_round_trip_through_the_json_report(tmp_path: Path) -> None:
    clean = [r for r in RESULTS if r.status is not Status.ERROR]
    path = tmp_path / "probe.json"
    path.write_text(json_dumps(_result(clean), SPEC), encoding="utf-8")
    known = load_baseline(path)
    # Error findings, crashes and hangs; not warnings or notes.
    assert known == frozenset({"CONF-004", "FUZZ-001/echo"})
    report = json.loads(json_dumps(_result(clean, known), SPEC))
    assert report["baseline"] == {"known_findings": 2}
    flags = {r["id"]: r["baselined"] for r in report["results"]}
    assert flags["CONF-004"] is True
    assert flags["CONF-017"] is False


@pytest.mark.parametrize(
    "content", ["not json", "[]", '{"format": "something-else"}', '{"format": "mcpprobe.report"}']
)
def test_bad_baselines_are_refused(tmp_path: Path, content: str) -> None:
    path = tmp_path / "b.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BaselineError):
        load_baseline(path)


# --------------------------------------------------------------------- CLI

cli = CliRunner()


def _reference(*breaks: str) -> str:
    flags = " ".join(f"--break {b}" for b in breaks)
    return f'"{sys.executable}" -u "{REFERENCE}" {flags}'


@pytest.mark.parametrize("fmt", ["junit", "sarif", "html", "json"])
def test_cli_writes_each_format(tmp_path: Path, fmt: str) -> None:
    out = tmp_path / f"report.{fmt}"
    args = ["run", "--stdio", _reference("CONF-004"), "--suites", "conf", "--seed", "1"]
    args += ["--report", fmt, "-o", str(out), "--cases-dir", str(tmp_path / "cases")]
    result = cli.invoke(app, args)
    assert result.exit_code == ExitCode.FAILED, result.output
    assert "CONF-004" in out.read_text(encoding="utf-8")


def test_cli_baseline_passes_known_and_fails_new(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    common = ["--suites", "conf", "--seed", "1", "--cases-dir", str(tmp_path / "cases")]
    first = cli.invoke(
        app,
        ["run", "--stdio", _reference("CONF-004"), *common, "--report", "json", "-o", str(base)],
    )
    assert first.exit_code == ExitCode.FAILED
    same = cli.invoke(
        app, ["run", "--stdio", _reference("CONF-004"), *common, "--baseline", str(base)]
    )
    assert same.exit_code == ExitCode.PASSED, same.output
    assert "1 known finding(s)" in " ".join(same.output.split())
    worse = cli.invoke(
        app,
        ["run", "--stdio", _reference("CONF-004", "CONF-005"), *common, "--baseline", str(base)],
    )
    assert worse.exit_code == ExitCode.FAILED


def test_cli_refuses_a_baseline_that_is_not_a_report(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    result = cli.invoke(app, ["run", "--stdio", "x", "--baseline", str(bad)])
    assert result.exit_code == ExitCode.CONFIG_INVALID
