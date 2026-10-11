"""Real NWL23 comparison through saved scripted actions and typed tools."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from scrabble_harness.domain import (
    EngineConfig, GameConfig, LexiconConfig, ModelSettings, RulesetConfig,
    RunConfig, RunLimits, RunSeeds, canonical_bytes, validate_decision,
)
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
from scrabble_harness.harness import (
    DependencyVersion, ExecutionManifest, RateCard, ToolExecutor,
    build_model_request, prepare_decision_context,
)
from scrabble_harness.harness.budget import InputTokenBound, verified_input_bound
from scrabble_harness.providers import (
    ProviderCapabilities, ScriptedProvider, ScriptedReply, ScriptedStep,
    TokenUsage, comparison_call, selection_call,
)
from scrabble_harness.runs import BudgetLedger, RunStore

pytestmark = pytest.mark.native
PROJECT = Path(__file__).parents[2]


@pytest.mark.parametrize("tier,work", [("light",60),("standard",300)])
def test_real_comparison_selects_without_committing_and_survives_reopen(tmp_path,tier,work):
    explicit=os.environ.get("SCRABBLE_NWL_MANIFEST")
    path=Path(explicit) if explicit else PROJECT/".native/nwl23/manifest.json"
    if not path.is_file():
        if explicit:
            pytest.fail("Configured NWL manifest is missing")
        pytest.skip("Full NWL23 profile is not installed")
    async def scenario():
        settings=MagpieSettings.from_manifest(path)
        analysis,arbiter=MagpieAnalysisEngine(settings),MagpieArbiterEngine(settings)
        try:
            rules=RulesetConfig(ruleset_id="english-six-scoreless-v1",scoreless_turn_limit=6)
            game=await arbiter.start_game(GameConfig(game_id="tools-native-"+tier,seed=42,
                lexicon_id=settings.lexicon_id,ruleset=rules))
            observation=await arbiter.observe(game.active_player)
            checkpoint=await arbiter.checkpoint()
            context=await prepare_decision_context(observation,analysis,decision_id="native-tools",candidate_order_seed=123)
            model=ModelSettings(provider="scripted",model="offline",max_output_tokens=1024)
            run=RunConfig(model=model,engine=EngineConfig(engine_commit=settings.engine_commit),
                lexicon=LexiconConfig(lexicon_id=settings.lexicon_id,sha256=json.loads(path.read_text())["lexicon_sha256"]),
                ruleset=rules,policy_version=context.config.workflow,candidate_config=context.config.candidate_config,
                seeds=RunSeeds(game=42,analysis=73,candidate_order=123,trial=1),
                limits=RunLimits(max_api_cost_microusd=1000000,max_model_calls=100,max_engine_work_units=10000,engine_work_unit="candidate_rollout",max_elapsed_seconds=3600.0))
            manifest=ExecutionManifest.create(run_config=run,harness_config=context.config,engine_configuration_id=settings.configuration_id,
                source_sha256="c"*64,trial_id="native-tools-"+tier,dependencies=(DependencyVersion(name="fixture",version="1"),),
                rate_card=RateCard(provider=model.provider,model=model.model,input_microusd_per_million_tokens=0,
                    output_microusd_per_million_tokens=0,source_url="https://example.com/synthetic-rates",
                    retrieved_on="2026-10-07",estimation_method="synthetic-offline"))
            caps=ProviderCapabilities(provider=model.provider,model=model.model,max_output_tokens=1024)
            provider=ScriptedProvider((ScriptedStep(attempt_id="compare-attempt",outcome=ScriptedReply(response_id="compare-response",request_id="compare-request",
                tool_calls=(comparison_call("compare-call",("c01","c02"),tier=tier),),usage=TokenUsage(input_tokens=100,output_tokens=10))),
                ScriptedStep(attempt_id="select-attempt",outcome=ScriptedReply(response_id="select-response",request_id="select-request",
                    tool_calls=(selection_call("select-call","c01",evidence_ids=("e01",)),),usage=TokenUsage(input_tokens=100,output_tokens=10)))),capabilities=caps)
            database=tmp_path/"private"/"tools.sqlite3"
            with RunStore(database) as store:
                store.create_campaign("campaign",run.limits)
                store.create_run("run","campaign",manifest)
                store.prepare_decision("run",context)
                ledger=BudgetLedger(store)
                executor=ToolExecutor(analysis,ledger)
                request=build_model_request(context.model_view(),attempt_id="compare-attempt",model=model,capabilities=caps,
                    remaining_limits=ledger.remaining_limits(context.decision_id),config=context.config)
                finish=InputTokenBound(provider=model.provider,model=model.model,purpose="final_selection",method="verified_complete_input_v1",
                    input_tokens=10000,max_output_tokens=1024,evidence="Synthetic offline follow-up ceiling")
                ledger.reserve_model("compare-model",request,verified_input_bound(request,input_tokens=1000,evidence="Synthetic offline counter"),finish_bound=finish)
                ledger.dispatch_model("compare-model",requested_timeout_seconds=5.0)
                ledger.settle_model("compare-model",await provider.complete(request))
                compared=await executor.execute(request.attempt_id)
                assert compared.view.work_used.count == work and compared.view.completion_status == "work_limit"
                saved=store.get_decision(context.decision_id).context
                final=build_model_request(saved.model_view(),attempt_id="select-attempt",model=model,capabilities=caps,
                    remaining_limits=ledger.remaining_limits(context.decision_id),config=saved.config,evidence=saved.evidence_views(),force_selection=True)
                ledger.reserve_model("select-model",final,verified_input_bound(final,input_tokens=1000,evidence="Synthetic offline counter"),use_finish_hold=True)
                ledger.dispatch_model("select-model",requested_timeout_seconds=5.0)
                ledger.settle_model("select-model",await provider.complete(final))
                selected=await executor.execute(final.attempt_id)
                candidate=validate_decision(selected.decision,state=game,observation=observation,
                    candidates=saved.full_pool,expected_revision=game.revision)
                assert candidate.candidate_id == saved.visible_bindings[0].candidate_id
                assert selected.decision.evidence_ids == ("e01",)
                assert (await arbiter.checkpoint()) == checkpoint  # Tools never commit a turn.
                assert ledger.totals(campaign_id="campaign").settled.engine_work_units == work
            with RunStore(database) as reopened:
                unused=object()  # Replay must not call the engine.
                restored=await ToolExecutor(unused,BudgetLedger(reopened)).execute("compare-attempt")
                assert canonical_bytes(restored)==canonical_bytes(compared)
                assert reopened.get_reservation(compared.reservation_id).native_result==compared.evidence.result
                reopened.check_integrity()
        finally:
            await analysis.close()
            await arbiter.close()
    asyncio.run(scenario())
