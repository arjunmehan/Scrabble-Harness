# Engine tests

Step 4 includes offline public-CGP/static-parser and string-ownership tests,
plus an isolated real-engine probe. It verifies generation, scoring, preview
isolation, sample floors, restricted simulation, hidden-state invariance,
seed repeatability, cancellation, errors, and native commit gaps.

Build with `scripts/bootstrap_magpie.py`. The native test uses
`.native/manifest.json` or `SCRABBLE_MAGPIE_MANIFEST`; it skips with instructions
when the default manifest is absent and fails if an explicitly configured
manifest is missing. It verifies artifact hashes before loading.

Step 5 adds concrete-adapter checks for scoring/crosswords, premium reuse,
blank exchanges, invalid and stale commits, actual simulation work, hidden
state invariance, both terminal conventions, checkpoint draw recovery,
cancellation, crash isolation, and a complete game. Native adapters are tested
against the pinned library rather than only test doubles.

Step 6 adds offline spawned fault processes for protocol limits, hard deadlines,
stop/completion races, shutdown, and request isolation. Filesystem tests verify
immutable revision uniqueness, corruption detection, and uncertain publication
recovery. Real-native tests cover failed validation, changed assets, storage
errors, concurrent commit/resume, three complete restart game pairs, and three
repeated cancellation/crash cycles. `scripts/verify_engine_recovery.py` writes a
compact acceptance report and uses temporary private checkpoint/trace storage.

Mark integration tests with `pytest.mark.native`; skip with a clear setup reason
when the configured engine or lexicon data is unavailable.

Full NWL23 coverage is separate from the golden fixture. Install it with
`scripts/setup_nwl23.py`; tests use `.native/nwl23/manifest.json` or
`SCRABBLE_NWL_MANIFEST`. Fifteen offline checks cover profile/data integrity;
three native checks verify word legality, nonzero leave effects, seeded bounded
analysis, offline installation, and a full game with checkpoint recovery.
