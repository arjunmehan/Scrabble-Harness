"""Scripted provider behavior; all outcomes are synthetic and offline."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import subprocess
import sys

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import ModelSettings, canonical_bytes
from scrabble_harness.harness import DecisionLimits, parse_tool_arguments
from scrabble_harness.harness.errors import ToolSchemaError
from scrabble_harness.providers import (
    ModelReply, ModelRequest, ProviderCapabilities, ProviderContinuation, ProviderToolCall,
    ScriptExhaustedError, ScriptMismatchError, ScriptedCall, ScriptedProvider, ScriptedReply,
    ScriptedStep, ScriptedTransportError, ScriptedTransportFailure, TokenUsage,
    comparison_call, selection_call,
)
from conftest import update


@pytest.fixture
def model_request(context):
    return ModelRequest(attempt_id="attempt-a",
        model=ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024),
        capabilities=ProviderCapabilities(provider="fixture", model="fixture-model", max_output_tokens=2048),
        view=context.model_view(), remaining_limits=DecisionLimits(), instructions="Choose a visible move.",
        prompt_version=context.config.prompt_version, tool_schema_version=context.config.tool_schema_version)


def response(**changes):
    return ScriptedReply(response_id="response-a", request_id="request-a", **changes)


def scripted(request, outcome=None, **changes):
    return ScriptedProvider((ScriptedStep(attempt_id=request.attempt_id,
        outcome=outcome or response(), **changes),), capabilities=request.capabilities)


def test_selection_response_has_exact_scope_and_explicit_identities(model_request):
    usage = TokenUsage(input_tokens=100, cached_input_tokens=0, cache_write_input_tokens=0,
                       output_tokens=10, reasoning_output_tokens=5, total_tokens=110)
    provider = scripted(model_request, response(resolved_model="fixture-model-resolved", usage=usage,
        tool_calls=(selection_call("call-a", "c01", brief_note="Synthetic selection"),)))
    reply = asyncio.run(provider.complete(model_request))
    assert reply.attempt_id == model_request.attempt_id
    assert reply.decision_id == model_request.view.decision_id
    assert reply.observation_id == model_request.view.observation.observation_id
    assert (reply.response_id, reply.request_id, reply.resolved_model) == ("response-a", "request-a", "fixture-model-resolved")
    assert reply.usage == usage
    action = parse_tool_arguments(reply.tool_calls[0].name, reply.tool_calls[0].arguments_json)
    assert action.candidate_handle == "c01" and action.brief_note == "Synthetic selection"
    assert provider.remaining_steps == 0 and provider.requests == (model_request,)
    assert provider.calls[0].reply == reply and provider.calls[0].state == "replied"
    assert ModelReply.model_validate_json(reply.model_dump_json()) == reply
    assert ScriptedCall.model_validate_json(provider.calls[0].model_dump_json()) == provider.calls[0]


def test_compare_then_select_preserves_continuation_and_evidence_request(model_request):
    continuation = ProviderContinuation(provider="fixture", payload_json='{"opaque":"private-script-state"}')
    initial = response(tool_calls=(comparison_call("compare-a", ("c01", "c02")),), continuation=continuation)
    final_request = update(model_request, attempt_id="attempt-b", force_selection=True, tools=("select_move",),
                           continuation=continuation)
    final = ScriptedReply(response_id="response-b", request_id="request-b",
        tool_calls=(selection_call("select-b", "c02", evidence_ids=("e01",)),))
    provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=initial),
        ScriptedStep(attempt_id="attempt-b", outcome=final,
            expected_request_sha256=hashlib.sha256(canonical_bytes(final_request)).hexdigest())),
        capabilities=model_request.capabilities)
    async def scenario():
        first = await provider.complete(model_request)
        assert first.continuation == continuation
        assert parse_tool_arguments(first.tool_calls[0].name, first.tool_calls[0].arguments_json).tier == "light"
        second = await provider.complete(final_request)
        assert parse_tool_arguments(second.tool_calls[0].name, second.tool_calls[0].arguments_json).evidence_ids == ("e01",)
        return second
    reply = asyncio.run(scenario())
    assert reply.attempt_id == "attempt-b" and provider.requests[1].continuation == continuation
    assert provider.remaining_steps == 0


@pytest.mark.parametrize("status", ["completed", "refused", "incomplete", "failed"])
def test_reply_status_and_usage_are_not_reinterpreted(model_request, status):
    usage = TokenUsage(input_tokens=10, output_tokens=5)
    provider = scripted(model_request, response(status=status, visible_text="Synthetic refusal or explanation",
        usage=usage, error_category="synthetic_error" if status == "failed" else None))
    reply = asyncio.run(provider.complete(model_request))
    assert reply.status == status and reply.usage == usage and reply.tool_calls == ()
    assert reply.visible_text == "Synthetic refusal or explanation"


@pytest.mark.parametrize("usage", [None, TokenUsage(total_tokens=30), TokenUsage(output_tokens=5),
                                   TokenUsage(input_tokens=0, output_tokens=0)])
def test_unknown_partial_and_zero_usage_are_distinct(model_request, usage):
    reply = asyncio.run(scripted(model_request, response(usage=usage)).complete(model_request))
    assert reply.usage == usage


@pytest.mark.parametrize("arguments", ["{broken", "", '{"candidate_handle":"c99"}', '{"x":NaN}'])
def test_malformed_argument_text_is_retained_verbatim(model_request, arguments):
    call = ProviderToolCall(call_id="call-a", name="select_move", arguments_json=arguments)
    reply = asyncio.run(scripted(model_request, response(tool_calls=(call,))).complete(model_request))
    assert reply.tool_calls == (call,)
    with pytest.raises(ToolSchemaError):
        parse_tool_arguments(call.name, call.arguments_json)


def test_multiple_actions_duplicate_call_ids_and_unknown_tools_survive(model_request):
    calls = (selection_call("duplicate-call", "c01"), comparison_call("duplicate-call", ("c01", "c02")),
             ProviderToolCall(call_id="unknown", name="unsupported_tool", arguments_json="{}"))
    reply = asyncio.run(scripted(model_request, response(tool_calls=calls)).complete(model_request))
    assert reply.tool_calls == calls
    assert len(reply.tool_calls) == 3


def test_script_can_return_a_tool_not_offered_on_the_final_request(model_request):
    final = update(model_request, tools=("select_move",), force_selection=True)
    reply = asyncio.run(scripted(final, response(tool_calls=(comparison_call("bad-final", ("c01", "c02")),))).complete(final))
    with pytest.raises(ToolSchemaError):
        parse_tool_arguments(reply.tool_calls[0].name, reply.tool_calls[0].arguments_json, allowed_tools=final.tools)


@pytest.mark.parametrize("category", ["connection_error", "timeout", "rate_limit", "server_error"])
def test_transport_failures_consume_one_step_without_implicit_retries(model_request, category):
    provider = scripted(model_request, ScriptedTransportFailure(error_category=category, request_id="failed-request"))
    with pytest.raises(ScriptedTransportError) as error:
        asyncio.run(provider.complete(model_request))
    assert error.value.attempt_id == "attempt-a" and error.value.error_category == category
    assert error.value.request_id == "failed-request"
    assert len(provider.requests) == 1 and provider.remaining_steps == 0
    assert provider.calls[0].state == "transport_error" and provider.calls[0].reply is None


def test_retry_is_an_explicit_second_attempt(model_request):
    retry = update(model_request, attempt_id="attempt-retry")
    provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=ScriptedTransportFailure(error_category="timeout")),
        ScriptedStep(attempt_id="attempt-retry", outcome=response())), capabilities=model_request.capabilities)
    async def scenario():
        with pytest.raises(ScriptedTransportError):
            await provider.complete(model_request)
        assert provider.remaining_steps == 1
        with pytest.raises(ScriptMismatchError):
            await provider.complete(model_request)
        assert provider.remaining_steps == 1
        return await provider.complete(retry)
    assert asyncio.run(scenario()).attempt_id == "attempt-retry"
    assert tuple(call.state for call in provider.calls) == ("transport_error", "replied")


def test_mismatch_and_exhaustion_do_not_consume_outcomes(model_request):
    provider = scripted(model_request, expected_request_sha256=hashlib.sha256(canonical_bytes(model_request)).hexdigest())
    async def scenario():
        for bad in (update(model_request, attempt_id="wrong"), update(model_request, instructions="Changed public prompt"),
                    update(model_request, capabilities=update(model_request.capabilities, supports_temperature=True))):
            with pytest.raises(ScriptMismatchError):
                await provider.complete(bad)
            assert provider.remaining_steps == 1 and provider.calls == ()
        await provider.complete(model_request)
        with pytest.raises(ScriptMismatchError):
            await provider.complete(model_request)
        with pytest.raises(ScriptExhaustedError):
            await provider.complete(update(model_request, attempt_id="extra-attempt"))
    asyncio.run(scenario())
    assert len(provider.calls) == 1


def test_empty_script_is_explicitly_exhausted(model_request):
    provider = ScriptedProvider((), capabilities=model_request.capabilities)
    with pytest.raises(ScriptExhaustedError):
        asyncio.run(provider.complete(model_request))
    assert provider.requests == ()


def test_delay_is_controllable_and_completed_reply_is_recorded(model_request):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        delays = []
        async def sleeper(seconds):
            delays.append(seconds)
            entered.set()
            await release.wait()
        provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response(), delay_seconds=3.0),),
            capabilities=model_request.capabilities, sleeper=sleeper)
        task = asyncio.create_task(provider.complete(model_request))
        await asyncio.wait_for(entered.wait(), 1.0)
        assert provider.calls[0].state == "pending" and provider.requests == (model_request,)
        assert provider.remaining_steps == 0 and not task.done()
        release.set()
        reply = await asyncio.wait_for(task, 1.0)
        assert delays == [3.0] and provider.calls[0].state == "replied"
        return reply
    assert asyncio.run(scenario()).response_id == "response-a"


def test_default_delay_can_be_cancelled_by_caller_timeout(model_request):
    provider = scripted(model_request, delay_seconds=60.0)
    async def scenario():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await provider.complete(model_request)
        assert provider.calls[0].state == "cancelled" and provider.calls[0].reply is None
        with pytest.raises(ScriptMismatchError):
            await provider.complete(model_request)
    asyncio.run(scenario())
    assert len(provider.requests) == 1 and provider.remaining_steps == 0


def test_external_cancellation_is_not_converted_to_a_reply(model_request):
    async def scenario():
        entered = asyncio.Event()
        async def sleeper(seconds):
            entered.set()
            await asyncio.Event().wait()
        provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response(), delay_seconds=1.0),),
            capabilities=model_request.capabilities, sleeper=sleeper)
        task = asyncio.create_task(provider.complete(model_request))
        await asyncio.wait_for(entered.wait(), 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.calls[0].state == "cancelled" and provider.remaining_steps == 0
    asyncio.run(scenario())


def test_injected_sleeper_failure_propagates_without_replaying(model_request):
    async def sleeper(seconds):
        raise OSError("Injected local failure")
    provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response(), delay_seconds=1.0),),
        capabilities=model_request.capabilities, sleeper=sleeper)
    with pytest.raises(OSError, match="Injected local failure"):
        asyncio.run(provider.complete(model_request))
    assert provider.calls[0].state == "interrupted" and provider.remaining_steps == 0


def test_concurrent_completions_keep_distinct_slots_and_replies(model_request):
    second = update(model_request, attempt_id="attempt-b")
    async def scenario():
        first_entered, release = asyncio.Event(), asyncio.Event()
        async def sleeper(seconds):
            first_entered.set()
            await release.wait()
        provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response(), delay_seconds=1.0),
            ScriptedStep(attempt_id="attempt-b", outcome=ScriptedReply(response_id="response-b", request_id="request-b"))),
            capabilities=model_request.capabilities, sleeper=sleeper)
        first = asyncio.create_task(provider.complete(model_request))
        await asyncio.wait_for(first_entered.wait(), 1.0)
        reply_b = await provider.complete(second)
        with pytest.raises(ScriptMismatchError):
            await provider.complete(model_request)
        assert tuple(call.state for call in provider.calls) == ("pending", "replied")
        release.set()
        reply_a = await asyncio.wait_for(first, 1.0)
        assert tuple(call.reply.response_id for call in provider.calls) == ("response-a", "response-b")
        return reply_a, reply_b
    assert tuple(reply.attempt_id for reply in asyncio.run(scenario())) == ("attempt-a", "attempt-b")


def test_provider_is_owned_by_one_event_loop_and_thread(model_request):
    second = update(model_request, attempt_id="attempt-b")
    provider = ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response()),
        ScriptedStep(attempt_id="attempt-b", outcome=ScriptedReply(response_id="response-b", request_id="request-b"))),
        capabilities=model_request.capabilities)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ScriptMismatchError, match="thread"):
            pool.submit(asyncio.run, provider.complete(model_request)).result(timeout=5)
    asyncio.run(provider.complete(model_request))
    with pytest.raises(ScriptMismatchError, match="event loop"):
        asyncio.run(provider.complete(second))
    assert provider.remaining_steps == 1


def test_scripts_are_serializable_and_reproducible_with_fresh_instances(model_request):
    step = ScriptedStep(attempt_id=model_request.attempt_id, outcome=response(
        tool_calls=(selection_call("call-a", "c01"),)), expected_request_sha256=hashlib.sha256(canonical_bytes(model_request)).hexdigest())
    loaded = ScriptedStep.model_validate_json(step.model_dump_json())
    assert loaded == step
    replies = [asyncio.run(ScriptedProvider((loaded,), capabilities=model_request.capabilities).complete(model_request)) for _ in range(2)]
    assert canonical_bytes(replies[0]) == canonical_bytes(replies[1])


@pytest.mark.parametrize("delay", [-1.0, float("inf"), float("nan"), True, "1"])
def test_invalid_delays_fail_validation(delay):
    with pytest.raises(ValidationError):
        ScriptedStep(attempt_id="attempt-a", outcome=response(), delay_seconds=delay)


def test_scripts_require_explicit_identities_and_valid_outcomes(model_request):
    with pytest.raises(ValidationError):
        ScriptedReply(response_id="response-a")
    with pytest.raises(ValidationError):
        ScriptedStep(attempt_id="attempt-a", outcome={"kind": "automatic_retry"})
    with pytest.raises(ScriptMismatchError):
        ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response()),
            ScriptedStep(attempt_id="attempt-a", outcome=ScriptedTransportFailure(error_category="timeout"))),
            capabilities=model_request.capabilities)
    with pytest.raises(ScriptMismatchError):
        ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response()),
            ScriptedStep(attempt_id="attempt-b", outcome=response())), capabilities=model_request.capabilities)
    with pytest.raises(ScriptMismatchError):
        scripted(model_request, response(continuation=ProviderContinuation(provider="other", payload_json="{}")))
    with pytest.raises(ScriptMismatchError):
        ScriptedProvider((ScriptedStep(attempt_id="attempt-a", outcome=response(
            continuation=ProviderContinuation(provider="fixture", payload_json="{}"))),),
            capabilities=update(model_request.capabilities, supports_stateless_continuation=False))


def test_inspection_snapshots_are_immutable(model_request):
    provider = scripted(model_request)
    asyncio.run(provider.complete(model_request))
    with pytest.raises(ValidationError):
        provider.calls[0].state = "pending"
    with pytest.raises(ValidationError):
        provider.requests[0].attempt_id = "changed"
    assert isinstance(provider.calls, tuple) and isinstance(provider.requests, tuple)


def test_scripted_provider_needs_no_keys_sdk_engine_or_network():
    program = '''
import sys
from scrabble_harness.providers import ScriptedProvider, ScriptedReply, ScriptedStep, ProviderCapabilities
assert not any(name == "ctypes" or name.startswith(("openai", "anthropic", "scrabble_harness.engine")) for name in sys.modules)
provider = ScriptedProvider((ScriptedStep(attempt_id="offline", outcome=ScriptedReply(response_id="response",request_id="request")),),
    capabilities=ProviderCapabilities(provider="offline",model="scripted",max_output_tokens=1024))
assert provider.remaining_steps == 1
print("offline imports")
'''
    result = subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "offline imports"
