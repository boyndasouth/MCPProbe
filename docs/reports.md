# Reports

| Format | For | Contents |
|---|---|---|
| `terminal` | Local runs | Summary, findings, a replay command per finding |
| `json` | Tooling, diffs, `--baseline` | Every result, schema-versioned (`"format": "mcpprobe.report", "version": 1`) |
| `junit` | CI test tabs | One test case per case; only findings that fail the run are failures |
| `sarif` | GitHub code scanning | SARIF 2.1.0, one rule per check with its spec link, stable fingerprints |
| `html` | Sharing | One self-contained page, light and dark |

Write several in one run by repeating `--report` with one `-o` each:

```bash
mcpprobe run --stdio "node dist/index.js" --report json -o probe.json --report html -o probe.html
```

When a report goes to a file (or to stdout), the summary is printed on stderr.

## Determinism

The same server, seed and MCPProbe version give byte-identical reports:
there are no timestamps or durations, results are sorted by case id, case
order and fuzz inputs come from the seed, and shrinking is bounded by a call
budget rather than a clock. A snapshot test pins a full report and CI checks
it on Linux, macOS and Windows with Python 3.11 to 3.13.

## Replaying a finding

Every finding is saved as a recording of exactly what crossed the wire.
`mcpprobe replay` starts a fresh server, sends the same bytes and reports any
difference:

```bash
mcpprobe replay .mcpprobe/cases/FUZZ-001_echo.json
```

Fuzz findings are recorded alone on a fresh server after shrinking, so the
file holds just the minimal call. Secrets (`--env` values, sensitive headers,
and anything SEC-003 found) are masked in saved recordings.
