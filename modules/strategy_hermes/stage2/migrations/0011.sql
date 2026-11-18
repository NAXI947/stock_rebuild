ALTER TABLE left_right_backfill_jobs DROP CONSTRAINT left_right_backfill_jobs_request_budget_check;
ALTER TABLE left_right_backfill_jobs ADD CONSTRAINT left_right_backfill_jobs_request_budget_check
    CHECK (request_budget BETWEEN 0 AND 12000);
ALTER TABLE left_right_backfill_jobs ADD COLUMN elapsed_seconds double precision NOT NULL DEFAULT 0
    CHECK (elapsed_seconds>=0);
ALTER TABLE left_right_backfill_jobs ADD COLUMN last_active_at timestamptz;
CREATE INDEX supplier_daily_bars_day ON supplier_daily_bars(trade_date,code);

CREATE TABLE supplier_tushare_pages (
    request_key text PRIMARY KEY,
    source_key text NOT NULL,
    api text NOT NULL,
    scope jsonb NOT NULL,
    fields jsonb NOT NULL,
    rows jsonb NOT NULL,
    collected_at timestamptz NOT NULL
);
CREATE INDEX supplier_tushare_pages_expiry ON supplier_tushare_pages(collected_at);
CREATE TABLE supplier_tushare_eod_runs (
    run_key text PRIMARY KEY,
    source_key text NOT NULL,
    api text NOT NULL,
    trade_date date NOT NULL,
    schema_version text NOT NULL,
    batch_id text NOT NULL,
    row_count integer NOT NULL CHECK (row_count>0),
    completed_at timestamptz NOT NULL
);
CREATE INDEX supplier_tushare_eod_runs_day ON supplier_tushare_eod_runs(api,trade_date);
CREATE TABLE supplier_lr_features (
    api text NOT NULL,
    code text NOT NULL,
    trade_date date NOT NULL,
    body jsonb NOT NULL,
    batch_id text NOT NULL REFERENCES supplier_market_batches(batch_id),
    PRIMARY KEY(api,code,trade_date)
);
CREATE INDEX supplier_lr_features_history ON supplier_lr_features(code,trade_date);

CREATE TABLE left_right_assessments (
    ns text NOT NULL,
    trade_date date NOT NULL,
    code text NOT NULL,
    family text NOT NULL,
    published_at timestamptz NOT NULL,
    evidence_sha256 text NOT NULL,
    status text NOT NULL,
    setup_score numeric,
    return_ratio numeric,
    name text,
    version_id text NOT NULL,
    source text NOT NULL,
    body jsonb NOT NULL,
    PRIMARY KEY(ns,trade_date,code,family)
);
CREATE INDEX left_right_assessments_rank ON left_right_assessments(ns,trade_date,family,setup_score DESC,code);
CREATE INDEX left_right_assessments_history ON left_right_assessments(ns,trade_date DESC);
CREATE TABLE left_right_assessment_sectors (
    ns text NOT NULL,
    trade_date date NOT NULL,
    code text NOT NULL,
    family text NOT NULL,
    sector_id text NOT NULL,
    PRIMARY KEY(ns,trade_date,code,family,sector_id),
    FOREIGN KEY(ns,trade_date,code,family) REFERENCES left_right_assessments(ns,trade_date,code,family) ON DELETE CASCADE
);
CREATE INDEX left_right_assessment_sector_rank ON left_right_assessment_sectors(ns,trade_date,sector_id,family);
