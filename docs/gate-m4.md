# M4 gate: MCPProbe on public MCP servers

Run on 2026-11-01 with MCPProbe at commit `1340bf8` (to be 1.0.0), seed 7, all
four suites (`--suites conf,fuzz,rob,sec --keep-going`). Every target was run
twice; **all 14 pairs of JSON reports are byte-identical**.

Gate criteria (spec, M4): 10+ public servers, every finding reviewed by hand,
no known false positives. **Result: passed.** One check was too strict for
dual-era servers (CONF-006, fixed with decision D39); every other finding was
confirmed against the wire exchange and the spec text.

## Targets

12 published servers, 14 targets. Versions are the latest at the time of the
run; the TypeScript and Python reference servers had been released three days
earlier with 2026-07-28 support.

| Target | Version | SDK | Era | Transport | Findings |
|---|---|---|---|---|---|
| everything | @modelcontextprotocol/server-everything 2.1.0 | TS | dual | stdio | CONF-016, CONF-020, ROB-001, ROB-002 |
| everything-http | same | TS | dual | HTTP | CONF-016, SEC-004 |
| filesystem ¹ | @modelcontextprotocol/server-filesystem 2.1.0 | TS | dual | stdio | CONF-020, ROB-001, ROB-002 |
| memory | @modelcontextprotocol/server-memory 2.0.1 | TS | dual | stdio | CONF-020, ROB-001, ROB-002 |
| seq-thinking | @modelcontextprotocol/server-sequential-thinking 2.0.0 | TS | dual | stdio | CONF-020, ROB-001, ROB-002 |
| pdf | @modelcontextprotocol/server-pdf 2.0.4 | TS | legacy | stdio | ROB-001, ROB-002 |
| time | mcp-server-time 2026.10.28 | Python | dual | stdio | FUZZ-001 ×1, ROB-001, ROB-002 |
| git ¹ | mcp-server-git 2026.10.28 | Python | dual | stdio | FUZZ-001 ×7, ROB-001, ROB-002 |
| fetch | mcp-server-fetch 2026.10.28 | Python | dual | stdio | ROB-001, ROB-002 (its one tool is open-world, not called) |
| excel | excel-mcp-server 2.2.6 | Python | dual | stdio | CONF-008, FUZZ-001 ×13, ROB-001, ROB-002 |
| excel-http | same | Python | dual | HTTP | CONF-008, FUZZ-003 ×1 (CONF-006 before D39) |
| motherduck ¹ | mcp-server-motherduck 1.1.0 | Python | dual | stdio | CONF-008, CONF-016, FUZZ-001 ×3, ROB-001, ROB-002 |
| calculator | mcp-server-calculator 0.2.1 | Python (older SDK) | legacy | stdio | CONF-008, ROB-001, ROB-002, ROB-003 |
| text-editor | mcp-text-editor 1.2.2 | Python (older SDK) | legacy | stdio | CONF-008, ROB-001, ROB-002, ROB-003 |

¹ Run with `--allow-destructive`, inside a scratch folder, a throwaway git
repository and an in-memory database respectively. CONF-008 is `info` and
never fails a run.

Not used: `mcp-server-sqlite` 2025.4.25 (archived) crashes at start-up with
the current Python SDK; `@upstash/context7-mcp` needs internet access, which
the test machine did not have.

## Findings, reviewed

Each verdict was made from the saved wire exchange (`mcpprobe replay` files),
not from the summary line.

| Check | Severity | Where | What the server did | Spec | Verdict |
|---|---|---|---|---|---|
| CONF-020 | error | TS SDK, all 4 dual-era stdio servers | On a fresh process, `1900-01-01` gets a correct -32022 with `supported` and `requested`. After one valid 2026-07-28 request, `1900-01-01` gets a normal result | "Servers **MUST NOT** rely on prior requests over the same connection to establish context (e.g., capabilities, protocol version …)" (basic#statelessness); unsupported version "**MUST**" get UnsupportedProtocolVersionError (versioning) | **True positive.** The TS SDK remembers the version per process |
| CONF-016 | warn | everything (both), motherduck | Declares the `logging` capability | Deprecated in 2026-07-28 | **True positive** |
| ROB-001 | warn | every stdio target (12) | Malformed JSON gets no reply at all | JSON-RPC 2.0 defines -32700 Parse error for invalid JSON | **True positive** (both SDKs, as found in M3) |
| ROB-002 | warn | every stdio target (12) | Envelopes without `jsonrpc`, with a wrong version, etc. get no reply | JSON-RPC 2.0 defines -32600 Invalid Request | **True positive** (both SDKs, as in M3) |
| ROB-003 | error | calculator, text-editor | Unknown method gets -32602 "Invalid request parameters"; the server's stderr shows it validated the method against its 28 known request types | JSON-RPC 2.0: -32601 is "Method not found"; every request must get a response | **True positive** in the older Python SDK; current Python SDK servers (time, git, fetch, excel) answer -32601 |
| FUZZ-001 | error | time, git, motherduck, excel (stdio only) | A request containing a lone surrogate (`\ud800`) or 512-deep nesting is never answered; the server keeps answering other requests | Every request must get a response (JSON-RPC 2.0; basic#messages) | **True positive.** The Python SDK's stdio reader drops the whole message, even when the bad value is in an unknown argument; excel over HTTP answers the same inputs |
| FUZZ-003 | warn | excel-http, `list_workbooks` | `{"recursive": 0}` (integer for a boolean) is accepted | Invalid input should be rejected with -32602 or `isError` | **True positive** (Pydantic lax coercion) |
| SEC-004 | warn | everything-http | Port also reachable on the machine's network address | "When running locally, servers **SHOULD** bind only to localhost" (streamable-http#security) | **True positive** (as in M3) |
| CONF-008 | info | calculator, text-editor, excel, motherduck | Unknown tool gets a tool result with `isError: true` | The spec lists unknown tools as protocol errors | **True positive**, reported as a note only |
| CONF-006 | error | excel-http (before D39) | Header `MCP-Protocol-Version: 2025-11-25` with a 2026-07-28 body is refused with HTTP 400 "Missing session ID" (the server routed it as a legacy request) instead of 400 + -32020 | Mismatch "**MUST**" get -32020; the spec does not say which era a dual-era server picks when header and body disagree | **Too strict for dual-era servers.** Fixed: D39 (approved) |

## MCPProbe changes made during the gate

| Change | Why |
|---|---|
| CONF-003 and CONF-020 start a fresh process and test the first request (D38) | CONF-003 blamed version checking for what is remembered state; each bug is now reported once, by the check that names it |
| CONF-006: dual-era servers pass the version case with any HTTP 400 (D39) | See above |
| A server that exits at start-up is reported as "exited with code N at start-up" | It was reported as "did not answer within 5s" (seen with mcp-server-sqlite) |
| `--total-timeout`: a case cut short by the deadline is "out of time", never a timeout | Windows fires timers up to one clock tick early (seen in CI) |

## Open item (not a false positive)

One Python SDK bug (stdio drops a message with deep nesting or a lone
surrogate) is reported once **per tool** by FUZZ-001: 13 findings on excel,
7 on git. Each one is true, but they share a root cause. The same situation
led to D19, which moved non-object arguments to a once-per-server ROB check.
A once-per-server ROB check for these inputs, with FUZZ no longer using them,
would report it once. This needs a decision.

## Reproduce

```bash
mcpprobe run --stdio "npx -y @modelcontextprotocol/server-everything@2.1.0" \
  --seed 7 --keep-going --report json -o everything.json
mcpprobe replay .mcpprobe/cases/CONF-020.json
```
