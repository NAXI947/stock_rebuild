-- OCTOP owns the five clocks; the stock worker owns collection and publication.
-- Frozen inputs and submitted decisions survive process/container restarts.
CREATE TABLE native_round_jobs (
    ns text NOT NULL,
    run_id uuid NOT NULL,
    trade_date date NOT NULL,
    slot text NOT NULL CHECK (slot IN ('09:35','10:35','13:05','14:05','14:30')),
    state text NOT NULL CHECK (state IN
        ('QUEUED','PREPARING','READY','SUBMITTED','PUBLISHING','PUBLISHED','FAILED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT clock_timestamp() + interval '600 seconds',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid,
    context jsonb,
    decisions jsonb,
    decision_hash text,
    report_id text,
    error_code text,
    failure_stage text,
    PRIMARY KEY (ns,run_id),
    UNIQUE (ns,trade_date,slot)
);
CREATE INDEX native_round_jobs_pending ON native_round_jobs(ns,created_at)
    WHERE state NOT IN ('PUBLISHED','FAILED');
