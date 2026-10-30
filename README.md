# MCPProbe

Tells an MCP server author, in one run, whether their server is spec-compliant,
robust to bad input, and usable by a real model.

> **Status: pre-alpha (milestone M0 complete on Linux and macOS).** The wire
> client, recorder, replay and runner work. No checks exist yet (they arrive
> in M1), so `mcpprobe run` currently only confirms the server starts and
> answers.

Targets the stateless **2026-07-28** revision plus the legacy
`initialize`-era revisions (2025-11-25, 2025-06-18), and detects dual-era
servers.

## Usage (planned v1.0 interface)

```bash
mcpprobe run --stdio "python -m my_server"
mcpprobe run --http https://localhost:8000/mcp --header "Authorization: Bearer $TOKEN"
mcpprobe run --stdio "node dist/index.js" --suites conf,fuzz,sec --seed 42 --report sarif -o probe.sarif
mcpprobe replay .mcpprobe/cases/FUZZ-003-7f2a.json
mcpprobe checks list
mcpprobe checks explain CONF-006
```

| Exit code | Meaning |
|---|---|
| 0 | All checks passed |
| 1 | At least one `error` finding |
| 2 | Probe could not run (server didn't start, unreachable) |
| 3 | Config or invocation invalid |

## Try it on a real server today

`record` sends a file of JSON-RPC messages (one per line, sent byte for byte)
and saves every byte that crossed the wire. `replay` re-sends it to a fresh
server and reports any difference.

```bash
mcpprobe record --stdio "npx -y @modelcontextprotocol/server-everything" \
  --send examples/legacy-session.jsonl -o everything.json --timeout 60
mcpprobe replay everything.json --timeout 60
```

For HTTP servers, `record --http URL` adds the `MCP-Protocol-Version`,
`Mcp-Method` and `Mcp-Name` headers derived from each body (turn off with
`--no-derive-headers`) and follows a legacy `Mcp-Session-Id`. Secrets from
`--env` and sensitive `--header` values are masked in recordings; pass them
again to `replay`.

## Suites

| Suite | What it checks |
|---|---|
| `CONF` | Base protocol, versioning, message patterns, stdio and Streamable HTTP |
| `FUZZ` | Schema-driven fuzzing of every tool's `inputSchema` |
| `ROB` | Malformed JSON-RPC, oversized payloads, cancellation, concurrency |
| `SEC` | Origin validation, `$ref` fetching, secrets in errors, OAuth metadata |
| `EVAL` | Does a real model pick the right tool with valid arguments? (v1.1, opt-in) |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create .venv and install everything
uv run mcpprobe --help
uv run pytest
uv run ruff format . && uv run ruff check .
uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [checks/README.md](checks/README.md).

## Repository layout

```
src/mcpprobe/
  cli.py          Typer entry point
  transport/      raw stdio, HTTP and SSE wire client + byte recorder
  protocol/       generated message models per revision, era detection
  engine/         runner, scheduler, seed handling, catalog, liveness probe
  suites/         conf/, fuzz/, rob/, sec/, eval/
  report/         terminal, json, junit, sarif, html
checks/           one YAML per check
fixtures/servers/ failing and passing fixture server per check
action/           GitHub Action wrapper
docs/             docs site
```

## License

MIT
