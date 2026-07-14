"""Contract tests for KEGG MODULE syntax and evaluation models."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis.contracts import (
    MODULE_CALCULATION_METHOD,
    MODULE_CALCULATION_VERSION,
    MODULE_PARSER_NAME,
    MODULE_PARSER_VERSION,
    CalculationMethodReference,
    EvaluatedDefinitionProvenance,
    MinimalMissingAlternatives,
    MissingKoAlternative,
    ModuleAnalysisLimits,
    ModuleBlockResult,
    ModuleBlockState,
    ModuleDefinition,
    ModuleDefinitionAst,
    ModuleDefinitionOrigin,
    ModuleDefinitionProvenance,
    ModuleDiagnosticCode,
    ModuleDiagnosticSeverity,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleOperatorKind,
    ModuleOperatorOccurrence,
    ModuleParseDiagnostic,
    ModuleParseLimits,
    ModuleParseResult,
    ModuleToken,
    ModuleTokenKind,
    PairedModuleEvaluation,
    SourceSpan,
)
from kegg_mcp.domain import DecisionPolicyReference, EvidenceMode
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)


def _retrieval(
    *,
    operation: KeggOperation = KeggOperation.GET,
    origin: ResponseOrigin = ResponseOrigin.CACHE,
) -> KeggBatchProvenance:
    retrieved_at = datetime(2026, 7, 14, tzinfo=UTC)
    cached = origin is ResponseOrigin.CACHE
    return KeggBatchProvenance(
        operation=operation,
        request_key_sha256="1" * 64,
        access_mode=AccessMode.OFFLINE_CACHE if cached else AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_fingerprint=PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        origin=origin,
        cache_lookup_state=CacheLookupState.STALE_HIT if cached else CacheLookupState.MISS,
        retrieved_at=retrieved_at,
        served_at=retrieved_at + timedelta(days=2) if cached else retrieved_at,
        expires_at=retrieved_at + timedelta(days=1),
        response_sha256="2" * 64,
        response_bytes=100,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release 116.0+/07-01, Jul 25",
        attempt_count=0 if cached else 1,
        is_stale=cached,
    )


def _span(start: int, end: int) -> SourceSpan:
    return SourceSpan(
        start_offset=start,
        end_offset=end,
        start_line=1,
        start_column=start + 1,
        end_line=1,
        end_column=end + 1,
    )


def _ko(value: str, start: int = 0) -> ModuleExpression:
    return ModuleExpression(
        kind=ModuleExpressionKind.KO,
        span=_span(start, start + len(value)),
        value=value,
    )


def _evaluation(
    *,
    mode: EvidenceMode,
    completed: int,
    evidence_ko_count: int,
) -> ModuleEvaluationResult:
    definition = ModuleDefinition.from_text(module_id="M00001", definition="K00001")
    is_complete = completed == 1
    missing = ()
    if not is_complete:
        missing = (
            ModuleBlockResult(
                block_index=1,
                state=ModuleBlockState.INCOMPLETE,
                source_span=_span(0, 6),
                matched_ko_ids=(),
                missing=MinimalMissingAlternatives(
                    alternatives=(MissingKoAlternative(ko_ids=("K00001",)),),
                    truncated=False,
                    combination_expansions=0,
                ),
            ),
        )
    return ModuleEvaluationResult(
        module_id="M00001",
        module_name=None,
        definition_sha256=definition.definition_sha256,
        dataset_id="dataset-1",
        decision_policy=DecisionPolicyReference(name="test_policy", version="1"),
        evidence_mode=mode,
        evidence_ko_count=evidence_ko_count,
        evaluation_status=(
            ModuleEvaluationStatus.COMPLETE if is_complete else ModuleEvaluationStatus.INCOMPLETE
        ),
        is_complete=is_complete,
        block_coverage=float(completed),
        completed_required_blocks=completed,
        evaluable_required_blocks=1,
        required_block_count=1,
        present_blocks_preview=(1,) if is_complete else (),
        missing_blocks_preview=missing,
        not_evaluable_blocks_preview=(),
        optional_components=(),
        uncertain_support=(),
        unresolved_references=(),
        calculation_method=CalculationMethodReference(
            name=MODULE_CALCULATION_METHOD,
            version=MODULE_CALCULATION_VERSION,
        ),
        warnings=(),
        provenance=(
            EvaluatedDefinitionProvenance(
                module_id="M00001",
                definition_sha256=definition.definition_sha256,
                provenance=definition.provenance,
            ),
        ),
        limits=ModuleAnalysisLimits(),
    )


def test_token_contract_requires_exact_span_and_canonical_identifier() -> None:
    token = ModuleToken(kind=ModuleTokenKind.KO, lexeme="K00001", span=_span(0, 6))

    assert token.span.end_offset == 6
    with pytest.raises(ValidationError, match="canonical K-number"):
        ModuleToken(kind=ModuleTokenKind.KO, lexeme="K001", span=_span(0, 4))
    with pytest.raises(ValidationError, match="span length"):
        ModuleToken(kind=ModuleTokenKind.UNSUPPORTED, lexeme="bad", span=_span(0, 2))


def test_ast_requires_minus_operator_to_correspond_to_optional_child() -> None:
    left = _ko("K00001", 0)
    right = _ko("K00002", 7)

    with pytest.raises(ValidationError, match="minus operators and optional children"):
        ModuleExpression(
            kind=ModuleExpressionKind.AND,
            span=_span(0, 13),
            children=(left, right),
            operators=(ModuleOperatorOccurrence(kind=ModuleOperatorKind.MINUS, span=_span(6, 7)),),
        )


def test_unsupported_leaf_can_remain_in_a_structurally_valid_parse() -> None:
    token = ModuleToken(
        kind=ModuleTokenKind.UNSUPPORTED,
        lexeme="RM001",
        span=_span(0, 5),
    )
    unsupported = ModuleExpression(
        kind=ModuleExpressionKind.UNSUPPORTED,
        span=token.span,
        value=token.lexeme,
    )

    result = ModuleParseResult(
        definition_sha256=hashlib.sha256(token.lexeme.encode("utf-8")).hexdigest(),
        parser_name=MODULE_PARSER_NAME,
        parser_version=MODULE_PARSER_VERSION,
        tokens=(token,),
        token_count=1,
        ast=ModuleDefinitionAst(
            span=token.span,
            required_blocks=(unsupported,),
            block_separators=(),
        ),
        ast_node_count=1,
        diagnostics=(
            ModuleParseDiagnostic(
                code=ModuleDiagnosticCode.UNSUPPORTED_TOKEN,
                severity=ModuleDiagnosticSeverity.WARNING,
                message="The token is not supported by the KO module evaluator.",
                span=token.span,
                token_preview=token.lexeme,
            ),
        ),
        is_valid=True,
        limits=ModuleParseLimits(),
    )

    assert result.is_valid
    assert result.ast is not None
    assert result.ast.required_blocks[0].kind is ModuleExpressionKind.UNSUPPORTED


def test_parse_result_rejects_noncontiguous_or_digest_mismatched_tokens() -> None:
    token = ModuleToken(kind=ModuleTokenKind.KO, lexeme="K00001", span=_span(0, 6))
    payload = {
        "definition_sha256": "0" * 64,
        "parser_name": MODULE_PARSER_NAME,
        "parser_version": MODULE_PARSER_VERSION,
        "tokens": (token,),
        "token_count": 1,
        "ast": ModuleDefinitionAst(
            span=token.span,
            required_blocks=(_ko("K00001"),),
        ),
        "ast_node_count": 1,
        "diagnostics": (),
        "is_valid": True,
        "limits": ModuleParseLimits(),
    }

    with pytest.raises(ValidationError, match="lossless token stream"):
        ModuleParseResult.model_validate(payload)


def test_missing_alternatives_must_be_sorted_and_form_an_antichain() -> None:
    with pytest.raises(ValidationError, match="inclusion-minimal antichain"):
        MinimalMissingAlternatives(
            alternatives=(
                MissingKoAlternative(ko_ids=("K00001",)),
                MissingKoAlternative(ko_ids=("K00001", "K00002")),
            ),
            truncated=False,
            combination_expansions=1,
        )


def test_definition_factory_hashes_exact_text_without_normalizing_whitespace() -> None:
    first = ModuleDefinition.from_text(module_id="M00001", definition="K00001 K00002")
    wrapped = ModuleDefinition.from_text(module_id="M00001", definition="K00001\nK00002")

    assert first.definition_sha256 != wrapped.definition_sha256
    with pytest.raises(ValidationError, match="definition_sha256"):
        ModuleDefinition(
            module_id="M00001",
            module_name=None,
            definition="K00001",
            definition_sha256="0" * 64,
            provenance=ModuleDefinitionProvenance(),
        )


def test_retrieved_definition_provenance_requires_sanitized_metadata() -> None:
    retrieval = _retrieval()
    provenance = ModuleDefinitionProvenance(
        origin=ModuleDefinitionOrigin.KEGG_CACHE,
        retrieval=retrieval,
    )

    assert provenance.is_stale
    assert provenance.retrieval == retrieval
    assert provenance.model_dump(mode="json")["retrieval"]["cache_lookup_state"] == "stale_hit"
    with pytest.raises(ValidationError, match="requires complete retrieval metadata"):
        ModuleDefinitionProvenance(origin=ModuleDefinitionOrigin.KEGG_NETWORK)
    with pytest.raises(ValidationError, match="requires a KEGG GET batch"):
        ModuleDefinitionProvenance(
            origin=ModuleDefinitionOrigin.KEGG_CACHE,
            retrieval=_retrieval(operation=KeggOperation.LINK),
        )
    with pytest.raises(ValidationError, match="conflicts with retrieval provenance"):
        ModuleDefinitionProvenance(
            origin=ModuleDefinitionOrigin.KEGG_NETWORK,
            retrieval=retrieval,
        )


def test_evaluation_contract_rejects_partial_denominator_coverage() -> None:
    complete = _evaluation(mode=EvidenceMode.STRICT, completed=1, evidence_ko_count=1)
    payload = complete.model_dump()
    payload.update(
        evaluation_status=ModuleEvaluationStatus.PARTIALLY_EVALUABLE,
        is_complete=None,
        block_coverage=1.0,
        evaluable_required_blocks=0,
        completed_required_blocks=0,
        present_blocks_preview=(),
    )

    with pytest.raises(ValidationError, match=r"evaluation_status|block coverage|block_coverage"):
        ModuleEvaluationResult.model_validate(payload)


def test_paired_contract_tracks_outcome_change_not_unused_uncertain_evidence() -> None:
    strict = _evaluation(mode=EvidenceMode.STRICT, completed=0, evidence_ko_count=0)
    unchanged_lenient = _evaluation(mode=EvidenceMode.LENIENT, completed=0, evidence_ko_count=1)
    unchanged = PairedModuleEvaluation(
        strict=strict,
        lenient=unchanged_lenient,
        strict_to_lenient_changed=False,
        newly_completed_block_indexes=(),
    )

    changed_lenient = _evaluation(mode=EvidenceMode.LENIENT, completed=1, evidence_ko_count=1)
    changed = PairedModuleEvaluation(
        strict=strict,
        lenient=changed_lenient,
        strict_to_lenient_changed=True,
        newly_completed_block_indexes=(1,),
    )

    assert not unchanged.strict_to_lenient_changed
    assert changed.newly_completed_block_indexes == (1,)


@pytest.mark.parametrize(
    ("model", "schema_id"),
    [
        (ModuleParseResult, "urn:kegg-mcp:schema:module-parse-result:1"),
        (ModuleEvaluationResult, "urn:kegg-mcp:schema:module-evaluation-result:2"),
        (PairedModuleEvaluation, "urn:kegg-mcp:schema:paired-module-evaluation:2"),
    ],
)
def test_public_contract_schemas_are_versioned(model: type[object], schema_id: str) -> None:
    assert model.model_json_schema()["$id"] == schema_id  # type: ignore[attr-defined]
