"""Local private SQLite persistence, separate from game authority and admission.

One thread owns a connection. Separate stores/processes may share a database;
all managed writes, opens, closes, and checkpoints use the same advisory lock.
Never await provider/native work inside transaction(). No SQL connection is
exposed publicly. runs.ledger adds budget admission using these transactional records.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import time
from typing import Iterator
from uuid import uuid4

from pydantic import TypeAdapter

from ..domain import AnalysisLimits, AnalysisResult, PlayerObservation, RunLimits, canonical_bytes, canonical_json
from ..domain._base import Identifier
from ..harness.context import DecisionContext
from ..harness.contracts import DecisionOutcome, ExecutionManifest
from ..providers.protocol import ModelReply, ModelRequest
from .errors import (
    StoreBusyError, StoreConflictError, StoreCorruptionError, StoreError,
    StoreNotFoundError, StorePathError,
)
from .events import AttemptEvent, CommitEvent, DecisionEvent, EventRecord, FinishEvent, ReservationEvent, RunEvent
from .migrations import APPLICATION_ID, MIGRATIONS
from .records import (
    AttemptRecord, CampaignRecord, CommitRecord, DecisionRecord, ReservationRecord,
    BudgetClockRecord, FinishHoldRecord, ResourceAmounts, RunRecord,
)

_MODELS = {"campaigns": CampaignRecord, "runs": RunRecord, "decisions": DecisionRecord,
           "attempts": AttemptRecord, "reservations": ReservationRecord, "commits": CommitRecord,
           "events": EventRecord, "finish_holds": FinishHoldRecord, "budget_clocks": BudgetClockRecord}
_IDS = {"campaigns": "campaign_id", "runs": "run_id", "decisions": "decision_id",
        "attempts": "attempt_id", "reservations": "reservation_id", "commits": "decision_id",
        "events": "event_id", "finish_holds": "hold_id", "budget_clocks": "clock_id"}
_ID = TypeAdapter(Identifier, config={"strict": True})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _replace(record, **changes):
    return type(record).model_validate(record.model_dump(mode="python") | changes)


def _same(left, right) -> bool:
    return canonical_bytes(left) == canonical_bytes(right)


def _columns(record) -> dict:
    """Relational indexes duplicate validated JSON; verify both on every read."""
    if isinstance(record, CampaignRecord):
        return {"campaign_id": record.campaign_id}
    if isinstance(record, RunRecord):
        return dict(run_id=record.run_id, campaign_id=record.campaign_id,
                    manifest_id=record.manifest.manifest_id, status=record.status)
    if isinstance(record, DecisionRecord):
        obs = record.observation
        return dict(decision_id=record.decision_id, run_id=record.run_id, game_id=obs.game_id,
                    revision=obs.revision, observation_id=obs.observation_id, status=record.status)
    if isinstance(record, AttemptRecord):
        return dict(attempt_id=record.attempt_id, run_id=record.run_id, decision_id=record.decision_id,
                    request_sha256=record.request_sha256, state=record.state)
    if isinstance(record, ReservationRecord):
        allocation = record.allocation
        return dict(reservation_id=record.reservation_id, run_id=record.run_id, decision_id=record.decision_id,
                    attempt_id=record.attempt_id, kind=record.kind, state=record.state,
                    analysis_id=record.native_result.analysis_id if record.native_result is not None else None,
                    api_cost_microusd=allocation.api_cost_microusd, model_calls=allocation.model_calls,
                    engine_work_units=allocation.engine_work_units, tool_steps=allocation.tool_steps)
    if isinstance(record, CommitRecord):
        return dict(decision_id=record.decision_id, run_id=record.run_id, game_id=record.game_id,
                    expected_revision=record.expected_revision, new_revision=record.new_revision,
                    source_checkpoint_id=record.source_checkpoint_id, checkpoint_id=record.checkpoint_id)
    if isinstance(record, FinishHoldRecord):
        return dict(hold_id=record.hold_id, run_id=record.run_id, decision_id=record.decision_id,
                    consumed_attempt_id=record.consumed_attempt_id, state=record.state)
    if isinstance(record, BudgetClockRecord):
        return dict(clock_id=record.clock_id, scope=record.scope, campaign_id=record.campaign_id,
                    run_id=record.run_id, decision_id=record.decision_id, state=record.state)
    if isinstance(record, EventRecord):
        return dict(event_id=record.event_id, run_id=record.run_id, sequence=record.sequence,
                    decision_id=record.decision_id, kind=record.payload.kind)
    raise TypeError("unsupported storage record")


def _private_file(path: Path) -> int:
    if path.is_symlink():
        raise StorePathError("store files cannot be symbolic links")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise StorePathError("store file cannot be safely opened") from None
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(descriptor)
        raise StorePathError("store files must be owned regular files with one link")
    os.fchmod(descriptor, 0o600)
    return descriptor


class RunStore:
    def __init__(self, path: Path | str, *, busy_timeout_seconds: float = 5.0):
        if (isinstance(busy_timeout_seconds, bool) or not isinstance(busy_timeout_seconds, (int, float))
                or not 0 < busy_timeout_seconds <= 60):
            raise ValueError("busy timeout must be finite and between zero and 60 seconds")
        supplied = Path(path).absolute()
        if supplied.name in {"", ".", ".."} or supplied.is_symlink():
            raise StorePathError("store requires a regular database path")
        parent = supplied.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.stat()
        if parent.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise StorePathError("database needs a dedicated owner-only directory (mode 0700)")
        self.path = parent.resolve() / supplied.name
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self._timeout = float(busy_timeout_seconds)
        self._depth = 0
        self._closed = False
        self._lock_fd = _private_file(self.path.with_name(self.path.name + ".lock"))
        self._connection = None
        try:
            with self._writer_lock():
                descriptor = _private_file(self.path)
                os.close(descriptor)
                # Existing sidecars must also be private regular files. SQLite inherits
                # new sidecar modes from the 0600 database; do not chmod shared parents.
                for suffix in ("-wal", "-shm", "-journal"):
                    sidecar = self.path.with_name(self.path.name + suffix)
                    if sidecar.exists() or sidecar.is_symlink():
                        descriptor = _private_file(sidecar)
                        os.close(descriptor)
                connection = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
                self._connection = connection
                connection.row_factory = sqlite3.Row
                self._validate_schema_identity()
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise StoreError("SQLite could not enable WAL")
                connection.execute("PRAGMA synchronous=FULL")
                if sys.platform == "darwin":
                    connection.execute("PRAGMA fullfsync=ON")
                    connection.execute("PRAGMA checkpoint_fullfsync=ON")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise StoreError("SQLite could not enable foreign keys")
                if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise StoreError("SQLite could not enable FULL durability")
                self._migrate()
                # Persist newly created directory entries as well as SQLite's own fsyncs.
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            # Closing can checkpoint; retain the same serialization protection.
            try:
                if self._connection is not None:
                    with self._writer_lock():
                        self._connection.close()
            finally:
                os.close(self._lock_fd)
                self._closed = True
            raise

    def _check_owner(self):
        if self._closed:
            raise StoreError("store is closed")
        if (os.getpid(), threading.get_ident()) != (self._pid, self._thread):
            raise StoreError("open a separate store in each process/thread")

    @contextmanager
    def _writer_lock(self):
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreBusyError("local store writer is busy") from None
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _validate_schema_identity(self):
        connection = self._connection
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if application_id == 0 and version == 0 and not tables:
            return
        if application_id != APPLICATION_ID or not 1 <= version <= len(MIGRATIONS):
            raise StoreCorruptionError("unrecognized database identity or schema version")
        try:
            applied = connection.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version").fetchall()
        except sqlite3.DatabaseError:
            raise StoreCorruptionError("missing migration history") from None
        expected = [(migration.version, migration.sha256) for migration in MIGRATIONS[:version]]
        if [tuple(row) for row in applied] != expected:
            raise StoreCorruptionError("migration history does not match packaged schema")

    def _migrate(self):
        connection = self._connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == len(MIGRATIONS):
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for migration in MIGRATIONS[version:]:
                for statement in migration.statements():
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                                   (migration.version, migration.sha256, _now()))
                connection.execute(f"PRAGMA user_version={migration.version}")
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @contextmanager
    def transaction(self) -> Iterator[RunStore]:
        """BEGIN IMMEDIATE; nested calls use savepoints, including their audit events.

        All operations in the outer block commit together. A failing inner method
        rolls itself back even if its exception is caught inside the outer block.
        Transactions must be short and synchronous, with no external side effects.
        """
        self._check_owner()
        outer = self._depth == 0
        guard = self._writer_lock() if outer else _no_lock()
        with guard:
            savepoint = f"store_{self._depth}"
            try:
                self._connection.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {savepoint}")
                self._depth += 1
                try:
                    yield self
                    self._connection.execute("COMMIT" if outer else f"RELEASE SAVEPOINT {savepoint}")
                except BaseException:
                    if self._connection.in_transaction:
                        if outer:
                            self._connection.execute("ROLLBACK")
                        else:
                            self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                            self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                finally:
                    self._depth -= 1
            except sqlite3.IntegrityError:
                raise StoreConflictError("record conflicts with a durable identity or relation") from None
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise StoreBusyError("SQLite writer is busy") from None
                raise StoreError("SQLite operation failed") from None

    def _decode(self, table: str, row):
        if row is None:
            raise StoreNotFoundError("record does not exist")
        body = row["body"]
        try:
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != row["body_sha256"]:
                raise ValueError("hash mismatch")
            record = _MODELS[table].model_validate_json(body)
            if canonical_json(record) != body or any(row[key] != value for key, value in _columns(record).items()):
                raise ValueError("indexed content mismatch")
        except (ValueError, TypeError, KeyError):
            raise StoreCorruptionError("stored record failed content verification") from None
        return record

    def _get(self, table: str, identity: str):
        self._check_owner()
        _ID.validate_python(identity)
        row = self._connection.execute(f"SELECT * FROM {table} WHERE {_IDS[table]}=?", (identity,)).fetchone()
        return self._decode(table, row)

    def _find(self, table: str, identity: str):
        try:
            return self._get(table, identity)
        except StoreNotFoundError:
            return None

    def _write(self, table: str, record, *, update: bool = False):
        if not self._depth:
            raise StoreError("internal write requires an active transaction")
        record = _MODELS[table].model_validate(record)
        body = canonical_json(record)
        columns = _columns(record) | {"body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest()}
        if update:
            identity_column = _IDS[table]
            identity = columns.pop(identity_column)
            assignments = ", ".join(f"{key}=?" for key in columns)
            cursor = self._connection.execute(f"UPDATE {table} SET {assignments} WHERE {identity_column}=?",
                                              (*columns.values(), identity))
            if cursor.rowcount != 1:
                raise StoreConflictError("record vanished during transition")
        else:
            names = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            self._connection.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(columns.values()))
        return self._get(table, getattr(record, _IDS[table]))

    def _event(self, kind: str, record):
        event_type = {RunRecord: RunEvent, DecisionRecord: DecisionEvent, AttemptRecord: AttemptEvent,
                      ReservationRecord: ReservationEvent, CommitRecord: CommitEvent, FinishHoldRecord: FinishEvent}[type(record)]
        sequence = self._connection.execute("SELECT COALESCE(MAX(sequence), 0)+1 FROM events WHERE run_id=?",
                                             (record.run_id,)).fetchone()[0]
        event = EventRecord(event_id="evt_" + uuid4().hex, run_id=record.run_id, sequence=sequence,
                            decision_id=getattr(record, "decision_id", None), created_at=_now(),
                            payload=event_type(kind=kind, record=record))
        self._write("events", event)

    def get_campaign(self, campaign_id: str) -> CampaignRecord:
        return self._get("campaigns", campaign_id)

    def get_run(self, run_id: str) -> RunRecord:
        return self._get("runs", run_id)

    def get_decision(self, decision_id: str) -> DecisionRecord:
        return self._get("decisions", decision_id)

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        return self._get("attempts", attempt_id)

    def get_reservation(self, reservation_id: str) -> ReservationRecord:
        return self._get("reservations", reservation_id)

    def get_commit(self, decision_id: str) -> CommitRecord:
        return self._get("commits", decision_id)

    def create_campaign(self, campaign_id: str, limits: RunLimits) -> CampaignRecord:
        proposed = CampaignRecord(campaign_id=campaign_id, limits=limits, created_at=_now())
        with self.transaction():
            existing = self._find("campaigns", campaign_id)
            if existing is not None:
                if not _same(existing.limits, limits):
                    raise StoreConflictError("campaign identity already has different limits")
                return existing
            return self._write("campaigns", proposed)

    def create_run(self, run_id: str, campaign_id: str, manifest: ExecutionManifest) -> RunRecord:
        manifest = ExecutionManifest.model_validate(manifest)
        manifest = _replace(manifest, dependencies=tuple(sorted(manifest.dependencies, key=lambda dep: dep.name)))
        proposed = RunRecord(run_id=run_id, campaign_id=campaign_id, manifest=manifest, created_at=_now())
        with self.transaction():
            existing = self._find("runs", run_id)
            if existing is not None:
                if existing.campaign_id != campaign_id or not _same(existing.manifest, manifest):
                    raise StoreConflictError("run identity already has different inputs")
                return existing
            self.get_campaign(campaign_id)
            record = self._write("runs", proposed)
            self._event("run_created", record)
            return record

    def set_run_status(self, run_id: str, status: str) -> RunRecord:
        with self.transaction():
            old = self.get_run(run_id)
            new = _replace(old, status=status)
            if old.status == new.status:
                return old
            allowed = {"created": {"running", "failed", "cancelled"},
                       "running": {"completed", "failed", "cancelled"}}
            if status not in allowed.get(old.status, set()):
                raise StoreConflictError("run status transition is not allowed")
            record = self._write("runs", new, update=True)
            self._event("run_status_changed", record)
            return record

    def start_decision(self, run_id: str, decision_id: str, observation: PlayerObservation) -> DecisionRecord:
        """Save the observation before preparation, so preparation failures can abort durably."""
        proposed = DecisionRecord(run_id=run_id, decision_id=decision_id, observation=observation)
        with self.transaction():
            existing = self._find("decisions", decision_id)
            if existing is not None:
                if existing.run_id != run_id or not _same(existing.observation, observation):
                    raise StoreConflictError("decision identity already has different public inputs")
                return existing
            run = self.get_run(run_id)
            self._check_decision_inputs(run, observation)
            record = self._write("decisions", proposed)
            self._event("decision_started", record)
            return record

    @staticmethod
    def _check_decision_inputs(run, observation):
        if run.status not in {"created", "running"}:
            raise StoreConflictError("run cannot accept new decisions")
        if (observation.lexicon_id != run.manifest.run_config.lexicon.lexicon_id
                or observation.ruleset_id != run.manifest.run_config.ruleset.ruleset_id):
            raise StoreConflictError("public position configuration differs from its run")

    def prepare_decision(self, run_id: str, context: DecisionContext) -> DecisionRecord:
        proposed = DecisionRecord(run_id=run_id, decision_id=context.decision_id,
                                  observation=context.observation, context=context, status="prepared")
        if context.evidence:
            raise StoreConflictError("prepare a decision before recording evidence")
        with self.transaction():
            existing = self._find("decisions", context.decision_id)
            if existing is not None:
                if existing.run_id != run_id or not _same(existing.observation, context.observation):
                    raise StoreConflictError("decision identity already has different inputs")
                if existing.context is not None:
                    initial = _replace(existing.context, evidence=())
                    if not _same(initial, context):
                        raise StoreConflictError("decision identity already has a different snapshot")
                    return existing
                if existing.status != "preparing":
                    raise StoreConflictError("completed preparation failure cannot be retried")
            run = self.get_run(run_id)
            self._check_decision_inputs(run, context.observation)
            if (not _same(context.config, run.manifest.harness_config)
                    or context.candidate_order_seed != run.manifest.run_config.seeds.candidate_order
                    or any(candidate.provenance.engine_commit not in (None, run.manifest.run_config.engine.engine_commit)
                           for candidate in context.full_pool)):
                raise StoreConflictError("decision configuration differs from its run manifest")
            record = self._write("decisions", proposed, update=existing is not None)
            self._event("decision_prepared", record)
            return record

    def record_evidence(self, context: DecisionContext) -> DecisionRecord:
        context = DecisionContext.model_validate(context)
        with self.transaction():
            old = self.get_decision(context.decision_id)
            if old.context is not None and _same(old.context, context):
                return old
            if old.status != "prepared":
                raise StoreConflictError("completed decision cannot accept evidence")
            base = _replace(context, evidence=old.context.evidence)
            previous = tuple(map(canonical_bytes, old.context.evidence))
            if (not _same(base, old.context) or len(context.evidence) <= len(previous)
                    or tuple(map(canonical_bytes, context.evidence[:len(previous)])) != previous):
                raise StoreConflictError("only append-only evidence updates are allowed")
            record = self._write("decisions", _replace(old, context=context), update=True)
            self._event("decision_evidence_recorded", record)
            return record

    def complete_decision(self, outcome: DecisionOutcome) -> DecisionRecord:
        outcome = DecisionOutcome.model_validate(outcome)
        with self.transaction():
            old = self.get_decision(outcome.decision_id)
            proposed = _replace(old, status="completed", outcome=outcome)
            if old.status == "completed":
                if not _same(old, proposed):
                    raise StoreConflictError("decision already has a different outcome")
                return old
            record = self._write("decisions", proposed, update=True)
            self._event("decision_completed", record)
            return record

    def prepare_attempt(self, run_id: str, decision_id: str, request: ModelRequest) -> AttemptRecord:
        request = ModelRequest.model_validate(request)
        proposed = AttemptRecord(run_id=run_id, decision_id=decision_id, request=request,
                                 request_sha256=hashlib.sha256(canonical_bytes(request)).hexdigest())
        with self.transaction():
            old = self._find("attempts", request.attempt_id)
            if old is not None:
                if (old.run_id != run_id or old.decision_id != decision_id
                        or not _same(old.request, request)):
                    raise StoreConflictError("attempt identity already has a different request")
                return old
            decision = self.get_decision(decision_id)
            run = self.get_run(run_id)
            if decision.run_id != run_id or decision.status != "prepared" or run.status not in {"created", "running"}:
                raise StoreConflictError("attempt requires an active decision in its run")
            if (not _same(request.view, decision.context.model_view())
                    or tuple(map(canonical_bytes, request.evidence)) != tuple(map(canonical_bytes, decision.context.evidence_views()))
                    or not _same(request.model, run.manifest.run_config.model)
                    or request.prompt_version != decision.context.config.prompt_version
                    or request.tool_schema_version != decision.context.config.tool_schema_version):
                raise StoreConflictError("request differs from persisted public inputs/configuration")
            record = self._write("attempts", proposed)
            self._event("attempt_prepared", record)
            return record

    def mark_attempt_dispatched(self, attempt_id: str) -> AttemptRecord:
        return self._attempt_transition(attempt_id, "dispatched")

    def mark_attempt_unresolved(self, attempt_id: str) -> AttemptRecord:
        return self._attempt_transition(attempt_id, "unresolved")

    def record_reply(self, reply: ModelReply) -> AttemptRecord:
        reply = ModelReply.model_validate(reply)
        return self._attempt_transition(reply.attempt_id, "replied", reply=reply)

    def _attempt_transition(self, attempt_id: str, state: str, *, reply=None) -> AttemptRecord:
        with self.transaction():
            old = self.get_attempt(attempt_id)
            proposed = _replace(old, state=state, reply=reply)
            if old.state == state:
                if not _same(old, proposed):
                    raise StoreConflictError("attempt already has a different reply")
                return old
            allowed = {"prepared": {"dispatched", "unresolved"}, "dispatched": {"replied", "unresolved"},
                       "unresolved": {"replied"}}
            if state not in allowed.get(old.state, set()):
                raise StoreConflictError("attempt transition is not allowed")
            if state == "dispatched":
                decision = self.get_decision(old.decision_id)
                run = self.get_run(old.run_id)
                if decision.status != "prepared" or run.status not in {"created", "running"}:
                    raise StoreConflictError("dispatch requires an active decision/run")
                rows = self._connection.execute("SELECT * FROM reservations WHERE attempt_id=?", (attempt_id,)).fetchall()
                if rows and self._decode("reservations", rows[0]).state == "released":
                    raise StoreConflictError("released attempt allowance cannot be dispatched")
            record = self._write("attempts", proposed, update=True)
            self._event("attempt_" + state, record)
            return record

    def create_reservation(self, reservation: ReservationRecord) -> ReservationRecord:
        """Low-level persistence only. Runtime dispatch must go through BudgetLedger."""
        reservation = ReservationRecord.model_validate(reservation)
        if reservation.state != "reserved":
            raise StoreConflictError("new reservations must be reserved")
        with self.transaction():
            old = self._find("reservations", reservation.reservation_id)
            if old is not None:
                initial = _replace(old, state="reserved", settlement=None, settlement_method=None,
                                   bound_exceeded=False, native_result=None, native_dispatch_limits=None)
                if not _same(initial, reservation):
                    raise StoreConflictError("reservation identity already has a different allocation")
                return old
            decision = self.get_decision(reservation.decision_id)
            run = self.get_run(reservation.run_id)
            if (decision.run_id != reservation.run_id or decision.status != "prepared"
                    or run.status not in {"created", "running"}):
                raise StoreConflictError("reservation requires an active decision in its run")
            if reservation.attempt_id is not None:
                attempt = self.get_attempt(reservation.attempt_id)
                if (attempt.run_id != reservation.run_id or attempt.decision_id != reservation.decision_id
                        or attempt.state != "prepared"):
                    raise StoreConflictError("reservation requires its own undispatched attempt")
            record = self._write("reservations", reservation)
            self._event("reservation_created", record)
            return record

    def record_native_dispatch_limits(self, reservation_id: str, limits: AnalysisLimits) -> ReservationRecord:
        """Save exactly the clipped limits passed to the engine, once, before await."""
        limits = AnalysisLimits.model_validate(limits)
        with self.transaction():
            old = self.get_reservation(reservation_id)
            if old.native_dispatch_limits is not None:
                if old.native_dispatch_limits != limits:
                    raise StoreConflictError("native dispatch already has different limits")
                return old
            if old.state != "dispatched" or old.native_request is None:
                raise StoreConflictError("native dispatch limits require a dispatched typed intent")
            record = self._write("reservations", _replace(old, native_dispatch_limits=limits), update=True)
            self._event("reservation_changed", record)
            return record

    def transition_reservation(self, reservation_id: str, state: str,
                               *, settlement: ResourceAmounts | None = None,
                               settlement_method: str | None = None, bound_exceeded: bool = False,
                               native_result: AnalysisResult | None = None) -> ReservationRecord:
        with self.transaction():
            old = self.get_reservation(reservation_id)
            proposed = _replace(old, state=state, settlement=settlement,
                                settlement_method=settlement_method, bound_exceeded=old.bound_exceeded or bound_exceeded, native_result=native_result)
            if old.state == state:
                if not _same(old, proposed):
                    raise StoreConflictError("reservation already has different settlement facts")
                return old
            allowed = {"reserved": {"dispatched", "released", "unresolved"},
                       "dispatched": {"settled", "unresolved"}, "unresolved": {"settled"}}
            if state not in allowed.get(old.state, set()):
                raise StoreConflictError("reservation transition is not allowed")
            if old.kind == "model":
                attempt = self.get_attempt(old.attempt_id)
                if state == "released" and attempt.state != "prepared":
                    raise StoreConflictError("cannot release a dispatched attempt's allowance")
                if state in {"dispatched", "settled", "unresolved"} and attempt.state == "prepared":
                    raise StoreConflictError("mark the attempt dispatched before its allowance")
            record = self._write("reservations", proposed, update=True)
            self._event("reservation_changed", record)
            return record

    def intend_commit(self, commit: CommitRecord) -> CommitRecord:
        """Record intent only; this never calls the arbiter or creates a checkpoint."""
        commit = CommitRecord.model_validate(commit)
        if commit.new_revision is not None:
            raise StoreConflictError("new commit must be an unconfirmed intent")
        with self.transaction():
            old = self._find("commits", commit.decision_id)
            if old is not None:
                initial = _replace(old, new_revision=None, checkpoint_id=None)
                if not _same(initial, commit):
                    raise StoreConflictError("decision already has a different commit intent")
                return old
            decision = self.get_decision(commit.decision_id)
            obs = decision.observation
            outcome = decision.outcome
            if (decision.run_id != commit.run_id or commit.game_id != obs.game_id
                    or commit.expected_revision != obs.revision or outcome is None or outcome.decision is None):
                raise StoreConflictError("commit does not match a completed selected decision")
            selected = next(candidate for candidate in decision.context.full_pool
                            if candidate.candidate_id == outcome.decision.candidate_id)
            if not _same(commit.move, selected.move):
                raise StoreConflictError("commit action differs from the selected canonical move")
            record = self._write("commits", commit)
            self._event("commit_intended", record)
            return record

    def confirm_commit(self, decision_id: str, *, new_revision: int, checkpoint_id: str) -> CommitRecord:
        """Persist caller-supplied arbiter receipt; full checkpoint reconciliation is Task 15."""
        with self.transaction():
            old = self.get_commit(decision_id)
            proposed = _replace(old, new_revision=new_revision, checkpoint_id=checkpoint_id)
            if old.new_revision is not None:
                if not _same(old, proposed):
                    raise StoreConflictError("commit already has a different confirmation")
                return old
            record = self._write("commits", proposed, update=True)
            self._event("commit_confirmed", record)
            return record

    def events(self, run_id: str, *, after_sequence: int = 0, limit: int = 1000) -> tuple[EventRecord, ...]:
        self.get_run(run_id)
        if type(after_sequence) is not int or not 0 <= after_sequence <= 2**63 - 1:
            raise ValueError("invalid event cursor")
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("event page size must be between 1 and 10000")
        rows = self._connection.execute("SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                                        (run_id, after_sequence, limit)).fetchall()
        return tuple(self._decode("events", row) for row in rows)

    def reservations(self, run_id: str, *, decision_id: str | None = None) -> tuple[ReservationRecord, ...]:
        self.get_run(run_id)
        if decision_id is not None:
            if self.get_decision(decision_id).run_id != run_id:
                raise StoreConflictError("decision belongs to another run")
        rows = self._connection.execute(
            "SELECT * FROM reservations WHERE run_id=? AND (? IS NULL OR decision_id=?) ORDER BY reservation_id",
            (run_id, decision_id, decision_id)).fetchall()
        return tuple(self._decode("reservations", row) for row in rows)

    def get_finish_hold(self, hold_id: str) -> FinishHoldRecord:
        return self._get("finish_holds", hold_id)

    def finish_hold(self, decision_id: str) -> FinishHoldRecord | None:
        self.get_decision(decision_id)
        row = self._connection.execute("SELECT * FROM finish_holds WHERE decision_id=?", (decision_id,)).fetchone()
        return None if row is None else self._decode("finish_holds", row)

    def accounting_records(self, campaign_id: str) -> tuple[tuple[ReservationRecord, ...], tuple[FinishHoldRecord, ...]]:
        """Validated campaign-wide facts; caller uses transaction() for a coherent snapshot."""
        self.get_campaign(campaign_id)
        results = []
        for table in ("reservations", "finish_holds"):
            rows = self._connection.execute(
                f"SELECT item.* FROM {table} item JOIN runs ON runs.run_id=item.run_id WHERE runs.campaign_id=?",
                (campaign_id,)).fetchall()
            results.append(tuple(self._decode(table, row) for row in rows))
        return tuple(results)

    def diagnostics(self) -> dict:
        self._check_owner()
        return {"sqlite_version": sqlite3.sqlite_version, "schema_version": self._connection.execute("PRAGMA user_version").fetchone()[0],
                "journal_mode": self._connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": self._connection.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": self._connection.execute("PRAGMA foreign_keys").fetchone()[0]}

    def check_integrity(self) -> None:
        self._check_owner()
        if (self._connection.execute("PRAGMA quick_check").fetchall()[0][0] != "ok"
                or self._connection.execute("PRAGMA foreign_key_check").fetchone() is not None):
            raise StoreCorruptionError("database integrity check failed")

    def checkpoint(self) -> tuple[int, int, int]:
        self._check_owner()
        if self._depth:
            raise StoreError("cannot checkpoint inside a transaction")
        with self._writer_lock():
            return tuple(self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())

    def close(self) -> None:
        if self._closed:
            return
        self._check_owner()
        if self._depth:
            raise StoreError("cannot close inside a transaction")
        with self._writer_lock():
            self._connection.close()
            self._closed = True
        os.close(self._lock_fd)
        # Keep .lock on disk: unlinking it could create two independent lock inodes.

    def __enter__(self) -> RunStore:
        self._check_owner()
        return self

    def __exit__(self, *exc):
        self.close()


@contextmanager
def _no_lock():
    yield
