"""Bounded analysis records with explicit metrics, work units, and completion."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import (
    CandidateId, DomainModel, FiniteValue, Identifier, NonEmptyText, NonNegativeInt,
    NonNegativeValue, ObservationId, PositiveInt, PositiveValue,
)


class AnalysisLimits(DomainModel):
    max_work_units: PositiveInt
    work_unit: Identifier
    max_elapsed_seconds: PositiveValue


class WorkUsed(DomainModel):
    unit: Identifier
    definition: NonEmptyText
    count: NonNegativeInt


class MetricDefinition(DomainModel):
    name: Identifier
    unit: Identifier
    description: NonEmptyText
    perspective: Literal["observing_player"] = "observing_player"
    value_kind: Literal["estimate", "exact"]


class CandidateEvaluation(DomainModel):
    candidate_id: CandidateId
    value: FiniteValue
    standard_error: NonNegativeValue | None = None


CompletionStatus = Literal["complete", "work_limit", "timeout", "cancelled", "failed"]


class AnalysisResult(DomainModel):
    analysis_id: Identifier
    observation_id: ObservationId
    values: tuple[CandidateEvaluation, ...]
    metric_definition: MetricDefinition
    work_used: WorkUsed
    elapsed_time_seconds: NonNegativeValue
    seed: NonNegativeInt
    completion_status: CompletionStatus
    completion_note: NonEmptyText | None = None

    @model_validator(mode="after")
    def honest_completion(self) -> Self:
        if len({value.candidate_id for value in self.values}) != len(self.values):
            raise ValueError("analysis contains duplicate candidate IDs")
        if self.completion_status == "complete" and not self.values:
            raise ValueError("completed analysis must contain candidate values")
        if self.metric_definition.value_kind == "exact":
            if self.completion_status != "complete":
                raise ValueError("incomplete analysis cannot claim exact values")
            if any(value.standard_error is not None for value in self.values):
                raise ValueError("exact values must not carry estimated standard errors")
        return self


class AgentDecision(DomainModel):
    observation_id: ObservationId
    candidate_id: CandidateId
    workflow: Identifier
    evidence_ids: tuple[Identifier, ...] = ()
    brief_note: Annotated[str, Field(max_length=500)] = ""
    fallback_reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("decision evidence IDs must be unique")
        return self
