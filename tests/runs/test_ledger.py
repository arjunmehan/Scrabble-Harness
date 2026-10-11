"""Offline budget acceptance tests with synthetic rates and controllable clocks."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import AnalysisLimits, AnalysisResult, MetricDefinition, WorkUsed, canonical_bytes
from scrabble_harness.harness import DecisionLimits, DecisionOutcome, ExecutionManifest, RateCard, build_model_request
from scrabble_harness.harness.budget import (
    BudgetBoundError, BudgetDeadlineError, BudgetExceededError, BudgetStateError,
    InputTokenBound, estimate_usage, quote_cost, text_input_bound, verified_input_bound,
)
from scrabble_harness.providers.protocol import TokenUsage
from scrabble_harness.runs import BudgetLedger, RunStore, StoreConflictError, StoreNotFoundError
# Import fixture functions explicitly, avoiding a second ambiguous conftest module.
from test_store import context, manifest, model_request, db_path, store, changed, reply


class FakeClock:
    def __init__(self):
        self.wall = 1_800_000_000_000_000_000
        self.mono = 10_000_000_000

    def wall_ns(self):
        return self.wall

    def monotonic_ns(self):
        return self.mono

    def advance(self, seconds, *, wall=True, mono=True):
        delta = int(Decimal(str(seconds)) * Decimal(1_000_000_000))
        if wall:
            self.wall += delta
        if mono:
            self.mono += delta


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def ledger(store, clock):
    return BudgetLedger(store, clock=clock)


def bound(request, tokens=1000):
    return verified_input_bound(request, input_tokens=tokens, evidence="Synthetic complete-input counter v1")


def finish_bound(request, tokens=10000):
    return InputTokenBound(provider=request.model.provider, model=request.model.model, purpose="final_selection",
        method="verified_complete_input_v1", evidence="Synthetic follow-up ceiling includes tools/results/continuation",
        input_tokens=tokens, max_output_tokens=request.model.max_output_tokens)


def selection_request(request, *, attempt_id=None):
    return changed(request, attempt_id=attempt_id or request.attempt_id, tools=("select_move",), force_selection=True)


def admitted_model(ledger, request, reservation_id="model-a", *, compare=False):
    if not compare:
        request = selection_request(request)
    reservation = ledger.reserve_model(reservation_id, request, bound(request),
        finish_bound=finish_bound(request) if compare else None)
    return reservation, request


def native_result(context, work=12, **changes):
    return AnalysisResult(analysis_id="analysis-a", observation_id=context.observation.observation_id,
        values=(), metric_definition=MetricDefinition(name="synthetic", unit="points", description="Storage test", value_kind="estimate"),
        work_used=WorkUsed(unit="candidate_rollout", definition="Total candidate rollouts", count=work),
        elapsed_time_seconds=0.1, seed=73, completion_status="timeout", **changes)


def new_manifest(original, *, decision_limits=None, run_limits=None):
    data = original.model_dump(mode="python", exclude={"manifest_id"})
    if decision_limits is not None:
        data["harness_config"] = changed(original.harness_config, decision_limits=decision_limits).model_dump(mode="python")
    if run_limits is not None:
        data["run_config"] = changed(original.run_config, limits=run_limits).model_dump(mode="python")
    return ExecutionManifest.create(**data)


def prepare_other(store, original, context, *, run_id, decision_id, campaign_id="campaign-a"):
    store.create_run(run_id, campaign_id, original)
    other = changed(context, decision_id=decision_id, config=original.harness_config)
    store.prepare_decision(run_id, other)
    return other


def build_request(other, manifest, *, attempt_id):
    from scrabble_harness.providers.protocol import ProviderCapabilities
    return build_model_request(other.model_view(), attempt_id=attempt_id, model=manifest.run_config.model,
        capabilities=ProviderCapabilities(provider="fixture", model="fixture-model", max_output_tokens=1024),
        remaining_limits=other.config.decision_limits, config=other.config, force_selection=True)


def test_quotes_round_up_and_include_reasoning_output(model_request, manifest):
    rates = changed(manifest.rate_card, input_microusd_per_million_tokens=1, output_microusd_per_million_tokens=1)
    quote = quote_cost(bound(model_request, 1), rates, safety_margin_basis_points=0, minimum_margin_microusd=0)
    assert quote.api_cost_microusd == 1
    assert quote.rate_card_sha256 == hashlib.sha256(canonical_bytes(rates)).hexdigest()
    usage = TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1000, reasoning_output_tokens=900)
    assert estimate_usage(usage, rates).api_cost_microusd == 1  # Reasoning is already in output.


def test_cache_read_write_pricing_and_unknown_categories(model_request, manifest):
    rates = changed(manifest.rate_card, input_microusd_per_million_tokens=1000000,
        cached_input_microusd_per_million_tokens=100000, cache_write_microusd_per_million_tokens=1250000,
        output_microusd_per_million_tokens=2000000)
    quote = quote_cost(bound(model_request, 1000), rates, safety_margin_basis_points=0, minimum_margin_microusd=0)
    assert quote.api_cost_microusd == 1250 + 2048
    usage = TokenUsage(input_tokens=1000, cached_input_tokens=400, cache_write_input_tokens=200,
                       output_tokens=100, reasoning_output_tokens=90)
    cost = estimate_usage(usage, rates)
    assert cost.api_cost_microusd == 400 + 40 + 250 + 200
    assert cost.method == "reported_categories_v1"
    unknown = estimate_usage(TokenUsage(input_tokens=1000, output_tokens=100), rates)
    assert unknown.api_cost_microusd == 1250 + 200
    assert unknown.method == "category_upper_bound_v1"


@pytest.mark.parametrize("usage", [None, TokenUsage(total_tokens=100), TokenUsage(input_tokens=10), TokenUsage(output_tokens=10)])
def test_missing_usage_does_not_become_zero(manifest, usage):
    assert estimate_usage(usage, manifest.rate_card) is None


@pytest.mark.parametrize("counts", [dict(input_tokens=10, cached_input_tokens=6, cache_write_input_tokens=5),
                                     dict(input_tokens=10, cache_write_input_tokens=11), dict(cache_write_input_tokens=True)])
def test_invalid_cache_partitions(counts):
    with pytest.raises(ValidationError):
        TokenUsage(**counts)


def test_zero_rates_allow_zero_cost_quote(model_request, manifest):
    rates = changed(manifest.rate_card, input_microusd_per_million_tokens=0, output_microusd_per_million_tokens=0)
    assert quote_cost(bound(model_request), rates).api_cost_microusd == 0


def test_pricing_band_provider_model_and_bound_identity(model_request, manifest):
    with pytest.raises(BudgetBoundError):
        quote_cost(bound(model_request, 100), changed(manifest.rate_card, max_input_tokens_at_rate=99))
    with pytest.raises(BudgetBoundError):
        quote_cost(bound(model_request), changed(manifest.rate_card, model="other"))
    with pytest.raises(ValidationError):
        changed(bound(model_request), method="characters-divided-by-four")
    with pytest.raises(ValidationError):
        changed(finish_bound(model_request), request_sha256="a" * 64)


def test_text_bound_covers_schemas_and_rejects_opaque_continuation(model_request):
    from scrabble_harness.providers.protocol import ProviderContinuation
    quoted = text_input_bound(model_request, framing_tokens=100, tokenizer_evidence="Synthetic byte-tokenizer/frame contract")
    assert quoted.input_tokens > len(model_request.model_dump_json().encode()) + 100
    opaque = changed(model_request, continuation=ProviderContinuation(provider="fixture", payload_json='{"opaque":"state"}'))
    with pytest.raises(BudgetBoundError):
        text_input_bound(opaque, framing_tokens=100, tokenizer_evidence="Synthetic")


def test_model_dispatch_settlement_and_idempotence(ledger, store, model_request):
    reserved, request = admitted_model(ledger, model_request)
    before = ledger.totals(decision_id="decision-a")
    assert before.reserved.model_calls == 1 and before.charged.api_cost_microusd == reserved.allocation.api_cost_microusd
    assert ledger.reserve_model("model-a", request, bound(request)) == reserved
    permit = ledger.dispatch_model("model-a", requested_timeout_seconds=100.0)
    assert permit.timeout_seconds == 30.0
    with pytest.raises(BudgetStateError):
        ledger.dispatch_model("model-a", requested_timeout_seconds=10.0)
    result = ledger.settle_model("model-a", reply(request))
    assert result.state == "settled" and result.settlement.model_calls == 1
    assert result.settlement.api_cost_microusd == 2
    assert ledger.settle_model("model-a", reply(request)) == result
    assert ledger.totals(decision_id="decision-a").settled.api_cost_microusd == 2
    assert store.get_attempt(request.attempt_id).reply.tool_calls[0].arguments_json.startswith("{bad")


@pytest.mark.parametrize("reply_status", ["refused", "incomplete", "failed"])
def test_unusable_replies_are_still_billable(ledger, store, model_request, reply_status):
    _, request = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    malformed = changed(reply(request), status=reply_status)
    result = ledger.settle_model("model-a", malformed)
    assert result.settlement.api_cost_microusd == 2 and result.settlement.model_calls == 1
    assert store.get_attempt(request.attempt_id).reply == malformed


def test_missing_usage_keeps_full_reservation(ledger, store, model_request):
    reserved, request = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    unknown = changed(reply(request), usage=None)
    ledger.settle_model("model-a", unknown)
    total = ledger.totals(campaign_id="campaign-a")
    assert total.unresolved.api_cost_microusd == reserved.allocation.api_cost_microusd
    assert total.unresolved.model_calls == 1 and total.settled.api_cost_microusd == 0
    assert store.get_attempt(request.attempt_id).state == "replied"
    with pytest.raises(BudgetStateError):
        ledger.release_undispatched("model-a")


def test_timed_out_model_recovery_can_settle_late_reply(ledger, model_request):
    reserved, request = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    ledger.mark_unresolved("model-a")
    assert ledger.totals(run_id="run-a").charged.api_cost_microusd == reserved.allocation.api_cost_microusd
    assert ledger.settle_model("model-a", reply(request)).state == "settled"


def test_dispatch_and_reservation_changes_are_one_transaction(ledger, store, model_request, monkeypatch):
    admitted_model(ledger, model_request)
    original = store.transition_reservation
    def fail(*args, **kwargs):
        raise RuntimeError("injected allocation transition failure")
    with monkeypatch.context() as patch:
        patch.setattr(store, "transition_reservation", fail)
        with pytest.raises(RuntimeError):
            ledger.dispatch_model("model-a", requested_timeout_seconds=10.0)
    assert store.get_attempt("attempt-a").state == "prepared"
    assert store.get_reservation("model-a").state == "reserved"
    assert original is not None


def test_finish_hold_and_final_selection_transfer(ledger, store, model_request):
    initial, request = admitted_model(ledger, model_request, compare=True)
    assert ledger.totals(decision_id="decision-a").held.model_calls == 1
    assert ledger.remaining_limits("decision-a").max_model_attempts == 1  # Initial + held slot already reserved.
    ledger.dispatch_model("model-a", requested_timeout_seconds=20.0)
    ledger.settle_model("model-a", reply(request))
    final = selection_request(model_request, attempt_id="attempt-final")
    # Builder should use remaining_limits; a stale request snapshot cannot bypass admission.
    reserved = ledger.reserve_model("model-final", final, bound(final), use_finish_hold=True)
    assert store.finish_hold("decision-a").state == "consumed"
    total = ledger.totals(decision_id="decision-a")
    assert total.charged.model_calls == 2 and total.held.model_calls == 0
    assert total.charged.api_cost_microusd == 2 + reserved.allocation.api_cost_microusd
    ledger.dispatch_model("model-final", requested_timeout_seconds=5.0)


def test_comparison_without_finish_allowance_fails_without_records(ledger, store, model_request):
    with pytest.raises(BudgetStateError):
        ledger.reserve_model("model-a", model_request, bound(model_request))
    with pytest.raises(StoreNotFoundError):
        store.get_attempt("attempt-a")
    assert len(store.events("run-a")) == 2
    with pytest.raises(BudgetStateError):
        ledger.reserve_native("native-a", "decision-a", AnalysisLimits(max_work_units=12,
                              work_unit="candidate_rollout", max_elapsed_seconds=4.0))


def test_finish_slot_cannot_be_spent_by_another_attempt(ledger, model_request):
    admitted_model(ledger, model_request, compare=True)
    extra = selection_request(model_request, attempt_id="extra-attempt")
    with pytest.raises(BudgetExceededError) as error:
        ledger.reserve_model("extra-reservation", extra, bound(extra))
    assert error.value.resource == "model_calls"


def test_finish_input_ceiling_and_selection_only_rule(ledger, model_request):
    admitted_model(ledger, model_request, compare=True)
    final = selection_request(model_request, attempt_id="attempt-final")
    with pytest.raises(BudgetBoundError):
        ledger.reserve_model("final", final, bound(final, 10001), use_finish_hold=True)
    with pytest.raises(BudgetStateError):
        ledger.reserve_model("final", changed(model_request, attempt_id="attempt-final"),
                             bound(changed(model_request, attempt_id="attempt-final")), use_finish_hold=True)
    assert ledger.totals(decision_id="decision-a").held.model_calls == 1


def test_native_settlement_keeps_step_and_releases_unused_work(ledger, context, model_request):
    ledger.reserve_finish("decision-a", finish_bound(model_request))
    limits = AnalysisLimits(max_work_units=60, work_unit="candidate_rollout", max_elapsed_seconds=8.0)
    ledger.reserve_native("native-a", "decision-a", limits)
    actual = ledger.dispatch_native("native-a")
    assert actual.max_work_units == 60 and actual.max_elapsed_seconds == 8.0
    settled = ledger.settle_native("native-a", native_result(context, 12))
    assert settled.settlement.engine_work_units == 12 and settled.settlement.tool_steps == 1
    totals = ledger.totals(decision_id="decision-a")
    assert totals.settled.engine_work_units == 12 and totals.charged.tool_steps == 1
    with pytest.raises(BudgetExceededError) as error:
        ledger.reserve_native("native-b", "decision-a", limits)
    assert error.value.resource == "tool_steps"


def test_unknown_native_work_retains_full_allowance(ledger, model_request):
    ledger.reserve_finish("decision-a", finish_bound(model_request))
    ledger.reserve_native("native-a", "decision-a", AnalysisLimits(max_work_units=300,
                          work_unit="candidate_rollout", max_elapsed_seconds=8.0))
    ledger.dispatch_native("native-a")
    ledger.settle_native("native-a", None)
    assert ledger.totals(decision_id="decision-a").unresolved.engine_work_units == 300
    with pytest.raises(BudgetStateError):
        ledger.release_undispatched("native-a")


@pytest.mark.parametrize("resource", ["api_cost_microusd", "model_calls", "engine_work_units"])
def test_shared_campaign_caps_across_runs(store, manifest, context, clock, resource):
    # Each run individually permits more than the campaign; new runs cannot reset it.
    caps = changed(manifest.run_config.limits, **{"max_" + resource: {"api_cost_microusd":400,"model_calls":1,"engine_work_units":10}[resource]})
    store.create_campaign("small-campaign", caps)
    first = prepare_other(store, manifest, context, run_id="other-run-1", decision_id="other-decision-1", campaign_id="small-campaign")
    second = prepare_other(store, manifest, context, run_id="other-run-2", decision_id="other-decision-2", campaign_id="small-campaign")
    ledger = BudgetLedger(store, clock=clock)
    a, b = build_request(first, manifest, attempt_id="other-attempt-1"), build_request(second, manifest, attempt_id="other-attempt-2")
    if resource == "engine_work_units":
        ledger.reserve_finish(first.decision_id, finish_bound(a))
        ledger.reserve_finish(second.decision_id, finish_bound(b))
        limits = AnalysisLimits(max_work_units=6, work_unit="candidate_rollout", max_elapsed_seconds=4.0)
        ledger.reserve_native("shared-a", first.decision_id, limits)
        with pytest.raises(BudgetExceededError) as error:
            ledger.reserve_native("shared-b", second.decision_id, limits)
    else:
        ledger.reserve_model("shared-a", a, bound(a))
        with pytest.raises(BudgetExceededError) as error:
            ledger.reserve_model("shared-b", b, bound(b))
    assert error.value.scope == "campaign" and error.value.resource == resource


@pytest.mark.parametrize("scope", ["run", "decision"])
def test_run_and_decision_cost_caps(store, manifest, context, clock, scope):
    limits = changed(manifest.harness_config.decision_limits, max_api_cost_microusd=100)
    run_limits = changed(manifest.run_config.limits, max_api_cost_microusd=100) if scope == "run" else manifest.run_config.limits
    new = new_manifest(manifest, decision_limits=limits, run_limits=run_limits)
    other = prepare_other(store, new, context, run_id="limited-run", decision_id="limited-decision")
    request = build_request(other, new, attempt_id="limited-attempt")
    ledger = BudgetLedger(store, clock=clock)
    with pytest.raises(BudgetExceededError) as error:
        ledger.reserve_model("limited", request, bound(request))
    assert error.value.scope == scope
    with pytest.raises(StoreNotFoundError):
        store.get_attempt("limited-attempt")


def test_failed_initial_admission_rolls_back_finish_hold(store, manifest, context, clock):
    limits = changed(manifest.harness_config.decision_limits, max_api_cost_microusd=1300)
    new = new_manifest(manifest, decision_limits=limits)
    other = prepare_other(store, new, context, run_id="tight-run", decision_id="tight-decision")
    request = build_request(other, new, attempt_id="tight-attempt")
    request = changed(request, tools=("select_move","compare_moves"), force_selection=False)
    ledger = BudgetLedger(store, clock=clock)
    with pytest.raises(BudgetExceededError):
        ledger.reserve_model("tight", request, bound(request), finish_bound=finish_bound(request))
    assert store.finish_hold(other.decision_id) is None
    with pytest.raises(StoreNotFoundError):
        store.get_reservation("tight")


def test_retry_is_a_new_call_slot_even_after_zero_token_reply(ledger, model_request):
    _, first = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    ledger.settle_model("model-a", changed(reply(first), usage=TokenUsage(input_tokens=0, output_tokens=0)))
    second = selection_request(model_request, attempt_id="retry-attempt")
    ledger.reserve_model("retry", second, bound(second))
    third = selection_request(model_request, attempt_id="third-attempt")
    with pytest.raises(BudgetExceededError):
        ledger.reserve_model("third", third, bound(third))
    assert ledger.totals(decision_id="decision-a").charged.model_calls == 2


def test_reported_overrun_is_persisted_and_stops_future_admission(ledger, store, model_request):
    _, request = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    usage = TokenUsage(input_tokens=1001, output_tokens=5)
    result = ledger.settle_model("model-a", changed(reply(request), usage=usage))
    assert result.bound_exceeded and result.state == "settled"
    assert store.get_attempt(request.attempt_id).reply.usage == usage
    another = selection_request(model_request, attempt_id="another")
    with pytest.raises(BudgetExceededError, match="reported-usage-bound"):
        ledger.reserve_model("another", another, bound(another))


def test_only_live_session_can_release_or_dispatch_reserved_work(ledger, store, model_request, clock):
    admitted_model(ledger, model_request)
    reopened = BudgetLedger(store, clock=clock)
    with pytest.raises(BudgetStateError):
        reopened.release_undispatched("model-a")
    with pytest.raises(BudgetStateError):
        reopened.dispatch_model("model-a", requested_timeout_seconds=5.0)
    ledger.release_undispatched("model-a")
    assert ledger.totals(decision_id="decision-a").charged.model_calls == 0


def test_restart_preserves_budget_and_quarantines_stale_allocations(ledger, store, db_path, model_request, clock):
    reserved, request = admitted_model(ledger, model_request)
    original = ledger.totals(campaign_id="campaign-a").charged
    store.close()
    clock.advance(1)
    with RunStore(db_path) as reopened:
        recovering = BudgetLedger(reopened, clock=clock)
        assert recovering.totals(campaign_id="campaign-a").charged == original
        changes = recovering.recover_unfinished("run-a")
        assert len(changes) == 1 and changes[0].state == "unresolved"
        assert reopened.get_attempt("attempt-a").state == "unresolved"
        assert recovering.totals(campaign_id="campaign-a").unresolved.api_cost_microusd == reserved.allocation.api_cost_microusd
        assert recovering.recover_unfinished("run-a") == ()
        with pytest.raises(BudgetStateError):
            recovering.dispatch_model("model-a", requested_timeout_seconds=5.0)


def test_explicit_recovery_does_not_overwrite_saved_reply(ledger, store, model_request):
    _, request = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    store.record_reply(reply(request))  # Crash between reply saving and settlement.
    ledger.recover_unfinished("run-a")
    assert store.get_attempt("attempt-a").reply == reply(request)
    assert ledger.settle_model("model-a", reply(request)).state == "settled"


def test_absolute_deadline_clips_tools_and_protects_selection_time(ledger, clock, model_request):
    deadline = ledger.deadline("decision-a")
    clock.advance(25)
    assert deadline.timeout(20.0) == 5.0
    assert deadline.timeout(20.0, nonterminal=True) == 3.0
    ledger.reserve_finish("decision-a", finish_bound(model_request))
    ledger.reserve_native("native-a", "decision-a", AnalysisLimits(max_work_units=12,
                          work_unit="candidate_rollout", max_elapsed_seconds=8.0))
    assert ledger.dispatch_native("native-a").max_elapsed_seconds == 3.0
    clock.advance(3)
    with pytest.raises(BudgetDeadlineError):
        ledger.reserve_native("native-b", "decision-a", AnalysisLimits(max_work_units=12,
                              work_unit="candidate_rollout", max_elapsed_seconds=8.0))
    assert ledger.deadline("decision-a").remaining_seconds() == 2.0


def test_restart_counts_downtime_and_does_not_restart_decision_clock(ledger, store, db_path, clock):
    ledger.deadline("decision-a")
    clock.advance(10)
    store.close()
    clock.mono = 0  # Different monotonic origin in another process is legitimate.
    with RunStore(db_path) as reopened:
        resumed = BudgetLedger(reopened, clock=clock)
        assert resumed.deadline("decision-a").remaining_seconds() == 20.0
        clock.advance(2, wall=False)
        assert resumed.deadline("decision-a").remaining_seconds() == 18.0
        clock.advance(20)
        with pytest.raises(BudgetDeadlineError):
            resumed.deadline("decision-a")


@pytest.mark.parametrize("which", ["wall", "monotonic"])
def test_clock_rollback_fails_closed_durably(ledger, store, clock, which):
    ledger.deadline("decision-a")
    clock.advance(1)
    ledger.deadline("decision-a")
    if which == "wall":
        clock.wall -= 2_000_000_000
    else:
        clock.mono -= 2_000_000_000
    with pytest.raises(BudgetDeadlineError):
        ledger.deadline("decision-a")
    clock.advance(10)
    with pytest.raises(BudgetDeadlineError):
        BudgetLedger(store, clock=clock).deadline("decision-a")


def test_begin_decision_counts_preparation_time(ledger, store, manifest, context, clock):
    store.create_run("preparing-run", "campaign-a", manifest)
    begun = ledger.begin_decision("preparing-run", "preparing-decision", context.observation)
    assert begun.context is None
    clock.advance(4)
    prepared = changed(context, decision_id="preparing-decision")
    store.prepare_decision("preparing-run", prepared)
    assert ledger.deadline("preparing-decision").remaining_seconds() == 26.0


def test_campaign_elapsed_cap_survives_starting_another_run(store, manifest, context, clock):
    campaign_limits = changed(manifest.run_config.limits, max_elapsed_seconds=5.0)
    store.create_campaign("short-campaign", campaign_limits)
    a = prepare_other(store, manifest, context, run_id="elapsed-a", decision_id="elapsed-a", campaign_id="short-campaign")
    b = prepare_other(store, manifest, context, run_id="elapsed-b", decision_id="elapsed-b", campaign_id="short-campaign")
    ledger = BudgetLedger(store, clock=clock)
    assert ledger.deadline(a.decision_id).remaining_seconds() == 5.0
    clock.advance(5)
    with pytest.raises(BudgetDeadlineError):
        ledger.deadline(b.decision_id)


def test_terminal_outcome_uses_durable_usage_and_releases_finish(ledger, store, context, model_request):
    _, request = admitted_model(ledger, model_request, compare=True)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    ledger.settle_model("model-a", reply(request))
    outcome = DecisionOutcome(decision_id=context.decision_id, observation_id=context.observation.observation_id,
        status="fallback", decision=context.fallback_decision("done"), trusted_pool=context.full_pool,
        visible_bindings=context.visible_bindings, terminal_reason="done")
    recorded = ledger.complete_decision(outcome)
    assert recorded.outcome.usage.model_attempts == 1
    assert recorded.outcome.usage.settled_api_cost_microusd == 2
    assert store.finish_hold("decision-a").state == "released"
    assert ledger.totals(decision_id="decision-a").charged.model_calls == 1
    assert ledger.complete_decision(outcome) == recorded


def test_concurrent_reservations_cannot_overspend_campaign(store, manifest, context, db_path):
    store.create_campaign("contended", changed(manifest.run_config.limits, max_model_calls=2))
    requests = []
    for index in range(6):
        other = prepare_other(store, manifest, context, run_id=f"race-run-{index}",
                              decision_id=f"race-decision-{index}", campaign_id="contended")
        requests.append(build_request(other, manifest, attempt_id=f"race-attempt-{index}"))
    def contender(item):
        index, request = item
        try:
            with RunStore(db_path) as local:
                local_ledger = BudgetLedger(local)
                local_ledger.reserve_model(f"race-reservation-{index}", request, bound(request))
            return "admitted"
        except BudgetExceededError:
            return "denied"
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(contender, enumerate(requests)))
    assert results.count("admitted") == 2 and results.count("denied") == 4
    assert BudgetLedger(store).totals(campaign_id="contended").charged.model_calls == 2
    store.check_integrity()


def test_cross_process_reservations_cannot_overspend_shared_cap(store, manifest, context, db_path):
    store.create_campaign("process-campaign", changed(manifest.run_config.limits, max_model_calls=1))
    for index in range(3):
        other = prepare_other(store, manifest, context, run_id=f"process-run-{index}",
                              decision_id=f"process-decision-{index}", campaign_id="process-campaign")
        request = build_request(other, manifest, attempt_id=f"process-attempt-{index}")
        store.prepare_attempt(other.run_id if hasattr(other,"run_id") else f"process-run-{index}", other.decision_id, request)
    code = '''
import sys
from scrabble_harness.runs import RunStore, BudgetLedger
from scrabble_harness.harness.budget import BudgetExceededError, verified_input_bound
with RunStore(sys.argv[1]) as store:
    request = store.get_attempt(f"process-attempt-{sys.argv[2]}").request
    bound = verified_input_bound(request,input_tokens=1000,evidence="Synthetic process counter")
    try:
        BudgetLedger(store).reserve_model(f"process-reservation-{sys.argv[2]}",request,bound)
        print("admitted")
    except BudgetExceededError:
        print("denied")
'''
    processes = [subprocess.Popen([sys.executable, "-c", code, str(db_path), str(index)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(3)]
    outcomes = []
    for process in processes:
        output, errors = process.communicate(timeout=15)
        assert process.returncode == 0, errors.decode()
        outcomes.append(output.decode().strip())
    assert outcomes.count("admitted") == 1 and outcomes.count("denied") == 2
    assert BudgetLedger(store).totals(campaign_id="process-campaign").charged.model_calls == 1


@pytest.mark.parametrize("category", ["cached_input_tokens", "cache_write_input_tokens"])
def test_reported_category_without_a_rate_stays_unresolved(manifest, category):
    assert estimate_usage(TokenUsage(input_tokens=10, output_tokens=5, **{category: 5}), manifest.rate_card) is None


def test_partial_usage_that_breaches_bound_stops_admission(ledger, store, model_request):
    _, request = admitted_model(ledger, model_request)
    ledger.dispatch_model("model-a", requested_timeout_seconds=5.0)
    raw = changed(reply(request), usage=TokenUsage(input_tokens=1001))
    saved = ledger.settle_model("model-a", raw)
    assert saved.state == "unresolved" and saved.bound_exceeded
    assert ledger.mark_unresolved("model-a").bound_exceeded  # Cannot clear known overruns.
    assert store.get_attempt(request.attempt_id).reply == raw
    with pytest.raises(BudgetExceededError):
        ledger.reserve_model("blocked", selection_request(model_request, attempt_id="blocked"),
            bound(selection_request(model_request, attempt_id="blocked")))


@pytest.mark.parametrize("native", [False, True])
def test_expiry_during_dispatch_commit_returns_no_permit(ledger, store, model_request, clock, monkeypatch, native):
    if native:
        ledger.reserve_finish("decision-a", finish_bound(model_request))
        ledger.reserve_native("work", "decision-a", AnalysisLimits(max_work_units=12,
            work_unit="candidate_rollout", max_elapsed_seconds=8.0))
    else:
        admitted_model(ledger, model_request, "work")
    event = store._event
    def delayed_event(kind, record):
        saved = event(kind, record)
        if kind == "reservation_changed" and record.state == "dispatched":
            clock.advance(31)
        return saved
    monkeypatch.setattr(store, "_event", delayed_event)
    with pytest.raises(BudgetDeadlineError):
        if native:
            ledger.dispatch_native("work")
        else:
            ledger.dispatch_model("work", requested_timeout_seconds=10.0)
    assert store.get_reservation("work").state == "unresolved"
    totals = ledger.totals(decision_id="decision-a")
    if native:
        assert totals.unresolved.engine_work_units == 12
    else:
        assert totals.unresolved == totals.charged


def test_nonterminal_permit_expiry_preserves_finish_time(ledger, model_request, clock):
    admitted_model(ledger, model_request, compare=True)
    permit = ledger.dispatch_model("model-a", requested_timeout_seconds=60.0)
    assert permit.timeout_seconds == 28.0
    assert permit.expires_monotonic_ns == clock.mono + 28_000_000_000


def test_native_result_is_durable_and_cannot_be_charged_twice(ledger, store, model_request, context, db_path):
    ledger.reserve_finish("decision-a", finish_bound(model_request))
    limits = AnalysisLimits(max_work_units=12, work_unit="candidate_rollout", max_elapsed_seconds=8.0)
    ledger.reserve_native("native-a", "decision-a", limits)
    ledger.dispatch_native("native-a")
    result = native_result(context, 10)
    saved = ledger.settle_native("native-a", result)
    assert ledger.settle_native("native-a", result) == saved
    assert saved.native_result == result
    original = store.get_run("run-a").manifest
    other = prepare_other(store, original, context, run_id="other-run", decision_id="other-decision")
    other_request = build_request(other, original, attempt_id="other-attempt")
    ledger.reserve_finish(other.decision_id, finish_bound(other_request))
    ledger.reserve_native("native-b", other.decision_id, limits)
    ledger.dispatch_native("native-b")
    with pytest.raises(StoreConflictError):
        ledger.settle_native("native-b", result)
    assert store.get_reservation("native-b").state == "dispatched"
    with RunStore(db_path) as reopened:
        assert reopened.get_reservation("native-a").native_result == result
        reopened.check_integrity()


def test_native_overrun_is_preserved_and_stops_admission(ledger, store, context, model_request):
    ledger.reserve_finish("decision-a", finish_bound(model_request))
    ledger.reserve_native("native-a", "decision-a", AnalysisLimits(max_work_units=12,
        work_unit="candidate_rollout", max_elapsed_seconds=8.0))
    ledger.dispatch_native("native-a")
    saved = ledger.settle_native("native-a", native_result(context, 13))
    assert saved.bound_exceeded and saved.settlement.engine_work_units == 13
    assert store.get_reservation("native-a").native_result.work_used.count == 13
    with pytest.raises(BudgetExceededError):
        ledger.reserve_model("blocked", selection_request(model_request), bound(selection_request(model_request)), use_finish_hold=True)


def test_wrong_request_bound_leaves_no_attempt_or_reservation(ledger, store, model_request):
    request = selection_request(model_request)
    with pytest.raises(BudgetBoundError):
        ledger.reserve_model("bad-bound", request, bound(model_request))
    assert store.reservations("run-a") == ()
    with pytest.raises(StoreNotFoundError):
        store.get_attempt(request.attempt_id)


def test_hard_process_exit_keeps_full_charge_and_never_replays(store, db_path, model_request):
    request = selection_request(model_request)
    store.prepare_attempt("run-a", "decision-a", request)
    code = '''
import os, sys
from scrabble_harness.runs import RunStore, BudgetLedger
from scrabble_harness.harness.budget import verified_input_bound
store = RunStore(sys.argv[1])
ledger = BudgetLedger(store)
request = store.get_attempt("attempt-a").request
ledger.reserve_model("crashed", request, verified_input_bound(request, input_tokens=1000, evidence="Synthetic crash counter"))
ledger.dispatch_model("crashed", requested_timeout_seconds=5.0)
os._exit(73)
'''
    result = subprocess.run([sys.executable, "-c", code, str(db_path)], capture_output=True, timeout=15)
    assert result.returncode == 73, result.stderr.decode()
    with RunStore(db_path) as reopened:
        resumed = BudgetLedger(reopened)
        allocation = reopened.get_reservation("crashed").allocation
        assert resumed.totals(campaign_id="campaign-a").unresolved.api_cost_microusd == allocation.api_cost_microusd
        assert resumed.recover_unfinished("run-a")[0].state == "unresolved"
        with pytest.raises(BudgetStateError):
            resumed.dispatch_model("crashed", requested_timeout_seconds=5.0)
        assert resumed.totals(campaign_id="campaign-a").charged.model_calls == 1


def test_v1_migration_preserves_canonical_records_and_audit(db_path, manifest, context, model_request, monkeypatch):
    import scrabble_harness.runs.store as storage
    from scrabble_harness.runs import ReservationRecord, ResourceAmounts
    original_columns = storage._columns
    def v1_columns(record):
        return {key: value for key, value in original_columns(record).items() if key != "analysis_id"}
    # Exercise the original, unchanged 001 schema and the old serialized records.
    with monkeypatch.context() as patch:
        patch.setattr(storage, "MIGRATIONS", storage.MIGRATIONS[:1])
        patch.setattr(storage, "_columns", v1_columns)
        with RunStore(db_path) as old:
            old.create_campaign("campaign-a", manifest.run_config.limits)
            old.create_run("run-a", "campaign-a", manifest)
            old.prepare_decision("run-a", context)
            attempt = old.prepare_attempt("run-a", "decision-a", model_request)
            old.create_reservation(ReservationRecord(reservation_id="legacy", run_id="run-a", decision_id="decision-a",
                attempt_id=model_request.attempt_id, kind="model", allocation=ResourceAmounts(api_cost_microusd=700, model_calls=1)))
            old.mark_attempt_dispatched(model_request.attempt_id)
            old.transition_reservation("legacy", "dispatched")
            old.record_reply(reply(model_request))
            old.transition_reservation("legacy", "settled", settlement=ResourceAmounts(api_cost_microusd=2, model_calls=1))
    with sqlite3.connect(db_path) as sql:
        before = {table: sql.execute(f"SELECT body, body_sha256 FROM {table} ORDER BY body_sha256").fetchall()
                  for table in ("campaigns", "runs", "decisions", "attempts", "reservations", "events")}
        assert sql.execute("PRAGMA user_version").fetchone()[0] == 1
        assert 'cache_write_microusd_per_million_tokens' not in before["runs"][0][0]
        assert 'cache_write_input_tokens' not in before["attempts"][0][0]
        assert 'native_result' not in before["reservations"][0][0]
    with RunStore(db_path) as upgraded:
        assert upgraded.get_run("run-a").manifest.manifest_id == manifest.manifest_id
        assert upgraded.get_attempt(model_request.attempt_id).request_sha256 == attempt.request_sha256
        assert BudgetLedger(upgraded).totals(campaign_id="campaign-a").charged.api_cost_microusd == 2
        upgraded.check_integrity()
    with sqlite3.connect(db_path) as sql:
        assert sql.execute("PRAGMA user_version").fetchone()[0] == 2
        for table, bodies in before.items():
            assert sql.execute(f"SELECT body, body_sha256 FROM {table} ORDER BY body_sha256").fetchall() == bodies
