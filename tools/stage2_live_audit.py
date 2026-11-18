"""Read the live trial ledger and save a compact, source-bound acceptance report."""
import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path

from redis.exceptions import RedisError

from shared_kernel.temp_manager import TempManager, store_file
from modules.strategy_hermes.ledger import encode, fingerprint
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.inputs import cache_client
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.stage2.worker import SLOTS, planned_at


def audit(ledger, day, now):
    first = f"{ledger.ns}:feed:{day}:"
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tasks = {
            slot: (state,error,attempts,json.loads(calls),rid)
            for slot,state,error,attempts,calls,rid in conn.execute(
                """SELECT slot,state,error_code,attempts,calls,report_id FROM round_tasks
                   WHERE ns=%s AND trade_date=%s""",(ledger.ns,day))
        }
        refs = {}
        for key,source,status,result in conn.execute(
            """SELECT run_key,source,status,result FROM collection_requests
               WHERE starts_with(run_key,%s) ORDER BY started_at""",(first,)
        ):
            batch = json.loads(result) if result is not None else {}
            refs.setdefault(key.removeprefix(first),[]).append({
                "source":source, "state":status, "batch":batch.get("batch"),
                "rows":batch.get("rows"), "observed_at":batch.get("source_clock"), "error":batch.get("error"),
            })
        reports = {
            slot:(json.loads(payload), delivery)
            for slot,payload,delivery in conn.execute(
                """SELECT t.slot,e.payload,o.state FROM round_tasks t
                   JOIN events e ON e.ns=t.ns AND e.id=t.report_id AND e.kind='report'
                   LEFT JOIN outbox o ON o.ns=t.ns AND o.report_id=t.report_id AND o.target='redis'
                   WHERE t.ns=%s AND t.trade_date=%s""",(ledger.ns,day))
        }
        orders,fills,actual = conn.execute(
            """SELECT (SELECT count(*) FROM simulation_orders WHERE ns=%s),
                      (SELECT count(*) FROM simulation_fills WHERE ns=%s),
                      (SELECT count(*) FROM actual_trades WHERE ns=%s)""",
            (ledger.ns,ledger.ns,ledger.ns),
        ).fetchone()
    rounds=[]
    for slot in SLOTS:
        planned=planned_at(day,slot)
        default="UPCOMING" if now<planned else "MISSING" if now>=planned+timedelta(minutes=8) else "AWAITING_CLAIM"
        state,error,attempts,calls,rid=tasks.get(slot,(default,None,0,{},None))
        report,redis=reports.get(slot,({},None))
        details=report.get("input_meta",{}).get("octop",{})
        mx=details.get("mx") or {}
        batches=refs.get(slot.replace(":",""),[])
        rounds.append(dict(slot=slot,planned_at=planned.isoformat(),state=state,error=error,
                           attempts=attempts,calls=calls,report_id=rid,published_at=report.get("at"),
                           report_status=report.get("status"),data_as_of=report.get("data_as_of"),
                           snapshot_id=details.get("market_snapshot_id"),
                           direction={key:row.get("phase") for key,row in report.get("sectors",{}).items()},
                           candidates=len(report.get("candidates",{})),redis_delivery=redis,
                           mx_status=mx.get("status"),mx_rows=len(mx.get("items",[])),collection=batches))
    try:
        with cache_client(ledger.settings) as cache:
            shared_views=len(list(cache.scan_iter(match=ledger.ns+":snap:*")))
    except (OSError, RedisError):
        shared_views=None
    return dict(schema_version="stock-v4-live-trial-audit-v1",namespace=ledger.ns,trade_date=day,
                checked_at=now.isoformat(),rounds=rounds,redis_views=shared_views,
                simulation_orders=orders,simulation_fills=fills,actual_trades=actual)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--day",type=date.fromisoformat,required=True)
    parser.add_argument("--output-dir",type=Path)
    args=parser.parse_args()
    settings=Settings.read(args.env_file)
    if settings.env!="development":
        parser.error("audit is limited to a development live trial")
    day=args.day.isoformat()
    ns=settings.redis_prefix+"stage2-live-"+args.day.strftime("%Y%m%d")
    ledger=PostgresLedger(replace(settings,namespace=ns),"LIVE")
    report=audit(ledger,day,datetime.now(CHINA))
    if args.output_dir:
        out=args.output_dir
        if (not out.is_absolute() or out.resolve()!=out or not out.is_dir()
                or out.stat().st_mode & 0o077):
            parser.error("output must be a private absolute directory")
        manager=TempManager(os.environ["APP_TMP_ROOT"],persistent=out)
        path=store_file(manager,out,f"live-trial-audit-{day}-{fingerprint(report)[:16]}.json",encode(report).encode())
        print(path)
    else:
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
