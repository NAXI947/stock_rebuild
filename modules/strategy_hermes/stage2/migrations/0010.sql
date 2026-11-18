CREATE TABLE left_right_backfill_jobs (
    ns text NOT NULL,
    job_id text NOT NULL,
    request_id text NOT NULL,
    payload_hash text NOT NULL,
    body jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')),
    request_budget integer NOT NULL CHECK (request_budget BETWEEN 0 AND 800),
    requests_used integer NOT NULL DEFAULT 0 CHECK (requests_used >= 0 AND requests_used <= request_budget),
    attempts integer NOT NULL DEFAULT 0,
    cancel_requested boolean NOT NULL DEFAULT false,
    claim_token text,
    lease_until timestamptz,
    result jsonb,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY(ns,job_id),
    UNIQUE(ns,request_id)
);
CREATE INDEX left_right_backfill_pending ON left_right_backfill_jobs(status,created_at);
CREATE TABLE left_right_backfill_requests (
    ns text NOT NULL,
    job_id text NOT NULL,
    request_key text NOT NULL,
    source text NOT NULL,
    status text NOT NULL CHECK (status IN ('STARTED','SAVED','FAILED')),
    result jsonb,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY(ns,job_id,request_key),
    FOREIGN KEY(ns,job_id) REFERENCES left_right_backfill_jobs(ns,job_id)
);
