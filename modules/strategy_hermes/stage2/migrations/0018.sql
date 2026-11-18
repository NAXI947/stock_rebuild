-- Durable bounded material targets; importing/collecting evidence never approves a rule.
CREATE TABLE product_verification_targets (
    ns text NOT NULL,
    target_id text NOT NULL,
    etf_code text NOT NULL,
    mapping_version text NOT NULL,
    product_basis jsonb NOT NULL,
    sector_ids jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('NEEDS_MATERIALS','AWAITING_REVIEW','QUEUED','REVIEW_REQUIRED','PUBLISHED')),
    documents jsonb NOT NULL DEFAULT '{}',
    reason text,
    request_id text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY(ns,target_id)
);
CREATE INDEX product_verification_targets_code ON product_verification_targets(ns,etf_code,created_at);
