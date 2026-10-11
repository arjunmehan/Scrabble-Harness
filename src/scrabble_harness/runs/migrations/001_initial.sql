CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    sha256 TEXT NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE campaigns (
    campaign_id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    manifest_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('created', 'running', 'completed', 'failed', 'cancelled')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL
) STRICT;
CREATE INDEX runs_campaign ON runs(campaign_id);

CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    game_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    observation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('preparing', 'prepared', 'completed')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    UNIQUE (run_id, decision_id),
    UNIQUE (run_id, game_id, revision)
) STRICT;

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared', 'dispatched', 'replied', 'unresolved')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    UNIQUE (run_id, decision_id, attempt_id),
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id)
) STRICT;
CREATE INDEX attempts_decision ON attempts(run_id, decision_id);

CREATE TABLE reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    attempt_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('model', 'native')),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'dispatched', 'settled', 'unresolved', 'released')),
    api_cost_microusd INTEGER NOT NULL CHECK (api_cost_microusd >= 0),
    model_calls INTEGER NOT NULL CHECK (model_calls >= 0),
    engine_work_units INTEGER NOT NULL CHECK (engine_work_units >= 0),
    tool_steps INTEGER NOT NULL CHECK (tool_steps >= 0),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    UNIQUE (attempt_id),
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id),
    FOREIGN KEY (run_id, decision_id, attempt_id) REFERENCES attempts(run_id, decision_id, attempt_id),
    CHECK ((kind = 'model' AND attempt_id IS NOT NULL AND model_calls = 1
            AND engine_work_units = 0 AND tool_steps = 0)
        OR (kind = 'native' AND attempt_id IS NULL AND api_cost_microusd = 0
            AND model_calls = 0 AND engine_work_units > 0 AND tool_steps = 1))
) STRICT;
CREATE INDEX reservations_scope ON reservations(run_id, decision_id, state);

CREATE TABLE commits (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    expected_revision INTEGER NOT NULL CHECK (expected_revision >= 0),
    new_revision INTEGER,
    source_checkpoint_id TEXT NOT NULL,
    checkpoint_id TEXT,
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id),
    UNIQUE (game_id, expected_revision),
    UNIQUE (game_id, new_revision),
    CHECK ((new_revision IS NULL AND checkpoint_id IS NULL)
        OR (new_revision IS NOT NULL AND checkpoint_id IS NOT NULL
            AND new_revision = expected_revision + 1 AND checkpoint_id != source_checkpoint_id))
) STRICT;

CREATE TABLE events (
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    decision_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'run_created', 'run_status_changed', 'decision_started', 'decision_prepared', 'decision_evidence_recorded',
        'decision_completed', 'attempt_prepared', 'attempt_dispatched', 'attempt_replied',
        'attempt_unresolved', 'reservation_created', 'reservation_changed',
        'commit_intended', 'commit_confirmed')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence),
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id)
) STRICT;

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
