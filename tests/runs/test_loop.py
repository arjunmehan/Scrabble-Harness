"""Offline decision termination and durable accounting, not model quality."""

import asyncio
import hashlib

import pytest

from scrabble_harness.domain import MovePreview, canonical_bytes
from scrabble_harness.harness import DecisionLoop
from scrabble_harness.harness.budget import InputTokenBound, verified_input_bound
from scrabble_harness.harness.errors import ToolExecutionError
from scrabble_harness.providers import (
    ProviderCapabilities, ProviderContinuation, ProviderToolCall, ScriptedProvider,
    ScriptedReply, ScriptedStep, ScriptedTransportFailure, TokenUsage,
    comparison_call, selection_call,
)
from scrabble_harness.runs import BudgetLedger, RunStore
from test_store import context, manifest, db_path, changed
from test_ledger import FakeClock
from test_tools import RecordingAnalysis


class PreparedAnalysis(RecordingAnalysis):
    def __init__(self, context):
        super().__init__(context)
        self.preparation = []
        self.prepare_failure = False
        self.prepare_delay = 0.0
        self.on_load = None

    async def load_observation(self, observation):
        self.preparation.append("load")
        assert observation == self.context.observation
        if self.on_load:
            self.on_load()
        if self.prepare_failure:
            raise RuntimeError("Cannot prepare")
        await asyncio.sleep(self.prepare_delay)

    async def generate_candidates(self, config):
        self.preparation.append("generate")
        return self.context.full_pool

    async def preview(self, move):
        self.preparation.append("preview")
        candidate = next(c for c in self.context.full_pool if c.move == move)
        return MovePreview(observation_id=self.context.observation.observation_id,
            move=move, exact_score=candidate.exact_score, leave=candidate.leave,
            board_after=self.context.observation.board)

    async def analyze(self, ids, limits, seed):
        result = await super().analyze(ids, limits, seed)
        return changed(result, analysis_id="analysis-" + str(len(self.calls)))


def attempt(index):
    return "attempt_" + hashlib.sha256(f"decision-a:{index}".encode()).hexdigest()


def step(index, calls=(), *, status="completed", usage=True, continuation=None, delay=0.0):
    return ScriptedStep(attempt_id=attempt(index), delay_seconds=delay,
        outcome=ScriptedReply(response_id=f"response-{index}", request_id=f"request-{index}",
            status=status, tool_calls=calls, continuation=continuation,
            usage=TokenUsage(input_tokens=10, output_tokens=5) if usage else None))


def setup(db_path, manifest, context, steps, *, clock=None, finish=True, timeout=30.0, campaign_limits=None):
    store = RunStore(db_path)
    store.create_campaign("campaign-a", campaign_limits or manifest.run_config.limits)
    store.create_run("run-a", "campaign-a", manifest)
    ledger = BudgetLedger(store, clock=clock)
    caps = ProviderCapabilities(provider=manifest.run_config.model.provider,
        model=manifest.run_config.model.model, max_output_tokens=1024)
    provider = ScriptedProvider(steps, capabilities=caps)
    analysis = PreparedAnalysis(context)
    final_bound = InputTokenBound(provider=caps.provider, model=caps.model, purpose="final_selection",
        method="verified_complete_input_v1", input_tokens=10000, max_output_tokens=1024,
        evidence="Synthetic complete follow-up ceiling includes continuation") if finish else None
    loop = DecisionLoop(provider, analysis, ledger, capabilities=caps,
        input_bound=lambda request: verified_input_bound(request, input_tokens=1000,
            evidence="Synthetic full-request token ceiling"), finish_bound=final_bound,
        model_timeout_seconds=timeout)
    return store, ledger, provider, analysis, loop


def run(loop, context):
    return asyncio.run(loop.decide("run-a", "decision-a", context.observation))


def new_config(manifest, **updates):
    config = changed(manifest.harness_config, **updates)
    fields = manifest.model_dump(mode="python")
    fields.pop("manifest_id")
    fields.pop("schema_version")
    fields["harness_config"] = config
    return type(manifest).create(**fields)


def test_early_selection_prepares_once_releases_hold_and_reopens(db_path, manifest, context):
    store, ledger, provider, analysis, loop = setup(db_path,manifest,context,[step(1,(selection_call("s1","c01"),))])
    with store:
        outcome = run(loop,context)
        assert outcome.status == "selected" and outcome.usage.model_attempts == 1
        assert analysis.preparation == ["load","generate","preview"] and analysis.calls == []
        assert store.finish_hold("decision-a").state == "released"
        events = store.events("run-a")
        assert events.index(next(e for e in events if e.payload.kind == "attempt_replied")) < events.index(next(e for e in events if e.payload.kind == "decision_completed"))
        assert run(loop,context) == outcome and len(provider.requests) == 1
    with RunStore(db_path) as reopened:
        assert canonical_bytes(reopened.get_decision("decision-a").outcome) == canonical_bytes(outcome)


def test_compare_select_preserves_evidence_continuation_and_usage(db_path,manifest,context):
    continuation=ProviderContinuation(provider="fixture",payload_json='{"token":"private"}')
    steps=[step(1,(comparison_call("c1",("c01","c02")),),continuation=continuation),
        step(2,(selection_call("s2","c01",evidence_ids=("e01",)),))]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected" and outcome.decision.evidence_ids == ("e01",)
        assert outcome.usage.model_attempts == 2 and outcome.usage.tool_steps == 1
        assert outcome.usage.confirmed_engine_work_units == 60 and len(outcome.evidence) == 1
        assert provider.requests[1].tools == ("select_move",)
        assert provider.requests[1].continuation == continuation
        assert provider.requests[1].evidence == store.get_decision("decision-a").context.evidence_views()
        assert store.finish_hold("decision-a").state == "consumed"
        assert len(analysis.calls) == 1


@pytest.mark.parametrize("bad", ["refused","incomplete","failed","empty","multiple","malformed","unknown","forged","huge"])
def test_invalid_reply_gets_one_selection_only_repair(db_path,manifest,context,bad):
    status=bad if bad in {"refused","incomplete","failed"} else "completed"
    calls={"empty":(),"multiple":(selection_call("a","c01"),selection_call("b","c02")),
        "malformed":(ProviderToolCall(call_id="a",name="select_move",arguments_json="{bad"),),
        "unknown":(selection_call("a","c99"),),"forged":(selection_call("a","c01",evidence_ids=("e99",)),),
        "huge":(ProviderToolCall(call_id="a",name="compare_moves",arguments_json='{"candidate_handles":["c01","c02"],"tier":"huge"}'),)}.get(bad,())
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,calls,status=status),step(2,(selection_call("fixed","c01"),))])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected" and outcome.usage.model_attempts == 2
        assert "REPAIR:" in provider.requests[1].instructions
        assert provider.requests[1].tools == ("select_move",) and not analysis.calls
        assert store.get_attempt(attempt(1)).reply.tool_calls == calls


def test_second_invalid_reply_falls_back_without_third_call(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1),step(2)])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "invalid_action"
        assert outcome.decision.fallback_reason == "invalid_action" and len(provider.requests) == 2


def test_missing_usage_selection_is_valid_but_full_cost_remains_unresolved(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(selection_call("s","c01"),),usage=False)])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected" and outcome.usage.model_attempts == 1
        assert outcome.usage.unresolved_api_cost_microusd > 0


@pytest.mark.parametrize("failure", ["transport","timeout","native"])
def test_failures_terminate_with_unresolved_allowance(db_path,manifest,context,failure):
    steps=[step(1,(comparison_call("c",("c01","c02")),))]
    if failure == "transport":
        steps=[ScriptedStep(attempt_id=attempt(1),outcome=ScriptedTransportFailure(error_category="connection_error"))]
    elif failure == "timeout":
        steps=[step(1,(selection_call("s","c01"),),delay=60.0)]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps,timeout=0.01 if failure=="timeout" else 30.0)
    if failure == "native": analysis.failure=RuntimeError("Lost engine receipt")
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and len(provider.requests) == 1
        assert (outcome.usage.unresolved_engine_work_units == 60) if failure=="native" else (outcome.usage.unresolved_api_cost_microusd > 0)
        assert store.finish_hold("decision-a").state == "released"


def test_explicit_transport_retry_has_fresh_attempt_and_keeps_original_charge(db_path,manifest,context):
    manifest=new_config(manifest,max_transport_retries=1)
    steps=[ScriptedStep(attempt_id=attempt(1),outcome=ScriptedTransportFailure(error_category="timeout")),step(2,(selection_call("s","c01"),))]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected" and outcome.usage.model_attempts == 2
        assert outcome.usage.unresolved_api_cost_microusd > 0
        assert provider.requests[1].force_selection
        assert store.get_attempt(attempt(1)).state == "unresolved"


@pytest.mark.parametrize("phase", ["preparation","model","native"])
def test_cancellation_aborts_durably_and_propagates(db_path,manifest,context,phase):
    steps=[step(1,(comparison_call("c",("c01","c02")),),delay=60.0 if phase=="model" else 0.0)]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    if phase=="preparation": analysis.prepare_delay=60.0
    if phase=="native": analysis.block=True
    async def scenario():
        task=asyncio.create_task(loop.decide("run-a","decision-a",context.observation))
        for _ in range(1000):
            ready=analysis.preparation if phase=="preparation" else provider.requests if phase=="model" else analysis.calls
            if ready: break
            await asyncio.sleep(0.001)
        assert ready
        with pytest.raises(ToolExecutionError):
            await loop.decide("run-a","other",context.observation)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    with store:
        asyncio.run(scenario())
        outcome=store.get_decision("decision-a").outcome
        assert outcome.status == "aborted" and outcome.terminal_reason == "cancelled"
        assert outcome.usage.model_attempts == (0 if phase=="preparation" else 1)
        assert outcome.usage.unresolved_engine_work_units == (60 if phase=="native" else 0)


def test_preparation_failure_aborts_before_provider(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[])
    analysis.prepare_failure=True
    with store:
        outcome=run(loop,context)
        assert outcome.status == "aborted" and not outcome.trusted_pool
        assert outcome.terminal_reason == "preparation_failed" and not provider.requests


def test_preparation_time_consumes_absolute_deadline(db_path,manifest,context):
    clock=FakeClock()
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[],clock=clock)
    analysis.on_load=lambda:clock.advance(31.0)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "budget_exhausted"
        assert outcome.usage.elapsed_seconds == 31.0 and not provider.requests


def test_no_finish_bound_exposes_selection_only(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(selection_call("s","c01"),))],finish=False)
    with store:
        assert run(loop,context).status == "selected"
        assert provider.requests[0].tools == ("select_move",) and store.finish_hold("decision-a") is None


def test_unfinished_decision_never_replays(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[])
    with store:
        ledger.begin_decision("run-a","decision-a",context.observation)
        with pytest.raises(ToolExecutionError,match="recovery"):
            run(loop,context)
        assert not analysis.preparation and not provider.requests


def test_zero_attempt_budget_returns_static_fallback(db_path,manifest,context):
    manifest=new_config(manifest,decision_limits=changed(manifest.harness_config.decision_limits,max_model_attempts=0))
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.usage.model_attempts == 0


def test_repeated_comparison_is_rejected_before_second_native_call(db_path,manifest,context):
    manifest=new_config(manifest,decision_limits=changed(manifest.harness_config.decision_limits,
        max_model_attempts=3,max_tool_steps=2,max_engine_work_units=600))
    steps=[step(1,(comparison_call("c1",("c01","c02")),)),step(2,(comparison_call("c2",("c02","c01")),))]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "repeated_action"
        assert outcome.usage.model_attempts == 2 and outcome.usage.tool_steps == 1
        assert len(analysis.calls) == 1


def test_held_request_bound_overrun_blocks_final_provider_call(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(comparison_call("c",("c01","c02")),))])
    original=loop.input_bound
    loop.input_bound=lambda request: verified_input_bound(request,input_tokens=10001,evidence="Exceeds held follow-up") if request.evidence else original(request)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "budget_exhausted"
        assert len(provider.requests) == 1 and outcome.usage.confirmed_engine_work_units == 60


def test_exhausted_shared_money_prevents_any_request(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[],
        campaign_limits=changed(manifest.run_config.limits,max_api_cost_microusd=0))
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.usage.model_attempts == 0
        assert not provider.requests and not store.reservations("run-a")


def test_late_reply_is_saved_and_billed_but_not_selected(db_path,manifest,context):
    clock=FakeClock()
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(selection_call("s","c01"),))],clock=clock)
    original=provider.complete
    async def late(request):
        reply=await original(request)
        clock.advance(31.0)
        return reply
    provider.complete=late
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "budget_exhausted"
        assert outcome.usage.model_attempts == 1 and outcome.usage.settled_api_cost_microusd > 0
        assert store.get_attempt(attempt(1)).reply is not None


def test_wrong_seed_native_receipt_is_charged_without_repair(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(comparison_call("c",("c01","c02")),))])
    analysis.transform=lambda result:changed(result,seed=result.seed+1)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "tool_failed"
        assert not outcome.evidence and outcome.usage.confirmed_engine_work_units == 60
        assert len(provider.requests) == 1


def test_comparison_in_final_reply_cannot_run_native_again(db_path,manifest,context):
    steps=[step(1,(comparison_call("c1",("c01","c02")),)),step(2,(comparison_call("c2",("c01","c02")),))]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and len(analysis.calls) == 1
        assert outcome.usage.model_attempts == 2 and outcome.usage.tool_steps == 1


def test_model_usage_overrun_blocks_comparison(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(comparison_call("c",("c01","c02")),))])
    original=provider.complete
    async def overrun(request):
        return changed(await original(request),usage=TokenUsage(input_tokens=1001,output_tokens=5))
    provider.complete=overrun
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.usage.model_attempts == 1
        assert not analysis.calls and ledger.totals(decision_id="decision-a").bound_exceeded


def test_foreign_reply_is_unresolved_without_action(db_path,manifest,context):
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(selection_call("s","c01"),))])
    original=provider.complete
    async def foreign(request):
        return changed(await original(request),attempt_id="foreign-attempt")
    provider.complete=foreign
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "reply_rejected"
        assert outcome.usage.unresolved_api_cost_microusd > 0
        assert store.get_attempt(attempt(1)).reply is None


def test_three_attempt_config_keeps_finish_hold_across_two_comparisons(db_path,manifest,context):
    manifest=new_config(manifest,decision_limits=changed(manifest.harness_config.decision_limits,
        max_model_attempts=3,max_tool_steps=2,max_engine_work_units=600))
    steps=[step(1,(comparison_call("c1",("c01","c02"),tier="light"),)),
        step(2,(comparison_call("c2",("c01","c02"),tier="standard"),)),
        step(3,(selection_call("s3","c01",evidence_ids=("e02",)),))]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected" and outcome.usage.model_attempts == 3
        assert outcome.usage.tool_steps == 2 and outcome.usage.confirmed_engine_work_units == 360
        assert len(outcome.evidence) == 2 and store.finish_hold("decision-a").state == "consumed"
        assert provider.requests[1].tools == ("select_move","compare_moves")
        assert provider.requests[2].tools == ("select_move",)


def test_disabled_repairs_stop_after_first_invalid_reply(db_path,manifest,context):
    manifest=new_config(manifest,max_repair_rounds=0)
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1)])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "invalid_action"
        assert len(provider.requests) == 1


def test_repeated_call_identity_in_final_selection_is_not_accepted(db_path,manifest,context):
    steps=[step(1,(comparison_call("same",("c01","c02")),)),step(2,(selection_call("same","c01"),))]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.terminal_reason == "repeated_action"
        assert len(analysis.calls) == 1 and len(provider.requests) == 2
