"""Build a dated LEFT/RIGHT reference from already saved daily observations."""
import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from math import isfinite
from pathlib import Path
import re
import sys

import psycopg

from shared_kernel.temp_manager import TempManager, store_file
from ..ledger import encode, fingerprint
from ..simulator import CHINA, decimal, moment
from .inputs import document, read_file, trading_days
from .left_right_features.model_inputs import Point
from .left_right_features.model_inputs_v312 import build_inputs
from .left_right_features.scores import LeftSetup, RightSetup
from .postgres import PostgresLedger
from .settings import Settings

INPUT_SCHEMA = "stock-v4-left-right-input-v1"
OUTPUT_SCHEMA = "stock-v4-left-right-eod-v1"
VERSIONS = {"LEFT": "left_v3.1.2_adj", "RIGHT": "right_v3.1.2_adj"}
STOCK = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
BARS_WINDOW = 80
WEEKLY_LIMIT = 40
MAX_CODES = 50
SOURCE_DAILY_LIMIT = 220


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("expected exact model input")
    try:
        result = float(Decimal(value))
    except (ArithmeticError, OverflowError) as exc:
        raise ValueError("invalid model input") from exc
    # Compounded sector returns legitimately exceed the ledger's decimal scale.
    # Scores use floats; keep their finite-number boundary separate from money.
    if not isfinite(result):
        raise ValueError("non-finite model input")
    return result


def _released(row, as_of):
    return moment(row["available_at"]) <= as_of


def _points(code, rows, day, as_of):
    result = {}
    for row in rows:
        observed = date.fromisoformat(row["trade_date"])
        if observed > day or not _released(row, as_of):
            continue
        if moment(row["available_at"]) < datetime.combine(observed, time(15), CHINA):
            raise ValueError("daily bar was marked available before its close")
        if observed in result:
            raise ValueError(f"duplicate daily input date for {code}")
        result[observed] = Point(observed, *(_number(row.get(key)) for key in
            ("open", "high", "low", "close", "volume_shares", "turnover_ratio",
             "volume_ratio", "adj_factor", "circ_mv_cny")))
    points = [result[when] for when in sorted(result)[-BARS_WINDOW:]]
    return points if points and points[-1].trade_date == day else []


def _weekly(rows, day, as_of):
    available = {}
    for row in rows:
        week_end = date.fromisoformat(row["week_end"])
        if week_end > day or row.get("complete") is not True or not _released(row, as_of):
            continue
        if week_end in available or moment(row["available_at"]) < datetime.combine(week_end, time(15), CHINA):
            raise ValueError("invalid completed weekly input")
        available[week_end] = _number(row["close"])
    return [available[when] for when in sorted(available)[-WEEKLY_LIMIT:]]


def _extra(row, day, as_of):
    if not row or date.fromisoformat(row["trade_date"]) > day or not _released(row, as_of):
        return None
    return _number(row["value"])


def produce(day, calendar, as_of, evidence):
    if day.isoformat() not in calendar or as_of < datetime.combine(day, time(15, 35), CHINA):
        raise ValueError("LEFT/RIGHT requires a completed trading day after 15:35 Beijing time")
    if evidence.get("schema_version") != INPUT_SCHEMA:
        raise ValueError("unsupported LEFT/RIGHT evidence")
    symbols, source = evidence["symbols"], evidence["source"]
    if (not isinstance(symbols, dict) or not 1 <= len(symbols) <= MAX_CODES
            or not isinstance(source, str) or not 1 <= len(source) <= 200):
        raise ValueError("LEFT/RIGHT requires a bounded source and symbol scope")
    items = []
    evidence_hash = fingerprint(evidence)
    for code, asset in sorted(symbols.items()):
        if not STOCK.fullmatch(code):
            raise ValueError("invalid LEFT/RIGHT stock identity")
        points = _points(code, asset["daily"], day, as_of)
        if not points:
            continue
        raw_close = "未知" if points[-1].close is None else f"{points[-1].close:g} 元"
        cost85 = _extra(asset.get("cost85"), day, as_of)
        sector_fact = asset.get("sector_return_5d")
        sector_return = (_extra(sector_fact, day, as_of)
                         if sector_fact and sector_fact.get("trade_date") == day.isoformat() else None)
        weekly = _weekly(asset.get("weekly", []), day, as_of)
        model = build_inputs(points, cost85=cost85, sector_return=sector_return, weekly_closes=weekly)
        latest = next(row for row in asset["daily"] if row["trade_date"] == day.isoformat() and _released(row, as_of))
        structural = {key: str(decimal(latest[key])) if latest.get(key) is not None else None
                      for key in ("close", "return_ratio", "volume_ratio", "turnover_ratio")}
        for side, parts, classifier in (("LEFT", model.left, LeftSetup), ("RIGHT", model.right, RightSetup)):
            score = classifier(**parts).score()
            numeric = round(score.value, 4) if score.value is not None else None
            if numeric is None:
                missing = ["复权因子"] if any(point.adj_factor is None for point in points) else []
                if len(points) < (62 if side == "LEFT" else 70):
                    missing.append("日线窗口")
                if cost85 is None:
                    missing.append("筹码成本")
                if side == "LEFT" and len(weekly) < 26:
                    missing.append("完整周线")
                if side == "LEFT" and sector_return is None:
                    missing.append("板块五日收益")
                if side == "RIGHT" and any(point.turnover_ratio is None for point in points):
                    missing.append("换手率")
                note = f"收盘价 {raw_close} · 评分缺失"
                if missing:
                    note += "（缺" + "、".join(missing) + "）"
            else:
                note = f"收盘价 {raw_close} · {VERSIONS[side]} 评分 {numeric:.4f}"
            items.append(dict(code=code, side=side, note=note, status=score.status, score=numeric,
                              name=asset.get("name"), version_id=VERSIONS[side], **structural,
                              observed_at=datetime.combine(day, time(15), CHINA).isoformat(),
                              published_at=as_of.isoformat(), evidence_sha256=evidence_hash,
                              source=evidence["source"], sectors=asset.get("sectors", []),
                              quality=dict(daily_count=len(points), completed_weeks=len(weekly),
                                           sector_return_available=sector_return is not None),
                              weights=classifier.__dataclass_fields__["weights"].default_factory(),
                              parts={key: value.value for key, value in parts.items()},
                              reasons=sorted({reason for part in parts.values() for reason in part.reasons}
                                             | set(score.reasons))))
    if not items:
        return None
    return dict(schema_version=OUTPUT_SCHEMA, trade_date=day.isoformat(),
                # Original close is the market observation time; as_of records actual availability.
                observed_at=datetime.combine(day, time(15), CHINA).isoformat(), input_as_of=as_of.isoformat(),
                source=source, model_versions=VERSIONS, feature_set="daily_inputs_adj_v1",
                source_batches=evidence.get("source_batches", []), evidence_sha256=evidence_hash, items=items)


def _object(raw):
    return document(raw) if isinstance(raw, (str, bytes)) else raw


def _safe_decimal(value):
    try:
        return decimal(value) if value is not None and not isinstance(value, bool) else None
    except (ValueError, TypeError, ArithmeticError):
        return None


def _completed_week(end, calendar, day):
    monday = end - timedelta(days=end.weekday())
    dates = [monday + timedelta(days=i) for i in range(7)]
    if not all(when in calendar for when in dates):
        return None
    opened = [when for when in dates if calendar[when]]
    return opened if opened and opened[-1] == end and end <= day else None


def _weekly_inputs(daily, saved, calendar, day):
    """Preserve legacy unadjusted weekly.close; never invent an incomplete week."""
    result = {}
    by_day = {date.fromisoformat(row["trade_date"]): row for row in daily}
    for end, row in by_day.items():
        opened = _completed_week(end, calendar, day)
        if not opened or any(when not in by_day for when in opened):
            continue
        close = _safe_decimal(row.get("close"))
        if close is not None and close > 0:
            result[end] = dict(week_end=end.isoformat(), complete=True, close=str(close),
                available_at=max(by_day[when]["available_at"] for when in opened),
                price_basis="LEGACY_UNADJUSTED_CLOSE", source="saved daily week close")
    for row in saved:
        end = date.fromisoformat(row["trade_date"])
        close = _safe_decimal(row.get("close"))
        if (row.get("complete") is True and _completed_week(end, calendar, day)
                and close is not None and close > 0):
            result[end] = dict(week_end=end.isoformat(), complete=True, close=str(close),
                available_at=row["available_at"], source_batch=row["source_batch"],
                price_basis="LEGACY_UNADJUSTED_CLOSE", source="Tushare weekly")
    # A missing week cannot be compressed out of a MACD input sequence.
    expected = [end for end in sorted(calendar) if _completed_week(end, calendar, day)]
    consecutive = []
    for end in reversed(expected[-WEEKLY_LIMIT:]):
        if end not in result:
            break
        consecutive.append(result[end])
    return list(reversed(consecutive))


def _membership_links(pages, codes, day):
    """Latest complete 046 membership dated at/before the requested day.

    Legacy personal_discovery.py _memberships selected Date <= trade_date;
    this preserves the actual membership date rather than relabeling it.
    """
    from ..left_right_backfill_collectors import MEMBER_PAGES, MEMBER_PAGE_SIZE
    member_limit = MEMBER_PAGES * MEMBER_PAGE_SIZE
    identities = defaultdict(list)
    for code in codes:
        identities[code.split(".")[0]].append(code)
    groups = defaultdict(dict)
    for raw_scope, raw_body, collected, batch in pages:
        scope, body = _object(raw_scope), _object(raw_body)
        member_day, plate = scope.get("Date"), scope.get("PlateID")
        if (not isinstance(member_day, str) or member_day > day.isoformat()
                or not isinstance(plate, str) or not re.fullmatch(r"[0-9]{6}", plate)
                or body.get("trade_days") != [member_day]):
            continue
        try:
            date.fromisoformat(member_day)
            offset = int(scope.get("Index", "0"))
        except (ValueError, TypeError):
            continue
        if not 0 <= offset <= member_limit-MEMBER_PAGE_SIZE:
            continue
        key = (plate, member_day)
        prior = groups[key].get(offset)
        if prior is None or (collected, batch) > (prior[1], prior[2]):
            groups[key][offset] = (body, collected, batch)
    latest = {}
    for (plate, member_day), parts in groups.items():
        offset, total, seen, matches, batches, available = 0, None, set(), {}, [], []
        for start, (body, collected, batch) in sorted(parts.items()):
            raw_codes = body.get("codes", [])
            reported = body.get("reported_total")
            if (not isinstance(raw_codes, list) or len(raw_codes) > MEMBER_PAGE_SIZE
                    or type(reported) is not int or not 0 < reported <= member_limit
                    or len(raw_codes) != len(set(raw_codes)) or seen.intersection(raw_codes)
                    or start != offset or total is not None and total != reported):
                break
            total = reported
            seen.update(raw_codes)
            offset += len(raw_codes)
            batches.append(batch)
            available.append(collected)
            for raw_code, name in body.get("matches") or []:
                if len(identities[raw_code]) == 1:
                    matches[identities[raw_code][0]] = (name, batch)
        else:
            if total is not None and offset == total and (plate not in latest or member_day > latest[plate][0]):
                latest[plate] = (member_day, matches, batches, max(available))
    links, names = defaultdict(list), {}
    for plate, (member_day, matches, batches, available) in latest.items():
        for code, (name, batch) in matches.items():
            names[code] = name
            links[code].append(dict(id="kaipanla:"+plate, name=None, membership_date=member_day,
                source="开盘啦 046 已保存板块成员", source_batch=batch, source_batches=batches,
                available_at=available.astimezone(CHINA).isoformat()))
    return links, names


def _sector_inputs(rows, links, days, prior):
    facts, names = {}, {}
    for endpoint, raw_scope, raw_body, collected, source_clock, batch in rows:
        scope, body = _object(raw_scope), _object(raw_body)
        dates = body.get("trade_days")
        if not isinstance(dates, list) or len(dates) != 1 or dates[0] not in days:
            continue
        day = dates[0]
        if endpoint == "042":
            if (scope.get("Date") != day or scope.get("RStart") != "0925"
                    or scope.get("REnd") != "1500"):
                continue
        elif endpoint == "041":
            if (source_clock is None or source_clock.astimezone(CHINA).date().isoformat() != day
                    or source_clock.astimezone(CHINA).time() < time(15)):
                continue
        else:
            continue
        for row in body.get("rows", []):
            if not isinstance(row, list) or len(row) < 4 or not re.fullmatch(r"[0-9]{6}", str(row[0])):
                continue
            sector = "kaipanla:"+str(row[0])
            change, strength = _safe_decimal(row[3]), _safe_decimal(row[2])
            if change is None or change < -100:
                continue
            key, rank = (sector, day), (endpoint == "042", collected, batch)
            if key not in facts or rank > facts[key][0]:
                facts[key] = (rank, change/100, strength, batch, collected)
                names[sector] = str(row[1])
    result = {}
    if len(days) != 5 or prior is None:
        return result, names
    for code, memberships in links.items():
        candidates = []
        for link in memberships:
            sid = link["id"]
            previous = facts.get((sid, prior))
            values = [facts.get((sid, day)) for day in days]
            if previous and previous[2] is not None and all(values):
                candidates.append((sid, previous[2], values, link))
        if not candidates:
            continue
        sid, _, values, link = min(candidates, key=lambda row: (-row[1], row[0]))
        value = Decimal(1)
        for fact in values:
            value *= 1+fact[1]
        available = max([fact[4] for fact in values] + [moment(link["available_at"])])
        result[code] = dict(trade_date=days[-1], value=str(value-1), sector_id=sid,
            available_at=available.astimezone(CHINA).isoformat(), source="kaipanla.041_close_or_042_history.main_sector_previous_day.v1",
            source_batches=sorted({fact[3] for fact in values} | set(link["source_batches"])),
            trading_days=days, membership_date=link["membership_date"])
    return result, names


def _database_context(ledger, day, codes, as_of, *, checkpoint=None):
    """Read shared dated calendar/sector evidence once for a whole market day."""
    codes = list(codes)  # psycopg binds a tuple as a record, not an array
    if checkpoint:
        checkpoint()
    with ledger._connect() as conn:
        calendar_rows = conn.execute("""SELECT c.cal_date,c.is_open,c.batch_id FROM supplier_trade_calendar c
            JOIN supplier_batches b ON b.batch_id=c.batch_id WHERE c.exchange='SSE'
            AND c.cal_date BETWEEN %s AND %s AND b.collected_at<=%s ORDER BY c.cal_date""",
            (day-timedelta(days=330), day+timedelta(days=6-day.weekday()), as_of)).fetchall()
        opened = [when.isoformat() for when, is_open, _ in calendar_rows if is_open and when <= day]
        days, prior = opened[-5:], opened[-2] if len(opened) > 1 else None
        if checkpoint:
            checkpoint()
        board_rows = conn.execute("""SELECT endpoint,scope,body,collected_at,source_clock,batch_id
            FROM supplier_market_batches WHERE provider='kaipanla' AND endpoint IN ('041','042')
            AND collected_at<=%s AND body->'trade_days'->>0=ANY(%s)
            ORDER BY collected_at DESC,batch_id DESC LIMIT 1500""", (as_of, days)).fetchall()
        if checkpoint:
            checkpoint()
        # Keep the same bounded pages and their complete member-code lists. A
        # distinct lookup avoids comparing every member with the whole universe;
        # ordinality preserves duplicate source rows and their original order.
        membership_rows = conn.execute("""WITH requested AS MATERIALIZED (
                SELECT coalesce(jsonb_object_agg(code,true),'{}'::jsonb) AS codes
                FROM (SELECT DISTINCT code FROM unnest(%s::text[]) AS wanted(code) WHERE code IS NOT NULL) unique_codes
            ), pages AS MATERIALIZED (
                SELECT scope,body,collected_at,batch_id FROM supplier_market_batches
                WHERE provider='kaipanla' AND endpoint='046'
                  AND collected_at<=%s AND scope->>'Date'<=%s
                ORDER BY scope->>'Date' DESC,collected_at DESC,batch_id DESC LIMIT 3000
            )
            SELECT page.scope,jsonb_build_object(
                'trade_days',fields.trade_days,'reported_total',fields.reported_total,
                'codes',members.codes,'matches',members.matches),page.collected_at,page.batch_id
            FROM pages page
            CROSS JOIN LATERAL jsonb_to_record(jsonb_path_query_first(
                page.body,'strict $ ? (@.type() == "object")'
            )) AS fields(trade_days jsonb,reported_total jsonb,rows jsonb)
            CROSS JOIN LATERAL (
                SELECT jsonb_agg(member.value->0 ORDER BY member.position) AS codes,
                    jsonb_agg(jsonb_build_array(member.value->>0,member.value->>1)
                        ORDER BY member.position) FILTER (
                            WHERE (SELECT codes FROM requested) ? (member.value->>0)) AS matches
                FROM jsonb_array_elements(coalesce(fields.rows,page.body->'rows'))
                    WITH ORDINALITY AS member(value,position)
            ) members
            ORDER BY page.scope->>'Date' DESC,page.collected_at DESC,page.batch_id DESC""",
            ([code.split('.')[0] for code in codes], as_of, day.isoformat())).fetchall()
        if checkpoint:
            checkpoint()
        reference_names = conn.execute("""SELECT DISTINCT ON(entity_key)
                entity_key,body->>'name',batch_id,observed_at
            FROM supplier_reference_rows WHERE dataset='stock_basic' AND entity_key=ANY(%s)
            AND observed_at<=%s AND jsonb_typeof(body->'name')='string'
            AND btrim(body->>'name')<>''
            ORDER BY entity_key,observed_at DESC,business_date DESC,batch_id DESC""",
            (codes, as_of)).fetchall()
    if checkpoint:
        checkpoint()
    links, names = _membership_links(membership_rows, codes, day)
    name_sources = {}
    for code, name, batch, observed in reference_names:
        if not names.get(code):
            names[code] = name.strip()
            name_sources[code] = dict(source="Tushare stock_basic 已保存股票资料",
                source_batch=batch, available_at=observed.astimezone(CHINA).isoformat())
    prior_links, _ = _membership_links(membership_rows, codes, date.fromisoformat(prior)) if prior else ({}, {})
    sector_values, sector_names = _sector_inputs(board_rows, prior_links, days, prior)
    return dict(calendar={when: opened for when, opened, _ in calendar_rows},
                calendar_batches={batch for _, _, batch in calendar_rows}, links=links, names=names,
                name_sources=name_sources,
                sector_values=sector_values, sector_names=sector_names)


def _database_input(ledger, day, codes, as_of, *, checkpoint=None, context=None):
    codes = list(codes)
    context = context if context is not None else _database_context(ledger, day, codes, as_of, checkpoint=checkpoint)
    if checkpoint:
        checkpoint()
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = conn.execute("""
            SELECT code,trade_date,open,high,low,close,volume_shares,return_ratio,collected_at,batch_id
            FROM (
                SELECT b.*,s.collected_at,
                       row_number() OVER (PARTITION BY code ORDER BY trade_date DESC) AS recent
                FROM supplier_daily_bars b JOIN supplier_batches s USING (batch_id)
                WHERE b.code=ANY(%s) AND b.trade_date<=%s AND s.collected_at<=%s
            ) AS history
            WHERE recent<=%s ORDER BY code,trade_date
        """, (codes, day, as_of, SOURCE_DAILY_LIMIT)).fetchall()
        if checkpoint:
            checkpoint()
        feature_batches = conn.execute("""
            SELECT endpoint,body,collected_at,batch_id
            FROM (
                SELECT endpoint,body,collected_at,batch_id,
                       row_number() OVER (PARTITION BY endpoint,scope->>'ts_code'
                                          ORDER BY collected_at DESC,batch_id DESC) AS recent
                FROM supplier_market_batches
                WHERE provider='tushare' AND endpoint IN ('adj_factor','daily_basic','cyq_perf','weekly')
                  AND scope->>'ts_code'=ANY(%s) AND collected_at<=%s
                  AND scope->>'start_date'<=%s
            ) AS history
            WHERE recent<=3 ORDER BY collected_at,batch_id
        """, (codes, as_of, day.strftime("%Y%m%d"))).fetchall()
        if checkpoint:
            checkpoint()
        feature_rows = conn.execute("""SELECT f.api,f.code,f.trade_date,f.body,b.collected_at,f.batch_id
            FROM supplier_lr_features f JOIN supplier_market_batches b ON b.batch_id=f.batch_id
            WHERE f.code=ANY(%s) AND f.trade_date BETWEEN %s AND %s AND b.collected_at<=%s
            ORDER BY f.api,f.code,f.trade_date""", (codes, day-timedelta(days=400), day, as_of)).fetchall()
        if checkpoint:
            checkpoint()
    symbols, batches, factors, basics, chips = defaultdict(list), set(), {}, {}, {}
    weekly = defaultdict(dict)
    for endpoint, raw, available, batch in feature_batches:
        body = _object(raw)
        code = body["code"]
        if endpoint == "weekly":
            for row in body.get("weekly", []):
                if date.fromisoformat(row["trade_date"]) <= day:
                    weekly[code][row["trade_date"]] = dict(row,
                        available_at=available.astimezone(CHINA).isoformat(), source_batch=batch)
            continue
        features = body["factors"] if endpoint == "adj_factor" else body["features"]
        for when, value in features.items():
            if date.fromisoformat(when) <= day:
                target = {"adj_factor": factors, "daily_basic": basics, "cyq_perf": chips}[endpoint]
                target[(code, when)] = (value, available, batch)
    for endpoint, code, when, raw, available, batch in feature_rows:
        value, when = _object(raw), when.isoformat()
        if endpoint == "weekly":
            previous = weekly[code].get(when)
            if previous is None or moment(previous["available_at"]) <= available:
                weekly[code][when] = dict(value, trade_date=when,
                    available_at=available.astimezone(CHINA).isoformat(), source_batch=batch)
        else:
            target = {"adj_factor": factors, "daily_basic": basics, "cyq_perf": chips}[endpoint]
            previous = target.get((code, when))
            if previous is None or previous[1] <= available:
                target[(code, when)] = (value.get("adj_factor") if endpoint == "adj_factor" else value, available, batch)
    for code, when, opening, high, low, close, volume, change, available, batch in rows:
        trade_date = when.isoformat()
        values = dict(zip(("open", "high", "low", "close", "volume_shares", "return_ratio"),
                          (str(value) if value is not None else None for value in (opening, high, low, close, volume, change))))
        factor = factors.get((code, trade_date))
        basic = basics.get((code, trade_date))
        for feature in (factor, basic):
            if feature:
                available = max(available, feature[1])
                batches.add(feature[2])
        if factor:
            values["adj_factor"] = factor[0]
        if basic:
            values.update({key: basic[0].get(key) for key in ("turnover_ratio", "volume_ratio", "circ_mv_cny")})
        symbols[code].append(dict(values, trade_date=trade_date, available_at=available.astimezone(CHINA).isoformat()))
        batches.add(batch)
    calendar, links, names = (context[key] for key in ("calendar", "links", "names"))
    sector_values, sector_names = (context[key] for key in ("sector_values", "sector_names"))
    evidence = {}
    for code in codes:
        completed = _weekly_inputs(symbols[code], list(weekly[code].values()), calendar, day)
        memberships = links.get(code, [])
        for link in memberships:
            link["name"] = sector_names.get(link["id"])
            batches.update(link["source_batches"])
        evidence[code] = dict(daily=symbols[code], weekly=completed, name=names.get(code), sectors=memberships)
        name_source = context.get("name_sources", {}).get(code)
        if name_source:
            evidence[code]["name_source"] = name_source
            batches.add(name_source["source_batch"])
        if code in sector_values:
            evidence[code]["sector_return_5d"] = sector_values[code]
            batches.update(sector_values[code]["source_batches"])
        batches.update(row["source_batch"] for row in completed if row.get("source_batch"))
        latest_chip = max((when for symbol, when in chips if symbol == code), default=None)
        if latest_chip:
            values, available, batch = chips[(code, latest_chip)]
            evidence[code]["cost85"] = dict(trade_date=latest_chip, value=values.get("cost85"),
                                           available_at=available.astimezone(CHINA).isoformat())
            batches.add(batch)
    batches.update(context["calendar_batches"])
    return dict(schema_version=INPUT_SCHEMA, source="Tushare / 开盘啦已保存盘后资料",
                symbols=evidence, source_batches=sorted(batches))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--calendar", required=True, type=Path)
    parser.add_argument("--as-of", type=moment)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--input", type=Path, help="saved LEFT/RIGHT evidence JSON")
    inputs.add_argument("--env-file", type=Path, help="private test or development database configuration")
    parser.add_argument("--code", action="append", default=[])
    parser.add_argument("--tmp", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        now = datetime.now(CHINA)
        as_of = args.as_of or now.replace(microsecond=0)
        if as_of > now:
            raise ValueError("as-of time is in the future")
        if bool(args.tmp) != bool(args.output_dir):
            raise ValueError("provide both managed temporary and persistent output directories")
        calendar = trading_days(args.calendar)
        if args.env_file:
            settings = Settings.read(args.env_file)
            if settings.env not in ("test", "development") or not 1 <= len(args.code) <= MAX_CODES:
                raise ValueError("database intake requires isolated configuration and 1–50 stock codes")
            if len(set(args.code)) != len(args.code) or any(not STOCK.fullmatch(code) for code in args.code):
                raise ValueError("invalid or duplicate stock code")
            ledger = PostgresLedger(settings, "FROZEN" if settings.env == "test" else "LIVE")
            evidence = _database_input(ledger, args.day, args.code, as_of)
        else:
            if args.code:
                raise ValueError("saved evidence input already specifies the stock universe")
            evidence = read_file(args.input)
        archive = produce(args.day, calendar, as_of, evidence)
        if archive is None:
            print(encode({"status": "NO_SAVED_CLOSE", "trade_date": args.day.isoformat()}))
        elif args.output_dir:
            output = args.output_dir
            if not output.is_absolute() or output.resolve() != output or not output.is_dir():
                raise ValueError("output requires a persistent absolute directory")
            manager = TempManager(args.tmp, persistent=output)
            path = store_file(manager, output, "left-right-eod-"+fingerprint(archive)[:24]+".json",
                              encode(archive).encode())
            print(encode({"status": "ARCHIVED", "path": path, "rows": len(archive["items"]),
                          "ready": sum(row["status"] == "READY" for row in archive["items"])}))
        else:
            print(encode(archive))
    except (ValueError, TypeError, KeyError, OSError, psycopg.Error) as exc:
        print(f"LEFT/RIGHT archive: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
