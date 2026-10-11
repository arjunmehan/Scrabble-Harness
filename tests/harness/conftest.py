"""Synthetic contract data; native tests separately establish Scrabble legality."""

import asyncio

import pytest

from scrabble_harness.domain import (
    AnalysisLimits, AnalysisResult, Board, BoardTile, CandidateEvaluation,
    CandidateProvenance, ExchangeMove, MetricDefinition, MovePreview, PassMove,
    PlacementMove, WorkUsed, build_candidate,
)
from scrabble_harness.harness import (
    CompareAction, HarnessConfig, ScopedAction, prepare_decision_context,
)


def update(record, **changes):
    return type(record).model_validate(record.model_dump(mode="python") | changes)


class RecordingAnalysis:
    def __init__(self, observation, pool):
        self.observation, self.pool = observation, pool
        self.calls = []
        self.preview_override = None
        self.failure = None

    async def load_observation(self, observation):
        self.calls.append("load")
        if self.failure:
            raise self.failure
        assert observation == self.observation

    async def generate_candidates(self, configuration):
        self.calls.append(("generate", configuration))
        return self.pool

    async def preview(self, move):
        self.calls.append(("preview", move))
        if self.preview_override is not None:
            return self.preview_override
        candidate = next(candidate for candidate in self.pool if candidate.move == move)
        board = self.observation.board
        if isinstance(move, PlacementMove):
            board = Board(tiles=board.tiles + move.tiles)
        return MovePreview(observation_id=candidate.observation_id, move=move,
                           exact_score=candidate.exact_score, leave=candidate.leave,
                           board_after=board)


@pytest.fixture
def synthetic_pool(active_observation):
    rack = active_observation.own_rack
    provenance = CandidateProvenance(source="synthetic-contract-test", configuration_id="test-v1")
    pool = []
    for column in range(15):
        move = PlacementMove(tiles=(BoardTile(row=6, column=column,
                                              displayed_letter=rack[0], is_blank=False),))
        pool.append(build_candidate(active_observation, move, exact_score=2,
                                    leave=rack[1:], static_equity=float(100 - column), provenance=provenance))
    for index, tile in enumerate(rack):
        pool.append(build_candidate(active_observation, ExchangeMove(tiles=(tile,)), exact_score=0,
                                    leave=rack[:index] + rack[index + 1:],
                                    static_equity=float(-index), provenance=provenance))
    pool.append(build_candidate(active_observation, PassMove(), exact_score=0, leave=rack,
                                static_equity=-50.0, provenance=provenance))
    return tuple(pool)


@pytest.fixture
def recording_analysis(active_observation, synthetic_pool):
    return RecordingAnalysis(active_observation, synthetic_pool)


@pytest.fixture
def context(active_observation, recording_analysis):
    return asyncio.run(prepare_decision_context(active_observation, recording_analysis,
                       decision_id="decision-a", candidate_order_seed=123))


@pytest.fixture
def comparison(context):
    return ScopedAction(decision_id=context.decision_id,
        observation_id=context.observation.observation_id,
        action=CompareAction(candidate_handles=("c01", "c02"), tier="light"))


@pytest.fixture
def analysis_limits():
    return AnalysisLimits(max_work_units=12, work_unit="candidate_rollout", max_elapsed_seconds=4.0)


@pytest.fixture
def analysis_result(context, comparison):
    requested = context.resolve_comparison(comparison)
    return AnalysisResult(analysis_id="analysis-a", observation_id=context.observation.observation_id,
        values=tuple(CandidateEvaluation(candidate_id=candidate.candidate_id, value=float(index),
                                        standard_error=0.5) for index, candidate in enumerate(requested)),
        metric_definition=MetricDefinition(name="two-ply-equity", unit="points",
            description="Synthetic estimate for validation, not a strength result", value_kind="estimate"),
        work_used=WorkUsed(unit="candidate_rollout", definition="Total candidate rollouts", count=12),
        elapsed_time_seconds=0.2, seed=73, completion_status="work_limit")


@pytest.fixture
def evidenced_context(context, comparison, analysis_result, analysis_limits):
    return context.issue_evidence(comparison, analysis_result, limits=analysis_limits, seed=73)[0]
