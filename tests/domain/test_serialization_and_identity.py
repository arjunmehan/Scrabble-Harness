import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import TypeAdapter, ValidationError

from conftest import replace_validated
from scrabble_harness.domain import (
    Board, BoardTile, Candidate, CandidateProvenance, Checkpoint, ExchangeMove,
    Move, ObservationData, PassMove, PlacementMove, PlayerObservation,
    TileInventory, build_candidate, build_observation, canonical_bytes, canonical_json,
)


def observation_with(observation, **updates):
    data = observation.model_dump(mode="python", exclude={"observation_id"})
    data.update(updates)
    return PlayerObservation.from_data(ObservationData.model_validate(data))


def make_candidate(observation, move, **updates):
    metadata = dict(
        exact_score=3 if isinstance(move, PlacementMove) else 0,
        leave=(), provenance=CandidateProvenance(source="fixture", configuration_id="v1"),
    )
    metadata.update(updates)
    return build_candidate(observation, move, **metadata)


def test_equivalent_observation_collections_have_identical_bytes_and_ids(observation):
    board = replace_validated(observation.board, tiles=observation.board.tiles[::-1])
    inventory = replace_validated(observation.tile_distribution, counts=observation.tile_distribution.counts[::-1])
    unseen = replace_validated(observation.unseen_tile_counts, counts=observation.unseen_tile_counts.counts[::-1])
    turn = observation.public_history[0]
    turn = replace_validated(turn, move=PlacementMove(tiles=turn.move.tiles[::-1]))
    reordered = observation_with(
        observation, board=board, own_rack=observation.own_rack[::-1],
        tile_distribution=inventory, unseen_tile_counts=unseen, public_history=(turn,),
    )
    assert reordered.observation_id == observation.observation_id
    assert canonical_bytes(reordered) == canonical_bytes(observation)


@pytest.mark.parametrize("updates", [
    {"game_id": "other-game"}, {"revision": 2}, {"player": 1}, {"active_player": 0},
    {"ruleset_id": "other-rules"}, {"lexicon_id": "other-lexicon"},
    {"scoreless_turns": 1}, {"public_history": ()},
    {"scores": (3, 0), "public_history": ()},
    {"opponent_rack_size": 6, "bag_size": 85},
    {"status": "finished", "terminal_reason": "scoreless_limit"},
])
def test_relevant_public_context_changes_observation_identity(observation, updates):
    changed = observation_with(observation, **updates)
    assert changed.observation_id != observation.observation_id


def test_board_position_and_blank_assignment_affect_identity(observation):
    tile = observation.board.tiles[0]
    for changed_tile in (
        replace_validated(tile, row=6),
        replace_validated(tile, displayed_letter="A"),
        replace_validated(tile, is_blank=False),
    ):
        board = Board(tiles=(changed_tile, observation.board.tiles[1]))
        unseen = observation.tile_distribution.without(board.physical_tiles + observation.own_rack)
        changed = observation_with(observation, board=board, unseen_tile_counts=unseen)
        assert changed.observation_id != observation.observation_id


def test_rack_composition_and_distribution_affect_identity(observation):
    rack = ("Q",) + observation.own_rack[1:]
    changed = observation_with(
        observation, own_rack=rack,
        unseen_tile_counts=observation.tile_distribution.without(observation.board.physical_tiles + rack),
    )
    assert changed.observation_id != observation.observation_id
    counter = observation.tile_distribution.as_counter()
    counter["A"] -= 1
    counter["Q"] += 1
    inventory = TileInventory.from_tiles(tuple(counter.elements()))
    changed = observation_with(
        observation, tile_distribution=inventory,
        unseen_tile_counts=inventory.without(observation.board.physical_tiles + observation.own_rack),
    )
    assert changed.observation_id != observation.observation_id


@pytest.mark.parametrize("move", [
    PassMove(), ExchangeMove(tiles=("A", "?", "A")),
    PlacementMove(tiles=(
        BoardTile(row=8, column=7, displayed_letter="Q", is_blank=True),
        BoardTile(row=8, column=8, displayed_letter="A"),
    )),
])
def test_candidate_serialization_round_trip_keeps_identity(observation, move):
    candidate = make_candidate(observation, move)
    restored = Candidate.model_validate_json(canonical_json(candidate))
    assert restored.candidate_id == candidate.candidate_id
    assert canonical_bytes(restored) == canonical_bytes(candidate)


def test_move_order_is_irrelevant_but_duplicate_exchange_counts_are_retained(observation):
    a = make_candidate(observation, ExchangeMove(tiles=("A", "?", "A")))
    b = make_candidate(observation, ExchangeMove(tiles=("?", "A", "A")))
    fewer = make_candidate(observation, ExchangeMove(tiles=("?", "A")))
    assert a.candidate_id == b.candidate_id
    assert a.candidate_id != fewer.candidate_id
    tiles = (
        BoardTile(row=8, column=7, displayed_letter="A"),
        BoardTile(row=8, column=8, displayed_letter="B"),
    )
    assert make_candidate(observation, PlacementMove(tiles=tiles)).candidate_id == make_candidate(
        observation, PlacementMove(tiles=tiles[::-1])
    ).candidate_id


def test_candidate_identity_includes_blank_physical_identity_and_observation(observation):
    ordinary = PlacementMove(tiles=(BoardTile(row=8, column=7, displayed_letter="Q"),))
    blank = PlacementMove(tiles=(BoardTile(row=8, column=7, displayed_letter="Q", is_blank=True),))
    a = make_candidate(observation, ordinary)
    assert a.candidate_id != make_candidate(observation, blank).candidate_id
    assert a.candidate_id != make_candidate(observation_with(observation, revision=2), ordinary).candidate_id


def test_score_estimates_and_provenance_do_not_change_action_identity(observation):
    move = PlacementMove(tiles=(BoardTile(row=8, column=7, displayed_letter="A"),))
    a = make_candidate(observation, move, leave=("B", "?"), static_equity=1.0)
    b = make_candidate(
        observation, move, exact_score=8, leave=("?", "B"), static_equity=2.0,
        provenance=CandidateProvenance(source="other", configuration_id="v2"),
    )
    assert a.candidate_id == b.candidate_id
    assert canonical_json(a) != canonical_json(b)


def test_private_checkpoint_canonicalization_preserves_draw_order_and_rng(state):
    checkpoint = Checkpoint(checkpoint_id="checkpoint", engine_commit="a" * 40, state=state)
    restored = Checkpoint.model_validate_json(canonical_json(checkpoint))
    assert restored.state.draw_state == state.draw_state
    assert canonical_bytes(restored) == canonical_bytes(checkpoint)
    alternate_draw = replace_validated(state.draw_state, ordered_tiles=state.draw_state.ordered_tiles[::-1])
    alternate = replace_validated(checkpoint, state=replace_validated(state, draw_state=alternate_draw))
    assert canonical_json(alternate) != canonical_json(checkpoint)
    assert build_observation(alternate.state, 0).observation_id == build_observation(state, 0).observation_id


def test_canonical_json_is_compact_and_rejects_unchecked_or_untyped_inputs(observation):
    serialized = canonical_json(observation)
    assert serialized == json.dumps(json.loads(serialized), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert canonical_bytes(observation) == serialized.encode("utf-8")
    with pytest.raises(TypeError):
        canonical_json(observation.model_dump())
    with pytest.raises(ValidationError, match="canonical public content"):
        canonical_json(observation.model_copy(update={"revision": 2}))


@pytest.mark.parametrize("updates", [
    {"observation_id": "obs_v1_" + "0" * 64}, {"revision": 2},
    {"observation_id": "obs_v2_" + "0" * 64},
])
def test_observation_json_rejects_forged_or_unsupported_ids(observation, updates):
    data = observation.model_dump(mode="json")
    data.update(updates)
    with pytest.raises(ValidationError):
        PlayerObservation.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("updates", [
    {"candidate_id": "cand_v1_" + "0" * 64},
    {"observation_id": "obs_v1_" + "0" * 64},
    {"move": {"kind": "exchange", "tiles": ["A"]}},
])
def test_candidate_json_rejects_identity_payload_mismatches(candidate, updates):
    data = candidate.model_dump(mode="json")
    data.update(updates)
    with pytest.raises(ValidationError, match="candidate ID"):
        Candidate.model_validate_json(json.dumps(data))


def test_fixture_identity_vectors_remain_stable(observation):
    path = Path(__file__).parents[1] / "fixtures" / "identity-v1-vectors.json"
    vectors = json.loads(path.read_text())
    assert observation.observation_id == vectors["observation_id"]
    for vector in vectors["candidates"]:
        move = TypeAdapter(Move).validate_json(json.dumps(vector["move"]))
        assert make_candidate(observation, move).candidate_id == vector["candidate_id"]


def test_identity_is_independent_of_python_hash_randomization(state, observation):
    fixture = Path(__file__).parents[1] / "fixtures" / "opening-blank-v1.json"
    script = (
        "from pathlib import Path; import sys; "
        "from scrabble_harness.domain import GameState, build_observation; "
        "print(build_observation(GameState.model_validate_json(Path(sys.argv[1]).read_text()), 0).observation_id)"
    )
    for seed in ("1", "725"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(fixture)],
            env={**os.environ, "PYTHONHASHSEED": seed}, check=True, capture_output=True, text=True,
        )
        assert result.stdout.strip() == observation.observation_id
