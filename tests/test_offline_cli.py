"""Offline CLI acceptance; all provider billing is explicitly synthetic."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from scrabble_harness.cli import main
from scrabble_harness.offline import offline_config, scripted_steps, source_fingerprint
from scrabble_harness.runs import RunStore

PROJECT=Path(__file__).parents[1]


@pytest.mark.parametrize("key,value",[("max_model_attempts",0),("max_tool_steps",0),("max_engine_work_units",13),
    ("max_api_cost_microusd",0),("max_elapsed_seconds",0.1)])
def test_offline_reductions_preserve_valid_configuration(key,value):
    config=offline_config("standard",**{key:value})
    assert getattr(config.decision_limits,key)==value
    assert config.decision_limits.finish_reserve_seconds < config.decision_limits.max_elapsed_seconds
    assert all(t.max_elapsed_seconds<=config.decision_limits.max_elapsed_seconds for t in config.comparison_tiers)


@pytest.mark.parametrize("key,value",[("max_model_attempts",3),("max_tool_steps",2),("max_engine_work_units",301),
    ("max_api_cost_microusd",30001),("max_elapsed_seconds",31.0),("max_model_attempts",-1),("max_elapsed_seconds",0.0),
    ("max_tool_steps",True),("max_elapsed_seconds",float("nan")),("unknown",1)])
def test_invalid_overrides_fail_before_native_work(key,value):
    with pytest.raises((ValueError,ValidationError)): offline_config("standard",**{key:value})


def test_scripts_keep_reduced_work_for_selection_instead_of_unavailable_second_tool():
    config=offline_config("deep",max_engine_work_units=300)
    steps=scripted_steps("decision",config,"compare-select")
    assert [s.outcome.tool_calls[0].name for s in steps]==["compare_moves","select_move","select_move"]
    assert json.loads(steps[1].outcome.tool_calls[0].arguments_json)["evidence_ids"]==["e01"]


def test_help_loads_no_provider_sdk_or_native_worker():
    code="import sys;from scrabble_harness.cli import main\ntry: main(['decide','--help'])\nexcept SystemExit as e: assert e.code==0\nassert not any(n in sys.modules for n in ('openai','anthropic','ctypes','scrabble_harness.engine.worker'))"
    result=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True)
    assert result.returncode==0 and "--workflow" in result.stdout


def test_source_fingerprint_is_stable():
    assert source_fingerprint()==source_fingerprint() and len(source_fingerprint())==64


def test_cli_rejects_existing_output_before_creating_database(tmp_path,capsys):
    output=tmp_path/"report.json";output.write_text("keep")
    database=tmp_path/"private/runs.sqlite3"
    assert main(["decide","--database",str(database),"--output",str(output)])==2
    assert output.read_text()=="keep" and not database.exists()
    assert "output must be a new file" in capsys.readouterr().err


def test_cli_rejects_output_database_collision(tmp_path):
    path=tmp_path/"private/runs.sqlite3"
    assert main(["decide","--database",str(path),"--output",str(path)])==2
    assert not path.exists()


def test_cli_missing_native_manifest_returns_controlled_error(tmp_path,capsys):
    assert main(["decide","--manifest",str(tmp_path/"missing.json"),"--database",str(tmp_path/"private/runs.sqlite3")])==2
    assert "Invalid native manifest" in capsys.readouterr().err


def native_command(tmp_path,*extra):
    explicit=os.environ.get("SCRABBLE_NWL_MANIFEST")
    manifest=Path(explicit) if explicit else PROJECT/".native/nwl23/manifest.json"
    if not manifest.is_file():
        if explicit: pytest.fail("Configured NWL manifest missing")
        pytest.skip("NWL23 not installed")
    database=tmp_path/"private/runs.sqlite3"
    result=subprocess.run([sys.executable,"-m","scrabble_harness","decide","--manifest",str(manifest),
        "--database",str(database),"--run-id","acceptance",*extra],capture_output=True,text=True,timeout=60)
    assert result.returncode in {0,1},result.stderr
    report=json.loads(result.stdout)
    assert report["paid_provider_requests"]==0 and report["game_unchanged"]
    assert report["resources"]["reserved"]["model_calls"]==0
    with RunStore(database) as store:
        store.check_integrity()
        saved=store.get_decision(report["outcome"]["decision_id"]).outcome
        assert saved.model_dump(mode="json")==report["outcome"]
        assert store.get_run("acceptance").status in {"completed","failed"}
    return report,result,database


@pytest.mark.native
@pytest.mark.parametrize("workflow,calls,work",[("fast",1,0),("standard",2,300),("deep",3,600)])
def test_native_cli_full_workflows_produce_validated_canonical_selection(tmp_path,workflow,calls,work):
    report,result,database=native_command(tmp_path,"--workflow",workflow)
    assert result.returncode==0 and report["selection_validated"]
    assert report["outcome"]["status"]=="selected"
    assert report["outcome"]["usage"]["model_attempts"]==calls
    assert report["outcome"]["usage"]["confirmed_engine_work_units"]==work
    assert report["selected_candidate"]["candidate_id"]==report["outcome"]["decision"]["candidate_id"]


@pytest.mark.native
@pytest.mark.parametrize("scenario,status,unknown",[("malformed","selected",False),("forged-evidence","selected",False),("refusal","fallback",False),
    ("missing-usage","selected",True),("transport-failure","fallback",True),("timeout","fallback",True)])
def test_native_cli_failures_and_uncertain_usage_are_reconciled(tmp_path,scenario,status,unknown):
    report,result,database=native_command(tmp_path,"--scenario",scenario,"--model-timeout-seconds","0.02")
    assert report["outcome"]["status"]==status
    assert (report["outcome"]["usage"]["unresolved_api_cost_microusd"]>0)==unknown
    assert report["selection_validated"]


@pytest.mark.native
@pytest.mark.parametrize("flag,value",[("--max-model-attempts","1"),("--max-tool-steps","0"),
    ("--max-engine-work-units","13"),("--max-api-cost-microusd","0"),("--max-elapsed-seconds","0.000001")])
def test_native_cli_acceptance_for_every_resource_ceiling(tmp_path,flag,value):
    report,result,database=native_command(tmp_path,flag,value)
    usage=report["outcome"]["usage"]
    if flag=="--max-model-attempts": assert usage["model_attempts"]<=1
    elif flag=="--max-tool-steps": assert usage["tool_steps"]==0
    elif flag=="--max-engine-work-units": assert usage["confirmed_engine_work_units"]+usage["unresolved_engine_work_units"]<=13
    elif flag=="--max-api-cost-microusd": assert usage["settled_api_cost_microusd"]+usage["unresolved_api_cost_microusd"]==0 and usage["model_attempts"]==0
    else: assert report["outcome"]["status"]=="aborted" and usage["model_attempts"]==0 and result.returncode==1


@pytest.mark.native
def test_cli_report_is_private_and_existing_run_is_not_replayed(tmp_path):
    output=tmp_path/"report.json"
    report,result,database=native_command(tmp_path,"--output",str(output))
    assert json.loads(output.read_text())==report and output.stat().st_mode&0o777==0o600
    command=[sys.executable,"-m","scrabble_harness","decide","--manifest",os.environ.get("SCRABBLE_NWL_MANIFEST",str(PROJECT/".native/nwl23/manifest.json")),
        "--database",str(database),"--run-id","acceptance"]
    again=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert again.returncode==2 and "run ID already exists" in again.stderr
    with RunStore(database) as store:
        assert len(store.reservations("acceptance"))==3  # Two model calls and one comparison.
