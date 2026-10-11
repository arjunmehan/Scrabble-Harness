"""Offline single-position acceptance path; synthetic billing, no API transport."""

from __future__ import annotations

import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter

from .domain import (
    Candidate, EngineConfig, GameConfig, LexiconConfig, ModelSettings, RulesetConfig,
    RunConfig, RunLimits, RunSeeds, canonical_bytes, validate_decision,
)
from .domain._base import DomainModel, Identifier, NonNegativeInt
from .harness import DecisionLoop, DecisionOutcome, DependencyVersion, ExecutionManifest, HarnessConfig, RateCard, load_workflow
from .harness.budget import InputTokenBound, text_input_bound
from .harness.tool_schemas import tool_definitions
from .providers import (
    ProviderCapabilities, ProviderToolCall, ScriptedProvider, ScriptedReply,
    ScriptedStep, ScriptedTransportFailure, TokenUsage, comparison_call, selection_call,
)
from .runs import BudgetLedger, BudgetTotals, RunStore, StoreNotFoundError

Scenario = Literal["compare-select", "select", "refusal", "malformed", "forged-evidence", "missing-usage", "transport-failure", "timeout"]
_CAMPAIGN_LIMITS = RunLimits(max_api_cost_microusd=1_000_000, max_model_calls=100,
    max_engine_work_units=10000, engine_work_unit="candidate_rollout", max_elapsed_seconds=3600.0)


class OfflineDecisionReport(DomainModel):
    purpose: Literal["offline acceptance; synthetic billing; no paid API calls"] = "offline acceptance; synthetic billing; no paid API calls"
    campaign_id: Identifier
    run_id: Identifier
    manifest_id: Identifier
    scenario: Scenario
    lexicon_id: Identifier
    outcome: DecisionOutcome
    selected_candidate: Candidate | None
    resources: BudgetTotals
    selection_validated: bool
    game_unchanged: bool
    paid_provider_requests: Literal[0] = 0


def offline_config(workflow="standard", *, fallback_policy="static", **reductions) -> HarnessConfig:
    """Acceptance overrides may reduce fixed preset ceilings, never increase them."""
    config = load_workflow(workflow, fallback_policy=fallback_policy)
    limits = config.decision_limits
    allowed = {"max_api_cost_microusd", "max_model_attempts", "max_tool_steps", "max_engine_work_units", "max_elapsed_seconds"}
    if set(reductions) - allowed:
        raise ValueError("unknown offline limit")
    values = limits.model_dump(mode="python")
    for key, value in reductions.items():
        if value is None:
            continue
        if isinstance(value, bool) or value > getattr(limits, key):
            raise ValueError("offline overrides may only reduce preset limits")
        values[key] = value
    seconds = values["max_elapsed_seconds"]
    values["finish_reserve_seconds"] = min(limits.finish_reserve_seconds, seconds / 2)
    clipped = type(limits).model_validate(values)
    tiers = tuple(type(t).model_validate(t.model_dump(mode="python") | {
        "max_elapsed_seconds": min(t.max_elapsed_seconds, clipped.max_elapsed_seconds)}) for t in config.comparison_tiers)
    return HarnessConfig.model_validate(config.model_dump(mode="python") | {"decision_limits": clipped, "comparison_tiers": tiers})


def scripted_steps(decision_id: str, config: HarnessConfig, scenario: Scenario) -> tuple[ScriptedStep, ...]:
    scenario = TypeAdapter(Scenario).validate_python(scenario, strict=True)
    steps = []
    limits = config.decision_limits
    comparisons = min(limits.max_tool_steps, max(0, limits.max_model_attempts - 1),
        2 if limits.max_engine_work_units >= 302 else 1 if limits.max_engine_work_units >= 2 else 0) if scenario in {"compare-select", "missing-usage"} else 0
    for index in range(1, limits.max_model_attempts + 1):
        call = comparison_call(f"compare-{index}", ("c01", "c02") if index == 1 else ("c02", "c03"), tier="standard") if index <= comparisons else selection_call(
            f"select-{index}", "c01" if index == 1 else "c02", evidence_ids=(f"e{comparisons:02d}",) if comparisons else ())
        status = "refused" if scenario == "refusal" else "completed"
        calls = () if status == "refused" else (call,)
        if scenario == "malformed" and index == 1:
            calls = (ProviderToolCall(call_id="malformed-1", name="select_move", arguments_json="{invalid"),)
        if scenario == "forged-evidence" and index == 1:
            calls = (selection_call("forged-1", "c01", evidence_ids=("e99",)),)
        attempt_id = "attempt_" + hashlib.sha256(f"{decision_id}:{index}".encode()).hexdigest()
        outcome = ScriptedTransportFailure(error_category="connection_error") if scenario == "transport-failure" else ScriptedReply(
            response_id=f"response-{index}", request_id=f"request-{index}", status=status,
            tool_calls=calls, usage=None if scenario == "missing-usage" and index == 1 else TokenUsage(input_tokens=10, output_tokens=5))
        steps.append(ScriptedStep(attempt_id=attempt_id, outcome=outcome,
            delay_seconds=60.0 if scenario == "timeout" and index == 1 else 0.0))
    return tuple(steps)


def source_fingerprint() -> str:
    """Hash packaged code/configuration bytes rather than a placeholder revision."""
    from importlib.resources import files
    digest = hashlib.sha256()
    def visit(directory, prefix=""):
        for item in sorted(directory.iterdir(), key=lambda item: item.name):
            name = prefix + item.name
            if item.is_dir() and item.name != "__pycache__":
                visit(item, name + "/")
            elif item.is_file() and item.name.endswith((".py", ".json", ".sql")):
                body = item.read_bytes()
                digest.update(name.encode() + b"\0" + hashlib.sha256(body).digest())
    visit(files("scrabble_harness"))
    return digest.hexdigest()


async def decide_offline(*, manifest: Path, database: Path, config: HarnessConfig,
                         scenario: Scenario = "compare-select", campaign_id: str = "offline",
                         run_id: str | None = None, game_seed: int = 42, analysis_seed: int = 73,
                         candidate_seed: int = 123, model_timeout_seconds: float = 30.0) -> OfflineDecisionReport:
    from .engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
    from .domain._base import PositiveValue
    config = HarnessConfig.model_validate(config)
    campaign_id = TypeAdapter(Identifier, config={"strict": True}).validate_python(campaign_id)
    run_id = TypeAdapter(Identifier, config={"strict": True}).validate_python(run_id or "offline_" + uuid4().hex)
    seeds = [TypeAdapter(NonNegativeInt, config={"strict": True}).validate_python(seed) for seed in (game_seed, analysis_seed, candidate_seed)]
    timeout = TypeAdapter(PositiveValue, config={"strict": True}).validate_python(model_timeout_seconds)
    settings = MagpieSettings.from_manifest(manifest)
    decision_id = "decision_" + hashlib.sha256(run_id.encode()).hexdigest()
    model = ModelSettings(provider="scripted", model="offline-v1", max_output_tokens=1024)
    caps = ProviderCapabilities(provider=model.provider, model=model.model, max_output_tokens=1024)
    provider = ScriptedProvider(scripted_steps(decision_id, config, scenario), capabilities=caps)
    rules = RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6)
    limits = config.decision_limits
    run = RunConfig(model=model, engine=EngineConfig(engine_commit=settings.engine_commit),
        lexicon=LexiconConfig(lexicon_id=settings.lexicon_id, sha256=json.loads(Path(manifest).read_text())["lexicon_sha256"]),
        ruleset=rules, policy_version=config.workflow, candidate_config=config.candidate_config,
        seeds=RunSeeds(game=seeds[0], analysis=seeds[1], candidate_order=seeds[2], trial=0),
        limits=RunLimits(max_api_cost_microusd=limits.max_api_cost_microusd, max_model_calls=limits.max_model_attempts,
            max_engine_work_units=limits.max_engine_work_units, engine_work_unit="candidate_rollout", max_elapsed_seconds=limits.max_elapsed_seconds))
    execution = ExecutionManifest.create(run_config=run, harness_config=config, engine_configuration_id=settings.configuration_id,
        source_sha256=source_fingerprint(), trial_id="scenario_" + scenario,
        dependencies=(DependencyVersion(name="python", version=platform.python_version()), DependencyVersion(name="pydantic", version=version("pydantic"))),
        rate_card=RateCard(provider=model.provider, model=model.model, input_microusd_per_million_tokens=100000,
            output_microusd_per_million_tokens=200000, source_url="https://example.com/scrabble-harness/synthetic-offline-rates",
            retrieved_on="2026-10-11", estimation_method="synthetic-offline-v1"))
    schemas = json.dumps([t.as_function_tool() for t in tool_definitions()], ensure_ascii=True).encode()
    finish = InputTokenBound(provider=model.provider, model=model.model, purpose="final_selection", method="verified_complete_input_v1",
        input_tokens=config.presentation.max_request_bytes + len(schemas) + 1024, max_output_tokens=model.max_output_tokens,
        evidence="Offline text-only contract: entire normalized request byte ceiling, strict schemas and framing; no opaque continuation")
    with RunStore(database) as store:
        with store.transaction():
            try:
                store.get_run(run_id)
            except StoreNotFoundError:
                pass
            else:
                raise ValueError("run ID already exists; use a new ID, without replaying prior work")
            store.create_campaign(campaign_id, _CAMPAIGN_LIMITS)
            store.create_run(run_id, campaign_id, execution)
            store.set_run_status(run_id, "running")
        analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
        try:
            state = await arbiter.start_game(GameConfig(game_id="game_" + hashlib.sha256(run_id.encode()).hexdigest(),
                seed=seeds[0], lexicon_id=settings.lexicon_id, ruleset=rules))
            observation = await arbiter.observe(state.active_player)
            before = await arbiter.checkpoint()
            ledger = BudgetLedger(store)
            loop = DecisionLoop(provider, analysis, ledger, capabilities=caps, finish_bound=finish,
                input_bound=lambda request: text_input_bound(request, framing_tokens=1024,
                    tokenizer_evidence="Synthetic offline byte-level complete-request ceiling; no transport or continuation"),
                model_timeout_seconds=timeout)
            outcome = await loop.decide(run_id, decision_id, observation)
            candidate = None if outcome.decision is None else validate_decision(outcome.decision, state=state,
                observation=observation, candidates=outcome.trusted_pool, expected_revision=state.revision)
            unchanged = before == await arbiter.checkpoint()
            if not unchanged:
                raise RuntimeError("single-position decision changed the game")
            totals = ledger.totals(decision_id=decision_id)
            if any(r.state in {"reserved", "dispatched"} for r in store.reservations(run_id)):
                raise RuntimeError("decision left unreconciled dispatch allocations")
            if (outcome.usage.model_attempts != totals.charged.model_calls or outcome.usage.tool_steps != totals.charged.tool_steps
                    or outcome.usage.confirmed_engine_work_units != totals.settled.engine_work_units
                    or outcome.usage.unresolved_engine_work_units != totals.unresolved.engine_work_units
                    or outcome.usage.settled_api_cost_microusd != totals.settled.api_cost_microusd
                    or outcome.usage.unresolved_api_cost_microusd != totals.unresolved.api_cost_microusd
                    or totals.held.model_calls):
                raise RuntimeError("saved usage differs from resource ledger")
            store.check_integrity()
            store.set_run_status(run_id, "completed" if outcome.status != "aborted" else "failed")
            return OfflineDecisionReport(campaign_id=campaign_id, run_id=run_id, manifest_id=execution.manifest_id,
                scenario=scenario, lexicon_id=settings.lexicon_id, outcome=outcome, selected_candidate=candidate,
                resources=totals, selection_validated=candidate is not None, game_unchanged=unchanged)
        except BaseException as error:
            try:
                if store.get_run(run_id).status in {"created", "running"}:
                    import asyncio
                    store.set_run_status(run_id, "cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
            except Exception as cleanup_error:
                error.add_note(f"Run finalization also failed: {type(cleanup_error).__name__}")
            raise
        finally:
            try:
                await analysis.close()
            finally:
                await arbiter.close()
