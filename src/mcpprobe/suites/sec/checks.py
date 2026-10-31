"""SEC checks: what a server author can act on to keep users safe.

- SEC-001 (warn): tool schemas do not depend on a network ``$ref``.
- SEC-002 (warn): error messages and tool errors carry no stack traces.
- SEC-003 (warn): responses carry no secrets: no value passed with ``--env``
  and nothing shaped like a credential. Judged from SEC-002's traffic.
- SEC-004 (warn, HTTP): a local server listens on loopback only.
- SEC-005 (error, HTTP): a server that requires authorization answers 401,
  publishes Protected Resource Metadata (RFC 9728), and rejects an invalid
  token. Skipped for servers that need no authorization.
- SEC-006 (warn): tool metadata holds no injection-style instructions or
  hidden characters (heuristic).
- SEC-007 (error, modern era): the tool list is the same on every connection
  and does not change as a side effect of a tool call.

Heuristic checks favour precision over recall: a pattern only counts when a
human would agree it is that thing, because a false positive costs more
trust than a missed bug. Secrets are never copied into a report: messages
name the kind of secret and show at most its first four characters, and the
value is masked in the saved recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit, urlunsplit

from mcpprobe.engine.catalog import Transport
from mcpprobe.engine.connection import HttpConnection, HttpServer, StdioConnection, encode
from mcpprobe.engine.runner import Outcome, Status
from mcpprobe.protocol import legacy
from mcpprobe.protocol.http_headers import request_metadata_headers
from mcpprobe.protocol.instances import UNSUPPORTED, minimal_instance
from mcpprobe.protocol.modern import request as modern_request
from mcpprobe.suites.conf.helpers import era_request
from mcpprobe.suites.conf.tools import fetch_tools, unsafe_reason
from mcpprobe.suites.fuzz.engine import prepared
from mcpprobe.transport.errors import TransportError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mcpprobe.engine.connection import Connection, Reply
    from mcpprobe.engine.runner import CaseContext

JsonObject = dict[str, Any]
MAX_PROBED_TOOLS: Final = 5
PROBE_TIMEOUT: Final = 5.0


def _short(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _texts(value: Any) -> Iterable[str]:
    """Every string inside a JSON value (keys included), depth first."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


# ------------------------------------------------------------------ SEC-001


def _remote_refs(schema: Any) -> list[str]:
    found: list[str] = []
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            found.append(ref)
        for value in schema.values():
            found.extend(_remote_refs(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_remote_refs(value))
    return found


async def sec_001(ctx: CaseContext) -> Outcome:
    tools = await fetch_tools(ctx)
    if not tools:
        return Outcome.skipped("the server lists no tools")
    for tool in sorted(tools, key=lambda t: str(t.get("name"))):
        for key in ("inputSchema", "outputSchema"):
            refs = _remote_refs(tool.get(key))
            if refs:
                return Outcome.failed(
                    f"tool {tool.get('name')!r} {key} uses $ref {refs[0]!r}: clients MUST NOT "
                    "fetch it, so they cannot validate against this schema"
                )
    return Outcome.passed()


# ------------------------------------------------------- SEC-002 / SEC-003

STACK_TRACES: Final = (
    ("a Python traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("a Python stack frame", re.compile(r'File "[^"\n]+", line \d+')),
    (
        "a JavaScript stack frame",
        re.compile(r"\n\s*at [^\n]*?\(?(?:file://|node:)?[^\s()]+:\d+:\d+\)?"),
    ),
    ("a Java stack frame", re.compile(r"\n\s*at [\w$.]+\([\w$]+\.(?:java|kt|scala):\d+\)")),
    ("a .NET stack frame", re.compile(r"\n\s*at [\w.`<>]+\(.*\) in .+:line \d+")),
    ("a Go panic", re.compile(r"goroutine \d+ \[running\]:")),
    ("a Rust panic", re.compile(r"thread '[^']*' panicked at ")),
)

SECRET_PATTERNS: Final = (
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "a GitHub token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    ),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("a private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("an API key (sk-…)", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{24,}\b")),
    (
        "a JSON Web Token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
)
MIN_ENV_SECRET: Final = 8


@dataclass
class Probe:
    """One request sent to provoke an error, and every string it got back."""

    what: str
    texts: list[str] = field(default_factory=list)


def _reply_texts(reply: Reply) -> list[str]:
    """Error messages and data, and tool-error content: where servers leak internals."""
    texts: list[str] = []
    response = reply.response or {}
    error = response.get("error")
    if isinstance(error, dict):
        texts.extend(_texts(error))
    result = response.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        texts.extend(_texts(result.get("content")))
        texts.extend(_texts(result.get("structuredContent")))
    texts.extend(item.decode("utf-8", "replace") for item in reply.other)
    return texts


def _wrong_types(arguments: JsonObject) -> JsonObject:
    """Every argument replaced by a value of another JSON type."""
    return {key: 0 if isinstance(value, str) else "mcpprobe" for key, value in arguments.items()}


async def _probes(ctx: CaseContext) -> list[Probe]:
    """Requests that should fail, sent to every safe tool (up to five) and the server."""
    plans: list[tuple[str, str, JsonObject]] = [
        ("an unknown method", "mcpprobe/no-such-method", {}),
        ("tools/call of an unknown tool", "tools/call", {"name": "mcpprobe-no-such-tool"}),
        ("tools/list with an invalid cursor", "tools/list", {"cursor": "mcpprobe-bad-cursor"}),
    ]
    tools = sorted(await fetch_tools(ctx, label="sec"), key=lambda t: str(t.get("name")))
    probed = 0
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str) or probed >= MAX_PROBED_TOOLS:
            continue
        if unsafe_reason(
            tool, allow_destructive=ctx.allow_destructive, allow_open_world=ctx.allow_open_world
        ):
            continue
        probed += 1
        plans.append(
            (f"tools/call {name} with no arguments", "tools/call", {"name": name, "arguments": {}})
        )
        valid = minimal_instance(tool.get("inputSchema", {"type": "object"}))
        if valid is not UNSUPPORTED and isinstance(valid, dict) and valid:
            plans.append(
                (
                    f"tools/call {name} with arguments of the wrong type",
                    "tools/call",
                    {"name": name, "arguments": _wrong_types(valid)},
                )
            )
    probes: list[Probe] = []
    for number, (what, method, params) in enumerate(plans):
        probe = Probe(what)
        try:
            reply = await ctx.connection.call(
                era_request(ctx, method, f"sec-002-{number}", params),
                min(PROBE_TIMEOUT, ctx.timeout),
            )
        except TransportError:
            break  # the liveness probe after the case reports a crash or hang
        probe.texts = _reply_texts(reply)
        probes.append(probe)
    return probes


def _sent_values(ctx: CaseContext) -> set[str]:
    """Env values passed to a stdio server: a server echoing them leaks secrets."""
    connection = ctx.connection
    if isinstance(connection, StdioConnection):
        return {v for v in connection.spec.env.values() if len(v) >= MIN_ENV_SECRET}
    return set()


def _env_name(ctx: CaseContext, value: str) -> str:
    connection = ctx.connection
    assert isinstance(connection, StdioConnection)
    return next(k for k, v in sorted(connection.spec.env.items()) if v == value)


def _secret_finding(ctx: CaseContext, probes: list[Probe]) -> tuple[str, str] | None:
    """(message, secret value) for the first secret found (env values first), or None."""
    env_values = _sent_values(ctx)
    for probe in probes:
        for text in probe.texts:
            for value in sorted(env_values):
                if value in text:
                    name = _env_name(ctx, value)
                    return (
                        f"the value of --env {name} came back in the answer to {probe.what}",
                        value,
                    )
            for kind, pattern in SECRET_PATTERNS:
                match = pattern.search(text)
                if match:
                    secret = match.group()
                    return (
                        f"{kind} ({secret[:4]}…, {len(secret)} characters) came back in the "
                        f"answer to {probe.what}",
                        secret,
                    )
    return None


def _all_secrets(ctx: CaseContext, probes: list[Probe]) -> set[str]:
    found: set[str] = set()
    env_values = _sent_values(ctx)
    for probe in probes:
        for text in probe.texts:
            found.update(value for value in env_values if value in text)
            for _, pattern in SECRET_PATTERNS:
                found.update(match.group() for match in pattern.finditer(text))
    return found


async def sec_002(ctx: CaseContext) -> Outcome:
    probes = await _probes(ctx)
    trace: Outcome = Outcome.passed()
    for probe in probes:
        for text in probe.texts:
            for kind, pattern in STACK_TRACES:
                match = pattern.search(text)
                if match:
                    excerpt = _short(text[max(0, match.start() - 40) : match.end() + 80])
                    trace = Outcome.failed(f"the answer to {probe.what} contains {kind}: {excerpt}")
                    break
            if trace.status is not Status.PASSED:
                break
        if trace.status is not Status.PASSED:
            break
    secret = _secret_finding(ctx, probes)
    # Every secret-shaped string the server sent is masked in the saved case,
    # not only the one the message names.
    ctx.connection.extra_secrets.update(_all_secrets(ctx, probes))
    leak = Outcome.passed() if secret is None else Outcome.failed(secret[0])
    return Outcome(trace.status, trace.message, related={"SEC-003": leak})


# ------------------------------------------------------------------ SEC-004


def _non_loopback_address() -> str | None:
    """This machine's primary non-loopback IPv4 address (no packet is sent)."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1: only selects a route
        address = str(sock.getsockname()[0])
        if not ipaddress.ip_address(address).is_loopback:
            return address
    return None


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host).is_loopback
    return False


async def sec_004(ctx: CaseContext) -> Outcome:
    connection = ctx.connection
    assert isinstance(connection, HttpConnection)
    url = urlsplit(connection.spec.url)
    host = url.hostname or ""
    if not _is_loopback_host(host):
        return Outcome.skipped(
            "the server is not on this machine (its URL is not a loopback address)"
        )
    port = url.port or (443 if url.scheme == "https" else 80)
    address = _non_loopback_address()
    if address is None:
        return Outcome.skipped("this machine has no non-loopback address to test from")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), 2.0)
    except (OSError, TimeoutError):
        return Outcome.passed()
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return Outcome.failed(
        f"port {port} also accepts connections on this machine's network address, so other "
        "machines can reach the server; bind to 127.0.0.1 instead of 0.0.0.0"
    )


# ------------------------------------------------------------------ SEC-005

INVALID_TOKEN: Final = "Bearer mcpprobe-invalid-token"
_AUTH_PARAM = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))')


def _challenge(headers: Iterable[tuple[str, str]]) -> tuple[str, dict[str, str]] | None:
    """(scheme, parameters) of the first WWW-Authenticate header, if any."""
    for name, value in headers:
        if name.lower() == "www-authenticate":
            scheme, _, rest = value.strip().partition(" ")
            params = {
                m.group(1).lower(): m.group(2) or m.group(3) or ""
                for m in _AUTH_PARAM.finditer(rest)
            }
            return scheme, params
    return None


def _well_known(url: str) -> list[str]:
    """RFC 9728 metadata URLs: path-inserted first, then the root."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    root = urlunsplit((parts.scheme, parts.netloc, "/.well-known/oauth-protected-resource", "", ""))
    if path:
        inserted = urlunsplit(
            (parts.scheme, parts.netloc, f"/.well-known/oauth-protected-resource{path}", "", "")
        )
        return [inserted, root]
    return [root]


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


async def _post(
    connection: HttpConnection, body: bytes, extra: list[tuple[str, str]], timeout: float
) -> Any:
    headers = [
        ("Content-Type", "application/json"),
        ("Accept", "application/json, text/event-stream"),
    ]
    return await connection.http.request(
        "POST", body, [*headers, *request_metadata_headers(body), *extra], timeout=timeout
    )


async def _metadata(connection: HttpConnection, url: str, timeout: float) -> Any:
    exchange = await connection.http.request(
        "GET", b"", [("Accept", "application/json")], url=url, timeout=timeout
    )
    if exchange.status != 200:
        return None
    with contextlib.suppress(UnicodeDecodeError, json.JSONDecodeError):
        return json.loads(exchange.body)
    return None


async def sec_005(ctx: CaseContext) -> Outcome:
    connection = ctx.connection
    assert isinstance(connection, HttpConnection)
    url = connection.spec.url
    bare = HttpConnection(HttpServer(url, ()))  # no --header: no credentials
    try:
        message = (
            legacy.initialize("sec-005")
            if ctx.mode == "legacy"
            else modern_request("tools/list", "sec-005")
        )
        body = encode(message)
        exchange = await _post(bare, body, [], ctx.timeout)
        if exchange.status != 401:
            if 200 <= exchange.status < 300:
                return Outcome.skipped("the server answers without authorization")
            return Outcome(
                Status.FAILED,
                f"a request without credentials got HTTP {exchange.status}; a server that "
                "requires authorization MUST answer 401 Unauthorized",
                evidence=bare,
            )
        problems: list[str] = []
        challenge = _challenge(exchange.headers)
        params = challenge[1] if challenge else {}
        if challenge is None:
            problems.append("the 401 has no WWW-Authenticate header")
        elif challenge[0].lower() != "bearer":
            problems.append(f"WWW-Authenticate uses the {challenge[0]!r} scheme, not Bearer")
        candidates = [params["resource_metadata"]] if params.get("resource_metadata") else []
        candidates += _well_known(url)
        metadata: Any = None
        for candidate in candidates:
            if not _same_origin(candidate, url):
                metadata = {
                    "authorization_servers": ["(on another host; not fetched)"],
                    "resource": url,
                }
                break
            metadata = await _metadata(bare, candidate, ctx.timeout)
            if metadata is not None:
                break
        if metadata is None:
            problems.append(
                "no Protected Resource Metadata (RFC 9728): neither resource_metadata in "
                "WWW-Authenticate nor /.well-known/oauth-protected-resource answers"
            )
        elif not isinstance(metadata, dict):
            problems.append("the Protected Resource Metadata is not a JSON object")
        else:
            servers = metadata.get("authorization_servers")
            if not (
                isinstance(servers, list) and servers and all(isinstance(s, str) for s in servers)
            ):
                problems.append("the Protected Resource Metadata has no authorization_servers")
            if not isinstance(metadata.get("resource"), str):
                problems.append("the Protected Resource Metadata has no resource")
        invalid = await _post(bare, body, [("Authorization", INVALID_TOKEN)], ctx.timeout)
        if invalid.status != 401:
            problems.append(f"an invalid bearer token got HTTP {invalid.status}, not 401")
        if problems:
            return Outcome(Status.FAILED, "; ".join(problems), evidence=bare)
        return Outcome.passed()
    except TransportError as exc:
        return Outcome(Status.FAILED, f"authorization discovery failed: {exc}", evidence=bare)
    finally:
        await bare.close()


# ------------------------------------------------------------------ SEC-006

INJECTION_PATTERNS: Final = (
    (
        "an instruction to ignore earlier instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?"
            r"(?:previous|prior|above|earlier|preceding|other)\s+(?:instructions|prompts?|rules)",
            re.IGNORECASE,
        ),
    ),
    (
        "a request to hide something from the user",
        re.compile(
            r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|mention|reveal|show|notify)\b"
            r"[^.]{0,30}\buser\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a pseudo-tag such as <IMPORTANT> or <SYSTEM>",
        re.compile(r"<\s*/?\s*(?:important|system|instructions?|secret|hidden)\s*>", re.IGNORECASE),
    ),
    (
        "a reference to a sensitive local file",
        re.compile(
            r"(?:~|\$HOME|%USERPROFILE%)[/\\][^\s'\"]*?(?:\.ssh|id_rsa|\.aws|\.env\b|mcp\.json|"
            r"credentials|\.netrc|\.npmrc|\.pypirc)",
            re.IGNORECASE,
        ),
    ),
    ("an SSH private key path", re.compile(r"\bid_(?:rsa|ed25519|ecdsa|dsa)\b")),
)
HIDDEN: Final = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")


def _metadata_texts(tool: JsonObject) -> list[tuple[str, str]]:
    """(where, text) for the tool's name, title, description and schema descriptions."""
    out: list[tuple[str, str]] = []
    for key in ("name", "title", "description"):
        if isinstance(tool.get(key), str):
            out.append((key, tool[key]))

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key in ("description", "title"):
                if isinstance(node.get(key), str):
                    out.append((f"{where} {key}", node[key]))
            for key, value in node.items():
                if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
                    for name, sub in value.items():
                        walk(sub, f"{where}.{name}")
                elif isinstance(value, (dict, list)) and key not in ("description", "title"):
                    walk(value, where)
        elif isinstance(node, list):
            for sub in node:
                walk(sub, where)

    walk(tool.get("inputSchema"), "inputSchema")
    return out


async def sec_006(ctx: CaseContext) -> Outcome:
    tools = await fetch_tools(ctx)
    if not tools:
        return Outcome.skipped("the server lists no tools")
    for tool in sorted(tools, key=lambda t: str(t.get("name"))):
        for where, text in _metadata_texts(tool):
            hidden = HIDDEN.search(text)
            if hidden:
                return Outcome.failed(
                    f"tool {tool.get('name')!r} {where} contains an invisible character "
                    f"(U+{ord(hidden.group()):04X}), which can hide text from the user but not "
                    "from the model"
                )
            for kind, pattern in INJECTION_PATTERNS:
                match = pattern.search(text)
                if match:
                    return Outcome.failed(
                        f"tool {tool.get('name')!r} {where} contains {kind}: "
                        f"{_short(match.group(), 80)!r}"
                    )
    return Outcome.passed()


# ------------------------------------------------------------------ SEC-007


def _canonical(tools: list[JsonObject]) -> dict[str, str]:
    """Tool name -> canonical JSON of its definition (without ``_meta``)."""
    out: dict[str, str] = {}
    for tool in tools:
        body = {k: v for k, v in tool.items() if k != "_meta"}
        out[str(tool.get("name"))] = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return out


def _difference(before: dict[str, str], after: dict[str, str]) -> str | None:
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    if added:
        return f"tool {added[0]!r} appeared"
    if removed:
        return f"tool {removed[0]!r} disappeared"
    for name in sorted(before):
        if before[name] != after[name]:
            changed = sorted(
                k
                for k in set(json.loads(before[name])) | set(json.loads(after[name]))
                if json.loads(before[name]).get(k) != json.loads(after[name]).get(k)
            )
            return f"tool {name!r} changed its {', '.join(changed)}"
    return None


async def sec_007(ctx: CaseContext) -> Outcome:
    first = _canonical(await fetch_tools(ctx, label="first"))
    if not first:
        return Outcome.skipped("the server lists no tools")
    tools = await fetch_tools(ctx, label="tools")
    for tool in sorted(tools, key=lambda t: str(t.get("name"))):
        if unsafe_reason(
            tool, allow_destructive=ctx.allow_destructive, allow_open_world=ctx.allow_open_world
        ):
            continue
        arguments = minimal_instance(tool.get("inputSchema", {"type": "object"}))
        if arguments is UNSUPPORTED or not isinstance(arguments, dict):
            continue
        params = {"name": tool.get("name"), "arguments": arguments}
        with contextlib.suppress(TransportError):
            await ctx.connection.call(
                era_request(ctx, "tools/call", "sec-007-call", params), ctx.timeout
            )
        after_call = _canonical(await fetch_tools(ctx, label="after-call"))
        change = _difference(first, after_call)
        if change:
            return Outcome.failed(f"{change} after a tools/call of {tool.get('name')!r}")
        break
    fresh: Connection = await prepared(ctx)
    other = _canonical(await fetch_tools(ctx, fresh, label="fresh"))
    change = _difference(first, other)
    if change:
        return Outcome(Status.FAILED, f"{change} on a second connection", evidence=fresh)
    return Outcome.passed()


# --------------------------------------------------------------------- cases

CaseSpec = tuple[str, str, "Callable[[CaseContext], Awaitable[Outcome]]", frozenset[Transport]]
BOTH = frozenset({Transport.STDIO, Transport.HTTP})
HTTP = frozenset({Transport.HTTP})

CASES: list[CaseSpec] = [
    ("SEC-001", "", sec_001, BOTH),
    ("SEC-002", "", sec_002, BOTH),
    ("SEC-004", "", sec_004, HTTP),
    ("SEC-005", "", sec_005, HTTP),
    ("SEC-006", "", sec_006, BOTH),
    ("SEC-007", "", sec_007, BOTH),
]
RELATED: Final = {"SEC-002": ("SEC-003",)}
FRESH_SESSIONS: Final = {"SEC-007": 1}
TIMEOUT_FACTORS: Final = {"SEC-002": 3.0, "SEC-005": 3.0, "SEC-007": 2.0}
__all__ = ["CASES", "FRESH_SESSIONS", "RELATED", "TIMEOUT_FACTORS"]
