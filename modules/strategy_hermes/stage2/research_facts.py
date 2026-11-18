"""Scoped numeric evidence for model judgments; rendering owns every current number."""
from decimal import Decimal
import re

from ..simulator import moment
from .penetration import number
from .stock_research import BREADTH_ALIGNMENT_SECONDS, _usable_quote

NUMERIC_CONTRACT = "scoped-facts-v1"
ETF_SELECTION_CONTRACT = "explicit-etfs-v1"
MAX_FACT_REFS = 4
_CHINESE_QUANTITY = re.compile(r"百分之|[零〇一二两三四五六七八九十百千万]+(?:成|只|家|倍|个百分点|分之)")


def _percent(value, *, signed=False):
    return format(Decimal(value), "+.2%" if signed else ".2%")


def _fact(scope, subject, text, metrics, refs=(), times=()):
    return dict(scope=scope, subject_id=subject, text=text, metrics=metrics,
                source_refs=list(dict.fromkeys(ref for ref in refs if ref)),
                source_times=list(dict.fromkeys(at for at in times if at)))


def market_facts(market, at):
    """Use the same current-session, two-minute stock alignment as sector breadth."""
    if market is None or market.body.get("trade_date") != at.date().isoformat():
        return {}
    result, boards = {}, []
    for row in market.body.get("sectors", ()):
        try:
            observed, value = moment(row["observed_at"]), number(row["return_ratio"])
            if observed.date() == at.date() and observed <= at and value is not None:
                boards.append((row, value))
        except (ValueError, KeyError, TypeError):
            continue
    if boards:
        up = sum(value > 0 for _, value in boards)
        down = sum(value < 0 for _, value in boards)
        metrics = dict(observed_count=len(boards), up=up, down=down, flat=len(boards)-up-down)
        result["market_boards"] = _fact("MARKET_BOARDS", "shared_boards",
            f"已采集板块：{len(boards)}个，上涨{up}个、下跌{down}个，上涨占比{Decimal(up)/len(boards):.2%}",
            metrics, [row.get("source_batch") for row, _ in boards],
            [row["observed_at"] for row, _ in boards])
    research = market.body.get("stock_research", {})
    values = []
    for row in research.get("quotes", {}).values():
        if not _usable_quote(row, at):
            continue
        value = number(row.get("return_ratio"))
        if value is not None and (at-moment(row["observed_at"])).total_seconds() <= BREADTH_ALIGNMENT_SECONDS:
            values.append((row, value))
    if values:
        up = sum(value > 0 for _, value in values)
        down = sum(value < 0 for _, value in values)
        metrics = dict(observed_count=len(values), up=up, down=down, flat=len(values)-up-down,
                       alignment_seconds=BREADTH_ALIGNMENT_SECONDS)
        result["market_stocks"] = _fact("MARKET_STOCKS", "shared_a_share_quotes",
            f"已采集且对齐的A股：{len(values)}只，上涨{up}只、下跌{down}只，上涨占比{Decimal(up)/len(values):.2%}",
            metrics, [research.get("batch")],
            [min(row["observed_at"] for row, _ in values), max(row["observed_at"] for row, _ in values)])
    return result


def fact_catalog(evidence, common):
    sid, name, facts = evidence["sector_id"], evidence["sector_name"], evidence["facts"]
    current = evidence.get("current", ())
    refs, times = [row.get("ref") for row in current], [row.get("observed_at") for row in current]
    result = {}
    if number(facts.get("board_return_ratio")) is not None:
        result["sector_return"] = _fact("SECTOR", sid,
            f"{name}板块涨跌{_percent(facts['board_return_ratio'], signed=True)}",
            dict(board_return_ratio=facts["board_return_ratio"]), refs, times)
    change, compared = facts.get("board_return_change_ratio"), facts.get("comparison_observed_at")
    if number(change) is not None and compared:
        result["sector_change"] = _fact("SECTOR", sid,
            f"{name}较{moment(compared):%H:%M}日涨幅变化{Decimal(change)*100:+.2f}个百分点",
            dict(board_return_change_ratio=change, comparison_observed_at=compared),
            [*refs, facts.get("comparison_ref")], [*times, compared])
    breadth = facts.get("breadth", {})
    if breadth.get("observed_count") and number(breadth.get("up_ratio")) is not None:
        count, up = breadth["observed_count"], breadth["up"]
        text = f"{name}成分股：已报价{count}/{breadth['member_count']}只，上涨{up}只（{_percent(breadth['up_ratio'])}）"
        if number(breadth.get("median_return")) is not None:
            text += f"，涨幅中位数{_percent(breadth['median_return'], signed=True)}"
        result["sector_breadth"] = _fact("SECTOR_MEMBERS", sid, text,
            {key: breadth.get(key) for key in ("member_count", "observed_count", "up", "down", "flat", "up_ratio", "median_return")},
            [breadth.get("batch")], [breadth.get("oldest_at"), breadth.get("newest_at")])
    for row in evidence.get("stock_evidence", ()):
        value = number(row.get("return_ratio"))
        if value is not None:
            result["stock_" + row["code"].replace(".", "_")] = _fact("STOCK", row["code"],
                f"{row['code']}涨跌{_percent(value, signed=True)}", dict(return_ratio=str(value)),
                [row.get("ref")], [row.get("observed_at")])
    options = evidence.get("etf_options", {})
    for row in options.get("products", ()):
        code, quote = row["etf"], row.get("quote", {})
        exposure = number(row.get("metrics", {}).get("E_sector"))
        if exposure is not None:
            basis = "完整权重" if row.get("exposure_basis") == "COMPLETE" else "已知部分权重"
            result["etf_exposure_" + code.replace(".", "_")] = _fact("ETF_SECTOR", code,
                f"{code}按{basis}计算的{name}暴露{_percent(exposure)}",
                dict(sector_id=sid, sector_exposure=str(exposure), exposure_basis=row.get("exposure_basis")),
                [options.get("mapping_version")], [row.get("weight_as_of")])
        if number(quote.get("price_cny")) is not None and quote.get("observed_at") and quote.get("source"):
            try:
                observed = moment(quote["observed_at"])
                if observed <= moment(evidence["data_as_of"]):
                    result["etf_quote_" + code.replace(".", "_")] = _fact("ETF_QUOTE", code,
                        f"{code}参考价{quote['price_cny']}元（源时间{observed:%m-%d %H:%M:%S}）",
                        dict(price_cny=quote["price_cny"]), [quote["source"]], [quote["observed_at"]])
            except (ValueError, TypeError):
                pass
    result.update(common)
    return result


def _has_unbound_number(text, evidence):
    """Known source dates, clocks and instrument IDs are references, not invented metrics."""
    allowed = {evidence.get("trade_date", ""), evidence.get("sector_name", "")}
    rows = [row for key in ("current", "previous", "stock_evidence") for row in evidence.get(key, ())]
    rows.extend(evidence.get("etf_options", {}).get("products", ()))
    for row in rows:
        for key in ("code", "etf"):
            if isinstance(row.get(key), str):
                allowed.update((row[key], row[key].split(".")[0]))
        if isinstance(row.get("name"), str):
            allowed.add(row["name"])
        if isinstance(row.get("weight_as_of"), str):
            allowed.add(row["weight_as_of"])
        stamp = row.get("observed_at") or (row.get("quote") or {}).get("observed_at")
        if isinstance(stamp, str):
            try:
                at = moment(stamp)
                allowed.update((stamp, at.date().isoformat(), at.strftime("%H:%M:%S"), at.strftime("%H:%M")))
            except ValueError:
                pass
    for value in sorted(allowed, key=len, reverse=True):
        if value:
            text = re.sub(r"(?<![A-Za-z0-9.])" + re.escape(value) + r"(?![A-Za-z0-9.%％])", "", text)
    return any(char.isdecimal() for char in text) or bool(_CHINESE_QUANTITY.search(text))


def judgment_review(decision, evidence):
    if evidence.get("numeric_contract") != NUMERIC_CONTRACT:
        return []
    catalog = evidence.get("fact_catalog", {})
    refs = decision.get("fact_refs")
    if (not isinstance(refs, list) or len(refs) > MAX_FACT_REFS
            or any(not isinstance(ref, str) or ref not in catalog for ref in refs)
            or len(set(refs)) != len(refs)):
        return ["FACT_REFERENCES_INVALID"]
    if evidence.get("current") and any(key.startswith("sector_") for key in catalog) and not any(
            catalog[ref]["scope"] in ("SECTOR", "SECTOR_MEMBERS") for ref in refs):
        return ["SECTOR_FACT_REFERENCE_REQUIRED"]
    rationale = decision.get("research", {}).get("rationale") if isinstance(decision.get("research"), dict) else None
    if isinstance(rationale, str) and _has_unbound_number(rationale, evidence):
        return ["RATIONALE_NUMERIC_FACT_UNBOUND"]
    return []


def selection(decision, evidence):
    """Only validate the explicit selection contract here; execution eligibility is separate."""
    if evidence.get("etf_selection_contract") != ETF_SELECTION_CONTRACT:
        return None, []
    choices = decision.get("etf_choices")
    allowed = {row["etf"] for row in evidence.get("etf_options", {}).get("products", ())}
    if (not isinstance(choices, list) or len(choices) > 3
            or any(not isinstance(code, str) or code not in allowed for code in choices)
            or len(set(choices)) != len(choices)):
        return None, ["ETF_SELECTION_UNCONFIRMED"]
    return choices, []


def explanation(decision, evidence, checked, review=()):
    """Every new-contract sector gets a labelled, program-rendered factual explanation."""
    if evidence.get("numeric_contract") != NUMERIC_CONTRACT:
        return checked
    catalog = evidence.get("fact_catalog", {})
    refs = decision.get("fact_refs", []) if not review else []
    if not isinstance(refs, list) or not refs:
        refs = [key for key in ("sector_return", "sector_change", "sector_breadth") if key in catalog][:2]
    facts = [catalog[ref] for ref in refs if isinstance(ref, str) and ref in catalog]
    texts = [row["text"] for row in facts]
    result = dict(checked) if checked and not review else dict(role="WATCH", counter_evidence_status="NOT_CHECKED",
        counter_evidence_refs=[], invalidation_condition="等待下一轮可比证据", key_unknown="缺少深度解释")
    result["source"] = "AI_WITH_FACTS" if checked and not review else "PROGRAM_FACTS"
    result["qualitative_rationale"] = checked["rationale"] if checked and not review else ""
    result["fact_refs"] = [ref for ref in refs if isinstance(ref, str) and ref in catalog]
    result["fact_evidence"] = facts
    result["rationale"] = "；".join(([result["rationale"]] if checked and not review else []) + texts) or "本轮缺少可用数值事实"
    if review:
        result.update(review_reasons=list(review), key_unknown="判断依据待复核")
    return result
