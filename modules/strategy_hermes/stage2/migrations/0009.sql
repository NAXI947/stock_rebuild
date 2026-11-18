CREATE TABLE actual_trades (
    ns text NOT NULL,
    request_id text NOT NULL,
    executed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    body jsonb NOT NULL,
    PRIMARY KEY(ns,request_id)
);
CREATE INDEX actual_trades_time ON actual_trades(ns,executed_at);
