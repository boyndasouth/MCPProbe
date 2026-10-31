"""SARIF 2.1.0 for GitHub code scanning.

One run; one rule per check that produced a result (with its spec link as
help); one SARIF result per non-passing case: findings and warnings
(``error`` / ``warning``) and info findings (``note``). Skipped and passing
cases are left out, as code scanning expects.

Code scanning only shows results that have a location, and a server finding
has no line of code. Every result therefore points at line 1 of one file in
the repository: ``--sarif-path`` if given, else the first relative file in
the stdio command (``dist/index.js``), else ``README.md``. Alerts are
deduplicated across runs by ``partialFingerprints.primaryLocationLineHash``,
derived from the case id, which is stable across runs.

Built as plain JSON (no sarif-om objects) so key order and output are fixed;
tests validate it against the official 2.1.0 schema.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from mcpprobe import __version__
from mcpprobe.engine.catalog import Severity
from mcpprobe.engine.runner import Status
from mcpprobe.report.common import INFORMATION_URI, TOOL_NAME, Outcome, ReportContext, outcome

if TYPE_CHECKING:
    from pathlib import Path

    from mcpprobe.engine.catalog import Check
    from mcpprobe.engine.runner import CaseResult

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
DEFAULT_LOCATION = "README.md"

_LEVEL = {Severity.ERROR: "error", Severity.WARN: "warning", Severity.INFO: "note"}
_PROBLEM = {Severity.ERROR: "error", Severity.WARN: "warning", Severity.INFO: "recommendation"}


def default_location(argv: tuple[str, ...] | None, cwd: Path) -> str:
    """A repository file for alerts to point at (see the module docstring)."""
    for arg in argv or ():
        candidate = PurePosixPath(arg.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts or arg.startswith("-"):
            continue
        if (cwd / candidate).is_file():
            return candidate.as_posix()
    return DEFAULT_LOCATION


def _rule(check: Check) -> dict[str, Any]:
    tags = [check.suite.value.lower(), "mcp"]
    if check.suite.value == "SEC":
        tags.append("security")
    return {
        "id": check.id,
        "shortDescription": {"text": check.title[:1024]},
        "fullDescription": {"text": check.description[:1024]},
        "helpUri": check.spec_ref,
        "help": {
            "text": f"{check.description}\n\nSpec: {check.spec_ref}",
            "markdown": f"{check.description}\n\n[Spec section]({check.spec_ref})",
        },
        "defaultConfiguration": {"level": _LEVEL[check.severity]},
        "properties": {
            "tags": tags,
            "precision": "high" if check.severity is Severity.ERROR else "medium",
            "problem.severity": _PROBLEM[check.severity],
        },
    }


def _level(result: CaseResult) -> str:
    if result.status in (Status.CRASHED, Status.TIMEOUT):
        return "error"
    return _LEVEL[result.severity]


def _fingerprint(case_id: str) -> str:
    return hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:32] + ":1"


def _result(ctx: ReportContext, result: CaseResult, location: str) -> dict[str, Any]:
    text = result.message or ctx.title(result)
    if result.status in (Status.CRASHED, Status.TIMEOUT):
        text = f"[{result.status.value}] {text}"
    command = ctx.repro_command(result)
    if command:
        text += f"\n\nReproduce: {command}"
    item: dict[str, Any] = {
        "ruleId": result.case_id.split("/", 1)[0].split("@", 1)[0],
        "level": _level(result),
        "message": {"text": f"{result.case_id}: {text}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": location},
                    "region": {"startLine": 1},
                }
            }
        ],
        "partialFingerprints": {"primaryLocationLineHash": _fingerprint(result.case_id)},
        "properties": {"caseId": result.case_id, "status": result.status.value},
    }
    if ctx.baselined(result):
        item["baselineState"] = "unchanged"
    elif ctx.result.baseline is not None:
        item["baselineState"] = "new"
    return item


def build(ctx: ReportContext, location: str) -> dict[str, Any]:
    reported = [
        r
        for r in sorted(ctx.result.results, key=lambda r: r.case_id)
        if outcome(r) in (Outcome.FINDING, Outcome.WARNING, Outcome.NOTE)
    ]
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "semanticVersion": __version__,
                        "informationUri": INFORMATION_URI,
                        "rules": [_rule(check) for check in ctx.used_checks()],
                    }
                },
                "results": [_result(ctx, r, location) for r in reported],
                "properties": {"seed": ctx.result.seed},
            }
        ],
    }


def dumps(ctx: ReportContext, location: str) -> str:
    data = build(ctx, location)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate in a message: escape everything
        text = json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    return text
