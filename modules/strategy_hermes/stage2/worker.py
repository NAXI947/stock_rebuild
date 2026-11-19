"""Five-round orchestration on the development database."""
from datetime import date, datetime, time, timedelta
from time import monotonic, sleep
from random import uniform

from ..adapter import READ_ONLY_OBSERVATION, normalize
from ..simulator import CHINA, moment
from .inputs import read_file
from .notifications import report_market_scope, stock_watchlist
from .outbox import dispatch, redis_delivery
from .penetration import evaluate_recommendations, instrument_units, recommendation_candidates

SLOTS = ("09:35","10:35","13:05","14:05","14:30")


def planned_at(day, slot):
    if slot not in SLOTS:
        raise ValueError("unknown Hermes round slot")
    return datetime.combine(date.fromisoformat(day),time.fromisoformat(slot),tzinfo=CHINA)


def due_round(now, days, grace=8):
    now=now.astimezone(CHINA)
    day=now.date().isoformat()
    if day not in days:
        return None
    for slot in SLOTS:
        planned=planned_at(day,slot)
        if planned<=now<planned+timedelta(minutes=grace):
            return day, slot, planned
    return None


class RequestBudget:
    def __init__(self, ledger, claim, *, seconds=75):
        self.ledger, self.claim = ledger, claim
        self.deadline=monotonic()+seconds

    def call(self, service, operation, *, attempts, timeout):
        if service not in ("hermes","mx"):
            raise ValueError("unsupported round service")
        failure=None
        retries=0
        while True:
            remaining=self.deadline-monotonic()
            if remaining<=0 or not self.ledger.take_call(self.claim, service, attempts):
                raise TimeoutError(f"{service} request budget exhausted") from failure
            try:
                return operation(timeout=min(timeout,remaining))
            except (TimeoutError,ConnectionError) as exc:
                failure=exc
                retries += 1
                sleep(min(max(0, self.deadline-monotonic()), uniform(0, min(4, .25 * 2**min(retries, 4)))))


def rotation_window(ledger, days, day, slot, report_id):
    idx=days.index(day)
    keys=[]
    if idx:
        keys.extend((days[idx-1],s) for s in SLOTS[2:])
    keys.extend((day,s) for s in SLOTS[:SLOTS.index(slot)+1])
    with ledger._connect() as conn:
        rows=conn.execute(
            """SELECT trade_date,slot,state,report_id FROM round_tasks
               WHERE ns=%s AND trade_date=ANY(%s::date[])""",
            (ledger.ns,sorted({d for d,_ in keys})),
        )
        found={(d.isoformat(),s):(status,rid) for d,s,status,rid in rows}
    found[day,slot]=("PUBLISHED",report_id)
    return [dict(trade_date=d,slot=s,status=found.get((d,s),("MISSING",None))[0],
                 report_id=found.get((d,s),("MISSING",None))[1],
                 planned_at=planned_at(d,s).isoformat()) for d,s in keys]


def publish_round(ledger, claim, envelope, days, mapping, market, *, targets=("redis",), prepared=None):
    if envelope.get("input_kind") != ledger.origin:
        raise ValueError("round report input kind does not match its environment")
    if envelope.get("purpose") == READ_ONLY_OBSERVATION:
        raise ValueError("read-only OCTOP observation cannot publish a round")
    event = {"kind":"report","report_json":envelope["report_json"],
             "published_at":envelope["published_at"],"judgments":envelope.get("judgments",[])}
    decoded=normalize(event)
    at=moment(decoded["at"])
    if at.date().isoformat() != claim.day or claim.day not in days:
        raise ValueError("report outside the configured trading day")
    if ledger.origin=="LIVE":
        if market and moment(market.observed_at)>at:
            raise ValueError("market snapshot postdates the report publication")
    octop = envelope.get("octop", {})
    prepared = prepared or {}
    policy = prepared.get("etf_policy") or {}
    mode = policy.get("policy_mode", "V2")
    pairs = prepared.get("etf_pair_facts")
    recommendations = evaluate_recommendations(event, mapping, market,
        core_leaders=octop.get("core_leaders"), event_members=octop.get("event_members"),
        etf_choices=octop.get("etf_choices"),
        representation=pairs if mode == "V3_ENFORCE" else None,
        selection_audit=octop.get("etf_selection_audit") if mode == "V3_ENFORCE" else None)
    event["candidates"] = recommendation_candidates(recommendations)
    # One saved evaluation feeds display, candidate admission, and replay.
    metadata=dict(penetration=recommendations, etf_recommendations=recommendations,
                  mapping_version=mapping["version"],
                  source_observed_at=market.observed_at if market else None,
                  rotation_window=rotation_window(ledger,days,claim.day,claim.slot,decoded["id"]),
                  analyst_version=envelope.get("analyst_version"),
                  stock_watchlist=stock_watchlist(decoded,mapping,market,slot=claim.slot),
                  market_scope=report_market_scope(market,mapping,decoded))
    if "octop" in envelope:
        metadata["octop"] = envelope["octop"]
    if "manual_recovery" in envelope:
        metadata["manual_recovery"] = envelope["manual_recovery"]
    if policy:
        metadata["etf_policy"] = policy
    options = {}
    quoted = sorted(octop.get("market_evidence", {}))
    if mode == "V3_ENFORCE":
        options["etf_projection"] = dict(policy=policy, pairs=pairs, mapping=mapping, quote_sectors=quoted)
    elif mode == "V3_SHADOW":
        from copy import deepcopy
        from .etf_representativeness import shadow_result

        def shadow_recommendations():
            result = evaluate_recommendations(deepcopy(event), deepcopy(mapping), deepcopy(market),
                core_leaders=deepcopy(octop.get("core_leaders")), event_members=deepcopy(octop.get("event_members")),
                etf_choices=deepcopy(octop.get("etf_choices")), representation=deepcopy(pairs))
            return dict(policy=deepcopy(policy), pairs=deepcopy(pairs), mapping=deepcopy(mapping),
                        recommendations=result, quote_sectors=list(quoted))

        diagnostic = prepared.get("etf_shadow_error")
        if not diagnostic:
            projection, diagnostic = shadow_result("RECOMMEND", shadow_recommendations)
            if projection is not None:
                options["etf_projection"] = projection
        if diagnostic:
            metadata["representation_shadow_error"] = diagnostic
    return ledger.publish(claim,event,days,instrument_units(mapping,at),metadata,targets=targets,**options)


def run_due(ledger, now, days, mapping, market, analyst, *, prepare=None, targets=("redis",)):
    due=due_round(now,days)
    if due is None:
        return "NO_DUE_ROUND"
    day, slot, planned=due
    task=ledger.claim_round(day,slot,planned)
    if task is None:
        return "ROUND_ALREADY_CLAIMED"
    try:
        budget=RequestBudget(ledger,task,seconds=100 if prepare else 75)
        try:
            view=ledger.inspect()
        except ValueError as exc:
            if str(exc)!="unknown replay namespace":
                raise
            view={}
        holdings=view.get("cycles", [])
        tracked = set()
        for row in holdings:
            if row["status"] in ("WAIT_BUY", "OPEN", "WAIT_EXIT"):
                tracked.add(row["primary_sector"])
                tracked.update(item["sector_id"] for item in row.get("entry_sources", ()))
        request=dict(trade_date=day,round_slot=slot,planned_at=planned.isoformat(),
                     tracked_sectors=sorted(tracked),market_snapshot_id=market.snapshot_id if market else None)
        prepared = None
        if prepare is None:
            report = budget.call("hermes", lambda timeout: analyst(request, budget, timeout),
                                 attempts=2, timeout=35)
        else:
            request["active_directions"] = view.get("rotation", {})
            prepared = prepare(request, budget)
            report = analyst(prepared, budget)
        return publish_round(ledger,task,report,days,mapping,market,targets=targets,prepared=prepared)
    except Exception as exc:
        ledger.fail_round(task,type(exc).__name__)
        raise


def tick_inbox(ledger, cache, now, days, mapping, market, inbox):
    """Process one published-report handoff and the latest shared prices."""
    result={"round":"NO_DUE_ROUND","quote_events":0,"quote_issues":{}}
    due=due_round(now,days)
    if due is not None:
        day, slot, planned=due
        path=inbox / day / (slot.replace(":","")+".json")
        if not path.is_file():
            result["round"]="WAITING_REPORT"
        else:
            claim=ledger.claim_round(day,slot,planned)
            if claim is None:
                result["round"]="ROUND_ALREADY_CLAIMED"
            else:
                try:
                    envelope=read_file(path)
                    report=publish_round(ledger,claim,envelope,days,mapping,market)
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    ledger.fail_round(claim,type(exc).__name__)
                    result.update(round="FAILED",round_error=type(exc).__name__)
                else:
                    result.update(round="PUBLISHED",report_id=report["id"])
    result.update(apply_market(ledger, now, days, mapping, market))
    result["redis_delivery"] = dispatch(ledger, "redis", lambda event: redis_delivery(ledger, cache, event))
    return result


def apply_market(ledger, now, days, mapping, market):
    if ledger.origin == "LIVE":
        ledger.refresh_entry_admission()
    if (market is None or now.date().isoformat() not in days
            or (ledger.origin == "LIVE" and moment(market.observed_at).date() != now.date())):
        return {"quote_events": 0, "quote_issues": {}}
    from .execution_units import quote_instruments
    units = quote_instruments(ledger, instrument_units(mapping, now))
    quotes, issues = market.quote_events(units, now)
    if quotes:
        ledger.apply_quotes(quotes, days, units)
    return {"quote_events": len(quotes), "quote_issues": issues}


def scheduled_tick(ledger, cache, now, days, mapping, market, analyst, *, qq=None):
    from .postgres import LeaseLost

    result = apply_market(ledger, now, days, mapping, market)
    try:
        targets = ("redis","octop") if qq is not None else ("redis",)
        outcome = run_due(ledger, now, days, mapping, market, analyst, prepare=analyst.prepare, targets=targets)
    except (ValueError, TypeError, KeyError, OSError, LeaseLost) as exc:
        result.update(round="FAILED", round_error=type(exc).__name__)
    else:
        if isinstance(outcome, dict):
            result.update(round="PUBLISHED", report_id=outcome["id"],
                          mx=outcome["input_meta"]["octop"]["mx"]["status"])
        else:
            result["round"] = outcome
    result["redis_delivery"] = dispatch(ledger, "redis", lambda event: redis_delivery(ledger, cache, event))
    if qq is not None:
        result["qq_delivery"] = dispatch(ledger,"octop",qq)
    return result
