"""Fixed workflow ceilings and evidence-backed fallback behavior."""

import asyncio
import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import AnalysisLimits, canonical_json
from scrabble_harness.harness import CompareAction, DecisionOutcome, DecisionUsage, ScopedAction, load_workflow, select_fallback
from test_store import context, manifest, db_path, changed
from test_loop import setup, run, step
from test_tools import RecordingAnalysis
from scrabble_harness.providers import comparison_call, selection_call


def workflow_manifest(manifest, config):
    fields=manifest.model_dump(mode="python")
    fields.pop("manifest_id");fields.pop("schema_version")
    fields["harness_config"]=config
    fields["run_config"]=changed(manifest.run_config,policy_version=config.workflow,
        candidate_config=config.candidate_config)
    return type(manifest).create(**fields)


def with_policy(context,policy):
    return changed(context,config=changed(context.config,fallback_policy=policy))


def issue(context, *, status="work_limit", values=None):
    action=ScopedAction(decision_id=context.decision_id,observation_id=context.observation.observation_id,
        action=CompareAction(candidate_handles=("c01","c02"),tier="light"))
    ids=tuple(c.candidate_id for c in context.resolve_comparison(action))
    limits=AnalysisLimits(max_work_units=60,work_unit="candidate_rollout",max_elapsed_seconds=4.0)
    result=asyncio.run(RecordingAnalysis(context).analyze(ids,limits,73))
    result=changed(result,analysis_id="analysis-"+str(len(context.evidence)+1),completion_status=status,
        values=result.values if values is None else values(result.values))
    return context.issue_evidence(action,result,limits=limits,seed=73)[0]


@pytest.mark.parametrize("name,ceilings",[("fast",(1,0,0)),("standard",(2,1,300)),("deep",(3,2,600))])
def test_packaged_presets_have_fixed_ceilings_and_explicit_defaults(name,ceilings):
    config=load_workflow(name)
    limits=config.decision_limits
    assert (limits.max_model_attempts,limits.max_tool_steps,limits.max_engine_work_units)==ceilings
    assert config.workflow == name and config.fallback_policy == "static"
    assert limits.max_api_cost_microusd == 30000 and limits.max_elapsed_seconds == 30.0
    assert json.loads(files("scrabble_harness.harness").joinpath("presets",name+".json").read_text()) == config.model_dump(mode="json")
    comparison=load_workflow(name,fallback_policy="latest_comparison")
    assert comparison.decision_limits == limits and comparison.fallback_policy == "latest_comparison"
    with pytest.raises(ValidationError): config.workflow="changed"


@pytest.mark.parametrize("name",["Fast","../fast","unknown",1,None])
def test_unknown_preset_cannot_select_arbitrary_resource(name):
    with pytest.raises(ValidationError): load_workflow(name)


def test_unknown_fallback_is_rejected():
    with pytest.raises(ValidationError): load_workflow("standard",fallback_policy="random")


@pytest.mark.parametrize("name",["fast","standard","deep"])
def test_every_preset_can_stop_after_one_selection(db_path,manifest,context,name):
    manifest=workflow_manifest(manifest,load_workflow(name))
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(selection_call("s","c01"),))])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected" and outcome.selection_origin == "model"
        assert outcome.usage.model_attempts == 1 and not analysis.calls
        assert outcome.decision.workflow == name
        assert provider.requests[0].tools == (("select_move",) if name=="fast" else ("select_move","compare_moves"))


@pytest.mark.parametrize("name",["fast","standard","deep"])
def test_preset_attempt_and_native_caps_force_final_selection(db_path,manifest,context,name):
    config=load_workflow(name)
    manifest=workflow_manifest(manifest,config)
    calls=[]
    if name!="fast": calls.append(step(1,(comparison_call("c1",("c01","c02"),tier="standard"),)))
    if name=="deep": calls.append(step(2,(comparison_call("c2",("c01","c02"),tier="light"),)))
    calls.append(step(config.decision_limits.max_model_attempts,(selection_call("s","c01"),)))
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,calls)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "selected"
        assert outcome.usage.model_attempts == config.decision_limits.max_model_attempts
        assert outcome.usage.tool_steps == config.decision_limits.max_tool_steps
        assert outcome.usage.confirmed_engine_work_units == {"fast":0,"standard":300,"deep":360}[name]
        assert outcome.usage.confirmed_engine_work_units <= config.decision_limits.max_engine_work_units
        assert provider.requests[-1].force_selection and provider.requests[-1].tools == ("select_move",)


def test_fast_cannot_dispatch_comparison_or_repair_outside_its_slot(db_path,manifest,context):
    manifest=workflow_manifest(manifest,load_workflow("fast"))
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,[step(1,(comparison_call("c",("c01","c02")),))])
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.selection_origin == "static"
        assert outcome.usage.model_attempts == 1 and outcome.usage.tool_steps == 0
        assert not analysis.calls and len(provider.requests)==1


def test_latest_fallback_without_usable_evidence_uses_verified_static(context):
    context=with_policy(context,"latest_comparison")
    result=select_fallback(context,"provider_failed")
    assert result.origin == "static" and result.decision.candidate_id == context.fallback_candidate_id
    assert result.decision.fallback_reason == "provider_failed" and result.decision.evidence_ids == ()


def test_static_policy_ignores_comparison_winner(context):
    context=issue(context)
    result=select_fallback(context,"invalid_action")
    assert result.origin == "static" and result.decision.candidate_id == context.fallback_candidate_id
    assert not result.decision.evidence_ids


@pytest.mark.parametrize("status",["complete","work_limit","timeout"])
def test_latest_fallback_uses_maximum_from_full_coverage_batch(context,status):
    context=issue(with_policy(context,"latest_comparison"),status=status)
    result=select_fallback(context,"invalid_action")
    assert result.origin == "latest_comparison"
    assert result.decision.candidate_id == context.evidence[-1].result.values[1].candidate_id
    assert result.decision.evidence_ids == ("e01",) and result.decision.fallback_reason == "invalid_action"


@pytest.mark.parametrize("status,partial",[("failed",False),("cancelled",False),("timeout",True)])
def test_unusable_latest_batch_does_not_replace_older_full_batch(context,status,partial):
    context=issue(with_policy(context,"latest_comparison"))
    older=select_fallback(context,"reason")
    context=issue(context,status=status,values=(lambda v:v[:1]) if partial else None)
    result=select_fallback(context,"reason")
    assert result == older and result.decision.evidence_ids == ("e01",)


def test_newest_usable_batch_wins_without_combining_estimates(context):
    context=issue(with_policy(context,"latest_comparison"),values=lambda v:tuple(changed(x,value=100.0+i) for i,x in enumerate(v)))
    context=issue(context,values=lambda v:tuple(changed(x,value=-float(i)) for i,x in enumerate(v)))
    result=select_fallback(context,"reason")
    assert result.decision.evidence_ids == ("e02",)
    assert result.decision.candidate_id == context.evidence[-1].result.values[0].candidate_id


def test_equal_estimates_tie_break_by_canonical_id_not_order(context):
    context=issue(with_policy(context,"latest_comparison"),values=lambda v:tuple(changed(x,value=1.0) for x in reversed(v)))
    result=select_fallback(context,"reason")
    assert result.decision.candidate_id == min(v.candidate_id for v in context.evidence[0].result.values)


@pytest.mark.parametrize("policy",["static","latest_comparison"])
def test_failed_final_reply_persists_fallback_origin_and_evidence(db_path,manifest,context,policy):
    manifest=workflow_manifest(manifest,load_workflow("standard",fallback_policy=policy))
    steps=[step(1,(comparison_call("c",("c01","c02")),)),step(2,status="refused")]
    store,ledger,provider,analysis,loop=setup(db_path,manifest,context,steps)
    with store:
        outcome=run(loop,context)
        assert outcome.status == "fallback" and outcome.selection_origin == policy
        assert outcome.terminal_reason == outcome.decision.fallback_reason == "attempts_exhausted"
        assert outcome.decision.evidence_ids == (("e01",) if policy=="latest_comparison" else ())
        assert outcome.usage.model_attempts == 2 and outcome.usage.confirmed_engine_work_units == 60
        serialized=canonical_json(outcome)
    from scrabble_harness.runs import RunStore
    with RunStore(db_path) as reopened:
        assert canonical_json(reopened.get_decision("decision-a").outcome) == serialized


def test_legacy_outcome_omits_optional_origin_from_canonical_bytes(context):
    old=DecisionOutcome(decision_id=context.decision_id,observation_id=context.observation.observation_id,
        status="fallback",decision=context.fallback_decision("legacy"),trusted_pool=context.full_pool,
        visible_bindings=context.visible_bindings,terminal_reason="legacy")
    assert "selection_origin" not in json.loads(canonical_json(old))
    assert DecisionOutcome.model_validate_json(canonical_json(old)) == old
    with pytest.raises(ValidationError): changed(old,selection_origin="model")
    with pytest.raises(ValidationError): changed(old,selection_origin="latest_comparison")


def test_comparison_origin_cannot_claim_a_losing_or_partial_estimate(context):
    context=issue(with_policy(context,"latest_comparison"))
    fallback=select_fallback(context,"reason")
    outcome=DecisionOutcome(decision_id=context.decision_id,observation_id=context.observation.observation_id,
        status="fallback",decision=fallback.decision,selection_origin=fallback.origin,
        trusted_pool=context.full_pool,visible_bindings=context.visible_bindings,evidence=context.evidence,
        usage=DecisionUsage(confirmed_engine_work_units=60),terminal_reason="reason")
    loser=context.evidence[0].result.values[0].candidate_id
    with pytest.raises(ValidationError): changed(outcome,decision=changed(outcome.decision,candidate_id=loser))
    with pytest.raises(ValidationError): changed(outcome,selection_origin="model")


def test_persisted_static_origin_requires_prepared_fallback(db_path,manifest,context):
    from scrabble_harness.runs import RunStore
    other=next(b.candidate_id for b in context.visible_bindings if b.candidate_id!=context.fallback_candidate_id)
    outcome=DecisionOutcome(decision_id=context.decision_id,observation_id=context.observation.observation_id,
        status="fallback",decision=changed(context.fallback_decision("reason"),candidate_id=other),
        selection_origin="static",trusted_pool=context.full_pool,visible_bindings=context.visible_bindings,
        terminal_reason="reason")
    with RunStore(db_path) as store:
        store.create_campaign("campaign-a",manifest.run_config.limits)
        store.create_run("run-a","campaign-a",manifest)
        store.prepare_decision("run-a",context)
        with pytest.raises(ValidationError): store.complete_decision(outcome)
        assert store.get_decision("decision-a").status == "prepared"
