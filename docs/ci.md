# Using MCPProbe in CI

## GitHub Action

```yaml
# .github/workflows/mcpprobe.yml
name: MCPProbe
on: [push, pull_request]

permissions:
  contents: read
  security-events: write   # to upload findings to code scanning

jobs:
  probe:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22 }
      - run: npm ci && npm run build
      - uses: boyndasouth/MCPProbe@v1
        with:
          stdio: node dist/index.js
          seed: "42"
```

The action runs every suite, then:

- writes the summary to the job summary,
- uploads SARIF to **code scanning**, so findings show up as alerts with a
  link to the spec section (needs `security-events: write`; on private
  repositories it also needs GitHub Advanced Security, and the upload is
  skipped with a warning without it),
- uploads `probe.json`, `probe.sarif`, `junit.xml`, `report.html` and the
  saved cases as an artifact,
- fails the job if the exit code is not 0.

| Input | Default | Meaning |
|---|---|---|
| `stdio` / `http` | | The server: a command, or the URL of a running server |
| `headers` | | HTTP headers, one `Name: value` per line; pass tokens from secrets |
| `env` | | Environment for a stdio server, one `KEY=VALUE` per line; masked in reports |
| `suites` | all | e.g. `conf,fuzz` |
| `seed` | random | Pin it so a rerun reproduces a failure |
| `config` | `./mcpprobe.toml` | Config file |
| `baseline` | | A committed JSON report: only new findings fail the job |
| `args` | | Any other `mcpprobe run` options |
| `upload-sarif` | `true` | Upload to code scanning |
| `sarif-category` | `mcpprobe` | Distinguishes several servers in one repository |
| `sarif-path` | entry file | The repository file alerts point at |

Outputs: `exit-code` and `sarif` (the report's path).

For an HTTP server that needs a token, keep the token in a repository secret
and pass it from your workflow:

```yaml
      - uses: boyndasouth/MCPProbe@v1
        with:
          http: http://127.0.0.1:8000/mcp
          headers: |
            Authorization: Bearer ${{ secrets.MCP_TOKEN }}
```

Header values that look sensitive are masked in every report.

The action installs MCPProbe from its own checkout, so `@v1` always runs v1's
code. For an HTTP server, start it in an earlier step (in the background) and
wait until it answers before the action runs.

## Adopting it on an existing server: baselines

A server that already has findings can adopt MCPProbe without fixing all of
them first:

```bash
mcpprobe run --stdio "node dist/index.js" --seed 42 --report json -o mcpprobe-baseline.json
git add mcpprobe-baseline.json
```

```yaml
      - uses: boyndasouth/MCPProbe@v1
        with:
          stdio: node dist/index.js
          seed: "42"
          baseline: mcpprobe-baseline.json
```

Findings in the baseline are reported as *known* and do not fail the job;
anything new does. Findings are matched by case id (`FUZZ-001/echo`), which is
stable across runs. Delete lines from the baseline (or regenerate it) as you
fix things.

## Other CI systems

Install with `pipx install mcpprobe-kit` and write the reports you need in one
run:

```bash
mcpprobe run --stdio "node dist/index.js" --seed 42 \
  --report junit -o junit.xml --report sarif -o probe.sarif --report html -o report.html
```

JUnit works with GitLab (`artifacts:reports:junit`), Jenkins, Azure Pipelines
and CircleCI test tabs: findings that fail the run are failures; warnings and
notes pass, with the finding in the test output, so the test tab agrees with
the exit code.
