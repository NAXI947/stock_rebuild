"""Refresh the shared research snapshot with bounded, batched intraday requests."""
from datetime import datetime

import psycopg

from redis.exceptions import RedisError

from ..ledger import fingerprint
from ..simulator import CHINA, moment
from .build_sources import board_snapshot, publish_snapshot
from .bulk_collect import RequestLog, board_page, quote_pool
from .inputs import SharedMarket, document
from .market_bootstrap import ProbeError

BOARD_PAGE_LIMIT = 5


def collect_boards(store, budget, captures, day):
    pages = []
    offset = 0
    for _ in range(BOARD_PAGE_LIMIT):
        try:
            page = budget.call("kaipanla:041", [day, offset],
                               lambda: board_page(store, captures, offset, day), limit=BOARD_PAGE_LIMIT)
        except (ProbeError, OSError, ValueError, KeyError, TypeError):
            break
        pages.append(page)
        offset += page["rows"]
        if offset >= page["total"] or page["rows"] < page["page_size"]:
            break
    return pages


def assemble(store, day, previous, tick, pages):
    prior = previous.body if previous and previous.body["trade_date"] == day else {}
    quotes = dict(prior.get("quotes", {}))
    boards = {row["sector_id"]: row for row in prior.get("sectors", [])}
    board_ids = set()
    quote_count = 0
    with store._connect() as conn:
        if tick:
            raw, = conn.execute("SELECT body FROM supplier_market_batches WHERE batch_id=%s",
                                (tick["batch"],)).fetchone()
            for row in document(raw)["quotes"]:
                at = moment(row["observed_at"])
                code = row["code"]
                if at.date().isoformat() != day:
                    continue
                existing = quotes.get(code)
                if not existing or moment(existing["observed_at"]) < at:
                    quotes[code] = {key: value for key, value in row.items() if key != "code"} | {
                        "source": "tickflow", "trade_date": day,
                    }
                quote_count += 1
        for page in pages:
            if page["source_clock"] is None or moment(page["source_clock"]).date().isoformat() != day:
                continue
            rows, _, _, _ = board_snapshot(conn, [page])
            for sid, row in rows.items():
                existing = boards.get(sid)
                if not existing or moment(existing["observed_at"]) < moment(row["observed_at"]):
                    boards[sid] = row
            board_ids.update(rows)
    if not quotes and not boards:
        return None
    times = [row["observed_at"] for row in (*quotes.values(), *boards.values())]
    snapshot_id = "intraday-" + fingerprint([day, sorted(quotes.items()), sorted(boards.items())])[:24]
    return dict(schema_version="hotspot_market_snapshot_v1", snapshot_id=snapshot_id,
                trade_date=day, observed_at=max(times, key=moment), mode="PARTIAL_REALTIME",
                source="tickflow+kaipanla", quotes=quotes, sectors=list(boards.values()),
                quality=dict(etf_quote_count=len(quotes), board_count=len(boards), fresh_etf_count=quote_count,
                             fresh_board_count=len(board_ids), board_reported_total=pages[0]["total"] if pages else None,
                             stock_quotes="NOT_COLLECTED", iopv="NOT_COLLECTED"),
                provenance=dict(previous_snapshot=prior.get("snapshot_id"), tickflow=tick["batch"] if tick else None,
                                kaipanla=[page["batch"] for page in pages]))


def collect_books(ledger, budget, config, mapping, market, now):
    """Optional depth evidence; a missing book never removes a usable quote."""
    from .market_bootstrap import depth_batch
    from .penetration import instrument_units
    from .momentum import MAX_SKEW_SECONDS

    eligible = set(instrument_units(mapping, now)) & market.get("quotes", {}).keys()
    codes = sorted(eligible)[:100]
    if not codes:
        return dict(status="EMPTY_SCOPE", requested=0, valid=0)
    # Clear any previous round's independently timed books for this requested scope.
    for code in codes:
        if code in market.get("quotes", {}):
            market["quotes"][code] = {key: value for key, value in market["quotes"][code].items()
                                     if key not in ("bid1_cny", "ask1_cny", "book_observed_at", "book_source")}
    result = budget.call("tickflow:depth", {"codes": codes, "trade_date": now.date().isoformat()},
                         lambda: depth_batch(ledger, config, codes), limit=1)
    valid, issues = 0, dict(result.get("issues", {}))
    for book in result.get("books", []):
        code = book["code"]
        quote = market.get("quotes", {}).get(code)
        if quote is None:
            continue
        try:
            clock, quote_clock = moment(book["book_observed_at"]), moment(quote["observed_at"])
            if (clock.date() != now.date() or clock > now
                    or (now-clock).total_seconds() > MAX_SKEW_SECONDS
                    or abs((clock-quote_clock).total_seconds()) > MAX_SKEW_SECONDS):
                issues[code] = "BOOK_TIME_UNALIGNED"
                continue
        except (ValueError, KeyError, TypeError):
            issues[code] = "BOOK_TIME_UNVERIFIED"
            continue
        quote.update({key: value for key, value in book.items() if key != "code"})
        quote["book_source"] = "tickflow"
        valid += 1
    market.setdefault("provenance", {})["tickflow_depth"] = result["batch"]
    market["snapshot_id"] = "intraday-" + fingerprint([market["snapshot_id"], result["batch"],
                                                       market.get("quotes", {})])[:24]
    return dict(status="AVAILABLE" if valid else "UNAVAILABLE", requested=len(codes), valid=valid,
                batch=result["batch"], issues=issues, omitted=max(0, len(eligible)-len(codes)))


def refresh(ledger, cache, key, due, previous, config, captures, *, stock_mapping=None, findesk=None):
    # Separate from request-budget locks. One collector assembles the whole slot.
    with ledger._connect() as conn:
        acquired, = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
            (ledger.ns + ":intraday-collector",),
        ).fetchone()
        if not acquired:
            return None
        return _refresh(ledger, cache, key, due, previous, config, captures,
                        stock_mapping=stock_mapping, findesk=findesk)


def _refresh(ledger, cache, key, due, previous, config, captures, *, stock_mapping=None, findesk=None):
    from .inputs import shared_market

    committed = shared_market(ledger,cache,key=key)
    previous = committed or previous
    day, slot, _ = due
    # Preserve development trial audit keys and bound custom production namespaces.
    scope = ("prod:" + fingerprint(ledger.ns)[:24]
             if ledger.settings.env == "production" else ledger.ns)
    budget = RequestLog(ledger, f"{scope}:feed:{day}:{slot.replace(':', '')}")
    try:
        tick = budget.call("tickflow:quotes", ["CN_ETF"], lambda: quote_pool(ledger, config), limit=1)
    except (ProbeError, OSError, ValueError, KeyError, TypeError):
        tick = None
    pages = collect_boards(ledger, budget, captures, day)
    market = assemble(ledger, day, previous, tick, pages)
    stocks = {"status": "DISABLED", "new_requests": 0}
    if stock_mapping is not None:
        stocks = {"status": "NO_SHARED_SNAPSHOT", "new_requests": 0}
        if market:
            from .stock_research import attach, collect, read_batch

            try:
                stocks = collect(ledger, config, stock_mapping, SharedMarket.read(market),
                                 datetime.now(CHINA), budget.run_key, enabled=True)
                if stocks.get("batch"):
                    data = read_batch(ledger, stocks["batch"])
                    attach(market, data, stocks["batch"], slot)
            except (ProbeError, psycopg.Error, OSError, ValueError, KeyError, TypeError) as exc:
                stocks = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
                market["quality"]["stock_quotes"] = "UNAVAILABLE"
    iopv = {"status": "DISABLED"}
    if findesk is not None:
        iopv = {"status": "NO_SHARED_SNAPSHOT"}
        if market:
            from .findesk_iopv import collect, mark_quality

            findesk_config, mapping = findesk
            try:
                iopv = collect(ledger, budget, findesk_config, mapping, market, datetime.now(CHINA))
            except (ProbeError, psycopg.Error, OSError, ValueError, KeyError, TypeError) as exc:
                iopv = {"status": "UNAVAILABLE", "error_type": type(exc).__name__}
            mark_quality(market, iopv)
    books = {"status": "DISABLED", "requested": 0, "valid": 0}
    depth_mapping = stock_mapping if stock_mapping is not None else findesk[1] if findesk else None
    if market and depth_mapping is not None:
        try:
            books = collect_books(ledger, budget, config, depth_mapping, market, datetime.now(CHINA))
        except (ProbeError, psycopg.Error, OSError, ValueError, KeyError, TypeError) as exc:
            books = {"status": "UNAVAILABLE", "error_type": type(exc).__name__, "valid": 0}
        market["quality"].update(book=books["status"], book_count=books["valid"])
    if market:
        try:
            publish_snapshot(ledger, cache, market, key=key)
        except RedisError:
            # publish_snapshot has already committed PostgreSQL fallback.
            cache_available = False
        else:
            cache_available = True
    else:
        cache_available = None
    return dict(physical_requests=budget.new_calls, cache_available=cache_available, tickflow_available=tick is not None,
                kaipanla_pages=len(pages), stock_collection=stocks, findesk_iopv=iopv, order_book=books,
                snapshot_id=market["snapshot_id"] if market else None,
                completed_at=datetime.now(CHINA).isoformat(timespec="seconds"))
