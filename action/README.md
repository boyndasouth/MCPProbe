# mcpprobe-action

GitHub Action wrapper, planned for milestone M4: takes `server` and `suites`
inputs, uploads SARIF to code scanning, fails the job on `error` findings,
and supports `--baseline` so existing servers can adopt it incrementally.
