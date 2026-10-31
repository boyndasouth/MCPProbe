"""FUZZ checks: schema-driven fuzzing of every tool's ``inputSchema``.

One case per tool, ``FUZZ-001/<tool>``, sends ``--examples`` generated
argument sets (valid values and near-misses, see ``strategies``) and judges
four checks from the same traffic:

- FUZZ-001 (error): no argument set crashes or hangs the server.
- FUZZ-002 (error): invalid arguments are rejected cleanly, not with an
  internal error (-32603) or a reply that is not JSON-RPC.
- FUZZ-003 (warn): invalid arguments are not accepted as a success.
- FUZZ-004 (warn): schema-valid arguments are not rejected as invalid params.

Validity is decided by validating against the tool's own schema. Tools are
called only under the safety rules in ``suites.conf.tools`` (read-only or
non-destructive, never open-world, unless the user opts in).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Final

from mcpprobe.engine.runner import Outcome, Status
from mcpprobe.suites.conf.tools import unsafe_reason
from mcpprobe.suites.fuzz.engine import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    NO_ANSWER,
    Driver,
    FuzzResult,
    Observation,
    Predicate,
    call_tool,
    prepared,
    show,
)
from mcpprobe.suites.fuzz.strategies import UnsupportedSchema, tool_strategy
from mcpprobe.transport.errors import TransportError

if TYPE_CHECKING:
    from mcpprobe.engine.connection import Connection
    from mcpprobe.engine.runner import CaseContext

PRIMARY: Final = "FUZZ-001"
RELATED: Final = ("FUZZ-002", "FUZZ-003", "FUZZ-004")
TIMEOUT_FACTOR: Final = 60.0
FRESH_SESSIONS: Final = 30
"""Restarts while shrinking a crash, plus one fresh connection per finding."""


def _crash_or_hang(o: Observation) -> bool:
    return o.verdict.kind in NO_ANSWER


def _invalid_rejected_badly(o: Observation) -> bool:
    if o.valid:
        return False
    return o.verdict.kind == "no_response" or (
        o.verdict.kind == "rpc_error" and o.verdict.code == INTERNAL_ERROR
    )


def _invalid_accepted(o: Observation) -> bool:
    return not o.valid and o.verdict.kind == "result"


def _valid_rejected(o: Observation) -> bool:
    return o.valid and o.verdict.kind == "rpc_error" and o.verdict.code == INVALID_PARAMS


CHECKS: Final[dict[str, Predicate]] = {
    "FUZZ-001": _crash_or_hang,
    "FUZZ-002": _invalid_rejected_badly,
    "FUZZ-003": _invalid_accepted,
    "FUZZ-004": _valid_rejected,
}
_WHAT: Final = {
    "FUZZ-001": "",
    "FUZZ-002": "invalid arguments got ",
    "FUZZ-003": "invalid arguments were ",
    "FUZZ-004": "schema-valid arguments got ",
}


def _describe(check_id: str, tool: str, o: Observation, shrunk: bool, reproduced: bool) -> str:
    verdict = o.verdict.describe()
    if check_id == PRIMARY:
        text = f"tools/call {tool} with {show(o.example.arguments)}: {verdict}"
    else:
        text = f"{_WHAT[check_id]}{verdict}: tools/call {tool} with {show(o.example.arguments)}"
    if o.example.mutation is not None:
        mutation = o.example.mutation
        target = f" on {mutation.target!r}" if mutation.target else ""
        text += f" [near-miss: {mutation.kind.replace('_', ' ')}{target}]"
    if not shrunk:
        text += " (not shrunk)"
    if not reproduced:
        text += " (did not reproduce on a fresh connection)"
    return text


async def _evidence(
    ctx: CaseContext, check_id: str, tool: str, o: Observation
) -> tuple[Connection | None, bool]:
    """Send the minimal input alone on a fresh connection; its recording is the repro."""
    if o.verdict.kind == "hang":
        return None, True  # the server is stuck; the case's own recording shows it
    try:
        connection = await prepared(ctx)
    except TransportError:
        return None, True  # the server cannot be reached again (HTTP crash)
    try:
        verdict = await call_tool(ctx, connection, tool, o.example.arguments, 0)
    finally:
        with contextlib.suppress(TransportError):
            await connection.close()
    again = Observation(o.example, o.valid, verdict)
    return connection, CHECKS[check_id](again)


def _summary(result: FuzzResult) -> str:
    valid = sum(1 for o in result.observations if o.valid)
    total = len(result.observations)
    text = f"{total} calls ({valid} with valid arguments, {total - valid} invalid)"
    last = result.observations[-1] if result.observations else None
    if last is not None and last.verdict.kind == "slow":
        text += f"; stopped early: the last call was {last.verdict.describe()}"
    elif last is not None and last.verdict.kind in NO_ANSWER:
        text += f"; stopped early at the {PRIMARY} finding, so later inputs went untested"
    return text


async def fuzz_tool(ctx: CaseContext) -> Outcome:
    tool = ctx.tool
    assert tool is not None
    name = str(tool.get("name"))
    reason = unsafe_reason(
        tool, allow_destructive=ctx.allow_destructive, allow_open_world=ctx.allow_open_world
    )
    if reason is not None:
        return Outcome.skipped(f"tool {reason}")
    try:
        strategy = tool_strategy(tool.get("inputSchema"))
    except UnsupportedSchema as exc:
        return Outcome.skipped(f"tool not fuzzed: {str(exc)[:160]}")

    driver = Driver(ctx, strategy, CHECKS, asyncio.get_running_loop())
    try:
        result = await asyncio.to_thread(driver.run)
    except BaseException:
        driver.cancel()
        raise
    if driver.crashes and PRIMARY not in result.minimal:
        result.minimal[PRIMARY] = driver.crashes[0]  # seen while shrinking another finding
        result.shrunk[PRIMARY] = False

    verdicts: dict[str, Outcome] = {}
    for check_id in CHECKS:
        found = result.minimal.get(check_id)
        if found is None:
            verdicts[check_id] = Outcome(Status.PASSED, _summary(result))
            continue
        evidence, reproduced = await _evidence(ctx, check_id, name, found)
        message = _describe(check_id, name, found, result.shrunk[check_id], reproduced)
        status = Status.TIMEOUT if found.verdict.kind == "hang" else Status.FAILED
        verdicts[check_id] = Outcome(status, message, evidence=evidence)

    primary = verdicts.pop(PRIMARY)
    return Outcome(primary.status, primary.message, related=verdicts, evidence=primary.evidence)
