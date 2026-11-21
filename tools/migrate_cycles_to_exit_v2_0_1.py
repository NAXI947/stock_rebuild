"""One-off: put named open v2 cycles under hermes_signal_cycle_v2_0_1 (owner authorisation 2026-11-21, CLAUDE.md rule 33).

v2.0.1 differs from v2 in one point: a HOLD report no longer clears the weak trading days, only UP does. Each named
cycle gets the v2.0.1 rule snapshot and the weak trading days its stored reports give under that rule. Nothing is
sold here; fills, orders, cash, lots and the exit plan are untouched. Without --apply the database is only read.

    python -B -m tools.migrate_cycles_to_exit_v2_0_1 --env-file /absolute/.env --etf 512800.SH [--apply]
    docker exec -i stock-v4-backend python - --etf 512800.SH ... < tools/migrate_cycles_to_exit_v2_0_1.py
"""
import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import os
import sys

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import CHINA, V2, V2_0_1, Rules, moment, shares

# The three cycles of the 2026-11-21 authorisation; any other v2 cycle keeps its frozen rule.
AUTHORISED = {"512800.SH": "887665adce5a7c6e27473e5d500d207ff01e776fa8c3a221737cbf198dad80d9",
              "512480.SH": "fa0a2d9b4472b27390effb9d05587d93b88c0c174588b21e08caf5a1a4319c42",
              "588100.SH": "fdadecf44d5c3ae652b9048a01814e2e162766205a7fd368612a9eb4610e694d"}


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def migrate(cycle, events, at):
    """The v2.0.1 body of one open v2 cycle; ``events`` are (kind, payload) in ledger order."""
    fills = cycle["book"]["fills"]
    if (cycle["status"] != "OPEN" or cycle["rule"]["version"] != V2 or "exit_plan" not in cycle or not fills
            or not shares(cycle["book"])):
        raise ValueError(f"{cycle['etf']}: only an open v2 position can be migrated")
    result = deepcopy(cycle)
    result["rule"] = Rules(**(cycle["rule"] | dict(version=V2_0_1, hold_keeps_weak_days=True))).snapshot()
    days, held = [], False
    for kind, event in events:
        if not held:
            held = kind == "quote" and event["id"] == fills[0]["quote_id"]
        elif kind == "report":
            row = event["sectors"].get(cycle["primary_sector"], {})
            if row.get("state") == "WEAK":
                day = moment(event["at"]).date().isoformat()
                if day not in days:
                    days.append(day)
            elif row.get("state") == "UP" and not row.get("duplicate_evidence"):
                days = []
    if not held:
        raise ValueError(f"{cycle['etf']}: the buy quote is not among the stored events")
    result["weak_trade_days"] = days
    result["rule_change"] = dict(at=at, from_version=V2, weak_trade_days_before=cycle.get("weak_trade_days", []))
    return result


def run(ledger, codes, rules, now, apply):
    stamp, rows = now.isoformat(), []
    with ledger._connect() as conn, conn.transaction():
        if apply:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (ledger.ns,))
        else:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        found = {etf: (cid, _decode(body)) for cid, etf, body in conn.execute(
            "SELECT id,etf,body FROM cycles WHERE ns=%s AND etf=ANY(%s) AND status IN ('WAIT_BUY','OPEN','WAIT_EXIT')"
            + (" FOR UPDATE" if apply else ""), (ledger.ns, codes)).fetchall()}
        for code in codes:
            if code not in found:
                raise ValueError(f"{code}: no active cycle")
            cid, cycle = found[code]
            if AUTHORISED.get(code) != cid:
                raise ValueError(f"{code}: this cycle is not covered by the 2026-11-21 authorisation")
            if cycle["rule"]["version"] == V2_0_1:
                rows.append(dict(etf=code, cycle_id=cid, status="ALREADY_V2_0_1"))
                continue
            bought = cycle["book"]["fills"][0] if cycle["book"]["fills"] else None
            if bought is None:
                raise ValueError(f"{code}: the buy has not filled")
            # A saved report carries its whole source document; three fields of it are needed.
            events = [(kind, _decode(payload)) for kind, payload in conn.execute(
                """SELECT kind,CASE WHEN kind='report' THEN jsonb_build_object(
                       'id',payload->'id','at',payload->'at','sectors',payload->'sectors')
                       ELSE jsonb_build_object('id',payload->'id') END
                   FROM events WHERE ns=%s AND at>=%s AND (kind='report' OR payload->>'etf'=%s)
                   ORDER BY at,(kind<>'report'),id""", (ledger.ns, bought["filled_at"], code)).fetchall()]
            migrated = migrate(cycle, events, stamp)
            if migrated["rule"] != rules.snapshot():
                raise ValueError(f"{code}: the cycle's rule differs from the configured one in more than the HOLD rule")
            days = migrated["weak_trade_days"]
            rows.append(dict(etf=code, cycle_id=cid, status="MIGRATED" if apply else "DRY_RUN", from_rule=V2,
                             to_rule=V2_0_1, shares=shares(cycle["book"]), reduced=cycle["reduced"],
                             weak_trade_days_before=cycle.get("weak_trade_days", []), weak_trade_days=days,
                             next_weak_report_exits=len(days) + 1 >= rules.weak_exit_trade_days))
            if apply:
                conn.execute("UPDATE cycles SET body=%s WHERE ns=%s AND id=%s", (encode(migrated), ledger.ns, cid))
        if apply and any(row["status"] == "MIGRATED" for row in rows):
            frozen = encode(rules.snapshot())
            conn.execute("INSERT INTO rule_versions VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                         (ledger.ns, rules.version, frozen))
            saved, = conn.execute("SELECT snapshot FROM rule_versions WHERE ns=%s AND version=%s",
                                  (ledger.ns, rules.version)).fetchone()
            if saved != frozen:
                raise ValueError("changed rules require a new version")
    return dict(schema_version="stock-v4-exit-migration-v2", namespace=ledger.ns, applied=apply, cycles=rows)


def main(argv=None):
    from modules.strategy_hermes.stage2.postgres import PostgresLedger
    from modules.strategy_hermes.stage2.settings import Settings
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file")
    parser.add_argument("--namespace", help="another run of the same environment; defaults to AUTO_RUN_NAMESPACE")
    parser.add_argument("--etf", action="append", required=True, help="ETF of an open v2 cycle; repeat per cycle")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
    if args.namespace:
        if not args.namespace.startswith(settings.redis_prefix) or len(args.namespace) > 120:
            raise ValueError("namespace outside configured environment")
        settings = replace(settings, namespace=args.namespace)
    if settings.regime_mode is None:
        raise ValueError("REGIME_MODE is not set here")
    json.dump(run(PostgresLedger(settings, "LIVE"), args.etf, Rules.v2_0_1(settings.regime_mode),
                  datetime.now(CHINA), args.apply), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
