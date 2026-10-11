"""Save/reopen a real full-NWL pool and authoritative commit receipt offline."""

import asyncio
import json
import os
from pathlib import Path
import platform

import pytest

from scrabble_harness.domain import (
    AnalysisLimits, EngineConfig, GameConfig, LexiconConfig, ModelSettings, RulesetConfig, RunConfig,
    RunLimits, RunSeeds, canonical_bytes,
)
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
from scrabble_harness.harness import (
    DecisionOutcome, DependencyVersion, ExecutionManifest, RateCard,
    prepare_decision_context,
)
from scrabble_harness.harness.budget import InputTokenBound
from scrabble_harness.runs import BudgetLedger, CommitRecord, RunStore

pytestmark = pytest.mark.native
PROJECT = Path(__file__).parents[2]


def test_real_nwl_snapshot_and_arbiter_receipt_survive_reopen(tmp_path):
    explicit = os.environ.get("SCRABBLE_NWL_MANIFEST")
    path = Path(explicit) if explicit else PROJECT / ".native/nwl23/manifest.json"
    if not path.is_file():
        if explicit:
            pytest.fail("Configured SCRABBLE_NWL_MANIFEST does not exist")
        pytest.skip("Full NWL23 profile is not installed")

    async def scenario():
        settings = MagpieSettings.from_manifest(path)
        analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
        try:
            ruleset = RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6)
            game = await arbiter.start_game(GameConfig(game_id="store-native", seed=42,
                                                       lexicon_id=settings.lexicon_id, ruleset=ruleset))
            observation = await arbiter.observe(game.active_player)
            source = await arbiter.checkpoint()
            context = await prepare_decision_context(observation, analysis, decision_id="native-decision", candidate_order_seed=123)
            run = RunConfig(model=ModelSettings(provider="fixture", model="offline", max_output_tokens=1024),
                engine=EngineConfig(engine_commit=settings.engine_commit),
                lexicon=LexiconConfig(lexicon_id=settings.lexicon_id, sha256=json.loads(path.read_text())["lexicon_sha256"]),
                ruleset=ruleset, policy_version=context.config.workflow, candidate_config=context.config.candidate_config,
                seeds=RunSeeds(game=42, analysis=73, candidate_order=123, trial=1),
                limits=RunLimits(max_api_cost_microusd=1000000, max_model_calls=100,
                    max_engine_work_units=10000, engine_work_unit="candidate_rollout", max_elapsed_seconds=3600.0))
            manifest = ExecutionManifest.create(run_config=run, harness_config=context.config,
                engine_configuration_id=settings.configuration_id, source_sha256="c" * 64, trial_id="offline-native-test",
                dependencies=(DependencyVersion(name="python", version=platform.python_version()),),
                rate_card=RateCard(provider="fixture", model="offline", input_microusd_per_million_tokens=0,
                    output_microusd_per_million_tokens=0, source_url="https://example.com/synthetic-test-rates",
                    retrieved_on="2026-10-07", estimation_method="synthetic-offline-test"))
            database = tmp_path / "private" / "native.sqlite3"
            with RunStore(database) as store:
                store.create_campaign("native-campaign", run.limits)
                store.create_run("native-run", "native-campaign", manifest)
                store.prepare_decision("native-run", context)
                ledger = BudgetLedger(store)
                ledger.reserve_finish(context.decision_id, InputTokenBound(provider="fixture", model="offline",
                    purpose="final_selection", method="verified_complete_input_v1", input_tokens=10000,
                    max_output_tokens=1024, evidence="Synthetic bound; no provider is dispatched"))
                ledger.reserve_native("native-work", context.decision_id, AnalysisLimits(max_work_units=12,
                    work_unit="candidate_rollout", max_elapsed_seconds=4.0))
                actual_limits = ledger.dispatch_native("native-work")
                # Native work happens after the durable dispatch transaction commits.
                ids = tuple(binding.candidate_id for binding in context.visible_bindings[:2])
                result = await analysis.analyze(ids, actual_limits, 73)
                accounting = ledger.settle_native("native-work", result)
                assert accounting.settlement.engine_work_units == result.work_used.count == 12
                assert ledger.totals(campaign_id="native-campaign").charged.tool_steps == 1
                selected = context.fallback_decision("offline-test")
                ledger.complete_decision(DecisionOutcome(decision_id=context.decision_id,
                    observation_id=observation.observation_id, status="fallback", decision=selected,
                    trusted_pool=context.full_pool, visible_bindings=context.visible_bindings,
                    terminal_reason="offline-test"))
                move = next(candidate.move for candidate in context.full_pool if candidate.candidate_id == selected.candidate_id)
                store.intend_commit(CommitRecord(run_id="native-run", decision_id=context.decision_id,
                    game_id=observation.game_id, expected_revision=observation.revision,
                    move=move, source_checkpoint_id=source.checkpoint_id))
                committed = await arbiter.commit(move, observation.revision)
                receipt = await arbiter.checkpoint()
                store.confirm_commit(context.decision_id, new_revision=committed.revision, checkpoint_id=receipt.checkpoint_id)
            with RunStore(database) as reopened:
                saved = reopened.get_decision(context.decision_id)
                assert canonical_bytes(saved.context) == canonical_bytes(context)
                assert 12 <= len(saved.context.full_pool) <= 40
                assert len(saved.context.visible_bindings) == 12
                assert reopened.get_commit(context.decision_id).checkpoint_id == receipt.checkpoint_id
                assert reopened.get_reservation("native-work").native_result == result
                assert reopened.finish_hold(context.decision_id).state == "released"
                assert saved.outcome.usage.confirmed_engine_work_units == 12
                assert BudgetLedger(reopened).totals(campaign_id="native-campaign").charged.engine_work_units == 12
                assert [e.payload.kind for e in reopened.events("native-run")][-2:] == ["commit_intended", "commit_confirmed"]
                reopened.check_integrity()
        finally:
            await analysis.close()
            await arbiter.close()

    asyncio.run(scenario())
