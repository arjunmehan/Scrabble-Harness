# Scripted provider (Task 6)

October 7, 2026 · Implemented · No SDK, credentials, network, or paid requests

`providers/fake.py` implements `ModelProvider.complete(ModelRequest) -> ModelReply`
using an explicit ordered script. It supplies reproducible replies and failures
for developing the tool executor and decision loop. It returns provider records;
comparison execution, action validation, accounting, fallback, and game commits
belong to the surrounding harness.

## Try it

```sh
.venv/bin/python scripts/demo_scripted_provider.py
.venv/bin/python scripts/demo_scripted_provider.py --scenario compare
.venv/bin/python scripts/demo_scripted_provider.py --scenario malformed
.venv/bin/python scripts/demo_scripted_provider.py --scenario transport-error
.venv/bin/python scripts/demo_scripted_provider.py --scenario timeout
```

The demo reads the saved public NWL23 candidate view in
`docs/decision-context-demo.json`; it does not load an engine. Other scenarios
are `refuse`, `incomplete`, `multiple`, and `missing-usage`. JSON output shows
reply/error details, consumed script steps, and call state. Token counts are
synthetic fixture data, and actual API cost is zero. Selection uses a scripted
handle rather than evaluating Scrabble strength. The demo exercises one provider
call, without executing tools or committing a turn.

## Script records

| API | Purpose |
| --- | --- |
| `ScriptedReply` | Explicit response/request IDs, status, raw calls/text, optional usage, resolved model and private continuation |
| `ScriptedTransportFailure` | Connection, timeout, rate-limit, or server error with an optional request ID |
| `ScriptedStep` | Expected attempt ID, reply/failure, cancellable delay, optional exact request hash |
| `ScriptedProvider` | One frozen ordered script and its configured capabilities |
| `ScriptedCall` | Immutable inspection snapshot of an accepted request and its pending/terminal state |
| `selection_call` / `comparison_call` | Helpers producing complete strict v1 tool arguments |

Scripts use the existing strict, frozen Pydantic records and round-trip through
`ScriptedStep.model_dump_json()` / `model_validate_json()`. Unknown fields,
unsupported outcome kinds, invalid counts, missing identities, and negative or
nonfinite delays fail validation. Each provider requires unique attempt and
response IDs. Reply request IDs are explicit; a transport failure may have no
request ID because no response was available.

## Construct a scenario

With a validated `request` from `build_model_request`:

```python
from scrabble_harness.providers import (
    ScriptedProvider, ScriptedReply, ScriptedStep, TokenUsage, selection_call,
)

provider = ScriptedProvider(
    (
        ScriptedStep(
            attempt_id=request.attempt_id,
            outcome=ScriptedReply(
                response_id="offline-response-1",
                request_id="offline-request-1",
                tool_calls=(selection_call("offline-call-1", "c01"),),
                usage=TokenUsage(input_tokens=100, output_tokens=10),
            ),
        ),
    ),
    capabilities=request.capabilities,
)
reply = await provider.complete(request)
```

The returned reply takes its attempt, decision, observation, and provider scope
from the accepted request. `resolved_model` defaults to the requested model;
set it explicitly to test versioned model names. `selection_call` supports
`evidence_ids` and `brief_note`. `comparison_call` takes two to four handles
and a `light` or `standard` tier. These helpers validate argument shape; the
context/tool executor still owns handle visibility and evidence authority.

Raw `ProviderToolCall` records can deliberately contain broken JSON, unknown
tools/handles, duplicate call IDs, or multiple actions. The fake preserves them
verbatim, even when a tool was not offered. No repair or action parsing occurs
inside the provider. `ScriptedReply` can also return refusal, incomplete, failed,
or text-only responses. Usage defaults to `None`, and partially reported counts
remain partial; no token count is inferred from the prompt.

## Request matching and retries

`complete` revalidates each `ModelRequest` and checks its capabilities against
the provider's configured capabilities. Its attempt ID must match the next
step. An optional `expected_request_sha256` pins the canonical request, including
public inputs, offered tools, settings, and private continuation. Mismatches
consume no step and add no inspection record.

Every accepted request consumes exactly one step **before its first await**.
A retry must therefore be a separately scripted step with a new attempt ID.
There is no implicit retry or default response after exhaustion:

- `ScriptMismatchError`: wrong attempt/capabilities/hash, reused attempt,
  conflicting script identities/continuation, or another owner loop/thread.
- `ScriptExhaustedError`: no outcome remains for a new attempt.
- `ScriptedTransportError`: the accepted step deliberately failed; its
  `attempt_id`, `request_id`, and `error_category` remain inspectable.

The first two identify script/setup errors rather than simulating a provider
transport failure. Reconstructing a fresh provider from the same script produces
the same replies for a repeat offline test. Durable admission and restart safety
remain the ledger's responsibility; resetting the fake does not authorize any
new runtime attempt.

## Delays, cancellation, and inspection

`delay_seconds` uses `asyncio.sleep` by default. Callers enforce deadlines with
`asyncio.timeout` or `wait_for`, using the ledger's dispatch permit. Cancellation
propagates as `CancelledError`; it does not become a fabricated failed reply or
zero usage. The consumed step stays consumed. A scripted transport error can
also be delayed.

Pass a trusted async `sleeper(seconds)` to use virtual time or an event barrier
in tests. This allows a request to remain pending until a test releases it,
without depending on a wall-clock race. A sleeper exception propagates and marks
the invocation interrupted. The provider is owned by its creating thread and,
after the first accepted request, one event loop. Concurrent completions in that
loop claim distinct steps in acceptance order and retain separate call records.

`provider.requests`, `provider.calls`, and `provider.remaining_steps` expose
immutable snapshots and remaining capacity. Call states are `pending`, `replied`,
`transport_error`, `cancelled`, or `interrupted`. These logs are for local
inspection; requests can contain opaque continuation, so they are not public
replay exports or the authoritative run journal.

## Connection to the budget ledger

Reserve and dispatch through `BudgetLedger` before calling `complete`. Save
and settle any returned reply before interpreting its actions. Refused,
malformed, incomplete, and multiple-action replies remain billable according
to their scripted usage. Missing usage retains the full reservation. After
transport failure or cancellation without a saved response, mark the dispatched
reservation unresolved; a timeout does not justify releasing it.

The provider itself performs no ledger writes. The integration tests exercise
these boundaries explicitly, including continuation loaded from the saved
initial reply, final-selection hold consumption, and a timed-out attempt
followed by a separately admitted retry. Synthetic ledger estimates may be
nonzero in tests, but no real invoice or API charge is created.

Task 7's [tool executor](harness-tools.md) now connects saved comparison calls
to native analysis and returns canonical selections. Task 8 adds the complete [bounded decision loop](harness-loop.md).

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/harness/test_fake_provider.py tests/runs/test_fake_provider_ledger.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Task 6 adds 50 tests: 38 provider checks and 12 ledger integration cases.
The focused provider/contracts/accounting check passes 86 tests. The full suite
passes 695 tests: 644 offline and 51 native. All nine demonstration scenarios
also run offline. Package installation checks confirm the new module is bundled
and an installed-wheel scripted reply works without an SDK or native import.
See `work/harness-task6/validation.json` for the saved checks.
