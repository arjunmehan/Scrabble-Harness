# Typed tool execution (Task 7)

October 7, 2026 · Implemented · Real NWL23 checks; no paid provider requests

`harness/tools.py` connects saved model actions to the frozen decision context,
resource ledger, and existing analysis adapter. `select_move` returns a canonical
`AgentDecision`. `compare_moves` runs bounded native analysis and returns saved,
host-issued evidence. Neither tool commits a turn or calls a model.

## API and ownership

```python
from scrabble_harness.harness import ToolExecutor

executor = ToolExecutor(analysis, ledger)
result = await executor.execute(attempt_id)
```

`attempt_id` identifies a reply already saved and accounted for through
`BudgetLedger.settle_model`. The executor reads its action from `RunStore`;
there is no alternate path accepting model-supplied candidate pools, native
parameters, arbitrary evidence, or a caller-replaced reply.

The caller owns the analysis adapter, including loading it during candidate
preparation and closing it afterward. Use one adapter for one active decision
context; do not reload or regenerate its candidate registry during execution.
Overlapping calls on one executor fail before another tool can be admitted.
The [decision loop](harness-loop.md) (Task 8) owns retries, repair, fallback, and termination.

| Record/module | Purpose |
| --- | --- |
| `SelectionToolResult` | Attempt/call IDs and a canonical decision; no game mutation |
| `ComparisonToolResult` | Attempt/call/reservation IDs, host evidence, public evidence view, bounded canonical JSON content |
| `ComparisonRequest` in `contracts.py` | Durable intent: scoped action, visible/canonical bindings, tier, seed, reserved limits |
| `ReservationRecord.native_dispatch_limits` | Exact clipped limits actually passed to the engine |
| `ReservationRecord.native_result` | Complete durable native receipt, including reported work and completion |

These records remain provider-neutral and use the existing strict, frozen
Pydantic policy. Importing tool execution loads no SDK, native worker, or library.

## Validation before work

Execution requires an active prepared decision/run and exactly one action in a
saved `completed` reply. A refused, incomplete, failed, missing, zero-action, or
multiple-action response is not executable. The corresponding model reservation
must already be settled or conservatively unresolved; invalid actions cannot
erase its recorded cost or call slot.

The executor checks the persisted public view and evidence prefix, then parses
arguments against the tools/schema version offered in that saved request.
Unknown tools, extra native-budget arguments, malformed JSON, excessive or
repeated candidate handles, and a comparison returned on a selection-only
request fail before native dispatch. Candidate handles resolve only through the
current host-owned visible map.

Selection must cite evidence both issued by the host and displayed in that
specific model request. Evidence issued later cannot justify an older reply.
Comparison uses the context's configured tier and phase eligibility; simulation
is unavailable with fewer than seven bag tiles, an opponent rack shorter than
seven, or insufficient visible candidates. The prompt builder omits comparison
in those phases, and execution also validates the context.

## Comparison limits and execution order

The default `light` tier allows 60 **total** candidate rollouts; `standard` allows
300 total rollouts. These are not per-candidate counts. The executor clips a tier
to the remaining campaign/run/decision work and time. If the work cannot cover
at least one sample per requested candidate, it rejects the request. The
configured decision tool-step cap is checked transactionally by the ledger.

The native seed comes from the saved execution manifest, not model arguments.
Before external work, the executor:

1. Binds the saved attempt/call to resolved candidate identities and limits.
2. Reserves work and one tool step through the ledger. An active finish hold
   remains required when model attempts are enabled.
3. Commits the native dispatch marker and saves the exact clipped engine limits.
4. Calls `analysis.analyze(candidate_ids, limits, seed)` outside all SQL
   transactions, with an outer deadline that protects final-selection time.

The engine's shorter tier timeout can return partial results and reported work.
The outer decision deadline also bounds native stop/cleanup. Cancellation or
failure without a receipt retains the full allowance; cleanup may exceed the
scheduling deadline, as with the existing engine adapter.

## Receipts, evidence, and repeat calls

Known usage is settled and saved **before** evidence validation. The context
then checks observation, candidates, seed, metric/work unit, completion, and
allowances. A wrong seed or unrequested candidate never becomes evidence, but
known work remains recorded. Reported work beyond its allowance is retained
truthfully and blocks further admission through the ledger's overrun flag.
Wrong-scope or unsupported-unit work remains conservatively unresolved.

On a valid result, evidence is appended to the saved context and projected into
an `EvidenceView`: model-visible handles and estimates, metric definition,
limits, actual work, seed, elapsed time, and completion status. Native candidate
IDs, static rankings, and fallback markers are not included in that view.
`render_comparison_result` enforces the configured result byte ceiling without
truncating facts. If rendering exceeds the ceiling, the receipt and full issued
evidence remain durable, while the tool raises a presentation error.

A reservation ID deterministically binds decision, attempt, and call identities.
A repeated invocation with a settled receipt reuses it and returns the same
issued evidence without another engine call or tool charge. It can also finish
an evidence write interrupted after receipt settlement. Existing allocations
without a saved receipt are never automatically dispatched again. Released,
pending, or unresolved work requires explicit recovery policy in the later
runner; tool execution fails rather than replaying it.

Repeat receipt inspection works after reopening, even if elapsed allowance has
expired. It authorizes no new work. Completing the decision and reconciling
full game checkpoints remain the loop/runner's responsibility.

## Selection and the next request

A `SelectionToolResult` contains the chosen canonical candidate identity,
workflow, cited evidence handles, and note. It leaves the decision prepared and
the game revision unchanged. Only the game runner may validate and commit that
move through the arbiter.

After a comparison, build a follow-up request from the newly saved context:

```python
context = ledger.store.get_decision(decision_id).context
request = build_model_request(
    context.model_view(),
    attempt_id=next_attempt_id,
    model=model,
    capabilities=capabilities,
    remaining_limits=ledger.remaining_limits(decision_id),
    config=context.config,
    evidence=context.evidence_views(),
    force_selection=True,
    continuation=saved_reply.continuation,
)
```

Admit the complete-input bound through `reserve_model(..., use_finish_hold=True)`,
dispatch, save/settle the reply, then execute its selection. The executor does not
perform those provider steps automatically; Task 8 connects them into a loop.

## Storage compatibility

Task 7 adds optional intent/dispatch fields to reservation JSON. Their absent
defaults are omitted during canonical serialization, preserving existing stored
record hashes. No SQL migration is needed: migrations 001 and 002 remain
unchanged. The store saves immutable dispatch limits once and existing audit
kinds capture the additional reservation update.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/runs/test_tools.py tests/runs/test_tools_native.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Forty Task 7 cases cover selection, both tiers, shared remaining caps/deadlines,
invalid and forged actions/evidence, unsupported phases, native failures,
cancellation, partial results, bad receipts, output-size failure, restart,
receipt reuse, and interrupted evidence saving. Two real NWL23 cases run scripted
compare/select sequences through the executor at 60 and 300 rollouts, validate
the canonical selection, prove the arbiter checkpoint unchanged, and reopen the
same receipt/evidence without a new engine call.

The complete suite passes 695 tests: 644 offline and 51 native. Validation and
installed-package results are saved in `work/harness-task7/validation.json`.
