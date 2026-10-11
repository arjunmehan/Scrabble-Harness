ALTER TABLE reservations ADD COLUMN analysis_id TEXT;
CREATE UNIQUE INDEX reservations_analysis_identity ON reservations(analysis_id) WHERE analysis_id IS NOT NULL;

CREATE TABLE finish_holds (
    hold_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    consumed_attempt_id TEXT UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('held', 'consumed', 'released')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id),
    FOREIGN KEY (run_id, decision_id, consumed_attempt_id) REFERENCES attempts(run_id, decision_id, attempt_id),
    CHECK ((state = 'consumed' AND consumed_attempt_id IS NOT NULL)
        OR (state != 'consumed' AND consumed_attempt_id IS NULL))
) STRICT;

CREATE UNIQUE INDEX runs_campaign_identity ON runs(run_id, campaign_id);
CREATE TABLE budget_clocks (
    clock_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('campaign', 'run', 'decision')),
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    run_id TEXT,
    decision_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'expired')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    FOREIGN KEY (run_id, campaign_id) REFERENCES runs(run_id, campaign_id),
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id),
    CHECK ((scope = 'campaign' AND run_id IS NULL AND decision_id IS NULL)
        OR (scope = 'run' AND run_id IS NOT NULL AND decision_id IS NULL)
        OR (scope = 'decision' AND run_id IS NOT NULL AND decision_id IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX clocks_campaign ON budget_clocks(campaign_id) WHERE scope='campaign';
CREATE UNIQUE INDEX clocks_run ON budget_clocks(run_id) WHERE scope='run';
CREATE UNIQUE INDEX clocks_decision ON budget_clocks(decision_id) WHERE scope='decision';

-- Preserve every existing event byte-for-byte while extending the allowed kinds.
DROP TRIGGER events_no_update;
DROP TRIGGER events_no_delete;
ALTER TABLE events RENAME TO events_v1;
CREATE TABLE events (
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    decision_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN (
        'run_created', 'run_status_changed', 'decision_started', 'decision_prepared', 'decision_evidence_recorded',
        'decision_completed', 'attempt_prepared', 'attempt_dispatched', 'attempt_replied',
        'attempt_unresolved', 'reservation_created', 'reservation_changed',
        'commit_intended', 'commit_confirmed', 'finish_reserved', 'finish_consumed', 'finish_released')),
    body TEXT NOT NULL,
    body_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence),
    FOREIGN KEY (run_id, decision_id) REFERENCES decisions(run_id, decision_id)
) STRICT;
INSERT INTO events SELECT * FROM events_v1;
DROP TABLE events_v1;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END;
