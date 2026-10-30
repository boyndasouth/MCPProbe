# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may
change check behavior; check IDs, exit codes and the report format are
versioned and never silently reused.

## [Unreleased]

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
- Windows: servers started through a wrapper that exits early (`npx.cmd`,
  `uvx`) left their real server process running after MCPProbe finished. Each
  server now runs inside a Job Object that is killed as a whole.

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

[Unreleased]: https://github.com/boyndasouth/MCPProbe/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/boyndasouth/MCPProbe/releases/tag/v0.2.0
[0.1.0]: https://github.com/boyndasouth/MCPProbe/releases/tag/v0.1.0
