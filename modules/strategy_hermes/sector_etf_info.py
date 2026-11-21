"""Paginated sector and ETF information from saved local evidence only."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
from urllib.parse import parse_qs

from .adapter import BOARD
from .market_stream import read_market
from .simulator import CHINA, CODE, PRECISION
from .stage2.inputs import read_file

SORTS = {
    "sectors": {"return_desc", "return_asc", "strength_desc", "name_asc", "code_asc"},
    "etfs": {"return_desc", "return_asc", "amount_desc", "name_asc", "code_asc"},
}


def _query(query):
    if not isinstance(query, str) or len(query) > 2000:
        raise ValueError("invalid sector/ETF query")
    args = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=5, errors="strict")
    if set(args) - {"kind", "q", "sort", "page", "page_size"} or any(len(v) != 1 for v in args.values()):
        raise ValueError("invalid sector/ETF query fields")
    kind = args.get("kind", ["sectors"])[0]
    sort = args.get("sort", ["return_desc"])[0]
    q = args.get("q", [""])[0]
    if kind not in SORTS or sort not in SORTS[kind] or len(q) > 80:
        raise ValueError("invalid sector/ETF kind, sort or search")
    page, size = args.get("page", ["1"])[0], args.get("page_size", ["20"])[0]
    if any(not value.isascii() or not value.isdigit() or len(value) > 9 for value in (page, size)):
        raise ValueError("invalid sector/ETF pagination")
    page, size = int(page), int(size)
    if page < 1 or not 1 <= size <= 100:
        raise ValueError("invalid sector/ETF pagination")
    return kind, sort, q.strip(), page, size


def _number(value, *, minimum=None, positive=False):
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = str(value)
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        value = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if (not value.is_finite() or len(value.as_tuple().digits) > 38
            or abs(value.as_tuple().exponent) > 100
            or positive and value <= 0 or minimum is not None and value < minimum):
        return None
    return value


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _value(value, **kwargs):
    parsed = _number(value, **kwargs)
    return str(parsed) if parsed is not None else None


def _object(raw):
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError("expected saved object")
    return value


def _mapping_names(path, issues):
    if path is None:
        return {}
    try:
        mapping = read_file(path)
    except FileNotFoundError:
        return {}
    except OSError:
        issues.add("MAPPING_FILE_UNAVAILABLE")
        return {}
    except (ValueError, UnicodeError):
        issues.add("MAPPING_FILE_INVALID")
        return {}
    if (mapping.get("schema_version") != "stock-v4-etf-mapping-v1"
            or not isinstance(mapping.get("products"), dict)):
        issues.add("MAPPING_FILE_INVALID")
        return {}
    names = {}
    for code, product in mapping["products"].items():
        if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(product, dict):
            issues.add("MAPPING_PRODUCT_INVALID")
            continue
        if name := _text(product.get("name")):
            names[code] = name
    return names


def _rejected(product):
    return (bool(product.get("rejection_reasons")) or product.get("recommendation_status") == "REJECTED"
            or product.get("static_status") in ("FAIL", "REJECTED"))


def _relations(report):
    """Explicit saved candidates and accepted matches, retaining report identity."""
    links, rejected = set(), set()
    meta = report.get("input_meta", {})
    if not isinstance(meta, dict):
        return links
    # Read both existing report formats; a rejection always wins for that pair.
    for field in ("penetration", "etf_recommendations"):
        groups = meta.get(field, {}).get("sectors", {}) if isinstance(meta.get(field), dict) else {}
        if not isinstance(groups, dict):
            continue
        for sid, group in groups.items():
            if not isinstance(sid, str) or not BOARD.fullmatch(sid) or not isinstance(group, dict):
                continue
            products = group.get("products", [])
            if not isinstance(products, list):
                continue
            for product in products:
                if not isinstance(product, dict):
                    continue
                code = product.get("etf")
                if not isinstance(code, str) or not CODE.fullmatch(code):
                    continue
                pair = (sid, code)
                representation = product.get("representativeness")
                if isinstance(representation, dict) and representation.get("entry_allowed") is False:
                    rejected.add(pair)
                elif _rejected(product):
                    rejected.add(pair)
                elif isinstance(representation, dict) and representation.get("entry_allowed") is True:
                    links.add(pair)
                elif product.get("matched") is True or isinstance(product.get("matched"), list) and product["matched"]:
                    links.add(pair)
    candidates = report.get("candidates", {})
    if isinstance(candidates, dict):
        for code, choices in candidates.items():
            if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(choices, list):
                continue
            for item in choices:
                sid = item.get("sector_id") if isinstance(item, dict) else None
                if isinstance(sid, str) and BOARD.fullmatch(sid):
                    links.add((sid, code))
    return links - rejected


def _report_sector_names(report):
    try:
        rows = _object(report.get("raw", {})).get("sectors", [])
    except (ValueError, TypeError):
        return {}
    names = {}
    normalized = report.get("sectors", {})
    if not isinstance(normalized, dict):
        return names
    for sid, value in normalized.items():
        index = value.get("row") if isinstance(value, dict) else None
        if type(index) is int and isinstance(rows, list) and 0 <= index < len(rows) and isinstance(rows[index], dict):
            name = _text(rows[index].get("sector_name"))
            if name:
                names[sid] = name
    return names


def _saved(ledger, codes, issues):
    with ledger._connect() as conn:
        row = conn.execute("""SELECT id,at,payload FROM events WHERE ns=%s AND kind='report'
            ORDER BY at DESC,id DESC LIMIT 1""", (ledger.ns,)).fetchone()
        report_id, report_at, report = None, None, {}
        if row:
            report_id, report_at, raw = row
            report_at = report_at.isoformat() if hasattr(report_at, "isoformat") else report_at
            try:
                report = _object(raw)
            except (ValueError, TypeError):
                issues.add("SAVED_REPORT_INVALID")
        links = _relations(report)
        relevant = sorted(set(codes) | {code for _, code in links})
        catalog = conn.execute("SELECT code,reference_data FROM supplier_etf_catalog WHERE code=ANY(%s)", (relevant,)).fetchall() if relevant else []
    names = {}
    for code, raw in catalog:
        try:
            reference = _object(raw)
        except (ValueError, TypeError):
            issues.add("ETF_CATALOG_ROW_INVALID")
            continue
        if name := _text(reference.get("csname")):
            names[code] = name
    return report_id, report_at, report, links, names


def _rows(market):
    sectors, etfs = {}, {}
    sector_rows = market.get("sectors", [])
    for row in sector_rows if isinstance(sector_rows, list) else []:
        sid = row.get("sector_id") if isinstance(row, dict) else None
        if not isinstance(sid, str) or not BOARD.fullmatch(sid):
            continue
        sectors[sid] = {"id": sid, "code": sid, "name": _text(row.get("sector_name")),
                        "return_ratio": _value(row.get("return_ratio")), "strength": _value(row.get("strength")),
                        "observed_at": row.get("observed_at"), "source": row.get("source") or market.get("source"), "etfs": []}
    quotes = market.get("quotes", {})
    for code, row in (quotes if isinstance(quotes, dict) else {}).items():
        if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(row, dict):
            continue
        price, previous = _number(row.get("price_cny"), positive=True), _number(row.get("pre_close_cny"), positive=True)
        with localcontext(PRECISION):
            change = str(price / previous - 1) if price is not None and previous is not None else None
        etfs[code] = {"code": code, "name": None, "price_cny": str(price) if price is not None else None,
                      "return_ratio": change, "amount_cny": _value(row.get("amount_cny"), minimum=0),
                      "volume_shares": _value(row.get("volume_shares"), minimum=0),
                      "observed_at": row.get("observed_at"), "source": row.get("source") or market.get("source"), "sectors": []}
    return sectors, etfs


def _sort(rows, sort):
    field = {"return_desc": "return_ratio", "return_asc": "return_ratio", "amount_desc": "amount_cny",
             "strength_desc": "strength", "name_asc": "name", "code_asc": "code"}[sort]

    def key(row):
        value = row.get(field)
        if field in ("name", "code"):
            value = value.casefold() if isinstance(value, str) and value else None
        else:
            value = _number(value)
            if value is not None and sort.endswith("_desc"):
                value = value.copy_negate()
        return value is None, value, row["code"]
    return sorted(rows, key=key)


def _summary(market, sectors, etfs, report_id, report_at, issues):
    counts = dict(up=0, down=0, flat=0, unknown=0)
    for row in sectors.values():
        value = _number(row["return_ratio"])
        counts["unknown" if value is None else "up" if value > 0 else "down" if value < 0 else "flat"] += 1
    amounts = [_number(row["amount_cny"]) for row in etfs.values() if row["amount_cny"] is not None]
    leaders = [dict(name=row["name"], id=row["id"], return_ratio=row["return_ratio"], observed_at=row["observed_at"])
               for row in _sort(list(sectors.values()), "return_desc") if row["return_ratio"] is not None][:8]
    with localcontext(PRECISION):
        amount = str(sum(amounts, Decimal(0))) if amounts else None
    return {"snapshot_id": market.get("snapshot_id"), "trade_date": market.get("trade_date"),
            "observed_at": market.get("observed_at"), "mode": market.get("mode"), "source": market.get("source"),
            "quality": market.get("quality") or {}, "counts": counts, "leaders": leaders,
            "etf_amount_cny": amount, "etf_amount_count": len(amounts),
            "named_etf_count": sum(row["name"] is not None for row in etfs.values()),
            "relationship_report_id": report_id, "relationship_report_at": report_at,
            "metadata_issues": sorted(issues)}


def read_info(ledger, query="", fallback=None, mapping_file=None):
    kind, sort, q, page, page_size = _query(query)
    market = read_market(ledger, fallback) or {}
    sectors, etfs = _rows(market)
    issues = set()
    report_id, report_at, report, links, names = _saved(ledger, etfs, issues)
    names = _mapping_names(mapping_file, issues) | names
    sector_names = _report_sector_names(report)
    sector_names.update({sid: row["name"] for sid, row in sectors.items() if row["name"]})
    for code, row in etfs.items():
        row["name"] = names.get(code)
    for sid, code in sorted(links):
        provenance = {"report_id": report_id, "report_at": report_at}
        if sid in sectors:
            sectors[sid]["etfs"].append({"code": code, "name": names.get(code), **provenance})
        if code in etfs:
            etfs[code]["sectors"].append({"id": sid, "name": sector_names.get(sid), **provenance})
    rows = list((sectors if kind == "sectors" else etfs).values())
    needle = q.casefold()
    if needle:
        def matches(row):
            code = row["code"].casefold()
            # The common "kaipanla:" namespace contains "ai"; searching for AI
            # should find the sector name, not every provider-prefixed identity.
            if kind == "sectors" and ":" not in needle:
                code = code.partition(":")[2]
            return needle in code or needle in (row["name"] or "").casefold()
        rows = [row for row in rows if matches(row)]
    rows = _sort(rows, sort)
    total = len(rows)
    start = (page - 1) * page_size
    return {"summary": _summary(market, sectors, etfs, report_id, report_at, issues), "kind": kind,
            "items": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size,
            "sort": sort, "q": q, "pages": max(1, (total + page_size - 1) // page_size)}


MAPPING_VERSIONS = 12
LISTED = 40
_REACH = {}


def _reach(body, at):
    """What one immutable mapping version can penetrate, by the rule the report evaluation applies."""
    from .stage2.penetration import _dated_weights, _formal_members
    sectors = {sid: (_text(sector.get("sector_name")) if isinstance(sector, dict) else None,
                     _formal_members(body, sid, at)[0]) for sid, sector in body["sectors"].items()}
    bases, reach, missing, names, dates = {}, {}, {}, {}, set()
    for code, product in body["products"].items():
        if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(product, dict):
            continue
        index = product.get("index_code") if isinstance(product.get("index_code"), str) else None
        names[code] = _text(product.get("name"))
        if index not in bases:
            basis = _dated_weights(body["weights"].get(index), at)
            bases[index] = set(basis[2]) if basis else None
            if basis:
                dates.add(basis[0].isoformat())
        if bases[index] is None:
            missing.setdefault(index, []).append(code)
        else:
            reach[code] = (index, sorted(sid for sid, (_, members) in sectors.items() if members & bases[index]))
    return dict(reach=reach, missing=missing, names=names, weight_dates=sorted(dates),
                sectors={sid: name for sid, (name, _) in sectors.items()},
                empty=sorted(sid for sid, (_, members) in sectors.items() if not members),
                indexes=sorted(index for index, codes in bases.items() if codes is not None))


def _versions(ledger, at, issues):
    """Newest first. Bodies of versions already digested are not read again."""
    with ledger._connect() as conn:
        rows = conn.execute("""SELECT version,available_at,summary FROM maintained_mappings
            WHERE ns=%s AND available_at<=%s ORDER BY available_at DESC,version DESC LIMIT %s""",
                            (ledger.ns, at, MAPPING_VERSIONS)).fetchall()
        wanted = [version for version, _, _ in rows if (ledger.ns, version) not in _REACH]
        bodies = conn.execute("SELECT version,body FROM maintained_mappings WHERE ns=%s AND version=ANY(%s)",
                              (ledger.ns, wanted)).fetchall() if wanted else []
    for version, raw in bodies:
        try:
            body = _object(raw)
            if not all(isinstance(body.get(key), dict) for key in ("sectors", "products", "weights")):
                raise ValueError("invalid maintained mapping")
            if len(_REACH) >= 4 * MAPPING_VERSIONS:
                _REACH.clear()
            _REACH[ledger.ns, version] = _reach(body, at)
        except (ValueError, TypeError, KeyError):
            issues.add("MAPPING_VERSION_INVALID")
    result = []
    for version, available_at, raw in rows:
        if (ledger.ns, version) not in _REACH:
            continue
        try:
            summary = _object(raw)
        except (ValueError, TypeError):
            summary = {}
        if hasattr(available_at, "astimezone"):
            available_at = available_at.astimezone(CHINA).isoformat(timespec="seconds")
        result.append((version, available_at, summary, _REACH[ledger.ns, version]))
    return result


def _current(report_id, report_at, report, links, names, mapped):
    meta = report.get("input_meta", {}) if isinstance(report.get("input_meta"), dict) else {}
    groups = meta.get("penetration", {}).get("sectors", {}) if isinstance(meta.get("penetration"), dict) else {}
    groups = groups if isinstance(groups, dict) else {}
    evaluated, matched, usable, reasons, empty = set(), set(), set(), {}, []
    for sid, group in groups.items():
        products = group.get("products") if isinstance(group, dict) else None
        found = False
        for product in products if isinstance(products, list) else ():
            code = product.get("etf") if isinstance(product, dict) else None
            if not isinstance(code, str) or not CODE.fullmatch(code):
                continue
            found = True
            evaluated.add(code)
            if product.get("matched") is True or isinstance(product.get("matched"), list) and product["matched"]:
                matched.add(code)
            if _rejected(product):
                for reason in product.get("rejection_reasons") if isinstance(product.get("rejection_reasons"), list) else ():
                    if isinstance(reason, str):
                        reasons.setdefault(reason, set()).add(code)
            else:
                usable.add(code)
        if not found:
            empty.append(dict(id=sid, name=names.get(sid), in_mapping=sid in mapped,
                              reason=_text(group.get("reason")) if isinstance(group, dict) else None))
    entry = {code for _, code in links}
    rejected = evaluated - usable - entry
    return dict(report_id=report_id, report_at=report_at, sectors=len(groups), evaluated=len(evaluated),
                matched=len(matched), entry=len(entry), observe=len(matched - entry - rejected), rejected=len(rejected),
                reasons=[dict(reason=reason, etfs=len(codes & rejected)) for reason, codes in
                         sorted(reasons.items(), key=lambda item: (-len(item[1] & rejected), item[0])) if codes & rejected],
                empty_sectors=sorted(empty, key=lambda row: row["id"]))


def read_penetration(ledger, now=None):
    """Sector to ETF penetration: the latest report, the latest mapping, and what recent mapping versions added."""
    at, issues = now or datetime.now(CHINA), set()
    report_id, report_at, report, links, _ = _saved(ledger, (), issues)
    versions = _versions(ledger, at, issues)
    names = _report_sector_names(report)
    mapping, recent, gaps, mapped = None, [], dict(indexes=[], index_count=0, etf_count=0, sectors=[]), set()
    if versions:
        version, available_at, summary, latest = versions[0]
        names = latest["sectors"] | {sid: name for sid, name in names.items() if name}
        reached = {code for code, (_, sectors) in latest["reach"].items() if sectors}
        mapped = set(latest["sectors"]) - set(latest["empty"])
        queue = summary.get("retry_queue") if isinstance(summary.get("retry_queue"), dict) else {}
        mapping = dict(version=version, available_at=available_at, catalog=len(latest["names"]),
                       weighted=len(latest["reach"]), reachable=len(reached), sectors=len(latest["sectors"]),
                       indexes=len(latest["indexes"]) + len(latest["missing"]), weighted_indexes=len(latest["indexes"]),
                       weight_dates=latest["weight_dates"], status=_text(summary.get("status")),
                       message=_text(summary.get("message")), pending=queue.get("pending_count"),
                       review_required=queue.get("review_required"))
        ranked = sorted(latest["missing"].items(), key=lambda item: (-len(item[1]), item[0] or ""))
        gaps = dict(index_count=len(ranked), etf_count=sum(len(codes) for _, codes in ranked),
                    indexes=[dict(index=index, etfs=len(codes),
                                  samples=[dict(code=code, name=latest["names"].get(code)) for code in sorted(codes)[:3]])
                             for index, codes in ranked[:LISTED]],
                    sectors=[dict(id=sid, name=latest["sectors"].get(sid)) for sid in latest["empty"]])
        for (version, available_at, _, new), (_, _, _, old) in zip(versions, versions[1:]):
            before = {code for code, (_, sectors) in old["reach"].items() if sectors}
            added = sorted(code for code, (_, sectors) in new["reach"].items() if sectors and code not in before)
            sectors = sorted(set(new["sectors"]) - set(old["sectors"]))
            indexes = sorted(set(new["indexes"]) - set(old["indexes"]))
            if added or sectors or indexes:
                recent.append(dict(version=version, available_at=available_at, etf_count=len(added),
                                   etfs=[dict(code=code, name=new["names"].get(code), index=new["reach"][code][0],
                                              sectors=[new["sectors"].get(sid) or sid for sid in new["reach"][code][1]])
                                         for code in added[:LISTED]],
                                   sectors=[dict(id=sid, name=new["sectors"].get(sid)) for sid in sectors], indexes=indexes))
    since = versions[-1][1] if versions else None
    return dict(current=_current(report_id, report_at, report, links, names, mapped), mapping=mapping, recent=recent,
                recent_since=since, recent_etf_count=sum(row["etf_count"] for row in recent), gaps=gaps,
                metadata_issues=sorted(issues))
