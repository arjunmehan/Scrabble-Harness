# Scrabble Harness — technical implementation plan

Version 1.0 · October 6, 2026

**Objective:** build an assisted LLM Scrabble player that chooses how much analysis to perform, then measure decision quality, API spend, and local computation against fixed workflows.

**Constraints:** one experienced developer, approximately three weeks / 60–90 focused hours, M4 Pro MacBook, roughly $100 API budget. This document specifies the build; improvements must be established by experiments.

## 1. Scope and completion criteria

Ship two-player play on a standard board, one versioned lexicon, automatic dictionary adjudication, native engine integration, a bounded LLM harness, heuristic and learned workflow routing, replayable traces, and a held-out evaluation.

Include placements, blanks, exchanges, passes, and end-of-game scoring. Document the scoreless-turn ending convention and how rejected outputs consume the agent's budget. The arbiter must never apply an illegal move.

Completion means:
- A complete game runs locally and resumes from a saved committed state.
- Every decision can be inspected in a viewer without making API calls.
- Fixed, heuristic, and learned policies can be compared on identical positions.
- The report separates measured gains from estimates and includes failures.

Defer large-model training, online reinforcement learning, multi-agent debate, advanced opponent inference, live multiplayer, and cloud deployment.

## 2. Repository and boundaries

Suggested layout:

```text
scrabble-harness/
  pyproject.toml
  configs/                   # Engine, model, policy, evaluation, spend limits
  src/scrabble_harness/
    domain/                  # State, observations, moves, budgets, results
    engine/                  # Native worker, adapter, parsers
    tools/                   # Typed candidate, preview, comparison tools
    agents/                  # Provider adapter and bounded workflows
    routing/                 # Features, heuristic policy, learned policy
    runs/                    # Game runner, event log, checkpoints, ledger
    datasets/                # Generation, grouped splits, reference evaluation
    evaluation/              # Comparisons, metrics, analysis exports
    cli.py
  web/                       # React/TypeScript replay viewer
  tests/                     # Fixtures, invariants, integration checks
  data/                      # Local generated data; exclude secrets/large assets
  artifacts/                 # Released examples, reports, router models
```

Keep domain objects independent of engine commands, provider responses, and UI types. The adapter translates native output into domain objects; the harness uses those objects exclusively. Treat dictionaries and engine binaries as explicit setup dependencies.

## 3. Component contracts

| Component | Owns | Must not own |
|---|---|---|
| Arbiter / game runner | Full game state, draws, scores, legal commits | Model reasoning |
| Observation builder | Player-visible state and inferable tile inventory | Actual hidden rack or future draw sequence |
| Engine analysis adapter | Candidate generation and bounded analysis | Access to arbiter secrets during decisions |
| Harness | Tool execution, workflow state, limits, fallback | Scoring or word-validity judgments |
| LLM | Analysis requests and candidate selection | Arbitrary state mutation |
| Router | Choice of workflow | Future results or reference labels |
| Evaluator | Reference values and comparison metrics | Feeding labels into live decisions |
| Viewer | Display of exported events | Requiring paid calls to inspect examples |

## 4. Data models — implement first

Use Pydantic models with explicit schema versions and strict validation at external boundaries. Internal coordinates are zero-based; convert to Scrabble notation only at the adapter/display boundary.

| Record | Minimum fields |
|---|---|
| BoardTile | row, column, displayed_letter, is_blank |
| GameState | game_id, revision, board, both racks, draw state, scores, active_player, scoreless_turns, ruleset_id, lexicon_id |
| PlayerObservation | observation_id, revision, board, own_rack, scores, opponent_rack_size, bag_size, public_history, unseen_tile_counts |
| Move | kind, newly placed tiles or exchanged physical tiles; pass has no tile payload |
| Candidate | candidate_id, observation_id, move, exact_score, leave, optional static_equity, provenance |
| AnalysisResult | analysis_id, candidate values, metric_definition, work_used, elapsed_time, seed, completion_status |
| AgentDecision | observation_id, candidate_id, workflow, evidence_ids, brief_note, fallback_reason |
| RunConfig | model settings, engine commit, lexicon identity/hash, ruleset, policy version, candidate configuration, seeds, limits |

The unseen inventory combines the unknown rack and bag. It is not the exact bag composition. Preserve the physical identity of blanks in racks and on the board.

Hash a canonical observation and move representation for candidate identity. Include the observation identity so a candidate from an old board cannot be submitted to a new one.

**Acceptance:** fixture serialization round-trips; blank identity survives; stale decisions are rejected.

## 5. Native engine adapter — days 1–3

Start with MAGPIE. Pin a tested commit and prove the following operations before expanding the project:

```text
load_position(observation)
generate_candidates(configuration)
preview(move)
analyze(candidate_ids, work_limit, seed)
commit_to_arbiter(move, expected_revision)
export_checkpoint()
```

These are project interfaces, not native command names. Verify actual engine capabilities and output formats during the spike.

Use a worker process containing a native engine instance. Start with an executable protocol or the documented C/Python embedding interface, whichever reaches a reliable prototype faster. Serialize requests per engine handle; isolate crashes and enforce deadlines outside the native call.

Create separate arbiter and analysis contexts. Reconstruct the latter from the player observation, leaving hidden information unknown. If the native API cannot express that reliably, change adapters before collecting results.

Normalize errors into categories: invalid request, invalid move, unsupported operation, timeout, engine crash, incomplete analysis. Restart failed workers from the last committed checkpoint.

Candidate-restricted analysis may need adapter filtering or a narrow engine extension. Do not budget a major engine rewrite. Cancellation and sample-count semantics must be verified on the pinned build.

**Acceptance:** finish a local engine-only game; score and commit fixture moves; interrupt a long analysis; recover a worker without losing committed state.

## 6. Rules and information boundary — alongside the adapter

Delegate legality and scoring to the engine. Test the adapter around blanks, crosswords, premium-square reuse, exchanges, passes, tile conservation, and final penalties.

Never serialize GameState into an LLM request. Whitelist PlayerObservation fields. Likewise, do not give the decision-time analysis worker a checkpoint containing the real opponent rack.

Use a leakage test: hold PlayerObservation fixed, alter hidden arbiter rack/draw information, and confirm the observation, analysis request, and seeded single-worker result do not change.

For initial midgame simulation, use the engine's documented sampling from unseen tiles. Advanced history-based rack inference is optional. Bag-empty endgame analysis can use a rack derived from publicly known tile inventory.

**Acceptance:** hidden-state mutation tests pass; every applied move conserves physical tiles; preview does not mutate the arbiter.

## 7. Candidate tools — days 3–5

Generate a larger pool locally, then send an initial list of about 8–12 diverse candidates to the model. Preserve the complete pool for auditing.

Build the list from a deterministic mixture of strong static candidates and distinct placements/leaves. Include exchange and pass according to the configured rules. Record the selection policy and any engine-derived metadata shown to the LLM.

Expose:
- **Candidates:** obtain or broaden a bounded list.
- **Preview:** exact score, resulting placement, and remaining rack.
- **Compare:** estimated values for selected candidates under a work limit.
- **Small endgame:** optional completed-proof analysis.
- **Submit:** a selection request; only the arbiter performs the commit.

Randomize displayed candidate order with a recorded seed. Use the same pool and available metadata across principal assisted-agent comparisons. Treat broader lists in the deep workflow as a deliberate, measured intervention.

Analysis estimates require a named metric, depth/settings, sample count, and completion status. Do not label an interrupted endgame result “optimal.” Add uncertainty only where the adapter actually supports it.

**Acceptance:** candidate IDs resolve consistently; requests cannot exceed limits; a chosen candidate is revalidated against the current revision before commit.

## 8. Provider adapter and prompts — days 4–6

Start with one inexpensive tool-capable model. Add another only after a cost pilot.

Define a provider-neutral request/response layer. Retain provider-specific configuration and raw usage fields; avoid pretending reasoning settings are interchangeable across providers. A thin adapter or LiteLLM is sufficient.

Use a structured action union such as:
- select(candidate_id, evidence_ids, brief_note)
- compare(candidate_ids, analysis_tier)
- broaden(candidate_filter)

The host decides what the tiers mean. The model cannot request unlimited work or raw native commands.

Prompts contain the observation, candidate metadata, available actions, remaining limits, and compact tool results. Include an example of valid output. Keep explanations short and evidence-linked.

Log schema errors and tool failures. A format-repair call is allowed only inside the original call budget. Separate transport retries from decision retries, and count all requests conservatively until billing is reconciled.

**Acceptance:** the model returns valid selections or bounded requests on fixtures; bad output produces a logged fallback rather than a broken game.

## 9. Bounded workflows and budgets — days 5–7

Implement a small state machine:

```text
OBSERVE → PREPARE_CANDIDATES → ROUTE → REQUEST_MODEL
        → [VALIDATE_REQUEST → EXECUTE_TOOL → REQUEST_MODEL]
        → VALIDATE_SELECTION → COMMIT → CHECKPOINT
```

Each transition writes an event. Cache a legal fallback before any paid call.

Initial workflow presets:

| Workflow | Model-call ceiling | Native analysis |
|---|---:|---|
| Fast | 1 | Candidate generation and exact previews |
| Standard | 2 | At most one bounded candidate comparison |
| Deep | 3 | At most two comparisons or one broader comparison |

Tune native work limits after benchmarking the engine. Record the work unit's definition; do not assume a native “sample” equals a complete rollout. Endgames use a separate, capped route and are optional for the first working harness.

If the model selects early, commit early. If a request fails, exhausts a limit, or becomes unaffordable, finish with the best valid selection already available or the recorded fallback. Never silently give one policy extra attempts.

Track four separate limits: API dollars, model calls, engine work, and elapsed time. Reserve estimated maximum request cost atomically before dispatch; replace the reservation with observed usage afterward. Cost estimates can differ from billing, so retain a margin and reconcile totals. A request ceiling is not a guarantee of exact provider charges.

**Acceptance:** deliberately trigger each ceiling and verify graceful completion, correct accounting, and no duplicate commit.

## 10. Logging, recovery, and caching — begin on day 3

Use JSONL for events and SQLite for run manifests, ledger entries, checkpoint references, and job status.

Every event needs run/game/decision IDs, a sequence number, event type, timestamp, configuration hash, and payload. Capture tool requests/results, visible model outputs, validation failures, chosen move, resource usage, and completion status. Exclude API credentials.

Append one durable commit record per turn and checkpoint immediately afterward. Enforce uniqueness on game_id plus revision. Resume completed model/tool calls from saved results. Treat an interrupted API call without a recorded response as unresolved; rerunning it may incur another charge.

Cache keys include the full relevant observation/history, lexicon, engine version, model/prompt configuration, candidate configuration, analysis seed, and work limit. Distinguish cached replay from a new stochastic repetition: each independent model trial needs a different trial identity.

**Acceptance:** interrupt and resume a run; compare committed states; inspect logs to confirm a paid response was reused and a move was not applied twice.

## 11. Position dataset and reference evaluator — days 7–10

Generate reachable positions from games played by several cheap native policies. Retain source game ID, seed family, turn, game phase, and tags for blanks, exchanges, and crowded boards.

Deduplicate observations and split by source game/seed family into train, development, and test. Freeze the test manifest before prompt/router tuning. Start with hundreds of local positions; size paid comparisons from the pilot.

Reference evaluation is a separate job:
- For midgame, run higher-budget analysis with independent seeds and record engine-defined simulated equity, its settings, and limitations.
- For tractable bag-empty positions, obtain completed exact action values where supported. Otherwise solve the afterstate correctly from the opponent's perspective.
- Evaluate the selected action even when absent from the reference shortlist. Mark unsupported/unresolved values missing rather than inventing a loss.

Define midgame reference loss as the best evaluated candidate's equity minus the selected action's equity under the same reference protocol. This is an estimate relative to the evaluated pool, not a proof of global optimality. Report candidate-pool coverage separately.

Use exact final-spread regret for completed endgames. Keep it separate from the midgame learning target.

**Acceptance:** manifests have no game overlap; reference labels never enter player observations; labels specify metric, candidate coverage, and completion status.

## 12. Heuristic and learned routing — days 9–14

Implement the heuristic first: use game phase and cheap static candidate gaps to select a workflow. Calibrate thresholds on development data only.

Collect paid labels by running Fast, Standard, and Deep from the same training observations. Record each workflow's selected move, reference loss, API cost, and engine work. Repeat enough examples to gauge noise before expanding.

Extract only pre-workflow features: score difference, bag size, rack composition, blanks, board occupancy, legal-action count, static candidate gaps, model identity, and available budget.

Fit a small scikit-learn gradient-boosted model to predict reference loss per workflow; separately estimate resource usage. A shared model can include workflow as a categorical feature. Begin with one LLM to avoid spreading scarce labels across model families.

A simple selection rule:
1. Exclude workflows predicted to exceed limits.
2. Find the lowest predicted loss among feasible workflows.
3. Choose the cheapest workflow within a development-selected loss tolerance of that best prediction.
4. Enforce actual limits during execution.

This makes the quality/cost preference explicit without silently converting dollars into seconds. Freeze the tolerance, features, training data, and router artifact for final evaluation. Fall back to the heuristic if the model is missing or receives unsupported inputs.

**Acceptance:** held-out development comparison against the heuristic; no label leakage; saved model includes feature schema and training manifest. If learning adds no value by day 14, ship the heuristic and report the result.

## 13. Evaluation runner — scaffold week 1, finish days 15–18

Compare:
- Unaided coordinate-output LLM, as a diagnostic baseline.
- Fixed assisted workflow.
- Heuristic adaptive router.
- Learned router.
- Native static and budgeted-search policies.

For unaided play, define invalid-output handling in advance; for example, one invalid final submission becomes a pass. Report raw legality independently of assisted fallback behavior. Keep the principal fixed/heuristic/learned comparison identical in model, observations, tool surface, and global limits.

Export one result row per position/policy/trial: move, validity, fallback, reference loss, calls, API cost, engine work, latency, and run status.

Plot quality against actual API cost and native work separately. Compute paired differences and uncertainty, grouping related positions by source game. Keep unresolved references and failed runs visible. Report approximate midgame and exact endgame results separately.

Begin with roughly 100–200 frozen test positions only if projected spend permits. Repeat a subset to quantify model variability. Run a modest game batch with rotated starting seats as a secondary demonstration; do not report a precise Elo or human-equivalent rating from it.

**Acceptance:** analysis reruns from saved traces without model calls; all policies use declared budgets; the report includes negative outcomes.

## 14. Viewer — days 11–14 and 19–20

Use React/TypeScript to render exported traces. Start without a backend.

Build four views:
- Board/rack/score and move placement.
- Decision timeline with model actions and tool evidence.
- Side-by-side policy comparison.
- Quality, cost, and latency summary.

Allow stepping through turns and comparing recorded budget levels from the same observation. Display “estimated” versus “exact” evaluations and highlight fallback use.

A later FastAPI endpoint can trigger live reruns with server-held credentials and spend limits. It is optional; prerecorded examples are enough for the resume demo.

**Acceptance:** a new viewer user can inspect a correction, an early stop, and a failed improvement without an API key.

## 15. Build sequence and gates

| Days | Deliverable | Dependency / gate |
|---|---|---|
| 1–3 | Contracts, native spike, observations, engine-only game | If integration fails, change interface/engine and simplify |
| 4–5 | Candidate tools, one-model adapter, event logs | No hidden-information leakage |
| 6–7 | Fixed workflows, ledger, checkpoints, tiny comparison | Demonstrate full pipeline and measure cost |
| 8–10 | Grouped dataset, reference jobs, heuristic, workflow labels | Preserve evaluation budget; verify label quality |
| 11–14 | Learned router and replay viewer | Compare learning with the simple heuristic |
| 15–18 | Frozen evaluation and limited games | Stop tuning on test results |
| 19–21 | Documentation, example bundle, report, video, setup checks | Saved-result demo and analysis work without paid calls |

Do not wait for the UI or learned model to start testing. Week one should already produce complete runs and analyzable results.

## 16. Spend plan and final deliverables

| API use | Ceiling |
|---|---:|
| Initial model/cost pilot | $10 |
| Development and workflow training labels | $25 |
| Frozen evaluation and limited games | $45 |
| Stronger-model spot checks | $10 |
| Reserve | $10 |
| Total | $100 |

Forecast spend from observed cost per decision, including tool-result input, revisions, and retries. Reduce model count or game count before increasing the budget.

Deliver a runnable repository, pinned setup, policy/config files, router artifact, grouped dataset manifests, example traces, replay viewer, comparison report, and a short demo video. README claims must use measured results and credit the engine's contribution.

## 17. Integration references

These references support the engine/technology choices; wrapper contracts and limits above are proposed project design.

- [MAGPIE repository and setup](https://github.com/jvc56/MAGPIE)
- [MAGPIE embedding API](https://raw.githubusercontent.com/jvc56/MAGPIE/main/src/impl/cmd_api.h)
- [MAGPIE Python wrapper example](https://raw.githubusercontent.com/jvc56/MAGPIE/main/examples/magpie.py)
- [Macondo alternative engine](https://github.com/domino14/macondo)
- [Pydantic](https://docs.pydantic.dev/latest/)
- [scikit-learn ensembles](https://scikit-learn.org/stable/modules/ensemble.html)
- [LiteLLM usage/cost documentation](https://docs.litellm.ai/docs/completion/token_usage)
- [Hypothesis stateful testing](https://hypothesis.readthedocs.io/en/latest/stateful.html)

Confirm lexicon acquisition/distribution terms and preserve the native engine's license during setup. See the companion project recommendation for research background.

## Implementation progress — October 6, 2026

The six-step engine foundation is implemented in the project root:
package/contracts, domain models, canonical identities, pinned native capability
probe, concrete adapters/game transitions, and native limits/recovery coverage.
The latest [limits and recovery guide](engine-recovery.md)
records usage and validation. Durable private revision snapshots and compact
worker diagnostics are available; the saved recovery acceptance report compares
three restart games against uninterrupted baselines.

This foundation does not complete the later LLM workflow, paid-request ledger,
full decision replay/cache, dataset/reference evaluator, router, or benchmark
runner. Exact endgame solving remains optional future adapter work.

On October 7, 2026, the [full NWL2023 profile](nwl23.md) was added: 196,601 words
and 914,624 leave values, pinned and audited independently of the small interface
fixture. Generation, previews, bounded simulations, and a complete game pass
with that profile. The full suite passes 250 tests. The real lexicon and leave
assets are now available for the later workflow and evaluation layers.

The [agent harness implementation plan](harness-implementation-plan.md) now
specifies the next phase against the implemented interfaces. Use it for the
remaining package layout, workflow presets, budget/recovery design, and build
order; optional learning and viewer work have explicit completion gates.
