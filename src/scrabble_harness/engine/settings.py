"""Explicit, fingerprinted inputs to the supported MAGPIE build."""

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re

from .errors import EngineConfigurationError

MAGPIE_COMMIT = "375ad953e20d4edc582d345e233e8d8a2586cc06"
WORK_UNIT = "candidate_rollout"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class MagpieSettings:
    library: Path
    data: Path
    lexicon_id: str = "NWL_HARNESS_SMOKE"
    engine_commit: str = MAGPIE_COMMIT
    operation_timeout_seconds: float = 10.0
    startup_timeout_seconds: float = 10.0
    stop_grace_seconds: float = 0.5
    configuration_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "library", Path(self.library).resolve())
        object.__setattr__(self, "data", Path(self.data).resolve())
        if self.engine_commit != MAGPIE_COMMIT:
            raise EngineConfigurationError("This adapter supports only the tested MAGPIE pin")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", self.lexicon_id):
            raise EngineConfigurationError("Unsafe or unsupported native lexicon name")
        for value in (self.operation_timeout_seconds, self.startup_timeout_seconds, self.stop_grace_seconds):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise EngineConfigurationError("Worker deadlines must be positive and finite")
        object.__setattr__(self, "configuration_id", self.fingerprint())

    def fingerprint(self) -> str:
        assets = ["layouts/standard15.txt", "letterdistributions/english.csv",
                  "strategy/winpct_english.csv", f"lexica/{self.lexicon_id}.kwg",
                  f"lexica/{self.lexicon_id}.klv2"]
        try:
            payload = {"engine_commit": self.engine_commit, "lexicon_id": self.lexicon_id,
                       "library": file_hash(self.library),
                       "assets": {name: file_hash(self.data / name) for name in assets},
                       "adapter_version": 1, "simulation_plies": 2, "threads": 1}
        except OSError as exc:
            raise EngineConfigurationError(f"Missing native input: {exc}") from exc
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"magpie_v1_{digest}"

    @classmethod
    def from_manifest(cls, path: Path, **options) -> "MagpieSettings":
        """Load bootstrap paths and verify its recorded binary and asset hashes."""
        try:
            manifest = json.loads(Path(path).read_text())
            lexicon_id = manifest.get("lexicon_id", "NWL_HARNESS_SMOKE")
            requested_lexicon = options.pop("lexicon_id", lexicon_id)
            if requested_lexicon != lexicon_id:
                raise EngineConfigurationError("Requested lexicon differs from the manifest profile")
            settings = cls(library=Path(manifest["library"]), data=Path(manifest["data"]),
                           lexicon_id=lexicon_id, engine_commit=manifest["engine_commit"], **options)
            pairs = [(settings.library, manifest["library_sha256"]),
                     (settings.data / f"lexica/{settings.lexicon_id}.kwg", manifest["kwg_sha256"]),
                     (settings.data / f"lexica/{settings.lexicon_id}.klv2", manifest["klv_sha256"])]
            pairs.extend((settings.data / name, expected)
                         for name, expected in manifest["input_asset_sha256"].items())
            if "lexicon_sha256" in manifest:
                pairs.append((settings.data / f"lexica/{settings.lexicon_id}.txt", manifest["lexicon_sha256"]))
            if any(file_hash(file) != expected for file, expected in pairs):
                raise EngineConfigurationError("Native manifest hash mismatch")
            return settings
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise EngineConfigurationError(f"Invalid native manifest: {exc}") from exc
