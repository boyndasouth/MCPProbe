"""A single self-contained HTML page for sharing a run.

No external assets, scripts or fonts; light and dark themes follow the
viewer's system setting. Like every report, it carries no timestamps and is
byte-identical for the same seed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from mcpprobe import __version__
from mcpprobe.engine.connection import HttpServer
from mcpprobe.report.common import Outcome, ReportContext, outcome, safe_text

if TYPE_CHECKING:
    from mcpprobe.engine.connection import ServerSpec
    from mcpprobe.engine.runner import CaseResult

_LABELS = {
    Outcome.FINDING: "fails the run",
    Outcome.WARNING: "warning",
    Outcome.NOTE: "note",
    Outcome.PROBE_ERROR: "MCPProbe error",
    Outcome.PASSED: "passed",
    Outcome.SKIPPED: "skipped",
}


def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("mcpprobe.report", "templates"),
        autoescape=select_autoescape(default=True, default_for_string=True),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def _server(spec: ServerSpec) -> str:
    if isinstance(spec, HttpServer):
        return spec.url
    return " ".join(spec.argv)


def _item(ctx: ReportContext, result: CaseResult) -> dict[str, Any]:
    kind = outcome(result)
    known = ctx.baselined(result)
    check = ctx.check_for(result)
    label = _LABELS[kind]
    if kind == Outcome.FINDING and result.status.value in ("crashed", "timeout"):
        label = result.status.value
    return {
        "id": result.case_id,
        "kind": "error" if kind == Outcome.PROBE_ERROR else kind,
        "label": label,
        "known": known,
        "title": ctx.title(result),
        "message": safe_text(result.message),
        "repro": ctx.repro_command(result),
        "spec": check.spec_ref if check is not None else result.spec_ref,
    }


def dumps(ctx: ReportContext, spec: ServerSpec) -> str:
    result = ctx.result
    ordered = sorted(result.results, key=lambda r: r.case_id)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in ordered:
        by_kind.setdefault(outcome(r), []).append(_item(ctx, r))
    findings = by_kind.get(Outcome.FINDING, []) + by_kind.get(Outcome.PROBE_ERROR, [])
    new = [i for i in findings if not i["known"]]
    sections = [
        {
            "title": "Findings that fail the run" if result.baseline is None else "New findings",
            "entries": new,
            "empty": "None.",
        }
    ]
    if result.baseline is not None:
        known = [i for i in findings if i["known"]]
        sections.append(
            {"title": "Known findings (in baseline)", "entries": known, "empty": "None."}
        )
    sections += [
        {"title": "Warnings", "entries": by_kind.get(Outcome.WARNING, []), "empty": "None."},
        {"title": "Notes", "entries": by_kind.get(Outcome.NOTE, []), "empty": "None."},
    ]
    rest = by_kind.get(Outcome.PASSED, []) + by_kind.get(Outcome.SKIPPED, [])
    rest.sort(key=lambda i: str(i["id"]))
    facts = result.facts
    tiles = [
        ("cases", len(ordered)),
        ("fail the run", len(new)),
        ("warnings", len(by_kind.get(Outcome.WARNING, []))),
        ("notes", len(by_kind.get(Outcome.NOTE, []))),
        ("passed", len(by_kind.get(Outcome.PASSED, []))),
        ("skipped", len(by_kind.get(Outcome.SKIPPED, []))),
    ]
    exit_code = int(result.exit_code)
    verdict = {0: "Passed", 1: "Failed", 2: "Could not run"}.get(exit_code, "Error")
    template = _environment().get_template("report.html.j2")
    page = template.render(
        version=__version__,
        server=_server(spec),
        era="unknown" if facts is None else facts.era.value,
        seed=result.seed,
        baseline=None if result.baseline is None else len(result.baseline),
        exit_code=exit_code,
        verdict=verdict,
        tiles=tiles,
        sections=sections,
        rest=rest,
    )
    return safe_text(page)
