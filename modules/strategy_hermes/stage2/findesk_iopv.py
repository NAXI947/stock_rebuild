"""Optional Findesk IOPV for one registered ETF batch."""
from datetime import datetime, timedelta
from itertools import islice
import os
from pathlib import Path
import re
import stat
from urllib.request import Request

from ..ledger import encode, fingerprint
from ..simulator import CHINA, CODE, decimal, moment, session
from .inputs import document
from .market_bootstrap import ProbeError, fetch, save

URL = "https://ffd.findesk.cn/api/query"
SOURCE = "findesk:iopv"
ENDPOINT = "realtime_quote"
MAX_CODES = 100
MAX_CONFIG_BYTES = 4096
SOURCE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}")


def _credential(config):
    path = Path(config)
    if not path.is_absolute() or path.resolve() != path:
        raise ProbeError("Findesk config requires a private absolute file")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ProbeError("Findesk config requires a private absolute file")
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("Findesk config exceeds size bound")
        key = document(raw)["api_key"]
        if not isinstance(key, str) or not key.strip() or any(not 32 < ord(c) < 127 for c in key.strip()):
            raise ValueError("invalid Findesk key")
    except (OSError, KeyError, ValueError):
        raise ProbeError("Findesk configuration unavailable") from None
    return key.strip()


def _scope(codes):
    if isinstance(codes, str):
        raise ValueError("Findesk requires a collection of ETF codes")
    values = list(islice(codes, MAX_CODES + 1))
    if not 1 <= len(values) <= MAX_CODES or any(
        not isinstance(code, str) or not CODE.fullmatch(code) for code in values
    ):
        raise ValueError("Findesk requires 1–100 Shanghai/Shenzhen ETF codes")
    return sorted(set(values))


def _reading(code, value, original_time, received):
    row = {"etf": code, "iopv": None, "observed_at": None,
           "source_time": original_time if isinstance(original_time, str) and len(original_time) <= 64 else None,
           "source": "findesk"}
    if not row["source_time"] or not SOURCE_TIME.match(row["source_time"]):
        return row
    try:
        observed = datetime.fromisoformat(original_time)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=CHINA)
        observed = observed.astimezone(CHINA)
        if observed > received + timedelta(seconds=10):
            return row
        row["observed_at"] = observed.isoformat()
    except (ValueError, OverflowError):
        return row
    try:
        iopv = decimal(value)
        if iopv > 0:
            row["iopv"] = str(iopv)
    except ValueError:
        pass
    return row


def _normalize(body, requested, received):
    if type(body.get("code")) is not int or body["code"] != 200:
        raise ProbeError("Findesk rejected the request")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ProbeError("invalid Findesk response")
    columns = [data.get(key) for key in ("code", "iopv", "time")]
    if (not all(isinstance(column, list) for column in columns)
            or len({len(column) for column in columns}) != 1 or len(columns[0]) > len(requested)):
        raise ProbeError("invalid Findesk response columns")
    requested_set, readings = set(requested), {}
    for code, value, original_time in zip(*columns, strict=True):
        if not isinstance(code, str) or code not in requested_set or code in readings:
            raise ProbeError("unexpected or duplicate ETF in Findesk response")
        readings[code] = _reading(code, value, original_time, received)
    return [readings.get(code) or _reading(code, None, None, received) for code in requested]


def _request(config, scope):
    return Request(URL, method="POST", data=encode({
        "function": ENDPOINT, "codes": ",".join(scope), "indicators": "iopv;time",
    }).encode(), headers={
        "Authorization": "Bearer " + _credential(config),
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "stock-v4-findesk-client/1.0",
    })


def _query(request, scope):
    body = fetch(request, timeout=15)
    received = datetime.now(CHINA)
    return _normalize(body, scope, received), received


def fetch_iopv(config, codes):
    """Fetch once. The calling collector owns the shared request budget and storage."""
    scope = _scope(codes)
    rows, _ = _query(_request(config, scope), scope)
    return rows


def collect(store, budget, config, mapping, market, now):
    """Collect up to 100 eligible mapped ETF readings in one shared request."""
    from .penetration import instrument_units

    eligible = instrument_units(mapping, now)
    quoted = market["quotes"]
    codes = sorted(eligible, key=lambda code: (code not in quoted, code))[:MAX_CODES]
    codes.sort()
    if not codes:
        return {"status": "EMPTY_SCOPE", "requested": 0}
    try:
        request = _request(config, codes)
    except (ProbeError, OSError, ValueError):
        return {"status": "CONFIG_UNAVAILABLE", "requested": 0}
    scope = {"codes": codes, "trade_date": now.date().isoformat(), "mapping_version": mapping["version"]}

    def store_batch():
        rows, received = _query(request, codes)
        valid = [row for row in rows if usable_reading(row, received)]
        source_at = max((row["observed_at"] for row in valid), key=moment, default=None)
        return save(store, "findesk", ENDPOINT, scope, {"readings": rows}, len(rows), source_at, received)

    batch = budget.call(SOURCE, scope, store_batch, limit=1)
    with store._connect() as connection:
        saved = connection.execute(
            "SELECT body FROM supplier_market_batches WHERE batch_id=%s AND provider='findesk' AND endpoint=%s",
            (batch["batch"], ENDPOINT),
        ).fetchone()
    if saved is None:
        raise ValueError("unknown Findesk batch")
    readings = document(saved[0])["readings"]
    cutoff = datetime.now(CHINA)
    valid = {row["etf"]: row for row in readings if usable_reading(row, cutoff)}
    market["iopv"] = {"batch": batch["batch"], "source": "findesk", "readings": valid}
    return {"status": "AVAILABLE" if valid else "UNAVAILABLE", "requested": len(codes),
            "valid": len(valid), "batch": batch["batch"], "source_clock": batch["source_clock"]}


def usable_reading(row, cutoff):
    if not isinstance(row, dict) or row.get("source") != "findesk":
        return False
    try:
        observed = moment(row["observed_at"])
        return (observed.date() == cutoff.date() and observed <= cutoff and session(observed)
                and decimal(row["iopv"]) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def mark_quality(market, result):
    market["quality"]["iopv"] = result["status"]
    market["quality"]["iopv_count"] = result.get("valid", 0)
    market["snapshot_id"] = "intraday-" + fingerprint([
        market["snapshot_id"], "findesk", result["status"], result.get("batch"),
    ])[:24]
