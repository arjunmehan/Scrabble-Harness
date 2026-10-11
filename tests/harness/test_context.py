import asyncio

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import Board, ExchangeMove, PassMove, build_observation, canonical_bytes
from scrabble_harness.harness import (
    ContextPreparationError, ContextScopeError, DecisionContext, HarnessConfig,
    UnknownHandleError, prepare_decision_context,
)
from conftest import RecordingAnalysis, update


def test_preparation_generates_once_and_previews_the_static_fallback(context, recording_analysis):
    assert [call if isinstance(call, str) else call[0] for call in recording_analysis.calls] == ["load", "generate", "preview"]
    assert recording_analysis.calls[1][1].pool_size == 40
    assert len(context.visible_bindings) == 12
    best = max(context.full_pool, key=lambda candidate: candidate.static_equity)
    assert context.fallback_candidate_id == best.candidate_id
    assert context.fallback_preview.move == best.move
    visible_ids = {binding.candidate_id for binding in context.visible_bindings}
    visible = tuple(candidate for candidate in context.full_pool if candidate.candidate_id in visible_ids)
    assert best in visible
    assert any(isinstance(candidate.move, PassMove) for candidate in visible)
    assert any(isinstance(candidate.move, ExchangeMove) for candidate in visible)
    assert DecisionContext.model_validate_json(context.model_dump_json()) == context


def test_order_is_seeded_and_independent_of_pool_order_workflow_and_decision(context):
    fields = dict(observation=context.observation, config=context.config, candidate_order_seed=123,
                  candidates=context.full_pool, fallback_preview=context.fallback_preview)
    paired = DecisionContext.from_candidates(decision_id="decision-b", **(fields | {
        "config": update(context.config, workflow="deep"), "candidates": tuple(reversed(context.full_pool))}))
    assert paired.visible_bindings == context.visible_bindings
    different = DecisionContext.from_candidates(decision_id="decision-c", **(fields | {"candidate_order_seed": 124}))
    assert different.visible_bindings != context.visible_bindings
    assert {binding.candidate_id for binding in different.visible_bindings} == {binding.candidate_id for binding in context.visible_bindings}


def test_equal_equity_fallback_ties_use_canonical_identity(context):
    tied = tuple(update(candidate, static_equity=1.0) for candidate in context.full_pool)
    best = min(tied, key=lambda candidate: candidate.candidate_id)
    engine = RecordingAnalysis(context.observation, tied)
    first = asyncio.run(prepare_decision_context(context.observation, engine,
                       decision_id="tied", candidate_order_seed=1))
    assert first.fallback_candidate_id == best.candidate_id


def test_model_view_is_explicit_public_projection(context):
    view = context.model_view()
    encoded = view.model_dump_json()
    for forbidden in ("static_equity", "provenance", "full_pool", "fallback", "racks", "draw_state", "rng_state"):
        assert f'"{forbidden}"' not in encoded
    assert len(view.candidates) == 12 and view.observation == context.observation
    for binding, candidate in zip(context.visible_bindings, view.candidates, strict=True):
        resolved = context.resolve_handle(binding.handle, decision_id=context.decision_id,
                                          observation_id=context.observation.observation_id)
        assert canonical_bytes(candidate.move) == canonical_bytes(resolved.move)
        assert candidate.exact_score == resolved.exact_score
        assert candidate.leave == resolved.leave


def test_private_draw_and_opponent_changes_cannot_change_preparation(state, synthetic_pool):
    # Active player is 1. Swap hidden player 0's first tile with the bag's tail.
    bag = state.draw_state.ordered_tiles
    hidden = state.racks[0]
    alternate = update(state, racks=((bag[-1],) + hidden[1:], state.racks[1]),
                       draw_state=update(state.draw_state, ordered_tiles=bag[:-1] + (hidden[0],),
                                         rng_state="different-private-rng"))
    assert alternate.racks != state.racks
    assert canonical_bytes(build_observation(alternate, 1)) == canonical_bytes(build_observation(state, 1))
    views = []
    for private in (state, alternate):
        observation = build_observation(private, 1)
        engine = RecordingAnalysis(observation, synthetic_pool)
        prepared = asyncio.run(prepare_decision_context(observation, engine,
                               decision_id="same-public-decision", candidate_order_seed=123))
        views.append(canonical_bytes(prepared.model_view()))
    assert views[0] == views[1]


def test_handles_are_scoped_and_invisible_actions_cannot_be_addressed(context):
    scope = dict(decision_id=context.decision_id, observation_id=context.observation.observation_id)
    assert context.resolve_handle("c01", **scope).candidate_id == context.visible_bindings[0].candidate_id
    with pytest.raises(ContextScopeError):
        context.resolve_handle("c01", **(scope | {"decision_id": "previous-decision"}))
    with pytest.raises(ContextScopeError):
        context.resolve_handle("c01", **(scope | {"observation_id": "obs_v1_" + "a" * 64}))
    with pytest.raises(UnknownHandleError):
        context.resolve_handle("c13", **scope)
    invisible = next(candidate for candidate in context.full_pool
                     if candidate.candidate_id not in {binding.candidate_id for binding in context.visible_bindings})
    with pytest.raises(ValidationError):
        context.resolve_handle(invisible.candidate_id, **scope)


def test_boundary_rejects_private_state_or_bad_scope_before_engine_calls(state, recording_analysis):
    for value, seed in ((state, 1), (recording_analysis.observation, True)):
        with pytest.raises(ValidationError):
            asyncio.run(prepare_decision_context(value, recording_analysis,
                        decision_id="decision-a", candidate_order_seed=seed))
    assert recording_analysis.calls == []


@pytest.mark.parametrize("problem", ["empty", "duplicate", "missing_equity", "wrong_leave", "wrong_observation", "too_large"])
def test_invalid_candidate_pool_aborts_before_fallback_preview(recording_analysis, problem):
    pool = recording_analysis.pool
    if problem == "empty":
        pool = ()
    elif problem == "duplicate":
        pool += (pool[0],)
    elif problem == "missing_equity":
        pool = (update(pool[0], static_equity=None),) + pool[1:]
    elif problem == "wrong_leave":
        pool = (update(pool[0], leave=()),) + pool[1:]
    elif problem == "wrong_observation":
        # Unchecked copy must also be revalidated, not accepted as already trusted.
        pool = (pool[0].model_copy(update={"observation_id": "obs_v1_" + "a" * 64}),) + pool[1:]
    else:
        pool *= 2
    recording_analysis.pool = pool
    with pytest.raises(ContextPreparationError):
        asyncio.run(prepare_decision_context(recording_analysis.observation, recording_analysis,
                    decision_id="decision-a", candidate_order_seed=1))
    assert len(recording_analysis.calls) == 2


@pytest.mark.parametrize("problem", ["score", "board", "observation"])
def test_inconsistent_preview_aborts(context, recording_analysis, problem):
    preview = context.fallback_preview
    changes = {"score": {"exact_score": preview.exact_score + 1}, "board": {"board_after": Board()},
               "observation": {"observation_id": "obs_v1_" + "a" * 64}}[problem]
    recording_analysis.preview_override = update(preview, **changes)
    with pytest.raises(ContextPreparationError):
        asyncio.run(prepare_decision_context(context.observation, recording_analysis,
                    decision_id="decision-b", candidate_order_seed=1))


def test_engine_failure_wrapped_and_cancellation_propagated(recording_analysis):
    recording_analysis.failure = RuntimeError("engine failed")
    with pytest.raises(ContextPreparationError) as error:
        asyncio.run(prepare_decision_context(recording_analysis.observation, recording_analysis,
                    decision_id="decision-a", candidate_order_seed=1))
    assert isinstance(error.value.__cause__, RuntimeError)
    recording_analysis.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(prepare_decision_context(recording_analysis.observation, recording_analysis,
                    decision_id="decision-a", candidate_order_seed=1))


def test_impossible_display_ceiling_and_unchecked_context_fail(context, recording_analysis):
    configuration = update(context.config, candidate_config=update(context.config.candidate_config, display_count=2))
    with pytest.raises(ContextPreparationError):
        asyncio.run(prepare_decision_context(context.observation, recording_analysis,
                    decision_id="decision-b", candidate_order_seed=1, config=configuration))
    corrupt = context.model_copy(update={"fallback_candidate_id": context.full_pool[-1].candidate_id})
    with pytest.raises(ValidationError):
        corrupt.model_view()
    with pytest.raises(ValidationError):
        update(context, presentation_identity_version=True)
