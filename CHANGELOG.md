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
- Request metadata header helper for 2026-07-28 (`Mcp-Method`, `Mcp-Name`,
  Base64 sentinel encoding).
