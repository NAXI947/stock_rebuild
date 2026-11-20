"""Versioned ETF association facts, direction choices and execution eligibility."""
from datetime import date
from decimal import Decimal, localcontext
import re

from ..adapter import normalize
from ..simulator import CODE, PRECISION, decimal, integer, moment
from .etf_representativeness import METHOD as V3_METHOD, evaluate_pair_representativeness
from .inputs import document

MIN_ADV20 = Decimal("30000000")
MIN_AUM = Decimal("200000000")
METHOD = "etf-r3-main-2-v14-observability"


def number(value):
    try:
        return decimal(value)
    except ValueError:
        return None


def weights(basis, day):
    if not isinstance(basis, dict):
        return None
    try:
        as_of=date.fromisoformat(basis["as_of_date"])
        rows=basis["rows"]
        values={row["code"]: decimal(row["weight"]) for row in rows}
    except (ValueError, KeyError, TypeError):
        return None
    total=sum(values.values())
    if (as_of>day or not values or len(values)!=len(rows)
            or min(values.values())<0 or total>Decimal("1.005") or total<=0
            or (basis.get("complete") is True and abs(total-1)>Decimal(".005"))):
        return None
    ordered=sorted(rows,key=lambda row:(-values[row["code"]],row["code"]))
    return as_of, ordered, {code:value/total if basis.get("complete") is True else value for code,value in values.items()}


def product_settlement(rule):
    """Read both spellings; retain the original T+1 contract for verified archives."""
    values = [rule[key] for key in ("settlement_cycle", "settlement") if key in rule]
    if not values:
        return "T+1" if rule.get("verified") is True else None
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    values = [value.strip().upper() for value in values]
    return values[0] if len(set(values)) == 1 else None


def _product_rule_reason(product, at):
    if not isinstance(product, dict):
        return "PRODUCT_IDENTITY_INVALID"
    rule = product.get("rule") if isinstance(product.get("rule"), dict) else {}
    if rule.get("review_required") is True:
        return "RULE_REVIEW_REQUIRED"
    if not isinstance(product.get("index_code"), str) or not product["index_code"].strip():
        return "PRODUCT_IDENTITY_INVALID"
    if product.get("list_status") not in (None, "L"):
        return "PRODUCT_NOT_LISTED"
    try:
        if ((product.get("catalog_available_at") and moment(product["catalog_available_at"]) > at)
                or (product.get("list_date") and date.fromisoformat(str(product["list_date"])) > at.date())):
            return "PRODUCT_IDENTITY_UNVERIFIED"
    except (TypeError, ValueError):
        return "PRODUCT_IDENTITY_UNVERIFIED"
    try:
        if (rule.get("verified") is not True or moment(rule["verified_at"]) > at
                or (rule.get("available_at") and moment(rule["available_at"]) > at)
                or (rule.get("effective_at") and moment(rule["effective_at"]) > at)
                or (rule.get("as_of_date") and date.fromisoformat(rule["as_of_date"]) > at.date())):
            return "PRODUCT_RULE_UNVERIFIED"
    except (TypeError, ValueError, KeyError):
        return "PRODUCT_RULE_UNVERIFIED"
    settlement = product_settlement(rule)
    if settlement is None:
        return "SETTLEMENT_RULE_CONFLICT"
    if (rule.get("scope") != "domestic_passive_sector"
            or rule.get("index_code") != product["index_code"] or settlement != "T+1"):
        return "PRODUCT_OUTSIDE_SCOPE"
    ratio = number(rule.get("price_limit_ratio"))
    if ratio is None or not 0 < ratio <= 1:
        return "PRODUCT_RULE_UNVERIFIED"
    return None


def product_entry_reason(product, at):
    """Pure current-entry rule check shared by reporting and transaction admission."""
    at = moment(at) if isinstance(at, str) else at
    reason = _product_rule_reason(product, at)
    if reason:
        return reason
    try:
        integer(product["trade_unit"])
    except (KeyError, ValueError, TypeError):
        return "TRADE_UNIT_UNVERIFIED"
    return None


def _verified_product(product, at, *, legacy=False):
    return (_product_rule_reason(product, at) is None
            and (not legacy or number(product["rule"].get("price_limit_ratio")) == Decimal(".1")))


def verified_product(product, at):
    """Validate the recorded product rule, including its actual price-limit ratio."""
    return _verified_product(product, at)


def instrument_units(mapping, at, *, legacy=False):
    units = {}
    for code, item in mapping["products"].items():
        if CODE.fullmatch(code) and _verified_product(item, at, legacy=legacy):
            try:
                units[code] = integer(item["trade_unit"])
            except (KeyError, ValueError):
                continue
    return units


def research_products(mapping, at):
    """Catalog identities eligible for bounded research, without granting execution."""
    products = {}
    for code, product in mapping.get("products", {}).items():
        if (not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(product, dict)
                or not isinstance(product.get("index_code"), str) or not product["index_code"].strip()
                or product.get("list_status") not in (None, "L")):
            continue
        try:
            if product.get("catalog_available_at") and moment(product["catalog_available_at"]) > at:
                continue
            listed = product.get("list_date")
            if listed and date.fromisoformat(str(listed)) > at.date():
                continue
        except (ValueError, TypeError):
            continue
        products[code] = product
    return products


def maintained_stats(product, at):
    """Overlay only dated, already-available maintenance facts on the legacy input.

    Existing frozen mappings retain their original behavior. This is a read-time
    copy: neither historic reports nor a product's archived ``stats`` are changed.
    """
    from .momentum import history_count, history_ready

    original = product.get("stats") or {}
    result = dict(original)
    if "aum_available_at" in original or "aum_valid_until" in original:
        try:
            dated_aum = (date.fromisoformat(original["aum_as_of"]) <= at.date()
                         <= date.fromisoformat(original["aum_valid_until"])
                         and moment(original["aum_available_at"]) <= at)
        except (KeyError, TypeError, ValueError):
            dated_aum = False
        if not dated_aum:
            result["aum_verified"] = False
    current = product.get("recommendation_stats") or {}
    if not isinstance(current, dict):
        return result
    if history_ready(current, at):
        # A known partial new window explicitly keeps its missing metrics unknown.
        for key, count in (("adv20", 20), ("adv5", 5)):
            if key in current:
                result[key] = current[key] if history_count(current) >= count else None
    try:
        aum_current = (current.get("aum_verified") is True
                       and date.fromisoformat(current["aum_as_of"]) <= at.date()
                       <= date.fromisoformat(current["aum_valid_until"])
                       and moment(current["aum_available_at"]) <= at)
    except (KeyError, TypeError, ValueError):
        aum_current = False
    if aum_current:
        result.update({key: value for key, value in current.items() if key.startswith("aum_")})
    return result


def _metrics(rows, w, members, core, stock_quotes, at):
    weighted=lambda keys:sum((w.get(code,Decimal(0)) for code in sorted(keys)),Decimal(0))
    top3=[row["code"] for row in rows[:3]]
    top10={row["code"] for row in rows[:10]}
    covered={}
    for code in w:
        row=stock_quotes.get(code)
        if not isinstance(row,dict):
            continue
        try:
            seen=moment(row["observed_at"])
        except (KeyError,TypeError,ValueError):
            continue
        ratio=number(row.get("return_ratio"))
        if seen.date()==at.date() and seen<=at and ratio is not None:
            covered[code]=ratio
    exposure=weighted(members)
    q=weighted(covered)
    targets=members.intersection(covered)
    target_weight=weighted(targets)
    m=weighted(row["code"] for row in rows if row.get("mainboard") is True)
    unknown=weighted(row["code"] for row in rows if row.get("mainboard") is None)
    core=core.intersection(members)
    r_top3=(sum(w[code]*covered[code] for code in top3)/weighted(top3)
            if top3 and all(code in covered for code in top3) else None)
    return dict(E=exposure,E_sector=exposure,Cw=weighted(core),Cc=Decimal(len(core.intersection(w)))/len(core) if core else None,Hit10=len(core.intersection(top10)),
        core_count=len(core),M=m,M_unknown=unknown,Q=q,Qt=target_weight/exposure if exposure else None,
        R_top3=r_top3,U_active=weighted(code for code in targets if covered[code]>=Decimal(".015"))/target_weight
          if target_weight else None,
        R_est=sum(w[code]*value for code,value in covered.items())/q if q>=Decimal(".85") else None)


def explain_sector(mapping, sector_id, quotes, at, core=(), *, iopv=None, stock_quotes=None):
    from .findesk_iopv import usable_reading

    sector=mapping["sectors"].get(sector_id)
    if not isinstance(sector,dict) or not isinstance(sector.get("members"),list) or not sector["members"]:
        return {"reason":"SECTOR_MEMBERS_UNAVAILABLE"}
    members,core=set(sector["members"]),set(core)
    units=instrument_units(mapping,at,legacy=True)
    products,by_index=[],{}
    missing_weights, missing_indexes = 0, set()
    with localcontext(PRECISION):
        bases={index:weights(basis,at.date()) for index,basis in mapping["weights"].items()}
        for code,product in mapping["products"].items():
            if not CODE.fullmatch(code) or not isinstance(product,dict):
                continue
            index=product.get("index_code")
            basis=bases.get(index)
            if not basis:
                missing_weights += 1
                if isinstance(index, str):
                    missing_indexes.add(index)
                continue
            as_of,rows,w=basis
            if not members.intersection(w):
                continue
            m=_metrics(rows,w,members,core,quotes if stock_quotes is None else stock_quotes,at)
            stats=maintained_stats(product, at)
            adv20,adv5=number(stats.get("adv20")),number(stats.get("adv5"))
            aum=number(stats.get("aum_yuan")) if stats.get("aum_verified") is True else None
            threshold=Decimal(".50") if m["core_count"]>=2 else Decimal(".60")
            reasons=[]
            if mapping["weights"][index].get("complete") is not True:
                reasons.append("PARTIAL_WEIGHTS")
            if code not in units:
                reasons.append("PRODUCT_RULE_UNVERIFIED")
            if m["E"]<threshold:
                reasons.append("EXPOSURE_LOW")
            if m["M"]<Decimal(".65"):
                reasons.append("MAINBOARD_LOW_OR_UNKNOWN")
            if m["core_count"]>=2 and m["Cw"]<Decimal(".12") and m["Hit10"]<2:
                reasons.append("CORE_EXPOSURE_LOW")
            if adv20 is not None and adv20<MIN_ADV20:
                reasons.append("LIQUIDITY_BELOW_MINIMUM")
            if aum is not None and aum<MIN_AUM:
                reasons.append("AUM_BELOW_MINIMUM")
            quote=quotes.get(code) or {}
            if quote.get("trading_status")=="SUSPENDED":
                reasons.append("TRADING_SUSPENDED")
            labels=[]
            if (at.date()-as_of).days>60:
                labels.append("OLDER_MAPPING")
            if adv20 is None:
                labels.append("LIQUIDITY_UNKNOWN")
            if aum is None:
                labels.append("AUM_UNKNOWN")
            if m["Q"]<Decimal(".85"):
                labels.append("LOW_COVERAGE")
            reading = iopv.get(code) if iopv else None
            verified_iopv = reading if usable_reading(reading, at) else None
            if verified_iopv is None and number(quote.get("iopv")) is None:
                labels.append("IOPV_MISSING")
            matched=[dict(code=row["code"],weight=str(w[row["code"]]),core=row["code"] in core)
                     for row in rows if row["code"] in members]
            products.append(dict(etf=code,index=index,weight_as_of=as_of.isoformat(),matched=matched,
                metrics={key:str(value) if isinstance(value,Decimal) else value for key,value in m.items()},
                rejection_reasons=reasons,quality=labels, iopv=verified_iopv))
            if not reasons:
                by_index.setdefault(index,[]).append((code,m,adv20 or Decimal(0),adv5 or Decimal(0)))
        representatives=[min(group,key=lambda row:(-row[2],-row[3],row[0])) for group in by_index.values()]
        representatives.sort(key=lambda row:(-row[1]["Cw"],-row[1]["Hit10"],-row[1]["E"],-row[1]["M"],
                                             mapping["products"][row[0]]["index_code"]))
    return dict(products=products,representatives=[row[0] for row in representatives[:3]],
                coverage_gaps=dict(weights=dict(reason="WEIGHTS_UNVERIFIED", products=missing_weights,
                                                indexes=len(missing_indexes))))


def evaluate(report_event, mapping, market, *, use_report_core=True):
    report=normalize(report_event)
    raw=document(report["raw"])
    at=moment(report["at"])
    quotes=market.body["quotes"] if market else {}
    iopv = market.body.get("iopv", {}).get("readings") if market else None
    explanation,candidates={},[]
    for sid,signal in report["sectors"].items():
        core=(raw["sectors"][signal["row"]].get("core_stock_codes") or ()) if use_report_core else ()
        stocks = (market.body.get("stock_research") or {}).get("quotes") if market else None
        details=explain_sector(mapping,sid,quotes,at,core,iopv=iopv,stock_quotes=stocks)
        explanation[sid]=details
        if signal["state"]=="UP":
            candidates.extend(dict(etf=code,sector_id=sid,rank=rank,mapping_ref=mapping["version"])
                              for rank,code in enumerate(details.get("representatives",[]),1))
    return candidates,dict(method=METHOD,mapping_version=mapping["version"],
                           market_snapshot=market.snapshot_id if market else None,sectors=explanation)


RECOMMENDATION_METHOD = "etf-v16-recommendation-v1"
STOCK_ID = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")


def _verified_events(entry, at, sources):
    """Only explicitly sourced, already-verified event identities enter H."""
    if not isinstance(entry, dict) or entry.get("status") != "VERIFIED":
        return set()
    codes, refs = entry.get("codes"), entry.get("evidence_refs")
    try:
        stamp = moment(entry["observed_at"])
    except (KeyError, TypeError, ValueError):
        return set()
    if (stamp > at or not isinstance(codes, list) or not isinstance(refs, list)
            or not refs or not all(isinstance(ref,str) and ref for ref in refs)
            or not set(refs).issubset(sources)):
        return set()
    return {code for code in codes if isinstance(code, str) and STOCK_ID.fullmatch(code)}


def _usable_stock_quotes(stock_quotes, at, reference, cache):
    """Reuse identical quote alignment within one immutable recommendation run."""
    from .momentum import valid_quote
    if reference is None:
        return {}
    key = (id(stock_quotes), at, reference)
    if key not in cache:
        # Bound memory even if a large catalog has many different source clocks.
        if len(cache) >= 16:
            cache.pop(next(iter(cache)))
        cache[key] = {code: row for code, row in stock_quotes.items()
                      if isinstance(row, dict) and row.get("source")
                      and valid_quote(row, at, reference) is not None
                      and number(row.get("return_ratio")) is not None}
    return cache[key]


def _formal_members(mapping, sid, at):
    sector = mapping["sectors"].get(sid) or {}
    raw_members = sector.get("members") if isinstance(sector,dict) else None
    members = {code for code in raw_members if isinstance(code,str) and STOCK_ID.fullmatch(code)} if isinstance(raw_members,list) else set()
    try:
        complete_members = (bool(members) and len(members)==len(raw_members)==sector["reported_count"]
            and sector["unresolved_count"]==0 and bool(sector["source_batches"])
            and date.fromisoformat(sector["as_of_date"])<=at.date()
            and ("available_at" not in sector or moment(sector["available_at"])<=at))
    except (KeyError,TypeError,ValueError):
        complete_members=False
    try:
        if date.fromisoformat(sector["as_of_date"])>at.date() or ("available_at" in sector and moment(sector["available_at"])>at):
            members=set()
    except (KeyError,TypeError,ValueError):
        members=set()
    return members, complete_members


def _dated_weights(raw, at):
    if isinstance(raw, dict) and raw.get("available_at"):
        try:
            if moment(raw["available_at"]) > at:
                return None
        except (ValueError, TypeError):
            return None
    return weights(raw, at.date())


def _recommend_sector(mapping, sid, market, at, core, core_status, events, *, quote_cache=None,
                      direction_policy=False, weight_cache=None):
    from .momentum import aggregate, check, evaluate as momentum, history_ready, history_count, valid_quote

    members, complete_members = _formal_members(mapping, sid, at)
    sector = mapping["sectors"].get(sid) or {}
    if not members and (not events if direction_policy else len(events) < 5):
        return dict(scope_mode="sector_members", core_status=core_status, products=[], representatives=[],
                    static_representatives=[], reason="SECTOR_MEMBERS_UNAVAILABLE", evaluation_complete=False,
                    evaluation_scope="MAPPED_PRODUCTS")
    event_scope = len(events) >= 5 or (direction_policy and not members and bool(events))
    target = events if event_scope else members | events
    scope = "event_members" if event_scope else "sector_plus_event_members" if events else "sector_members"
    core_outside = sorted(set(core) - target)
    core = set(core).intersection(target)
    quotes = market.body.get("quotes", {}) if market else {}
    research = market.body.get("stock_research", {}) if market else {}
    stock_quotes = research.get("quotes", {})
    iopvs = market.body.get("iopv", {}).get("readings", {}) if market else {}
    products, eligible = [], {}
    missing_weights, missing_indexes = 0, set()
    quote_cache = {} if quote_cache is None else quote_cache
    weight_cache = {} if weight_cache is None else weight_cache
    for etf, product in sorted(mapping["products"].items()):
        if not isinstance(product, dict) or not CODE.fullmatch(etf):
            continue
        index = product.get("index_code")
        raw_basis = mapping["weights"].get(index)
        if index not in weight_cache:
            weight_cache[index] = _dated_weights(raw_basis, at) if direction_policy else weights(raw_basis, at.date())
        basis = weight_cache[index]
        if basis is None:
            # A catalog entry without weights has no demonstrated relationship
            # to this sector. Keep the coverage gap without inventing a candidate.
            missing_weights += 1
            if isinstance(index, str):
                missing_indexes.add(index)
            continue
        weight_date, rows, w = basis
        complete_weights = mapping["weights"][index].get("complete") is True
        if not target.intersection(w):
            continue
        quote = quotes.get(etf) or {}
        ref = valid_quote(quote, at)
        usable = _usable_stock_quotes(stock_quotes, at, ref or (at if direction_policy else None), quote_cache)
        m = _metrics(rows, w, target, core, usable, at)
        m["E_sector"] = sum((w.get(code, Decimal(0)) for code in sorted(members)), Decimal(0)) if members else None
        m["core_outside_target"] = core_outside
        stats = {**(product.get("stats") or {}), **(product.get("recommendation_stats") or {})}
        history = history_ready(stats, at)
        adv20 = number(stats.get("adv20")) if history and history_count(stats) >= 20 else None
        adv5 = number(stats.get("adv5")) if history and history_count(stats) >= 5 else None
        aum = number(stats.get("aum_yuan")) if stats.get("aum_verified") is True else None
        try:
            aum_day = date.fromisoformat(stats["aum_as_of"])
            aum_until = date.fromisoformat(stats["aum_valid_until"])
            if not aum_day <= at.date() <= aum_until or moment(stats["aum_available_at"]) > at:
                aum = None
        except (KeyError, TypeError, ValueError):
            aum = None
        main_status = "PASS" if m["M"] >= Decimal(".65") else "UNKNOWN" if m["M"]+m["M_unknown"] >= Decimal(".65") else "FAIL"
        rule = product.get("rule") or {}
        product_status = "PASS" if _verified_product(product, at, legacy=True) else "UNKNOWN"
        try:
            rule_available = rule.get("verified") is True and moment(rule["verified_at"]) <= at
        except (KeyError,TypeError,ValueError):
            rule_available = False
        if rule_available and (rule.get("scope") not in (None,"domestic_passive_sector")
                or (number(rule.get("price_limit_ratio")) is not None
                    and number(rule["price_limit_ratio"]) != Decimal(".1"))
                or rule.get("index_code") not in (None,index)):
            product_status = "FAIL"
        exposure = m["E"] if scope == "event_members" else m["E_sector"]
        threshold = Decimal(".30") if scope == "event_members" else Decimal(".50")
        checks = {
            "product": check(product_status, "PRODUCT_RULE", rule.get("scope")),
            "weights": check("PASS" if complete_weights else "UNKNOWN", "COMPLETE_WEIGHTS" if complete_weights else "PARTIAL_WEIGHTS", weight_date.isoformat()),
            "mainboard": check(main_status, "MIN_MAINBOARD_65_PERCENT", m["M"]),
            "exposure": check("PASS" if exposure >= threshold else "FAIL" if complete_weights and (complete_members or scope == "event_members") else "UNKNOWN", "MIN_EXPOSURE", exposure),
            "liquidity": check("UNKNOWN" if adv20 is None else "PASS" if adv20 >= MIN_ADV20 else "FAIL",
                               "ADV20_UNKNOWN" if adv20 is None else "MIN_ADV20_30_MILLION", adv20),
            "aum": check("UNKNOWN" if aum is None else "PASS" if aum >= MIN_AUM else "FAIL",
                         "AUM_UNAVAILABLE" if aum is None else "MIN_AUM_200_MILLION", aum,
                         required=False),
        }
        if core_status != "VERIFIED":
            checks["core"] = check("UNKNOWN", "CORE_NOT_RESEARCHED")
        elif len(core) < 2:
            checks["core"] = check("NOT_APPLICABLE", "FEWER_THAN_TWO_VERIFIED_CORES", len(core))
        else:
            checks["core"] = check("PASS" if m["Cw"] >= Decimal(".12") or m["Hit10"] >= 2 else "FAIL",
                                   "CORE_WEIGHT_OR_TOP10", {"Cw":str(m["Cw"]), "Hit10":m["Hit10"]})
        # Geometry is separate from evidence/representative quality.
        static_status = aggregate([checks[key] for key in ("product", "weights", "mainboard", "exposure")])
        result = momentum(quote, m, stats, at, iopv=iopvs.get(etf))
        from .findesk_iopv import usable_reading
        result["iopv"] = iopvs.get(etf) if usable_reading(iopvs.get(etf), at) else None
        result["checks"] = {**checks, **result["checks"]}
        known_static_fail = static_status == "FAIL" or checks["liquidity"]["result"] == "FAIL" or checks["aum"]["result"] == "FAIL"
        if known_static_fail:
            status = "REJECTED"
        elif result["execution_status"] == "FAILED" or result["momentum_status"] == "FAIL":
            status = "STATIC_ONLY" if static_status == "PASS" else "PENDING"
        elif (static_status != "PASS" or checks["liquidity"]["result"] == "UNKNOWN"
              or checks["core"]["result"] in ("FAIL", "UNKNOWN") or result["momentum_status"] != "PASS"):
            status = "PENDING"
        else:
            status = "CONFIRMED"
        quality = []
        if (at.date()-weight_date).days > 60:
            quality.append("OLDER_MAPPING")
        quality.extend(row["reason"] for row in checks.values() if row["result"] == "UNKNOWN")
        rejection_reasons = [row["reason"] for row in checks.values() if row["result"] == "FAIL"]
        result.update(etf=etf,index=index,weight_as_of=weight_date.isoformat(),static_status=static_status,
            exposure_basis="COMPLETE" if complete_weights and complete_members else "KNOWN_LOWER_BOUND",
            coverage=dict(known_members=len(members), reported_members=sector.get("reported_count"),
                          known_weight=str(sum(w.values())), weights_complete=complete_weights,
                          members_complete=complete_members, weight_source=mapping["weights"][index].get("source")),
            recommendation_status=status,representative_current=False,quality=quality,rejection_reasons=rejection_reasons,
            metrics={key:str(value) if isinstance(value, Decimal) else value for key,value in m.items()},
            matched=[dict(code=row["code"], weight=str(w[row["code"]]),core=row["code"] in core)
                     for row in rows if row["code"] in target])
        products.append(result)
        if not known_static_fail and static_status == "PASS":
            rank=(-adv20 if adv20 is not None else Decimal(0),-adv5 if adv5 is not None else Decimal(0),
                  result["spread"] is None, Decimal(result["spread"]) if result["spread"] is not None else Decimal(0),etf)
            eligible.setdefault(index,[]).append((result,rank,m))
    representatives, static_representatives = [], []
    for index, group in eligible.items():
        group.sort(key=lambda x:x[1])
        static_representatives.append(group[0])
        current=[item for item in group if item[0]["quote_current"] and item[0]["execution_status"] != "FAILED"]
        if current:
            representative=current[0]
            representatives.append(representative)
        for result,_,_ in group:
            result["alternatives"]=[row[0]["etf"] for row in group if row[0]["etf"] != result["etf"]]
    rank=lambda row:(-row[2]["Cw"],-row[2]["Hit10"],-row[2]["E"],-row[2]["M"],str(row[0]["index"]))
    representatives.sort(key=rank)
    static_representatives.sort(key=rank)
    for representative,_,_ in representatives[:3]:
        representative["representative_current"]=True
    return dict(scope_mode=scope,core_status=core_status,core_codes=sorted(core),core_outside_target=core_outside,
                reason=None if complete_members else "SECTOR_MEMBERS_UNAVAILABLE",
                products=products,representatives=[item[0]["etf"] for item in representatives[:3]],
                static_representatives=[item[0]["etf"] for item in static_representatives[:3]],
                coverage_gaps=dict(weights=dict(reason="WEIGHTS_UNVERIFIED", products=missing_weights,
                                                indexes=len(missing_indexes))),
                evaluation_complete=True,evaluated_products=len(products),evaluation_scope="MAPPED_PRODUCTS")


def evaluate_recommendations_legacy(report_event, mapping, market, *, core_leaders=None, event_members=None,
                                    _direction_policy=False):
    """Evaluate the new recommendation version separately from immutable automatic candidates."""
    from .momentum import check, valid_quote
    report = normalize(report_event)
    raw = document(report["raw"])
    at = moment(report["at"])
    explanations, quote_cache, weight_cache = {}, {}, {}
    with localcontext(PRECISION):
        for sid, signal in report["sectors"].items():
            source = raw["sectors"][signal["row"]]
            core_entry = (core_leaders or {}).get(sid) or {}
            codes = core_entry.get("codes", [])
            status = core_entry.get("status", "UNKNOWN")
            stock_quotes = market.body.get("stock_research",{}).get("quotes",{}) if market else {}
            evidence = core_entry.get("evidence") or []
            verified = {row.get("code") for row in evidence if isinstance(row,dict) and row.get("ref")}
            if (status != "VERIFIED" or not isinstance(codes,list) or len(codes)>8
                    or any(not isinstance(code,str) or not STOCK_ID.fullmatch(code) or code not in verified
                           or valid_quote(stock_quotes.get(code),at) is None for code in codes)):
                codes, status = [], "UNKNOWN"
            events = _verified_events((event_members or {}).get(sid),at,set(signal.get("sources",[])))
            details = _recommend_sector(mapping,sid,market,at,codes,status,events,quote_cache=quote_cache,
                                        direction_policy=_direction_policy, weight_cache=weight_cache)
            positive = signal["phase"] in ("NEW","MAINTAIN","STRENGTHEN")
            details["direction"] = signal["phase"]
            for product in details["products"]:
                product["checks"]["direction"] = check("PASS" if positive else "UNKNOWN",
                    "POSITIVE_DIRECTION" if positive else "DIRECTION_NOT_POSITIVE",signal["phase"])
                if not positive and product["recommendation_status"] == "CONFIRMED":
                    product["recommendation_status"] = "PENDING"
            explanations[sid] = details
    return dict(method=RECOMMENDATION_METHOD,automatic_method=METHOD,mapping_version=mapping["version"],
                market_snapshot=market.snapshot_id if market else None,sectors=explanations,
                mapping_scope=dict(sectors=len(mapping["sectors"]),products=len(mapping["products"]),indexes=len(mapping["weights"])))


DIRECTION_METHOD = "etf-direction-first-v2"
MAX_ETF_CHOICES = 3


def _execution_facts(code, product, quote, at, *, market_body=None):
    """Only product identity, actual trading rules and usable prices govern entry."""
    from .inputs import CLOSED_STATUS
    from .momentum import check, valid_quote
    blocked, pending = [], []
    market_body = market_body or {}
    if market_body.get("mode") == "EOD":
        blocked.append("EOD_OBSERVATION_ONLY")
    if market_body.get("trade_date", at.date().isoformat()) != at.date().isoformat():
        pending.append("MARKET_DATE_MISMATCH")
    rule = product.get("rule") if isinstance(product.get("rule"), dict) else {}
    reason = product_entry_reason(product, at) if CODE.fullmatch(code) else "PRODUCT_IDENTITY_INVALID"
    rule_blocks = {"PRODUCT_IDENTITY_INVALID", "PRODUCT_OUTSIDE_SCOPE", "PRODUCT_NOT_LISTED", "SETTLEMENT_RULE_CONFLICT"}
    if reason:
        (blocked if reason in rule_blocks else pending).append(reason)
    try:
        unit = integer(product["trade_unit"])
    except (KeyError, ValueError, TypeError):
        unit = None
        if "TRADE_UNIT_UNVERIFIED" not in pending:
            pending.append("TRADE_UNIT_UNVERIFIED")
    quote = quote if isinstance(quote, dict) else {}
    source = quote.get("source", market_body.get("source"))
    price_quote = {**quote, "price_cny":quote.get("price_cny", quote.get("last"))}
    status = str(quote.get("trading_status", "")).upper()
    if status in CLOSED_STATUS | {"HALTED", "DELISTED"} or quote.get("tradeable") is False:
        blocked.append("TRADING_UNAVAILABLE")
    elif not isinstance(source, str) or not source.strip() or valid_quote(price_quote, at) is None:
        pending.append("USABLE_PRICE_UNAVAILABLE")
    state = "BLOCKED" if blocked else "PENDING" if pending else "READY"
    checks = {
        "product": check("PASS" if verified_product(product, at) else "FAIL" if reason in rule_blocks else "UNKNOWN",
                         _product_rule_reason(product, at) or "VERIFIED_PRODUCT_RULE", rule.get("scope")),
        "trade_unit": check("PASS" if unit else "UNKNOWN", "VERIFIED_TRADE_UNIT", unit),
        "entry_quote": check("FAIL" if "TRADING_UNAVAILABLE" in blocked else
                             "UNKNOWN" if "USABLE_PRICE_UNAVAILABLE" in pending else "PASS",
                             "USABLE_SAME_DAY_PRICE", quote.get("observed_at")),
    }
    for item in checks.values():
        item["effect"] = "EXECUTION"
    return dict(execution_eligibility=state, execution_reasons=blocked + pending,
                execution_checks=checks, trade_unit=unit)


def _fact_rank(item, mapping, held):
    adv20 = number(((item.get("checks") or {}).get("liquidity") or {}).get("value"))
    return (item["etf"] not in held, item.get("execution_eligibility") != "READY",
            -(number(item.get("metrics", {}).get("E")) or Decimal(0)),
            -(adv20 or Decimal(0)), -len(item.get("matched", [])), item["etf"])


def _explicit_relations(mapping, sid, at):
    relations = {}
    for row in mapping.get("sector_index_relations", []):
        if (not isinstance(row, dict) or row.get("sector_id") != sid or row.get("verified") is not True
                or not isinstance(row.get("index_code"), str) or not row["index_code"]
                or not isinstance(row.get("evidence_refs"), list) or not row["evidence_refs"]
                or any(not isinstance(ref, str) or not ref for ref in row["evidence_refs"])):
            continue
        try:
            if moment(row["available_at"]) > at or (row.get("as_of_date") and date.fromisoformat(row["as_of_date"]) > at.date()):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        relations[row["index_code"]] = row
    return relations


def _add_explicit_relations(details, mapping, sid, market, at, *, representation=None):
    """A verified dated sector/index link supplies association, never invented weights."""
    from .momentum import check, evaluate as momentum
    relations = _explicit_relations(mapping, sid, at)
    existing = {item["etf"] for item in details["products"]}
    for code, product in research_products(mapping, at).items():
        index = product["index_code"]
        if code in existing or index not in relations:
            continue
        # A known weight basis with no intersecting members remains a factual
        # contradiction to review, rather than silently replacing that evidence.
        if representation is None and weights(mapping.get("weights", {}).get(index), at.date()) is not None:
            continue
        quote = (market.body.get("quotes", {}) if market else {}).get(code) or {}
        stats = {**(product.get("stats") or {}), **(product.get("recommendation_stats") or {})}
        metrics = dict.fromkeys(("E", "E_sector", "M", "M_unknown", "Q", "Qt", "Cw", "Cc", "Hit10",
                                 "R_top3", "U_active", "R_est", "core_count"))
        item = momentum(quote, metrics, stats, at)
        item["checks"].update(weights=check("UNKNOWN", "WEIGHTS_UNVERIFIED"),
                              exposure=check("UNKNOWN", "EXPOSURE_UNAVAILABLE"))
        item.update(etf=code, index=index, weight_as_of=None, static_status="UNKNOWN", matched=[],
                    metrics=metrics, quality=["WEIGHTS_UNVERIFIED"], rejection_reasons=[],
                    association_status="VERIFIED", association_basis="EXPLICIT_INDEX_RELATION",
                    association_evidence=relations[index]["evidence_refs"], exposure_basis="UNAVAILABLE",
                    coverage=dict(known_members=None, reported_members=None, known_weight=None,
                                  weights_complete=False, members_complete=False, weight_source=None))
        details["products"].append(item)
    if relations:
        details["evaluated_products"] = len(details["products"])


def _reference_stats(product, at):
    """Small, dated statistics from existing storage, with no threshold verdicts."""
    from .momentum import history_count

    def group(prefix):
        candidates = [value for value in (product.get("recommendation_stats"), product.get("stats"))
                      if isinstance(value, dict) and any(key.startswith(prefix + "_")
                          or (prefix == "history" and key in ("adv20", "adv5")) for key in value)]
        available = []
        for value in candidates:
            try:
                day = date.fromisoformat(value[prefix + "_as_of"])
                seen = moment(value[prefix + "_available_at"])
                if seen <= at and (day < at.date() if prefix == "history" else day <= at.date()):
                    available.append((day, seen, value))
            except (KeyError, TypeError, ValueError):
                continue
        return max(available, key=lambda row: row[:2])[2] if available else (candidates[0] if candidates else {})

    def fact(stats, key, prefix, days=None):
        observed, available = stats.get(prefix + "_as_of"), stats.get(prefix + "_available_at")
        until, source = stats.get(prefix + "_valid_until"), stats.get(prefix + "_source")
        row = dict(value=None, unit="CNY" if days is None else "CNY/trading_day",
                   as_of_date=observed, available_at=available, valid_until=until,
                   source=source if isinstance(source, str) else None,
                   missing=True, expired=None, age_days=None, status="MISSING")
        if days is not None:
            batches = stats.get("history_batches")
            batches = [value for value in batches if isinstance(value, str)] if isinstance(batches, list) else []
            row.update(covered_days=history_count(stats), required_days=days,
                       source_batches=batches[:4], source_batch_count=len(batches))
        try:
            day = date.fromisoformat(observed)
            seen = moment(available)
        except (TypeError, ValueError):
            if stats:
                row["status"] = "TIMING_UNVERIFIED"
            return row
        if seen > at or (day >= at.date() if days is not None else day > at.date()):
            row["status"] = "NOT_YET_AVAILABLE"
            return row
        row["age_days"] = (at.date() - day).days
        if until is not None:
            try:
                expiry = date.fromisoformat(until)
                if expiry < day:
                    raise ValueError("validity precedes source date")
                row["expired"] = at.date() > expiry
            except (TypeError, ValueError):
                row["status"] = "VALIDITY_UNVERIFIED"
                return row
        value = number(stats.get(key))
        if days is None and stats.get("aum_verified") is not True:
            row["status"] = "UNVERIFIED"
        elif days is not None and history_count(stats) < days:
            row["status"] = "INSUFFICIENT_HISTORY"
        elif value is None or value < 0:
            row["status"] = "MISSING" if stats.get(key) is None else "INVALID"
        else:
            row.update(value=str(value), missing=False, status="EXPIRED" if row["expired"] else "AVAILABLE")
        return row

    aum, history = group("aum"), group("history")
    return dict(effect="REFERENCE", aum_yuan=fact(aum, "aum_yuan", "aum"),
                adv20=fact(history, "adv20", "history", 20), adv5=fact(history, "adv5", "history", 5))


def freeze_pair_facts(mapping, at, sector_ids, policy):
    """Compute the complete associated pair set from one point-in-time mapping."""
    products = research_products(mapping, at)
    indexes = {item["index_code"] for item in products.values()}
    bases = {idx: _dated_weights(mapping.get("weights", {}).get(idx), at) for idx in indexes}
    taxonomy = mapping.get("etf_taxonomy") or {}
    attributed = {(row["sector_id"], row["index_code"]) for row in taxonomy.get("rows", ())
                  if policy.get("taxonomy_version") and taxonomy.get("version") == policy["taxonomy_version"]
                  and row.get("evidence_refs") and moment(row["available_at"]) <= at}
    facts = {}
    for sid in dict.fromkeys(sector_ids):
        members, complete = _formal_members(mapping, sid, at)
        relations = _explicit_relations(mapping, sid, at)
        by_index = {}
        for index, source in bases.items():
            linked = index in relations
            known = source[2] if source else {}
            if not linked and members.isdisjoint(known):
                continue
            raw = mapping.get("weights", {}).get(index) or {}
            proof = raw.get("absolute_weight_proof")
            by_index[index] = evaluate_pair_representativeness(
                members, known, sector_members_complete=complete,
                weights_complete_and_valid=source is not None and raw.get("complete") is True,
                partial_upper_bound_proven=bool(source and isinstance(proof, dict)
                    and proof.get("scope") == "FULL_INDEX_ABSOLUTE"
                    and proof.get("index_code") == index and proof.get("as_of_date") == raw.get("as_of_date")
                    and proof.get("source_ref") and raw.get("available_at")),
                explicit_relation=linked, attributed=(sid, index) in attributed, policy=policy)
            by_index[index].update(explicit_relation=linked, weight_available=source is not None,
                                   weight_complete=source is not None and raw.get("complete") is True,
                                   source_time=raw.get("available_at") if source else relations.get(index, {}).get("available_at"))
        facts[sid] = {
            code: dict(by_index[p["index_code"]], sector_id=sid, etf=code, index=p["index_code"],
                       policy_mode=policy["policy_mode"], policy_version=policy["policy_version"],
                       parameter_digest=policy["parameter_digest"], mapping_version=policy["mapping_version"],
                       fact_refs=["etf_exposure_" + code.replace(".", "_")]
                       if by_index[p["index_code"]]["known_exposure"] is not None else [])
            for code, p in products.items() if p["index_code"] in by_index
        }
    return facts


def prepare_etf_facts(mapping, market, at, sector_ids, *, held_codes=(), limit=8, representation=None):
    """Bounded shared facts for AI selection; no strategy preference filters this list."""
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("invalid ETF facts limit")
    at = moment(at) if isinstance(at, str) else at
    held = set(held_codes)
    result, quote_cache, weight_cache = {}, {}, {}
    for sid in dict.fromkeys(sector_ids):
        details = _recommend_sector(mapping, sid, market, at, [], "UNKNOWN", set(),
                                    quote_cache=quote_cache, direction_policy=True, weight_cache=weight_cache)
        pairs = representation.get(sid, {}) if representation is not None else None
        _add_explicit_relations(details, mapping, sid, market, at, representation=pairs)
        for item in details["products"]:
            quote = (market.body.get("quotes", {}) if market else {}).get(item["etf"]) or {}
            item.update(_execution_facts(item["etf"], mapping["products"][item["etf"]], quote, at,
                                        market_body=market.body if market else None))
        ranked = sorted(details["products"], key=lambda item: _fact_rank(item, mapping, held))
        chosen, indexes, ready_indexes = [], set(), set()
        observations = []
        held_count = sum(item["etf"] in held for item in ranked)
        for item in ranked:
            ready_alternative = item["execution_eligibility"] == "READY" and item["index"] not in ready_indexes
            pair = pairs.get(item["etf"]) if pairs is not None else None
            rejected = pairs is not None and not (pair and pair["entry_allowed"])
            if rejected and (not pair or (len(observations) >= limit and item["etf"] not in held)):
                continue
            if not rejected and item["etf"] not in held and (len(chosen) >= max(limit, held_count)
                    or (item["index"] in indexes and not ready_alternative)):
                continue
            product = mapping["products"][item["etf"]]
            quote = (market.body.get("quotes", {}) if market else {}).get(item["etf"]) or {}
            execution = {key:item[key] for key in ("execution_eligibility", "execution_reasons", "execution_checks", "trade_unit")}
            option = dict(etf=item["etf"], name=product.get("name"), index=item["index"],
                weight_as_of=item["weight_as_of"], association_status="VERIFIED",
                association_basis=item.get("association_basis", "WEIGHTED_MEMBERS"),
                association_evidence=item.get("association_evidence", []), exposure_basis=item["exposure_basis"],
                metrics=item["metrics"], coverage=item["coverage"], reference_stats=_reference_stats(product, at),
                matched_count=len(item["matched"]),
                matched=item["matched"][:8], held=item["etf"] in held,
                quote=dict(price_cny=quote.get("price_cny", quote.get("last")), observed_at=quote.get("observed_at"),
                           source=quote.get("source", market.body.get("source") if market else None),
                           trading_status=quote.get("trading_status")),
                rule=dict(scope=(product.get("rule") or {}).get("scope"),
                          verified=verified_product(product, at),
                          review_required=(product.get("rule") or {}).get("review_required") is True,
                          settlement_cycle=product_settlement(product.get("rule") or {}),
                          price_limit_ratio=(product.get("rule") or {}).get("price_limit_ratio")),
                **execution)
            if pair is not None:
                option.update(representativeness=pair, exposure_basis=pair["exposure_basis"],
                              matched_count=pair["matched_count"])
            if rejected:
                observations.append(option)
                continue
            chosen.append(option)
            indexes.add(item["index"])
            if item["execution_eligibility"] == "READY":
                ready_indexes.add(item["index"])
        result[sid] = dict(products=chosen, coverage_gaps=details.get("coverage_gaps", {}),
                           reason=details.get("reason"), associated_products=len(ranked),
                           omitted_products=max(0, len(ranked)-len(chosen)), method=DIRECTION_METHOD,
                           mapping_version=mapping["version"])
        if pairs is not None:
            result[sid].update(observation_products=observations, method=V3_METHOD,
                               omitted_count=max(0, len(pairs)-len(observations)-len(chosen)))
    return result


def evaluate_recommendations(report_event, mapping, market, *, core_leaders=None, event_members=None,
                             etf_choices=None, representation=None, selection_audit=None):
    """One direction-first result for notification, workbench and new signal entries."""
    report = normalize(report_event)
    at = moment(report["at"])
    result = evaluate_recommendations_legacy(report_event, mapping, market, core_leaders=core_leaders,
                                            event_members=event_members, _direction_policy=True)
    result.update(method=DIRECTION_METHOD, automatic_method=DIRECTION_METHOD)
    if representation is not None:
        result.update(method=V3_METHOD, automatic_method=V3_METHOD)
    all_choices = etf_choices if isinstance(etf_choices, dict) else {}
    for sid, details in result["sectors"].items():
        pairs = representation.get(sid, {}) if representation is not None else None
        if pairs is not None and details["scope_mode"] != "sector_members":
            already = {item["etf"] for item in details["products"]}
            formal = _recommend_sector(mapping, sid, market, at, [], "UNKNOWN", set(), direction_policy=True)
            details["products"].extend(item for item in formal["products"]
                                       if item["etf"] in pairs and item["etf"] not in already)
        _add_explicit_relations(details, mapping, sid, market, at, representation=pairs)
        products = {item["etf"]: item for item in details["products"]}
        for code, item in products.items():
            quote = (market.body.get("quotes", {}) if market else {}).get(code) or {}
            item.update(_execution_facts(code, mapping["products"][code], quote, at,
                                        market_body=market.body if market else None))
        requested = all_choices.get(sid)
        valid_selection = (isinstance(requested, list) and len(requested) <= MAX_ETF_CHOICES
            and all(isinstance(code, str) and code in products for code in requested)
            and len(set(requested)) == len(requested))
        if valid_selection and pairs is not None:
            requested = [code for code in requested if pairs.get(code, {}).get("entry_allowed") is True]
        selected, indexes = [], set()
        if valid_selection:
            ordered = sorted(requested, key=lambda code:(products[code]["execution_eligibility"] != "READY", requested.index(code)))
            for code in ordered:
                index = products[code]["index"]
                if index not in indexes:
                    selected.append(code)
                    indexes.add(index)
            selected.sort(key=requested.index)
        details["selection_status"] = "CONFIRMED" if valid_selection else "UNCONFIRMED"
        details["selection_reasons"] = [] if valid_selection else ["ETF_SELECTION_UNCONFIRMED"]
        if selection_audit and sid in selection_audit:
            details["selection_audit"] = selection_audit[sid]
        details["requested_choices"] = list(requested) if valid_selection else []
        details["selection_duplicates"] = [code for code in requested if code not in selected] if valid_selection else []
        details["representatives"] = selected
        details["direction_state"] = report["sectors"][sid]["state"]
        positive = details["direction_state"] == "UP"
        static, static_indexes = [], set()
        for item in sorted(products.values(), key=lambda row: _fact_rank(row, mapping, set(selected))):
            code = item["etf"]
            pair = pairs.get(code) if pairs is not None else None
            if pair is not None:
                item["representativeness"] = pair
            if (pairs is None or pair and pair["entry_allowed"]) and item["index"] not in static_indexes and len(static) < MAX_ETF_CHOICES:
                static.append(code)
                static_indexes.add(item["index"])
            item["reference_static_status"] = item["static_status"]
            item["reference_execution_status"] = item["execution_status"]
            for check in item["checks"].values():
                check.update(required=False, effect="REFERENCE")
            execution = {key:item[key] for key in ("execution_eligibility", "execution_reasons", "execution_checks", "trade_unit")}
            item["checks"].update(execution["execution_checks"])
            item["association_status"] = "VERIFIED"
            item.setdefault("association_basis", "WEIGHTED_MEMBERS")
            item["static_status"] = "PASS"
            item["ai_selected"] = code in selected
            item["selection_basis"] = "AI_CHOICE" if code in selected else "ASSOCIATION_OBSERVATION"
            eligible = execution["execution_eligibility"]
            item["execution_status"] = {"READY":"COMPLETE", "PENDING":"PARTIAL", "BLOCKED":"FAILED"}[eligible]
            item["rejection_reasons"] = execution["execution_reasons"] if eligible == "BLOCKED" else []
            item["reference_reasons"] = [row["reason"] for row in item["checks"].values()
                                         if row.get("effect") == "REFERENCE" and row["result"] in ("FAIL", "UNKNOWN")]
            item["quality"] = list(dict.fromkeys(item.get("quality", []) + execution["execution_reasons"]
                                                + item["reference_reasons"]))
            item["recommendation_status"] = ("OBSERVE" if code not in selected or not positive else
                "CONFIRMED" if eligible == "READY" else "REJECTED" if eligible == "BLOCKED" else "PENDING")
            item["representative_current"] = code in selected and positive and eligible == "READY"
        details["static_representatives"] = static
    _global_index_representatives(result)
    result["candidates"] = recommendation_candidates(result)
    return result


def _global_index_representatives(result):
    """Choose only among explicit executable choices; keep each sector's own intent."""
    groups = {}
    for sid, details in result["sectors"].items():
        ranks = {code:rank for rank, code in enumerate(details["representatives"], 1)}
        for item in details["products"]:
            if item["recommendation_status"] == "CONFIRMED":
                adv20 = number((item["checks"].get("liquidity") or {}).get("value")) or Decimal(0)
                groups.setdefault(item["index"], []).append((ranks[item["etf"]], -adv20, item["etf"], sid, item))
    for rows in groups.values():
        winner = min(rows, key=lambda row:row[:3])[2]
        source_sectors = sorted({sid for _, _, code, sid, _ in rows if code == winner})
        for _, _, code, _, item in rows:
            if code != winner:
                item.update(recommendation_status="OBSERVE", representative_current=False,
                            same_index_alternative=winner,
                            selection_basis="AI_CHOICE_SAME_INDEX_ALTERNATIVE")
            else:
                item["selected_source_sectors"] = source_sectors


def recommendation_candidates(result):
    """Project the same selected facts into the existing signal candidate contract."""
    candidates = []
    for sid, details in result.get("sectors", {}).items():
        products = {item["etf"]: item for item in details.get("products", [])}
        for rank, code in enumerate(details.get("representatives", []), 1):
            item = products.get(code, {})
            if (details.get("selection_status") == "CONFIRMED" and item.get("ai_selected") is True
                    and item.get("association_status") == "VERIFIED"
                    and item.get("recommendation_status") == "CONFIRMED"
                    and item.get("execution_eligibility") == "READY"):
                candidates.append(dict(etf=code, sector_id=sid, rank=rank, mapping_ref=result["mapping_version"]))
    return candidates
