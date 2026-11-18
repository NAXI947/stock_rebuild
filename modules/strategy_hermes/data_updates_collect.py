"""The retained Tushare daily/weekly/reference/month/year update contracts.

Collection is worker-owned, every physical page is budgeted, and successful
pages retain their original collection time across a later publication failure.
"""
from calendar import monthrange
from datetime import date, datetime, timedelta
import json
import re
from time import sleep
from urllib.parse import urlsplit

import psycopg

from .bulk_eod_collect import BulkTushare, STOCK_CODE, _cached_page, _save_page, collect_day
from .ledger import encode, fingerprint
from .simulator import CHINA, decimal
from .stage2.http_json import post_json

SCHEMA = "tushare-reference-restored-v1"
# Reference archives include retired alphanumeric identifiers (e.g. T600018.SH).
# Keep those identities verbatim; price collection still uses STOCK_CODE.
REFERENCE_CODE = re.compile(r"[A-Z0-9]{1,12}\.(?:SH|SZ|BJ)\Z")
FIELDS = {
    "stock_basic": ("ts_code", "symbol", "name", "industry", "market", "list_date", "delist_date", "list_status"),
    "trade_cal": ("exchange", "cal_date", "is_open"),
    "index_member_all": ("l1_name", "l2_name", "ts_code", "name", "in_date", "out_date"),
    "dc_member": ("trade_date", "ts_code", "con_code", "name"),
    "share_float": ("ts_code", "ann_date", "float_date", "float_share", "float_ratio", "holder_name", "share_type"),
    "fina_indicator_vip": ("ts_code", "ann_date", "end_date", "roe", "debt_to_assets"),
}
EOD_FIELDS = {
    "cyq_perf": ("ts_code", "trade_date", "winner_rate", "cost_50pct", "cost_85pct"),
    "weekly": ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"),
}


def object_value(value):
    return json.loads(value) if isinstance(value, str) else value


def day_value(value, optional=False):
    if optional and value in (None, ""):
        return None
    return date.fromisoformat(str(value))


class UpdateTushare(BulkTushare):
    completed_run_ttl_seconds = 21600

    def fields(self, api):
        return FIELDS.get(api) or EOD_FIELDS.get(api) or super().fields(api)

    def endpoint(self, api):
        if api not in FIELDS:
            return super().endpoint(api)
        override = (self.config.get("api_overrides") or {}).get(api) or {}
        token = override.get("token", self.config.get("token"))
        endpoint = override.get("base_url", self.config.get("base_url", "https://api.tushare.pro"))
        uri = urlsplit(endpoint)
        timeout = min(30, int(override.get("timeout_seconds", self.config.get("timeout_seconds", 15))))
        if (not isinstance(token, str) or not token.strip() or timeout <= 0 or uri.scheme not in ("http", "https")
                or not uri.hostname or uri.username or uri.password or uri.fragment):
            raise ValueError("INVALID_PROVIDER_CONFIGURATION")
        return endpoint, token.strip(), timeout

    def fetch_reference_page(self, api, scope):
        endpoint, token, timeout = self.endpoint(api)
        response = post_json(endpoint, dict(api_name=api, token=token, params=scope,
                             fields=",".join(self.fields(api))), timeout=timeout, protocols="=http,https")
        if type(response.get("code")) is not int or response["code"] != 0:
            raise ValueError("SOURCE_BUSINESS_ERROR")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("SOURCE_SCHEMA_INVALID")
        fields, rows = data.get("fields"), data.get("items")
        required = set(self.fields(api)) - ({"market"} if api == "stock_basic" else set())
        if (not isinstance(fields, list) or not all(isinstance(x, str) for x in fields)
                or len(set(fields)) != len(fields) or not required.issubset(fields)
                or not isinstance(rows, list) or len(rows) > scope["limit"]
                or any(not isinstance(row, list) or len(row) != len(fields) for row in rows)):
            raise ValueError("SOURCE_SCHEMA_INVALID")
        return dict(fields=fields, rows=rows, collected_at=datetime.now(CHINA).isoformat())


def reference_pages(store, client, api, scope, request, checkpoint):
    size = 2000 if api == "index_member_all" else 4000
    source = client.identity(api)
    result, seen, columns, observed = [], set(), None, []
    for page_number in range(100):
        checkpoint()
        params = dict(scope, limit=size, offset=page_number*size)
        key = fingerprint([source, api, client.fields(api), params, SCHEMA])
        page = _cached_page(store, key)
        if page is None:
            def fetch():
                value = client.fetch_reference_page(api, params)
                return _save_page(store, key, source, api, params, value)
            receipt = request("tushare:"+api, dict(params, source_key=source, schema_version=SCHEMA), fetch)
            if receipt["status"] != "SAVED":
                raise ValueError("SOURCE_REQUEST_FAILED")
            page = _cached_page(store, key)
            if page is None:
                raise ValueError("SOURCE_PAGE_UNAVAILABLE")
        if columns is not None and page["fields"] != columns:
            raise ValueError("SOURCE_FIELDS_CHANGED")
        columns = page["fields"]
        rows = [dict(zip(columns, row, strict=True)) for row in page["rows"]]
        digest = fingerprint(rows)
        if rows and digest in seen:
            raise ValueError("SOURCE_PAGINATION_STALLED")
        seen.add(digest)
        result.extend(rows)
        observed.append(datetime.fromisoformat(page["collected_at"]))
        if len(rows) < size:
            return result, max(observed)
        sleep(.15)  # Retained reference-provider pacing between full pages.
    raise ValueError("SOURCE_PAGE_LIMIT_REACHED")


def reference_scopes(dataset, business_date, body):
    if dataset == "stock_basic":
        return [(dataset, dict(list_status=status)) for status in ("L", "D", "P")]
    if dataset == "trade_cal":
        year = datetime.fromisoformat(body["submitted_at"]).astimezone(CHINA).year
        return [(dataset, dict(exchange=exchange, start_date=f"{year-1}0101", end_date=f"{year+1}1231"))
                for exchange in ("SSE", "SZSE")]
    if dataset == "index_member_all":
        return [(dataset, dict(is_new="Y"))]
    if dataset == "dc_member":
        return [(dataset, dict(trade_date=business_date.strftime("%Y%m%d")))]
    if dataset == "share_float":
        year, month = map(int, body["month"].split("-"))
        return [(dataset, dict(start_date=f"{year:04d}{month:02d}01", end_date=f"{year:04d}{month:02d}{monthrange(year,month)[1]:02d}"))]
    if dataset == "fina_indicator":
        return [("fina_indicator_vip", dict(period=business_date.strftime("%Y%m%d")))]
    raise ValueError("UNSUPPORTED_REFERENCE_DATASET")


def normalize_reference(dataset, raw, business_date, body, observed):
    selected, scoped_raw, details, upstream = [], [], {}, len(raw)
    for original in raw:
        row = dict(original)
        # Retained providers can return rows beyond their requested period.
        # Apply the original scope filter before validating or storing a row.
        if dataset == "dc_member" and day_value(row["trade_date"]) != business_date:
            continue
        if dataset == "fina_indicator" and day_value(row["end_date"]) != business_date:
            continue
        if dataset == "share_float":
            scopes = reference_scopes(dataset,business_date,body)[0][1]
            if not day_value(scopes["start_date"]) <= day_value(row["float_date"]) <= day_value(scopes["end_date"]):
                continue
        if dataset == "trade_cal":
            exchange, when, flag = row["exchange"], day_value(row["cal_date"]), str(row["is_open"]).lower()
            year = datetime.fromisoformat(body["submitted_at"]).astimezone(CHINA).year
            if exchange not in ("SSE", "SZSE") or not date(year-1,1,1) <= when <= date(year+1,12,31) or flag not in ("0","1","true","false","1.0","0.0"):
                raise ValueError("SOURCE_CALENDAR_SCOPE_INVALID")
            row.update(cal_date=when.isoformat(), is_open=flag in ("1","true","1.0"))
            entity = exchange+"|"+when.isoformat()
        else:
            code = row.get("con_code") if dataset == "dc_member" else row.get("ts_code")
            code_rule = REFERENCE_CODE if dataset in ("stock_basic", "index_member_all") else STOCK_CODE
            if not isinstance(code, str) or not code_rule.fullmatch(code):
                raise ValueError("SOURCE_STOCK_CODE_MISSING")
            entity = code
            if dataset == "stock_basic":
                if not isinstance(row.get("name"), str) or not row["name"].strip():
                    raise ValueError("SOURCE_STOCK_NAME_MISSING")
                row["name"] = row["name"].strip()
                if row.get("list_status") not in ("L", "D", "P"):
                    raise ValueError("SOURCE_STOCK_STATUS_INVALID")
                for key in ("list_date", "delist_date"):
                    value = day_value(row.get(key), optional=True)
                    row[key] = value.isoformat() if value else None
                row["exchange"] = code.rsplit(".",1)[-1]
                market = row.get("market")
                row["market"] = market.strip() if isinstance(market, str) and market.strip() else None
            elif dataset == "dc_member":
                row["trade_date"] = business_date.isoformat()
                entity = str(row["ts_code"])+"|"+code
            elif dataset == "index_member_all":
                for key in ("in_date", "out_date"):
                    value = day_value(row.get(key), optional=True)
                    row[key] = value.isoformat() if value else None
            elif dataset == "share_float":
                when = day_value(row["float_date"])
                row["float_date"] = when.isoformat()
                ratio = row.get("float_ratio")
                row["float_ratio"] = str(decimal(ratio)/100) if ratio is not None else None
                entity = "|".join((code, str(when), str(row.get("holder_name") or ""), str(row.get("share_type") or "")))
            else:
                announced = day_value(row["ann_date"])
                if announced > observed.astimezone(CHINA).date():
                    raise ValueError("SOURCE_FUTURE_ANNOUNCEMENT")
                row.update(end_date=business_date.isoformat(), ann_date=announced.isoformat(), ratio_unit="percent")
                entity = "|".join((code, business_date.isoformat(), announced.isoformat()))
        selected.append(dict(entity_key=entity, body=row))
        scoped_raw.append(original)
    if not selected and dataset != "share_float":
        raise ValueError("SOURCE_EMPTY_REQUIRED_DATASET")
    if dataset == "trade_cal":
        dates = [row["body"]["cal_date"] for row in selected]
        details.update(coverage_start=min(dates), coverage_end=max(dates))
    if dataset in ("index_member_all", "dc_member"):
        details["snapshot_date"] = business_date.isoformat()
    if dataset == "share_float":
        scope = reference_scopes(dataset, business_date, body)[0][1]
        details.update(range_start=day_value(scope["start_date"]).isoformat(), range_end=day_value(scope["end_date"]).isoformat())
    if dataset == "fina_indicator":
        details["report_year"] = body["year"]
    if len(selected) != upstream:
        details.update(upstream_rows=upstream, excluded_out_of_scope=upstream-len(selected))
    return selected, details, scoped_raw


def publish_reference(store, dataset, day, run_key, raw, rows, observed, details):
    batch = fingerprint([SCHEMA, dataset, day.isoformat(), run_key, raw, observed.isoformat()])
    status = "OK" if rows else "EMPTY"
    receipt = dict(status=status, row_count=len(rows), batch_id=batch, observed_at=observed.isoformat(), details=details)
    scope = dict(run_key=run_key, trade_date=day.isoformat(), schema_version=SCHEMA)
    with store._connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("reference:"+dataset,))
        conn.execute("""INSERT INTO supplier_market_batches(batch_id,provider,endpoint,scope,collected_at,source_clock,row_count,body)
            VALUES(%s,'tushare',%s,%s,%s,NULL,%s,%s) ON CONFLICT DO NOTHING""",
            (batch,dataset,encode(scope),observed,len(rows),encode(dict(raw=raw,rows=rows,receipt=receipt))))
        if dataset in ("stock_basic", "trade_cal"):
            conn.execute("""INSERT INTO supplier_batches(batch_id,api,requested_scope,collected_at,row_count,raw_rows,normalized)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (batch,dataset,encode(scope),observed,len(rows),encode(raw),encode(rows)))
        # COPY into a transaction-local staging table bounds statement count for full-market lists.
        conn.execute("CREATE TEMP TABLE reference_rows (LIKE supplier_reference_rows INCLUDING DEFAULTS) ON COMMIT DROP")
        conn.execute("ALTER TABLE reference_rows ADD COLUMN row_order integer")
        with conn.cursor().copy("COPY reference_rows(dataset,entity_key,business_date,observed_at,body,batch_id,row_order) FROM STDIN") as copy:
            for ordinal,row in enumerate(rows):
                copy.write_row((dataset,row["entity_key"],day,observed,encode(row["body"]),batch,ordinal))
        conn.execute("""INSERT INTO supplier_reference_rows(dataset,entity_key,business_date,observed_at,body,batch_id)
            SELECT DISTINCT ON(dataset,entity_key,business_date) dataset,entity_key,business_date,observed_at,body,batch_id
            FROM reference_rows ORDER BY dataset,entity_key,business_date,row_order DESC
            ON CONFLICT(dataset,entity_key,business_date) DO UPDATE SET observed_at=excluded.observed_at,
                body=excluded.body,batch_id=excluded.batch_id
            WHERE supplier_reference_rows.observed_at<=excluded.observed_at""")
        if dataset == "trade_cal":
            conn.execute("""INSERT INTO supplier_trade_calendar(exchange,cal_date,is_open,batch_id)
                SELECT DISTINCT ON(body->>'exchange',body->>'cal_date') body->>'exchange',(body->>'cal_date')::date,
                    (body->>'is_open')::boolean,batch_id FROM reference_rows
                ORDER BY body->>'exchange',body->>'cal_date',row_order DESC
                ON CONFLICT(exchange,cal_date) DO UPDATE SET is_open=excluded.is_open,batch_id=excluded.batch_id
                WHERE (SELECT collected_at FROM supplier_batches WHERE batch_id=supplier_trade_calendar.batch_id)<=%s""",(observed,))
        if dataset == "stock_basic":
            conn.execute("""INSERT INTO supplier_stock_catalog(code,symbol,exchange,market,batch_id)
                SELECT DISTINCT ON(body->>'ts_code') body->>'ts_code',body->>'symbol',
                    CASE body->>'exchange' WHEN 'SH' THEN 'SSE' WHEN 'SZ' THEN 'SZSE' ELSE 'BSE' END,
                    coalesce(nullif(btrim(body->>'market'),''),''),batch_id
                FROM reference_rows ORDER BY body->>'ts_code',row_order DESC
                ON CONFLICT(code) DO UPDATE SET symbol=excluded.symbol,exchange=excluded.exchange,
                    market=coalesce(nullif(excluded.market,''),supplier_stock_catalog.market),batch_id=excluded.batch_id
                WHERE (SELECT collected_at FROM supplier_batches WHERE batch_id=supplier_stock_catalog.batch_id)<=%s""",(observed,))
    return receipt


def collect_reference(store, client, dataset, day, body, request, checkpoint):
    scopes = reference_scopes(dataset, day, body)
    run_key = fingerprint([SCHEMA,dataset,day.isoformat(),[(client.identity(api),client.fields(api),scope) for api,scope in scopes]])
    checkpoint()
    with store._connect() as conn:
        existing = conn.execute("""SELECT body FROM supplier_market_batches WHERE provider='tushare' AND endpoint=%s
            AND scope->>'run_key'=%s AND collected_at>=clock_timestamp()-interval '6 hours'
            ORDER BY collected_at DESC LIMIT 1""", (dataset,run_key)).fetchone()
    if existing:
        return object_value(existing[0])["receipt"]
    raw, observed = [], []
    for api,scope in scopes:
        values, stamp = reference_pages(store,client,api,scope,request,checkpoint)
        raw.extend(values);observed.append(stamp)
    available = max(observed)
    normalized,details,scoped_raw = normalize_reference(dataset,raw,day,body,available)
    checkpoint()
    return publish_reference(store,dataset,day,run_key,scoped_raw,normalized,available,details)


def planned_units(body):
    mode = body["mode"]
    latest = body["latest_business_date"]
    if mode == "daily":
        return [(dataset,day) for day in body["trade_dates"] for dataset in ("daily","adj_factor","daily_basic","cyq_perf")]
    if mode == "weekly":
        return [("weekly",day) for day in body["weekly_dates"]]
    if mode == "basic":
        return [(dataset,latest) for dataset in ("stock_basic","trade_cal","index_member_all")]
    if mode == "monthly":
        snapshot = body.get("snapshot_date") or body["trade_dates"][-1]
        return [("dc_member",snapshot),("share_float",body["month"]+"-01")]
    if mode == "yearly":
        return [("fina_indicator",f'{body["year"]:04d}-12-31')]
    raise ValueError("UNSUPPORTED_UPDATE_MODE")


def execute_update(store, body, config, captures, request, checkpoint, record, progress=None):
    units = planned_units(body)
    client = UpdateTushare(config)
    completed, failed, recompute = 0, 0, None
    for dataset, day in units:
        checkpoint()
        record(dataset,day,dict(status="RUNNING"))
        try:
            if dataset in ("daily","adj_factor","daily_basic","cyq_perf","weekly"):
                when = date.fromisoformat(day)
                # The planner freezes the already validated calendar-file coverage
                # for a new database; committed exchange observations take priority.
                calendar = dict(body.get("calendar") or {})
                with store._connect() as conn:
                    calendar.update({str(d):opened for d,opened in conn.execute(
                        "SELECT cal_date,is_open FROM supplier_trade_calendar WHERE exchange='SSE' AND cal_date BETWEEN %s AND %s",
                        (when-timedelta(days=7),when+timedelta(days=7))).fetchall()})
                collected = collect_day(store,client,dataset,when,calendar,request,checkpoint)
                table = "supplier_batches" if dataset == "daily" else "supplier_market_batches"
                with store._connect() as conn:
                    observed = conn.execute("SELECT collected_at FROM "+table+" WHERE batch_id=%s",(collected["batch"],)).fetchone()[0]
                receipt = dict(status="OK",row_count=collected["rows"],batch_id=collected["batch"],observed_at=observed.isoformat(),details={})
            else:
                receipt = collect_reference(store,client,dataset,date.fromisoformat(day),body,request,checkpoint)
            if dataset == "fina_indicator":
                with store._connect() as conn:
                    latest = conn.execute("SELECT max(trade_date) FROM supplier_daily_bars").fetchone()[0]
                recompute = latest.isoformat() if latest else None
            checkpoint()
            record(dataset,day,receipt)
            completed += 1
        except (ValueError,TypeError,KeyError,OSError,psycopg.Error) as exc:
            failed += 1
            known = str(exc) if isinstance(exc,ValueError) and str(exc).startswith("SOURCE_") else type(exc).__name__
            record(dataset,day,dict(status="FAILED",error_code=known))
        if progress:
            progress(dataset=dataset,trade_date=day,completed_units=completed+failed,total_units=len(units))
    return dict(completed=completed,failed=failed,recompute_date=recompute)
