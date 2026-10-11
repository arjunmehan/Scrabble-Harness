"""Storage errors with no provider payloads or private state in their messages."""


class StoreError(RuntimeError):
    pass


class StoreConflictError(StoreError):
    """An identity, scope, revision, or state transition conflicts with durable data."""


class StoreNotFoundError(StoreError):
    pass


class StoreCorruptionError(StoreError):
    """Stored content, schema identity, or indexed fields fail verification."""


class StoreBusyError(StoreError):
    """The bounded local writer-lock/SQLite wait expired."""


class StorePathError(StoreError):
    """Store location is not a private, regular local file."""
