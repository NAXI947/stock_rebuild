CREATE TABLE manual_drafts (
    ns text NOT NULL,
    id text NOT NULL,
    parent_id text,
    cloned_from text,
    body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(ns,id),
    FOREIGN KEY(ns,parent_id) REFERENCES manual_drafts(ns,id),
    FOREIGN KEY(ns,cloned_from) REFERENCES manual_drafts(ns,id)
);

CREATE TABLE manual_jobs (
    ns text NOT NULL,
    draft_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('QUEUED','RUNNING','CANCELLED','FAILED','WAIT_DATA','SUCCEEDED')),
    attempts integer NOT NULL DEFAULT 0,
    cancel_requested boolean NOT NULL DEFAULT false,
    claim_token uuid,
    lease_until timestamptz,
    result jsonb,
    error_code text,
    submitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY(ns,draft_id),
    FOREIGN KEY(ns,draft_id) REFERENCES manual_drafts(ns,id),
    CHECK ((status='RUNNING')=(claim_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX manual_jobs_pending ON manual_jobs(ns,submitted_at,draft_id) WHERE status='QUEUED';
CREATE INDEX manual_jobs_running ON manual_jobs(lease_until) WHERE status='RUNNING';
