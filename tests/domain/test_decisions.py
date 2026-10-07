import random

import pytest
from pydantic import ValidationError

from conftest import replace_validated
from scrabble_harness.domain import (
    AgentDecision, CandidateProvenance, DecisionValidationError, DrawState,
    ExchangeMove, PassMove, StaleDecisionError, UnknownCandidateError,
    build_candidate, build_observation, validate_decision,
)


def resolve(state, observation, candidate, decision, **updates):
    arguments = dict(state=state, observation=observation, candidates=(candidate,), expected_revision=state.revision)
    arguments.update(updates)
    return validate_decision(decision, **arguments)


def test_current_selection_resolves_without_mutating_state(state, active_observation, candidate, decision):
    before = state.model_dump_json()
    assert resolve(state, active_observation, candidate, decision) == candidate
    assert state.model_dump_json() == before


def test_old_expected_revision_is_rejected(state, active_observation, candidate, decision):
    with pytest.raises(StaleDecisionError, match="revision"):
        resolve(state, active_observation, candidate, decision, expected_revision=state.revision - 1)


@pytest.mark.parametrize("revision", [True, "1", -1])
def test_expected_revision_is_strict(state, active_observation, candidate, decision, revision):
    with pytest.raises(ValidationError):
        resolve(state, active_observation, candidate, decision, expected_revision=revision)


@pytest.mark.parametrize("updates", [
    {"revision": 2}, {"game_id": "other-game"}, {"active_player": 0},
    {"ruleset_id": "other-rules"}, {"lexicon_id": "other-lexicon"},
])
def test_old_context_is_rejected_even_when_caller_supplies_current_revision(
    state, active_observation, candidate, decision, updates,
):
    changed = replace_validated(state, **updates)
    with pytest.raises(StaleDecisionError, match="observation"):
        resolve(changed, active_observation, candidate, decision)


def test_old_decision_is_rejected_with_a_fresh_observation_and_pool(state, candidate, decision):
    changed = replace_validated(state, revision=2)
    fresh = build_observation(changed, changed.active_player)
    new_candidate = build_candidate(
        fresh, PassMove(), exact_score=0, leave=fresh.own_rack, provenance=candidate.provenance,
    )
    with pytest.raises(StaleDecisionError, match="decision"):
        resolve(changed, fresh, new_candidate, decision)


def test_inactive_player_observation_cannot_submit(state, observation):
    candidate = build_candidate(
        observation, PassMove(), exact_score=0, leave=observation.own_rack,
        provenance=CandidateProvenance(source="fixture", configuration_id="v1"),
    )
    decision = AgentDecision(
        observation_id=observation.observation_id, candidate_id=candidate.candidate_id, workflow="fixture",
    )
    with pytest.raises(StaleDecisionError, match="active player"):
        resolve(state, observation, candidate, decision)


def test_unknown_selection_is_rejected(state, active_observation, candidate):
    outside_pool = build_candidate(
        active_observation, ExchangeMove(tiles=("G",)), exact_score=0,
        leave=active_observation.own_rack[1:], provenance=candidate.provenance,
    )
    unknown = AgentDecision(
        observation_id=active_observation.observation_id,
        candidate_id=outside_pool.candidate_id, workflow="fixture",
    )
    with pytest.raises(UnknownCandidateError):
        resolve(state, active_observation, candidate, unknown)


def test_mixed_contexts_and_duplicate_pool_ids_are_rejected(state, active_observation, candidate, decision):
    fresh = build_observation(replace_validated(state, revision=2), state.active_player)
    foreign = build_candidate(fresh, PassMove(), exact_score=0, leave=(), provenance=candidate.provenance)
    with pytest.raises(StaleDecisionError, match="candidate pool"):
        resolve(state, active_observation, candidate, decision, candidates=(candidate, foreign))
    revised_metadata = replace_validated(candidate, static_equity=5.0)
    with pytest.raises(DecisionValidationError, match="duplicate IDs"):
        resolve(state, active_observation, candidate, decision, candidates=(candidate, revised_metadata))


def test_unchecked_payload_changes_are_revalidated(state, active_observation, candidate, decision):
    tampered_candidate = candidate.model_copy(update={"move": ExchangeMove(tiles=("G",))})
    with pytest.raises(ValidationError, match="candidate ID"):
        resolve(state, active_observation, candidate, decision, candidates=(tampered_candidate,))
    tampered_observation = active_observation.model_copy(update={"revision": 2})
    with pytest.raises(ValidationError, match="observation ID"):
        resolve(state, tampered_observation, candidate, decision)


def test_finished_game_rejects_selection(state, active_observation, candidate, decision):
    finished = replace_validated(state, status="finished", terminal_reason="scoreless_limit")
    with pytest.raises(DecisionValidationError, match="finished game"):
        resolve(finished, active_observation, candidate, decision)


def test_hidden_state_changes_cannot_change_selection_validation(state, active_observation, candidate, decision):
    hidden = list(state.racks[0] + state.draw_state.ordered_tiles)
    random.Random(19).shuffle(hidden)
    changed = replace_validated(
        state, racks=(tuple(hidden[:7]), state.racks[1]),
        draw_state=DrawState(ordered_tiles=tuple(hidden[7:]), rng_algorithm="other", rng_state="other-state"),
    )
    assert resolve(changed, active_observation, candidate, decision) == candidate


def test_selection_checked_again_after_revision_advance_is_rejected(state, active_observation, candidate, decision):
    assert resolve(state, active_observation, candidate, decision) == candidate
    advanced = replace_validated(state, revision=2)
    with pytest.raises(StaleDecisionError, match="revision"):
        resolve(advanced, active_observation, candidate, decision, expected_revision=state.revision)
