"""Async structural interfaces for analysis and authoritative game operations.

Native command strings and library handles are implementation details.
"""

from typing import Protocol

from scrabble_harness.domain import (
    AnalysisLimits, AnalysisResult, Candidate, CandidateConfig, CandidateId, Checkpoint,
    GameConfig, GameState, Move, MovePreview, PlayerObservation,
)


class AnalysisEngine(Protocol):
    """Analyze public information without access to private arbiter state."""

    async def load_observation(self, observation: PlayerObservation) -> None:
        """Replace the analysis context with a player-visible position."""
        ...

    async def generate_candidates(
        self, config: CandidateConfig
    ) -> tuple[Candidate, ...]:
        """Return candidates bound to the currently loaded observation."""
        ...

    async def preview(self, move: Move) -> MovePreview:
        """Return move effects without mutating authoritative game state."""
        ...

    async def analyze(
        self,
        candidate_ids: tuple[CandidateId, ...],
        limits: AnalysisLimits,
        seed: int,
    ) -> AnalysisResult:
        """Compare requested candidates and report actual work/completion."""
        ...

    async def close(self) -> None:
        """Release worker and native resources; concrete cleanup is idempotent."""
        ...


class ArbiterEngine(Protocol):
    """Own private game state and apply validated, revision-checked moves."""

    async def start_game(self, config: GameConfig) -> GameState:
        """Initialize a game and return its private authoritative state."""
        ...

    async def observe(self, player: int) -> PlayerObservation:
        """Return only the information available to the specified player."""
        ...

    async def commit(self, move: Move, expected_revision: int) -> GameState:
        """Validate and atomically apply one move, rejecting stale revisions."""
        ...

    async def checkpoint(self) -> Checkpoint:
        """Capture sufficient private recovery data, including draw behavior."""
        ...

    async def restore(self, checkpoint: Checkpoint) -> GameState:
        """Recover private state and future draw behavior from a checkpoint."""
        ...

    async def close(self) -> None:
        """Release worker and native resources; concrete cleanup is idempotent."""
        ...
