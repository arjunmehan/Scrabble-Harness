"""One-attempt Responses adapter. Importing this module does not import the SDK.

Continuation is private host data, never prompt text or a public replay field.
The caller owns admission, token bounds, pricing, retries, and game commits.
"""

import asyncio
import hashlib
import json
import math
from typing import Any

from pydantic import Field

from ..domain._base import DomainModel
from ..harness.contracts import CompareAction
from ..harness.tool_schemas import _json_object, parse_tool_arguments, tool_definitions
from .protocol import (
    ModelReply, ModelRequest, ProviderCapabilities, ProviderContinuation,
    ProviderToolCall, TokenUsage,
)

SDK_VERSION = "3.29.0"
_START = {"role": "user", "content": "Choose a move using the offered function tools."}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _scope(request: ModelRequest) -> str:
    # Evidence/remaining limits/repair instructions change each attempt; the
    # position, model settings, and protocol versions must remain identical.
    return hashlib.sha256(_json({"view": request.view.model_dump(mode="json"),
        "model": request.model.model_dump(mode="json"),
        "capabilities": request.capabilities.model_dump(mode="json"),
        "prompt_version": request.prompt_version,
        "tool_schema_version": request.tool_schema_version}).encode()).hexdigest()


def _evidence(request: ModelRequest) -> dict[str, str]:
    return {item.evidence_id: hashlib.sha256(item.model_dump_json().encode()).hexdigest()
            for item in request.evidence}


class _Continuation(DomainModel):
    adapter: str = "openai-responses-v1"
    scope_sha256: str
    history: list[dict[str, Any]] = Field(max_length=4096)
    pending: list[dict[str, Any]] = Field(max_length=16)
    evidence_sha256: dict[str, str]


class ProviderTransportError(RuntimeError):
    """Sanitized failure; no SDK exception body, request headers, or credentials."""


def serialize_request(request: ModelRequest) -> dict[str, Any]:
    """Build the complete wire body locally, including stateless history.

    This is also the payload a trusted input-bound calculator must cover. Its
    byte size is a presentation limit, not a claimed token or billing bound.
    """
    request = ModelRequest.model_validate(request)
    if request.model.provider != "openai":
        raise ValueError("OpenAI adapter requires provider=openai")
    if not request.capabilities.supports_stateless_continuation:
        raise ValueError("OpenAI adapter requires stateless continuation support")
    history = [dict(_START)]
    if request.continuation is not None:
        state = _Continuation.model_validate_json(request.continuation.payload_json)
        if state.adapter != "openai-responses-v1" or state.scope_sha256 != _scope(request):
            raise ValueError("continuation belongs to another position or configuration")
        current = _evidence(request)
        if any(current.get(key) != digest for key, digest in state.evidence_sha256.items()):
            raise ValueError("prior evidence was changed or removed")
        new = [item for item in request.evidence if item.evidence_id not in state.evidence_sha256]
        history = list(state.history)
        if new:
            if len(new) != 1 or len(state.pending) != 1:
                raise ValueError("comparison evidence cannot be correlated uniquely")
            call = state.pending[0]
            action = parse_tool_arguments(call["name"], call["arguments"],
                                          version=request.tool_schema_version)
            if (not isinstance(action, CompareAction)
                    or action.candidate_handles != new[0].requested_handles or action.tier != new[0].tier):
                raise ValueError("comparison evidence differs from its pending tool call")
        for call in state.pending:
            # A repair closes unexecuted/invalid actions without inventing a
            # native receipt or echoing model arguments as host instructions.
            result = (new[0].model_dump_json() if new else
                      _json({"error": "action_not_executed", "instruction": "Use the current offered tools."}))
            history.append({"type": "function_call_output", "call_id": call["call_id"], "output": result})
        if not state.pending:
            history.append({"role": "user", "content": "Return a tool action under the current instructions."})
    payload = {"model": request.model.model, "instructions": request.instructions,
        "input": history, "tools": [tool.as_function_tool() for tool in
            tool_definitions(request.tools, version=request.tool_schema_version)],
        "tool_choice": {"type": "function", "name": "select_move"} if request.force_selection else "required",
        "parallel_tool_calls": False, "max_output_tokens": request.model.max_output_tokens,
        "store": False, "include": ["reasoning.encrypted_content"]}
    if request.model.temperature is not None:
        payload["temperature"] = request.model.temperature
    if request.model.reasoning_effort is not None:
        if request.model.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("reasoning effort is unsupported by the pinned SDK")
        payload["reasoning"] = {"effort": request.model.reasoning_effort}
    # ASCII encoding is conservative relative to the SDK's UTF-8 compact JSON.
    if len(_json(payload).encode()) > request.max_request_bytes:
        raise ValueError("OpenAI wire payload exceeds the request byte ceiling")
    return payload


def _usage(data: dict | None) -> TokenUsage | None:
    if data is None:
        return None
    raw = _json(data)
    try:
        inputs = data.get("input_tokens_details") or {}
        outputs = data.get("output_tokens_details") or {}
        return TokenUsage(input_tokens=data.get("input_tokens"),
            cached_input_tokens=inputs.get("cached_tokens"),
            cache_write_input_tokens=inputs.get("cache_write_tokens"),
            output_tokens=data.get("output_tokens"),
            reasoning_output_tokens=outputs.get("reasoning_tokens"),
            total_tokens=data.get("total_tokens"), raw_usage_json=raw)
    except (ValueError, TypeError, AttributeError):
        # Keep malformed vendor usage for audit, but do not price bogus counts.
        return TokenUsage(raw_usage_json=raw)


def normalize_response(request: ModelRequest, data: dict, *, request_id: str | None = None,
                       payload: dict | None = None) -> ModelReply:
    """Translate a receipt before the harness validates or executes its actions."""
    usage = _usage(data.get("usage"))
    base = dict(attempt_id=request.attempt_id, decision_id=request.view.decision_id,
        observation_id=request.view.observation.observation_id, provider="openai",
        resolved_model=data.get("model") or request.model.model, response_id=data.get("id"),
        request_id=request_id, usage=usage)
    try:
        status = data.get("status")
        if status not in {"completed", "incomplete", "failed"}:
            return ModelReply(**base, status="failed", error_category="unsupported_response_status")
        calls, texts, pending = [], [], []
        refused = False
        output = data.get("output") or []
        for item in output:
            if item["type"] == "function_call":
                if status == "completed" and item.get("status", "completed") != "completed":
                    raise ValueError("function call is incomplete")
                calls.append(ProviderToolCall(call_id=item["call_id"], name=item["name"],
                                              arguments_json=item["arguments"]))
                pending.append(item)
            elif item["type"] == "message":
                for content in item.get("content", []):
                    if content["type"] == "refusal":
                        refused = True
                    elif content["type"] == "output_text":
                        texts.append(content["text"])
                    else:
                        raise ValueError("unsupported message content")
            elif item["type"] != "reasoning":
                raise ValueError("unsupported output item")
        continuation = None
        if status == "completed" and not refused and len({call.call_id for call in calls}) == len(calls):
            state = _Continuation(scope_sha256=_scope(request),
                history=(payload or serialize_request(request))["input"] + output,
                pending=pending, evidence_sha256=_evidence(request))
            continuation = ProviderContinuation(provider="openai", payload_json=state.model_dump_json())
        category = ("refusal" if refused else "output_incomplete" if status == "incomplete"
                    else "response_failed" if status == "failed" else None)
        return ModelReply(**base, status="refused" if refused else status,
            tool_calls=tuple(calls), visible_text="\n".join(texts), continuation=continuation,
            error_category=category)
    except (ValueError, TypeError, KeyError, AttributeError):
        # A billed but unusable response is still a receipt. Never silently
        # truncate calls/history/text to fit the protocol or execute a subset.
        return ModelReply(**base, status="failed", error_category="invalid_response")


class OpenAIProvider:
    """Async provider with no hidden retries. Inject an SDK client for testing.

    No client/key is included in records or repr. Caller-provided capabilities
    must be verified for the chosen model before the paid pilot (Task 12).
    """
    def __init__(self, *, capabilities: ProviderCapabilities, client=None,
                 api_key: str | None = None, timeout_seconds: float = 30.0):
        self.capabilities = ProviderCapabilities.model_validate(capabilities)
        if capabilities.provider != "openai" or not capabilities.supports_stateless_continuation:
            raise ValueError("incompatible OpenAI capabilities")
        if not capabilities.supports_tools or not capabilities.supports_strict_tools:
            raise ValueError("strict function calling is required")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        if client is not None and api_key is not None:
            raise ValueError("supply a client or a key, not both")
        from openai import AsyncOpenAI, __version__
        if __version__ != SDK_VERSION:
            raise RuntimeError("install the pinned scrabble-harness[openai] dependency")
        if client is not None and not isinstance(client, AsyncOpenAI):
            raise TypeError("client must be an AsyncOpenAI instance")
        self._owned = client is None
        self._client = client or AsyncOpenAI(api_key=api_key, max_retries=0, timeout=timeout_seconds)
        self._timeout = float(timeout_seconds)
        self._closed = False

    async def complete(self, request: ModelRequest) -> ModelReply:
        request = ModelRequest.model_validate(request)
        if self._closed or request.capabilities != self.capabilities:
            raise ValueError("provider is closed or capabilities differ")
        payload = serialize_request(request)
        timeout = min(self._timeout, request.remaining_limits.max_elapsed_seconds)
        try:
            async with asyncio.timeout(timeout):
                response = await self._client.with_options(max_retries=0, timeout=timeout).responses.create(
                    **payload, extra_headers={"X-Client-Request-Id": request.attempt_id})
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderTransportError("OpenAI attempt failed; admission and retries belong to the harness") from None
        return normalize_response(request, response.model_dump(mode="json", exclude_none=True, by_alias=True),
                                  request_id=getattr(response, "_request_id", None), payload=payload)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owned:
                await self._client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()
