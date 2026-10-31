"""Shared helpers for report writers: check lookup, labels, repro commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcpprobe.engine.catalog import Severity
from mcpprobe.engine.runner import Status

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcpprobe.engine.catalog import Check
    from mcpprobe.engine.runner import CaseResult, RunResult

TOOL_NAME = "mcpprobe"
INFORMATION_URI = "https://github.com/boyndasouth/MCPProbe"


_SURROGATE = re.compile("[\ud800-\udfff]")


def safe_text(text: str) -> str:
    """Lone surrogates (echoed from fuzzed input) shown as \\udXXX: they have no UTF-8 form."""
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def check_id(case_id: str) -> str:
    """``"FUZZ-001/echo@legacy"`` -> ``"FUZZ-001"``."""
    return case_id.split("/", 1)[0].split("@", 1)[0]


class Outcome:
    """How a result reads in a report (one of these strings)."""

    PASSED = "passed"
    FINDING = "finding"  # fails the run (new) or would have (baselined)
    WARNING = "warning"
    NOTE = "note"
    SKIPPED = "skipped"
    PROBE_ERROR = "probe error"


def outcome(result: CaseResult) -> str:
    if result.status is Status.PASSED:
        return Outcome.PASSED
    if result.status is Status.SKIPPED:
        return Outcome.SKIPPED
    if result.status is Status.ERROR:
        return Outcome.PROBE_ERROR
    if result.is_finding:
        return Outcome.FINDING
    return Outcome.NOTE if result.severity is Severity.INFO else Outcome.WARNING


@dataclass(frozen=True)
class ReportContext:
    result: RunResult
    checks: Mapping[str, Check]
    cases_dir: str
    """As given on the command line, for repro commands (never resolved)."""

    @classmethod
    def build(cls, result: RunResult, catalog: Sequence[Check], cases_dir: str) -> ReportContext:
        return cls(result, {check.id: check for check in catalog}, cases_dir)

    def check_for(self, result: CaseResult) -> Check | None:
        return self.checks.get(check_id(result.case_id))

    def title(self, result: CaseResult) -> str:
        check = self.check_for(result)
        return check.title if check is not None else result.case_id

    def repro_command(self, result: CaseResult) -> str | None:
        if result.repro is None:
            return None
        return f"mcpprobe replay {self.cases_dir.rstrip('/')}/{result.repro}"

    def baselined(self, result: CaseResult) -> bool:
        baseline = self.result.baseline
        return baseline is not None and result.is_finding and result.case_id in baseline

    def used_checks(self) -> list[Check]:
        """Catalog entries for every check that has a result, sorted by id."""
        ids = sorted({check_id(r.case_id) for r in self.result.results})
        return [self.checks[i] for i in ids if i in self.checks]
