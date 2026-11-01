# MCPProbe

MCPProbe tells an MCP server author, in one run, whether their server is
spec-compliant and robust to bad input. It speaks the stateless
**2026-07-28** revision and the legacy `initialize` era (2025-11-25,
2025-06-18), and runs four suites:

| Suite | What it does |
|---|---|
| **CONF** | Conformance with the MCP specification, each check citing the section it enforces |
| **FUZZ** | Calls every tool with arguments generated from its `inputSchema`, valid and almost valid, and shrinks every failure to a minimal input |
| **ROB** | Malformed JSON, bad envelopes, 1 MB and 10 MB messages, cancellation, concurrent requests |
| **SEC** | Stack traces and secrets in errors, network `$ref`s, local binding, authorization discovery, injection-style tool descriptions, tool lists that change |

The same seed always gives the same report, and every failure is saved as a
byte-for-byte recording that `mcpprobe replay` re-runs.

## Install

Python 3.11 or later; no compiler needed on any platform.

```bash
pipx install mcpprobe-kit       # or: uv tool install mcpprobe-kit
mcpprobe --version
```

The package is called `mcpprobe-kit`; the command is `mcpprobe`.

## First run

```bash
# A stdio server: give the command that starts it
mcpprobe run --stdio "node dist/index.js"
mcpprobe run --stdio "npx -y @modelcontextprotocol/server-everything@latest"

# A Streamable HTTP server that is already running
mcpprobe run --http http://127.0.0.1:8000/mcp --header "Authorization: Bearer $TOKEN"

# See which tools would be called, without calling any
mcpprobe run --stdio "node dist/index.js" --dry-run
```

The summary lists every finding with the command that replays it:

```text
seed 1205925468
era: modern (2026-07-28)
 crashed FUZZ-001/echo (error): server stopped responding after this case
         (failed: tools/call echo with {"message": "\u0080"}: the server crashed)
         replay: mcpprobe replay .mcpprobe/cases/FUZZ-001_echo.json
 warning ROB-002 (warn): a request without "jsonrpc" got a result
50 cases: 39 passed, 1 crashed, 9 skipped, 1 warning(s)
```

Rerun with `--seed 1205925468` to get exactly the same report.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No `error` findings (warnings and notes are allowed) |
| 1 | At least one `error` finding, crash or hang |
| 2 | The probe could not run: the server did not start, was unreachable, needs credentials, or the total timeout ran out |
| 3 | Invalid configuration or command line |

## Next

- [Using MCPProbe in CI](ci.md): the GitHub Action, baselines, other CI systems
- [Configuration](configuration.md): `mcpprobe.toml` and every option
- [Reports](reports.md): terminal, JSON, JUnit, SARIF and HTML
- [Calling your tools safely](safety.md): what MCPProbe calls, and what it never calls
- [Check catalog](checks/index.md): every check, its severity and its spec section
- [Decisions and departures](decisions.md): where MCPProbe differs from its own spec, and why
