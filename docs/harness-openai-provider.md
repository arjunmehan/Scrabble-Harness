# OpenAI Responses provider (Task 11)

Task 11 supplies the real provider adapter, verified with the pinned SDK and a
fake HTTP transport. It makes no paid calls during validation. The offline
`decide` command remains scripted. Task 12 chooses and verifies an actual model,
rate card, complete-input token bound, and pilot budget before enabling paid CLI
requests.

## Installation and ownership

```sh
.venv/bin/python -m pip install -e '.[dev,openai]'
```

The optional `openai` extra pins `openai==3.29.0`. The core package and offline
CLI import without the SDK. Import the provider explicitly:

```python
from scrabble_harness.providers.openai import OpenAIProvider, serialize_request
```

Construct `OpenAIProvider(capabilities=verified_capabilities,
timeout_seconds=30.0)` inside an async function. It reads `OPENAI_API_KEY` through
the SDK if no explicit key is supplied. Use `async with provider:` to close its
owned client. An injected `AsyncOpenAI` client belongs to its caller; the caller
closes it. Credentials belong to the client, never the manifest, request,
response, continuation envelope, or exported decision outcome. Provider repr
contains no credentials.

Capabilities must use provider `openai`, the exact configured model, strict
function tools, and stateless continuation. The adapter checks request
capabilities against its constructor capabilities on every attempt. It rejects
unsupported temperature, reasoning effort, output caps, provider/configuration
mismatches, invalid timeouts, and incompatible continuation. Capabilities are
attestations supplied by trusted setup code; this adapter does not probe model
support or assume a particular model's capabilities. Task 12 verifies them.

## Complete request serialization

`serialize_request(ModelRequest)` is SDK independent. The wire body contains:

- The existing public-position instructions, one small user task message, and
  any required private conversation history. It does not copy the structured
  board/rack/candidate view into a second prompt.
- The packaged strict `select_move` and optionally `compare_moves` definitions.
  Every root field is required and additional properties are forbidden.
- `tool_choice="required"`, or the specific `select_move` function on a final
  selection/repair request; `parallel_tool_calls=False`.
- The configured model and output cap, plus temperature/reasoning effort only
  when explicitly configured and allowed.
- `store=False` and `include=["reasoning.encrypted_content"]`. The latter is
  accepted for compatibility and makes the desired continuation explicit.

Both the normalized protocol record and the complete wire body must fit
`max_request_bytes`. The wire check includes strict schemas, replayed output,
encrypted reasoning, and correlated function results. Oversized history is
rejected without truncation or a provider call. ASCII JSON sizing conservatively
bounds the SDK's compact UTF-8 body. This is a **byte limit**, not an input-token
or cost bound.

`serialize_request` lets a trusted input-bound calculator inspect the actual
body. A paid caller must provide a defensible complete-input bound to
`DecisionLoop`, and a separate verified follow-up bound before offering
comparison. The existing text-only bound rejects opaque continuation. Do not
reuse the synthetic bounds or rates in these tests for paid requests.

## Tool calls and private continuation

The adapter returns raw function arguments in `ProviderToolCall`, including
malformed arguments, unknown names, and multiple calls. The host saves and
accounts for the reply before deciding whether any action is usable. The
Responses item's `id` and its `call_id` are different: function outputs always
use `call_id`.

For a completed response, the private versioned continuation retains the full
replayable output, including encrypted reasoning and assistant message metadata.
It binds history to the frozen position, model settings, capabilities, prompt
version, and tool-schema version. It also fingerprints previously issued
public evidence. The next request must preserve that evidence unchanged.

A newly issued comparison batch must match the sole pending `compare_moves`
call's ordered handles and tier. The adapter appends its public `EvidenceView`
as `function_call_output` under the original call ID. This required function
result also appears in the current instructions' evidence section; it is
bounded and contains no canonical candidate IDs. Each subsequent batch is
appended once to history, preserving earlier output/result pairs.

When the harness repairs an unusable action, pending calls receive a fixed
`action_not_executed` error output. This does not claim a native receipt.
Refusals, incomplete/failed responses, and duplicate call IDs do not produce a
continuation to replay as a completed tool turn. Repairs start fresh in these
cases under the current public instructions. Unsupported output types or
oversized replies produce an unusable receipt with available usage retained.

Continuation stays in private saved requests/replies. It never enters visible
reply text, instructions as literal JSON, or `DecisionOutcome`. Task 16 will
implement the dedicated public replay/export surface; raw ModelReply JSON is a
private audit record and must not be treated as a public export.

## Usage, deadlines, and retries

Usage preserves input/output/total tokens, cached input, cache writes, reasoning
output, and the original vendor usage JSON. Input already includes cache tokens;
output already includes reasoning tokens. Counts are not added a second time.
Missing counts remain `None`, and absent usage remains `usage=None`. Invalid
counts remain auditable in raw usage with normalized counts unknown. The ledger
uses its existing conservative unresolved-cost rules. Pricing reported cache
categories requires corresponding saved rates.

Response ID, resolved model, and HTTP request ID are recorded separately. Each
attempt sends its host attempt ID as `X-Client-Request-Id`. This is correlation,
not a promise of server-side deduplication.

Every SDK call applies `max_retries=0`, overriding even an injected client's
retry defaults. The adapter bounds the SDK timeout and wraps the entire await
in an asyncio deadline, limited by its configured timeout and the request's
remaining elapsed allowance. The enclosing decision loop enforces the actual
ledger deadline. Cancellation propagates. Transport failures use a fixed error
message without SDK error bodies or headers. An explicit retry requires a new
harness attempt, reservation, dispatch, and receipt; missing receipts remain
charged as unresolved.

## Verification

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/runs/test_openai_provider.py -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
```

Pure translation tests run without the SDK. Transport tests skip with an
installation instruction when the optional extra is absent. With the pinned
extra installed, all 47 Task 11 tests run: actual SDK wire serialization,
strict/final tools, supported settings, encrypted continuation and byte bounds,
call-ID correlation, full Standard and Deep workflows, repairs, usage/cache
pricing, database reopen, HTTP 429/500/503 without hidden retries, separately
admitted retries, cancellation, timeout, and client ownership. MockTransport
answers every SDK request locally with a dummy key and no external API calls.

The validation record and wheel are under `work/harness-task11/`.

## API references

The request/tool/result contract follows the official
[function calling guide](https://developers.openai.com/api/docs/guides/function-calling).
Stateless replay of encrypted reasoning follows the official
[reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).
SDK constructor, per-call options, transport serialization, output types, and
usage fields were checked against the pinned SDK's installed source and tested
through its actual HTTP client.
