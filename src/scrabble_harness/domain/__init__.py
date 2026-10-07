"""Validated engine-independent records and public-observation construction."""

from ._base import CandidateId, ObservationId
from .analysis import (
    AgentDecision, AnalysisLimits, AnalysisResult, CandidateEvaluation,
    CompletionStatus, MetricDefinition, WorkUsed,
)
from .config import (
    CandidateConfig, EngineConfig, GameConfig, LexiconConfig, ModelSettings,
    RulesetConfig, RunConfig, RunLimits, RunSeeds,
)
from .moves import (
    Candidate, CandidateProvenance, ExchangeMove, Move, MovePreview, PassMove,
    PlacementMove, PublicExchangeMove, PublicMove, public_move,
)
from .state import (
    Checkpoint, DrawState, GameState, ObservationData, PlayerObservation, PublicTurn,
    build_observation,
)
from .decisions import (
    DecisionValidationError, StaleDecisionError, UnknownCandidateError,
    build_candidate, validate_decision,
)
from .serialization import IDENTITY_VERSION, canonical_bytes, canonical_json
from .tiles import (
    BOARD_SIZE, RACK_SIZE, STANDARD_ENGLISH_INVENTORY, Board, BoardTile, PhysicalTile,
    Rack, TileCount, TileInventory,
)

__all__ = [
    "AgentDecision", "AnalysisLimits", "AnalysisResult", "BOARD_SIZE", "Board",
    "BoardTile", "Candidate", "CandidateConfig", "CandidateEvaluation", "CandidateId",
    "CandidateProvenance", "Checkpoint", "CompletionStatus", "DecisionValidationError", "DrawState",
    "EngineConfig", "ExchangeMove", "GameConfig", "GameState", "LexiconConfig",
    "IDENTITY_VERSION", "MetricDefinition", "ModelSettings", "Move", "MovePreview",
    "ObservationData", "ObservationId", "PassMove",
    "PhysicalTile", "PlacementMove", "PlayerObservation", "PublicExchangeMove",
    "PublicMove", "PublicTurn", "RACK_SIZE", "Rack", "RulesetConfig", "RunConfig",
    "RunLimits", "RunSeeds", "STANDARD_ENGLISH_INVENTORY", "StaleDecisionError", "TileCount",
    "TileInventory", "UnknownCandidateError", "WorkUsed", "build_candidate",
    "build_observation", "canonical_bytes", "canonical_json", "public_move", "validate_decision",
]
