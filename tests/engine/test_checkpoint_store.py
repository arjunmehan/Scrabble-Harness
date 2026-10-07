import asyncio
from concurrent.futures import ThreadPoolExecutor
import os

import pytest

from scrabble_harness.domain import GameConfig, RulesetConfig, canonical_bytes
from scrabble_harness.engine import (
    CheckpointConflictError, CheckpointStore, EngineConfigurationError, EngineError,
    InvalidCheckpointError, MagpieArbiterEngine, make_checkpoint,
)
from test_worker_recovery import fake_settings


def checkpoint(settings, seed=42):
    async def create():
        arbiter = MagpieArbiterEngine(settings)
        try:
            await arbiter.start_game(GameConfig(game_id='../../untrusted-game-id', seed=seed,
                lexicon_id=settings.lexicon_id,
                ruleset=RulesetConfig(ruleset_id='english-test-v1', scoreless_turn_limit=6)))
            return await arbiter.checkpoint()
        finally:
            await arbiter.close()
    return asyncio.run(create())


def test_atomic_private_store_roundtrip_idempotence_and_ignored_pending(tmp_path):
    settings = fake_settings(tmp_path / 'inputs')
    store = CheckpointStore(tmp_path / 'private', settings)
    record = checkpoint(settings)
    path = store.save(record)
    assert path.is_relative_to(store.root) and path.stat().st_mode & 0o777 == 0o600
    assert canonical_bytes(store.load(record.state.game_id)) == canonical_bytes(record)
    assert store.save(record) == path
    (path.parent / '.pending-interrupted-write').write_text('incomplete')
    assert store.load(record.state.game_id).checkpoint_id == record.checkpoint_id
    assert len(list(path.parent.glob('*.json'))) == 1
    with pytest.raises(InvalidCheckpointError):
        store.load('absent')
    with pytest.raises(InvalidCheckpointError):
        store.load(record.state.game_id, True)


def test_competing_writers_cannot_overwrite_game_revision(tmp_path):
    settings = fake_settings(tmp_path / 'inputs')
    stores = [CheckpointStore(tmp_path / 'private', settings) for _ in range(2)]
    records = [checkpoint(settings, seed) for seed in (1, 2)]
    def write(index):
        try:
            stores[index].save(records[index])
            return 'saved'
        except CheckpointConflictError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == ['conflict', 'saved']
    saved = stores[0].load(records[0].state.game_id)
    assert saved.checkpoint_id in {record.checkpoint_id for record in records}


def test_corrupt_newest_revision_is_rejected_without_rollback(tmp_path):
    settings = fake_settings(tmp_path / 'inputs')
    store = CheckpointStore(tmp_path / 'private', settings)
    record = checkpoint(settings)
    path = store.save(record)
    (path.parent / '00000000000000000001.json').write_text('truncated')
    with pytest.raises(InvalidCheckpointError):
        store.load(record.state.game_id)
    assert store.load(record.state.game_id, 0).checkpoint_id == record.checkpoint_id
    data = record.model_dump(mode='python')
    data['state'] = type(record.state)(**(record.state.model_dump(mode='python') | {'scores': (1, 0)}))
    tampered = type(record)(**data)
    with pytest.raises(EngineConfigurationError):
        store.save(tampered)


def test_persistence_failure_before_publication_preserves_previous_snapshot(tmp_path, monkeypatch):
    settings = fake_settings(tmp_path / 'inputs')
    store = CheckpointStore(tmp_path / 'private', settings)
    record = checkpoint(settings)
    def fail(*args):
        raise OSError('fault before publication')
    monkeypatch.setattr(os, 'link', fail)
    with pytest.raises(EngineError):
        store.save(record)
    assert not list(store.root.rglob('*.json'))
    assert not list(store.root.rglob('.pending-*'))


def test_failure_after_publication_can_be_resolved_idempotently(tmp_path, monkeypatch):
    settings = fake_settings(tmp_path / 'inputs')
    store = CheckpointStore(tmp_path / 'private', settings)
    record = checkpoint(settings)
    sync = store._sync_directory
    def fail_directory(directory):
        if directory != store.root:
            raise OSError('fault after atomic publication')
        sync(directory)
    monkeypatch.setattr(store, '_sync_directory', fail_directory)
    with pytest.raises(EngineError):
        store.save(record)
    assert store.load(record.state.game_id).checkpoint_id == record.checkpoint_id
    monkeypatch.setattr(store, '_sync_directory', sync)
    assert store.save(record).is_file()


def test_resume_revalidates_native_inputs(tmp_path):
    settings = fake_settings(tmp_path / 'inputs')
    store = CheckpointStore(tmp_path / 'private', settings)
    record = checkpoint(settings)
    store.save(record)
    settings.library.write_text('changed native build')
    with pytest.raises(EngineConfigurationError):
        store.load(record.state.game_id)


def test_arbiter_can_resume_without_loading_native_library(tmp_path):
    settings = fake_settings(tmp_path / 'inputs')
    store = CheckpointStore(tmp_path / 'private', settings)
    record = checkpoint(settings)
    store.save(record)
    async def scenario():
        arbiter = MagpieArbiterEngine(settings, checkpoint_store=store)
        try:
            state = await arbiter.resume(record.state.game_id)
            assert canonical_bytes(state) == canonical_bytes(record.state)
            assert arbiter._worker.pid is None
        finally:
            await arbiter.close()
    asyncio.run(scenario())
