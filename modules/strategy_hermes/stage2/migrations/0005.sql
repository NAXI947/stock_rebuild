CREATE TABLE collection_requests (
    run_key text NOT NULL,
    source text NOT NULL,
    request_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('STARTED','SAVED','FAILED')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    result jsonb,
    PRIMARY KEY (run_key,source,request_key)
);
