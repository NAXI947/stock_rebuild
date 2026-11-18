"""Bounded manual EOD evidence adapters, reusing the existing private clients/storage."""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .simulator import CHINA, decimal
from .stage2.http_json import get_json, post_json
from .stage2.market_bootstrap import instant, save as save_market
from .stage2.tushare_bootstrap import FIELDS, Tushare, calendar_rows, daily_rows, save as save_reference

FEATURE_FIELDS = {
    "adj_factor": ("ts_code", "trade_date", "adj_factor"),
    "daily_basic": ("ts_code", "trade_date", "turnover_rate", "volume_ratio", "circ_mv"),
    "cyq_perf": ("ts_code", "trade_date", "cost_85pct"),
    "weekly": ("ts_code", "trade_date", "close", "vol", "amount"),
}
WARMUP_DAYS, BOARD_PAGES, MEMBER_PAGES, MAX_BOARDS = 280, 5, 10, 300
MEMBER_PAGE_SIZE = 1000


class HistoricalTushare(Tushare):
    def fetch(self, api, params):
        if api in FIELDS:
            return super().fetch(api, params)
        if api not in FEATURE_FIELDS or api in self.used:
            raise ValueError("unsupported or repeated historical endpoint")
        self.used.add(api)
        base = self.config
        override = (base.get("api_overrides") or {}).get(api) or {}
        token = override.get("token", base.get("token"))
        endpoint = override.get("base_url", base.get("base_url", "https://api.tushare.pro"))
        timeout = min(30, int(override.get("timeout_seconds", base.get("timeout_seconds", 15))))
        if not isinstance(token, str) or not token.strip() or timeout <= 0:
            raise ValueError("invalid provider configuration")
        fields = FEATURE_FIELDS[api]
        response = post_json(endpoint, dict(api_name=api, token=token.strip(), params=params,
                                          fields=",".join(fields)), timeout=timeout, protocols="=http,https")
        if type(response.get("code")) is not int or response["code"] != 0:
            raise ValueError("provider status rejected")
        data = response.get("data", {})
        columns, rows = data.get("fields"), data.get("items")
        if (not isinstance(columns, list) or len(columns) != len(set(columns))
                or not set(fields).issubset(columns) or not isinstance(rows, list) or not 0 < len(rows) <= 1000):
            raise ValueError("invalid historical provider rows")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError("invalid historical provider row")
        return [{key: value for key, value in zip(columns, row, strict=True) if key in fields} for row in rows]


def normalize_features(api, raw, code, first, last, calendar):
    values = {}
    for row in raw:
        day = date.fromisoformat(row["trade_date"])
        if row["ts_code"] != code or not first <= day <= last or day.isoformat() in values:
            raise ValueError("historical feature scope mismatch")
        if api == "adj_factor":
            value = decimal(row["adj_factor"])
            if value <= 0:
                raise ValueError("invalid adjustment factor")
            values[day.isoformat()] = str(value)
        elif api == "daily_basic":
            item = {}
            for field, target, multiplier in (("turnover_rate", "turnover_ratio", decimal(".01")),
                                               ("volume_ratio", "volume_ratio", 1),
                                               ("circ_mv", "circ_mv_cny", 10000)):
                value = row.get(field)
                if value is not None:
                    number = decimal(value)
                    if number < 0:
                        raise ValueError("negative daily feature")
                    item[target] = str(number * multiplier)
            values[day.isoformat()] = item
        elif api == "cyq_perf":
            value = row.get("cost_85pct")
            if value is not None and decimal(value) <= 0:
                raise ValueError("invalid cost85")
            values[day.isoformat()] = {"cost85": str(decimal(value)) if value is not None else None}
            if "cost_50pct" in row:
                value50 = row.get("cost_50pct")
                values[day.isoformat()]["cost50"] = str(decimal(value50)) if value50 is not None else None
            if "winner_rate" in row:
                winner = row.get("winner_rate")
                values[day.isoformat()]["winner_ratio"] = str(min(decimal(1), max(decimal(0), decimal(winner)/100))) if winner is not None else None
        else:
            close, vol, amount = (decimal(row[key]) for key in ("close", "vol", "amount"))
            if close <= 0 or min(vol, amount) < 0:
                raise ValueError("invalid weekly bar")
            monday = day - timedelta(days=day.weekday())
            week = [monday + timedelta(days=i) for i in range(7)]
            known = all(d.isoformat() in calendar for d in week)
            open_days = [d for d in week if calendar.get(d.isoformat())]
            complete = known and bool(open_days) and day == max(open_days) and day <= last
            # The retained tushare_eod._normalize('weekly') gateway contract is
            # already shares/CNY/ratio; daily's 100/1000 factors do not apply.
            values[day.isoformat()] = dict(trade_date=day.isoformat(), close=str(close),
                volume_shares=str(vol), amount_cny=str(amount), complete=complete)
            for field in ("open", "high", "low", "pct_chg"):
                if field in row:
                    values[day.isoformat()]["return_ratio" if field == "pct_chg" else field] = str(decimal(row[field])) if row[field] is not None else None
    if api == "weekly":
        return dict(code=code, weekly=[values[day] for day in sorted(values)], raw=raw)
    return dict(code=code, **{"factors" if api == "adj_factor" else "features": values}, raw=raw)


def _existing_tushare(store, api, scope):
    # A previously saved bounded response is reused, including legitimate missing/suspended dates.
    with store._connect() as conn:
        if api in ("trade_cal", "daily"):
            rows = conn.execute("""SELECT batch_id,requested_scope FROM supplier_batches
                WHERE api=%s AND coalesce(requested_scope->>'ts_code','')=%s
                ORDER BY collected_at DESC LIMIT 100""", (api, scope.get("ts_code", ""))).fetchall()
        else:
            rows = conn.execute("""SELECT batch_id,scope FROM supplier_market_batches
                WHERE provider='tushare' AND endpoint=%s AND scope->>'ts_code'=%s
                ORDER BY collected_at DESC LIMIT 100""", (api, scope.get("ts_code"))).fetchall()
    for batch, raw in rows:
        saved = json.loads(raw)
        if (saved.get("ts_code") == scope.get("ts_code")
                and saved.get("exchange") == scope.get("exchange")
                and saved.get("start_date", "99999999") <= scope["start_date"]
                and saved.get("end_date", "") >= scope["end_date"]):
            return batch
    return None


def _calendar(store, first, last):
    with store._connect() as conn:
        rows = conn.execute("SELECT cal_date,is_open FROM supplier_trade_calendar "
                            "WHERE exchange='SSE' AND cal_date BETWEEN %s AND %s", (first, last)).fetchall()
    return {day.isoformat(): is_open for day, is_open in rows}


def history_request(captures, endpoint, day, offset, plate=None):
    max_offset = (MEMBER_PAGES-1) * MEMBER_PAGE_SIZE if endpoint == "046" else 1999
    if endpoint not in ("042", "046") or not 0 <= offset <= max_offset:
        raise ValueError("invalid historical request scope")
    root = Path(captures)
    if not root.is_absolute():
        raise ValueError("historical captures must be absolute")
    files = list(root.glob(endpoint + "_*.json"))
    if len(files) != 1 or files[0].is_symlink() or files[0].stat().st_size > 1024 * 1024:
        raise ValueError("expected one bounded private historical capture")
    capture = json.loads(files[0].read_text(encoding="utf-8-sig"))
    parsed = urlsplit(capture["专属接口链接"].strip())
    if (parsed.scheme != "https" or parsed.hostname != "apphis.longhuvip.com"
            or parsed.path != "/w1/api/index.php" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("historical capture origin rejected")
    args = dict(parse_qsl(parsed.query, keep_blank_values=True))
    expected_action = "RealRankingInfo" if endpoint == "042" else "ZhiShuStockList_W8"
    if args.get("a") != expected_action or args.get("c") != "ZhiShuRanking":
        raise ValueError("historical capture action rejected")
    args.update(Date=day, Index=str(offset), st="60" if endpoint == "042" else str(MEMBER_PAGE_SIZE))
    if endpoint == "042":
        args.update(RStart="0925", REnd="1500", Type="1", Order="1")
    else:
        if not isinstance(plate, str) or not re.fullmatch(r"[0-9]{6}", plate):
            raise ValueError("invalid explicit historical board")
        args["PlateID"] = plate
        args["REnd"] = "1500"
    scope = {key: args[key] for key in ("Date", "PlateID", "Index", "st", "RStart", "REnd", "Type", "Order") if key in args}
    return urlunsplit(parsed._replace(query=urlencode(args))), scope


def _existing_page(store, endpoint, day, offset, plate):
    with store._connect() as conn:
        row = conn.execute("""SELECT batch_id,body FROM supplier_market_batches
            WHERE provider='kaipanla' AND endpoint=%s AND scope->>'Date'=%s
              AND scope->>'Index'=%s AND coalesce(scope->>'PlateID','')=%s
              AND (%s<>'042' OR (scope->>'Type'='1' AND scope->>'Order'='1'
                   AND scope->>'RStart'='0925' AND scope->>'REnd'='1500' AND scope->>'st'='60'))
            ORDER BY collected_at DESC LIMIT 1""", (endpoint, day, str(offset), plate or "", endpoint)).fetchone()
    if not row:
        return None
    payload = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    rows, total = payload.get("rows"), _page_count(payload.get("reported_total"))
    if (payload.get("trade_days") != [day] or not isinstance(rows, list) or total is None
            or len(rows) > (60 if endpoint == "042" else MEMBER_PAGE_SIZE)
            or endpoint == "042" and (total != len(rows) or not rows and offset == 0)
            or endpoint == "046" and (not rows or total < offset + len(rows))):
        return None
    if any(not isinstance(item, list) or len(item) < (4 if endpoint == "042" else 2)
           or not re.fullmatch(r"[0-9]{6}", str(item[0])) for item in rows):
        return None
    ids = [str(item[0]) for item in rows]
    if len(set(ids)) != len(ids):
        return None
    return dict(batch=row[0], rows=len(rows), total=total, ids=ids)


def _page_count(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 9:
        value = int(value)
    # The provider's total is a fact, not a request budget. Larger member lists
    # remain explicit partial results at MEMBER_PAGES rather than failing page 1.
    return value if type(value) is int and value >= 0 else None


def _page(store, captures, endpoint, day, offset, plate):
    url, scope = history_request(captures, endpoint, day, offset, plate)
    raw = get_json(url, timeout=15)
    rows, total = raw.get("list"), _page_count(raw.get("Count"))
    if (str(raw.get("errcode")) != "0" or raw.get("Day") != [day]
            or not isinstance(rows, list) or total is None or len(rows) > int(scope["st"])
            or endpoint == "042" and (total != len(rows) or not rows and offset == 0)
            or endpoint == "046" and (not rows or total < offset + len(rows))):
        raise ValueError("historical page date, rows or total mismatch")
    if any(not isinstance(row, list) or len(row) < (4 if endpoint == "042" else 2)
           or not re.fullmatch(r"[0-9]{6}", str(row[0])) for row in rows):
        raise ValueError("invalid historical board/member identity")
    ids = [str(row[0]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate historical page identity")
    received = datetime.now(CHINA)
    source = instant(raw["Time"], received, milliseconds=False) if "Time" in raw else None
    body = dict(rows=rows, trade_days=[day], reported_total=total, raw=raw,
                count_basis="PAGE_ROWS" if endpoint == "042" else "TOTAL_ROWS")
    receipt = save_market(store, "kaipanla", endpoint, scope, body, len(rows), source, received)
    return {**receipt, "total": total, "ids": ids}


def collect(store, body, config, captures, request, checkpoint, progress=None):
    """Dispatch whole-market jobs; retain the explicit-stock compatibility path."""
    if body.get("scope") == "ALL" or not body.get("codes"):
        from .bulk_eod_collect import collect_all
        return collect_all(store, body, config, captures, request, checkpoint, progress)
    from .left_right_backfill import Interrupted
    started, issues = monotonic(), []
    first, last = date.fromisoformat(body["date_from"]), date.fromisoformat(body["date_to"])
    warmup = first - timedelta(days=WARMUP_DAYS)
    calendar_end = last + timedelta(days=6 - last.weekday())
    failures = 0

    def call(source, scope, operation):
        nonlocal failures
        checkpoint()
        if monotonic() - started >= 1500:
            raise Interrupted("COLLECTION_TIME_LIMIT")
        result = request(source, scope, operation)
        failures = failures + 1 if result["status"] == "FAILED" else 0
        if failures >= 3:
            raise Interrupted("CONSECUTIVE_PROVIDER_FAILURES")
        return result

    def tushare(api, code=None):
        end = calendar_end if api == "trade_cal" else last
        scope = dict(start_date=warmup.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        scope.update({"ts_code": code} if code else {"exchange": "SSE"})
        if _existing_tushare(store, api, scope):
            return
        def operation():
            raw = HistoricalTushare(config).fetch(api, scope)
            if api == "trade_cal":
                normalized = calendar_rows(raw, warmup, end)
                if len(normalized) != (end - warmup).days + 1:
                    raise ValueError("incomplete historical calendar")
                return save_reference(store, api, scope, raw, normalized)
            if api == "daily":
                return save_reference(store, api, scope, raw, daily_rows(raw, code, warmup, end))
            normalized = normalize_features(api, raw, code, warmup, end, _calendar(store, warmup, calendar_end))
            source_days = list(normalized.get("factors", normalized.get("features", {})))
            if api == "weekly":
                source_days = [row["trade_date"] for row in normalized["weekly"]]
            # The observed day is source evidence; collection time is stored separately.
            source = max(source_days) + "T15:00:00+08:00" if source_days else None
            return save_market(store, "tushare", api, scope, normalized, len(raw), source, datetime.now(CHINA))
        call("tushare:" + api, scope, operation)

    try:
        if not config:
            issues.append(dict(source="tushare", reason="CONFIG_UNAVAILABLE"))
        else:
            tushare("trade_cal")
            for code in body["codes"]:
                for api in ("daily", "adj_factor", "daily_basic", "cyq_perf", "weekly"):
                    tushare(api, code)
        collect_boards(store, body, warmup, calendar_end, captures, call, checkpoint, issues)
    except Interrupted as exc:
        if str(exc) not in ("REQUEST_BUDGET_EXHAUSTED", "COLLECTION_TIME_LIMIT", "CONSECUTIVE_PROVIDER_FAILURES"):
            raise
        issues.append(dict(source="collection", reason=str(exc)))
    return issues


def collect_boards(store, body, warmup, calendar_end, captures, call, checkpoint, issues):
    """The same bounded historical board pages serve bulk and optional stock jobs."""
    def pages(endpoint, day, plate=None):
        offset, total, ids = 0, None, []
        bound = BOARD_PAGES if endpoint == "042" else MEMBER_PAGES
        for _ in range(bound):
            checkpoint()
            page = _existing_page(store, endpoint, day, offset, plate)
            if page is None:
                result = call("kaipanla:" + endpoint, [day, plate, offset],
                              lambda: _page(store, captures, endpoint, day, offset, plate))
                if result["status"] != "SAVED":
                    issues.append(dict(source="kaipanla:" + endpoint, trade_date=day, sector_id=plate, reason="REQUEST_FAILED"))
                    return ids
                page = result["receipt"]
            if (set(ids).intersection(page["ids"]) or page["rows"] != len(page["ids"])
                    or page["rows"] < 0 or endpoint == "046" and
                    ((total is not None and total != page["total"]) or page["rows"] == 0)):
                issues.append(dict(source="kaipanla:" + endpoint, trade_date=day, sector_id=plate, reason="INCONSISTENT_PAGES"))
                return ids
            total = page["total"]
            ids.extend(page["ids"])
            offset += page["rows"]
            # Retained collector._collect_until_empty contract: 042 Count is
            # this page's length, while 046 Count is the whole member list.
            # A cached full first page is reusable but never a complete day.
            if endpoint == "042" and page["rows"] < 60 or endpoint == "046" and offset == total:
                return ids
        issues.append(dict(source="kaipanla:" + endpoint, trade_date=day, sector_id=plate, reason="PAGE_LIMIT_INCOMPLETE"))
        return ids

    calendar = sorted(day for day, is_open in _calendar(store, warmup, calendar_end).items() if is_open)
    output = [day for day in calendar if body["date_from"] <= day <= body["date_to"]]
    if not captures or not output or calendar.index(output[0]) < 4:
        issues.append(dict(source="kaipanla", reason="CAPTURES_OR_CALENDAR_UNAVAILABLE"))
        return
    first_index = calendar.index(output[0])
    history = calendar[first_index - 4:calendar.index(output[-1]) + 1]
    boards = set()
    for day in history:
        boards.update(pages("042", day))
    if len(boards) > MAX_BOARDS:
        issues.append(dict(source="kaipanla:046", reason="BOARD_LIMIT_INCOMPLETE"))
    member_day = calendar[first_index - 1]
    for plate in sorted(boards)[:MAX_BOARDS]:
        pages("046", member_day, plate)
