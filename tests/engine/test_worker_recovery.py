"""Real spawned fault processes exercise the parent watchdog without C code."""

import asyncio
import json
from pathlib import Path
import time

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import AnalysisLimits, PassMove
from scrabble_harness.engine import (
    EngineCrashedError, EngineProtocolError, EngineTimeoutError, InvalidRequestError,
    MagpieSettings, TraceJournal, WorkerEvent, WORK_UNIT,
)
from scrabble_harness.engine import worker as module
from scrabble_harness.engine.worker import Dispatch, NativeReply, NativeRequest, NativeWorker, Response, Ready


def fake_settings(root: Path, **options) -> MagpieSettings:
    root.mkdir(parents=True, exist_ok=True)
    library = root / 'fake-library'
    library.write_text('echo')
    for name in ('layouts/standard15.txt', 'letterdistributions/english.csv',
                 'strategy/winpct_english.csv', 'lexica/NWL_HARNESS_SMOKE.kwg',
                 'lexica/NWL_HARNESS_SMOKE.klv2'):
        path = root / 'data' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('offline fault-test input')
    return MagpieSettings(library=library, data=root / 'data',
                          stop_grace_seconds=0.05, operation_timeout_seconds=3.0, **options)


def fault_worker(connection, settings):
    """Picklable test child. No native library is loaded by this process."""
    try:
        if settings.library.read_text() == 'startup-hang':
            time.sleep(30)
        connection.send_bytes(Ready(ready=True, configuration_id=settings.configuration_id).model_dump_json().encode())
        while True:
            data = connection.recv_bytes(module.MAX_WIRE_BYTES)
            if json.loads(data).get('control') == 'close':
                return
            dispatch = Dispatch.model_validate_json(data)
            mode = settings.library.read_text()
            if mode == 'crash':
                return
            if mode == 'hang' and dispatch.request.operation == 'analyze':
                # Ignores cooperative stop. Only the parent can terminate it.
                time.sleep(30)
            if mode == 'race' and dispatch.request.operation == 'analyze':
                connection.recv_bytes(module.MAX_WIRE_BYTES)
                # Complete racing with the stop packet, followed by a late reply.
                response = Response(request_id=dispatch.request_id, reply=NativeReply(output='ok'))
                connection.send_bytes(response.model_dump_json().encode())
                connection.send_bytes(response.model_dump_json().encode())
                continue
            if mode == 'oversize':
                connection.send_bytes(b'x' * (module.MAX_WIRE_BYTES + 1))
                continue
            if mode == 'malformed':
                connection.send_bytes(b'not JSON')
                continue
            request_id = 'req_' + 'f' * 32 if mode == 'wrong-id' else dispatch.request_id
            response = Response(request_id=request_id, reply=NativeReply(output='ok',
                               status='cancelled' if mode == 'incomplete' else 'complete'))
            connection.send_bytes(response.model_dump_json().encode())
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


def analysis_request(observation, seconds=0.06):
    return NativeRequest(operation='analyze', observation=observation, moves=(PassMove(),),
                         limits=AnalysisLimits(max_work_units=100, work_unit=WORK_UNIT,
                                               max_elapsed_seconds=seconds))


@pytest.mark.parametrize('updates', [
    {'operation': 'preview'},
    {'operation': 'preview', 'moves': (PassMove(), PassMove())},
    {'operation': 'generate', 'moves': (PassMove(),)},
    {'operation': 'generate', 'seed': 1},
    {'operation': 'analyze', 'moves': (PassMove(),)},
    {'operation': 'preview', 'moves': (PassMove(),), 'seed': 1},
    {'operation': 'preview', 'moves': (PassMove(),), 'pool_size': 2},
])
def test_request_rejects_inconsistent_operations(observation, updates):
    with pytest.raises(ValidationError):
        NativeRequest(observation=observation, **updates)


@pytest.mark.parametrize('work,unit', [(2**63, WORK_UNIT), (1, 'undefined')])
def test_direct_analysis_cannot_bypass_adapter_limits(observation, work, unit):
    with pytest.raises(ValidationError):
        NativeRequest(operation='analyze', observation=observation, moves=(PassMove(),),
                      limits=AnalysisLimits(max_work_units=work, work_unit=unit, max_elapsed_seconds=1.0))


@pytest.mark.parametrize('mode,error', [('wrong-id', EngineProtocolError), ('malformed', EngineProtocolError),
                                        ('oversize', EngineProtocolError), ('crash', EngineCrashedError),
                                        ('incomplete', EngineProtocolError)])
def test_faulty_reply_retires_process_and_next_request_recovers(tmp_path, observation, monkeypatch, mode, error):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    settings = fake_settings(tmp_path)
    async def scenario():
        worker = NativeWorker(settings)
        request = NativeRequest(operation='preview', observation=observation, moves=(PassMove(),))
        try:
            assert (await worker.request(request)).output == 'ok'
            pid = worker.pid
            settings.library.write_text(mode)
            with pytest.raises(error):
                await worker.request(request)
            assert worker.pid is None
            settings.library.write_text('echo')
            assert (await worker.request(request)).output == 'ok'
            assert worker.pid != pid
        finally:
            await worker.close()
    asyncio.run(scenario())


def test_unresponsive_child_hard_deadline_and_queued_request_recover(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    settings = fake_settings(tmp_path)
    async def scenario():
        worker = NativeWorker(settings)
        request = NativeRequest(operation='preview', observation=observation, moves=(PassMove(),))
        ticks = []
        async def heartbeat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)
        try:
            await worker.request(request)
            settings.library.write_text('hang')
            beat = asyncio.create_task(heartbeat())
            started = time.monotonic()
            first = asyncio.create_task(worker.request(analysis_request(observation)))
            await asyncio.sleep(0.01)
            queued = asyncio.create_task(worker.request(request))
            with pytest.raises(EngineTimeoutError):
                await first
            assert time.monotonic() - started < 1.0
            assert (await queued).output == 'ok'
            beat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await beat
            assert len(ticks) >= 3 and max(b - a for a, b in zip(ticks, ticks[1:])) < 0.3
        finally:
            await worker.close()
    asyncio.run(scenario())


def test_startup_deadline_and_cancelled_queued_call(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    settings = fake_settings(tmp_path, startup_timeout_seconds=0.05)
    async def scenario():
        worker = NativeWorker(settings)
        request = NativeRequest(operation='preview', observation=observation, moves=(PassMove(),))
        settings.library.write_text('startup-hang')
        try:
            with pytest.raises(EngineTimeoutError):
                await worker.request(request)
            assert worker.pid is None
        finally:
            await worker.close()
        worker = NativeWorker(fake_settings(tmp_path / 'second'))
        try:
            await worker.request(request)
            worker.settings.library.write_text('hang')
            active = asyncio.create_task(worker.request(analysis_request(observation, 5.0)))
            await asyncio.sleep(0.02)
            pid = worker.pid
            queued = asyncio.create_task(worker.request(request))
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert worker.pid == pid
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            assert worker.pid is None
        finally:
            await worker.close()
    asyncio.run(scenario())


def test_completion_stop_race_never_contaminates_next_request(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    settings = fake_settings(tmp_path)
    async def scenario():
        worker = NativeWorker(settings)
        preview = NativeRequest(operation='preview', observation=observation, moves=(PassMove(),))
        try:
            for _ in range(3):
                settings.library.write_text('echo')
                await worker.request(preview)
                settings.library.write_text('race')
                reply = await worker.request(analysis_request(observation))
                assert reply.status == 'timeout' and worker.pid is None
                settings.library.write_text('echo')
                next_reply = await worker.request(preview)
                assert next_reply.status == 'complete' and next_reply.request_id != reply.request_id
        finally:
            await worker.close()
    asyncio.run(scenario())


def test_close_during_analysis_rejects_queued_calls_and_reaps_child(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    settings = fake_settings(tmp_path)
    async def scenario():
        worker = NativeWorker(settings)
        preview = NativeRequest(operation='preview', observation=observation, moves=(PassMove(),))
        await worker.request(preview)
        settings.library.write_text('hang')
        active = asyncio.create_task(worker.request(analysis_request(observation, 5.0)))
        await asyncio.sleep(0.02)
        queued = asyncio.create_task(worker.request(preview))
        await worker.close()
        with pytest.raises(EngineTimeoutError):
            await active
        with pytest.raises(InvalidRequestError):
            await queued
        assert worker.pid is None
        await worker.close()
    asyncio.run(scenario())


def test_trace_is_correlated_allowlisted_and_can_resume(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    settings = fake_settings(tmp_path / 'inputs')
    path = tmp_path / 'events.jsonl'
    journal = TraceJournal(path, run_id='offline-recovery')
    async def scenario():
        worker = NativeWorker(settings, trace=journal)
        request = NativeRequest(operation='preview', observation=observation, moves=(PassMove(),))
        try:
            await worker.request(request)
            settings.library.write_text('wrong-id')
            with pytest.raises(EngineProtocolError):
                await worker.request(request)
        finally:
            await worker.close()
        assert worker.trace_failure_count == 0
    asyncio.run(scenario())
    events = [WorkerEvent.model_validate_json(line) for line in path.read_bytes().splitlines()]
    starts = [event for event in events if event.event_type == 'request_started']
    ends = [event for event in events if event.event_type in {'request_finished', 'request_failed'}]
    assert {event.request_id for event in starts} == {event.request_id for event in ends}
    assert len({event.request_id for event in starts}) == 2
    text = path.read_text()
    for forbidden in ('own_rack', 'racks', 'rng_state', 'ordered_tiles', 'output', 'message', 'moves'):
        assert '"' + forbidden + '"' not in text
    resumed = TraceJournal(path, run_id='offline-recovery')
    resumed.append(worker_id='test', configuration_id=settings.configuration_id, role='worker', event_type='worker_closed')
    assert json.loads(path.read_text().splitlines()[-1])['sequence'] == len(events) + 1
    with pytest.raises(ValueError):
        TraceJournal(path, run_id='different')


def test_trace_write_failure_does_not_strand_worker(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    journal = TraceJournal(tmp_path / 'events.jsonl', run_id='trace-failure')
    def fail(**kwargs):
        raise OSError('test filesystem failure')
    monkeypatch.setattr(journal, 'append', fail)
    async def scenario():
        worker = NativeWorker(fake_settings(tmp_path / 'inputs'), trace=journal)
        reply = await worker.request(NativeRequest(operation='preview', observation=observation, moves=(PassMove(),)))
        assert reply.output == 'ok' and worker.trace_failure_count > 0
        await worker.close()
        assert worker.pid is None
    asyncio.run(scenario())


@pytest.mark.parametrize('updates', [
    {'wire_version': True}, {'wire_version': 1.0}, {'wire_version': 2},
    {'reply': NativeReply(output='ok'), 'error': 'EngineError', 'message': 'ambiguous'},
    {'reply': None, 'error': None},
    {'reply': NativeReply(output='ok', request_id='req_' + '1' * 32)},
])
def test_response_protocol_rejects_ambiguous_or_unsupported_records(updates):
    values = {'request_id': 'req_' + '0' * 32, 'reply': NativeReply(output='ok')}
    with pytest.raises(ValidationError):
        Response(**(values | updates))


def test_oversized_request_is_rejected_before_spawning(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, 'MAX_WIRE_BYTES', 32)
    worker = NativeWorker(fake_settings(tmp_path))
    async def scenario():
        with pytest.raises(InvalidRequestError):
            await worker.request(NativeRequest(operation='generate', observation=observation))
        assert worker.pid is None
        await worker.close()
    asyncio.run(scenario())


def test_cancelled_close_still_reaps_idle_child(tmp_path, observation, monkeypatch):
    monkeypatch.setattr(module, '_worker_main', fault_worker)
    async def scenario():
        worker = NativeWorker(fake_settings(tmp_path))
        await worker.request(NativeRequest(operation='preview', observation=observation, moves=(PassMove(),)))
        entered = asyncio.Event()
        async def interrupted_join(*args):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(asyncio, 'to_thread', interrupted_join)
        task = asyncio.create_task(worker.close())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert worker.pid is None
    asyncio.run(scenario())


def test_trace_cannot_resume_a_torn_final_line(tmp_path):
    path = tmp_path / 'events.jsonl'
    journal = TraceJournal(path, run_id='torn-trace')
    journal.append(worker_id='test', configuration_id='test', role='worker', event_type='worker_closed')
    path.write_bytes(path.read_bytes().rstrip(b'\n'))
    with pytest.raises(ValueError, match='incomplete final line'):
        TraceJournal(path, run_id='torn-trace')
