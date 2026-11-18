"""Stage 2 operator entry for shared data intake and committed Hermes reports."""
import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys

import psycopg
from redis.exceptions import RedisError

from ..simulator import CHINA, moment
from .inputs import cache_client, mapping_file, read_file, shared_market, trading_days
from .outbox import dispatch, redis_delivery
from .penetration import instrument_units
from .postgres import LeaseLost, PostgresLedger
from .settings import Settings
from .worker import SLOTS, planned_at, publish_round, tick_inbox


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--input-kind", choices=("FROZEN","LIVE"), required=True)
    namespaces = parser.add_mutually_exclusive_group()
    namespaces.add_argument("--test-namespace", help="dedicated namespace in the test database")
    namespaces.add_argument("--dev-namespace", help="dedicated development namespace")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("migrate")
    commands.add_parser("inspect")
    commands.add_parser("flush-redis")
    commands.add_parser("report").add_argument("id")
    recovery=commands.add_parser("authorize-publication-recovery",
        help="authorize one saved PUBLISH failure; then use OCTOP's original run-now")
    recovery.add_argument("parent_run_id")
    recovery.add_argument("--calendar",type=Path,required=True)
    recovery.add_argument("--operator",required=True)
    recovery.add_argument("--reason",required=True)
    for name in ("publish-report","apply-quotes","tick","serve"):
        cmd=commands.add_parser(name)
        cmd.add_argument("--calendar",type=Path,required=True)
        cmd.add_argument("--mapping",type=Path,required=True)
        source=cmd.add_mutually_exclusive_group(required=True)
        source.add_argument("--snapshot-key")
        source.add_argument("--snapshot-file",type=Path)
        if name=="publish-report":
            cmd.add_argument("--day",required=True)
            cmd.add_argument("--slot",choices=SLOTS,required=True)
            cmd.add_argument("envelope",type=Path)
        else:
            cmd.add_argument("--received-at",help="explicit clock for frozen replay only")
        if name == "serve":
            transport = cmd.add_mutually_exclusive_group(required=True)
            transport.add_argument("--octop-stdio", action="store_true", help="operator RPC relay on standard streams")
            transport.add_argument("--octop-command-json", help="OCTOP bridge command as a JSON argument list")
            cmd.add_argument("--poll-seconds", type=float, default=15)
            cmd.add_argument("--once", action="store_true")
            cmd.add_argument("--manual-queue", action="store_true", help="process durable manual backtests in a dedicated subprocess")
            cmd.add_argument("--trial-day", help="one trading-day development run")
            cmd.add_argument("--tickflow-config", type=Path)
            cmd.add_argument("--kaipanla-captures", type=Path)
            cmd.add_argument("--qq", action="store_true", help="deliver automatic simulation notices through the pinned OCTOP QQ route")
        if name=="tick":
            cmd.add_argument("--inbox",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        settings=Settings.read(args.env_file)
        if args.test_namespace:
            if settings.env != "test" or not args.test_namespace.startswith(settings.redis_prefix):
                raise ValueError("test namespace outside the test environment")
            settings = replace(settings, namespace=args.test_namespace)
        if args.dev_namespace:
            if settings.env != "development" or not args.dev_namespace.startswith(settings.redis_prefix):
                raise ValueError("namespace outside the development environment")
            settings = replace(settings, namespace=args.dev_namespace)
        ledger=PostgresLedger(settings,args.input_kind)
        if args.command=="migrate":
            ledger.migrate()
            with ledger._connect() as connection:
                revisions=[row[0] for row in connection.execute(
                    "SELECT revision FROM schema_revisions ORDER BY revision")]
            result={"migrations":revisions,"environment":settings.env}
        elif args.command=="inspect":
            result=ledger.inspect()
        elif args.command=="report":
            result=ledger.report(args.id)
        elif args.command=="authorize-publication-recovery":
            from .native_rounds import NativeRounds
            from .native_recovery import authorize
            from ..runtime import _paused
            result=authorize(NativeRounds(ledger,args.calendar,paused=_paused()),args.parent_run_id,
                             operator=args.operator,reason=args.reason)
        else:
            with cache_client(settings) as cache:
                if args.command == "serve":
                    from .schedule import serve
                    return serve(ledger, cache, args)
                if args.command=="flush-redis":
                    result={"delivery":dispatch(ledger,"redis",lambda event:redis_delivery(ledger,cache,event))}
                else:
                    days=trading_days(args.calendar)
                    mapping=mapping_file(args.mapping)
                    market=shared_market(ledger,cache,key=args.snapshot_key,path=args.snapshot_file)
                    if args.command in ("apply-quotes","tick"):
                        if args.received_at and ledger.origin!="FROZEN":
                            raise ValueError("explicit receiver clock is available only for frozen replay")
                        now=moment(args.received_at) if args.received_at else datetime.now(CHINA)
                    if args.command=="tick":
                        result=tick_inbox(ledger,cache,now,days,mapping,market,args.inbox)
                    elif args.command=="apply-quotes":
                        if market is None:
                            raise ValueError("shared market snapshot unavailable")
                        units=instrument_units(mapping,now)
                        events,issues=market.quote_events(units,now)
                        ledger.apply_quotes(events,days,units)
                        result={"events_read":len(events),"issues":issues,"market":market.snapshot_id}
                    else:
                        envelope=read_file(args.envelope)
                        if envelope.get("input_kind")!=ledger.origin or args.day not in days:
                            raise ValueError("report environment or trading day mismatch")
                        claim=ledger.claim_round(args.day,args.slot,planned_at(args.day,args.slot))
                        if claim is None:
                            raise ValueError("round is already claimed, committed or exhausted")
                        try:
                            result=publish_round(ledger,claim,envelope,days,mapping,market)
                        except Exception as exc:
                            ledger.fail_round(claim,type(exc).__name__)
                            raise
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if result.get("round")=="FAILED" else 0
    except (ValueError,KeyError,TypeError,OSError,LeaseLost) as exc:
        print(f"stage2: {type(exc).__name__}: {exc}",file=sys.stderr)
        return 1
    except (psycopg.Error,RedisError) as exc:
        print(f"stage2: database/cache failure ({type(exc).__name__})",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
