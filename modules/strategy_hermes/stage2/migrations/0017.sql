-- Operator-reviewed official documents are durable evidence, not temporary files.
-- Only the existing maintenance worker may verify and activate these requests.
CREATE TABLE product_verification_documents (
    ns text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content bytea NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 8388608),
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ns,sha256)
);

CREATE TABLE product_verification_requests (
    ns text NOT NULL,
    request_id text NOT NULL,
    etf_code text NOT NULL,
    expected_mapping_version text NOT NULL,
    state text NOT NULL CHECK (state IN
        ('PENDING','WAITING_DOCUMENTS','VERIFIED','REVIEW_REQUIRED','NEEDS_REBASE','SUPERSEDED','PUBLISHED')),
    dossier jsonb NOT NULL,
    imported_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempts integer NOT NULL DEFAULT 0,
    result jsonb NOT NULL DEFAULT '{}',
    published_version text,
    published_at timestamptz,
    PRIMARY KEY (ns,request_id),
    FOREIGN KEY (ns,published_version) REFERENCES maintained_mappings(ns,version)
);
CREATE UNIQUE INDEX product_verification_one_active_product
    ON product_verification_requests(ns,etf_code)
    WHERE state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED');
CREATE INDEX product_verification_pending
    ON product_verification_requests(ns,created_at)
    WHERE state IN ('PENDING','WAITING_DOCUMENTS','VERIFIED');
