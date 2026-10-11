# Public prompts and strict tool schemas

October 7, 2026 · Harness Task 3 implemented

The harness now renders a complete model prompt from its public candidate view
and issued evidence. It can build a validated `ModelRequest`, export two
versioned strict function definitions, and parse logical tool arguments.
The renderer and parser dispatch no model or native work. Task 4 now adds
[persistent storage](harness-run-store.md). Task 5 provides resource admission
and Task 6 adds an [offline scripted provider](harness-scripted-provider.md).
Task 7 connects [typed tool execution](harness-tools.md); the [bounded agent loop](harness-loop.md) is implemented in Task 8.

## Presentation

`harness/prompts.py` implements `scrabble-decision-v1`:

- A board labeled A–O and 1–15. Empty cells show `..`, `DL`, `TL`, `DW`, or
  `TW`; occupied cells show their letters. Premiums beneath occupied letters
  are already consumed. H8 is the center double-word square.
- Lowercase letters identify assigned blanks worth zero points. Own rack and
  candidate leaves use `?` for a physical unassigned blank. `H7=z` places a
  blank assigned Z at zero-based row 6, column 7.
- Own rack, scores from the player's perspective, bag/opponent rack sizes,
  publicly inferable unseen physical counts, and scoreless-turn count.
- The existing candidate order and short handles, newly placed tile
  coordinates, exact immediate scores, and physical leaves. Placement text
  excludes existing board letters; the displayed board supplies them.
- A deterministic suffix of public history: eight available retained turns
  by default. Public exchange history reveals only the tile count. A limit
  of zero shows no history. The complete public observation stays in the host
  context and request audit record.
- Issued evidence with metric/units, actual work, request tier/limits, seed,
  elapsed time, standard errors when supplied, and completion status.
- Remaining attempt, comparison-step, native-work, API-cost, and time
  allowances. Attempts include the current request. Tier work/time allowances
  are clipped to the remaining decision limits and finish reserve.

Static equity, native rank/provenance, the hidden candidate pool, and the
fallback identity never enter the prompt. The renderer accepts `DecisionView`
and `EvidenceView`, rejecting private game state/checkpoints and whole contexts.
It reads no private files or engine state. Equivalent board, inventory, and
rack/leave multiset order gives the same presentation; displayed candidate
and public-history order retain their meaning.

## API

```python
from scrabble_harness.harness import render_prompt, build_model_request

prompt = render_prompt(
    context.model_view(), remaining_limits,
    config=context.config,
    evidence=context.evidence_views(),
)
print(prompt.instructions)
function_payload = prompt.as_payload()

request = build_model_request(
    context.model_view(),
    attempt_id="run-1-attempt-1",
    model=model_settings,
    capabilities=provider_capabilities,
    remaining_limits=remaining_limits,
    config=context.config,
    evidence=context.evidence_views(),
)
```

`request.instructions` contains the complete rendered prompt: instructions
and position. `request.view` and `request.evidence` retain structured public
inputs for host auditing. A future provider adapter must send the complete
instructions and appropriate function definitions, and must avoid adding the
raw observation again as a second prompt body. That would defeat the declared
history policy. Opaque provider continuation remains private and is never
rendered, shortened, or interpreted by this module.

The builder checks capabilities through the existing `ModelRequest` contract
and bounds both the audit record and neutral function payload, including
continuation and model settings. It does not reserve resources or dispatch a
request. `BudgetLedger.remaining_limits` supplies the remaining-limit snapshot.

## Tool definitions and parsing

The packaged asset is
`harness/schemas/scrabble-tools-v1.json`. `tool_definitions(...)` returns
immutable definitions; `as_function_tool()` returns fresh function dictionaries.
The schemas stay the same across positions. The host checks which candidate
and evidence handles belong to the current decision.

| Function | Required arguments |
| --- | --- |
| `select_move` | `candidate_handle`, `evidence_ids`, `brief_note` |
| `compare_moves` | `candidate_handles` (2–4), `tier` (`light` or `standard`) |

Use `[]` when no evidence is cited and `""` when no note is needed. The model
supplies no schema version, action kind, observation ID, native command, file
path, or arbitrary work limit. Tool objects forbid extra fields and require
every declared field, following
[OpenAI's strict function-calling format](https://developers.openai.com/api/docs/guides/function-calling#strict-mode).
The schema uses supported types, patterns, enums, and size constraints;
fine-tuned model schema compatibility has not been tested.

```python
from scrabble_harness.harness import parse_tool_arguments, ScopedAction

action = parse_tool_arguments(
    "select_move",
    '{"candidate_handle":"c01","evidence_ids":[],"brief_note":""}',
    allowed_tools=request.tools,
)
scoped = ScopedAction(
    decision_id=context.decision_id,
    observation_id=context.observation.observation_id,
    action=action,
)
decision = context.select(scoped)
```

The parser rejects unknown or disabled functions, malformed JSON, duplicate
object keys, extra/missing arguments, nonfinite numbers, excessive bytes,
duplicate comparison/evidence handles, and invalid argument types. It preserves
the original reply by returning a separate validated action. Handle visibility,
evidence ownership, and scope are still checked by `DecisionContext`; native
tool admission/execution is implemented in Task 7.

Comparison disappears before prompting when only one model attempt remains,
the caller forces selection, tool/work allowance is exhausted, fewer than two
candidates are visible, or the public position is outside native simulation
scope (bag below seven or opponent rack below seven). Selection-only payloads
force `select_move`. Other payloads require a tool and disable parallel calls,
following the documented
[tool-choice controls](https://developers.openai.com/api/docs/guides/function-calling#tool-choice).
The decision loop rejects replies containing zero or multiple actions.

## Bounds and failure behavior

Defaults are a 65,536-byte request ceiling, an 8,192-byte comparison-result
ceiling, and a 16,384-character complete-instructions ceiling. Measurements
use serialized UTF-8 bytes, including schemas, rather than assuming one
character is one byte. Tool arguments are bounded to at most 65,536 bytes.

`render_comparison_result(evidence, presentation=...)` returns complete,
canonical JSON. It never removes values, uncertainty, units, work, seed, or
status to fit a limit. Oversized presentation raises `PromptSizeError` before
model dispatch; unsupported versions raise `PresentationVersionError`;
invalid logical tool arguments raise `ToolSchemaError`.

Only the public-history suffix is deliberately shortened. Candidates,
evidence, and continuation are never truncated to make a request fit. The
decision loop records preparation/presentation failure and chooses its
explicit fallback or abort path. Task 11 must also check the final serialized
SDK payload; these offline checks do not validate a live provider response or
guarantee a runtime budget.

## Demo and validation

From the project root:

```sh
.venv/bin/python scripts/demo_decision_context.py --prompt-output docs/decision-prompt-demo.txt
.venv/bin/python -m pytest tests/harness/test_prompts.py tests/harness/test_tool_schemas.py
.venv/bin/python -m pytest tests/harness/test_prompts_native.py
```

The demo saves a real NWL23 public view plus the function payload in
`docs/decision-context-demo.json`, and optionally a readable prompt. It makes
no model call and commits no turn.

Golden snapshots freeze the initial and evidence-bearing final prompts.
At Task 3, the complete suite passed 400 tests: 372 offline and 28 native, including 52
new Task 3 checks.
Tests check private-state invariance of complete model requests, deterministic
history and multiset ordering, blank geometry, schema/parser agreement,
malformed/duplicate arguments, exhausted and unsupported-phase tool exposure,
request/result bounds, version rejection, and canonical evidence-backed
selection. Native checks compare all 225 premium squares with MAGPIE and
exercise real NWL23 preparation, comparison, result rendering, and final
selection. A wheel check verifies that the versioned JSON asset ships and
loads outside the source checkout. No API key or paid call is used.
