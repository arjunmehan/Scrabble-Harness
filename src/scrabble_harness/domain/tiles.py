"""Physical tiles, sparse boards, and inventories for a 15 by 15 English board."""

from collections import Counter
from typing import Annotated, Self

from pydantic import Field, model_validator

from ._base import DomainModel


BOARD_SIZE = 15
RACK_SIZE = 7
Letter = Annotated[str, Field(pattern=r"^[A-Z]$")]
PhysicalTile = Annotated[str, Field(pattern=r"^[A-Z?]$")]
Coordinate = Annotated[int, Field(ge=0, lt=BOARD_SIZE)]
Rack = Annotated[tuple[PhysicalTile, ...], Field(max_length=RACK_SIZE)]


class BoardTile(DomainModel):
    row: Coordinate
    column: Coordinate
    displayed_letter: Letter
    is_blank: bool = False

    @property
    def physical_tile(self) -> str:
        """A blank remains '?' regardless of its assigned displayed letter."""
        return "?" if self.is_blank else self.displayed_letter


class Board(DomainModel):
    tiles: Annotated[tuple[BoardTile, ...], Field(max_length=BOARD_SIZE**2)] = ()

    @model_validator(mode="after")
    def unique_squares(self) -> Self:
        positions = {(tile.row, tile.column) for tile in self.tiles}
        if len(positions) != len(self.tiles):
            raise ValueError("board contains duplicate squares")
        return self

    @property
    def physical_tiles(self) -> tuple[str, ...]:
        return tuple(tile.physical_tile for tile in self.tiles)


class TileCount(DomainModel):
    tile: PhysicalTile
    count: Annotated[int, Field(gt=0, le=100)]


class TileInventory(DomainModel):
    """An immutable multiset; zero-count tiles are omitted.

    Counts describe physical tiles. This record never assigns letters to blanks.
    """

    counts: Annotated[tuple[TileCount, ...], Field(max_length=27)] = ()

    @model_validator(mode="after")
    def valid_inventory(self) -> Self:
        if len({item.tile for item in self.counts}) != len(self.counts):
            raise ValueError("inventory contains duplicate tile symbols")
        if self.total > 100:
            raise ValueError("inventory exceeds the supported 100-tile game")
        return self

    @property
    def total(self) -> int:
        return sum(item.count for item in self.counts)

    def as_counter(self) -> Counter[str]:
        """Return a detached, mutable copy for local multiset arithmetic."""
        return Counter({item.tile: item.count for item in self.counts})

    @classmethod
    def from_tiles(cls, tiles: tuple[str, ...]) -> Self:
        return cls(
            counts=tuple(
                TileCount(tile=tile, count=count)
                for tile, count in sorted(Counter(tiles).items())
            )
        )

    def without(self, tiles: tuple[str, ...]) -> Self:
        remaining = self.as_counter()
        used = Counter(tiles)
        if any(count > remaining[tile] for tile, count in used.items()):
            raise ValueError("visible tiles exceed the configured tile distribution")
        remaining.subtract(used)
        return type(self)(
            counts=tuple(
                TileCount(tile=tile, count=count)
                for tile, count in sorted(remaining.items())
                if count > 0
            )
        )


# Standard English set: 98 letter tiles and two physical blanks.
STANDARD_ENGLISH_INVENTORY = TileInventory(
    counts=tuple(
        TileCount(tile=tile, count=count)
        for tile, count in (
            ("?", 2), ("A", 9), ("B", 2), ("C", 2), ("D", 4), ("E", 12),
            ("F", 2), ("G", 3), ("H", 2), ("I", 9), ("J", 1), ("K", 1),
            ("L", 4), ("M", 2), ("N", 6), ("O", 8), ("P", 2), ("Q", 1),
            ("R", 6), ("S", 4), ("T", 6), ("U", 4), ("V", 2), ("W", 2),
            ("X", 1), ("Y", 2), ("Z", 1),
        )
    )
)
