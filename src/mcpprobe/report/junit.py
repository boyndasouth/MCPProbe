"""JUnit XML for CI test tabs: one ``<testsuite>`` per suite, one ``<testcase>`` per case.

Mapping, chosen so the test tab agrees with the exit code:

- a finding that fails the run (error severity, crash, hang) -> ``<failure>``
- a finding that a ``--baseline`` already knew -> passing, with a note
- a warning or info finding -> passing, with the finding in ``<system-out>``
- skipped -> ``<skipped>``; a bug in MCPProbe itself -> ``<error>``

No timestamps or durations (``time="0"``), so the file is deterministic.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from mcpprobe.engine.catalog import Suite
from mcpprobe.report.common import Outcome, ReportContext, outcome

if TYPE_CHECKING:
    from mcpprobe.engine.runner import CaseResult


# Characters XML 1.0 cannot carry at all (control characters, lone surrogates).
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml(text: str, limit: int | None = None) -> str:
    """Text made safe for XML: impossible characters shown as \\uXXXX."""
    clean = _NOT_XML.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
    return clean if limit is None else clean[:limit]


def _details(ctx: ReportContext, result: CaseResult) -> str:
    lines = [result.message] if result.message else []
    check = ctx.check_for(result)
    if check is not None:
        lines.append(f"Check: {check.id} ({check.severity.value}): {check.title}")
        lines.append(f"Spec: {check.spec_ref}")
    command = ctx.repro_command(result)
    if command:
        lines.append(f"Reproduce: {command}")
    return _xml("\n".join(lines))


def _case(ctx: ReportContext, parent: ET.Element, result: CaseResult) -> tuple[int, int, int]:
    """Append one testcase; return (failures, errors, skipped) it adds."""
    node = ET.SubElement(
        parent,
        "testcase",
        {"classname": f"mcpprobe.{result.suite.value}", "name": _xml(result.case_id), "time": "0"},
    )
    kind = outcome(result)
    if kind == Outcome.FINDING and not ctx.baselined(result):
        failure = ET.SubElement(
            node, "failure", {"type": result.status.value, "message": _xml(result.message, 1000)}
        )
        failure.text = _details(ctx, result)
        return 1, 0, 0
    if kind == Outcome.PROBE_ERROR:
        error = ET.SubElement(
            node, "error", {"type": "mcpprobe", "message": _xml(result.message, 1000)}
        )
        error.text = _details(ctx, result)
        return 0, 1, 0
    if kind == Outcome.SKIPPED:
        ET.SubElement(node, "skipped", {"message": _xml(result.message, 1000)})
        return 0, 0, 1
    if kind != Outcome.PASSED:
        label = "known finding (in --baseline)" if kind == Outcome.FINDING else kind
        out = ET.SubElement(node, "system-out")
        out.text = f"{label}: {_details(ctx, result)}"
    return 0, 0, 0


def dumps(ctx: ReportContext) -> str:
    result = ctx.result
    root = ET.Element("testsuites", {"name": "mcpprobe", "time": "0"})
    totals = [0, 0, 0, 0]
    for suite in Suite:
        cases = sorted((r for r in result.results if r.suite is suite), key=lambda r: r.case_id)
        if not cases:
            continue
        node = ET.SubElement(root, "testsuite", {"name": suite.value, "time": "0"})
        properties = ET.SubElement(node, "properties")
        ET.SubElement(properties, "property", {"name": "seed", "value": str(result.seed)})
        counts = [0, 0, 0]
        for case in cases:
            for i, n in enumerate(_case(ctx, node, case)):
                counts[i] += n
        node.set("tests", str(len(cases)))
        node.set("failures", str(counts[0]))
        node.set("errors", str(counts[1]))
        node.set("skipped", str(counts[2]))
        totals[0] += len(cases)
        for i in range(3):
            totals[i + 1] += counts[i]
    for name, value in zip(("tests", "failures", "errors", "skipped"), totals, strict=True):
        root.set(name, str(value))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
