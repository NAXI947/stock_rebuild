"""Read saved Kaipanla rankings and explicitly linked LEFT/RIGHT history."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import parse_qs

from .adapter import BOARD
from .simulator import CHINA, moment
from .stage2.inputs import read_file

MAX_DAYS = 20
THRESHOLDS = {"LEFT": Decimal(75), "RIGHT": Decimal(70)}


def _query(query):
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("invalid hot-sector query")
    args = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3, errors="strict")
    if set(args) - {"days", "limit", "q"} or any(len(values) != 1 for values in args.values()):
        raise ValueError("invalid hot-sector query fields")
    numbers = [args.get(key, [default])[0] for key, default in (("days", "5"), ("limit", "10"))]
    if any(not value.isascii() or not value.isdigit() or len(value) > 2 for value in numbers):
        raise ValueError("invalid hot-sector bounds")
    days, limit = map(int, numbers)
    q = args.get("q", [""])[0].strip()
    if not 1 <= days <= MAX_DAYS or not 1 <= limit <= 50 or len(q) > 80:
        raise ValueError("invalid hot-sector bounds or search")
    return days, limit, q


def _number(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    if not result.is_finite() or len(result.as_tuple().digits) > 38 or abs(result.as_tuple().exponent) > 100:
        return None
    return result


def _value(value, divisor=1):
    number = _number(value)
    return str(number / divisor) if number is not None else None


def _object(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError("expected saved hot-sector object")
    return value


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid hot-sector date")
    return date.fromisoformat(value).isoformat()


def _clock(value):
    value = value.isoformat() if isinstance(value, datetime) else value
    return moment(value).isoformat() if value else None


def _saved_days(ledger, issues):
    # Suppliers are shared within the environment database. Select one logical
    # collection round per date, so partial pages never mix different rounds.
    with ledger._connect() as conn:
        rows = conn.execute("""
            WITH rounds AS (
                SELECT run_key,result::jsonb->>'day' AS day,max(started_at) AS started
                FROM collection_requests WHERE source='kaipanla:041' AND status='SAVED'
                GROUP BY run_key,result::jsonb->>'day'
            ), selected AS (
                SELECT DISTINCT ON (day) run_key,day,started FROM rounds
                WHERE day ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                ORDER BY day DESC,started DESC,run_key DESC LIMIT 20
            )
            SELECT s.day,s.run_key,b.scope,b.body,b.source_clock,b.collected_at,b.batch_id
            FROM selected s JOIN collection_requests r ON r.run_key=s.run_key
                AND r.source='kaipanla:041' AND r.status='SAVED' AND r.result::jsonb->>'day'=s.day
            JOIN supplier_market_batches b ON b.batch_id=r.result::jsonb->>'batch'
                AND b.provider='kaipanla' AND b.endpoint='041'
            ORDER BY s.day DESC,b.collected_at,b.batch_id LIMIT 1000
        """).fetchall()
    return _parse_pages(rows, issues)


def _saved_historical_days(ledger, issues):
    # The manual collector requests the same Type=1/Order=1 ranking as live 041.
    # Historical Day and the explicit REnd establish the closed observation
    # window; the response Time remains the separate collection clock.
    with ledger._connect() as conn:
        rows = conn.execute("""
            WITH pages AS (
                SELECT DISTINCT ON (scope->>'Date',scope->>'Index')
                    scope->>'Date' AS day,scope,body,collected_at,batch_id
                FROM supplier_market_batches WHERE provider='kaipanla' AND endpoint='042'
                    AND scope->>'Type'='1' AND scope->>'Order'='1'
                    AND scope->>'RStart'='0925' AND scope->>'REnd'='1500'
                    AND scope->>'Date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                ORDER BY scope->>'Date' DESC,scope->>'Index',collected_at DESC,batch_id DESC
            ), days AS (SELECT DISTINCT day FROM pages ORDER BY day DESC LIMIT 20)
            SELECT p.day,'historical-close',p.scope,p.body,p.day||'T15:00:00+08:00',
                p.collected_at,p.batch_id FROM pages p JOIN days d ON d.day=p.day
            ORDER BY p.day DESC,p.collected_at,p.batch_id LIMIT 1000
        """).fetchall()
    return _parse_pages(rows, issues, historical=True)


def _parse_pages(rows, issues, *, historical=False):
    dates = {}
    seen_batches = set()
    for day, run, raw_scope, raw_body, source, collected, batch in rows:
        if batch in seen_batches:
            continue
        seen_batches.add(batch)
        try:
            day = _day(day)
            scope, body = _object(raw_scope), _object(raw_body)
            if historical and (body.get("trade_days") != [day] or scope.get("Date") != day
                    or scope.get("RStart") != "0925" or scope.get("REnd") != "1500"
                    or scope.get("Type") != "1" or scope.get("Order") != "1"):
                raise ValueError("historical ranking scope mismatch")
            index = str(scope.get("Index", ""))
            if not index.isascii() or not index.isdigit() or not 0 <= int(index) <= 10000:
                raise ValueError("invalid saved page index")
            source, collected = _clock(source), _clock(collected)
            if not source or source[:10] != day or not collected:
                raise ValueError("saved page clock mismatch")
            records = body["rows"]
            if not isinstance(records, list) or len(records) > 1000:
                raise ValueError("invalid saved page rows")
            page_size = int(scope.get("st", "60")) if historical else None
            if historical and (not 1 <= page_size <= 1000 or len(records) > page_size):
                raise ValueError("invalid historical page size")
        except (ValueError, TypeError, KeyError):
            issues.add("SAVED_BOARD_PAGE_INVALID")
            continue
        target = dates.setdefault(day, dict(trade_date=day,
            source="开盘啦 042 已保存收盘强度榜单" if historical else "开盘啦 041 已保存榜单",
            source_observed_at=source, collected_at=collected, source_batches=[], sectors={}, totals=set(),
            page_lengths={}, valid_rows=True))
        target["source_observed_at"] = max(source, target["source_observed_at"])
        target["collected_at"] = max(collected, target["collected_at"])
        target["source_batches"].append(batch)
        total = body.get("reported_total")
        if historical:
            target["page_lengths"][int(index)] = (len(records), page_size)
        elif type(total) is int and 0 < total <= 10000:
            target["totals"].add(total)
        for offset, row in enumerate(records):
            if (not isinstance(row, list) or len(row) < 4 or not re.fullmatch(r"[0-9]{6}", str(row[0]))
                    or not isinstance(row[1], str) or not 1 <= len(row[1]) <= 100):
                issues.add("SAVED_BOARD_ROW_INVALID")
                target["valid_rows"] = False
                continue
            sid, rank = "kaipanla:" + str(row[0]), int(index) + offset + 1
            if sid in target["sectors"] and target["sectors"][sid]["rank"] <= rank:
                target["valid_rows"] = False
                continue
            target["sectors"][sid] = dict(sector_id=sid, name=row[1], rank=rank,
                strength_raw=_value(row[2]), net_cny=_value(row[6]) if len(row) > 6 else None,
                return_ratio=_value(row[3], 100), observed_at=source)
    result = {}
    for day, body in dates.items():
        totals = body.pop("totals")
        page_lengths, valid_rows = body.pop("page_lengths"), body.pop("valid_rows")
        body["sectors"] = sorted(body["sectors"].values(), key=lambda item: (item["rank"], item["sector_id"]))
        total = next(iter(totals)) if len(totals) == 1 else None
        if historical:
            # 042 Count is a page length, including in old cached bodies named
            # reported_total. Only a contiguous 0..N sequence with a short/empty
            # last page establishes the size of the complete historical list.
            offset, terminal = 0, False
            for start, (count, size) in sorted(page_lengths.items()):
                if start != offset or terminal:
                    valid_rows = False
                    break
                offset += count
                terminal = count < size
            complete = valid_rows and terminal and offset == len(body["sectors"])
            total = offset if complete else None
            if not complete:
                issues.add("SAVED_BOARD_PAGES_INCOMPLETE")
        body["coverage"] = dict(loaded=len(body["sectors"]), reported_total=total)
        if len(totals) > 1:
            issues.add("SAVED_BOARD_TOTAL_CONFLICT")
        result[day] = body
    return result


def _historical_days(path, issues):
    if path is None:
        return {}
    try:
        data = read_file(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        issues.add("HOT_HISTORY_UNAVAILABLE")
        return {}
    if data.get("schema_version") != "stock-v4-hot-sector-history-v1" or not isinstance(data.get("days"), list) or len(data["days"]) > 120:
        issues.add("HOT_HISTORY_INVALID")
        return {}
    result = {}
    for body in data["days"]:
        try:
            if not isinstance(body, dict):
                raise ValueError("invalid archive day")
            day = _day(body["trade_date"])
            observed, collected = _clock(body["source_observed_at"]), _clock(body["collected_at"])
            if not observed or observed[:10] != day or not collected:
                raise ValueError("invalid archive clock")
            source, batches, rows = body["source"], body["source_batches"], body["sectors"]
            if (not isinstance(source, str) or not 1 <= len(source) <= 200 or not isinstance(batches, list)
                    or not 1 <= len(batches) <= 100 or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in batches)
                    or not isinstance(rows, list) or len(rows) > 10000):
                raise ValueError("invalid archive source")
            sectors = {}
            for row in rows:
                sid, name, rank = row["sector_id"], row["name"], row["rank"]
                if (not isinstance(sid, str) or not BOARD.fullmatch(sid) or not isinstance(name, str)
                        or not 1 <= len(name) <= 100 or type(rank) is not int or not 1 <= rank <= 10000):
                    raise ValueError("invalid archive sector")
                stamp = _clock(row.get("observed_at"))
                if stamp and stamp[:10] != day:
                    raise ValueError("archive row clock mismatch")
                if sid in sectors:
                    raise ValueError("duplicate archive sector")
                sectors[sid] = dict(sector_id=sid, name=name, rank=rank, strength_raw=_value(row.get("strength_raw")),
                    net_cny=_value(row.get("net_cny")), return_ratio=_value(row.get("return_ratio")), observed_at=stamp)
            coverage = body.get("coverage", {})
            total = coverage.get("reported_total") if isinstance(coverage, dict) else None
            total = total if type(total) is int and len(sectors) <= total <= 10000 else None
            candidate = dict(trade_date=day, source=source, source_observed_at=observed, collected_at=collected,
                source_batches=batches, coverage=dict(loaded=len(sectors), reported_total=total),
                sectors=sorted(sectors.values(), key=lambda row: (row["rank"], row["sector_id"])))
            if day not in result or candidate["source_observed_at"] > result[day]["source_observed_at"]:
                result[day] = candidate
        except (ValueError, TypeError, KeyError):
            issues.add("HOT_HISTORY_DAY_INVALID")
    return result


def _groups(archive, sid, day):
    groups = {"LEFT": [], "RIGHT": []}
    for row in archive.get("items", []) if archive else []:
        family, score = row.get("family"), _number(row.get("setup_score"))
        if family not in groups or row.get("trade_date") != day or score is None or score < THRESHOLDS[family] or row.get("status") != "READY":
            continue
        relations = row.get("sectors", [])
        # Legacy discovery reuses the latest complete membership available by
        # the score date. Keep its actual date in the row for the UI to display.
        if any(link.get("id") == sid and isinstance(link.get("membership_date"), str)
               and link["membership_date"] <= day and link.get("source_batch") for link in relations):
            groups[family].append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: (_number(row["setup_score"]).copy_negate(), row["ts_code"]))
    return {family: rows[:5] for family, rows in groups.items()}, {family: len(rows) for family, rows in groups.items()}


def read_info(ledger, query="", *, eod_file=None, lr_history_file=None, hot_history_file=None, now=None):
    from .left_right_info import load_history, load_sector_groups
    days_count, limit, q = _query(query)
    now = now or datetime.now(CHINA)
    today = now.astimezone(CHINA).date().isoformat()
    issues = set()
    dates = _historical_days(hot_history_file, issues)
    saved = _saved_historical_days(ledger, issues)
    for day, body in _saved_days(ledger, issues).items():
        if day not in saved or body["source_observed_at"] >= saved[day]["source_observed_at"]:
            saved[day] = body
    for day, body in saved.items():
        if day not in dates or body["source_observed_at"] >= dates[day]["source_observed_at"]:
            dates[day] = body
    available = sorted((day for day in dates if day <= today), reverse=True)[:MAX_DAYS]
    latest = available[0] if available else None
    # Latest trading date also remains sector-only over weekends and holidays.
    historical = [day for day in available[:days_count] if day != latest and day != today]
    selected_rows = {day: [row for row in dates[day]["sectors"] if not q or q.casefold() in
                          (row["name"] + " " + row["sector_id"].split(":", 1)[-1]).casefold()][:limit]
                     for day in available[:days_count]}
    indexed, indexed_days = load_sector_groups(ledger, {day: [row["sector_id"] for row in selected_rows[day]] for day in historical})
    legacy_days = [day for day in historical if day not in indexed_days]
    history = (load_history(ledger, eod_file=eod_file, history_file=lr_history_file, dates=legacy_days)
               if legacy_days else {"archives": [], "issues": []})
    issues.update(history["issues"])
    archives = {item["trade_date"]: item for item in history["archives"]}
    result = []
    for day in available[:days_count]:
        body = dates[day]
        stock_mode = "HISTORICAL_LR" if day in historical else "TODAY_SECTORS_ONLY"
        archive = archives.get(day)
        selected = []
        for row in selected_rows[day]:
            groups, totals = (indexed[day][row["sector_id"]] if day in indexed_days else
                             _groups(archive, row["sector_id"], day) if stock_mode == "HISTORICAL_LR"
                             else ({"LEFT": [], "RIGHT": []}, {"LEFT": 0, "RIGHT": 0}))
            selected.append({**row, "stock_groups": groups, "group_totals": totals})
        result.append({key: value for key, value in body.items() if key != "sectors"} | {
            "stock_mode": stock_mode, "score_trade_date": day if (archive or day in indexed_days) and stock_mode == "HISTORICAL_LR" else None,
            "top_sectors": selected})
    return dict(today_shanghai=today, latest_trade_date=latest, available_dates=available, days_requested=days_count,
                limit=limit, q=q, issues=sorted(issues), days=result)
