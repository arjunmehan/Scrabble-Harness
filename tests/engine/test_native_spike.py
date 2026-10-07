import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

PROJECT = Path(__file__).resolve().parents[2]


@pytest.mark.native
def test_real_pinned_native_capabilities_in_isolated_process(tmp_path):
    configured = os.environ.get("SCRABBLE_MAGPIE_MANIFEST")
    manifest = Path(configured) if configured else PROJECT / ".native/manifest.json"
    if not manifest.is_file():
        if configured:
            pytest.fail(f"Configured native manifest is missing: {manifest}")
        pytest.skip("Build scripts/bootstrap_magpie.py or set SCRABBLE_MAGPIE_MANIFEST to a setup manifest")
    report_path = tmp_path / "results.json"
    result = subprocess.run(
        [sys.executable, str(PROJECT / "scripts/probe_magpie.py"),
         "--manifest", str(manifest.resolve()), "--report", str(report_path)],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text())
    assert report["all_checks_passed"]
    assert len(report["checks"]) == 13
    assert report["checks"]["unknown_rack_and_seed_repeatability"]["passed"]
    assert report["checks"]["restricted_bounded_analysis"]["result"]["iterations"] == 12
    assert report["checks"]["cancellation_and_handle_reuse"]["passed"]
