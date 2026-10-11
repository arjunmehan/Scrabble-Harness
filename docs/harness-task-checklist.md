# Scrabble Harness — chronological task checklist

October 11, 2026 · **Status: Tasks 1–11 complete; Tasks 12–27 planned**

This checklist turns the [harness implementation plan](harness-implementation-plan.md)
into individual work items. Task numbering starts again for the harness; the
existing engine foundation and full NWL23 setup are already complete.

Complete the required tasks in order. Each task includes its verification work;
do not defer all testing until the end. Use fake providers for development,
then run a small paid pilot only after accounting and termination work.

## First working agent: tasks 1–12

- [x] **Task 1 — Define harness contracts and configuration.**
  Add `harness/contracts.py` and the provider-neutral records in
  `providers/protocol.py`. Define strict actions, evidence, `HarnessConfig`,
  `DecisionLimits`, `DecisionOutcome`, model requests/replies, capabilities,
  usage, and an execution manifest. Reuse existing domain models and integer
  microusd amounts.
  **Done when:** configuration/action fixtures round-trip, malformed fields
  fail, and the harness API accepts only player-visible observations.
  **Implemented:** strict records, capability/usage/request validation, bounded
  provider continuation, and content-verified execution manifests. See the
  [foundation guide](harness-foundation.md).

- [x] **Task 2 — Build the frozen decision context.**
  Implement `harness/context.py`. Generate one 40-candidate pool, retain its
  canonical IDs, choose the static fallback, and select 12 visible candidates
  including fallback/pass/exchange options where available. Shuffle with a
  recorded seed and assign short handles afterward. Own the visible-handle map
  and issued-evidence registry for one decision.
  **Done when:** identical public input/seeds give identical presentation,
  stale/unknown handles fail, and preparation failure aborts before a paid call.
  **Implemented:** one generated pool, versioned seeded presentation, verified
  static fallback, scoped handles, immutable issued evidence, and a real NWL23
  integration check. Run `scripts/demo_decision_context.py` for a public view.

- [x] **Task 3 — Create prompts and tool schemas.**
  Implement `harness/prompts.py` and versioned schemas for `select_move` and
  `compare_moves`. Show the board/premiums/blanks, own rack, public inventory,
  scores, candidate placements, exact scores, leaves, and remaining limits.
  Keep native rank/static equity out of the primary model presentation. Set
  request/result size bounds and deterministic public-history presentation.
  **Done when:** prompt snapshots are stable and changing private racks/draws
  cannot alter provider input; strict schemas and last-round selection work.
  **Implemented:** deterministic labeled board/premium/blank presentation,
  bounded public-history suffix and evidence, complete model-request builder,
  packaged strict function schemas, argument validation, and forced final
  selection. Offline snapshots and full NWL23 checks pass. See the
  [prompt guide](harness-prompts.md).

- [x] **Task 4 — Implement the persistent run store.**
  Add `runs/store.py`, database migrations, and typed events. Store campaigns,
  runs, decisions, attempts, reservations, and commit records with foreign keys,
  stable identifiers, unique revision records, and per-run event sequences.
  Use local SQLite with durable settings and private file locations.
  **Done when:** records survive reopening, transactions roll back cleanly,
  and conflicting or duplicate identities cannot silently overwrite data.
  **Implemented:** private durable SQLite, checksum-verified packaged migrations,
  typed scoped records/events, idempotent writes, append-only evidence,
  transactional dispatch/reservation states, unique commit revisions, and
  cross-process writer serialization. Crash/reopen, rollback, concurrency,
  and real NWL23 receipt checks pass. See the [run-store guide](harness-run-store.md).

- [x] **Task 5 — Implement resource accounting and admission.**
  Build `runs/ledger.py` and `harness/budget.py`. Atomically reserve cost,
  attempt slots, and native work against campaign/run/decision caps; enforce
  elapsed time separately. Add rate cards, conservative request-cost bounds,
  settlement, unresolved usage, and reservations for a final selection call.
  **Done when:** competing reservations cannot overspend; retries and malformed
  replies remain billable; timed-out API/native work retains unresolved allowance;
  restart cannot reset the campaign budget.
  **Implemented:** atomic admission across all scopes, saved price/input bounds,
  cache-category accounting, durable finish holds, one-use dispatch permits,
  persisted deadlines, conservative settlement/restart handling, and saved
  native receipts. Concurrent threads/processes, hard exits, v1 upgrades, and
  real NWL23 work accounting pass. See the [budget ledger guide](harness-budget-ledger.md).

- [x] **Task 6 — Implement the scripted provider.**
  Add `providers/fake.py` using the Task 1 protocol. Script selections, tool
  requests, refusals, malformed/multiple actions, missing usage, transport errors,
  and delayed replies. Assign explicit attempt and response identities.
  **Done when:** later harness tests can reproduce each failure without an API
  key, network connection, or real charge.
  **Implemented:** immutable ordered scripts, explicit attempt/response identities,
  selection/comparison helpers, raw malformed/multiple actions, statuses/unknown
  usage, transport exceptions, controlled delays/cancellation, exact request
  matching, and inspection logs. Fifty new offline tests cover provider behavior
  and durable ledger integration; all nine demo scenarios run without a paid call.
  See the [scripted provider guide](harness-scripted-provider.md).

- [x] **Task 7 — Implement typed tool execution.**
  Add `harness/tools.py`. Resolve only visible handles, validate cited evidence,
  and connect comparison requests to the existing analysis adapter. Configure
  60/300 total-rollout tiers, respect remaining time/work, save evidence, and
  disable comparison outside supported game phases. `select_move` returns a
  decision rather than committing a game.
  **Done when:** real NWL23 comparison results are recorded faithfully; forged
  evidence, excessive requests, and unsupported comparisons fail before dispatch.
  **Implemented:** saved-reply execution, offered-tool and displayed-evidence
  validation, 60/300 total-rollout tiers, scoped durable intents, clipped native
  limits, faithful receipts/issued evidence, conservative failure/cancellation,
  and receipt reuse without redispatch. Forty new tests include both real NWL23
  tiers and unchanged game checkpoints. See the [tool execution guide](harness-tools.md).

- [x] **Task 8 — Implement the bounded decision loop.**
  Add `harness/loop.py`. Connect preparation, reservations, provider requests,
  action validation, tool execution, evidence, selection, repair, fallback,
  and abort. Save replies before executing their actions; reserve enough
  capacity to finish after a tool. Apply one absolute decision deadline.
  **Done when:** scripted select/compare/select sequences finish; invalid outputs,
  refusals, repeated actions, exhausted budgets, and cancellation terminate
  predictably without unaccounted extra requests.
  **Implemented:** preparation through durable completion; protected final-selection
  holds; saved/accounted replies before tools; bounded selection-only repair and
  explicit retries; repeat detection; one absolute deadline; static fallback;
  cancellation aborts; and completed-outcome reuse without replay. Thirty-eight
  new tests include real NWL23 loops at both tiers. See the
  [decision loop guide](harness-loop.md).

- [x] **Task 9 — Add fixed workflows and fallback policies.**
  Implement `harness/policies.py` and initial JSON presets: Fast = 1 model
  attempt/0 comparisons; Standard = 2 attempts/1 comparison/300 rollouts;
  Deep = 3 attempts/2 comparisons/600 rollouts. Define static and optional
  latest-comparison fallback behavior. Keep origins and reasons explicit.
  **Done when:** each preset obeys its ceiling, can stop early, and offers only
  selection on its final permitted model request.
  **Implemented:** packaged strict Fast/Standard/Deep configurations; static
  and optional newest-full-coverage comparison fallback; deterministic ties;
  explicit durable model/static/comparison origins and evidence citations;
  legacy outcome compatibility. Thirty-four new tests include all three
  workflows with real NWL23 and Deep at 600 rollouts. See the
  [policy guide](harness-policies.md).

- [x] **Task 10 — Complete an offline single-position harness.**
  Add the first `decide` CLI path using a scripted provider, persisted records,
  and the real native adapter. Run the complete prepare/compare/select path
  from a reachable NWL23 position. Add acceptance checks across all resource
  ceilings, evidence validation, and missing/uncertain usage.
  **Done when:** a command produces a canonical decision, evidence, and a
  reconciled resource record without a provider network call.
  **Implemented:** module/installed `decide` command, seeded reachable NWL23
  opening, scripted workflows/failures, strict reduction-only limits, private
  SQLite/output, real source/config fingerprint, validated canonical moves,
  unchanged game checkpoints, and complete known/unknown resource reconciliation.
  Thirty-six new tests include 15 native command invocations across all ceilings
  and evidence/usage failures. See the [offline CLI guide](harness-offline-cli.md).

- [x] **Task 11 — Implement the real provider adapter.**
  Add `providers/openai.py`, pin the SDK, and translate Responses function
  calling into the Task 1 protocol. Preserve required continuation and usage;
  disable automatic retries and parallel tools; reject unsupported settings.
  Keep credentials and opaque continuation outside public exports.
  **Done when:** fake transport tests cover request serialization, tool-call
  correlation, continuation, refusal/truncation, usage, and explicit retries.
  **Implemented:** pinned optional OpenAI SDK, stateless Responses adapter,
  strict single-call transport, scoped private continuation, correlated public
  comparison results, conservative usage, deadlines, and sanitized failures.
  Forty-seven offline tests cover SDK serialization and complete Standard/Deep
  workflows, saved receipts, repairs, and separately admitted retries. See the
  [provider guide](harness-openai-provider.md).

- [ ] **Task 12 — Run the first model/cost pilot.**
  Choose one actual model configuration and verified rate card. Enable real
  `decide` requests, run a small reachable-position sample, and measure action
  validity, tool use, latency, tokens, estimated cost, and native runtime.
  Calibrate output caps, rollout tiers, and decision limits within a $5 pilot cap.
  **Done when:** one real model completes a bounded decision and comparison;
  saved usage reconciles; measured costs support a forecast for the remaining
  evaluation. Avoid selecting the final model lineup before this gate.

**Milestone:** an LLM can request evidence and select a legal candidate with
recorded limits, spending, and fallback behavior.

## Complete games and recovery: tasks 13–16

- [ ] **Task 13 — Share authoritative turn-transition logic.**
  Extract the existing arbiter's state application into a pure host helper used
  by commit and recovery. Reuse the existing draw functions, score rules, and
  checkpoint identities; keep native legality validation inside the arbiter.
  **Done when:** placements, blanks, exchanges, passes, and both ending rules
  preserve their existing behavior; the engine regression suite passes.

- [ ] **Task 14 — Connect the harness to a game runner.**
  Implement `runs/runner.py` and `play`. Start games with durable checkpoints,
  provide only the active player's observation to the harness, validate its
  decision against the trusted pool, and commit with the expected revision.
  Persist commit intent/source checkpoint and confirmation. Hold one per-game
  ownership lock and enforce run/campaign caps across turns.
  **Done when:** a scripted agent finishes a complete NWL23 game and no invalid,
  stale, or duplicate selection can advance state.

- [ ] **Task 15 — Implement interrupted-run reconciliation.**
  Add `resume`. Recover saved replies/results without repeating paid work;
  mark dispatched attempts with missing responses unresolved. Reconcile commit
  intent with the authoritative checkpoint using the shared transition helper.
  Check complete exchange effects, not just the public exchange count.
  **Done when:** injected crashes before/after response saving and checkpoint
  publication cause neither duplicate billing attempts nor duplicate turns;
  conflicting transitions stop explicitly and uncertain usage remains recorded.

- [ ] **Task 16 — Implement offline replay and safe exports.**
  Add `runs/replay.py` and `replay`. Export player-perspective positions,
  displayed candidates, visible model actions, evidence, selections, costs,
  timing, and fallback status. Omit credentials, private checkpoints, and
  opaque continuation. Distinguish replay from a fresh stochastic trial.
  **Done when:** a recorded decision/game can be inspected without model or
  native calls, and exported records reconstruct the displayed decision sequence.

**Milestone:** a full agent game can run, recover, and be inspected offline.

## Fair comparison and evaluation: tasks 17–22

- [ ] **Task 17 — Generate and split the position corpus.**
  Add `evaluation/positions.py`. Collect reachable positions from several
  native policies; preserve source game, seed family, turn, phase, and tags.
  Deduplicate and split by source game/seed family. Freeze the test-position
  manifest before heuristic/prompt tuning; keep it unopened for tuning.
  **Done when:** groups do not overlap, test identity is fixed, and synthetic
  engine fixtures are absent from the performance corpus.

- [ ] **Task 18 — Build engine baselines and reference jobs.**
  Add highest-score, best-static, and automatic budgeted-search policies.
  Implement `evaluation/references.py` over the frozen candidate pools using
  larger local budgets and independent seeds. Start with single-candidate
  requests so reported work is attributable; mark incomplete references unresolved.
  Store labels separately from decision inputs.
  **Done when:** reference jobs resume, coverage/counts/settings are recorded,
  engine baselines use declared caps, and labels cannot leak into the harness.

- [ ] **Task 19 — Add the heuristic workflow router.**
  Use pre-decision public/static features and remaining budgets to select
  Fast, Standard, or Deep. Disable comparisons outside simulation scope.
  Tune thresholds on development positions and compare against fixed presets.
  **Done when:** routing is deterministic, affordable, independent of reference
  labels/hidden state, and versioned with its development configuration.

- [ ] **Task 20 — Add the second model configuration.**
  Use the same provider adapter and a small development-cost sample to confirm
  capability and affordability. Declare effective settings and reuse the same
  candidate presentation, schemas, and tool semantics. Forecast the full matrix
  from measured costs; reduce its size if needed.
  **Done when:** both models complete the same workflow fixtures and the proposed
  position/policy/trial matrix fits the campaign budget and evaluation allocation.

- [ ] **Task 21 — Freeze the final experiment configuration.**
  Lock prompts, models/settings, schemas, workflow/router versions, rate cards,
  engine fingerprint, candidate-order/analysis seeds, references, trial IDs,
  resource caps, and result metrics. Tune only using training/development data.
  Finalize the report/export schema before spending evaluation funds.
  **Done when:** one manifest can reproduce the declared setup, every job has a
  stable identity, and no final-test output has been used to adjust a policy.

- [ ] **Task 22 — Run and analyze the frozen position benchmark.**
  Implement `evaluate`, `evaluation/metrics.py`, and `report`. Run the planned
  model/policy matrix and engine baselines with persistent job status and spend
  admission. Repeat an affordable subset. Export estimated pool-relative loss,
  raw validity, served behavior/fallback, calls, costs, work, latency, failures,
  and missing references. Produce paired comparisons and source-game uncertainty.
  **Done when:** the report reruns from saved results without paid calls, credits
  fallback separately, and includes failures or negative outcomes. Keep this
  stage within its $50 evaluation allocation.

**Milestone:** at least two models and simpler engine policies have a fair,
inspectable comparison with declared quality and resource metrics.

## Demonstration and release: tasks 23–24

- [ ] **Task 23 — Record a small full-game demonstration.**
  Use the frozen setup in a cost-sized game batch, rotate starting seats, and
  save complete replay bundles. Include an example of tool use, early stopping,
  recovery, and fallback. Share the $15 allocation with evaluation repetitions;
  account for any demonstration already recorded during development.
  **Done when:** the examples show sustained valid play, costs are recorded,
  and game results are presented without an unsupported precise strength rating.

- [ ] **Task 24 — Finish setup, documentation, and release artifacts.**
  Document commands/configuration, supported limits, budget semantics,
  information boundaries, licensing references, and benchmark methodology.
  Verify a clean installation and offline replay; save example traces, result
  tables, figures, and a brief demo. Record the final validation results.
  **Done when:** another developer can set up the project, inspect examples
  without credentials, and understand both measured results and limitations.

**Required release is complete after Task 24.**

## Optional tasks and where they fit

- [ ] **Task 25 — Build the replay viewer.**
  After Task 16, and when required work is on schedule, add the static
  React/TypeScript viewer for board/rack state, candidate moves, action/evidence
  timeline, budgets, and paired policy examples. Estimate 6–10 additional hours.
  **Done when:** exported examples load without API access or a live backend.

- [ ] **Task 26 — Train and evaluate a learned router.**
  Branch after Tasks 17–19 only if matched workflow labels fit the development
  budget. Collect training outcomes, train small quality/resource predictors,
  and validate on development groups. Freeze the router before Task 21 if it
  will join the first final evaluation. Estimate 8–12 additional hours plus
  label-generation cost/time.
  **Done when:** a saved artifact with a feature/data manifest improves the
  measured tradeoff or its negative result is reported. If added after final
  test results have been inspected, use a fresh held-out corpus for its claim.

- [ ] **Task 27 — Extend tools or providers.**
  After the required harness works, add wider candidate presentation, previews,
  a second provider, or a separately scored unaided-output baseline as distinct
  interventions. Extend capability/budget tests and use a new experiment manifest.
  **Done when:** each extension has measured overhead and comparable evaluation;
  it does not silently alter already-frozen runs.

## Effort and spend checkpoints

| Required tasks | Planning allowance | Gate |
| --- | ---: | --- |
| 1–3 | 4–5 hours | Public input and strict contracts |
| 4–5 | 6–8 hours | Durable accounting before paid calls |
| 6–10 | 6–8 hours | Complete offline decision |
| 11–12 | 6–8 hours | First real model and cost pilot |
| 13–16 | 6–8 hours | Complete, recoverable game and replay |
| 17–22 | 12–16 hours | Frozen multi-model evaluation |
| 23–24 | 4–6 hours | Reviewable release |

The original 44–59-hour estimate applies to these grouped tasks, not to each
individual item. Allow approximately 50–65 hours with contingency. The pilot
is capped at $5, development at $20, final position evaluation at $50, and
repetitions/game demonstrations at $15; retain $10 for unresolved charges and
contingency. Enforce shared campaign admission so separate runs cannot reset
the spending allowance.
