"""Bounded public-position decisions; the runner retains all game authority.

Input counters are trusted adapter contracts, not guessed token counts. An
unfinished decision is never resumed here: dispatch recovery needs an explicit
inactive-owner policy. Completed outcomes can be read without any external work.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from ..domain import PlayerObservation, canonical_bytes
from ..domain._base import Identifier, PositiveValue
from .budget import BudgetError, BudgetDeadlineError, InputTokenBound
from .context import prepare_decision_context
from .contracts import DecisionOutcome, DecisionUsage, ScopedAction, SelectAction
from .errors import HarnessError, ToolExecutionError
from .prompts import build_model_request
from .tool_schemas import parse_tool_arguments
from .tools import SelectionToolResult, ToolExecutor
from .policies import select_fallback

if TYPE_CHECKING:
    from ..engine.protocol import AnalysisEngine
    from ..providers.protocol import ModelProvider, ModelRequest, ProviderCapabilities
    from ..runs.ledger import BudgetLedger


class DecisionLoop:
    """One active decision per instance; caller owns store/provider/adapter lifetimes."""

    def __init__(self, provider: ModelProvider, analysis: AnalysisEngine, ledger: BudgetLedger,
                 *, capabilities: ProviderCapabilities,
                 input_bound: Callable[[ModelRequest], InputTokenBound],
                 finish_bound: InputTokenBound | None = None,
                 model_timeout_seconds: float = 30.0):
        from ..providers.protocol import ProviderCapabilities
        self.provider, self.analysis, self.ledger, self.store = provider, analysis, ledger, ledger.store
        self.capabilities = ProviderCapabilities.model_validate(capabilities)
        self.input_bound = input_bound
        self.finish_bound = None if finish_bound is None else InputTokenBound.model_validate(finish_bound)
        self.model_timeout_seconds = TypeAdapter(PositiveValue, config={"strict": True}).validate_python(model_timeout_seconds)
        self._busy = False

    async def decide(self, run_id: str, decision_id: str, observation: PlayerObservation) -> DecisionOutcome:
        from ..runs.errors import StoreNotFoundError
        run_id = TypeAdapter(Identifier, config={"strict": True}).validate_python(run_id)
        decision_id = TypeAdapter(Identifier, config={"strict": True}).validate_python(decision_id)
        observation = PlayerObservation.model_validate(observation)
        if self._busy:
            raise ToolExecutionError("one decision loop cannot run overlapping decisions")
        run = self.store.get_run(run_id)
        model, config = run.manifest.run_config.model, run.manifest.harness_config
        if (self.capabilities.provider, self.capabilities.model) != (model.provider, model.model):
            raise ValueError("provider capabilities differ from the saved run model")
        try:
            old = self.store.get_decision(decision_id)
        except StoreNotFoundError:
            old = None
        if old is not None:
            if old.run_id != run_id or canonical_bytes(old.observation) != canonical_bytes(observation):
                raise ToolExecutionError("decision identity belongs to different public inputs")
            if old.status == "completed":
                return old.outcome
            raise ToolExecutionError("unfinished decision requires explicit recovery; do not replay it")
        self._busy = True
        started = False
        try:
            # Claim a fresh decision atomically, including across loop instances.
            # The claim commits before clocks/preparation so failed clock admission
            # still has a durable public record to abort.
            with self.store.transaction():
                try:
                    self.store.get_decision(decision_id)
                except StoreNotFoundError:
                    self.store.start_decision(run_id, decision_id, observation)
                else:
                    raise RuntimeError("decision was claimed concurrently; do not replay it")
            started = True
            self.ledger.begin_decision(run_id, decision_id, observation)
            deadline = self.ledger.deadline(decision_id)
            try:
                async with asyncio.timeout(deadline.timeout(config.decision_limits.max_elapsed_seconds)):
                    context = await prepare_decision_context(observation, self.analysis,
                        decision_id=decision_id, config=config,
                        candidate_order_seed=run.manifest.run_config.seeds.candidate_order)
                self.store.prepare_decision(run_id, context)
            except (HarnessError, TimeoutError):
                return self._finish(decision_id, "aborted", "preparation_failed")
            return await self._run(run, decision_id)
        except asyncio.CancelledError as error:
            if started:
                try:
                    self._finish(decision_id, "aborted", "cancelled")
                except Exception as cleanup_error:
                    error.add_note(f"Abort persistence also failed: {type(cleanup_error).__name__}")
            raise
        except BudgetError:
            return self._finish(decision_id, "fallback", "budget_exhausted")
        except (HarnessError, ValidationError):
            return self._finish(decision_id, "fallback", "request_rejected")
        finally:
            self._busy = False

    async def _run(self, run, decision_id):
        config, model = run.manifest.harness_config, run.manifest.run_config.model
        executor = ToolExecutor(self.analysis, self.ledger)
        repair_rounds = transport_retries = 0
        continuation = None
        force_selection = False
        repair = False
        seen_calls, seen_comparisons = set(), set()
        for index in range(1, config.decision_limits.max_model_attempts + 1):
            context = self.store.get_decision(decision_id).context
            remaining = self.ledger.remaining_limits(decision_id)
            if remaining.max_model_attempts < 1:
                return self._finish(decision_id, "fallback", "attempts_exhausted")
            attempt_id = "attempt_" + hashlib.sha256(f"{decision_id}:{index}".encode()).hexdigest()
            request = build_model_request(context.model_view(), attempt_id=attempt_id, model=model,
                capabilities=self.capabilities, remaining_limits=remaining, config=config,
                evidence=context.evidence_views(), force_selection=force_selection,
                continuation=continuation, repair=repair)
            # No verified future-input ceiling means selection-only, before admission.
            if "compare_moves" in request.tools and self.finish_bound is None:
                request = build_model_request(context.model_view(), attempt_id=attempt_id, model=model,
                    capabilities=self.capabilities, remaining_limits=remaining, config=config,
                    evidence=context.evidence_views(), force_selection=True,
                    continuation=continuation, repair=repair)
            hold = self.store.finish_hold(decision_id)
            use_hold = request.force_selection and hold is not None and hold.state == "held"
            reservation_id = "model_" + attempt_id.removeprefix("attempt_")
            try:
                self.ledger.reserve_model(reservation_id, request, self.input_bound(request),
                    finish_bound=self.finish_bound if "compare_moves" in request.tools else None,
                    use_finish_hold=use_hold)
                permit = self.ledger.dispatch_model(reservation_id,
                    requested_timeout_seconds=self.model_timeout_seconds)
            except BaseException:
                self._account(reservation_id, run.run_id)
                raise
            try:
                from ..providers.protocol import ModelReply
                from ..runs.errors import StoreConflictError
                # Refresh after all synchronous accounting, never reset the deadline.
                timeout = self.ledger.deadline(decision_id, nonterminal="compare_moves" in request.tools).timeout(
                    permit.timeout_seconds, nonterminal="compare_moves" in request.tools)
                async with asyncio.timeout(timeout):
                    reply = ModelReply.model_validate(await self.provider.complete(request))
            except asyncio.CancelledError as error:
                try:
                    self._account(reservation_id, run.run_id)
                except Exception as cleanup_error:
                    error.add_note(f"Accounting cleanup also failed: {type(cleanup_error).__name__}")
                raise
            except BudgetError:
                self._account(reservation_id, run.run_id)
                raise
            except Exception:
                self._account(reservation_id, run.run_id)
                if transport_retries < config.max_transport_retries:
                    transport_retries += 1
                    force_selection = True
                    continue  # New attempt, original allowance remains unresolved.
                return self._finish(decision_id, "fallback", "provider_failed")
            # Save/bill before inspecting actions. Storage failures propagate; they
            # cannot authorize a repair, another request, or an unaudited selection.
            try:
                self.ledger.settle_model(reservation_id, reply)
            except (ValidationError, ValueError, BudgetError, StoreConflictError):
                self._account(reservation_id, run.run_id)
                return self._finish(decision_id, "fallback", "reply_rejected")
            self.ledger.deadline(decision_id)  # Late replies stay billed, not selected.
            continuation = reply.continuation
            try:
                if reply.status != "completed" or len(reply.tool_calls) != 1:
                    raise ToolExecutionError("one completed action is required")
                call = reply.tool_calls[0]
                action = parse_tool_arguments(call.name, call.arguments_json,
                    allowed_tools=request.tools, version=request.tool_schema_version)
                if call.call_id in seen_calls:
                    return self._finish(decision_id, "fallback", "repeated_action")
                seen_calls.add(call.call_id)
                envelope = ScopedAction(decision_id=decision_id, observation_id=context.observation.observation_id, action=action)
                if isinstance(action, SelectAction):
                    context.select(envelope)
                    if not set(action.evidence_ids).issubset({e.evidence_id for e in request.evidence}):
                        raise ToolExecutionError("selection cited undisplayed evidence")
                else:
                    context.resolve_comparison(envelope)
                    signature = (tuple(sorted(action.candidate_handles)), action.tier)
                    if signature in seen_comparisons:
                        return self._finish(decision_id, "fallback", "repeated_action")
                    seen_comparisons.add(signature)
            except HarnessError:
                if repair_rounds < config.max_repair_rounds:
                    repair_rounds += 1
                    force_selection = repair = True
                    continue
                return self._finish(decision_id, "fallback", "invalid_action")
            try:
                result = await executor.execute(attempt_id)
            except asyncio.CancelledError:
                raise
            except BudgetError:
                raise
            except Exception:
                return self._finish(decision_id, "fallback", "tool_failed")
            self.ledger.deadline(decision_id)
            if isinstance(result, SelectionToolResult):
                return self._finish(decision_id, "selected", "model_selected", result.decision)
        return self._finish(decision_id, "fallback", "attempts_exhausted")

    def _account(self, reservation_id, run_id):
        saved = next((r for r in self.store.reservations(run_id) if r.reservation_id == reservation_id), None)
        if saved is not None:
            if saved.state == "reserved":
                self.ledger.release_undispatched(reservation_id)
            elif saved.state == "dispatched":
                self.ledger.mark_unresolved(reservation_id)

    def _finish(self, decision_id, status, reason, selection=None):
        record = self.store.get_decision(decision_id)
        context = record.context
        origin = "model" if status == "selected" else None
        if context is None:
            status = "aborted"
            origin = None
        elif status == "fallback":
            fallback = select_fallback(context, reason)
            selection, origin = fallback.decision, fallback.origin
        totals = self.ledger.totals(decision_id=decision_id)
        outcome = DecisionOutcome(decision_id=decision_id, observation_id=record.observation.observation_id,
            status=status, terminal_reason=reason, decision=selection, selection_origin=origin,
            trusted_pool=() if context is None else context.full_pool,
            visible_bindings=() if context is None else context.visible_bindings,
            evidence=() if context is None else context.evidence,
            usage=DecisionUsage(confirmed_engine_work_units=totals.settled.engine_work_units))
        return self.ledger.complete_decision(outcome).outcome
