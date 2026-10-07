"""Candidate construction and pure checks before authoritative engine commit."""

from pydantic import TypeAdapter

from ._base import NonNegativeInt
from .analysis import AgentDecision
from .moves import Candidate, CandidateProvenance, Move
from .serialization import _candidate_identity
from .state import GameState, PlayerObservation, build_observation
from .tiles import Rack


class DecisionValidationError(ValueError):
    """The selection cannot be resolved against the trusted current context."""


class StaleDecisionError(DecisionValidationError):
    """The revision, observation, player, or candidate belongs to another context."""


class UnknownCandidateError(DecisionValidationError):
    """The selected candidate does not belong to the supplied candidate pool."""


def build_candidate(
    observation: PlayerObservation,
    move: Move,
    *,
    exact_score: int,
    leave: Rack,
    provenance: CandidateProvenance,
    static_equity: float | None = None,
) -> Candidate:
    """Bind engine-supplied metadata and a validated move to an observation.

    Hashes identify actions, not verified legality or trustworthy engine output.
    The same action retains its ID when scoring estimates/provenance change.
    """
    observation = PlayerObservation.model_validate(observation)
    move = TypeAdapter(Move).validate_python(move)
    return Candidate(
        candidate_id=_candidate_identity(observation.observation_id, move),
        observation_id=observation.observation_id,
        move=move, exact_score=exact_score, leave=leave,
        provenance=provenance, static_equity=static_equity,
    )


def validate_decision(
    decision: AgentDecision,
    *,
    state: GameState,
    observation: PlayerObservation,
    candidates: tuple[Candidate, ...],
    expected_revision: int,
) -> Candidate:
    """Resolve a selection only against current private state and a trusted pool.

    This does not mutate state, evaluate legality, validate evidence, or make a
    commit atomic. The arbiter must still check expected_revision and engine
    legality inside its serialized commit operation. Never trust a pool supplied
    by the model: IDs are content checks, not authentication.
    """
    state = GameState.model_validate(state)
    expected_revision = TypeAdapter(
        NonNegativeInt, config={"strict": True}
    ).validate_python(expected_revision)
    if expected_revision != state.revision:
        raise StaleDecisionError("expected revision does not match current game revision")
    if state.status != "in_progress":
        raise DecisionValidationError("cannot select a move for a finished game")
    observation = PlayerObservation.model_validate(observation)
    current = build_observation(state, state.active_player)
    if observation.observation_id != current.observation_id:
        raise StaleDecisionError("observation does not match the current active player's state")
    decision = AgentDecision.model_validate(decision)
    if decision.observation_id != current.observation_id:
        raise StaleDecisionError("decision belongs to a different observation")
    candidates = TypeAdapter(
        tuple[Candidate, ...], config={"strict": True}
    ).validate_python(candidates)
    pool: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.observation_id != current.observation_id:
            raise StaleDecisionError("candidate pool contains a different observation")
        if candidate.candidate_id in pool:
            raise DecisionValidationError("candidate pool contains duplicate IDs")
        pool[candidate.candidate_id] = candidate
    try:
        return pool[decision.candidate_id]
    except KeyError:
        raise UnknownCandidateError("selected candidate is absent from the current pool") from None
