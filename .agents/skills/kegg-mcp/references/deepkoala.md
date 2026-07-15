# DeepKOALA guidance

DeepKOALA is an optional annotator external to the core `kegg-mcp` server. Core version 0.1.0
imports its output but does not run it. This repository now contains a separately installed
`deepkoala-mcp` companion and runner process; it is implemented but has not received an independent
release sign-off. Execution remains outside the Skill and core server. The commands, defaults, and
fields below were checked against the official repository and GenomeNet pages on 2026-07-15.
Recheck them when a different DeepKOALA version is used.

## Select a model and retain detailed output

For expected complete proteins:

```bash
python3 -m deepkoala.cli -i proteins.fasta -o results.csv --model full --detail --device auto
```

For fragmented proteins, including many metagenomic gene predictions:

```bash
python3 -m deepkoala.cli -i fragments.fasta -o results.csv --model frag --detail --device auto
```

Use `--detail`. The simple output omits below-threshold candidate evidence. Preserve the exact
command, DeepKOALA version, model choice, model artifact or date, execution date, and relevant
resource versions. Do not infer a version that was not recorded.

Detailed output currently includes `name`, `predict_label`, `probability`, `threshold`, and
`annotate`. Multi-domain mode may also include `start` and `end`. Preserve top-k and repeated rows
as separate evidence records.

Treat `--multi` as an advanced option for likely multi-domain proteins. It additionally requires
HMMER and KOfam profiles; verify their installation, access terms, and versions separately. Do not
recommend it merely because a sequence is long.

## Device, weights, and execution defaults

Use `device=auto` by default. Before execution, tell the user that automatic selection may use an
available CUDA or MPS device rather than CPU, and retain the resolved device when the program
reports it. Do not start a GPU job merely because the Skill was triggered; execution belongs only
to the separately configured companion or to a command the user explicitly chooses to run.

Use the model resources bundled in the configured official DeepKOALA GitHub checkout by default.
Select `date=latest` only within the installed resources, then record the actual resolved model
date rather than leaving final provenance as `latest`. Before every run, display the configured
weight source and resolved version when available, provide the newer-weight location
<https://www.genome.jp/ftp/db/deepkoala/>, and state that weights will not be downloaded or replaced
silently.

The companion exposes these effective defaults unless the user requests a supported override:

- `model=full`, with explicit `frag` selection for fragmented proteins;
- detailed output enabled;
- `batch_size=32`;
- `num_workers=2`;
- `topk=1`; and
- `multi=false` for the initial companion contract.

The companion fixes `cpu_threads=2` by default and pins its child-process CPU thread controls to
that configured value. Its `max_concurrent_jobs=1` is a hard initial-contract limit, not a
batch-size setting. Job concurrency is the number of independent DeepKOALA processes allowed to
run at once; `batch_size` is the number of sequences processed per inference batch inside one
process. `device` still defaults to `auto`; select `device=cpu` explicitly when GPU use is not
authorized.

## Companion MCP boundary

The implemented companion exposes six tools:

- `get_deepkoala_runner_status` reports redacted structural readiness, effective defaults, hard
  limits, and scheduler counts; `prepare_deepkoala_job` remains the authoritative preflight for a
  selected model, date, device, and artifact set, but does not load the model or guarantee that
  inference will succeed;
- `prepare_deepkoala_job` validates and privately stages either inline FASTA or an allowed-root
  path, resolves device and installed execution artifacts, and returns a notice without inference;
- `submit_deepkoala_job` requires `acknowledged=true` plus the exact prepared `plan_id` and
  `notice_sha256` before starting or queueing the reviewed plan;
- `get_deepkoala_job` reports lifecycle state and, after success, the importer handoff;
- `cancel_deepkoala_job` cancels a queued job or terminates and reaps a running process group; and
- `delete_deepkoala_job` deletes a terminal job and its retained local artifacts.

Treat `diagnostics_truncated=true` as an explicit warning that the terminal diagnostic resource is
only a sanitized bounded tail. A delete retry is recognized only while its identifier remains in
the companion's bounded process-local tombstone window; do not promise unconditional idempotence.

The execution notice includes the FASTA digest and non-sequence summary, resolved device, model
date, installed interpreter/source/weight/config identities, effective settings, queue disposition,
weight source, no-download warning, and updated-weight URL. The companion revalidates those
identities and the staged FASTA at submission, immediately before launch, and again after a
successful process exit. `multi=true` is rejected, detailed output is mandatory, weights are never
downloaded or replaced, and the initial contract permits only one running job.

Output, provenance, and sanitized diagnostics use opaque, process-scoped
`deepkoala-job://jobs/{job_id}/{section}` resources. Direct content is limited to 64 KiB; larger
artifacts use binary-safe byte ranges of at most 1 MiB. The client must decode each range's
`content_base64`, assemble ranges in order, verify the declared total bytes and SHA-256 digest, and
pass the verified detailed CSV plus the returned source-provenance template to core
`normalize_ko_annotations` with
`input_format=deepkoala_detailed`. A companion URI is not a portable payload and must not be handed
to the core server for dereferencing.

The Skill only orchestrates discovered tools and explains the result; it never implements these
controls itself.

## Interpret source decisions

- Treat `annotate == "*"`, or a verified `probability >= threshold` source rule for that version,
  as source-accepted.
- A prediction below its source threshold is source-rejected with reason
  `below_source_threshold`, not automatically uncertain.
- Treat a row with no usable prediction as unclassified and a malformed K number as invalid.
- Do not interpret accepted as experimentally validated or rejected as functional absence.
- Do not compare probabilities across different models or model/database versions without an
  explicit compatibility basis.

Sources:

- <https://github.com/zhaoxi120/deepkoala>
- <https://www.genome.jp/tools/deepkoala/>
