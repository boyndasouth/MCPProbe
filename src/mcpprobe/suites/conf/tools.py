"""Calling a server's tools safely, for checks that need real tool results.

Safety rules (spec: "Destructive tools are skipped unless --allow-destructive"):

- MCP's annotation defaults treat a tool as destructive unless it says
  otherwise, so a tool is called only if it is ``readOnlyHint: true`` or
  explicitly ``destructiveHint: false`` (or ``--allow-destructive`` is set).
- Tools explicitly marked ``openWorldHint: true`` are not called unless
  ``--allow-open-world`` is set: they reach outside the server, and MCPProbe
  never sends traffic to hosts the user did not name.
- Arguments are the minimal valid instance of the ``inputSchema``; tools
  whose arguments cannot be built are skipped, not guessed at.

Multi round-trip requests are answered with canned, harmless responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mcpprobe.protocol import legacy
from mcpprobe.protocol.instances import UNSUPPORTED, minimal_instance
from mcpprobe.protocol.modern import meta, request
from mcpprobe.suites.conf.helpers import era_request
from mcpprobe.transport.errors import TransportTimeout

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcpprobe.engine.connection import Reply
    from mcpprobe.engine.runner import CaseContext

JsonObject = dict[str, Any]

TOOL_CALL_TIMEOUT = 5.0
MAX_ROUNDS = 3

# Input request method -> the client capability that allows it (mrtr#server-requirements).
INPUT_REQUEST_CAPABILITY = {
    "elicitation/create": "elicitation",
    "sampling/createMessage": "sampling",
    "roots/list": "roots",
}
FULL_CAPABILITIES: JsonObject = {
    "elicitation": {"form": {}, "url": {}},
    "sampling": {},
    "roots": {},
}
SAMPLING_ANSWER: JsonObject = {
    "role": "assistant",
    "content": {"type": "text", "text": "mcpprobe"},
    "model": "mcpprobe",
    "stopReason": "endTurn",
}


@dataclass(frozen=True)
class CallableTool:
    name: str
    arguments: JsonObject
    definition: JsonObject


@dataclass(frozen=True)
class ToolInventory:
    callable: tuple[CallableTool, ...]
    skipped: tuple[tuple[str, str], ...] = ()
    """(tool name, reason) for tools that were not called."""


@dataclass
class Exchange:
    """One tools/call, following input_required rounds."""

    tool: CallableTool
    replies: list[Reply] = field(default_factory=list)
    timed_out: bool = False

    @property
    def final(self) -> Reply | None:
        return self.replies[-1] if self.replies else None


def unsafe_reason(
    tool: Mapping[str, Any], *, allow_destructive: bool, allow_open_world: bool
) -> str | None:
    """Why calling ``tool`` is not allowed under the safety rules, or None."""
    annotations = tool.get("annotations")
    annotations = annotations if isinstance(annotations, dict) else {}
    if annotations.get("openWorldHint") is True and not allow_open_world:
        return "reaches outside the server (openWorldHint; pass --allow-open-world to include it)"
    if allow_destructive:
        return None
    if annotations.get("readOnlyHint") is True or annotations.get("destructiveHint") is False:
        return None
    return "may be destructive (pass --allow-destructive to include it)"


async def list_tools(ctx: CaseContext, allow_destructive: bool) -> ToolInventory:
    """All tools from tools/list (following pagination), split into callable and skipped."""
    tools: list[JsonObject] = []
    cursor: str | None = None
    for page in range(20):
        params: JsonObject = {"cursor": cursor} if cursor else {}
        reply = await ctx.connection.call(
            era_request(ctx, "tools/list", f"{ctx.case_id}-list-{page}", params), ctx.timeout
        )
        result = reply.result
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            break
        tools.extend(t for t in result["tools"] if isinstance(t, dict))
        cursor = result.get("nextCursor") if isinstance(result.get("nextCursor"), str) else None
        if not cursor:
            break

    callable_tools: list[CallableTool] = []
    skipped: list[tuple[str, str]] = []
    for tool in sorted(tools, key=lambda t: str(t.get("name"))):
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        reason = unsafe_reason(
            tool, allow_destructive=allow_destructive, allow_open_world=ctx.allow_open_world
        )
        if reason is None:
            arguments = minimal_instance(tool.get("inputSchema", {"type": "object"}))
            if arguments is UNSUPPORTED or not isinstance(arguments, dict):
                reason = "could not build valid arguments from its inputSchema"
            else:
                callable_tools.append(CallableTool(name, arguments, tool))
                continue
        skipped.append((name, reason))
    return ToolInventory(tuple(callable_tools), tuple(skipped))


def answer_input_requests(input_requests: JsonObject) -> JsonObject:
    """Harmless responses: minimal form answers, a canned sampling reply, no roots."""
    responses: JsonObject = {}
    for key, item in input_requests.items():
        method = item.get("method") if isinstance(item, dict) else None
        params = item.get("params") if isinstance(item, dict) else None
        params = params if isinstance(params, dict) else {}
        if method == "elicitation/create":
            content = minimal_instance(params.get("requestedSchema", {"type": "object"}))
            if params.get("mode", "form") == "form" and content is not UNSUPPORTED:
                responses[key] = {"action": "accept", "content": content}
            else:
                responses[key] = {"action": "decline"}
        elif method == "sampling/createMessage":
            responses[key] = dict(SAMPLING_ANSWER)
        elif method == "roots/list":
            responses[key] = {"roots": []}
    return responses


async def call_tool(
    ctx: CaseContext, tool: CallableTool, capabilities: JsonObject, *, follow: bool = True
) -> Exchange:
    """Call a tool; when ``follow`` is set, answer input_required rounds and retry."""
    exchange = Exchange(tool)
    params: JsonObject = {"name": tool.name, "arguments": tool.arguments}
    timeout = min(TOOL_CALL_TIMEOUT, ctx.timeout)
    for round_number in range(MAX_ROUNDS + 1):
        request_id = f"{ctx.case_id}-{tool.name}-{round_number}"
        if ctx.mode == "legacy":
            # Capabilities were fixed at initialize (none declared), and legacy
            # servers never answer with input_required.
            message = legacy.request("tools/call", request_id, params)
        else:
            message = request(
                "tools/call", request_id, params, request_meta=meta(capabilities=capabilities)
            )
        try:
            reply = await ctx.connection.call(message, timeout)
        except TransportTimeout:
            exchange.timed_out = True
            return exchange
        exchange.replies.append(reply)
        result = reply.result
        if not (
            follow and isinstance(result, dict) and result.get("resultType") == "input_required"
        ):
            return exchange
        input_requests = result.get("inputRequests")
        params = {"name": tool.name, "arguments": tool.arguments}
        if isinstance(input_requests, dict):
            params["inputResponses"] = answer_input_requests(input_requests)
        if isinstance(result.get("requestState"), str):
            params["requestState"] = result["requestState"]
    return exchange
