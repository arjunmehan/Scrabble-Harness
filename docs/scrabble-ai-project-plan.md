# Scrabble Harness: a Scrabble agent that learns how to spend its thinking budget

Research and recommendation prepared October 6, 2026.

**Recommendation:** build an adaptive LLM harness over a native Scrabble engine, train a small controller to choose how much analysis a position deserves, and ship an interactive replay viewer plus a reproducible evaluation. The principal engineering question is whether intelligently allocating model calls and local search improves the quality–cost tradeoff over fixed workflows.

This is a project proposal, not an implementation or a claim of measured improvement. The architecture choices below are my assessment of the best fit for an experienced programmer with about three weeks, an M4 Pro MacBook, and a roughly $100 API budget. Assume approximately 60–90 focused development hours; with substantially fewer hours, keep the fixed harness and replay viewer and postpone the learned router.

The deliverable should be a working assisted Scrabble agent, not a promise to defeat leading engines or to train a frontier model. Its distinctive work is the controller, experiment design, engine integration, and observable decision process.

**1. What the research changes about the idea**

Several tempting features already exist. The following sources are important because they establish a realistic starting point, rather than because every feature should be copied.

| Source | Relevant existing work | Implication for this project |
|---|---|---|
| [ScrabbleBench](https://sunnybala.github.io/scrabblebench/) | LLM games, assisted placement, retries, and a position dataset. | A leaderboard and basic retry loop have precedents. |
| [MAGPIE](https://github.com/jvc56/MAGPIE) | Move generation, simulation, inference, autoplay, and endgames. | Reuse game algorithms and concentrate effort on the harness. |
| [MAGPIE PlayChooser source](https://github.com/jvc56/MAGPIE/blob/main/src/impl/play_chooser.c) | Existing time allocation and selection of analysis appropriate to game phase. | Adaptive analysis is not new by itself; include the native policy as a comparison. |
| [Macondo changelog](https://domino14.github.io/macondo/changelog.html) | Neural evaluation, Bayesian rack inference, and generative move explanations. | Opponent inference and a chatbot explanation are insufficient differentiators. |
| [Scribblez](https://github.com/eigen7/Scribblez) | A separate project pursuing neural Scrabble evaluation and self-play training. | A large training effort overlaps existing work and exceeds this schedule. |
| [Mastering Board Games by External and Internal Planning with Language Models, ICML 2025](https://proceedings.mlr.press/v267/schultz25a.html) | Domain-trained language models combined with planning in several board games. | Learn from planning methods, but do not assume those results transfer to an off-the-shelf Scrabble agent. |
| [Budget-Aware Tool-Use Enables Effective Agent Scaling](https://arxiv.org/abs/2511.17006) | Budget tracking and adaptive tool use for search agents. | Make remaining resources explicit and evaluate complete scaling curves. |
| [Rational Metareasoning for Large Language Models](https://arxiv.org/abs/2410.05563) | Learning when additional reasoning is worth its cost. | Use a small cost-sensitive controller as a practical adaptation of this idea. |

I did not establish that nobody has built the proposed combination. The recommendation is about a strong, feasible portfolio project, not an unverified novelty claim.

**2. What a user should see**

A viewer loads a real board position, its rack, and the score. The user compares an unaided response, a fixed assisted agent, and the adaptive agent. Each decision has a timeline: candidates considered, tool requests, observed results, time and API cost consumed, and the submitted move.

The viewer also permits a fork from a saved position. Increasing the analysis budget reruns the decision or displays a previously recorded run. The comparison shows whether additional computation changed the move and whether the reference evaluation improved. Recorded examples should work without an API key.

An illustrative sequence, not a measured result: a model initially prefers a high-scoring move; a simulation finds that it gives the opponent access to a strong scoring lane; the agent selects a lower-scoring alternative. A second position demonstrates an early stop because further search does not change the decision. A third demonstrates an unsuccessful extra review, so the product also makes wasted computation visible.

Present tool actions and brief decision notes. Do not present them as a faithful reconstruction of hidden model reasoning. Any narrative explanation should link its numerical and tactical claims to recorded tool evidence.

**3. Architecture and responsibilities**

The system has six small components:

1. A native engine owns legality, scoring, move generation, and simulation.
2. An observation adapter exposes only information the player can know.
3. A Python harness executes a bounded decision workflow and tracks its budget.
4. An interchangeable LLM proposes candidates, requests analysis, and selects a move.
5. A learned router chooses a fast, standard, or deeper workflow from cheap position features.
6. An append-only event log supports replay, evaluation, and training-data export.

The engine owns the game state. The model owns strategic proposals and selections. The router owns resource allocation. The evaluator judges the final action independently of the model's explanation.

Use MAGPIE as the first engine candidate. Its [C embedding API](https://raw.githubusercontent.com/jvc56/MAGPIE/main/src/impl/cmd_api.h) exposes synchronous and asynchronous execution, status, cancellation, and returned-string ownership. There is also a [Python ctypes example](https://raw.githubusercontent.com/jvc56/MAGPIE/main/examples/magpie.py). These are documented integration surfaces, not evidence that a particular build has been tested on your MacBook.

Put each engine instance in a worker process and allow only one active command per handle. A worker crash should fail one job, not corrupt the entire experiment. Start with the simplest subprocess integration if it works; use the shared library when parsing and cancellation are under control. Pin an engine commit. Prefer a narrow adapter to a large fork.

The first integration spike must confirm that the chosen engine can import/export a position, validate and commit a move, generate candidates, run bounded analysis, and represent an unknown opponent rack. Candidate-restricted simulation may require filtering or a small extension. Verify it early rather than assuming every proposed tool is already a native command.

**4. Agent tools and turn loop**

Expose a small, typed tool surface. These are proposed wrapper APIs, not names of existing MAGPIE commands.

| Tool | Responsibility | Resource accounting |
|---|---|---|
| `legal_candidates` | Return a bounded, diverse set of legal placements, exchanges, and pass where appropriate. | Record engine time, metadata exposed, candidate count, and generation policy. |
| `preview_move` | Return exact score, resulting board changes, and remaining rack. | A cheap deterministic call. |
| `compare_candidates` | Run bounded simulations and return estimates and sample counts. | Count simulation work and elapsed time; distinguish estimates from facts. |
| `solve_small_endgame` | Analyze a tractable bag-empty position. | Record completion/proof status; an interrupted search is not an exact answer. |
| `commit` | Atomically submit one validated action. | One state transition; retain the source decision and validation result. |

Generate a larger candidate pool locally but send only a short list to the model, initially about 8–12 moves. Include variety in placements and tiles retained, rather than sending hundreds of near-duplicates. Candidate truncation can exclude the best move; measure that ceiling separately. Keep static metadata consistent across the assisted-agent comparisons.

Give moves stable IDs derived from their placement, new tiles, and blank assignments. The model can choose a candidate ID rather than repeatedly reconstruct coordinates. This deliberately measures assisted play; keep a coordinate-output baseline to measure the unassisted problem.

The turn starts with a legal fallback and cheap position features. The router selects a workflow. The LLM chooses candidates and can ask for more evidence. The harness validates the request, executes a bounded tool call, summarizes its results, and asks for a final selection when needed. A hard ceiling ends the turn and records any fallback use.

A useful hypothesis is specific and testable: compare two placements under sampled opponent replies, or broaden candidates that retain a useful rack. Avoid paying for unrestricted prose or a debate among several models. A short structured request is enough.

**5. The learned component**

For the three-week version, learn to choose among three workflows:

- **Fast:** one model decision using legal candidates and exact move metadata.
- **Standard:** a bounded comparison of selected candidates followed by a model decision or revision.
- **Deep:** a larger comparison budget or wider candidate set, with a strict maximum on model calls.

The first router can use hand-written rules: small candidate gaps receive more analysis; obvious choices receive less. Then replace it with a cost-sensitive supervised model. A gradient-boosted tree model is a sensible starting point for tabular features; [scikit-learn's ensemble documentation](https://scikit-learn.org/stable/modules/ensemble.html) provides the relevant implementation choices. A large neural network is unnecessary at this stage.

Candidate features include tiles remaining, score difference, rack composition, blank count, legal-action count, cheap static candidate gaps, board occupancy, game phase, model identity, and remaining budget. Include only features available before the selected workflow runs. A true opponent rack or a high-budget reference result must never become an input feature.

Collect training examples by running the available workflows from the same saved training positions. Evaluate their selected actions using a separate, higher-budget reference. Each record contains the workflow, observed action quality, API spend, and native analysis work. Train models to predict quality and cost separately, then choose the best predicted workflow that fits the constraints. This avoids silently treating a dollar, a second, and a simulation as equivalent.

Use cheap local engine runs to create and characterize many positions. Start paid workflow labels on a smaller stratified sample, then enlarge it only if the pilot cost supports doing so. Local engine labels can teach a router when more engine search matters; they cannot by themselves prove which LLM workflow is best. That distinction must be preserved in the report.

Training the router is an offline learning step. It is not fine-tuning the LLM, and the agent does not automatically learn from each game. Freeze the router, prompts, and settings before final evaluation. A later extension could learn individual tool actions or use a contextual bandit; that is beyond the core three-week commitment.

**6. Hidden information, rules, and reproducibility**

Use two-player, standard-board play with one explicitly versioned lexicon and automatic dictionary adjudication. Include exchanges, passes, blanks, final rack penalties, and the chosen scoreless-turn ending convention. Automatic adjudication removes the separate challenge/bluffing problem; describe that choice explicitly.

Maintain separate records for full arbiter state and player observation. The player sees its own rack, board, score, public history, and publicly inferable unseen inventory. Its simulation tool samples plausible hidden racks and draws from that information. It must not receive the actual opponent rack or future bag order from the arbiter.

A particularly valuable test: hold the public observation fixed, swap the arbiter's hidden rack and bag ordering, and confirm the analysis request and seeded tool behavior are unchanged. This catches information leakage that could otherwise invalidate the entire experiment.

Represent board blanks with both displayed letter and actual blank identity. Cache keys must include board state, blank flags, rack, scores, lexicon, engine version, workflow configuration, and analysis seed. Recorded responses can reproduce analysis; API reruns should not be advertised as guaranteed identical. The [MAGPIE README](https://github.com/jvc56/MAGPIE) also notes that clock-limited games are not reproducible from seed alone. Use fixed-work analysis for controlled comparisons, and preserve complete logs for timed demonstrations.

Keep dictionary acquisition separate from code packaging and record its version. NASPA's [international support guidance](https://naspa.games/w/International_Support_in_NASPA_Zyzzyva) directs developers to check word-list distribution terms with the publisher. Resolve this during setup rather than bundling a list merely because it is downloadable. Keep the engine's license visible in the repository and packaging.

**7. Evaluation that makes the project credible**

Begin with one inexpensive tool-capable model and add a second after the pipeline is stable. Model choice should follow a small measured pilot of valid structured responses, decision quality, latency, and cost. A stronger model is a limited reference sample, not the default for every turn.

| Comparison | Purpose |
|---|---|
| Unaided LLM | Establish the original problem and classify illegal outputs. |
| Fixed assisted workflow | Test whether adaptive allocation adds value beyond tool access. |
| Hand-written adaptive router | Test whether the learned model beats simple engineering judgment. |
| Learned router with the same tools | The principal proposed system. |
| Native static and budgeted-search policies | Determine what a conventional engine achieves without an LLM. |

The most important comparison is learned versus fixed and heuristic workflows using the same model, observation, and tool surface. Native-engine comparisons are essential for attribution: beating the unaided model does not establish that language reasoning contributes beyond the engine.

Start with several hundred reachable positions generated from games played by different policies. Split by source game or seed family, not by individual turns. Use training and development positions for prompts and routing. Reserve roughly 100–200 positions for the final comparison if the measured pilot cost permits. These are initial scope targets, not a statistical power calculation.

Stratify by opening, midgame, late game, blanks, exchanges, and crowded boards. Include a small set of endgames that actually finish exhaustive solving. For midgame positions, compare chosen moves with a higher-budget reference and label the resulting regret as estimated, not exact. Use independent simulation seeds for reference evaluation; match hidden scenarios across candidates when supported and valid. Report unresolved close comparisons.

Measure proposed-move legality, fallback rate, estimated strategic regret, solved-endgame success, model calls, billed/estimated API cost, engine work, and latency. Show quality versus cost and quality versus local computation separately. Bootstrap paired differences, grouping related positions by source game. Do not infer human skill levels from bot results.

Full games demonstrate sustained behavior but cost many more model decisions. Start with a modest batch against fixed reference opponents, rotate starting seats, and expand only after estimating spend. Treat small game batches as demonstrations with broad uncertainty, not a precise Elo ranking. API costs, CPU budgets, and shared seeds do not make all game continuations equivalent.

Ablations should answer specific questions: does learned routing outperform a gap-based rule; does the LLM improve selection over using the same engine results directly; does extra review improve quality; does a wider candidate set fix misses? Avoid an exhaustive feature matrix that consumes the budget.

**8. Technology choices and the skills they demonstrate**

| Layer | Proposed technology | Skill demonstrated |
|---|---|---|
| Game/search core | MAGPIE native C engine, isolated behind a narrow Python adapter | Native integration, process lifecycle, performance measurement |
| Harness | Python, asyncio, a small explicit state machine | Reliable agent orchestration, bounded execution, retries and cancellation |
| Data contracts | [Pydantic](https://docs.pydantic.dev/latest/) | Typed tool interfaces and state validation |
| Model integration | Thin provider adapter; optionally [LiteLLM](https://docs.litellm.ai/docs/completion/token_usage) | Provider independence, usage accounting, model configuration |
| Learned router | scikit-learn gradient boosting | Feature design, supervised learning, held-out evaluation |
| Persistence | JSONL event traces and SQLite run manifests | Event logging, recovery, auditability |
| Analysis | NumPy/pandas or equivalent plus ordinary plotting | Paired experiments, uncertainty, performance/cost analysis |
| Viewer | [React](https://react.dev/) + TypeScript; [FastAPI](https://fastapi.tiangolo.com/) only for live reruns | Interactive debugging and full-stack integration |
| Verification | pytest and [Hypothesis stateful tests](https://hypothesis.readthedocs.io/en/latest/stateful.html) | State invariants and meaningful automated verification |
| Reproducibility | [uv](https://docs.astral.sh/uv/), pinned dependencies, GitHub Actions | Repeatable setup and continuous integration |

The static replay viewer can consume exported JSON without a backend. Add live execution only after the core works. Store provider-reported usage whenever available and mark cost estimates as estimates. LiteLLM's price map is community-maintained, so reconcile important totals with actual billing.

Keep the setup small. Kubernetes, a vector database, multi-agent debate, and a GPU training stack each add work without resolving the main question.

**9. Three-week sequence and decision gates**

| Days | Work | Concrete completion condition |
|---|---|---|
| 1–3 | Engine spike, observations, stable move IDs, rule fixtures | Play a complete local game and demonstrate no hidden-information leakage. |
| 4–7 | One-model fixed harness, budgets, traces, baseline pipeline | Run a small comparison end to end and replay every submitted action. |
| 8–10 | Position generation, training labels, simple router rules | Produce a clean grouped split and measured workflow costs. |
| 11–14 | Train small router and build replay comparison view | Compare learned and heuristic routing on development data. |
| 15–18 | Freeze settings, run held-out comparisons and limited games | Produce quality/cost charts with paired uncertainty and all failures retained. |
| 19–21 | Finish demo, documentation, setup, report, and short video | Another person can reproduce the saved-results analysis and inspect examples without API spend. |

If engine integration fails by day 3, switch to a supported executable interface or Macondo and simplify the tools. If no workflow differences appear by day 7, examine candidate quality and choose harder development positions before spending on learning. If learned routing does not beat a simple rule by day 14, keep and report that result, ship the heuristic version, and prioritize the evaluation and viewer. Do not spend the final week hiding a negative result in more tuning.

**10. Budget and hardware**

Use the M4 Pro for native move generation, local simulation, classical ML, and the viewer. These core components do not require CUDA or an external GPU. The engine still needs a build and runtime benchmark on the actual machine; difficult exhaustive endgames can be expensive even with a fast CPU. Choose tractable cases and impose hard work limits.

| API allocation | Spending ceiling |
|---|---:|
| Initial model and cost pilot | $10 |
| Harness development and training labels | $25 |
| Frozen evaluation and limited games | $45 |
| Stronger-model spot checks | $10 |
| Reserve | $10 |
| Total | $100 |

These are spending allocations, not claims about how many calls a particular model price will buy. After the pilot, estimate total spend as number of decisions × model calls per decision × measured mean cost per call, plus retries and game runs. Cap each phase. Reduce models, repeated runs, or full games before increasing the budget. Preserve the final evaluation allocation instead of spending it all on prompt iteration.

**11. Finish line and honest claims**

The finished repository contains a runnable agent, documented tools, a learned or heuristic budget router, saved example games, position splits, a reproducible report, and a viewer that makes decisions inspectable. Include a short video showing a corrected decision, an early stop, and a case where more analysis failed to help.

The desirable outcome is better decision quality at comparable resources, or fewer calls/search work at similar quality, compared with fixed assisted workflows. Those are hypotheses to test. Beating a leading Scrabble engine is not the acceptance criterion.

If the learned router or LLM fails to add value, the finished project can still demonstrate reliable agent infrastructure and a careful measured comparison, but its resume wording must reflect that result. A conditional resume bullet is: “Built an adaptive LLM Scrabble agent integrating a native search engine, learned budget routing, and replayable tool traces; reduced [measured resource use] by [measured amount] at [measured decision quality] across [held-out scope].”

The strongest later extension would be per-step allocation of computation learned from larger decision traces. Opponent-rack inference, adversarial position generation, and model fine-tuning can follow after a successful core; they should not compete with finishing this version.
