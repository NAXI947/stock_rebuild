"""Bounded ETF history intake and point-in-time recommendation statistics."""
from copy import deepcopy
from datetime import date, datetime, time
from decimal import localcontext
from pathlib import Path


from ..ledger import encode, fingerprint
from ..simulator import CHINA, CODE, PRECISION, decimal, moment
from .bulk_collect import RequestLog
from .inputs import document, mapping_file
from .market_bootstrap import ProbeError, save
from .penetration import research_products
from .tushare_bootstrap import SourceFailure, Tushare
from .provider_errors import error_diagnostic

VERSION = "etf-history-v1"
MAX_PRODUCTS = 24
FETCH_DAYS = 25
ATR_LAG_DAYS = 3
DAILY = "etf_statistics_daily"
ADJUSTMENT = "etf_statistics_adj"
START = time(15, 35)


def maintenance_attempt(calendar, now):
    """Two bounded daytime windows, sharing the maintenance task policy."""
    from ..maintenance_policy import maintenance_attempt as attempt
    now = now.astimezone(CHINA)
    return attempt(now) if now.date().isoformat() in calendar else None


def _stored_window(ledger, codes, days, at):
    """Read exact date coverage, not merely the newest batch's row count."""
    expected = {day.isoformat() for day in days}
    completed = {code: {} for code in codes}
    if not codes or not expected:
        return completed
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("""SELECT endpoint,scope->>'ts_code',body,collected_at,batch_id
            FROM (SELECT endpoint,scope,body,collected_at,batch_id,
                row_number() OVER (PARTITION BY endpoint,scope->>'ts_code'
                    ORDER BY collected_at DESC,batch_id DESC) AS recent
                FROM supplier_market_batches WHERE provider='tushare'
                AND endpoint=ANY(%s) AND scope->>'ts_code'=ANY(%s) AND collected_at<=%s
            ) AS history WHERE recent<=3 ORDER BY collected_at,batch_id""",
            ([DAILY, ADJUSTMENT], list(codes), at)).fetchall()
    coverage = {}
    batches = {}
    for endpoint, code, body, available, batch in rows:
        key = (code, endpoint)
        values = document(body)["rows"]
        valid = {row.get("trade_date") for row in values
                 if row.get("code") == code and row.get("trade_date") in expected
                 and datetime.combine(date.fromisoformat(row["trade_date"]), time(15), CHINA) <= available}
        coverage.setdefault(key, set()).update(valid)
        batches[key] = batch
    for (code, endpoint), dates in coverage.items():
        if dates == expected:
            completed[code][endpoint] = dict(status="ALREADY_STORED", batch=batches[(code, endpoint)],
                                             rows=len(expected), complete=True)
    return completed


def select_targets(ledger, mapping, calendar, now, *, limit=MAX_PRODUCTS):
    """Prioritize held/recommended products while reserving capacity for rotation."""
    if type(limit) is not int or not 1 <= limit <= MAX_PRODUCTS:
        raise ValueError("invalid ETF statistics batch limit")
    eligible = sorted(research_products(mapping, now))
    days = history_window(calendar, now, include_closed_today=True, size=FETCH_DAYS)
    if not eligible or not days:
        return []
    complete = _stored_window(ledger, eligible, days, now)
    eligible = [code for code in eligible if len(complete[code]) < 2]
    if not eligible:
        return []
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        held = {row[0] for row in conn.execute(
            "SELECT etf FROM cycles WHERE ns=%s AND status=ANY(%s)",
            (ledger.ns, ["OPEN", "WAIT_BUY", "WAIT_EXIT"])).fetchall()}
        # Every report of the latest published session, not only its last round.
        reports = [document(row[0]) for row in conn.execute("""SELECT payload FROM events
            WHERE ns=%s AND kind='report' AND at<=%s AND left(at,10)=(SELECT left(max(at),10)
                FROM events WHERE ns=%s AND kind='report' AND at<=%s)""",
            (ledger.ns, now.isoformat(), ledger.ns, now.isoformat())).fetchall()]
        recent = {row[0]: row[1] for row in conn.execute("""SELECT scope->>'ts_code',max(collected_at)
            FROM supplier_market_batches WHERE provider='tushare' AND endpoint=ANY(%s)
            AND scope->>'ts_code'=ANY(%s) AND collected_at<=%s GROUP BY scope->>'ts_code'""",
            ([DAILY, ADJUSTMENT], eligible, now)).fetchall()}
    recommended = set()
    for report in reports:
        recommended.update(report.get("candidates", {}))
        for sector in report.get("input_meta", {}).get("etf_recommendations", {}).get("sectors", {}).values():
            recommended.update(sector.get("representatives", []))
    # Oldest-first plus a completed-session rotation prevents permanent code-order starvation.
    offset = days[-1].toordinal() % len(eligible)
    rotated = eligible[offset:] + eligible[:offset]
    order = {code: i for i, code in enumerate(rotated)}
    oldest = datetime.min.replace(tzinfo=CHINA)
    age = lambda code: (recent.get(code, oldest), order[code])
    priority = sorted((code for code in eligible if code in held | recommended),
                      key=lambda code: (code not in held, *age(code)))
    reserved = priority[:max(1, limit - 1)]
    remaining = sorted((code for code in eligible if code not in reserved), key=age)
    return (reserved + remaining)[:limit]


def history_window(calendar, at, *, include_closed_today=False, size=20):
    """Only explicitly supplied trading sessions; no weekday reconstruction."""
    at = at.astimezone(CHINA)
    days = sorted(set(date.fromisoformat(d) if isinstance(d, str) else d for d in calendar))
    last = at.date()
    return [day for day in days if day < last or
            (include_closed_today and day == last and at.time() >= START)][-size:]


def normalize_daily(rows, symbol, days):
    result = {}
    allowed = set(days)
    for row in rows:
        try:
            day = date.fromisoformat(row["trade_date"])
            values = {key: decimal(row[key]) for key in ("open", "high", "low", "close", "pre_close", "vol", "amount")}
        except (ValueError, TypeError, ArithmeticError, KeyError):
            raise SourceFailure("invalid ETF statistics daily value") from None
        if row.get("ts_code") != symbol or day not in allowed or day in result:
            raise SourceFailure("ETF statistics daily identity/date mismatch")
        if (min(values[k] for k in ("open", "high", "low", "close", "pre_close")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"])
                or values["vol"] < 0 or values["amount"] < 0):
            raise SourceFailure("ETF statistics daily price/amount mismatch")
        result[day] = dict(code=symbol, trade_date=day.isoformat(),
                           close_cny=str(values["close"]), pre_close_cny=str(values["pre_close"]),
                           high_cny=str(values["high"]), low_cny=str(values["low"]),
                           volume_units=str(values["vol"] * 100),
                           amount_cny=str(values["amount"] * 1000))
    if not result:
        raise SourceFailure("ETF statistics daily empty")
    return [result[day] for day in sorted(result)]


def normalize_adjustment(rows, symbol, days):
    result = {}
    for row in rows:
        try:
            day, factor = date.fromisoformat(row["trade_date"]), decimal(row["adj_factor"])
        except (ValueError, TypeError, ArithmeticError, KeyError):
            raise SourceFailure("invalid ETF adjustment factor") from None
        if row.get("ts_code") != symbol or day not in days or day in result or factor <= 0:
            raise SourceFailure("ETF adjustment identity/date mismatch")
        result[day] = dict(code=symbol, trade_date=day.isoformat(), adj_factor=str(factor))
    if not result:
        raise SourceFailure("ETF adjustment factors empty")
    return [result[day] for day in sorted(result)]


def derive(symbol, daily, factors, calendar, at):
    """Each normalized row retains its original collected_at and batch reference."""
    expected = history_window(calendar, at)
    expected_s = [day.isoformat() for day in expected]
    eligible = {}
    adjustment = {}
    for rows, target in ((daily, eligible), (factors, adjustment)):
        for row in rows:
            if (row.get("code") != symbol or row.get("trade_date") not in expected_s
                    or moment(row["available_at"]) > at):
                continue
            when = date.fromisoformat(row["trade_date"])
            if moment(row["available_at"]) < datetime.combine(when, time(15), CHINA):
                continue
            previous = target.get(row["trade_date"])
            if previous is None or moment(previous["available_at"]) < moment(row["available_at"]):
                target[row["trade_date"]] = row
    covered = [day for day in expected_s if day in eligible]
    result = dict(history_version=VERSION, history_as_of=expected_s[-1] if expected_s else None,
                  history_days=len(covered), complete20=len(expected_s) == 20 and len(covered) == 20,
                  adv20=None, adv5=None, ma5_raw_prev5=None, ma5_basis_status="UNVERIFIED",
                  prior_close_cny=None, adjustment_factor_latest=None, history_available_at=None,
                  history_batches=[])
    used = [eligible[day] for day in covered]
    if used:
        result["history_available_at"] = max((row["available_at"] for row in used), key=moment)
        result["history_batches"] = sorted({row["batch"] for row in used})
    with localcontext(PRECISION):
        if result["complete20"]:
            result["adv20"] = str(sum(decimal(eligible[day]["amount_cny"]) for day in expected_s) / 20)
        last5 = expected_s[-5:]
        if len(last5) == 5 and all(day in eligible for day in last5):
            result["adv5"] = str(sum(decimal(eligible[day]["amount_cny"]) for day in last5) / 5)
            result["ma5_raw_prev5"] = str(sum(decimal(eligible[day]["close_cny"]) for day in last5) / 5)
            result["prior_close_cny"] = eligible[last5[-1]]["close_cny"]
            if all(day in adjustment for day in last5):
                fact = [decimal(adjustment[day]["adj_factor"]) for day in last5]
                result["adjustment_factor_latest"] = str(fact[-1])
                result["ma5_basis_status"] = "VERIFIED" if len(set(fact)) == 1 else "CORP_ACTION_WINDOW"
                used += [adjustment[day] for day in last5]
                result["history_available_at"] = max((row["available_at"] for row in used), key=moment)
                result["history_batches"] = sorted({row["batch"] for row in used})
    return result


def atr20(symbol, daily, calendar, at):
    """Mean true range ratio of 20 completed sessions before the session of ``at``.

    The window may end up to ATR_LAG_DAYS sessions back; the day used is reported.
    """
    at = at.astimezone(CHINA)
    days = sorted({d if isinstance(d, str) else d.isoformat() for d in calendar})
    days = [day for day in days if day < at.date().isoformat()]
    rows = {}
    for row in daily:
        day = row.get("trade_date")
        if (row.get("code") != symbol or not all(row.get(key) for key in ("high_cny", "low_cny", "pre_close_cny"))
                or not datetime.combine(date.fromisoformat(day), time(15), CHINA) <= moment(row["available_at"]) <= at):
            continue
        if day not in rows or moment(rows[day]["available_at"]) < moment(row["available_at"]):
            rows[day] = row
    for lag in range(ATR_LAG_DAYS):
        window = days[len(days) - lag - 20:len(days) - lag]
        if len(window) == 20 and all(day in rows for day in window):
            with localcontext(PRECISION):
                total = 0
                for day in window:
                    high, low, base = (decimal(rows[day][key]) for key in ("high_cny", "low_cny", "pre_close_cny"))
                    total += max(high - low, abs(high - base), abs(low - base)) / base
                return dict(atr20=str(total / 20), atr_as_of=window[-1])
    return dict(atr20=None, atr_as_of=None)


def refresh(ledger, config, mapping, calendar, now, *, symbols=None, attempt=None, request=None):
    """At most 48 requests per completed session/opportunity, with durable deduplication.

    ``request`` optionally wraps each actual provider operation in the maintenance
    job's shared budget. The inner budget also bounds restarts and repeated jobs.
    """
    now = now.astimezone(CHINA)
    due = maintenance_attempt(calendar, now)
    if due is None or (attempt is not None and attempt != due):
        return dict(status="OUTSIDE_WINDOW", new_requests=0)
    attempt = due
    days = history_window(calendar, now, include_closed_today=True, size=FETCH_DAYS)
    if len(days) < 20:
        return dict(status="INSUFFICIENT_CALENDAR", new_requests=0)
    if not Path(config).is_file():
        return dict(status="CONFIG_UNAVAILABLE", new_requests=0)
    eligible = sorted(research_products(mapping, now))
    if symbols is None:
        symbols = select_targets(ledger, mapping, calendar, now)
    else:
        symbols = list(symbols)
        if (len(symbols) > MAX_PRODUCTS or len(set(symbols)) != len(symbols)
                or any(code not in eligible for code in symbols)):
            raise ValueError("ETF statistics scope outside catalog identities or bounded products")
    if not symbols:
        return dict(status="EMPTY_SCOPE", new_requests=0)
    stored = _stored_window(ledger, symbols, days, now)
    run_key = "etf-stats:" + fingerprint([ledger.ns, days[-1].isoformat(), VERSION, attempt])[:48]
    budget = RequestLog(ledger, run_key)
    results = []
    for symbol in symbols:
        params = dict(ts_code=symbol, start_date=days[0].strftime("%Y%m%d"), end_date=days[-1].strftime("%Y%m%d"))
        for api, endpoint, normalizer in (("fund_daily", DAILY, normalize_daily),
                                           ("fund_adj", ADJUSTMENT, normalize_adjustment)):
            scope = dict(params, history_version=VERSION)
            if endpoint in stored[symbol]:
                results.append(dict(code=symbol, api=api, **stored[symbol][endpoint]))
                continue
            def operation(api=api, endpoint=endpoint, normalizer=normalizer, scope=scope, params=params, symbol=symbol):
                client = Tushare(config)
                client.config["timeout_seconds"] = min(15,int(client.config.get("timeout_seconds",15)))
                override = (client.config.get("api_overrides") or {}).get(api)
                if override:
                    override["timeout_seconds"] = min(15,int(override.get("timeout_seconds",15)))
                raw = client.fetch(api, params)
                received = datetime.now(CHINA)
                rows = normalizer(raw, symbol, set(days))
                body = dict(rows=rows, source_api=api, raw=raw,
                            units="fund_daily:amount_thousand_cny,vol_100_units" if api == "fund_daily" else "ratio")
                return save(ledger, "tushare", endpoint, scope, body, len(rows), None, received)
            try:
                def counted():
                    if request is None:
                        return operation()
                    response = request("tushare:" + endpoint, dict(scope, attempt=attempt), operation)
                    if response.get("status") != "SAVED":
                        raise SourceFailure.from_receipt(response)
                    return response["receipt"]
                result = budget.call("tushare:" + endpoint, scope, counted, limit=MAX_PRODUCTS)
                results.append(dict(code=symbol, api=api, status=result["status"], batch=result["batch"],
                                    rows=result.get("rows"), complete=result.get("rows") == len(days)))
            except (SourceFailure, ProbeError, OSError, ValueError, TypeError, KeyError) as exc:
                results.append(dict(code=symbol, api=api, status="UNAVAILABLE", error=type(exc).__name__,
                                    **error_diagnostic(exc)))
    return dict(status="READY" if all(r["status"] in ("STORED", "ALREADY_STORED") and r.get("complete") is True for r in results) else "PARTIAL",
                history_as_of=days[-1].isoformat(), attempt=attempt, new_requests=budget.new_calls, items=results,
                evaluated_products=len(symbols), omitted_products=max(0,len(eligible)-len(symbols)))


def overlay_mapping(ledger, mapping, calendar, at):
    """Read stored history only, without changing persisted mapping or trading rules."""
    codes = sorted(mapping["products"])
    if not codes:
        return mapping
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("""
            SELECT endpoint,body,collected_at,batch_id FROM (
                SELECT endpoint,body,collected_at,batch_id,
                       row_number() OVER (PARTITION BY endpoint,scope->>'ts_code'
                                          ORDER BY collected_at DESC,batch_id DESC) AS recent
                FROM supplier_market_batches
                WHERE provider='tushare' AND endpoint=ANY(%s)
                    AND scope->>'ts_code'=ANY(%s) AND collected_at<=%s
            ) AS history WHERE recent<=3 ORDER BY collected_at,batch_id
        """, ([DAILY,ADJUSTMENT], codes, at)).fetchall()
    daily, factors = [], []
    for endpoint, body, available, batch in rows:
        target = daily if endpoint == DAILY else factors
        target.extend(row | dict(available_at=available.astimezone(CHINA).isoformat(), batch=batch)
                      for row in document(body)["rows"])
    result = deepcopy(mapping)
    for code, product in result["products"].items():
        if not CODE.fullmatch(code):
            continue
        product["recommendation_stats"] = derive(code,daily,factors,calendar,at)
    return result


class StatisticsRefresh:
    """Retired compatibility entry: all history intake uses the maintenance queue."""
    def __init__(self, ledger, config, calendar_file, mapping_file_path):
        raise ValueError("ETF_STATISTICS_POLLING_RETIRED_USE_MAINTENANCE_QUEUE")
