"""Move shapes and engine-supplied candidates; no dictionary or scoring logic."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import (
    CandidateId, DomainModel, EngineCommit, FiniteValue, Identifier,
    NonNegativeInt, ObservationId,
)
from .serialization import _candidate_identity
from .tiles import Board, BoardTile, PhysicalTile, Rack, RACK_SIZE


class PlacementMove(DomainModel):
    """Only newly placed tiles; existing board letters are not in the payload."""

    kind: Literal["place"] = "place"
    tiles: Annotated[tuple[BoardTile, ...], Field(min_length=1, max_length=RACK_SIZE)]

    @model_validator(mode="after")
    def straight_unique_placement(self) -> Self:
        positions = {(tile.row, tile.column) for tile in self.tiles}
        if len(positions) != len(self.tiles):
            raise ValueError("placement contains duplicate squares")
        if len({tile.row for tile in self.tiles}) > 1 and len(
            {tile.column for tile in self.tiles}
        ) > 1:
            raise ValueError("newly placed tiles must share a row or column")
        # Gaps can contain existing board tiles; the engine checks connectivity.
        return self

    @property
    def physical_tiles(self) -> tuple[str, ...]:
        return tuple(tile.physical_tile for tile in self.tiles)


class ExchangeMove(DomainModel):
    kind: Literal["exchange"] = "exchange"
    tiles: Annotated[
        tuple[PhysicalTile, ...], Field(min_length=1, max_length=RACK_SIZE)
    ]


class PassMove(DomainModel):
    kind: Literal["pass"] = "pass"


Move = Annotated[PlacementMove | ExchangeMove | PassMove, Field(discriminator="kind")]


class PublicExchangeMove(DomainModel):
    """Public history may reveal the count, never the exchanged identities."""

    kind: Literal["exchange"] = "exchange"
    tile_count: Annotated[int, Field(ge=1, le=RACK_SIZE)]


PublicMove = Annotated[
    PlacementMove | PublicExchangeMove | PassMove, Field(discriminator="kind")
]


def public_move(move: Move) -> PublicMove:
    if isinstance(move, ExchangeMove):
        return PublicExchangeMove(tile_count=len(move.tiles))
    return move


class CandidateProvenance(DomainModel):
    source: Identifier
    configuration_id: Identifier
    engine_commit: EngineCommit | None = None


class Candidate(DomainModel):
    candidate_id: CandidateId
    observation_id: ObservationId
    move: Move
    exact_score: NonNegativeInt
    leave: Rack
    static_equity: FiniteValue | None = None
    provenance: CandidateProvenance

    @model_validator(mode="after")
    def zero_nonplacement_score(self) -> Self:
        if not isinstance(self.move, PlacementMove) and self.exact_score != 0:
            raise ValueError("exchange and pass candidates must have zero turn score")
        if self.candidate_id != _candidate_identity(self.observation_id, self.move):
            raise ValueError("candidate ID does not match its observation and canonical move")
        return self


class MovePreview(DomainModel):
    observation_id: ObservationId
    move: Move
    exact_score: NonNegativeInt
    leave: Rack
    board_after: Board

    @model_validator(mode="after")
    def zero_nonplacement_score(self) -> Self:
        if not isinstance(self.move, PlacementMove) and self.exact_score != 0:
            raise ValueError("exchange and pass previews must have zero turn score")
        return self
