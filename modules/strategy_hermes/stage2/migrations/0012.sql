ALTER TABLE supplier_market_batches DROP CONSTRAINT supplier_market_batches_row_count_check;
ALTER TABLE supplier_market_batches ADD CONSTRAINT supplier_market_batches_row_count_check CHECK (row_count>=0);
CREATE TABLE supplier_reference_rows (
    dataset text NOT NULL,
    entity_key text NOT NULL,
    business_date date NOT NULL,
    observed_at timestamptz NOT NULL,
    body jsonb NOT NULL,
    batch_id text NOT NULL REFERENCES supplier_market_batches(batch_id),
    PRIMARY KEY(dataset,entity_key,business_date)
);
CREATE INDEX supplier_reference_rows_date ON supplier_reference_rows(dataset,business_date);
CREATE TABLE data_update_units (
    ns text NOT NULL,
    job_id text NOT NULL,
    unit_key text NOT NULL,
    dataset text NOT NULL,
    business_date date NOT NULL,
    status text NOT NULL CHECK(status IN ('PENDING','RUNNING','OK','EMPTY','FAILED','CANCELLED')),
    row_count integer CHECK(row_count>=0),
    source_at timestamptz,
    details jsonb NOT NULL DEFAULT '{}',
    error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY(ns,job_id,unit_key),
    FOREIGN KEY(ns,job_id) REFERENCES left_right_backfill_jobs(ns,job_id) ON DELETE CASCADE
);
CREATE INDEX data_update_units_recent ON data_update_units(ns,created_at DESC);
CREATE INDEX data_update_units_dataset ON data_update_units(ns,dataset,business_date DESC,created_at DESC);
