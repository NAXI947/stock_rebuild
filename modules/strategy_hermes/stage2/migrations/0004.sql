CREATE TABLE supplier_market_batches (
    batch_id text PRIMARY KEY,
    provider text NOT NULL,
    endpoint text NOT NULL,
    scope jsonb NOT NULL,
    collected_at timestamptz NOT NULL,
    source_clock timestamptz,
    row_count integer NOT NULL CHECK (row_count > 0),
    body jsonb NOT NULL
);
CREATE TABLE supplier_etf_quote_observations (
    provider text NOT NULL,
    code text NOT NULL,
    observed_at timestamptz NOT NULL,
    price_cny numeric NOT NULL CHECK (price_cny > 0),
    pre_close_cny numeric CHECK (pre_close_cny > 0),
    volume_shares numeric CHECK (volume_shares >= 0),
    amount_cny numeric CHECK (amount_cny >= 0),
    batch_id text NOT NULL REFERENCES supplier_market_batches,
    PRIMARY KEY (provider,code,observed_at)
);
