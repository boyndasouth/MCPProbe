# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
- CONF-009 (undeclared client capabilities), CONF-010 (multi round-trip
  requests) and CONF-011 (structuredContent vs outputSchema). They call only
  tools marked read-only or non-destructive, never open-world ones, with
  minimal valid arguments built from each inputSchema.
- Reference fixture server (`fixtures/servers/reference.py`, stdio and HTTP)
  with `--break <ID>` to violate one check at a time.
- Era probe: `server/discover` is sent first, and modern checks are skipped
  on servers that do not speak 2026-07-28.
- JSON report (`--report json`), deterministic for a given seed.
- Request metadata header helper for 2026-07-28 (`Mcp-Method`, `Mcp-Name`,
  Base64 sentinel encoding).
