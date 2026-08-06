# Annotation Evidence and Import Contracts

This document records the current annotation-import contract. It covers immutable
annotation evidence, exact K-number normalization, three inline import formats, versioned decision
policies, import diagnostics, and derived strict/lenient KO views. It does not cover KEGG access,
module or pathway analysis, reporting services, MCP transport, or the repository-scoped Codex
Skill.

## Public modules

Stable domain types and functions are exported from `kegg_mcp.domain`. Stable importer contracts
and functions are exported from `kegg_mcp.importers`. The root `kegg_mcp` package currently exports
only package metadata.

The importer entry points are:

```python
import_plain_ko(...)
import_generic_table(...)
import_deepkoala_detailed(...)
```

All low-level importers accept only inline `str` or `bytes` payloads. The service/MCP layer owns
allowed-root path resolution before passing bounded content to these functions.

## Input limits

Every importer requires an explicit immutable `ImportLimits` value:

```python
from kegg_mcp.importers import ImportLimits

limits = ImportLimits(
    max_bytes=1_000_000,
    max_rows=50_000,
    max_columns=128,
    max_field_length=10_000,
)
```

These numbers are an application choice, not package defaults. Keeping limits caller-selected
prevents the importer layer from duplicating deployment-owned service and MCP limits.
Limits are enforced for bytes, logical rows, emitted annotation records, table columns, individual
field lengths, and auxiliary metadata size. A DeepKOALA composite label can emit more than one
record, but the same `max_rows` value bounds both source rows and expanded records. Dataset and
source metadata each allow at most 128 immutable fields. Invalid Unicode
in payloads is returned as a structured input error rather than a codec exception; invalid metadata
is rejected by its Pydantic configuration contract. Byte, row, and column limits must fit in the
portable signed 32-bit parser range. Field length has a separate hard ceiling of 5,000,000
characters, aligned with the immutable retained-evidence string contract; callers should normally
select a substantially lower application limit.

## K-number normalization

`normalize_ko_id(raw)` accepts only:

- an exact uppercase K number matching `K[0-9]{5}`; or
- the same identifier with a case-insensitive `ko:` namespace prefix.

Surrounding whitespace is removed for the normalized value. Scalar raw input is retained unchanged
in the annotation record and row evidence. The DeepKOALA composite-label exception is described
below: each expanded record retains its canonical component in `raw_ko`, while row evidence retains
the complete source label unchanged. The normalizer does not extract identifiers from free text and
does not convert a lowercase `k` identifier to uppercase.

An invalid value remains an `AnnotationRecord` with `ko_id=None` and
`normalized_status="invalid"`; it is also reported by the import report and never enters a strict
or lenient KO set.

## Canonical evidence

The canonical Pydantic models use strict validation, forbid unknown fields, and are frozen. Nested
evidence is represented by tuples of frozen `EvidenceField` objects rather than mutable dictionaries
or lists. This makes raw logical cell values, record collections, provenance metadata, and import
diagnostics immutable after validation.

`EvidenceField` values use JSON scalar types. Numeric metadata values are finite and bounded to the
signed 64-bit range so runtime serialization and the published JSON Schema enforce the same limit;
imported table cells remain their original strings.

The primary models are:

- `SourceProvenance`
- `AnnotationRecord`
- `ImportReport`
- `AnnotationDataset`
- `KOEvidenceView`

`AnnotationDataset.sources` retains source provenance even when an input is empty or every logical
row is structurally skipped. Every emitted record source must also appear in this tuple.

Nullable provenance fields remain `None` when the source did not provide them. Importers never
infer a tool version, model name, model/database version, annotation date, organism, or domain
coordinate. Workflow digests are not part of source provenance. A caller may provide a controlled
absolute `input_path`; the MCP boundary validates it against deployment allowed roots before the
importer sees the content.

`SourceProvenance.input_uri` is a sanitized logical identifier and remains distinct from
`input_path`. It accepts a simple basename or the `inline`, `mcp`, `resource`, and `urn` schemes.
URI credentials, percent encoding, whitespace, control characters, query parameters, and fragments
are rejected. Hierarchical schemes require an authority, and URNs require a namespace-specific
identifier. `input_path` must be an absolute path without traversal components.

Every record preserves:

- the raw KO and raw decision;
- all logical source cells in their original column order;
- normalized status and machine-readable reason;
- score semantics and threshold rule when explicitly known;
- rank and one-based inclusive domain coordinates when valid; and
- immutable source and policy provenance.

Multiple records for the same sample and sequence are valid. Importers never use `sequence_id` as a
unique key and never collapse top-k, multi-label, or multi-domain assignments.

## Decision policies

Decision behavior is named and versioned. Changing behavior requires a new policy version and new
truth-table tests.

### `user_supplied_ko`

Used by the plain KO importer and available explicitly to the generic importer:

- a valid K number becomes `accepted` with reason `user_supplied_annotation`;
- an invalid K number becomes `invalid`.

This describes input handling. It does not claim experimental validation.

### `canonical_source_status`

Available to the generic table importer. It recognizes only the explicit source values
`accepted`, `uncertain`, `rejected`, and `unclassified` after trimming and case normalization.
Unknown or absent decisions become `unclassified`. The policy never compares a generic score with a
threshold.

This is the only built-in policy that can currently create `uncertain` evidence.

### `deepkoala_detailed`

Used only by the DeepKOALA detailed importer:

1. A malformed non-empty prediction is `invalid` even if an acceptance marker is present.
2. An empty prediction is `unclassified`.
3. A valid prediction with the exact, untrimmed `annotate == "*"` marker is `accepted`.
4. Any other non-empty marker is `unclassified` and reported as
   `UNRECOGNIZED_SOURCE_DECISION`; numeric values do not override it.
5. With an empty marker, a valid prediction with `probability >= threshold` is `accepted`.
6. With an empty marker, a valid prediction below the threshold is `rejected` with reason
   `below_source_threshold`.
7. Missing or malformed decision numbers produce `unclassified` unless the exact marker supplies
   an accepted source decision.

The policy never creates `uncertain` evidence. A source marker that disagrees with the numeric
comparison is preserved and reported as `SOURCE_DECISION_CONFLICT`.

## Plain KO import

```python
from kegg_mcp.domain import AnalysisUnit, EvidenceMode, build_ko_evidence_view, select_ko_ids
from kegg_mcp.importers import ImportLimits, import_plain_ko

limits = ImportLimits(
    max_bytes=10_000,
    max_rows=1_000,
    max_columns=4,
    max_field_length=100,
)
dataset = import_plain_ko(
    "K00001\nko:K00002\nBAD\n",
    limits=limits,
    analysis_unit=AnalysisUnit.ISOLATE_PROTEOME,
)
view = build_ko_evidence_view(dataset)
strict_kos = select_ko_ids(view, EvidenceMode.STRICT)
```

Blank lines are ignored. Every non-empty line
produces one record, including invalid and duplicate rows. Plain records use `sequence_id=None` and
default to `sample_id="sample-1"` only for this unnamed single-input workflow.

## Generic CSV and TSV import

Generic tables require both an explicit `TableDialect` and `GenericColumnMapping`. Column names are
matched exactly. The importer does not use delimiter sniffing, fuzzy header matching, or score-based
decision guesses.

```python
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS, ScoreType, ThresholdRule
from kegg_mcp.importers import GenericColumnMapping, TableDialect, import_generic_table

mapping = GenericColumnMapping(
    sequence_id="protein",
    ko_id="ko",
    raw_decision="decision",
    score="probability",
    score_type=ScoreType.PROBABILITY,
    threshold="threshold",
    threshold_rule=ThresholdRule.GTE,
    domain_start="start",
    domain_end="end",
)
dataset = import_generic_table(
    csv_text,
    dialect=TableDialect.CSV,
    mapping=mapping,
    policy=CANONICAL_SOURCE_STATUS,
    limits=limits,
)
```

`sequence_id` and `ko_id` mappings are required. Score columns require declared score semantics,
threshold columns require a declared threshold rule, and domain columns must be mapped as a pair.
One source column cannot supply two logical fields. Unmapped columns remain in raw row evidence.

Rows with malformed identifiers are retained as records. Structurally ragged rows or rows missing a
required sequence/sample identifier are retained under `ImportReport.unparsed_rows` and reported as
skipped. Missing mapped headers, duplicate headers, invalid CSV structure, and input-limit failures
are recoverable structured errors.

The low-level generic importer requires an explicit `mapping` in its Python signature and generated
schema. The service and MCP layer may perform bounded signature detection, but an ambiguous table
returns `AMBIGUOUS_COLUMN_MAPPING`; no layer normalizes it by guessing.

## DeepKOALA detailed import

`import_deepkoala_detailed` requires the exact documented columns:

```text
name,predict_label,probability,threshold,annotate
```

Optional `start` and `end` columns must occur together. All additional columns remain in raw
evidence. `score_type` is recorded as `probability`; a present threshold uses `threshold_rule="gte"`.
The importer accepts previously generated output only. It does not import DeepKOALA code, load a
model, inspect a GPU, execute an annotation command, or infer a missing model version.

An exact composite `predict_label` consisting only of two or more canonical K numbers joined by
`+` is expanded deterministically into one `AnnotationRecord` per component. Horizontal whitespace
is allowed around the complete label and each plus sign, and more than two components are supported.
Each component record retains the same source row evidence, score, threshold, source decision,
domain coordinates, source provenance, and normalized decision semantics. Its `raw_ko` is the
canonical component; the unchanged composite source string remains in the `predict_label` field of
its `RowEvidence`. Mixed text, empty components, commas, namespaced components, and lowercase
K-number components are not partially recovered: they remain one invalid record. No LLM or
heuristic extraction participates in this rule.

Deterministic expansion of every canonical component is required by the current importer contract;
the complete composite source string remains immutable row evidence.

`ImportReport.input_rows` continues to count source rows, while `emitted_records` counts expanded
records. Therefore one valid composite row can make `emitted_records` greater than `input_rows`.

The detailed-output contract was reviewed against official DeepKOALA documentation on 2026-07-14.
Release-candidate compatibility checks use an independently installed official DeepKOALA runtime
and this same importer boundary. Their environment and result belong in release evidence rather
than this long-lived interface contract.

## Duplicate and conflict reporting

No importer removes duplicate or conflicting evidence.

- The plain importer reports repeated normalized K numbers as duplicates.
- Table importers report exact repeated logical rows as duplicates.
- The generic importer reports a conflict only when different KOs or statuses occupy the same
  explicit rank/domain slot for the same sample and sequence. Unslotted generic top-k or multi-label
  records remain valid.
- The DeepKOALA importer uses an explicit domain as its assignment slot when coordinates are
  present. Without coordinates, the sample-scoped sequence is the assignment slot, so different
  assignments from different source rows to that sequence are reported as a conflict. Components
  expanded from the same source row are not conflicts. Different explicit domains remain valid.

The derived KO view de-duplicates only its status-specific KO tuples. The record indexes retain all
source record identifiers. Accepted, uncertain, and rejected KO tuples may overlap when different
source records support different statuses for the same KO.

For table inputs, `ImportReport.source_columns` preserves every original header in order, including
unmapped headers when the input has no data rows. Logical cells from emitted and skipped rows remain
available through record evidence or `unparsed_rows`. Parsers and schemas, rather than content
digests, determine whether imported content is usable.

## Strict and lenient evidence

`build_ko_evidence_view(dataset)` creates sorted, deterministic tuples and sample-scoped record
indexes without mutating the dataset.

- Strict mode is the set of accepted K numbers.
- Lenient mode is accepted plus policy-defined uncertain K numbers.
- Rejected, unclassified, and invalid records never enter lenient mode.

These are annotation-evidence views, not statements that a biological function is experimentally
present or absent.

## Errors and JSON Schema

Payload parsing and import-execution failures use `KeggMcpError` with an `ErrorDetail` containing a
stable code, repairable message, suggested action, and bounded safe details. Invalid Pydantic
configuration objects or direct model construction use `pydantic.ValidationError`. Raw input
payloads and unrestricted local paths are not included in errors.

Public Pydantic models expose validation and serialization JSON Schemas through
`model_json_schema()`. Canonical top-level models declare JSON Schema Draft 2020-12 and carry
versioned URN `$id` values; all public object contracts forbid additional properties.
JSON-compatible output is available through `model_dump(mode="json")` and `model_dump_json()`.
