# Scrabble Harness

This directory is the project root. The source, tests, scripts, and native
setup are here; the original project plans are in `docs/`, and earlier
builds and validation artifacts are retained in `work/`. A local `.venv`
is configured for editable development with the dependencies available
on this machine. Run `.venv/bin/python -m pytest` from this directory.

A Python foundation for an adaptive, observable harness for assisted LLM
Scrabble play.

Steps 1 through 3 implement package structure, concrete engine interfaces,
validated domain models, public observations, canonical IDs, and selection
validation. Step 4 adds a pinned MAGPIE build and executable integration spike
for generation, scoring, public unknown-rack simulation, and cancellation.
Step 5 implements public analysis and authoritative game adapters, spawned
workers, complete game transitions, and draw-preserving checkpoints. Step 6 adds
bounded JSON worker messages, request correlation, diagnostic traces, durable
revision checkpoints, and repeated fault/recovery acceptance tests. LLM calls
and exact endgame solving remain future work. No native library is loaded on
package import.

Full NWL2023 support is now installed: 196,601 words and a matching nonzero
leave-value table. The engine demo selects this profile by default. See the
[full dictionary guide](docs/nwl23.md) for setup, profile selection, source
identities, and distribution terms.

The next phase is specified in the [agent harness implementation plan](docs/harness-implementation-plan.md):
architecture, typed tools, provider integration, resource accounting, recovery,
paired evaluation, technology choices, and implementation gates.

## Development setup

Use Python 3.12 or newer and a project-local virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

If you already use uv, the equivalent setup is:

```sh
uv venv
uv pip install -e '.[dev]'
```

Pydantic validates the domain models. The development extra provides pytest,
Hypothesis for future property tests, and the wheel-building frontend. Current
tests use pytest and deterministic generated hidden-state variations.

## Layout

```text
src/scrabble_harness/
  domain/
    tiles.py              # Physical tiles and board blank assignments
    state.py              # Private state, public observations, checkpoints
    moves.py              # Placement, exchange, pass, candidate identity
    analysis.py           # Results, work accounting, completion status
    config.py             # Engine and run settings
    serialization.py      # Canonical JSON and versioned content identities
    decisions.py          # Candidate construction and selection checks
  engine/
    protocol.py           # Public analysis and private arbiter interfaces
    magpie_ffi.py          # Internal C API binding, loaded only in a child
    codec.py              # Public CGP, board-aware moves, native output parsers
    worker.py             # Spawned process, typed requests, cancellation/deadlines
    adapter.py            # Concrete analysis and authoritative game adapters
    draws.py              # Host-owned bag order, exchanges, and full RNG state
    settings.py           # Pinned native inputs and configuration fingerprint
    trace.py              # Allowlisted, correlated JSONL worker diagnostics
    checkpoints.py        # Immutable private revision snapshots and recovery
    errors.py             # Shared engine failure categories
tests/
  domain/
  engine/
  fixtures/
```

## Interface contracts

`AnalysisEngine` receives a player observation and provides candidate generation,
move previews, and bounded analysis. It must not receive private arbiter state.

`ArbiterEngine` starts games, exposes player observations, commits validated
moves against an expected revision, and creates/restores checkpoints.

Both interfaces are asynchronous because native work executes in a worker
process. `MagpieAnalysisEngine` and `MagpieArbiterEngine` implement them with
concrete domain models; construct them with `MagpieSettings.from_manifest(...)`.

The dependency direction is `engine -> domain`. Domain records must remain
independent of MAGPIE commands, native-library types, provider SDKs, and UI code.

The native boundary has two layers: `magpie_ffi.py` manages C calls and
ownership; `codec.py` translates representations. `adapter.py` presents
the project-facing engine interfaces. No arbitrary command string
belongs in the public interfaces.

## Domain models

The initial scope is two players on a 15 by 15 English board with racks of at
most seven tiles. The standard distribution is 100 physical tiles, including
two blanks. An explicit distribution can describe smaller test sets. Board and
placement coordinates are zero-based.

Every record has `schema_version=1`. Models are frozen, nested collections are
tuples, and unknown fields are rejected. Python inputs require the declared
types (including tuples); JSON arrays are accepted by `model_validate_json`.
Numeric strings, boolean coordinates, nonfinite values, and unsupported schema
versions are rejected. Use validated construction for updates;
`model_copy(update=...)` and `model_construct` bypass validation in Pydantic.

- `BoardTile` records the displayed letter and physical blank flag. An assigned
  blank remains `?` in physical inventory; racks use uppercase letters or `?`.
- `Move` is a discriminated union of `PlacementMove`, `ExchangeMove`, and
  `PassMove`. Placements contain only new tiles, with unique squares in a row
  or column. Gaps may contain existing letters. Connectivity, ownership,
  dictionary validity, and scoring remain engine checks.
- `GameState` retains both racks, ordered bag contents, and opaque full RNG
  state. Board, racks, and bag must exactly conserve the configured distribution.
- `PlayerObservation` includes only public facts and the player's own rack.
  Unseen counts are distribution minus board minus own rack: the unknown rack
  and bag combined. `PublicExchangeMove` exposes a count, never exchanged tiles.
- `Candidate`, `MovePreview`, and `AgentDecision` bind to an observation ID.
  Observations and candidates verify their canonical content IDs when loaded.
  These records do not authorize a game commit.
- `AnalysisResult` records named metrics, actual work and its definition,
  elapsed time, seed, and completion status. Interrupted analysis cannot claim
  exact values. Completed exact values describe evaluated candidates; they do
  not prove global optimality or coverage of all legal moves.
- `Checkpoint` is private and retains full draw state. Concrete checkpoints also
  carry game configuration and engine fingerprint, bound by their content ID.
- `RunConfig` records model settings, a full engine commit, lexicon SHA-256,
  ruleset, policy, candidate settings, seeds, and independent resource caps.
  API cost uses integer millionths of a US dollar (`100_000_000` equals $100).
  Configuration models do not execute or enforce runtime budgets.

Load the saved fixture and create a safe public projection after installation:

```python
from pathlib import Path
from scrabble_harness.domain import GameState, build_observation

state = GameState.model_validate_json(
    Path("tests/fixtures/opening-blank-v1.json").read_text()
)
observation = build_observation(state, player=0)
print(observation.model_dump_json(indent=2))
```

Send only observations to decision-time consumers. A `GameState` or `Checkpoint`
contains secrets. Public history is an ordered retained suffix of committed
turns; native integration must supply valid history and terminal scoring.

## Canonical serialization and identities

Use `canonical_json(record)` or `canonical_bytes(record)` for deterministic
serialization. Both revalidate the model, include defaults and schema versions,
sort object keys, and emit compact JSON with ASCII escapes. Board/placement
tiles are sorted by coordinates, inventory entries by symbol, and rack/leave/
exchange tiles as multisets. Duplicates in an exchange are retained. Player seat
order, scores, public history, candidate-result order, bag draw order, and opaque
RNG data keep their meaning and order. Models are not mutated by serialization.

Load the output with the corresponding model's `model_validate_json` method.
Unordered tuple fields may be reordered, while their canonical bytes remain
identical. A private state's canonical JSON still contains its private data.

Identity v1 uses SHA-256 over the UTF-8 bytes of this compact, sorted-key envelope:

```text
{"identity_version":1,"kind":"observation","payload":<public content without observation_id>}
{"identity_version":1,"kind":"candidate","payload":{"move":<normalized move>,"observation_id":<observation ID>}}
```

Observation IDs have the form `obs_v1_<64 lowercase hex digits>`. They include
game ID, revision, player perspective, board and blank assignments, own rack,
scores, unknown sizes, public history, distribution, ruleset, lexicon, and game
status. Opponent tiles and private draw/RNG data never enter this hash.
Candidate IDs are `cand_v1_<64 lowercase hex digits>` and identify an action in
one observation. Score, leave, estimates, and provenance are recorded metadata,
not part of action identity. IDs are not general-purpose analysis cache keys;
future caches must also bind engine/configuration, seeds, and work limits.

`build_observation(state, player)` derives an ID automatically. For imported
public positions, validate `ObservationData` and use
`PlayerObservation.from_data(content)`. `build_candidate(...)` derives the
candidate ID. Persisted observations and candidates must already carry a
matching ID; arbitrary labels, unsupported ID versions, and altered payloads
with an old ID are rejected. Hashes check content binding, not authenticity.

The format is project-defined, not RFC 8785. Identity payloads contain no floats;
other domain records use Python's JSON float representation. Do not change v1
normalization or envelope semantics without a new identity version and explicit
migration. Saved compatibility vectors lock in the current format.

## Validate a selection

Use a host-owned candidate pool and the current authoritative state:

```python
from scrabble_harness.domain import (
    AgentDecision, CandidateProvenance, PassMove,
    build_candidate, build_observation, validate_decision,
)

observation = build_observation(state, state.active_player)
candidate = build_candidate(
    observation, PassMove(), exact_score=0, leave=observation.own_rack,
    provenance=CandidateProvenance(source="example", configuration_id="example-v1"),
)
decision = AgentDecision(
    observation_id=observation.observation_id,
    candidate_id=candidate.candidate_id, workflow="example",
)
selected = validate_decision(
    decision, state=state, observation=observation, candidates=(candidate,),
    expected_revision=observation.revision,
)
```

`validate_decision` returns the candidate from the supplied pool. It rejects a
stale revision or observation, the wrong active player/game, a finished game,
mixed or duplicate pool IDs, and a selection absent from the pool. It revalidates
all records to catch unchecked Pydantic copies. Context mismatches raise
`StaleDecisionError`, missing selections raise `UnknownCandidateError`, and
other selection failures raise `DecisionValidationError`; malformed records
raise Pydantic `ValidationError`.

This check is pure: it does not change the state, verify engine legality, validate
evidence, or perform an atomic commit. The future arbiter must recheck the
expected revision and legality within its serialized commit operation before
applying `selected.move`.

## Checks

Build a distributable wheel:

```sh
.venv/bin/python -m build --wheel
```

Run the suite (native checks skip without setup; no API keys required):

```sh
.venv/bin/python -m pytest
```

Tests cover malformed input, all move variants, JSON round-trips, physical blank
identity, tile conservation, private-state filtering, public exchange history,
analysis completeness, reproducibility configuration, and hidden-state
variations. Identity tests also cover equivalent ordering, meaningful context
changes, tampered IDs, compatibility vectors, and process independence. Selection
tests reject stale, mixed, unknown, and inactive-player submissions without
mutating state. Engine tests also cover public-position encoding, captured output
parsing, string ownership, and import safety. Registered `native` tests run the
capability probe, concrete adapter checks, and a complete engine-only game when
a setup manifest is available.

## Native integration spike (step 4)

Build and probe the pinned engine with the authored fixture dictionary:

```sh
.venv/bin/python scripts/bootstrap_magpie.py
.venv/bin/python scripts/probe_magpie.py
.venv/bin/python -m pytest
```

Setup requires Git, Make, and a C compiler. It verifies source/data identities
and asset hashes and records the local binary hash. The probe runs in a child
process with a 30-second deadline. Native tests skip with setup instructions
when `.native/manifest.json` is absent; set `SCRABBLE_MAGPIE_MANIFEST` to use a
manifest elsewhere. Other tests run without native setup.

The [spike report](docs/magpie-spike.md) records 13 verified native behaviors
and the implementation order for step 5. Public analysis was invariant under
private rack/draw mutations. Simulation needs explicit unknown-rack and minimum
sample controls; phony output must be rejected before commit. Native history
commits do not retain/refill CGP racks. The host must own complete state
transitions and draw-preserving checkpoints. The toy dictionary and zero leave
values establish interface behavior only.

## Full NWL2023 dictionary

After native bootstrap, install the full profile and play an engine-only game:

```sh
.venv/bin/python scripts/setup_nwl23.py
.venv/bin/python scripts/demo_engine_game.py
```

The setup verifies pinned MAGPIE-DATA assets and audits 196,601 distinct words
and 914,624 leave values. The saved [full-dictionary game](docs/nwl23-demo.json)
finishes in 24 turns with scores 404–426, including checkpoint recovery. This
is a static engine policy; LLM play remains future work. See the
[full dictionary guide](docs/nwl23.md) for explicit adapter settings and NASPA
licensing information. Dictionary data stays in ignored local directories.

## Concrete adapters (step 5)

Run the original fixture game, including checkpoint recovery:

```sh
.venv/bin/python scripts/demo_engine_game.py --manifest .native/manifest.json
```

It finishes 33 turns with scores 10–30 using the fixture dictionary. The
[adapter guide](docs/engine-adapters.md) includes an analysis/commit example,
supported rules, work units, cancellation behavior, and checkpoint semantics.
Generation and preview support late-game positions. Simulation currently
requires at least seven actual bag tiles and a full unknown opponent rack;
exact endgame solving remains future work.

## Limits and recovery (step 6)

Run the native acceptance checks:

```sh
.venv/bin/python scripts/verify_engine_recovery.py
```

Three restart games match uninterrupted baselines across all 64 compared states
and 61 selected actions. Repeated timeouts, cancellations, and process crashes
preserve later seeded results. The [limits and recovery guide](docs/engine-recovery.md)
explains optional `TraceJournal`, durable `CheckpointStore`, the arbiter's
`resume(game_id)` method, failure handling, and current boundaries.

The suite now passes 250 tests, including 25 native integration tests. The saved
[acceptance report](docs/engine-recovery-results.json) verifies 270 correlated
requests. Run-wide API/model/work/time accounting belongs to the future workflow
layer; this step enforces native per-request caps and recovery.

## Reference choices

The English board, rack, tile, and blank conventions follow the
[Hasbro rules](https://www.hasbro.com/common/instruct/Scrabble_(2003).pdf).
The scoreless-turn limit must be configured explicitly for the chosen ruleset.
Validation follows Pydantic's documented
[strict mode](https://docs.pydantic.dev/2.10/concepts/strict_mode/) and
[model validators](https://docs.pydantic.dev/2.10/concepts/validators/).
