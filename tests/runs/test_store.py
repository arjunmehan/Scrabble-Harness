"""Persistence acceptance tests; no provider calls or model SDKs."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import subprocess
import sys

import pytest
from pydantic import ValidationError

from scrabble_harness.domain import (
    CandidateProvenance, EngineConfig, ExchangeMove, LexiconConfig, ModelSettings,
    MovePreview, PassMove, RulesetConfig, RunConfig, RunLimits, RunSeeds,
    canonical_bytes, canonical_json, build_candidate,
)
from scrabble_harness.harness import (
    DecisionContext, DecisionOutcome, DependencyVersion, ExecutionManifest,
    HarnessConfig, RateCard, build_model_request,
)
from scrabble_harness.providers.protocol import (
    ModelReply, ProviderCapabilities, ProviderContinuation, ProviderToolCall, TokenUsage,
)
from scrabble_harness.runs import (
    CommitRecord, EventRecord, ReservationRecord, ResourceAmounts, RunStore,
    StoreBusyError, StoreConflictError, StoreCorruptionError, StoreError,
    StoreNotFoundError, StorePathError,
)


def changed(record, **updates):
    return type(record).model_validate(record.model_dump(mode="python") | updates)


@pytest.fixture
def context(active_observation):
    obs = active_observation
    provenance = CandidateProvenance(source="storage-fixture", configuration_id="fixture-v1")
    passing = build_candidate(obs, PassMove(), exact_score=0, leave=obs.own_rack,
                              static_equity=1.0, provenance=provenance)
    exchange = build_candidate(obs, ExchangeMove(tiles=(obs.own_rack[0],)), exact_score=0,
                               leave=obs.own_rack[1:], static_equity=0.0, provenance=provenance)
    preview = MovePreview(observation_id=obs.observation_id, move=passing.move,
                          exact_score=0, leave=passing.leave, board_after=obs.board)
    return DecisionContext.from_candidates(decision_id="decision-a", observation=obs,
        config=HarnessConfig(), candidate_order_seed=123, candidates=(passing, exchange), fallback_preview=preview)


@pytest.fixture
def manifest(context):
    run = RunConfig(model=ModelSettings(provider="fixture", model="fixture-model", max_output_tokens=1024),
        engine=EngineConfig(engine_commit="a" * 40),
        lexicon=LexiconConfig(lexicon_id=context.observation.lexicon_id, sha256="b" * 64),
        ruleset=RulesetConfig(ruleset_id=context.observation.ruleset_id, scoreless_turn_limit=6),
        policy_version=context.config.workflow, candidate_config=context.config.candidate_config,
        seeds=RunSeeds(game=42, analysis=73, candidate_order=123, trial=1),
        limits=RunLimits(max_api_cost_microusd=1000000, max_model_calls=100,
            max_engine_work_units=10000, engine_work_unit="candidate_rollout", max_elapsed_seconds=3600.0))
    return ExecutionManifest.create(run_config=run, harness_config=context.config,
        engine_configuration_id="engine-v1", source_sha256="c" * 64, trial_id="trial-a",
        dependencies=(DependencyVersion(name="python", version="3.13.5"),
                      DependencyVersion(name="pydantic", version="2.10.3")),
        rate_card=RateCard(provider="fixture", model="fixture-model", input_microusd_per_million_tokens=100000,
            output_microusd_per_million_tokens=200000, source_url="https://example.com/synthetic-test-rates",
            retrieved_on="2026-10-07", estimation_method="published-token-rates-v1"))


@pytest.fixture
def model_request(context, manifest):
    model = manifest.run_config.model
    caps = ProviderCapabilities(provider=model.provider, model=model.model, max_output_tokens=1024)
    return build_model_request(context.model_view(), attempt_id="attempt-a", model=model, capabilities=caps,
        remaining_limits=context.config.decision_limits, config=context.config)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "private" / "runs.sqlite3"


@pytest.fixture
def store(db_path, manifest, context):
    with RunStore(db_path) as store:
        store.create_campaign("campaign-a", manifest.run_config.limits)
        store.create_run("run-a", "campaign-a", manifest)
        store.prepare_decision("run-a", context)
        yield store


def reservation(name="reservation-a", attempt_id="attempt-a", **changes):
    return ReservationRecord(reservation_id=name, run_id="run-a", decision_id="decision-a",
        kind="model", attempt_id=attempt_id, allocation=ResourceAmounts(api_cost_microusd=2500, model_calls=1), **changes)


def reply(model_request, **updates):
    return ModelReply(attempt_id=model_request.attempt_id, decision_id=model_request.view.decision_id,
        observation_id=model_request.view.observation.observation_id, provider=model_request.model.provider,
        resolved_model="fixture-model-resolved", response_id="response-a", request_id="provider-model_request-a",
        status="completed", tool_calls=(ProviderToolCall(call_id="call-a", name="select_move",
        arguments_json="{bad model JSON retained verbatim"),), usage=TokenUsage(input_tokens=10, output_tokens=5),
        continuation=ProviderContinuation(provider="fixture", payload_json='{"private_adapter_state":"secret"}'), **updates)


def finish(store, context):
    outcome = DecisionOutcome(decision_id=context.decision_id, observation_id=context.observation.observation_id,
        status="fallback", decision=context.fallback_decision("test-fallback"), trusted_pool=context.full_pool,
        visible_bindings=context.visible_bindings, evidence=context.evidence, terminal_reason="test-fallback")
    return store.complete_decision(outcome)


def test_complete_private_lifecycle_survives_reopen(store, model_request, context, db_path):
    attempt = store.prepare_attempt("run-a", "decision-a", model_request)
    assert attempt.request_sha256 == hashlib.sha256(canonical_bytes(model_request)).hexdigest()
    store.create_reservation(reservation())
    with store.transaction():
        store.mark_attempt_dispatched("attempt-a")
        store.transition_reservation("reservation-a", "dispatched")
    store.record_reply(reply(model_request))
    store.transition_reservation("reservation-a", "settled",
                                 settlement=ResourceAmounts(api_cost_microusd=700, model_calls=1))
    finish(store, context)
    intent = CommitRecord(run_id="run-a", decision_id=context.decision_id, game_id=context.observation.game_id,
        expected_revision=context.observation.revision, move=PassMove(), source_checkpoint_id="checkpoint-before")
    store.intend_commit(intent)
    store.confirm_commit("decision-a", new_revision=context.observation.revision + 1, checkpoint_id="checkpoint-after")
    store.set_run_status("run-a", "running")
    store.set_run_status("run-a", "completed")
    expected = store.events("run-a")
    store.check_integrity()
    store.close()
    with RunStore(db_path) as reopened:
        assert reopened.get_campaign("campaign-a").campaign_id == "campaign-a"
        assert reopened.get_run("run-a").status == "completed"
        assert reopened.get_decision("decision-a").status == "completed"
        assert reopened.get_attempt("attempt-a").reply.tool_calls[0].arguments_json.startswith("{bad")
        assert "secret" in reopened.get_attempt("attempt-a").reply.continuation.payload_json
        assert reopened.get_reservation("reservation-a").settlement.api_cost_microusd == 700
        assert reopened.get_commit("decision-a").checkpoint_id == "checkpoint-after"
        assert reopened.events("run-a") == expected
        assert [event.sequence for event in expected] == list(range(1, len(expected) + 1))
        assert all(EventRecord.model_validate_json(canonical_json(event)) == event for event in expected)


def test_idempotent_retries_preserve_events_and_creation_time(store, manifest, context, model_request):
    before = store.events("run-a")
    run = store.get_run("run-a")
    reversed_manifest = changed(manifest, dependencies=tuple(reversed(manifest.dependencies)))
    assert store.create_run("run-a", "campaign-a", reversed_manifest) == run
    assert store.create_campaign("campaign-a", manifest.run_config.limits) == store.get_campaign("campaign-a")
    assert store.prepare_decision("run-a", context) == store.get_decision("decision-a")
    assert store.events("run-a") == before
    store.prepare_attempt("run-a", "decision-a", model_request)
    store.mark_attempt_dispatched("attempt-a")
    store.record_reply(reply(model_request))
    after = store.events("run-a")
    assert store.prepare_attempt("run-a", "decision-a", model_request).state == "replied"
    assert store.record_reply(reply(model_request)).state == "replied"
    assert store.events("run-a") == after


def test_conflicting_creation_and_reply_cannot_overwrite(store, manifest, context, model_request):
    with pytest.raises(StoreConflictError):
        store.create_campaign("campaign-a", changed(manifest.run_config.limits, max_model_calls=101))
    different = ExecutionManifest.create(**manifest.model_dump(mode="python", exclude={"manifest_id"}) | {"trial_id": "trial-b"})
    with pytest.raises(StoreConflictError):
        store.create_run("run-a", "campaign-a", different)
    with pytest.raises(StoreConflictError):
        store.prepare_decision("run-a", changed(context, candidate_order_seed=456))
    store.prepare_attempt("run-a", "decision-a", model_request)
    with pytest.raises(StoreConflictError):
        store.prepare_attempt("run-a", "decision-a", changed(model_request, instructions="different prompt"))
    store.mark_attempt_dispatched("attempt-a")
    store.record_reply(reply(model_request))
    different_reply = changed(reply(model_request), response_id="different-response")
    with pytest.raises(StoreConflictError):
        store.record_reply(different_reply)
    assert store.get_attempt("attempt-a").reply.response_id == "response-a"


def test_record_and_event_roll_back_together(store, model_request, monkeypatch):
    previous = store.events("run-a")
    def fail(*args):
        raise RuntimeError("injected event failure")
    with monkeypatch.context() as patch:
        patch.setattr(store, "_event", fail)
        with pytest.raises(RuntimeError):
            store.prepare_attempt("run-a", "decision-a", model_request)
    with pytest.raises(StoreNotFoundError):
        store.get_attempt("attempt-a")
    assert store.events("run-a") == previous
    store.prepare_attempt("run-a", "decision-a", model_request)
    assert store.events("run-a")[-1].sequence == previous[-1].sequence + 1


def test_outer_transaction_and_nested_savepoint_rollbacks(store, manifest):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.create_run("rolled-back", "campaign-a", manifest)
            raise RuntimeError("cancel transaction")
    with pytest.raises(StoreNotFoundError):
        store.get_run("rolled-back")
    with store.transaction():
        store.create_run("survives", "campaign-a", manifest)
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.create_run("nested-rollback", "campaign-a", manifest)
                raise RuntimeError("cancel savepoint")
        store.set_run_status("survives", "running")
    assert store.get_run("survives").status == "running"
    with pytest.raises(StoreNotFoundError):
        store.get_run("nested-rollback")
    assert [event.sequence for event in store.events("survives")] == [1, 2]


@pytest.mark.parametrize("reservation_state", ["dispatched", "unresolved", "settled"])
def test_dispatched_resources_cannot_be_released(store, model_request, reservation_state):
    store.prepare_attempt("run-a", "decision-a", model_request)
    store.create_reservation(reservation())
    store.mark_attempt_dispatched("attempt-a")
    store.transition_reservation("reservation-a", "dispatched")
    if reservation_state != "dispatched":
        store.transition_reservation("reservation-a", reservation_state,
            settlement=ResourceAmounts(api_cost_microusd=2500, model_calls=1) if reservation_state == "settled" else None)
    with pytest.raises(StoreConflictError):
        store.transition_reservation("reservation-a", "released")


def test_unresolved_state_survives_and_accepts_late_reply(store, model_request, db_path):
    store.prepare_attempt("run-a", "decision-a", model_request)
    store.create_reservation(reservation())
    with store.transaction():
        store.mark_attempt_dispatched("attempt-a")
        store.transition_reservation("reservation-a", "dispatched")
    store.mark_attempt_unresolved("attempt-a")
    store.transition_reservation("reservation-a", "unresolved")
    store.close()
    with RunStore(db_path) as reopened:
        assert reopened.get_attempt("attempt-a").state == "unresolved"
        assert reopened.get_reservation("reservation-a").allocation.api_cost_microusd == 2500
        with pytest.raises(StoreConflictError):
            reopened.mark_attempt_dispatched("attempt-a")
        reopened.record_reply(reply(model_request))
        reopened.transition_reservation("reservation-a", "settled",
            settlement=ResourceAmounts(api_cost_microusd=3000, model_calls=1))
        assert reopened.get_reservation("reservation-a").settlement.api_cost_microusd == 3000


def test_releasing_undispatched_allocation_and_unique_attempt_reservation(store, model_request):
    store.prepare_attempt("run-a", "decision-a", model_request)
    store.create_reservation(reservation())
    with pytest.raises(StoreConflictError):
        store.create_reservation(reservation("another-id"))
    store.transition_reservation("reservation-a", "released")
    assert store.create_reservation(reservation()).state == "released"


@pytest.mark.parametrize("action", ["reply-before-dispatch", "reservation-before-dispatch", "resurrect-run", "duplicate-revision", "model_request-wrong-run"])
def test_invalid_transitions_and_scope(store, context, manifest, model_request, action):
    store.prepare_attempt("run-a", "decision-a", model_request)
    with pytest.raises((StoreConflictError, ValidationError)):
        if action == "reply-before-dispatch":
            store.record_reply(reply(model_request))
        elif action == "reservation-before-dispatch":
            store.create_reservation(reservation())
            store.transition_reservation("reservation-a", "dispatched")
        elif action == "resurrect-run":
            store.set_run_status("run-a", "cancelled")
            store.set_run_status("run-a", "running")
        elif action == "duplicate-revision":
            store.prepare_decision("run-a", changed(context, decision_id="decision-b"))
        else:
            store.create_run("run-b", "campaign-a", manifest)
            store.prepare_attempt("run-b", "decision-a", changed(model_request, attempt_id="attempt-b"))


def test_revision_uniqueness_across_runs_and_conflicting_confirmations(store, context, manifest):
    finish(store, context)
    intent = CommitRecord(run_id="run-a", decision_id="decision-a", game_id=context.observation.game_id,
        expected_revision=context.observation.revision, move=PassMove(), source_checkpoint_id="before")
    store.intend_commit(intent)
    store.confirm_commit("decision-a", new_revision=context.observation.revision + 1, checkpoint_id="after")
    previous = store.events("run-a")
    assert store.intend_commit(intent).checkpoint_id == "after"
    store.confirm_commit("decision-a", new_revision=context.observation.revision + 1, checkpoint_id="after")
    assert store.events("run-a") == previous
    with pytest.raises(StoreConflictError):
        store.confirm_commit("decision-a", new_revision=context.observation.revision + 1, checkpoint_id="other")
    store.create_run("run-b", "campaign-a", manifest)
    other = changed(context, decision_id="decision-b")
    store.prepare_decision("run-b", other)
    finish(store, other)
    with pytest.raises(StoreConflictError):
        store.intend_commit(changed(intent, run_id="run-b", decision_id="decision-b"))
    with pytest.raises(ValidationError):
        store.confirm_commit("decision-a", new_revision=context.observation.revision + 2, checkpoint_id="other")


def test_commit_requires_selected_physical_action(store, context):
    finish(store, context)
    intent = CommitRecord(run_id="run-a", decision_id="decision-a", game_id=context.observation.game_id,
        expected_revision=context.observation.revision, move=ExchangeMove(tiles=(context.observation.own_rack[0],)),
        source_checkpoint_id="before")
    with pytest.raises(StoreConflictError):
        store.intend_commit(intent)
    with pytest.raises(StoreNotFoundError):
        store.get_commit("decision-a")


def test_event_pagination(store, model_request):
    store.prepare_attempt("run-a", "decision-a", model_request)
    assert [event.sequence for event in store.events("run-a", after_sequence=1, limit=1)] == [2]
    assert store.events("run-a", after_sequence=3) == ()
    with pytest.raises(ValueError):
        store.events("run-a", after_sequence=True)


def test_private_files_and_durable_pragmas(store, db_path):
    diagnostics = store.diagnostics()
    assert diagnostics["journal_mode"] == "wal"
    assert diagnostics["synchronous"] == 2
    assert diagnostics["foreign_keys"] == 1
    assert diagnostics["schema_version"] == 2
    assert db_path.parent.stat().st_mode & 0o777 == 0o700
    for file in db_path.parent.iterdir():
        assert file.stat().st_mode & 0o777 == 0o600


def test_reject_public_directory_and_symlink(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(StorePathError):
        RunStore(public / "runs.sqlite3")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    original = private / "original"
    original.write_text("untouched")
    link = private / "linked"
    link.symlink_to(original)
    with pytest.raises(StorePathError):
        RunStore(link)
    assert original.read_text() == "untouched"


def test_database_identity_and_migration_checksums(store, db_path):
    store.close()
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE schema_migrations SET sha256=?", ("d" * 64,))
    with pytest.raises(StoreCorruptionError):
        RunStore(db_path)


@pytest.mark.parametrize("kind", ["foreign", "future"])
def test_reject_foreign_or_future_database(db_path, kind):
    db_path.parent.mkdir(mode=0o700)
    with sqlite3.connect(db_path) as raw:
        if kind == "foreign":
            raw.execute("CREATE TABLE unrelated (value TEXT)")
        else:
            raw.execute("PRAGMA application_id=1396920904")
            raw.execute("PRAGMA user_version=999")
    with pytest.raises(StoreCorruptionError):
        RunStore(db_path)
    with sqlite3.connect(db_path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("tampering", ["body", "index"])
def test_corrupt_content_or_index_is_detected(store, db_path, tampering):
    store.close()
    with sqlite3.connect(db_path) as raw:
        if tampering == "body":
            raw.execute("UPDATE decisions SET body='{}' WHERE decision_id='decision-a'")
        else:
            raw.execute("UPDATE decisions SET observation_id='wrong' WHERE decision_id='decision-a'")
    with RunStore(db_path) as reopened:
        with pytest.raises(StoreCorruptionError):
            reopened.get_decision("decision-a")


def test_sql_foreign_keys_and_immutable_events(store):
    connection = store._connection  # Deliberate direct SQL fault injection only.
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM campaigns WHERE campaign_id='campaign-a'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE events SET kind='run_status_changed'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM events")


def test_atomic_migration_failure_rolls_back_all_ddl(db_path, monkeypatch):
    import scrabble_harness.runs.store as module
    from scrabble_harness.runs.migrations import Migration, MIGRATIONS
    broken = Migration(1, MIGRATIONS[0].sql + '\nCREATE TABLE broken( ;\n')
    with monkeypatch.context() as patch:
        patch.setattr(module, "MIGRATIONS", (broken,))
        with pytest.raises((sqlite3.DatabaseError, ValueError)):
            RunStore(db_path)
    with sqlite3.connect(db_path) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
        assert raw.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() == []
    with RunStore(db_path) as valid:
        assert valid.diagnostics()["schema_version"] == 2


def test_hard_crash_before_and_after_commit(store, db_path, manifest):
    store.close()
    code = '''
import os, sys
from scrabble_harness.runs import RunStore
with RunStore(sys.argv[1]) as store:
    manifest = store.get_run("run-a").manifest
    if sys.argv[2] == "before":
        with store.transaction():
            store.create_run("crash-before", "campaign-a", manifest)
            os._exit(17)
    store.create_run("crash-after", "campaign-a", manifest)
    os._exit(18)
'''
    for phase, expected in (("before", 17), ("after", 18)):
        result = subprocess.run([sys.executable, "-c", code, str(db_path), phase], capture_output=True, timeout=15)
        assert result.returncode == expected, result.stderr.decode()
    with RunStore(db_path) as reopened:
        with pytest.raises(StoreNotFoundError):
            reopened.get_run("crash-before")
        assert reopened.get_run("crash-after").status == "created"
        assert [event.sequence for event in reopened.events("crash-after")] == [1]
        reopened.check_integrity()


def test_concurrent_threads_have_unique_contiguous_events(store, db_path):
    def writer(index):
        with RunStore(db_path) as local:
            for step in range(8):
                local.create_reservation(ReservationRecord(reservation_id=f"native-{index}-{step}",
                    run_id="run-a", decision_id="decision-a", kind="native",
                    allocation=ResourceAmounts(engine_work_units=1, tool_steps=1)))
                local.checkpoint()
            local.create_reservation(ReservationRecord(reservation_id="same-identity",
                run_id="run-a", decision_id="decision-a", kind="native",
                allocation=ResourceAmounts(engine_work_units=1, tool_steps=1)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(writer, range(4)))
    events = store.events("run-a")
    assert len(events) == 35  # Run + decision + 32 unique allocations + one retry identity.
    assert [event.sequence for event in events] == list(range(1, 36))
    assert len(store.reservations("run-a")) == 33
    store.check_integrity()


def test_concurrent_processes_share_writer_lock(store, db_path):
    code = '''
import sys
from scrabble_harness.runs import RunStore, ReservationRecord, ResourceAmounts
with RunStore(sys.argv[1]) as store:
    for n in range(8):
        store.create_reservation(ReservationRecord(reservation_id=f"process-{sys.argv[2]}-{n}",
            run_id="run-a", decision_id="decision-a", kind="native",
            allocation=ResourceAmounts(engine_work_units=1, tool_steps=1)))
        store.checkpoint()
'''
    processes = [subprocess.Popen([sys.executable, "-c", code, str(db_path), str(index)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(3)]
    for process in processes:
        output, error = process.communicate(timeout=20)
        assert process.returncode == 0, error.decode()
    assert [event.sequence for event in store.events("run-a")] == list(range(1, 27))
    store.check_integrity()


def test_bounded_busy_timeout_and_connection_ownership(store, db_path):
    with RunStore(db_path, busy_timeout_seconds=0.05) as other:
        with store.transaction():
            with pytest.raises(StoreBusyError):
                other.set_run_status("run-a", "running")
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(StoreError, match="separate store"):
                pool.submit(other.get_run, "run-a").result()
    store.close()
    with pytest.raises(StoreError, match="closed"):
        store.get_run("run-a")


@pytest.mark.parametrize("value", [-1, True, 2**63])
def test_integer_amounts_reject_invalid_values(value):
    with pytest.raises(ValidationError):
        ResourceAmounts(api_cost_microusd=value)


def test_preparation_failure_has_durable_observation_and_aborted_outcome(store, context, manifest):
    store.create_run("failed-preparation", "campaign-a", manifest)
    begun = store.start_decision("failed-preparation", "failed-decision", context.observation)
    assert begun.status == "preparing" and begun.context is None
    outcome = DecisionOutcome(decision_id="failed-decision", observation_id=context.observation.observation_id,
                              status="aborted", terminal_reason="native-preparation-failed")
    finished = store.complete_decision(outcome)
    assert finished.status == "completed" and finished.context is None
    assert [event.payload.kind for event in store.events("failed-preparation")] == [
        "run_created", "decision_started", "decision_completed"]
    with pytest.raises(StoreConflictError):
        store.prepare_decision("failed-preparation", changed(context, decision_id="failed-decision"))
    assert store.start_decision("failed-preparation", "failed-decision", context.observation) == finished


def test_started_decision_accepts_one_frozen_context(store, context, manifest):
    store.create_run("started-run", "campaign-a", manifest)
    other = changed(context, decision_id="started-decision")
    store.start_decision("started-run", other.decision_id, other.observation)
    prepared = store.prepare_decision("started-run", other)
    assert prepared.status == "prepared"
    assert [event.payload.kind for event in store.events("started-run")] == [
        "run_created", "decision_started", "decision_prepared"]


def test_evidence_is_append_only_and_requests_require_saved_evidence(store, context, model_request):
    from scrabble_harness.domain import AnalysisLimits, AnalysisResult, CandidateEvaluation, MetricDefinition, WorkUsed
    from scrabble_harness.harness import CompareAction, DecisionUsage, ScopedAction
    envelope = ScopedAction(decision_id=context.decision_id, observation_id=context.observation.observation_id,
                            action=CompareAction(candidate_handles=("c01", "c02"), tier="light"))
    result = AnalysisResult(analysis_id="analysis-a", observation_id=context.observation.observation_id,
        values=tuple(CandidateEvaluation(candidate_id=c.candidate_id, value=float(n))
                     for n, c in enumerate(context.resolve_comparison(envelope))),
        metric_definition=MetricDefinition(name="synthetic", unit="points", description="Storage fixture", value_kind="estimate"),
        work_used=WorkUsed(unit="candidate_rollout", definition="Total candidate rollouts", count=12),
        elapsed_time_seconds=0.2, seed=73, completion_status="work_limit")
    updated, evidence = context.issue_evidence(envelope, result,
        limits=AnalysisLimits(max_work_units=12, work_unit="candidate_rollout", max_elapsed_seconds=4.0), seed=73)
    assert store.record_evidence(updated).context.evidence == (evidence,)
    previous = store.events("run-a")
    store.record_evidence(updated)
    assert store.events("run-a") == previous
    with pytest.raises(StoreConflictError):
        store.record_evidence(context)
    with pytest.raises(StoreConflictError):
        store.prepare_attempt("run-a", "decision-a", model_request)
    updated_request = build_model_request(updated.model_view(), attempt_id="attempt-a", model=model_request.model,
        capabilities=model_request.capabilities, remaining_limits=updated.config.decision_limits,
        config=updated.config, evidence=updated.evidence_views())
    store.prepare_attempt("run-a", "decision-a", updated_request)
    outcome = DecisionOutcome(decision_id=context.decision_id, observation_id=context.observation.observation_id,
        status="fallback", decision=updated.fallback_decision("done"), trusted_pool=updated.full_pool,
        visible_bindings=updated.visible_bindings, evidence=updated.evidence,
        usage=DecisionUsage(confirmed_engine_work_units=12), terminal_reason="done")
    store.complete_decision(outcome)
    assert store.get_decision("decision-a").outcome.evidence == (evidence,)


def test_released_attempt_cannot_later_be_dispatched(store, model_request):
    store.prepare_attempt("run-a", "decision-a", model_request)
    store.create_reservation(reservation())
    store.transition_reservation("reservation-a", "released")
    with pytest.raises(StoreConflictError):
        store.mark_attempt_dispatched("attempt-a")
    assert store.get_attempt("attempt-a").state == "prepared"


def test_reply_scope_validation_leaves_dispatched_attempt_unchanged(store, model_request):
    store.prepare_attempt("run-a", "decision-a", model_request)
    store.mark_attempt_dispatched("attempt-a")
    with pytest.raises(ValidationError):
        store.record_reply(changed(reply(model_request), decision_id="other-decision"))
    assert store.get_attempt("attempt-a").state == "dispatched"


def test_two_writer_identity_conflict_has_one_winner(store, db_path):
    def writer(amount):
        try:
            with RunStore(db_path) as local:
                local.create_reservation(ReservationRecord(reservation_id="contested",
                    run_id="run-a", decision_id="decision-a", kind="native",
                    allocation=ResourceAmounts(engine_work_units=amount, tool_steps=1)))
                return "stored"
        except StoreConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, (10, 20)))
    assert sorted(results) == ["conflict", "stored"]
    assert len(store.events("run-a")) == 3
    assert store.get_reservation("contested").allocation.engine_work_units in {10, 20}
