-- Coverage is durable derived state, committed with the underlying facts.
-- Counts describe stored observations, not whole-market completeness.
CREATE TABLE data_coverage (
    scope text NOT NULL,
    dataset text NOT NULL,
    business_date date NOT NULL,
    row_count bigint NOT NULL CHECK (row_count >= 0),
    ready_count bigint CHECK (ready_count >= 0 AND ready_count <= row_count),
    source_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (scope,dataset,business_date)
);
CREATE INDEX data_coverage_month ON data_coverage(scope,business_date,dataset);

-- The retained explicit-code collector publishes these already-normalized JSON
-- shapes. Project the same values (no unit/formula conversion) so historical
-- and new single-code publications share one fact key with bulk publications.
CREATE FUNCTION legacy_lr_feature_rows(api text, payload jsonb)
RETURNS TABLE(code text,trade_date date,body jsonb) LANGUAGE sql IMMUTABLE
SET search_path = stock_v4_stage2, pg_catalog AS $$
    SELECT payload->>'code', item.observation_date::date, item.body
    FROM (
        SELECT key observation_date, CASE WHEN api='adj_factor' THEN jsonb_build_object('adj_factor',value)
            ELSE value END body
        FROM jsonb_each(CASE
            WHEN api='adj_factor' AND jsonb_typeof(payload->'factors')='object' THEN payload->'factors'
            WHEN api IN ('daily_basic','cyq_perf') AND jsonb_typeof(payload->'features')='object' THEN payload->'features'
            ELSE '{}'::jsonb END)
        UNION ALL
        SELECT value->>'trade_date', value
        FROM jsonb_array_elements(CASE WHEN api='weekly' AND jsonb_typeof(payload->'weekly')='array'
            THEN payload->'weekly' ELSE '[]'::jsonb END)
        WHERE value->'complete'='true'::jsonb
    ) item
    WHERE payload->>'code' ~ '^[0-9]{6}\.(SH|SZ|BJ)$'
      AND jsonb_typeof(payload->'raw')='array'
      AND item.observation_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$';
$$;

INSERT INTO supplier_lr_features AS facts(api,code,trade_date,body,batch_id)
SELECT DISTINCT ON (b.endpoint,r.code,r.trade_date) b.endpoint,r.code,r.trade_date,r.body,b.batch_id
FROM supplier_market_batches b CROSS JOIN LATERAL legacy_lr_feature_rows(b.endpoint,b.body) r
WHERE b.provider='tushare' AND b.scope->>'ts_code'=r.code
ORDER BY b.endpoint,r.code,r.trade_date,b.collected_at DESC,b.batch_id DESC
ON CONFLICT(api,code,trade_date) DO UPDATE SET body=excluded.body,batch_id=excluded.batch_id
WHERE (SELECT collected_at FROM supplier_market_batches WHERE batch_id=facts.batch_id)
    < (SELECT collected_at FROM supplier_market_batches WHERE batch_id=excluded.batch_id);

-- One-time initialization from published normalized facts. Raw provider pages
-- and unfinished job receipts are deliberately outside this projection.
INSERT INTO data_coverage(scope,dataset,business_date,row_count,source_at)
SELECT '', 'daily', r.trade_date, count(*), max(b.collected_at)
FROM supplier_daily_bars r JOIN supplier_batches b USING(batch_id)
GROUP BY r.trade_date;
INSERT INTO data_coverage(scope,dataset,business_date,row_count,source_at)
SELECT '', r.api, r.trade_date, count(*), max(b.collected_at)
FROM supplier_lr_features r JOIN supplier_market_batches b USING(batch_id)
WHERE r.api IN ('adj_factor','daily_basic','cyq_perf','weekly')
GROUP BY r.api,r.trade_date;
INSERT INTO data_coverage(scope,dataset,business_date,row_count,source_at)
SELECT '', r.dataset, r.business_date, count(*), max(r.observed_at)
FROM supplier_reference_rows r
WHERE r.dataset IN ('stock_basic','trade_cal','index_member_all','dc_member','share_float','fina_indicator')
GROUP BY r.dataset,r.business_date;
INSERT INTO data_coverage(scope,dataset,business_date,row_count,ready_count,source_at)
SELECT ns, 'daily_assessment', trade_date, count(*), count(*) FILTER (WHERE status='READY'), max(published_at)
FROM left_right_assessments GROUP BY ns,trade_date;

-- A successful empty unlock range is evidence too; an absent/failed request is
-- not. Existing positive facts take precedence over an empty provider reply.
INSERT INTO data_coverage(scope,dataset,business_date,row_count,source_at,details)
SELECT '', 'share_float', (scope->>'trade_date')::date, 0, max(collected_at), '{"valid_empty":true}'
FROM supplier_market_batches
WHERE provider='tushare' AND endpoint='share_float'
  AND scope->>'schema_version'='tushare-reference-restored-v1'
  AND body->'receipt'->>'status'='EMPTY' AND row_count=0
GROUP BY scope->>'trade_date'
ON CONFLICT (scope,dataset,business_date) DO NOTHING;

CREATE FUNCTION maintain_data_coverage() RETURNS trigger LANGUAGE plpgsql
SET search_path = stock_v4_stage2, pg_catalog AS $$
DECLARE
    projection text;
    changes text;
    changed record;
BEGIN
    -- Each projection only reads the statement's transition relation. Both
    -- sides of an UPDATE participate, including moved dates/namespaces.
    CASE TG_TABLE_NAME
    WHEN 'supplier_daily_bars' THEN
        projection := 'SELECT ''''::text scope, ''daily''::text dataset, r.trade_date business_date,
            count(*) n, NULL::bigint ready, max(b.collected_at) source_at
            FROM %I r JOIN supplier_batches b USING(batch_id) GROUP BY r.trade_date';
    WHEN 'supplier_lr_features' THEN
        projection := 'SELECT ''''::text scope, r.api dataset, r.trade_date business_date,
            count(*) n, NULL::bigint ready, max(b.collected_at) source_at
            FROM %I r JOIN supplier_market_batches b USING(batch_id)
            WHERE r.api IN (''adj_factor'',''daily_basic'',''cyq_perf'',''weekly'') GROUP BY r.api,r.trade_date';
    WHEN 'supplier_reference_rows' THEN
        projection := 'SELECT ''''::text scope, r.dataset, r.business_date,
            count(*) n, NULL::bigint ready, max(r.observed_at) source_at
            FROM %I r WHERE r.dataset IN (''stock_basic'',''trade_cal'',''index_member_all'',''dc_member'',''share_float'',''fina_indicator'')
            GROUP BY r.dataset,r.business_date';
    WHEN 'left_right_assessments' THEN
        projection := 'SELECT r.ns scope, ''daily_assessment''::text dataset, r.trade_date business_date,
            count(*) n, count(*) FILTER (WHERE r.status=''READY'') ready, max(r.published_at) source_at
            FROM %I r GROUP BY r.ns,r.trade_date';
    ELSE
        RAISE EXCEPTION 'unsupported coverage source';
    END CASE;
    changes := '';
    IF TG_OP IN ('INSERT','UPDATE') THEN
        changes := format(projection, 'inserted');
    END IF;
    IF TG_OP IN ('DELETE','UPDATE') THEN
        IF changes<>'' THEN changes := changes || ' UNION ALL '; END IF;
        changes := changes || 'SELECT scope,dataset,business_date,-n n,-ready ready,NULL::timestamptz source_at FROM ('
            || format(projection, 'deleted') || ') previous';
    END IF;
    FOR changed IN EXECUTE 'SELECT scope,dataset,business_date,sum(n)::bigint n,sum(ready)::bigint ready,
          max(source_at) source_at FROM (' || changes || ') incoming
          GROUP BY scope,dataset,business_date ORDER BY scope,dataset,business_date'
    LOOP
        INSERT INTO data_coverage AS coverage
            (scope,dataset,business_date,row_count,ready_count,source_at)
        VALUES (changed.scope,changed.dataset,changed.business_date,greatest(changed.n,0),
            CASE WHEN changed.ready IS NOT NULL THEN least(greatest(changed.ready,0),greatest(changed.n,0)) END,
            changed.source_at)
        ON CONFLICT(scope,dataset,business_date) DO UPDATE SET
            row_count=coverage.row_count+changed.n,
            ready_count=CASE WHEN changed.ready IS NOT NULL THEN coalesce(coverage.ready_count,0)+changed.ready END,
            -- Retain the actual latest observation incorporated for this day;
            -- a calendar GET never changes this timestamp.
            source_at=CASE WHEN coverage.row_count+changed.n=0 THEN NULL
                ELSE greatest(coverage.source_at,changed.source_at) END,
            updated_at=clock_timestamp(),
            details=CASE WHEN coverage.row_count+changed.n>0 THEN '{}'::jsonb ELSE coverage.details END
        WHERE changed.n<>0 OR coalesce(changed.ready,0)<>0
            OR changed.source_at>coverage.source_at
            OR coverage.source_at IS NULL AND changed.source_at IS NOT NULL;
    END LOOP;
    RETURN NULL;
END;
$$;

DO $$
DECLARE relation text;
BEGIN
    FOREACH relation IN ARRAY ARRAY['supplier_daily_bars','supplier_lr_features','supplier_reference_rows','left_right_assessments']
    LOOP
        EXECUTE format('CREATE TRIGGER coverage_insert AFTER INSERT ON %I
            REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION maintain_data_coverage()',relation);
        EXECUTE format('CREATE TRIGGER coverage_update AFTER UPDATE ON %I
            REFERENCING OLD TABLE AS deleted NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION maintain_data_coverage()',relation);
        EXECUTE format('CREATE TRIGGER coverage_delete AFTER DELETE ON %I
            REFERENCING OLD TABLE AS deleted FOR EACH STATEMENT EXECUTE FUNCTION maintain_data_coverage()',relation);
    END LOOP;
END;
$$;

CREATE FUNCTION maintain_empty_data_coverage() RETURNS trigger LANGUAGE plpgsql
SET search_path = stock_v4_stage2, pg_catalog AS $$
BEGIN
    INSERT INTO data_coverage AS coverage(scope,dataset,business_date,row_count,source_at,details)
    SELECT '', 'share_float', (scope->>'trade_date')::date, 0, max(collected_at), '{"valid_empty":true}'
    FROM inserted
    WHERE provider='tushare' AND endpoint='share_float'
      AND scope->>'schema_version'='tushare-reference-restored-v1'
      AND body->'receipt'->>'status'='EMPTY' AND row_count=0
    GROUP BY scope->>'trade_date'
    ON CONFLICT(scope,dataset,business_date) DO UPDATE SET
        source_at=greatest(coverage.source_at,excluded.source_at),
        updated_at=clock_timestamp(),details=excluded.details
    WHERE coverage.row_count=0 AND
        (excluded.source_at>coverage.source_at OR coverage.source_at IS NULL OR coverage.details<>excluded.details);
    RETURN NULL;
END;
$$;
CREATE TRIGGER coverage_empty_insert AFTER INSERT ON supplier_market_batches
    REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION maintain_empty_data_coverage();

CREATE FUNCTION project_legacy_lr_features() RETURNS trigger LANGUAGE plpgsql
SET search_path = stock_v4_stage2, pg_catalog AS $$
BEGIN
    INSERT INTO supplier_lr_features AS facts(api,code,trade_date,body,batch_id)
    SELECT DISTINCT ON (b.endpoint,r.code,r.trade_date) b.endpoint,r.code,r.trade_date,r.body,b.batch_id
    FROM inserted b CROSS JOIN LATERAL legacy_lr_feature_rows(b.endpoint,b.body) r
    WHERE b.provider='tushare' AND b.scope->>'ts_code'=r.code
    ORDER BY b.endpoint,r.code,r.trade_date,b.collected_at DESC,b.batch_id DESC
    ON CONFLICT(api,code,trade_date) DO UPDATE SET body=excluded.body,batch_id=excluded.batch_id
    -- Existing typed values win ties; an older legacy observation cannot
    -- displace a newer typed fact. Its receipt still remains available.
    WHERE (SELECT collected_at FROM supplier_market_batches WHERE batch_id=facts.batch_id)
        < (SELECT collected_at FROM supplier_market_batches WHERE batch_id=excluded.batch_id);
    RETURN NULL;
END;
$$;
CREATE TRIGGER legacy_lr_features_insert AFTER INSERT ON supplier_market_batches
    REFERENCING NEW TABLE AS inserted FOR EACH STATEMENT EXECUTE FUNCTION project_legacy_lr_features();
