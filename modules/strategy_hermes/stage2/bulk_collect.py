"""Batch-first provider collection and its isolated Stage 2 test entry."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
from urllib.request import Request

from ..ledger import encode, fingerprint
from ..simulator import CHINA, CODE
from .market_bootstrap import (ProbeError, TICKFLOW_URL, capture_request, fetch,
                               normalize_kaipanla, normalize_tickflow, save, tickflow_config)
from .postgres import PostgresLedger
from .settings import Settings
from .tushare_bootstrap import (INDEX_CODE, SourceFailure, Tushare, calendar_rows,
                                save as save_reference)


class RequestLog:
    def __init__(self, store, run_key):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}",run_key):
            raise ValueError("invalid collection run key")
        self.store, self.run_key = store, run_key
        self.new_calls=0

    def call(self, source, scope, operation, *, limit):
        key=fingerprint(scope)
        identity=(self.run_key,source,key)
        with self.store._connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                         (self.run_key+":"+source,))
            prior=conn.execute("""SELECT status,result FROM collection_requests
                   WHERE run_key=%s AND source=%s AND request_key=%s""",identity).fetchone()
            if prior:
                if prior[0]!="SAVED":
                    raise ProbeError("request already attempted in this logical batch")
                return json.loads(prior[1])
            count,=conn.execute(
                "SELECT count(*) FROM collection_requests WHERE run_key=%s AND source=%s",
                (self.run_key,source)).fetchone()
            if count>=limit:
                raise ProbeError("logical collection budget exhausted")
            conn.execute("""INSERT INTO collection_requests (run_key,source,request_key,status)
                            VALUES (%s,%s,%s,'STARTED')""",identity)
        self.new_calls+=1
        try:
            result=operation()
        except Exception as exc:
            self._finish(identity,"FAILED",{"error":type(exc).__name__})
            raise
        self._finish(identity,"SAVED",result)
        return result

    def _finish(self, identity, status, result):
        with self.store._connect() as conn:
            conn.execute("""UPDATE collection_requests SET status=%s,result=%s,finished_at=clock_timestamp()
                            WHERE run_key=%s AND source=%s AND request_key=%s""",
                         (status,encode(result),*identity))


def quote_pool(store, config):
    request = Request(TICKFLOW_URL, data=encode({"universes": ["CN_ETF"]}).encode(),
                      headers={"Content-Type": "application/json", "Accept": "application/json",
                               "User-Agent": "stock-v4-tickflow-client/1.0", "x-api-key": tickflow_config(config)})
    body = fetch(request, timeout=15)
    now=datetime.now(CHINA)
    rows, issues=normalize_tickflow(body,None,now)
    source=max(row["observed_at"] for row in rows)
    from collections import Counter
    status=dict(Counter(issues.values()))
    return save(store,"tickflow","quotes",{"universes":["CN_ETF"]},
                dict(quotes=rows,issue_counts=status),len(rows),source,now,quotes=rows) | {"issue_counts":status}


def bulk_calendar(store, config, first, last):
    params=dict(exchange="SSE",start_date=first.strftime("%Y%m%d"),end_date=last.strftime("%Y%m%d"))
    raw=Tushare(config).fetch("trade_cal",params)
    rows=calendar_rows(raw,first,last)
    if len(rows)!=(last-first).days+1:
        raise SourceFailure("calendar response does not cover the requested interval")
    return save_reference(store,"trade_cal",params,raw,rows)


def bulk_catalog(store, config):
    params={"list_status":"L"}
    raw=Tushare(config).fetch("etf_basic",params)
    items={}
    rejected=0
    for row in raw:
        code,index=row.get("ts_code"),row.get("index_code")
        if (not isinstance(code,str) or not CODE.fullmatch(code)
                or not isinstance(index,str) or not INDEX_CODE.fullmatch(index)
                or row.get("list_status")!="L"):
            rejected+=1
            continue
        if code in items:
            raise SourceFailure("duplicate ETF code in bulk catalogue")
        items[code]=row
    if not items:
        raise SourceFailure("bulk ETF catalogue contains no validated rows")
    return save_reference(store,"etf_basic",params,raw,list(items.values())) | {"unresolved_rows":rejected}


def bulk_stocks(store, config):
    params={"list_status":"L"}
    raw=Tushare(config).fetch("stock_basic",params)
    allowed={"SSE":"SH","SZSE":"SZ","BSE":"BJ"}
    items={}
    for row in raw:
        symbol,code,exchange=row.get("symbol"),row.get("ts_code"),row.get("exchange")
        suffix=allowed.get(exchange)
        if (not isinstance(symbol,str) or not re.fullmatch(r"[0-9]{6}",symbol)
                or not suffix or code!=symbol+"."+suffix or row.get("list_status")!="L"):
            continue
        if code in items:
            raise SourceFailure("duplicate stock code in bulk catalogue")
        items[code]=row
    if not items:
        raise SourceFailure("bulk stock catalogue contains no validated rows")
    return save_reference(store,"stock_basic",params,raw,list(items.values())) | {
        "unresolved_rows":len(raw)-len(items)}


def board_page(store, captures, offset, expected_day):
    request,scope=capture_request(captures,"041",{"Index":str(offset)})
    now=datetime.now(CHINA)
    body,count,source=normalize_kaipanla(fetch(request),"041",scope,now)
    days=body.get("trade_days")
    total=body.get("reported_total")
    if (days!=[expected_day] or type(total) is not int or total<offset+count
            or count>int(scope["st"])):
        raise ProbeError("Kaipanla board page date or count changed")
    stored=save(store,"kaipanla","041",scope,body,count,source,now)
    return stored | {"total":total,"page_size":int(scope["st"]),"day":expected_day,
                     "ids":[str(row[0]) for row in body["rows"]]}


def board_list(store, budget, captures, day, *, page_limit):
    ids=set()
    pages=[]
    offset=0
    total=None
    while len(pages)<page_limit:
        page=budget.call("kaipanla:041",[day,offset],
                         lambda:board_page(store,captures,offset,day),limit=page_limit)
        if total is None:
            total=page["total"]
        page_ids=set(page["ids"])
        if total!=page["total"] or len(page_ids)!=page["rows"] or ids.intersection(page_ids):
            raise ProbeError("Kaipanla page total or identities changed during collection")
        ids.update(page_ids)
        pages.append(page["batch"])
        offset+=page["rows"]
        if offset==total:
            break
        if page["rows"]<page["page_size"]:
            break
    return dict(rows=len(ids),reported_total=total,complete=len(ids)==total,
                pages=len(pages),batches=pages,trade_date=day)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",required=True)
    parser.add_argument("--run-key",required=True)
    sub=parser.add_subparsers(dest="dataset",required=True)
    for command in ("quotes","calendar","catalog","stocks"):
        cmd=sub.add_parser(command)
        cmd.add_argument("--config",required=True)
        if command=="calendar":
            cmd.add_argument("--from",dest="first",type=date.fromisoformat,required=True)
            cmd.add_argument("--to",dest="last",type=date.fromisoformat,required=True)
    boards=sub.add_parser("boards")
    boards.add_argument("--captures",type=Path,required=True)
    boards.add_argument("--day",type=date.fromisoformat,required=True)
    boards.add_argument("--page-limit",type=int,default=6)
    args=parser.parse_args()
    try:
        settings=Settings.read(args.env_file)
        if settings.env!="test":
            raise ValueError("bulk validation uses the isolated test database")
        store=PostgresLedger(settings,"FROZEN")
        budget=RequestLog(store,args.run_key)
        if args.dataset=="quotes":
            result=budget.call("tickflow:quotes",["CN_ETF"],lambda:quote_pool(store,args.config),limit=1)
        elif args.dataset=="calendar":
            if not 0<=(args.last-args.first).days<=90:
                raise ValueError("calendar bulk window exceeds 91 days")
            result=budget.call("tushare:trade_cal",[args.first.isoformat(),args.last.isoformat()],
                               lambda:bulk_calendar(store,args.config,args.first,args.last),limit=1)
        elif args.dataset=="catalog":
            result=budget.call("tushare:etf_basic",["L"],lambda:bulk_catalog(store,args.config),limit=1)
        elif args.dataset=="stocks":
            result=budget.call("tushare:stock_basic",["L"],lambda:bulk_stocks(store,args.config),limit=1)
        else:
            if not 1<=args.page_limit<=8 or args.day>datetime.now(CHINA).date():
                raise ValueError("invalid board day or page limit")
            result=board_list(store,budget,args.captures,args.day.isoformat(),page_limit=args.page_limit)
        print(json.dumps(result|{"new_requests":budget.new_calls},ensure_ascii=False,indent=2))
        return 0
    except (ProbeError,SourceFailure) as exc:
        parser.exit(1,str(exc)+"\n")
    except (OSError,TypeError,KeyError,ValueError) as exc:
        parser.exit(1,"bulk intake failed ("+type(exc).__name__+")\n")


if __name__=="__main__":
    raise SystemExit(main())
