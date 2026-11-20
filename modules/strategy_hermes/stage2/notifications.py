"""Prepare verified brief sections and deliver committed QQ messages via OCTOP."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import json
import re

from ..rotation_chain import LABELS
from ..simulator import decimal, moment, session
from .inputs import CLOSED_STATUS
from .outbox import DeliveryPermanent, DeliveryUncertain, save_qq_text
from .penetration import number
from .stock_research import for_sector

ACTIONS = {"BUY": "买入节点", "REDUCE": "首次转弱减仓", "EXIT": "退潮清仓"}
QQ_LIMIT = 1500
QQ_RESEARCH_SECTORS = 3
QQ_FORMAT = "octop-markdown-v1"
ETF_REASONS = {
    "SECTOR_MEMBERS_UNAVAILABLE": "缺板块成分", "WEIGHTS_UNVERIFIED": "权重待核验",
    "PRODUCT_RULE_UNVERIFIED": "交易规则待核验", "MAINBOARD_LOW_OR_UNKNOWN": "主板权重不足或待核验",
    "EXPOSURE_LOW": "板块暴露不足", "CORE_EXPOSURE_LOW": "核心股暴露不足",
    "LIQUIDITY_BELOW_MINIMUM": "流动性不足", "AUM_BELOW_MINIMUM": "规模不足", "TRADING_SUSPENDED": "停牌",
    "PRODUCT_RULE": "产品交易规则待核验", "TRADE_UNIT_UNVERIFIED": "交易单位待核验",
    "QUOTE_UNAVAILABLE": "有效报价待补", "QUOTE_INVALID": "报价无效",
    "ETF_SELECTION_UNCONFIRMED": "ETF选择待复核",
    "NO_OVERLAP_CONFIRMED": "正式板块无正权重交集",
    "WEAK_OVERLAP_REJECTED": "弱关联观察",
    "SINGLE_STOCK_CROSSOVER_REJECTED": "单股跨界观察",
    "WEAK_OVERLAP_UPPER_BOUND_REJECTED": "暴露上界低于底线",
    "EMPTY_AFTER_WEAK_PAIR_STRIP": "弱关联剔除后本轮未选",
    "RULE_UNVERIFIED": "交易规则待核验", "TRADE_UNIT_CHANGED": "交易单位与冻结规则冲突",
    "RULE_REVIEW_REQUIRED": "产品规则待复核，暂停买入",
    "ENTRY_ADMISSION_UNAVAILABLE": "准入资料暂不可用，暂停买入",
    "ENTRY_REFERENCE_UNPUBLISHED": "准入资料尚未发布，暂停买入",
    "SETTLEMENT_RULE_CONFLICT": "结算规则冲突，暂停买入",
    "PRODUCT_OUTSIDE_SCOPE": "产品交易范围不符", "PRODUCT_IDENTITY_INVALID": "产品身份待核验",
    "PRODUCT_NOT_LISTED": "产品非上市交易状态", "TRADING_UNAVAILABLE": "当前暂停交易",
    "USABLE_PRICE_UNAVAILABLE": "有效报价待补",
    "EOD_OBSERVATION_ONLY": "收盘快照仅供观察",
    "MARKET_DATE_MISMATCH": "行情日期待更新", "PRODUCT_IDENTITY_UNVERIFIED": "产品身份待核验",
}
COUNTER_STATUS = {"NOT_FOUND": "比较后未发现", "NOT_CHECKED": "待核实"}
BOARD_GAPS = {
    "SECTOR_MEMBERS_UNAVAILABLE": "缺成分名单",
    "STOCK_QUOTES_MISSING": "缺个股行情",
    "NO_COMPARABLE_PRIOR": "缺可比前序",
    "NO_CURRENT_SECTOR_EVIDENCE": "缺本轮板块行情",
    "OCTOP_DECISION_UNCONFIRMED": "模型判断待核验",
}
STOCK_CODE = re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit-1]+"…"


def _display_sector(sid, state, rows):
    row = rows[state["row"]]
    return _text(row.get("sector_name"), 28) or sid


NO_CARRIER_IN_SCOPE = "当前资料范围未找到"


def _short_list(values, missing, limit):
    if not values:
        return missing
    shown = "、".join(values[:limit])
    return shown if len(values) <= limit else f"{shown} 等 {len(values)} 项"


def _valid_stock_quote(quote, at):
    if not isinstance(quote, dict) or quote.get("tradeable") is False:
        return False
    try:
        seen = moment(quote["observed_at"])
        source = quote.get("source")
        return (seen.date() == at.date() and seen <= at and session(seen)
                and isinstance(source, str) and source.strip()
                and quote.get("trading_status") not in CLOSED_STATUS
                and decimal(quote.get("price_cny", quote.get("last"))) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def stock_watchlist(report, mapping, market, *, slot=None):
    """Only explicit stock codes with contemporaneous source and membership evidence."""
    at = moment(report["at"])
    if (not market or market.body["trade_date"] != at.date().isoformat()
            or market.body.get("mode") == "EOD"):
        return {}
    rows = json.loads(report["raw"]).get("sectors", [])
    research = market.body.get("stock_research")
    watch = {}
    for sid, state in report["sectors"].items():
        if state["state"] not in ("UP", "HOLD") or not state["sources"]:
            continue
        sector = mapping["sectors"].get(sid) or {}
        members = sector.get("members")
        codes = rows[state["row"]].get("core_stock_codes")
        if not isinstance(members, list) or not isinstance(codes, list):
            continue
        evidence = {row["code"]: row for row in for_sector(market, sid, slot, at)} if research else market.body["quotes"]
        verified = sorted({code for code in codes if isinstance(code, str)
                           and STOCK_CODE.fullmatch(code) and code in members
                           and _valid_stock_quote(evidence.get(code), at)})
        if verified:
            watch[sid] = verified
    return watch


def report_market_scope(market, mapping, report):
    if market is None:
        return None
    counts = dict(up=0, down=0, flat=0, unknown=0)
    ranked = []
    mapped = set()
    for row in market.body.get("sectors", ()):
        sid = row.get("sector_id")
        if sid in mapping["sectors"]:
            mapped.add(sid)
        change = number(row.get("return_ratio"))
        counts["unknown" if change is None else "up" if change > 0 else "down" if change < 0 else "flat"] += 1
        if change is not None:
            ranked.append((change, sid or "", row.get("sector_name")))
    ranked.sort(key=lambda item: (item[0], item[1]))

    def leaders(rows):
        return [{"name": name or sid, "return_ratio": str(change)} for change, sid, name in rows]

    quality = market.body.get("quality", {})
    return dict(board_count=sum(counts.values()), board_counts=counts, etf_count=len(market.body["quotes"]),
                mapped_count=len(mapped), analyzed_count=len(report["sectors"]),
                stock_quotes=quality.get("stock_quotes"), iopv=quality.get("iopv"),
                gainers=leaders([row for row in reversed(ranked) if row[0] > 0][:3]),
                decliners=leaders([row for row in ranked if row[0] < 0][:3]))


def _market_lines(scope):
    if not scope:
        return []
    counts = scope["board_counts"]
    lines = [
        f"{scope['board_count']}板块，涨{counts['up']}／跌{counts['down']}／平{counts['flat']}／未知{counts['unknown']}；"
        f"{scope['etf_count']}只ETF报价；已映射{scope['mapped_count']}／研判{scope['analyzed_count']}个板块",
    ]
    extremes = []
    for field, title in (("gainers", "领涨"), ("decliners", "领跌")):
        items = [f"{_text(row['name'], 14)} {number(row['return_ratio']):+.2%}" for row in scope.get(field, ())]
        if items:
            extremes.append(f"{title} {'、'.join(items[:2])}")
    if extremes:
        lines.append("；".join(extremes))
    return lines


def _recommendation_sections(report, ranked, names):
    groups = report["input_meta"]["etf_recommendations"]["sectors"]
    candidates, references, blocked = [], [], []
    reference_groups = ([], [], [])
    missing_weights = 0
    direction_first = report["input_meta"]["etf_recommendations"].get("method") in (
        "etf-direction-first-v2", "etf-direction-first-v3")
    labels = {"CONFIRMED": "AI已选·执行就绪" if direction_first else "动向已确认",
              "STATIC_ONLY": "仅静态匹配", "PENDING": "执行资料待核验" if direction_first else "待核验",
              "OBSERVE": "关联观察", "REJECTED": "执行受限" if direction_first else "静态未通过"}
    for sid in ranked:
        details = groups.get(sid, {})
        if (details.get("selection_audit") or {}).get("reason") == "EMPTY_AFTER_WEAK_PAIR_STRIP":
            reference_groups[1].append(f"{names[sid]}（{ETF_REASONS['EMPTY_AFTER_WEAK_PAIR_STRIP']}）")
        selected = details.get("representatives")
        current_codes = set(selected) if isinstance(selected, list) else set()
        static_codes = set(details.get("static_representatives") or [])
        products, unweighted = [], 0
        # Older saved reports may include the entire unweighted catalog in every
        # sector. Summarize those gaps without rewriting the archived report.
        for product in details.get("products", ()):
            if (not direction_first and not product.get("matched")
                    and "WEIGHTS_UNVERIFIED" in product.get("quality", [])):
                unweighted += 1
            else:
                products.append(product)
        missing_weights = max(missing_weights, unweighted,
            ((details.get("coverage_gaps") or {}).get("weights") or {}).get("products", 0))
        order = list(dict.fromkeys([*(selected if isinstance(selected, list) else []),
                                   *(details.get("static_representatives") or [])]))
        ranking = {code: index for index, code in enumerate(order)}
        products.sort(key=lambda product: (ranking.get(product["etf"], len(ranking)),
            -(number((product.get("metrics") or {}).get("E")) or 0),
            -len(product.get("matched", [])), product["etf"]))
        for product in products:
            state = product.get("recommendation_status", "PENDING")
            line = f"{names[sid]} {product['etf']}（{labels.get(state, '待核验')}）"
            pair = product.get("representativeness")
            if pair is not None and not pair["entry_allowed"]:
                label = ETF_REASONS.get(pair["representativeness_status"], pair["representativeness_status"])
                reference_groups[2].append(f"{names[sid]} {product['etf']}（{label}）")
                continue
            if direction_first and state in ("PENDING", "REJECTED") and product.get("ai_selected"):
                reasons = [ETF_REASONS.get(reason, reason) for reason in product.get("execution_reasons", ())]
                blocked.append(f"{names[sid]} {product['etf']}（{'、'.join(reasons) or '执行条件待核验'}）")
            elif not direction_first and product.get("execution_status") == "FAILED":
                blocked.append(f"{names[sid]} {product['etf']}（交易条件未通过）")
            elif (state == "CONFIRMED" and product.get("representative_current")
                  and (not isinstance(selected, list) or product["etf"] in selected)):
                if not direction_first and product.get("execution_status") == "PARTIAL":
                    line = line[:-1] + "，交易条件部分未核验）"
                candidates.append(line)
            elif state == "REJECTED" and not direction_first:
                blocked.append(line)
            else:
                priority = 0 if product['etf'] in current_codes else 1 if product['etf'] in static_codes else 2
                reference_groups[priority].append(line)
    references = [line for group in reference_groups for line in group]
    if report["input_meta"]["etf_recommendations"].get("method") == "etf-direction-first-v3":
        support = (report["input_meta"].get("representation") or {}).get("current_support", ())
        grouped = [row for row in support if row.get("up_branch_count", 0) >= 2]
        for row in grouped[:2]:
            conflicts = "，有方向冲突" if row.get("direction_conflicts") else ""
            references.insert(0, f"{row['etf']}（{row['up_branch_count']}个同源分支含有效加强{conflicts}）")
    return candidates, references, blocked


def _etf_sections(report, ranked, names):
    if report["input_meta"].get("etf_recommendations"):
        return _recommendation_sections(report, ranked, names)
    penetration = report["input_meta"].get("penetration", {}).get("sectors", {})
    active = defaultdict(list)
    for code, choices in report["candidates"].items():
        for choice in choices:
            sid = choice["sector_id"]
            if (report["sectors"][sid]["state"] == "UP"
                    and code in penetration.get(sid, {}).get("representatives", ())):
                active[sid].append((choice["rank"], code))
    candidates, references, blocked = [], [], []
    missing_weights = 0
    for sid in ranked:
        details = penetration.get(sid, {})
        missing_weights = max(missing_weights,
            ((details.get("coverage_gaps") or {}).get("weights") or {}).get("products", 0))
        chosen = [code for _, code in sorted(set(active[sid]))]
        if chosen:
            candidates.append(f"{names[sid]} {_short_list(chosen, '', 3)}")
        other = [code for code in details.get("representatives", ()) if code not in chosen]
        if other:
            state = report["sectors"][sid]
            label = state.get("label") or LABELS.get(state["phase"], "待核实")
            references.append(f"{names[sid]} {_short_list(other, '', 3)}（{label}）")
        unweighted = 0
        for product in details.get("products", ()):
            if not product.get("matched") and "WEIGHTS_UNVERIFIED" in product.get("rejection_reasons", []):
                unweighted += 1
                continue
            reasons = [ETF_REASONS[reason] for reason in product.get("rejection_reasons", ()) if reason in ETF_REASONS]
            if reasons:
                blocked.append(f"{names[sid]} {product['etf']}（{'、'.join(reasons)}）")
        missing_weights = max(missing_weights, unweighted)
    return candidates, references, blocked


def _etf_gaps(report, ranked, names):
    """Coverage is not a rejected candidate, including in archived reports."""
    meta = report["input_meta"]
    groups = (meta.get("etf_recommendations") or meta.get("penetration") or {}).get("sectors", {})
    missing_weights, members = 0, []
    for sid in ranked:
        details = groups.get(sid, {})
        legacy = sum(not product.get("matched") and "WEIGHTS_UNVERIFIED" in
                     product.get("quality", []) + product.get("rejection_reasons", [])
                     for product in details.get("products", ()))
        missing_weights = max(missing_weights, legacy,
            ((details.get("coverage_gaps") or {}).get("weights") or {}).get("products", 0))
        if details.get("reason") == "SECTOR_MEMBERS_UNAVAILABLE":
            members.append(names[sid])
    result = []
    if missing_weights:
        result.append(f"目录{missing_weights}只ETF缺成分权重，穿透待核验")
    if members:
        result.append(f"{len(members)}个板块成员资料待补：{'、'.join(members[:3])}" + ("等" if len(members) > 3 else ""))
    return result


def _blocked_text(values, *, limit=2):
    if not values:
        return ""
    codes = {code for value in values for code in re.findall(r"[0-9]{6}\.(?:SH|SZ)", value)}
    shown = " ｜ ".join(values[:limit])
    if len(values) > limit:
        shown += f" ｜ 共{len(codes)}只ETF/{len(values)}条板块关联"
    return shown


def _detail_order(report, ranked, orders):
    """Execution and held directions precede optional research and breadth-only cards."""
    meta = report.get("input_meta", {}).get("octop", {})
    selected_codes = {row[0] for row in orders}
    action_sectors = set()
    for sid, group in report.get("input_meta", {}).get("etf_recommendations", {}).get("sectors", {}).items():
        if any(product.get("etf") in selected_codes for product in group.get("products", [])):
            action_sectors.add(sid)
    return sorted(ranked, key=lambda sid: (
        sid not in action_sectors, sid not in meta.get("tracked_sectors", []),
        meta.get("research_explanations", {}).get(sid, {}).get("source") == "PROGRAM_FACTS",
        sid not in meta.get("research_explanations", {}), ranked.index(sid)))


def _pending_label(action, eligible, at, last_block=None):
    if action != "BUY" and eligible is None:
        return "待交易日历"
    if action != "BUY" and moment(eligible) > at:
        return "待T+1"
    return {"INSUFFICIENT_CASH_FOR_ONE_LOT": "资金不足一手",
            "INSUFFICIENT_CASH_FOR_SELL_FEE": "卖出费用资金不足"}.get(
                last_block, ETF_REASONS.get(last_block, "等待节点后新报价"))


def _research_lines(octop, ranked, names):
    explanations = octop.get("research_explanations", {})
    for sid in ranked:
        item = explanations.get(sid)
        if not item:
            continue
        yield f"{names[sid]}依据 {_rationale_summary(item, 180)}"
        counter = COUNTER_STATUS.get(item.get("counter_evidence_status"))
        if item.get("counter_evidence_status") == "FOUND":
            evidence = octop.get("research_explanation_evidence", {}).get(sid, {})
            facts = []
            for ref in item.get("counter_evidence_refs", ()):
                row = evidence.get(ref, {})
                value = number(row.get("board_return_ratio", row.get("return_ratio")))
                if value is not None and row.get("observed_at"):
                    name = _text(row.get("code"), 12) or "板块"
                    facts.append(f"{moment(row['observed_at']):%H:%M} {name} {value:+.2%}")
            counter = "、".join(facts) or "已发现，原始引用见报告详情"
        if counter:
            yield f"{names[sid]}反证 {_text(counter, 180)}"
        for key, label in (("invalidation_condition", "失效条件"), ("key_unknown", "待核实")):
            if item.get(key):
                yield f"{names[sid]}{label} {_text(item[key], 120)}"


def _breadth_summary(raw, ranked, names):
    """Keep complete member denominators and explicit partial coverage in the brief."""
    facts = raw.get("source_facts", {})
    lines = []
    for sid in ranked[:QQ_RESEARCH_SECTORS]:
        value = facts.get(sid, {}).get("breadth")
        if not isinstance(value, dict):
            continue
        coverage = number(value.get("coverage"))
        ratio = number(value.get("up_ratio"))
        median = number(value.get("median_return"))
        if value.get("status") not in ("COMPLETE", "PARTIAL") or coverage is None or ratio is None:
            lines.append(f"{names[sid]}待核验")
            continue
        detail = (f"{names[sid]} 涨{value.get('up')}/{value.get('observed_count')}只"
                  f"（覆盖{coverage:.0%}，上涨占比{ratio:.0%}")
        if median is not None:
            detail += f"，中位{median:+.2%}"
        detail += "）"
        lines.append(detail)
    return ["普涨观察 " + _text("；".join(lines), 275)] if lines else []


def _manual_recovery_header(slot, meta):
    recovery = meta.get("manual_recovery")
    if not isinstance(recovery, dict) or not recovery:
        return None
    source = moment(recovery["source_cutoff"])
    executed = moment(recovery["executed_at"])
    return (f"【Hermes 研判与模拟｜{slot}轮·手动恢复】",
            f"事实截止 {source:%m-%d %H:%M:%S} · 恢复发布 {executed:%m-%d %H:%M:%S}")


def brief_sections(slot, report, orders, *, preview=False):
    raw = json.loads(report["raw"])
    rows = raw.get("sectors", [])
    states = report["sectors"]
    meta = report["input_meta"]
    octop = meta.get("octop", {})
    names = {sid: _display_sector(sid, state, rows) for sid, state in states.items()}
    ranks = {"UP": 0, "HOLD": 1, "WEAK": 2, "EXIT": 3, "UNKNOWN": 4}
    ranked = sorted(states, key=lambda sid: (ranks.get(states[sid]["state"], 5), sid))
    statuses = {"READY": "已就绪", "PARTIAL": "部分证据", "UNAVAILABLE": "数据待补"}
    at = moment(report["at"])
    observed = meta.get("source_observed_at") or report.get("data_as_of")
    recovery = _manual_recovery_header(slot, meta)
    if recovery:
        title, clock = recovery
    elif preview and observed:
        snapshot = moment(observed)
        title = f"【盘后试发｜Hermes {snapshot:%m-%d} {slot}轮回放】"
        clock = f"快照 {snapshot:%H:%M:%S} · 试发 {at:%H:%M:%S}"
    else:
        title = f"【Hermes 研判与模拟｜{slot}轮】"
        clock = f"发布 {at:%m-%d %H:%M:%S}"
        if observed:
            clock += f" · 行情 {moment(observed):%H:%M:%S}"
    phases = []
    for sid in ranked:
        state = states[sid]
        label = state.get("label") or LABELS.get(state["phase"], "待核实")
        market_row = octop.get("market_evidence", {}).get(sid, {})
        change = number(market_row.get("board_return_ratio"))
        phase = f"{names[sid]} {_text(label, 12)}"
        if change is not None:
            phase += f" {change:+.2%}"
        if state["state"] == "UNKNOWN":
            gaps = [BOARD_GAPS[code] for code in rows[state["row"]].get("risks", ()) if code in BOARD_GAPS]
            if not gaps and any(w.startswith(f"{sid}:OCTOP_DECISION_UNCONFIRMED") for w in raw.get("warnings", ())):
                gaps = [BOARD_GAPS["OCTOP_DECISION_UNCONFIRMED"]]
            if gaps:
                phase += f"（{'、'.join(gaps)}）"
        phases.append(phase)

    candidates, references, blocked = _etf_sections(report, ranked, names)
    stocks = meta.get("stock_watchlist", {})
    stock_lines = [f"{names[sid]} {_short_list(stocks[sid], '', 4)}"
                   for sid in ranked if sid in stocks and states[sid]["state"] in ("UP", "HOLD")]
    scope = meta.get("market_scope")
    if scope and scope.get("stock_quotes") in ("NOT_COLLECTED", "UNAVAILABLE"):
        stock_empty = "缺本轮已核验个股报价"
    elif octop.get("stock_research"):
        stock_empty = "本轮有个股报价，暂无通过核验的观察标的"
    else:
        stock_empty = "无已核验个股"

    node_lines = []
    for order in orders:
        code, action, quantity, status, eligible, is_new = order[:6]
        text = f"{code} {ACTIONS[action]}"
        if quantity is not None:
            text += f" {quantity} 份"
        if status == "BELOW_TRADING_UNIT":
            text += "，低于交易单位"
        elif status == "PENDING":
            text += "，" + _pending_label(action, eligible, at, order[6] if len(order) > 6 else None).replace("待T+1", "待 T+1")
        if not is_new:
            text += "（前轮）"
        node_lines.append(text)

    market_lines = _market_lines(scope)
    market, leaders = (market_lines + ["", ""])[:2]
    sections = dict(
        title=title, clock=f"{clock}（北京时间）· {statuses[report['status']]}",
        market=_text(market, 190), leaders=_text(leaders, 160),
        boards=_text(_short_list(phases, "暂无板块判断", 4), 145),
        etf_candidates=_text(_short_list(candidates, "本轮无候选", 3), 135),
        etf_watch=_text(_short_list(references, "", 3), 125),
        etf_blocked=_text(_blocked_text(blocked), 180),
        etf_gaps=_text(" ｜ ".join(_etf_gaps(report, ranked, names)), 180),
        stocks=_text(_short_list(stock_lines, stock_empty, 3), 135),
        simulation=_text(_short_list(node_lines, "本次试发无节点" if preview else "无待执行节点", 5), 160),
        report_id=_text(report["id"], 128),
    )
    if scope and scope.get("iopv") in ("NOT_COLLECTED", "UNAVAILABLE", "CONFIG_UNAVAILABLE", "EMPTY_SCOPE"):
        sections["iopv"] = "本轮缺数据"
    active = [phase for sid, phase in zip(ranked, phases, strict=True) if states[sid]["state"] in ("UP", "HOLD", "WEAK")]
    if active and meta.get("rotation_window") and not preview:
        sections["active_directions"] = _text(_short_list(active, "", 3), 90)
    research_ranked = [sid for sid in _detail_order(report, ranked, orders) if sid in octop.get("research_explanations", {})]
    details = _breadth_summary(raw, ranked, names)
    details.extend(_research_lines(octop, research_ranked[:QQ_RESEARCH_SECTORS], names))
    if not research_ranked and not octop.get("receipt") and raw.get("summary"):
        details.append("摘要 " + _text(raw["summary"], 165))
    if len(research_ranked) > QQ_RESEARCH_SECTORS or len(ranked) > QQ_RESEARCH_SECTORS:
        details.append("其余内容见完整回放" if preview else "其余内容见工作台报告详情")
    sections["details"] = details
    if preview:
        sections["preview"] = True
    return sections


VISUAL_PHASES = {
    "NEW": "🆕", "MAINTAIN": "🟢", "STRENGTHEN": "🟢", "STRENGTHEN_X2": "🟢",
    "WEAKEN": "🟠", "RETREAT": "🔴", "UNKNOWN": "⚪", "NOT_COVERED": "⚪", "EVENT_ONLY": "⚡",
}


def _visual_bar(ratio):
    """A display-only ten-cell scale; unknown data never becomes an empty scale."""
    # Ratios derived from integer counts may exceed persisted decimal precision.
    value = ratio if isinstance(ratio, Decimal) and ratio.is_finite() else number(ratio)
    if value is None or not Decimal(0) <= value <= Decimal(1):
        return "待核实"
    filled = int((value * 10).to_integral_value(rounding=ROUND_HALF_UP))
    return "▰" * filled + "▱" * (10-filled)


def _visual_phrase(value, limit=180):
    """Keep a whole evidence clause; do not remove negation or cut a condition."""
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value if len(value) <= limit else "内容较长，详见工作台报告"


def _rationale_summary(explanation, limit):
    """Select whole scoped facts, preserving their subjects and denominators."""
    if "qualitative_rationale" not in explanation:
        return _visual_phrase(explanation.get("rationale"), limit)
    parts = [explanation.get("qualitative_rationale", "")]
    parts.extend(item.get("text", "") for item in explanation.get("fact_evidence", ()))
    parts = [" ".join(part.split()) for part in parts if isinstance(part, str) and part.strip()]
    suffix = "其余依据见工作台报告"
    shown = []
    omitted = False
    for index, part in enumerate(parts):
        reserve = [suffix] if omitted or index < len(parts)-1 else []
        if len("；".join([*shown, part, *reserve])) <= limit:
            shown.append(part)
        else:
            omitted = True
    if omitted:
        shown.append(suffix)
    return "；".join(shown)


def _visual_entries(values, missing, count, limit, separator="\n"):
    """Select complete entries, with an explicit remainder instead of mid-entry cuts."""
    if not values:
        return missing
    shown = []
    for value in values[:count]:
        suffix = f"等{len(values)}项" if len(shown)+1 < len(values) else ""
        candidate = separator.join([*shown, value, *([suffix] if suffix else [])])
        if len(candidate) > limit:
            break
        shown.append(value)
    if not shown:
        return f"共{len(values)}项，详见工作台报告"
    if len(shown) < len(values):
        shown.append(f"等{len(values)}项")
    return separator.join(shown)


def _visual_market(scope):
    if not isinstance(scope, dict):
        return "市场覆盖待核实", ""
    counts = scope.get("board_counts") or {}
    total, up = scope.get("board_count"), counts.get("up")

    def count(value):
        return str(value) if type(value) is int and value >= 0 else "未知"

    complete = (type(total) is int and total > 0
                and all(type(counts.get(key)) is int and counts[key] >= 0 for key in ("up", "down", "flat", "unknown"))
                and sum(counts[key] for key in ("up", "down", "flat", "unknown")) == total)
    ratio = Decimal(up)/total if complete else None
    market = (f"涨{count(up)}/{count(total)} {_visual_bar(ratio)} 跌{count(counts.get('down'))} "
              f"(平{count(counts.get('flat'))}·未知{count(counts.get('unknown'))})\n"
              f"📦 {count(total)}板块 ｜ {count(scope.get('etf_count'))}只ETF ｜ "
              f"映射{count(scope.get('mapped_count'))}/研判{count(scope.get('analyzed_count'))}")
    leaders = []
    for field, title in (("gainers", "🚀 领涨"), ("decliners", "💧 领跌")):
        items = []
        for row in scope.get(field, ())[:2]:
            change = number(row.get("return_ratio"))
            if change is not None:
                items.append(f"{_text(row.get('name'), 14)} {change:+.2%}")
        if items:
            leaders.append(f"{title}：" + " ｜ ".join(items))
    return market, "\n".join(leaders)


def _visual_stage(state):
    phase = state.get("phase", "UNKNOWN")
    label = state.get("label") or LABELS.get(phase) or {"NOT_COVERED": "未覆盖", "EVENT_ONLY": "事件先行"}.get(phase, "待确认")
    return VISUAL_PHASES.get(phase, "⚪"), _text(label, 16)


def _visual_etfs(values):
    replacements = (("动向已确认", "动向确认"), ("仅静态匹配", "静态匹配"),
                    ("，交易条件部分未核验", "·条件待核"), ("缺板块穿透资料", "缺穿透资料"))
    result = []
    for value in values:
        for old, new in replacements:
            value = value.replace(old, new)
        result.append(value)
    return result


def _visual_counter(octop, sid, explanation):
    status = explanation.get("counter_evidence_status")
    if status == "NOT_FOUND":
        return "暂无（已核实）"
    if status != "FOUND":
        return "待核实"
    evidence = octop.get("research_explanation_evidence", {}).get(sid, {})
    facts = []
    for ref in explanation.get("counter_evidence_refs", ()):
        row = evidence.get(ref, {})
        value = number(row.get("board_return_ratio", row.get("return_ratio")))
        if value is not None and row.get("observed_at"):
            name = _text(row.get("code"), 12) or "板块"
            facts.append(f"{moment(row['observed_at']):%H:%M} {name} {value:+.2%}")
    return _visual_phrase(" ｜ ".join(facts), 180) if facts else "已发现，详见原始引用"


def _visual_card(sid, name, state, raw, octop):
    icon, label = _visual_stage(state)
    evidence = octop.get("market_evidence", {}).get(sid, {})
    change = number(evidence.get("board_return_ratio"))
    change_text = f" {change:+.2%}" if change is not None else " 涨跌待核"
    breadth = raw.get("source_facts", {}).get(sid, {}).get("breadth") or {}
    coverage, ratio = number(breadth.get("coverage")), number(breadth.get("up_ratio"))
    observed, members, up = (breadth.get(key) for key in ("observed_count", "member_count", "up"))
    valid = (breadth.get("status") in ("COMPLETE", "PARTIAL")
             and type(observed) is int and observed > 0 and type(members) is int and members >= observed
             and type(up) is int and 0 <= up <= observed
             and coverage is not None and Decimal(0) <= coverage <= Decimal(1)
             and ratio is not None and Decimal(0) <= ratio <= Decimal(1))
    if valid:
        width = (f"涨{up}/{observed} ({_visual_bar(ratio)} {ratio:.0%})"
                 f"·覆盖{observed}/{members}({coverage:.0%})")
        median = number(breadth.get("median_return"))
        if median is not None:
            width += f"·中位{median:+.2%}"
        oldest, newest = breadth.get("oldest_at"), breadth.get("newest_at")
        if oldest and newest:
            width += f"·源{moment(oldest):%H:%M:%S}–{moment(newest):%H:%M:%S}"
    else:
        width = "待核验"
    explanation = octop.get("research_explanations", {}).get(sid, {})
    rationale = _rationale_summary(explanation, 240) or "本轮未选入详细解释"
    basis_label = "事实摘要" if explanation.get("source") == "PROGRAM_FACTS" else "依据"
    condition = _visual_phrase(explanation.get("invalidation_condition"), 120) or "待核实"
    unknown = _visual_phrase(explanation.get("key_unknown"), 120) or "待核实"
    return (f"【{name}{change_text} | {icon} {label}】宽度 {width}\n"
            f"✅ {basis_label}：{rationale}\n⚡ 反证：{_visual_counter(octop, sid, explanation)}\n"
            f"🛡️ 风控：{condition} ｜ ⏳ 待核：{unknown}")


def visual_brief_sections(slot, report, orders, *, preview=False):
    """QQ-only presentation of committed facts; v1/API sections stay unchanged."""
    raw = json.loads(report["raw"])
    rows, states, meta = raw.get("sectors", []), report["sectors"], report["input_meta"]
    octop = meta.get("octop", {})
    names = {sid: _display_sector(sid, state, rows) for sid, state in states.items()}
    ranks = {"UP": 0, "HOLD": 1, "WEAK": 2, "EXIT": 3, "UNKNOWN": 4}
    ranked = sorted(states, key=lambda sid: (ranks.get(states[sid]["state"], 5), sid))
    at = moment(report["at"])
    observed = meta.get("source_observed_at") or report.get("data_as_of")
    recovery = _manual_recovery_header(slot, meta)
    if recovery:
        title, clock = recovery
    elif preview and observed:
        snapshot = moment(observed)
        title = f"【盘后试发｜Hermes {snapshot:%m-%d} {slot}轮回放】"
        clock = f"快照 {snapshot:%H:%M:%S} · 试发 {at:%H:%M:%S}"
    else:
        title = f"【Hermes 研判与模拟｜{slot}轮】"
        clock = f"{at:%m-%d %H:%M:%S}"
        clock += f" · 行情 {moment(observed):%H:%M:%S}" if observed else " · 行情时间待核实"
    clock += "（北京时间）· " + {"READY": "✅已就绪", "PARTIAL": "⚠️部分证据", "UNAVAILABLE": "⏳数据待补"}[report["status"]]
    phases = []
    for sid in ranked:
        state = states[sid]
        icon, label = _visual_stage(state)
        change = number(octop.get("market_evidence", {}).get(sid, {}).get("board_return_ratio"))
        phase = f"{icon} {names[sid]} ({label})"
        phase += f" {change:+.2%}" if change is not None else " 涨跌待核"
        if state["state"] == "UNKNOWN":
            gaps = [BOARD_GAPS[key] for key in rows[state["row"]].get("risks", ()) if key in BOARD_GAPS]
            if not gaps and any(w.startswith(f"{sid}:OCTOP_DECISION_UNCONFIRMED") for w in raw.get("warnings", ())):
                gaps = [BOARD_GAPS["OCTOP_DECISION_UNCONFIRMED"]]
            if gaps:
                phase += " [" + "、".join(gaps) + "]"
        phases.append(phase)
    candidates, references, blocked = map(_visual_etfs, _etf_sections(report, ranked, names))
    candidate_text = _visual_entries(candidates, "本轮无", 3, 160, " ｜ ")
    statistics = (meta.get("representation") or {}).get("statistics")
    if statistics:
        sectors, pairs, indexes, products, nodes_count = (
            statistics[key] for key in ("sectors", "pairs", "indexes", "products", "orders"))
        tiers = pairs["selectable_by_tier"]
        candidate_text += (
            f"\n穿透统计　板块 {sectors['with_selectable']}/{sectors['total']} · "
            f"配对 {pairs['selectable']}/{pairs['total']} · 指数 {indexes['weights_available']}/{indexes['total']} · "
            f"ETF {products['selectable']}/{products['total']} · 本轮节点 {nodes_count['total']}\n"
            f"可选配对　主干 {tiers['core']} · 低暴露 {tiers['below_core']} · 未知 {tiers['unknown']}")
        # Saved pair groups are the one scope: an empty group means this mapping holds no carrier.
        saved_pairs = meta["representation"].get("pairs") or {}
        without_carrier = [names[sid] for sid in ranked if sid in saved_pairs and not saved_pairs[sid]]
        if without_carrier:
            candidate_text += f"\n{NO_CARRIER_IN_SCOPE}　{_short_list(without_carrier, '', 6)}"
    stock_groups = [f"{names[sid]} {_short_list(meta['stock_watchlist'][sid], '', 4)}"
                    for sid in ranked if sid in meta.get("stock_watchlist", {}) and states[sid]["state"] in ("UP", "HOLD")]
    scope = meta.get("market_scope")
    if scope and scope.get("stock_quotes") in ("NOT_COLLECTED", "UNAVAILABLE"):
        stock_empty = "缺已核验报价"
    elif octop.get("stock_research"):
        stock_empty = "暂无通过核验标的"
    else:
        stock_empty = "无已核验个股"
    nodes = []
    for order in orders:
        code, action, quantity, status, eligible, is_new = order[:6]
        line = f"{code} " + {"BUY": "买入", "REDUCE": "转弱减", "EXIT": "退潮清"}[action]
        if quantity is not None:
            line += f"{quantity}份"
        if status == "BELOW_TRADING_UNIT":
            line += " [低于最小单位]"
        elif status == "PENDING":
            pending = _pending_label(action, eligible, at, order[6] if len(order) > 6 else None)
            line += f" [{pending}]"
        if not is_new:
            line += "（前轮）"
        nodes.append(line)
    market, leaders = _visual_market(scope)
    iopv_status = scope.get("iopv") if scope else None
    iopv = "有数据" if iopv_status == "AVAILABLE" else "未启用" if iopv_status == "DISABLED" else "缺数据" if iopv_status in ("NOT_COLLECTED", "UNAVAILABLE", "CONFIG_UNAVAILABLE", "EMPTY_SCOPE") else "待核实"
    active = [line for sid, line in zip(ranked, phases, strict=True) if states[sid]["state"] in ("UP", "HOLD", "WEAK")]
    details = []
    detail_sectors = [sid for sid in _detail_order(report, ranked, orders) if sid in octop.get("research_explanations", {})
                      or isinstance(raw.get("source_facts", {}).get(sid, {}).get("breadth"), dict)]
    for sid in detail_sectors[:QQ_RESEARCH_SECTORS]:
        details.append(_visual_card(sid, names[sid], states[sid], raw, octop))
    if not details and not octop.get("receipt") and raw.get("summary"):
        details.append("摘要：" + (_visual_phrase(raw["summary"], 180) or "待核实"))
    if len(detail_sectors) > QQ_RESEARCH_SECTORS or len(ranked) > QQ_RESEARCH_SECTORS:
        details.append("⋯ 其余详见完整回放" if preview else "⋯ 其余详见工作台报告")
    return dict(title=title, clock=clock, market=market, leaders=leaders,
                boards=_visual_entries(phases, "暂无板块判断", 4, 220),
                etf_candidates=candidate_text,
                etf_watch=_visual_entries(references, "", 3, 160, " ｜ "),
                etf_blocked=_blocked_text(blocked),
                etf_gaps=" ｜ ".join(_etf_gaps(report, ranked, names)),
                stocks=_visual_entries(stock_groups, stock_empty, 3, 160, " ｜ "),
                simulation=_visual_entries(nodes, "本次试发无节点" if preview else "无待执行", 5, 200, " ｜ "),
                iopv=iopv, active_directions=_visual_entries(active, "", 3, 150, " ｜ ") if meta.get("rotation_window") and not preview else "",
                details=details, report_id=_text(report["id"], 128))


class QQFormatUnavailable(RuntimeError):
    """The format request can safely retry before any QQ send."""


class QQFormatRejected(DeliveryPermanent):
    """The renderer returned an invalid or unsupported result."""


def format_qq(client, sections):
    try:
        result = client.call("format", {"version": QQ_FORMAT, **sections}, timeout=8)
    except (OSError, ValueError, TypeError) as exc:
        raise QQFormatUnavailable("OCTOP brief rendering unavailable") from exc
    if not isinstance(result, dict) or result.get("status") == "UNAVAILABLE":
        raise QQFormatUnavailable("OCTOP brief rendering unavailable")
    text, template = result.get("text"), result.get("template")
    if (result.get("status") != "READY" or not isinstance(text, str) or not 1 <= len(text) <= QQ_LIMIT
            or not isinstance(template, str) or not re.fullmatch(r"octop-markdown-v1:(?:workspace|deployment):[a-f0-9]{64}", template)):
        raise QQFormatRejected("OCTOP brief rendering rejected")
    return text, template, result.get("workspace_template_invalid") is True


def deliver_qq(ledger, client, event):
    from ..qq_templates import MESSAGE_FORMAT, render_message
    text = event.payload.get("text")
    if text is None:
        if not isinstance(event.payload.get("sections"), dict):
            raise QQFormatRejected("invalid queued QQ format")
        if event.payload.get('format') == MESSAGE_FORMAT:
            try:
                formatted=render_message(event.payload.get('template_kind','research'),event.payload['sections'],
                                         preview=event.payload.get('preview',False))
            except (OSError,ValueError,TypeError) as exc:
                raise QQFormatRejected('invalid managed QQ template or fields') from exc
            text,template,fallback=formatted['text'],formatted['template'],formatted['template_fallback']
        elif event.payload.get('format') == QQ_FORMAT:
            text, template, fallback = format_qq(client, event.payload["sections"])
        else:
            raise QQFormatRejected("invalid queued QQ format")
        if not save_qq_text(ledger, event, text, template, fallback=fallback):
            raise QQFormatUnavailable("QQ delivery lease expired before rendering was saved")
    try:
        reply = client.call("qq", {"event_key": event.event_key, "text": text}, timeout=18)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DeliveryUncertain("QQ receipt unavailable") from exc
    status = reply.get("status")
    if status == "ACCEPTED":
        return reply
    if status == "RATE_LIMITED" or (status == "RETRYABLE" and reply.get("message_posts") == 0):
        error = TimeoutError("QQ temporarily unavailable")
        error.receipt = _failure_receipt(reply)
        raise error
    if status in ("REJECTED", "DISABLED"):
        raise DeliveryPermanent("QQ rejected the event", receipt=_failure_receipt(reply))
    raise DeliveryUncertain("QQ send outcome uncertain", receipt=_failure_receipt(reply))


def _failure_receipt(reply):
    """Keep provider failure evidence without credentials, recipients or content."""
    result = {}
    if reply.get("status") in ("REJECTED", "DISABLED", "UNCERTAIN", "RETRYABLE", "RATE_LIMITED"):
        result["status"] = reply["status"]
    for key in ("message_posts", "http_status", "provider_code"):
        value = reply.get(key)
        if type(value) is int and 0 <= value <= 10**12:
            result[key] = value
    return result
