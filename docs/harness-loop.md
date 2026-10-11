# Bounded decision loop (Task 8)

October 7, 2026 · Implemented · Scripted provider and real NWL23 integration

`harness/loop.py` connects public-position preparation, the budget ledger, saved
model replies, typed tools, evidence, selection, repair, fallback, and abort.
It returns a durable `DecisionOutcome`; the game runner retains authority to
validate and commit a turn. No real provider adapter or CLI is added in this task.

## API

```python
from scrabble_harness.harness import DecisionLoop

loop = DecisionLoop(
    provider, analysis, ledger,
    capabilities=capabilities,
    input_bound=count_complete_request,
    finish_bound=verified_followup_ceiling,
    model_timeout_seconds=30.0,
)
outcome = await loop.decide(run_id, decision_id, observation)
```

The campaign and run must already exist in `RunStore`. Their execution manifest
supplies the model, harness configuration, seeds, rates, and resource ceilings.
`observation` is the active player's public `PlayerObservation`. The caller owns
the store, provider, and analysis adapter, including closing them. Use one active
decision per loop/adapter and preserve the candidate registry until it ends.

`count_complete_request(request)` returns a verified `InputTokenBound` for that
exact request, including tools, evidence, framing, and opaque continuation.
`verified_followup_ceiling` is a separately attested `final_selection` bound that
covers the largest bounded follow-up. The loop does not invent token counts.
Without that ceiling, it offers selection only. An inadequate held ceiling fails
closed before the follow-up is dispatched. Production counters and transport
contracts belong to the real adapter in Task 11.

## Execution and termination

1. Validate run/model capabilities and claim a fresh decision atomically. Save
   the public observation and start campaign/run/decision clocks before native
   preparation. Generate one pool and preview the static fallback once.
2. Build a bounded request from the saved context, current issued evidence,
   remaining allowances, and the last saved provider continuation. The last
   available model slot offers selection only.
3. Reserve model cost and a slot. Comparison-capable requests also require a
   durable final-selection hold. Commit the dispatch marker before the provider
   call, and clip its timeout to the original decision deadline.
4. Save and account for the reply before parsing actions. Unknown usage retains
   the full allocation. Late replies remain saved/billed but cannot select a move.
5. Resolve actions through the frozen context and execute typed tools. A valid
   comparison appends evidence; the next request includes its public projection.
   A valid selection produces a completed outcome and releases any unused hold.

One unusable output can receive a selection-only repair, within both
`max_repair_rounds` and the original attempt/cost/time ceilings. Its persisted
request includes fixed host instructions, rather than copying invalid output
into the prompt. Refusals, incomplete/failed replies, missing/multiple actions,
malformed arguments, invisible handles, and forged evidence follow this path.

Transport failures default to immediate static fallback with unresolved charges.
If `max_transport_retries=1` is explicitly configured, one fresh, separately
admitted selection-only attempt is allowed. Original uncertain work stays
charged. Every retry and repair consumes a new model slot.

Repeated call IDs or an identical comparison set/tier terminate with
`repeated_action`; they cannot authorize another native run. Native failure,
invalid native evidence, or presentation failure ends with `tool_failed` and
preserves saved receipts/uncertain allowance. Budget exhaustion ends with
`budget_exhausted`. Preparation failure has no verified fallback and aborts.

External cancellation accounts for dispatched work, saves an `aborted` outcome
with reason `cancelled`, releases the held final allowance, and re-raises
`CancelledError`. If persistence itself fails, cancellation still propagates;
the durable dispatch marker remains available for explicit recovery. No extra
provider request is used to repair cancellation.

Final usage is derived by `BudgetLedger.complete_decision` from durable facts.
Each outcome contains the trusted pool, visible bindings, issued evidence,
canonical selection or abort, resource totals, and terminal reason. Cleanup and
native stopping may exceed the scheduling deadline; new work never gets a new
decision clock.

## Recovery and boundaries

A completed decision can be read repeatedly, including after reopening, without
calling the provider, preparing candidates, recounting tokens, or changing its
saved usage. An unfinished decision is rejected; the loop cannot infer that a
previous owner is inactive or safely replay uncertain dispatch. Use the explicit
ledger/runner recovery policy when that support is implemented.

Task 9 adds packaged workflow presets and optional `latest_comparison` fallback;
see the [policy guide](harness-policies.md). Outcomes now retain an explicit
selection origin. The [offline `decide` CLI](harness-offline-cli.md) is implemented in Task 10; the real provider is Task 11. The loop changes no game state or native engine code.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/runs/test_loop.py tests/runs/test_loop_native.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Task 8 adds 38 tests: 36 offline and two native. They cover early selection,
compare/select, two-comparison configurations, repairs, repeat detection,
transport retry, unknown usage, exhausted shared money, overrun/held bounds,
late/foreign replies, preparation failure, cancellation in all three external
phases, and completed/unfinished recovery boundaries. Native tests exercise
60 and 300 rollouts with NWL23, validate the selected candidate, prove the game
checkpoint unchanged, and read the identical completed outcome after reopening.

The full suite passes 695 tests: 644 offline and 51 native. See
`work/harness-task8/validation.json` for validation and installed-wheel checks.
No paid provider requests or new dependencies were needed.
