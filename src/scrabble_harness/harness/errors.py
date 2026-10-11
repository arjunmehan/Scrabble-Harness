"""Failures before a model request or when resolving decision-scoped records."""


class HarnessError(RuntimeError):
    pass


class ContextPreparationError(HarnessError):
    """No validated candidate context/fallback is available; abort preparation."""


class ContextScopeError(HarnessError):
    """The caller used a different decision or observation."""


class UnknownHandleError(HarnessError):
    """A handle was not exposed in this decision."""


class EvidenceValidationError(HarnessError):
    """Evidence is unissued, inconsistent, or belongs to a different request."""


class PromptSizeError(HarnessError):
    """Public presentation exceeds a byte/character ceiling; do not dispatch it."""


class PresentationVersionError(HarnessError):
    """The requested prompt, presentation, or tool-schema version is unsupported."""


class ToolSchemaError(HarnessError):
    """A tool definition or its arguments violate the versioned protocol."""


class ToolExecutionError(HarnessError):
    """Saved action is unusable, execution is concurrent, or uncertain work cannot replay."""
