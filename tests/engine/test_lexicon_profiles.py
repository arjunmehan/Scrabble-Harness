"""Profile selection and data integrity checks without loading native code."""

import hashlib
import importlib.util
import json
from pathlib import Path
import struct

import pytest

from scrabble_harness.engine import EngineConfigurationError, MagpieSettings
from scrabble_harness.engine.settings import file_hash
from test_worker_recovery import fake_settings

PROJECT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("setup_nwl23", PROJECT / "scripts/setup_nwl23.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def manifest(root, lexicon=None):
    settings = fake_settings(root)
    name = lexicon or settings.lexicon_id
    for suffix in ("kwg", "klv2"):
        (settings.data / f"lexica/{name}.{suffix}").write_bytes(b"test profile")
    text = settings.data / f"lexica/{name}.txt"
    text.write_text("QI\nZA\n")
    value = {"library": str(settings.library), "data": str(settings.data),
             "engine_commit": settings.engine_commit, "library_sha256": file_hash(settings.library),
             "kwg_sha256": file_hash(settings.data / f"lexica/{name}.kwg"),
             "klv_sha256": file_hash(settings.data / f"lexica/{name}.klv2"),
             "lexicon_sha256": file_hash(text), "input_asset_sha256": {
                 asset: file_hash(settings.data / asset) for asset in (
                     "layouts/standard15.txt", "letterdistributions/english.csv", "strategy/winpct_english.csv")}}
    if lexicon is not None:
        value["lexicon_id"] = lexicon
    path = root / "manifest.json"
    path.write_text(json.dumps(value))
    return path


def test_legacy_and_named_profiles_select_separate_lexica(tmp_path):
    smoke = MagpieSettings.from_manifest(manifest(tmp_path / "smoke"))
    full = MagpieSettings.from_manifest(manifest(tmp_path / "full", "NWL23"))
    assert smoke.lexicon_id == "NWL_HARNESS_SMOKE"
    assert full.lexicon_id == "NWL23"
    assert smoke.configuration_id != full.configuration_id


def test_manifest_cannot_be_silently_switched_to_another_lexicon(tmp_path):
    path = manifest(tmp_path, "NWL23")
    assert MagpieSettings.from_manifest(path, lexicon_id="NWL23").lexicon_id == "NWL23"
    with pytest.raises(EngineConfigurationError, match="differs"):
        MagpieSettings.from_manifest(path, lexicon_id="NWL_HARNESS_SMOKE")


@pytest.mark.parametrize("suffix", ["kwg", "klv2", "txt"])
def test_manifest_rejects_modified_dictionary_inputs(tmp_path, suffix):
    path = manifest(tmp_path, "NWL23")
    (tmp_path / f"data/lexica/NWL23.{suffix}").write_bytes(b"modified")
    with pytest.raises(EngineConfigurationError, match="hash mismatch"):
        MagpieSettings.from_manifest(path)


def test_download_verification_checks_size_and_both_identities():
    content = b"pinned asset"
    record = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
              "git_blob_sha1": hashlib.sha1(b"blob 12\0" + content).hexdigest()}
    setup.verify_asset(content, record)
    for key, replacement in (("bytes", 0), ("sha256", "0" * 64), ("git_blob_sha1", "0" * 40)):
        with pytest.raises(ValueError):
            setup.verify_asset(content, record | {key: replacement})


@pytest.mark.parametrize("words", ["QI\nQI\n", "QI\n", "QI\nza\n", "A\nZA\n"])
def test_audit_rejects_incomplete_duplicate_or_malformed_words(tmp_path, words):
    path = tmp_path / "words.txt"
    path.write_text(words)
    with pytest.raises(ValueError, match="incomplete, duplicated, or malformed"):
        setup.word_statistics(path, 2)


@pytest.mark.parametrize("content", [b"", struct.pack("<I", 100),
    struct.pack("<IIff", 0, 2, 0.0, 0.0), struct.pack("<IIff", 0, 2, 1.0, float("nan")),
    struct.pack("<IIf", 0, 2, 1.0)])
def test_audit_rejects_truncated_zero_or_nonfinite_leave_tables(tmp_path, content):
    path = tmp_path / "leave.klv2"
    path.write_bytes(content)
    with pytest.raises(ValueError):
        setup.leave_statistics(path, 2)
