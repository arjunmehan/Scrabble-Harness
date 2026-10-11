"""Fixed workflows through the real NWL23 adapter and durable loop."""

import asyncio
import json
import hashlib
import os
from pathlib import Path

import pytest

from scrabble_harness.domain import (
    EngineConfig, GameConfig, LexiconConfig, ModelSettings, RulesetConfig,
    RunConfig, RunLimits, RunSeeds, canonical_bytes, validate_decision,
)
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
from scrabble_harness.harness import (
    DependencyVersion, ExecutionManifest, RateCard, DecisionLoop, load_workflow,
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


@pytest.mark.parametrize("name,work", [("fast",0),("standard",300),("deep",600)])
def test_real_fixed_workflows_and_comparison_fallback(tmp_path,name,work):
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
            game=await arbiter.start_game(GameConfig(game_id="policies-native-"+name,seed=42,
                lexicon_id=settings.lexicon_id,ruleset=rules))
            observation=await arbiter.observe(game.active_player)
            checkpoint=await arbiter.checkpoint()
            config=load_workflow(name,fallback_policy="latest_comparison")
            model=ModelSettings(provider="scripted",model="offline",max_output_tokens=1024)
            run=RunConfig(model=model,engine=EngineConfig(engine_commit=settings.engine_commit),
                lexicon=LexiconConfig(lexicon_id=settings.lexicon_id,sha256=json.loads(path.read_text())["lexicon_sha256"]),
                ruleset=rules,policy_version=config.workflow,candidate_config=config.candidate_config,
                seeds=RunSeeds(game=42,analysis=73,candidate_order=123,trial=1),
                limits=RunLimits(max_api_cost_microusd=1000000,max_model_calls=100,max_engine_work_units=10000,engine_work_unit="candidate_rollout",max_elapsed_seconds=3600.0))
            manifest=ExecutionManifest.create(run_config=run,harness_config=config,engine_configuration_id=settings.configuration_id,
                source_sha256="c"*64,trial_id="native-policies-"+name,dependencies=(DependencyVersion(name="fixture",version="1"),),
                rate_card=RateCard(provider=model.provider,model=model.model,input_microusd_per_million_tokens=0,
                    output_microusd_per_million_tokens=0,source_url="https://example.com/synthetic-rates",
                    retrieved_on="2026-10-07",estimation_method="synthetic-offline"))
            caps=ProviderCapabilities(provider=model.provider,model=model.model,max_output_tokens=1024)
            def attempt(index):
                return "attempt_"+hashlib.sha256(f"native-loop:{index}".encode()).hexdigest()
            def step(index,calls=(),status="completed"):
                return ScriptedStep(attempt_id=attempt(index),outcome=ScriptedReply(
                    response_id=f"response-{index}",request_id=f"request-{index}",
                    status=status,tool_calls=calls,usage=TokenUsage(input_tokens=100,output_tokens=10)))
            steps=[]
            if name!="fast":
                steps.append(step(1,(comparison_call("c1",("c01","c02"),tier="standard"),)))
            if name=="deep":
                steps.append(step(2,(comparison_call("c2",("c02","c03"),tier="standard"),)))
            final=config.decision_limits.max_model_attempts
            steps.append(step(final,status="refused") if name=="standard" else
                step(final,(selection_call("select","c02" if name=="deep" else "c01",evidence_ids=("e02",) if name=="deep" else ()),)))
            provider=ScriptedProvider(steps,capabilities=caps)
            database=tmp_path/"private"/"tools.sqlite3"
            with RunStore(database) as store:
                store.create_campaign("campaign",run.limits)
                store.create_run("run","campaign",manifest)
                ledger=BudgetLedger(store)
                finish=InputTokenBound(provider=model.provider,model=model.model,purpose="final_selection",method="verified_complete_input_v1",
                    input_tokens=10000,max_output_tokens=1024,evidence="Synthetic offline follow-up ceiling")
                loop=DecisionLoop(provider,analysis,ledger,capabilities=caps,finish_bound=finish,
                    input_bound=lambda request:verified_input_bound(request,input_tokens=1000,evidence="Synthetic offline counter"))
                outcome=await loop.decide("run","native-loop",observation)
                assert outcome.status == ("fallback" if name=="standard" else "selected")
                assert outcome.selection_origin == ("latest_comparison" if name=="standard" else "model")
                assert outcome.usage.model_attempts == config.decision_limits.max_model_attempts
                assert outcome.usage.tool_steps == config.decision_limits.max_tool_steps
                assert outcome.usage.confirmed_engine_work_units == work
                assert all(e.result.completion_status == "work_limit" for e in outcome.evidence)
                candidate=validate_decision(outcome.decision,state=game,observation=observation,
                    candidates=outcome.trusted_pool,expected_revision=game.revision)
                assert candidate.candidate_id == outcome.decision.candidate_id
                assert outcome.decision.evidence_ids == {"fast":(),"standard":("e01",),"deep":("e02",)}[name]
                assert (await arbiter.checkpoint()) == checkpoint
                assert provider.requests[-1].tools == ("select_move",)
            with RunStore(database) as reopened:
                unused=object()
                restored_loop=DecisionLoop(unused,unused,BudgetLedger(reopened),capabilities=caps,
                    input_bound=lambda request:(_ for _ in ()).throw(AssertionError("No new request")))
                restored=await restored_loop.decide("run","native-loop",observation)
                assert canonical_bytes(restored)==canonical_bytes(outcome)
                reopened.check_integrity()
        finally:
            await analysis.close()
            await arbiter.close()
    asyncio.run(scenario())
