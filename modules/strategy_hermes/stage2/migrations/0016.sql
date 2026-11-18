-- Recovery attempts live separately so the original slot key, and old-image
-- INSERT ... ON CONFLICT(ns,trade_date,slot), remain valid after an upgrade.
CREATE TABLE native_round_recovery_jobs (
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
    retry_of uuid NOT NULL,
    PRIMARY KEY (ns,run_id),
    UNIQUE (ns,retry_of),
    FOREIGN KEY (ns,retry_of) REFERENCES native_round_jobs(ns,run_id)
);
CREATE INDEX native_round_recovery_jobs_pending ON native_round_recovery_jobs(ns,created_at)
    WHERE state NOT IN ('PUBLISHED','FAILED');

-- Read both kinds of attempt without making an ambiguous writable relation.
CREATE VIEW native_round_attempts AS
    SELECT ns,run_id,trade_date,slot,state,created_at,expires_at,updated_at,
           lease_token,context,decisions,decision_hash,report_id,error_code,
           failure_stage,NULL::uuid AS retry_of
    FROM native_round_jobs
    UNION ALL
    SELECT ns,run_id,trade_date,slot,state,created_at,expires_at,updated_at,
           lease_token,context,decisions,decision_hash,report_id,error_code,
           failure_stage,retry_of
    FROM native_round_recovery_jobs;

CREATE TABLE native_publication_recoveries (
    ns text NOT NULL,
    parent_run_id uuid NOT NULL,
    child_run_id uuid NOT NULL,
    authorized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL DEFAULT clock_timestamp() + interval '600 seconds',
    authorized_by text NOT NULL,
    reason text NOT NULL,
    parent_task jsonb NOT NULL,
    context_hash text NOT NULL,
    decision_hash text NOT NULL,
    consumed_at timestamptz,
    PRIMARY KEY (ns,parent_run_id),
    UNIQUE (ns,child_run_id),
    FOREIGN KEY (ns,parent_run_id) REFERENCES native_round_jobs(ns,run_id)
);
