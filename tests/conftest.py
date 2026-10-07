"""Small, complete positions with the standard physical tile distribution."""

from pathlib import Path

import pytest

from scrabble_harness.domain import (
    AgentDecision, Candidate, CandidateProvenance, GameState, PassMove,
    PlayerObservation, build_candidate, build_observation,
)


@pytest.fixture
def state() -> GameState:
    path = Path(__file__).parent / "fixtures" / "opening-blank-v1.json"
    return GameState.model_validate_json(path.read_text())


@pytest.fixture
def observation(state: GameState) -> PlayerObservation:
    return build_observation(state, 0)


@pytest.fixture
def active_observation(state: GameState) -> PlayerObservation:
    return build_observation(state, state.active_player)


@pytest.fixture
def candidate(active_observation: PlayerObservation) -> Candidate:
    return build_candidate(
        active_observation, PassMove(), exact_score=0, leave=active_observation.own_rack,
        provenance=CandidateProvenance(source="fixture", configuration_id="fixture-v1"),
    )


@pytest.fixture
def decision(candidate: Candidate) -> AgentDecision:
    return AgentDecision(
        observation_id=candidate.observation_id, candidate_id=candidate.candidate_id,
        workflow="fixture",
    )


def replace_validated(record, **updates):
    """Revalidate updates instead of using Pydantic's unchecked model_copy."""
    data = record.model_dump(mode="python")
    data.update(updates)
    return type(record).model_validate(data)
