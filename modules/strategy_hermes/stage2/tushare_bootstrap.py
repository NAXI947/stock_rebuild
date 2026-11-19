"""One-shot Tushare smoke collection in the isolated PostgreSQL test database."""
import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

import psycopg

from ..ledger import encode, fingerprint
from ..simulator import CHINA, CODE, decimal
from .http_json import ProviderHTTPError, post_json
from .provider_errors import ProviderFailure
from .postgres import PostgresLedger
from .settings import Settings

FIELDS = {
    "trade_cal": ("exchange", "cal_date", "is_open"),
    "stock_basic": ("ts_code", "symbol", "exchange", "market", "list_status"),
    "daily": ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount", "pct_chg"),
    "fund_daily": ("ts_code", "trade_date", "open", "high", "low", "close", "pre_close", "vol", "amount"),
    "fund_adj": ("ts_code", "trade_date", "adj_factor"),
    "etf_basic": ("ts_code", "csname", "index_code", "index_name", "list_date", "list_status", "exchange", "etf_type"),
    "index_weight": ("index_code", "con_code", "trade_date", "weight"),
    "etf_share_size": ("trade_date", "ts_code", "total_share", "total_size", "nav"),
}
INDEX_CODE = re.compile(r"[A-Z0-9]{1,32}\.(SH|SZ|CSI|CNI)\Z")
CONSTITUENT = re.compile(r"(?:[0-9]{6}\.(?:SH|SZ|BJ)|[0-9]{5}\.HK)\Z")


class SourceFailure(ProviderFailure):
    pass


class Tushare:
    def __init__(self, config):
        path=Path(config)
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Tushare config requires a private absolute file")
        try:
            self.config=json.loads(path.read_text())
        except json.JSONDecodeError:
            raise ValueError("Tushare config is invalid JSON") from None
        self.used=set()

    def fetch(self, api, params, *, allow_empty=False):
        if api in self.used:
            raise SourceFailure("endpoint budget already consumed")
        self.used.add(api)
        base=self.config
        override=(base.get("api_overrides") or {}).get(api) or {}
        token=override.get("token",base.get("token"))
        endpoint=override.get("base_url",base.get("base_url","https://api.tushare.pro"))
        uri=urlsplit(endpoint)
        if (not isinstance(token,str) or not token.strip() or uri.scheme not in ("http","https")
                or not uri.hostname or uri.username or uri.password or uri.fragment):
            raise SourceFailure("invalid credential or endpoint configuration", kind='CONFIG_UNAVAILABLE')
        timeout=min(30,int(override.get("timeout_seconds",base.get("timeout_seconds",15))))
        if timeout<=0:
            raise ValueError("invalid provider timeout")
        payload=dict(api_name=api,token=token.strip(),params=params,fields=",".join(FIELDS[api]))
        try:
            result=post_json(endpoint,payload,timeout=timeout,protocols="=http,https")
        except ProviderHTTPError as exc:
            raise SourceFailure('provider request failed', kind=exc.kind, details=exc.details) from None
        code=result.get("code")
        if type(code) is not int:
            raise SourceFailure("invalid provider status", kind='FIELD_MISMATCH')
        if code!=0:
            raise SourceFailure('provider status rejected', kind='PROVIDER_STATUS', details={'provider_code': code})
        data=result.get("data")
        if not isinstance(data,dict) or not isinstance(data.get("items"),list):
            raise SourceFailure("missing provider rows", kind='FIELD_MISMATCH')
        columns=data.get("fields")
        if (not isinstance(columns,list) or not all(isinstance(c,str) for c in columns)
                or len(set(columns))!=len(columns) or not set(FIELDS[api]).issubset(columns)):
            raise SourceFailure("invalid or incomplete provider fields", kind='FIELD_MISMATCH')
        row_limit=10000 if api=="stock_basic" else 5000
        if len(data["items"])>row_limit:
            raise SourceFailure("provider result exceeds request bound", kind='PAGE_MISMATCH',
                                details={'row_count': len(data['items']), 'page_size': row_limit})
        rows=[]
        for values in data["items"]:
            if not isinstance(values,list) or len(values)!=len(columns):
                raise SourceFailure("invalid provider row", kind='FIELD_MISMATCH')
            row=dict(zip(columns,values,strict=True))
            rows.append({field:row[field] for field in FIELDS[api]})
        if not rows and not allow_empty:
            raise SourceFailure("empty provider result")
        return rows


def calendar_rows(rows,start,end):
    result={}
    for row in rows:
        when=date.fromisoformat(row["cal_date"])
        value=row["is_open"]
        if (row["exchange"]!="SSE" or when in result or not start<=when<=end
                or str(value).lower() not in ("0","1","false","true")):
            raise SourceFailure("calendar scope, flag or identity mismatch")
        result[when]=str(value).lower() in ("1","true")
    return [dict(exchange="SSE",cal_date=day.isoformat(),is_open=is_open)
            for day,is_open in sorted(result.items())]


def etf_rows(rows,code):
    if len(rows)!=1 or rows[0]["ts_code"]!=code or rows[0]["list_status"]!="L":
        raise SourceFailure("ETF catalogue scope mismatch")
    index=rows[0]["index_code"]
    if not isinstance(index,str) or not INDEX_CODE.fullmatch(index):
        raise SourceFailure("ETF index identity unavailable")
    return rows


def weight_rows(rows,index,start,end):
    by_day={}
    for row in rows:
        when=date.fromisoformat(row["trade_date"])
        code=row["con_code"]
        weight=decimal(row["weight"])
        if (row["index_code"]!=index or not start<=when<=end or not isinstance(code,str)
                or not CONSTITUENT.fullmatch(code) or weight<0):
            raise SourceFailure("weight scope or unit mismatch")
        day=by_day.setdefault(when,{})
        if code in day:
            raise SourceFailure("duplicate constituent within weight date")
        day[code]=weight
    latest=max(by_day)
    return dict(index_code=index,weight_date=latest.isoformat(),complete=False,
                completeness_basis="unverified",rows=[dict(code=code,weight_ratio=str(weight/100))
                    for code,weight in sorted(by_day[latest].items())])


def daily_rows(rows, code, start, end):
    result={}
    for row in rows:
        day=date.fromisoformat(row["trade_date"])
        if row["ts_code"]!=code or day in result or not start<=day<=end:
            raise SourceFailure("daily bar scope or identity mismatch")
        prices={key:decimal(row[key]) for key in ("open","high","low","close","pre_close")}
        if (min(prices.values())<=0 or prices["low"]>min(prices["open"],prices["close"])
                or prices["high"]<max(prices["open"],prices["close"])):
            raise SourceFailure("invalid daily bar prices")
        volume,amount=decimal(row["vol"]),decimal(row["amount"])
        if min(volume,amount)<0:
            raise SourceFailure("invalid daily bar volume or amount")
        result[day]=dict(code=code,trade_date=day.isoformat(),
            **{key:str(value) for key,value in prices.items()},volume_shares=str(volume*100),
            amount_cny=str(amount*1000),return_ratio=str(decimal(row["pct_chg"])/100))
    return [result[day] for day in sorted(result)]


def save(store, api, params, raw, normalized):
    batch=fingerprint([api,params,raw])
    rows=normalized["rows"] if api=="index_weight" else normalized
    with store._connect() as conn, conn.transaction():
        changed=conn.execute(
            """INSERT INTO supplier_batches VALUES (%s,%s,%s,clock_timestamp(),%s,%s,%s)
               ON CONFLICT DO NOTHING RETURNING batch_id""",
            (batch,api,encode(params),len(rows),encode(raw),encode(normalized)),
        ).fetchone()
        if not changed:
            return dict(api=api,rows=len(rows),status="ALREADY_STORED",batch=batch)
        if api=="trade_cal":
            for row in rows:
                conn.execute(
                    """INSERT INTO supplier_trade_calendar VALUES (%s,%s,%s,%s)
                       ON CONFLICT(exchange,cal_date) DO UPDATE SET is_open=excluded.is_open,batch_id=excluded.batch_id""",
                    (row["exchange"],row["cal_date"],row["is_open"],batch))
        elif api=="etf_basic":
            for row in rows:
                conn.execute(
                    """INSERT INTO supplier_etf_catalog VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT(code) DO UPDATE SET index_code=excluded.index_code,list_status=excluded.list_status,
                        batch_id=excluded.batch_id,reference_data=excluded.reference_data""",
                    (row["ts_code"],row["index_code"],row["list_status"],batch,encode(row)))
        elif api=="stock_basic":
            for row in rows:
                conn.execute(
                    """INSERT INTO supplier_stock_catalog VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT(code) DO UPDATE SET symbol=excluded.symbol,exchange=excluded.exchange,
                         market=excluded.market,batch_id=excluded.batch_id""",
                    (row["ts_code"],row["symbol"],row["exchange"],row["market"],batch))
        elif api=="daily":
            for row in rows:
                conn.execute(
                    """INSERT INTO supplier_daily_bars VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(code,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,
                         low=excluded.low,close=excluded.close,pre_close=excluded.pre_close,
                         volume_shares=excluded.volume_shares,amount_cny=excluded.amount_cny,
                         return_ratio=excluded.return_ratio,batch_id=excluded.batch_id""",
                    (*[row[key] for key in ("code","trade_date","open","high","low","close","pre_close",
                                           "volume_shares","amount_cny","return_ratio")],batch))
        else:
            for row in rows:
                conn.execute(
                    """INSERT INTO supplier_index_weights VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT(index_code,weight_date,constituent_code) DO UPDATE
                       SET weight_ratio=excluded.weight_ratio,batch_id=excluded.batch_id""",
                    (normalized["index_code"],normalized["weight_date"],row["code"],row["weight_ratio"],batch))
    return dict(api=api,rows=len(rows),status="STORED",batch=batch)


def collect(store,client,etf,calendar_start,calendar_end,weight_start,weight_end):
    output=[]
    def run(api,params,normalize):
        try:
            raw=client.fetch(api,params)
            valid=normalize(raw)
            output.append(save(store,api,params,raw,valid))
            return valid
        except (SourceFailure,ValueError,KeyError,TypeError):
            reason=sys.exc_info()[1]
            output.append(dict(api=api,status="FAILED",reason=str(reason) if isinstance(reason,SourceFailure)
                               else type(reason).__name__))
            return None
    date_range=lambda a,b:dict(start_date=a.strftime("%Y%m%d"),end_date=b.strftime("%Y%m%d"))
    run("trade_cal",dict(exchange="SSE",**date_range(calendar_start,calendar_end)),
        lambda rows:calendar_rows(rows,calendar_start,calendar_end))
    catalog=run("etf_basic",dict(ts_code=etf,list_status="L"),lambda rows:etf_rows(rows,etf))
    if catalog:
        index=catalog[0]["index_code"]
        run("index_weight",dict(index_code=index,**date_range(weight_start,weight_end)),
            lambda rows:weight_rows(rows,index,weight_start,weight_end))
    else:
        output.append(dict(api="index_weight",status="WAITING_ETF_INDEX"))
    return output


def collect_eod(store, client, code, start, end):
    dates=dict(start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"))
    scopes=(("trade_cal",dict(exchange="SSE",**dates),lambda rows:calendar_rows(rows,start,end)),
            ("daily",dict(ts_code=code,**dates),lambda rows:daily_rows(rows,code,start,end)))
    output=[]
    for api,params,normalize in scopes:
        try:
            raw=client.fetch(api,params)
            output.append(save(store,api,params,raw,normalize(raw)))
        except SourceFailure as exc:
            output.append(dict(api=api,status="FAILED",reason=str(exc)))
        except (ValueError,KeyError,TypeError) as exc:
            output.append(dict(api=api,status="FAILED",reason=type(exc).__name__))
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file",required=True)
    p.add_argument("--tushare-config",required=True)
    sample=p.add_mutually_exclusive_group(required=True)
    sample.add_argument("--etf")
    sample.add_argument("--eod-stock")
    for key in ("calendar-start","calendar-end","weight-start","weight-end"):
        p.add_argument("--"+key,type=date.fromisoformat,required=key.startswith("calendar"))
    a=p.parse_args()
    try:
        settings=Settings.read(a.env_file)
        if (settings.env!="test" or not CODE.fullmatch(a.etf or a.eod_stock)
                or not 0<=(a.calendar_end-a.calendar_start).days<=45):
            raise ValueError("invalid test database, instrument or calendar scope")
        today=datetime.now(CHINA).date()
        if a.etf and (not a.weight_start or not a.weight_end
                      or not 0<=(a.weight_end-a.weight_start).days<=62 or a.weight_end>today):
            raise ValueError("invalid index weight date range")
        if a.eod_stock and a.calendar_end>=today:
            raise ValueError("EOD probe uses completed dates before today")
        client=Tushare(a.tushare_config)
        store=PostgresLedger(settings,"FROZEN")
        if a.eod_stock:
            result=collect_eod(store,client,a.eod_stock,a.calendar_start,a.calendar_end)
        else:
            result=collect(store,client,a.etf,a.calendar_start,a.calendar_end,a.weight_start,a.weight_end)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return int(any(x["status"] not in ("STORED","ALREADY_STORED") for x in result))
    except (ValueError,KeyError,TypeError,OSError,psycopg.Error) as exc:
        print(f"Tushare probe configuration/database error ({type(exc).__name__})",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
