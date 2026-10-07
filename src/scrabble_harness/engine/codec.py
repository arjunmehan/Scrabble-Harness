"""Board-aware move translation and pinned native machine-output parsing.

CGP positions contain only public information. Draw order and private RNG state
never enter native commands. Unsupported formats fail explicitly.
"""

from collections import Counter
from dataclasses import dataclass
import math
import re

from pydantic import TypeAdapter

from ..domain import (
    Board, BoardTile, ExchangeMove, Move, PassMove, PlacementMove,
    PlayerObservation, STANDARD_ENGLISH_INVENTORY,
)
from .errors import InvalidMoveError, InvalidRequestError, UnsupportedOperationError


def observation_to_cgp(observation: PlayerObservation, *, allow_late_game: bool = False) -> str:
    """Encode an active observation, leaving the unknown opponent rack empty.

    Native player zero is the observer, regardless of the original seat. The
    native bag contains *all unseen tiles*, not the actual public bag. Call
    simulate with '-' to force sampling. Never use this as an arbiter snapshot.
    allow_late_game opts into static validation/generation for smaller bags and
    a publicly derived opponent rack when the bag is empty. The default retains
    the narrower step-4 spike scope; simulation support is checked separately.
    """
    observation = PlayerObservation.model_validate(observation)
    if observation.status != "in_progress" or observation.player != observation.active_player:
        raise InvalidRequestError("CGP spike supports only an active, in-progress observation")
    if observation.tile_distribution.as_counter() != STANDARD_ENGLISH_INVENTORY.as_counter():
        raise UnsupportedOperationError("CGP spike requires the standard English distribution")
    if not allow_late_game and (observation.bag_size < 7 or observation.opponent_rack_size != 7):
        raise UnsupportedOperationError("Unknown-rack spike requires at least seven bag tiles and seven opponent tiles")
    occupied = {(tile.row, tile.column): tile for tile in observation.board.tiles}
    rows = []
    for row in range(15):
        tokens, empty = [], 0
        for column in range(15):
            tile = occupied.get((row, column))
            if tile is None:
                empty += 1
                continue
            if empty:
                tokens.append(str(empty))
                empty = 0
            tokens.append(tile.displayed_letter.lower() if tile.is_blank else tile.displayed_letter)
        if empty:
            tokens.append(str(empty))
        rows.append("".join(tokens))
    player = observation.player
    scores = f"{observation.scores[player]}/{observation.scores[1 - player]}"
    opponent = ""
    if allow_late_game and observation.bag_size == 0:
        # All remaining tiles are publicly inferable; never use a private rack.
        opponent = "".join(sorted(observation.unseen_tile_counts.as_counter().elements()))
    return f"{'/'.join(rows)} {''.join(sorted(observation.own_rack))}/{opponent} {scores} {observation.scoreless_turns}"


@dataclass(frozen=True)
class StaticMoveRow:
    index: int
    notation: str
    leave: str
    score: int
    static_equity: float


_ROW = re.compile(r"^(\d+):\s+(pass|\(exch [A-Z?]+\)|(?:[A-O](?:[1-9]|1[0-5])|(?:[1-9]|1[0-5])[A-O]) [A-Za-z()]+)\s+(?:([A-Z?]+)\s+)?(\d+)\s+(-?\d+\.\d+)\s*$")
_FOOTER = re.compile(r"^Showing (\d+) of (\d+) plays$")


def parse_static_moves(output: str) -> tuple[StaticMoveRow, ...]:
    """Parse this pin's headerless table with shwithmoves=false, not old UCGI.

    Parentheses mark existing board letters; lowercase letters mark blanks.
    Preserve notation for a later full codec. Require the complete table so an
    accidental human/board/simulation format cannot be accepted as static data.
    """
    rows, footer = [], None
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _FOOTER.fullmatch(line)
        if match and footer is None:
            footer = tuple(map(int, match.groups()))
            continue
        match = _ROW.fullmatch(line)
        if not match or footer is not None:
            raise InvalidRequestError(f"Unsupported MAGPIE static output: {line[:100]}")
        index, notation, leave, score, equity = match.groups()
        value = float(equity)
        if not math.isfinite(value) or int(index) != len(rows) + 1:
            raise InvalidRequestError("Invalid static row index/equity")
        rows.append(StaticMoveRow(int(index), notation, leave or "", int(score), value))
    if footer is None or footer != (len(rows), len(rows)):
        raise InvalidRequestError("Missing, truncated, or filtered MAGPIE static table")
    return tuple(rows)


def move_to_native(move: Move, board: Board) -> str:
    """Encode only new tiles, expanding contiguous played-through letters."""
    move = TypeAdapter(Move).validate_python(move)
    board = Board.model_validate(board)
    if isinstance(move, PassMove):
        return "pass"
    if isinstance(move, ExchangeMove):
        return "ex " + "".join(sorted(move.tiles))
    occupied = {(tile.row, tile.column): tile for tile in board.tiles}
    placed = {(tile.row, tile.column): tile for tile in move.tiles}
    if set(occupied) & set(placed):
        raise InvalidMoveError("Cannot overwrite an existing board tile")
    first = min(placed)
    vertical = len({tile.row for tile in move.tiles}) > 1
    if len(placed) == 1:
        row, column = first
        horizontal_neighbor = (row, column - 1) in occupied or (row, column + 1) in occupied
        vertical = not horizontal_neighbor and ((row - 1, column) in occupied or (row + 1, column) in occupied)
    row_step, col_step = (1, 0) if vertical else (0, 1)
    last = max(placed)
    while (first[0] - row_step, first[1] - col_step) in occupied:
        first = first[0] - row_step, first[1] - col_step
    while (last[0] + row_step, last[1] + col_step) in occupied:
        last = last[0] + row_step, last[1] + col_step
    letters, position = [], first
    while True:
        tile = placed.get(position)
        if tile is not None:
            letters.append(tile.displayed_letter.lower() if tile.is_blank else tile.displayed_letter)
        elif position in occupied:
            letters.append(".")
        else:
            raise InvalidMoveError("A gap between new tiles must contain an existing tile")
        if position == last:
            break
        position = position[0] + row_step, position[1] + col_step
    coordinate = (f"{chr(65 + first[1])}{first[0] + 1}" if vertical
                  else f"{first[0] + 1}{chr(65 + first[1])}")
    return f"{coordinate} {''.join(letters)}"


def native_to_move(notation: str, board: Board) -> Move:
    """Decode the pinned display notation, checking existing squares exactly."""
    board = Board.model_validate(board)
    if notation == "pass":
        return PassMove()
    exchange = re.fullmatch(r"\(exch ([A-Z?]+)\)", notation)
    if exchange:
        return ExchangeMove(tiles=tuple(exchange[1]))
    match = re.fullmatch(r"([A-O](?:[1-9]|1[0-5])|(?:[1-9]|1[0-5])[A-O]) ([A-Za-z()]+)", notation)
    if not match:
        raise InvalidRequestError("Unsupported native move notation")
    coordinate, word = match.groups()
    vertical = coordinate[0].isalpha()
    row = int(coordinate[1:] if vertical else coordinate[:-1]) - 1
    column = ord(coordinate[0] if vertical else coordinate[-1]) - 65
    occupied = {(tile.row, tile.column): tile for tile in board.tiles}
    tiles, through, group_count = [], False, 0
    for character in word:
        if character == "(":
            if through:
                raise InvalidRequestError("Nested native playthrough group")
            through, group_count = True, 0
            continue
        if character == ")":
            if not through or group_count == 0:
                raise InvalidRequestError("Invalid native playthrough group")
            through = False
            continue
        if row >= 15 or column >= 15:
            raise InvalidRequestError("Native move extends beyond board")
        existing = occupied.get((row, column))
        if through:
            if existing is None or existing.displayed_letter != character.upper() or existing.is_blank != character.islower():
                raise InvalidRequestError("Native playthrough does not match board")
            group_count += 1
        else:
            if existing is not None:
                raise InvalidRequestError("Native move overwrites board")
            tiles.append(BoardTile(row=row, column=column,
                                   displayed_letter=character.upper(), is_blank=character.islower()))
        row += int(vertical)
        column += int(not vertical)
    if through or not tiles:
        raise InvalidRequestError("Unclosed playthrough or no new tiles")
    return PlacementMove(tiles=tuple(tiles))


def leave_after(move: Move, rack: tuple[str, ...]) -> tuple[str, ...]:
    used = (move.physical_tiles if isinstance(move, PlacementMove)
            else move.tiles if isinstance(move, ExchangeMove) else ())
    remaining, consumed = Counter(rack), Counter(used)
    if any(count > remaining[tile] for tile, count in consumed.items()):
        raise InvalidMoveError("Move consumes tiles absent from the player's rack")
    remaining.subtract(consumed)
    return tuple(sorted(remaining.elements()))


def board_after(move: Move, board: Board) -> Board:
    if not isinstance(move, PlacementMove):
        return board
    return Board(tiles=tuple(sorted(board.tiles + move.tiles, key=lambda tile: (tile.row, tile.column))))


@dataclass(frozen=True)
class SimulationRow:
    notation: str
    score: int
    equity: float
    equity_sem: float
    samples: int


@dataclass(frozen=True)
class SimulationTable:
    rows: tuple[SimulationRow, ...]
    iterations: int
    status: str
    known_opponent_rack: str


def parse_simulation(output: str) -> SimulationTable:
    """Require the complete pinned two-ply machine table, with BU disabled."""
    rows, metadata = [], {}
    pattern = re.compile(
        r"^(\d+):\s+(pass|\(exch [A-Z?]+\)|(?:[A-O](?:[1-9]|1[0-5])|(?:[1-9]|1[0-5])[A-O]) [A-Za-z()]+)\s+"
        r"(?:([A-Z?]+)\s+)?(\d+)\s+([-X])\s+(.+)$"
    )
    showing = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        match = pattern.fullmatch(line)
        if match:
            if metadata or showing:
                raise InvalidRequestError("Native simulation rows follow footer")
            index, notation, leave, score, ignored, tail = match.groups()
            values = tail.split()
            try:
                numbers = tuple(float(value) for value in values)
                samples = int(values[5])
            except (ValueError, IndexError) as exc:
                raise InvalidRequestError("Invalid native simulation values") from exc
            if (int(index) != len(rows) + 1 or len(values) != 12
                    or not all(math.isfinite(value) for value in numbers)
                    or not 0 <= numbers[0] <= 100 or numbers[1] < 0
                    or numbers[3] < 0 or samples < 0):
                raise InvalidRequestError("Unexpected native simulation columns")
            rows.append(SimulationRow(notation, int(score), numbers[2], numbers[3], samples))
            continue
        footer = re.fullmatch(r"Showing (\d+) of (\d+) (plays|plies)", line)
        if footer:
            showing.append((footer[3], int(footer[1]), int(footer[2])))
            continue
        summary = re.fullmatch(r"(Iters|Time|Opp Rack|Infer|Wp Cutoff|Status|Nps):\s*(.*)", line)
        if not summary or summary[1] in metadata:
            raise InvalidRequestError("Unsupported native simulation footer")
        metadata[summary[1]] = summary[2]
    if (not rows or showing != [("plays", len(rows), len(rows)), ("plies", 2, 2)]
            or set(metadata) != {"Iters", "Time", "Opp Rack", "Infer", "Wp Cutoff", "Status", "Nps"}):
        raise InvalidRequestError("Incomplete native simulation table")
    try:
        iterations = int(metadata["Iters"])
    except ValueError as exc:
        raise InvalidRequestError("Invalid native work count") from exc
    if iterations != sum(row.samples for row in rows):
        raise InvalidRequestError("Native total does not match per-play sample counts")
    return SimulationTable(tuple(rows), iterations, metadata["Status"], metadata["Opp Rack"])
