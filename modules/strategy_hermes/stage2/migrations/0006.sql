ALTER TABLE supplier_batches DROP CONSTRAINT supplier_batches_api_check;
ALTER TABLE supplier_batches ADD CONSTRAINT supplier_batches_api_check
    CHECK (api IN ('trade_cal','etf_basic','index_weight','daily','stock_basic'));
CREATE TABLE supplier_stock_catalog (
    code text PRIMARY KEY,
    symbol text NOT NULL, exchange text NOT NULL, market text,
    batch_id text NOT NULL REFERENCES supplier_batches
);
