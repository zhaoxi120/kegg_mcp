"""Named, versioned annotation decision policies."""

from dataclasses import dataclass
from typing import Protocol, Self

from pydantic import ValidationInfo, field_validator, model_validator

from kegg_mcp.domain.annotations import (
    DecisionPolicyReference,
    FiniteFloat,
    FrozenModel,
    InputFormat,
    KNumber,
    MachineReason,
    NormalizedStatus,
    ScoreType,
    ThresholdRule,
    validate_utf8_text,
)
from kegg_mcp.domain.identifiers import try_normalize_ko_id


class DecisionEvidence(FrozenModel):
    """Evidence supplied to a normalization policy for one source row."""

    raw_ko: str
    ko_id: KNumber | None
    raw_decision: str | None
    score: FiniteFloat | None
    score_type: ScoreType | None
    threshold: FiniteFloat | None
    threshold_rule: ThresholdRule | None

    @field_validator("raw_ko", "raw_decision")
    @classmethod
    def require_utf8_raw_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "raw field")

    @model_validator(mode="after")
    def validate_decision_evidence(self) -> Self:
        normalized_ko, _ = try_normalize_ko_id(self.raw_ko)
        if normalized_ko != self.ko_id:
            raise ValueError("ko_id must be the exact normalization of raw_ko")
        if self.score is not None and self.score_type is None:
            raise ValueError("score_type is required when score is present")
        if (self.threshold is None) != (self.threshold_rule is None):
            raise ValueError("threshold and threshold_rule must be provided together")
        if self.score_type is ScoreType.PROBABILITY:
            for name, value in (("score", self.score), ("threshold", self.threshold)):
                if value is not None and not 0.0 <= value <= 1.0:
                    raise ValueError(f"probability {name} must be between zero and one")
        return self


class DecisionOutcome(FrozenModel):
    """Normalized status and stable machine-readable reason."""

    status: NormalizedStatus
    reason: MachineReason


class DecisionPolicy(Protocol):
    """Interface implemented by immutable, versioned decision policies."""

    @property
    def reference(self) -> DecisionPolicyReference: ...

    @property
    def supported_formats(self) -> frozenset[InputFormat]: ...

    def classify(self, evidence: DecisionEvidence) -> DecisionOutcome: ...


@dataclass(frozen=True, slots=True)
class UserSuppliedKOPolicy:
    """Treat a supplied valid K number as accepted input for analysis."""

    @property
    def reference(self) -> DecisionPolicyReference:
        return DecisionPolicyReference(name="user_supplied_ko", version="1")

    @property
    def supported_formats(self) -> frozenset[InputFormat]:
        return frozenset({InputFormat.PLAIN_KO, InputFormat.GENERIC_CSV, InputFormat.GENERIC_TSV})

    def classify(self, evidence: DecisionEvidence) -> DecisionOutcome:
        if evidence.ko_id is None:
            return DecisionOutcome(
                status=NormalizedStatus.INVALID,
                reason="invalid_ko_identifier",
            )
        return DecisionOutcome(
            status=NormalizedStatus.ACCEPTED,
            reason="user_supplied_annotation",
        )


@dataclass(frozen=True, slots=True)
class CanonicalSourceStatusPolicy:
    """Normalize only explicit canonical source decisions, never scores."""

    @property
    def reference(self) -> DecisionPolicyReference:
        return DecisionPolicyReference(name="canonical_source_status", version="1")

    @property
    def supported_formats(self) -> frozenset[InputFormat]:
        return frozenset({InputFormat.GENERIC_CSV, InputFormat.GENERIC_TSV})

    def classify(self, evidence: DecisionEvidence) -> DecisionOutcome:
        if evidence.ko_id is None:
            if not evidence.raw_ko.strip():
                return DecisionOutcome(
                    status=NormalizedStatus.UNCLASSIFIED,
                    reason="missing_ko_prediction",
                )
            return DecisionOutcome(
                status=NormalizedStatus.INVALID,
                reason="invalid_ko_identifier",
            )
        decision = (evidence.raw_decision or "").strip().lower()
        outcomes = {
            "accepted": DecisionOutcome(
                status=NormalizedStatus.ACCEPTED,
                reason="source_accepted",
            ),
            "uncertain": DecisionOutcome(
                status=NormalizedStatus.UNCERTAIN,
                reason="source_uncertain",
            ),
            "rejected": DecisionOutcome(
                status=NormalizedStatus.REJECTED,
                reason="source_rejected",
            ),
            "unclassified": DecisionOutcome(
                status=NormalizedStatus.UNCLASSIFIED,
                reason="source_unclassified",
            ),
        }
        return outcomes.get(
            decision,
            DecisionOutcome(
                status=NormalizedStatus.UNCLASSIFIED,
                reason="unrecognized_source_decision",
            ),
        )


@dataclass(frozen=True, slots=True)
class DeepKoalaDetailedPolicy:
    """Normalize the documented DeepKOALA detailed-output decision rule."""

    @property
    def reference(self) -> DecisionPolicyReference:
        return DecisionPolicyReference(name="deepkoala_detailed", version="1")

    @property
    def supported_formats(self) -> frozenset[InputFormat]:
        return frozenset({InputFormat.DEEPKOALA_DETAILED})

    def classify(self, evidence: DecisionEvidence) -> DecisionOutcome:
        if evidence.ko_id is None:
            if not evidence.raw_ko.strip():
                return DecisionOutcome(
                    status=NormalizedStatus.UNCLASSIFIED,
                    reason="missing_ko_prediction",
                )
            return DecisionOutcome(
                status=NormalizedStatus.INVALID,
                reason="invalid_ko_identifier",
            )
        if evidence.raw_decision == "*":
            return DecisionOutcome(
                status=NormalizedStatus.ACCEPTED,
                reason="source_acceptance_marker",
            )
        if evidence.raw_decision not in (None, ""):
            return DecisionOutcome(
                status=NormalizedStatus.UNCLASSIFIED,
                reason="unrecognized_source_decision",
            )
        if evidence.score is None or evidence.threshold is None:
            return DecisionOutcome(
                status=NormalizedStatus.UNCLASSIFIED,
                reason="insufficient_decision_evidence",
            )
        if (
            evidence.score_type is not ScoreType.PROBABILITY
            or evidence.threshold_rule is not ThresholdRule.GTE
        ):
            return DecisionOutcome(
                status=NormalizedStatus.UNCLASSIFIED,
                reason="incompatible_score_semantics",
            )
        if evidence.score >= evidence.threshold:
            return DecisionOutcome(
                status=NormalizedStatus.ACCEPTED,
                reason="meets_source_threshold",
            )
        return DecisionOutcome(
            status=NormalizedStatus.REJECTED,
            reason="below_source_threshold",
        )


USER_SUPPLIED_KO_V1 = UserSuppliedKOPolicy()
CANONICAL_SOURCE_STATUS_V1 = CanonicalSourceStatusPolicy()
DEEPKOALA_DETAILED_V1 = DeepKoalaDetailedPolicy()
