"""Provider-neutral request/reply contracts, with no SDK or transport dependency.

Tool-call argument text deliberately remains unparsed: invalid model actions
must be retained alongside billable usage, then checked by the harness.
"""

import json
import math
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_serializer, model_validator

from ..domain import ModelSettings
from ..domain._base import DomainModel, Identifier, NonNegativeInt, ObservationId, PositiveInt
from ..harness.contracts import DecisionLimits, DecisionView, EvidenceView

ToolName = Literal["select_move", "compare_moves"]


def _json_container(text: str, max_bytes: int, *, object_only: bool = False) -> None:
    if len(text.encode("utf-8")) > max_bytes:
        raise ValueError("provider JSON exceeds its byte ceiling")

    def finite_number(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("provider JSON cannot contain nonfinite numbers")
        return number

    def invalid_constant(value: str):
        raise ValueError("provider JSON cannot contain nonfinite constants")

    try:
        parsed = json.loads(text, parse_float=finite_number, parse_constant=invalid_constant)
    except RecursionError as exc:
        raise ValueError("provider JSON nesting exceeds the parser limit") from exc
    if not isinstance(parsed, dict if object_only else (dict, list)):
        raise ValueError("provider JSON must contain an object" if object_only else "provider JSON must contain an object or array")


class ProviderCapabilities(DomainModel):
    provider: Identifier
    model: Identifier
    max_output_tokens: PositiveInt
    supports_tools: bool = True
    supports_strict_tools: bool = True
    supports_temperature: bool = False
    supported_reasoning_efforts: tuple[Identifier, ...] = ()
    supports_stateless_continuation: bool = True

    @model_validator(mode="after")
    def distinct_reasoning_settings(self) -> Self:
        if len(set(self.supported_reasoning_efforts)) != len(self.supported_reasoning_efforts):
            raise ValueError("supported reasoning settings must be unique")
        return self


class TokenUsage(DomainModel):
    """Normalized input includes cache reads/writes; output includes reasoning tokens.

    None means unknown, never zero. Retain vendor fields in raw_usage_json.
    These are usage facts; rates and billing reconciliation are separate.
    """
    input_tokens: NonNegativeInt | None = None
    cached_input_tokens: NonNegativeInt | None = None
    cache_write_input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    reasoning_output_tokens: NonNegativeInt | None = None
    total_tokens: NonNegativeInt | None = None
    raw_usage_json: Annotated[str, Field(max_length=65_536)] | None = None

    @model_serializer(mode="wrap")
    def compatible_v1_defaults(self, handler):
        # Keep existing manifest/request/body hashes stable when new optional
        # accounting fields are unused. Non-default values remain identity-bearing.
        data = handler(self)
        if data.get('cache_write_input_tokens') == None:
            data.pop('cache_write_input_tokens', None)
        return data

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if all(value is None for value in (self.input_tokens, self.cached_input_tokens, self.cache_write_input_tokens, self.output_tokens,
                                          self.reasoning_output_tokens, self.total_tokens, self.raw_usage_json)):
            raise ValueError("use usage=None when no usage was reported")
        if self.cached_input_tokens is not None and self.input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input cannot exceed total input")
        cache_total = (self.cached_input_tokens or 0) + (self.cache_write_input_tokens or 0)
        if self.input_tokens is not None and cache_total > self.input_tokens:
            raise ValueError("cache reads and writes cannot exceed total input")
        if self.reasoning_output_tokens is not None and self.output_tokens is not None and self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("reasoning output cannot exceed total output")
        if self.total_tokens is not None and self.input_tokens is not None and self.output_tokens is not None:
            if self.total_tokens != self.input_tokens + self.output_tokens:
                raise ValueError("normalized total must equal input plus output")
        if self.raw_usage_json is not None:
            _json_container(self.raw_usage_json, 65_536, object_only=True)
        return self


class ProviderContinuation(DomainModel):
    """Opaque adapter-owned continuation. Persist privately, never display it.

    This is not a position-input escape hatch: only a trusted provider adapter
    may create/consume it. Structured positions belong exclusively in DecisionView.
    """
    provider: Identifier
    payload_json: Annotated[str, Field(max_length=1024 * 1024)]

    @model_validator(mode="after")
    def bounded_json(self) -> Self:
        _json_container(self.payload_json, 1024 * 1024)
        return self


class ProviderToolCall(DomainModel):
    call_id: Identifier
    name: Identifier
    arguments_json: Annotated[str, Field(max_length=65_536)]


class ModelRequest(DomainModel):
    """Structured position inputs cannot represent arbiter state or a checkpoint.

    build_model_request stores the complete rendered position in instructions;
    view/evidence also retain structured public inputs for host auditing. An
    adapter must not serialize those again as an unbounded second prompt body.
    max_request_bytes bounds this normalized record; a transport adapter must
    also bound its final SDK payload. Remaining limits are an admission snapshot,
    not a runtime ledger implementation.
    """
    attempt_id: Identifier
    model: ModelSettings
    capabilities: ProviderCapabilities
    view: DecisionView
    remaining_limits: DecisionLimits
    instructions: Annotated[str, Field(min_length=1, max_length=16_384)]
    prompt_version: Identifier
    tool_schema_version: Identifier
    tools: Annotated[tuple[ToolName, ...], Field(min_length=1, max_length=2)] = ("select_move", "compare_moves")
    force_selection: bool = False
    evidence: Annotated[tuple[EvidenceView, ...], Field(max_length=1000)] = ()
    continuation: ProviderContinuation | None = None
    max_request_bytes: Annotated[int, Field(ge=1024, le=16 * 1024 * 1024)] = 65_536

    @model_validator(mode="after")
    def public_scoped_request(self) -> Self:
        capabilities, model = self.capabilities, self.model
        if (capabilities.provider, capabilities.model) != (model.provider, model.model):
            raise ValueError("capabilities do not match the requested provider/model")
        if not capabilities.supports_tools or not capabilities.supports_strict_tools:
            raise ValueError("the first harness requires strict function tools")
        if model.max_output_tokens > capabilities.max_output_tokens:
            raise ValueError("output cap exceeds model capability")
        if model.temperature is not None and not capabilities.supports_temperature:
            raise ValueError("model does not support the configured temperature")
        if model.reasoning_effort is not None and model.reasoning_effort not in capabilities.supported_reasoning_efforts:
            raise ValueError("model does not support the configured reasoning effort")
        if len(set(self.tools)) != len(self.tools) or "select_move" not in self.tools:
            raise ValueError("request tools must be unique and include selection")
        if self.force_selection and self.tools != ("select_move",):
            raise ValueError("final selection requests cannot expose comparison")
        if self.remaining_limits.max_model_attempts < 1:
            raise ValueError("request has no remaining model-attempt slot")
        if "compare_moves" in self.tools:
            if self.remaining_limits.max_model_attempts < 2:
                raise ValueError("comparison requires a subsequent selection-attempt slot")
            observation = self.view.observation
            if observation.bag_size < 7 or observation.opponent_rack_size != 7 or len(self.view.candidates) < 2:
                raise ValueError("comparison is unavailable in this public position")
            if self.remaining_limits.max_tool_steps < 1 or self.remaining_limits.max_engine_work_units < 2:
                raise ValueError("comparison has no remaining tool/work allowance")
        if self.continuation is not None:
            if self.continuation.provider != model.provider or not capabilities.supports_stateless_continuation:
                raise ValueError("continuation is incompatible with this provider")
        handles = {candidate.handle for candidate in self.view.candidates}
        evidence_ids = tuple(item.evidence_id for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("request evidence handles must be unique")
        for item in self.evidence:
            if item.decision_id != self.view.decision_id or item.observation_id != self.view.observation.observation_id:
                raise ValueError("request evidence belongs to another decision")
            if not set(item.requested_handles).issubset(handles):
                raise ValueError("request evidence contains invisible candidate handles")
        encoded = self.model_dump_json().encode("utf-8")
        if len(encoded) > self.max_request_bytes:
            raise ValueError("normalized model request exceeds its byte ceiling")
        return self


class ModelReply(DomainModel):
    """Can retain unusable actions and unknown usage without claiming a selection."""
    attempt_id: Identifier
    decision_id: Identifier
    observation_id: ObservationId
    provider: Identifier
    resolved_model: Identifier
    response_id: Identifier | None = None
    request_id: Identifier | None = None
    status: Literal["completed", "incomplete", "refused", "failed"]
    tool_calls: Annotated[tuple[ProviderToolCall, ...], Field(max_length=16)] = ()
    visible_text: Annotated[str, Field(max_length=32_768)] = ""
    usage: TokenUsage | None = None
    continuation: ProviderContinuation | None = None
    error_category: Identifier | None = None

    @model_validator(mode="after")
    def continuation_matches_provider(self) -> Self:
        if self.continuation is not None and self.continuation.provider != self.provider:
            raise ValueError("reply continuation belongs to another provider")
        return self


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelReply:
        """Perform one explicitly admitted attempt; no implicit transport retries."""
        ...
