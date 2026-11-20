"""Replay hand-selected ETF nodes with the shared Decimal and FIFO execution engine."""
from collections import defaultdict
from decimal import Decimal, localcontext
from itertools import chain, groupby
import re

from .ledger import event_pair, fingerprint
from .manual_actions import action_schedule
from .simulator import (
    CODE, PRECISION, Calendar, Rules, affordable, buy, decimal, eligible_lots,
    floor_lot, integer, moment, money, new_book, sell, shares, valuation,
)

SCHEMA = "stock-v4-manual-v1"
EXECUTION_VERSION = "manual_portfolio_v3"
ACTION_EXECUTION_VERSION = "manual_portfolio_v4"
ACTION_MODES = {
    "BUY": {"AMOUNT", "SHARES", "CASH_RATIO"},
    "ADD": {"AMOUNT", "SHARES", "CASH_RATIO"},
    "REDUCE": {"SHARES", "POSITION_RATIO"},
    "SELL": {"SHARES", "ALL"},
}
BUY_ACTIONS = frozenset(("BUY", "ADD"))
NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")


def execution_version(bundle):
    return ACTION_EXECUTION_VERSION if bundle.get("corporate_actions") else EXECUTION_VERSION


def _nodes(rows, instruments, calendar, cutoff):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise ValueError("scenario requires 1 to 2000 nodes")
    result, ids = [], set()
    for index, raw in enumerate(rows):
        identifier, code = raw["id"], raw["etf"]
        if (not isinstance(identifier, str) or not NODE_ID.fullmatch(identifier)
                or identifier in ids or code not in instruments):
            raise ValueError("invalid node identifier, duplicate, or unknown ETF")
        ids.add(identifier)
        at = moment(raw["at"])
        if at.date() not in calendar.days or at > cutoff:
            raise ValueError(f"node {identifier} is outside the replay window")
        action, mode = raw["action"], raw["size_mode"]
        if action not in ACTION_MODES or mode not in ACTION_MODES[action]:
            raise ValueError(f"node {identifier} has an invalid action or size mode")
        value = raw.get("size")
        if mode == "SHARES":
            value = integer(value)
            if value % instruments[code]:
                raise ValueError(f"node {identifier} quantity is outside its trading unit")
        elif mode == "ALL":
            if value is not None:
                raise ValueError(f"node {identifier} ALL cannot include a quantity")
        else:
            value = decimal(value)
            if value <= 0 or (mode.endswith("RATIO") and value > 1):
                raise ValueError(f"node {identifier} size is outside its range")
            value = str(value)
        result.append((at, 1, index, {
            "id": identifier, "etf": code, "at": at.isoformat(), "action": action,
            "size_mode": mode, "size": value, "status": "PENDING", "fills": [],
        }))
    return result


def _quotes(rows, instruments, calendar, cutoff, price_mode):
    if not isinstance(rows, list) or len(rows) > 100000:
        raise ValueError("scenario supports up to 100000 quote records")
    result, seen = [], {}
    for raw in rows:
        _, kind, quote, digest = event_pair(raw)
        if kind != "quote" or quote["etf"] not in instruments:
            raise ValueError("scenario quote has an unknown instrument")
        basis = quote.get("price_kind", "HISTORICAL")
        if basis not in ("HISTORICAL", "ASSUMED") or (basis == "ASSUMED" and price_mode != "ASSUMPTION"):
            raise ValueError("assumed quote requires an assumption scenario")
        identifier = quote["id"]
        if identifier in seen:
            if seen[identifier] != digest:
                raise ValueError("scenario quote identifier collides")
            continue
        seen[identifier] = digest
        observed, received = moment(quote["observed_at"]), moment(quote["received_at"])
        if not quote["tradeable"] or not calendar.legal(observed) or received > cutoff:
            continue
        quote["price_kind"] = basis
        result.append((received, 0, (observed, -quote["revision"], identifier), quote))
    return result


def _eligible(book, at):
    return sum(lot["remaining"] for lot in eligible_lots(book, at.date()))


def _reference(quote):
    if quote is None:
        return None
    return {key: quote[key] for key in ("id", "observed_at", "received_at", "price_cny",
                                        "source", "price_kind", "revision")}


class Portfolio:
    def __init__(self, rules, instruments, calendar):
        self.rules, self.instruments, self.calendar = rules, instruments, calendar
        self.cash = decimal(new_book(rules)["cash"])
        self.books = {code: new_book(rules) for code in instruments}
        self.latest = {}
        self.pending = defaultdict(list)
        self.fills = []
        self.corporate_actions = {}
        self.action_boundaries = {}

    def corporate_action(self, at, step, event):
        code, identifier = event["etf"], event["id"]
        book = self.books[code]
        if step != "PAY":
            self.action_boundaries[code] = at
        if step == "RECORD":
            count = shares(book)
            self.corporate_actions[identifier] = dict(
                event, status="RECORDED", eligible_shares=count,
                cash_cny=str(money(count * decimal(event["cash_per_share_cny"]))),
            )
        elif step == "EX":
            self.latest.pop(code, None)
            self.corporate_actions[identifier]["status"] = "RECEIVABLE"
        elif step == "PAY":
            record = self.corporate_actions[identifier]
            amount = decimal(record["cash_cny"])
            self.cash += amount
            book["realized"] = str(decimal(book["realized"]) + amount)
            record["status"] = "PAID"
        else:
            old_count = shares(book)
            for lot in book["lots"]:
                units, remainder = divmod(lot["remaining"] * event["numerator"], event["denominator"])
                if remainder:
                    raise ValueError("unit conversion has fractional lots; issuer settlement evidence required")
                lot["remaining"] = integer(units, minimum=0)
            self.latest.pop(code, None)
            for order in self.pending.pop(code, []):
                self._reject(order, "UNIT_CONVERSION_REQUIRES_NEW_ORDER")
            self.corporate_actions[identifier] = dict(
                event, status="APPLIED", shares_before=old_count, shares_after=shares(book),
            )

    def register(self, at, node):
        code, mode, action = node["etf"], node["size_mode"], node["action"]
        book, unit = self.books[code], self.instruments[code]
        position = shares(book)
        node.update(reference_quote=_reference(self.latest.get(code)), position_at_intent=position,
                    sellable_at_intent=_eligible(book, at), cash_at_intent=str(self.cash))
        if action in BUY_ACTIONS:
            if action == "ADD" and not position:
                return self._reject(node, "ADD_WITHOUT_POSITION")
            if mode == "SHARES":
                node["target_quantity"] = node["size"]
            else:
                amount = decimal(node["size"])
                cap = amount if mode == "AMOUNT" else self.cash * amount
                node["cash_limit"] = str(cap)
                if cap <= 0 or cap > self.cash:
                    return self._reject(node, "INSUFFICIENT_CASH")
        else:
            if mode == "ALL":
                target = position
            elif mode == "POSITION_RATIO":
                target = floor_lot(Decimal(position) * decimal(node["size"]), unit)
            else:
                target = node["size"]
            node.update(target_quantity=target, remaining_quantity=target)
            if not target:
                return self._reject(node, "NO_POSITION" if not position else "BELOW_TRADING_UNIT")
            reserved = [order for order in self.pending[code] if order["action"] not in BUY_ACTIONS]
            if mode == "ALL":
                for order in reserved:
                    order.update(status="SUPERSEDED", reason="REPLACED_BY_ALL", replaced_by=node["id"])
                self.pending[code] = [order for order in self.pending[code] if order not in reserved]
            elif target + sum(order["remaining_quantity"] for order in reserved) > position:
                return self._reject(node, "INSUFFICIENT_UNRESERVED_POSITION")
        self.pending[code].append(node)

    @staticmethod
    def _reject(node, reason):
        node.update(status="CONFLICT", reason=reason)

    def quote(self, quote, *, defer=False):
        code = quote["etf"]
        before = self.latest.get(code)
        observed = moment(quote["observed_at"])
        boundary = self.action_boundaries.get(code)
        if boundary and observed <= boundary:
            return
        if before:
            previous_at = moment(before["observed_at"])
            if observed < previous_at or (observed == previous_at and quote["revision"] <= before["revision"]):
                return
        self.latest[code] = quote
        if before and observed == moment(before["observed_at"]):
            return
        if defer:
            return True
        pending = self.pending[code]
        for node in pending:
            if observed > moment(node["at"]):
                self._fill(node, quote, observed)
        self.pending[code] = [node for node in pending if node["status"] in ("PENDING", "PARTIAL")]

    def _fill(self, node, quote, at):
        code, action = node["etf"], node["action"]
        book, unit = self.books[code], self.instruments[code]
        price = decimal(quote["price_cny"])
        book["cash"] = str(self.cash)
        if action in BUY_ACTIONS:
            if self.calendar.next(at.date()) is None:
                node["reason"] = "WAIT_CALENDAR"
                return
            cap = decimal(node.get("cash_limit", str(self.cash)))
            if cap > self.cash:
                return self._reject(node, "INSUFFICIENT_CASH")
            maximum = affordable(cap, self.rules, price, unit)
            target = node.get("target_quantity", maximum)
            if target > maximum:
                return self._reject(node, "INSUFFICIENT_CASH")
            if not target:
                return self._reject(node, "BELOW_TRADING_UNIT_OR_COST")
            result = buy(book, self.rules, target, price, at.date(), self.calendar.next(at.date()))
            node.update(target_quantity=target, remaining_quantity=0)
        else:
            target = min(node["remaining_quantity"], _eligible(book, at))
            if not target:
                return
            try:
                result = sell(book, self.rules, target, price, at.date())
            except ValueError as exc:
                if str(exc) != "INSUFFICIENT_CASH_FOR_SELL_FEE":
                    raise
                return self._reject(node, str(exc))
            node["remaining_quantity"] -= target
        self.cash = decimal(book["cash"])
        result.update(node_id=node["id"], etf=code, action=action, intent_at=node["at"],
                      filled_at=at.isoformat(), price_quote=_reference(quote),
                      deferred_t1=action not in BUY_ACTIONS and at.date() > moment(node["at"]).date()
                      and node["target_quantity"] > node["sellable_at_intent"],
                      cash_after=str(self.cash), position_after=shares(book))
        node["fills"].append(result)
        self.fills.append(result)
        node.pop("reason", None)
        node["status"] = "PARTIAL" if node["remaining_quantity"] else "FILLED"

    def close(self, cutoff, nodes):
        for code, pending in self.pending.items():
            for node in pending:
                if node["action"] not in BUY_ACTIONS and _eligible(self.books[code], cutoff) == 0:
                    node["reason"] = ("WAIT_T1" if any(lot["sellable_from"] for lot in self.books[code]["lots"]
                                                      if lot["remaining"]) else "WAIT_CALENDAR")
                else:
                    node.setdefault("reason", "WAIT_PRICE")
        positions, total_value, realized, unrealized = [], Decimal(0), Decimal(0), Decimal(0)
        for code, book in self.books.items():
            price = self.latest.get(code)
            view = valuation(book, self.rules, decimal(price["price_cny"]) if price else None)
            realized += decimal(view["realized"])
            if not view["shares"]:
                continue
            position = {"etf": code, "shares": view["shares"], "sellable": _eligible(book, cutoff),
                        "realized": view["realized"], "unrealized": view["unrealized"],
                        "price_quote": _reference(price), "market_value": None}
            if price:
                value = money(view["shares"] * decimal(price["price_cny"]))
                position["market_value"] = str(value)
                total_value += value
                unrealized += decimal(view["unrealized"])
            positions.append(position)
        known = all(position["market_value"] is not None for position in positions)
        initial = money(decimal(self.rules.initial_cash))
        receivable = sum((decimal(event["cash_cny"]) for event in self.corporate_actions.values()
                          if event["status"] == "RECEIVABLE"), Decimal(0))
        equity = self.cash + total_value + receivable if known else None
        profit = equity - initial if equity is not None else None
        if any(node["status"] == "CONFLICT" for node in nodes):
            status = "CONFLICT"
        elif any(node["status"] in ("PENDING", "PARTIAL") for node in nodes):
            status = "INCOMPLETE"
        else:
            status = "COMPLETE"
        return dict(status=status, comparable=status == "COMPLETE" and known, basis=self.rules.basis,
                    initial_cash=str(initial), cash=str(self.cash), equity=str(equity) if known else None,
                    profit=str(profit) if profit is not None else None,
                    return_pct=str((profit / initial * 100).quantize(Decimal("0.0001"))) if profit is not None else None,
                    realized=str(realized), unrealized=str(unrealized) if known else None,
                    positions=positions, fills=self.fills, nodes=nodes,
                    **(dict(corporate_actions=list(self.corporate_actions.values()),
                            distribution_receivable_cny=str(money(receivable)))
                       if self.rules.version == ACTION_EXECUTION_VERSION else {}))


def replay(bundle):
    if bundle.get("schema_version") != SCHEMA:
        raise ValueError("unsupported manual scenario schema")
    price_mode = bundle.get("price_mode", "HISTORICAL")
    if price_mode not in ("HISTORICAL", "ASSUMPTION"):
        raise ValueError("unsupported price mode")
    instruments = bundle["instruments"]
    if not isinstance(instruments, dict) or not 1 <= len(instruments) <= 300:
        raise ValueError("manual scenario requires 1 to 300 instruments")
    for code, unit in instruments.items():
        if not CODE.fullmatch(code):
            raise ValueError("invalid instrument")
        integer(unit)
    costs = bundle.get("costs", {})
    if not isinstance(costs, dict) or set(costs) - {"fee_rate", "minimum_fee", "slippage_bps"}:
        raise ValueError("unknown manual transaction costs")
    rules = Rules(version=execution_version(bundle), initial_cash=bundle.get("initial_cash", "10000.00"), **costs)
    calendar = Calendar(bundle["calendar"])
    cutoff = moment(bundle["cutoff"])
    if cutoff.date() not in calendar.days:
        raise ValueError("cutoff outside frozen trading calendar")
    nodes = _nodes(bundle["nodes"], instruments, calendar, cutoff)
    quotes = _quotes(bundle["quotes"], instruments, calendar, cutoff, price_mode)
    actions = action_schedule(bundle.get("corporate_actions", []), instruments, calendar)
    portfolio = Portfolio(rules, instruments, calendar)
    with localcontext(PRECISION):
        for at, batch in groupby(sorted(chain(nodes, quotes, actions), key=lambda row: row[:3]), key=lambda row: row[0]):
            if at > cutoff:
                break
            events = list(batch)
            for _, kind, _, event in events:
                if kind not in (0, 1):
                    portfolio.corporate_action(at, *event)
            executable = []
            for _, kind, _, quote in events:
                if kind == 0 and portfolio.quote(quote, defer=True):
                    observed = moment(quote["observed_at"])
                    for node in portfolio.pending[quote["etf"]]:
                        if observed > moment(node["at"]):
                            executable.append((observed, node["action"] in BUY_ACTIONS, node["at"], node["id"], node, quote))
            for observed, _, _, _, node, quote in sorted(executable, key=lambda row: row[:4]):
                if node["status"] in ("PENDING", "PARTIAL"):
                    portfolio._fill(node, quote, observed)
            for code in portfolio.pending:
                portfolio.pending[code] = [node for node in portfolio.pending[code] if node["status"] in ("PENDING", "PARTIAL")]
            for _, kind, _, event in events:
                if kind == 1:
                    portfolio.register(at, event)
        ordered = [row[-1] for row in sorted(nodes, key=lambda row: row[:3])]
        result = portfolio.close(cutoff, ordered)
    return {"schema_version": "stock-v4-manual-result-v1", "price_mode": price_mode,
            "cutoff": cutoff.isoformat(), "source_sha256": fingerprint(bundle), "rules": rules.snapshot(),
            "price_evidence_sha256": fingerprint(bundle["quotes"]),
            "limitations": ["CORPORATE_ACTIONS_SUPPLIED_ONLY" if actions else "CORPORATE_ACTIONS_NOT_INCLUDED"],
            **result}
