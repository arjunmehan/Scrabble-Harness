import pytest
from pydantic import ValidationError

from conftest import replace_validated
from scrabble_harness.domain import (
    AgentDecision, AnalysisLimits, AnalysisResult, CandidateConfig,
    CandidateEvaluation, EngineConfig, LexiconConfig, MetricDefinition,
    ModelSettings, RulesetConfig, RunConfig, RunLimits, RunSeeds, WorkUsed,
)


# These tests validate result schemas; pool binding is covered by decision tests.
OBSERVATION_ID = "obs_v1_" + "a" * 64
CANDIDATE_ID = "cand_v1_" + "b" * 64

def analysis_result(**updates):
    data = dict(
        analysis_id="analysis", observation_id=OBSERVATION_ID,
        values=(CandidateEvaluation(candidate_id=CANDIDATE_ID, value=2.5, standard_error=0.2),),
        metric_definition=MetricDefinition(
            name="fixture-equity", unit="points", description="Fixture estimate only",
            value_kind="estimate",
        ),
        work_used=WorkUsed(unit="fixture-sample", definition="One fixture sample", count=20),
        elapsed_time_seconds=0.2, seed=42, completion_status="complete",
    )
    data.update(updates)
    return AnalysisResult(**data)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), "2.5"])
def test_evaluations_reject_nonfinite_values_and_string_coercion(value):
    with pytest.raises(ValidationError):
        CandidateEvaluation(candidate_id=CANDIDATE_ID, value=value)


def test_complete_estimates_and_incomplete_results_round_trip():
    result = analysis_result()
    assert AnalysisResult.model_validate_json(result.model_dump_json()) == result
    for status in ("work_limit", "timeout", "cancelled", "failed"):
        partial = analysis_result(completion_status=status, values=())
        assert partial.completion_status == status
        assert AnalysisResult.model_validate_json(partial.model_dump_json()) == partial
    with pytest.raises(ValidationError, match="must contain"):
        analysis_result(values=())


def test_interrupted_results_cannot_claim_exact_values():
    exact = MetricDefinition(
        name="final-spread", unit="points", description="Solved final spread",
        value_kind="exact",
    )
    with pytest.raises(ValidationError, match="incomplete"):
        analysis_result(metric_definition=exact, completion_status="timeout")
    with pytest.raises(ValidationError, match="standard errors"):
        analysis_result(metric_definition=exact)
    solved = analysis_result(
        metric_definition=exact, values=(CandidateEvaluation(candidate_id=CANDIDATE_ID, value=5.0),),
    )
    assert solved.metric_definition.value_kind == "exact"


def test_duplicate_candidate_values_and_evidence_are_rejected():
    value = CandidateEvaluation(candidate_id=CANDIDATE_ID, value=2.0)
    with pytest.raises(ValidationError, match="duplicate candidate"):
        analysis_result(values=(value, value))
    with pytest.raises(ValidationError, match="unique"):
        AgentDecision(
            observation_id=OBSERVATION_ID, candidate_id=CANDIDATE_ID, workflow="standard",
            evidence_ids=("analysis", "analysis"),
        )


def test_decision_round_trip_preserves_evidence_and_fallback():
    decision = AgentDecision(
        observation_id=OBSERVATION_ID, candidate_id=CANDIDATE_ID, workflow="standard",
        evidence_ids=("analysis",), brief_note="Selected from the available estimates.",
        fallback_reason="Model call limit reached",
    )
    assert AgentDecision.model_validate_json(decision.model_dump_json()) == decision


@pytest.mark.parametrize("updates", [
    {"max_work_units": 0}, {"max_work_units": True},
    {"max_elapsed_seconds": 0.0}, {"max_elapsed_seconds": float("inf")},
])
def test_analysis_limits_are_positive_and_strict(updates):
    data = dict(max_work_units=20, work_unit="fixture-sample", max_elapsed_seconds=1.0)
    data.update(updates)
    with pytest.raises(ValidationError):
        AnalysisLimits(**data)


def test_run_config_round_trip_records_all_reproducibility_inputs():
    config = RunConfig(
        model=ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=512),
        engine=EngineConfig(engine_commit="a" * 40),
        lexicon=LexiconConfig(lexicon_id="fixture-v1", sha256="b" * 64),
        ruleset=RulesetConfig(ruleset_id="english-test-v1", scoreless_turn_limit=6),
        policy_version="standard-v1", candidate_config=CandidateConfig(),
        seeds=RunSeeds(game=1, analysis=2, candidate_order=3, trial=4),
        limits=RunLimits(
            max_api_cost_microusd=100_000_000, max_model_calls=3,
            max_engine_work_units=1000, engine_work_unit="fixture-sample",
            max_elapsed_seconds=30.0,
        ),
    )
    assert RunConfig.model_validate_json(config.model_dump_json()) == config
    assert config.limits.max_api_cost_microusd == 100 * 1_000_000
    with pytest.raises(ValidationError):
        replace_validated(config.limits, max_api_cost_microusd=100.5)
    with pytest.raises(ValidationError, match="Extra inputs"):
        replace_validated(config.model, api_key="secret")


def test_build_and_lexicon_identities_require_full_hashes():
    with pytest.raises(ValidationError):
        EngineConfig(engine_commit="main")
    with pytest.raises(ValidationError):
        LexiconConfig(lexicon_id="fixture", sha256="short")


def test_displayed_candidates_cannot_exceed_pool():
    with pytest.raises(ValidationError, match="cannot exceed"):
        CandidateConfig(pool_size=2, display_count=3)
