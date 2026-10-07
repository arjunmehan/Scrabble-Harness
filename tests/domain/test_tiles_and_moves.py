import json

import pytest
from pydantic import TypeAdapter, ValidationError

from scrabble_harness.domain import (
    Board, BoardTile, Candidate, CandidateProvenance, ExchangeMove, Move,
    MovePreview, PassMove, PlacementMove, STANDARD_ENGLISH_INVENTORY,
    TileCount, TileInventory, build_candidate, public_move,
)


@pytest.mark.parametrize("update", [
    {"row": -1}, {"row": 15}, {"row": "7"}, {"row": True}, {"column": 15},
    {"displayed_letter": "q"}, {"displayed_letter": "?"},
    {"displayed_letter": "QU"}, {"displayed_letter": "É"}, {"is_blank": "true"},
    {"schema_version": 2}, {"schema_version": True}, {"premium": "triple"},
])
def test_board_tile_rejects_malformed_external_json(update):
    data = {"row": 7, "column": 7, "displayed_letter": "Q", "is_blank": True}
    data.update(update)
    with pytest.raises(ValidationError):
        BoardTile.model_validate_json(json.dumps(data))


def test_blank_assignment_does_not_change_physical_identity():
    blank = BoardTile(row=7, column=7, displayed_letter="Q", is_blank=True)
    restored = BoardTile.model_validate_json(blank.model_dump_json())
    assert restored.physical_tile == "?"
    assert restored.displayed_letter == "Q"
    assert BoardTile(row=0, column=0, displayed_letter="Q").physical_tile == "Q"


def test_duplicate_board_squares_are_rejected():
    a = BoardTile(row=7, column=7, displayed_letter="A")
    b = BoardTile(row=7, column=7, displayed_letter="B")
    with pytest.raises(ValidationError, match="duplicate squares"):
        Board(tiles=(a, b))


@pytest.mark.parametrize("payload", [
    {"kind": "place", "tiles": []},
    {"kind": "exchange", "tiles": []},
    {"kind": "exchange", "tiles": ["A"] * 8},
    {"kind": "exchange", "tiles": ["a"]},
    {"kind": "pass", "tiles": ["A"]},
    {"kind": "other"},
])
def test_move_union_rejects_wrong_payloads(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(Move).validate_json(json.dumps(payload))


@pytest.mark.parametrize("payload", [
    {"kind": "place", "tiles": [
        {"row": 7, "column": 7, "displayed_letter": "Q", "is_blank": True},
    ]},
    {"kind": "exchange", "tiles": ["A", "?"]},
    {"kind": "pass"},
])
def test_move_union_round_trips_and_keeps_its_variant(payload):
    adapter = TypeAdapter(Move)
    move = adapter.validate_json(json.dumps(payload))
    assert adapter.validate_json(adapter.dump_json(move)) == move


def test_placement_structure_allows_gaps_for_existing_board_letters():
    a = BoardTile(row=7, column=7, displayed_letter="A")
    b = BoardTile(row=7, column=9, displayed_letter="B")
    assert PlacementMove(tiles=(a, b)).physical_tiles == ("A", "B")
    with pytest.raises(ValidationError, match="duplicate squares"):
        PlacementMove(tiles=(a, a))
    with pytest.raises(ValidationError, match="share a row or column"):
        PlacementMove(tiles=(a, BoardTile(row=8, column=8, displayed_letter="B")))


def test_inventory_rejects_duplicates_missing_tiles_and_excess():
    with pytest.raises(ValidationError, match="duplicate tile symbols"):
        TileInventory(counts=(TileCount(tile="A", count=1), TileCount(tile="A", count=2)))
    with pytest.raises(ValidationError, match="100-tile"):
        TileInventory(counts=(TileCount(tile="A", count=100), TileCount(tile="B", count=1)))
    with pytest.raises(ValueError, match="exceed"):
        TileInventory.from_tiles(("A", "?")).without(("A", "A"))


def test_inventory_uses_a_detached_counter_and_supports_empty_endgames():
    inventory = TileInventory.from_tiles(("A", "?", "A"))
    counter = inventory.as_counter()
    counter["A"] = 100
    assert inventory.total == 3
    assert inventory.without(("A", "A", "?")).total == 0
    assert STANDARD_ENGLISH_INVENTORY.total == 100
    assert STANDARD_ENGLISH_INVENTORY.as_counter()["?"] == 2


def test_exchange_history_retains_only_the_count():
    move = ExchangeMove(tiles=("Q", "?"))
    public = public_move(move)
    assert public.model_dump() == {"schema_version": 1, "kind": "exchange", "tile_count": 2}
    assert public_move(PassMove()) == PassMove()


def test_candidate_and_preview_cannot_assign_points_to_a_pass(observation):
    common = dict(move=PassMove(), exact_score=1, leave=())
    with pytest.raises(ValidationError, match="zero turn score"):
        build_candidate(
            observation, provenance=CandidateProvenance(
                source="fixture", configuration_id="fixture-v1",
            ), **common,
        )
    with pytest.raises(ValidationError, match="zero turn score"):
        MovePreview(observation_id=observation.observation_id, board_after=Board(), **common)


def test_candidate_keeps_physical_blank_in_leave(observation):
    candidate = build_candidate(
        observation, move=PassMove(),
        exact_score=0, leave=("Q", "?"), static_equity=-3.5,
        provenance=CandidateProvenance(source="fixture", configuration_id="fixture-v1"),
    )
    assert Candidate.model_validate_json(candidate.model_dump_json()) == candidate
