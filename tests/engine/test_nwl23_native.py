"""Real full-lexicon checks; the small golden fixture remains independent."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scrabble_harness.domain import AnalysisLimits, Board, CandidateConfig, PlacementMove, build_observation
from scrabble_harness.engine import (
    InvalidMoveError, MagpieAnalysisEngine, MagpieSettings, WORK_UNIT,
)
from test_adapters_native import placement, position

PROJECT = Path(__file__).parents[2]
pytestmark = pytest.mark.native


@pytest.fixture
def nwl_manifest():
    explicit = os.environ.get("SCRABBLE_NWL_MANIFEST")
    path = Path(explicit) if explicit else PROJECT / ".native/nwl23/manifest.json"
    if not path.is_file():
        if explicit:
            pytest.fail("Configured SCRABBLE_NWL_MANIFEST does not exist")
        pytest.skip("Run scripts/setup_nwl23.py to install the full dictionary")
    assert MagpieSettings.from_manifest(path).lexicon_id == "NWL23"
    return path


def test_full_dictionary_generation_legality_leave_values_and_seeded_analysis(nwl_manifest):
    async def scenario():
        settings = MagpieSettings.from_manifest(nwl_manifest)
        engine = MagpieAnalysisEngine(settings)
        try:
            await engine.load_observation(build_observation(position(settings, own="ABDEIJ?", opponent="CFGHOTU"), 0))
            assert (await engine.preview(placement("JEDI"))).exact_score == 24
            with pytest.raises(InvalidMoveError):
                await engine.preview(placement("qA"))
            pool = await engine.generate_candidates(CandidateConfig(pool_size=30, display_count=12))
            assert sum(isinstance(candidate.move, PlacementMove) for candidate in pool) > 20
            assert any(candidate.leave and candidate.static_equity != candidate.exact_score for candidate in pool)
            for candidate in pool[:5]:
                preview = await engine.preview(candidate.move)
                assert preview.exact_score == candidate.exact_score
                assert sorted(preview.leave) == sorted(candidate.leave)
            chosen = tuple(candidate.candidate_id for candidate in pool[:3])
            limits = AnalysisLimits(max_work_units=30, work_unit=WORK_UNIT, max_elapsed_seconds=10.0)
            first = await engine.analyze(chosen, limits, 73)
            second = await engine.analyze(chosen, limits, 73)
            assert first.values == second.values
            assert first.work_used.count == second.work_used.count == 30
            assert first.completion_status == "work_limit"
            assert first.metric_definition.value_kind == "estimate"
            # Eight-letter SCRABBLE extends an existing seven-tile board word.
            board = Board(tiles=placement("SCRABBL", column=4).tiles)
            state = position(settings, own="?ADEINR", opponent="CFGHOTU", board=board)
            await engine.load_observation(build_observation(state, 0))
            assert (await engine.preview(placement("E", column=11))).exact_score == 15
        finally:
            await engine.close()
    asyncio.run(scenario())


def test_offline_profile_setup_audits_full_data_and_is_idempotent(nwl_manifest, tmp_path):
    profile = json.loads(nwl_manifest.read_text())
    output = tmp_path / "profile"
    command = [sys.executable, str(PROJECT / "scripts/setup_nwl23.py"),
               "--base-manifest", str(nwl_manifest), "--output", str(output),
               "--asset-source", str(Path(profile["data"]) / "lexica")]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=70)
    saved = (output / "manifest.json").read_bytes()
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=15)
    assert (output / "manifest.json").read_bytes() == saved
    installed = json.loads(saved)
    assert installed["word_count"] == 196601
    assert installed["length_counts"]["15"] == 3839
    assert installed["leave_count"] == 914624
    assert installed["nonzero_leave_count"] == 914607
    assert MagpieSettings.from_manifest(output / "manifest.json").configuration_id == MagpieSettings.from_manifest(nwl_manifest).configuration_id


def test_full_dictionary_game_finishes_after_checkpoint_restore(nwl_manifest, tmp_path):
    output = tmp_path / "game.json"
    subprocess.run([sys.executable, str(PROJECT / "scripts/demo_engine_game.py"),
                    "--manifest", str(nwl_manifest), "--output", str(output)],
                   check=True, capture_output=True, text=True, timeout=60)
    result = json.loads(output.read_text())
    assert result["game_config"]["lexicon_id"] == "NWL23"
    assert result["checkpoint_restored"]
    assert result["turns"][-1]["status"] == "finished"
    assert result["terminal_reason"] == "rack_out"
    assert any(turn["candidate"]["exact_score"] >= 50 for turn in result["turns"])
    assert min(result["final_scores"]) > 100
