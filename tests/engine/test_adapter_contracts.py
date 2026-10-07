from collections import Counter
import json

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import (
    Board, BoardTile, DrawState, ExchangeMove, GameConfig, PassMove, PlacementMove,
    RulesetConfig, canonical_bytes,
)
from scrabble_harness.engine.codec import (
    leave_after, move_to_native, native_to_move, observation_to_cgp, parse_simulation,
)
from scrabble_harness.engine.draws import exchange, initial_draws, restore_rng
from scrabble_harness.engine.errors import EngineConfigurationError, InvalidMoveError, InvalidRequestError
from scrabble_harness.engine.worker import NativeRequest


@pytest.mark.parametrize("notation,tiles", [
    ("8H QI", (BoardTile(row=7, column=7, displayed_letter="Q"), BoardTile(row=7, column=8, displayed_letter="I"))),
    ("H8 qI", (BoardTile(row=7, column=7, displayed_letter="Q", is_blank=True), BoardTile(row=8, column=7, displayed_letter="I"))),
])
def test_coordinate_and_blank_translation_roundtrip(notation, tiles):
    decoded = native_to_move(notation, Board())
    assert canonical_bytes(decoded) == canonical_bytes(PlacementMove(tiles=tiles))
    assert move_to_native(decoded, Board()) == notation


def test_playthrough_gap_and_physical_blank_identity():
    board = Board(tiles=(BoardTile(row=7, column=7, displayed_letter="Q", is_blank=True),
                         BoardTile(row=7, column=8, displayed_letter="I")))
    move = native_to_move("8H (qI)T", board)
    assert move.tiles == (BoardTile(row=7, column=9, displayed_letter="T"),)
    assert move_to_native(move, board) == "8H ..T"
    with pytest.raises(InvalidRequestError):
        native_to_move("8H (QI)T", board)
    with pytest.raises(InvalidMoveError):
        move_to_native(PlacementMove(tiles=(BoardTile(row=7, column=7, displayed_letter="A"),)), board)


@pytest.mark.parametrize("notation", ["P1 AT", "1P AT", "15O AT", "8H (A", "8H ()A", "8H ((A))", "8H (?)A", "ex ?A"])
def test_unsupported_native_moves_rejected(notation):
    with pytest.raises((InvalidRequestError, ValidationError)):
        native_to_move(notation, Board())


def test_exchange_multiset_consumption_and_gap_rejection():
    move = ExchangeMove(tiles=("?", "A", "A"))
    assert move_to_native(move, Board()) == "ex ?AA"
    assert leave_after(move, ("A", "?", "A", "I")) == ("I",)
    with pytest.raises(InvalidMoveError):
        leave_after(move, ("?", "A"))
    with pytest.raises(InvalidMoveError):
        move_to_native(PlacementMove(tiles=(BoardTile(row=7, column=7, displayed_letter="A"),
                                             BoardTile(row=7, column=9, displayed_letter="T"))), Board())


def test_worker_schema_rejects_private_state(state, active_observation):
    with pytest.raises(ValidationError):
        NativeRequest(operation="generate", observation=state)
    request = NativeRequest(operation="preview", observation=active_observation, moves=(PassMove(),))
    wire = request.model_dump_json()
    assert '"draw_state"' not in wire and '"racks"' not in wire and '"rng_state"' not in wire
    assert NativeRequest.model_validate_json(wire) == request


def test_complete_rng_restoration_preserves_subsequent_exchanges():
    config = GameConfig(game_id="draws", lexicon_id="fixture", seed=7,
                        ruleset=RulesetConfig(ruleset_id="test", scoreless_turn_limit=6))
    rack, _, draws = initial_draws(config)
    restored = DrawState.model_validate_json(draws.model_dump_json())
    kept = tuple(tile for index, tile in enumerate(rack) if index != 0)
    a, next_a = exchange(kept, rack[:1], draws)
    b, next_b = exchange(kept, rack[:1], restored)
    assert a == b and next_a == next_b
    assert Counter(next_a.ordered_tiles) + Counter(a) == Counter(draws.ordered_tiles) + Counter(rack)


@pytest.mark.parametrize("payload", [
    '{"version":3,"words":[],"gaussian":null}',
    '{"version":true,"words":[],"gaussian":null}',
    'not-json',
])
def test_malformed_rng_payloads_rejected(payload):
    with pytest.raises(EngineConfigurationError):
        restore_rng(DrawState(ordered_tiles=(), rng_algorithm="python-mt19937-v1", rng_state=payload))


def test_captured_native_simulation_counts_and_tampering():
    from pathlib import Path
    report = json.loads((Path(__file__).parents[2] / "docs/native-spike-results.json").read_text())
    output = report["raw_output"]["bounded_simulation"]
    parsed = parse_simulation(output)
    assert parsed.iterations == sum(row.samples for row in parsed.rows) == 12
    assert parsed.known_opponent_rack == ""
    with pytest.raises(InvalidRequestError):
        parse_simulation(output.replace("Showing 3 of 3 plays", "Showing 2 of 3 plays"))
    with pytest.raises(InvalidRequestError):
        parse_simulation(output.replace("Iters:     12", "Iters:     13"))
