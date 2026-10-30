# MCPProbe

Tells an MCP server author, in one run, whether their server is spec-compliant,
robust to bad input, and usable by a real model.

> **Status: pre-alpha (M2 in progress).** 21 conformance checks over stdio and
> Streamable HTTP for the stateless **2026-07-28** revision *and* the legacy
> `initialize` era (2025-11-25, 2025-06-18). Each server is classified as
> modern, legacy, dual-era or unknown, and only the checks for the era(s) it
> speaks run.

Every check cites the spec section it enforces, every failure is saved as a
byte-for-byte replayable case, and the same seed always gives the same report.

## Install

Requires Python 3.11+. No compiler needed on any platform.

```bash
pipx install git+https://github.com/boyndasouth/MCPProbe@v0.1.0
# or, inside a project
uv add git+https://github.com/boyndasouth/MCPProbe@v0.1.0
```

## Usage

```bash
# A stdio server: give the command that starts it. Pin @latest with npx:
# a bare `npx -y pkg` silently reuses whatever version it cached first.
mcpprobe run --stdio "npx -y @modelcontextprotocol/server-everything@latest"
mcpprobe run --stdio "python -m my_server" --env API_KEY=...

# A Streamable HTTP server
mcpprobe run --http http://127.0.0.1:8000/mcp --header "Authorization: Bearer $TOKEN"

# Reproducible run with a machine-readable report
mcpprobe run --stdio "node dist/index.js" --seed 42 --report json -o probe.json

# Re-run one failure byte for byte against a fresh server
mcpprobe replay .mcpprobe/cases/CONF-020.json

# Browse the checks
mcpprobe checks list
mcpprobe checks explain CONF-006
```

Useful options for `run`:

| Option | Default | What it does |
|---|---|---|
| `--seed N` | random, printed | Controls case order; same seed, same report |
| `--report terminal\|json` | terminal | JSON is versioned and deterministic |
| `--timeout S` | 10 | Seconds allowed per case |
| `--startup-timeout S` | 30 | Seconds for the server's first answer (cold `npx`/`uvx`) |
| `--allow-destructive` | off | Also call tools not marked read-only or non-destructive |
| `--keep-going` | off | Run later suites even if conformance fails |
| `--cases-dir DIR` | `.mcpprobe/cases` | Where failing cases are saved |

| Exit code | Meaning |
|---|---|
| 0 | No `error` findings (warnings and notes allowed) |
| 1 | At least one `error` finding, crash or hang |
| 2 | Probe could not run (server didn't start, unreachable) |
| 3 | Config or invocation invalid |

## Eras

Before any check runs, MCPProbe asks `server/discover` (the 2026-07-28 way)
and, failing that, `initialize` (the legacy way), then probes a modern server
with `initialize` on a fresh process or session to spot dual-era servers.

| Era | Meaning | What runs |
|---|---|---|
| modern | Speaks 2026-07-28 only | Modern and both-era checks |
| legacy | Speaks an `initialize`-era revision only | Legacy and both-era checks, inside a legacy session |
| dual | Speaks both | Everything; both-era checks run twice, the legacy run suffixed `@legacy` |
| unknown | Answered neither way | Nothing |

## Checks

`error` checks enforce a MUST and fail the run; `warn` enforces a SHOULD or
flags a deprecated feature; `info` is advice and shows as a note.

| ID | Severity | Era | Check |
|---|---|---|---|
| CONF-001 | error | modern | server/discover exists and lists supported versions and capabilities |
| CONF-002 | error | modern | Requests missing required `_meta` fields are rejected with -32602 |
| CONF-003 | error | modern | Unsupported protocol version returns -32022 with `data.supported` |
| CONF-004 | error | modern | Every result carries `resultType` |
| CONF-005 | error | both | Request ids: null is rejected and responses echo the id exactly |
| CONF-006 | error | modern, HTTP | HTTP headers that disagree with the body are rejected with -32020 |
| CONF-007 | error | both, HTTP | Origin header is validated on Streamable HTTP |
| CONF-008 | info | both | Unknown tools get a JSON-RPC error, not a tool result |
| CONF-009 | error | modern | Undeclared client capabilities are never relied on |
| CONF-010 | error | modern | `input_required` results are well formed and only used where allowed |
| CONF-011 | error | both | `structuredContent` matches the tool's `outputSchema` |
| CONF-012 | warn | modern | `tools/list` returns tools in a deterministic order |
| CONF-013 | error | legacy | `initialize` handshake and version negotiation |
| CONF-014 | warn | modern | Initialize-era versions listed in `supportedVersions` really work |
| CONF-015 | error | both, stdio | stdout carries only newline-delimited JSON-RPC messages |
| CONF-016 | warn | modern | Deprecated logging capability is declared |
| CONF-017 | warn | modern | Results identify the server in `_meta` serverInfo |
| CONF-018 | error | modern | Cacheable results carry `ttlMs` and `cacheScope` |
| CONF-020 | error | modern | Protocol version is read from each request, not remembered |
| CONF-021 | warn | legacy | `initialize` answers an unsupported version with a supported one |
| CONF-022 | info | legacy | Server negotiates an outdated revision (2025-03-26 or older) |

`mcpprobe checks explain <ID>` prints what a check sends, what it expects,
and a link to the spec section.

## Calling your tools safely

CONF-008 to 011 call real tools. MCPProbe only calls tools marked
`readOnlyHint: true` or `destructiveHint: false` (MCP treats unannotated tools
as destructive), never tools marked `openWorldHint: true`, and only with the
smallest valid arguments built from each `inputSchema`. Any URL it generates
points at a reserved `.invalid` name. `--allow-destructive` lifts the first
rule; don't use it against a server connected to real data.

## Developer tools

`record` sends a file of JSON-RPC messages (one per line, byte for byte) and
saves everything that crossed the wire; `replay` re-sends it to a fresh server
and reports any difference.

```bash
mcpprobe record --stdio "npx -y @modelcontextprotocol/server-everything" \
  --send examples/legacy-session.jsonl -o everything.json --timeout 60
mcpprobe replay everything.json --timeout 60
```

Secrets passed with `--env` or sensitive `--header` values are masked in
every saved recording; pass them again to `replay`.

## Roadmap

| Milestone | Scope |
|---|---|
| M0 (done) | Raw wire client, recorder, replay, runner |
| M1 (done, v0.1.0) | Conformance checks for 2026-07-28, JSON report |
| M2 (in progress) | Legacy and dual-era detection and checks |
| M3 | Schema fuzzing (FUZZ) and robustness (ROB) suites |
| M4 | Security suite, SARIF/JUnit/HTML reports, GitHub Action, `--baseline` |
| M5 | Model-in-the-loop usability evaluation (EVAL, opt-in) |

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff format . && uv run ruff check . && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [checks/README.md](checks/README.md).

```
src/mcpprobe/
  cli.py          Typer entry point
  transport/      raw stdio, HTTP and SSE wire client + byte recorder
  protocol/       request building, era detection, schema instances
  engine/         runner, seeds, check catalog, record and replay
  suites/         conf/ (fuzz/, rob/, sec/, eval/ to come)
  report/         terminal, json
checks/           one YAML file per check
fixtures/servers/ reference server; --break <ID> violates one check
```

## License

MIT
