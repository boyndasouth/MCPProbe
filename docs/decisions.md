# Decisions and departures from the spec

The MCPProbe spec ("MCPProbe — MCP Conformance Fuzzer & Evaluator Spec",
Oct 29, 2026) is the source of truth. Where the code differs from it, the
difference and the reason are recorded here. When a later milestone starts,
read this file alongside the spec: an entry here overrides the spec text it
names.

How the sources rank when they disagree:

1. **The MCP specification (modelcontextprotocol.io)** decides what a server
   MUST or SHOULD do. Every check cites it.
2. **Behaviour of real servers** decides whether a check is precise enough
   to ship: a check that is correct but flags most servers for the same SDK
   habit is reshaped or downgraded.
3. **The MCPProbe spec** sets goals, priorities and scope. Its principles
   (determinism, cite the spec, "a false positive costs more trust than a
   missed bug") have held; some of its details were guesses made before any
   real server was run.

Each entry has a status:

- **Approved**: Shiwin chose it when asked.
- **Review**: a judgment call made during the build. It is reasonable, but
  nobody signed it off; confirm or reverse it.
- **Open**: in the spec but not built yet.

Last updated: 2026-10-31, during M4 (after batch 3: config file, dry run,
total timeout, GitHub Action, docs site, PyPI publishing; before the M4 gate).

---

## Packaging and release

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D1 | Package `mcpprobe` on PyPI ("Is the name free? Check before the first commit") | Distribution `mcpprobe-kit`; the command and import name stay `mcpprobe` | PyPI compares names with `-`, `_` and `.` removed and `o`→`0`, `l`/`i`→`1`, and refuses a match as "too similar". `mcpprobe` matches the unrelated `mcp-probe`, and `mcpprobe-cli` (the first choice) matches `mcp-probe-cli`, so both are refused. `mcpprobe-kit` was accepted on PyPI on 2026-10-31 | Approved |
| D2 | GitHub Action at `uses: <owner>/mcpprobe-action@v1` (a separate repo), but the repo layout shows an `action/` folder | The Action lives in this repo (`uses: boyndasouth/MCPProbe@v1`) | One repo, so the Action and the tool are always versioned together | Approved (M4 batch 3) |
| D3 | M4 ends in the v1.0 release; v1.0 success criteria include "adopted in CI by at least 3 public MCP server repos that aren't mine" | v1.0.0 is tagged at the end of M4 | Adoption can only happen after a release; it is tracked as a goal, not a release blocker | Approved |
| D4 | Reports use `sarif-om` for SARIF | SARIF is written as plain JSON; `sarif-om` was removed | `sarif-om` needs extra serializer packages and cannot guarantee key order (determinism). Output is validated against the official SARIF 2.1.0 schema in tests | Review |

## Configuration and CLI

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D5 | Config example: `fuzz_examples_per_tool = 200` | Default 50 (`--examples`) | 200 × 20 tools is 4,000 calls plus shrinking, which cannot meet the v1.0 target of "under 60 seconds for a 20-tool server" | Approved |
| D6 | "Destructive tools (`destructiveHint: true`) are skipped unless `--allow-destructive`" | A tool is called only if marked `readOnlyHint: true` or `destructiveHint: false`; unannotated tools are skipped too. `openWorldHint: true` tools need `--allow-open-world` | MCP's annotation defaults treat an unannotated tool as destructive and open-world. Calling open-world tools with random input would send traffic to hosts the user never named, which the spec forbids | Approved (open-world flag); Review (unannotated = destructive, in place since M1) |
| D7 | Concurrency "(default 8)" | `--concurrency N`, default 8 | The spec's wording implies it is configurable | Review |
| D8 | `--report sarif` with GitHub code scanning | SARIF alerts point at line 1 of a repo file: `--sarif-path`, else the server's entry file from the stdio command, else `README.md` | Code scanning only shows alerts that have a file location, and a server finding has none | Review |
| D34 | Config keys `[run] suites, seed, fuzz_examples_per_tool, timeout_ms, allow_destructive` | Also `startup_timeout_ms`, `total_timeout_ms`, `concurrency`, `allow_open_world`, `keep_going`, `cases_dir`, `baseline`, and `[server] headers`; `${VAR}` expansion in `[server]` | Every CLI option the Action may need has a config key; expansion keeps secrets out of the file | Review |
| D35 | "Per-request and total timeouts" | `--total-timeout` defaults to 1800 s; when it runs out, the rest of the cases are skipped and the exit code is 2 | A run cut short must not pass a CI gate | Review |
| D36 | (not in the spec) | The Action installs MCPProbe from its own checkout (`uv tool run --from <action path>`), not from PyPI | `@v1` then always runs v1's code, and the Action works before and without PyPI | Review |
| D37 | Docs site (no tool named) | MkDocs with Material, built with `--strict` and published to GitHub Pages | Material reaches end of life on 2027-05-05; its successor, Zensical, reads the same `mkdocs.yml`, so moving is a small change before then | Review |
| D38 | CONF-003: unsupported version returns -32022 (no transport or ordering given) | On stdio, CONF-003 and CONF-020 each start a fresh server process and send 1900-01-01 as its first request; CONF-020 then sends a valid request and 1900-01-01 again, and runs on stdio only | server-everything 2.1.0 refuses 1900-01-01 correctly on a fresh process but accepts it after one valid request. On the shared connection both checks failed, and CONF-003 blamed version checking for what is remembered state. Now each bug is reported once, by the check that names it. Costs two extra server starts per run | Review |
| D39 | CONF-006: a header that disagrees with the body gets HTTP 400 + -32020 | For dual-era servers, the MCP-Protocol-Version case passes with any HTTP 400; modern-only servers still need -32020 | The mismatched header names 2025-11-25, a legacy revision. excel-mcp-server 2.2.6 routes such a request to its legacy handling and refuses it for lacking a session. The spec does not say which era a dual-era server must pick when header and body disagree; the request is refused either way. A server that does not check headers at all still fails | Approved (2026-11-01) |
| D40 | FUZZ near-miss inputs include deep nesting and odd Unicode | FUZZ no longer generates 512-deep nesting or a lone surrogate; new ROB-008 (error) sends each once per server in a vendor `_meta` key and fails only if the request is dropped. FUZZ keeps 32-deep nesting and the other odd strings | At the M4 gate one Python SDK bug (its stdio reader drops such messages before any tool runs) was reported once per tool: 13 times on excel-mcp-server, 7 on mcp-server-git. Same reasoning as D19. ROB-008 also caught the bug in an older-SDK server that FUZZ had missed | Approved (2026-11-01) |
| D9 | `mcpprobe replay .mcpprobe/cases/FUZZ-003-7f2a.json` (hash in the name) | Cases are saved as `<case id>.json` (`FUZZ-003_echo.json`) | Names derived only from the case id keep runs byte-identical | Review |

## Conformance (CONF)

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D10 | CONF-008: tool failures return `isError: true`, not a protocol error (error) | CONF-008 (info): unknown tools get a JSON-RPC error, not a tool result | The section it would enforce has no MUST or SHOULD | Approved (M1) |
| D11 | CONF-012: deterministic `tools/list` order *and* `ttlMs`/`cacheScope` present (warn) | CONF-012 (warn) covers order; CONF-018 (error) covers caching hints | The current MCP text says servers MUST include caching hints, so that half is an error | Review |
| D12 | CONF-014: a dual-era server serves both eras on the same endpoint (error) | CONF-014 (warn): initialize-era versions listed in `supportedVersions` really work | Era detection already proves both eras are served (a dual-era result requires it); what remained testable is whether advertised versions work. Warn because the MCP spec does not say `supportedVersions` covers initialize-era versions | Review |
| D13 | CONF-016: deprecated roots, sampling *or* logging in use (warn) | CONF-016 covers the logging capability only | Roots and sampling are client features; a server cannot "use" them except through input requests | Review |
| D14 | The CONF table ends at CONF-016 | Added CONF-017 (serverInfo, warn), CONF-018 (caching hints), CONF-020 (statelessness), CONF-021 (version counter-offer, warn), CONF-022 (outdated revision, info). There is no CONF-019 | Each has a MUST or SHOULD behind it. CONF-019 was dropped after it turned out to rest on a wrong reading of the spec; IDs are never reused | Review |
| D15 | CONF-002: a request missing `_meta` fields returns -32602 | On dual-era servers any error is accepted | A dual-era server cannot tell which era a request without a protocol version belongs to | Review |

## Fuzzing (FUZZ)

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D16 | Lists fuzzing behaviour without check IDs | FUZZ-001 (error): no crash or unanswered request; FUZZ-002 (error): invalid input not met with -32603 or a non-JSON-RPC reply; FUZZ-003 (warn): invalid input not accepted; FUZZ-004 (warn): valid input not rejected with -32602 | One check per behaviour, judged from the same calls | Approved (FUZZ-003 as warn); Review (the rest) |
| D17 | "Invalid inputs return `isError: true` or -32602" | FUZZ-002 fails only on -32603 or a reply that is not JSON-RPC; any other specific error passes | Servers check capabilities or headers before arguments and legitimately answer -32021 or -32020; flagging those would be false positives | Review |
| D18 | Generation with hypothesis-jsonschema from JSON Schema 2020-12 | hypothesis-jsonschema supports drafts 4–7 only, so 2020-12 schemas are translated for the generator. Every value is still judged by a real 2020-12 validator | "The generator proposes, the validator decides": gaps cost coverage, never a wrong verdict | Review |
| D19 | Near-miss inputs include arguments that are not an object | Moved to ROB-004, one case per server | It breaks the request format, not the tool schema; per tool it flagged the same SDK bug on every tool (7–10 findings per server) | Review |
| D20 | Invalid inputs must never produce a stack trace | Stack traces are detected by SEC-002, from its own probes | One check per behaviour. Cost: a trace triggered only by one odd fuzz input can be missed | Review |
| D21 | "Shrink every failure to a minimal reproducing input" | Crashes and wrong answers are shrunk; hangs and dropped requests are not | Each shrink attempt on an unanswered request costs a full timeout | Review |
| D22 | A dual-era server "gets both suites" | FUZZ, ROB and SEC run once per server, in the modern era when it is spoken | Input handling does not depend on the era; running twice doubled the time | Review |
| D23 | (not in the spec) | Calls carry a `progressToken`; progress keeps a slow call alive up to 3× the timeout; "slow", "dropped" and "hung" are told apart | Without it, server-everything's long-running tool was reported as a hang | Review |

## Robustness (ROB)

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D24 | Lists robustness behaviour without IDs or severities | ROB-001 to ROB-007. ROB-001 (parse error), ROB-002 (invalid envelopes) and ROB-004 (non-object arguments) are warnings | JSON-RPC shows those answers in examples rather than MUSTs, and both official SDKs drop malformed lines silently. Crashes and hangs still fail the run | Review |
| D25 | (not in the spec) | ROB-002's four envelope variants are one case; the batch variant is skipped in 2025-03-26 sessions | One finding per server instead of four; 2025-03-26 allowed JSON-RPC batching | Review |

## Security (SEC)

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D26 | "HTTP server rejects missing or foreign Origin" | Missing Origin is not flagged; foreign Origin stays CONF-007 | MCP only requires rejecting a present, invalid Origin; non-browser clients send none | Approved |
| D27 | "Server does not auto-fetch network `$ref` URIs" | SEC-001 flags remote `$ref`s in the server's own tool schemas | Fetching cannot be observed: no MCP message sends a server a schema to validate | Review |
| D28 | "No stack traces, file paths or secret-looking strings" | SEC-002 (stack traces) and SEC-003 (secrets) are separate warnings; file paths count only inside stack traces | The filesystem and git servers echo paths by design. Secrets are never printed in full and are masked in saved cases | Review |
| D29 | Tool descriptions scanned for injection-style text (heuristic, warn) | SEC-006 (warn) | No MCP MUST or SHOULD covers it, which conflicts with the spec's own rule "no check without a MUST/SHOULD behind it"; shipped because the SEC list asks for it explicitly | Review |
| D30 | OAuth checks for servers with authorization | SEC-005 runs only when a token is passed with `--header`; metadata is fetched only from the server's own origin | Without credentials the run cannot get past the server; fetching other hosts would break the no-unrequested-traffic rule | Review |
| D31 | Tool list stable across calls | SEC-007 (error) checks the list across a tool call and a second connection; modern era only | Initialize-era sessions were stateful, so per-session differences were allowed there | Review |

## Reports

| # | The spec says | What was built | Why | Status |
|---|---|---|---|---|
| D32 | JUnit: "one test case per check" | One test case per case (`FUZZ-001/echo`); only findings that fail the run are `<failure>`s, warnings and notes are written to `<system-out>` | Per-case is more useful; the CI test tab then agrees with the exit code | Review |
| D33 | `--baseline probe.json` fails only on new findings | Matched by case id; known findings also do not stop later suites | Case ids are stable across runs; messages (e.g. shrunk inputs) are not | Review |

---

## Not built yet

| # | The spec says | Where it stands |
|---|---|---|
| O1 | Optional `mcpprobe.toml` | **Built in M4 batch 3** (see D34) |
| O2 | "Dry-run mode lists what would be called" (Safety) | **Built in M4 batch 3**: `--dry-run` |
| O3 | "Per-request and total timeouts" | **Built in M4 batch 3** (see D35) |
| O4 | Pydantic message models generated from the official JSON Schema per revision | Requests are built by hand in `protocol/` on purpose (the raw wire client must be able to send malformed messages); there are no generated models. Decide whether they are still wanted |
| O5 | GitHub Action, docs site, PyPI via trusted publishing, signed releases | **Built in M4 batch 3**; needs one-time setup on pypi.org and in the repository settings before the first release |
| O6 | Run on 10+ public servers, review every finding by hand, no known false positives | **Passed 2026-11-01**: 12 servers, 14 targets; see gate-m4.md |
| O7 | EVAL suite | M5 (v1.1) |


## Keeping this file current

Any change that departs from the spec adds a row here in the same commit,
marked Review until Shiwin confirms it. When the spec itself is revised to
match, the row moves to a short "Folded into the spec" list.
