"""One immutable candidate snapshot and its decision-scoped evidence registry.

The mutable engine context must belong to one active decision at a time.
Only model_view()/evidence_views() are provider inputs, never model_dump() of
this host record. This module prepares data; it does not dispatch model calls,
execute comparison tools, enforce a ledger, or authorize a game commit.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Self, TYPE_CHECKING

from pydantic import Field, TypeAdapter, model_validator

from ..domain import (
    AgentDecision, AnalysisLimits, AnalysisResult, Board, Candidate, CandidateConfig,
    ExchangeMove, MovePreview, PassMove, PlacementMove, PlayerObservation,
    canonical_bytes,
)
from ..domain._base import CandidateId, DomainModel, Identifier, NonNegativeInt, ObservationId
from .contracts import (
    CandidateBinding, CandidateHandle, CompareAction, ComparisonEvidence, DecisionView,
    EvidenceView, HarnessConfig, ScopedAction, SelectAction, VisibleCandidate,
    VisibleEvaluation, check_candidate_inventory,
)
from .errors import (
    ContextPreparationError, ContextScopeError, EvidenceValidationError, UnknownHandleError,
)

if TYPE_CHECKING:
    from ..engine import AnalysisEngine


def _check_observation(observation: PlayerObservation) -> PlayerObservation:
    observation = PlayerObservation.model_validate(observation)
    if observation.status != "in_progress" or observation.player != observation.active_player:
        raise ValueError("preparation requires the active player's in-progress observation")
    return observation


def _check_pool(pool: tuple[Candidate, ...], observation: PlayerObservation,
                configuration: CandidateConfig) -> tuple[Candidate, ...]:
    pool = TypeAdapter(tuple[Candidate, ...], config={"strict": True}).validate_python(pool)
    if not pool or len(pool) > configuration.pool_size:
        raise ValueError("candidate pool is empty or exceeds its configured ceiling")
    if len({candidate.candidate_id for candidate in pool}) != len(pool):
        raise ValueError("candidate pool contains duplicate actions")
    for candidate in pool:
        if candidate.observation_id != observation.observation_id:
            raise ValueError("candidate pool belongs to another observation")
        if candidate.static_equity is None:
            raise ValueError("static equity is required to rank a trusted candidate pool")
        if isinstance(candidate.move, PassMove) and not configuration.include_pass:
            raise ValueError("candidate pool violates the pass filter")
        if isinstance(candidate.move, ExchangeMove) and not configuration.include_exchanges:
            raise ValueError("candidate pool violates the exchange filter")
        check_candidate_inventory(observation, candidate.move, candidate.leave)
    return pool


def _ranked(pool: tuple[Candidate, ...]) -> tuple[Candidate, ...]:
    # Metadata ranks actions, while the canonical ID breaks equal-equity ties.
    return tuple(sorted(pool, key=lambda candidate: (-candidate.static_equity, candidate.candidate_id)))


def _shuffle_key(candidate: Candidate, seed: int) -> tuple[bytes, str]:
    # Versioned hash permutation avoids dependence on random.shuffle internals.
    # Deliberately exclude decision/workflow IDs so paired policies share order.
    payload = {"presentation_version": 1, "seed": seed, "candidate_id": candidate.candidate_id}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).digest(), candidate.candidate_id


def _presentation(pool: tuple[Candidate, ...], configuration: CandidateConfig,
                  seed: int) -> tuple[CandidateBinding, ...]:
    ranked = _ranked(pool)
    reserved = {ranked[0].candidate_id}
    if configuration.include_pass:
        candidate = next((candidate for candidate in ranked if isinstance(candidate.move, PassMove)), None)
        if candidate is not None:
            reserved.add(candidate.candidate_id)
    if configuration.include_exchanges:
        candidate = next((candidate for candidate in ranked if isinstance(candidate.move, ExchangeMove)), None)
        if candidate is not None:
            reserved.add(candidate.candidate_id)
    if len(reserved) > configuration.display_count:
        raise ValueError("display ceiling cannot hold fallback/pass/exchange reservations")
    selected = set(reserved)
    for candidate in ranked:
        if len(selected) >= configuration.display_count:
            break
        selected.add(candidate.candidate_id)
    visible = sorted((candidate for candidate in ranked if candidate.candidate_id in selected),
                     key=lambda candidate: _shuffle_key(candidate, seed))
    return tuple(CandidateBinding(handle=f"c{index:02d}", candidate_id=candidate.candidate_id)
                 for index, candidate in enumerate(visible, 1))


def _check_fallback_preview(observation: PlayerObservation, fallback: Candidate,
                            preview: MovePreview) -> None:
    if (preview.observation_id != observation.observation_id
            or canonical_bytes(preview.move) != canonical_bytes(fallback.move)
            or preview.exact_score != fallback.exact_score
            or sorted(preview.leave) != sorted(fallback.leave)):
        raise ValueError("fallback preview differs from its trusted candidate")
    expected = observation.board
    if isinstance(fallback.move, PlacementMove):
        expected = Board(tiles=observation.board.tiles + fallback.move.tiles)
    if canonical_bytes(preview.board_after) != canonical_bytes(expected):
        raise ValueError("fallback preview returned inconsistent board effects")


class DecisionContext(DomainModel):
    """Host-owned snapshot. Evidence updates return a new context, never mutate it."""
    presentation_identity_version: Annotated[int, Field(ge=1, le=1)] = 1
    decision_id: Identifier
    observation: PlayerObservation
    config: HarnessConfig
    candidate_order_seed: NonNegativeInt
    full_pool: Annotated[tuple[Candidate, ...], Field(min_length=1, max_length=10_000)]
    visible_bindings: Annotated[tuple[CandidateBinding, ...], Field(min_length=1, max_length=10_000)]
    fallback_candidate_id: CandidateId
    fallback_preview: MovePreview
    evidence: Annotated[tuple[ComparisonEvidence, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def consistent_snapshot(self) -> Self:
        observation = _check_observation(self.observation)
        pool = _check_pool(self.full_pool, observation, self.config.candidate_config)
        fallback = _ranked(pool)[0]
        if fallback.candidate_id != self.fallback_candidate_id:
            raise ValueError("fallback is not the best static candidate")
        expected = _presentation(pool, self.config.candidate_config, self.candidate_order_seed)
        if self.visible_bindings != expected:
            raise ValueError("visible bindings differ from the versioned presentation")
        _check_fallback_preview(observation, fallback, self.fallback_preview)
        analyses = set()
        for index, item in enumerate(self.evidence, 1):
            if item.evidence_id != f"e{index:02d}":
                raise ValueError("issued evidence handles must be consecutive")
            if item.decision_id != self.decision_id or item.observation_id != observation.observation_id:
                raise ValueError("issued evidence belongs to another decision")
            if any(binding not in self.visible_bindings for binding in item.requested):
                raise ValueError("issued evidence contains an invisible or remapped handle")
            tier = next((tier for tier in self.config.comparison_tiers if tier.name == item.tier), None)
            if (tier is None or item.limits.max_work_units > tier.max_work_units
                    or item.limits.max_elapsed_seconds > tier.max_elapsed_seconds):
                raise ValueError("issued evidence exceeds its configured tier")
            if item.result.analysis_id in analyses:
                raise ValueError("an analysis cannot be issued twice")
            analyses.add(item.result.analysis_id)
        return self

    @classmethod
    def from_candidates(cls, *, decision_id: str, observation: PlayerObservation,
                        config: HarnessConfig, candidate_order_seed: int,
                        candidates: tuple[Candidate, ...], fallback_preview: MovePreview) -> DecisionContext:
        """Import a trusted generated pool plus engine preview, not a model-owned pool."""
        observation = _check_observation(observation)
        config = HarnessConfig.model_validate(config)
        seed = TypeAdapter(NonNegativeInt, config={"strict": True}).validate_python(candidate_order_seed)
        pool = _check_pool(candidates, observation, config.candidate_config)
        return cls(decision_id=decision_id, observation=observation, config=config,
                   candidate_order_seed=seed, full_pool=pool,
                   visible_bindings=_presentation(pool, config.candidate_config, seed),
                   fallback_candidate_id=_ranked(pool)[0].candidate_id,
                   fallback_preview=fallback_preview)

    def _checked(self) -> DecisionContext:
        return type(self).model_validate(self)

    def _scope(self, decision_id: str, observation_id: str) -> None:
        if decision_id != self.decision_id or observation_id != self.observation.observation_id:
            raise ContextScopeError("action belongs to a different decision or observation")

    @property
    def comparison_available(self) -> bool:
        return (self.observation.bag_size >= 7 and self.observation.opponent_rack_size == 7
                and len(self.visible_bindings) >= 2)

    def model_view(self) -> DecisionView:
        context = self._checked()
        pool = {candidate.candidate_id: candidate for candidate in context.full_pool}
        visible = tuple(VisibleCandidate(handle=binding.handle, move=pool[binding.candidate_id].move,
                                         exact_score=pool[binding.candidate_id].exact_score,
                                         leave=pool[binding.candidate_id].leave)
                        for binding in context.visible_bindings)
        return DecisionView(decision_id=context.decision_id, observation=context.observation, candidates=visible)

    def resolve_handle(self, handle: str, *, decision_id: str, observation_id: str) -> Candidate:
        context = self._checked()
        context._scope(decision_id, observation_id)
        handle = TypeAdapter(CandidateHandle, config={"strict": True}).validate_python(handle)
        binding = next((binding for binding in context.visible_bindings if binding.handle == handle), None)
        if binding is None:
            raise UnknownHandleError("candidate handle is not visible in this decision")
        return next(candidate for candidate in context.full_pool if candidate.candidate_id == binding.candidate_id)

    def resolve_comparison(self, envelope: ScopedAction) -> tuple[Candidate, ...]:
        context = self._checked()
        envelope = ScopedAction.model_validate(envelope)
        context._scope(envelope.decision_id, envelope.observation_id)
        if not isinstance(envelope.action, CompareAction):
            raise EvidenceValidationError("comparison requires a compare action")
        if not context.comparison_available:
            raise EvidenceValidationError("comparison is unavailable in this public position")
        if envelope.action.tier not in {tier.name for tier in context.config.comparison_tiers}:
            raise EvidenceValidationError("comparison tier is not configured")
        return tuple(context.resolve_handle(handle, decision_id=envelope.decision_id,
                                            observation_id=envelope.observation_id)
                     for handle in envelope.action.candidate_handles)

    def issue_evidence(self, envelope: ScopedAction, result: AnalysisResult, *,
                       limits: AnalysisLimits, seed: int) -> tuple[DecisionContext, ComparisonEvidence]:
        """Register an executed comparison; admission/tool execution belong to later tasks."""
        context = self._checked()
        envelope = ScopedAction.model_validate(envelope)
        context.resolve_comparison(envelope)
        result = AnalysisResult.model_validate(result)
        limits = AnalysisLimits.model_validate(limits)
        seed = TypeAdapter(NonNegativeInt, config={"strict": True}).validate_python(seed)
        tier = next(tier for tier in context.config.comparison_tiers if tier.name == envelope.action.tier)
        if limits.max_work_units > tier.max_work_units or limits.max_elapsed_seconds > tier.max_elapsed_seconds:
            raise EvidenceValidationError("comparison allowance exceeds its configured tier")
        requested = tuple(next(binding for binding in context.visible_bindings if binding.handle == handle)
                          for handle in envelope.action.candidate_handles)
        existing = next((item for item in context.evidence if item.result.analysis_id == result.analysis_id), None)
        try:
            item = ComparisonEvidence(evidence_id=existing.evidence_id if existing else f"e{len(context.evidence) + 1:02d}",
                decision_id=context.decision_id, observation_id=context.observation.observation_id,
                requested=requested, tier=envelope.action.tier, limits=limits, seed=seed, result=result)
        except ValueError as exc:
            raise EvidenceValidationError("comparison result is inconsistent with its request") from exc
        if existing is not None:
            if canonical_bytes(existing) != canonical_bytes(item):
                raise EvidenceValidationError("analysis identity already has different evidence")
            return context, existing
        updated = type(context).model_validate(context.model_dump(mode="python") | {"evidence": context.evidence + (item,)})
        return updated, item

    def evidence_views(self) -> tuple[EvidenceView, ...]:
        context = self._checked()
        projected = []
        for item in context.evidence:
            handles = {binding.candidate_id: binding.handle for binding in item.requested}
            result = item.result
            projected.append(EvidenceView(evidence_id=item.evidence_id, decision_id=context.decision_id,
                observation_id=context.observation.observation_id,
                requested_handles=tuple(binding.handle for binding in item.requested),
                tier=item.tier,
                values=tuple(VisibleEvaluation(candidate_handle=handles[value.candidate_id], value=value.value,
                                               standard_error=value.standard_error) for value in result.values),
                metric_definition=result.metric_definition, limits=item.limits, work_used=result.work_used,
                elapsed_time_seconds=result.elapsed_time_seconds, seed=result.seed,
                completion_status=result.completion_status))
        return tuple(projected)

    def select(self, envelope: ScopedAction) -> AgentDecision:
        context = self._checked()
        envelope = ScopedAction.model_validate(envelope)
        context._scope(envelope.decision_id, envelope.observation_id)
        if not isinstance(envelope.action, SelectAction):
            raise EvidenceValidationError("selection requires a select action")
        candidate = context.resolve_handle(envelope.action.candidate_handle, decision_id=envelope.decision_id,
                                           observation_id=envelope.observation_id)
        issued = {item.evidence_id for item in context.evidence}
        if not set(envelope.action.evidence_ids).issubset(issued):
            raise EvidenceValidationError("selection cites evidence not issued in this decision")
        return AgentDecision(observation_id=context.observation.observation_id,
            candidate_id=candidate.candidate_id, workflow=context.config.workflow,
            evidence_ids=envelope.action.evidence_ids, brief_note=envelope.action.brief_note)

    def fallback_decision(self, reason: str) -> AgentDecision:
        """Return the verified static fallback; alternate policies belong to the loop."""
        context = self._checked()
        return AgentDecision(observation_id=context.observation.observation_id,
            candidate_id=context.fallback_candidate_id, workflow=context.config.workflow, fallback_reason=reason)


async def prepare_decision_context(observation: PlayerObservation, analysis: AnalysisEngine, *,
                                   decision_id: str, candidate_order_seed: int,
                                   config: HarnessConfig = HarnessConfig()) -> DecisionContext:
    """Generate once and verify the static fallback, entirely from public facts.

    The caller owns the analysis adapter, including closing it. Cancellation
    propagates; engine/preparation failures raise before any model request.
    """
    observation = _check_observation(observation)
    config = HarnessConfig.model_validate(config)
    seed = TypeAdapter(NonNegativeInt, config={"strict": True}).validate_python(candidate_order_seed)
    decision_id = TypeAdapter(Identifier, config={"strict": True}).validate_python(decision_id)
    try:
        await analysis.load_observation(observation)
        pool = _check_pool(await analysis.generate_candidates(config.candidate_config),
                           observation, config.candidate_config)
        _presentation(pool, config.candidate_config, seed)
        preview = await analysis.preview(_ranked(pool)[0].move)
        return DecisionContext.from_candidates(decision_id=decision_id, observation=observation,
            config=config, candidate_order_seed=seed, candidates=pool, fallback_preview=preview)
    except Exception as exc:
        raise ContextPreparationError("failed to prepare a validated candidate context and fallback") from exc
