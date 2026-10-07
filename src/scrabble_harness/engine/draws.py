"""Host-owned draws; no pickle, native bag, or seed-only recovery."""

import json
import random

from ..domain import DrawState, GameConfig
from .errors import EngineConfigurationError, InvalidMoveError

RNG_ALGORITHM = "python-mt19937-v1"


def _encode(generator: random.Random) -> str:
    version, words, gaussian = generator.getstate()
    return json.dumps({"version": version, "words": words, "gaussian": gaussian},
                      separators=(",", ":"), allow_nan=False)


def restore_rng(state: DrawState) -> random.Random:
    state = DrawState.model_validate(state)
    if state.rng_algorithm != RNG_ALGORITHM:
        raise EngineConfigurationError("Unsupported checkpoint RNG algorithm")
    try:
        data = json.loads(state.rng_state)
        if (set(data) != {"version", "words", "gaussian"} or type(data["version"]) is not int
                or data["version"] != 3 or not isinstance(data["words"], list)
                or len(data["words"]) != 625 or data["gaussian"] is not None
                or any(type(word) is not int for word in data["words"])
                or any(not 0 <= word <= 2**32 - 1 for word in data["words"][:-1])
                or not 0 <= data["words"][-1] <= 624):
            raise ValueError("Invalid MT19937 state")
        generator = random.Random(0)
        generator.setstate((3, tuple(data["words"]), None))
        return generator
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise EngineConfigurationError("Invalid checkpoint RNG state") from exc


def initial_draws(config: GameConfig) -> tuple[tuple[str, ...], tuple[str, ...], DrawState]:
    generator = random.Random(config.seed)
    bag = sorted(config.tile_distribution.as_counter().elements())
    generator.shuffle(bag)
    first, second = tuple(sorted(bag[:7])), tuple(sorted(bag[7:14]))
    state = DrawState(ordered_tiles=tuple(bag[14:]), rng_algorithm=RNG_ALGORITHM,
                      rng_state=_encode(generator))
    return first, second, state


def refill(leave: tuple[str, ...], state: DrawState) -> tuple[tuple[str, ...], DrawState]:
    count = min(7 - len(leave), len(state.ordered_tiles))
    rack = tuple(sorted(leave + state.ordered_tiles[:count]))
    next_state = DrawState(ordered_tiles=state.ordered_tiles[count:],
                           rng_algorithm=state.rng_algorithm, rng_state=state.rng_state)
    return rack, next_state


def exchange(leave: tuple[str, ...], returned: tuple[str, ...], state: DrawState) -> tuple[tuple[str, ...], DrawState]:
    if len(state.ordered_tiles) < len(returned):
        raise InvalidMoveError("Insufficient bag tiles for exchange")
    # Draw before returning tiles: an exchanged physical tile cannot be redrawn
    # immediately. Shuffle the remaining bag with the returned tiles afterward.
    rack = tuple(sorted(leave + state.ordered_tiles[:len(returned)]))
    bag = list(state.ordered_tiles[len(returned):] + returned)
    generator = restore_rng(state)
    generator.shuffle(bag)
    return rack, DrawState(ordered_tiles=tuple(bag), rng_algorithm=RNG_ALGORITHM,
                          rng_state=_encode(generator))


TILE_POINTS = {
    "?": 0, **dict.fromkeys("AEILNORSTU", 1), **dict.fromkeys("DG", 2),
    **dict.fromkeys("BCMP", 3), **dict.fromkeys("FHVWY", 4), "K": 5,
    "J": 8, "X": 8, "Q": 10, "Z": 10,
}


def rack_points(rack: tuple[str, ...]) -> int:
    return sum(TILE_POINTS[tile] for tile in rack)
