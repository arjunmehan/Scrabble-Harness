"""Real pinned SDK, fake HTTP transport: no credentials or paid API calls."""
import asyncio
import json
import subprocess
import sys

import pytest

from scrabble_harness.domain import ModelSettings, canonical_bytes
from scrabble_harness.harness import DecisionLoop, build_model_request
from scrabble_harness.harness.budget import InputTokenBound, verified_input_bound
from scrabble_harness.providers import ProviderCapabilities, ProviderContinuation
from scrabble_harness.providers.openai import (
    SDK_VERSION, OpenAIProvider, ProviderTransportError, normalize_response, serialize_request,
)
from scrabble_harness.runs import BudgetLedger, RunStore
from test_store import context, manifest, db_path, changed
from test_loop import PreparedAnalysis, attempt


@pytest.fixture
def caps():
    return ProviderCapabilities(provider="openai", model="test-model", max_output_tokens=1024)


@pytest.fixture
def model_request(context, caps):
    return build_model_request(context.model_view(), attempt_id="attempt-first",
        model=ModelSettings(provider="openai", model=caps.model, max_output_tokens=1024),
        capabilities=caps, remaining_limits=context.config.decision_limits, config=context.config)


def call(name="select_move", *, call_id="call-one", arguments=None):
    args = arguments or ({"candidate_handles": ["c01", "c02"], "tier": "light"} if name == "compare_moves"
                        else {"candidate_handle": "c01", "evidence_ids": [], "brief_note": "Chosen."})
    return {"type": "function_call", "id": "fc-distinct-item-id", "call_id": call_id,
            "name": name, "arguments": json.dumps(args), "status": "completed"}


def receipt(*, output=None, status="completed", usage=True, **updates):
    return {"id": "resp-test", "object": "response", "created_at": 1.0, "model": "test-model",
        "status": status, "output": [call()] if output is None else output,
        "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
            "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 30}, "total_tokens": 150} if usage else None,
        **updates}


def sdk():
    module = pytest.importorskip("openai", reason="install scrabble-harness[openai] for transport acceptance")
    assert module.__version__ == SDK_VERSION
    return module, pytest.importorskip("httpx2")


def transport_client(handler, *, retries=5):
    openai, httpx = sdk()
    client = openai.AsyncOpenAI(api_key="fake-test-credential", base_url="https://test.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=retries)
    return client, httpx


def test_wire_request_contains_only_public_prompt_and_strict_tools(model_request):
    wire = serialize_request(model_request)
    assert wire["instructions"] == model_request.instructions
    assert wire["input"] == [{"role": "user", "content": "Choose a move using the offered function tools."}]
    assert "view" not in wire and "evidence" not in wire
    assert wire["parallel_tool_calls"] is False and wire["store"] is False
    assert wire["include"] == ["reasoning.encrypted_content"]
    assert wire["tool_choice"] == "required" and wire["max_output_tokens"] == 1024
    for tool in wire["tools"]:
        assert tool["strict"] is True and tool["parameters"]["additionalProperties"] is False
        assert set(tool["parameters"]["required"]) == set(tool["parameters"]["properties"])
    assert "temperature" not in wire and "reasoning" not in wire


def test_final_selection_and_supported_settings(model_request):
    capabilities = changed(model_request.capabilities, supports_temperature=True, supported_reasoning_efforts=("low",))
    model = changed(model_request.model, temperature=0.3, reasoning_effort="low")
    final = changed(model_request, capabilities=capabilities, model=model, tools=("select_move",), force_selection=True)
    wire = serialize_request(final)
    assert wire["tool_choice"] == {"type": "function", "name": "select_move"}
    assert len(wire["tools"]) == 1 and wire["temperature"] == 0.3 and wire["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("settings", [{"temperature": 0.2}, {"reasoning_effort": "high"}, {"max_output_tokens": 2048}])
def test_unsupported_settings_rejected_before_transport(model_request, settings):
    with pytest.raises(ValueError):
        changed(model_request, model=changed(model_request.model, **settings))


def test_unknown_usage_is_never_zero(model_request):
    assert normalize_response(model_request, receipt(usage=False)).usage is None
    data = receipt(usage=False) | {"usage": {"unknown_counter": 9}}
    normalized = normalize_response(model_request, data).usage
    assert normalized.input_tokens is None and normalized.output_tokens is None
    assert json.loads(normalized.raw_usage_json) == {"unknown_counter": 9}


def test_usage_preserves_cache_writes_and_reasoning(model_request):
    reply = normalize_response(model_request, receipt(), request_id="http-model_request-one")
    assert reply.usage.input_tokens == 100 and reply.usage.output_tokens == 50
    assert reply.usage.cached_input_tokens == 20 and reply.usage.cache_write_input_tokens == 10
    assert reply.usage.reasoning_output_tokens == 30 and reply.usage.total_tokens == 150
    assert reply.request_id == "http-model_request-one" and reply.response_id == "resp-test"
    assert reply.tool_calls[0].call_id == "call-one"


@pytest.mark.parametrize("counts", [{"input_tokens": -1}, {"total_tokens": 999}, {"output_tokens": True}])
def test_invalid_usage_remains_unknown_auditable(model_request, counts):
    data = receipt()
    data["usage"].update(counts)
    usage = normalize_response(model_request, data).usage
    assert usage.input_tokens is None and usage.output_tokens is None
    assert json.loads(usage.raw_usage_json) == data["usage"]


@pytest.mark.parametrize("status", ["incomplete", "failed", "queued", "in_progress"])
def test_noncompleted_receipt_never_selects_or_replays_partial_output(model_request, status):
    reply = normalize_response(model_request, receipt(status=status, incomplete_details={"reason": "max_output_tokens"}))
    assert reply.status == (status if status in {"incomplete", "failed"} else "failed")
    assert reply.usage.output_tokens == 50 and reply.continuation is None


def test_refusal_retains_usage_without_refusal_text_in_public_reply(model_request):
    message = {"type": "message", "id": "msg-refusal", "role": "assistant", "status": "completed",
               "content": [{"type": "refusal", "refusal": "Refusal text"}]}
    reply = normalize_response(model_request, receipt(output=[message]))
    assert reply.status == "refused" and reply.usage.total_tokens == 150
    assert reply.continuation is None and reply.visible_text == ""


@pytest.mark.parametrize("bad", ["malformed", "unknown", "multiple"])
def test_unusable_actions_are_saved_before_host_validation(model_request, bad):
    outputs = [call()]
    if bad == "malformed": outputs[0]["arguments"] = "{bad"
    if bad == "unknown": outputs[0]["name"] = "unoffered_tool"
    if bad == "multiple": outputs.append(call(call_id="call-two"))
    reply = normalize_response(model_request, receipt(output=outputs))
    assert reply.status == "completed" and reply.usage.total_tokens == 150
    assert len(reply.tool_calls) == len(outputs)
    assert reply.tool_calls[0].arguments_json == outputs[0]["arguments"]


@pytest.mark.parametrize("output", [[{"type": "web_search_call"}], [call()] * 17,
    [{"type": "message", "content": [{"type": "output_text", "text": "x" * 32769}]}]])
def test_unsupported_or_oversized_output_fails_with_usage(model_request, output):
    reply = normalize_response(model_request, receipt(output=output))
    assert reply.status == "failed" and reply.error_category == "invalid_response"
    assert reply.usage.total_tokens == 150 and not reply.tool_calls


def next_request(model_request, continuation, *, evidence=()):
    return changed(model_request, attempt_id="attempt-next", continuation=continuation,
                   tools=("select_move",), force_selection=True, evidence=evidence)


def test_private_reasoning_history_is_replayed_and_repair_closes_calls(model_request):
    reasoning = {"type": "reasoning", "id": "rs-private", "summary": [], "encrypted_content": "opaque-private"}
    reply = normalize_response(model_request, receipt(output=[reasoning, call(arguments={"candidate_handle": "c99"})]))
    persisted = ProviderContinuation.model_validate_json(reply.continuation.model_dump_json())
    wire = serialize_request(next_request(model_request, persisted))
    assert wire["input"][1] == reasoning
    assert wire["input"][2]["id"] == "fc-distinct-item-id"
    assert wire["input"][3]["call_id"] == "call-one"
    assert json.loads(wire["input"][3]["output"])["error"] == "action_not_executed"
    assert "opaque-private" not in wire["instructions"]


def test_continuation_rejects_different_position_model_versions(model_request):
    continuation = normalize_response(model_request, receipt()).continuation
    for updates in [{"view": changed(model_request.view, decision_id="decision-other")},
                    {"prompt_version": "different-version"},
                    {"model": changed(model_request.model, max_output_tokens=512)}]:
        with pytest.raises(ValueError, match="another position"):
            serialize_request(changed(next_request(model_request, continuation), **updates))


def test_wire_bound_includes_schemas_and_opaque_history(model_request):
    reasoning = {"type": "reasoning", "id": "rs-big", "summary": [], "encrypted_content": "é" * 12000}
    continuation = normalize_response(model_request, receipt(output=[reasoning, call()])).continuation
    bounded = next_request(model_request, continuation)
    assert len(bounded.model_dump_json().encode()) < bounded.max_request_bytes
    with pytest.raises(ValueError, match="wire payload"):
        serialize_request(bounded)


def test_core_import_does_not_load_sdk():
    subprocess.run([sys.executable, "-c", "import sys; import scrabble_harness.providers; "
        "import scrabble_harness.providers.openai; assert 'openai' not in sys.modules"], check=True)


def test_real_sdk_serialization_and_request_id(model_request, caps):
    seen = []
    async def go():
        def handler(req):
            seen.append(req)
            return httpx.Response(200, json=receipt(), headers={"x-request-id": "req-http"})
        client, httpx = transport_client(handler)
        provider = OpenAIProvider(capabilities=caps, client=client)
        try:
            reply = await provider.complete(model_request)
            assert reply.request_id == "req-http"
            assert json.loads(seen[0].content) == serialize_request(model_request)
            assert len(seen[0].content) <= model_request.max_request_bytes
            assert seen[0].headers["x-client-request-id"] == model_request.attempt_id
            assert "fake-test-credential" not in reply.model_dump_json()
            await provider.aclose()
            assert not client.is_closed()  # injected client belongs to caller
        finally:
            await client.close()
    asyncio.run(go())


@pytest.mark.parametrize("http_status", [429, 500, 503])
def test_sdk_retries_disabled_even_for_injected_retrying_client(model_request, caps, http_status):
    seen = []
    async def go():
        def handler(req):
            seen.append(req)
            return httpx.Response(http_status, json={"error": {"message": "private-error-body", "type": "server_error"}})
        client, httpx = transport_client(handler, retries=5)
        try:
            provider = OpenAIProvider(capabilities=caps, client=client)
            with pytest.raises(ProviderTransportError) as exc:
                await provider.complete(model_request)
            assert "private-error-body" not in str(exc.value)
        finally:
            await client.close()
    asyncio.run(go())
    assert len(seen) == 1


def test_timeout_and_cancellation_do_not_retry(model_request, caps):
    async def go():
        seen = []
        async def handler(req):
            seen.append(req)
            await asyncio.sleep(1)
            return httpx.Response(200, json=receipt())
        client, httpx = transport_client(handler)
        try:
            provider = OpenAIProvider(capabilities=caps, client=client, timeout_seconds=0.01)
            with pytest.raises(ProviderTransportError):
                await provider.complete(model_request)
            assert len(seen) == 1
            task = asyncio.create_task(provider.complete(model_request))
            await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert len(seen) == 2
        finally: await client.close()
    asyncio.run(go())


def openai_manifest(manifest, *, retries=0):
    fields = manifest.model_dump(mode="python")
    fields.pop("manifest_id"); fields.pop("schema_version")
    fields["run_config"] = changed(manifest.run_config, model=ModelSettings(
        provider="openai", model="test-model", max_output_tokens=1024))
    fields["rate_card"] = changed(manifest.rate_card, provider="openai", model="test-model",
        cached_input_microusd_per_million_tokens=100000, cache_write_microusd_per_million_tokens=100000)
    fields["harness_config"] = changed(manifest.harness_config, max_transport_retries=retries)
    return type(manifest).create(**fields)


def loop_for(provider, store, context, caps):
    ledger = BudgetLedger(store)
    finish = InputTokenBound(provider="openai", model="test-model", purpose="final_selection",
        method="verified_complete_input_v1", input_tokens=10000, max_output_tokens=1024,
        evidence="Synthetic fake-transport bound including encrypted continuation")
    return DecisionLoop(provider, PreparedAnalysis(context), ledger, capabilities=caps,
        input_bound=lambda req: verified_input_bound(req, input_tokens=1000,
            evidence="Synthetic fake-transport complete wire bound"), finish_bound=finish)


def test_end_to_end_compare_select_private_receipt_and_reopen(db_path, manifest, context, caps):
    seen = []
    async def go():
        def handler(req):
            wire = json.loads(req.content); seen.append(wire)
            if len(seen) == 1:
                return httpx.Response(200, json=receipt(output=[
                    {"type": "reasoning", "id": "rs-one", "summary": [], "encrypted_content": "secret-ciphertext"},
                    call("compare_moves", call_id="call-compare")]))
            result = next(item for item in wire["input"] if item.get("type") == "function_call_output")
            evidence = json.loads(result["output"])
            assert result["call_id"] == "call-compare" and evidence["evidence_id"] == "e01"
            assert evidence["requested_handles"] == ["c01", "c02"]
            assert "candidate_id" not in result["output"]
            return httpx.Response(200, json=receipt(output=[call(call_id="call-select", arguments={
                "candidate_handle": "c01", "evidence_ids": ["e01"], "brief_note": "Compared."})]))
        client, httpx = transport_client(handler)
        try:
            with RunStore(db_path) as store:
                m = openai_manifest(manifest)
                store.create_campaign("campaign-a", m.run_config.limits); store.create_run("run-a", "campaign-a", m)
                provider = OpenAIProvider(capabilities=caps, client=client)
                outcome = await loop_for(provider, store, context, caps).decide("run-a", "decision-a", context.observation)
                assert outcome.status == "selected" and outcome.usage.model_attempts == 2
                assert outcome.usage.tool_steps == 1 and outcome.usage.confirmed_engine_work_units == 60
                assert outcome.usage.settled_api_cost_microusd == 40
                assert "secret-ciphertext" not in outcome.model_dump_json()
                saved = store.get_attempt(attempt(1)).reply
                assert saved.usage.reasoning_output_tokens == 30 and "secret-ciphertext" in saved.continuation.payload_json
                saved_bytes = canonical_bytes(saved)
            with RunStore(db_path) as reopened:
                assert canonical_bytes(reopened.get_attempt(attempt(1)).reply) == saved_bytes
                assert reopened.get_decision("decision-a").outcome == outcome
                reopened.check_integrity()
        finally: await client.close()
    asyncio.run(go())
    assert len(seen) == 2


@pytest.mark.parametrize("retries", [0, 1])
def test_explicit_retry_requires_fresh_durable_harness_attempt(db_path, manifest, context, caps, retries):
    seen = []
    async def go():
        def handler(req):
            seen.append(req.headers["x-client-request-id"])
            if len(seen) == 1:
                return httpx.Response(500, json={"error": {"message": "failure"}})
            return httpx.Response(200, json=receipt())
        client, httpx = transport_client(handler)
        try:
            with RunStore(db_path) as store:
                m = openai_manifest(manifest, retries=retries)
                store.create_campaign("campaign-a", m.run_config.limits); store.create_run("run-a", "campaign-a", m)
                provider = OpenAIProvider(capabilities=caps, client=client)
                outcome = await loop_for(provider, store, context, caps).decide("run-a", "decision-a", context.observation)
                assert outcome.status == ("selected" if retries else "fallback")
                assert outcome.usage.model_attempts == retries + 1
                assert outcome.usage.unresolved_api_cost_microusd > 0
                assert store.get_attempt(attempt(1)).reply is None
                if retries: assert store.get_attempt(attempt(2)).reply.usage.total_tokens == 150
        finally: await client.close()
    asyncio.run(go())
    assert seen == [attempt(i) for i in range(1, retries + 2)]


def test_incomplete_call_and_duplicate_ids_cannot_create_usable_continuation(model_request):
    partial = call(); partial["status"] = "incomplete"
    reply = normalize_response(model_request, receipt(output=[partial]))
    assert reply.status == "failed" and reply.usage.total_tokens == 150
    duplicated = normalize_response(model_request, receipt(output=[call(), call()]))
    assert len(duplicated.tool_calls) == 2 and duplicated.continuation is None


def test_optional_details_remain_unknown_and_do_not_double_count_reasoning(model_request):
    data = receipt(); data["usage"] = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    usage = normalize_response(model_request, data).usage
    assert usage.cached_input_tokens is None and usage.cache_write_input_tokens is None
    assert usage.reasoning_output_tokens is None and usage.total_tokens == 150


def test_continuation_envelope_is_closed_and_versioned(model_request):
    continuation = normalize_response(model_request, receipt()).continuation
    state = json.loads(continuation.payload_json)
    for update in [{"adapter": "future-adapter"}, {"credential": "should-not-be-accepted"}]:
        forged = changed(continuation, payload_json=json.dumps(state | update))
        with pytest.raises(ValueError): serialize_request(next_request(model_request, forged))


@pytest.mark.parametrize("bad", ["refusal", "truncated", "malformed", "multiple"])
def test_real_sdk_receipt_saved_and_repaired_with_separate_attempt(db_path, manifest, context, caps, bad):
    seen = []
    async def go():
        def handler(req):
            wire = json.loads(req.content); seen.append(wire)
            if len(seen) == 2:
                assert wire["tool_choice"] == {"type": "function", "name": "select_move"}
                assert "REPAIR:" in wire["instructions"]
                if bad in {"malformed", "multiple"}:
                    results = [item for item in wire["input"] if item.get("type") == "function_call_output"]
                    assert len(results) == (2 if bad == "multiple" else 1)
                    assert all(json.loads(item["output"])["error"] == "action_not_executed" for item in results)
                return httpx.Response(200, json=receipt(output=[call(call_id="call-repaired")]))
            if bad == "refusal":
                data = receipt(output=[{"type": "message", "id": "msg-one", "role": "assistant", "status": "completed",
                    "content": [{"type": "refusal", "refusal": "Declined"}]}])
            elif bad == "truncated": data = receipt(status="incomplete")
            elif bad == "malformed":
                invalid = call(); invalid["arguments"] = "{bad"
                data = receipt(output=[invalid])
            else: data = receipt(output=[call(), call(call_id="call-other")])
            return httpx.Response(200, json=data)
        client, httpx = transport_client(handler)
        try:
            with RunStore(db_path) as store:
                m = openai_manifest(manifest)
                store.create_campaign("campaign-a", m.run_config.limits); store.create_run("run-a", "campaign-a", m)
                loop = loop_for(OpenAIProvider(capabilities=caps, client=client), store, context, caps)
                outcome = await loop.decide("run-a", "decision-a", context.observation)
                assert outcome.status == "selected" and outcome.usage.model_attempts == 2
                assert outcome.usage.settled_api_cost_microusd == 40 and outcome.usage.tool_steps == 0
                assert store.get_attempt(attempt(1)).reply.usage.total_tokens == 150
        finally: await client.close()
    asyncio.run(go())
    assert len(seen) == 2


def test_deep_history_correlates_each_batch_without_replaying_old_outputs(db_path, manifest, context, caps):
    from scrabble_harness.harness import load_workflow
    deep = load_workflow("deep")
    context = changed(context, config=deep)
    m = openai_manifest(manifest)
    fields = m.model_dump(mode="python")
    fields.pop("manifest_id"); fields.pop("schema_version")
    fields["harness_config"] = deep
    fields["run_config"] = changed(m.run_config, policy_version="deep", candidate_config=deep.candidate_config)
    m = type(manifest).create(**fields)
    seen = []
    async def go():
        def handler(req):
            wire = json.loads(req.content); seen.append(wire)
            count = len(seen)
            results = [item for item in wire["input"] if item.get("type") == "function_call_output"]
            assert [r["call_id"] for r in results] == ["call-compare-" + str(i) for i in range(1, count)]
            assert [json.loads(r["output"])["evidence_id"] for r in results] == ["e0" + str(i) for i in range(1, count)]
            if count < 3:
                output = [call("compare_moves", call_id="call-compare-" + str(count), arguments={
                    "candidate_handles": ["c01", "c02"], "tier": "light" if count == 1 else "standard"})]
            else: output = [call(call_id="call-final", arguments={
                "candidate_handle": "c01", "evidence_ids": ["e01", "e02"], "brief_note": "Compared twice"})]
            return httpx.Response(200, json=receipt(output=output))
        client, httpx = transport_client(handler)
        try:
            with RunStore(db_path) as store:
                store.create_campaign("campaign-a", m.run_config.limits); store.create_run("run-a", "campaign-a", m)
                loop = loop_for(OpenAIProvider(capabilities=caps, client=client), store, context, caps)
                outcome = await loop.decide("run-a", "decision-a", context.observation)
                assert outcome.status == "selected" and outcome.usage.model_attempts == 3
                assert outcome.usage.tool_steps == 2 and outcome.usage.confirmed_engine_work_units == 360
                assert outcome.usage.settled_api_cost_microusd == 60
                # Binding changed/removed evidence fails before another HTTP call.
                req = store.get_attempt(attempt(3)).request
                with pytest.raises(ValueError, match="changed or removed"):
                    serialize_request(changed(req, evidence=req.evidence[1:]))
                altered = changed(req.evidence[-1], requested_handles=tuple(reversed(req.evidence[-1].requested_handles)))
                with pytest.raises(ValueError, match="differs from its pending"):
                    serialize_request(changed(req, evidence=req.evidence[:-1] + (altered,)))
        finally: await client.close()
    asyncio.run(go())
    assert len(seen) == 3


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_invalid_timeouts_are_rejected_without_sdk_import(caps, timeout):
    with pytest.raises(ValueError): OpenAIProvider(capabilities=caps, timeout_seconds=timeout)


def test_provider_owns_only_its_created_client(caps):
    sdk()
    async def go():
        provider = OpenAIProvider(capabilities=caps, api_key="fake-test-credential")
        async with provider:
            assert not provider._client.is_closed()
            assert "fake-test-credential" not in repr(provider)
        assert provider._client.is_closed()
        await provider.aclose()
    asyncio.run(go())
