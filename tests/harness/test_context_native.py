import asyncio
import os
from pathlib import Path

import pytest

from scrabble_harness.domain import (
    AnalysisLimits, ExchangeMove, GameConfig, PassMove, RulesetConfig, validate_decision,
)
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
from scrabble_harness.harness import CompareAction, ScopedAction, SelectAction, prepare_decision_context

pytestmark = pytest.mark.native
PROJECT = Path(__file__).parents[2]


class CountedAnalysis:
    def __init__(self, engine):
        self.engine = engine
        self.generated = 0
        self.previewed = 0

    async def load_observation(self, observation):
        await self.engine.load_observation(observation)

    async def generate_candidates(self, configuration):
        self.generated += 1
        return await self.engine.generate_candidates(configuration)

    async def preview(self, move):
        self.previewed += 1
        return await self.engine.preview(move)


def test_full_nwl_context_keeps_registry_for_comparison_and_authoritative_commit():
    explicit = os.environ.get("SCRABBLE_NWL_MANIFEST")
    path = Path(explicit) if explicit else PROJECT / ".native/nwl23/manifest.json"
    if not path.is_file():
        if explicit:
            pytest.fail("Configured SCRABBLE_NWL_MANIFEST does not exist")
        pytest.skip("Full NWL23 profile is not installed")

    async def scenario():
        settings = MagpieSettings.from_manifest(path)
        analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
        counted = CountedAnalysis(analysis)
        try:
            state = await arbiter.start_game(GameConfig(game_id="context-native", seed=42, lexicon_id=settings.lexicon_id,
                ruleset=RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6)))
            observation = await arbiter.observe(state.active_player)
            context = await prepare_decision_context(observation, counted, decision_id="native-a", candidate_order_seed=123)
            assert counted.generated == counted.previewed == 1
            assert 12 <= len(context.full_pool) <= 40 and len(context.visible_bindings) == 12
            assert any(isinstance(candidate.move, PassMove) for candidate in context.full_pool)
            assert any(isinstance(candidate.move, ExchangeMove) for candidate in context.full_pool)
            scope = dict(decision_id=context.decision_id, observation_id=observation.observation_id)
            envelope = ScopedAction(**scope, action=CompareAction(candidate_handles=("c01", "c02"), tier="light"))
            selected = context.resolve_comparison(envelope)
            limits = AnalysisLimits(max_work_units=12, work_unit="candidate_rollout", max_elapsed_seconds=4.0)
            result = await analysis.analyze(tuple(candidate.candidate_id for candidate in selected), limits, 73)
            updated, evidence = context.issue_evidence(envelope, result, limits=limits, seed=73)
            assert evidence.result.work_used.count == 12 and evidence.result.completion_status == "work_limit"
            decision = updated.select(ScopedAction(**scope, action=SelectAction(candidate_handle="c01", evidence_ids=("e01",))))
            candidate = validate_decision(decision, state=state, observation=observation,
                                         candidates=updated.full_pool, expected_revision=state.revision)
            committed = await arbiter.commit(candidate.move, state.revision)
            assert committed.revision == state.revision + 1
            assert (await arbiter.checkpoint()).state == committed
            paired = await prepare_decision_context(observation, counted, decision_id="native-b", candidate_order_seed=123)
            assert paired.visible_bindings == context.visible_bindings
            assert counted.generated == counted.previewed == 2
        finally:
            await analysis.close()
            await arbiter.close()

    asyncio.run(scenario())
