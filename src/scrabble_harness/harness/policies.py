"""Packaged fixed workflows and deterministic, host-only fallback selection."""

from importlib.resources import files
from typing import Literal

from pydantic import TypeAdapter, model_validator

from ..domain import AgentDecision
from ..domain._base import DomainModel
from .context import DecisionContext
from .contracts import HarnessConfig

_CEILINGS = {"fast": (1, 0, 0), "standard": (2, 1, 300), "deep": (3, 2, 600)}


def load_workflow(name: Literal["fast", "standard", "deep"], *,
                  fallback_policy: Literal["static", "latest_comparison"] = "static") -> HarnessConfig:
    """Load a frozen preset; only the explicit fallback policy can be changed.

    Attach this configuration to the execution manifest before creating a run.
    Preset ceilings remain subject to shared campaign/run admission and time.
    """
    name = TypeAdapter(Literal["fast", "standard", "deep"]).validate_python(name, strict=True)
    fallback_policy = TypeAdapter(Literal["static", "latest_comparison"]).validate_python(fallback_policy, strict=True)
    config = HarnessConfig.model_validate_json(files("scrabble_harness.harness").joinpath("presets", name + ".json").read_text())
    limits = config.decision_limits
    if (config.workflow != name or config.fallback_policy != "static"
            or (limits.max_model_attempts, limits.max_tool_steps, limits.max_engine_work_units) != _CEILINGS[name]):
        raise ValueError("packaged workflow does not match its fixed ceilings")
    return HarnessConfig.model_validate(config.model_dump(mode="python") | {"fallback_policy": fallback_policy})


class FallbackSelection(DomainModel):
    decision: AgentDecision
    origin: Literal["static", "latest_comparison"]

    @model_validator(mode="after")
    def explicit_fallback(self):
        if self.decision.fallback_reason is None:
            raise ValueError("fallback selection requires a reason")
        if (self.origin == "static" and self.decision.evidence_ids
                or self.origin == "latest_comparison" and len(self.decision.evidence_ids) != 1):
            raise ValueError("fallback citations disagree with origin")
        return self


def select_fallback(context: DecisionContext, reason: str) -> FallbackSelection:
    """Use the newest usable batch, or the prepared and previewed static move.

    Usable means all requested candidates have estimates and the status is
    complete, work_limit, or timeout. Partial/failed/cancelled batches are skipped.
    Never mix estimates between batches. Exact ties use canonical candidate IDs,
    so presentation/request ordering cannot decide a tie. No engine work occurs.
    """
    context = DecisionContext.model_validate(context)
    if context.config.fallback_policy == "latest_comparison":
        for evidence in reversed(context.evidence):
            result = evidence.result
            if (result.completion_status not in {"complete", "work_limit", "timeout"}
                    or {v.candidate_id for v in result.values} != {b.candidate_id for b in evidence.requested}):
                continue
            best = min(result.values, key=lambda value: (-value.value, value.candidate_id))
            return FallbackSelection(origin="latest_comparison", decision=AgentDecision(
                observation_id=context.observation.observation_id, candidate_id=best.candidate_id,
                workflow=context.config.workflow, evidence_ids=(evidence.evidence_id,),
                fallback_reason=reason))
    return FallbackSelection(origin="static", decision=context.fallback_decision(reason))
