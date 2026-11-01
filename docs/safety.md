# Calling your tools safely

CONF-008 to 011, the FUZZ suite, ROB-004, SEC-002 and SEC-007 call real tools.
FUZZ calls each one `--examples` times with unusual input. MCPProbe:

- calls a tool only if it is marked `readOnlyHint: true` or
  `destructiveHint: false`. MCP treats an unannotated tool as destructive,
  and so does MCPProbe; `--allow-destructive` lifts this rule;
- never calls a tool marked `openWorldHint: true` (it reaches other hosts)
  unless `--allow-open-world` is passed;
- generates URLs, emails and host names under the reserved `.invalid` domain
  and IP addresses from the documentation ranges, so a tool that follows them
  reaches nothing;
- never fetches anything from a host you did not name.

See exactly what a run would call, without calling anything:

```bash
mcpprobe run --stdio "node dist/index.js" --dry-run
```

Even so, fuzzing sends many inputs a real client never would. Run MCPProbe
against a development or staging copy of your server, never one connected to
production data or credentials.
