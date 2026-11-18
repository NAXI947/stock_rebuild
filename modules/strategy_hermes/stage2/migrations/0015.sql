-- Immutable maintenance versions. Failed components retain their previous data.
CREATE TABLE maintained_mappings (
    ns text NOT NULL,
    version text NOT NULL,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    body jsonb NOT NULL,
    summary jsonb NOT NULL,
    PRIMARY KEY (ns,version)
);
CREATE INDEX maintained_mappings_available ON maintained_mappings(ns,available_at DESC);
