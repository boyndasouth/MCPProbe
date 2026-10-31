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

- **Per-tool cases.** A case marked ``per_tool`` runs once per tool from
  ``tools/list`` (fetched on the suite's connection) as ``<id>/<tool>``.
- **Related checks.** A case can judge several checks from one stream of
  traffic (FUZZ does); each gets its own result and repro.

Conformance errors stop later suites unless ``keep_going`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from mcpprobe.engine.catalog import Era as CheckEra
from mcpprobe.engine.catalog import Severity, Suite
from mcpprobe.engine.connection import Connection, ServerSpec, connect, recording_of
from mcpprobe.engine.seed import rng
from mcpprobe.exit_codes import ExitCode
from mcpprobe.protocol import legacy
from mcpprobe.protocol.era import Era, ServerFacts, detect_era
from mcpprobe.protocol.modern import request as modern_request
from mcpprobe.transport.errors import TransportClosed, TransportError, TransportTimeout

if TYPE_CHECKING:
    import random
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

LIVENESS_METHOD = "tools/list"
MAX_TOOL_PAGES = 20


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
    related: Mapping[str, Outcome] = field(default_factory=dict, compare=False)
    """Verdicts for the case's related checks, keyed by check id (see ``Case.related``)."""
    evidence: Connection | None = field(default=None, compare=False, repr=False)
    """The connection whose recording best reproduces this outcome. Defaults to
    the case's own connection; a fuzz case points at a fresh connection that
    carries only the shrunk input."""

    @classmethod
    def passed(cls) -> Outcome:
        return cls(Status.PASSED)

    @classmethod
    def failed(cls, message: str) -> Outcome:
        return cls(Status.FAILED, message)

    @classmethod
    def skipped(cls, message: str) -> Outcome:
        return cls(Status.SKIPPED, message)


Mode = Literal["modern", "legacy"]
LEGACY_SUFFIX = "@legacy"


@dataclass(frozen=True)
class CaseContext:
    connection: Connection
    seed: int
    case_id: str
    timeout: float
    rng: random.Random = field(repr=False)
    facts: ServerFacts = field(default_factory=lambda: ServerFacts(Era.MODERN))
    allow_destructive: bool = False
    mode: Mode = "modern"
    """Which era this case speaks: ``legacy`` means ``connection`` holds an
    initialized legacy session."""
    startup_timeout: float = 30.0
    open_fresh: Callable[[], Awaitable[Connection]] | None = field(default=None, repr=False)
    """Opens a new, started, uninitialized connection; closed after the case."""
    tool: Mapping[str, Any] | None = None
    """The tool definition a per-tool case targets (see ``Case.per_tool``)."""
    allow_open_world: bool = False
    fuzz_examples: int = 50
    """Generated arguments per tool for FUZZ cases."""

    @property
    def capabilities(self) -> dict[str, Any]:
        """Server capabilities for this case's era."""
        if self.mode == "legacy":
            return self.facts.legacy_capabilities
        return self.facts.capabilities

    @property
    def capabilities_known(self) -> bool:
        if self.mode == "legacy":
            return self.facts.initialize is not None
        return self.facts.capabilities_known


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
    timeout_factor: float = 1.0
    """Cases that call many tools get a multiple of the per-case timeout."""
    fresh_sessions: int = 0
    """Fresh connections the case opens; each adds the start-up timeout to its budget."""
    per_tool: bool = False
    """Run once per tool from tools/list, as ``<id>/<tool name>``, with ``ctx.tool`` set."""
    once: bool = False
    """A both-era case that runs in one era only: modern if the server speaks it.
    For checks about tool behaviour, which does not depend on the era."""
    related: tuple[RelatedCheck, ...] = ()
    """Further checks judged from the same traffic as this one. Each gets its own
    result, ``<related id><this case's suffix>``, from ``Outcome.related``."""
    tool: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RelatedCheck:
    id: str
    severity: Severity
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


def _split_suffix(case_id: str) -> tuple[str, str]:
    """``"FUZZ-001/echo@legacy"`` -> ``("FUZZ-001/echo", "@legacy")``."""
    if case_id.endswith(LEGACY_SUFFIX):
        return case_id[: -len(LEGACY_SUFFIX)], LEGACY_SUFFIX
    return case_id, ""


def _related_id(case: Case, rel: RelatedCheck) -> str:
    """The related check's id with this case's detail: FUZZ-001/echo -> FUZZ-002/echo."""
    return rel.id + case.id[len(case.id.split("/", 1)[0].split("@", 1)[0]) :]


@dataclass
class Runner:
    spec: ServerSpec
    seed: int
    cases_dir: Path
    case_timeout: float = 10.0
    liveness_timeout: float = 5.0
    startup_timeout: float = 30.0
    keep_going: bool = False
    max_restarts: int = 20
    allow_destructive: bool = False
    allow_open_world: bool = False
    fuzz_examples: int = 50
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

    async def alive(self, connection: Connection, timeout: float | None = None) -> bool:
        """Any JSON-RPC reply to a cheap tools/list, in the connection's era, means alive."""
        if not connection.running:
            return False
        self._liveness_counter += 1
        probe_id = f"mcpprobe-liveness-{self._liveness_counter}"
        probe: dict[str, Any]
        if connection.legacy_version is None and self._facts.era.speaks_modern:
            probe = modern_request(LIVENESS_METHOD, probe_id)
        else:
            probe = legacy.request(LIVENESS_METHOD, probe_id)
        try:
            reply = await connection.call(probe, timeout or self.liveness_timeout)
        except TransportError:
            return False
        return reply.response is not None or reply.http_status is not None

    async def preflight(self) -> ServerFacts:
        """Start the server, learn its era, and make sure it answers."""
        connection = await self._open()
        try:
            # Era first: on stdio, server/discover must be the first request, or a
            # dual-era server may lock into legacy semantics (stdio#backward-compatibility).
            self._facts = await detect_era(
                connection, self.liveness_timeout, self.startup_timeout, open_fresh=self._open
            )
            if self._facts.auth_required:
                raise ServerUnavailable(
                    "the server requires authorization (HTTP 401); pass credentials with "
                    "--header 'Authorization: Bearer <token>' (or the header your server expects)"
                )
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
            runnable: list[tuple[Case, Mode]] = []
            for case in cases:
                expanded = self._expand(case)
                if isinstance(expanded, str):
                    result.results.extend(self._skipped_all(case, expanded))
                    continue
                for instance, mode in expanded:
                    if result.stopped_early:
                        result.results.extend(self._skipped_all(instance, "conformance failed"))
                    else:
                        runnable.append((instance, mode))
            for mode in ("modern", "legacy"):
                group = [case for case, case_mode in runnable if case_mode == mode]
                if group:
                    result.results.extend(await self._run_group(suite, mode, group, result))
            if suite is Suite.CONF and not self.keep_going:
                result.stopped_early = any(
                    r.suite is Suite.CONF and r.is_finding for r in result.results
                )
        result.results.sort(key=lambda r: r.case_id)
        return result

    def _expand(self, case: Case) -> list[tuple[Case, Mode]] | str:
        """The (case, mode) runs for this server, or why the case is skipped.

        A both-era case runs once per era the server speaks; its legacy run is
        suffixed ``@legacy`` so ids stay stable when a server migrates.
        """
        era = self._facts.era
        if era is Era.UNKNOWN:
            return "server answered neither server/discover nor initialize"
        if case.era is CheckEra.MODERN:
            if not era.speaks_modern:
                return f"server does not speak 2026-07-28 (detected era: {era.value})"
            return [(case, "modern")]
        if case.era is CheckEra.LEGACY:
            if not era.speaks_legacy:
                return (
                    f"server does not speak an initialize-era revision (detected era: {era.value})"
                )
            return [(case, "legacy")]
        if case.era is CheckEra.DUAL:
            if era is not Era.DUAL:
                return f"server is not dual-era (detected era: {era.value})"
            return [(case, "modern")]
        runs: list[tuple[Case, Mode]] = []
        if era.speaks_modern:
            runs.append((case, "modern"))
        if era.speaks_legacy and not (case.once and runs):
            runs.append((replace(case, id=case.id + LEGACY_SUFFIX), "legacy"))
        return runs

    @staticmethod
    def _skipped(case: Case, reason: str) -> CaseResult:
        return CaseResult(
            case.id, case.suite, case.severity, Status.SKIPPED, reason, spec_ref=case.spec_ref
        )

    @classmethod
    def _skipped_all(cls, case: Case, reason: str) -> list[CaseResult]:
        """The case and each of its related checks, skipped for one reason."""
        return [cls._skipped(case, reason)] + [
            CaseResult(
                _related_id(case, rel),
                case.suite,
                rel.severity,
                Status.SKIPPED,
                reason,
                spec_ref=rel.spec_ref,
            )
            for rel in case.related
        ]

    async def _tools(self, connection: Connection, mode: Mode) -> list[dict[str, Any]] | str:
        """Every tool from tools/list (following pagination), or why there are none."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(MAX_TOOL_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            request_id = f"mcpprobe-tools-{page}"
            message = (
                legacy.request("tools/list", request_id, params)
                if mode == "legacy"
                else modern_request("tools/list", request_id, params)
            )
            try:
                reply = await connection.call(message, self.case_timeout)
            except TransportError as exc:
                return f"tools/list failed: {exc}"
            result = reply.result
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                return "the server did not list any tools"
            tools.extend(t for t in result["tools"] if isinstance(t, dict))
            next_cursor = result.get("nextCursor")
            cursor = next_cursor if isinstance(next_cursor, str) and next_cursor else None
            if cursor is None:
                break
        if not tools:
            return "the server has no tools"
        return sorted(tools, key=lambda t: str(t.get("name")))

    @staticmethod
    def _per_tool(case: Case, tools: Sequence[dict[str, Any]]) -> list[Case]:
        out: list[Case] = []
        seen: dict[str, int] = {}
        base, suffix = _split_suffix(case.id)
        for tool in tools:
            name = str(tool.get("name"))
            seen[name] = seen.get(name, 0) + 1
            label = name if seen[name] == 1 else f"{name}#{seen[name]}"
            out.append(replace(case, id=f"{base}/{label}{suffix}", tool=tool, per_tool=False))
        return out

    async def _open_ready(self, mode: Mode) -> Connection | None:
        """A started connection that has answered once, in ``mode``; None if it won't."""
        try:
            connection = await self._open()
        except ServerUnavailable:
            return None
        # A fresh process may still be starting (npx, uvx): wait for its first
        # answer here, so the start-up delay is never charged to a case.
        if mode == "legacy":
            with contextlib.suppress(TransportError):
                await legacy.open_session(connection, self.startup_timeout)
            ready = connection.legacy_version is not None and await self.alive(connection)
        else:
            ready = await self.alive(connection, self.startup_timeout)
        if ready:
            return connection
        await connection.close()
        return None

    async def _run_group(
        self, suite: Suite, mode: Mode, cases: Sequence[Case], run: RunResult
    ) -> list[CaseResult]:
        results: list[CaseResult] = []
        connection = await self._open_ready(mode)
        down = "server is down" if mode == "modern" else "a legacy session could not be opened"
        if connection is None:
            for case in sorted(cases, key=lambda c: c.id):
                results.extend(self._skipped_all(case, down))
            return results
        expanded: list[Case] = []
        tools: list[dict[str, Any]] | str | None = None
        for case in cases:
            if not case.per_tool:
                expanded.append(case)
                continue
            if tools is None:
                tools = await self._tools(connection, mode)
            if isinstance(tools, str):
                results.extend(self._skipped_all(case, tools))
            else:
                expanded.extend(self._per_tool(case, tools))
        order = sorted(expanded, key=lambda c: c.id)
        rng(self.seed, "order", suite.value, mode).shuffle(order)
        try:
            for case in order:
                if connection is None:
                    results.extend(self._skipped_all(case, down))
                    continue
                case_connection = connection
                outcome, fresh = await self._run_case(connection, case, mode)
                saved_from = outcome.evidence or (fresh[-1] if fresh else connection)
                if not await self.alive(connection):
                    reason = f": {outcome.message}" if outcome.message else ""
                    outcome = replace(
                        outcome,
                        status=Status.CRASHED,
                        message=(
                            f"server stopped responding after this case ({outcome.status}{reason})"
                        ),
                    )
                    repro: str | None = self._save(case.id, outcome.evidence or connection)
                    await connection.close()
                    connection = await self._restart(run, mode)
                elif outcome.status in (Status.PASSED, Status.SKIPPED):
                    repro = None
                else:
                    repro = self._save(case.id, saved_from)
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
                results.extend(self._related_results(case, outcome, case_connection))
                for extra in fresh:
                    await extra.close()
        finally:
            if connection is not None:
                await connection.close()
        return results

    def _related_results(
        self, case: Case, outcome: Outcome, fallback: Connection
    ) -> list[CaseResult]:
        out: list[CaseResult] = []
        for rel in case.related:
            rel_id = _related_id(case, rel)
            verdict = outcome.related.get(rel.id)
            if verdict is None and outcome.status is Status.SKIPPED:
                verdict = outcome
            elif verdict is None:
                verdict = Outcome.skipped(f"not judged: {case.id} {outcome.status.value}")
            repro = None
            if verdict.status not in (Status.PASSED, Status.SKIPPED):
                repro = self._save(rel_id, verdict.evidence or outcome.evidence or fallback)
            out.append(
                CaseResult(
                    rel_id,
                    case.suite,
                    rel.severity,
                    verdict.status,
                    verdict.message,
                    repro,
                    rel.spec_ref,
                )
            )
        return out

    async def _restart(self, run: RunResult, mode: Mode) -> Connection | None:
        """New connection after a crash; None when the server cannot come back."""
        if run.restarts >= self.max_restarts:
            return None
        run.restarts += 1
        return await self._open_ready(mode)

    async def _run_case(
        self, connection: Connection, case: Case, mode: Mode
    ) -> tuple[Outcome, list[Connection]]:
        fresh: list[Connection] = []

        async def open_fresh() -> Connection:
            opened = await self._open()
            fresh.append(opened)
            return opened

        context = CaseContext(
            connection=connection,
            seed=self.seed,
            case_id=case.id,
            timeout=self.case_timeout,
            rng=rng(self.seed, "case", case.id),
            facts=self._facts,
            allow_destructive=self.allow_destructive,
            mode=mode,
            startup_timeout=self.startup_timeout,
            open_fresh=open_fresh,
            tool=case.tool,
            allow_open_world=self.allow_open_world,
            fuzz_examples=self.fuzz_examples,
        )
        budget = (
            self.case_timeout * case.timeout_factor + self.startup_timeout * case.fresh_sessions
        )
        try:
            async with asyncio.timeout(budget):
                return await case.run(context), fresh
        except (TimeoutError, TransportTimeout):
            return Outcome(Status.TIMEOUT, f"no answer within {budget:g}s"), fresh
        except TransportClosed as exc:
            return Outcome(Status.CRASHED, str(exc)), fresh
        except Exception as exc:  # a bug in the check, reported rather than raised
            return Outcome(Status.ERROR, f"{type(exc).__name__}: {exc}"), fresh

    def _save(self, case_id: str, connection: Connection) -> str:
        # Named by case id alone so the report stays byte-identical across runs.
        text = recording_of(connection).dumps()
        name = f"{_safe_name(case_id)}.json"
        self.cases_dir.mkdir(parents=True, exist_ok=True)
        (self.cases_dir / name).write_text(text, encoding="utf-8")
        return name


def summarize(result: RunResult) -> dict[str, Any]:
    """Counts by status, in a fixed order (for reports)."""
    return {status.value: result.count(status) for status in Status}
