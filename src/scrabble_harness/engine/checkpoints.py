"""Immutable private revision snapshots, atomically published before state commit."""

import hashlib
import os
from pathlib import Path
import re
import tempfile

from ..domain import Checkpoint, canonical_bytes
from .errors import CheckpointConflictError, EngineConfigurationError, EngineError
from .settings import MagpieSettings


class CheckpointStore:
    """Local filesystem store with one immutable record per game/revision.

    Atomic hard-link publication prevents a competing writer overwriting a
    revision. File and directory fsync precede success. Use a local filesystem
    supporting hard links and directory fsync; snapshots contain game secrets.
    """

    def __init__(self, root: Path, settings: MagpieSettings) -> None:
        self.root, self.settings = Path(root).resolve(), settings
        missing = []
        directory = self.root
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        # Persist each newly created directory entry, including store parents.
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            self._sync_directory(directory.parent)

    def _directory(self, game_id: str) -> Path:
        # IDs cannot become paths, including IDs supplied by imported records.
        return self.root / hashlib.sha256(game_id.encode()).hexdigest()

    def _checked(self, checkpoint: Checkpoint) -> Checkpoint:
        from .adapter import make_checkpoint
        checkpoint = Checkpoint.model_validate(checkpoint)
        if (checkpoint.game_config is None or checkpoint.native_snapshot is not None
                or checkpoint.engine_commit != self.settings.engine_commit
                or checkpoint.engine_configuration_id != self.settings.configuration_id):
            raise EngineConfigurationError("Checkpoint is incompatible with this store")
        rebuilt = make_checkpoint(checkpoint.state, checkpoint.game_config, self.settings)
        if checkpoint.checkpoint_id != rebuilt.checkpoint_id:
            raise EngineConfigurationError("Checkpoint contents do not match its identity")
        return rebuilt

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def save(self, checkpoint: Checkpoint) -> Path:
        checkpoint = self._checked(checkpoint)
        directory = self._directory(checkpoint.state.game_id)
        destination = directory / f"{checkpoint.state.revision:020d}.json"
        data = canonical_bytes(checkpoint) + b"\n"
        temporary = None
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            self._sync_directory(self.root)
            descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing = self.load(checkpoint.state.game_id, checkpoint.state.revision)
                if existing.checkpoint_id != checkpoint.checkpoint_id:
                    raise CheckpointConflictError("Game revision already has a different checkpoint")
            # After publication, a failure here is an uncertain durable outcome.
            # Retrying the same commit is safe: save is content-idempotent.
            self._sync_directory(directory)
            return destination
        except OSError as exc:
            raise EngineError("Checkpoint persistence failed; inspect the stored revision before resuming") from exc
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    # An unpublished temporary record is ignored on recovery.
                    # Cleanup failure cannot undo a durable publication.
                    pass

    def load(self, game_id: str, revision: int | None = None) -> Checkpoint:
        directory = self._directory(game_id)
        if revision is None:
            revisions = [int(path.stem) for path in directory.glob("*.json")
                         if re.fullmatch(r"[0-9]{20,}", path.stem)]
            if not revisions:
                raise InvalidCheckpointError("No saved checkpoint for this game")
            revision = max(revisions)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise InvalidCheckpointError("Revision must be a nonnegative integer")
        try:
            data = (directory / f"{revision:020d}.json").read_bytes()
            checkpoint = self._checked(Checkpoint.model_validate_json(data))
        except (OSError, ValueError) as exc:
            raise InvalidCheckpointError("Saved checkpoint is missing or malformed") from exc
        if checkpoint.state.game_id != game_id or checkpoint.state.revision != revision:
            raise InvalidCheckpointError("Checkpoint contents differ from their storage key")
        return checkpoint


class InvalidCheckpointError(EngineError):
    """A requested saved revision is absent, malformed, or inconsistent."""
