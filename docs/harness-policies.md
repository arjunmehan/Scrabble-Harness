# Workflow presets and fallback policies (Task 9)

October 8, 2026 · Implemented · No paid provider requests

`harness/policies.py` loads packaged JSON workflows and chooses deterministic
fallback moves from the frozen decision context. The decision loop uses these
policies without changing game state or creating extra model/engine requests.

| Preset | Model attempts | Comparisons | Total rollout ceiling |
| --- | ---: | ---: | ---: |
| Fast | 1 | 0 | 0 |
| Standard | 2 | 1 | 300 |
| Deep | 3 | 2 | 600 |

These are ceilings, not targets. Every preset can select on its first request.
The final permitted request offers only `select_move`; repair and transport
retry consume the same attempt allowance. Comparison tiers still mean 60 or
300 total candidate rollouts, clipped to remaining shared work and time.

Each preset retains a 30-second decision deadline, a two-second finish reserve,
and a 30,000-microusd ($0.03) API ceiling. Campaign/run limits can reduce the
available resources further. Tool use still requires a verified final-input
bound and a durable selection hold. A preset does not promise that paid calls
will fit the dollar ceiling or that native analysis will finish every rollout.

## Configuration

```python
from scrabble_harness.harness import load_workflow

config = load_workflow("standard")
config = load_workflow("deep", fallback_policy="latest_comparison")
```

Names are lowercase and validated before resource lookup. Only the fallback
policy is an argument; the packaged attempt/comparison/work ceilings are checked
when loaded. Files live in `harness/presets/{fast,standard,deep}.json` and are
included in installed wheels. Results are frozen, strict `HarnessConfig` records.

Use the configuration when creating the run's execution manifest. Set the
existing `RunConfig.policy_version` to `config.workflow` and its
`candidate_config` to `config.candidate_config`; shared run limits must cover
the decision's declared ceilings. The manifest identity includes the complete
configuration, so changing fallback policy changes the experiment identity.
Running an existing persisted run uses its saved configuration.

## Fallback behavior

The default `static` policy uses the candidate selected and previewed during
preparation. It ignores comparison estimates. No new engine request is needed.

The optional `latest_comparison` policy scans issued batches newest first and
uses the first batch with estimates for every requested move. Eligible statuses
are `complete`, `work_limit`, and `timeout`; a timeout with full coverage still
contains estimates. Partial, failed, or cancelled batches are skipped. If no
usable batch exists, the policy uses the prepared static fallback.

Within the chosen batch, select the highest estimate. Exact ties use the
lexicographically smallest canonical candidate ID, independent of display and
request order. Estimates from different batches are never pooled or compared.
This policy considers only the moves in that batch; it does not claim to find
the best move from the entire candidate pool. It also does not turn uncertain
estimates into exact results.

```python
from scrabble_harness.harness import select_fallback

fallback = select_fallback(context, "provider_failed")
# fallback.decision: canonical AgentDecision with fallback_reason
# fallback.origin: "static" or "latest_comparison"
```

A comparison fallback cites exactly the chosen batch's evidence handle. Static
fallback has no evidence citation. The original failure remains both the
terminal reason and fallback reason in loop-produced outcomes. A validated
model selection ends immediately and retains origin `model`; fallback does not
replace it. Preparation failure and cancellation retain the loop's abort path.

## Durable provenance and compatibility

New loop-produced `DecisionOutcome` records carry `selection_origin`: `model`,
`static`, or `latest_comparison`; aborted outcomes have no selection origin.
Outcome validation checks status/origin agreement and that a comparison fallback
selects the deterministic winner from a usable cited batch. Persistence also
checks that a static-origin decision matches the prepared static fallback.

The optional field defaults to `None` and is omitted from canonical serialization
when absent. Old outcomes remain readable with unchanged canonical bytes and
hashes; no provenance is invented for historical records. SQL migrations 001/002
remain unchanged. Completed outcomes reopen with their frozen origin, reasons,
evidence, and usage, without new work.

## Verification

Task 9 adds 34 tests: 31 offline and three native. They cover packaged presets,
strict names, ceilings, early stop, final-selection tools, Fast rejecting native
work, both fallback policies, missing/partial/failed batches, newest-batch
selection, deterministic ties, invalid origin claims, persistence, and legacy
serialization. Native NWL23 runs exercise Fast, Standard's comparison fallback
after a refusal, and Deep's two 300-rollout comparisons. Each validates the move,
leaves the game checkpoint unchanged, and reopens the identical saved outcome.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/runs/test_policies.py tests/runs/test_policies_native.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

The complete suite passes 695 tests: 644 offline and 51 native. Package and
compatibility checks are saved in `work/harness-task9/validation.json`.
Task 10 implements the [offline single-position `decide` CLI](harness-offline-cli.md); real provider execution
remains Task 11.
