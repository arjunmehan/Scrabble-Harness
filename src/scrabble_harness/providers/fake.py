"""Scripted ModelProvider for offline decisions and failure injection.

One accepted request consumes one explicitly named step before any await. No
automatic retry, tool execution, token estimation, or paid transport is hidden
here. Requests and opaque continuation in the inspection log remain private.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import hashlib
import json
import threading
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ..domain import canonical_bytes
from ..domain._base import DomainModel, Identifier, NonNegativeInt, NonNegativeValue, Sha256
from ..harness.contracts import CompareAction, SelectAction
from .protocol import (
    ModelReply, ModelRequest, ProviderCapabilities, ProviderContinuation,
    ProviderToolCall, TokenUsage,
)


class ScriptError(RuntimeError):
    """The offline script does not match the requested scenario."""


class ScriptMismatchError(ScriptError):
    pass


class ScriptExhaustedError(ScriptError):
    pass


class ScriptedReply(DomainModel):
    """Reply contents; complete() binds scope and provider from the request.

    Usage defaults to unknown. Raw calls may contain malformed JSON, unknown
    tools/handles, repeated call IDs, or multiple actions for downstream checks.
    """
    kind: Literal["reply"] = "reply"
    response_id: Identifier
    request_id: Identifier
    resolved_model: Identifier | None = None
    status: Literal["completed", "incomplete", "refused", "failed"] = "completed"
    tool_calls: Annotated[tuple[ProviderToolCall, ...], Field(max_length=16)] = ()
    visible_text: Annotated[str, Field(max_length=32_768)] = ""
    usage: TokenUsage | None = None
    continuation: ProviderContinuation | None = None
    error_category: Identifier | None = None


class ScriptedTransportFailure(DomainModel):
    """Failure without a response; billing/usage remains unknown to the caller."""
    kind: Literal["transport_error"] = "transport_error"
    error_category: Literal["connection_error", "timeout", "rate_limit", "server_error"]
    request_id: Identifier | None = None


class ScriptedTransportError(RuntimeError):
    def __init__(self, attempt_id: str, failure: ScriptedTransportFailure):
        self.attempt_id = attempt_id
        self.request_id = failure.request_id
        self.error_category = failure.error_category
        super().__init__(f"scripted transport error: {failure.error_category}")


ScriptedOutcome = Annotated[ScriptedReply | ScriptedTransportFailure, Field(discriminator="kind")]


class ScriptedStep(DomainModel):
    attempt_id: Identifier
    outcome: ScriptedOutcome
    delay_seconds: NonNegativeValue = 0.0
    expected_request_sha256: Sha256 | None = None


class ScriptedCall(DomainModel):
    """Immutable inspection snapshot, separate from authoritative run accounting."""
    script_index: NonNegativeInt
    request: ModelRequest
    state: Literal["pending", "replied", "transport_error", "cancelled", "interrupted"]
    reply: ModelReply | None = None
    failure: ScriptedTransportFailure | None = None

    @model_validator(mode="after")
    def outcome_matches_state(self) -> Self:
        if (self.state == "replied") != (self.reply is not None):
            raise ValueError("only a replied invocation contains a reply")
        if (self.state == "transport_error") != (self.failure is not None):
            raise ValueError("only a transport error invocation contains a failure")
        if self.reply is not None and (
            self.reply.attempt_id != self.request.attempt_id
            or self.reply.decision_id != self.request.view.decision_id
            or self.reply.observation_id != self.request.view.observation.observation_id
            or self.reply.provider != self.request.model.provider
        ):
            raise ValueError("scripted reply belongs to another request")
        return self


def selection_call(call_id: str, candidate_handle: str, *, evidence_ids: tuple[str, ...] = (),
                   brief_note: str = "") -> ProviderToolCall:
    """Build strict v1 arguments; handle visibility/evidence ownership stays downstream."""
    action = SelectAction(candidate_handle=candidate_handle, evidence_ids=evidence_ids, brief_note=brief_note)
    return ProviderToolCall(call_id=call_id, name="select_move", arguments_json=json.dumps(
        action.model_dump(mode="json", exclude={"schema_version", "kind"}),
        sort_keys=True, separators=(",", ":"), allow_nan=False))


def comparison_call(call_id: str, candidate_handles: tuple[str, ...], *, tier: str = "light") -> ProviderToolCall:
    """Build a comparison request, without running an engine or enforcing admission."""
    action = CompareAction(candidate_handles=candidate_handles, tier=tier)
    return ProviderToolCall(call_id=call_id, name="compare_moves", arguments_json=json.dumps(
        action.model_dump(mode="json", exclude={"schema_version", "kind"}),
        sort_keys=True, separators=(",", ":"), allow_nan=False))


class ScriptedProvider:
    """One immutable ordered script, owned by a single async event loop.

    Valid requests must match configured capabilities and the next attempt ID.
    Optional request hashes additionally pin tools, public inputs, and private
    continuation. Invalid requests consume nothing. Accepted requests consume
    their step even when delayed work is cancelled or interrupted.

    The injectable sleeper enables controlled barriers/virtual delays in tests.
    With the default asyncio.sleep, outer asyncio.timeout/wait_for cancellation
    propagates normally. Timeouts and budget admission belong to the caller.
    """

    def __init__(self, steps: Iterable[ScriptedStep], *, capabilities: ProviderCapabilities,
                 sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._capabilities = ProviderCapabilities.model_validate(capabilities)
        self._steps = tuple(ScriptedStep.model_validate(step) for step in steps)
        attempts = tuple(step.attempt_id for step in self._steps)
        responses = tuple(step.outcome.response_id for step in self._steps if isinstance(step.outcome, ScriptedReply))
        if len(set(attempts)) != len(attempts) or len(set(responses)) != len(responses):
            raise ScriptMismatchError("script attempt and response identities must be unique")
        if not callable(sleeper):
            raise TypeError("sleeper must be an async callable")
        for step in self._steps:
            if (isinstance(step.outcome, ScriptedReply) and step.outcome.continuation is not None
                    and (step.outcome.continuation.provider != self._capabilities.provider
                         or not self._capabilities.supports_stateless_continuation)):
                raise ScriptMismatchError("scripted continuation conflicts with provider capabilities")
        self._sleeper = sleeper
        self._next = 0
        self._calls: list[ScriptedCall] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.get_ident()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def remaining_steps(self) -> int:
        return len(self._steps) - self._next

    @property
    def calls(self) -> tuple[ScriptedCall, ...]:
        return tuple(self._calls)

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(call.request for call in self._calls)

    async def complete(self, request: ModelRequest) -> ModelReply:
        if threading.get_ident() != self._thread:
            raise ScriptMismatchError("use a fresh scripted provider in another thread")
        request = ModelRequest.model_validate(request)
        loop = asyncio.get_running_loop()
        if self._loop is not None and loop is not self._loop:
            raise ScriptMismatchError("use a fresh scripted provider in another event loop")
        if request.capabilities != self._capabilities:
            raise ScriptMismatchError("request capabilities differ from the scripted provider")
        if any(call.request.attempt_id == request.attempt_id for call in self._calls):
            raise ScriptMismatchError("attempt was already consumed; use an explicit new retry attempt")
        if self._next >= len(self._steps):
            raise ScriptExhaustedError("script has no remaining outcome")
        step = self._steps[self._next]
        if step.attempt_id != request.attempt_id:
            raise ScriptMismatchError("request does not match the next scripted attempt")
        if (step.expected_request_sha256 is not None
                and step.expected_request_sha256 != hashlib.sha256(canonical_bytes(request)).hexdigest()):
            raise ScriptMismatchError("request content differs from the scripted request hash")
        outcome = step.outcome
        reply = None
        if isinstance(outcome, ScriptedReply):
            # Validate all metadata before consuming a step or awaiting its delay.
            reply = ModelReply(attempt_id=request.attempt_id, decision_id=request.view.decision_id,
                observation_id=request.view.observation.observation_id, provider=request.model.provider,
                resolved_model=outcome.resolved_model or request.model.model,
                **outcome.model_dump(mode="python", exclude={"schema_version", "kind", "resolved_model"}))
        index = self._next
        self._loop = loop
        self._next += 1
        self._calls.append(ScriptedCall(script_index=index, request=request, state="pending"))
        try:
            if step.delay_seconds > 0:
                await self._sleeper(step.delay_seconds)
        except asyncio.CancelledError:
            self._calls[index] = ScriptedCall(script_index=index, request=request, state="cancelled")
            raise
        except BaseException:
            self._calls[index] = ScriptedCall(script_index=index, request=request, state="interrupted")
            raise
        if isinstance(outcome, ScriptedTransportFailure):
            self._calls[index] = ScriptedCall(script_index=index, request=request, state="transport_error", failure=outcome)
            raise ScriptedTransportError(request.attempt_id, outcome)
        assert reply is not None  # Exhaustive validated outcome union.
        self._calls[index] = ScriptedCall(script_index=index, request=request, state="replied", reply=reply)
        return reply
