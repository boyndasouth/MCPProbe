# Configuration

Everything can be given on the command line. For anything you run more than
once, put it in `mcpprobe.toml` next to your server; `mcpprobe run` reads it
from the current directory (or `--config PATH`). Command-line options win over
the file, and the file wins over the defaults.

```toml
[server]
stdio = "python -m my_server"                    # or: http = "http://127.0.0.1:8000/mcp"
env = { API_KEY = "${API_KEY}" }                  # stdio servers
headers = { Authorization = "Bearer ${TOKEN}" }   # HTTP servers

[run]
suites = ["conf", "fuzz", "rob", "sec"]
seed = 42
fuzz_examples_per_tool = 50
timeout_ms = 10000          # per case
startup_timeout_ms = 30000  # the server's first answer (cold npx/uvx)
total_timeout_ms = 1800000  # the whole run; 0 means no limit
concurrency = 8             # ROB-007
allow_destructive = false
allow_open_world = false
keep_going = false
cases_dir = ".mcpprobe/cases"
baseline = "mcpprobe-baseline.json"

[checks]
disable = ["CONF-012"]
severity_overrides = { "SEC-002" = "info" }
```

!!! note "Windows paths"
    In a TOML `"..."` string a backslash starts an escape, so
    `"C:\Users\me\server.py"` is an error. Use single quotes, which keep
    backslashes as they are: `stdio = 'python C:\Users\me\server.py'`. If the
    command itself contains quotes, use three: `stdio = '''"C:\Program Files\node\node.exe" dist\index.js'''`.

`${NAME}` is replaced by the environment variable `NAME`, so secrets stay out
of the file; a missing variable is an error. Unknown keys and unknown check
ids are errors too (exit code 3), so a typo never silently does nothing. An
`[eval]` section is accepted and reserved for the tool-usability eval (v1.1).

## `mcpprobe run` options

| Option | Default | What it does |
|---|---|---|
| `--stdio CMD` / `--http URL` | | The server |
| `--header "Name: value"` | | HTTP header; repeatable |
| `--env KEY=VALUE` | | Environment for a stdio server; repeatable; masked in reports |
| `--config PATH` | `./mcpprobe.toml` | Config file |
| `--suites LIST` | `conf,fuzz,rob,sec` | Suites to run |
| `--seed N` | random, printed | Same seed, same report |
| `--report FORMAT` | `terminal` | `terminal`, `json`, `junit`, `sarif`, `html`; repeatable |
| `-o FILE` | stdout | One per non-terminal `--report`, in the same order |
| `--baseline FILE` | | Known findings do not fail the run |
| `--examples N` | 50 | Generated argument sets per tool (FUZZ) |
| `--concurrency N` | 8 | Requests in flight at once (ROB-007) |
| `--timeout S` | 10 | Seconds per case |
| `--startup-timeout S` | 30 | Seconds for the server's first answer |
| `--total-timeout S` | 1800 | Seconds for the whole run; when it runs out the rest is skipped and the exit code is 2 |
| `--allow-destructive` | off | Also call tools not marked read-only or non-destructive |
| `--allow-open-world` | off | Also call tools marked `openWorldHint: true` |
| `--dry-run` | off | List the tools and which would be called; call none |
| `--keep-going` | off | Run later suites even if conformance fails |
| `--cases-dir DIR` | `.mcpprobe/cases` | Where failing cases are saved |
| `--sarif-path PATH` | entry file | Repository file SARIF alerts point at |
