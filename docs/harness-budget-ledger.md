# Resource accounting and admission (Task 5)

October 7, 2026 · Implemented · Offline verification; no paid provider requests

`BudgetLedger` is the admission boundary for model calls and native comparisons.
It reserves resources before dispatch, records actual usage afterward, and keeps
uncertain usage charged. Campaign limits are shared by every run in the same
local database; creating another run cannot reset them. Task 6's
[scripted provider](harness-scripted-provider.md) is verified with this API;
Task 7 adds the [typed tool executor](harness-tools.md), and Task 8 adds
the [bounded decision loop](harness-loop.md).

## Components

| Component | Responsibility |
| --- | --- |
| `harness/budget.py` | Complete-input attestations, exact price arithmetic, cost quotes, usage estimates, absolute deadlines, budget errors |
| `runs/ledger.py` | Atomic admission at all scopes, finish holds, one-use dispatch permits, settlement, explicit interrupted-work accounting |
| `runs/records.py` | Typed quotes, allocations, settlements, native results, finish holds, persisted clock watermarks |
| `runs/store.py` | Durable transactions, checked records, audit events, and writer serialization |
| `runs/migrations/002_accounting.sql` | Finish-hold/clock tables, unique analysis receipts, extended event kinds |

No SDK, credentials, network call, or native process is needed to import these
modules. Pricing is provider-neutral. Runtime code must use the ledger rather
than treating `RunStore.create_reservation` as authorization to dispatch.

## Admission and charged resources

Every reservation checks campaign, run, and decision caps in one writer
transaction. Model reservations consume a cost allowance and one attempt slot.
Native reservations consume bounded `candidate_rollout` work and one tool step;
the separate tool-step cap belongs to the decision. Aggregate integer sums use
Python integers so SQLite integer overflow cannot reset a cap.

`totals(campaign_id=...)`, `totals(run_id=...)`, or
`totals(decision_id=...)` accepts exactly one scope. It returns these groups:

| Group | Charge against admission |
| --- | --- |
| `settled` | Saved usage-priced cost or reported native work; dispatched call/tool slots remain consumed |
| `reserved` | Full allocation for admitted work awaiting dispatch |
| `unresolved` | Full allocation for dispatched or interrupted work without usable usage |
| `held` | Cost and one call reserved for final selection |

`totals.charged` adds all four groups. Dispatched work is included in unresolved
accounting until settled. Released allocations and consumed/released holds add
no separate charge. A zero-token, refused, incomplete, or malformed response
still consumes its dispatched call slot. Each explicit retry needs its own
attempt identity and reservation. Repeating the same reservation write is
idempotent, but repeating dispatch never issues another permit.

Native settlements save the complete `AnalysisResult`, including its unique
analysis identity, in the same transaction as usage. One result cannot settle
two reservations. Unknown work retains the full allocation; a later known
result can settle it. No fabricated zero is substituted after a timeout.

## Price bounds and settlement

Money uses integer microusd: 1,000,000 microusd = $1. Rate-card arithmetic uses
`Decimal` and rounds each reservation/settlement upward. Rates, source/date,
model, billing tier, and optional input pricing-band ceiling are frozen in the
execution manifest. Each saved quote includes the rate-card hash, input bound,
output cap, and margin. The default margin is 10%, with a minimum of 1 microusd
for a nonzero base cost; a zero-rate synthetic request reserves zero cost.

Admission uses the largest configured ordinary-input, cache-read, or
cache-write rate, then the full output cap. Cache writes may cost more than
ordinary input, so reserving only the uncached rate is insufficient for some
configurations. Settlement partitions total input into ordinary, cache-read,
and cache-write categories when reported. Reasoning is already included in
normalized output and is charged once. This normalization follows the
[official prompt-caching guide](https://developers.openai.com/api/docs/guides/prompt-caching).

Missing total input/output retains the full reservation. Unknown cache
categories are priced at the highest applicable saved rate and labeled
`category_upper_bound_v1`; reported partitions use `reported_categories_v1`.
A positive reported category without its rate stays unresolved. These are
rate-card estimates, not provider invoices.

A request requires an `InputTokenBound` supplied by trusted adapter code.
`verified_input_bound(request, input_tokens=..., evidence=...)` binds a complete
payload count or verified ceiling to that exact request hash and output cap.
Include tools, framing, results, and continuation. Local character heuristics
cannot establish that bound. The
[official token-counting guide](https://developers.openai.com/api/docs/guides/token-counting)
describes counting the complete provider input; the real adapter will implement
its verified counter/contract in Task 11. A count endpoint may itself require
network access, so it is not called by this ledger.

`text_input_bound` is an alternative only for a verified text-only byte-level
tokenizer and framing contract. It overcounts the normalized request and full
strict tool schemas, requires explicit framing/tokenizer evidence, and rejects
opaque continuation. It does not automatically establish a bound for an
arbitrary provider. Model-supplied token counts never authorize dispatch.

Reported usage beyond an input/output/cost bound is preserved truthfully and
sets `bound_exceeded`; later admission and dispatch stop across that campaign.
Out-of-band or unrepresentable price facts remain unresolved and also stop
admission. This flag cannot be cleared by marking work unresolved again.

## Preserving a final selection

A comparison-capable model request must have a durable finish hold. Supply a
future-selection `InputTokenBound` that covers the entire possible follow-up,
including bounded results and continuation. Do not extrapolate it from the
current prompt. Creating the hold and first reservation is atomic: if their
combined allocation cannot fit, neither is saved.

A hold protects one call and its cost at all three scopes. Native comparisons
also require a hold whenever model attempts are enabled. A forced
selection-only request can consume it using `use_finish_hold=True`; its input,
output, and cost must fit the saved hold. The ledger replaces the hold with the
actual reservation without double charging. Unused capacity becomes available
for other work. Completing the decision releases an unconsumed hold.

`remaining_limits(decision_id)` supplies a current request-building snapshot.
It includes that decision's held final call/cost, which are available **only to
finish**. Admission still prevents arbitrary requests from spending them. The
snapshot is informative; the transactional reservation is authoritative.

## Caller sequence

The surrounding executor should follow this order:

1. Create the campaign/run with a frozen manifest. Call `begin_decision` before
   candidate preparation so preparation consumes decision time, then persist
   the prepared context with the store.
2. Build a model request from public inputs and `remaining_limits`; obtain a
   trusted complete-input bound. For comparison-capable requests, also provide
   the verified future-selection bound.
3. Call `reserve_model`. Call `dispatch_model` immediately before transport;
   it commits both attempt/reservation markers and returns one `DispatchPermit`.
   Bound transport by its timeout and absolute monotonic expiry. Do not await
   external work inside any store transaction.
4. On a reply, call `settle_model` before parsing or executing actions. It saves
   the private raw reply, including malformed arguments and continuation, along
   with settlement. If no response/usage is available, call `mark_unresolved`.
5. For a comparison, validate handles/phase in the tool executor, reserve native
   limits, then use the clipped limits returned by `dispatch_native` for the
   actual engine call. Save its result with `settle_native`. Evidence issuance
   and public display remain the context/tool executor's responsibility.
6. Build the forced final request and consume the finish hold. At termination,
   release any allocation this live session can prove was never dispatched,
   then call `complete_decision(outcome)`.

The ledger provides admission and accounting, not provider calls, action
validation, scheduling, or game commits. Those remain distinct components.

## Deadlines and restart

Campaign, run, and decision clocks start on first ledger use; `begin_decision`
starts them before preparation. Each cap is independent. In-process monotonic
time combines with persisted elapsed floors and wall-clock watermarks.
Reopening uses those records and counts downtime, so it cannot restart an
existing Task 5 deadline. Wall-clock rollback or a backwards in-session
monotonic clock expires the saved clock rather than granting extra time.

Admission checks time again after acquiring the writer lock. Dispatch checks
again after its marker transaction commits. Nonterminal operations leave the
configured finish reserve; their permits expire before that reserve begins.
Expired work returns no permit and keeps a conservative unresolved allocation.
Use permits immediately and enforce their expiry in the transport/executor;
cleanup and final validation may exceed the scheduling deadline.

Reservations belong to a live admission session. Another ledger instance cannot
release or dispatch its old reservations automatically. `recover_unfinished`
explicitly converts an inactive run's reserved/dispatched work to unresolved,
retains the allocation and finish hold, and preserves saved replies. It never
replays a call. The caller must own the inactive run; the runner ownership lock
and full checkpoint reconciliation are Tasks 14–15.

`complete_decision` derives usage from durable accounting and elapsed time,
rather than trusting caller-supplied usage. Its outcome is an immutable terminal
snapshot. Later settlement can refine the ledger; reports should use current
ledger facts when reconciling those late costs.

Migration 002 keeps migration 001 unchanged and preserves existing canonical
record bytes, manifest/request hashes, and audit events. New optional fields
omit their defaults during serialization to preserve older identities. Existing
Task 4 allocations count toward admission. Task 4 did not save budget clocks,
so its historic elapsed time cannot be reconstructed; those scopes acquire a
clock on first ledger use.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/runs -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

The full suite now passes 695 tests: 644 offline and 51 native. Budget tests cover
shared admission with six threads and three processes, hard process exit,
restart/late settlement, deadline rollback and commit-time expiry, protected
finish capacity, conservative cache pricing, billable malformed/refused replies,
unknown usage, native overrun and duplicate receipts, and v1 migration. The real
NWL23 test reserves, dispatches, and settles a native comparison, then confirms
that its result, work usage, released hold, and arbiter receipt survive reopening.
Synthetic rate cards are used throughout; no paid API request is made.

See `work/harness-task5/validation.json` for the saved validation and package
checks, and the [chronological checklist](harness-task-checklist.md) for Task 11.
