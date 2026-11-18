CREATE SCHEMA IF NOT EXISTS stock_v4_stage2;
SET LOCAL search_path TO stock_v4_stage2, pg_catalog;

CREATE TABLE IF NOT EXISTS runs (
    ns text PRIMARY KEY, config text NOT NULL, watermark text, origin text NOT NULL
        CHECK (origin IN ('FROZEN','LIVE'))
);
CREATE TABLE IF NOT EXISTS rule_versions (
    ns text REFERENCES runs, version text NOT NULL, snapshot text NOT NULL,
    PRIMARY KEY (ns, version)
);
CREATE TABLE IF NOT EXISTS events (
    ns text REFERENCES runs, kind text NOT NULL CHECK (kind IN ('report','quote')),
    id text NOT NULL, at text NOT NULL, fingerprint text NOT NULL, payload jsonb NOT NULL,
    PRIMARY KEY (ns, kind, id)
);
CREATE INDEX IF NOT EXISTS events_chronology ON events (ns, kind, at, id);
CREATE TABLE IF NOT EXISTS cycles (
    ns text REFERENCES runs, id text NOT NULL, etf text NOT NULL, opened_at text NOT NULL,
    status text NOT NULL CHECK (status IN ('WAIT_BUY','OPEN','WAIT_EXIT','CLOSED','CANCELLED')),
    body jsonb NOT NULL, PRIMARY KEY (ns, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_cycle ON cycles(ns, etf)
    WHERE status IN ('WAIT_BUY','OPEN','WAIT_EXIT');
CREATE TABLE IF NOT EXISTS rotation (
    ns text REFERENCES runs, sector_id text NOT NULL, body jsonb NOT NULL,
    PRIMARY KEY (ns, sector_id)
);
CREATE TABLE IF NOT EXISTS latest (
    ns text REFERENCES runs, etf text NOT NULL, quote jsonb NOT NULL,
    PRIMARY KEY (ns, etf)
);

CREATE TABLE IF NOT EXISTS simulation_orders (
    ns text NOT NULL, cycle_id text NOT NULL, order_id text NOT NULL,
    action text NOT NULL CHECK (action IN ('BUY','REDUCE','EXIT')),
    requested_at timestamptz NOT NULL, status text NOT NULL,
    target_quantity bigint CHECK (target_quantity >= 0),
    remaining_quantity bigint CHECK (remaining_quantity >= 0),
    evidence jsonb NOT NULL, PRIMARY KEY (ns, cycle_id, order_id),
    FOREIGN KEY (ns, cycle_id) REFERENCES cycles,
    CHECK (remaining_quantity <= target_quantity)
);
CREATE TABLE IF NOT EXISTS simulation_fills (
    ns text NOT NULL, cycle_id text NOT NULL, quote_id text NOT NULL, order_id text NOT NULL,
    filled_at timestamptz NOT NULL, quantity bigint NOT NULL CHECK (quantity > 0),
    price numeric NOT NULL CHECK (price > 0), amount numeric NOT NULL CHECK (amount >= 0),
    fees numeric NOT NULL CHECK (fees >= 0), evidence jsonb NOT NULL,
    PRIMARY KEY (ns, cycle_id, quote_id),
    FOREIGN KEY (ns, cycle_id, order_id) REFERENCES simulation_orders
);
CREATE TABLE IF NOT EXISTS round_tasks (
    ns text NOT NULL, trade_date date NOT NULL, slot text NOT NULL,
    planned_at timestamptz NOT NULL, report_id text,
    state text NOT NULL CHECK (state IN ('RUNNING','PUBLISHED','FAILED')),
    lease_token uuid, lease_until timestamptz, attempts integer NOT NULL CHECK (attempts > 0),
    calls jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code text,
    PRIMARY KEY (ns, trade_date, slot)
);
CREATE TABLE IF NOT EXISTS outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ns text NOT NULL,
    report_id text NOT NULL, event_key text NOT NULL, target text NOT NULL,
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','SENDING','SENT','FAILED')),
    attempts integer NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL DEFAULT clock_timestamp(),
    lease_token uuid, lease_until timestamptz,
    delivered_at timestamptz, error_code text,
    UNIQUE (ns, event_key, target)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (ns, target, next_attempt, id)
    WHERE state IN ('PENDING','SENDING');

CREATE TABLE IF NOT EXISTS snapshot_state (
    ns text NOT NULL, dataset text NOT NULL, observed_at timestamptz NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0), body jsonb NOT NULL,
    PRIMARY KEY (ns, dataset)
);
