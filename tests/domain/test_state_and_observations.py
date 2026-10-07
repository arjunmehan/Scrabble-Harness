import json
import random
from collections import Counter

import pytest
from pydantic import ValidationError

from conftest import replace_validated
from scrabble_harness.domain import (
    Board, BoardTile, Checkpoint, DrawState, ExchangeMove, GameState, PassMove,
    PlayerObservation, PublicTurn, STANDARD_ENGLISH_INVENTORY, TileInventory,
    build_observation, public_move,
)


def test_private_state_and_checkpoint_round_trip_with_draw_state(state):
    checkpoint = Checkpoint(checkpoint_id="checkpoint", engine_commit="a" * 40, state=state)
    restored = Checkpoint.model_validate_json(checkpoint.model_dump_json())
    assert restored == checkpoint
    assert restored.state.draw_state.ordered_tiles == state.draw_state.ordered_tiles
    assert restored.state.draw_state.rng_state == "fixture-state-a"
    assert restored.state.board.tiles[0].physical_tile == "?"


def test_tile_loss_and_reclassification_of_a_blank_are_rejected(state):
    missing = replace_validated(state.draw_state, ordered_tiles=state.draw_state.ordered_tiles[1:])
    with pytest.raises(ValidationError, match="conserve"):
        replace_validated(state, draw_state=missing)
    ordinary_q = replace_validated(state.board.tiles[0], is_blank=False)
    with pytest.raises(ValidationError, match="conserve"):
        replace_validated(state, board=Board(tiles=(ordinary_q, state.board.tiles[1])))


def test_unseen_counts_are_opponent_rack_plus_bag_not_bag_composition(state, observation):
    expected = Counter(state.racks[1] + state.draw_state.ordered_tiles)
    assert observation.unseen_tile_counts.as_counter() == expected
    assert observation.unseen_tile_counts.total == observation.opponent_rack_size + observation.bag_size
    assert observation.unseen_tile_counts.as_counter() != Counter(state.draw_state.ordered_tiles)
    assert observation.unseen_tile_counts.as_counter()["?"] == 0
    assert observation.unseen_tile_counts.as_counter()["Q"] == 1
    assert PlayerObservation.model_validate_json(observation.model_dump_json()) == observation


@pytest.mark.parametrize("seed", range(20))
def test_hidden_repartitions_and_draw_changes_leave_observation_identical(state, observation, seed):
    hidden = list(state.racks[1] + state.draw_state.ordered_tiles)
    random.Random(seed).shuffle(hidden)
    alternate = replace_validated(
        state,
        racks=(state.racks[0], tuple(hidden[:7])),
        draw_state=DrawState(
            ordered_tiles=tuple(hidden[7:]), rng_algorithm="other-rng-v2",
            rng_state=f"private-alternate-state-{seed}",
        ),
    )
    visible = build_observation(alternate, 0)
    assert visible.model_dump_json() == observation.model_dump_json()


def test_both_player_perspectives_select_their_own_rack(state):
    for player in (0, 1):
        visible = build_observation(state, player)
        assert visible.own_rack == state.racks[player]
        assert visible.unseen_tile_counts.as_counter() == Counter(
            state.racks[1 - player] + state.draw_state.ordered_tiles
        )


def test_empty_bag_makes_the_unknown_rack_publicly_inferable(state):
    remaining = STANDARD_ENGLISH_INVENTORY.without(("Q", "?"))
    board = Board(tiles=tuple(
        BoardTile(
            row=index // 15, column=index % 15,
            displayed_letter="A" if tile == "?" else tile, is_blank=tile == "?",
        )
        for index, tile in enumerate(sorted(remaining.as_counter().elements()))
    ))
    endgame = replace_validated(
        state, board=board, racks=(("Q",), ("?",)), public_history=(),
        draw_state=DrawState(ordered_tiles=(), rng_algorithm="fixture", rng_state="state"),
    )
    visible = build_observation(endgame, 0)
    assert visible.bag_size == 0
    assert visible.unseen_tile_counts.as_counter() == Counter({"?": 1})


@pytest.mark.parametrize("player", [-1, 2, True, "0"])
def test_observation_builder_rejects_bad_player_indices(state, player):
    with pytest.raises(ValidationError):
        build_observation(state, player)


@pytest.mark.parametrize("field,value", [
    ("racks", [["Q"], ["Z"]]), ("draw_state", {"next_tile": "Z"}),
    ("opponent_rack", ["Q"]), ("rng_state", "secret"), ("bag_tiles", ["Z"]),
])
def test_observations_reject_private_fields_at_the_json_boundary(observation, field, value):
    data = observation.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError, match="Extra inputs"):
        PlayerObservation.model_validate_json(json.dumps(data))


def test_private_state_cannot_be_used_as_an_observation(state):
    with pytest.raises(ValidationError):
        PlayerObservation.model_validate(state)


def test_public_exchange_history_cannot_contain_private_tile_payloads(state):
    turn = PublicTurn(
        revision=2, player=1, move=public_move(ExchangeMove(tiles=("Q", "?"))),
        score=0, scores_after=state.scores,
    )
    visible = build_observation(
        replace_validated(state, revision=2, public_history=state.public_history + (turn,)),
        0,
    )
    assert visible.model_dump(mode="json")["public_history"][-1]["move"] == {
        "schema_version": 1, "kind": "exchange", "tile_count": 2,
    }
    payload = turn.model_dump(mode="json")
    payload["move"]["tiles"] = ["Q", "?"]
    with pytest.raises(ValidationError):
        PublicTurn.model_validate_json(json.dumps(payload))


def test_observations_reject_inconsistent_inventory_or_hidden_sizes(observation):
    with pytest.raises(ValidationError, match="inferred"):
        replace_validated(observation, unseen_tile_counts=TileInventory())
    with pytest.raises(ValidationError, match="total"):
        replace_validated(observation, bag_size=observation.bag_size - 1)


def test_records_and_nested_collections_are_immutable_and_revalidated(state):
    with pytest.raises(ValidationError, match="frozen"):
        state.revision = 10
    assert isinstance(state.racks, tuple) and isinstance(state.racks[0], tuple)
    # Even an unchecked copy is rejected when reintroduced at a validated boundary.
    bad_draw = state.draw_state.model_copy(update={"ordered_tiles": ("invalid",)})
    with pytest.raises(ValidationError):
        replace_validated(state, draw_state=bad_draw)


def test_two_racks_have_a_seven_tile_limit(state):
    with pytest.raises(ValidationError):
        replace_validated(state, racks=(state.racks[0] + ("A",), state.racks[1]))
    with pytest.raises(ValidationError):
        replace_validated(state, racks=(state.racks[0],))


def test_public_history_must_be_ordered_and_cannot_be_from_the_future(state):
    future = PublicTurn(revision=2, player=1, move=PassMove(), score=0, scores_after=state.scores)
    with pytest.raises(ValidationError, match="future"):
        replace_validated(state, public_history=state.public_history + (future,))
    with pytest.raises(ValidationError, match="strictly increase"):
        replace_validated(state, revision=2, public_history=(future, state.public_history[0]))
    with pytest.raises(ValidationError, match="current scores"):
        replace_validated(state, scores=(5, 0))


def test_terminal_state_allows_negative_final_scores_and_requires_reason(state):
    with pytest.raises(ValidationError, match="terminal reason"):
        replace_validated(state, status="finished")
    with pytest.raises(ValidationError, match="terminal reason"):
        replace_validated(state, terminal_reason="rack_out")
    turn = PublicTurn(revision=2, player=1, move=PassMove(), score=0, scores_after=(2, -10))
    terminal = replace_validated(
        state, status="finished", terminal_reason="scoreless_limit", revision=2,
        scores=(2, -10), public_history=state.public_history + (turn,),
    )
    assert GameState.model_validate_json(terminal.model_dump_json()).scores == (2, -10)
