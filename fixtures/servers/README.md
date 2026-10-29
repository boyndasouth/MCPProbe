# Fixture servers

Each check ships with two tiny servers in `fixtures/servers/<CHECK-ID>/`:

- `bad.py` violates the check and must make it fail
- `good.py` satisfies it and must make it pass

CI runs every check against both. Reference servers that pass the whole suite
live in `fixtures/servers/reference/`. Fixtures are written against the raw
wire protocol, not an MCP SDK, so they can break the protocol on purpose.
