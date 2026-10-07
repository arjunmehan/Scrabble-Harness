"""Concrete public analysis and host-owned authoritative game adapters."""

import asyncio
from collections import Counter
import hashlib
import json
import time
import uuid

from pydantic import TypeAdapter

from ..domain import (
    AnalysisLimits, AnalysisResult, Board, Candidate, CandidateConfig, CandidateId,
    CandidateEvaluation, CandidateProvenance, Checkpoint, ExchangeMove, GameConfig,
    GameState, MetricDefinition, Move, MovePreview, PassMove, PlacementMove,
    PlayerObservation, PublicTurn, STANDARD_ENGLISH_INVENTORY, WorkUsed,
    build_candidate, build_observation, canonical_bytes, canonical_json, public_move,
)
from ..domain._base import NonNegativeInt
from .codec import (
    board_after, leave_after, move_to_native, native_to_move, observation_to_cgp,
    parse_simulation, parse_static_moves,
)
from .draws import exchange, initial_draws, rack_points, refill, restore_rng
from .checkpoints import CheckpointStore
from .errors import (
    EngineConfigurationError, EngineError, InvalidMoveError, InvalidRequestError,
    StaleRevisionError, UnsupportedOperationError,
)
from .settings import MagpieSettings, WORK_UNIT
from .trace import TraceJournal
from .worker import NativeRequest, NativeWorker


def _digest(prefix: str, payload: dict) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return prefix + hashlib.sha256(data).hexdigest()


def _check_observation(observation: PlayerObservation, settings: MagpieSettings) -> PlayerObservation:
    observation = PlayerObservation.model_validate(observation)
    if observation.lexicon_id != settings.lexicon_id:
        raise EngineConfigurationError("Observation lexicon differs from native configuration")
    observation_to_cgp(observation, allow_late_game=True)
    return observation


def _check_move(move: Move, observation: PlayerObservation) -> Move:
    move = TypeAdapter(Move).validate_python(move)
    leave_after(move, observation.own_rack)
    move_to_native(move, observation.board)
    if isinstance(move, ExchangeMove) and observation.bag_size < 7:
        raise InvalidMoveError("Exchange requires at least seven actual bag tiles")
    return move


def _check_displayed_leave(displayed: str, leave: tuple[str, ...], move: Move, *, allow_omitted: bool) -> None:
    if allow_omitted and not displayed:
        return
    expected = Counter(leave)
    if isinstance(move, ExchangeMove):
        # The pinned display formatter skips machine-letter zero, which is a
        # physical blank in exchanges. It incorrectly leaves exchanged blanks
        # in the printed leave. The actual leave always comes from the host.
        expected.update(tile for tile in move.tiles if tile == "?")
    if Counter(displayed) != expected:
        raise EngineError("Native displayed leave does not match the pinned format")


async def _preview(worker: NativeWorker, observation: PlayerObservation, move: Move) -> MovePreview:
    move = _check_move(move, observation)
    reply = await worker.request(NativeRequest(operation="preview", observation=observation, moves=(move,)))
    rows = parse_static_moves(reply.output)
    if len(rows) != 1:
        raise EngineError("Native preview did not return exactly one move")
    decoded = native_to_move(rows[0].notation, observation.board)
    if canonical_bytes(decoded) != canonical_bytes(move):
        raise EngineError("Native preview changed the requested move")
    leave = leave_after(move, observation.own_rack)
    # A fresh addmoves-only context omits its rack/leave display at this pin.
    # Derive the leave from verified physical ownership; check any supplied one.
    _check_displayed_leave(rows[0].leave, leave, move, allow_omitted=True)
    if not isinstance(move, PlacementMove) and rows[0].score != 0:
        raise EngineError("Native nonplacement score is nonzero")
    return MovePreview(observation_id=observation.observation_id, move=move,
                       exact_score=rows[0].score, leave=leave,
                       board_after=board_after(move, observation.board))


class MagpieAnalysisEngine:
    """Decision-time analysis receives public observations only."""

    def __init__(self, settings: MagpieSettings, *, trace: TraceJournal | None = None) -> None:
        self.settings = settings
        self._worker = NativeWorker(settings, trace=trace, role="analysis")
        self._lock = asyncio.Lock()
        self._observation = None
        self._candidates: dict[str, Candidate] = {}
        self._closed = False

    def _current(self) -> PlayerObservation:
        if self._closed:
            raise InvalidRequestError("Analysis engine is closed")
        if self._observation is None:
            raise InvalidRequestError("Load an observation before analysis")
        return self._observation

    async def load_observation(self, observation: PlayerObservation) -> None:
        async with self._lock:
            if self._closed:
                raise InvalidRequestError("Analysis engine is closed")
            checked = _check_observation(observation, self.settings)
            self._observation, self._candidates = checked, {}

    async def generate_candidates(self, config: CandidateConfig) -> tuple[Candidate, ...]:
        config = CandidateConfig.model_validate(config)
        if config.selection_policy not in {"static-diverse-v1", "static-top-v1"}:
            raise UnsupportedOperationError("Unsupported candidate selection policy")
        async with self._lock:
            observation = self._current()
            reply = await self._worker.request(NativeRequest(
                operation="generate", observation=observation,
                pool_size=config.pool_size, include_pass=config.include_pass,
            ))
            provenance = CandidateProvenance(
                source="magpie-static", engine_commit=self.settings.engine_commit,
                configuration_id=_digest("generation_v1_", {
                    "native": self.settings.configuration_id, "candidates": json.loads(canonical_json(config)),
                }),
            )
            candidates: dict[str, Candidate] = {}
            for row in parse_static_moves(reply.output):
                move = native_to_move(row.notation, observation.board)
                if isinstance(move, ExchangeMove) and (not config.include_exchanges or observation.bag_size < 7):
                    continue
                if isinstance(move, PassMove) and not config.include_pass:
                    continue
                leave = leave_after(move, observation.own_rack)
                _check_displayed_leave(row.leave, leave, move, allow_omitted=False)
                candidate = build_candidate(observation, move, exact_score=row.score,
                                            leave=leave, static_equity=row.static_equity,
                                            provenance=provenance)
                previous = candidates.get(candidate.candidate_id)
                if previous and (previous.exact_score != candidate.exact_score or previous.leave != candidate.leave):
                    raise EngineError("Equivalent native moves have inconsistent scores/leaves")
                candidates.setdefault(candidate.candidate_id, candidate)
            ordered = tuple(candidates.values())
            reserved = [candidate for candidate in ordered if isinstance(candidate.move, PassMove)]
            if config.selection_policy == "static-diverse-v1" and len(reserved) < config.pool_size - 1:
                best_exchange = next((candidate for candidate in ordered if isinstance(candidate.move, ExchangeMove)), None)
                if best_exchange:
                    reserved.append(best_exchange)
            reserved_ids = {candidate.candidate_id for candidate in reserved}
            best = [candidate for candidate in ordered if candidate.candidate_id not in reserved_ids]
            chosen_ids = reserved_ids | {candidate.candidate_id for candidate in best[:config.pool_size - len(reserved)]}
            pool = tuple(candidate for candidate in ordered if candidate.candidate_id in chosen_ids)
            self._candidates = {candidate.candidate_id: candidate for candidate in pool}
            return pool

    async def preview(self, move: Move) -> MovePreview:
        async with self._lock:
            return await _preview(self._worker, self._current(), move)

    async def analyze(self, candidate_ids: tuple[CandidateId, ...], limits: AnalysisLimits, seed: int) -> AnalysisResult:
        candidate_ids = TypeAdapter(tuple[CandidateId, ...], config={"strict": True}).validate_python(candidate_ids)
        limits = AnalysisLimits.model_validate(limits)
        seed = TypeAdapter(NonNegativeInt, config={"strict": True}).validate_python(seed)
        if limits.work_unit != WORK_UNIT or limits.max_work_units > 2**63 - 1 or seed > 2**64 - 1:
            raise InvalidRequestError("Unsupported work unit or native integer range")
        if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise InvalidRequestError("Analysis needs distinct candidate IDs")
        if limits.max_work_units < len(candidate_ids):
            raise InvalidRequestError("Work limit must allow one sample per candidate")
        async with self._lock:
            started = time.monotonic()
            observation = self._current()
            if observation.bag_size < 7 or observation.opponent_rack_size != 7:
                raise UnsupportedOperationError("Simulation currently supports full unknown racks with at least seven bag tiles")
            try:
                candidates = tuple(self._candidates[identifier] for identifier in candidate_ids)
            except KeyError as exc:
                raise InvalidRequestError("Candidate is absent from the current observation's pool") from exc
            reply = await self._worker.request(NativeRequest(
                operation="analyze", observation=observation,
                moves=tuple(candidate.move for candidate in candidates), limits=limits, seed=seed,
            ))
            values, count, note = (), 0, None
            status = reply.status
            try:
                table = parse_simulation(reply.output)
            except InvalidRequestError:
                if reply.status == "complete":
                    raise
                # A fresh handle stopped before sampling can return a static list.
                parse_static_moves(reply.output)
                note = "Interrupted before sampled results were available; zero reported completed samples"
            else:
                if table.known_opponent_rack or table.iterations > limits.max_work_units:
                    raise EngineError("Native analysis violated unknown-rack or work constraints")
                mapped, seen = {}, set()
                for row in table.rows:
                    move = native_to_move(row.notation, observation.board)
                    candidate = build_candidate(observation, move, exact_score=row.score,
                                                leave=leave_after(move, observation.own_rack),
                                                provenance=candidates[0].provenance)
                    if candidate.candidate_id not in candidate_ids or candidate.candidate_id in seen:
                        raise EngineError("Native analysis returned extra/duplicate candidates")
                    seen.add(candidate.candidate_id)
                    expected = self._candidates[candidate.candidate_id]
                    if row.score != expected.exact_score:
                        raise EngineError("Native simulation score changed")
                    if row.samples:
                        mapped[candidate.candidate_id] = CandidateEvaluation(
                            candidate_id=candidate.candidate_id, value=row.equity,
                            standard_error=row.equity_sem if row.samples >= 2 else None,
                        )
                if seen != set(candidate_ids) or (reply.status == "complete" and len(mapped) != len(candidate_ids)):
                    raise EngineError("Native analysis omitted requested candidates")
                count = table.iterations
                values = tuple(mapped[identifier] for identifier in candidate_ids if identifier in mapped)
                note = table.status
                if status == "complete":
                    if table.status == "Finished (max iterations reached)":
                        status = "work_limit"
                    elif table.status == "Finished (time limit exceeded)":
                        status = "timeout"
                    elif table.status == "Finished (user interrupt)":
                        status = "cancelled"
                    elif table.status != "Finished (statistical threshold achieved)":
                        raise EngineError(f"Unsupported native completion: {table.status}")
            elapsed = time.monotonic() - started
            if elapsed > limits.max_elapsed_seconds and status not in {"cancelled", "timeout"}:
                status = "timeout"
                note = "Analysis exceeded its wall-time limit while completing result translation"
            result = AnalysisResult(
                analysis_id="analysis_" + uuid.uuid4().hex,
                observation_id=observation.observation_id, values=values,
                metric_definition=MetricDefinition(name="two_ply_equity", unit="points",
                    description="MAGPIE two-ply simulation equity from the observing player's perspective",
                    value_kind="estimate"),
                work_used=WorkUsed(unit=WORK_UNIT, definition="One completed candidate rollout sample, summed across candidates", count=count),
                elapsed_time_seconds=elapsed, seed=seed,
                completion_status=status, completion_note=note,
            )
            self._worker.record("analysis_result", request_id=reply.request_id,
                                game_id=observation.game_id, revision=observation.revision,
                                observation_id=observation.observation_id, operation="analyze",
                                work_used=count, elapsed_seconds=elapsed, completion_status=status)
            return result

    @property
    def trace_failure_count(self) -> int:
        return self._worker.trace_failure_count

    async def cancel(self) -> None:
        """Cooperatively interrupt the current analysis and retain usable estimates."""
        self._worker.cancel()

    async def close(self) -> None:
        self._closed = True
        await self._worker.close()


def _check_game_config(config: GameConfig, settings: MagpieSettings) -> GameConfig:
    config = GameConfig.model_validate(config)
    if config.lexicon_id != settings.lexicon_id:
        raise EngineConfigurationError("Game lexicon differs from native configuration")
    if config.tile_distribution.as_counter() != STANDARD_ENGLISH_INVENTORY.as_counter():
        raise UnsupportedOperationError("Arbiter supports the standard English tile set")
    if config.ruleset.exchange_minimum_bag_tiles != 7 or config.ruleset.scoreless_turn_limit != 6:
        raise UnsupportedOperationError("This adapter supports exchange minimum seven and scoreless limit six")
    return config


def make_checkpoint(state: GameState, config: GameConfig, settings: MagpieSettings) -> Checkpoint:
    """Bind a complete private state for persistence or trusted position import.

    Identity binds contents; it does not authenticate an imported game's history.
    The caller is responsible for the provenance of an imported board.
    """
    state = GameState.model_validate(state)
    config = _check_game_config(config, settings)
    if settings.fingerprint() != settings.configuration_id:
        raise EngineConfigurationError("Native inputs changed after settings were created")
    restore_rng(state.draw_state)
    rack_out = not state.draw_state.ordered_tiles and any(not rack for rack in state.racks)
    scoreless_end = state.scoreless_turns >= config.ruleset.scoreless_turn_limit
    if (state.status == "in_progress" and (rack_out or scoreless_end)
            or state.terminal_reason == "rack_out" and not rack_out
            or state.terminal_reason == "scoreless_limit" and not scoreless_end):
        raise EngineConfigurationError("Checkpoint terminal state conflicts with its game rules")
    record = Checkpoint(checkpoint_id="pending", engine_commit=settings.engine_commit,
                        state=state, game_config=config,
                        engine_configuration_id=settings.configuration_id)
    identifier = _digest("checkpoint_v1_", json.loads(canonical_json(record)) | {"checkpoint_id": None})
    return Checkpoint(**(record.model_dump(mode="python") | {"checkpoint_id": identifier}))


class MagpieArbiterEngine:
    """Atomic revision-checked transactions; native workers receive no draw secrets."""

    def __init__(self, settings: MagpieSettings, *, trace: TraceJournal | None = None,
                 checkpoint_store: CheckpointStore | None = None) -> None:
        self.settings = settings
        if checkpoint_store is not None and checkpoint_store.settings.configuration_id != settings.configuration_id:
            raise EngineConfigurationError("Checkpoint store uses a different native configuration")
        self._store = checkpoint_store
        self._worker = NativeWorker(settings, trace=trace, role="arbiter")
        self._lock = asyncio.Lock()
        self._state: GameState | None = None
        self._config: GameConfig | None = None
        self._closed = False

    def _current(self) -> GameState:
        if self._closed:
            raise InvalidRequestError("Arbiter is closed")
        if self._state is None:
            raise InvalidRequestError("Start or restore a game first")
        return self._state

    async def start_game(self, config: GameConfig) -> GameState:
        async with self._lock:
            if self._closed:
                raise InvalidRequestError("Arbiter is closed")
            config = _check_game_config(config, self.settings)
            first, second, draws = initial_draws(config)
            state = GameState(game_id=config.game_id, revision=0, board=Board(),
                              racks=(first, second), draw_state=draws, scores=(0, 0),
                              active_player=0, scoreless_turns=0, ruleset_id=config.ruleset.ruleset_id,
                              lexicon_id=config.lexicon_id, tile_distribution=config.tile_distribution)
            if self._store is not None:
                self._store.save(make_checkpoint(state, config, self.settings))
            self._state, self._config = state, config
            return state

    async def observe(self, player: int) -> PlayerObservation:
        async with self._lock:
            return build_observation(self._current(), player)

    async def commit(self, move: Move, expected_revision: int) -> GameState:
        expected_revision = TypeAdapter(NonNegativeInt, config={"strict": True}).validate_python(expected_revision)
        async with self._lock:
            state = self._current()
            if expected_revision != state.revision:
                raise StaleRevisionError("Commit revision differs from current state")
            if state.status != "in_progress":
                raise InvalidRequestError("Cannot commit to a finished game")
            observation = build_observation(state, state.active_player)
            preview = await _preview(self._worker, observation, move)
            move = preview.move
            player, opponent = state.active_player, 1 - state.active_player
            draws, racks = state.draw_state, list(state.racks)
            if isinstance(move, PlacementMove):
                racks[player], draws = refill(preview.leave, draws)
            elif isinstance(move, ExchangeMove):
                racks[player], draws = exchange(preview.leave, move.tiles, draws)
            scores = list(state.scores)
            scores[player] += preview.exact_score
            scoreless = 0 if preview.exact_score > 0 else state.scoreless_turns + 1
            reason = None
            if isinstance(move, PlacementMove) and not racks[player] and not draws.ordered_tiles:
                reason = "rack_out"
                penalty = rack_points(racks[opponent])
                scores[player] += penalty
                scores[opponent] -= penalty
            elif scoreless >= self._config.ruleset.scoreless_turn_limit:
                reason = "scoreless_limit"
                scores = [score - rack_points(rack) for score, rack in zip(scores, racks)]
            revision = state.revision + 1
            turn = PublicTurn(revision=revision, player=player, move=public_move(move),
                              score=preview.exact_score, scores_after=tuple(scores))
            updated = GameState(
                **(state.model_dump(mode="python") | {
                    "revision": revision, "board": preview.board_after,
                    "racks": tuple(racks), "draw_state": draws, "scores": tuple(scores),
                    "active_player": opponent, "scoreless_turns": scoreless,
                    "public_history": state.public_history + (turn,),
                    "status": "finished" if reason else "in_progress", "terminal_reason": reason,
                })
            )
            # Durable publication precedes the in-memory commit. This small
            # synchronous section has no cancellation point between the two.
            if self._store is not None:
                self._store.save(make_checkpoint(updated, self._config, self.settings))
            self._state = updated
            return updated

    async def checkpoint(self) -> Checkpoint:
        async with self._lock:
            return make_checkpoint(self._current(), self._config, self.settings)

    async def restore(self, checkpoint: Checkpoint) -> GameState:
        async with self._lock:
            return self._restore_locked(checkpoint)

    def _restore_locked(self, checkpoint: Checkpoint) -> GameState:
        if self._closed:
            raise InvalidRequestError("Arbiter is closed")
        checkpoint = Checkpoint.model_validate(checkpoint)
        if (checkpoint.game_config is None or checkpoint.native_snapshot is not None
                or checkpoint.engine_commit != self.settings.engine_commit
                or checkpoint.engine_configuration_id != self.settings.configuration_id):
            raise EngineConfigurationError("Checkpoint lacks compatible game/native configuration")
        rebuilt = make_checkpoint(checkpoint.state, checkpoint.game_config, self.settings)
        if rebuilt.checkpoint_id != checkpoint.checkpoint_id:
            raise EngineConfigurationError("Checkpoint contents do not match its identity")
        if self._store is not None:
            self._store.save(rebuilt)
        # Native handles are scratch contexts rebuilt from public observations.
        self._state, self._config = rebuilt.state, rebuilt.game_config
        return self._state

    async def resume(self, game_id: str) -> GameState:
        """Restore the newest durable revision; fail if no store was configured."""
        async with self._lock:
            if self._store is None:
                raise InvalidRequestError("Resume requires a checkpoint store")
            # Load after acquiring the transaction lock: an earlier read could
            # roll back a commit that finished while this call was queued.
            return self._restore_locked(self._store.load(game_id))

    @property
    def trace_failure_count(self) -> int:
        return self._worker.trace_failure_count

    async def close(self) -> None:
        self._closed = True
        await self._worker.close()
