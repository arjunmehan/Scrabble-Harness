"""Transactional campaign/run/decision admission and conservative usage accounting.

Only BudgetLedger may authorize runtime dispatch. RunStore remains a trusted
low-level persistence API. A dispatch permit is issued once, after its marker
commits. No external side effect belongs inside any ledger/store transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from uuid import uuid4

from ..domain import AnalysisLimits, AnalysisResult, PlayerObservation, canonical_bytes
from ..domain._base import DomainModel, Identifier, NonNegativeInt, PositiveValue
from ..harness.budget import (
    BudgetBoundError, BudgetDeadlineError, BudgetExceededError, BudgetStateError,
    CostQuote, Deadline, InputTokenBound, SystemClock, duration_ns, estimate_usage, quote_cost,
)
from ..harness.contracts import ComparisonRequest, DecisionLimits, DecisionOutcome, DecisionUsage
from ..providers.protocol import ModelReply, ModelRequest
from .errors import StoreConflictError
from .records import BudgetClockRecord, FinishHoldRecord, ReservationRecord, ResourceAmounts
from .store import RunStore, _replace

_FIELDS = ("api_cost_microusd", "model_calls", "engine_work_units", "tool_steps")


class ResourceTotals(DomainModel):
    """Python-integer sums cannot overflow SQLite SUM or reset a budget."""
    api_cost_microusd: NonNegativeInt = 0
    model_calls: NonNegativeInt = 0
    engine_work_units: NonNegativeInt = 0
    tool_steps: NonNegativeInt = 0


class BudgetTotals(DomainModel):
    settled: ResourceTotals = ResourceTotals()
    reserved: ResourceTotals = ResourceTotals()
    unresolved: ResourceTotals = ResourceTotals()
    held: ResourceTotals = ResourceTotals()
    bound_exceeded: bool = False

    @property
    def charged(self) -> ResourceTotals:
        groups = (self.settled, self.reserved, self.unresolved, self.held)
        return ResourceTotals(**{field: sum(getattr(group, field) for group in groups) for field in _FIELDS})


class DispatchPermit(DomainModel):
    reservation_id: Identifier
    attempt_id: Identifier
    timeout_seconds: PositiveValue
    expires_monotonic_ns: NonNegativeInt


@dataclass(frozen=True)
class _ClockAnchor:
    monotonic_ns: int
    elapsed_ns: int


class BudgetLedger:
    def __init__(self, store: RunStore, *, clock=None):
        self.store = store
        self.clock = clock or SystemClock()
        self.session_id = "session_" + uuid4().hex
        self._anchors: dict[str, _ClockAnchor] = {}

    def begin_decision(self, run_id: str, decision_id: str, observation: PlayerObservation):
        """Start BEFORE native preparation. Campaign/run clocks start on first use."""
        with self.store.transaction():
            record = self.store.start_decision(run_id, decision_id, observation)
        self.deadline(decision_id)
        return record

    def _scope(self, decision_id):
        decision = self.store.get_decision(decision_id)
        run = self.store.get_run(decision.run_id)
        campaign = self.store.get_campaign(run.campaign_id)
        limits = run.manifest.harness_config.decision_limits
        return decision, run, campaign, limits

    def _clock_records(self, decision_id, *, create: bool):
        decision, run, campaign, limits = self._scope(decision_id)
        wall, mono = self.clock.wall_ns(), self.clock.monotonic_ns()
        if any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in (wall, mono)):
            raise BudgetDeadlineError("clock sample is outside supported range")
        records = []
        for scope, identity, seconds in (("campaign", campaign.campaign_id, campaign.limits.max_elapsed_seconds),
                                         ("run", run.run_id, run.manifest.run_config.limits.max_elapsed_seconds),
                                         ("decision", decision.decision_id, limits.max_elapsed_seconds)):
            clock_id = "clock_" + hashlib.sha256(f"{scope}:{identity}".encode()).hexdigest()
            old = self.store._find("budget_clocks", clock_id)
            if old is None:
                if not create:
                    raise BudgetStateError("start the decision deadline before dispatch")
                old = BudgetClockRecord(clock_id=clock_id, scope=scope, campaign_id=campaign.campaign_id,
                    run_id=run.run_id if scope != "campaign" else None,
                    decision_id=decision_id if scope == "decision" else None,
                    started_wall_ns=wall, observed_wall_ns=wall, limit_ns=duration_ns(seconds))
                self.store._write("budget_clocks", old)
            if old.limit_ns != duration_ns(seconds):
                raise BudgetStateError("persisted elapsed cap differs from immutable configuration")
            anchor = self._anchors.setdefault(clock_id, _ClockAnchor(mono, max(old.elapsed_ns, max(0, wall - old.started_wall_ns))))
            rollback = wall < old.observed_wall_ns or mono < anchor.monotonic_ns
            elapsed = max(old.elapsed_ns, wall - old.started_wall_ns,
                          anchor.elapsed_ns + max(0, mono - anchor.monotonic_ns))
            elapsed = min(elapsed, 2**63 - 1)
            state = "expired" if rollback or old.state == "expired" or elapsed >= old.limit_ns else "active"
            updated = _replace(old, observed_wall_ns=max(wall, old.observed_wall_ns), elapsed_ns=elapsed, state=state)
            if canonical_bytes(updated) != canonical_bytes(old):
                self.store._write("budget_clocks", updated, update=True)
            records.append(updated)
        return records, mono, limits

    def deadline(self, decision_id: str, *, nonterminal: bool = False) -> Deadline:
        """Persist monotonic floors/wall watermarks, including expiry, before raising.

        Downtime consumes elapsed allowance on restart. A backwards wall clock or
        a backwards in-session monotonic clock fails closed; neither resets time.
        """
        with self.store.transaction():
            records, mono, limits = self._clock_records(decision_id, create=True)
        if any(record.state == "expired" for record in records):
            raise BudgetDeadlineError("decision/run/campaign elapsed budget expired or clock moved backwards")
        available_ns = min(record.limit_ns - record.elapsed_ns for record in records)
        deadline = Deadline(decision_id=decision_id, expires_monotonic_ns=mono + available_ns,
                            clock=self.clock, finish_reserve_seconds=limits.finish_reserve_seconds)
        if deadline.remaining_seconds(nonterminal=nonterminal) <= 0:
            raise BudgetDeadlineError("no dispatch time remains after the finish reserve")
        return deadline

    def _check_locked_deadline(self, decision_id: str, *, nonterminal: bool = False):
        # The writer-lock wait itself consumes time. Refresh after acquiring it.
        records, mono, limits = self._clock_records(decision_id, create=False)
        if any(record.state == "expired" for record in records):
            raise BudgetDeadlineError("elapsed budget expired while waiting for the writer")
        deadline = Deadline(decision_id, mono + min(r.limit_ns - r.elapsed_ns for r in records),
                            self.clock, limits.finish_reserve_seconds)
        deadline.timeout(60.0, nonterminal=nonterminal)
        return deadline

    def totals(self, *, campaign_id: str | None = None, run_id: str | None = None,
               decision_id: str | None = None) -> BudgetTotals:
        if sum(identity is not None for identity in (campaign_id, run_id, decision_id)) != 1:
            raise ValueError("specify exactly one budget scope")
        with self.store.transaction():
            if decision_id is not None:
                run_id = self.store.get_decision(decision_id).run_id
            if run_id is not None:
                campaign_id = self.store.get_run(run_id).campaign_id
            reservations, holds = self.store.accounting_records(campaign_id)
            if run_id is not None:
                reservations = tuple(item for item in reservations if item.run_id == run_id)
                holds = tuple(item for item in holds if item.run_id == run_id)
            if decision_id is not None:
                reservations = tuple(item for item in reservations if item.decision_id == decision_id)
                holds = tuple(item for item in holds if item.decision_id == decision_id)
            return self._totals(reservations, holds)

    @staticmethod
    def _totals(reservations, holds):
        groups = {name: {field: 0 for field in _FIELDS} for name in ("settled", "reserved", "unresolved", "held")}
        exceeded = False
        for reservation in reservations:
            if reservation.state == "released":
                continue
            exceeded |= reservation.bound_exceeded
            group = "settled" if reservation.state == "settled" else (
                "reserved" if reservation.state == "reserved" else "unresolved")
            amount = reservation.settlement if reservation.state == "settled" else reservation.allocation
            for field in _FIELDS:
                groups[group][field] += getattr(amount, field)
        for hold in holds:
            if hold.state == "held":
                groups["held"]["api_cost_microusd"] += hold.quote.api_cost_microusd
                groups["held"]["model_calls"] += 1
        return BudgetTotals(**{name: ResourceTotals(**values) for name, values in groups.items()}, bound_exceeded=exceeded)

    def _admit(self, decision_id, allocation: ResourceAmounts, *, replacing_hold=None):
        decision, run, campaign, limits = self._scope(decision_id)
        if decision.status != "prepared" or run.status not in {"created", "running"}:
            raise BudgetStateError("admission requires an active prepared decision/run")
        reservations, holds = self.store.accounting_records(campaign.campaign_id)
        for scope, items, scoped_holds, caps in (
            ("campaign", reservations, holds, campaign.limits),
            ("run", tuple(r for r in reservations if r.run_id == run.run_id),
             tuple(h for h in holds if h.run_id == run.run_id), run.manifest.run_config.limits),
            ("decision", tuple(r for r in reservations if r.decision_id == decision_id),
             tuple(h for h in holds if h.decision_id == decision_id), limits)):
            total = self._totals(items, scoped_holds)
            if total.bound_exceeded:
                raise BudgetExceededError(scope, "reported-usage-bound")
            charged = total.charged
            caps_by_resource = dict(api_cost_microusd=caps.max_api_cost_microusd,
                model_calls=limits.max_model_attempts if scope == "decision" else caps.max_model_calls,
                engine_work_units=caps.max_engine_work_units)
            if scope == "decision":
                caps_by_resource["tool_steps"] = limits.max_tool_steps
            for resource, cap in caps_by_resource.items():
                replace_amount = (replacing_hold.quote.api_cost_microusd if resource == "api_cost_microusd" else
                                  1 if resource == "model_calls" else 0) if replacing_hold is not None else 0
                if getattr(charged, resource) - replace_amount + getattr(allocation, resource) > cap:
                    raise BudgetExceededError(scope, resource)

    def _quote(self, bound: InputTokenBound, run) -> CostQuote:
        return quote_cost(bound, run.manifest.rate_card)

    def _reserve_finish(self, decision_id: str, bound: InputTokenBound):
        decision, run, campaign, limits = self._scope(decision_id)
        if bound.purpose != "final_selection" or bound.max_output_tokens != run.manifest.run_config.model.max_output_tokens:
            raise BudgetBoundError("finish bound must cover the configured selection output cap")
        quote = self._quote(bound, run)
        old = self.store.finish_hold(decision_id)
        if old is not None:
            if canonical_bytes(old.quote) != canonical_bytes(quote):
                raise StoreConflictError("decision already has a different finish allowance")
            if old.state != "held":
                raise BudgetStateError("finish allowance was already consumed/released")
            return old
        self._admit(decision_id, ResourceAmounts(api_cost_microusd=quote.api_cost_microusd, model_calls=1))
        hold = FinishHoldRecord(hold_id="finish_" + hashlib.sha256(decision_id.encode()).hexdigest(),
                                run_id=run.run_id, decision_id=decision_id, quote=quote)
        record = self.store._write("finish_holds", hold)
        self.store._event("finish_reserved", record)
        return record

    def reserve_finish(self, decision_id: str, bound: InputTokenBound) -> FinishHoldRecord:
        bound = InputTokenBound.model_validate(bound)
        self.deadline(decision_id, nonterminal=True)
        with self.store.transaction():
            self._check_locked_deadline(decision_id, nonterminal=True)
            return self._reserve_finish(decision_id, bound)

    def reserve_model(self, reservation_id: str, request: ModelRequest, bound: InputTokenBound,
                      *, finish_bound: InputTokenBound | None = None, use_finish_hold: bool = False) -> ReservationRecord:
        request, bound = ModelRequest.model_validate(request), InputTokenBound.model_validate(bound)
        decision_id = request.view.decision_id
        _, run, _, _ = self._scope(decision_id)
        if (bound.purpose != "request" or bound.request_sha256 != hashlib.sha256(canonical_bytes(request)).hexdigest()
                or bound.max_output_tokens != request.model.max_output_tokens):
            raise BudgetBoundError("input bound differs from this exact request/output cap")
        quote = self._quote(bound, run)
        self.deadline(decision_id, nonterminal="compare_moves" in request.tools)
        with self.store.transaction():
            old = self.store._find("reservations", reservation_id)
            if old is not None:
                if (old.run_id != run.run_id or old.decision_id != decision_id or old.attempt_id != request.attempt_id
                        or old.quote is None or canonical_bytes(old.quote) != canonical_bytes(quote)):
                    raise StoreConflictError("reservation identity already has different model inputs")
                self.store.prepare_attempt(run.run_id, decision_id, request)
                return old
            self._check_locked_deadline(decision_id, nonterminal="compare_moves" in request.tools)
            hold = self.store.finish_hold(decision_id)
            if use_finish_hold:
                if not request.force_selection or request.tools != ("select_move",) or finish_bound is not None:
                    raise BudgetStateError("finish allowance can only fund a forced selection request")
                if hold is None or hold.state != "held":
                    raise BudgetStateError("no active finish allowance")
                if (bound.input_tokens > hold.quote.bound.input_tokens or bound.max_output_tokens > hold.quote.bound.max_output_tokens
                        or quote.api_cost_microusd > hold.quote.api_cost_microusd):
                    raise BudgetBoundError("final request exceeds its held input/output/cost ceiling")
            elif "compare_moves" in request.tools:
                if finish_bound is not None:
                    hold = self._reserve_finish(decision_id, InputTokenBound.model_validate(finish_bound))
                if hold is None or hold.state != "held":
                    raise BudgetStateError("comparison-capable request needs a durable final selection allowance")
            elif finish_bound is not None:
                raise BudgetStateError("selection-only request does not need another finish allowance")
            allocation = ResourceAmounts(api_cost_microusd=quote.api_cost_microusd, model_calls=1)
            self._admit(decision_id, allocation, replacing_hold=hold if use_finish_hold else None)
            self.store.prepare_attempt(run.run_id, decision_id, request)
            reservation = ReservationRecord(reservation_id=reservation_id, run_id=run.run_id,
                decision_id=decision_id, attempt_id=request.attempt_id, kind="model", allocation=allocation,
                quote=quote, admission_session_id=self.session_id)
            recorded = self.store.create_reservation(reservation)
            if use_finish_hold:
                consumed = self.store._write("finish_holds", _replace(hold, state="consumed", consumed_attempt_id=request.attempt_id), update=True)
                self.store._event("finish_consumed", consumed)
            return recorded

    def reserve_native(self, reservation_id: str, decision_id: str, limits: AnalysisLimits,
                       *, request: ComparisonRequest | None = None) -> ReservationRecord:
        limits = AnalysisLimits.model_validate(limits)
        if request is not None:
            request = ComparisonRequest.model_validate(request)
            decision = self.store.get_decision(decision_id)
            if (request.limits != limits or request.envelope.decision_id != decision_id
                    or request.envelope.observation_id != decision.observation.observation_id):
                raise BudgetBoundError("native intent differs from reservation scope/limits")
        if limits.work_unit != "candidate_rollout" or limits.max_work_units < 2:
            raise BudgetBoundError("comparison requires at least two candidate rollouts")
        self.deadline(decision_id, nonterminal=True)
        with self.store.transaction():
            old = self.store._find("reservations", reservation_id)
            if old is not None:
                if old.decision_id != decision_id or old.kind != "native" or old.native_limits != limits or old.native_request != request:
                    raise StoreConflictError("reservation identity already has different native limits")
                return old
            self._check_locked_deadline(decision_id, nonterminal=True)
            decision, run, _, configured = self._scope(decision_id)
            hold = self.store.finish_hold(decision_id)
            if configured.max_model_attempts and (hold is None or hold.state != "held"):
                raise BudgetStateError("native comparison requires a held final selection allowance")
            allocation = ResourceAmounts(engine_work_units=limits.max_work_units, tool_steps=1)
            self._admit(decision_id, allocation)
            return self.store.create_reservation(ReservationRecord(reservation_id=reservation_id, run_id=run.run_id,
                decision_id=decision_id, kind="native", allocation=allocation, native_limits=limits, native_request=request,
                admission_session_id=self.session_id))

    def _dispatchable(self, reservation):
        if reservation.state != "reserved" or reservation.admission_session_id != self.session_id:
            raise BudgetStateError("reservation is stale or already dispatched; do not replay it")
        decision, run, _, _ = self._scope(reservation.decision_id)
        if decision.status != "prepared" or run.status not in {"created", "running"}:
            raise BudgetStateError("dispatch requires an active decision/run")
        # Existing allocations count already. Reject any subsequent reported overrun.
        self._admit(reservation.decision_id, ResourceAmounts())

    def dispatch_model(self, reservation_id: str, *, requested_timeout_seconds: float) -> DispatchPermit:
        reservation = self.store.get_reservation(reservation_id)
        if reservation.kind != "model" or reservation.quote is None:
            raise BudgetStateError("dispatch needs a ledger-priced model reservation")
        request = self.store.get_attempt(reservation.attempt_id).request
        nonterminal = "compare_moves" in request.tools
        self.deadline(reservation.decision_id, nonterminal=nonterminal)
        with self.store.transaction():
            reservation = self.store.get_reservation(reservation_id)
            self._dispatchable(reservation)
            deadline = self._check_locked_deadline(reservation.decision_id, nonterminal=nonterminal)
            timeout = deadline.timeout(requested_timeout_seconds, nonterminal=nonterminal)
            if nonterminal:
                hold = self.store.finish_hold(reservation.decision_id)
                if hold is None or hold.state != "held":
                    raise BudgetStateError("comparison-capable dispatch lost its finish allowance")
            self.store.mark_attempt_dispatched(reservation.attempt_id)
            self.store.transition_reservation(reservation_id, "dispatched")
        try:
            timeout = deadline.timeout(timeout, nonterminal=nonterminal)
        except BudgetDeadlineError:
            self.mark_unresolved(reservation_id)
            raise
        return DispatchPermit(reservation_id=reservation_id, attempt_id=reservation.attempt_id,
                              timeout_seconds=timeout, expires_monotonic_ns=deadline.expires_monotonic_ns -
                              (duration_ns(deadline.finish_reserve_seconds) if nonterminal and deadline.finish_reserve_seconds else 0))

    def dispatch_native(self, reservation_id: str) -> AnalysisLimits:
        reservation = self.store.get_reservation(reservation_id)
        if reservation.kind != "native" or reservation.native_limits is None:
            raise BudgetStateError("dispatch needs a ledger-bounded native reservation")
        self.deadline(reservation.decision_id, nonterminal=True)
        with self.store.transaction():
            reservation = self.store.get_reservation(reservation_id)
            self._dispatchable(reservation)
            deadline = self._check_locked_deadline(reservation.decision_id, nonterminal=True)
            _, _, _, configured = self._scope(reservation.decision_id)
            hold = self.store.finish_hold(reservation.decision_id)
            if configured.max_model_attempts and (hold is None or hold.state != "held"):
                raise BudgetStateError("native comparison lost its finish allowance")
            actual = _replace(reservation.native_limits,
                max_elapsed_seconds=deadline.timeout(reservation.native_limits.max_elapsed_seconds, nonterminal=True))
            self.store.transition_reservation(reservation_id, "dispatched")
        try:
            return _replace(actual, max_elapsed_seconds=deadline.timeout(actual.max_elapsed_seconds, nonterminal=True))
        except BudgetDeadlineError:
            self.mark_unresolved(reservation_id)
            raise

    def settle_model(self, reservation_id: str, reply: ModelReply) -> ReservationRecord:
        reply = ModelReply.model_validate(reply)
        with self.store.transaction():
            reservation = self.store.get_reservation(reservation_id)
            if reservation.kind != "model" or reservation.attempt_id != reply.attempt_id:
                raise BudgetStateError("reply belongs to another reservation")
            self.store.record_reply(reply)  # Persist malformed/refused/incomplete actions BEFORE pricing/actions.
            run = self.store.get_run(reservation.run_id)
            quote = reservation.quote
            breached = reservation.bound_exceeded
            if quote is not None and reply.usage is not None:
                breached |= ((reply.usage.input_tokens is not None and reply.usage.input_tokens > quote.bound.input_tokens)
                             or (reply.usage.output_tokens is not None and reply.usage.output_tokens > quote.bound.max_output_tokens))
            try:
                estimated = estimate_usage(reply.usage, run.manifest.rate_card)
            except BudgetBoundError:
                estimated = None
                breached = True  # Out-of-band/overflow facts invalidate further admission.
            if estimated is None:
                if reservation.state == "settled":
                    raise BudgetStateError("cannot replace settled facts with unknown usage")
                return self.store.transition_reservation(reservation_id, "unresolved", bound_exceeded=breached)
            breached |= estimated.api_cost_microusd > reservation.allocation.api_cost_microusd
            return self.store.transition_reservation(reservation_id, "settled",
                settlement=ResourceAmounts(api_cost_microusd=estimated.api_cost_microusd, model_calls=1),
                settlement_method=estimated.method, bound_exceeded=breached)

    def settle_native(self, reservation_id: str, result: AnalysisResult | None) -> ReservationRecord:
        with self.store.transaction():
            reservation = self.store.get_reservation(reservation_id)
            if reservation.kind != "native":
                raise BudgetStateError("expected native work reservation")
            if result is None:
                if reservation.state == "settled":
                    raise BudgetStateError("cannot replace settled native facts with unknown work")
                return self.store.transition_reservation(reservation_id, "unresolved")
            result = AnalysisResult.model_validate(result)
            decision = self.store.get_decision(reservation.decision_id)
            if result.observation_id != decision.observation.observation_id or result.work_used.unit != "candidate_rollout":
                raise BudgetStateError("analysis result scope or work unit differs")
            return self.store.transition_reservation(reservation_id, "settled",
                settlement=ResourceAmounts(engine_work_units=result.work_used.count, tool_steps=1),
                settlement_method="reported_native_work_v1",
                bound_exceeded=result.work_used.count > reservation.allocation.engine_work_units, native_result=result)

    def mark_unresolved(self, reservation_id: str) -> ReservationRecord:
        with self.store.transaction():
            reservation = self.store.get_reservation(reservation_id)
            if reservation.state not in {"dispatched", "unresolved"}:
                raise BudgetStateError("only dispatched work can become unresolved outside recovery")
            if reservation.kind == "model":
                attempt = self.store.get_attempt(reservation.attempt_id)
                if attempt.state == "dispatched":
                    self.store.mark_attempt_unresolved(attempt.attempt_id)
            return self.store.transition_reservation(reservation_id, "unresolved")

    def release_undispatched(self, reservation_id: str) -> ReservationRecord:
        """Only this live admission session can prove its own work never dispatched."""
        with self.store.transaction():
            reservation = self.store.get_reservation(reservation_id)
            if reservation.state == "released":
                return reservation
            if reservation.state != "reserved" or reservation.admission_session_id != self.session_id:
                raise BudgetStateError("cannot prove this reservation undispatched")
            return self.store.transition_reservation(reservation_id, "released")

    def recover_unfinished(self, run_id: str) -> tuple[ReservationRecord, ...]:
        """Explicit restart accounting; caller must own the inactive run (runner lock in Task 14).

        Stale reserved/dispatched allocations become unresolved, never free.
        Prepared attempt markers become unresolved without asserting an HTTP call
        occurred. No request is sent, and no provider reply is overwritten.
        """
        with self.store.transaction():
            reservations = self.store.reservations(run_id)
            recovered = []
            for reservation in reservations:
                if reservation.state not in {"reserved", "dispatched"}:
                    continue
                if reservation.kind == "model":
                    attempt = self.store.get_attempt(reservation.attempt_id)
                    if attempt.state in {"prepared", "dispatched"}:
                        self.store.mark_attempt_unresolved(attempt.attempt_id)
                recorded = self.store.transition_reservation(reservation.reservation_id, "unresolved")
                recovered.append(recorded)
            return tuple(recovered)

    def remaining_limits(self, decision_id: str) -> DecisionLimits:
        deadline = self.deadline(decision_id)
        with self.store.transaction():
            decision, run, campaign, limits = self._scope(decision_id)
            total_campaign = self.totals(campaign_id=campaign.campaign_id)
            total_run = self.totals(run_id=run.run_id)
            total_decision = self.totals(decision_id=decision_id)
            hold = self.store.finish_hold(decision_id)
            hold_cost = hold.quote.api_cost_microusd if hold is not None and hold.state == "held" else 0
            hold_calls = 1 if hold_cost or (hold is not None and hold.state == "held") else 0
            if any(total.bound_exceeded for total in (total_campaign, total_run, total_decision)):
                raise BudgetExceededError("campaign/run/decision", "reported-usage-bound")
            costs = [campaign.limits.max_api_cost_microusd - total_campaign.charged.api_cost_microusd,
                     run.manifest.run_config.limits.max_api_cost_microusd - total_run.charged.api_cost_microusd,
                     limits.max_api_cost_microusd - total_decision.charged.api_cost_microusd]
            calls = [campaign.limits.max_model_calls - total_campaign.charged.model_calls,
                     run.manifest.run_config.limits.max_model_calls - total_run.charged.model_calls,
                     limits.max_model_attempts - total_decision.charged.model_calls]
            work = min(campaign.limits.max_engine_work_units - total_campaign.charged.engine_work_units,
                       run.manifest.run_config.limits.max_engine_work_units - total_run.charged.engine_work_units,
                       limits.max_engine_work_units - total_decision.charged.engine_work_units)
            seconds = deadline.remaining_seconds()
            if seconds <= 0:
                raise BudgetDeadlineError("decision expired")
            # Own finish allowance is available for the next FINAL request, not for
            # arbitrary dispatch. Admission still protects it at all shared scopes.
            return DecisionLimits(max_api_cost_microusd=max(0, min(costs) + hold_cost),
                max_model_attempts=max(0, min(calls) + hold_calls),
                max_engine_work_units=max(0, work), max_tool_steps=max(0, limits.max_tool_steps - total_decision.charged.tool_steps),
                max_elapsed_seconds=seconds, finish_reserve_seconds=min(limits.finish_reserve_seconds, seconds / 2))

    def complete_decision(self, outcome: DecisionOutcome):
        """Finalize audit usage from durable facts and release the undispatchable finish hold."""
        outcome = DecisionOutcome.model_validate(outcome)
        with self.store.transaction():
            decision, run, _, _ = self._scope(outcome.decision_id)
            reservations = self.store.reservations(run.run_id, decision_id=decision.decision_id)
            if any(item.state == "reserved" for item in reservations):
                raise BudgetStateError("release proven-undispatched allocations or mark stale ones unresolved first")
            hold = self.store.finish_hold(decision.decision_id)
            if hold is not None and hold.state == "held":
                released = self.store._write("finish_holds", _replace(hold, state="released"), update=True)
                self.store._event("finish_released", released)
            records, _, _ = self._clock_records(decision.decision_id, create=True)
            totals = self._totals(reservations, ())
            usage = DecisionUsage(model_attempts=totals.settled.model_calls + totals.unresolved.model_calls,
                tool_steps=totals.settled.tool_steps + totals.unresolved.tool_steps,
                confirmed_engine_work_units=totals.settled.engine_work_units,
                unresolved_engine_work_units=totals.unresolved.engine_work_units,
                settled_api_cost_microusd=totals.settled.api_cost_microusd,
                unresolved_api_cost_microusd=totals.unresolved.api_cost_microusd,
                elapsed_seconds=records[-1].elapsed_ns / 1_000_000_000)
            if decision.status == "completed":
                # Time passing after completion must not alter its frozen outcome.
                usage = decision.outcome.usage
            return self.store.complete_decision(_replace(outcome, usage=usage))
