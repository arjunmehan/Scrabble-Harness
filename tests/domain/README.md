# Domain tests

The suite covers structural validation, JSON round-trips, physical blank
identity, tile conservation, observation filtering, exchange-history privacy,
analysis status, and configuration. Twenty deterministic hidden-state
repartitions check that changing the opponent rack, draw order, and RNG state
cannot change a fixed public observation.

Canonical identity tests check normalized collection order, blank assignments,
public context changes, versioned compatibility vectors, JSON identity checks,
and independence from Python's randomized hashes. Selection tests reject stale
revisions, old or foreign observations, the wrong player, finished games,
mixed/duplicate pools, unknown IDs, and unchecked payload alterations.

Native legality and scoring checks belong in `tests/engine` once the adapter is
implemented.
