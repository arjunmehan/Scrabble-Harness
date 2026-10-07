"""Spawned workers with bounded JSON IPC, correlated replies, and host deadlines."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
from typing import Annotated, Literal, Self
import uuid

from pydantic import Field, model_validator

from ..domain import AnalysisLimits, Move, PlayerObservation, canonical_bytes
from ..domain._base import DomainModel
from .codec import move_to_native, observation_to_cgp
from .errors import (
    EngineConfigurationError, EngineCrashedError, EngineError, EngineProtocolError,
    EngineTimeoutError, InvalidMoveError, InvalidRequestError,
)
from .settings import MagpieSettings, WORK_UNIT
from .trace import TraceJournal

WIRE_VERSION = 1
MAX_WIRE_BYTES = 16 * 1024 * 1024
RequestId = Annotated[str, Field(pattern=r"^req_[0-9a-f]{32}$")]
WireVersion = Annotated[int, Field(ge=1, le=1)]


class NativeRequest(DomainModel):
    operation: Literal["generate", "preview", "analyze"]
    observation: PlayerObservation
    moves: Annotated[tuple[Move, ...], Field(max_length=10000)] = ()
    pool_size: Annotated[int, Field(ge=1, le=10000)] = 40
    include_pass: bool = True
    limits: AnalysisLimits | None = None
    seed: Annotated[int, Field(ge=0, le=2**64 - 1)] = 0

    @model_validator(mode="after")
    def operation_constraints(self) -> Self:
        if self.operation == "generate":
            if self.moves or self.limits is not None or self.seed:
                raise ValueError("Generation cannot contain moves, analysis limits, or seed")
        else:
            if self.pool_size != 40 or not self.include_pass:
                raise ValueError("Pool settings belong only to generation")
            if not self.moves or len({canonical_bytes(move) for move in self.moves}) != len(self.moves):
                raise ValueError("Preview/analysis requires distinct moves")
        if self.operation == "preview":
            if len(self.moves) != 1 or self.limits is not None or self.seed:
                raise ValueError("Preview requires exactly one move and no analysis options")
        if self.operation == "analyze":
            if (self.limits is None or self.limits.work_unit != WORK_UNIT
                    or not len(self.moves) <= self.limits.max_work_units <= 2**63 - 1):
                raise ValueError("Analysis requires supported, bounded candidate-rollout work")
            if self.observation.bag_size < 7 or self.observation.opponent_rack_size != 7:
                raise ValueError("Simulation requires a full unknown rack and at least seven bag tiles")
        return self


class NativeReply(DomainModel):
    output: Annotated[str, Field(max_length=8 * 1024 * 1024)]
    status: Literal["complete", "timeout", "cancelled"] = "complete"
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    request_id: RequestId | None = None


class Dispatch(DomainModel):
    wire_version: WireVersion = WIRE_VERSION
    request_id: RequestId
    request: NativeRequest


class Response(DomainModel):
    wire_version: WireVersion = WIRE_VERSION
    request_id: RequestId
    reply: NativeReply | None = None
    error: Literal["EngineConfigurationError", "EngineCrashedError", "EngineError",
                   "EngineProtocolError", "EngineTimeoutError", "InvalidMoveError", "InvalidRequestError"] | None = None
    message: Annotated[str, Field(max_length=2000)] | None = None

    @model_validator(mode="after")
    def one_outcome(self) -> Self:
        if (self.reply is None) == (self.error is None) or (self.error is None) != (self.message is None):
            raise ValueError("Response requires either a reply or an error/message pair")
        if self.reply is not None and self.reply.request_id is not None:
            raise ValueError("Only the response envelope supplies the wire request ID")
        return self


class Ready(DomainModel):
    wire_version: WireVersion = WIRE_VERSION
    ready: bool
    configuration_id: str


class StopControl(DomainModel):
    wire_version: WireVersion = WIRE_VERSION
    control: Literal["stop"] = "stop"
    request_id: RequestId
    reason: Literal["timeout", "cancelled"]


_ERRORS = {cls.__name__: cls for cls in (
    EngineConfigurationError, EngineCrashedError, EngineError, EngineProtocolError,
    EngineTimeoutError, InvalidMoveError, InvalidRequestError,
)}


def _send_record(connection, record: DomainModel) -> None:
    data = record.model_dump_json().encode()
    if len(data) > MAX_WIRE_BYTES:
        raise EngineProtocolError("Worker message exceeds the wire size limit")
    connection.send_bytes(data)


def _execute(connection, settings: MagpieSettings, dispatch: Dispatch, settings_file: Path) -> NativeReply:
    # Only this child constructs a native handle and imports ctypes.
    from .magpie_ffi import MagpieCommandError, MagpieSession

    request = dispatch.request
    if settings.fingerprint() != settings.configuration_id:
        raise EngineConfigurationError("Native inputs changed after settings were created")
    with MagpieSession(settings.library, settings.data, settings=settings_file) as native:
        try:
            native.run(f"set -lex {settings.lexicon_id} -leaves {settings.lexicon_id} "
                       "-wmp false -savesettings false -shwithmoves false "
                       "-maxnumdplays 10001 -threads 1 -showbu false -sinfer false "
                       "-uspread 0 -minp 1 -seed 0 -tlim 1000000000")
        except MagpieCommandError as exc:
            raise EngineConfigurationError(exc.details) from exc
        native.run("cgp " + observation_to_cgp(request.observation, allow_late_game=True))
        if request.operation == "generate":
            output = native.run(f"generate -numplays {request.pool_size}")
            if request.include_pass:
                native.run("addmoves pass")
                output = native.run("shmoves")
            return NativeReply(output=output)
        notation = ",".join(move_to_native(move, request.observation.board) for move in request.moves)
        try:
            warnings = native.run("addmoves " + notation)
        except MagpieCommandError as exc:
            raise InvalidMoveError(exc.details) from exc
        if warnings.strip():
            raise InvalidMoveError(warnings.strip())
        if request.operation == "preview":
            return NativeReply(output=native.run("shmoves"))
        native.start(f"simulate - -it {request.limits.max_work_units} -minp 1 "
                     f"-plies 2 -scond none -threads 1 -seed {request.seed}")
        status = "complete"
        while True:
            if connection.poll():
                control = StopControl.model_validate_json(connection.recv_bytes(MAX_WIRE_BYTES))
                if control.request_id != dispatch.request_id:
                    raise EngineProtocolError("Unexpected or unrelated worker stop message")
                status = control.reason
                native.stop()
                break
            if native.thread_status() == 3:
                break
            time.sleep(0.002)
        return NativeReply(output=native.await_output(), status=status)


def _worker_main(connection, settings: MagpieSettings) -> None:
    """Byte-only JSON transport; no private state/checkpoint fits the schema."""
    try:
        if settings.fingerprint() != settings.configuration_id:
            raise EngineConfigurationError("Native inputs changed after settings were created")
        _send_record(connection, Ready(ready=True, configuration_id=settings.configuration_id))
        with tempfile.TemporaryDirectory(prefix="scrabble-worker-") as directory:
            settings_file = Path(directory) / "empty-settings.txt"
            settings_file.touch()
            while True:
                data = connection.recv_bytes(MAX_WIRE_BYTES)
                if json.loads(data) == {"control": "close", "wire_version": WIRE_VERSION}:
                    return
                dispatch = Dispatch.model_validate_json(data)
                try:
                    reply = _execute(connection, settings, dispatch, settings_file)
                    response = Response(request_id=dispatch.request_id, reply=reply)
                except EngineError as exc:
                    response = Response(request_id=dispatch.request_id, error=type(exc).__name__, message=str(exc)[:2000])
                except Exception as exc:
                    response = Response(request_id=dispatch.request_id, error="EngineError",
                                        message=f"Native worker failed: {exc}"[:2000])
                _send_record(connection, response)
                # Fresh handle per request prevents stale lists/results/settings.
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception as exc:
        try:
            connection.send_bytes(json.dumps({"error": "EngineConfigurationError", "message": str(exc)[:2000]}).encode())
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class NativeWorker:
    """One serialized spawned process per adapter; only the child loads C code."""

    def __init__(self, settings: MagpieSettings, *, trace: TraceJournal | None = None,
                 role: Literal["analysis", "arbiter", "worker"] = "worker") -> None:
        self.settings, self.trace, self.role = settings, trace, role
        self.worker_id = "worker_" + uuid.uuid4().hex
        self.trace_failure_count = 0
        self._lock = asyncio.Lock()
        self._process = self._connection = None
        self._closed = False
        self._cancel = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def record(self, event_type: str, **fields) -> None:
        if self.trace is not None:
            try:
                self.trace.append(worker_id=self.worker_id, configuration_id=self.settings.configuration_id,
                                  role=self.role, event_type=event_type, **fields)
            except (OSError, ValueError):
                # Diagnostics cannot strand native work or prevent a state commit.
                self.trace_failure_count += 1

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _discard(self, reason: str = "crashed") -> None:
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            connection.close()
        if process is not None:
            pid = process.pid
            if process.is_alive():
                process.kill()
            process.join(timeout=0.2)
            if not process.is_alive():
                process.close()
            self.record("worker_retired", pid=pid, retirement_reason=reason)

    async def _receive(self, deadline: float) -> bytes:
        connection, process = self._connection, self._process
        while True:
            try:
                if connection.poll():
                    return await asyncio.wait_for(asyncio.to_thread(connection.recv_bytes, MAX_WIRE_BYTES),
                                                  timeout=max(0.001, deadline - time.monotonic()))
            except TimeoutError as exc:
                raise EngineTimeoutError("Native worker response exceeded its deadline") from exc
            except OSError as exc:
                if str(exc) == "bad message length":
                    raise EngineProtocolError("Native worker response exceeds the wire size limit") from exc
                raise EngineCrashedError("Native worker disconnected") from exc
            except EOFError as exc:
                raise EngineCrashedError("Native worker disconnected") from exc
            if not process.is_alive():
                raise EngineCrashedError("Native worker exited unexpectedly")
            if time.monotonic() >= deadline:
                raise EngineTimeoutError("Native worker exceeded its deadline")
            await asyncio.sleep(0.002)

    async def _ensure_started(self, deadline: float) -> None:
        if self._process is not None and not self._process.is_alive():
            self._discard()
        if self._process is not None:
            return
        import multiprocessing
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_worker_main, args=(child, self.settings), daemon=True)
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        self._process, self._connection = process, parent
        try:
            data = await self._receive(min(deadline, time.monotonic() + self.settings.startup_timeout_seconds))
            message = json.loads(data)
            if isinstance(message, dict) and message.get("error") == "EngineConfigurationError":
                raise EngineConfigurationError(message.get("message", "Native startup failed"))
            ready = Ready.model_validate_json(data)
            if not ready.ready or ready.configuration_id != self.settings.configuration_id:
                raise EngineProtocolError("Invalid native worker startup response")
            self.record("worker_started", pid=process.pid)
        except BaseException:
            self._discard("startup")
            raise

    async def request(self, request: NativeRequest) -> NativeReply:
        request = NativeRequest.model_validate(request)
        dispatch = Dispatch(request_id="req_" + uuid.uuid4().hex, request=request)
        data = dispatch.model_dump_json().encode()
        if len(data) > MAX_WIRE_BYTES:
            raise InvalidRequestError("Request exceeds the wire size limit")
        async with self._lock:
            if self._closed:
                raise InvalidRequestError("Native worker is closed")
            started = time.monotonic()
            timeout = (request.limits.max_elapsed_seconds if request.operation == "analyze"
                       else self.settings.operation_timeout_seconds)
            deadline = started + timeout
            context = dict(request_id=dispatch.request_id, game_id=request.observation.game_id,
                           revision=request.observation.revision, observation_id=request.observation.observation_id,
                           operation=request.operation)
            self.record("request_started", **context, max_elapsed_seconds=timeout,
                        max_work_units=request.limits.max_work_units if request.limits else None,
                        seed=request.seed if request.operation == "analyze" else None)
            self._cancel = asyncio.Event()
            stop_reason = None
            try:
                await self._ensure_started(deadline)
                connection = self._connection
                await asyncio.wait_for(asyncio.to_thread(connection.send_bytes, data),
                                       timeout=max(0.001, deadline - time.monotonic()))
                while True:
                    now = time.monotonic()
                    if self._cancel.is_set() or now >= deadline:
                        stop_reason = "cancelled" if self._cancel.is_set() else "timeout"
                        if request.operation != "analyze":
                            raise EngineTimeoutError(f"Native operation {stop_reason}")
                        _send_record(connection, StopControl(reason=stop_reason, request_id=dispatch.request_id))
                        message = await self._receive(now + self.settings.stop_grace_seconds)
                        break
                    if connection.poll():
                        message = await self._receive(deadline)
                        break
                    if not self._process.is_alive():
                        raise EngineCrashedError("Native worker exited unexpectedly")
                    await asyncio.sleep(0.002)
                response = Response.model_validate_json(message)
                if response.request_id != dispatch.request_id:
                    raise EngineProtocolError("Native response belongs to a different request")
                if response.error:
                    raise _ERRORS[response.error](response.message)
                reply = response.reply
                if request.operation != "analyze" and reply.status != "complete":
                    raise EngineProtocolError("Incomplete reply cannot validate a move or candidate pool")
                status = stop_reason or reply.status
                if status != "complete":
                    # Retire even cooperative children: completion can race stop.
                    self._discard(status)
                elapsed = time.monotonic() - started
                self.record("request_finished", **context, elapsed_seconds=elapsed, completion_status=status)
                return NativeReply(output=reply.output, status=status, elapsed_seconds=elapsed,
                                   request_id=dispatch.request_id)
            except BaseException as exc:
                category = type(exc).__name__
                if isinstance(exc, asyncio.CancelledError):
                    self._discard("cancelled")
                elif isinstance(exc, (EngineTimeoutError, TimeoutError)):
                    self._discard(stop_reason or "timeout")
                elif isinstance(exc, (OSError, EOFError, EngineCrashedError)):
                    self._discard("crashed")
                elif isinstance(exc, (ValueError, KeyError, EngineProtocolError)):
                    self._discard("protocol")
                elif not isinstance(exc, EngineError):
                    self._discard("protocol")
                elif stop_reason:
                    self._discard(stop_reason)
                self.record("request_failed", **context, elapsed_seconds=time.monotonic() - started,
                            completion_status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                            error_category=category)
                if isinstance(exc, TimeoutError):
                    raise EngineTimeoutError("Native request exceeded its deadline") from exc
                if isinstance(exc, (EOFError, OSError)):
                    raise EngineCrashedError("Native worker disconnected") from exc
                if isinstance(exc, (ValueError, KeyError)):
                    raise EngineProtocolError("Invalid native worker response") from exc
                raise
            finally:
                self._cancel = None

    async def close(self) -> None:
        self._closed = True
        self.cancel()
        async with self._lock:
            try:
                if self._connection is not None and self._process.is_alive():
                    self._connection.send_bytes(b'{"control":"close","wire_version":1}')
                    await asyncio.to_thread(self._process.join, 0.2)
            except (OSError, EOFError):
                pass
            finally:
                self._discard("closed")
                self.record("worker_closed")
