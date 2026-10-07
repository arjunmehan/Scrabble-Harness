"""Engine failure categories shared by adapters and their callers.

Incomplete analysis with usable results will be represented by result status,
not silently treated as an exact answer or automatically raised as a failure.
"""


class EngineError(RuntimeError):
    """Base class for failures at the engine boundary."""


class EngineConfigurationError(EngineError):
    """The engine build, lexicon, or native data configuration is unusable."""


class InvalidRequestError(EngineError):
    """A request is malformed or references an unavailable analysis context."""


class InvalidMoveError(EngineError):
    """The engine rejected a move without applying it to authoritative state."""


class StaleRevisionError(InvalidRequestError):
    """The requested revision does not match the authoritative game state."""


class UnsupportedOperationError(EngineError):
    """The configured engine cannot provide the requested operation."""


class EngineTimeoutError(EngineError):
    """An operation exceeded its deadline without a usable completion."""


class EngineCrashedError(EngineError):
    """The native worker terminated unexpectedly."""


class EngineProtocolError(EngineError):
    """The worker returned an oversized, malformed, or unrelated message."""


class CheckpointConflictError(EngineError):
    """A different state is already durably committed at this game revision."""
