# Offline single-position command (Task 10)

October 11, 2026 · Implemented · Real NWL23 analysis; no provider network calls

The `decide` command builds a seeded, reachable opening position, prepares a
frozen candidate pool, runs a scripted model through the bounded loop, and
returns a canonical selection or recorded fallback/abort. It persists private
records, validates the selected move against the original state, and confirms
that the game checkpoint is unchanged. This is an acceptance tool, not a model
strength result or a full-game runner.

## Run it

From the project directory with the existing environment and NWL23 profile:

```sh
.venv/bin/python -m scrabble_harness decide \
  --database work/offline/private/runs.sqlite3 \
  --workflow standard \
  --output work/offline/report.json
```

The default native manifest is `.native/nwl23/manifest.json`, relative to the
working directory. Supply `--manifest /absolute/path/manifest.json` elsewhere.
The installed wheel also provides `scrabble-harness decide`.

`--database` is required. Its parent must satisfy the existing private-store
rules; a new `private` directory is created with mode 0700 and the database with
0600. `--output` also creates a 0600 JSON file and refuses to overwrite an
existing path or use the database path. JSON is printed to stdout whether or not
an output file is requested. The report is a local host artifact, including the
trusted pool and native identities; it is not the future public export format.

Each invocation gets a fresh run ID unless `--run-id` is supplied. An existing
run ID is rejected before starting native workers. Runs share a durable campaign
named `offline` by default; `--campaign-id` selects another explicit campaign.
Repeated runs do not reset that campaign's budget or elapsed clock. Its fixed
offline caps are 100 model calls, 10,000 rollouts, 1,000,000 synthetic microusd,
and 3,600 seconds. A depleted/expired campaign can stop a new decision.

## Workflows and scripted scenarios

`--workflow fast|standard|deep` selects the packaged presets. Default
`compare-select` selects immediately in Fast, runs compare/select in Standard,
and two comparisons/select in Deep. Each comparison uses the standard tier,
clipped by actual remaining work/time. Reduced budgets can shorten the script.

| `--scenario` | Behavior |
| --- | --- |
| `compare-select` | Normal bounded compare/select sequence |
| `select` | Immediate valid selection |
| `refusal` | Refused replies; bounded repair then fallback |
| `malformed` | Invalid first arguments, then a valid repair selection |
| `forged-evidence` | First selection cites unissued `e99`; repaired within the same budget |
| `missing-usage` | First reply has unknown usage; full admitted cost remains unresolved |
| `transport-failure` | No reply; conservative unknown charge and fallback |
| `timeout` | First reply delayed 60 seconds; caller timeout retains unknown cost |

Use `--model-timeout-seconds 0.02` with `timeout` for a quick timeout check.
`--fallback-policy static|latest_comparison` chooses the existing fallback policy.
All script replies are saved and accounted for before tool execution. Neither
the CLI nor scripted provider imports a real provider SDK or sends API traffic.

The report explicitly labels **synthetic billing**. Rates and token usage are
offline test fixtures, not a current provider price or an invoice. Nonzero
settled/unresolved microusd exercise accounting; `paid_provider_requests` is
always zero. Request bounds cover the entire normalized record, schemas, and
framing under the offline text-only contract. They are not production token
counters. Real pricing, complete transport counting, and continuation support
belong to Task 11.

## Acceptance controls

These flags may only reduce the preset's ceilings:

- `--max-model-attempts`
- `--max-tool-steps`
- `--max-engine-work-units`
- `--max-api-cost-microusd`
- `--max-elapsed-seconds`

For example, `--max-api-cost-microusd 0` prevents model admission, and
`--max-engine-work-units 13` runs a comparison within that total-rollout limit.
Very short elapsed caps can abort before preparation yields a verified fallback.
The finish reserve and native tier timeout are clipped to reduced deadlines.
Game construction precedes the decision deadline; native candidate preparation
and every model/tool dispatch use the decision's single persisted deadline.

Seeds default to game 42, analysis 73, and candidate order 123. Override with
`--game-seed`, `--analysis-seed`, and `--candidate-seed`. Native manifests verify
binary/assets; the execution manifest records actual packaged source/config
hashes, engine identity, lexicon hash, configuration, seeds, dependency versions,
and scripted scenario. The dictionary and hidden bag/opponent rack are not
embedded in the report.

## Result and persistence

`OfflineDecisionReport` contains the run/campaign/manifest identity, scenario,
lexicon, durable `DecisionOutcome`, validated canonical candidate, resource
totals, and unchanged-game/validation flags. Confirmed and unresolved usage stay
separate. Before returning, the command checks that no allocation remains
reserved/dispatched, final holds are released/consumed, outcome usage matches
the ledger (including costs), and database integrity holds.

Successful selections and expected fallbacks exit 0. An aborted decision emits
its JSON report and exits 1. Invalid inputs, persistence/engine failures, or
output-write failures exit 2. Interrupts exit 130; the loop retains its durable
abort/accounting policy. If report writing fails after a decision completes, the
private database still contains that completed outcome. Saved runs are never
automatically replayed.

The command currently creates seeded openings; loading arbitrary positions,
checkpoint resume, real API calls, game commits, and tournaments remain later
tasks. The Python entry point is `offline.decide_offline(...)`; `cli.py` provides
argument parsing, output, and exit status, while existing harness/engine modules
retain decision and game authority.

## Verification

Task 10 adds 36 tests: 21 offline and 15 native. Acceptance invokes the actual
module command for every workflow, scenario, and resource ceiling, reopens saved
records, checks private output permissions, and rejects replay/unsafe output.
Additional offline tests verify override validation, script routing, source
fingerprinting, help without SDK/native imports, and controlled input errors.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_offline_cli.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

The full suite passes 695 tests: 644 offline and 51 native. A real compare/select
report is saved at `docs/offline-decision-demo.json`; its private SQLite record,
wheel, and validation checks are in `work/harness-task10/`. No paid calls or new
dependencies were used. Task 11 implements the real provider adapter.
