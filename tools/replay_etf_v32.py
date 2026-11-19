"""Compare ETF policies using archived native reports and an isolated test DB."""
import argparse
from copy import deepcopy
from dataclasses import replace
import gzip
import json
from pathlib import Path
from uuid import uuid4

from modules.strategy_hermes.ledger import fingerprint
from modules.strategy_hermes.simulator import moment
from modules.strategy_hermes.stage2.etf_representativeness import freeze_policy
from modules.strategy_hermes.stage2.etf_support_facts import attach_static_support
from modules.strategy_hermes.stage2.inputs import SharedMarket
from modules.strategy_hermes.stage2.native_rounds import NativeAnalyst
from modules.strategy_hermes.stage2.penetration import freeze_pair_facts, instrument_units, prepare_etf_facts
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.research_facts import fact_catalog, market_facts
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.stage2.worker import planned_at, publish_round


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


def replay(ledger, archive, mode, *, audit=False):
    result = []
    calendar = []
    for archived in archive:
        context = decode(archived["context"])
        mapping = context["mapping"]
        # The archive starts with an empty book. Preserve verified future
        # trading dates and ignore revisions to dates before the replay starts.
        calendar = sorted(set(calendar).union(day for day in context["calendar"]
            if day >= archive[0]["day"] and (not result or day > result[-1]["day"])))
        request = deepcopy(context["request"])
        cutoff = moment(context["cutoff"])
        market = SharedMarket.read(context["market"]) if context["market"] else None
        at = moment(archived["published_at"])
        policy = freeze_policy(mapping, cutoff, mode=mode)
        request["etf_policy"] = policy
        request["etf_pair_facts"] = None
        if mode != "V2":
            request["etf_pair_facts"] = freeze_pair_facts(mapping, cutoff, request["evidence"], policy)
        if mode == "V3_ENFORCE":
            options = prepare_etf_facts(mapping, market, cutoff, request["evidence"],
                                       held_codes=request.get("held_etfs", ()),
                                       representation=request["etf_pair_facts"])
            attach_static_support(options, mapping, request["etf_pair_facts"], policy)
            common = market_facts(market, cutoff)
            for sid, evidence in request["evidence"].items():
                evidence["etf_options"] = options[sid]
                evidence["fact_catalog"] = fact_catalog(evidence, common)
        if result and market:
            units = instrument_units(mapping, at)
            quotes, _ = market.quote_events(units, at)
            if quotes:
                ledger.apply_quotes(quotes, calendar, units)
        day, slot = archived["day"], archived["slot"]
        claim = ledger.claim_round(day, slot, planned_at(day, slot))
        analyst = NativeAnalyst(ledger, mapping, market, cutoff, decode(archived["decisions"]), at, archived["run_id"])
        envelope = analyst(request, None)
        report = publish_round(ledger, claim, envelope, calendar, mapping, market,
                               targets=("redis", "octop"), prepared=request)
        meta = report["input_meta"]
        projection = meta.get("representation") or meta.get("representation_shadow") or {}
        cycle_facts = [dict(etf=c["etf"], status=c["status"], primary_sector=c["primary_sector"],
                           orders=[dict(action=o["action"], status=o["status"], target=o["target"],
                                        remaining=o["remaining"]) for o in c["orders"]])
                       for c in ledger.inspect().get("cycles", ())]
        with ledger._connect() as conn:
            targets = [row[0] for row in conn.execute("SELECT target FROM outbox WHERE ns=%s AND report_id=%s "
                "AND event_key=%s ORDER BY target", (ledger.ns, report["id"], "report:"+report["id"]))]
        row = dict(day=day, slot=slot, source_report=archived["source_report_id"],
            method=meta["etf_recommendations"]["method"], mapping=mapping["version"],
            decisions=fingerprint({sid:row["state"] for sid, row in report["sectors"].items()}),
            recommendations=fingerprint(meta["etf_recommendations"]), candidates=report["candidates"],
            cycles=sorted(cycle_facts,key=lambda row:(row["etf"],row["primary_sector"])), outbox=targets,
            statistics=projection.get("statistics"), shadow_error=meta.get("representation_shadow_error"))
        if audit:
            row["states"] = {sid: value["state"] for sid, value in report["sectors"].items()}
            row["reasons"] = {}
            for item in decode(archived["decisions"]):
                sid = item["sector_id"]
                selection = meta["etf_recommendations"]["sectors"].get(sid) or {}
                options = {product["etf"]:product for product in selection.get("products", ())}
                for code in item.get("etf_choices", ()) or ():
                    pair = (request["etf_pair_facts"] or {}).get(sid, {}).get(code) or {}
                    product = options.get(code) or {}
                    row["reasons"][sid,code] = dict(
                        pair=pair.get("representativeness_status"),
                        exposure=pair.get("known_exposure"),
                        selection=selection.get("selection_status"),
                        eligibility=product.get("execution_eligibility"),
                        recommendation=product.get("recommendation_status"),
                        selection_basis=product.get("selection_basis"),
                        selected=code in selection.get("representatives", ()),
                        direction=report["sectors"].get(sid, {}).get("state"),
                    )
        result.append(row)
    return result


def audit_rounds(base, enforced):
    rows = []
    for old, new in zip(base, enforced, strict=True):
        def candidates(row):
            return {(choice["sector_id"], code): choice["rank"]
                    for code, choices in row["candidates"].items() for choice in choices}

        before, after = candidates(old), candidates(new)
        changes = []
        for pair in sorted(before.keys() | after.keys()):
            if before.get(pair) != after.get(pair):
                changes.append(dict(sector=pair[0], etf=pair[1], old_rank=before.get(pair), new_rank=after.get(pair),
                                    v2=old["reasons"].get(pair), v3=new["reasons"].get(pair)))
        anchors = {cycle["etf"]:cycle["primary_sector"] for cycle in old["cycles"]}
        changed_anchors = {cycle["etf"]:dict(old=anchors[cycle["etf"]], new=cycle["primary_sector"],
                            source=new["reasons"].get((cycle["primary_sector"],cycle["etf"])))
                           for cycle in new["cycles"]
                           if cycle["etf"] in anchors and anchors[cycle["etf"]] != cycle["primary_sector"]}
        rows.append(dict(day=old["day"], slot=old["slot"], candidates_v2=len(before), candidates_v3=len(after),
                         candidate_changes=changes, anchors_changed=changed_anchors,
                         directions_changed=[sid for sid in sorted(old["states"].keys() | new["states"].keys())
                                             if old["states"].get(sid) != new["states"].get(sid)],
                         cycles_equal=old["cycles"] == new["cycles"], outbox_equal=old["outbox"] == new["outbox"]))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-env", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--audit", action="store_true", help="emit per-round execution differences and their facts")
    args = parser.parse_args()
    settings = Settings.read(args.test_env)
    if settings.env != "test":
        raise ValueError("ETF replay requires an isolated test configuration")
    with gzip.open(args.archive, "rt", encoding="utf-8") as file:
        archive = [json.loads(line) for line in file]
    if not 1 <= len(archive) <= 25:
        raise ValueError("ETF replay requires 1 to 25 archived rounds")
    run = uuid4().hex[:12]
    modes = {}
    for mode in ("V2", "V3_SHADOW", "V3_ENFORCE"):
        ledger = PostgresLedger(replace(settings,namespace=settings.namespace+":replay:"+run+":"+mode),"FROZEN")
        ledger.migrate()
        try:
            modes[mode] = replay(ledger, archive, mode, audit=args.audit)
        finally:
            if args.audit:
                with ledger._connect() as conn, conn.transaction():
                    for table in ("simulation_fills", "simulation_orders", "outbox", "round_tasks",
                                  "rotation", "latest", "cycles", "events", "rule_versions", "runs"):
                        conn.execute(f"DELETE FROM {table} WHERE ns=%s", (ledger.ns,))
    compared = ("method", "decisions", "recommendations", "candidates", "cycles", "outbox")
    unchanged = all(all(a[key] == b[key] for key in compared)
                    for a,b in zip(modes["V2"], modes["V3_SHADOW"]))
    shadow_errors = [row["shadow_error"] for row in modes["V3_SHADOW"] if row["shadow_error"]]
    report = dict(replayed_rounds=len(archive), unchanged_v2_shadow=unchanged)
    if args.audit:
        report.update(shadow_errors=shadow_errors, execution_differences=audit_rounds(modes["V2"], modes["V3_ENFORCE"]))
    else:
        report["modes"] = modes
    print(json.dumps(report, ensure_ascii=False, default=str))
    if not unchanged or shadow_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
