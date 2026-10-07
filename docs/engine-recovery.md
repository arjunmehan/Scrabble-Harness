# Step 6: native limits, tracing, and durable recovery

The engine foundation now handles bounded requests and durable restart games.
This step adds a stricter worker protocol, optional diagnostic traces, immutable
private checkpoints, and repeatable fault/recovery acceptance checks. Existing
adapter constructors and async interfaces continue to work.

## Run the checks

After native bootstrap, from the project root:

```sh
.venv/bin/python scripts/verify_engine_recovery.py
.venv/bin/python -m pytest
.venv/bin/python -m pytest -m 'not native'
```

The [saved acceptance report](engine-recovery-results.json) compares three
uninterrupted games with three games restored from disk. Seeds 7, 42, and 73
finish in 12, 33, and 16 turns. All **64 states**, including the initial states,
and all **61 selected actions** match their respective baselines. Comparison
includes board, both racks, future bag order, full exchange RNG, scores, history,
and termination. There are seven midgame restores across the three games.

Three additional stress cycles each exercise a native timeout, cooperative
cancellation, asyncio task cancellation, and forced process crash. After each
cycle, seeded analysis matches the uninterrupted result. The report checks all
270 request IDs against exactly one terminal request event. Timing-dependent
interrupted sample counts can vary across executions.

The authored 20-word dictionary and zero leave values verify engine interfaces;
these games do not measure Scrabble strength.

## Worker protocol and limits

Each request/reply has a versioned, validated JSON envelope and a random request
ID. The parent accepts only a matching reply. Startup binds the worker to the
configured native fingerprint. IPC sends bytes, with a 16 MiB message ceiling;
it no longer sends pickled request objects. Native output strings have a separate
8 MiB character ceiling. Invalid, oversized, unrelated, or incomplete preview
replies retire the child. No private state or checkpoint fits the request schema.

Operation-specific validation happens before spawning or dispatch:

- Generation accepts a bounded pool, with no supplied moves or simulation limits.
- Preview accepts exactly one move and no simulation settings.
- Analysis accepts distinct moves, a supported work unit, at least one sample
  per action, and a work cap within the native signed-integer range. Simulation
  still requires at least seven actual bag tiles and a full unknown opponent rack.

Parent deadlines bound startup and native preparation/execution. Analysis first
requests cooperative stop, then kills a child that exceeds its stop grace.
Timeout/cancel processes are retired even when they return usable output, so
late stop packets or extra replies cannot affect a later request. A cancelled
queued call does not terminate the active call. Closing rejects queued work and
cleans up the child, including when the close task itself is cancelled during join.

Queue wait is excluded from the per-operation deadline. Host serialization,
result translation, cooperative-stop grace, and bounded process cleanup can
extend return latency; analysis reports elapsed time and incomplete status.
This is a watchdog, not a real-time scheduling guarantee or a native memory cap.
Generation/preview interruption without usable completion raises an engine error.

Analysis validates returned candidate identities, scores, and total reported
work against the request. `candidate_rollout` means one completed rollout for
one candidate, summed across the set. A sample-cap result is an estimate with
`work_limit` status. Killed work without a usable report stays unresolved; it is
not fabricated as zero work or cached as a successful analysis.

## Optional traces

Share one `TraceJournal(path, run_id=...)` between an analysis and arbiter adapter
through their `trace=` argument. Tracing is opt-in. Every event contains run,
worker, configuration, sequence, timestamp, and role. Request events add game,
revision, observation, operation, and request IDs. Parsed analysis events record
actual reported work, elapsed time, and completion status. Worker lifecycle events
record starts and retirement reasons.

The schema has no arbitrary payload field. It omits racks, bag contents, RNG,
move tiles, raw native output, native error messages, checkpoints, and credentials.
These compact traces are diagnostics rather than complete decision replay logs.
Future model/workflow logging will need additional records with decision IDs.

A valid journal can resume with continuous sequence numbers. A corrupt/torn
journal is rejected; it does not silently drop records. Use a new diagnostic file
if necessary. One process owns the journal; adapters in that process share the
same instance. It does not coordinate independent writers. If a later trace
write fails, engine cleanup and commits still proceed; inspect the adapter's
`trace_failure_count`. Diagnostic logs are separate from durable checkpoints.

## Durable private checkpoints

Configure `CheckpointStore(root, settings)` using a local filesystem supporting
hard links and directory fsync, then pass it as `checkpoint_store=` to the arbiter.
The arbiter persists its initial state and every accepted turn before publishing
that state in memory. This small synchronous persistence section has no asyncio
cancellation point between durable publication and the in-memory assignment.

Each game has a hashed directory name and one immutable JSON record per revision.
A temporary file is flushed and fsynced, atomically linked to its destination,
and followed by directory fsync. New checkpoint files have mode 0600 and new
private directories use 0700. Records contain both racks, bag order, RNG, and
seed/configuration: keep the store private.

Saving the same content is idempotent. Different content at an existing game
revision raises `CheckpointConflictError`. This provides revision uniqueness;
it does not provide a multi-process run lease. Use one active arbiter per game.
Unused temporary files are ignored on recovery. Corruption in the newest saved
revision fails explicitly instead of silently reverting to an older state.

`await arbiter.resume(game_id)` validates and loads the newest saved revision
under the arbiter's turn lock. It cannot roll back a commit completed while the
resume call was queued. All restore paths recheck checkpoint identity, rules,
physical inventory, complete RNG format, and the native fingerprint. Checkpoint
hashes bind content; they do not authenticate imported board history.

A storage error before publication leaves the in-memory game unchanged. An error
after atomic publication but before fsync acknowledgement is an uncertain durable
outcome. Inspect/resume the stored revision before continuing. Retrying the exact
same state is content-idempotent; a changed move cannot overwrite that revision.
There is no await between successful persistence and memory publication, although
an abrupt host crash can still occur there. Recovery uses the saved revision and
rejects a commit with the previous revision.

## Example

Save this as a Python file; spawned workers require the main guard:

```python
import asyncio
from pathlib import Path
from scrabble_harness.domain import CandidateConfig, GameConfig, RulesetConfig
from scrabble_harness.engine import (
    CheckpointStore, MagpieAnalysisEngine, MagpieArbiterEngine,
    MagpieSettings, TraceJournal,
)


async def main():
    settings = MagpieSettings.from_manifest(Path('.native/manifest.json'))
    store = CheckpointStore(Path('private/checkpoints'), settings)
    trace = TraceJournal(Path('logs/example.jsonl'), run_id='example')
    arbiter = MagpieArbiterEngine(settings, checkpoint_store=store, trace=trace)
    analysis = MagpieAnalysisEngine(settings, trace=trace)
    try:
        state = await arbiter.start_game(GameConfig(
            game_id='example', seed=42, lexicon_id=settings.lexicon_id,
            ruleset=RulesetConfig(ruleset_id='english-six-scoreless-v1', scoreless_turn_limit=6),
        ))
        await analysis.load_observation(await arbiter.observe(state.active_player))
        pool = await analysis.generate_candidates(CandidateConfig())
        state = await arbiter.commit(pool[0].move, expected_revision=state.revision)
    finally:
        await analysis.close()
        await arbiter.close()

    resumed = MagpieArbiterEngine(settings, checkpoint_store=store, trace=trace)
    try:
        state = await resumed.resume('example')
        print(f'Resumed revision {state.revision}')
    finally:
        await resumed.close()


if __name__ == '__main__':
    asyncio.run(main())
```

Use a fresh game ID for a new game and `resume` for an existing game.

## Validation and remaining scope

Step 6 validation passed **232 tests**: 210 offline and 22 native integration tests. Native tests
include the existing 13-check capability probe. New coverage exercises oversized
and unrelated replies, ambiguous responses, unresponsive children, startup
limits, stop/completion races, queued cancellation, shutdown, trace failures,
competing checkpoint writers, corrupt snapshots, storage failures on either side
of publication, stale commits after resume, changed native assets, and a crash
during real native validation. A concurrent commit/resume test guards the lock race.

The built wheel also passed an installed-package smoke check outside the source
tree: native generation, 30 reported samples, durable resume, identical future
exchange draws, stale revision rejection, and typed trace output. The parent
never imported ctypes. No paid API calls were made.

This completes the limits/recovery step for the native engine foundation. Exact
endgame solving, full decision traces/result caching, provider calls, and the
four-dimensional aggregate run ledger remain later harness work. Native two-ply
estimates still use the pinned engine's rollout termination convention, including
its zero-point-placement difference from the host rules described in the
[adapter guide](engine-adapters.md). Power-loss durability is not established by
a real machine outage; fsync boundary failures are tested through fault injection.
