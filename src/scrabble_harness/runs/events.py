"""Validated private audit events. Only the store assigns IDs, clocks, and sequence."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ..domain._base import DomainModel, Identifier
from .records import (
    AttemptRecord, CommitRecord, DecisionRecord, ReservationRecord, RunRecord,
    FinishHoldRecord, UTCStamp,
)


class RunEvent(DomainModel):
    kind: Literal["run_created", "run_status_changed"]
    record: RunRecord


class DecisionEvent(DomainModel):
    kind: Literal["decision_started", "decision_prepared", "decision_evidence_recorded", "decision_completed"]
    record: DecisionRecord


class AttemptEvent(DomainModel):
    kind: Literal["attempt_prepared", "attempt_dispatched", "attempt_replied", "attempt_unresolved"]
    record: AttemptRecord


class ReservationEvent(DomainModel):
    kind: Literal["reservation_created", "reservation_changed"]
    record: ReservationRecord


class CommitEvent(DomainModel):
    kind: Literal["commit_intended", "commit_confirmed"]
    record: CommitRecord


class FinishEvent(DomainModel):
    kind: Literal["finish_reserved", "finish_consumed", "finish_released"]
    record: FinishHoldRecord


EventPayload = Annotated[RunEvent | DecisionEvent | AttemptEvent | ReservationEvent | CommitEvent | FinishEvent,
                         Field(discriminator="kind")]


class EventRecord(DomainModel):
    event_id: Identifier
    run_id: Identifier
    sequence: Annotated[int, Field(ge=1, le=2**63 - 1)]
    decision_id: Identifier | None = None
    created_at: UTCStamp
    payload: EventPayload

    @model_validator(mode="after")
    def consistent_event(self) -> Self:
        record, kind = self.payload.record, self.payload.kind
        if record.run_id != self.run_id or getattr(record, "decision_id", None) != self.decision_id:
            raise ValueError("event envelope differs from its record scope")
        expected = {"run_created": ("status", "created"),
                    "decision_started": ("status", "preparing"),
                    "decision_prepared": ("status", "prepared"),
                    "decision_evidence_recorded": ("status", "prepared"),
                    "decision_completed": ("status", "completed"),
                    "attempt_prepared": ("state", "prepared"),
                    "attempt_dispatched": ("state", "dispatched"),
                    "attempt_replied": ("state", "replied"),
                    "attempt_unresolved": ("state", "unresolved"),
                    "reservation_created": ("state", "reserved"),
                    "finish_reserved": ("state", "held"),
                    "finish_consumed": ("state", "consumed"),
                    "finish_released": ("state", "released")}.get(kind)
        if expected is not None and getattr(record, expected[0]) != expected[1]:
            raise ValueError("event kind differs from record state")
        if kind.startswith("commit_"):
            if (kind == "commit_confirmed") != (record.new_revision is not None):
                raise ValueError("commit event kind differs from confirmation state")
        return self
