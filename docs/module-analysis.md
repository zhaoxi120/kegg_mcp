# KEGG MODULE syntax and evaluation contract

This document specifies the current parser and evaluator. It is an implementation contract, not an
official KEGG grammar or an official KEGG completeness calculation.

The analysis layer is pure and local. It accepts MODULE definitions that a caller has already
obtained and does not call KEGG, read the project cache, or execute an annotation tool. Retrieval,
licensing, rate limiting, and caching remain responsibilities of the KEGG client layer.

## Supported definition syntax

The project uses the following conservative grammar:

```text
definition       := required_block (TOP_LEVEL_SPACE required_block)*
required_block   := or_expression
or_expression    := and_expression ("," and_expression)*
and_expression   := factor (("+" | INNER_SPACE) factor | "-" factor)*
factor           := K_NUMBER | M_REFERENCE | "(" or_expression ")" | UNSUPPORTED
```

The precedence, from strongest to weakest, is:

1. parentheses;
2. optional attachment by a minus sign;
3. plus signs and semantic whitespace inside parentheses as AND;
4. commas as OR; and
5. semantic whitespace at depth zero as required top-level block separation.

Operators at one precedence level associate from left to right. An optional expression remains in
the AST and is evaluated for descriptive presence, but it is neutral for exact completion and the
required-block denominator.

ASCII space, horizontal tab, carriage return, and line feed are the only logical whitespace
characters. Whitespace next to `+`, `,`, `-`, `(`, or `)` is formatting rather than another logical
operator. This rule preserves flat-file line wrapping such as a newline immediately after `+`.
Whitespace between an operand-ending token and an operand-starting token is semantic: it separates
required blocks at depth zero and means AND inside parentheses. Other Unicode whitespace,
including non-breaking space and Unicode line separators, is retained as unsupported content so a
damaged definition cannot silently gain logical meaning. Identifiers split across physical lines
are not reconstructed.

Every input code point belongs to exactly one token. Canonical `K` and `M` identifiers contain the
letter followed by exactly five ASCII digits. A malformed or unknown delimiter-bounded chunk, such
as a reaction-module identifier or a token containing `/`, becomes one or more explicit
`unsupported` tokens. It is never shortened into a valid identifier and its characters are never
discarded.

Token and AST spans use zero-based, half-open Unicode code-point offsets. Line and column positions
are one-based and the end position is exclusive. The exact definition text, including line endings
and whitespace, remains the evaluation input; no workflow digest is required.

An unsupported token may coexist with a structurally valid AST, but it makes any reachable
evaluation unsafe. Missing operands, unmatched parentheses, or exceeded parser limits are syntax
errors and produce no AST.

The parser enforces UTF-8 byte, token, AST-node, and nesting limits. UTF-8 byte counting and
over-limit scanning are incremental. Tokenization stops at the configured token boundary and represents the
remaining exact text with one bounded token rather than constructing and discarding an unbounded
intermediate token list.

## Module references

An `M` number is a reference to another supplied MODULE definition. Resolution uses a bounded,
memoized depth-first traversal and records edges in source order. It never retrieves a missing
definition automatically.

The resolver records all reachable definitions and sanitized provenance. It returns an
explicit issue for:

- a definition that was not supplied;
- a supplied definition that did not parse;
- a cycle on the active traversal path;
- a reference-depth, module-count, reference-count, or total-AST-node limit; or
- any other reference that cannot be evaluated conservatively.

Shared acyclic references are parsed once. A cycle report contains the complete active path that
closed the cycle. Reference problems are not interpreted as biological absence.

The configured per-definition AST-node limit cannot exceed the graph-wide AST-node limit. Module,
reference, depth, and total-node limits are enforced before an additional definition is retained.
An invalid root definition is represented by its parser diagnostics; graph issues describe
M-number reference failures only.

## Exact completion

Evaluation uses three internal truth states: satisfied, unsatisfied, and not evaluable.

- A K-number leaf is satisfied when it is in the selected KO evidence set.
- An AND expression is satisfied only when every required child is satisfied.
- An OR expression is satisfied when at least one branch is satisfied. A satisfied branch can prove
  the OR even when another branch is not evaluable. With no satisfied branch, any not-evaluable
  branch makes the OR not evaluable.
- An optional child is neutral for its parent expression. Its own presence is reported separately.
- An M-number reference evaluates the referenced definition using the same KO set.
- Reachable unsupported content, unsafe reference content, or an exceeded evaluation bound is not
  evaluable.

The result statuses are:

| Status | Meaning |
| --- | --- |
| `complete` | Every required top-level block is evaluable and satisfied. |
| `incomplete` | Every required top-level block is evaluable and at least one is unsatisfied. |
| `partially_evaluable` | Some, but not all, required top-level blocks are evaluable. |
| `not_evaluable` | No required top-level block can be evaluated safely. |

`is_complete` is Boolean only for fully evaluable definitions. It is null for partial and
not-evaluable results.

## Project-defined block coverage

Block coverage is calculated only when every required top-level block is evaluable:

```text
completed required top-level blocks / required top-level blocks
```

The numerator, evaluable count, and full required-block count are always returned. Coverage is null
for partial and not-evaluable results, so an unsafe block cannot silently disappear from the
denominator. This metric is named
`exact_completion_and_top_level_block_coverage`. It is descriptive project output and must not
be called an official KEGG completeness percentage.

Optional components do not add required blocks and do not change the denominator. Their presence,
absence, partial presence, or inability to be evaluated is returned separately.

## Minimal missing alternatives

For an evaluable incomplete expression, the evaluator returns an inclusion-minimal antichain of KO
sets that would satisfy it:

- a missing K-number leaf contributes its singleton set;
- AND combines child alternatives with bounded Cartesian unions; and
- OR collects alternatives from its unsatisfied branches.

Supersets of another returned alternative are removed. Alternatives are sorted by set size and then
lexicographically. Enumeration, set size, and output count are bounded before a Cartesian product
can expand without limit. The combination budget applies to the complete accepted-KO evaluation,
not separately to each AST node. If early truncation prevents proof that an alternative is globally
minimal, the evaluator returns no alternative for that expression and labels it truncated rather
than making a false minimality claim. Not-evaluable expressions do not claim missing alternatives.

## Accepted-only evidence

The evaluator returns one `ModuleEvaluationResult` from the sorted unique accepted K numbers.
Rejected, unclassified, and invalid records never enter MODULE evaluation. Full-record evidence can
still retain those records for audit and reporting; the explicit large-file projection instead
retains aggregate counts and bounded diagnostics without record-level evidence.

Matched K-number lists, block-state previews, optional-component summaries, and missing
alternatives all have serialized limits. Truncation is represented by typed flags and warnings
rather than an omitted tail that appears complete.

A K-number assignment remains an annotation rather than experimental validation. A source-rejected
prediction is not evidence that a biological function is absent. For a metagenomic community, a
complete result describes pooled encoded potential and does not imply a complete pathway in one
organism, pathway activity, flux, or phenotype.

## Provenance and bounds

Each MODULE result serializes the exact root and reachable definition text, sanitized retrieval
provenance, dataset and decision-policy identity, parser and calculation versions, and the
effective limits. Service-level `AnalysisExecutionProvenance` separately records full-record or
projection retention. Raw KEGG payloads, cache paths, endpoints, and credentials are not emitted by
this layer.

Definitions obtained from a cache retain retrieval time, endpoint class and label, database release
when available, and stale status. A stale definition produces an explicit warning.

## Public Python surface

The stable interface is exported from `kegg_mcp.analysis`:

```python
from kegg_mcp.analysis import (
    ModuleDefinition,
    ModuleDefinitionCollection,
    evaluate_module,
    resolve_module_definitions,
)

definitions = ModuleDefinitionCollection(
    root_module_id="M00001",
    definitions=(
        ModuleDefinition.from_text(
            module_id="M00001",
            definition="(K00001,K00002+K00003) K00004",
        ),
    ),
)
graph = resolve_module_definitions(definitions)
result = evaluate_module(graph, annotation_dataset)
```

The evidence argument may be an immutable `AnnotationDataset` or a `KoAnalysisProjection`; both
expose the same sorted unique accepted-KO set to evaluation. Definition retrieval remains an
explicit caller step; none of these functions performs network or filesystem I/O.

## Primary sources

The syntax contract was checked against the following primary KEGG pages on 2026-07-14:

- [KEGG MODULE Database](https://www.kegg.jp/kegg/module.html), page updated 2026-06-01;
- [KEGG MODULE entry help](https://www.kegg.jp/kegg/document/help_bget_module.html);
- [KEGG database entry format](https://www.kegg.jp/kegg/docs/dbentry.html), page updated
  2026-06-12; and
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html), page updated 2026-06-17.

The examples used during parser review included M00009, M00011, M00051, M00611, M00615, and M00617.
KEGG does not publish a formal EBNF for MODULE definitions, so the grammar above is a project
contract derived conservatively from the documented operators and those entries.
