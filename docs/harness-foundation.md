# Harness contracts and frozen decision context

October 7, 2026 · Contracts/context: Tasks 1–2; prompts: Task 3; persistence: Task 4; accounting: Task 5; scripted provider: Task 6; typed tools: Task 7; bounded loop: Task 8

The harness can now prepare a verified, reproducible set of legal candidates
from a public observation and expose a restricted view for a future model
request. It also defines strict configuration, action, provider, evidence,
outcome, and execution-manifest records. Task 3 now adds a prompt renderer and
strict tool schemas. Task 4 adds a [persistent run store](harness-run-store.md).
Task 5 adds a [resource ledger](harness-budget-ledger.md) and persistent deadlines.
Task 6 adds an [offline scripted provider](harness-scripted-provider.md).
Task 7 adds [typed tool execution](harness-tools.md). The real provider client
remains Task 11; the [decision loop](harness-loop.md) is implemented in Task 8.

See the [chronological checklist](harness-task-checklist.md) for remaining work.
The [prompt guide](harness-prompts.md) documents the Task 3 presentation APIs.

## Modules and dependency direction

| Module | Responsibility |
| --- | --- |
| `harness/contracts.py` | Limits, actions, public views, evidence, outcomes, rate cards, manifest identity |
| `harness/context.py` | One candidate snapshot, deterministic presentation, handle resolution, evidence registration, canonical selection |
| `harness/errors.py` | Preparation, scope, unknown-handle, and evidence failure categories |
| `providers/protocol.py` | Async provider interface, capabilities, requests/replies, normalized usage, private continuation |

The contracts depend on existing domain records and Pydantic. The context uses
the existing `AnalysisEngine` interface; its engine import is for type checking
only. The provider protocol depends on the public harness views. Importing
either new package loads no engine worker, native library, or provider SDK.

Records remain frozen and strict: unknown fields, unsupported versions,
numeric strings, boolean counts, duplicate handles, and nonfinite estimates
fail validation. Nested collections are tuples. Use `model_validate_json`
for serialized records and validated reconstruction for updates.

## Preparation, in order

1. Validate the active player's in-progress `PlayerObservation`, configuration,
   decision identifier, and ordering seed before dispatching native work.
2. Load that public observation into the caller-owned analysis adapter.
3. Generate exactly once, using `CandidateConfig(pool_size=40, display_count=12)`
   by default. Forty is a ceiling: fewer unique actions may exist.
4. Validate the returned pool's observation IDs, unique canonical actions,
   static equity, physical rack/leave conservation, and action filters.
5. Choose the highest static-equity fallback, breaking ties by canonical ID.
   Reserve it, pass, and the best exchange when present; fill the displayed
   subset from the static ranking. Fail if the display ceiling cannot hold
   the distinct reserved options.
6. Order the subset by a versioned SHA-256 permutation using the recorded seed
   and canonical candidate IDs; assign `c01`, `c02`, … afterward.
7. Preview the fallback through the engine and check its observation, action,
   score, leave, and board effects against the candidate. Freeze the context
   only after this succeeds.

Using a hash permutation makes ordering independent of Python's shuffle
implementation, input pool order, decision ID, and workflow name. Paired
policies see the same order when public position, candidate configuration,
pool metadata, and ordering seed match. The version is recorded in the
context. Changing the seed changes presentation, not the selected subset.

Dictionary legality and exact scoring remain MAGPIE's responsibility. The
context requires a trusted engine-generated pool; canonical IDs detect content
changes and do not authenticate an arbitrary caller-supplied pool.

Fallback preview preserves the adapter's generated-candidate registry, so a
later comparison can use the cached canonical IDs without regenerating. One
analysis adapter must belong to one active decision at a time. Do not reload
or regenerate it while that decision is using the registry.

## Run the preparation demo

From the project root, with the installed full NWL23 profile:

```sh
.venv/bin/python scripts/demo_decision_context.py
```

The script starts an engine game, prepares its opening context, saves a public
view to `docs/decision-context-demo.json`, and closes both adapters. It makes
no model request and commits no move. Use `--output` to choose another file,
or `--manifest .native/manifest.json` for the small fixture dictionary.

Preparation can be used directly inside an async caller:

```python
from scrabble_harness.harness import prepare_decision_context

context = await prepare_decision_context(
    observation, analysis,
    decision_id="run-1-decision-1",
    candidate_order_seed=123,
)
view = context.model_view()
```

The caller owns adapter cleanup. `ContextPreparationError` aborts preparation
on engine failures or inconsistent returned data. Invalid public inputs fail
before an engine request; cancellation propagates. The decision loop completes
preparation before admitting a paid request.

## Public view and host-owned records

`DecisionContext` contains the full pool, native static metadata, canonical ID
bindings, the verified fallback, and issued evidence. It is a host record.
Never serialize its whole contents into model instructions or continuations.

`model_view()` explicitly constructs `DecisionView`: public observation and
displayed candidates with short handles, move geometry, exact score, and
physical leave. It excludes static equity, native rank, provenance, the
undisplayed pool, and the fallback marker. Own rack and publicly inferable
unseen counts are included; opponent tiles, bag order, and RNG are unavailable.
The Task 3 renderer adds board premiums and a bounded public-history suffix.

`ModelRequest.view` accepts only `DecisionView`. Private `GameState`,
`Checkpoint`, whole contexts, and extra candidate fields fail validation.
Free-form instructions and opaque continuation still require trusted host
construction: types cannot prevent a caller from embedding private text.

Short handles are local to the decision. The host attaches `decision_id` and
`observation_id` when wrapping parsed tool arguments in `ScopedAction`.
Resolution requires both to match. A `c01` from another decision cannot resolve
in this one, and an undisplayed canonical ID is not a valid handle.

## Selection and evidence helpers

```python
from scrabble_harness.harness import ScopedAction, SelectAction

decision = context.select(ScopedAction(
    decision_id=context.decision_id,
    observation_id=context.observation.observation_id,
    action=SelectAction(candidate_handle="c01"),
))
fallback = context.fallback_decision("provider_refused")
```

Both return the existing canonical `AgentDecision`; neither commits a turn.
The game runner must still call `validate_decision` with the trusted current
state and pool, then the arbiter's revision-checked `commit`. The fallback
helper always returns the verified static candidate. The configured optional
latest-comparison policy is implemented in [Task 9](harness-policies.md).

`resolve_comparison` accepts two to four distinct visible handles and a
configured `light` or `standard` tier. Comparison is unavailable when the bag
has fewer than seven tiles or the opponent lacks a full unknown rack;
selection and fallback remain available.

After a trusted executor calls `analysis.analyze`, it can register the result:

```python
requested = context.resolve_comparison(scoped_compare)
result = await analysis.analyze(
    tuple(candidate.candidate_id for candidate in requested), limits, seed=73
)
context, evidence = context.issue_evidence(
    scoped_compare, result, limits=limits, seed=73
)
public_evidence = context.evidence_views()
```

Registration records the requested tier, actual request limits, and seed. It rejects foreign
observations, unrequested candidates, incompatible work units or metric kinds,
work overruns, seed mismatches, and incomplete results claiming completion.
Timeout/cancelled/failed results can retain partial or empty values with their
actual completion status. Elapsed time and reported uncertainty are preserved.
The adapter reports total `candidate_rollout` work, not per-action sample counts.

The context issues consecutive `e01`, `e02`, … handles and returns a new
immutable context. Re-registering the same analysis/result is idempotent;
reusing an analysis ID with changed contents fails. `select` accepts only
evidence handles already issued in that context. `evidence_views()` projects
canonical IDs back to visible handles and retains metrics, units, limits,
work, seed, uncertainty, elapsed time, and completion.

These helpers validate and bind records. Tasks 4–5 now provide persistence,
resource admission, dispatch accounting, and settlement. Task 7 now connects
these records to native tool execution; loop termination is implemented in [Task 8](harness-loop.md).

## Provider and configuration contracts

`ModelProvider.complete(ModelRequest) -> ModelReply` describes one explicitly
admitted async attempt. No implicit transport retry is permitted by the
interface contract. Task 6 implements this interface with `ScriptedProvider`;
real API transport remains Task 11.

`ProviderCapabilities` checks configured model/provider, output cap, strict
tool support, temperature, reasoning settings, and continuation support.
Requests are decision-scoped, bound their normalized UTF-8 size, and expose
comparison only with public-phase eligibility and remaining attempt/tool/work
allowances. The final attempt must expose selection alone. A future adapter
must also bound its actual transport payload.

Replies retain refusal/incomplete/failure statuses and raw tool argument text,
including malformed or multiple calls, alongside usage. Action parsing is
separate so unusable replies can still be accounted for. `TokenUsage` preserves
unknown counts as `None`: normalized input includes mutually exclusive cache
reads/writes, and normalized output includes reasoning output. Raw vendor usage is bounded finite JSON.
Opaque provider continuation is private adapter-owned JSON, never a public
display or a place for position state.

`HarnessConfig` defaults to 2 model attempts, 1 comparison step, 300 total
rollouts, 30 seconds, a 2-second finish reserve, and $0.03 per decision.
The comparison tiers cap total work at 60/300 rollouts. These are configuration
and request-validation limits; Task 5 now enforces runtime reservations and
deadlines through `BudgetLedger`. The [loop](harness-loop.md) connects them in Task 8. Workflow presets are implemented in [Task 9](harness-policies.md).

`DecisionOutcome` links the trusted pool, visible bindings, chosen canonical
decision, issued evidence, terminal reason, and confirmed/unresolved resource
usage. It rejects selections outside the visible pool, foreign evidence,
double-counted analyses, and usage that omits reported evidence work.

`ExecutionManifest.create(...)` combines existing `RunConfig`, harness settings,
engine configuration fingerprint, source SHA-256, dependency versions, rate
card, and trial identity. Its versioned content hash is insensitive to
dependency ordering and detects changed content when loaded. Run/model/rate
and decision/run caps must agree. Rate cards use integer microusd per million
tokens and require a dated source; no live pricing or billing is inferred.
The caller supplies trusted provenance fields. Tasks 4–5 now persist manifests
and calculate bounded costs; no real provider has been configured or called.

## Verification

```sh
.venv/bin/python -m pytest tests/harness -m 'not native'
.venv/bin/python -m pytest tests/harness/test_context_native.py
.venv/bin/python -m pytest
```

Offline tests cover strict records, manifest identity, capability and size
limits, private-input rejection, paired presentation, stale handles,
preparation failures, cancellation, fallback previews, and owned evidence.
Native verification uses a real NWL23 opening: one generation/preview,
40-candidate ceiling and 12 visible actions, bounded comparison using the
retained registry, evidence registration, canonical validation, authoritative
commit, and deterministic presentation on repeated preparation. No API key
or paid request is used.
