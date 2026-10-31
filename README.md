# MCPProbe

Tells an MCP server author, in one run, whether their server is spec-compliant,
robust to bad input, and usable by a real model.

> **Status: v0.3.0, pre-alpha.** Conformance (CONF), schema fuzzing (FUZZ)
> and robustness (ROB) suites over stdio and Streamable HTTP, for the
> stateless **2026-07-28** revision *and* the legacy `initialize` era
> (2025-11-25, 2025-06-18). Each server is classified as modern, legacy,
> dual-era or unknown, and only the checks for the era(s) it speaks run.

Every check cites the spec section it enforces, every failure is saved as a
byte-for-byte replayable case, and the same seed always gives the same report.

## Install

Requires Python 3.11+. No compiler needed on any platform.

```bash
pipx install git+https://github.com/boyndasouth/MCPProbe@v0.3.0
# or, inside a project
uv add git+https://github.com/boyndasouth/MCPProbe@v0.3.0
```

From v1.0 it will be on PyPI as **`mcpprobe-cli`** (`pipx install mcpprobe-cli`);
the command stays `mcpprobe`. (PyPI treats "mcpprobe" as too similar to an
unrelated existing project, "mcp-probe".)

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

# Reports for CI: JUnit for test tabs, SARIF for GitHub code scanning, HTML to share
mcpprobe run --stdio "node dist/index.js" --report sarif -o probe.sarif
mcpprobe run --stdio "node dist/index.js" --report html -o probe.html

# Adopt in CI without fixing everything first: fail only on new findings
mcpprobe run --stdio "node dist/index.js" --report json -o probe-baseline.json  # once, commit it
mcpprobe run --stdio "node dist/index.js" --baseline probe-baseline.json

# Re-run one failure byte for byte against a fresh server
mcpprobe replay .mcpprobe/cases/CONF-020.json

# Browse the checks
mcpprobe checks list
mcpprobe checks explain CONF-006
```

Useful options for `run`:

| Option | Default | What it does |
|---|---|---|
| `--seed N` | random, printed | Controls case order and fuzzing; same seed, same report |
| `--suites LIST` | `conf,fuzz,rob,sec` | Which suites to run (`sec` arrives in M4) |
| `--examples N` | 50 | Generated argument sets per tool (FUZZ) |
| `--concurrency N` | 8 | Requests in flight at once (ROB-007) |
| `--report FORMAT` | terminal | `terminal`, `json`, `junit`, `sarif` or `html`; all deterministic |
| `-o FILE` | stdout | Where a non-terminal report goes (the summary then prints on stderr) |
| `--baseline FILE` | none | A previous JSON report: its findings are shown as known and don't fail the run |
| `--sarif-path PATH` | entry file | Repository file SARIF alerts point at (code scanning needs one) |
| `--timeout S` | 10 | Seconds allowed per case |
| `--startup-timeout S` | 30 | Seconds for the server's first answer (cold `npx`/`uvx`) |
| `--allow-destructive` | off | Also call tools not marked read-only or non-destructive |
| `--allow-open-world` | off | Also call tools marked `openWorldHint: true` |
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
| FUZZ-001 | error | both | Fuzzed tool arguments never crash the server or go unanswered |
| FUZZ-002 | error | both | Invalid tool arguments are rejected cleanly, not with an internal error |
| FUZZ-003 | warn | both | Invalid tool arguments are not accepted as a success |
| FUZZ-004 | warn | both | Schema-valid tool arguments are not rejected as invalid params |
| ROB-001 | warn | both | Malformed JSON gets a -32700 Parse error |
| ROB-002 | warn | both | Invalid JSON-RPC envelopes are rejected, not executed |
| ROB-003 | error | both | Unknown methods get -32601 Method not found |
| ROB-004 | warn | both | `tools/call` with non-object arguments is rejected without an internal error |
| ROB-005 | error | both | 1 MB and 10 MB requests are answered or rejected, never dropped |
| ROB-006 | error | both | Cancellation is handled gracefully |
| ROB-007 | error | both | Concurrent requests each get their own response |

FUZZ and ROB run once per server, in the modern era when the server speaks
it: they test how the server handles input, which does not depend on the era.
The liveness probe after every case turns a crash or hang into a finding,
whatever the check's severity.
| CONF-021 | warn | legacy | `initialize` answers an unsupported version with a supported one |
| CONF-022 | info | legacy | Server negotiates an outdated revision (2025-03-26 or older) |

`mcpprobe checks explain <ID>` prints what a check sends, what it expects,
and a link to the spec section.

## Fuzzing

The FUZZ suite calls each tool `--examples` times (default 50) with arguments
generated from its `inputSchema` by [Hypothesis](https://hypothesis.works/):
valid values, and near-misses with one thing wrong (a required field missing,
a wrong type, a value just past a bound, an extra property, null, deep
nesting, a 100,000-character string, odd Unicode). Each argument set is
classified as valid or invalid by validating it against the tool's own schema,
so a finding never rests on MCPProbe's guess. One case per tool,
`FUZZ-001/<tool>`, judges all four FUZZ checks from the same calls.

Every finding is shrunk to a minimal input (`{"message": ""}` rather than a
page of random Unicode) and saved as a recording of just that call on a fresh
server, so `mcpprobe replay` reproduces it in one step. Crashes are shrunk too,
restarting a stdio server between attempts; requests that are never answered
are not, since each attempt would cost a full timeout. Calls carry a
`progressToken`, and a tool that keeps reporting progress gets up to three
times `--timeout` before MCPProbe moves on (without a finding: slow is not
stuck).

Generation, shrinking and case order all derive from `--seed`, and shrinking
is bounded by a call budget rather than a clock, so the same seed gives the
same report on any machine.

Generation uses hypothesis-jsonschema, which speaks JSON Schema drafts 4 to 7;
2020-12 schemas (the MCP default) are translated for the generator, and
keywords with no draft-7 form (`unevaluatedProperties`) only cost coverage,
never a wrong verdict. Tools whose schemas the generator cannot handle
(recursive `$ref`, or a remote `$ref`, which MCPProbe never fetches) are
skipped with the reason.

## Calling your tools safely

CONF-008 to 011 and the FUZZ suite call real tools. MCPProbe only calls tools
marked `readOnlyHint: true` or `destructiveHint: false` (MCP treats unannotated
tools as destructive), and never tools marked `openWorldHint: true`. Any URL,
email or hostname it generates points at a reserved `.invalid` name, and IP
addresses come from the documentation ranges. `--allow-destructive` and
`--allow-open-world` lift those rules; don't use them against a server
connected to real data or credentials. Fuzzing sends many unusual inputs, so
test against a staging copy.

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
| M2 (done, v0.2.0) | Legacy and dual-era detection and checks |
| M3 (done, v0.3.0) | Schema fuzzing (FUZZ) and robustness (ROB) suites |
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
  suites/         conf/, fuzz/, rob/ (sec/, eval/ to come)
  report/         terminal, json
checks/           one YAML file per check
fixtures/servers/ reference server; --break <ID> violates one check
```

## License

MIT
