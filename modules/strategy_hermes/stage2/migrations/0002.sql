CREATE TABLE IF NOT EXISTS supplier_batches (
    batch_id text PRIMARY KEY,
    api text NOT NULL CHECK (api IN ('trade_cal','etf_basic','index_weight')),
    requested_scope jsonb NOT NULL, collected_at timestamptz NOT NULL,
    row_count integer NOT NULL CHECK (row_count > 0), raw_rows jsonb NOT NULL,
    normalized jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_trade_calendar (
    exchange text NOT NULL, cal_date date NOT NULL, is_open boolean NOT NULL,
    batch_id text NOT NULL REFERENCES supplier_batches, PRIMARY KEY (exchange,cal_date)
);
CREATE TABLE IF NOT EXISTS supplier_etf_catalog (
    code text PRIMARY KEY, index_code text NOT NULL, list_status text NOT NULL,
    batch_id text NOT NULL REFERENCES supplier_batches, reference_data jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_index_weights (
    index_code text NOT NULL, weight_date date NOT NULL, constituent_code text NOT NULL,
    weight_ratio numeric NOT NULL CHECK (weight_ratio>=0),
    batch_id text NOT NULL REFERENCES supplier_batches,
    PRIMARY KEY (index_code,weight_date,constituent_code)
);
