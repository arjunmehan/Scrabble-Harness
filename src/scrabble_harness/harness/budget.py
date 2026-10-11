"""Pure price bounds and monotonic deadlines; no transport, SDK, or native calls.

Token bounds are attestations from trusted adapter code, not model arguments.
No universal characters-per-token heuristic is used. A byte bound requires an
explicit byte-level tokenizer/framing contract; opaque continuation requires a
verified complete-input count instead. All prices come from a frozen rate card.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, localcontext
import hashlib
import json
import math
import time
from typing import Annotated, Literal, Self, TYPE_CHECKING

from pydantic import Field, model_validator

from ..domain import canonical_bytes
from ..domain._base import DomainModel, Identifier, NonEmptyText, Sha256
from .contracts import RateCard
from .tool_schemas import tool_definitions

if TYPE_CHECKING:
    from ..providers.protocol import ModelRequest, TokenUsage

Count = Annotated[int, Field(ge=0, le=2**63 - 1)]


class BudgetError(RuntimeError):
    pass


class BudgetExceededError(BudgetError):
    def __init__(self, scope: str, resource: str):
        self.scope, self.resource = scope, resource
        super().__init__(f"{scope} budget cannot admit {resource}")


class BudgetDeadlineError(BudgetError):
    pass


class BudgetBoundError(BudgetError):
    pass


class BudgetStateError(BudgetError):
    pass


class InputTokenBound(DomainModel):
    """Documented bound for complete model input, including tools and continuation.

    Future selection bounds must cover the entire bounded follow-up request,
    including tool results and opaque continuation. They are never assumed from
    the size of the current prompt. Evidence describes the counter/contract.
    """
    provider: Identifier
    model: Identifier
    purpose: Literal["request", "final_selection"]
    method: Literal["verified_complete_input_v1", "text_utf8_ceiling_v1"]
    evidence: Annotated[NonEmptyText, Field(max_length=2048)]
    input_tokens: Count
    max_output_tokens: Annotated[int, Field(gt=0, le=2**63 - 1)]
    request_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def bound_identity(self) -> Self:
        if (self.purpose == "request") != (self.request_sha256 is not None):
            raise ValueError("only a concrete request bound has a request hash")
        return self


def verified_input_bound(request: ModelRequest, *, input_tokens: int, evidence: str) -> InputTokenBound:
    """Caller supplies an independently verified complete payload count/ceiling."""
    from ..providers.protocol import ModelRequest
    request = ModelRequest.model_validate(request)
    return InputTokenBound(provider=request.model.provider, model=request.model.model,
        purpose="request", method="verified_complete_input_v1", evidence=evidence,
        input_tokens=input_tokens, max_output_tokens=request.model.max_output_tokens,
        request_sha256=hashlib.sha256(canonical_bytes(request)).hexdigest())


def text_input_bound(request: ModelRequest, *, framing_tokens: int, tokenizer_evidence: str) -> InputTokenBound:
    """Ceiling for an attested text-only, byte-level tokenizer transport contract.

    Count the whole normalized record (including duplicate audit inputs) plus
    full strict tool definitions and caller-verified framing. This deliberately
    overcounts visible text. The adapter must verify that its wire input uses no
    unrepresented items or additional token expansion. This is not an automatic
    assertion that a particular provider/model satisfies that contract.
    """
    from ..providers.protocol import ModelRequest
    request = ModelRequest.model_validate(request)
    if type(framing_tokens) is not int or not 0 <= framing_tokens <= 2**63 - 1:
        raise BudgetBoundError("invalid framing allowance")
    if request.continuation is not None:
        raise BudgetBoundError("opaque continuation requires a verified complete-input bound")
    schemas = [tool.as_function_tool() for tool in tool_definitions(request.tools, version=request.tool_schema_version)]
    schema_bytes = len(json.dumps(schemas, ensure_ascii=True, separators=(",", ":")).encode())
    bound = verified_input_bound(request, input_tokens=len(request.model_dump_json().encode()) + schema_bytes + framing_tokens,
                                 evidence=tokenizer_evidence)
    return _replace(bound, method="text_utf8_ceiling_v1")


def _replace(record, **changes):
    return type(record).model_validate(record.model_dump(mode="python") | changes)


class CostQuote(DomainModel):
    bound: InputTokenBound
    rate_card_sha256: Sha256
    safety_margin_basis_points: Annotated[int, Field(ge=0, le=10000)] = 1000
    minimum_margin_microusd: Count = 1
    api_cost_microusd: Count


class CostEstimate(DomainModel):
    api_cost_microusd: Count
    method: Literal["reported_categories_v1", "category_upper_bound_v1"]


def _ceil_cost(input_terms: tuple[tuple[int, int], ...], output_tokens: int, output_rate: int,
               *, basis_points: int = 0, minimum_margin: int = 0) -> int:
    for count, rate in input_terms + ((output_tokens, output_rate),):
        if type(count) is not int or type(rate) is not int or not 0 <= count <= 2**63 - 1 or not 0 <= rate <= 2**63 - 1:
            raise BudgetBoundError("token count or rate exceeds supported integer range")
    with localcontext() as context:
        context.prec = 80
        base = sum((Decimal(count) * Decimal(rate) for count, rate in input_terms), Decimal(0))
        base = (base + Decimal(output_tokens) * Decimal(output_rate)) / Decimal(1_000_000)
        margin = max(base * Decimal(basis_points) / Decimal(10000), Decimal(minimum_margin)) if base else Decimal(0)
        amount = int((base + margin).to_integral_value(rounding=ROUND_CEILING))
    if amount > 2**63 - 1:
        raise BudgetBoundError("cost exceeds supported integer range")
    return amount


def quote_cost(bound: InputTokenBound, rates: RateCard, *, safety_margin_basis_points: int = 1000,
               minimum_margin_microusd: int = 1) -> CostQuote:
    bound, rates = InputTokenBound.model_validate(bound), RateCard.model_validate(rates)
    if (bound.provider, bound.model) != (rates.provider, rates.model):
        raise BudgetBoundError("rate card does not match bound provider/model")
    if rates.max_input_tokens_at_rate is not None and bound.input_tokens > rates.max_input_tokens_at_rate:
        raise BudgetBoundError("input bound exceeds this rate card's pricing band")
    if type(safety_margin_basis_points) is not int or not 0 <= safety_margin_basis_points <= 10000:
        raise BudgetBoundError("invalid safety margin")
    if type(minimum_margin_microusd) is not int or not 0 <= minimum_margin_microusd <= 2**63 - 1:
        raise BudgetBoundError("invalid minimum margin")
    # Cache writes can be MORE expensive than uncached input. Never reserve a discount.
    ceiling = max(rates.input_microusd_per_million_tokens,
                  rates.cached_input_microusd_per_million_tokens or 0,
                  rates.cache_write_microusd_per_million_tokens or 0)
    amount = _ceil_cost(((bound.input_tokens, ceiling),), bound.max_output_tokens,
        rates.output_microusd_per_million_tokens, basis_points=safety_margin_basis_points,
        minimum_margin=minimum_margin_microusd)
    return CostQuote(bound=bound, rate_card_sha256=hashlib.sha256(canonical_bytes(rates)).hexdigest(),
        safety_margin_basis_points=safety_margin_basis_points, minimum_margin_microusd=minimum_margin_microusd,
        api_cost_microusd=amount)


def estimate_usage(usage: TokenUsage | None, rates: RateCard) -> CostEstimate | None:
    """Unknown total input/output retains the reservation. Unknown categories use
    the highest possible remaining-category rate; discounts require reported facts.
    Reasoning tokens are already included in output, never charged twice.
    """
    from ..providers.protocol import TokenUsage
    rates = RateCard.model_validate(rates)
    if usage is None:
        return None
    usage = TokenUsage.model_validate(usage)
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    if rates.max_input_tokens_at_rate is not None and usage.input_tokens > rates.max_input_tokens_at_rate:
        raise BudgetBoundError("reported input lies outside the saved pricing band")
    known_reads, known_writes = usage.cached_input_tokens, usage.cache_write_input_tokens
    terms, remaining = [], usage.input_tokens
    unknown_rates = [rates.input_microusd_per_million_tokens]
    exact = True
    if known_reads is not None and rates.cached_input_microusd_per_million_tokens is not None:
        terms.append((known_reads, rates.cached_input_microusd_per_million_tokens))
        remaining -= known_reads
    else:
        if known_reads not in (None, 0) and rates.cached_input_microusd_per_million_tokens is None:
            return None  # A reported category needs a defensible saved rate.
        if known_reads is None:
            exact = False
        if rates.cached_input_microusd_per_million_tokens is not None:
            unknown_rates.append(rates.cached_input_microusd_per_million_tokens)
    if known_writes is not None and rates.cache_write_microusd_per_million_tokens is not None:
        terms.append((known_writes, rates.cache_write_microusd_per_million_tokens))
        remaining -= known_writes
    else:
        if known_writes not in (None, 0) or (known_writes is None and rates.cache_write_microusd_per_million_tokens is not None):
            exact = False
        if rates.cache_write_microusd_per_million_tokens is not None:
            unknown_rates.append(rates.cache_write_microusd_per_million_tokens)
        if known_writes not in (None, 0) and rates.cache_write_microusd_per_million_tokens is None:
            # A reported category with no known rate cannot be priced defensibly.
            return None
    terms.append((remaining, max(unknown_rates)))
    amount = _ceil_cost(tuple(terms), usage.output_tokens, rates.output_microusd_per_million_tokens)
    return CostEstimate(api_cost_microusd=amount,
        method="reported_categories_v1" if exact else "category_upper_bound_v1")


class SystemClock:
    def wall_ns(self) -> int:
        return time.time_ns()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@dataclass(frozen=True)
class Deadline:
    decision_id: str
    expires_monotonic_ns: int
    clock: object
    finish_reserve_seconds: float

    def remaining_seconds(self, *, nonterminal: bool = False) -> float:
        remaining = (self.expires_monotonic_ns - self.clock.monotonic_ns()) / 1_000_000_000
        if nonterminal:
            remaining -= self.finish_reserve_seconds
        return max(0.0, remaining)

    def timeout(self, requested_seconds: float, *, nonterminal: bool = False) -> float:
        if isinstance(requested_seconds, bool) or not isinstance(requested_seconds, (int, float)) or not math.isfinite(requested_seconds) or requested_seconds <= 0:
            raise BudgetDeadlineError("timeout must be positive and finite")
        remaining = self.remaining_seconds(nonterminal=nonterminal)
        if remaining <= 0:
            raise BudgetDeadlineError("decision has no dispatch time left")
        return min(requested_seconds, remaining)


def duration_ns(seconds: float) -> int:
    with localcontext() as context:
        context.prec = 80
        result = int(Decimal(str(seconds)) * Decimal(1_000_000_000))
    if not 0 < result <= 2**63 - 1:
        raise BudgetBoundError("elapsed cap is outside supported nanosecond range")
    return result
