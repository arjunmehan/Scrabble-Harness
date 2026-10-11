import asyncio
import json
import os
from pathlib import Path

import pytest

from scrabble_harness.domain import AnalysisLimits, GameConfig, ModelSettings, RulesetConfig
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings
from scrabble_harness.harness import (
    ScopedAction, build_model_request, parse_tool_arguments, prepare_decision_context,
    render_comparison_result, render_prompt,
)
from scrabble_harness.harness.prompts import STANDARD_PREMIUMS
from scrabble_harness.providers import ProviderCapabilities
from conftest import update

pytestmark = pytest.mark.native
PROJECT = Path(__file__).parents[2]


@pytest.fixture
def nwl_settings():
    explicit = os.environ.get("SCRABBLE_NWL_MANIFEST")
    path = Path(explicit) if explicit else PROJECT / ".native/nwl23/manifest.json"
    if not path.is_file():
        if explicit:
            pytest.fail("Configured SCRABBLE_NWL_MANIFEST does not exist")
        pytest.skip("Full NWL23 profile is not installed")
    return MagpieSettings.from_manifest(path)


def test_rendered_premium_layout_matches_all_native_board_squares(nwl_settings):
    layout = (nwl_settings.data / "layouts/standard15.txt").read_text().splitlines()
    assert layout[0] == "7, 7"
    mapping = {"=": "TW", "-": "DW", "'": "DL", '"': "TL", " ": ".."}
    expected = tuple(tuple(mapping[square] for square in row) for row in layout[1:])
    assert STANDARD_PREMIUMS == expected


def test_real_nwl_prompt_compare_result_and_final_selection_protocol(nwl_settings):
    async def scenario():
        analysis, arbiter = MagpieAnalysisEngine(nwl_settings), MagpieArbiterEngine(nwl_settings)
        try:
            await arbiter.start_game(GameConfig(game_id="prompt-native", seed=42, lexicon_id=nwl_settings.lexicon_id,
                ruleset=RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6)))
            context = await prepare_decision_context(await arbiter.observe(0), analysis,
                decision_id="prompt-native-a", candidate_order_seed=123)
            view, limits = context.model_view(), context.config.decision_limits
            model = ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024)
            capabilities = ProviderCapabilities(provider="fixture", model="fixture-model", max_output_tokens=1024)
            request = build_model_request(view, attempt_id="native-attempt-1", model=model,
                capabilities=capabilities, remaining_limits=limits)
            assert request.tools == ("select_move", "compare_moves")
            assert "NWL23" in request.instructions and len(request.model_dump_json().encode()) <= 65536
            compare = parse_tool_arguments("compare_moves", '{"candidate_handles":["c01","c02"],"tier":"light"}',
                                           allowed_tools=request.tools)
            envelope = ScopedAction(decision_id=context.decision_id, observation_id=view.observation.observation_id, action=compare)
            chosen = context.resolve_comparison(envelope)
            allowance = AnalysisLimits(max_work_units=12, work_unit="candidate_rollout", max_elapsed_seconds=4.0)
            result = await analysis.analyze(tuple(candidate.candidate_id for candidate in chosen), allowance, 73)
            context, evidence = context.issue_evidence(envelope, result, limits=allowance, seed=73)
            visible = context.evidence_views()
            final_limits = update(limits, max_model_attempts=1, max_tool_steps=0, max_engine_work_units=288)
            final = build_model_request(view, attempt_id="native-attempt-2", model=model,
                capabilities=capabilities, remaining_limits=final_limits, evidence=visible)
            assert final.tools == ("select_move",) and final.force_selection
            assert json.loads(render_comparison_result(visible[0]))["work_used"]["count"] == 12
            assert render_comparison_result(visible[0]) in final.instructions
            selected = parse_tool_arguments("select_move", '{"candidate_handle":"c01","evidence_ids":["e01"],"brief_note":"After comparison"}',
                                            allowed_tools=final.tools)
            decision = context.select(ScopedAction(decision_id=context.decision_id,
                observation_id=view.observation.observation_id, action=selected))
            assert decision.candidate_id == chosen[0].candidate_id and decision.evidence_ids == ("e01",)
        finally:
            await analysis.close()
            await arbiter.close()

    asyncio.run(scenario())
