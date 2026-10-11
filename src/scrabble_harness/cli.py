"""Command-line entry point; offline decisions are the first supported command."""

import argparse
import asyncio
import os
from pathlib import Path
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scrabble-harness")
    commands = parser.add_subparsers(dest="command", required=True)
    decide = commands.add_parser("decide", help="Run one offline scripted decision with the native engine")
    decide.add_argument("--manifest", type=Path, default=Path(".native/nwl23/manifest.json"))
    decide.add_argument("--database", type=Path, required=True, help="Private SQLite location; its parent must be private")
    decide.add_argument("--output", type=Path, help="Also save JSON privately; an existing file is never overwritten")
    decide.add_argument("--workflow", choices=("fast", "standard", "deep"), default="standard")
    decide.add_argument("--fallback-policy", choices=("static", "latest_comparison"), default="static")
    decide.add_argument("--scenario", choices=("compare-select", "select", "refusal", "malformed", "forged-evidence", "missing-usage", "transport-failure", "timeout"), default="compare-select")
    decide.add_argument("--campaign-id", default="offline")
    decide.add_argument("--run-id")
    for name, default in (("game",42),("analysis",73),("candidate",123)):
        decide.add_argument(f"--{name}-seed", type=int, default=default)
    for name in ("api-cost-microusd", "model-attempts", "tool-steps", "engine-work-units"):
        decide.add_argument("--max-"+name, type=int, help="Reduce this preset ceiling for acceptance checks")
    decide.add_argument("--max-elapsed-seconds", type=float)
    decide.add_argument("--model-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        from .offline import decide_offline, offline_config
        from .domain import canonical_json
        if args.output is not None and (args.output.exists() or args.output.is_symlink()
                or args.output.resolve() == args.database.resolve()):
            raise ValueError("output must be a new file distinct from the database")
        config = offline_config(args.workflow, fallback_policy=args.fallback_policy,
            max_api_cost_microusd=args.max_api_cost_microusd, max_model_attempts=args.max_model_attempts,
            max_tool_steps=args.max_tool_steps, max_engine_work_units=args.max_engine_work_units,
            max_elapsed_seconds=args.max_elapsed_seconds)
        result = asyncio.run(decide_offline(manifest=args.manifest, database=args.database, config=config,
            scenario=args.scenario, campaign_id=args.campaign_id, run_id=args.run_id,
            game_seed=args.game_seed, analysis_seed=args.analysis_seed, candidate_seed=args.candidate_seed,
            model_timeout_seconds=args.model_timeout_seconds))
        body = canonical_json(result) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(body)
        print(body, end="")
        return 0 if result.outcome.status != "aborted" else 1
    except KeyboardInterrupt:
        print("Decision cancelled; inspect the private run database for saved accounting.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"scrabble-harness: {error}", file=sys.stderr)
        return 2
