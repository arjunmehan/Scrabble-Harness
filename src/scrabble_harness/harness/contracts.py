"""Strict harness records. Configuration describes limits; runs.ledger enforces admission.

DecisionContext and DecisionOutcome are host records. Only DecisionView and
EvidenceView belong in a model request. No provider SDK or native code is loaded.
"""

from collections import Counter
from datetime import date
import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, model_serializer, model_validator

from ..domain import (
    AgentDecision, AnalysisLimits, AnalysisResult, Candidate, CandidateConfig,
    ExchangeMove, MetricDefinition, Move, PlacementMove, PlayerObservation,
    RunConfig, WorkUsed, canonical_bytes, canonical_json,
)
from ..domain._base import (
    CandidateId, DomainModel, FiniteValue, Identifier, NonNegativeInt,
    NonNegativeValue, ObservationId, PositiveValue, Sha256,
)
from ..domain.tiles import Rack

CandidateHandle = Annotated[str, Field(pattern=r"^c(?:[0-9]{2,5})$")]
EvidenceHandle = Annotated[str, Field(pattern=r"^e(?:[0-9]{2,5})$")]
WorkCap = Annotated[int, Field(ge=0, le=2**63 - 1)]
HarnessActionKind = Literal["select", "compare"]


def check_candidate_inventory(observation: PlayerObservation, move: Move, leave: Rack) -> None:
    """Check public physical facts; dictionary legality and scoring stay in the engine."""
    used = move.physical_tiles if isinstance(move, PlacementMove) else (
        move.tiles if isinstance(move, ExchangeMove) else ())
    if Counter(used + leave) != Counter(observation.own_rack):
        raise ValueError("candidate move and leave do not conserve the player's rack")
    if isinstance(move, PlacementMove):
        occupied = {(tile.row, tile.column) for tile in observation.board.tiles}
        if any((tile.row, tile.column) in occupied for tile in move.tiles):
            raise ValueError("candidate places a new tile on an occupied square")
    if isinstance(move, ExchangeMove) and observation.bag_size < 7:
        raise ValueError("candidate exchange needs at least seven bag tiles")


class DecisionLimits(DomainModel):
    max_api_cost_microusd: NonNegativeInt = 30_000
    max_model_attempts: NonNegativeInt = 2
    max_tool_steps: NonNegativeInt = 1
    max_engine_work_units: WorkCap = 300
    engine_work_unit: Literal["candidate_rollout"] = "candidate_rollout"
    max_elapsed_seconds: PositiveValue = 30.0
    finish_reserve_seconds: NonNegativeValue = 2.0

    @model_validator(mode="after")
    def finish_within_deadline(self) -> Self:
        if self.finish_reserve_seconds >= self.max_elapsed_seconds:
            raise ValueError("finish reserve must be shorter than the decision deadline")
        return self


class ComparisonTier(DomainModel):
    name: Literal["light", "standard"]
    max_work_units: Annotated[int, Field(ge=4, le=2**63 - 1)]
    max_elapsed_seconds: PositiveValue = 8.0


class PresentationConfig(DomainModel):
    version: Literal["candidate-view-v1"] = "candidate-view-v1"
    max_history_turns: NonNegativeInt = 8
    max_request_bytes: Annotated[int, Field(ge=1024, le=16 * 1024 * 1024)] = 65_536
    max_tool_result_bytes: Annotated[int, Field(ge=256, le=1024 * 1024)] = 8192

    @model_validator(mode="after")
    def result_fits_request(self) -> Self:
        if self.max_tool_result_bytes >= self.max_request_bytes:
            raise ValueError("tool result ceiling must be smaller than the request ceiling")
        return self


class HarnessConfig(DomainModel):
    workflow: Identifier = "standard"
    candidate_config: CandidateConfig = CandidateConfig()
    decision_limits: DecisionLimits = DecisionLimits()
    comparison_tiers: Annotated[tuple[ComparisonTier, ...], Field(min_length=1, max_length=2)] = (
        ComparisonTier(name="light", max_work_units=60),
        ComparisonTier(name="standard", max_work_units=300),
    )
    presentation: PresentationConfig = PresentationConfig()
    prompt_version: Identifier = "scrabble-decision-v1"
    tool_schema_version: Identifier = "scrabble-tools-v1"
    fallback_policy: Literal["static", "latest_comparison"] = "static"
    max_repair_rounds: Annotated[int, Field(ge=0, le=1)] = 1
    max_transport_retries: Annotated[int, Field(ge=0, le=1)] = 0

    @model_validator(mode="after")
    def supported_configuration(self) -> Self:
        if self.candidate_config.selection_policy not in {"static-diverse-v1", "static-top-v1"}:
            raise ValueError("unsupported candidate generation policy")
        names = tuple(tier.name for tier in self.comparison_tiers)
        if len(set(names)) != len(names):
            raise ValueError("comparison tier names must be unique")
        tiers = {tier.name: tier for tier in self.comparison_tiers}
        if "light" in tiers and "standard" in tiers:
            if tiers["light"].max_work_units > tiers["standard"].max_work_units:
                raise ValueError("light comparison cannot request more work than standard")
        if any(tier.max_elapsed_seconds > self.decision_limits.max_elapsed_seconds for tier in self.comparison_tiers):
            raise ValueError("comparison deadline cannot exceed the decision deadline")
        return self


class SelectAction(DomainModel):
    kind: Literal["select"] = "select"
    candidate_handle: CandidateHandle
    evidence_ids: Annotated[tuple[EvidenceHandle, ...], Field(max_length=1000)] = ()
    brief_note: Annotated[str, Field(max_length=500)] = ""

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence handles must be unique")
        return self


class CompareAction(DomainModel):
    kind: Literal["compare"] = "compare"
    candidate_handles: Annotated[tuple[CandidateHandle, ...], Field(min_length=2, max_length=4)]
    tier: Literal["light", "standard"]

    @model_validator(mode="after")
    def distinct_handles(self) -> Self:
        if len(set(self.candidate_handles)) != len(self.candidate_handles):
            raise ValueError("comparison needs distinct candidate handles")
        return self


HarnessAction = Annotated[SelectAction | CompareAction, Field(discriminator="kind")]


class ScopedAction(DomainModel):
    """The host attaches scope to provider tool arguments; models do not supply it."""
    decision_id: Identifier
    observation_id: ObservationId
    action: HarnessAction


class CandidateBinding(DomainModel):
    """Host-only mapping; handle numbers never imply native rank."""
    handle: CandidateHandle
    candidate_id: CandidateId


class ComparisonRequest(DomainModel):
    """Durable native intent bound to one saved model tool call."""
    attempt_id: Identifier
    call_id: Identifier
    envelope: ScopedAction
    requested: Annotated[tuple[CandidateBinding, ...], Field(min_length=2, max_length=4)]
    limits: AnalysisLimits
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)]

    @model_validator(mode="after")
    def bound_intent(self) -> Self:
        if not isinstance(self.envelope.action, CompareAction):
            raise ValueError("native intent requires a comparison")
        if tuple(item.handle for item in self.requested) != self.envelope.action.candidate_handles:
            raise ValueError("native intent handles differ from its action")
        if len({item.candidate_id for item in self.requested}) != len(self.requested):
            raise ValueError("native intent requires distinct candidate identities")
        if self.limits.work_unit != "candidate_rollout" or self.limits.max_work_units < len(self.requested):
            raise ValueError("native intent cannot cover requested candidates")
        return self


class VisibleCandidate(DomainModel):
    handle: CandidateHandle
    move: Move
    exact_score: NonNegativeInt
    leave: Rack

    @model_validator(mode="after")
    def zero_nonplacement_score(self) -> Self:
        if not isinstance(self.move, PlacementMove) and self.exact_score != 0:
            raise ValueError("nonplacement visible candidates must score zero")
        return self


class DecisionView(DomainModel):
    """Allowlisted model input: no full pool, fallback marker, equity, or provenance."""
    decision_id: Identifier
    observation: PlayerObservation
    presentation_version: Literal["candidate-view-v1"] = "candidate-view-v1"
    candidates: Annotated[tuple[VisibleCandidate, ...], Field(min_length=1, max_length=10_000)]

    @model_validator(mode="after")
    def valid_visible_candidates(self) -> Self:
        if self.observation.status != "in_progress" or self.observation.player != self.observation.active_player:
            raise ValueError("a decision requires the active player's in-progress observation")
        handles = tuple(candidate.handle for candidate in self.candidates)
        if handles != tuple(f"c{index:02d}" for index in range(1, len(handles) + 1)):
            raise ValueError("visible handles must be consecutive in presentation order")
        moves = tuple(canonical_bytes(candidate.move) for candidate in self.candidates)
        if len(set(moves)) != len(moves):
            raise ValueError("visible candidate actions must be unique")
        for candidate in self.candidates:
            check_candidate_inventory(self.observation, candidate.move, candidate.leave)
        return self


class ComparisonEvidence(DomainModel):
    """Host-issued result binding a request, decision scope, and canonical actions."""
    evidence_id: EvidenceHandle
    decision_id: Identifier
    observation_id: ObservationId
    requested: Annotated[tuple[CandidateBinding, ...], Field(min_length=2, max_length=4)]
    tier: Literal["light", "standard"]
    limits: AnalysisLimits
    seed: NonNegativeInt
    result: AnalysisResult

    @model_validator(mode="after")
    def bind_result_to_request(self) -> Self:
        ids = tuple(binding.candidate_id for binding in self.requested)
        handles = tuple(binding.handle for binding in self.requested)
        if len(set(ids)) != len(ids) or len(set(handles)) != len(handles):
            raise ValueError("evidence request needs distinct actions and handles")
        if self.result.observation_id != self.observation_id:
            raise ValueError("evidence result belongs to another observation")
        if (self.limits.work_unit != "candidate_rollout" or self.limits.max_work_units < len(ids)
                or self.result.work_used.count > self.limits.max_work_units):
            raise ValueError("evidence violates its requested work allowance")
        if self.seed > 2**64 - 1 or self.result.seed != self.seed:
            raise ValueError("evidence result differs from its requested seed")
        returned = {value.candidate_id for value in self.result.values}
        if not returned.issubset(ids):
            raise ValueError("evidence returned an unrequested action")
        if self.result.completion_status in {"complete", "work_limit"} and returned != set(ids):
            raise ValueError("completed comparison must report every requested action")
        if self.result.work_used.unit != "candidate_rollout" or self.result.metric_definition.value_kind != "estimate":
            raise ValueError("harness comparison requires candidate-rollout estimates")
        if self.result.values and self.result.work_used.count < len(self.result.values):
            raise ValueError("reported work cannot cover the comparison values")
        return self


class VisibleEvaluation(DomainModel):
    candidate_handle: CandidateHandle
    value: FiniteValue
    standard_error: NonNegativeValue | None = None


class EvidenceView(DomainModel):
    """Model-visible projection of one issued comparison, with no canonical IDs."""
    evidence_id: EvidenceHandle
    decision_id: Identifier
    observation_id: ObservationId
    requested_handles: Annotated[tuple[CandidateHandle, ...], Field(min_length=2, max_length=4)]
    tier: Literal["light", "standard"]
    values: Annotated[tuple[VisibleEvaluation, ...], Field(max_length=4)]
    metric_definition: MetricDefinition
    limits: AnalysisLimits
    work_used: WorkUsed
    elapsed_time_seconds: NonNegativeValue
    seed: NonNegativeInt
    completion_status: Literal["complete", "work_limit", "timeout", "cancelled", "failed"]

    @model_validator(mode="after")
    def honest_visible_result(self) -> Self:
        if self.seed > 2**64 - 1:
            raise ValueError("evidence seed exceeds native range")
        returned = tuple(value.candidate_handle for value in self.values)
        if len(set(self.requested_handles)) != len(self.requested_handles) or len(set(returned)) != len(returned):
            raise ValueError("evidence view handles must be unique")
        if not set(returned).issubset(self.requested_handles):
            raise ValueError("evidence view contains unrequested handles")
        if self.completion_status in {"complete", "work_limit"} and set(returned) != set(self.requested_handles):
            raise ValueError("completed evidence view must include every requested handle")
        if self.metric_definition.value_kind != "estimate" or self.work_used.unit != "candidate_rollout":
            raise ValueError("evidence view requires candidate-rollout estimates")
        if (self.limits.work_unit != "candidate_rollout" or self.limits.max_work_units < len(self.requested_handles)
                or self.work_used.count > self.limits.max_work_units):
            raise ValueError("evidence view violates requested work")
        if self.values and self.work_used.count < len(self.values):
            raise ValueError("evidence view lacks work for its values")
        return self


class DecisionUsage(DomainModel):
    """Unknown allowance is separate from confirmed work and settled cost estimates."""
    model_attempts: NonNegativeInt = 0
    tool_steps: NonNegativeInt = 0
    confirmed_engine_work_units: NonNegativeInt = 0
    unresolved_engine_work_units: NonNegativeInt = 0
    engine_work_unit: Literal["candidate_rollout"] = "candidate_rollout"
    settled_api_cost_microusd: NonNegativeInt = 0
    unresolved_api_cost_microusd: NonNegativeInt = 0
    elapsed_seconds: NonNegativeValue = 0.0


class DecisionOutcome(DomainModel):
    """Host record, not a provider request or an authorization to commit a turn."""
    decision_id: Identifier
    observation_id: ObservationId
    status: Literal["selected", "fallback", "aborted"]
    decision: AgentDecision | None = None
    trusted_pool: Annotated[tuple[Candidate, ...], Field(max_length=10_000)] = ()
    visible_bindings: Annotated[tuple[CandidateBinding, ...], Field(max_length=10_000)] = ()
    evidence: Annotated[tuple[ComparisonEvidence, ...], Field(max_length=1000)] = ()
    usage: DecisionUsage = DecisionUsage()
    terminal_reason: Identifier
    selection_origin: Literal["model", "static", "latest_comparison"] | None = None

    @model_serializer(mode="wrap")
    def compatible_origin_default(self, handler):
        data = handler(self)
        if data.get("selection_origin") is None:
            data.pop("selection_origin", None)
        return data

    @model_validator(mode="after")
    def bound_outcome(self) -> Self:
        if self.selection_origin is not None:
            if self.status == "aborted" or (self.status == "selected") != (self.selection_origin == "model"):
                raise ValueError("selection origin disagrees with outcome status")
            if self.selection_origin == "latest_comparison":
                if self.decision is None or len(self.decision.evidence_ids) != 1:
                    raise ValueError("comparison fallback must cite its chosen batch")
                batch = next((e for e in self.evidence if e.evidence_id == self.decision.evidence_ids[0]), None)
                if (batch is None or batch.result.completion_status not in {"complete", "work_limit", "timeout"}
                        or {v.candidate_id for v in batch.result.values} != {b.candidate_id for b in batch.requested}):
                    raise ValueError("comparison fallback requires a usable full-coverage batch")
                best = min(batch.result.values, key=lambda value: (-value.value, value.candidate_id))
                if self.decision.candidate_id != best.candidate_id:
                    raise ValueError("comparison fallback differs from its cited batch winner")
        pool = {candidate.candidate_id: candidate for candidate in self.trusted_pool}
        if len(pool) != len(self.trusted_pool) or any(c.observation_id != self.observation_id for c in self.trusted_pool):
            raise ValueError("outcome pool contains duplicate or foreign candidates")
        handles = tuple(binding.handle for binding in self.visible_bindings)
        ids = tuple(binding.candidate_id for binding in self.visible_bindings)
        if handles != tuple(f"c{index:02d}" for index in range(1, len(handles) + 1)) or len(set(ids)) != len(ids):
            raise ValueError("outcome visible bindings are inconsistent")
        if not set(ids).issubset(pool):
            raise ValueError("outcome bindings contain a candidate outside its pool")
        issued = {item.evidence_id: item for item in self.evidence}
        if len(issued) != len(self.evidence):
            raise ValueError("outcome evidence handles must be unique")
        if len({item.result.analysis_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("outcome cannot count one analysis twice")
        for item in self.evidence:
            if item.decision_id != self.decision_id or item.observation_id != self.observation_id:
                raise ValueError("outcome evidence belongs to another decision")
            if any(binding not in self.visible_bindings for binding in item.requested):
                raise ValueError("outcome evidence references an invisible candidate")
        if self.usage.confirmed_engine_work_units < sum(item.result.work_used.count for item in self.evidence):
            raise ValueError("outcome usage omits reported evidence work")
        if self.status == "aborted":
            if self.decision is not None:
                raise ValueError("an aborted outcome cannot contain a selection")
        else:
            if self.decision is None or self.decision.observation_id != self.observation_id:
                raise ValueError("selected outcomes require a decision in this observation")
            if self.decision.candidate_id not in ids:
                raise ValueError("selected outcome must resolve a visible candidate")
            if not set(self.decision.evidence_ids).issubset(issued):
                raise ValueError("decision cites evidence not issued in this outcome")
            if (self.status == "fallback") != (self.decision.fallback_reason is not None):
                raise ValueError("fallback status and decision reason disagree")
        return self


class RateCard(DomainModel):
    provider: Identifier
    model: Identifier
    currency: Literal["USD"] = "USD"
    input_microusd_per_million_tokens: NonNegativeInt
    output_microusd_per_million_tokens: NonNegativeInt
    cached_input_microusd_per_million_tokens: NonNegativeInt | None = None
    cache_write_microusd_per_million_tokens: NonNegativeInt | None = None
    billing_tier: Identifier = "standard"
    max_input_tokens_at_rate: Annotated[int, Field(gt=0, le=2**63 - 1)] | None = None
    source_url: Annotated[str, Field(pattern=r"^https://\S+$", max_length=2048)]
    retrieved_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    estimation_method: Identifier

    @model_serializer(mode="wrap")
    def compatible_v1_defaults(self, handler):
        # Keep existing manifest/request/body hashes stable when new optional
        # accounting fields are unused. Non-default values remain identity-bearing.
        data = handler(self)
        if data.get('cache_write_microusd_per_million_tokens') == None:
            data.pop('cache_write_microusd_per_million_tokens', None)
        if data.get('billing_tier') == 'standard':
            data.pop('billing_tier', None)
        if data.get('max_input_tokens_at_rate') == None:
            data.pop('max_input_tokens_at_rate', None)
        return data

    @model_validator(mode="after")
    def real_calendar_date(self) -> Self:
        date.fromisoformat(self.retrieved_on)
        return self


class DependencyVersion(DomainModel):
    name: Identifier
    version: Identifier


class ManifestContent(DomainModel):
    run_config: RunConfig
    harness_config: HarnessConfig
    engine_configuration_id: Identifier
    source_sha256: Sha256
    dependencies: Annotated[tuple[DependencyVersion, ...], Field(min_length=1, max_length=1000)]
    rate_card: RateCard
    trial_id: Identifier

    @model_validator(mode="after")
    def consistent_execution(self) -> Self:
        run, harness = self.run_config, self.harness_config
        if run.candidate_config != harness.candidate_config or run.policy_version != harness.workflow:
            raise ValueError("run and harness candidate/workflow configuration disagree")
        if (run.model.provider, run.model.model) != (self.rate_card.provider, self.rate_card.model):
            raise ValueError("rate card does not match the configured provider/model")
        if run.limits.engine_work_unit != "candidate_rollout":
            raise ValueError("run requires candidate-rollout work units")
        limits = harness.decision_limits
        for turn, total in ((limits.max_model_attempts, run.limits.max_model_calls),
                            (limits.max_api_cost_microusd, run.limits.max_api_cost_microusd),
                            (limits.max_engine_work_units, run.limits.max_engine_work_units),
                            (limits.max_elapsed_seconds, run.limits.max_elapsed_seconds)):
            if turn > total:
                raise ValueError("decision limits cannot exceed run limits")
        if run.seeds.analysis > 2**64 - 1:
            raise ValueError("analysis seed exceeds native range")
        if len({dependency.name for dependency in self.dependencies}) != len(self.dependencies):
            raise ValueError("dependency names must be unique")
        return self


def _manifest_id(content: ManifestContent) -> str:
    payload = json.loads(canonical_json(content))
    payload["dependencies"] = sorted(payload["dependencies"], key=lambda item: item["name"])
    envelope = {"manifest_identity_version": 1, "payload": payload}
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return "manifest_v1_" + hashlib.sha256(encoded.encode()).hexdigest()


class ExecutionManifest(ManifestContent):
    """Content identity detects changes; it does not authenticate a run or its inputs."""
    manifest_id: Annotated[str, Field(pattern=r"^manifest_v1_[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        content = ManifestContent.model_validate(self.model_dump(mode="python", exclude={"manifest_id"}))
        if self.manifest_id != _manifest_id(content):
            raise ValueError("execution manifest contents differ from its identity")
        return self

    @classmethod
    def create(cls, **fields) -> "ExecutionManifest":
        content = ManifestContent(**fields)
        return cls(**content.model_dump(mode="python"), manifest_id=_manifest_id(content))
