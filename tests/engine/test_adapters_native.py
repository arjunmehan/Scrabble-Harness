import asyncio
from collections import Counter
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scrabble_harness.domain import (
    AnalysisLimits, Board, BoardTile, CandidateConfig, Checkpoint, DrawState,
    ExchangeMove, GameConfig, GameState, PassMove, PlacementMove, RulesetConfig,
    STANDARD_ENGLISH_INVENTORY, build_observation, canonical_bytes,
)
from scrabble_harness.engine import (
    EngineConfigurationError, EngineCrashedError, EngineTimeoutError, InvalidMoveError, InvalidRequestError,
    MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings, StaleRevisionError,
    UnsupportedOperationError, WORK_UNIT, make_checkpoint,
)
from scrabble_harness.engine.draws import initial_draws, rack_points

pytestmark = pytest.mark.native


@pytest.fixture
def settings():
    path = Path(os.environ.get("SCRABBLE_MAGPIE_MANIFEST", Path(__file__).parents[2] / ".native/manifest.json"))
    if not path.is_file():
        pytest.skip("Build native setup or configure SCRABBLE_MAGPIE_MANIFEST")
    return MagpieSettings.from_manifest(path)


def config(settings, seed=42):
    return GameConfig(game_id="adapter-fixture", seed=seed, lexicon_id=settings.lexicon_id,
                      ruleset=RulesetConfig(ruleset_id="english-test-v1", scoreless_turn_limit=6))


def position(settings, own="?AEIQRT", opponent="BCDFGHJ", board=None, scores=(0, 0), scoreless=0):
    board = board or Board()
    cfg = config(settings)
    draws = initial_draws(cfg)[2]
    bag = tuple(sorted(STANDARD_ENGLISH_INVENTORY.without(board.physical_tiles + tuple(own) + tuple(opponent)).as_counter().elements()))
    return GameState(game_id=cfg.game_id, revision=0, board=board, racks=(tuple(own), tuple(opponent)),
                     draw_state=DrawState(ordered_tiles=bag, rng_algorithm=draws.rng_algorithm, rng_state=draws.rng_state),
                     scores=scores, active_player=0, scoreless_turns=scoreless,
                     ruleset_id=cfg.ruleset.ruleset_id, lexicon_id=settings.lexicon_id)


def placement(word, row=7, column=7, vertical=False):
    return PlacementMove(tiles=tuple(BoardTile(row=row + index * int(vertical),
                                               column=column + index * int(not vertical),
                                               displayed_letter=letter.upper(), is_blank=letter.islower())
                                     for index, letter in enumerate(word)))


def run(scenario):
    asyncio.run(scenario)


def test_preview_scores_blank_bingo_crossword_and_premium_reuse(settings):
    async def scenario():
        engine = MagpieAnalysisEngine(settings)
        try:
            state = position(settings)
            observation = build_observation(state, 0)
            await engine.load_observation(observation)
            for move, score in [(placement("QI"), 22), (placement("qI"), 2), (placement("QI", vertical=True), 22)]:
                preview = await engine.preview(move)
                assert preview.exact_score == score
                assert len(preview.board_after.tiles) == 2
            assert canonical_bytes(observation) == canonical_bytes(build_observation(state, 0))
            state = position(settings, own="AEINRST")
            await engine.load_observation(build_observation(state, 0))
            preview = await engine.preview(placement("RETINAS", column=4))
            assert preview.exact_score == 64 and not preview.leave
            state = position(settings, board=Board(tiles=placement("AT").tiles))
            await engine.load_observation(build_observation(state, 0))
            assert (await engine.preview(placement("E", column=9))).exact_score == 3
            board = Board(tiles=placement("RAT").tiles + placement("AT", row=8, vertical=True).tiles)
            state = position(settings, board=board)
            await engine.load_observation(build_observation(state, 0))
            assert (await engine.preview(placement("I", row=8, column=8))).exact_score == 6
        finally:
            await engine.close()
            await engine.close()
    run(scenario())


def test_invalid_phony_disconnected_and_unowned_moves_leave_state_unchanged(settings):
    async def scenario():
        arbiter = MagpieArbiterEngine(settings)
        try:
            original = position(settings)
            await arbiter.restore(make_checkpoint(original, config(settings), settings))
            for move in [placement("QA"), placement("AT", row=0, column=0), placement("ZZ")]:
                with pytest.raises(InvalidMoveError):
                    await arbiter.commit(move, 0)
                assert canonical_bytes((await arbiter.checkpoint()).state) == canonical_bytes(original)
            accepted = await arbiter.commit(placement("qI"), 0)
            assert accepted.scores == (2, 0) and accepted.board.tiles[0].is_blank
            assert len(accepted.racks[0]) == 7 and len(accepted.draw_state.ordered_tiles) == 84
        finally:
            await arbiter.close()
    run(scenario())


def test_generation_ids_filters_and_restricted_seeded_analysis(settings):
    async def scenario():
        engine = MagpieAnalysisEngine(settings)
        try:
            observation = build_observation(position(settings), 0)
            await engine.load_observation(observation)
            pool = await engine.generate_candidates(CandidateConfig(pool_size=12, display_count=6))
            assert len(pool) <= 12 and any(isinstance(candidate.move, PassMove) for candidate in pool)
            assert len({candidate.candidate_id for candidate in pool}) == len(pool)
            chosen = tuple(candidate.candidate_id for candidate in pool[:3])
            limits = AnalysisLimits(max_work_units=12, work_unit=WORK_UNIT, max_elapsed_seconds=10.0)
            first = await engine.analyze(chosen, limits, 42)
            second = await engine.analyze(chosen, limits, 42)
            assert first.values == second.values
            assert first.work_used.count == second.work_used.count == 12
            assert first.completion_status == "work_limit"
            assert {value.candidate_id for value in first.values} == set(chosen)
            assert first.metric_definition.value_kind == "estimate"
            for candidate in pool:
                assert candidate.observation_id == observation.observation_id
                assert (await engine.preview(candidate.move)).exact_score == candidate.exact_score
            await engine.load_observation(observation)
            with pytest.raises(InvalidRequestError):
                await engine.analyze(chosen, limits, 42)
            filtered = await engine.generate_candidates(CandidateConfig(pool_size=10, display_count=5,
                                        include_pass=False, include_exchanges=False))
            assert all(isinstance(candidate.move, PlacementMove) for candidate in filtered)
        finally:
            await engine.close()
    run(scenario())


def test_concurrent_stale_commits_and_terminal_pass_penalties(settings):
    async def scenario():
        arbiter = MagpieArbiterEngine(settings)
        try:
            initial = await arbiter.start_game(config(settings))
            results = await asyncio.gather(arbiter.commit(PassMove(), 0), arbiter.commit(PassMove(), 0), return_exceptions=True)
            assert sum(isinstance(result, GameState) for result in results) == 1
            assert sum(isinstance(result, StaleRevisionError) for result in results) == 1
            state = (await arbiter.checkpoint()).state
            for revision in range(1, 6):
                state = await arbiter.commit(PassMove(), revision)
            assert state.status == "finished" and state.terminal_reason == "scoreless_limit"
            assert state.scores == tuple(-rack_points(rack) for rack in initial.racks)
            assert state.public_history[-1].scores_after == state.scores
            with pytest.raises(InvalidRequestError):
                await arbiter.commit(PassMove(), state.revision)
        finally:
            await arbiter.close()
    run(scenario())


def test_checkpoint_preserves_placement_and_exchange_draws_and_can_restart(settings):
    async def scenario():
        left, right = MagpieArbiterEngine(settings), MagpieArbiterEngine(settings)
        try:
            initial = position(settings)
            await left.restore(make_checkpoint(initial, config(settings), settings))
            saved = Checkpoint.model_validate_json((await left.checkpoint()).model_dump_json())
            await right.restore(saved)
            for move in [placement("QI"), ExchangeMove(tiles=("B", "C"))]:
                first = await left.commit(move, (await left.checkpoint()).state.revision)
                second = await right.commit(move, (await right.checkpoint()).state.revision)
                assert canonical_bytes(first) == canonical_bytes(second)
            assert first.public_history[-1].move.tile_count == 2
            assert first.draw_state.rng_state != initial.draw_state.rng_state
            await right.close()
            right = MagpieArbiterEngine(settings)
            restored = await right.restore(await left.checkpoint())
            assert canonical_bytes(restored) == canonical_bytes(first)
            move = ExchangeMove(tiles=(first.racks[first.active_player][0],))
            a = await left.commit(move, first.revision)
            b = await right.commit(move, restored.revision)
            assert canonical_bytes(a) == canonical_bytes(b)
        finally:
            await left.close()
            await right.close()
    run(scenario())


def test_leakage_invariance_through_concrete_analysis(settings):
    async def scenario():
        engine = MagpieAnalysisEngine(settings)
        try:
            states = [position(settings), position(settings, opponent="UUUWWYZ")]
            results, pools = [], []
            for state in states:
                observation = build_observation(state, 0)
                await engine.load_observation(observation)
                pool = await engine.generate_candidates(CandidateConfig(pool_size=8, display_count=4))
                pools.append(pool)
                results.append(await engine.analyze(tuple(candidate.candidate_id for candidate in pool[:2]),
                         AnalysisLimits(max_work_units=20, work_unit=WORK_UNIT, max_elapsed_seconds=10.0), 73))
            assert pools[0] == pools[1] and results[0].values == results[1].values
            assert results[0].work_used == results[1].work_used
        finally:
            await engine.close()
    run(scenario())


def test_checkpoint_tampering_configuration_and_legacy_rejected(settings):
    async def scenario():
        arbiter = MagpieArbiterEngine(settings)
        try:
            await arbiter.start_game(config(settings))
            saved = await arbiter.checkpoint()
            tampered = saved.model_dump(mode="python")
            state = saved.state.model_dump(mode="python")
            state["draw_state"] = DrawState(ordered_tiles=saved.state.draw_state.ordered_tiles[::-1],
                              rng_algorithm=saved.state.draw_state.rng_algorithm, rng_state=saved.state.draw_state.rng_state)
            tampered["state"] = GameState.model_validate(state)
            with pytest.raises(EngineConfigurationError):
                await arbiter.restore(Checkpoint.model_validate(tampered))
            with pytest.raises(EngineConfigurationError):
                await arbiter.restore(Checkpoint(checkpoint_id="legacy", engine_commit=settings.engine_commit, state=saved.state))
            assert canonical_bytes((await arbiter.checkpoint()).state) == canonical_bytes(saved.state)
        finally:
            await arbiter.close()
    run(scenario())


def test_blank_exchange_leave_and_draw_before_return(settings):
    async def scenario():
        analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
        try:
            state = position(settings, own="?AIILOU")
            await analysis.load_observation(build_observation(state, 0))
            pool = await analysis.generate_candidates(CandidateConfig(pool_size=100, display_count=10))
            blanks = [candidate for candidate in pool if isinstance(candidate.move, ExchangeMove) and "?" in candidate.move.tiles]
            assert blanks
            for candidate in blanks:
                assert "?" not in candidate.leave
                preview = await analysis.preview(candidate.move)
                assert preview.leave == candidate.leave
            await arbiter.restore(make_checkpoint(state, config(settings), settings))
            exchanged = await arbiter.commit(ExchangeMove(tiles=("?",)), 0)
            expected = Counter(state.racks[0])
            expected.subtract(("?",))
            expected.update(state.draw_state.ordered_tiles[:1])
            assert Counter(exchanged.racks[0]) == +expected
            assert len(exchanged.draw_state.ordered_tiles) == len(state.draw_state.ordered_tiles)
        finally:
            await analysis.close()
            await arbiter.close()
    run(scenario())


def synthetic_late_position(settings, own, opponent, bag):
    # Synthetic inventory fixture for transitions, not a historical legal board.
    # Keep the scored RAT region clear of unrelated board letters.
    remaining = STANDARD_ENGLISH_INVENTORY.without(tuple(own + opponent + bag + "R")).as_counter()
    squares = [(row, col) for row in range(6) for col in range(15)]
    squares += [(14, col) for col in range(15)]
    tiles = [BoardTile(row=7, column=7, displayed_letter="R")]
    tiles.extend(BoardTile(row=row, column=col, displayed_letter="A" if tile == "?" else tile,
                           is_blank=tile == "?") for (row, col), tile in zip(squares, sorted(remaining.elements())))
    return position(settings, own=own, opponent=opponent, board=Board(tiles=tuple(tiles)))


def test_small_bag_exchange_gate_and_bag_empty_rackout_penalties(settings):
    async def scenario():
        analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
        try:
            small = synthetic_late_position(settings, "?AEIQRT", "BCDFGHJ", "EEEEEE")
            assert len(small.draw_state.ordered_tiles) == 6
            await analysis.load_observation(build_observation(small, 0))
            pool = await analysis.generate_candidates(CandidateConfig(pool_size=20, display_count=10))
            assert pool and not any(isinstance(candidate.move, ExchangeMove) for candidate in pool)
            with pytest.raises(InvalidMoveError):
                await analysis.preview(ExchangeMove(tiles=("A",)))
            with pytest.raises(UnsupportedOperationError):
                await analysis.analyze((pool[0].candidate_id,),
                        AnalysisLimits(max_work_units=2, work_unit=WORK_UNIT, max_elapsed_seconds=5.0), 1)
            await arbiter.restore(make_checkpoint(small, config(settings), settings))
            with pytest.raises(InvalidMoveError):
                await arbiter.commit(ExchangeMove(tiles=("A",)), 0)
            assert (await arbiter.checkpoint()).state == small
            terminal = synthetic_late_position(settings, "AT", "QZ", "")
            assert not terminal.draw_state.ordered_tiles
            observation = build_observation(terminal, 0)
            await analysis.load_observation(observation)
            assert (await analysis.preview(placement("AT", column=8))).exact_score == 3
            await arbiter.restore(make_checkpoint(terminal, config(settings), settings))
            finished = await arbiter.commit(placement("AT", column=8), 0)
            assert finished.status == "finished" and finished.terminal_reason == "rack_out"
            assert finished.scores == (23, -20)
        finally:
            await analysis.close()
            await arbiter.close()
    run(scenario())


def test_work_validation_and_single_candidate_analysis(settings):
    async def scenario():
        engine = MagpieAnalysisEngine(settings)
        try:
            await engine.load_observation(build_observation(position(settings), 0))
            pool = await engine.generate_candidates(CandidateConfig(pool_size=3, display_count=3))
            ids = tuple(candidate.candidate_id for candidate in pool)
            for requested, work, unit in [(ids, 1, WORK_UNIT), ((ids[0], ids[0]), 4, WORK_UNIT),
                                           (ids[:1], 4, "iterations"), (("cand_v1_" + "f" * 64,), 4, WORK_UNIT)]:
                with pytest.raises(InvalidRequestError):
                    await engine.analyze(requested, AnalysisLimits(max_work_units=work, work_unit=unit,
                                                                 max_elapsed_seconds=5.0), 1)
            result = await engine.analyze(ids[:1], AnalysisLimits(max_work_units=3, work_unit=WORK_UNIT,
                                                                  max_elapsed_seconds=5.0), 1)
            assert len(result.values) == 1 and result.work_used.count <= 3
        finally:
            await engine.close()
    run(scenario())


def test_timeout_cooperative_cancel_task_cancel_and_reuse(settings):
    async def scenario():
        engine = MagpieAnalysisEngine(settings)
        try:
            await engine.load_observation(build_observation(position(settings), 0))
            pool = await engine.generate_candidates(CandidateConfig(pool_size=5, display_count=5))
            ids = tuple(candidate.candidate_id for candidate in pool[:3])
            result = await engine.analyze(ids, AnalysisLimits(max_work_units=100000000, work_unit=WORK_UNIT,
                                                             max_elapsed_seconds=0.08), 2)
            assert result.completion_status == "timeout" and result.work_used.count < 100000000
            assert result.elapsed_time_seconds < 1.5
            limits = AnalysisLimits(max_work_units=100000000, work_unit=WORK_UNIT, max_elapsed_seconds=10.0)
            task = asyncio.create_task(engine.analyze(ids, limits, 2))
            await asyncio.sleep(0.08)
            await engine.cancel()
            result = await task
            assert result.completion_status == "cancelled"
            assert (await engine.preview(placement("QI"))).exact_score == 22
            task = asyncio.create_task(engine.analyze(ids, limits, 2))
            await asyncio.sleep(0.04)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert engine._worker.pid is None
            assert (await engine.preview(placement("QI"))).exact_score == 22
        finally:
            await engine.close()
    run(scenario())


def test_native_crash_isolated_and_next_request_restarts(settings):
    async def scenario():
        engine = MagpieAnalysisEngine(settings)
        try:
            await engine.load_observation(build_observation(position(settings), 0))
            pool = await engine.generate_candidates(CandidateConfig(pool_size=5, display_count=5))
            task = asyncio.create_task(engine.analyze(tuple(candidate.candidate_id for candidate in pool[:3]),
                    AnalysisLimits(max_work_units=100000000, work_unit=WORK_UNIT, max_elapsed_seconds=5.0), 4))
            await asyncio.sleep(0.04)
            old_pid = engine._worker.pid
            engine._worker._process.kill()
            with pytest.raises(EngineCrashedError):
                await task
            assert (await engine.preview(placement("QI"))).exact_score == 22
            assert engine._worker.pid != old_pid
        finally:
            await engine.close()
    run(scenario())


def test_zero_score_blank_placement_counts_toward_scoreless_ending(settings):
    async def scenario():
        arbiter = MagpieArbiterEngine(settings)
        try:
            state = position(settings, own="??AEIRT", scoreless=5)
            await arbiter.restore(make_checkpoint(state, config(settings), settings))
            result = await arbiter.commit(placement("at"), 0)
            assert result.public_history[-1].score == 0
            assert result.status == "finished" and result.terminal_reason == "scoreless_limit"
            assert result.scoreless_turns == 6
        finally:
            await arbiter.close()
    run(scenario())


def test_complete_engine_only_game_with_midgame_recovery(settings, tmp_path):
    project = Path(__file__).parents[2]
    manifest = Path(os.environ.get("SCRABBLE_MAGPIE_MANIFEST", project / ".native/manifest.json"))
    report = tmp_path / "game.json"
    result = subprocess.run([sys.executable, str(project / "scripts/demo_engine_game.py"),
                             "--manifest", str(manifest), "--output", str(report)],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["checkpoint_restored"] and data["terminal_reason"] == "scoreless_limit"
    assert len(data["turns"]) == 33 and data["final_scores"] == [10, 30]
    assert data["turns"][-1]["status"] == "finished"


def test_timed_out_commit_preserves_authoritative_state(settings):
    async def scenario():
        constrained = MagpieSettings(library=settings.library, data=settings.data,
                                     operation_timeout_seconds=0.001)
        arbiter = MagpieArbiterEngine(constrained)
        try:
            initial = await arbiter.start_game(config(settings))
            with pytest.raises(EngineTimeoutError):
                await arbiter.commit(PassMove(), 0)
            assert (await arbiter.checkpoint()).state == initial
        finally:
            await arbiter.close()
    run(scenario())
