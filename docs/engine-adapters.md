# Step 5: concrete engine adapters

`MagpieAnalysisEngine` implements public analysis; `MagpieArbiterEngine`
implements authoritative game operations. Each owns a separate spawned worker.
Only children load native libraries. Both implement the existing async protocols.

## Run

After Python setup and native bootstrap, from the project root:

```sh
.venv/bin/python scripts/demo_engine_game.py --manifest .native/manifest.json
.venv/bin/python -m pytest
```

The saved [trace](engine-demo.json) records a deterministic 33-turn game with
scores **10–30** and a scoreless ending. The demo closes the arbiter at revision
3 and restores a checkpoint into a new instance before continuing. Per-turn
observations are public; the trace also contains replay metadata including the
game seed and is not a decision-time payload. The 20-word dictionary and zero
leave values remain interface fixtures, not a strength benchmark.

The [full NWL2023 profile](nwl23.md) is now available. Installing it makes the
demo use NWL23 by default; select `.native/nwl23/manifest.json` explicitly in
adapter code. The fixture command above retains the historical result.

## Example

Save as a Python file; the main guard is required for spawned workers:

```python
import asyncio
from pathlib import Path
from scrabble_harness.domain import AnalysisLimits, CandidateConfig, GameConfig, RulesetConfig
from scrabble_harness.engine import MagpieAnalysisEngine, MagpieArbiterEngine, MagpieSettings, WORK_UNIT


async def main():
    settings = MagpieSettings.from_manifest(Path(".native/manifest.json"))
    arbiter, analysis = MagpieArbiterEngine(settings), MagpieAnalysisEngine(settings)
    try:
        state = await arbiter.start_game(GameConfig(
            game_id="example", seed=42, lexicon_id=settings.lexicon_id,
            ruleset=RulesetConfig(ruleset_id="english-six-scoreless-v1", scoreless_turn_limit=6),
        ))
        observation = await arbiter.observe(state.active_player)
        await analysis.load_observation(observation)
        pool = await analysis.generate_candidates(CandidateConfig(pool_size=20, display_count=8))
        result = await analysis.analyze(
            tuple(candidate.candidate_id for candidate in pool[:3]),
            AnalysisLimits(max_work_units=100, work_unit=WORK_UNIT, max_elapsed_seconds=5.0),
            seed=73,
        )
        print(result.model_dump_json(indent=2))
        # Inspect completion and sampled values; use a legal fallback if needed.
        if result.values:
            best = max(result.values, key=lambda value: value.value)
            selected = next(c for c in pool if c.candidate_id == best.candidate_id)
        else:
            selected = pool[0]
        state = await arbiter.commit(selected.move, expected_revision=state.revision)
        checkpoint = await arbiter.checkpoint()
        Path("private-checkpoint.json").write_text(checkpoint.model_dump_json(indent=2))
    finally:
        await analysis.close()
        await arbiter.close()


if __name__ == "__main__":
    asyncio.run(main())
```

## Boundaries and budgets

Worker requests contain only a validated `PlayerObservation`, operation, and
optional moves/limits. The schema cannot represent private state or checkpoints.
Even arbiter legality checks use the active player's public projection. Each
request gets a fresh handle and scratch position, preventing stale move lists,
results, or history inference. Configuration fixes one thread, disables
inference/blended utility, and rejects ambient settings. Simulation explicitly
samples unknown racks.

Settings fingerprint the library, lexicon, leaves, board, distribution,
win-probability table, and adapter configuration. Workers recheck inputs before
execution; changed or missing files fail explicitly. Handles stay in children;
asynchronous IPC keeps waits off the event loop. Requests are serialized.

Parent deadlines cover worker startup and native preparation/execution. Analysis
requests cooperative stop, then kills an unresponsive child after the configured
grace period. Cleanup and result translation can extend return latency beyond
the requested limit; elapsed time and completion status report that. Queue wait
is excluded. A forced termination without usable output raises
`EngineTimeoutError`. `await analysis.cancel()` retains usable interrupted
estimates; asyncio task cancellation propagates `CancelledError` and terminates
native work. Interrupted processes are retired so late replies cannot affect
the next request. Crashed workers restart lazily on the next operation.

`WORK_UNIT` is `candidate_rollout`: one completed rollout for one candidate,
summed across the requested set. Caps must allow one sample per candidate.
Analysis adds only requested actions, sets `-minp 1`, and verifies returned
counts against the cap. The metric is **estimated two-ply equity in points**
from the observing player's perspective. Reaching the sample cap returns
`work_limit`, not an exact result. Fewer than two samples yield no standard-error
claim. Loading an observation clears candidate IDs; unknown/stale/duplicate IDs
are rejected. Single-thread seeded repeatability and hidden-state invariance are
tested through this concrete adapter.

## Moves and candidate policy

Native coordinates, played-through letters, blank assignments, exchanges, and
passes translate to domain moves containing only new tiles. Equivalent native
orientations deduplicate by action ID. Physical inventory supplies truthful
leaves and verifies ownership. Dictionary warnings are rejected even when the
native exit code is zero. Two pinned display quirks are handled: addmoves-only
contexts can omit leaves, and exchange leaves incorrectly retain exchanged
blanks. Neither display quirk changes host inventory.

`pool_size` caps the returned pool. Native generation examines its top static
pool; filtering can return fewer moves and does not prove exhaustive coverage.
`static-diverse-v1` reserves a requested pass and one generated exchange when
space permits, then fills by native static rank. `static-top-v1` reserves only
the requested pass. Pool size one with pass enabled returns the pass fallback.
`display_count` is for downstream presentation; the adapter returns the full pool.

Generation and previews support smaller bags and bag-empty positions. Exchanges
use the actual public bag size; at bag empty, the native opponent rack is derived
only from public unseen inventory. **Simulation requires at least seven bag
tiles and a full unknown opponent rack; exact endgame solving is not implemented.**
Native rollouts use MAGPIE's termination conventions, including resetting the
counter on zero-point placement, which differs from the host's six-scoreless-turn
rule. The metric is a pinned native estimate, not exact host-game minimax value.

## Authoritative turns and checkpoints

Supported rules are the standard English 100-tile set, exchange minimum seven,
and six consecutive zero-score turns. MAGPIE supplies legality and move scores.
Under one lock, the arbiter checks revision, validates, applies board/racks,
draws, scores, and history, then publishes one validated state. Rejected or
interrupted validation cannot partially publish a turn. Two simultaneous commits
against one revision yield one success and one stale-revision error.

The host shuffles a seeded bag and draws from its front. Exchanges draw before
returning tiles, then shuffle remaining and returned tiles. Checked JSON stores
full MT19937 state; checkpoints preserve both future bag order and exchange RNG
behavior. Rack-out transfers the opponent's remaining rack points; scoreless
ending deducts each player's rack points. The last public turn records final scores.

Checkpoint identity binds canonical private state, game/rule configuration, and
engine fingerprint. Restore validates these fields, RNG format, and identity
before replacing state. Legacy records without configuration are readable domain
models but rejected by the adapter. Checkpoints are bound to the tested build;
other builds need migration. No native history-commit or bag snapshot is used.
`make_checkpoint(state, config, settings)` supports trusted position import:
content binding does not authenticate provenance or adjudicate board history.
Keep these private records out of decision-time consumers.

## Validation and next work

At completion of step 5, validation passed all 190 tests, including 15 concrete-adapter native
scenarios and the 13-check step-4 probe. The complete demo game exercises
placements, exchanges, scoreless termination, and midgame checkpoint recovery.
The 174 offline tests also pass without running native scenarios. The wheel
built and its installed adapters passed generation, commit, 30-sample analysis,
and draw-preserving exchange recovery without importing ctypes in the parent.

Tests cover coordinates, blanks and exchange leaves, crosswords, premium reuse,
phonies, actual work counts, seeds, leakage, cancellation, crashes, stale commits,
both endings, placement/exchange recovery, and a complete engine-only game.
Late-game inventory fixtures are labeled synthetic transition tests, not
benchmark positions.

Step 6 has now completed worker hardening, optional tracing, durable revision
checkpoints, and repeatable stress/recovery checks. See the current
[limits and recovery guide](engine-recovery.md). Exact endgame analysis and
benchmark position corpora remain future work.
