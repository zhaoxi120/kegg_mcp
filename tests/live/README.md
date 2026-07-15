# Default KEGG live compatibility campaign

This suite makes exactly 120 serialized requests: 30 each for `INFO`, `GET`, `LINK`, and `CONV`.
It is part of the default pytest suite and pull-request CI.
The campaign repeats the previously verified KO `INFO`, BRITE `GET`, KO-to-pathway `LINK`, and
selected gene-to-UniProt `CONV` cases; injected-transport tests cover broader parameter boundaries.

The client is limited to one request per second without burst and zero retries. A transport wrapper
enforces the 120-request wire budget and opens its circuit after any transport or non-200 response.
Every call bypasses the temporary cache, and the workflow stops after the first failed test. The
temporary SQLite cache is deleted after the session. Do not use pytest-xdist or upload KEGG
responses, cache files, or generated artifacts.

Run the default suite:

```bash
uv run --frozen pytest
```

An authorized licensed endpoint may be used with the documented `licensed` access variables.
The expanded 120-request campaign and BRITE compact htext shape were checked against the official
KEGG API on 2026-07-16 from the implementation working tree. The complete unconfigured default
suite passed 686 tests, including the campaign, in 127.04 seconds. Repeat the campaign against the
exact merged candidate before release. The governing primary sources were retrieved again on
2026-07-16:
[KEGG API page](https://www.kegg.jp/kegg/rest/) and
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html).

GitHub Actions runs the same campaign for pull requests, pushes to `main`, and manual workflow
dispatches. The workflow does not upload KEGG responses or cache files.
