#!/usr/bin/env python3
"""Prepare a real public candidate view without calling an LLM or committing a turn."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from scrabble_harness.domain import GameConfig, RulesetConfig
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
from scrabble_harness.harness import prepare_decision_context, render_prompt


async def prepare(manifest: Path, game_seed: int, candidate_seed: int) -> dict:
    settings = MagpieSettings.from_manifest(manifest)
    analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
    try:
        state = await arbiter.start_game(GameConfig(game_id=f"context-demo-{game_seed}", seed=game_seed,
            lexicon_id=settings.lexicon_id,
            ruleset=RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6)))
        observation = await arbiter.observe(state.active_player)
        context = await prepare_decision_context(observation, analysis, decision_id="context-demo-decision-1",
                                                 candidate_order_seed=candidate_seed)
        prompt = render_prompt(context.model_view(), context.config.decision_limits, config=context.config)
        return {"schema_version": 1, "purpose": "Candidate-context demonstration; no LLM calls or game commits",
                "lexicon_id": settings.lexicon_id, "candidate_order_seed": candidate_seed,
                "generated_candidate_count": len(context.full_pool),
                "fallback_verified": True, "model_view": context.model_view().model_dump(mode="json"),
                "prompt_version": prompt.prompt_version, "tool_schema_version": prompt.tool_schema_version,
                "rendered_prompt": prompt.as_payload()}
    finally:
        await analysis.close()
        await arbiter.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=PROJECT / ".native/nwl23/manifest.json")
    parser.add_argument("--game-seed", type=int, default=42)
    parser.add_argument("--candidate-seed", type=int, default=123)
    parser.add_argument("--output", type=Path, default=PROJECT / "docs/decision-context-demo.json")
    parser.add_argument("--prompt-output", type=Path, help="Also save the readable model prompt as text")
    args = parser.parse_args()
    result = asyncio.run(prepare(args.manifest, args.game_seed, args.candidate_seed))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.prompt_output:
        args.prompt_output.parent.mkdir(parents=True, exist_ok=True)
        args.prompt_output.write_text(result["rendered_prompt"]["instructions"])
    print(f"Prepared {result['generated_candidate_count']} candidates; "
          f"displaying {len(result['model_view']['candidates'])}; verified static fallback.")
    print(f"Public view: {args.output}")
    if args.prompt_output:
        print(f"Readable prompt: {args.prompt_output}")


if __name__ == "__main__":
    main()
