"""Authoritative private state and an explicit, validated public projection."""

from collections import Counter
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from ._base import (
    DomainModel, EngineCommit, Identifier, NonEmptyText, NonNegativeInt,
    ObservationId, PlayerIndex,
)
from .moves import PlacementMove, PublicMove
from .config import GameConfig
from .serialization import _observation_identity
from .tiles import (
    Board, PhysicalTile, Rack, RACK_SIZE, STANDARD_ENGLISH_INVENTORY, TileInventory,
)


GameStatus = Literal["in_progress", "finished"]
TerminalReason = Literal["rack_out", "scoreless_limit"]


class DrawState(DomainModel):
    """Private draw order and opaque RNG state, including future exchange draws.

    The first ordered tile is drawn next. rng_state is a full state captured by
    the named implementation, not merely its initial seed. The adapter owns its
    encoding and restoration; this model does not execute draws.
    """

    ordered_tiles: Annotated[tuple[PhysicalTile, ...], Field(max_length=100)]
    rng_algorithm: Identifier
    rng_state: NonEmptyText


class PublicTurn(DomainModel):
    """A committed turn with only public facts; history may be a retained suffix."""

    revision: Annotated[int, Field(gt=0)]
    player: PlayerIndex
    move: PublicMove
    score: NonNegativeInt
    scores_after: tuple[int, int]

    @model_validator(mode="after")
    def zero_nonplacement_score(self) -> Self:
        if not isinstance(self.move, PlacementMove) and self.score != 0:
            raise ValueError("public exchange and pass turns must have zero turn score")
        return self


def _validate_public_metadata(record: "GameState | ObservationData") -> None:
    revisions = tuple(turn.revision for turn in record.public_history)
    if any(a >= b for a, b in zip(revisions, revisions[1:])):
        raise ValueError("public history revisions must strictly increase")
    if revisions and revisions[-1] > record.revision:
        raise ValueError("public history cannot contain a future revision")
    if revisions and revisions[-1] == record.revision:
        if record.public_history[-1].scores_after != record.scores:
            raise ValueError("latest public scores must match current scores")
    if (record.status == "finished") != (record.terminal_reason is not None):
        raise ValueError("finished games require a terminal reason, active games forbid it")


class GameState(DomainModel):
    """Private, complete state. Never pass this record to an analysis worker or LLM."""

    game_id: Identifier
    revision: NonNegativeInt
    board: Board
    racks: tuple[Rack, Rack]
    draw_state: DrawState
    scores: tuple[int, int]
    active_player: PlayerIndex
    scoreless_turns: NonNegativeInt
    ruleset_id: Identifier
    lexicon_id: Identifier
    tile_distribution: TileInventory = STANDARD_ENGLISH_INVENTORY
    public_history: tuple[PublicTurn, ...] = ()
    status: GameStatus = "in_progress"
    terminal_reason: TerminalReason | None = None

    @model_validator(mode="after")
    def conserve_tiles_and_public_metadata(self) -> Self:
        actual = Counter(
            self.board.physical_tiles + self.racks[0] + self.racks[1]
            + self.draw_state.ordered_tiles
        )
        if actual != self.tile_distribution.as_counter():
            raise ValueError("board, racks, and bag must conserve the tile distribution")
        _validate_public_metadata(self)
        return self


class ObservationData(DomainModel):
    """Validated public content before its derived observation ID is attached."""

    game_id: Identifier
    revision: NonNegativeInt
    player: PlayerIndex
    active_player: PlayerIndex
    board: Board
    own_rack: Rack
    scores: tuple[int, int]
    opponent_rack_size: Annotated[int, Field(ge=0, le=RACK_SIZE)]
    bag_size: Annotated[int, Field(ge=0, le=100)]
    scoreless_turns: NonNegativeInt
    ruleset_id: Identifier
    lexicon_id: Identifier
    tile_distribution: TileInventory = STANDARD_ENGLISH_INVENTORY
    unseen_tile_counts: TileInventory
    public_history: tuple[PublicTurn, ...] = ()
    status: GameStatus = "in_progress"
    terminal_reason: TerminalReason | None = None

    @model_validator(mode="after")
    def inferable_inventory_and_public_metadata(self) -> Self:
        expected = self.tile_distribution.without(
            self.board.physical_tiles + self.own_rack
        )
        if expected.as_counter() != self.unseen_tile_counts.as_counter():
            raise ValueError("unseen inventory must be inferred from public tiles and own rack")
        if self.unseen_tile_counts.total != self.opponent_rack_size + self.bag_size:
            raise ValueError("unseen inventory must total opponent rack size plus bag size")
        _validate_public_metadata(self)
        return self


class PlayerObservation(ObservationData):
    """Public content bound to its versioned, self-verifying content identity."""

    observation_id: ObservationId

    @model_validator(mode="after")
    def verify_identity(self) -> Self:
        if self.observation_id != _observation_identity(self):
            raise ValueError("observation ID does not match its canonical public content")
        return self

    @classmethod
    def from_data(cls, content: ObservationData) -> Self:
        """Compute an ID for validated public content, including imported positions."""
        content = ObservationData.model_validate(content)
        return cls(
            **content.model_dump(mode="python"),
            observation_id=_observation_identity(content),
        )


def build_observation(state: GameState, player: int) -> PlayerObservation:
    """Project private state by whitelisting fields and subtracting visible tiles.

    The opponent rack contributes only its length; draw state only its length.
    The ID contains no hidden state. Private input is revalidated before use.
    """
    state = GameState.model_validate(state)
    player = TypeAdapter(PlayerIndex, config={"strict": True}).validate_python(player)
    content = ObservationData(
        game_id=state.game_id,
        revision=state.revision,
        player=player,
        active_player=state.active_player,
        board=state.board,
        own_rack=state.racks[player],
        scores=state.scores,
        opponent_rack_size=len(state.racks[1 - player]),
        bag_size=len(state.draw_state.ordered_tiles),
        scoreless_turns=state.scoreless_turns,
        ruleset_id=state.ruleset_id,
        lexicon_id=state.lexicon_id,
        tile_distribution=state.tile_distribution,
        unseen_tile_counts=state.tile_distribution.without(
            state.board.physical_tiles + state.racks[player]
        ),
        public_history=state.public_history,
        status=state.status,
        terminal_reason=state.terminal_reason,
    )
    return PlayerObservation.from_data(content)


class Checkpoint(DomainModel):
    """Private recovery record. Native snapshots may supplement validated state."""

    checkpoint_id: Identifier
    engine_commit: EngineCommit
    state: GameState
    native_snapshot: NonEmptyText | None = None
    game_config: GameConfig | None = None
    engine_configuration_id: Identifier | None = None

    @model_validator(mode="after")
    def configuration_matches_state(self) -> Self:
        if self.game_config is not None:
            config = self.game_config
            if (config.game_id != self.state.game_id
                    or config.ruleset.ruleset_id != self.state.ruleset_id
                    or config.lexicon_id != self.state.lexicon_id
                    or config.tile_distribution.as_counter() != self.state.tile_distribution.as_counter()):
                raise ValueError("checkpoint configuration does not match private state")
        return self
