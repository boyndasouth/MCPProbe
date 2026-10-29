# Contributing

## Setup

```bash
uv sync
uv run pytest
```

Before opening a PR, all of these must pass (CI runs them on Linux, macOS and
Windows with Python 3.11–3.13):

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## Rules of the codebase

- **Talk to servers through `transport/` only.** Never use an MCP SDK client
  against the server under test; SDKs hide exactly the mistakes we look for.
- **Determinism.** Nothing that lands in a report may contain timestamps,
  durations, unordered sets or unseeded randomness. Get randomness from
  `mcpprobe.engine.seed.rng(seed, "<component>", ...)`.
- **Every check cites the spec.** See [checks/README.md](checks/README.md).
- **No false positives.** A wrong check costs more trust than a missed bug.
  If the spec is ambiguous, ship the check as `warn`.
- **Public contracts.** Exit codes, check IDs and the report JSON schema are
  versioned; check IDs are never reused.

## Adding a check (target: under 30 minutes)

1. Read the current spec section on modelcontextprotocol.io.
2. Add `checks/<ID>.yaml`.
3. Add `fixtures/servers/<ID>/bad.py` and `good.py`.
4. Add a test that the check fails `bad.py` and passes `good.py`.
5. Add a line to `CHANGELOG.md`.
