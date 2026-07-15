"""Serializable contracts for conservative KEGG MODULE parsing and evaluation."""

from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Self

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    DecisionPolicyReference,
    EvidenceMode,
    FrozenModel,
    KNumber,
    RecordIdentifier,
    validate_utf8_text,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggOperation, ResponseOrigin

MODULE_PARSER_NAME = "kegg_module_definition"
MODULE_PARSER_VERSION = "1"
MODULE_RESOLVER_VERSION = "1"
MODULE_CALCULATION_METHOD = "exact_completion_and_top_level_block_coverage"
MODULE_CALCULATION_VERSION = "1"

ModuleId = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]


def _utf8_size(parts: Iterable[str]) -> int:
    """Count UTF-8 bytes incrementally without allocating one unbounded byte string."""
    byte_count = 0
    for part in parts:
        for offset in range(0, len(part), 4_096):
            encoded = part[offset : offset + 4_096].encode("utf-8")
            byte_count += len(encoded)
    return byte_count


def _ast_node_count(ast: "ModuleDefinitionAst") -> int:
    """Count expression occurrences iteratively to avoid recursive validation limits."""
    count = 0
    pending = list(ast.required_blocks)
    while pending:
        expression = pending.pop()
        count += 1
        pending.extend(expression.children)
    return count


class ModuleTokenKind(StrEnum):
    """Token kinds emitted without discarding any definition text."""

    KO = "ko"
    MODULE_REFERENCE = "module_reference"
    PLUS = "plus"
    COMMA = "comma"
    MINUS = "minus"
    LEFT_PAREN = "left_parenthesis"
    RIGHT_PAREN = "right_parenthesis"
    WHITESPACE = "whitespace"
    UNSUPPORTED = "unsupported"


class ModuleOperatorKind(StrEnum):
    """Logical operator kinds retained in the AST."""

    PLUS = "plus"
    SPACE = "space"
    COMMA = "comma"
    MINUS = "minus"


class ModuleExpressionKind(StrEnum):
    """Expression kinds supported by the conservative MODULE AST."""

    KO = "ko"
    MODULE_REFERENCE = "module_reference"
    AND = "and"
    OR = "or"
    OPTIONAL = "optional"
    GROUP = "group"
    UNSUPPORTED = "unsupported"


class ModuleDiagnosticSeverity(StrEnum):
    """Whether a parser diagnostic invalidates the syntactic AST."""

    ERROR = "error"
    WARNING = "warning"


class ModuleDiagnosticCode(StrEnum):
    """Stable parser diagnostics for repairable MODULE definitions."""

    EMPTY_DEFINITION = "EMPTY_DEFINITION"
    DEFINITION_LIMIT_EXCEEDED = "DEFINITION_LIMIT_EXCEEDED"
    TOKEN_LIMIT_EXCEEDED = "TOKEN_LIMIT_EXCEEDED"
    AST_NODE_LIMIT_EXCEEDED = "AST_NODE_LIMIT_EXCEEDED"
    NESTING_LIMIT_EXCEEDED = "NESTING_LIMIT_EXCEEDED"
    UNSUPPORTED_TOKEN = "UNSUPPORTED_TOKEN"
    UNEXPECTED_TOKEN = "UNEXPECTED_TOKEN"
    MISSING_OPERAND = "MISSING_OPERAND"
    UNMATCHED_LEFT_PARENTHESIS = "UNMATCHED_LEFT_PARENTHESIS"
    UNMATCHED_RIGHT_PARENTHESIS = "UNMATCHED_RIGHT_PARENTHESIS"


class ModuleDefinitionOrigin(StrEnum):
    """Where one already-retrieved MODULE definition originated."""

    INLINE = "inline"
    KEGG_NETWORK = "kegg_network"
    KEGG_CACHE = "kegg_cache"


class ModuleReferenceIssueKind(StrEnum):
    """Reasons a referenced MODULE definition could not be evaluated."""

    UNRESOLVED = "unresolved"
    INVALID_DEFINITION = "invalid_definition"
    CYCLE = "cycle"
    DEPTH_LIMIT = "depth_limit"
    MODULE_LIMIT = "module_limit"
    REFERENCE_LIMIT = "reference_limit"
    TOTAL_NODE_LIMIT = "total_node_limit"


class ModuleEvaluationStatus(StrEnum):
    """Overall exact evaluation status."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    PARTIALLY_EVALUABLE = "partially_evaluable"
    NOT_EVALUABLE = "not_evaluable"


class ModuleBlockState(StrEnum):
    """State of one required top-level block."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    NOT_EVALUABLE = "not_evaluable"


class OptionalComponentState(StrEnum):
    """Descriptive presence of an optional expression."""

    PRESENT = "present"
    ABSENT = "absent"
    PARTIALLY_PRESENT = "partially_present"
    NOT_EVALUABLE = "not_evaluable"


class ModuleWarningCode(StrEnum):
    """Stable conservative evaluation warnings."""

    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    UNRESOLVED_REFERENCE = "UNRESOLVED_REFERENCE"
    REFERENCE_CYCLE = "REFERENCE_CYCLE"
    REFERENCE_LIMIT = "REFERENCE_LIMIT"
    PARTIAL_EVALUATION = "PARTIAL_EVALUATION"
    NO_REQUIRED_COMPONENT = "NO_REQUIRED_COMPONENT"
    MISSING_ALTERNATIVES_TRUNCATED = "MISSING_ALTERNATIVES_TRUNCATED"
    OUTPUT_PREVIEW_TRUNCATED = "OUTPUT_PREVIEW_TRUNCATED"
    UNCERTAIN_SUPPORT_TRUNCATED = "UNCERTAIN_SUPPORT_TRUNCATED"
    STALE_DEFINITION = "STALE_DEFINITION"


class SourceSpan(FrozenModel):
    """Half-open Unicode code-point span with one-based line and column positions."""

    start_offset: NonNegativeCount
    end_offset: NonNegativeCount
    start_line: PositiveCount
    start_column: PositiveCount
    end_line: PositiveCount
    end_column: PositiveCount

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_offset < self.start_offset:
            raise ValueError("span end_offset must not precede start_offset")
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("span end position must not precede start position")
        return self


class ModuleToken(FrozenModel):
    """One lossless lexical token from a MODULE definition."""

    kind: ModuleTokenKind
    lexeme: str = Field(min_length=1)
    span: SourceSpan

    @field_validator("lexeme")
    @classmethod
    def validate_lexeme(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="module token lexeme")

    @model_validator(mode="after")
    def validate_kind_and_span(self) -> Self:
        if self.span.end_offset - self.span.start_offset != len(self.lexeme):
            raise ValueError("token span length must match its lexeme")
        fixed_lexemes = {
            ModuleTokenKind.PLUS: "+",
            ModuleTokenKind.COMMA: ",",
            ModuleTokenKind.MINUS: "-",
            ModuleTokenKind.LEFT_PAREN: "(",
            ModuleTokenKind.RIGHT_PAREN: ")",
        }
        if self.kind in fixed_lexemes and self.lexeme != fixed_lexemes[self.kind]:
            raise ValueError("operator token kind is incompatible with its lexeme")
        if self.kind is ModuleTokenKind.KO and not (
            len(self.lexeme) == 6
            and self.lexeme.startswith("K")
            and self.lexeme[1:].isascii()
            and self.lexeme[1:].isdigit()
        ):
            raise ValueError("KO tokens require canonical K-number syntax")
        if self.kind is ModuleTokenKind.MODULE_REFERENCE and not (
            len(self.lexeme) == 6
            and self.lexeme.startswith("M")
            and self.lexeme[1:].isascii()
            and self.lexeme[1:].isdigit()
        ):
            raise ValueError("module-reference tokens require canonical M-number syntax")
        if self.kind is ModuleTokenKind.WHITESPACE and not self.lexeme.isspace():
            raise ValueError("whitespace tokens must contain only whitespace")
        return self


class ModuleOperatorOccurrence(FrozenModel):
    """One semantic operator occurrence and its exact source span."""

    kind: ModuleOperatorKind
    span: SourceSpan


class ModuleExpression(FrozenModel):
    """Recursive expression node that preserves grouping and operator occurrences."""

    kind: ModuleExpressionKind
    span: SourceSpan
    value: str | None = Field(default=None, min_length=1, max_length=1_000)
    children: tuple["ModuleExpression", ...] = ()
    operators: tuple[ModuleOperatorOccurrence, ...] = ()

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name="module expression value")

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        leaves = {
            ModuleExpressionKind.KO,
            ModuleExpressionKind.MODULE_REFERENCE,
            ModuleExpressionKind.UNSUPPORTED,
        }
        if self.kind in leaves:
            if self.value is None or self.children or self.operators:
                raise ValueError("leaf expressions require only a value")
            if self.kind is ModuleExpressionKind.KO and not (
                len(self.value) == 6
                and self.value.startswith("K")
                and self.value[1:].isascii()
                and self.value[1:].isdigit()
            ):
                raise ValueError("KO expressions require canonical K-number syntax")
            if self.kind is ModuleExpressionKind.MODULE_REFERENCE and not (
                len(self.value) == 6
                and self.value.startswith("M")
                and self.value[1:].isascii()
                and self.value[1:].isdigit()
            ):
                raise ValueError("module-reference expressions require canonical M-number syntax")
        elif self.kind is ModuleExpressionKind.GROUP:
            if self.value is not None or len(self.children) != 1 or self.operators:
                raise ValueError("group expressions require exactly one child")
        elif self.kind is ModuleExpressionKind.OPTIONAL:
            if self.value is not None or len(self.children) != 1 or self.operators:
                raise ValueError("optional expressions require exactly one child")
        elif self.kind in {ModuleExpressionKind.AND, ModuleExpressionKind.OR}:
            if self.value is not None or len(self.children) < 2:
                raise ValueError("logical expressions require at least two children")
            if len(self.operators) != len(self.children) - 1:
                raise ValueError("logical expressions require one operator between each child")
            if self.kind is ModuleExpressionKind.OR and any(
                item.kind is not ModuleOperatorKind.COMMA for item in self.operators
            ):
                raise ValueError("OR expressions may contain only comma operators")
            if self.kind is ModuleExpressionKind.AND:
                for index, operator in enumerate(self.operators):
                    next_is_optional = (
                        self.children[index + 1].kind is ModuleExpressionKind.OPTIONAL
                    )
                    if (operator.kind is ModuleOperatorKind.MINUS) != next_is_optional:
                        raise ValueError("minus operators and optional children must correspond")
                    if operator.kind not in {
                        ModuleOperatorKind.PLUS,
                        ModuleOperatorKind.SPACE,
                        ModuleOperatorKind.MINUS,
                    }:
                        raise ValueError("AND expressions contain an invalid operator")
        else:  # pragma: no cover - defensive against future enum additions
            raise ValueError("unknown module expression kind")
        for child in self.children:
            if not (
                self.span.start_offset <= child.span.start_offset
                and child.span.end_offset <= self.span.end_offset
            ):
                raise ValueError("child expression spans must be contained by their parent")
        return self


class ModuleDefinitionAst(FrozenModel):
    """Parsed definition with top-level required blocks kept structurally separate."""

    span: SourceSpan
    required_blocks: Annotated[tuple[ModuleExpression, ...], Field(min_length=1)]
    block_separators: tuple[ModuleOperatorOccurrence, ...] = ()

    @model_validator(mode="after")
    def validate_blocks(self) -> Self:
        if len(self.block_separators) != len(self.required_blocks) - 1:
            raise ValueError("one top-level separator is required between each block")
        if any(
            separator.kind is not ModuleOperatorKind.SPACE for separator in self.block_separators
        ):
            raise ValueError("top-level block separators must be spaces")
        if any(
            not (
                self.span.start_offset <= block.span.start_offset
                and block.span.end_offset <= self.span.end_offset
            )
            for block in self.required_blocks
        ):
            raise ValueError("required-block spans must be contained by the definition")
        return self


class ModuleParseDiagnostic(FrozenModel):
    """Bounded parser diagnostic tied to an optional source span."""

    code: ModuleDiagnosticCode
    severity: ModuleDiagnosticSeverity
    message: str = Field(min_length=1, max_length=1_000)
    span: SourceSpan | None = None
    token_preview: str | None = Field(default=None, max_length=120)

    @field_validator("message", "token_preview")
    @classmethod
    def validate_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "diagnostic text")


class ModuleParseLimits(FrozenModel):
    """Hard bounds for lossless tokenization and recursive parsing."""

    max_definition_bytes: int = Field(default=65_536, strict=True, gt=0, le=1_000_000)
    max_tokens: int = Field(default=4_096, strict=True, gt=0, le=100_000)
    max_ast_nodes: int = Field(default=4_096, strict=True, gt=0, le=100_000)
    max_nesting_depth: int = Field(default=64, strict=True, gt=0, le=512)


class ModuleParseResult(FrozenModel):
    """Lossless parse result; unsupported tokens remain explicit AST leaves."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-parse-result:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    parser_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    parser_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    tokens: Annotated[tuple[ModuleToken, ...], Field(max_length=100_000)]
    token_count: NonNegativeCount
    ast: ModuleDefinitionAst | None
    ast_node_count: NonNegativeCount
    diagnostics: tuple[ModuleParseDiagnostic, ...] = ()
    is_valid: bool
    limits: ModuleParseLimits

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.token_count != len(self.tokens):
            raise ValueError("token_count must match tokens")
        next_offset = 0
        for token in self.tokens:
            if token.span.start_offset != next_offset:
                raise ValueError("tokens must provide contiguous lossless source coverage")
            next_offset = token.span.end_offset
        definition_bytes = _utf8_size(token.lexeme for token in self.tokens)
        if self.ast is not None and (
            self.ast.span.start_offset != 0 or self.ast.span.end_offset != next_offset
        ):
            raise ValueError("the definition AST span must cover the complete token stream")
        has_error = any(
            diagnostic.severity is ModuleDiagnosticSeverity.ERROR for diagnostic in self.diagnostics
        )
        expected_valid = self.ast is not None and not has_error
        if self.is_valid != expected_valid:
            raise ValueError("is_valid must reflect the AST and error diagnostics")
        if self.ast is None and self.ast_node_count != 0:
            raise ValueError("missing ASTs must report zero AST nodes")
        if self.ast is not None and self.ast_node_count != _ast_node_count(self.ast):
            raise ValueError("ast_node_count must match the complete expression tree")
        if self.ast_node_count > self.limits.max_ast_nodes:
            raise ValueError("AST nodes exceed the configured parser limit")
        if self.token_count > self.limits.max_tokens:
            raise ValueError("tokens exceed the configured parser limit")
        byte_limit_exceeded = definition_bytes > self.limits.max_definition_bytes
        reports_byte_limit = any(
            diagnostic.code is ModuleDiagnosticCode.DEFINITION_LIMIT_EXCEEDED
            and diagnostic.severity is ModuleDiagnosticSeverity.ERROR
            for diagnostic in self.diagnostics
        )
        if byte_limit_exceeded != reports_byte_limit:
            raise ValueError("definition byte limits and diagnostics must agree")
        return self


class ModuleDefinitionProvenance(FrozenModel):
    """Complete safe retrieval provenance for one supplied MODULE definition."""

    origin: ModuleDefinitionOrigin = ModuleDefinitionOrigin.INLINE
    retrieval: KeggBatchProvenance | None = None

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if self.origin is ModuleDefinitionOrigin.INLINE:
            if self.retrieval is not None:
                raise ValueError("inline definition provenance cannot claim KEGG retrieval")
            return self
        if self.retrieval is None:
            raise ValueError("KEGG definition provenance requires complete retrieval metadata")
        if self.retrieval.operation is not KeggOperation.GET:
            raise ValueError("MODULE definition provenance requires a KEGG GET batch")
        expected_origin = (
            ResponseOrigin.NETWORK
            if self.origin is ModuleDefinitionOrigin.KEGG_NETWORK
            else ResponseOrigin.CACHE
        )
        if self.retrieval.origin is not expected_origin:
            raise ValueError("MODULE definition origin conflicts with retrieval provenance")
        return self

    @property
    def is_stale(self) -> bool:
        """Return whether the complete retrieval batch was served stale."""
        return self.retrieval.is_stale if self.retrieval is not None else False


class ModuleDefinition(FrozenModel):
    """One bounded already-retrieved MODULE definition and its provenance."""

    module_id: ModuleId
    module_name: str | None = Field(default=None, max_length=1_000)
    definition: str = Field(min_length=1, max_length=1_000_000)
    provenance: ModuleDefinitionProvenance = Field(default_factory=ModuleDefinitionProvenance)

    @field_validator("module_name", "definition")
    @classmethod
    def validate_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "module definition")

    @classmethod
    def from_text(
        cls,
        *,
        module_id: str,
        definition: str,
        module_name: str | None = None,
        provenance: ModuleDefinitionProvenance | None = None,
    ) -> "ModuleDefinition":
        """Construct a definition while retaining the exact supplied UTF-8 text."""
        validate_utf8_text(definition, field_name="module definition")
        return cls(
            module_id=module_id,
            module_name=module_name,
            definition=definition,
            provenance=provenance or ModuleDefinitionProvenance(),
        )


class ModuleDefinitionCollection(FrozenModel):
    """Root MODULE plus a duplicate-free local set of referenced definitions."""

    root_module_id: ModuleId
    definitions: Annotated[tuple[ModuleDefinition, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_definitions(self) -> Self:
        module_ids = tuple(item.module_id for item in self.definitions)
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("module definition identifiers must be unique")
        if self.root_module_id not in module_ids:
            raise ValueError("the root module definition must be supplied")
        return self


class ModuleResolutionLimits(FrozenModel):
    """Hard bounds for recursive M-number reference resolution."""

    max_reference_depth: int = Field(default=32, strict=True, gt=0, le=256)
    max_modules: int = Field(default=256, strict=True, gt=0, le=10_000)
    max_references: int = Field(default=2_048, strict=True, gt=0, le=100_000)
    max_total_ast_nodes: int = Field(default=50_000, strict=True, gt=0, le=1_000_000)


class ModuleReferenceEdge(FrozenModel):
    """One reachable M-number reference in source order."""

    source_module_id: ModuleId
    target_module_id: ModuleId
    source_span: SourceSpan


class ModuleReferenceIssue(FrozenModel):
    """One unresolved or unsafe reference with the complete traversal path."""

    kind: ModuleReferenceIssueKind
    source_module_id: ModuleId
    target_module_id: ModuleId
    path: Annotated[tuple[ModuleId, ...], Field(min_length=1)]
    source_span: SourceSpan
    message: str = Field(min_length=1, max_length=1_000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="module reference issue message")


class ResolvedModuleDefinition(FrozenModel):
    """One reachable definition and its syntax result."""

    definition: ModuleDefinition
    parse_result: ModuleParseResult

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        if (
            "".join(token.lexeme for token in self.parse_result.tokens)
            != self.definition.definition
        ):
            raise ValueError("parse-result tokens must reproduce the exact supplied definition")
        return self


class ResolvedModuleGraph(FrozenModel):
    """Bounded reference graph used by the pure evaluator."""

    root_module_id: ModuleId
    modules: Annotated[tuple[ResolvedModuleDefinition, ...], Field(min_length=1)]
    edges: tuple[ModuleReferenceEdge, ...]
    issues: tuple[ModuleReferenceIssue, ...]
    retrieval_provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=5_000),
    ] = ()
    total_ast_nodes: NonNegativeCount
    resolver_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    limits: ModuleResolutionLimits

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        module_ids = tuple(item.definition.module_id for item in self.modules)
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("resolved modules must have unique identifiers")
        if self.root_module_id not in module_ids:
            raise ValueError("resolved graph must contain the root module")
        if self.total_ast_nodes != sum(item.parse_result.ast_node_count for item in self.modules):
            raise ValueError("total_ast_nodes must match resolved parse results")
        if len(self.modules) > self.limits.max_modules:
            raise ValueError("resolved modules exceed the configured module limit")
        if len(self.edges) > self.limits.max_references:
            raise ValueError("resolved edges exceed the configured reference limit")
        if self.total_ast_nodes > self.limits.max_total_ast_nodes:
            raise ValueError("resolved AST nodes exceed the configured total-node limit")
        if any(batch.operation is not KeggOperation.GET for batch in self.retrieval_provenance):
            raise ValueError("MODULE graph retrieval provenance must contain only GET batches")
        edge_keys = tuple(
            (
                edge.source_module_id,
                edge.target_module_id,
                edge.source_span.start_offset,
                edge.source_span.end_offset,
            )
            for edge in self.edges
        )
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("resolved reference edges must identify unique source occurrences")
        if any(edge.source_module_id not in module_ids for edge in self.edges):
            raise ValueError("reference edges must originate in resolved modules")
        edge_key_set = set(edge_keys)
        for issue in self.issues:
            issue_key = (
                issue.source_module_id,
                issue.target_module_id,
                issue.source_span.start_offset,
                issue.source_span.end_offset,
            )
            if (
                issue_key not in edge_key_set
                and issue.kind is not ModuleReferenceIssueKind.REFERENCE_LIMIT
            ):
                raise ValueError("reference issues must correspond to retained reference edges")
        return self


class ModuleEvaluationLimits(FrozenModel):
    """Hard bounds for combinatorial evaluation and returned previews."""

    max_missing_alternatives: int = Field(default=64, strict=True, gt=0, le=10_000)
    max_combination_expansions: int = Field(default=10_000, strict=True, gt=0, le=1_000_000)
    max_ko_ids_per_alternative: int = Field(default=256, strict=True, gt=0, le=10_000)
    max_matched_ko_ids: int = Field(default=256, strict=True, gt=0, le=10_000)
    max_block_previews: int = Field(default=50, strict=True, gt=0, le=10_000)
    max_optional_components: int = Field(default=200, strict=True, gt=0, le=10_000)
    max_uncertain_support_items: int = Field(default=500, strict=True, gt=0, le=10_000)
    max_uncertain_records_per_ko: int = Field(default=100, strict=True, gt=0, le=10_000)


class ModuleAnalysisLimits(FrozenModel):
    """Serializable parser, resolver, and evaluator bounds for one analysis."""

    parsing: ModuleParseLimits = Field(default_factory=ModuleParseLimits)
    resolution: ModuleResolutionLimits = Field(default_factory=ModuleResolutionLimits)
    evaluation: ModuleEvaluationLimits = Field(default_factory=ModuleEvaluationLimits)

    @model_validator(mode="after")
    def require_compatible_ast_budgets(self) -> Self:
        if self.parsing.max_ast_nodes > self.resolution.max_total_ast_nodes:
            raise ValueError("parsing.max_ast_nodes must not exceed resolution.max_total_ast_nodes")
        return self


class MissingKoAlternative(FrozenModel):
    """One inclusion-minimal KO set that would satisfy an incomplete expression."""

    ko_ids: Annotated[tuple[KNumber, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_ko_order(self) -> Self:
        if self.ko_ids != tuple(sorted(set(self.ko_ids))):
            raise ValueError("missing alternative K numbers must be sorted and unique")
        return self


class MinimalMissingAlternatives(FrozenModel):
    """Bounded antichain of minimal missing KO alternatives."""

    alternatives: tuple[MissingKoAlternative, ...]
    truncated: bool
    combination_expansions: NonNegativeCount

    @model_validator(mode="after")
    def validate_antichain(self) -> Self:
        keys = tuple((len(item.ko_ids), item.ko_ids) for item in self.alternatives)
        if keys != tuple(sorted(keys)):
            raise ValueError("missing alternatives must use deterministic size and lexical order")
        sets = tuple(frozenset(item.ko_ids) for item in self.alternatives)
        for index, candidate in enumerate(sets):
            if any(
                other <= candidate for other_index, other in enumerate(sets) if other_index != index
            ):
                raise ValueError("missing alternatives must form an inclusion-minimal antichain")
        return self


class ModuleBlockResult(FrozenModel):
    """Bounded evaluation detail for one required top-level block."""

    block_index: PositiveCount
    state: ModuleBlockState
    source_span: SourceSpan
    matched_ko_ids: tuple[KNumber, ...]
    missing: MinimalMissingAlternatives | None
    matched_ko_ids_truncated: bool = False

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.matched_ko_ids != tuple(sorted(set(self.matched_ko_ids))):
            raise ValueError("matched block K numbers must be sorted and unique")
        if self.state is ModuleBlockState.COMPLETE and self.missing is not None:
            raise ValueError("complete blocks cannot report missing alternatives")
        if self.state is ModuleBlockState.INCOMPLETE and self.missing is None:
            raise ValueError("incomplete blocks require missing alternatives")
        if self.state is ModuleBlockState.NOT_EVALUABLE and self.missing is not None:
            raise ValueError("not-evaluable blocks cannot claim missing alternatives")
        return self


class OptionalComponentResult(FrozenModel):
    """Presence summary for one optional component excluded from completion."""

    component_index: PositiveCount
    source_module_id: ModuleId
    source_span: SourceSpan
    state: OptionalComponentState
    matched_ko_ids: tuple[KNumber, ...]
    matched_ko_ids_truncated: bool = False

    @model_validator(mode="after")
    def validate_ko_order(self) -> Self:
        if self.matched_ko_ids != tuple(sorted(set(self.matched_ko_ids))):
            raise ValueError("matched optional K numbers must be sorted and unique")
        return self


class UncertainSupport(FrozenModel):
    """Policy-defined uncertain evidence responsible for a lenient block change."""

    ko_id: KNumber
    record_ids: Annotated[tuple[RecordIdentifier, ...], Field(min_length=1)]
    required_block_indexes: Annotated[tuple[PositiveCount, ...], Field(min_length=1)]
    record_ids_truncated: bool = False
    required_block_indexes_truncated: bool = False

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.record_ids != tuple(sorted(set(self.record_ids))):
            raise ValueError("uncertain record identifiers must be sorted and unique")
        if self.required_block_indexes != tuple(sorted(set(self.required_block_indexes))):
            raise ValueError("uncertain block indexes must be sorted and unique")
        return self


class ModuleWarning(FrozenModel):
    """One bounded machine-readable interpretation or safety warning."""

    code: ModuleWarningCode
    message: str = Field(min_length=1, max_length=1_000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="module warning message")


class CalculationMethodReference(FrozenModel):
    """Named and versioned deterministic calculation method."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)


class EvaluatedDefinitionProvenance(FrozenModel):
    """Sanitized retrieval provenance for a reachable definition."""

    module_id: ModuleId
    provenance: ModuleDefinitionProvenance


class ModuleEvaluationResult(FrozenModel):
    """Conservative exact completion and project-defined block coverage."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-evaluation-result:2",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    module_id: ModuleId
    module_name: str | None = Field(default=None, max_length=1_000)
    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    decision_policy: DecisionPolicyReference
    evidence_mode: EvidenceMode
    evidence_ko_count: NonNegativeCount
    evaluation_status: ModuleEvaluationStatus
    is_complete: bool | None
    block_coverage: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    completed_required_blocks: NonNegativeCount
    evaluable_required_blocks: NonNegativeCount
    required_block_count: NonNegativeCount
    present_blocks_preview: tuple[PositiveCount, ...]
    missing_blocks_preview: tuple[ModuleBlockResult, ...]
    not_evaluable_blocks_preview: tuple[ModuleBlockResult, ...]
    optional_components: tuple[OptionalComponentResult, ...]
    uncertain_support: tuple[UncertainSupport, ...]
    unresolved_references: tuple[ModuleReferenceIssue, ...]
    calculation_method: CalculationMethodReference
    warnings: tuple[ModuleWarning, ...]
    reference_retrieval_provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=5_000),
    ] = ()
    provenance: Annotated[tuple[EvaluatedDefinitionProvenance, ...], Field(min_length=1)]
    limits: ModuleAnalysisLimits

    @model_validator(mode="after")
    def validate_evaluation_summary(self) -> Self:
        if not (
            0
            <= self.completed_required_blocks
            <= self.evaluable_required_blocks
            <= self.required_block_count
        ):
            raise ValueError("required-block counts are inconsistent")
        fully_evaluable = (
            self.required_block_count > 0
            and self.evaluable_required_blocks == self.required_block_count
        )
        expected = {
            ModuleEvaluationStatus.COMPLETE: (True, True),
            ModuleEvaluationStatus.INCOMPLETE: (False, True),
            ModuleEvaluationStatus.PARTIALLY_EVALUABLE: (None, False),
            ModuleEvaluationStatus.NOT_EVALUABLE: (None, False),
        }[self.evaluation_status]
        if self.is_complete is not expected[0]:
            raise ValueError("is_complete is inconsistent with evaluation_status")
        if fully_evaluable != expected[1]:
            raise ValueError("evaluation_status is inconsistent with evaluable blocks")
        if fully_evaluable and self.required_block_count > 0:
            expected_coverage = self.completed_required_blocks / self.required_block_count
            if self.block_coverage != expected_coverage:
                raise ValueError("block_coverage must use all required top-level blocks")
        elif self.block_coverage is not None:
            raise ValueError("partial or zero-block analyses cannot report block coverage")
        if self.evaluation_status is ModuleEvaluationStatus.COMPLETE and (
            self.completed_required_blocks != self.required_block_count
        ):
            raise ValueError("complete results require every required block")
        if self.evaluation_status is ModuleEvaluationStatus.INCOMPLETE and (
            self.completed_required_blocks == self.required_block_count
        ):
            raise ValueError("incomplete results require at least one missing block")
        if self.evaluation_status is ModuleEvaluationStatus.NOT_EVALUABLE and (
            self.evaluable_required_blocks != 0
        ):
            raise ValueError("not-evaluable results cannot contain evaluable blocks")
        if self.evaluation_status is ModuleEvaluationStatus.PARTIALLY_EVALUABLE and not (
            0 < self.evaluable_required_blocks < self.required_block_count
        ):
            raise ValueError("partial results require both evaluable and unevaluable blocks")
        preview_limit = self.limits.evaluation.max_block_previews
        expected_preview_counts = (
            min(self.completed_required_blocks, preview_limit),
            min(
                self.evaluable_required_blocks - self.completed_required_blocks,
                preview_limit,
            ),
            min(self.required_block_count - self.evaluable_required_blocks, preview_limit),
        )
        actual_preview_counts = (
            len(self.present_blocks_preview),
            len(self.missing_blocks_preview),
            len(self.not_evaluable_blocks_preview),
        )
        if actual_preview_counts != expected_preview_counts:
            raise ValueError("block previews must be complete up to the configured preview limit")
        present_indexes = self.present_blocks_preview
        missing_indexes = tuple(item.block_index for item in self.missing_blocks_preview)
        unknown_indexes = tuple(item.block_index for item in self.not_evaluable_blocks_preview)
        for indexes in (present_indexes, missing_indexes, unknown_indexes):
            if indexes != tuple(sorted(set(indexes))):
                raise ValueError("block preview indexes must be sorted and unique")
            if any(index > self.required_block_count for index in indexes):
                raise ValueError("block previews must identify required blocks")
        if (
            set(present_indexes) & set(missing_indexes)
            or set(present_indexes) & set(unknown_indexes)
            or set(missing_indexes) & set(unknown_indexes)
        ):
            raise ValueError("block preview states must not overlap")
        if any(
            item.state is not ModuleBlockState.INCOMPLETE for item in self.missing_blocks_preview
        ):
            raise ValueError("missing block previews require incomplete block results")
        if any(
            item.state is not ModuleBlockState.NOT_EVALUABLE
            for item in self.not_evaluable_blocks_preview
        ):
            raise ValueError("not-evaluable previews require not-evaluable block results")
        matched_limit = self.limits.evaluation.max_matched_ko_ids
        for block in (*self.missing_blocks_preview, *self.not_evaluable_blocks_preview):
            if len(block.matched_ko_ids) > matched_limit:
                raise ValueError("matched block K numbers exceed the configured output limit")
            if block.matched_ko_ids_truncated and len(block.matched_ko_ids) != matched_limit:
                raise ValueError("truncated matched block previews must fill their output limit")
            if block.missing is not None:
                if (
                    len(block.missing.alternatives)
                    > self.limits.evaluation.max_missing_alternatives
                ):
                    raise ValueError("missing alternatives exceed the configured output limit")
                if block.missing.combination_expansions > (
                    self.limits.evaluation.max_combination_expansions
                ):
                    raise ValueError("combination expansions exceed the configured analysis limit")
                if any(
                    len(alternative.ko_ids) > self.limits.evaluation.max_ko_ids_per_alternative
                    for alternative in block.missing.alternatives
                ):
                    raise ValueError("a missing alternative exceeds its configured KO limit")
        if len(self.optional_components) > self.limits.evaluation.max_optional_components:
            raise ValueError("optional components exceed the configured output limit")
        optional_indexes = tuple(item.component_index for item in self.optional_components)
        if optional_indexes != tuple(range(1, len(self.optional_components) + 1)):
            raise ValueError("optional component indexes must be contiguous in output order")
        for component in self.optional_components:
            if len(component.matched_ko_ids) > matched_limit:
                raise ValueError("matched optional K numbers exceed the configured output limit")
            if (
                component.matched_ko_ids_truncated
                and len(component.matched_ko_ids) != matched_limit
            ):
                raise ValueError("truncated optional KO previews must fill their output limit")
        if len(self.uncertain_support) > self.limits.evaluation.max_uncertain_support_items:
            raise ValueError("uncertain support exceeds the configured output limit")
        if self.evidence_mode is EvidenceMode.STRICT and self.uncertain_support:
            raise ValueError("strict evaluations cannot report uncertain support")
        support_ko_ids = tuple(item.ko_id for item in self.uncertain_support)
        if support_ko_ids != tuple(sorted(set(support_ko_ids))):
            raise ValueError("uncertain support must have sorted unique K numbers")
        for support in self.uncertain_support:
            record_limit = self.limits.evaluation.max_uncertain_records_per_ko
            if len(support.record_ids) > record_limit:
                raise ValueError("uncertain record identifiers exceed the configured output limit")
            if support.record_ids_truncated and len(support.record_ids) != record_limit:
                raise ValueError("truncated uncertain record previews must fill their output limit")
            if len(support.required_block_indexes) > preview_limit:
                raise ValueError("uncertain block indexes exceed the configured preview limit")
            if (
                support.required_block_indexes_truncated
                and len(support.required_block_indexes) != preview_limit
            ):
                raise ValueError("truncated uncertain block previews must fill their output limit")
            if any(index > self.required_block_count for index in support.required_block_indexes):
                raise ValueError("uncertain support must identify required blocks")
        provenance_ids = tuple(item.module_id for item in self.provenance)
        if len(provenance_ids) != len(set(provenance_ids)):
            raise ValueError("evaluated definition provenance must have unique module identifiers")
        if self.module_id not in provenance_ids:
            raise ValueError("evaluation provenance must include the root module")
        if any(
            batch.operation is not KeggOperation.GET
            for batch in self.reference_retrieval_provenance
        ):
            raise ValueError("MODULE evaluation retrieval provenance requires GET batches")
        if self.calculation_method != CalculationMethodReference(
            name=MODULE_CALCULATION_METHOD,
            version=MODULE_CALCULATION_VERSION,
        ):
            raise ValueError("calculation_method is incompatible with this result schema")
        return self


class PairedModuleEvaluation(FrozenModel):
    """Separate strict and lenient evaluations over one immutable dataset."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:paired-module-evaluation:2",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    strict: ModuleEvaluationResult
    lenient: ModuleEvaluationResult
    strict_to_lenient_changed: bool
    newly_completed_block_indexes: tuple[PositiveCount, ...]
    newly_completed_blocks_truncated: bool = False

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        if self.strict.evidence_mode is not EvidenceMode.STRICT:
            raise ValueError("strict result must use strict evidence")
        if self.lenient.evidence_mode is not EvidenceMode.LENIENT:
            raise ValueError("lenient result must use lenient evidence")
        identity_fields = (
            "module_id",
            "module_name",
            "dataset_id",
            "decision_policy",
            "required_block_count",
            "calculation_method",
            "unresolved_references",
            "reference_retrieval_provenance",
            "provenance",
            "limits",
        )
        if any(
            getattr(self.strict, name) != getattr(self.lenient, name) for name in identity_fields
        ):
            raise ValueError("strict and lenient results must share analysis identity")
        if self.newly_completed_block_indexes != tuple(
            sorted(set(self.newly_completed_block_indexes))
        ):
            raise ValueError("newly completed block indexes must be sorted and unique")
        if self.lenient.evidence_ko_count < self.strict.evidence_ko_count:
            raise ValueError("lenient evidence must contain at least as many K numbers as strict")
        if any(
            index > self.strict.required_block_count for index in self.newly_completed_block_indexes
        ):
            raise ValueError("newly completed block indexes must identify required blocks")
        completed_delta = (
            self.lenient.completed_required_blocks - self.strict.completed_required_blocks
        )
        if completed_delta < 0:
            raise ValueError("lenient evidence cannot complete fewer blocks than strict evidence")
        preview_count = len(self.newly_completed_block_indexes)
        preview_limit = self.lenient.limits.evaluation.max_block_previews
        if self.newly_completed_blocks_truncated:
            if not (completed_delta > preview_count and preview_count == preview_limit):
                raise ValueError(
                    "truncated new-block previews must be full and omit at least one block"
                )
        elif preview_count != completed_delta:
            raise ValueError("complete new-block previews must match the completed-block change")
        expected_changed = completed_delta > 0 or any(
            getattr(self.strict, field_name) != getattr(self.lenient, field_name)
            for field_name in ("evaluation_status", "is_complete", "block_coverage")
        )
        if self.strict_to_lenient_changed != expected_changed:
            raise ValueError("strict_to_lenient_changed must reflect the paired results")
        return self


ModuleExpression.model_rebuild()
