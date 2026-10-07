"""Provider-neutral configuration records; no credentials or SDK dependencies."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from ._base import (
    DomainModel, EngineCommit, Identifier, NonEmptyText, NonNegativeInt,
    PositiveInt, PositiveValue, Sha256,
)
from .tiles import STANDARD_ENGLISH_INVENTORY, TileInventory


class RulesetConfig(DomainModel):
    """Standard board/rack, automatic adjudication; ending convention is explicit."""

    ruleset_id: Identifier
    scoreless_turn_limit: PositiveInt
    exchange_minimum_bag_tiles: Annotated[int, Field(ge=0, le=100)] = 7


class GameConfig(DomainModel):
    game_id: Identifier
    ruleset: RulesetConfig
    lexicon_id: Identifier
    seed: NonNegativeInt
    tile_distribution: TileInventory = STANDARD_ENGLISH_INVENTORY


class EngineConfig(DomainModel):
    engine_commit: EngineCommit
    library_path: NonEmptyText | None = None
    data_path: NonEmptyText | None = None


class LexiconConfig(DomainModel):
    lexicon_id: Identifier
    sha256: Sha256
    file_path: NonEmptyText | None = None


class CandidateConfig(DomainModel):
    pool_size: Annotated[int, Field(ge=1, le=10000)] = 40
    display_count: Annotated[int, Field(ge=1, le=10000)] = 12
    include_exchanges: bool = True
    include_pass: bool = True
    selection_policy: Identifier = "static-diverse-v1"

    @model_validator(mode="after")
    def display_within_pool(self) -> Self:
        if self.display_count > self.pool_size:
            raise ValueError("display count cannot exceed candidate pool size")
        return self


class ModelSettings(DomainModel):
    provider: Identifier
    model: Identifier
    max_output_tokens: PositiveInt
    temperature: Annotated[float, Field(ge=0, le=2, allow_inf_nan=False)] | None = None
    reasoning_effort: Identifier | None = None


class RunSeeds(DomainModel):
    game: NonNegativeInt
    analysis: NonNegativeInt
    candidate_order: NonNegativeInt
    trial: NonNegativeInt


class RunLimits(DomainModel):
    """Independent caps; USD is integer millionths to avoid floating-point money."""

    max_api_cost_microusd: NonNegativeInt
    max_model_calls: NonNegativeInt
    max_engine_work_units: NonNegativeInt
    engine_work_unit: Identifier
    max_elapsed_seconds: PositiveValue


class RunConfig(DomainModel):
    model: ModelSettings
    engine: EngineConfig
    lexicon: LexiconConfig
    ruleset: RulesetConfig
    policy_version: Identifier
    candidate_config: CandidateConfig
    seeds: RunSeeds
    limits: RunLimits
