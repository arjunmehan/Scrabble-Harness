"""Allowlisted worker telemetry; never serialize requests or private checkpoints."""

from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Annotated, Literal

from pydantic import Field

from ..domain._base import DomainModel, Identifier, NonNegativeInt, ObservationId


class WorkerEvent(DomainModel):
    run_id: Identifier
    worker_id: Identifier
    sequence: Annotated[int, Field(gt=0)]
    timestamp: str
    configuration_id: Identifier
    role: Literal["analysis", "arbiter", "worker"]
    event_type: Literal["request_started", "request_finished", "request_failed",
                        "worker_started", "worker_retired", "worker_closed", "analysis_result"]
    request_id: Identifier | None = None
    game_id: Identifier | None = None
    revision: NonNegativeInt | None = None
    observation_id: ObservationId | None = None
    operation: Literal["generate", "preview", "analyze"] | None = None
    pid: Annotated[int, Field(gt=0)] | None = None
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    completion_status: Literal["complete", "work_limit", "timeout", "cancelled", "failed"] | None = None
    error_category: Identifier | None = None
    retirement_reason: Literal["timeout", "cancelled", "crashed", "protocol", "closed", "startup"] | None = None
    max_work_units: NonNegativeInt | None = None
    work_used: NonNegativeInt | None = None
    max_elapsed_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None
    seed: NonNegativeInt | None = None


class TraceJournal:
    """One shared journal per run/process. Existing valid journals can resume.

    This is diagnostic telemetry, not the durable game commit log. Callers can
    inspect a worker's trace_failure_count if the filesystem stops accepting it.
    There are no free-form payloads or native error/output strings to leak state.
    """

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._sequence = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            data = self.path.read_bytes()
            if data and not data.endswith(b"\n"):
                raise ValueError("Trace journal has an incomplete final line")
            for line in data.splitlines():
                event = WorkerEvent.model_validate_json(line)
                if event.run_id != run_id or event.sequence != self._sequence + 1:
                    raise ValueError("Trace journal has a different run or broken sequence")
                self._sequence = event.sequence

    def append(self, *, worker_id: str, configuration_id: str, role: str,
               event_type: str, **fields) -> WorkerEvent:
        with self._lock:
            event = WorkerEvent(run_id=self.run_id, worker_id=worker_id,
                                sequence=self._sequence + 1,
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                configuration_id=configuration_id, role=role,
                                event_type=event_type, **fields)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                stream.write(event.model_dump_json().encode() + b"\n")
            self._sequence = event.sequence
            return event
