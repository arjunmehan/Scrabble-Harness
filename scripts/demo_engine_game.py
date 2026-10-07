#!/usr/bin/env python3
"""Play a deterministic engine-only game through the concrete adapters."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from scrabble_harness.domain import CandidateConfig, GameConfig, RulesetConfig
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings


async def play(manifest: Path, seed: int) -> dict:
    settings = MagpieSettings.from_manifest(manifest)
    analysis, arbiter = MagpieAnalysisEngine(settings), MagpieArbiterEngine(settings)
    config = GameConfig(game_id=f"engine-demo-{seed}", lexicon_id=settings.lexicon_id,
                        seed=seed, ruleset=RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6))
    trace = []
    try:
        state = await arbiter.start_game(config)
        for _ in range(200):
            observation = await arbiter.observe(state.active_player)
            await analysis.load_observation(observation)
            pool = await analysis.generate_candidates(CandidateConfig(pool_size=40, display_count=12))
            if not pool:
                raise RuntimeError("No fallback candidate generated")
            selected = pool[0]
            # Round-trip a private checkpoint into a new arbiter midway through
            # the game. The native worker is rebuilt lazily from public facts.
            if state.revision == 3:
                saved = await arbiter.checkpoint()
                await arbiter.close()
                arbiter = MagpieArbiterEngine(settings)
                state = await arbiter.restore(saved)
            state = await arbiter.commit(selected.move, observation.revision)
            trace.append({"observation": observation.model_dump(mode="json"),
                          "candidate": selected.model_dump(mode="json"),
                          "scores_after": state.scores, "status": state.status})
            if state.status == "finished":
                break
        else:
            raise RuntimeError("Demo did not finish within 200 turns")
        return {"schema_version": 1, "engine_commit": settings.engine_commit,
                "configuration_id": settings.configuration_id, "game_config": config.model_dump(mode="json"),
                "purpose": ("adapter smoke game with toy dictionary; not a strength benchmark"
                            if settings.lexicon_id == "NWL_HARNESS_SMOKE" else
                            "full-lexicon static-policy game; not an LLM benchmark"),
                "turns": trace, "final_scores": state.scores, "terminal_reason": state.terminal_reason,
                "checkpoint_restored": len(trace) > 3}
    finally:
        await analysis.close()
        await arbiter.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    full_profile = PROJECT / ".native/nwl23/manifest.json"
    default_manifest = full_profile if full_profile.is_file() else PROJECT / ".native/manifest.json"
    parser.add_argument("--manifest", type=Path, default=default_manifest)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(play(args.manifest, args.seed))
    if args.output is None:
        name = "engine-demo.json" if result["game_config"]["lexicon_id"] == "NWL_HARNESS_SMOKE" else "nwl23-demo.json"
        args.output = PROJECT / "docs" / name
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Finished {len(result['turns'])} turns; scores {result['final_scores']}; {result['terminal_reason']}")
    print(f"Trace: {args.output}")


if __name__ == "__main__":
    main()
