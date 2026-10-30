# Fixture servers

`reference.py` is a small MCP server written against the raw wire protocol
(standard library only), so it can break the protocol on purpose.

- Run with no flags, it passes every implemented check: the **passing**
  fixture.
- `--break <CHECK-ID>` makes it violate exactly that check: the **failing**
  fixture.

```bash
python fixtures/servers/reference.py                    # stdio, conformant
python fixtures/servers/reference.py --break CONF-004   # results lack resultType
python fixtures/servers/reference.py --http 0           # HTTP, prints its URL on stderr
```

`tests/test_conf.py` runs every check against both, on each transport the
check applies to. Adding a check means adding its violation to `BREAKS` in
`reference.py`; the test fails until you do.

One parameterised server instead of a `good.py`/`bad.py` pair per check keeps
each failing fixture to the few lines that differ, and guarantees the good and
bad servers are otherwise identical.
