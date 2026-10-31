# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may
change check behavior; check IDs, exit codes and the report format are
versioned and never silently reused.

## [Unreleased]

Milestone M4, part 1: reports and baseline mode.

### Added
- `--report junit`: one test case per case; findings that fail the run are
  `<failure>`s, warnings and notes are in `<system-out>`.
- `--report sarif`: SARIF 2.1.0 for GitHub code scanning, with one rule per
  check (linking its spec section) and fingerprints that are stable across
  runs. `--sarif-path` picks the repository file alerts point at.
- `--report html`: a single self-contained page with light and dark themes.
- `--baseline probe.json`: findings already in an earlier JSON report are
  shown as known and do not fail the run (or stop later suites); only new
  ones do. The JSON report gains `baseline` and per-result `baselined`.
- The determinism tests now cover every report format.

### Changed
- The package is published as `mcpprobe-cli` (the command and import name
  stay `mcpprobe`): PyPI rejects "mcpprobe" as too similar to "mcp-probe".
- The terminal report labels warnings as `warning` instead of `failed`.
- Dropped the unused `sarif-om` dependency: SARIF is written as plain JSON and
  tested against the official 2.1.0 schema.
- Messages containing a lone surrogate (echoed back from fuzzed input) are
  escaped in every report instead of breaking the file.

## [0.3.0] - 2026-10-31

Milestone M3: the FUZZ and ROB suites. The M3 gate holds: the same seed gives
a byte-identical report, pinned by a snapshot test on every CI platform and
checked by running six public servers twice.

### Added
- FUZZ suite: each tool is called `--examples` times (default 50) with
  arguments generated from its `inputSchema` by Hypothesis, both valid and
  near-miss (missing required field, wrong type, value past a bound, extra
  property, null, deep nesting, huge string, odd Unicode). Validity is decided
  by the tool's own schema, in its own dialect.
- FUZZ-001 (error): no input crashes the server or goes unanswered. FUZZ-002
  (error): invalid input is not answered with -32603 or a non-JSON-RPC reply.
  FUZZ-003 (warn): invalid input is not accepted as a success. FUZZ-004
  (warn): schema-valid input is not rejected with -32602.
- Every FUZZ finding is shrunk to a minimal input and saved as a one-call
  recording on a fresh server for `mcpprobe replay`; stdio crashes are shrunk
  by restarting the server between attempts.
- `--examples N` and `--allow-open-world`. Open-world tools are still never
  called by default.
- Requests can carry a `progressToken`: progress notifications keep a slow
  call alive up to three times the timeout, on stdio and Streamable HTTP.
- A request the server never answers, while it keeps answering others, is
  reported as dropped rather than as a hang.
- ROB suite, run once per server: ROB-001 (warn) malformed JSON gets -32700;
  ROB-002 (warn) invalid envelopes (no `jsonrpc`, version 1.0, numeric method,
  a batch array) are rejected; ROB-003 (error) unknown methods get -32601, with
  HTTP 404 in the modern era; ROB-004 (warn) `tools/call` with non-object
  arguments is rejected without -32603; ROB-005 (error) 1 MB and 10 MB
  requests are answered or rejected; ROB-006 (error) cancellation is handled
  gracefully (stdio notifications, HTTP disconnects); ROB-007 (error)
  concurrent requests each get their own response.
- `--concurrency N` (default 8) for ROB-007.
- A snapshot test pins a full CONF + FUZZ + ROB report, so any change to
  output for the same seed, on any platform, fails CI.

### Changed
- The terminal report no longer swallows text in square brackets in messages.
- Messages that contain a lone surrogate are sent with it escaped (`\ud800`)
  instead of failing to encode.

## [0.2.0] - 2026-10-30

Milestone M2: legacy and dual-era servers. Era detection verified against 10
public MCP servers (14 configurations, stdio and HTTP).

### Added
- Era detection classifies every server as modern, legacy, dual-era or
  unknown: `server/discover` first, then `initialize`, and a dual-era probe
  on a fresh process or session so it never disturbs the modern connection.
- Legacy sessions: the `initialize` handshake, `Mcp-Session-Id` and the
  `MCP-Protocol-Version` header on Streamable HTTP. MCPProbe declares no
  client capabilities in them.
- CONF-005, 007, 008, 011 and 015 now run on legacy servers too; on dual-era
  servers they run once per era, the legacy run suffixed `@legacy`.
- CONF-013 (legacy handshake and version negotiation), CONF-014 (warn:
  initialize-era versions listed in supportedVersions really work), CONF-021
  (warn: unsupported version answered with an error instead of a counter-offer)
  and CONF-022 (info: outdated revision).
- Reference fixture server: `--era legacy|dual`, with stateful legacy sessions
  over stdio and HTTP.
- An HTTP server that answers 401 is reported as needing credentials (exit 2,
  with a hint to pass `--header`) instead of as an unknown era.
- CI type-checks Windows-only and macOS code paths too.

### Fixed
- Windows: servers started through a wrapper (`npx.cmd`, `uvx`, a venv's
  `python.exe`) could leave their real server process running after MCPProbe
  finished. MCPProbe now tracks the server's process tree while it runs and
  ends every process it saw, on top of a kill-on-close Job Object. Tracking
  catches processes Windows moves out of the job (such as the Microsoft Store
  Python) and children whose parent in the middle of the chain already exited.

### Changed
- CONF-002 accepts any error for a request missing `protocolVersion` on a
  dual-era server, which cannot tell which era such a request belongs to.
- The terminal report states the detected era.

## [0.1.0] - 2026-10-30

First release (milestones M0 and M1): 17 conformance checks for the
2026-07-28 revision over stdio and Streamable HTTP, with terminal and JSON
reports and byte-for-byte replay of every failure.

### Added
- Project scaffold: uv project, src layout, ruff, mypy `--strict`, pytest.
- CLI skeleton (`run`, `replay`, `checks list`, `checks explain`) with the
  documented exit codes.
- Check catalog loader with schema validation (spec link required).
- Seed derivation so one `--seed` controls all randomness.
- CI matrix: Linux, macOS, Windows × Python 3.11–3.13.
- Raw wire client: stdio transport (byte-exact sends, line framing, process
  tree shutdown per the spec's stdio Shutdown section) and Streamable HTTP
  transport (HTTP/1.1 and HTTP/2, JSON and SSE responses, early stream close).
- Incremental SSE parser following the WHATWG event-stream rules.
- Versioned recording format with secret masking.
- `mcpprobe record` (developer tool) and a working `mcpprobe replay`.
- Runner: seeded case order, per-suite fresh connection, per-case timeouts,
  a `tools/list` liveness probe between cases, crash attribution with server
  restart, and every non-passing case saved for `mcpprobe replay`.
- `mcpprobe run` executes the runner and prints a terminal summary.
- First conformance checks for 2026-07-28: CONF-001 to 007, 012, 015 to 018
  and 020, each tested against a passing and a failing fixture.
- CONF-008 (info): unknown tools get a JSON-RPC error, not a tool result.
  Info findings show as notes and never fail a run.
- CONF-009 (undeclared client capabilities), CONF-010 (multi round-trip
  requests) and CONF-011 (structuredContent vs outputSchema). They call only
  tools marked read-only or non-destructive, never open-world ones, with
  minimal valid arguments built from each inputSchema.
- `--startup-timeout` (default 30s): the server's first answer may take that
  long (cold `npx`/`uvx`), and every freshly started server is waited for
  before its first case runs.
- Reference fixture server (`fixtures/servers/reference.py`, stdio and HTTP)
  with `--break <ID>` to violate one check at a time.
- Era probe: `server/discover` is sent first, and modern checks are skipped
  on servers that do not speak 2026-07-28.
- JSON report (`--report json`), deterministic for a given seed.
- Request metadata header helper for 2026-07-28 (`Mcp-Method`, `Mcp-Name`,
  Base64 sentinel encoding).

[Unreleased]: https://github.com/boyndasouth/MCPProbe/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/boyndasouth/MCPProbe/releases/tag/v0.3.0
[0.2.0]: https://github.com/boyndasouth/MCPProbe/releases/tag/v0.2.0
[0.1.0]: https://github.com/boyndasouth/MCPProbe/releases/tag/v0.1.0
