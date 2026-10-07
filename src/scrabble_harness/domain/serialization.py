"""Project-defined canonical JSON and versioned SHA-256 content identities.

Identity v1 hashes integer/string/bool-only observation and move payloads. This
is a project format, not RFC 8785 or a general cross-language float canonicalizer.
Private records retain private data when serialized: only observations are safe
for decision-time consumers.
"""

import hashlib
import json
from typing import Any

from ._base import DomainModel


IDENTITY_VERSION = 1


def _normalize(value: Any) -> Any:
    """Normalize domain collection semantics, retaining meaningful sequences."""
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _normalize(item) for key, item in value.items()}
    if "tiles" in result:
        tiles = result["tiles"]
        if tiles and isinstance(tiles[0], dict):
            result["tiles"] = sorted(tiles, key=lambda tile: (tile["row"], tile["column"]))
        else:
            result["tiles"] = sorted(tiles)
    if "counts" in result:
        result["counts"] = sorted(result["counts"], key=lambda count: count["tile"])
    for key in ("own_rack", "leave"):
        if key in result:
            result[key] = sorted(result[key])
    if "racks" in result:
        # Player seat order is significant; tile order within each rack is not.
        result["racks"] = [sorted(rack) for rack in result["racks"]]
    # ordered_tiles, RNG payloads, public_history, scores, and result ranking
    # stay in their supplied order. Never sort an arbitrary JSON array.
    return result


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_json(record: DomainModel) -> str:
    """Revalidate a domain record and serialize all fields, including defaults.

    The output can be loaded with that model's model_validate_json method.
    Semantically unordered collections may have a different tuple order after
    loading. An unchecked model_copy/model_construct is revalidated here.
    """
    if not isinstance(record, DomainModel):
        raise TypeError("canonical_json requires a validated domain record")
    record = type(record).model_validate(record)
    return _json(_normalize(record.model_dump(mode="json")))


def canonical_bytes(record: DomainModel) -> bytes:
    return canonical_json(record).encode("utf-8")


def _identity(kind: str, payload: dict[str, Any], prefix: str) -> str:
    envelope = {"identity_version": IDENTITY_VERSION, "kind": kind, "payload": _normalize(payload)}
    digest = hashlib.sha256(_json(envelope).encode("utf-8")).hexdigest()
    return f"{prefix}_v{IDENTITY_VERSION}_{digest}"


def _observation_identity(content: DomainModel) -> str:
    """Internal: caller has already validated public observation fields.

    Do not revalidate here: this function also runs inside identity validators.
    """
    payload = content.model_dump(mode="json", exclude={"observation_id"})
    return _identity("observation", payload, "obs")


def _candidate_identity(observation_id: str, move: DomainModel) -> str:
    """Internal: canonical action identity excludes score/equity/provenance."""
    return _identity(
        "candidate",
        {"observation_id": observation_id, "move": move.model_dump(mode="json")},
        "cand",
    )
