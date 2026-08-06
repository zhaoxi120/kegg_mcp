# Opt-in KEGG live compatibility campaign

Pull-request CI permits at most 120 serialized requests through one shared circuit-breaking
transport. Local pytest skips this suite unless the operator explicitly enables it. The low-level
campaign executes each stable matrix case at most once; it does not spend the remaining budget by
repeating requests:

- `INFO`: KO, compound, genome, BRITE, glycan, reaction class, and drug;
- organism-pathway `LIST`: `hsa` and `eco`;
- `FIND`: KO, compound, glycan, drug, and reaction-class keywords; compound and drug formula,
  exact-mass, and molecular-weight modes;
- `GET`: one BRITE hierarchy plus representative KO, MODULE, pathway, reaction, enzyme, compound,
  glycan, gene, genome, drug, and reaction-class flat files; supported flat-file shapes also pass
  through the deterministic typed-card parser;
- `LINK`: the established KO, BRITE, gene, compound, and taxonomy cases plus organism-scoped
  KO/pathway-to-gene; module-to-KO/reaction/pathway; pathway-to-module/glycan;
  reaction-to-glycan; glycan-to-reaction/pathway; and drug-to-pathway directions; and
- `CONV`: the established selected gene directions plus ChEBI/PubChem SID-to-compound,
  PubChem SID-to-glycan/drug, and drug-to-PubChem SID directions.

The remaining default budget exercises scientist-shaped workflows through an MCP client session
and the real high-level services:

- mixed typed entry cards, retained snapshots, cache reuse, cached PubMed-reference projection,
  local snapshot comparison, and a durable selected-reference bundle;
- glycan and drug search plus the currently empty public reaction-class FIND response;
- ChEBI, PubChem SID-to-compound/glycan/drug, and direct compound/glycan/drug resolution;
- exact through phylum taxonomy resolution plus an organism pathway directory;
- every supported organism-scoped, MODULE, glycan, and drug relationship, including one depth-two
  trace;
- automatic BRITE mapping and complete JSON/TSV resource reads;
- completed, evidence-only, and request-limit-skipped annotation audits;
- all seven local-only KEGG Mapper/Syntax handoffs (Reconstruct, Search, Color, Join, MWsearch,
  KO composition, and caller-ordered KO sequence), all verified not to add a wire request; and
- direct and paginated retained-resource reconstruction.

Assertions cover response structure and request namespaces without freezing names, release labels,
or complete relationship counts. Injected-transport tests cover broader parameter and failure
boundaries. Aggregate row- and response-byte audit limits remain injected-test cases because
deliberately exhausting them against KEGG would create an inappropriate live workload.

The client is limited to one request per second without burst and zero retries. A transport wrapper
enforces the configured wire budget and opens its circuit after any transport or non-200 response.
Low-level compatibility calls explicitly bypass cache; high-level workflows use normal fresh-cache
semantics and verify cache reuse. The workflow stops after the first transport or non-200 failure.
The cache, result store, deployment-wide rate-limit state, and durable output bundles share one
owner-only temporary allowed root. The tests verify private file modes, reconstruct every retained
artifact in-session, verify reference and handoff manifests, clear the result scope, confirm that
explicit durable output bundles survive scope cleanup, and delete the complete temporary
deployment after the session. Do not use pytest-xdist or upload KEGG responses, cache files, or
generated artifacts.

Run the local non-live suite:

```bash
uv run --frozen pytest
```

Run the live campaign after confirming eligible access:

```bash
KEGG_MCP_RUN_LIVE_TESTS=true uv run --frozen pytest tests/live
```

For a separate installed-style stdio acceptance check, use:

```bash
KEGG_MCP_RUN_LIVE_STDIO_E2E=true \
  uv run --frozen pytest tests/live/test_stdio_scientist_live.py
```

That manual check performs two additional logical live operations through a real stdio subprocess,
then reuses the GET cache for a PubMed-reference projection and writes a durable reference bundle
plus all seven local-only Mapper/Syntax handoffs beneath an explicitly configured allowed root.
It verifies environment configuration, private state and output files, retained resource reads,
unchanged complete cache state across all local writes, normal-exit scope cleanup, and
durable-output survival without adding another logical live operation. It is intentionally
excluded from pull-request CI and from the shared at-most-120-request campaign budget.

The enabled default reserves a maximum of 20 requests per low-level operation class and runs the
complete high-level scientist workflow within the shared at-most-120-request ceiling. Set
`KEGG_MCP_LIVE_REQUESTS_PER_OPERATION` to an integer from 1 through 20 for an explicitly
authorized smaller low-level run. Values below 20 skip the complete high-level workflow so the
smaller transport ceiling remains enforceable.

An authorized licensed endpoint may be used with the documented `licensed` access variables.
The campaign, compact BRITE htext shape, typed-card source shapes, and selected relation directions
use official KEGG API behavior reviewed on 2026-07-30. On 2026-07-31, the public FIND endpoint
returned a well-formed empty response for the known `RC00002` identifier and its definition
fragments. The campaign therefore verifies RCLASS FIND parsing without promising positive keyword
discovery; use selected RCLASS GET when an identifier is already known. Unsupported selected
reaction-class relations and inconsistent `rmodule` selected endpoints remain outside the
allowlist. Repeat the campaign against the exact merged release commit. The governing primary
sources were retrieved on 2026-07-30:
[KEGG API page](https://www.kegg.jp/kegg/rest/) and
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html).

GitHub Actions runs the at-most-120-request campaign for pull requests and manual workflow
dispatches. A merge to `main` does not repeat the same validation. The workflow does not upload
KEGG responses or cache files.
