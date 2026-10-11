"""Private, immutable persistence records; resource facts are not budget admission."""

import hashlib
from typing import Annotated, Literal, Self

from pydantic import Field, model_serializer, model_validator

from ..domain import AnalysisLimits, AnalysisResult, Move, PlayerObservation, RunLimits, canonical_bytes
from ..domain._base import DomainModel, Identifier, Sha256
from ..harness.budget import CostQuote
from ..harness.context import DecisionContext
from ..harness.contracts import ComparisonRequest, DecisionOutcome, ExecutionManifest
from ..providers.protocol import ModelReply, ModelRequest

SQLiteCount = Annotated[int, Field(ge=0, le=2**63 - 1)]
UTCStamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")]


class ResourceAmounts(DomainModel):
    api_cost_microusd: SQLiteCount = 0
    model_calls: SQLiteCount = 0
    engine_work_units: SQLiteCount = 0
    tool_steps: SQLiteCount = 0
    engine_work_unit: Literal["candidate_rollout"] = "candidate_rollout"


def _sqlite_limits(limits: RunLimits) -> None:
    if limits.engine_work_unit != "candidate_rollout":
        raise ValueError("storage requires candidate-rollout units")
    for value in (limits.max_api_cost_microusd, limits.max_model_calls, limits.max_engine_work_units):
        if value > 2**63 - 1:
            raise ValueError("resource cap exceeds SQLite's integer range")


class CampaignRecord(DomainModel):
    campaign_id: Identifier
    limits: RunLimits
    created_at: UTCStamp

    @model_validator(mode="after")
    def bounded_caps(self) -> Self:
        _sqlite_limits(self.limits)
        return self


class RunRecord(DomainModel):
    run_id: Identifier
    campaign_id: Identifier
    manifest: ExecutionManifest
    created_at: UTCStamp
    status: Literal["created", "running", "completed", "failed", "cancelled"] = "created"

    @model_validator(mode="after")
    def bounded_caps(self) -> Self:
        _sqlite_limits(self.manifest.run_config.limits)
        return self


class DecisionRecord(DomainModel):
    run_id: Identifier
    decision_id: Identifier
    observation: PlayerObservation
    context: DecisionContext | None = None
    status: Literal["preparing", "prepared", "completed"] = "preparing"
    outcome: DecisionOutcome | None = None

    @model_validator(mode="after")
    def bound_outcome(self) -> Self:
        if self.observation.revision > 2**63 - 1:
            raise ValueError("revision exceeds SQLite's integer range")
        if self.observation.status != "in_progress" or self.observation.player != self.observation.active_player:
            raise ValueError("decision requires the active player's public observation")
        if self.context is not None:
            if (self.context.decision_id != self.decision_id
                    or canonical_bytes(self.context.observation) != canonical_bytes(self.observation)):
                raise ValueError("context differs from decision scope/observation")
        if self.status == "prepared" and self.context is None:
            raise ValueError("prepared decision requires its frozen context")
        if self.status == "preparing" and self.context is not None:
            raise ValueError("preparing decision cannot already contain a context")
        if (self.status == "completed") != (self.outcome is not None):
            raise ValueError("completed decisions require their outcome")
        if self.outcome is not None:
            outcome, context = self.outcome, self.context
            if outcome.decision_id != self.decision_id or outcome.observation_id != self.observation.observation_id:
                raise ValueError("outcome differs from the persisted decision scope")
            if context is None:
                if outcome.status != "aborted" or outcome.trusted_pool or outcome.visible_bindings or outcome.evidence:
                    raise ValueError("unprepared decision can only abort without a candidate pool")
                return self
            if (tuple(map(canonical_bytes, outcome.trusted_pool)) != tuple(map(canonical_bytes, context.full_pool))
                    or outcome.visible_bindings != context.visible_bindings
                    or tuple(map(canonical_bytes, outcome.evidence)) != tuple(map(canonical_bytes, context.evidence))):
                raise ValueError("outcome differs from the persisted decision snapshot")
            if (outcome.selection_origin == "static" and outcome.decision.candidate_id != context.fallback_candidate_id):
                raise ValueError("static fallback differs from verified context fallback")
            if outcome.decision is not None and outcome.decision.workflow != context.config.workflow:
                raise ValueError("outcome workflow differs from the persisted decision")
        return self


class AttemptRecord(DomainModel):
    run_id: Identifier
    decision_id: Identifier
    request: ModelRequest
    request_sha256: Sha256
    state: Literal["prepared", "dispatched", "replied", "unresolved"] = "prepared"
    reply: ModelReply | None = None

    @property
    def attempt_id(self) -> str:
        return self.request.attempt_id

    @model_validator(mode="after")
    def bound_reply(self) -> Self:
        if self.request_sha256 != hashlib.sha256(canonical_bytes(self.request)).hexdigest():
            raise ValueError("request hash does not match request")
        if self.decision_id != self.request.view.decision_id:
            raise ValueError("request belongs to another decision")
        if (self.state == "replied") != (self.reply is not None):
            raise ValueError("replied attempts require a reply")
        if self.reply is not None:
            if (self.reply.attempt_id != self.attempt_id or self.reply.decision_id != self.decision_id
                    or self.reply.observation_id != self.request.view.observation.observation_id
                    or self.reply.provider != self.request.model.provider):
                raise ValueError("reply belongs to another attempt/provider/observation")
        return self


class ReservationRecord(DomainModel):
    reservation_id: Identifier
    run_id: Identifier
    decision_id: Identifier
    kind: Literal["model", "native"]
    attempt_id: Identifier | None = None
    allocation: ResourceAmounts
    state: Literal["reserved", "dispatched", "settled", "unresolved", "released"] = "reserved"
    settlement: ResourceAmounts | None = None
    quote: CostQuote | None = None
    settlement_method: Identifier | None = None
    bound_exceeded: bool = False
    admission_session_id: Identifier | None = None
    native_limits: AnalysisLimits | None = None
    native_result: AnalysisResult | None = None
    native_request: ComparisonRequest | None = None
    native_dispatch_limits: AnalysisLimits | None = None

    @model_serializer(mode="wrap")
    def compatible_v1_defaults(self, handler):
        # Keep existing manifest/request/body hashes stable when new optional
        # accounting fields are unused. Non-default values remain identity-bearing.
        data = handler(self)
        if data.get("quote") is None:
            data.pop("quote", None)
        if data.get("settlement_method") is None:
            data.pop("settlement_method", None)
        if data.get("bound_exceeded") is False:
            data.pop("bound_exceeded", None)
        if data.get("admission_session_id") is None:
            data.pop("admission_session_id", None)
        if data.get("native_result") is None:
            data.pop("native_result", None)
        if data.get("native_limits") is None:
            data.pop("native_limits", None)
        for field in ("native_request", "native_dispatch_limits"):
            if data.get(field) is None:
                data.pop(field, None)
        return data

    @model_validator(mode="after")
    def consistent_allocation(self) -> Self:
        if self.native_request is not None:
            if (self.kind != "native" or self.native_limits != self.native_request.limits
                    or self.native_request.envelope.decision_id != self.decision_id):
                raise ValueError("native intent differs from reservation")
        if self.native_dispatch_limits is not None:
            limits = self.native_dispatch_limits
            if (self.kind != "native" or self.state not in {"dispatched", "settled", "unresolved"}
                    or self.native_limits is None or limits.work_unit != self.native_limits.work_unit
                    or limits.max_work_units != self.native_limits.max_work_units
                    or limits.max_elapsed_seconds > self.native_limits.max_elapsed_seconds):
                raise ValueError("dispatched native limits differ from allocation")
        if self.native_result is not None:
            if (self.kind != "native" or self.state != "settled" or self.settlement is None
                    or self.native_result.work_used.unit != "candidate_rollout"
                    or self.native_result.work_used.count != self.settlement.engine_work_units):
                raise ValueError("native result differs from settled work")
        if self.native_limits is not None:
            if (self.kind != "native" or self.native_limits.work_unit != "candidate_rollout"
                    or self.native_limits.max_work_units != self.allocation.engine_work_units):
                raise ValueError("native limits differ from allocation")
        if self.quote is not None:
            if (self.kind != "model" or self.quote.bound.purpose != "request"
                    or self.quote.api_cost_microusd != self.allocation.api_cost_microusd):
                raise ValueError("reservation quote differs from its model allocation")
        if self.settlement_method is not None and self.state != "settled":
            raise ValueError("settlement method requires settled facts")
        if (self.state == "settled") != (self.settlement is not None):
            raise ValueError("only settled reservations contain actual resource facts")
        amounts = (self.allocation,) + ((self.settlement,) if self.settlement is not None else ())
        if self.kind == "model":
            if self.attempt_id is None or self.allocation.model_calls != 1:
                raise ValueError("model reservation requires an attempt and one call slot")
            if any(a.engine_work_units or a.tool_steps for a in amounts):
                raise ValueError("model reservation cannot contain native work")
            if self.settlement is not None and self.settlement.model_calls != 1:
                raise ValueError("dispatched model attempt consumes its call slot")
        else:
            if self.attempt_id is not None or self.allocation.engine_work_units < 1 or self.allocation.tool_steps != 1:
                raise ValueError("native reservation requires work and one tool step")
            if any(a.api_cost_microusd or a.model_calls for a in amounts):
                raise ValueError("native reservation cannot contain model cost/calls")
            if self.settlement is not None and self.settlement.tool_steps != 1:
                raise ValueError("dispatched native tool consumes its step")
        # Actual usage may exceed an estimate. Persist it; the ledger blocks later admission.
        return self


class CommitRecord(DomainModel):
    run_id: Identifier
    decision_id: Identifier
    game_id: Identifier
    expected_revision: SQLiteCount
    move: Move
    source_checkpoint_id: Identifier
    new_revision: SQLiteCount | None = None
    checkpoint_id: Identifier | None = None

    @model_validator(mode="after")
    def consecutive_confirmation(self) -> Self:
        if (self.new_revision is None) != (self.checkpoint_id is None):
            raise ValueError("confirmation requires both revision and checkpoint identity")
        if self.new_revision is not None:
            if self.new_revision != self.expected_revision + 1:
                raise ValueError("confirmed revision must follow the expected revision")
            if self.checkpoint_id == self.source_checkpoint_id:
                raise ValueError("confirmation requires a new checkpoint identity")
        return self


class FinishHoldRecord(DomainModel):
    """One durable future selection allowance per decision, never dispatched itself."""
    hold_id: Identifier
    run_id: Identifier
    decision_id: Identifier
    quote: 'CostQuote'
    state: Literal["held", "consumed", "released"] = "held"
    consumed_attempt_id: Identifier | None = None

    @model_validator(mode="after")
    def selection_only(self) -> Self:
        if self.quote.bound.purpose != "final_selection":
            raise ValueError("finish allowance requires a future-selection bound")
        if (self.state == "consumed") != (self.consumed_attempt_id is not None):
            raise ValueError("only a consumed hold links to its selection attempt")
        return self


class BudgetClockRecord(DomainModel):
    clock_id: Identifier
    scope: Literal["campaign", "run", "decision"]
    campaign_id: Identifier
    run_id: Identifier | None = None
    decision_id: Identifier | None = None
    started_wall_ns: SQLiteCount
    observed_wall_ns: SQLiteCount
    elapsed_ns: SQLiteCount = 0
    limit_ns: Annotated[int, Field(gt=0, le=2**63 - 1)]
    state: Literal["active", "expired"] = "active"

    @model_validator(mode="after")
    def scoped_clock(self) -> Self:
        if ((self.scope != "campaign") != (self.run_id is not None)
                or (self.scope == "decision") != (self.decision_id is not None)):
            raise ValueError("clock scope and owner identities differ")
        if self.observed_wall_ns < self.started_wall_ns:
            raise ValueError("clock watermark cannot precede start")
        if self.elapsed_ns >= self.limit_ns and self.state != "expired":
            raise ValueError("elapsed clock must be expired")
        return self
