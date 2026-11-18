"""Collect one optional, bounded batch of A-share research evidence."""
from datetime import date, datetime
from decimal import Decimal, localcontext
from statistics import median
import re
from urllib.request import Request

from ..adapter import BOARD
from ..ledger import encode, fingerprint
from ..simulator import CHINA, CODE, PRECISION, decimal, moment, session
from .bulk_collect import RequestLog
from .inputs import CLOSED_STATUS, document
from .market_bootstrap import TICKFLOW_URL, fetch, normalize_tickflow, save, tickflow_config
from .penetration import number

MAX_SECTORS = 8
MAX_PER_SECTOR = 4
MAX_CORE_LEADERS = 8
MAX_STOCK_EVIDENCE = 12
BREADTH_ALIGNMENT_SECONDS = 120
STOCK_CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
SOURCE = "tickflow:stock_quotes"
ENDPOINT = "stock_quotes"


def _board_rank(board, at):
    try:
        observed = moment(board["observed_at"])
        ratio = decimal(board["return_ratio"])
        if observed.date() == at.date() and observed <= at:
            return -ratio, board["sector_id"]
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _members(sector, at):
    # 046 pages must have been fully reconciled against the stated population.
    try:
        codes = set(sector["members"])
        if (sector["unresolved_count"] == 0 and sector["reported_count"] == len(codes)
                and sector["source_batches"] and date.fromisoformat(sector["as_of_date"]) <= at.date()
                and (not sector.get("available_at") or moment(sector["available_at"]) <= at)):
            return codes
    except (KeyError, TypeError, ValueError):
        pass
    return set()


def shortlist(mapping, market, at):
    """Verified board members for explanation; independent of any ETF products."""
    if not market or market.body["trade_date"] != at.date().isoformat() or market.body.get("mode") == "EOD":
        return {}
    result = {}
    for sid, sector in sorted(mapping["sectors"].items()):
        members = _members(sector, at)
        if members:
            result[sid] = [dict(code=code, membership_as_of=sector["as_of_date"])
                           for code in sorted(members) if STOCK_CODE.fullmatch(code)]
    return result


def _fetch_batch(store, config, scope, evidence, cutoff):
    request = Request(TICKFLOW_URL, method="POST", data=encode({"universes": ["CN_Equity_A"]}).encode(),
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "User-Agent": "stock-v4-tickflow-client/1.0", "x-api-key": tickflow_config(config)})
    body = fetch(request, timeout=15)
    received = datetime.now(CHINA)
    rows, issues = normalize_tickflow(body, None, received, stock_universe=True)
    status = {row["symbol"]: row.get("trading_status") for row in body["data"]}
    quotes = {}
    for row in rows:
        code = row.pop("code")
        observed = moment(row["observed_at"])
        if observed.date() != cutoff.date() or observed > received or not session(observed):
            issues[code] = "NOT_CURRENT_SESSION"
            continue
        trade_status = status.get(code)
        if isinstance(trade_status, str) and trade_status.upper() in CLOSED_STATUS:
            issues[code] = "NOT_TRADING"
            continue
        row["source"] = "tickflow:stock_quotes"
        row["trading_status"] = trade_status.upper() if isinstance(trade_status, str) else None
        if row["pre_close_cny"]:
            with localcontext(PRECISION):
                row["return_ratio"] = str((decimal(row["price_cny"]) / decimal(row["pre_close_cny"]) - 1).quantize(Decimal("0.000000000001")))
        quotes[code] = row
    source_at = max((row["observed_at"] for row in quotes.values()), key=moment, default=None)
    return save(store, "tickflow", ENDPOINT, scope,
                {"quotes": quotes, "issues": issues, "research_scope": evidence},
                len(rows), source_at, received)


def collect(store, config, mapping, market, cutoff, run_key, *, enabled=False):
    """Reserve at most one attempt per logical task. The default never calls the provider."""
    if not enabled:
        return {"status": "DISABLED", "new_requests": 0}
    if not session(cutoff):
        return {"status": "OUTSIDE_SESSION", "new_requests": 0}
    evidence = shortlist(mapping, market, cutoff)
    scope = {"universes": ["CN_Equity_A"], "trade_date": cutoff.date().isoformat(),
             "mapping_version": mapping["version"]}
    budget = RequestLog(store, run_key)
    batch = budget.call(SOURCE, scope, lambda: _fetch_batch(store, config, scope, evidence, cutoff), limit=1)
    return batch | {"new_requests": budget.new_calls}


def read_batch(store, batch):
    """Read stock quote evidence for the shared snapshot or analyst, with no supplier call."""
    with store._connect() as conn:
        row = conn.execute("SELECT body FROM supplier_market_batches "
                           "WHERE batch_id=%s AND provider='tickflow' AND endpoint=%s",
                           (batch, ENDPOINT)).fetchone()
    if row is None:
        raise ValueError("unknown stock research batch")
    return document(row[0])


def attach(snapshot, data, batch, round_slot):
    """Keep stock evidence separate from ETF prices, source clocks and replay."""
    quotes, scope = data["quotes"], data["research_scope"]
    evidence = {"batch": batch, "round_slot": round_slot, "quotes": quotes, "research_scope": scope}
    stock_times = [row["observed_at"] for row in quotes.values()]
    if stock_times:
        evidence["observed_at"] = max(stock_times, key=moment)
    snapshot["stock_research"] = evidence
    snapshot["quality"]["stock_quotes"] = "AVAILABLE" if quotes else "UNAVAILABLE"
    snapshot["snapshot_id"] = "intraday-" + fingerprint([snapshot["snapshot_id"], batch])[:24]


def _usable_quote(row, at):
    if not isinstance(row, dict) or str(row.get("trading_status", "")).upper() in CLOSED_STATUS:
        return False
    try:
        observed = moment(row["observed_at"])
        return (observed.date() == at.date() and observed <= at and session(observed)
                and number(row.get("price_cny")) is not None and decimal(row["price_cny"]) > 0
                and isinstance(row.get("source"), str) and bool(row["source"]))
    except (ValueError, KeyError, TypeError):
        return False


def breadth(market, mapping, sid, slot, at):
    """Count observed verified members, retaining the true complete-member denominator."""
    members = _members(mapping["sectors"].get(sid, {}), at)
    result = dict(status="UNKNOWN", member_count=len(members) if members else None,
                  observed_count=0, up=None, down=None, flat=None, coverage=None,
                  up_ratio=None, median_return=None, oldest_at=None, newest_at=None)
    if not members or not market:
        return result
    research = market.body.get("stock_research", {})
    if research.get("round_slot") != slot:
        return result
    values = []
    for code in members:
        row = research.get("quotes", {}).get(code)
        if not _usable_quote(row, at):
            continue
        observed = moment(row["observed_at"])
        value = number(row.get("return_ratio"))
        if value is not None and (at - observed).total_seconds() <= BREADTH_ALIGNMENT_SECONDS:
            values.append((value, row["observed_at"]))
    if not values:
        return result
    count = len(values)
    up, down = sum(value > 0 for value, _ in values), sum(value < 0 for value, _ in values)
    # Display ratios must remain consumable by the shared exact-decimal contract.
    with localcontext(PRECISION):
        def ratio_text(value):
            return format(value.quantize(Decimal("0.000000000001")).normalize(), "f")
        coverage = ratio_text(Decimal(count) / len(members))
        up_ratio = ratio_text(Decimal(up) / count)
        median_return = ratio_text(median(value for value, _ in values))
    return dict(status="COMPLETE" if count == len(members) else "PARTIAL",
                member_count=len(members), observed_count=count, up=up, down=down, flat=count-up-down,
                coverage=coverage, up_ratio=up_ratio, median_return=median_return,
                oldest_at=min((stamp for _, stamp in values), key=moment),
                newest_at=max((stamp for _, stamp in values), key=moment),
                batch=research.get("batch"), mapping_version=mapping["version"])


def for_sector(market, sid, slot, at):
    """Provide leaders/liquid members from saved quotes, not an ETF-weight shortlist."""
    if not market:
        return []
    research = market.body.get("stock_research", {})
    if research.get("round_slot") != slot:
        return []
    scope = {row["code"]: row for row in research.get("research_scope", {}).get(sid, [])}
    quotes = research.get("quotes", {})
    usable = {code: quotes[code] for code in scope if _usable_quote(quotes.get(code), at)
              and (at - moment(quotes[code]["observed_at"])).total_seconds() <= BREADTH_ALIGNMENT_SECONDS}
    by_return = sorted(usable, key=lambda code: (number(usable[code].get("return_ratio")) is None,
                        -(number(usable[code].get("return_ratio")) or Decimal(0)), code))
    by_amount = sorted(usable, key=lambda code: (number(usable[code].get("amount_cny")) is None,
                        -(number(usable[code].get("amount_cny")) or Decimal(0)), code))
    chosen = list(dict.fromkeys([*by_return[:6], *by_amount[:6], *by_return]))[:MAX_STOCK_EVIDENCE]
    result = []
    for code in chosen:
        quote, item = usable[code], scope[code]
        ref = "stock:" + fingerprint([sid, code, quote["observed_at"], research["batch"]])
        row = dict(ref=ref, code=code, observed_at=quote["observed_at"],
                   price_cny=quote["price_cny"], return_ratio=quote.get("return_ratio"),
                   amount_cny=quote.get("amount_cny"), source=quote["source"], batch=research["batch"])
        for key in ("membership_as_of", "weight_ratio", "weight_as_of", "index_code"):
            if key in item:
                row[key] = item[key]
        result.append(row)
    return result
