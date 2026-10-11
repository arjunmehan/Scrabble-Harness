from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import (
    Board, BoardTile, Checkpoint, ModelSettings, ObservationData,
    PassMove, PlacementMove, PlayerObservation, PublicExchangeMove, PublicTurn, TileInventory, build_observation,
)
from scrabble_harness.harness import (
    ContextScopeError, DecisionLimits, DecisionView, HarnessConfig, PresentationConfig,
    PresentationVersionError, PromptSizeError, RenderedPrompt, ScopedAction, VisibleCandidate,
    build_model_request, parse_tool_arguments, render_board, render_comparison_result, render_prompt,
)
from scrabble_harness.providers import ModelRequest, ProviderCapabilities, ProviderContinuation
from conftest import update

SNAPSHOTS = Path(__file__).parents[1] / "fixtures/harness"


def test_prompt_snapshot_is_stable_and_renders_public_facts(context):
    prompt = render_prompt(context.model_view(), context.config.decision_limits, config=context.config)
    assert prompt.instructions == (SNAPSHOTS / "decision-prompt-v1.txt").read_text()
    assert RenderedPrompt.model_validate_json(prompt.model_dump_json()) == prompt
    text = prompt.instructions
    assert "Own rack (7/7): GHIJKLM" in text
    assert "Scores: you=0, opponent=2" in text
    assert "Bag size: 84; opponent rack size: 7" in text
    assert "opponent rack + bag" in text and "?:1" in text
    assert "Model attempts including this request: 2" in text
    assert "native work: 300 candidate_rollout" in text
    assert "Tier light: up to 60" in text and "Tier standard: up to 300" in text
    assert "exact score=" in text and "leave=" in text
    assert "static_equity" not in text and "provenance" not in text and "cand_v1_" not in text
    assert "fallback" not in text and "rng_state" not in text
    assert prompt.as_payload()["parallel_tool_calls"] is False


def test_standard_board_premiums_and_blank_representation():
    from scrabble_harness.harness.prompts import STANDARD_PREMIUMS
    counts = Counter(cell for row in STANDARD_PREMIUMS for cell in row)
    assert counts == {"TW": 8, "DW": 17, "TL": 12, "DL": 24, "..": 164}
    board = Board(tiles=(BoardTile(row=7, column=7, displayed_letter="Q", is_blank=True),))
    rows = render_board(board).splitlines()
    assert rows[8].split()[8] == "q"  # row label followed by columns A-H
    assert render_board(Board()).splitlines()[8].split()[8] == "DW"
    assert rows[1].split()[1] == "TW"
    assert "A" in rows[0] and "O" in rows[0]


def test_new_blank_placement_keeps_assigned_letter_distinct_from_physical_rack(state):
    state = update(state, active_player=0)
    observation = build_observation(state, 0)
    move = PlacementMove(tiles=(BoardTile(row=6, column=7, displayed_letter="Z", is_blank=True),))
    candidate = VisibleCandidate(handle="c01", move=move, exact_score=0,
                                 leave=tuple(tile for tile in observation.own_rack if tile != "?"))
    view = DecisionView(decision_id="new-blank", observation=observation, candidates=(candidate,))
    prompt = render_prompt(view, DecisionLimits())
    assert "Own rack (7/7): ?ABCDEF" in prompt.instructions
    assert "c01 | place H7=z | exact score=0 | leave=ABCDEF" in prompt.instructions
    assert prompt.force_selection


def test_equivalent_multiset_and_board_order_does_not_change_prompt(context):
    view = context.model_view()
    obs = view.observation
    equivalent = update(obs, board=Board(tiles=tuple(reversed(obs.board.tiles))), own_rack=tuple(reversed(obs.own_rack)),
        unseen_tile_counts=update(obs.unseen_tile_counts, counts=tuple(reversed(obs.unseen_tile_counts.counts))))
    candidates = tuple(update(candidate, leave=tuple(reversed(candidate.leave))) for candidate in view.candidates)
    other = update(view, observation=equivalent, candidates=candidates)
    assert render_prompt(view, context.config.decision_limits).as_payload() == render_prompt(other, context.config.decision_limits).as_payload()


def test_private_opponent_draw_and_rng_changes_cannot_change_provider_input(state, context):
    bag, hidden = state.draw_state.ordered_tiles, state.racks[0]
    alternate = update(state, racks=((bag[-1],) + hidden[1:], state.racks[1]),
        draw_state=update(state.draw_state, ordered_tiles=bag[:-1] + (hidden[0],), rng_state="different-private-rng"))
    model = ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024)
    caps = ProviderCapabilities(provider="fixture", model="fixture-model", max_output_tokens=1024)
    requests = []
    for private in (state, alternate):
        view = update(context.model_view(), observation=build_observation(private, private.active_player))
        requests.append(build_model_request(view, attempt_id="paired-attempt", model=model, capabilities=caps,
                         remaining_limits=context.config.decision_limits))
    assert requests[0].model_dump_json() == requests[1].model_dump_json()


def test_renderer_rejects_private_state_checkpoint_or_whole_context(state, context):
    checkpoint = Checkpoint(checkpoint_id="private", engine_commit="a" * 40, state=state)
    for private in (state, checkpoint, context, state.model_dump(mode="python")):
        with pytest.raises(ValidationError):
            render_prompt(private, DecisionLimits())


@pytest.mark.parametrize("changes", [{"max_model_attempts": 1}, {"max_tool_steps": 0}, {"max_engine_work_units": 0}])
def test_final_or_exhausted_request_exposes_only_forced_selection(context, changes):
    limits = update(context.config.decision_limits, **changes)
    prompt = render_prompt(context.model_view(), limits)
    assert prompt.force_selection and tuple(tool.name for tool in prompt.tools) == ("select_move",)
    assert prompt.as_payload()["tool_choice"] == {"type": "function", "name": "select_move"}
    assert "select_move only" in prompt.instructions
    assert "Tier light:" not in prompt.instructions


def test_forced_selection_and_clipped_tiers(context):
    view = context.model_view()
    assert render_prompt(view, context.config.decision_limits, force_selection=True).force_selection
    limits = update(context.config.decision_limits, max_engine_work_units=3, max_elapsed_seconds=4.0)
    prompt = render_prompt(view, limits)
    assert "compare_moves: 2-3 distinct" in prompt.instructions
    assert "Tier light: up to 3 total candidate_rollout; up to 2 seconds." in prompt.instructions


@pytest.mark.parametrize("opponent_size,bag_size", [(7, 6), (6, 7)])
def test_unsupported_public_phase_hides_comparison_before_request(context, opponent_size, bag_size):
    view = context.model_view()
    original = view.observation
    # A reduced public inventory isolates the phase boundary without claiming
    # that this synthetic observation is a reachable native game position.
    distribution = TileInventory.from_tiles(original.board.physical_tiles + original.own_rack +
                                           tuple("A" * (opponent_size + bag_size)))
    data = original.model_dump(mode="python", exclude={"observation_id"}) | {
        "bag_size": bag_size, "opponent_rack_size": opponent_size, "tile_distribution": distribution,
        "unseen_tile_counts": distribution.without(original.board.physical_tiles + original.own_rack)}
    observation = PlayerObservation.from_data(ObservationData.model_validate(data))
    candidates = tuple(update(candidate, handle=f"c{index:02d}") for index, candidate in enumerate(
        (candidate for candidate in view.candidates if isinstance(candidate.move, PlacementMove)), 1))
    late = update(view, observation=observation, candidates=candidates)
    prompt = render_prompt(late, context.config.decision_limits)
    assert len(late.candidates) > 1 and prompt.force_selection
    assert tuple(tool.name for tool in prompt.tools) == ("select_move",)


def history_view(context):
    view = context.model_view()
    original = view.observation
    turns = original.public_history + tuple(PublicTurn(revision=revision, player=revision % 2,
        move=PublicExchangeMove(tile_count=2) if revision == 18 else PassMove(), score=0,
        scores_after=original.scores) for revision in range(2, 21))
    content = ObservationData.model_validate(original.model_dump(mode="python", exclude={"observation_id"}) |
                                           {"revision": 20, "public_history": turns})
    return update(view, observation=PlayerObservation.from_data(content))


def test_history_is_a_deterministic_suffix_and_exchange_reveals_only_count(context):
    view = history_view(context)
    full = view.observation.public_history
    prompt = render_prompt(view, context.config.decision_limits)
    assert prompt.retained_history_turns == 8 and prompt.omitted_history_turns == 12
    assert "revision 13 |" in prompt.instructions and "revision 20 |" in prompt.instructions
    assert "revision 12 |" not in prompt.instructions and "revision 1 |" not in prompt.instructions
    assert "revision 18 | player 0 | exchange 2 tiles" in prompt.instructions
    assert view.observation.public_history == full
    disabled = update(context.config, presentation=update(context.config.presentation, max_history_turns=0))
    none = render_prompt(view, context.config.decision_limits, config=disabled)
    assert none.retained_history_turns == 0 and none.omitted_history_turns == 20
    assert "revision 20 |" not in none.instructions


def test_owned_evidence_serializes_losslessly_and_can_be_cited(evidenced_context):
    ctx = evidenced_context
    evidence = ctx.evidence_views()[0]
    rendered = render_comparison_result(evidence)
    assert type(evidence).model_validate_json(rendered) == evidence
    assert '"completion_status":"work_limit"' in rendered and '"standard_error":0.5' in rendered
    assert '"seed":73' in rendered and '"count":12' in rendered and "cand_v1_" not in rendered
    prompt = render_prompt(ctx.model_view(), update(ctx.config.decision_limits, max_model_attempts=1), evidence=(evidence,))
    assert prompt.instructions == (SNAPSHOTS / "selection-with-evidence-v1.txt").read_text()
    assert rendered in prompt.instructions
    action = parse_tool_arguments("select_move", '{"candidate_handle":"c01","evidence_ids":["e01"],"brief_note":"After comparison"}',
                                  allowed_tools=tuple(tool.name for tool in prompt.tools))
    decision = ctx.select(ScopedAction(decision_id=ctx.decision_id, observation_id=ctx.observation.observation_id, action=action))
    assert decision.evidence_ids == ("e01",)


def test_stale_or_invisible_evidence_fails_before_rendering(evidenced_context):
    ctx = evidenced_context
    evidence = ctx.evidence_views()[0]
    with pytest.raises(ContextScopeError):
        render_prompt(ctx.model_view(), ctx.config.decision_limits, evidence=(update(evidence, decision_id="stale"),))
    invisible = update(evidence, requested_handles=("c01", "c99"), values=(), completion_status="timeout")
    with pytest.raises(ValueError, match="invisible"):
        render_prompt(ctx.model_view(), ctx.config.decision_limits, evidence=(invisible,))


def test_request_and_result_size_fail_without_truncating(context, evidenced_context):
    config = update(context.config, presentation=PresentationConfig(max_request_bytes=1024, max_tool_result_bytes=256))
    with pytest.raises(PromptSizeError):
        render_prompt(context.model_view(), config.decision_limits, config=config)
    evidence = evidenced_context.evidence_views()[0]
    with pytest.raises(PromptSizeError):
        render_comparison_result(evidence, presentation=config.presentation)
    assert evidenced_context.evidence_views()[0] == evidence


def test_unchecked_rendered_prompt_is_revalidated_before_export(context):
    prompt = render_prompt(context.model_view(), context.config.decision_limits)
    corrupted = prompt.model_copy(update={"force_selection": True})
    with pytest.raises(ValidationError):
        corrupted.as_payload()


def test_evidence_utf8_bound_and_instructions_character_bound(evidenced_context):
    ctx = evidenced_context
    evidence = ctx.evidence_views()[0]
    large = update(evidence, metric_definition=update(evidence.metric_definition, description="🙂" * 1000))
    with pytest.raises(PromptSizeError):
        render_comparison_result(large, presentation=PresentationConfig(max_tool_result_bytes=2000))
    huge = update(evidence, metric_definition=update(evidence.metric_definition, description="x" * 17000))
    config = update(ctx.config, presentation=PresentationConfig(max_request_bytes=100000, max_tool_result_bytes=50000))
    with pytest.raises(PromptSizeError, match="character ceiling"):
        render_prompt(ctx.model_view(), ctx.config.decision_limits, config=config, evidence=(huge,))


def test_unknown_versions_zero_attempts_and_inflated_allowance_fail(context):
    for name in ("prompt_version", "tool_schema_version"):
        with pytest.raises(PresentationVersionError):
            render_prompt(context.model_view(), context.config.decision_limits, config=update(context.config, **{name: "unknown-v9"}))
    for changes in ({"max_model_attempts": 0}, {"max_engine_work_units": 301}):
        with pytest.raises(ValueError):
            render_prompt(context.model_view(), update(context.config.decision_limits, **changes))


def test_mixed_display_or_evidence_configuration_is_rejected(evidenced_context):
    ctx = evidenced_context
    smaller = update(ctx.config, candidate_config=update(ctx.config.candidate_config, display_count=11))
    with pytest.raises(ValueError, match="display ceiling"):
        render_prompt(ctx.model_view(), ctx.config.decision_limits, config=smaller)
    evidence = ctx.evidence_views()[0]
    excessive = update(evidence, limits=update(evidence.limits, max_work_units=61))
    with pytest.raises(ValueError, match="comparison tier"):
        render_prompt(ctx.model_view(), ctx.config.decision_limits, evidence=(excessive,))


def test_request_builder_keeps_complete_prompt_caps_continuation_and_public_audit(context):
    model = ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024)
    capabilities = ProviderCapabilities(provider="fixture", model="fixture-model", max_output_tokens=1024)
    continuation = ProviderContinuation(provider="fixture", payload_json='{"opaque":"adapter-owned"}')
    request = build_model_request(context.model_view(), attempt_id="prompt-attempt", model=model, capabilities=capabilities,
        remaining_limits=update(context.config.decision_limits, max_model_attempts=1), continuation=continuation)
    assert request.tools == ("select_move",) and request.force_selection
    assert request.view == context.model_view() and request.continuation == continuation
    assert "adapter-owned" not in request.instructions
    assert ModelRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValidationError):
        build_model_request(context.model_view(), attempt_id="bad-capability", model=update(model, max_output_tokens=1025),
                            capabilities=capabilities, remaining_limits=context.config.decision_limits)
