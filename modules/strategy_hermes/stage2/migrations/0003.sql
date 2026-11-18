ALTER TABLE supplier_batches DROP CONSTRAINT supplier_batches_api_check;
ALTER TABLE supplier_batches ADD CONSTRAINT supplier_batches_api_check
    CHECK (api IN ('trade_cal','etf_basic','index_weight','daily'));

CREATE TABLE supplier_daily_bars (
    code text NOT NULL, trade_date date NOT NULL,
    open numeric NOT NULL CHECK (open > 0), high numeric NOT NULL CHECK (high > 0),
    low numeric NOT NULL CHECK (low > 0), close numeric NOT NULL CHECK (close > 0),
    pre_close numeric NOT NULL CHECK (pre_close > 0),
    volume_shares numeric NOT NULL CHECK (volume_shares >= 0),
    amount_cny numeric NOT NULL CHECK (amount_cny >= 0), return_ratio numeric NOT NULL,
    batch_id text NOT NULL REFERENCES supplier_batches,
    PRIMARY KEY (code,trade_date),
    CHECK (low <= least(open,close) AND high >= greatest(open,close))
);
