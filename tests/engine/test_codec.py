from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import replace_validated
from scrabble_harness.domain import (
    Board, BoardTile, ObservationData, PlayerObservation, TileInventory,
)
from scrabble_harness.engine.codec import observation_to_cgp, parse_static_moves
from scrabble_harness.engine.errors import InvalidRequestError, UnsupportedOperationError

FIXTURES = Path(__file__).parents[1] / "fixtures/native"


def changed(observation, **updates):
    fields = observation.model_dump(mode="python", exclude={"observation_id"})
    fields.update(updates)
    return PlayerObservation.from_data(ObservationData.model_validate(fields))


def test_public_cgp_rotates_seats_and_preserves_blank(active_observation):
    cgp = observation_to_cgp(active_observation)
    board, racks, scores, scoreless = cgp.split()
    assert board.split("/")[7] == "7qI6"
    assert racks == "GHIJKLM/"
    assert scores == "0/2"
    assert scoreless == "0"
    assert "rng" not in cgp


def test_public_cgp_is_stable_for_equivalent_collections(active_observation):
    reordered = changed(active_observation, own_rack=active_observation.own_rack[::-1],
                        board=replace_validated(active_observation.board,
                                                tiles=active_observation.board.tiles[::-1]))
    assert observation_to_cgp(reordered) == observation_to_cgp(active_observation)


def test_cgp_rejects_private_state_and_inactive_observer(state, observation):
    with pytest.raises(ValidationError):
        observation_to_cgp(state)
    with pytest.raises(InvalidRequestError):
        observation_to_cgp(observation)


def test_cgp_rejects_terminal_and_partial_unknown_racks(active_observation):
    with pytest.raises(InvalidRequestError):
        observation_to_cgp(changed(active_observation, status="finished", terminal_reason="scoreless_limit"))
    with pytest.raises(UnsupportedOperationError):
        observation_to_cgp(changed(active_observation, opponent_rack_size=6, bag_size=85))


def test_cgp_rejects_other_tile_distributions(active_observation):
    distribution = TileInventory.from_tiles(active_observation.board.physical_tiles + active_observation.own_rack + tuple("AEIRSTU"))
    with pytest.raises(UnsupportedOperationError):
        observation_to_cgp(changed(active_observation, tile_distribution=distribution,
                                  unseen_tile_counts=distribution.without(active_observation.board.physical_tiles + active_observation.own_rack),
                                  bag_size=0, opponent_rack_size=7))


def test_cgp_rejects_small_bags_until_exchange_rules_are_handled(active_observation):
    # Valid public inventory with only six actual bag tiles. The native empty
    # rack CGP would hold thirteen unseen tiles and incorrectly permit exchange.
    hidden = tuple(active_observation.unseen_tile_counts.as_counter().elements())
    occupied = {(tile.row, tile.column) for tile in active_observation.board.tiles}
    squares = ((row, col) for row in range(15) for col in range(15) if (row, col) not in occupied)
    extra = tuple(BoardTile(row=row, column=col,
                           displayed_letter="A" if symbol == "?" else symbol,
                           is_blank=symbol == "?")
                  for (row, col), symbol in zip(squares, hidden[13:]))
    board = Board(tiles=active_observation.board.tiles + extra)
    narrow = changed(active_observation, board=board, bag_size=6,
                     unseen_tile_counts=active_observation.tile_distribution.without(board.physical_tiles + active_observation.own_rack))
    with pytest.raises(UnsupportedOperationError):
        observation_to_cgp(narrow)


def test_captured_scores_and_leaves():
    rows = parse_static_moves((FIXTURES / "restricted_static.txt").read_text())
    assert [(row.notation, row.score, row.leave) for row in rows] == [
        ("8H QI", 22, "AERT?"), ("8H qI", 2, "AEQRT"), ("pass", 0, "AEIQRT?"),
    ]


def test_captured_exchange_and_empty_bingo_leave():
    exchange = parse_static_moves((FIXTURES / "exchange_preview.txt").read_text())[0]
    assert exchange.notation == "(exch AE?)"
    assert exchange.score == 0
    bingo = parse_static_moves((FIXTURES / "bingo_preview.txt").read_text())[0]
    assert bingo.notation == "8E RETINAS"
    assert bingo.leave == "" and bingo.score == 64


def test_parenthesized_playthrough_notation_is_preserved():
    row = parse_static_moves("1: H8 (qI)T AERT? 12 11.30\n\nShowing 1 of 1 plays\n")[0]
    assert row.notation == "H8 (qI)T"


@pytest.mark.parametrize("output", [
    "", "info currmove 8h QI score 22", "1: 8H QI AERT? 22 21.30",
    "2: 8H QI AERT? 22 21.30\nShowing 1 of 1 plays",
    "1: 8H QI AERT? 22 21.30\nShowing 1 of 2 plays",
    "1: 8H QI AERT? 22 nan\nShowing 1 of 1 plays",
    "Move Lv Sc StEq\n1: 8H QI AERT? 22 21.30\nShowing 1 of 1 plays",
    "Invalid words formed by 8H QA: QA\n",
    "1: 8H QI AERT? 22 - 52.00 1.0000 24.00 1.0000 21.30 5\nShowing 1 of 1 plays",
    "Showing 0 of 0 plays\nextra output",
])
def test_parser_rejects_truncated_human_phony_or_changed_formats(output):
    with pytest.raises(InvalidRequestError):
        parse_static_moves(output)
