# Full NWL2023 dictionary

The project now has a full **NWL23** native profile, separate from the authored
20-word test fixture. NASPA identifies [NWL2023](https://www.scrabbleplayers.org/w/NWL2023)
as the official competitive lexicon for the United States and Canada, effective
February 29, 2024. The installed graph decodes to **196,601 distinct words**, all
2–15 letters, including 3,839 fifteen-letter words. These counts come from our
native export audit of the pinned graph.

## Run it

On this machine the profile is already installed. From the project root:

```sh
.venv/bin/python scripts/demo_engine_game.py
```

The demo selects `.native/nwl23/manifest.json` when present. Its saved
[full-dictionary game](nwl23-demo.json) finishes in 24 turns with scores
**404–426**, a rack-out ending, and checkpoint restoration at revision 3.
Both players choose the first statically ranked engine candidate; this verifies
full game integration and does not measure LLM performance.

For fresh setup after installing Python dependencies:

```sh
.venv/bin/python scripts/bootstrap_magpie.py
.venv/bin/python scripts/setup_nwl23.py
```

The second command reuses the built library and standard board/distribution
assets. It downloads approximately 8.4 MB from the pinned
[MAGPIE-DATA revision](https://github.com/jvc56/MAGPIE-DATA/tree/adf29316fcb2d7bd78a832e198c1bdfc112efa89/data/lexica),
verifies byte counts, SHA-256 hashes, and Git blob identities, then audits the
dictionary in a bounded native subprocess. It publishes the new data directory
only after validation. Repeating setup validates and reuses the installed
profile. No API key or paid model request is required.

For offline setup, provide the two verified upstream files in one directory:

```sh
.venv/bin/python scripts/setup_nwl23.py --asset-source /path/to/assets
```

That directory must contain `NWL23.kwg` and `NWL23.klv2` matching the records in
`configs/nwl23.lock.json`. `--base-manifest` and `--output` allow separate native
installations and profile locations; setup refuses to replace an incompatible
existing profile.

## Use it in code

Select the profile explicitly when constructing either adapter:

```python
from pathlib import Path
from scrabble_harness.engine import MagpieSettings

settings = MagpieSettings.from_manifest(Path(".native/nwl23/manifest.json"))
assert settings.lexicon_id == "NWL23"
```

Pass these settings to `MagpieAnalysisEngine` and `MagpieArbiterEngine`, and set
`GameConfig.lexicon_id=settings.lexicon_id`. Their generation, scoring, move
validation, and bounded simulations then use the full graph automatically.
Run async adapter examples from a Python file with a main guard, as shown in
the [adapter guide](engine-adapters.md).

The profile also installs **914,624 leave values**, of which 914,607 are nonzero.
These support static equity estimates for tiles retained after a move; they
replace the fixture's zero leave values. Simulated equity remains an estimate,
and the current adapter's simulation still requires at least seven bag tiles
and a full unknown opponent rack. Exact endgame solving remains future work.

The audited plain word list is available locally at
`.native/nwl23/data/lexica/NWL23.txt`. The engine uses the compiled `.kwg` graph;
there is no need to send the entire dictionary to an LLM or load it into each
prompt. The manifest records the exported text's hash as well as the binary
input hashes.

Lexicon identity and native asset contents enter the engine configuration
fingerprint. Fixture checkpoints cannot be restored with full-profile settings;
start a new NWL23 game. The loader rejects a manifest override that would
silently select a different lexicon.

## Reproducibility and tests

| Input | Pinned identity |
| --- | --- |
| MAGPIE engine | `375ad953e20d4edc582d345e233e8d8a2586cc06` |
| MAGPIE-DATA | `adf29316fcb2d7bd78a832e198c1bdfc112efa89` |
| NWL23.kwg SHA-256 | `3e74af981fdd974e107283f686da0fe4b7ec84ad0d825d444330c338c33b91ba` |
| NWL23.klv2 SHA-256 | `37dea945c29c3773eb4cd5a4117f3d3256c8b548cd3bf3b5ce8a219cb5e0a3fa` |

Run the full suite or just the new coverage:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/engine/test_lexicon_profiles.py tests/engine/test_nwl23_native.py
```

The new checks cover profile/hash integrity, rejection of incomplete exports
and invalid leave tables, JEDI/SCRABBLE acceptance, phony rejection with a blank,
candidate previews, nonzero static leave effects, repeatable bounded simulation,
offline profile installation, and a complete game with checkpoint recovery.
Full-profile native tests skip if it is absent; `SCRABBLE_NWL_MANIFEST` can select
a profile elsewhere, and a missing explicitly configured manifest is an error.

The original spike/recovery checks retain the small deterministic fixture. Run
its historical demo explicitly with:

```sh
.venv/bin/python scripts/demo_engine_game.py --manifest .native/manifest.json
```

## Dictionary distribution

The full word list and native data remain local under ignored `.native/` and
`work/` directories. Source code contains only setup logic, asset identities,
and tests. Availability in an upstream repository does not establish permission
to redistribute NWL data.

NASPA offers developer licensing for small apps and websites, with a fee of
zero while annual revenue remains below US$1,000. Contact NASPA to arrange the
appropriate terms before distributing the dictionary or publishing a service
that uses it; official text downloads are also available through Member
Services. See [NASPA licensing](https://www.scrabbleplayers.org/w/Licensing)
and [NWL2023 developer access](https://www.scrabbleplayers.org/w/NWL2023#Developer_Access).
