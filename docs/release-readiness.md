# Release Readiness

Use this checklist for the exact merged commit proposed for a GitHub release. This document is the
owner of the current distribution-version matrix, release status, and publication gates. Source
tests alone do not authorize KEGG access or redistribution of KEGG content.

Current status: validate the exact merged commit, real suite installation, and new-task Codex
discovery before creating the next tag. Record the final evidence in the release notes.

## Distribution boundary

| Distribution | Source version | Contract |
| --- | --- | --- |
| `kegg-mcp` | `0.8.0` | Core query, selected-reference/input handoff, and KO-analysis server; `RenderInput` producer |
| `deepkoala-mcp` | `0.4.0` | Optional controlled detailed-CSV handoff |
| `kegg-render-mcp` | `0.3.2` | Optional renderer requiring `kegg-mcp>=0.5,<0.9` |

All distributions support Linux with Python 3.11.x. They remain independently packaged, locked,
installed, and executed as separate stdio processes. The suite installer provisions them together
and generates one local Codex plugin; that plugin is not a fourth distribution.

The core Python wheel does not install either companion or any repository-scoped Skill. The suite
installer is the supported Codex installation path. Other MCP clients register independently
installed stdio servers manually.

The core produces `render_input.json` version 3 and preserves
`AnalysisExecutionProvenance` version 3 in output-bundle schema version 3. The renderer consumes
that authoritative handoff without normalizing evidence or recomputing analysis.

## Release identity

Record these values in the release notes:

- [ ] exact commit and unused tag;
- [ ] operating system and Python 3.11.x version;
- [ ] versions of all wheels and source distributions;
- [ ] `uv`, Git, and Codex CLI versions used for suite validation;
- [ ] generated plugin and runtime identities, with private paths redacted;
- [ ] final pull-request or exact-commit workflow result;
- [ ] KEGG access and data-rights reviewer; and
- [ ] security and privacy reviewer.

Never publish credentials, licensed endpoints, usernames, local paths, KEGG payloads, private
biological data, cache databases, or retained results.

## Automated validation

Run the full core profile from the exact candidate:

```bash
uv sync --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
```

Run the governed pull-request compatibility campaign exactly as defined in
[the live-test guide](../tests/live/README.md). If a live campaign is not authorized, record it as
not run; never substitute unauthorized access.

Validate the companions independently:

```bash
cd companions/deepkoala-mcp
uv sync --locked
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
uv build --no-sources

cd ../kegg-render-mcp
uv sync --locked --all-groups
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
uv build --no-sources
```

Renderer tests use generated synthetic assets and make no live KEGG requests.

Run `companions/kegg-render-mcp/tests/test_synthetic_pipeline.py` in the exact candidate. It carries
a FASTA-derived companion handoff through core high-level analysis and into renderer output across
three independent MCP sessions without manually rebuilding an intermediate contract. This verifies
composition of the three MCP boundaries without real inference, network access, or KEGG assets.

## Suite installation evidence

Validate `scripts/install-suite.py` independently from the three Python distributions:

- [ ] use reviewed source and an owner-only, non-symlink deployment TOML;
- [ ] exercise invalid configuration, unsafe ancestry, relative/missing paths, overlapping roots,
      registration conflicts, interrupted publication, rollback, and preservation of unrelated
      state;
- [ ] create three distinct frozen runtimes from the checked-in lockfiles;
- [ ] prove default `uv` operation is offline;
- [ ] confirm that `--allow-locked-dependency-downloads` is limited to locked dependencies and
      declared build requirements;
- [ ] confirm that the separate `--allow-deepkoala-install` path alone may clone the official
      repository and install its upstream requirements;
- [ ] confirm the bundled `202502` resources are selected and reported by default;
- [ ] confirm multi-domain capability defaults off, requires explicit deployment resources, and is
      enabled per request only after status reports it ready;
- [ ] confirm neither the default nor opt-in path downloads HMMER or KOfam profiles;
- [ ] when publishing multi-domain support, use operator-provided HMMER, KOfam profiles, and a small
      private FASTA for one real `multi=true` run; verify model and multi provenance without adding
      inputs, profiles, or results to the repository or release artifacts;
- [ ] verify generated launchers use validated absolute direct commands without a shell; and
- [ ] verify rollback removes only state proven to belong to the failed transaction.

No suite path may acquire Python, `uv`, Codex, later model weights, KOfam profiles, KEGG data, or
KEGG assets. Private runtime configuration must remain outside the generated plugin cache.

### Real Codex smoke

A successful installer exit or simulated Codex response is insufficient. From the exact candidate:

1. install into a fresh private suite root;
2. verify the generated plugin through the supported Codex app or CLI;
3. start a new Codex task outside the source checkout;
4. confirm discovery of all three Skills and all three MCP servers;
5. call the three status tools without live KEGG access or DeepKOALA inference, requiring the
   DeepKOALA structured status to return `route_state="local_ready"`;
6. keep that task loaded, open a second new task outside the checkout, and repeat discovery and all
   three status calls;
7. return to the first task and confirm that its three status tools remain usable; and
8. record versions, failures, and redacted evidence in the release notes.

The installation task is expected to retain its pre-install tool snapshot. Verify that the installer
reports `new_task_required=true`, `current_task_reload_supported=false`, and
`repeat_installation_required=false`; never treat missing newly installed tools in that same task as
a reason to reinstall. Only the required concurrent fresh-task smoke is discovery evidence.

Discovery, registration, rollback, or validator failures block release support for the suite path.

## Build and installed-artifact audit

Build every included distribution from the exact merged commit and inspect wheel and source
archives. Each archive must include its MIT license and exclude:

- SQLite databases, KEGG responses, KGML, pathway images, and rendered derivatives;
- DeepKOALA weights, KOfam profiles, external model code, and annotation databases;
- secrets, absolute local paths, private fixtures, biological inputs, and results;
- bytecode, virtual environments, caches, repository metadata, and build output; and
- another distribution's implementation or any repository-scoped Skill.

Reproduce the generated plugin from reviewed source rather than attaching a second assembled Skill
bundle. Audit its manifest and MCP configuration for exact absolute launchers, version binding, and
absence of secrets or private configuration.

Clean-install each wheel outside the checkout. Verify import and version, then exercise applicable
stdio startup, tool/resource discovery, schema-conforming output, clean stdout, redacted status,
scoped deletion, and safe bundle/export behavior.

## Rights and data gates

- [ ] Public KEGG REST access is shown only for eligible academic users performing academic work.
- [ ] Licensed mode requires explicit confirmation and an authorized HTTPS endpoint.
- [ ] Core and Renderer share a deployment-wide no-burst rate no greater than three requests per
      second, and GET requests contain at most ten entries.
- [ ] No KEGG payload, source asset, cache, or bulk export is tracked, packaged, or uploaded.
- [ ] Distribution of a KEGG-derived image has a separate rights review.
- [ ] External facts that affect contracts cite a current primary source and retrieval date.

The MIT license covers project source only. This checklist is operational guidance, not legal
advice.

## Security and scientific gates

- [ ] All servers remain local stdio processes and never use `shell=True`.
- [ ] Inputs, identifiers, resources, retained bytes, outputs, and summaries remain bounded.
- [ ] Allowed-root paths reject traversal, unsafe ancestry, replacement races, and symlink escape.
- [ ] Outputs never replace existing entries and publish their manifest last.
- [ ] Result IDs remain opaque, scoped, expiring, and safely indistinguishable when unavailable.
- [ ] Status, logs, and errors redact credentials, endpoints, environment values, and local paths.
- [ ] Offline Core and Renderer paths perform no HTTP request or cache write.
- [ ] Renderer XML, images, SVG, and resources remain static and free of active external content.
- [ ] The vulnerability-reporting route in `SECURITY.md` matches repository visibility.
- [ ] Raw evidence, ambiguity, multiple assignments, and provenance remain available.
- [ ] Core advertises exactly eighteen tools with self-contained schemas, including deterministic
      card/citation projection, local current-scope reference comparison, selected-reference
      export, and statistics-free handoff preparation.
- [ ] Entry cards retain the complete parsed GET detail, preserve unrecognized fields, and create
      no additional KEGG request; reference comparison accepts only current-schema snapshots for
      the same requested entries and makes no network request.
- [ ] Citation projection returns only PubMed identifiers explicitly present in KEGG flat-file
      `REFERENCE` fields, retains bounded provenance, rejects BRITE, and neither retrieves nor
      summarizes papers or upgrades a citation into mechanism or validation evidence.
- [ ] Selected-reference bundles contain only the requested bounded card snapshot and optional
      BRITE result. They do not export cache payloads or mirror KEGG; their manifest excludes result
      IDs, request keys, endpoint values, credentials, and local paths while recording hashes,
      parser/schema, selection, retrieval/cache/release, MIME, and size metadata.
- [ ] Enrichment handoff requires a non-empty explicit universe and foreground subset, preserves
      namespace, ambiguity, organism mismatch, unmapped outcomes, denominators, and provenance, and
      contains no statistical test, p-value, FDR, GSEA score, significance, activity, presence, or
      absence result.
- [ ] KEGG Mapper and Syntax handoffs only validate and serialize local inputs. They do not issue a
      KEGG request, upload, start a browser, execute an external tool, or parse a downstream result;
      Syntax KO Sequence order is explicitly caller supplied.
- [ ] All selected-reference and input-handoff files are bounded, owner-only, non-overwriting,
      transactionally rolled back on failure, and committed by a manifest installed last beneath
      an allowed root; tabular cells protect formula-like caller or endpoint text.
- [ ] Substance resolution distinguishes PubChem SID from CID and preserves all ChEBI/PubChem
      crosswalk candidates without chemical-identification claims.
- [ ] KO/pathway-to-gene LINK requires one matching organism scope; unbounded KO-to-all-genes,
      selected-entry RCLASS relations, and RMODULE remain unavailable.
- [ ] Taxonomy rank and candidate-materialization behavior is bounded and does not present
      identity-only candidates as fully retrieved GENOME records.
- [ ] An audit stopped by request, relationship-row, or response-byte limits preserves the complete
      local evidence audit, discards incomplete-target rows, and reports no partial-target yield.
- [ ] KEGG-returned text is preserved as untrusted database data and is never treated as an
      instruction to the LLM, MCP client, parser, or service.
- [ ] Only accepted plus policy-defined uncertain evidence may enter lenient analysis.
- [ ] Exact MODULE completion and block coverage remain separate.
- [ ] Unsupported MODULE syntax is preserved; a required block whose truth cannot be established
      safely because of it is not evaluable and retains a reason. The aggregate is
      `partially_evaluable` when another required block remains evaluable and `not_evaluable` when
      none can be evaluated safely.
- [ ] With no explicit targets or selection, the high-level service independently selects up to
      five MODULEs and up to five canonical KO reference pathways.
- [ ] Pathway results retain reference type, numerator, denominator, and retrieval provenance.
- [ ] Reports make no pathway-presence, activity, flux, phenotype, validation, or statistical claim
      from KO coverage.
- [ ] Skills orchestrate only their declared MCP and do not duplicate server logic.
- [ ] Every focused route in the
      [Codex Skill release review matrix](skill-evaluation.md#release-review-matrix) passes against
      this exact candidate, with the required evidence recorded.

## Publication

After every applicable gate passes:

1. reproduce the suite installation and record its gated evidence;
2. confirm the tag does not exist;
3. create and audit final wheel and source archives in clean directories;
4. create an annotated tag on the merged commit;
5. publish concise release notes and only audited archives; and
6. verify the tag, assets, and generated source archive.

Any failed applicable gate blocks publication.
