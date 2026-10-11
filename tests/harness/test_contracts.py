import json

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import (
    EngineConfig, LexiconConfig, ModelSettings, RulesetConfig, RunConfig, RunLimits, RunSeeds,
)
from scrabble_harness.harness import (
    CompareAction, ComparisonTier, DecisionLimits, DecisionOutcome, DecisionUsage,
    DependencyVersion, ExecutionManifest, HarnessConfig, PresentationConfig, RateCard,
    ScopedAction, SelectAction,
)
from conftest import update


@pytest.mark.parametrize("record", [
    HarnessConfig(), DecisionLimits(), PresentationConfig(),
    SelectAction(candidate_handle="c01", evidence_ids=("e01",), brief_note="Keep a balanced leave"),
    CompareAction(candidate_handles=("c01", "c03"), tier="light"),
])
def test_contract_json_roundtrip_and_immutability(record):
    assert type(record).model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        record.schema_version = 2
    with pytest.raises(ValidationError):
        update(record, unknown_field="not allowed")
    with pytest.raises(ValidationError):
        update(record, schema_version=True)


@pytest.mark.parametrize("changes", [
    {"max_model_attempts": "2"}, {"max_tool_steps": True},
    {"max_api_cost_microusd": -1}, {"max_engine_work_units": 2**63},
    {"max_elapsed_seconds": float("inf")}, {"finish_reserve_seconds": 30.0},
])
def test_invalid_limits(changes):
    with pytest.raises(ValidationError):
        DecisionLimits(**changes)


@pytest.mark.parametrize("record,changes", [
    (SelectAction(candidate_handle="c01"), {"candidate_handle": "cand_v1_" + "a" * 64}),
    (SelectAction(candidate_handle="c01"), {"evidence_ids": ("e01", "e01")}),
    (SelectAction(candidate_handle="c01"), {"brief_note": "x" * 501}),
    (CompareAction(candidate_handles=("c01", "c02"), tier="light"), {"candidate_handles": ("c01", "c01")}),
    (CompareAction(candidate_handles=("c01", "c02"), tier="light"), {"candidate_handles": ("c01",)}),
    (CompareAction(candidate_handles=("c01", "c02"), tier="light"), {"tier": "endgame"}),
    (HarnessConfig(), {"max_repair_rounds": 2}),
    (HarnessConfig(), {"comparison_tiers": (ComparisonTier(name="light", max_work_units=60),) * 2}),
])
def test_invalid_actions_configuration(record, changes):
    with pytest.raises(ValidationError):
        update(record, **changes)


def test_scoped_discriminated_actions(context):
    payload = {"decision_id": context.decision_id, "observation_id": context.observation.observation_id,
               "action": {"kind": "compare", "candidate_handles": ["c01", "c02"], "tier": "light"}}
    assert isinstance(ScopedAction.model_validate_json(json.dumps(payload)).action, CompareAction)
    payload["action"]["kind"] = "commit_game"
    with pytest.raises(ValidationError):
        ScopedAction.model_validate_json(json.dumps(payload))


def manifest_fields():
    harness = HarnessConfig()
    run = RunConfig(model=ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024),
        engine=EngineConfig(engine_commit="a" * 40),
        lexicon=LexiconConfig(lexicon_id="fixture", sha256="b" * 64),
        ruleset=RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6),
        policy_version=harness.workflow, candidate_config=harness.candidate_config,
        seeds=RunSeeds(game=42, analysis=73, candidate_order=123, trial=1),
        limits=RunLimits(max_api_cost_microusd=100_000_000, max_model_calls=100,
            max_engine_work_units=10000, engine_work_unit="candidate_rollout", max_elapsed_seconds=3600.0))
    return dict(run_config=run, harness_config=harness, engine_configuration_id="engine-v1",
        source_sha256="c" * 64, trial_id="trial-a",
        dependencies=(DependencyVersion(name="python", version="3.13.5"),
                      DependencyVersion(name="pydantic", version="2.10.3")),
        rate_card=RateCard(provider="fixture", model="fixture-model",
            input_microusd_per_million_tokens=100000, output_microusd_per_million_tokens=200000,
            source_url="https://example.com/synthetic-test-rates", retrieved_on="2026-10-07",
            estimation_method="published-token-rates-v1"))


def test_manifest_identity_roundtrip_tampering_and_dependency_order():
    fields = manifest_fields()
    first = ExecutionManifest.create(**fields)
    assert ExecutionManifest.model_validate_json(first.model_dump_json()) == first
    reordered = ExecutionManifest.create(**(fields | {"dependencies": tuple(reversed(fields["dependencies"]))}))
    assert reordered.manifest_id == first.manifest_id
    assert ExecutionManifest.create(**(fields | {"trial_id": "trial-b"})).manifest_id != first.manifest_id
    with pytest.raises(ValidationError, match="identity"):
        update(first, source_sha256="d" * 64)


@pytest.mark.parametrize("mutation", ["workflow", "rate_model", "caps", "analysis_seed", "duplicate_dependencies"])
def test_inconsistent_manifest_rejected(mutation):
    fields = manifest_fields()
    if mutation == "workflow":
        fields["harness_config"] = update(fields["harness_config"], workflow="deep")
    elif mutation == "rate_model":
        fields["rate_card"] = update(fields["rate_card"], model="other-model")
    elif mutation == "caps":
        run = fields["run_config"]
        fields["run_config"] = update(run, limits=update(run.limits, max_model_calls=1))
    elif mutation == "analysis_seed":
        run = fields["run_config"]
        fields["run_config"] = update(run, seeds=update(run.seeds, analysis=2**64))
    else:
        fields["dependencies"] = fields["dependencies"] * 2
    with pytest.raises(ValidationError):
        ExecutionManifest.create(**fields)


def test_outcome_links_issued_work_to_visible_selection(evidenced_context):
    ctx = evidenced_context
    decision = ctx.select(ScopedAction(decision_id=ctx.decision_id, observation_id=ctx.observation.observation_id,
        action=SelectAction(candidate_handle="c01", evidence_ids=("e01",))))
    outcome = DecisionOutcome(decision_id=ctx.decision_id, observation_id=ctx.observation.observation_id,
        status="selected", decision=decision, trusted_pool=ctx.full_pool, visible_bindings=ctx.visible_bindings,
        evidence=ctx.evidence, usage=DecisionUsage(confirmed_engine_work_units=12), terminal_reason="selected")
    assert DecisionOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    for changes in ({"usage": DecisionUsage()}, {"evidence": ()}, {"visible_bindings": ()},
                    {"status": "aborted"}, {"status": "fallback"}):
        with pytest.raises(ValidationError):
            update(outcome, **changes)
    duplicate = update(ctx.evidence[0], evidence_id="e02")
    with pytest.raises(ValidationError, match="analysis twice"):
        update(outcome, evidence=ctx.evidence + (duplicate,), usage=DecisionUsage(confirmed_engine_work_units=24))
    assert DecisionOutcome(decision_id="aborted-a", observation_id=ctx.observation.observation_id,
                           status="aborted", terminal_reason="preparation_failed").decision is None
