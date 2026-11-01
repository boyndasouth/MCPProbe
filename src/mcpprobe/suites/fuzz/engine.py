"""Drives Hypothesis against one tool and shrinks what it finds.

Hypothesis is synchronous and the wire client is asyncio, so the search runs
in a worker thread and hands each ``tools/call`` to the event loop.

1. **Observe.** One seeded search sends ``fuzz_examples`` generated argument
   sets and records how the server answered each. A crash or a hang ends it.
2. **Shrink.** For every check that saw a violation, the same seeded search
   runs again with that check as the failure condition. With the same seed it
   reaches the same failing example, and Hypothesis then shrinks it to a
   minimal one. Shrinking is capped by a call budget, not a clock, so the
   result does not depend on how fast the machine is.
3. **Evidence** (done by the caller): the minimal input is sent once more on a
   fresh connection, whose recording becomes the replayable case.

A crash is shrunk on stdio by restarting the server between attempts (up to
``MAX_RESTARTS``). An HTTP server is not ours to restart: when a request is
dropped without an answer, shrinking goes on only if the server still answers
others (a crashed request handler, not a dead server). Hangs are never
shrunk: each attempt would cost a full timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

from hypothesis import find
from hypothesis.errors import NoSuchExample

from mcpprobe.engine.connection import StdioConnection
from mcpprobe.engine.seed import derive_seed
from mcpprobe.protocol import legacy
from mcpprobe.protocol.modern import meta
from mcpprobe.protocol.modern import request as modern_request
from mcpprobe.suites.fuzz.strategies import Example, search_settings
from mcpprobe.transport.errors import TransportError, TransportTimeout

if TYPE_CHECKING:
    from concurrent.futures import Future

    from mcpprobe.engine.connection import Connection, Reply
    from mcpprobe.engine.runner import CaseContext
    from mcpprobe.suites.fuzz.strategies import ToolStrategy

MAX_SHRINK_CALLS: Final = 100
MAX_RESTARTS: Final = 25
REVIVE_TIMEOUT: Final = 5.0
INTERNAL_ERROR: Final = -32603
INVALID_PARAMS: Final = -32602

Kind = Literal[
    "result",
    "tool_error",
    "input_required",
    "rpc_error",
    "no_response",
    "crash",
    "hang",
    "dropped",
    "slow",
]
NO_ANSWER: Final = ("crash", "hang", "dropped")
"""Verdicts that violate FUZZ-001. A "slow" call (still reporting progress when
time ran out) is working, not stuck, so it ends the search without a finding."""
STOPS: Final = (*NO_ANSWER, "slow")
PROGRESS_FACTOR: Final = 3
"""Progress notifications keep a call alive up to this many timeouts in total."""


@dataclass(frozen=True)
class Verdict:
    """How the server answered one call."""

    kind: Kind
    code: int | None = None
    detail: str = ""

    def describe(self) -> str:
        if self.kind == "result":
            return "accepted (a result without isError)"
        if self.kind == "tool_error":
            return "a tool result with isError: true"
        if self.kind == "input_required":
            return "an input_required result"
        if self.kind == "rpc_error":
            return f"JSON-RPC error {self.code}" + (f" ({self.detail})" if self.detail else "")
        if self.kind == "no_response":
            return "no JSON-RPC response" + (f" ({self.detail})" if self.detail else "")
        if self.kind == "crash":
            return "the server crashed or closed the connection"
        if self.kind == "slow":
            return f"still reporting progress after {self.detail}"
        if self.kind == "dropped":
            return (
                f"no answer within {self.detail}, although the server kept answering "
                "other requests (the request was dropped)"
            )
        return f"no answer within {self.detail}, and the server stopped answering (it hung)"


@dataclass(frozen=True)
class Observation:
    example: Example
    valid: bool
    verdict: Verdict


Predicate = Callable[[Observation], bool]


def classify(reply: Reply) -> Verdict:
    error = reply.error
    if error is not None:
        code = error.get("code")
        message = error.get("message")
        return Verdict(
            "rpc_error",
            code if isinstance(code, int) else None,
            _one_line(message) if isinstance(message, str) else "",
        )
    result = reply.result
    if isinstance(result, dict):
        if result.get("resultType") == "input_required":
            return Verdict("input_required")
        if result.get("isError") is True:
            return Verdict("tool_error")
        return Verdict("result")
    status = f"HTTP {reply.http_status}" if reply.http_status is not None else ""
    return Verdict("no_response", detail=status)


def _one_line(text: str, limit: int = 80) -> str:
    """Error text for a report line: whitespace collapsed, abbreviated."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class _Stop(BaseException):
    """Ends a Hypothesis search at once (Hypothesis only catches Exception)."""


@dataclass
class FuzzResult:
    observations: list[Observation] = field(default_factory=list)
    minimal: dict[str, Observation] = field(default_factory=dict)
    """Check id -> smallest observation that violates it."""
    shrunk: dict[str, bool] = field(default_factory=dict)
    """Check id -> whether ``minimal`` came out of a shrink."""


class Driver:
    """Runs one tool's search. ``run`` executes in a worker thread."""

    def __init__(
        self,
        ctx: CaseContext,
        strategy: ToolStrategy,
        checks: dict[str, Predicate],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        assert ctx.tool is not None
        self.ctx = ctx
        self.tool_name = str(ctx.tool.get("name"))
        self.strategy = strategy
        self.checks = checks
        self.loop = loop
        self.stop = threading.Event()
        self.connection: Connection = ctx.connection
        self.restartable = isinstance(ctx.connection, StdioConnection)
        self.restarts = 0
        self.calls = 0
        self._dead = False
        self._pending: Future[Any] | None = None
        self.crashes: list[Observation] = []
        """Every crash seen, in either phase (a shrink attempt can crash it too)."""
        self.seed = derive_seed(ctx.seed, "fuzz", ctx.case_id)

    # -------------------------------------------------------------- calling

    def cancel(self) -> None:
        """Called from the event loop when the case is cancelled or times out."""
        self.stop.set()
        if self._pending is not None:
            self._pending.cancel()

    def _run(self, coro: Any) -> Any:
        if self.stop.is_set():
            coro.close()  # never scheduled: close it so it is not reported as un-awaited
            raise _Stop
        self._pending = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return self._pending.result()
        except BaseException as exc:
            if self.stop.is_set():
                raise _Stop from exc
            raise

    def call(self, arguments: Any) -> Verdict:
        if self._dead or not self.connection.running:
            if self.restartable:
                if self.restarts >= MAX_RESTARTS:
                    raise _Stop
                self.restarts += 1
                self.connection = self._run(self._fresh())
            elif not self._run(self._answers()):
                raise _Stop  # an HTTP server that is really down cannot be restarted
            self._dead = False
        self.calls += 1
        verdict: Verdict = self._run(self._call(self.connection, arguments, self.calls))
        if verdict.kind == "crash":
            self._dead = True
        elif verdict.kind == "hang" and self._run(self._answers()):
            verdict = Verdict("dropped", detail=verdict.detail)
        return verdict

    async def _fresh(self) -> Connection:
        return await prepared(self.ctx)

    async def _answers(self) -> bool:
        """Whether the server still answers other requests (after one went unanswered)."""
        message = (
            legacy.request("tools/list", "mcpprobe-revive")
            if self.ctx.mode == "legacy"
            else modern_request("tools/list", "mcpprobe-revive")
        )
        try:
            reply = await self.connection.call(message, min(self.ctx.timeout, REVIVE_TIMEOUT))
        except TransportError:
            return False
        return reply.response is not None

    async def _call(self, connection: Connection, arguments: Any, number: int) -> Verdict:
        return await call_tool(self.ctx, connection, self.tool_name, arguments, number)

    def observe(self, example: Example) -> Observation:
        verdict = self.call(example.arguments)
        observation = Observation(example, bool(self.strategy.is_valid(example.arguments)), verdict)
        if verdict.kind == "crash":
            self.crashes.append(observation)
        return observation

    # -------------------------------------------------------------- search

    def _search(self, condition: Callable[[Example], bool], examples: int) -> None:
        with contextlib.suppress(NoSuchExample):
            find(
                self.strategy.examples,
                condition,
                settings=search_settings(examples),
                random=random.Random(self.seed),
            )

    def run(self) -> FuzzResult:
        result = FuzzResult()
        examples = self.ctx.fuzz_examples

        def record(example: Example) -> bool:
            observation = self.observe(example)
            result.observations.append(observation)
            if observation.verdict.kind in STOPS:
                raise _Stop  # stop early rather than wait out more timeouts
            return False

        try:
            self._search(record, examples)
        except _Stop:
            if self.stop.is_set():
                return result
        for check_id, violates in self.checks.items():
            first = next((o for o in result.observations if violates(o)), None)
            if first is None:
                continue
            result.minimal[check_id] = first
            result.shrunk[check_id] = False
            if first.verdict.kind in ("hang", "dropped"):
                continue  # each shrink attempt would cost a full timeout
            try:
                best = self._shrink(violates, first, examples)
            except _Stop:
                if self.stop.is_set():
                    return result
                best = None
            if best is not None:
                result.minimal[check_id] = best
                result.shrunk[check_id] = True
        return result

    def _shrink(self, violates: Predicate, first: Observation, examples: int) -> Observation | None:
        """Re-run the seeded search with ``violates`` as the failure; keep the smallest hit."""
        best: list[Observation] = []
        budget = [examples + MAX_SHRINK_CALLS]

        def condition(example: Example) -> bool:
            budget[0] -= 1
            if budget[0] < 0:
                raise _Stop
            observation = self.observe(example)
            if violates(observation):
                best.append(observation)  # Hypothesis only tries smaller candidates
                return True
            if observation.verdict.kind in ("hang", "dropped", "slow"):
                raise _Stop  # never wait out timeouts while shrinking
            return False

        try:
            self._search(condition, examples)
        except _Stop:
            if self.stop.is_set():
                raise
        return best[-1] if best else None


# ------------------------------------------------------------------ helpers


async def prepared(ctx: CaseContext) -> Connection:
    """A fresh connection that has answered once, in the case's era."""
    assert ctx.open_fresh is not None
    connection = await ctx.open_fresh()
    if ctx.mode == "legacy":
        await legacy.open_session(connection, ctx.startup_timeout)
    else:
        await connection.call(modern_request("tools/list", "mcpprobe-ready"), ctx.startup_timeout)
    return connection


async def call_tool(
    ctx: CaseContext, connection: Connection, tool: str, arguments: Any, number: int
) -> Verdict:
    """One ``tools/call`` with ``arguments`` exactly as generated."""
    request_id = f"{ctx.case_id}-{number}"
    params: dict[str, Any] = {"name": tool, "arguments": arguments}
    # A progress token lets a slow tool that reports progress keep the call alive
    # (basic/patterns/cancellation#timeouts), so slow is not mistaken for hung.
    if ctx.mode == "legacy":
        params["_meta"] = {"progressToken": request_id}
        message = legacy.request("tools/call", request_id, params)
    else:
        request_meta = {**meta(), "progressToken": request_id}
        message = modern_request("tools/call", request_id, params, request_meta=request_meta)
    try:
        reply = await connection.call(
            message, ctx.timeout, max_timeout=ctx.timeout * PROGRESS_FACTOR
        )
    except TransportTimeout as exc:
        if exc.progress:
            return Verdict("slow", detail=f"{ctx.timeout * PROGRESS_FACTOR:g}s")
        return Verdict("hang", detail=f"{ctx.timeout:g}s")
    except TransportError as exc:
        return Verdict("crash", detail=str(exc)[:120])
    return classify(reply)


# ----------------------------------------------------------------- display

MAX_SHOWN_STRING: Final = 40
MAX_SHOWN_DEPTH: Final = 4
MAX_SHOWN: Final = 300


def _shorten(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str) and len(value) > MAX_SHOWN_STRING:
        return f"{value[:12]}…({len(value)} chars)"
    if isinstance(value, (list, dict)) and depth >= MAX_SHOWN_DEPTH:
        nested = 0
        node: Any = value
        while isinstance(node, (list, dict)) and node:
            node = node[0] if isinstance(node, list) else next(iter(node.values()))
            nested += 1
        return f"…({nested} more levels)"
    if isinstance(value, list):
        return [_shorten(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: _shorten(item, depth + 1) for key, item in value.items()}
    return value


def show(arguments: Any) -> str:
    """Arguments for a report line: escapes visible, long values abbreviated."""
    text = json.dumps(_shorten(arguments), ensure_ascii=True, separators=(", ", ": "))
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 1] + "…"
