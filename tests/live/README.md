# Opt-in KEGG Live Compatibility Tests

These tests are an explicitly authorized extended compatibility campaign. They are not part of the
default offline validation gate. They may run locally or in the separately guarded KEGG live CI
job. Merely configuring a live access mode does not enable them; the separate
`--run-kegg-live` option is also required.

The campaign contains 120 deterministic, non-sequential requests:

- 30 `INFO` requests, covering each of the ten allowlisted databases three times;
- 30 single-entry `GET` requests across KO, MODULE, PATHWAY, REACTION, ENZYME, COMPOUND, and BRITE;
- 30 single-source `LINK` requests, five for each supported relationship; and
- 30 single-source `CONV` requests, five for each supported conversion direction.

Cases are reproducibly shuffled across operations. Every call uses `refresh=True`, one identifier,
one HTTP batch, a temporary cache, one request per second with no burst, and zero retries. The
transport enforces a hard budget of 30 wire attempts per operation and 120 in total. It opens a
circuit after HTTP 429 or three consecutive transport/server failures. A complete run therefore
takes at least about two minutes. Do not use pytest-xdist or launch multiple campaigns in parallel.

The identifiers and conversion mappings were checked against the official KEGG API on 2026-07-15.
The governing primary sources were the [KEGG API page](https://www.kegg.jp/kegg/rest/) and the
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html). The public endpoint is restricted to
academic use by academic users and the official service permits no more than three calls per
second. Non-academic users must use an appropriately licensed endpoint. The test catalog stores no
KEGG response body, and the temporary SQLite cache is removed after the session.

For an eligible public-academic run:

```bash
module load pytorch
export UV_CACHE_DIR=/tmp/kegg-mcp-uv-cache
export KEGG_MCP_ACCESS_MODE=public_academic
export KEGG_MCP_ACADEMIC_USE_CONFIRMED=true
uv run --frozen pytest tests/live/test_kegg_api_live.py \
  --run-kegg-live -m live_kegg -vv
```

For an authorized licensed endpoint, use the documented `licensed` access variables instead. The
live suite reuses the production environment validation contract before it initializes a client or
opens a socket.

The production HTTPS transport deliberately ignores environment proxy variables. When a local
sandbox requires its configured proxy, add the explicit test-only option below:

```bash
uv run --frozen pytest tests/live/test_kegg_api_live.py \
  --run-kegg-live --kegg-live-use-env-proxy -m live_kegg -vv
```

Proxy mode keeps TLS validation, bounded GET-only responses, and redirect rejection, but it tests
the explicitly injected proxy path rather than the production default direct path.

To run exactly one 30-request operation subset, add one of `-k info`, `-k get`, `-k link`, or
`-k conv`. The unmarked catalog-contract test remains offline and validates the exact counts,
relationship balance, conversion directions, shuffled order, and one-request-per-case invariant.

## Guarded CI configuration

The `validate-kegg-live` job in `.github/workflows/ci.yml` runs only after the offline `validate`
job succeeds on a `main` push or a manual dispatch of `main`. It never runs for a pull request. The
job is skipped unless the repository or organization
Actions variable `KEGG_MCP_LIVE_TESTS_ENABLED` equals `true`. Keep this enablement variable outside
the `kegg-live` environment because GitHub evaluates the job condition before environment-scoped
variables are made available to the runner.

Create a GitHub Actions environment named `kegg-live` and restrict it to the `main` branch. Required
reviewers are recommended when the repository needs an approval before each campaign. Configure
one eligible access mode in that environment:

- For eligible public academic use, set `KEGG_MCP_ACCESS_MODE=public_academic` and
  `KEGG_MCP_ACADEMIC_USE_CONFIRMED=true` as environment variables.
- For licensed use, set `KEGG_MCP_ACCESS_MODE=licensed` and
  `KEGG_MCP_LICENSED_USE_CONFIRMED=true` as environment variables, and store the authorized
  `KEGG_MCP_LICENSED_ENDPOINT` as an environment secret.

The workflow uses a repository-wide `kegg-live-api` concurrency group. It never cancels an active
campaign and retains at most one newer pending campaign. It runs one pytest process without the
test-only proxy option, keeps the one-request-per-second and 120-attempt hard limits, and does not
upload its temporary cache or any KEGG response body.
