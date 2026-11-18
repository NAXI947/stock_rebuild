"""Bounded, worker-owned Shanghai/Shenzhen A-share turnover snapshots.

TickFlow amounts are CNY. Minute bars keep the legacy 09:30 opening-point
contract; a missing bar invalidates only cutoffs that need that bar. Daily
timestamps remain the provider's timestamps, separate from the 15:00 label.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
from pathlib import Path
from urllib.parse import urlencode

import psycopg

from ..ledger import fingerprint
from ..simulator import CHINA
from .bulk_collect import RequestLog
from .http_json import get_json, ProviderHTTPError
from .inputs import trading_days
from .market_bootstrap import ProbeError, save, tickflow_config

LOG = logging.getLogger(__name__)
SYMBOLS = ("000002.SH", "399107.SZ")
SLOTS = ("09:35", "10:35", "11:35", "13:05", "14:05", "15:00")
URL = "https://api.tickflow.org/v1/klines/batch"
SOURCE = "TickFlow · 上证A股指数 + 深证A股指数"
CONTRACT = "tickflow-shsz-a-cny-0930-minute-end-v1"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _now():
    return datetime.now(CHINA)


def _aware(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return stamp.astimezone(CHINA)


def _amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _stamp(value):
    if type(value) is not int or value <= 0:
        raise ValueError("millisecond timestamp required")
    stamp = (EPOCH + timedelta(milliseconds=value)).astimezone(CHINA)
    return stamp


def cutoff(day, slot):
    return datetime.combine(day, time.fromisoformat(slot), CHINA)


def minute_labels(day, slot):
    """Provider minute-end labels, including the separate opening point."""
    point, end, result = cutoff(day, "09:30"), cutoff(day, slot), []
    while point <= end:
        if time(9, 30) <= point.time() <= time(11, 30) or time(13, 1) <= point.time() <= time(15):
            result.append(point)
        point += timedelta(minutes=1)
    return result


def _series(body, received, maximum):
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or set(data) - set(SYMBOLS):
        raise ProbeError("turnover response symbol scope mismatch")
    parsed = {}
    for symbol in SYMBOLS:
        series = data.get(symbol)
        if not isinstance(series, dict):
            parsed[symbol] = None
            continue
        stamps, amounts = series.get("timestamp"), series.get("amount")
        if (not isinstance(stamps, list) or not isinstance(amounts, list)
                or len(stamps) != len(amounts) or len(stamps) > maximum):
            parsed[symbol] = None
            continue
        points = {}
        for raw, amount in zip(stamps, amounts):
            try:
                stamp = _stamp(raw)
            except (ValueError, OverflowError):
                points = None
                break
            # A dated bad point stays local to its day/cutoff. Future amounts
            # are never used, while older valid dates in the same batch survive.
            value = _amount(amount) if stamp <= received else None
            if stamp in points:
                # Even identical duplicates need source resolution, not summing.
                points[stamp] = None
            else:
                points[stamp] = value
        parsed[symbol] = points
    return parsed


def _point(amount=None, observed=None, reason="指数成交额待采集"):
    return {"amount_cny": str(amount) if amount is not None else None,
            "observed_at": observed.isoformat() if observed else None,
            "reason": None if amount is not None else reason}


def normalize_minutes(body, day, received):
    day = day if isinstance(day, date) else date.fromisoformat(day)
    received = _aware(received)
    parsed, slots = _series(body, received, 500), {}
    for slot in SLOTS[:-1]:
        target = cutoff(day, slot)
        if target > received:
            slots[slot] = _point(reason="未到采样时点")
            continue
        expected = minute_labels(day, slot)
        totals = []
        for symbol in SYMBOLS:
            points = parsed[symbol]
            if points is None:
                break
            relevant = {stamp: amount for stamp, amount in points.items()
                        if stamp.date() == day and stamp <= target
                        and (time(9, 30) <= stamp.time() <= time(11, 30)
                             or time(13, 1) <= stamp.time() <= time(15))}
            if set(relevant) != set(expected) or any(v is None for v in relevant.values()):
                break
            totals.append(sum(relevant.values(), Decimal(0)))
        slots[slot] = (_point(sum(totals), expected[-1]) if len(totals) == len(SYMBOLS)
                       else _point(reason="两市指数分钟数据未齐全"))
    return {"trade_date": day.isoformat(), "period": "1m", "slots": slots,
            "contract": CONTRACT, "collected_at": received.isoformat()}


def normalize_daily(body, days, received):
    days = [day if isinstance(day, date) else date.fromisoformat(day) for day in days]
    if not 1 <= len(days) <= 6 or len(set(days)) != len(days):
        raise ValueError("turnover daily range must contain one to six trading days")
    received, result = _aware(received), []
    parsed = _series(body, received, 6)
    for day in days:
        totals, observations = [], []
        for symbol in SYMBOLS:
            series = parsed[symbol]
            points = [(stamp, amount) for stamp, amount in (series or {}).items() if stamp.date() == day]
            if len(points) != 1 or points[0][1] is None:
                break
            observations.append(points[0][0])
            totals.append(points[0][1])
        if cutoff(day, "15:00") > received:
            point = _point(reason="尚未收盘")
        elif len(totals) == len(SYMBOLS):
            point = _point(sum(totals), max(observations))
        else:
            point = _point(reason="两市指数收盘数据未齐全")
        result.append({"trade_date": day.isoformat(), "period": "1d", "slots": {"15:00": point},
                       "contract": CONTRACT, "collected_at": received.isoformat()})
    return result


def _document(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


def _with_lunch_slot(record):
    """Derive the added cutoff from saved raw bars without writes or collection."""
    if (record.get("period") != "1m" or record.get("contract") != CONTRACT
            or "11:35" in record.get("slots", {}) or not isinstance(record.get("raw_response"), dict)):
        return record
    try:
        source_hash = fingerprint(record["raw_response"])
        if record.get("source_sha256", source_hash) != source_hash:
            return record
        point = normalize_minutes(record["raw_response"], record["trade_date"],
                                  record["collected_at"])["slots"]["11:35"]
    except (ValueError, TypeError, KeyError, OverflowError, ProbeError):
        return record
    point.update(collected_at=record["collected_at"], source_sha256=source_hash)
    return {**record, "slots": {**record.get("slots", {}), "11:35": point}}


def _read(ledger):
    """Bound result cardinality to six calendar days and twelve daily records."""
    with ledger._connect() as conn:
        row = conn.execute("""SELECT body FROM supplier_market_batches
            WHERE provider='tickflow' AND endpoint='market-turnover'
              AND scope->>'purpose'='market-turnover' AND scope->>'kind'='calendar'
            ORDER BY collected_at DESC,batch_id DESC LIMIT 1""").fetchone()
        if not row:
            return [], {}
        days = _document(row[0])["days"][-6:]
        records = conn.execute("""SELECT DISTINCT ON (scope->>'trade_date',scope->>'period') body
            FROM supplier_market_batches WHERE provider='tickflow' AND endpoint='market-turnover'
              AND scope->>'purpose'='market-turnover' AND scope->>'kind'='snapshot'
              AND scope->>'trade_date'=ANY(%s)
            ORDER BY scope->>'trade_date',scope->>'period',collected_at DESC,batch_id DESC""", (days,)).fetchall()
    indexed = {}
    for (raw,) in records:
        record = _with_lunch_slot(_document(raw))
        indexed[(record["trade_date"], record["period"])] = record
    return days, indexed


def presentation(days, records, now):
    """Read-only frontend contract; fixed calendar baselines never skip gaps."""
    now = _aware(now)
    days = sorted(set(day for day in days if date.fromisoformat(day) <= now.date()))[-6:]
    if not days:
        return {"scope": "沪深A股", "source": SOURCE, "collected_at": None,
                "trade_date": None, "default_slot": "15:00", "slots": [], "status": "待采集"}
    base = days[-1]

    def point(day, slot):
        record = records.get((day, "1d" if slot == "15:00" else "1m"), {})
        value = record.get("slots", {}).get(slot, _point())
        if record and record.get("contract") != CONTRACT:
            value = _point(reason="成交额来源口径待核对")
        if cutoff(date.fromisoformat(day), slot) > now:
            value = _point(reason="未到采样时点")
        return {"trade_date": day, **value,
                "collected_at": value.get("collected_at", record.get("collected_at"))}

    slots = []
    for slot in SLOTS:
        current = point(base, slot)
        previous = point(days[-2], slot) if len(days) > 1 else {}
        baseline = [point(day, slot) for day in days[:-1]]
        values = [_amount(p.get("amount_cny")) for p in baseline]
        valid = [value for value in values if value is not None]
        mean = sum(valid, Decimal(0)) / 5 if len(values) == 5 and len(valid) == 5 else None
        amount, prior = _amount(current.get("amount_cny")), _amount(previous.get("amount_cny"))
        ratio = lambda denominator: (str(amount / denominator - 1)
                                     if amount is not None and denominator is not None and denominator > 0 else None)
        slots.append({"time": slot, "label": slot + (" 收盘" if slot == "15:00" else " 时段"),
                      "current_amount_cny": current["amount_cny"], "observed_at": current["observed_at"],
                      "collected_at": current.get("collected_at"), "reason": current.get("reason"),
                      "previous_ratio": ratio(prior), "average5_ratio": ratio(mean),
                      "average5_amount_cny": str(mean) if mean is not None else None,
                      "baseline_count": len(valid), "points": [point(day, slot) for day in days[-5:]]})
    valid_slots = [slot["time"] for slot in slots if slot["current_amount_cny"] is not None]
    due_slots = [slot for slot in SLOTS if cutoff(date.fromisoformat(base), slot) <= now]
    collected = [record["collected_at"] for (day, _), record in records.items() if day in days]
    return {"scope": "沪深A股", "source": SOURCE, "source_contract": CONTRACT,
            "collected_at": max(collected, default=None), "trade_date": base,
            "default_slot": (valid_slots or due_slots or ["15:00"])[-1], "slots": slots}


def load(ledger):
    days, records = _read(ledger)
    return presentation(days, records, _now())


class TurnoverRefresh:
    """One physical request per tick; durable budgets also survive worker restarts."""
    def __init__(self, ledger, tickflow_config_path, calendar_path):
        self.ledger = ledger
        self.config = Path(tickflow_config_path)
        self.calendar = Path(calendar_path)
        self.due = 0
        self.status = None

    def _attempts(self, run):
        with self.ledger._connect() as conn:
            rows = conn.execute("SELECT source,request_key FROM collection_requests WHERE run_key=%s", (run,)).fetchall()
        return set(rows)

    def _last_current_attempt(self, run):
        with self.ledger._connect() as conn:
            row = conn.execute("""SELECT max(started_at) FROM collection_requests
                WHERE run_key=%s AND source='tickflow:turnover:current'""", (run,)).fetchone()
        return _aware(row[0]) if row and row[0] else None

    def _history_attempt_times(self, run):
        with self.ledger._connect() as conn:
            rows = conn.execute("""SELECT request_key,started_at FROM collection_requests
                WHERE run_key=%s AND source='tickflow:turnover:history'""", (run,)).fetchall()
        return {key: _aware(started) for key, started in rows}

    def _request(self, period, days, now):
        first, last = map(date.fromisoformat, (days[0], days[-1]))
        start = cutoff(first, "00:00" if period == "1d" else "09:30")
        end = min(cutoff(last, "15:00" if period == "1d" else "14:05"), now)
        query = {"symbols": ",".join(SYMBOLS), "period": period, "count": 6 if period == "1d" else 500,
                 "start_time": int(start.timestamp() * 1000), "end_time": int(end.timestamp() * 1000), "adjust": "none"}
        body = get_json(URL + "?" + urlencode(query),
                        headers=(("x-api-key", tickflow_config(self.config)),), timeout=8)
        received = _now()
        snapshots = (normalize_daily(body, days, received) if period == "1d"
                     else [normalize_minutes(body, first, received)])
        # Merge prior valid cutoffs when a newer partial response loses a field.
        _, previous = _read(self.ledger)
        for record in snapshots:
            record.update(raw_response=body, source_sha256=fingerprint(body))
            for value in record["slots"].values():
                value.update(collected_at=received.isoformat(), source_sha256=record["source_sha256"])
            older = previous.get((record["trade_date"], period), {})
            prior = older.get("slots", {}) if older.get("contract") == CONTRACT else {}
            for slot, value in record["slots"].items():
                if value["amount_cny"] is None and prior.get(slot, {}).get("amount_cny") is not None:
                    record["slots"][slot] = prior[slot]
            scope = {"purpose": "market-turnover", "kind": "snapshot", "trade_date": record["trade_date"], "period": period}
            observations = [p["observed_at"] for p in record["slots"].values() if p["observed_at"]]
            save(self.ledger, "tickflow", "market-turnover", scope, record, len(record["slots"]),
                 max(observations, default=None), received)
        return {"period": period, "days": days, "collected_at": received.isoformat(),
                "available": sum(p["amount_cny"] is not None for r in snapshots for p in r["slots"].values())}

    def tick(self, monotonic):
        if monotonic < self.due:
            return self.status
        self.due = monotonic + 900
        try:
            now = _now()
            days = [day for day in trading_days(self.calendar)
                    if cutoff(date.fromisoformat(day), "09:35") <= now][-6:]
            if not days:
                self.status = {"state": "WAITING_CALENDAR"}
                return self.status
            stored_days, records = _read(self.ledger)
            if stored_days != days:
                save(self.ledger, "tickflow", "market-turnover", {"purpose": "market-turnover", "kind": "calendar"},
                     {"days": days}, len(days), None, now)
            run = "market-turnover:" + now.date().isoformat()
            attempted = self._attempts(run)
            last_current = self._last_current_attempt(run)
            history_times = self._history_attempt_times(run)
            tasks = []
            cooling = False
            # One daily range after the latest included session has closed.
            if cutoff(date.fromisoformat(days[-1]), "15:00") <= now:
                if any(records.get((day, "1d"), {}).get("slots", {}).get("15:00", {}).get("amount_cny") is None for day in days):
                    tasks.append(("1d", days, "daily", {"days": days}, 1))
            for day in reversed(days):
                target = date.fromisoformat(day)
                due = [slot for slot in SLOTS[:-1] if cutoff(target, slot) <= now]
                prior = records.get((day, "1m"), {}).get("slots", {})
                if not due or all(prior.get(slot, {}).get("amount_cny") is not None for slot in due):
                    continue
                current = target == now.date() and now.time() <= time(15, 30)
                if current and last_current and now < last_current + timedelta(seconds=900):
                    cooling = True
                    continue
                scope = {"day": day}
                if current:
                    scope["bucket"] = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0).isoformat()
                elif ("tickflow:turnover:history", fingerprint(scope)) in attempted:
                    first_attempt = history_times.get(fingerprint(scope))
                    if not first_attempt or now < first_attempt + timedelta(seconds=900):
                        cooling = True
                        continue
                    # Six first attempts plus one delayed repair per day. Both
                    # failures and partial saved responses consume this budget.
                    scope["retry"] = 1
                tasks.append(("1m", [day], "current" if current else "history", scope, 32 if current else 12))
            for period, selected, kind, scope, limit in tasks:
                source = "tickflow:turnover:" + kind
                if (source, fingerprint(scope)) in attempted:
                    continue
                # Move to the next task on the next worker tick, even on failure.
                self.due = monotonic + 1
                budget = RequestLog(self.ledger, run)
                result = budget.call(source, scope, lambda: self._request(period, selected, now), limit=limit)
                self.status = {"state": "STORED", "new_requests": budget.new_calls, **result}
                return self.status
            self.status = {"state": "WAITING_RETRY" if tasks or cooling else "UP_TO_DATE", "trade_date": days[-1]}
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError, ProbeError, ProviderHTTPError, psycopg.Error) as exc:
            self.status = {"state": "FAILED", "error": type(exc).__name__}
            LOG.warning("turnover refresh failed: %s", type(exc).__name__)
        return self.status
