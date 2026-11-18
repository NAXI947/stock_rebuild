"""Whole-market EOD collection: bounded pages, durable page reuse, atomic publication.

The legacy provider contract is one trade_date per API, 3,000 rows per page,
at most ten pages. No stock list is expanded into individual HTTP requests.
"""
from datetime import date, datetime, timedelta
import json
import re
from time import monotonic
from urllib.parse import urlsplit

from .ledger import encode, fingerprint
from .simulator import CHINA
from .stage2.http_json import post_json
from .stage2.tushare_bootstrap import FIELDS, Tushare, calendar_rows, daily_rows, save as save_reference

PAGE_SIZE, MAX_PAGES, CACHE_SECONDS = 3000, 10, 21600
SCHEMA_VERSION = "lr-eod-bulk-v2"
STOCK_CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
DAILY_APIS = ("daily", "adj_factor", "daily_basic", "cyq_perf")
FEATURE_FIELDS = {
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": ("ts_code", "trade_date", "turnover_rate", "volume_ratio", "circ_mv"),
    "cyq_perf": ("ts_code", "trade_date", "cost_85pct"),
    "weekly": ("ts_code", "trade_date", "close", "vol", "amount"),
}


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class BulkTushare(Tushare):
    """Reuse the private configuration and HTTP transport without per-code budgets."""
    def endpoint(self, api):
        if api not in ("trade_cal", *DAILY_APIS, "weekly"):
            raise ValueError("unsupported bulk endpoint")
        override = (self.config.get("api_overrides") or {}).get(api) or {}
        token = override.get("token", self.config.get("token"))
        endpoint = override.get("base_url", self.config.get("base_url", "https://api.tushare.pro"))
        uri = urlsplit(endpoint)
        timeout = min(30, int(override.get("timeout_seconds", self.config.get("timeout_seconds", 15))))
        if (not isinstance(token, str) or not token.strip() or timeout <= 0
                or uri.scheme not in ("http", "https") or not uri.hostname
                or uri.username or uri.password or uri.fragment):
            raise ValueError("invalid bulk provider configuration")
        return endpoint, token.strip(), timeout

    def fields(self, api):
        return FIELDS[api] if api in FIELDS else FEATURE_FIELDS[api]

    def identity(self, api):
        endpoint, token, _ = self.endpoint(api)
        # Neither the URL nor the credential is persisted in clear text.
        return fingerprint([endpoint, token])

    def fetch_page(self, api, scope):
        endpoint, token, timeout = self.endpoint(api)
        fields = self.fields(api)
        response = post_json(endpoint, dict(api_name=api, token=token, params=scope,
                                          fields=",".join(fields)), timeout=timeout, protocols="=http,https")
        if type(response.get("code")) is not int or response["code"] != 0:
            raise ValueError("bulk provider status rejected")
        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("missing bulk provider data")
        columns, rows = data.get("fields"), data.get("items")
        if (not isinstance(columns, list) or not all(isinstance(item, str) for item in columns)
                or len(columns) != len(set(columns)) or not set(fields).issubset(columns)
                or not isinstance(rows, list) or len(rows) > PAGE_SIZE
                or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
            raise ValueError("invalid bulk provider fields or rows")
        return dict(fields=columns, rows=rows, collected_at=datetime.now(CHINA).isoformat())


def _cached_page(store, key):
    with store._connect() as conn:
        row = conn.execute("""SELECT fields,rows,collected_at FROM supplier_tushare_pages
            WHERE request_key=%s AND collected_at >= clock_timestamp() - interval '6 hours'""", (key,)).fetchone()
    return dict(fields=decoded(row[0]), rows=decoded(row[1]), collected_at=row[2].isoformat()) if row else None


def _save_page(store, key, source_key, api, scope, page):
    # An independent committed transaction survives a later page/publication failure.
    with store._connect() as conn, conn.transaction():
        conn.execute("""INSERT INTO supplier_tushare_pages
            (request_key,source_key,api,scope,fields,rows,collected_at) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(request_key) DO UPDATE SET fields=excluded.fields,rows=excluded.rows,
                collected_at=excluded.collected_at""",
            (key, source_key, api, encode(scope), encode(page["fields"]), encode(page["rows"]), page["collected_at"]))
    return dict(page_key=key, rows=len(page["rows"]), status="PAGE_SAVED")


def _completed_run(conn, api, run_key, max_age):
    if max_age is None:
        return conn.execute("SELECT batch_id,row_count FROM supplier_tushare_eod_runs WHERE run_key=%s",
                            (run_key,)).fetchone()
    table = "supplier_batches" if api == "daily" else "supplier_market_batches"
    return conn.execute("""SELECT r.batch_id,r.row_count FROM supplier_tushare_eod_runs r JOIN """+table+""" b
        ON b.batch_id=r.batch_id WHERE r.run_key=%s
        AND b.collected_at>=clock_timestamp()-%s*interval '1 second'""",(run_key,max_age)).fetchone()


def _validate_page(page, api, day, expected_fields, seen):
    fields, rows = page["fields"], page["rows"]
    if expected_fields is not None and fields != expected_fields:
        raise ValueError("bulk page fields changed")
    normalized, page_codes = [], set()
    for values in rows:
        row = dict(zip(fields, values, strict=True))
        code = row.get("ts_code")
        if (not isinstance(code, str) or not STOCK_CODE.fullmatch(code)
                or date.fromisoformat(row["trade_date"]) != day):
            raise ValueError("bulk page date or stock scope mismatch")
        if code in seen or code in page_codes:
            raise ValueError("bulk page repeats a stock")
        page_codes.add(code)
        normalized.append(row)
    return normalized, page_codes


def _normalized(api, raw, day, calendar):
    from .left_right_backfill_collectors import normalize_features
    rows = []
    for row in raw:
        code = row["ts_code"]
        if api == "daily":
            rows.extend(daily_rows([row], code, day, day))
            continue
        value = normalize_features(api, [row], code, day, day, calendar)
        if api == "adj_factor":
            body = {"adj_factor": value["factors"][day.isoformat()]}
        elif api == "weekly":
            body = value["weekly"][0]
            if not body["complete"]:
                raise ValueError("bulk weekly date is not a completed week")
        else:
            body = value["features"][day.isoformat()]
        rows.append(dict(code=code, trade_date=day.isoformat(), body=body))
    return rows


def _publish(store, api, day, run_key, source_key, raw, normalized, collected_at, *, max_age=None):
    scope = dict(trade_date=day.strftime("%Y%m%d"), universe="ALL", schema_version=SCHEMA_VERSION,
                 source_key=source_key)
    identity = [api, scope, raw]
    if max_age is not None:
        # A manual data refresh is a new observation even if the source values
        # are unchanged. Historical LR retains its permanent-content reuse.
        identity.append(collected_at.isoformat())
    batch = fingerprint(identity)
    with store._connect() as conn, conn.transaction():
        # The single worker normally serializes these writes; this also fences a retry.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (run_key,))
        existing = _completed_run(conn,api,run_key,max_age)
        if existing:
            return dict(batch=existing[0], rows=existing[1], status="ALREADY_STORED")
        if api == "daily":
            conn.execute("""INSERT INTO supplier_batches
                (batch_id,api,requested_scope,collected_at,row_count,raw_rows,normalized)
                VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (batch, api, encode(scope), collected_at, len(raw), encode(raw), encode(normalized)))
            conn.execute("CREATE TEMP TABLE bulk_daily_rows (LIKE supplier_daily_bars INCLUDING DEFAULTS) ON COMMIT DROP")
            with conn.cursor().copy("COPY bulk_daily_rows FROM STDIN") as copy:
                for row in normalized:
                    copy.write_row((*[row[key] for key in ("code", "trade_date", "open", "high", "low", "close",
                        "pre_close", "volume_shares", "amount_cny", "return_ratio")], batch))
            conn.execute("""INSERT INTO supplier_daily_bars SELECT * FROM bulk_daily_rows
                ON CONFLICT(code,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,
                low=excluded.low,close=excluded.close,pre_close=excluded.pre_close,
                volume_shares=excluded.volume_shares,amount_cny=excluded.amount_cny,
                return_ratio=excluded.return_ratio,batch_id=excluded.batch_id
                WHERE (SELECT collected_at FROM supplier_batches WHERE batch_id=supplier_daily_bars.batch_id) <= %s""",
                (collected_at,))
        else:
            source = day.isoformat() + "T15:00:00+08:00"
            body = dict(universe="ALL", trade_date=day.isoformat(), raw=raw, rows=normalized)
            conn.execute("""INSERT INTO supplier_market_batches
                (batch_id,provider,endpoint,scope,collected_at,source_clock,row_count,body)
                VALUES (%s,'tushare',%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (batch, api, encode(scope), collected_at, source, len(raw), encode(body)))
            conn.execute("CREATE TEMP TABLE bulk_lr_features (LIKE supplier_lr_features INCLUDING DEFAULTS) ON COMMIT DROP")
            with conn.cursor().copy("COPY bulk_lr_features(api,code,trade_date,body,batch_id) FROM STDIN") as copy:
                for row in normalized:
                    copy.write_row((api, row["code"], row["trade_date"], encode(row["body"]), batch))
            conn.execute("""INSERT INTO supplier_lr_features SELECT * FROM bulk_lr_features
                ON CONFLICT(api,code,trade_date) DO UPDATE SET body=excluded.body,batch_id=excluded.batch_id
                WHERE (SELECT collected_at FROM supplier_market_batches WHERE batch_id=supplier_lr_features.batch_id) <= %s""",
                (collected_at,))
        conn.execute("""INSERT INTO supplier_tushare_eod_runs
            (run_key,source_key,api,trade_date,schema_version,batch_id,row_count,completed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
            ON CONFLICT(run_key) DO UPDATE SET batch_id=excluded.batch_id,row_count=excluded.row_count,
                completed_at=excluded.completed_at""",
            (run_key, source_key, api, day, SCHEMA_VERSION, batch, len(raw)))
    return dict(batch=batch, rows=len(raw), status="STORED")


def collect_day(store, client, api, day, calendar, request, checkpoint):
    """Return a small receipt; every physical page uses the durable request budget."""
    source_key = client.identity(api)
    run_key = fingerprint([source_key, api, client.fields(api), day.isoformat(), "ALL", SCHEMA_VERSION])
    max_age = getattr(client,"completed_run_ttl_seconds",None)
    checkpoint()
    with store._connect() as conn:
        existing = _completed_run(conn,api,run_key,max_age)
    if existing:
        return dict(batch=existing[0], rows=existing[1], status="ALREADY_STORED", cached_pages=0)
    all_rows, seen, fields, observed, cached_pages = [], set(), None, [], 0
    for index in range(MAX_PAGES):
        checkpoint()
        scope = dict(trade_date=day.strftime("%Y%m%d"), limit=PAGE_SIZE, offset=index * PAGE_SIZE)
        key = fingerprint([source_key, api, client.fields(api), scope, SCHEMA_VERSION])
        page = _cached_page(store, key)
        if page is not None:
            cached_pages += 1
        if page is None:
            def operation():
                fresh = client.fetch_page(api, scope)
                _validate_page(fresh, api, day, fields, seen)
                return _save_page(store, key, source_key, api, scope, fresh)
            result = request("tushare:" + api, {**scope, "source_key": source_key, "schema_version": SCHEMA_VERSION}, operation)
            if result["status"] != "SAVED":
                raise ValueError("bulk page request failed")
            page = _cached_page(store, key)
            if page is None:
                raise ValueError("successful bulk page is unavailable")
        rows, codes = _validate_page(page, api, day, fields, seen)
        fields = page["fields"]
        all_rows.extend(rows)
        seen.update(codes)
        observed.append(datetime.fromisoformat(page["collected_at"]))
        if len(rows) < PAGE_SIZE:
            if not all_rows:
                raise ValueError("bulk dataset has no rows")
            normalized = _normalized(api, all_rows, day, calendar)
            checkpoint()
            # The complete dataset becomes available only after its latest page.
            # Earlier cache pages retain their own acquisition timestamps.
            return {**_publish(store, api, day, run_key, source_key, all_rows, normalized, max(observed), max_age=max_age),
                    "cached_pages": cached_pages}
    raise ValueError("bulk pagination limit reached without a final page")


def _collect_calendar(store, client, first, last, request, checkpoint):
    from .left_right_backfill_collectors import _calendar
    source = client.identity("trade_cal")
    scope = dict(exchange="SSE", start_date=first.strftime("%Y%m%d"), end_date=last.strftime("%Y%m%d"))
    checkpoint()
    with store._connect() as conn:
        exists = conn.execute("""SELECT 1 FROM supplier_batches WHERE api='trade_cal'
            AND requested_scope->>'source_key'=%s AND requested_scope->>'start_date'<=%s
            AND requested_scope->>'end_date'>=%s AND requested_scope->>'schema_version'=%s
            AND requested_scope->'fields'=%s::jsonb LIMIT 1""",
            (source, scope["start_date"], scope["end_date"], SCHEMA_VERSION, encode(client.fields("trade_cal")))).fetchone()
    calendar = _calendar(store, first, last)
    if exists and len(calendar) == (last - first).days + 1:
        return calendar
    def operation():
        page = client.fetch_page("trade_cal", scope)
        raw = [dict(zip(page["fields"], row, strict=True)) for row in page["rows"]]
        normalized = calendar_rows(raw, first, last)
        if len(normalized) != (last - first).days + 1:
            raise ValueError("incomplete historical calendar")
        return save_reference(store, "trade_cal", {**scope, "source_key": source, "schema_version": SCHEMA_VERSION,
                                                   "fields": client.fields("trade_cal")}, raw, normalized)
    result = request("tushare:trade_cal", {**scope, "source_key": source}, operation)
    if result["status"] != "SAVED":
        raise ValueError("bulk calendar request failed")
    return _calendar(store, first, last)


def _collection_plan(calendar, first, last):
    """Keep the full bounded warmup: exchange sessions are not stock bars."""
    from .left_right_backfill_collectors import WARMUP_DAYS
    warmup = first - timedelta(days=WARMUP_DAYS)
    days = [date.fromisoformat(day) for day, opened in sorted(calendar.items())
            if opened and warmup.isoformat() <= day <= last.isoformat()]
    planned = []
    for day in days:
        planned.extend((api, day) for api in DAILY_APIS)
        monday = day - timedelta(days=day.weekday())
        week = [monday + timedelta(days=i) for i in range(7)]
        if (all(d.isoformat() in calendar for d in week)
                and day == max(d for d in week if calendar[d.isoformat()])):
            planned.append(("weekly", day))
    return planned


def collect_all(store, body, config, captures, request, checkpoint, progress=None):
    """Collect market-wide evidence. Archive scoring remains in the existing worker."""
    from .left_right_backfill import Interrupted
    from .left_right_backfill_collectors import WARMUP_DAYS, collect_boards
    started, failures, issues = monotonic(), 0, []
    first, last = date.fromisoformat(body["date_from"]), date.fromisoformat(body["date_to"])
    warmup = first - timedelta(days=WARMUP_DAYS)
    calendar_end = last + timedelta(days=6 - last.weekday())
    def call(source, scope, operation):
        nonlocal failures
        checkpoint()
        if monotonic() - started >= 4800:
            raise Interrupted("COLLECTION_TIME_LIMIT")
        result = request(source, scope, operation)
        failures = failures + 1 if result["status"] == "FAILED" else 0
        if failures >= 3:
            raise Interrupted("CONSECUTIVE_PROVIDER_FAILURES")
        return result
    try:
        if not config:
            return [dict(source="tushare", reason="CONFIG_UNAVAILABLE")]
        client = BulkTushare(config)
        try:
            calendar = _collect_calendar(store, client, warmup, calendar_end, call, checkpoint)
        except ValueError:
            return [dict(source="tushare:trade_cal", reason="CALENDAR_UNAVAILABLE")]
        planned = _collection_plan(calendar, first, last)
        planned_days = {day for _, day in planned}
        daily_dates = [day for api, day in planned if api == "daily"]
        weekly_dates = [day for api, day in planned if api == "weekly"]
        reused_pages, completed_days = 0, 0
        for index, (api, day) in enumerate(planned):
            checkpoint()
            try:
                receipt = collect_day(store, client, api, day, calendar, call, checkpoint)
                reused_pages += receipt.get("cached_pages", 0)
            except ValueError as exc:
                issues.append(dict(source="tushare:" + api, trade_date=day.isoformat(), reason=str(exc)))
            if index + 1 == len(planned) or planned[index + 1][1] != day:
                completed_days += 1
            if progress:
                progress(phase="collect", dataset=api, trade_date=day.isoformat(),
                         completed_days=completed_days, total_days=len(planned_days), cached_pages=reused_pages,
                         completed_units=index+1, total_units=len(planned), daily_days=len(daily_dates),
                         weekly_days=len(weekly_dates),
                         daily_date_from=daily_dates[0].isoformat() if daily_dates else None,
                         daily_date_to=daily_dates[-1].isoformat() if daily_dates else None)
        collect_boards(store, body, warmup, calendar_end, captures, call, checkpoint, issues)
    except Interrupted as exc:
        if str(exc) not in ("REQUEST_BUDGET_EXHAUSTED", "COLLECTION_TIME_LIMIT", "CONSECUTIVE_PROVIDER_FAILURES"):
            raise
        issues.append(dict(source="collection", reason=str(exc)))
    return issues
