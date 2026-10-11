"""Typed execution from saved replies; no provider calls or game authority.

The caller owns the analysis adapter already loaded during preparation. Never
reload/regenerate its candidate registry while this executor uses the decision.
Known receipts are reusable; missing receipts are never automatically replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, TypeAdapter

from ..domain import AgentDecision, AnalysisLimits, AnalysisResult, canonical_bytes
from ..domain._base import DomainModel, Identifier
from .contracts import ComparisonEvidence, ComparisonRequest, EvidenceView, ScopedAction, SelectAction
from .errors import EvidenceValidationError, ToolExecutionError
from .prompts import render_comparison_result
from .tool_schemas import parse_tool_arguments

if TYPE_CHECKING:
    from ..engine.protocol import AnalysisEngine
    from ..runs.ledger import BudgetLedger


class SelectionToolResult(DomainModel):
    kind: Literal["selection"] = "selection"
    attempt_id: Identifier
    call_id: Identifier
    decision: AgentDecision


class ComparisonToolResult(DomainModel):
    kind: Literal["comparison"] = "comparison"
    attempt_id: Identifier
    call_id: Identifier
    reservation_id: Identifier
    evidence: ComparisonEvidence
    view: EvidenceView
    content: Annotated[str, Field(min_length=1, max_length=1024 * 1024)]


class ToolExecutor:
    def __init__(self, analysis: AnalysisEngine, ledger: BudgetLedger):
        self.analysis, self.ledger, self.store = analysis, ledger, ledger.store
        self._busy = False

    async def execute(self, attempt_id: str) -> SelectionToolResult | ComparisonToolResult:
        """Execute the sole completed action in a saved, accounted provider reply."""
        attempt_id = TypeAdapter(Identifier, config={"strict": True}).validate_python(attempt_id)
        if self._busy:
            raise ToolExecutionError("one executor cannot execute overlapping tools")
        self._busy = True
        try:
            attempt = self.store.get_attempt(attempt_id)
            decision = self.store.get_decision(attempt.decision_id)
            run = self.store.get_run(attempt.run_id)
            if decision.status != "prepared" or run.status not in {"created", "running"}:
                raise ToolExecutionError("tool execution requires an active prepared decision/run")
            reply = attempt.reply
            if attempt.state != "replied" or reply is None or reply.status != "completed" or len(reply.tool_calls) != 1:
                raise ToolExecutionError("tool execution requires one action in a saved completed reply")
            allocated = tuple(r for r in self.store.reservations(attempt.run_id) if r.attempt_id == attempt_id)
            if len(allocated) != 1 or allocated[0].state not in {"settled", "unresolved"}:
                raise ToolExecutionError("settle or account for the model reply before executing its action")
            context, request = decision.context, attempt.request
            if canonical_bytes(context.model_view()) != canonical_bytes(request.view):
                raise ToolExecutionError("saved request differs from current public context")
            current_views = context.evidence_views()
            if tuple(map(canonical_bytes, request.evidence)) != tuple(map(canonical_bytes, current_views[:len(request.evidence)])):
                raise EvidenceValidationError("request evidence is not an issued prefix")
            call = reply.tool_calls[0]
            action = parse_tool_arguments(call.name, call.arguments_json, allowed_tools=request.tools,
                version=request.tool_schema_version)
            envelope = ScopedAction(decision_id=decision.decision_id,
                observation_id=decision.observation.observation_id, action=action)
            if isinstance(action, SelectAction):
                if not set(action.evidence_ids).issubset({item.evidence_id for item in request.evidence}):
                    raise EvidenceValidationError("selection cites evidence not displayed in its request")
                return SelectionToolResult(attempt_id=attempt_id, call_id=call.call_id, decision=context.select(envelope))
            candidates = context.resolve_comparison(envelope)  # Scope, phase, tier, visible handles.
            seed = TypeAdapter(Annotated[int, Field(ge=0, le=2**64 - 1)], config={"strict": True}).validate_python(run.manifest.run_config.seeds.analysis)
            identity = json.dumps([decision.decision_id, attempt_id, call.call_id], separators=(",", ":")).encode()
            reservation_id = "tool_" + hashlib.sha256(identity).hexdigest()
            # Lookup without constructing new limits: remaining budgets can change after a receipt.
            existing = next((r for r in self.store.reservations(attempt.run_id) if r.reservation_id == reservation_id), None)
            if existing is not None:
                intent = existing.native_request
                if (intent is None or intent.attempt_id != attempt_id or intent.call_id != call.call_id
                        or intent.envelope != envelope or intent.seed != seed
                        or tuple(item.candidate_id for item in intent.requested) != tuple(c.candidate_id for c in candidates)):
                    raise ToolExecutionError("tool identity already has different native inputs")
                if existing.state != "settled" or existing.native_result is None or existing.native_dispatch_limits is None:
                    raise ToolExecutionError("native work has no saved receipt; do not replay it")
                return self._evidence(existing)
            if request.evidence != current_views:
                raise EvidenceValidationError("a new comparison cannot use a stale evidence snapshot")
            tier = next(tier for tier in context.config.comparison_tiers if tier.name == action.tier)
            remaining = self.ledger.remaining_limits(decision.decision_id)
            work = min(tier.max_work_units, remaining.max_engine_work_units)
            if work < len(candidates):
                raise ToolExecutionError("remaining rollout work cannot cover all requested candidates")
            deadline = self.ledger.deadline(decision.decision_id, nonterminal=True)
            limits = AnalysisLimits(max_work_units=work, work_unit="candidate_rollout",
                max_elapsed_seconds=deadline.timeout(tier.max_elapsed_seconds, nonterminal=True))
            intent = ComparisonRequest(attempt_id=attempt_id, call_id=call.call_id, envelope=envelope,
                requested=tuple(next(binding for binding in context.visible_bindings if binding.handle == handle)
                                for handle in action.candidate_handles), limits=limits, seed=seed)
            self.ledger.reserve_native(reservation_id, decision.decision_id, limits, request=intent)
            try:
                actual = self.ledger.dispatch_native(reservation_id)
                self.store.record_native_dispatch_limits(reservation_id, actual)
                # Allow the native adapter's own shorter tier timeout to return partial facts.
                # The outer deadline still protects the finish reserve during stop/cleanup.
                deadline = self.ledger.deadline(decision.decision_id, nonterminal=True)
                async with asyncio.timeout(deadline.timeout(60.0, nonterminal=True)):
                    raw = await self.analysis.analyze(tuple(c.candidate_id for c in candidates), actual, seed)
                result = AnalysisResult.model_validate(raw)
                receipt = self.ledger.settle_native(reservation_id, result)  # Known facts survive evidence rejection.
            except BaseException as error:
                try:
                    saved = self.store.get_reservation(reservation_id)
                    if saved.state == "reserved":
                        self.ledger.release_undispatched(reservation_id)
                    elif saved.state == "dispatched":
                        self.ledger.mark_unresolved(reservation_id)
                except Exception as cleanup_error:
                    # The durable dispatched allocation still charges full allowance.
                    error.add_note(f"Accounting cleanup also failed: {type(cleanup_error).__name__}")
                raise
            return self._evidence(receipt)
        finally:
            self._busy = False

    def _evidence(self, receipt) -> ComparisonToolResult:
        """Issue evidence from a durable receipt, without rerunning native work."""
        intent = receipt.native_request
        with self.store.transaction():
            record = self.store.get_decision(receipt.decision_id)
            if record.status != "prepared":
                raise ToolExecutionError("decision ended before evidence could be issued")
            updated, evidence = record.context.issue_evidence(intent.envelope, receipt.native_result,
                limits=receipt.native_dispatch_limits, seed=intent.seed)
            self.store.record_evidence(updated)
        view = next(item for item in updated.evidence_views() if item.evidence_id == evidence.evidence_id)
        content = render_comparison_result(view, presentation=updated.config.presentation)
        return ComparisonToolResult(attempt_id=intent.attempt_id, call_id=intent.call_id,
            reservation_id=receipt.reservation_id, evidence=evidence, view=view, content=content)
