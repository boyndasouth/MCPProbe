"""Runs cases against a server: scheduling, isolation, liveness and crash handling.

Lifecycle, per the spec's Architecture section:

- **Isolation.** Each suite gets a fresh connection. For stdio that is a fresh
  server process; HTTP servers are reused (they are not ours to restart).
- **Order.** Cases run in an order shuffled by the run seed, which surfaces
  hidden order dependencies; results are reported sorted by case id, so the
  report does not depend on that order.
- **Liveness.** After every case the runner sends a cheap ``tools/list``,
  carrying modern ``_meta`` when the server is modern so that a dual-era
  server is never nudged into its legacy mode. Any JSON-RPC reply, even an
  error, means alive. No reply, a dead process
  or a dropped connection marks the case that just ran as the crash cause;
  a stdio server is then restarted and the suite continues.
- **Repro.** Every case that does not pass is saved as a recording of the
  connection so far, which ``mcpprobe replay`` re-runs byte for byte.
- **Era.** The preflight decides whether the server is modern-era once per
  run, sending ``server/discover`` before anything else as the stdio binding
  requires; checks for an era the server does not speak are skipped.

Conformance errors stop later suites unless ``keep_going`` is set.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mcpprobe.engine.catalog import Era as CheckEra
from mcpprobe.engine.catalog import Severity, Suite
from mcpprobe.engine.connection import Connection, ServerSpec, connect, recording_of
from mcpprobe.engine.seed import rng
from mcpprobe.exit_codes import ExitCode
from mcpprobe.protocol.era import Era, ServerFacts, detect_era
from mcpprobe.protocol.modern import request as modern_request
from mcpprobe.transport.errors import TransportClosed, TransportError, TransportTimeout

if TYPE_CHECKING:
    import random
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

LIVENESS_METHOD = "tools/list"


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"  # the server violated the check
    CRASHED = "crashed"  # the server died or stopped answering because of this case
    TIMEOUT = "timeout"  # the case did not finish in time (server hang)
    SKIPPED = "skipped"
    ERROR = "error"  # MCPProbe itself failed: a bug in a check, not in the server


@dataclass(frozen=True)
class Outcome:
    status: Status
    message: str = ""

    @classmethod
    def passed(cls) -> Outcome:
        return cls(Status.PASSED)

    @classmethod
    def failed(cls, message: str) -> Outcome:
        return cls(Status.FAILED, message)

    @classmethod
    def skipped(cls, message: str) -> Outcome:
        return cls(Status.SKIPPED, message)


@dataclass(frozen=True)
class CaseContext:
    connection: Connection
    seed: int
    case_id: str
    timeout: float
    rng: random.Random = field(repr=False)
    facts: ServerFacts = field(default_factory=lambda: ServerFacts(Era.MODERN))


@dataclass(frozen=True)
class Case:
    id: str
    """Stable id: the check id, plus ``/<detail>`` for generated cases."""
    suite: Suite
    severity: Severity
    run: Callable[[CaseContext], Awaitable[Outcome]]
    title: str = ""
    era: CheckEra = CheckEra.BOTH
    spec_ref: str = ""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    suite: Suite
    severity: Severity
    status: Status
    message: str = ""
    repro: str | None = None
    """File name of the saved recording, relative to the cases directory."""
    spec_ref: str = ""

    @property
    def is_finding(self) -> bool:
        """Counts against the run: crashes and hangs always, failures by severity."""
        if self.status in (Status.CRASHED, Status.TIMEOUT):
            return True
        return self.status is Status.FAILED and self.severity is Severity.ERROR


@dataclass
class RunResult:
    seed: int
    results: list[CaseResult] = field(default_factory=list)
    restarts: int = 0
    stopped_early: bool = False
    facts: ServerFacts | None = None

    @property
    def exit_code(self) -> ExitCode:
        if any(r.status is Status.ERROR for r in self.results):
            return ExitCode.PROBE_ERROR
        if any(r.is_finding for r in self.results):
            return ExitCode.FAILED
        return ExitCode.PASSED

    def count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status is status)


class ServerUnavailable(TransportError):
    """The server could not be started or never answered. Maps to exit code 2."""


def _safe_name(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id)


@dataclass
class Runner:
    spec: ServerSpec
    seed: int
    cases_dir: Path
    case_timeout: float = 10.0
    liveness_timeout: float = 5.0
    keep_going: bool = False
    max_restarts: int = 20
    connect: Callable[[ServerSpec], Connection] = field(default=connect)
    _liveness_counter: int = field(default=0, init=False)
    _facts: ServerFacts = field(default_factory=lambda: ServerFacts(Era.MODERN), init=False)

    # ------------------------------------------------------------- lifecycle

    async def _open(self) -> Connection:
        connection = self.connect(self.spec)
        try:
            await connection.start()
        except TransportError as exc:
            raise ServerUnavailable(f"server did not start: {exc}") from exc
        return connection

    async def alive(self, connection: Connection) -> bool:
        if not connection.running:
            return False
        self._liveness_counter += 1
        probe_id = f"mcpprobe-liveness-{self._liveness_counter}"
        probe: dict[str, Any]
        if self._facts.era is Era.MODERN:
            probe = modern_request(LIVENESS_METHOD, probe_id)
        else:
            probe = {"jsonrpc": "2.0", "id": probe_id, "method": LIVENESS_METHOD}
        try:
            reply = await connection.call(probe, self.liveness_timeout)
        except TransportError:
            return False
        return reply.response is not None or reply.http_status is not None

    async def preflight(self) -> ServerFacts:
        """Start the server, make sure it answers, and learn its era."""
        connection = await self._open()
        try:
            # Era first: on stdio, server/discover must be the first request, or a
            # dual-era server may lock into legacy semantics (stdio#backward-compatibility).
            self._facts = await detect_era(connection, self.liveness_timeout)
            if not await self.alive(connection):
                detail = ""
                stderr = getattr(getattr(connection, "stdio", None), "stderr_tail", None)
                if stderr:
                    detail = f"; stderr: {' | '.join(stderr)}"
                raise ServerUnavailable(
                    f"server did not answer a {LIVENESS_METHOD} probe within "
                    f"{self.liveness_timeout:g}s{detail}"
                )
            return self._facts
        finally:
            await connection.close()

    # ------------------------------------------------------------------ runs

    async def run(self, plan: Sequence[tuple[Suite, Sequence[Case]]]) -> RunResult:
        result = RunResult(seed=self.seed)
        self._facts = await self.preflight()
        result.facts = self._facts
        for suite, cases in plan:
            runnable: list[Case] = []
            for case in cases:
                reason = self._skip_reason(case)
                if reason is None and result.stopped_early:
                    reason = "conformance failed"
                if reason is None:
                    runnable.append(case)
                else:
                    result.results.append(
                        CaseResult(
                            case.id,
                            case.suite,
                            case.severity,
                            Status.SKIPPED,
                            reason,
                            spec_ref=case.spec_ref,
                        )
                    )
            if not runnable:
                continue
            result.results.extend(await self._run_suite(suite, runnable, result))
            if suite is Suite.CONF and not self.keep_going:
                result.stopped_early = any(
                    r.suite is Suite.CONF and r.is_finding for r in result.results
                )
        result.results.sort(key=lambda r: r.case_id)
        return result

    def _skip_reason(self, case: Case) -> str | None:
        modern = self._facts.era is Era.MODERN
        if case.era is CheckEra.MODERN and not modern:
            return "server is not modern-era (2026-07-28); legacy checks arrive in M2"
        if case.era in (CheckEra.LEGACY, CheckEra.DUAL):
            return "legacy and dual-era checks arrive in M2"
        return None

    async def _run_suite(
        self, suite: Suite, cases: Sequence[Case], run: RunResult
    ) -> list[CaseResult]:
        order = sorted(cases, key=lambda c: c.id)
        rng(self.seed, "order", suite.value).shuffle(order)
        results: list[CaseResult] = []
        connection: Connection | None = await self._open()
        try:
            for case in order:
                if connection is None:
                    results.append(
                        CaseResult(
                            case.id,
                            case.suite,
                            case.severity,
                            Status.SKIPPED,
                            "server is down",
                            spec_ref=case.spec_ref,
                        )
                    )
                    continue
                outcome = await self._run_case(connection, case)
                if not await self.alive(connection):
                    reason = f": {outcome.message}" if outcome.message else ""
                    outcome = Outcome(
                        Status.CRASHED,
                        f"server stopped responding after this case ({outcome.status}{reason})",
                    )
                    repro = self._save(case, connection)
                    await connection.close()
                    connection = await self._restart(run)
                elif outcome.status in (Status.PASSED, Status.SKIPPED):
                    repro = None
                else:
                    repro = self._save(case, connection)
                results.append(
                    CaseResult(
                        case.id,
                        case.suite,
                        case.severity,
                        outcome.status,
                        outcome.message,
                        repro,
                        case.spec_ref,
                    )
                )
        finally:
            if connection is not None:
                await connection.close()
        return results

    async def _restart(self, run: RunResult) -> Connection | None:
        """New connection after a crash; None when the server cannot come back."""
        if run.restarts >= self.max_restarts:
            return None
        run.restarts += 1
        try:
            connection = await self._open()
        except ServerUnavailable:
            return None
        if await self.alive(connection):
            return connection
        await connection.close()
        return None

    async def _run_case(self, connection: Connection, case: Case) -> Outcome:
        context = CaseContext(
            connection=connection,
            seed=self.seed,
            case_id=case.id,
            timeout=self.case_timeout,
            rng=rng(self.seed, "case", case.id),
            facts=self._facts,
        )
        try:
            async with asyncio.timeout(self.case_timeout):
                return await case.run(context)
        except (TimeoutError, TransportTimeout):
            return Outcome(Status.TIMEOUT, f"no answer within {self.case_timeout:g}s")
        except TransportClosed as exc:
            return Outcome(Status.CRASHED, str(exc))
        except Exception as exc:  # a bug in the check, reported rather than raised
            return Outcome(Status.ERROR, f"{type(exc).__name__}: {exc}")

    def _save(self, case: Case, connection: Connection) -> str:
        # Named by case id alone so the report stays byte-identical across runs.
        text = recording_of(connection).dumps()
        name = f"{_safe_name(case.id)}.json"
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        (self.cases_dir / name).write_text(text, encoding="utf-8")
        return name


def summarize(result: RunResult) -> dict[str, Any]:
    """Counts by status, in a fixed order (for reports)."""
    return {status.value: result.count(status) for status in Status}
