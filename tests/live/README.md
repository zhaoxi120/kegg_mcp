# Opt-in KEGG live compatibility check

This suite makes exactly four serialized requests: one `INFO`, one BRITE `GET`, one `LINK`, and
one `CONV`. It is not part of the default offline validation gate. Both the
`--run-kegg-live` option and an explicitly confirmed live access mode are required.

The client is limited to one request per second without burst and zero retries. A transport wrapper
enforces a four-request wire budget and opens its circuit after HTTP 429, a server error, or a
transport failure. The temporary SQLite cache is deleted after the session. Do not use pytest-xdist
or upload KEGG responses, cache files, or generated artifacts.

For an eligible academic user performing academic work:

```bash
export KEGG_MCP_ACCESS_MODE=public_academic
export KEGG_MCP_ACADEMIC_USE_CONFIRMED=true
uv run --frozen pytest tests/live/test_kegg_api_live.py \
  --run-kegg-live -m live_kegg -q
```

An authorized licensed endpoint may be used with the documented `licensed` access variables.
The four-request campaign and BRITE compact htext shape were checked against the official KEGG API
on 2026-07-16. The governing primary sources were the
[KEGG API page](https://www.kegg.jp/kegg/rest/) and
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html).

The GitHub Actions job runs only after an explicit manual dispatch on `main`, with
`run_kegg_live=true`, both offline validation jobs successful, and the repository or organization
variable `KEGG_MCP_LIVE_TESTS_ENABLED` exactly `true`. Configure access variables in a `kegg-live`
environment restricted to `main`; required reviewers are recommended. The workflow serializes
campaigns and never cancels an active live run.
