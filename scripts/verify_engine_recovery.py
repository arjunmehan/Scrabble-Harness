#!/usr/bin/env python3
"""Compare durable restart games against uninterrupted games and stress workers."""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from scrabble_harness.domain import AnalysisLimits, CandidateConfig, GameConfig, RulesetConfig, canonical_bytes
from scrabble_harness.engine import (
    CheckpointStore, EngineCrashedError, MagpieAnalysisEngine, MagpieArbiterEngine,
    MagpieSettings, TraceJournal, WORK_UNIT,
)


def config(settings, seed):
    return GameConfig(game_id=f'recovery-game-{seed}', lexicon_id=settings.lexicon_id, seed=seed,
                      ruleset=RulesetConfig(ruleset_id='english-six-scoreless-v1', scoreless_turn_limit=6))


async def play(settings, seed, root, trace, *, restart):
    store = CheckpointStore(root, settings) if restart else None
    arbiter = MagpieArbiterEngine(settings, trace=trace, checkpoint_store=store)
    analysis = MagpieAnalysisEngine(settings, trace=trace)
    identities, selections, restorations = [], [], []
    try:
        state = await arbiter.start_game(config(settings, seed))
        identities.append(hashlib.sha256(canonical_bytes(state)).hexdigest())
        for _ in range(200):
            if restart and state.revision in {3, 8, 17}:
                before = canonical_bytes(state)
                # Retire the entire arbiter and recover from disk, including its
                # future bag and exchange RNG. No in-memory checkpoint is reused.
                if arbiter._worker._process is not None:
                    arbiter._worker._process.kill()
                await arbiter.close()
                arbiter = MagpieArbiterEngine(settings, trace=trace, checkpoint_store=store)
                state = await arbiter.resume(state.game_id)
                assert canonical_bytes(state) == before
                restorations.append(state.revision)
            observation = await arbiter.observe(state.active_player)
            await analysis.load_observation(observation)
            pool = await analysis.generate_candidates(CandidateConfig(pool_size=40, display_count=12))
            assert pool, 'Missing legal fallback'
            selected = pool[0]
            selections.append(selected.candidate_id)
            state = await arbiter.commit(selected.move, observation.revision)
            identities.append(hashlib.sha256(canonical_bytes(state)).hexdigest())
            if store is not None:
                assert canonical_bytes(store.load(state.game_id).state) == canonical_bytes(state)
            if state.status == 'finished':
                break
        else:
            raise AssertionError('Game exceeded 200-turn acceptance ceiling')
        assert arbiter.trace_failure_count == analysis.trace_failure_count == 0
        return {'state_hashes': identities, 'selections': selections, 'restored_revisions': restorations,
                'turns': state.revision, 'scores': state.scores, 'terminal_reason': state.terminal_reason}
    finally:
        await arbiter.close()
        await analysis.close()


async def stress(settings, trace):
    arbiter = MagpieArbiterEngine(settings)
    analysis = MagpieAnalysisEngine(settings, trace=trace)
    outcomes = []
    try:
        state = await arbiter.start_game(config(settings, 42))
        await analysis.load_observation(await arbiter.observe(state.active_player))
        pool = await analysis.generate_candidates(CandidateConfig(pool_size=8, display_count=4))
        ids = tuple(candidate.candidate_id for candidate in pool[:3])
        short = AnalysisLimits(max_work_units=12, work_unit=WORK_UNIT, max_elapsed_seconds=5.0)
        long = AnalysisLimits(max_work_units=100000000, work_unit=WORK_UNIT, max_elapsed_seconds=5.0)
        baseline = await analysis.analyze(ids, short, 73)
        for cycle in range(3):
            timed = await analysis.analyze(ids, AnalysisLimits(max_work_units=long.max_work_units,
                                        work_unit=WORK_UNIT, max_elapsed_seconds=0.06), 73)
            assert timed.completion_status == 'timeout' and timed.work_used.count <= long.max_work_units
            assert analysis._worker.pid is None
            await analysis.generate_candidates(CandidateConfig(pool_size=8, display_count=4))
            task = asyncio.create_task(analysis.analyze(ids, long, 73))
            await asyncio.sleep(0.03)
            await analysis.cancel()
            cancelled = await task
            assert cancelled.completion_status == 'cancelled' and analysis._worker.pid is None
            await analysis.generate_candidates(CandidateConfig(pool_size=8, display_count=4))
            task = asyncio.create_task(analysis.analyze(ids, long, 73))
            await asyncio.sleep(0.03)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError('Task cancellation did not propagate')
            assert analysis._worker.pid is None
            await analysis.generate_candidates(CandidateConfig(pool_size=8, display_count=4))
            task = asyncio.create_task(analysis.analyze(ids, long, 73))
            await asyncio.sleep(0.03)
            analysis._worker._process.kill()
            try:
                await task
            except EngineCrashedError:
                pass
            else:
                raise AssertionError('Worker crash did not propagate')
            assert analysis._worker.pid is None
            recovered = await analysis.analyze(ids, short, 73)
            assert recovered.values == baseline.values and recovered.work_used == baseline.work_used
            outcomes.append({'cycle': cycle, 'timeout_reported_work': timed.work_used.count,
                             'cancel_reported_work': cancelled.work_used.count,
                             'recovered_reported_work': recovered.work_used.count,
                             'seeded_values_preserved': True,
                             'unresolved_work_requests': 2})
        assert analysis.trace_failure_count == 0
        return outcomes
    finally:
        await arbiter.close()
        await analysis.close()


async def verify(manifest, seeds, root):
    settings = MagpieSettings.from_manifest(manifest)
    trace = TraceJournal(root / 'events.jsonl', run_id='engine-recovery-acceptance')
    games = []
    for seed in seeds:
        baseline = await play(settings, seed, root / f'baseline-{seed}', trace, restart=False)
        resumed = await play(settings, seed, root / f'resumed-{seed}', trace, restart=True)
        assert baseline['state_hashes'] == resumed['state_hashes']
        assert baseline['selections'] == resumed['selections']
        games.append({'seed': seed, 'turns': resumed['turns'], 'scores': resumed['scores'],
                      'terminal_reason': resumed['terminal_reason'],
                      'restored_revisions': resumed['restored_revisions'],
                      'compared_states': len(resumed['state_hashes']), 'all_states_equal': True,
                      'all_selections_equal': True, 'final_state_sha256': resumed['state_hashes'][-1]})
    cycles = await stress(settings, trace)
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    starts = Counter(e['request_id'] for e in events if e['event_type'] == 'request_started')
    finishes = Counter(e['request_id'] for e in events if e['event_type'] in {'request_finished', 'request_failed'})
    assert starts == finishes and all(count == 1 for count in starts.values())
    return {'schema_version': 1, 'purpose': 'engine limits/recovery acceptance with toy dictionary; not a strength benchmark',
            'engine_commit': settings.engine_commit, 'configuration_id': settings.configuration_id,
            'games': games, 'stress_cycles': cycles, 'request_count': len(starts),
            'event_counts': dict(Counter(e['event_type'] for e in events)), 'request_pairing_verified': True,
            'private_state_in_trace': False,
            'limitations': ['Abrupt host power loss is not simulated; fsync publication failures use fault-injection tests.',
                            'Killed analysis has unknown completed work and is not reported as zero.',
                            'No LLM/API calls, exact endgame solver, or aggregate run-budget enforcement.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=PROJECT / '.native/manifest.json')
    parser.add_argument('--seeds', type=int, nargs='+', default=[7, 42, 73])
    parser.add_argument('--output', type=Path, default=PROJECT / 'docs/engine-recovery-results.json')
    args = parser.parse_args()
    if len(set(args.seeds)) != len(args.seeds):
        parser.error('Seeds must be distinct')
    with tempfile.TemporaryDirectory(prefix='scrabble-recovery-') as directory:
        report = asyncio.run(verify(args.manifest, args.seeds, Path(directory)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f"Verified {len(report['games'])} game pairs, {len(report['stress_cycles'])} fault cycles, {report['request_count']} requests")
    print(f"Report: {args.output}")


if __name__ == '__main__':
    main()
