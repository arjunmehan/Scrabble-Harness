"""Saved tool actions through authoritative context, ledger, and durable receipts."""

import asyncio
import json

import pytest

from scrabble_harness.domain import AnalysisResult, Board, BoardTile, CandidateProvenance, MovePreview, PassMove, PlayerObservation, build_candidate, CandidateEvaluation, MetricDefinition, WorkUsed, canonical_bytes
from scrabble_harness.harness import (
    ComparisonToolResult, ScopedAction, SelectAction, SelectionToolResult, ToolExecutor,
    build_model_request,
)
from scrabble_harness.harness.budget import BudgetExceededError, InputTokenBound, verified_input_bound
from scrabble_harness.harness.errors import EvidenceValidationError, ToolExecutionError, ToolSchemaError, UnknownHandleError
from scrabble_harness.providers import ModelReply, ProviderToolCall, TokenUsage, comparison_call, selection_call
from scrabble_harness.runs import BudgetLedger, RunStore
from test_store import context, manifest, model_request, db_path, store, changed
from test_ledger import FakeClock, new_manifest, prepare_other, build_request


class RecordingAnalysis:
    def __init__(self, context):
        self.context, self.calls, self.transform, self.failure, self.block = context, [], None, None, False
        self.entered = None

    async def analyze(self, ids, limits, seed):
        self.calls.append((ids, limits, seed))
        if self.entered is not None:
            self.entered.set()
        if self.block:
            await asyncio.Event().wait()
        if self.failure is not None:
            raise self.failure
        result = AnalysisResult(analysis_id="analysis-tools", observation_id=self.context.observation.observation_id,
            values=tuple(CandidateEvaluation(candidate_id=identifier, value=float(index), standard_error=0.5)
                         for index, identifier in enumerate(ids)),
            metric_definition=MetricDefinition(name="synthetic-equity", unit="points", description="Synthetic tool test", value_kind="estimate"),
            work_used=WorkUsed(unit="candidate_rollout", definition="Total candidate rollouts", count=limits.max_work_units),
            elapsed_time_seconds=0.1, seed=seed, completion_status="work_limit")
        return self.transform(result) if self.transform else result


def save_reply(store, request, calls, *, ledger=None, status="completed"):
    ledger = ledger or BudgetLedger(store)
    bound = verified_input_bound(request, input_tokens=1000, evidence="Synthetic complete request counter")
    finish = InputTokenBound(provider=request.model.provider, model=request.model.model, purpose="final_selection",
        method="verified_complete_input_v1", input_tokens=10000, max_output_tokens=request.model.max_output_tokens,
        evidence="Synthetic final follow-up ceiling") if "compare_moves" in request.tools else None
    ledger.reserve_model("model-" + request.attempt_id, request, bound, finish_bound=finish)
    ledger.dispatch_model("model-" + request.attempt_id, requested_timeout_seconds=5.0)
    reply = ModelReply(attempt_id=request.attempt_id, decision_id=request.view.decision_id,
        observation_id=request.view.observation.observation_id, provider=request.model.provider,
        resolved_model=request.model.model, response_id="response-" + request.attempt_id,
        request_id="request-" + request.attempt_id, status=status, tool_calls=calls,
        usage=TokenUsage(input_tokens=10, output_tokens=5))
    ledger.settle_model("model-" + request.attempt_id, reply)
    return ledger


def test_selection_returns_canonical_decision_without_engine_or_commit(store, context, model_request):
    ledger = save_reply(store, model_request, (selection_call("select-a", "c01"),))
    analysis = RecordingAnalysis(context)
    result = asyncio.run(ToolExecutor(analysis, ledger).execute(model_request.attempt_id))
    assert isinstance(result, SelectionToolResult)
    assert result.decision.candidate_id == context.visible_bindings[0].candidate_id
    assert analysis.calls == []
    assert store.get_decision(context.decision_id).status == "prepared"
    assert ledger.totals(decision_id=context.decision_id).charged.engine_work_units == 0
    assert not any(event.payload.kind.startswith("commit_") for event in store.events("run-a"))


@pytest.mark.parametrize("tier,work", [("light",60),("standard",300)])
def test_comparison_uses_total_tier_work_and_saves_exact_receipt(store, context, model_request, db_path, tier, work):
    ledger = save_reply(store, model_request, (comparison_call("compare-a", ("c01","c02"), tier=tier),))
    analysis = RecordingAnalysis(context)
    executor = ToolExecutor(analysis, ledger)
    result = asyncio.run(executor.execute(model_request.attempt_id))
    assert isinstance(result, ComparisonToolResult)
    ids, limits, seed = analysis.calls[0]
    assert ids == tuple(binding.candidate_id for binding in context.visible_bindings)
    assert limits.max_work_units == work and 0 < limits.max_elapsed_seconds <= 8.0 and seed == 73
    assert result.evidence.result.work_used.count == work
    assert json.loads(result.content) == result.view.model_dump(mode="json")
    receipt = store.get_reservation(result.reservation_id)
    assert receipt.native_result == result.evidence.result
    assert receipt.native_dispatch_limits == limits == result.evidence.limits
    assert receipt.native_request.envelope.action.tier == tier
    assert receipt.native_request.call_id == "compare-a"
    assert ledger.totals(decision_id=context.decision_id).settled.engine_work_units == work
    before = store.events("run-a")
    with RunStore(db_path) as reopened:
        unused = RecordingAnalysis(context)
        restored = asyncio.run(ToolExecutor(unused, BudgetLedger(reopened)).execute(model_request.attempt_id))
        assert restored == result and unused.calls == []
        assert reopened.events("run-a") == before
        reopened.check_integrity()


@pytest.mark.parametrize("calls,status,error", [
    ((),"completed",ToolExecutionError),
    ((selection_call("a","c01"),selection_call("b","c02")),"completed",ToolExecutionError),
    ((selection_call("a","c01"),),"refused",ToolExecutionError),
    ((selection_call("a","c01"),),"incomplete",ToolExecutionError),
    ((ProviderToolCall(call_id="a",name="select_move",arguments_json="{bad"),),"completed",ToolSchemaError),
    ((ProviderToolCall(call_id="a",name="unknown",arguments_json="{}"),),"completed",ToolSchemaError),
    ((comparison_call("a",("c01","c99")),),"completed",UnknownHandleError),
    ((selection_call("a","c99"),),"completed",UnknownHandleError),
    ((selection_call("a","c01",evidence_ids=("e99",)),),"completed",EvidenceValidationError),
    ((ProviderToolCall(call_id="a",name="compare_moves",arguments_json='{"candidate_handles":["c01","c02"],"tier":"huge"}'),),"completed",ToolSchemaError),
    ((ProviderToolCall(call_id="a",name="compare_moves",arguments_json='{"candidate_handles":["c01","c01"],"tier":"light"}'),),"completed",ToolSchemaError),
    ((ProviderToolCall(call_id="a",name="compare_moves",arguments_json='{"candidate_handles":["c01","c02","c03","c04","c05"],"tier":"light"}'),),"completed",ToolSchemaError),
    ((ProviderToolCall(call_id="a",name="compare_moves",arguments_json='{"candidate_handles":["c01","c02"],"tier":"light","max_work_units":1000000000}'),),"completed",ToolSchemaError),
])
def test_invalid_actions_fail_before_native_dispatch(store, context, model_request, calls, status, error):
    ledger = save_reply(store, model_request, calls, status=status)
    analysis = RecordingAnalysis(context)
    with pytest.raises(error):
        asyncio.run(ToolExecutor(analysis, ledger).execute(model_request.attempt_id))
    assert analysis.calls == []
    assert all(reservation.kind == "model" for reservation in store.reservations("run-a"))


def test_final_request_cannot_dispatch_comparison(store, context, model_request):
    request = changed(model_request, tools=("select_move",), force_selection=True)
    ledger = save_reply(store, request, (comparison_call("compare-a", ("c01","c02")),))
    analysis = RecordingAnalysis(context)
    with pytest.raises(ToolSchemaError):
        asyncio.run(ToolExecutor(analysis, ledger).execute(request.attempt_id))
    assert analysis.calls == []


def test_result_with_wrong_seed_is_charged_but_never_issued(store, context, model_request):
    ledger = save_reply(store, model_request, (comparison_call("compare-a", ("c01","c02")),))
    analysis = RecordingAnalysis(context)
    analysis.transform = lambda result: changed(result, seed=74)
    executor = ToolExecutor(analysis, ledger)
    with pytest.raises(EvidenceValidationError):
        asyncio.run(executor.execute(model_request.attempt_id))
    assert store.get_decision(context.decision_id).context.evidence == ()
    assert ledger.totals(decision_id=context.decision_id).settled.engine_work_units == 60
    with pytest.raises(EvidenceValidationError):
        asyncio.run(executor.execute(model_request.attempt_id))
    assert len(analysis.calls) == 1


@pytest.mark.parametrize("failure", [RuntimeError("Engine crashed"), TimeoutError("No receipt")])
def test_native_failures_keep_unknown_work_and_cannot_replay(store, context, model_request, failure):
    ledger = save_reply(store, model_request, (comparison_call("compare-a", ("c01","c02")),))
    analysis = RecordingAnalysis(context)
    analysis.failure = failure
    executor = ToolExecutor(analysis, ledger)
    with pytest.raises(type(failure)):
        asyncio.run(executor.execute(model_request.attempt_id))
    assert ledger.totals(decision_id=context.decision_id).unresolved.engine_work_units == 60
    with pytest.raises(ToolExecutionError, match="do not replay"):
        asyncio.run(executor.execute(model_request.attempt_id))
    assert len(analysis.calls) == 1


def test_cancellation_and_concurrent_execution_are_safe(store, context, model_request):
    ledger = save_reply(store, model_request, (comparison_call("compare-a", ("c01","c02")),))
    analysis = RecordingAnalysis(context)
    analysis.block = True
    executor = ToolExecutor(analysis, ledger)
    async def scenario():
        analysis.entered = asyncio.Event()
        task = asyncio.create_task(executor.execute(model_request.attempt_id))
        await asyncio.wait_for(analysis.entered.wait(),1.0)
        with pytest.raises(ToolExecutionError,match="overlapping"):
            await executor.execute(model_request.attempt_id)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert ledger.totals(decision_id=context.decision_id).unresolved.engine_work_units == 60
    assert store.get_decision(context.decision_id).context.evidence == ()


def test_partial_timeout_keeps_honest_values_and_actual_work(store, context, model_request):
    ledger = save_reply(store, model_request, (comparison_call("compare-a", ("c01","c02")),))
    analysis = RecordingAnalysis(context)
    analysis.transform = lambda result: changed(result, values=result.values[:1],
        work_used=changed(result.work_used,count=7),completion_status="timeout")
    result = asyncio.run(ToolExecutor(analysis,ledger).execute(model_request.attempt_id))
    assert result.view.work_used.count == 7 and result.view.completion_status == "timeout"
    assert len(result.view.values) == 1
    assert ledger.totals(decision_id=context.decision_id).settled.engine_work_units == 7


def test_remaining_work_clips_tier_total_before_native_call(store, context, manifest):
    limits=changed(manifest.harness_config.decision_limits,max_engine_work_units=13)
    new=new_manifest(manifest,decision_limits=limits)
    other=prepare_other(store,new,context,run_id="clipped-run",decision_id="clipped-decision")
    request=build_request(other,new,attempt_id="clipped-attempt")
    request=changed(request,tools=("select_move","compare_moves"),force_selection=False)
    ledger=save_reply(store,request,(comparison_call("compare-a",("c01","c02"),tier="standard"),))
    analysis=RecordingAnalysis(other)
    result=asyncio.run(ToolExecutor(analysis,ledger).execute(request.attempt_id))
    assert result.evidence.limits.max_work_units == 13
    assert analysis.calls[0][1].max_work_units == 13


def test_deadline_clips_native_limits_and_keeps_finish_reserve(store,context,model_request):
    clock=FakeClock()
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),),ledger=BudgetLedger(store,clock=clock))
    clock.advance(25)
    analysis=RecordingAnalysis(context)
    result=asyncio.run(ToolExecutor(analysis,ledger).execute(model_request.attempt_id))
    assert analysis.calls[0][1].max_elapsed_seconds == 3.0
    assert result.evidence.limits.max_elapsed_seconds == 3.0


def test_selection_cites_only_evidence_displayed_in_that_request(store,context,model_request):
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(context)
    asyncio.run(ToolExecutor(analysis,ledger).execute(model_request.attempt_id))
    saved=store.get_decision(context.decision_id).context
    final=build_model_request(saved.model_view(),attempt_id="final-attempt",model=model_request.model,
        capabilities=model_request.capabilities,remaining_limits=ledger.remaining_limits(context.decision_id),
        config=saved.config,evidence=saved.evidence_views(),force_selection=True)
    ledger.reserve_model("final",final,verified_input_bound(final,input_tokens=1000,evidence="Synthetic follow-up"),use_finish_hold=True)
    ledger.dispatch_model("final",requested_timeout_seconds=5.0)
    reply=ModelReply(attempt_id=final.attempt_id,decision_id=final.view.decision_id,
        observation_id=final.view.observation.observation_id,provider=final.model.provider,resolved_model=final.model.model,
        response_id="final-response",request_id="final-request",status="completed",
        tool_calls=(selection_call("final-call","c02",evidence_ids=("e01",)),),usage=TokenUsage(input_tokens=10,output_tokens=5))
    ledger.settle_model("final",reply)
    result=asyncio.run(ToolExecutor(analysis,ledger).execute(final.attempt_id))
    assert result.decision.evidence_ids == ("e01",) and len(analysis.calls) == 1


def test_saved_receipt_can_finish_evidence_after_storage_failure(store, context, model_request, monkeypatch):
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(context)
    executor=ToolExecutor(analysis,ledger)
    with monkeypatch.context() as patch:
        patch.setattr(store,"record_evidence",lambda context: (_ for _ in ()).throw(RuntimeError("Evidence save failed")))
        with pytest.raises(RuntimeError,match="Evidence save failed"):
            asyncio.run(executor.execute(model_request.attempt_id))
    assert ledger.totals(decision_id=context.decision_id).settled.engine_work_units == 60
    assert store.get_decision(context.decision_id).context.evidence == ()
    result=asyncio.run(executor.execute(model_request.attempt_id))
    assert result.evidence.evidence_id == "e01" and len(analysis.calls) == 1


@pytest.mark.parametrize("phase", ["short_opponent_rack", "late_bag"])
def test_unsupported_phase_never_offers_or_dispatches_comparison(store, context, manifest, phase):
    from scrabble_harness.domain.state import ObservationData
    data=context.observation.model_dump(mode="python",exclude={"observation_id"})
    if phase=="short_opponent_rack":
        data.update(opponent_rack_size=6,bag_size=context.observation.bag_size+1)
    else:
        inventory=context.observation.unseen_tile_counts.as_counter()
        moved=tuple(inventory.elements())[:context.observation.unseen_tile_counts.total-13]
        occupied={(tile.row,tile.column) for tile in context.observation.board.tiles}
        squares=[(r,c) for r in range(15) for c in range(15) if (r,c) not in occupied]
        board=Board(tiles=context.observation.board.tiles+tuple(BoardTile(row=r,column=c,
            displayed_letter=tile if tile!="?" else "E",is_blank=tile=="?")
            for (r,c),tile in zip(squares,moved)))
        data.update(board=board,bag_size=6,opponent_rack_size=7,
            unseen_tile_counts=context.observation.tile_distribution.without(board.physical_tiles+context.observation.own_rack))
    obs=PlayerObservation.from_data(ObservationData.model_validate(data))
    candidate=build_candidate(obs,PassMove(),exact_score=0,leave=obs.own_rack,static_equity=0.0,
        provenance=CandidateProvenance(source="synthetic-unsupported-phase",configuration_id="fixture"))
    from scrabble_harness.harness import DecisionContext
    other=DecisionContext.from_candidates(decision_id="phase-decision",observation=obs,config=context.config,
        candidate_order_seed=123,candidates=(candidate,),fallback_preview=MovePreview(observation_id=obs.observation_id,
            move=candidate.move,exact_score=0,leave=candidate.leave,board_after=obs.board))
    store.create_run("phase-run","campaign-a",manifest)
    store.prepare_decision("phase-run",other)
    request=build_model_request(other.model_view(),attempt_id="phase-attempt",model=manifest.run_config.model,
        capabilities=model_request_capabilities(manifest),remaining_limits=other.config.decision_limits,config=other.config)
    assert request.tools == ("select_move",)
    ledger=save_reply(store,request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(other)
    with pytest.raises(ToolSchemaError):
        asyncio.run(ToolExecutor(analysis,ledger).execute(request.attempt_id))
    assert analysis.calls==[] and not any(r.kind=="native" for r in store.reservations("phase-run"))


def model_request_capabilities(manifest):
    from scrabble_harness.providers import ProviderCapabilities
    return ProviderCapabilities(provider=manifest.run_config.model.provider,model=manifest.run_config.model.model,max_output_tokens=1024)


@pytest.mark.parametrize("change", ["wrong_scope","wrong_unit","overspend","foreign_candidates"])
def test_invalid_native_results_never_become_model_evidence(store,context,model_request,change):
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(context)
    def transform(result):
        if change=="wrong_scope":
            return changed(result,observation_id="obs_v1_"+"a"*64)
        if change=="wrong_unit":
            return changed(result,work_used=changed(result.work_used,unit="unsupported"))
        if change=="overspend":
            return changed(result,work_used=changed(result.work_used,count=61))
        return changed(result,values=(changed(result.values[0],candidate_id="cand_v1_"+"a"*64),))
    analysis.transform=transform
    with pytest.raises((EvidenceValidationError,RuntimeError)):
        asyncio.run(ToolExecutor(analysis,ledger).execute(model_request.attempt_id))
    assert store.get_decision(context.decision_id).context.evidence==()
    totals=ledger.totals(decision_id=context.decision_id)
    assert totals.charged.engine_work_units == (61 if change=="overspend" else 60)
    if change=="overspend":
        assert totals.bound_exceeded


def test_unsettled_saved_reply_cannot_dispatch_native_work(store,context,model_request):
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),))
    # Another saved but undispatched attempt is never actionable.
    final=changed(model_request,attempt_id="prepared-only",tools=("select_move",),force_selection=True)
    store.prepare_attempt("run-a",context.decision_id,final)
    analysis=RecordingAnalysis(context)
    with pytest.raises(ToolExecutionError):
        asyncio.run(ToolExecutor(analysis,ledger).execute(final.attempt_id))
    assert analysis.calls==[]


def test_competing_scope_caps_reject_work_before_native_dispatch(store,context,manifest):
    store.create_campaign("tiny-work",changed(manifest.run_config.limits,max_engine_work_units=1))
    other=prepare_other(store,manifest,context,run_id="tiny-run",decision_id="tiny-decision",campaign_id="tiny-work")
    request=build_request(other,manifest,attempt_id="tiny-attempt")
    request=changed(request,tools=("select_move","compare_moves"),force_selection=False)
    ledger=save_reply(store,request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(other)
    with pytest.raises(ToolExecutionError,match="cannot cover"):
        asyncio.run(ToolExecutor(analysis,ledger).execute(request.attempt_id))
    assert analysis.calls==[] and all(r.kind=="model" for r in store.reservations("tiny-run"))


def test_proven_undispatched_native_allowance_is_released(store,context,model_request,monkeypatch):
    from scrabble_harness.harness.budget import BudgetDeadlineError
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(context)
    def expired(reservation_id):
        raise BudgetDeadlineError("Expired before dispatch")
    monkeypatch.setattr(ledger,"dispatch_native",expired)
    with pytest.raises(BudgetDeadlineError):
        asyncio.run(ToolExecutor(analysis,ledger).execute(model_request.attempt_id))
    assert analysis.calls==[]
    assert next(r for r in store.reservations("run-a") if r.kind=="native").state=="released"
    assert ledger.totals(decision_id=context.decision_id).charged.engine_work_units==0


def test_known_receipt_reuses_evidence_after_deadline_without_new_work(store,context,model_request):
    clock=FakeClock()
    ledger=save_reply(store,model_request,(comparison_call("compare-a",("c01","c02")),),ledger=BudgetLedger(store,clock=clock))
    analysis=RecordingAnalysis(context)
    executor=ToolExecutor(analysis,ledger)
    first=asyncio.run(executor.execute(model_request.attempt_id))
    clock.advance(40)
    second=asyncio.run(executor.execute(model_request.attempt_id))
    assert second==first and len(analysis.calls)==1


def test_evidence_output_size_failure_preserves_receipt_and_full_evidence(store,context,manifest):
    from scrabble_harness.harness import ExecutionManifest
    from scrabble_harness.harness.errors import PromptSizeError
    config=changed(context.config,presentation=changed(context.config.presentation,max_tool_result_bytes=256))
    new=ExecutionManifest.create(**manifest.model_dump(mode="python",exclude={"manifest_id"})|{"harness_config":config})
    other=prepare_other(store,new,context,run_id="small-result-run",decision_id="small-result")
    request=build_request(other,new,attempt_id="small-result-attempt")
    request=changed(request,tools=("select_move","compare_moves"),force_selection=False)
    ledger=save_reply(store,request,(comparison_call("compare-a",("c01","c02")),))
    analysis=RecordingAnalysis(other)
    executor=ToolExecutor(analysis,ledger)
    with pytest.raises(PromptSizeError):
        asyncio.run(executor.execute(request.attempt_id))
    assert len(store.get_decision(other.decision_id).context.evidence)==1
    assert ledger.totals(decision_id=other.decision_id).settled.engine_work_units==60
    with pytest.raises(PromptSizeError):
        asyncio.run(executor.execute(request.attempt_id))
    assert len(analysis.calls)==1


def test_host_issued_but_unseen_evidence_cannot_be_cited_by_old_reply(store,context,manifest):
    new=new_manifest(manifest,decision_limits=changed(context.config.decision_limits,max_model_attempts=3))
    other=prepare_other(store,new,context,run_id="deep-run",decision_id="deep-decision")
    first=build_request(other,new,attempt_id="deep-first")
    first=changed(first,tools=("select_move","compare_moves"),force_selection=False)
    ledger=save_reply(store,first,(comparison_call("compare-a",("c01","c02")),))
    old=build_model_request(other.model_view(),attempt_id="old-selection",model=first.model,capabilities=first.capabilities,
        remaining_limits=ledger.remaining_limits(other.decision_id),config=other.config,force_selection=True)
    save_reply(store,old,(selection_call("old-call","c01",evidence_ids=("e01",)),),ledger=ledger)
    analysis=RecordingAnalysis(other)
    executor=ToolExecutor(analysis,ledger)
    asyncio.run(executor.execute(first.attempt_id))
    with pytest.raises(EvidenceValidationError,match="not displayed"):
        asyncio.run(executor.execute(old.attempt_id))
    assert len(analysis.calls)==1
