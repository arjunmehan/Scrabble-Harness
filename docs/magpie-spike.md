# Step 4: pinned MAGPIE integration spike

**MAGPIE supports candidate generation, move scoring, and bounded midgame
simulation on this M4 Mac. A complete arbiter needs host-owned game and draw
state.** This step supplies reproducible setup, an internal C binding, a limited
public-position codec, captured output, and executable capability checks.
Step 5 still implements the project-facing engine protocols and workers.

Local verification: 157 tests passed with native setup enabled, all 13 native
checks passed, the wheel built successfully, and native generation also passed
from the installed wheel. Offline verification passed with the native test
skipped when no setup manifest was present.

## Reproduce

From the project root, after the README's Python setup:

```sh
.venv/bin/python scripts/bootstrap_magpie.py
.venv/bin/python scripts/probe_magpie.py
.venv/bin/python -m pytest
```

Setup needs Git, Make, and a C compiler (Xcode Command Line Tools on macOS).
It fetches an exact source commit, builds `libmagpie`, downloads three
hash-verified data assets, converts the authored dictionary to KWG, and creates
a zero-valued English KLV. It does not run upstream `setup.sh`. Fixture
conversion and probing run in child processes with deadlines. The probe defaults
to 30 seconds and kills a child that exceeds it.

`--engine-source` reuses a clean checkout at the exact pin; `--asset-source`
supplies a local data root for offline setup. Setup cleans that checkout's build
outputs before compiling. `--output` selects the setup directory (default
`.native/`). The manifest records paths, compiler, architecture, source/data
identities, and binary/fixture hashes. The probe rechecks artifact hashes.

To use another setup directory in tests:

```sh
SCRABBLE_MAGPIE_MANIFEST=/absolute/path/manifest.json .venv/bin/python -m pytest -m native
```

Without a setup manifest, the native test skips with setup instructions; the
remaining suite runs offline. The saved [result](native-spike-results.json)
contains raw output and measured evidence. Paths/timings describe this machine;
rebuilding produces a new local manifest and report.

## Pins and data

| Dependency | Identity |
|---|---|
| MAGPIE | `375ad953e20d4edc582d345e233e8d8a2586cc06` |
| MAGPIE-DATA | `adf29316fcb2d7bd78a832e198c1bdfc112efa89` |
| Build | `make -j4 libmagpie`, board 15, rack 7 |
| Verified platform | arm64, macOS 26.6.2, Apple Clang 21 |
| Fixture | `NWL_HARNESS_SMOKE`, 20 authored words, zero leaves, WMP disabled |

The [lock file](../configs/magpie.lock.json) pins board layout, English tile
distribution, win-probability table, and fixture SHA-256. Runtime version is
`0.0.0` at this pin; use commit and binary hash for provenance. MAGPIE's source
license is GPLv3. The fixture prefix selects English defaults; it is not NWL.
These checks verify interfaces, **not playing strength or representative values**.

## Verified behavior

The executable probe has 13 assertions, including these findings:

| Operation | Observed result | Adapter requirement |
|---|---|---|
| Generation | Ten candidates parsed from native output | Pin the format; translate notation to domain moves |
| Preview | QI scores 22; blank-q I scores 2; RETINAS scores 64 | Reload a scratch position before `addmoves`; preview leaves board unchanged |
| Exchange | `ex AE?` displays `(exch AE?)`, score zero | Preserve physical blanks and exchanged multiset |
| Restricted simulation | Three added actions, 12 total samples | Fresh CGP resets the list; do not generate extra moves first |
| Sample limits | Default 500/play turns requested 12 into 1,500 | Explicit `-minp 1`; reject limits below candidate count |
| Reproducibility | Independent handles return identical quantitative results | Fixed seed, one native thread, fixed candidates/configuration |
| Cancellation | User-interrupt result, joined thread, reusable handle | Only status/stop while running; await before output or new commands |
| Errors | Invalid CGP preserves board; creation failures surfaced | Clear errors, free returned strings, close handles |
| Dictionary | QA flagged but still added and committed, exit code zero | Reject any phony report before authoritative commit |
| Native commit | Legal QI changes score but leaves both CGP racks empty | Host owns complete transitions; native commit is history-oriented |

An iteration is **one candidate rollout sample**, summed across plays. The small
probe allocates 6, 5, and 1 samples, rather than 12 per action. Machine output
provides win percentage/equity, standard errors, static equity, sample counts,
and per-ply statistics. These are approximate values. A zero error at one sample
does not establish certainty. Interrupted runs must retain incomplete status;
an interrupt before any work can return only a static list.

## Public information boundary

The spike codec accepts active, ongoing standard-English observations with
seven opponent tiles and at least seven actual bag tiles. It rotates the observer
to native player zero and preserves lowercase assigned blanks. It rejects a
private `GameState` as input. Example requests:

```text
cgp 15/15/15/15/15/15/15/15/15/15/15/15/15/15/15 ?AEIQRT/ 0/0 0
addmoves 8H QI,8H qI,pass
simulate - -it 12 -minp 1 -plies 2 -scond none -threads 1 -seed 42
```

The empty offturn rack represents unknown tiles. Native CGP initially puts all
93 unseen tiles in its bag; simulation samples the opponent rack from them.
Explicit `simulate -` also prevents use of a stored offturn rack: the probe
confirms that omitting `-` on a private position uses BCDFGHJ.

The leakage check changes actual opponent rack, bag composition, draw order,
and private RNG while holding the public observation fixed. It verifies
identical IDs, native requests, and quantitative seeded results across fresh
handles. No authoritative private state enters analysis.

For smaller bags, empty-rack CGP could falsely allow exchange because its native
bag includes the unknown rack. This codec rejects those positions. Step 5 must
gate exchanges using actual public bag size. Partial unknown racks need explicit
support. At bag-empty positions, derive the opponent rack from public unseen
inventory before endgame solving. This probe does not verify endgame solving.

## Output and memory ownership

At this pin, machine output is a headerless numbered table with a footer, rather
than older `info ...` UCGI examples. `-shwithmoves false` prevents Unicode board
interleaving. Lowercase denotes blanks; parentheses mark existing board letters.
`shgame` remains a display command.

The binding imports ctypes only on explicit session construction. It preserves
returned pointers, copies strings, and frees them with `magpie_free_string` in
`finally`; failed construction closes the handle. It guards output/new commands
until async await, and requires one owning thread and an external deadline.
Explicit empty settings files avoid ambient configuration.

The static parser rejects human, simulation, phony, truncated, filtered, and
changed formats. It preserves notation and does not yet implement complete
`PlacementMove` translation. The probe's simulation parser assumes two plies
and default disabled blended utility; it is not the production analysis parser.

## Step 5 implementation order

1. Finish the board-aware move codec: existing letters, blanks, exchanges,
   derived leaves. Verify crosswords and premium-square reuse against native scores.
2. Add a spawned worker with serialized requests, deadlines, cooperative stop,
   forced termination, and restart; separate analysis and arbiter contexts.
3. Implement `AnalysisEngine` from public observations: generation, scratch
   previews, and simulation restricted to supplied IDs. Gate phonies, small-bag
   exchanges, unsupported states, and actual work limits.
4. Implement `ArbiterEngine` with host-owned `GameState`. Use MAGPIE to validate
   legality/score; apply accepted placements, racks, refills, exchanges, scores,
   and history in one revision-checked host transaction. Do not use history
   `commit` as a complete native game transition.
5. Own seeded bag order and full RNG state in the host. Checkpoint private state
   plus engine/configuration identities and reconstruct native scratch contexts
   on restore. Prove identical subsequent placement and exchange draws after a
   worker restart. CGP omits bag order/RNG; GCG is game history. The
   [pinned C API](https://github.com/jvc56/MAGPIE/blob/375ad953e20d4edc582d345e233e8d8a2586cc06/src/impl/cmd_api.h)
   has no full private snapshot function.
6. Finish a local engine-only game and verify final scoring, stale commits,
   crash recovery, cancellation, and conservation before paid LLM experiments.

Primary references: pinned
[configuration/commit code](https://github.com/jvc56/MAGPIE/blob/375ad953e20d4edc582d345e233e8d8a2586cc06/src/impl/config.c),
[sampling](https://github.com/jvc56/MAGPIE/blob/375ad953e20d4edc582d345e233e8d8a2586cc06/src/impl/random_variable.c),
and [simulation output](https://github.com/jvc56/MAGPIE/blob/375ad953e20d4edc582d345e233e8d8a2586cc06/src/str/sim_string.c).
