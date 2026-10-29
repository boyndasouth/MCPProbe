# Check catalog

One YAML file per check, named after its ID (`CONF-006.yaml`). The engine
loads this folder; adding a check never touches engine code. The schema is
enforced by `mcpprobe.engine.catalog.Check`.

```yaml
id: CONF-006                 # SUITE-NNN; IDs are never reused
title: Headers that disagree with the body are rejected
severity: error              # error (MUST) | warn (SHOULD, deprecated) | info
era: modern                  # modern | legacy | both | dual
spec_ref: https://modelcontextprotocol.io/specification/<revision>/<page>#<section>
transports: [http]           # optional; defaults to [stdio, http]
description: >
  What the check sends, what it expects back, and why.
```

## Adding a check

1. Look up the current text of the section on modelcontextprotocol.io. Don't
   write a check from memory of the protocol; `spec_ref` must link the exact
   section and anchor, and there must be a MUST or SHOULD behind it.
2. Add the YAML file here.
3. Add a failing and a passing fixture server under
   `fixtures/servers/<CHECK-ID>/`.
4. Add a test that runs the check against both fixtures.

If the spec text is ambiguous, ship the check as `warn` and say so in the
description.
