import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from scrabble_harness.domain import PassMove, canonical_bytes
from scrabble_harness.engine import (
    CheckpointConflictError, CheckpointStore, EngineCrashedError, EngineError,
    MagpieArbiterEngine, MagpieSettings, StaleRevisionError,
)
from test_adapters_native import config

pytestmark = pytest.mark.native


@pytest.fixture
def settings():
    path = Path(os.environ.get('SCRABBLE_MAGPIE_MANIFEST', Path(__file__).parents[2] / '.native/manifest.json'))
    if not path.is_file():
        if 'SCRABBLE_MAGPIE_MANIFEST' in os.environ:
            pytest.fail('Explicit native manifest is missing')
        pytest.skip('Build native setup or configure SCRABBLE_MAGPIE_MANIFEST')
    return MagpieSettings.from_manifest(path)


def test_durable_commit_rejects_conflict_and_preserves_state_on_disk_failure(settings, tmp_path, monkeypatch):
    async def scenario():
        store = CheckpointStore(tmp_path, settings)
        left = MagpieArbiterEngine(settings, checkpoint_store=store)
        right = MagpieArbiterEngine(settings, checkpoint_store=store)
        try:
            await left.start_game(config(settings))
            await right.resume(config(settings).game_id)
            saved = await left.commit(PassMove(), 0)
            assert store.load(saved.game_id).state == saved
            # A different move at the same revision cannot overwrite disk.
            different = right._current().racks[0][0]
            from scrabble_harness.domain import ExchangeMove
            with pytest.raises(CheckpointConflictError):
                await right.commit(ExchangeMove(tiles=(different,)), 0)
            assert right._current().revision == 0
            await right.resume(saved.game_id)
            original = canonical_bytes(right._current())
            def fail(checkpoint):
                raise EngineError('Injected persistence failure')
            monkeypatch.setattr(store, 'save', fail)
            with pytest.raises(EngineError):
                await right.commit(PassMove(), 1)
            assert canonical_bytes(right._current()) == original
            assert store.load(saved.game_id).state.revision == 1
        finally:
            await left.close()
            await right.close()
    asyncio.run(scenario())


def test_uncertain_save_after_publication_resumes_once_without_replaying_turn(settings, tmp_path, monkeypatch):
    async def scenario():
        store = CheckpointStore(tmp_path, settings)
        arbiter = MagpieArbiterEngine(settings, checkpoint_store=store)
        try:
            initial = await arbiter.start_game(config(settings))
            sync = store._sync_directory
            def fail(directory):
                if directory != store.root:
                    raise OSError('Fault after publishing committed snapshot')
                sync(directory)
            monkeypatch.setattr(store, '_sync_directory', fail)
            with pytest.raises(EngineError):
                await arbiter.commit(PassMove(), 0)
            assert arbiter._current() == initial
            assert store.load(initial.game_id).state.revision == 1
            monkeypatch.setattr(store, '_sync_directory', sync)
            await arbiter.close()
            arbiter = MagpieArbiterEngine(settings, checkpoint_store=store)
            resumed = await arbiter.resume(initial.game_id)
            assert resumed.revision == 1 and len(resumed.public_history) == 1
            with pytest.raises(StaleRevisionError):
                await arbiter.commit(PassMove(), 0)
            updated = await arbiter.commit(PassMove(), 1)
            assert updated.revision == 2 and len(updated.public_history) == 2
        finally:
            await arbiter.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not hasattr(signal, 'SIGSTOP'), reason='Requires POSIX process suspension')
def test_worker_crash_during_validation_cannot_publish_private_turn(settings, tmp_path):
    async def scenario():
        store = CheckpointStore(tmp_path, settings)
        arbiter = MagpieArbiterEngine(settings, checkpoint_store=store)
        try:
            state = await arbiter.start_game(config(settings))
            # Warm the real native worker without making an authoritative turn.
            from scrabble_harness.engine.worker import NativeRequest
            observation = await arbiter.observe(state.active_player)
            await arbiter._worker.request(NativeRequest(operation='preview', observation=observation, moves=(PassMove(),)))
            os.kill(arbiter._worker.pid, signal.SIGSTOP)
            task = asyncio.create_task(arbiter.commit(PassMove(), 0))
            await asyncio.sleep(0.03)
            arbiter._worker._process.kill()
            with pytest.raises(EngineCrashedError):
                await task
            assert canonical_bytes(arbiter._current()) == canonical_bytes(state)
            assert canonical_bytes(store.load(state.game_id).state) == canonical_bytes(state)
            accepted = await arbiter.commit(PassMove(), 0)
            assert accepted.revision == 1 and len(accepted.public_history) == 1
        finally:
            await arbiter.close()
    asyncio.run(scenario())


def test_reachable_games_and_repeated_worker_faults_match_baselines(settings, tmp_path):
    project = Path(__file__).parents[2]
    manifest = Path(os.environ.get('SCRABBLE_MAGPIE_MANIFEST', project / '.native/manifest.json'))
    output = tmp_path / 'recovery.json'
    result = subprocess.run([sys.executable, str(project / 'scripts/verify_engine_recovery.py'),
                             '--manifest', str(manifest), '--output', str(output)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert len(report['games']) == 3 and report['request_pairing_verified']
    assert all(game['all_states_equal'] and game['all_selections_equal'] for game in report['games'])
    assert all(cycle['seeded_values_preserved'] for cycle in report['stress_cycles'])
    assert len(report['stress_cycles']) == 3


def test_changed_native_asset_fails_before_state_commit_and_can_recover(settings, tmp_path):
    import shutil
    data = tmp_path / 'data'
    for name in ('layouts/standard15.txt', 'letterdistributions/english.csv',
                 'strategy/winpct_english.csv', f'lexica/{settings.lexicon_id}.kwg',
                 f'lexica/{settings.lexicon_id}.klv2'):
        destination = data / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(settings.data / name, destination)
    configured = MagpieSettings(library=settings.library, data=data)
    from scrabble_harness.engine import EngineConfigurationError
    async def scenario():
        arbiter = MagpieArbiterEngine(configured)
        try:
            await arbiter.start_game(config(configured))
            initial = await arbiter.commit(PassMove(), 0)
            asset = data / f'lexica/{settings.lexicon_id}.kwg'
            original = asset.read_bytes()
            asset.write_bytes(original + b'tampered')
            with pytest.raises(EngineConfigurationError):
                await arbiter.commit(PassMove(), 1)
            assert canonical_bytes(arbiter._current()) == canonical_bytes(initial)
            asset.write_bytes(original)
            updated = await arbiter.commit(PassMove(), 1)
            assert updated.revision == 2
        finally:
            await arbiter.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not hasattr(signal, 'SIGSTOP'), reason='Requires POSIX process suspension')
def test_resume_queued_behind_commit_loads_newest_revision(settings, tmp_path):
    async def scenario():
        arbiter = MagpieArbiterEngine(settings, checkpoint_store=CheckpointStore(tmp_path, settings))
        try:
            state = await arbiter.start_game(config(settings))
            from scrabble_harness.engine.worker import NativeRequest
            await arbiter._worker.request(NativeRequest(operation='preview',
                observation=await arbiter.observe(state.active_player), moves=(PassMove(),)))
            os.kill(arbiter._worker.pid, signal.SIGSTOP)
            commit = asyncio.create_task(arbiter.commit(PassMove(), 0))
            await asyncio.sleep(0.02)
            resume = asyncio.create_task(arbiter.resume(state.game_id))
            await asyncio.sleep(0.02)
            os.kill(arbiter._worker.pid, signal.SIGCONT)
            committed = await commit
            resumed = await resume
            assert resumed.revision == committed.revision == 1
            assert canonical_bytes(resumed) == canonical_bytes(committed)
            assert arbiter._current().revision == 1
        finally:
            await arbiter.close()
    asyncio.run(scenario())
