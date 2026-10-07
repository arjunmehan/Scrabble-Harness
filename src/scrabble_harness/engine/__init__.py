"""Project-facing engine contracts; importing them does not load MAGPIE."""

from .errors import (
    EngineConfigurationError,
    EngineCrashedError,
    EngineError,
    EngineTimeoutError,
    EngineProtocolError,
    CheckpointConflictError,
    InvalidMoveError,
    InvalidRequestError,
    StaleRevisionError,
    UnsupportedOperationError,
)
from .protocol import AnalysisEngine, ArbiterEngine
from .adapter import MagpieAnalysisEngine, MagpieArbiterEngine, make_checkpoint
from .settings import MagpieSettings, WORK_UNIT
from .checkpoints import CheckpointStore, InvalidCheckpointError
from .trace import TraceJournal, WorkerEvent

__all__ = [
    "AnalysisEngine",
    "MagpieAnalysisEngine", "MagpieArbiterEngine", "MagpieSettings", "WORK_UNIT", "make_checkpoint",
    "ArbiterEngine",
    "CheckpointStore", "CheckpointConflictError", "InvalidCheckpointError", "TraceJournal", "WorkerEvent",
    "EngineConfigurationError",
    "EngineCrashedError",
    "EngineError",
    "EngineTimeoutError",
    "EngineProtocolError",
    "InvalidMoveError",
    "InvalidRequestError",
    "StaleRevisionError",
    "UnsupportedOperationError",
]
