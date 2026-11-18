"""Assemble committed bulk data into test snapshots and a dated mapping pilot."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

import xlrd
from redis.exceptions import RedisError

from shared_kernel.temp_manager import TempManager, store_file
from ..ledger import encode, fingerprint
from ..simulator import CHINA, decimal, moment, session
from .inputs import SharedMarket, cache_client
from .market_notices import NOTICE_LUA, notice_body, notice_keys
from .postgres import PostgresLedger
from .settings import Settings

CSI = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/"
EXCHANGES = {"上海证券交易所":"SH","深圳证券交易所":"SZ","北京证券交易所":"BJ"}
CSI_LIMIT = 4 * 1024 * 1024
PILOT_ETF = "512480.SH"
PILOT_INDEX = "H30184.CSI"
PILOT_PLATE = "801001"


def csi_evidence(raw, index, url):
    sheet=xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
    header=sheet.row_values(0)
    if (sheet.ncols!=9 or sheet.nrows<2 or not str(header[0]).startswith("日期")
            or not str(header[4]).startswith("成份券代码") or not str(header[7]).startswith("交易所")):
        raise ValueError("unexpected CSI constituent workbook")
    symbols, days = set(), set()
    for idx in range(1,sheet.nrows):
        row=sheet.row_values(idx)
        code,exchange=str(row[4]),EXCHANGES.get(row[7])
        if row[1]!=index or not re.fullmatch(r"[0-9]{6}",code) or exchange is None:
            raise ValueError("invalid CSI constituent identity")
        full=code+"."+exchange
        if full in symbols:
            raise ValueError("duplicate CSI constituent identity")
        symbols.add(full)
        days.add(date.fromisoformat(row[0]).isoformat())
    if len(days)!=1:
        raise ValueError("inconsistent CSI constituent date")
    return {"source":url,"sha256":sha256(raw).hexdigest(),"as_of":days.pop(),"codes":sorted(symbols)}


def csi_download(index):
    url=CSI+index+"cons.xls"
    req=Request(url,headers={"Accept":"application/vnd.ms-excel","User-Agent":"stock-v4-reference-check/1.0"})
    with urlopen(req,timeout=10) as stream:
        raw=stream.read(CSI_LIMIT+1)
    if len(raw)>CSI_LIMIT:
        raise ValueError("CSI workbook exceeds size limit")
    return raw,csi_evidence(raw,index,url)


def load_batches(conn, run_key):
    refs=defaultdict(list)
    for source, raw in conn.execute(
        "SELECT source,result FROM collection_requests WHERE run_key=%s AND status='SAVED' ORDER BY started_at,request_key",(run_key,)
    ):
        result=json.loads(raw)
        refs[source].append(result)
    for source in ("tickflow:quotes","kaipanla:041","tushare:trade_cal","tushare:stock_basic","tushare:etf_basic"):
        if not refs.get(source):
            raise ValueError("missing completed collection " + source)
    return refs


def board_snapshot(conn, refs):
    rows={}
    totals,dates,times=set(),set(),[]
    for ref in refs:
        body,=conn.execute("SELECT body FROM supplier_market_batches WHERE batch_id=%s",(ref["batch"],)).fetchone()
        source=json.loads(body)
        totals.add(source["reported_total"])
        dates.update(source["trade_days"])
        times.append(ref["source_clock"])
        for row in source["rows"]:
            ident="kaipanla:"+str(row[0])
            if ident in rows:
                raise ValueError("overlapping board snapshot pages")
            item=dict(sector_id=ident,sector_name=str(row[1]),observed_at=ref["source_clock"],
                      source_batch=ref["batch"])
            for index,key,scale in ((2,"strength",1),(3,"return_ratio",100)):
                try:
                    item[key]=str(decimal(row[index])/scale)
                except (ValueError,IndexError):
                    item[key]=None
            rows[ident]=item
    if len(totals)!=1 or len(dates)!=1:
        raise ValueError("inconsistent board snapshot pages")
    total=next(iter(totals))
    if len(rows)>total:
        raise ValueError("board pages exceed the reported universe")
    return rows,dates.pop(),max(times),total


def stock_identity(conn, batch_id):
    data,=conn.execute("SELECT normalized FROM supplier_batches WHERE batch_id=%s",(batch_id,)).fetchone()
    identities=defaultdict(list)
    classes={}
    rows = json.loads(data) if isinstance(data, (str, bytes)) else data
    for item in rows:
        # Reference maintenance stores {entity_key, body}; older frozen batches
        # contain the provider row directly. Never infer a market from a code.
        if not isinstance(item, dict):
            continue
        row = item.get("body", item)
        if not isinstance(row, dict) or row.get("list_status", "L") != "L":
            continue
        code, symbol = row.get("ts_code"), row.get("symbol")
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code)
                or symbol != code[:6]):
            continue
        if code not in identities[symbol]:
            identities[symbol].append(code)
        market = row.get("market")
        if isinstance(market, str) and market.strip():
            classes[code] = market.strip()
    return identities,classes


def membership(conn, day, plate, identities):
    pages={}
    for batch,scope,raw in conn.execute(
        """SELECT batch_id,scope,body FROM supplier_market_batches
           WHERE provider='kaipanla' AND endpoint='046' AND scope->>'Date'=%s AND scope->>'PlateID'=%s
           ORDER BY collected_at""",(day,plate)
    ):
        offset=int(json.loads(scope)["Index"])
        pages[offset]=(batch,json.loads(raw))
    offset=0
    raw_ids=set()
    batches=[]
    total=None
    for start,(batch,body) in sorted(pages.items()):
        codes={str(row[0]) for row in body["rows"]}
        total=body["reported_total"] if total is None else total
        if (start!=offset or raw_ids.intersection(codes) or len(codes)!=len(body["rows"])
                or body["reported_total"]!=total or body["trade_days"]!=[day]):
            raise ValueError("invalid historical membership pages")
        raw_ids.update(codes)
        offset+=len(codes)
        batches.append(batch)
    if not raw_ids or offset!=total:
        raise ValueError("membership pages do not cover the reported total")
    resolved=sorted(identities[code][0] for code in raw_ids if len(identities[code])==1)
    return dict(members=resolved,as_of_date=day,reported_count=total,
                unresolved_count=total-len(resolved),source_batches=batches)


def assemble(conn, refs, csi, membership_day):
    stock_batch=refs["tushare:stock_basic"][0]["batch"]
    identities,classes=stock_identity(conn,stock_batch)
    boards,day,board_clock,board_total=board_snapshot(conn,refs["kaipanla:041"])
    tick=refs["tickflow:quotes"][0]
    raw,received=conn.execute("SELECT body,collected_at FROM supplier_market_batches WHERE batch_id=%s",
                              (tick["batch"],)).fetchone()
    quotes={}
    for row in json.loads(raw)["quotes"]:
        at=moment(row["observed_at"])
        if at.date().isoformat()==day:
            quotes[row["code"]]=dict(price_cny=row["price_cny"],pre_close_cny=row["pre_close_cny"],
                volume_shares=row["volume_shares"],amount_cny=row["amount_cny"],observed_at=row["observed_at"],
                trade_date=day,source="tickflow")
    if not quotes:
        raise ValueError("board and ETF source trading days do not match")
    observed=max(quote["observed_at"] for quote in quotes.values())
    market_id="tickflow-kpl-"+fingerprint([tick["batch"],sorted(b["batch"] for b in refs["kaipanla:041"])])[:24]
    market=dict(schema_version="hotspot_market_snapshot_v1",snapshot_id=market_id,
                trade_date=day,mode="PARTIAL_REALTIME" if session(received.astimezone(CHINA)) else "EOD",
                source="tickflow+kaipanla",observed_at=observed,source_data_at=observed,
                time_basis="SOURCE_TIME",quotes=quotes,sectors=list(boards.values()),
                quality=dict(etf_quote_count=len(quotes),board_count=len(boards),board_reported_total=board_total,
                             board_observed_at=board_clock,stock_quotes="NOT_COLLECTED",iopv="NOT_COLLECTED"),
                provenance=dict(tickflow=tick["batch"],kaipanla=[b["batch"] for b in refs["kaipanla:041"]]))
    SharedMarket.read(market)

    calendar_id=refs["tushare:trade_cal"][0]["batch"]
    calendar_body,calendar_at=conn.execute(
        "SELECT normalized,collected_at FROM supplier_batches WHERE batch_id=%s",(calendar_id,)).fetchone()
    days={r["cal_date"]:r["is_open"] for r in json.loads(calendar_body)}
    if day not in days or not any(d>day and o for d,o in days.items()):
        raise ValueError("calendar is missing the snapshot day or next session")
    calendar=dict(schema_version="hotspot_trade_calendar_v1",source="quant.trade_calendar",
                  exchange="SSE",generated_at=calendar_at.astimezone(CHINA).isoformat(),
                  range_start=min(days),range_end=max(days),days=days,source_batch=calendar_id)
    sector_id="kaipanla:"+PILOT_PLATE
    sector=membership(conn,membership_day,PILOT_PLATE,identities)
    sector["sector_name"]=boards[sector_id]["sector_name"]
    etf_raw,etf_batch=conn.execute(
        "SELECT reference_data,batch_id FROM supplier_etf_catalog WHERE code=%s",(PILOT_ETF,)).fetchone()
    product=json.loads(etf_raw)
    if product["index_code"]!=PILOT_INDEX or product["etf_type"]!="纯境内":
        raise ValueError("unexpected pilot ETF scope")
    weight_date,=conn.execute("SELECT max(weight_date) FROM supplier_index_weights WHERE index_code=%s",
                              (PILOT_INDEX,)).fetchone()
    weight_rows=conn.execute("""SELECT constituent_code,weight_ratio,batch_id FROM supplier_index_weights
        WHERE index_code=%s AND weight_date=%s ORDER BY constituent_code""",(PILOT_INDEX,weight_date)).fetchall()
    if not weight_rows:
        raise ValueError("pilot weights unavailable")
    weight_codes={r[0] for r in weight_rows}
    known_total=sum(r[1] for r in weight_rows)
    evidence_day=date.fromisoformat(csi["as_of"])
    same_set=weight_codes==set(csi["codes"])
    complete=(same_set and weight_date<=evidence_day<=date.fromisoformat(day)
              and abs(known_total-Decimal(1))<=Decimal(".005"))
    weighted_rows=[dict(code=code,weight=str(weight),
                   mainboard=(classes[code]=="主板") if classes.get(code) in
                     ("主板","创业板","科创板","北交所") else None) for code,weight,_ in weight_rows]
    overlap=set(sector["members"]).intersection(weight_codes)
    known_exposure=sum((weight for code,weight,_ in weight_rows if code in overlap),Decimal(0))
    mainboard_weight=sum((weight for code,weight,_ in weight_rows if classes.get(code)=="主板"),Decimal(0))
    mapping=dict(schema_version="stock-v4-etf-mapping-v1",sectors={sector_id:sector},
                products={PILOT_ETF:dict(index_code=PILOT_INDEX,name=product["csname"],trade_unit=100,
                    rule=dict(verified=False,scope="domestic_passive_sector",index_code=PILOT_INDEX,
                              reason="PRODUCT_PRICE_LIMIT_REQUIRES_VERIFICATION"),
                    source_batch=etf_batch)},
                weights={PILOT_INDEX:dict(complete=complete,as_of_date=weight_date.isoformat(),
                    rows=weighted_rows,completeness_basis="CSI_SET_MATCH" if complete else "unverified",
                    csi_evidence={key:value for key,value in csi.items() if key!="codes"},
                    source_batches=sorted({row[2] for row in weight_rows}))},
                stock_catalog_batch=stock_batch,
                evidence_summary=dict(index_constituents=len(weight_rows),csi_constituents=len(csi["codes"]),
                    matched_sector_constituents=len(overlap),known_exposure_ratio=str(known_exposure),
                    mainboard_weight_ratio=str(mainboard_weight),constituent_sets_match=same_set))
    mapping["version"]="pilot-"+fingerprint(mapping)[:24]
    return market,calendar,mapping


CACHE_LUA=NOTICE_LUA+"""
local before=redis.call('GET',KEYS[2])
local revision=tonumber(redis.call('GET',KEYS[3]) or '0')
if before and (before > ARGV[2] or (before == ARGV[2] and revision > tonumber(ARGV[3]))) then return 0 end
redis.call('SET',KEYS[1],ARGV[1],'EX',86400)
redis.call('SET',KEYS[2],ARGV[2],'EX',86400)
redis.call('SET',KEYS[3],ARGV[3],'EX',86400)
notice(KEYS[4],KEYS[5],ARGV[4],ARGV[5])
return 1
"""


def publish_snapshot(ledger, cache, market, *, key=None):
    now=market["observed_at"]
    with ledger._connect() as conn, conn.transaction():
        conn.execute(
            """INSERT INTO snapshot_state VALUES (%s,'market',%s,0,%s)
               ON CONFLICT(ns,dataset) DO UPDATE SET observed_at=excluded.observed_at,
                   revision=CASE WHEN snapshot_state.observed_at=excluded.observed_at
                                 THEN snapshot_state.revision+1 ELSE 0 END,
                   body=jsonb_set(excluded.body,'{revision_seq}',
                        to_jsonb(CASE WHEN snapshot_state.observed_at=excluded.observed_at
                                      THEN snapshot_state.revision+1 ELSE 0 END))
               WHERE snapshot_state.observed_at<=excluded.observed_at
                 AND snapshot_state.body->>'snapshot_id' IS DISTINCT FROM excluded.body->>'snapshot_id'""",
            (ledger.ns,now,encode({**market, "revision_seq": 0})),
        )
        saved,=conn.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset='market'",(ledger.ns,)).fetchone()
    stored=json.loads(saved)
    key=key or ledger.settings.redis_prefix+"source:market"
    snapshot_id=stored["snapshot_id"]
    keys=(key,key+":observed_at",key+":revision_seq",*notice_keys(ledger.ns,"market"))
    notice=notice_body("market","rt_k",snapshot_id,stored["observed_at"])
    cached=bool(cache.eval(CACHE_LUA,len(keys),*keys,encode(stored),stored["observed_at"],
                           stored.get("revision_seq",0),snapshot_id,notice))
    return key,cached,stored["snapshot_id"]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file",required=True)
    p.add_argument("--run-key",required=True)
    p.add_argument("--output-dir",required=True,type=Path)
    p.add_argument("--membership-day",required=True,type=date.fromisoformat)
    p.add_argument("--csi-file",type=Path)
    args=p.parse_args()
    settings=Settings.read(args.env_file)
    output=args.output_dir
    if (settings.env!="test" or not output.is_absolute() or output.resolve()!=output
            or not output.is_dir() or output.stat().st_mode & 0o077):
        p.error("this source assembly requires the isolated test database and a private output directory")
    ledger=PostgresLedger(settings,"FROZEN")
    manager=TempManager(os.environ["APP_TMP_ROOT"],persistent=output)
    if args.csi_file:
        raw=args.csi_file.read_bytes()
        if len(raw)>CSI_LIMIT:
            raise ValueError("CSI workbook exceeds size limit")
        csi=csi_evidence(raw,PILOT_INDEX.split(".")[0],CSI+"H30184cons.xls")
    else:
        raw,csi=csi_download(PILOT_INDEX.split(".")[0])
    with ledger._connect() as conn,conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        refs=load_batches(conn,args.run_key)
        market,calendar,mapping=assemble(conn,refs,csi,args.membership_day.isoformat())
    paths={
        "csi":store_file(manager,output,"CSI-"+csi["sha256"][:24]+".xls",raw),
        "market":store_file(manager,output,market["snapshot_id"]+".json",encode(market).encode()),
        "calendar":store_file(manager,output,"calendar-"+calendar["source_batch"][:24]+".json",encode(calendar).encode()),
        "mapping":store_file(manager,output,mapping["version"]+".json",encode(mapping).encode()),
    }
    with cache_client(settings) as cache:
        try:
            key,cached,active_id=publish_snapshot(ledger,cache,market)
        except RedisError:
            key,cached,active_id=None,False,market["snapshot_id"]
    sector=next(iter(mapping["sectors"].values()))
    print(json.dumps(dict(paths=paths,redis_key=key,cached=cached,active_snapshot=active_id,
        market=market["quality"],mapping=mapping["evidence_summary"],
        resolved_members=len(sector["members"]),unresolved_members=sector["unresolved_count"],
        index_weight_complete=mapping["weights"][PILOT_INDEX]["complete"]),
        ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
