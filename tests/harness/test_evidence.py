import pytest
from pydantic import ValidationError

from scrabble_harness.domain import (
    Board, BoardTile, CandidateProvenance, MovePreview, ObservationData, PassMove, PlacementMove,
    PlayerObservation, build_candidate,
)

from scrabble_harness.harness import (
    CompareAction, ContextScopeError, DecisionContext, EvidenceValidationError, ScopedAction, SelectAction,
)
from conftest import update


def selection(context, **changes):
    return ScopedAction(decision_id=context.decision_id, observation_id=context.observation.observation_id,
                        action=SelectAction(candidate_handle="c01", **changes))


def test_evidence_is_owned_immutable_scoped_and_idempotent(context, comparison, analysis_result, analysis_limits):
    updated, evidence = context.issue_evidence(comparison, analysis_result, limits=analysis_limits, seed=73)
    assert context.evidence == () and updated.evidence == (evidence,)
    assert evidence.evidence_id == "e01" and evidence.limits == analysis_limits
    assert evidence.tier == "light"
    assert evidence.requested == tuple(context.visible_bindings[:2])
    repeated, same = updated.issue_evidence(comparison, analysis_result, limits=analysis_limits, seed=73)
    assert repeated == updated and same == evidence
    assert updated.select(selection(updated, evidence_ids=("e01",))).candidate_id == updated.visible_bindings[0].candidate_id
    with pytest.raises(EvidenceValidationError):
        context.select(selection(context, evidence_ids=("e01",)))
    with pytest.raises(ContextScopeError):
        updated.select(update(selection(updated), decision_id="another-decision"))
    with pytest.raises(EvidenceValidationError):
        updated.select(selection(updated, evidence_ids=("e02",)))


def test_visible_evidence_retains_units_work_seed_uncertainty_and_completion(evidenced_context, analysis_result):
    visible = evidenced_context.evidence_views()[0]
    assert visible.requested_handles == ("c01", "c02")
    assert tuple(value.candidate_handle for value in visible.values) == ("c01", "c02")
    assert visible.values[0].standard_error == 0.5
    assert visible.work_used == analysis_result.work_used
    assert visible.metric_definition == analysis_result.metric_definition
    assert visible.seed == 73 and visible.completion_status == "work_limit"
    assert visible.tier == "light"
    assert "cand_v1_" not in visible.model_dump_json()
    assert "analysis_id" not in visible.model_dump_json()


@pytest.mark.parametrize("problem", ["foreign_observation", "unrequested", "missing_complete_value", "work_overrun",
                                     "wrong_work_unit", "seed_mismatch", "no_work", "wrong_metric"])
def test_bad_results_are_never_issued(context, comparison, analysis_result, analysis_limits, problem):
    result = analysis_result
    if problem == "foreign_observation":
        result = update(result, observation_id="obs_v1_" + "a" * 64)
    elif problem == "unrequested":
        other = context.resolve_handle("c03", decision_id=context.decision_id, observation_id=context.observation.observation_id)
        result = update(result, values=(update(result.values[0], candidate_id=other.candidate_id), result.values[1]))
    elif problem == "missing_complete_value":
        result = update(result, values=result.values[:1])
    elif problem == "work_overrun":
        result = update(result, work_used=update(result.work_used, count=13))
    elif problem == "wrong_work_unit":
        result = update(result, work_used=update(result.work_used, unit="iterations"))
    elif problem == "seed_mismatch":
        result = update(result, seed=74)
    elif problem == "no_work":
        result = update(result, work_used=update(result.work_used, count=0))
    else:
        result = update(result, completion_status="complete", values=tuple(update(value, standard_error=None) for value in result.values),
                        metric_definition=update(result.metric_definition, value_kind="exact"))
    with pytest.raises(EvidenceValidationError):
        context.issue_evidence(comparison, result, limits=analysis_limits, seed=73)
    assert context.evidence == ()


def test_partial_or_empty_timeout_is_honest_evidence(context, comparison, analysis_result, analysis_limits):
    partial = update(analysis_result, values=analysis_result.values[:1], completion_status="timeout")
    first, evidence = context.issue_evidence(comparison, partial, limits=analysis_limits, seed=73)
    assert len(first.evidence_views()[0].values) == 1
    empty = update(analysis_result, analysis_id="analysis-b", values=(), completion_status="cancelled",
                   work_used=update(analysis_result.work_used, count=0))
    second, evidence = first.issue_evidence(comparison, empty, limits=analysis_limits, seed=73)
    assert evidence.evidence_id == "e02" and second.evidence_views()[1].completion_status == "cancelled"


def test_conflicting_analysis_identity_and_tier_overrun_rejected(evidenced_context, comparison, analysis_result, analysis_limits):
    conflict = update(analysis_result, values=(update(analysis_result.values[0], value=999.0), analysis_result.values[1]))
    with pytest.raises(EvidenceValidationError, match="identity"):
        evidenced_context.issue_evidence(comparison, conflict, limits=analysis_limits, seed=73)
    for limits in (update(analysis_limits, max_work_units=61), update(analysis_limits, max_elapsed_seconds=9.0)):
        with pytest.raises(EvidenceValidationError, match="tier"):
            evidenced_context.issue_evidence(comparison, analysis_result, limits=limits, seed=73)


def test_context_rejects_injected_foreign_or_remapped_evidence(context, evidenced_context):
    evidence = evidenced_context.evidence[0]
    for corrupt in (update(evidence, decision_id="foreign-decision"), update(evidence, evidence_id="e02"),
                    update(evidence, requested=(update(evidence.requested[0], handle="c03"), evidence.requested[1])),
                    update(evidence, limits=update(evidence.limits, max_work_units=61))):
        with pytest.raises(ValidationError):
            update(context, evidence=(corrupt,))


def test_selection_returns_canonical_decision_and_static_fallback_reason(context):
    chosen = context.select(selection(context, brief_note="Save good tiles"))
    assert chosen.candidate_id == context.visible_bindings[0].candidate_id
    assert chosen.workflow == context.config.workflow and chosen.fallback_reason is None
    fallback = context.fallback_decision("provider_refused")
    assert fallback.candidate_id == context.fallback_candidate_id
    assert fallback.fallback_reason == "provider_refused"


@pytest.mark.parametrize("opponent_size,bag_size", [(7, 6), (6, 7)])
def test_late_position_disables_comparison_but_keeps_selection(context, opponent_size, bag_size):
    # Put the remaining physical tiles on the board. This is a public-domain
    # boundary fixture; no claim that these letter placements form legal words.
    original = context.observation
    invisible = tuple(original.unseen_tile_counts.as_counter().elements())
    board_letters = invisible[:len(invisible) - opponent_size - bag_size]
    existing = {(tile.row, tile.column) for tile in original.board.tiles}
    empty = [(row, column) for row in range(15) for column in range(15) if (row, column) not in existing]
    tiles = tuple(BoardTile(row=row, column=column, displayed_letter="A" if letter == "?" else letter,
                           is_blank=letter == "?") for (row, column), letter in zip(empty, board_letters))
    board = Board(tiles=original.board.tiles + tiles)
    data = original.model_dump(mode="python", exclude={"observation_id"}) | {
        "board": board, "bag_size": bag_size, "opponent_rack_size": opponent_size,
        "unseen_tile_counts": original.tile_distribution.without(board.physical_tiles + original.own_rack)}
    observation = PlayerObservation.from_data(ObservationData.model_validate(data))
    provenance = CandidateProvenance(source="synthetic-contract-test", configuration_id="test-v1")
    candidate = build_candidate(observation, PassMove(), exact_score=0, leave=observation.own_rack,
        static_equity=10.0, provenance=provenance)
    row, column = empty[len(board_letters)]
    move = PlacementMove(tiles=(BoardTile(row=row, column=column,
                                         displayed_letter=observation.own_rack[0], is_blank=False),))
    other = build_candidate(observation, move, exact_score=2, leave=observation.own_rack[1:],
                            static_equity=0.0, provenance=provenance)
    preview = MovePreview(observation_id=observation.observation_id, move=candidate.move,
        exact_score=0, leave=candidate.leave, board_after=board)
    late = DecisionContext.from_candidates(decision_id="late", observation=observation, config=context.config,
        candidate_order_seed=123, candidates=(candidate, other), fallback_preview=preview)
    assert not late.comparison_available
    with pytest.raises(EvidenceValidationError, match="unavailable"):
        late.resolve_comparison(ScopedAction(decision_id=late.decision_id, observation_id=observation.observation_id,
            action=CompareAction(candidate_handles=("c01", "c02"), tier="light")))
    assert late.select(selection(late)).candidate_id in {candidate.candidate_id, other.candidate_id}
