"""Offline scripted outcomes through Task 5's durable billing boundary."""

import asyncio

import pytest

from scrabble_harness.harness import build_model_request
from scrabble_harness.harness.budget import InputTokenBound, verified_input_bound
from scrabble_harness.providers import (
    ProviderContinuation, ProviderToolCall, ScriptedProvider, ScriptedReply, ScriptedStep,
    ScriptedTransportError, ScriptedTransportFailure, TokenUsage, comparison_call, selection_call,
)
from scrabble_harness.runs import BudgetLedger, RunStore
from test_store import context, manifest, model_request, db_path, store, changed


def bound(request):
    return verified_input_bound(request, input_tokens=1000, evidence="Synthetic scripted-provider input bound")


@pytest.mark.parametrize("scenario", ["selection", "refused", "incomplete", "malformed", "multiple", "missing_usage"])
def test_scripted_reply_is_saved_and_accounted_before_action_validation(store, db_path, model_request, scenario):
    request = changed(model_request, tools=("select_move",), force_selection=True)
    status = scenario if scenario in {"refused", "incomplete"} else "completed"
    calls = (selection_call("call-a", "c01"),)
    if scenario in {"refused", "incomplete"}:
        calls = ()
    elif scenario == "malformed":
        calls = (ProviderToolCall(call_id="call-a", name="select_move", arguments_json="{malformed retained verbatim"),)
    elif scenario == "multiple":
        calls += (selection_call("call-b", "c02"),)
    usage = None if scenario == "missing_usage" else TokenUsage(input_tokens=10, output_tokens=5)
    provider = ScriptedProvider((ScriptedStep(attempt_id=request.attempt_id, outcome=ScriptedReply(
        response_id="script-response-a", request_id="script-request-a", status=status, tool_calls=calls, usage=usage)),),
        capabilities=request.capabilities)
    ledger = BudgetLedger(store)
    reservation = ledger.reserve_model("model-a", request, bound(request))
    permit = ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    async def scenario_call():
        async with asyncio.timeout(permit.timeout_seconds):
            return await provider.complete(request)
    reply = asyncio.run(scenario_call())
    saved = ledger.settle_model("model-a", reply)
    assert saved.state == ("unresolved" if usage is None else "settled")
    totals = ledger.totals(campaign_id="campaign-a")
    assert totals.charged.model_calls == 1
    assert totals.charged.api_cost_microusd == (reservation.allocation.api_cost_microusd if usage is None else 2)
    with RunStore(db_path) as reopened:
        assert reopened.get_attempt(request.attempt_id).reply == reply
        assert reopened.get_attempt(request.attempt_id).reply.tool_calls == calls
        assert reopened.get_attempt(request.attempt_id).reply.response_id == "script-response-a"
        assert BudgetLedger(reopened).totals(campaign_id="campaign-a") == totals


@pytest.mark.parametrize("category", ["connection_error", "timeout", "rate_limit", "server_error"])
def test_transport_error_retains_allowance_and_dispatch_marker(store, db_path, model_request, category):
    request = changed(model_request, tools=("select_move",), force_selection=True)
    provider = ScriptedProvider((ScriptedStep(attempt_id=request.attempt_id,
        outcome=ScriptedTransportFailure(error_category=category, request_id="failed-request")),), capabilities=request.capabilities)
    ledger = BudgetLedger(store)
    reservation = ledger.reserve_model("model-a", request, bound(request))
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    with pytest.raises(ScriptedTransportError):
        asyncio.run(provider.complete(request))
    ledger.mark_unresolved("model-a")
    with RunStore(db_path) as reopened:
        assert reopened.get_attempt(request.attempt_id).state == "unresolved"
        assert reopened.get_attempt(request.attempt_id).reply is None
        assert BudgetLedger(reopened).totals(campaign_id="campaign-a").unresolved.api_cost_microusd == reservation.allocation.api_cost_microusd
        assert BudgetLedger(reopened).totals(campaign_id="campaign-a").charged.model_calls == 1
    assert len(provider.requests) == 1


def test_caller_timeout_and_explicit_retry_keep_distinct_billable_attempts(store, model_request):
    request = changed(model_request, tools=("select_move",), force_selection=True)
    retry = changed(request, attempt_id="attempt-retry")
    provider = ScriptedProvider((ScriptedStep(attempt_id=request.attempt_id,
        outcome=ScriptedReply(response_id="delayed-response", request_id="delayed-request"), delay_seconds=60.0),
        ScriptedStep(attempt_id=retry.attempt_id, outcome=ScriptedReply(response_id="retry-response", request_id="retry-request",
            tool_calls=(selection_call("retry-call", "c01"),), usage=TokenUsage(input_tokens=10, output_tokens=5)))),
        capabilities=request.capabilities)
    ledger = BudgetLedger(store)
    first = ledger.reserve_model("first", request, bound(request))
    permit = ledger.dispatch_model("first", requested_timeout_seconds=0.02)
    async def scenario():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(permit.timeout_seconds):
                await provider.complete(request)
        ledger.mark_unresolved("first")
        ledger.reserve_model("retry", retry, bound(retry))
        second_permit = ledger.dispatch_model("retry", requested_timeout_seconds=5.0)
        async with asyncio.timeout(second_permit.timeout_seconds):
            reply = await provider.complete(retry)
        ledger.settle_model("retry", reply)
    asyncio.run(scenario())
    totals = ledger.totals(decision_id="decision-a")
    assert totals.charged.model_calls == 2 and totals.settled.model_calls == 1 and totals.unresolved.model_calls == 1
    assert totals.unresolved.api_cost_microusd == first.allocation.api_cost_microusd
    assert store.get_attempt(request.attempt_id).reply is None
    assert store.get_attempt(retry.attempt_id).reply.response_id == "retry-response"
    assert tuple(call.state for call in provider.calls) == ("cancelled", "replied")


def test_compare_and_forced_selection_use_finish_hold_and_saved_continuation(store, context, manifest, model_request):
    initial = model_request
    continuation = ProviderContinuation(provider="fixture", payload_json='{"script_token":"private-continuation"}')
    provider = ScriptedProvider((ScriptedStep(attempt_id=initial.attempt_id, outcome=ScriptedReply(
        response_id="compare-response", request_id="compare-request", tool_calls=(comparison_call("compare-call", ("c01", "c02")),),
        continuation=continuation, usage=TokenUsage(input_tokens=10, output_tokens=5))),
        ScriptedStep(attempt_id="final-attempt", outcome=ScriptedReply(response_id="final-response", request_id="final-request",
            tool_calls=(selection_call("final-call", "c01"),), usage=TokenUsage(input_tokens=20, output_tokens=10)))),
        capabilities=initial.capabilities)
    ledger = BudgetLedger(store)
    finish_bound = InputTokenBound(provider="fixture", model="fixture-model", purpose="final_selection",
        method="verified_complete_input_v1", input_tokens=10000, max_output_tokens=1024,
        evidence="Synthetic complete follow-up ceiling includes continuation")
    ledger.reserve_model("initial", initial, bound(initial), finish_bound=finish_bound)
    ledger.dispatch_model("initial", requested_timeout_seconds=5.0)
    async def scenario():
        first = await provider.complete(initial)
        ledger.settle_model("initial", first)
        final = build_model_request(context.model_view(), attempt_id="final-attempt", model=manifest.run_config.model,
            capabilities=initial.capabilities, config=context.config, remaining_limits=ledger.remaining_limits(context.decision_id),
            force_selection=True, continuation=store.get_attempt(initial.attempt_id).reply.continuation)
        ledger.reserve_model("final", final, bound(final), use_finish_hold=True)
        ledger.dispatch_model("final", requested_timeout_seconds=5.0)
        last = await provider.complete(final)
        ledger.settle_model("final", last)
        return last
    reply = asyncio.run(scenario())
    assert reply.attempt_id == "final-attempt" and provider.requests[1].continuation == continuation
    assert provider.requests[1].tools == ("select_move",)
    assert store.finish_hold(context.decision_id).state == "consumed"
    assert ledger.totals(decision_id=context.decision_id).settled.model_calls == 2
    assert ledger.totals(decision_id=context.decision_id).charged.api_cost_microusd == 6
