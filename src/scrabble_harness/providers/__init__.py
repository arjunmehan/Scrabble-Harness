"""Provider contracts and an offline scripted client; no SDK or client construction on import."""

from .protocol import (
    ModelProvider, ModelReply, ModelRequest, ProviderCapabilities,
    ProviderContinuation, ProviderToolCall, TokenUsage,
)

from .fake import (
    ScriptError, ScriptExhaustedError, ScriptMismatchError, ScriptedCall,
    ScriptedProvider, ScriptedReply, ScriptedStep, ScriptedTransportError,
    ScriptedTransportFailure, comparison_call, selection_call,
)

__all__ = [
    "ModelProvider", "ModelReply", "ModelRequest", "ProviderCapabilities",
    "ProviderContinuation", "ProviderToolCall", "TokenUsage",
    "ScriptError", "ScriptExhaustedError", "ScriptMismatchError", "ScriptedCall",
    "ScriptedProvider", "ScriptedReply", "ScriptedStep", "ScriptedTransportError",
    "ScriptedTransportFailure", "comparison_call", "selection_call",
]
