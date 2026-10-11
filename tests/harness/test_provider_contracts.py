import subprocess
import sys

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import Checkpoint, ModelSettings
from scrabble_harness.harness import DecisionLimits, DecisionView
from scrabble_harness.providers import (
    ModelReply, ModelRequest, ProviderCapabilities, ProviderContinuation, ProviderToolCall, TokenUsage,
)
from conftest import update


@pytest.fixture
def model_request(context):
    return ModelRequest(attempt_id="attempt-a",
        model=ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024),
        capabilities=ProviderCapabilities(provider="fixture", model="fixture-model", max_output_tokens=2048),
        view=context.model_view(), remaining_limits=DecisionLimits(), instructions="Choose a visible move.",
        prompt_version=context.config.prompt_version, tool_schema_version=context.config.tool_schema_version)


def test_request_roundtrip_and_private_structured_inputs_rejected(model_request, context, state):
    assert ModelRequest.model_validate_json(model_request.model_dump_json()) == model_request
    checkpoint = Checkpoint(checkpoint_id="private-fixture", engine_commit="a" * 40, state=state)
    for private in (state, state.model_dump(mode="python"), checkpoint, checkpoint.model_dump(mode="python"),
                    context, context.model_dump(mode="python")):
        with pytest.raises(ValidationError):
            update(model_request, view=private)
    payload = model_request.view.model_dump(mode="python")
    payload["candidates"] = (payload["candidates"][0] | {"static_equity": 100.0},) + payload["candidates"][1:]
    with pytest.raises(ValidationError):
        DecisionView.model_validate(payload)


@pytest.mark.parametrize("problem", ["provider", "model", "temperature", "reasoning", "output_cap", "no_tools", "no_strict",
                                     "forced_compare", "no_attempts", "last_attempt_compare", "no_tool_steps", "no_work", "duplicate_tools"])
def test_invalid_request_capabilities_and_allowances(model_request, problem):
    changes = {}
    if problem in {"provider", "model", "temperature", "reasoning", "output_cap"}:
        field, value = {"provider": ("provider", "other"), "model": ("model", "other"),
                        "temperature": ("temperature", 0.5), "reasoning": ("reasoning_effort", "high"),
                        "output_cap": ("max_output_tokens", 2049)}[problem]
        changes["model"] = update(model_request.model, **{field: value})
    elif problem in {"no_tools", "no_strict"}:
        changes["capabilities"] = update(model_request.capabilities, **{
            "supports_tools" if problem == "no_tools" else "supports_strict_tools": False})
    elif problem == "forced_compare":
        changes["force_selection"] = True
    elif problem == "duplicate_tools":
        changes["tools"] = ("select_move", "select_move")
    else:
        field, value = {"no_attempts": ("max_model_attempts", 0), "last_attempt_compare": ("max_model_attempts", 1),
                        "no_tool_steps": ("max_tool_steps", 0), "no_work": ("max_engine_work_units", 0)}[problem]
        changes["remaining_limits"] = update(model_request.remaining_limits, **{field: value})
    with pytest.raises(ValidationError):
        update(model_request, **changes)


def test_last_request_select_only_and_supported_settings(model_request):
    final = update(model_request, tools=("select_move",), force_selection=True,
                   remaining_limits=update(model_request.remaining_limits, max_model_attempts=1, max_tool_steps=0,
                                           max_engine_work_units=0))
    assert final.force_selection
    supported = update(model_request, capabilities=update(model_request.capabilities, supports_temperature=True,
                       supported_reasoning_efforts=("high",)),
                       model=update(model_request.model, temperature=0.5, reasoning_effort="high"))
    assert supported.model.reasoning_effort == "high"


def test_request_bound_counts_utf8_bytes_and_continuation(model_request):
    text = "🙂" * 3000
    assert len(text) < 16000
    with pytest.raises(ValidationError, match="byte ceiling"):
        update(model_request, instructions=text, max_request_bytes=10000)
    continuation = ProviderContinuation(provider="fixture", payload_json='{"opaque_token":"test"}')
    assert update(model_request, continuation=continuation).continuation == continuation
    for changes in ({"continuation": update(continuation, provider="other")},
                    {"continuation": continuation, "capabilities": update(model_request.capabilities, supports_stateless_continuation=False)}):
        with pytest.raises(ValidationError):
            update(model_request, **changes)


def test_request_evidence_is_scoped_and_visible(model_request, evidenced_context):
    evidence = evidenced_context.evidence_views()
    assert update(model_request, evidence=evidence).evidence == evidence
    for bad in (update(evidence[0], decision_id="other"),
                update(evidence[0], requested_handles=("c01", "c99"), values=(), completion_status="timeout")):
        with pytest.raises(ValidationError):
            update(model_request, evidence=(bad,))


def test_reply_preserves_unusable_calls_usage_and_missing_usage(model_request):
    calls = (ProviderToolCall(call_id="call-a", name="select_move", arguments_json="{broken JSON"),
             ProviderToolCall(call_id="call-b", name="unexpected_tool", arguments_json=""))
    usage = TokenUsage(input_tokens=100, cached_input_tokens=50, output_tokens=20,
                       reasoning_output_tokens=10, total_tokens=120, raw_usage_json='{"vendor_count":120}')
    reply = ModelReply(attempt_id=model_request.attempt_id, decision_id=model_request.view.decision_id,
        observation_id=model_request.view.observation.observation_id, provider="fixture", resolved_model="fixture-model-2026",
        status="completed", tool_calls=calls, usage=usage)
    assert ModelReply.model_validate_json(reply.model_dump_json()) == reply
    assert reply.tool_calls[0].arguments_json == "{broken JSON"
    assert update(reply, status="failed", usage=None).usage is None


@pytest.mark.parametrize("fields", [
    {}, {"input_tokens": True}, {"input_tokens": "10"}, {"input_tokens": -1},
    {"input_tokens": 10, "cached_input_tokens": 11}, {"output_tokens": 10, "reasoning_output_tokens": 11},
    {"input_tokens": 10, "output_tokens": 10, "total_tokens": 21},
    {"raw_usage_json": "[]"}, {"raw_usage_json": '{"cost":NaN}'},
    {"raw_usage_json": '{"cost":1e999}'}, {"raw_usage_json": '{"x":"' + "🙂" * 17000 + '"}'},
])
def test_invalid_usage_is_not_silently_normalized(fields):
    with pytest.raises(ValidationError):
        TokenUsage(**fields)


@pytest.mark.parametrize("payload", ["null", "42", '{"x":Infinity}', '{"x":1e999}', "broken"])
def test_continuation_must_be_bounded_finite_json(payload):
    with pytest.raises(ValidationError):
        ProviderContinuation(provider="fixture", payload_json=payload)


def test_unknown_usage_components_remain_unknown():
    assert TokenUsage(input_tokens=0).output_tokens is None
    assert TokenUsage(raw_usage_json='{"vendor_field":2}').total_tokens is None


def test_imports_do_not_load_provider_sdks_native_bindings_or_workers():
    program = '''
import sys
import scrabble_harness.providers
import scrabble_harness.harness
assert not any(name == "ctypes" or name.startswith(("openai", "anthropic", "scrabble_harness.engine")) for name in sys.modules)
print("safe imports")
'''
    result = subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == "safe imports"
