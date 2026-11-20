"""Small live quote and board probes, recorded in the isolated test database."""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

import psycopg

from ..ledger import encode, fingerprint
from ..simulator import CHINA, CODE, decimal
from .http_json import ProviderHTTPError, post_json, get_json
from .provider_errors import ProviderFailure
from .inputs import document
from .postgres import PostgresLedger
from .settings import Settings

TICKFLOW_URL = "https://api.tickflow.org/v1/quotes"
CAPTURE_HOSTS = {
    "072": "apphwshhq.longhuvip.com",
    "016": "apphwshhq.longhuvip.com",
    "018": "apphwshhq.longhuvip.com",
    "041": "apphq.longhuvip.com",
    "046": "apphis.longhuvip.com",
    "107": "apphis.longhuvip.com",
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ProbeError(ProviderFailure):
    pass


def fetch(req, *, timeout=10):
    """Use the shared one-request transport, with no redirects or hidden retry."""
    try:
        if req.get_method() == 'POST':
            return post_json(req.full_url, document(req.data), headers=tuple(req.header_items()), timeout=timeout)
        if req.get_method() != 'GET' or req.data is not None:
            raise ProbeError('unsupported provider request method', kind='CONFIG_UNAVAILABLE')
        return get_json(req.full_url, headers=tuple(req.header_items()), timeout=timeout)
    except ProviderHTTPError as exc:
        raise ProbeError('provider request failed', kind=exc.kind, details=exc.details) from None


def instant(value, received, *, milliseconds):
    if type(value) is not int or value<=0:
        raise ProbeError("source timestamp unavailable", kind='INVALID_TIMESTAMP')
    try:
        delta=timedelta(milliseconds=value) if milliseconds else timedelta(seconds=value)
        when=(EPOCH+delta).astimezone(CHINA)
    except (ValueError,OverflowError):
        raise ProbeError("invalid source timestamp", kind='INVALID_TIMESTAMP') from None
    if when>received+timedelta(seconds=10):
        raise ProbeError("source timestamp is in the future", kind='INVALID_TIMESTAMP')
    return when.isoformat()


def tickflow_config(path):
    p=Path(path)
    if not p.is_absolute() or p.is_symlink() or p.stat().st_mode & 0o077:
        raise ProbeError("TickFlow configuration must be private and absolute")
    key=document(p.read_bytes())["api_key"]
    if not isinstance(key,str) or not key.strip() or any(ch in key for ch in "\r\n"):
        raise ProbeError("invalid TickFlow configuration")
    return key.strip()


def optional_positive(value, *, zero=False):
    if value is None:
        return None
    number=decimal(value)
    if number<0 or (not zero and number==0):
        raise ProbeError("invalid price, amount or volume")
    return number


def normalize_tickflow(body, requested, received, *, stock_universe=False):
    rows=body.get("data")
    if not isinstance(rows,list) or not rows:
        raise ProbeError("TickFlow returned no quote rows")
    accepted,issues={},{}
    seen=set()
    for row in rows:
        code=row.get("symbol") if isinstance(row,dict) else None
        valid_code = re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", code) if isinstance(code, str) and stock_universe else isinstance(code, str) and CODE.fullmatch(code)
        if not valid_code or code in seen:
            raise ProbeError("TickFlow quote scope or identity mismatch")
        if requested is not None and code not in requested:
            raise ProbeError("TickFlow quote outside requested symbols")
        seen.add(code)
        try:
            observed=instant(row["timestamp"],received,milliseconds=True)
            price=optional_positive(row.get("last_price"))
            if price is None:
                raise ProbeError("missing last price")
            prev=optional_positive(row.get("prev_close"))
            vol=optional_positive(row.get("volume"),zero=True)
            amount=optional_positive(row.get("amount"),zero=True)
        except (ProbeError,ValueError,KeyError,TypeError):
            issues[code]="PRICE_OR_TIME_UNVERIFIED"
            continue
        accepted[code]=dict(code=code,observed_at=observed,price_cny=str(price),
            pre_close_cny=str(prev) if prev is not None else None,
            volume_shares=str(vol*100) if vol is not None else None,
            amount_cny=str(amount) if amount is not None else None,
            volume_unit_verified=vol is not None, amount_unit_verified=amount is not None)
        for source, target in (("high", "high_cny"), ("low", "low_cny")):
            try:
                value = optional_positive(row.get(source))
                if value is not None:
                    accepted[code][target] = str(value)
            except (ProbeError, ValueError, TypeError):
                issues[code] = "OPTIONAL_RANGE_UNVERIFIED"
        if isinstance(row.get("trading_status"), str):
            accepted[code]["trading_status"] = row["trading_status"].upper()
    for code in sorted(set(requested or ())-accepted.keys()):
        issues.setdefault(code,"QUOTE_MISSING")
    if not accepted:
        raise ProbeError("no validated TickFlow quotes")
    return list(accepted.values()),issues


def normalize_tickflow_depth(body, requested, received):
    """Official /v1/depth/batch shape; absent/zero sides remain unavailable.

    Contract: https://docs.tickflow.org/zh-Hans/api-reference/openapi.json
    A separate source timestamp is retained so callers can align it with quotes.
    """
    rows = body.get("data")
    if not isinstance(rows, dict):
        raise ProbeError("invalid TickFlow depth response")
    expected = set(requested)
    accepted, issues = [], {}
    for code, row in rows.items():
        if code not in expected or not isinstance(row, dict) or row.get("symbol") != code:
            raise ProbeError("TickFlow depth identity mismatch")
        try:
            observed = instant(row["timestamp"], received, milliseconds=True)
            result = dict(code=code, book_observed_at=observed)
            for key, target in (("bid_prices", "bid1_cny"), ("ask_prices", "ask1_cny")):
                prices = row.get(key)
                if not isinstance(prices, list) or not prices:
                    continue
                value = optional_positive(prices[0], zero=True)
                if value is not None and value > 0:
                    result[target] = str(value)
            if ("bid1_cny" in result and "ask1_cny" in result
                    and decimal(result["ask1_cny"]) < decimal(result["bid1_cny"])):
                raise ProbeError("crossed TickFlow order book")
            if not any(key in result for key in ("bid1_cny", "ask1_cny")):
                issues[code] = "BOOK_UNAVAILABLE"
                continue
            if not all(key in result for key in ("bid1_cny", "ask1_cny")):
                issues[code] = "BOOK_ONE_SIDE_UNAVAILABLE"
            accepted.append(result)
        except (ProbeError, ValueError, TypeError, KeyError):
            issues[code] = "BOOK_UNVERIFIED"
    for code in expected - rows.keys():
        issues[code] = "BOOK_UNAVAILABLE"
    return accepted, issues


def depth_batch(store, config, codes):
    """One optional, bounded request; caller owns the shared round budget."""
    codes = sorted(set(codes))
    if not codes or len(codes) > 100 or any(not isinstance(code, str) or not CODE.fullmatch(code) for code in codes):
        raise ValueError("depth scope must contain 1-100 ETF codes")
    # The live batch endpoint accepts literal comma delimiters; %2C resets the connection.
    url = TICKFLOW_URL.removesuffix("/quotes") + "/depth/batch?" + urlencode({"symbols": ",".join(codes)}, safe=",")
    request = Request(url, headers={"Accept": "application/json", "x-api-key": tickflow_config(config),
                                   "User-Agent": "stock-v4-tickflow-client/1.0"})
    raw = fetch(request, timeout=15)
    received = datetime.now(CHINA)
    books, issues = normalize_tickflow_depth(raw, codes, received)
    source = max((row["book_observed_at"] for row in books), default=None)
    result = save(store, "tickflow", "depth", {"symbols": codes},
                  dict(books=books, issues=issues), len(books), source, received)
    return dict(result, books=books, issues=issues)


def capture_request(root, endpoint, overrides=None):
    files=list(root.glob(endpoint+"_*.json"))
    if len(files)!=1:
        raise ProbeError("expected one archived request per endpoint", kind='CONFIG_UNAVAILABLE')
    capture=json.loads(files[0].read_text(encoding="utf-8-sig"))
    uri=capture["专属接口链接"].strip()
    parsed=urlsplit(uri)
    if (parsed.scheme!="https" or parsed.hostname!=CAPTURE_HOSTS[endpoint]
            or parsed.path!="/w1/api/index.php" or parsed.username or parsed.password
            or parsed.port not in (None,443) or parsed.fragment):
        raise ProbeError("archived URL does not match its captured provider origin", kind='CONFIG_UNAVAILABLE')
    args=dict(parse_qsl(parsed.query,keep_blank_values=True))
    if overrides:
        allowed={"041":{"Index"},"046":{"Date","PlateID","Index"},"107":{"StockID"}}
        if not set(overrides).issubset(allowed.get(endpoint,set())):
            raise ProbeError("unsupported captured parameter override")
        args.update(overrides)
        uri=urlunsplit(parsed._replace(query=urlencode(args)))
    allowed=("Date","PlateID","Index","st","Type","Order","RStart","REnd","StockID")
    return Request(uri,headers={"Accept":"application/json"}),{k:args[k] for k in allowed if k in args}


def normalize_kaipanla(body, endpoint, scope, received):
    if not isinstance(body, dict):
        raise ProbeError('invalid provider document', kind='FIELD_MISMATCH')
    if endpoint == "072":
        from .macro_refresh import normalize_live
        result=normalize_live(body,received.isoformat())
        return result,len(result["items"]),None
    if endpoint == "107":
        from .market_regime import normalize_index
        try:
            rows=normalize_index(body,scope.get("StockID"))
        except (ValueError,TypeError,ArithmeticError):
            raise ProbeError("provider returned invalid index bars", kind='FIELD_MISMATCH') from None
        return dict(rows=rows,code=scope["StockID"]),len(rows),None
    code=body.get("errcode")
    if str(code)!="0":
        status=str(code)
        if not re.fullmatch(r"-?[0-9]{1,12}",status):
            status="unrecognized"
        raise ProbeError('provider status rejected', kind='PROVIDER_STATUS', details={'provider_code': status})
    key="info" if endpoint=="016" else "list"
    rows=body.get(key)
    expected=dict if endpoint=="018" else list
    if not isinstance(rows,expected):
        raise ProbeError("provider returned invalid board rows", kind='FIELD_MISMATCH')
    if not rows:
        raise ProbeError("provider returned no usable board rows", kind='NOT_PUBLISHED')
    if endpoint in ("041","046"):
        seen=set()
        for row in rows:
            if (not isinstance(row,list) or len(row)<2
                    or not re.fullmatch(r"[0-9]{6}",str(row[0])) or str(row[0]) in seen):
                raise ProbeError("invalid or duplicate board/constituent identity", kind='INVALID_IDENTITY')
            seen.add(str(row[0]))
    data=dict(rows=rows)
    if "Day" in body:
        data["trade_days"]=body["Day"]
    if endpoint=="046" and data.get("trade_days") != [scope.get("Date")]:
        raise ProbeError("constituent response day does not match request", kind='DATE_MISMATCH',
                         details={'expected_date': scope.get('Date'), 'returned_dates': data.get('trade_days')})
    if "Count" in body and type(body["Count"]) is int:
        data["reported_total"]=body["Count"]
    source=instant(body["Time"],received,milliseconds=False) if "Time" in body else None
    return data, (1 if endpoint=="018" else len(rows)), source


def save(store, provider, endpoint, scope, body, count, source, received, *, quotes=()):
    batch=fingerprint([provider,endpoint,scope,body,source])
    with store._connect() as conn, conn.transaction():
        inserted=conn.execute(
            """INSERT INTO supplier_market_batches VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING RETURNING batch_id""",
            (batch,provider,endpoint,encode(scope),received,source,count,encode(body)),
        ).fetchone()
        for row in quotes:
            conn.execute(
                """INSERT INTO supplier_etf_quote_observations VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(provider,code,observed_at) DO NOTHING""",
                (provider,row["code"],row["observed_at"],row["price_cny"],row["pre_close_cny"],
                 row["volume_shares"],row["amount_cny"],batch),
            )
    return dict(provider=provider,endpoint=endpoint,scope=scope,
                status="STORED" if inserted else "ALREADY_STORED",rows=count,
                source_clock=source,batch=batch)


def probe_tickflow(store, config, symbols):
    key=tickflow_config(config)
    try:
        body=post_json(TICKFLOW_URL,{"symbols":symbols},headers=(("x-api-key",key),))
    except ProviderHTTPError as exc:
        raise ProbeError(str(exc)) from None
    now=datetime.now(CHINA)
    rows,issues=normalize_tickflow(body,symbols,now)
    source=max(row["observed_at"] for row in rows)
    return save(store,"tickflow","quotes",{"symbols":symbols},dict(quotes=rows,issues=issues),
                len(rows),source,now,quotes=rows) | {"issues":issues}


def probe_kaipanla(store, root, endpoint, overrides=None):
    request,scope=capture_request(root,endpoint,overrides)
    body=fetch(request)
    now=datetime.now(CHINA)
    payload,count,source=normalize_kaipanla(body,endpoint,scope,now)
    result=save(store,"kaipanla",endpoint,scope,payload,count,source,now)
    for key in ("trade_days","reported_total"):
        if key in payload:
            result[key]=payload[key]
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file",required=True)
    sub=p.add_subparsers(dest="provider",required=True)
    tick=sub.add_parser("tickflow")
    tick.add_argument("--config",required=True)
    tick.add_argument("--symbol",action="append",required=True)
    boards=sub.add_parser("kaipanla")
    boards.add_argument("--captures",type=Path,required=True)
    boards.add_argument("--endpoint",choices=sorted(CAPTURE_HOSTS),action="append",required=True)
    boards.add_argument("--trade-date",type=date.fromisoformat)
    boards.add_argument("--plate-id")
    boards.add_argument("--offset",type=int)
    args=p.parse_args()
    try:
        settings=Settings.read(args.env_file)
        if settings.env!="test":
            raise ValueError("market probes use the isolated test database")
        store=PostgresLedger(settings,"FROZEN")
        if args.provider=="tickflow":
            if (not 0<len(args.symbol)<=10 or len(set(args.symbol))!=len(args.symbol)
                    or any(not CODE.fullmatch(code) for code in args.symbol)):
                raise ValueError("invalid or duplicate ETF symbols")
            probes=[("quotes",lambda:probe_tickflow(store,args.config,args.symbol))]
        else:
            overrides={}
            if args.trade_date or args.plate_id:
                if ("046" not in args.endpoint or not args.trade_date or not args.plate_id
                        or not re.fullmatch(r"[0-9]{6}",args.plate_id)
                        or args.trade_date>datetime.now(CHINA).date()):
                    raise ValueError("expected a valid plate ID and source date for endpoint 046")
                overrides.update(Date=args.trade_date.isoformat(),PlateID=args.plate_id)
            if args.offset is not None:
                if (len(args.endpoint)!=1 or args.endpoint[0] not in ("041","046")
                        or not 0<=args.offset<=10000):
                    raise ValueError("offset requires a single paginated endpoint and a bounded value")
                overrides["Index"]=str(args.offset)
            by_endpoint={"046":overrides,"041":{key:value for key,value in overrides.items() if key=="Index"}}
            probes=[(name,lambda name=name:probe_kaipanla(
                        store,args.captures,name,by_endpoint.get(name)))
                    for name in dict.fromkeys(args.endpoint)]
        results=[]
        for name,probe in probes:
            try:
                results.append(probe())
            except ProbeError as exc:
                results.append(dict(provider=args.provider,endpoint=name,status="FAILED",reason=str(exc)))
            except (ValueError,KeyError,TypeError,OSError,psycopg.Error) as exc:
                results.append(dict(provider=args.provider,endpoint=name,status="FAILED",reason=type(exc).__name__))
        print(json.dumps(results,ensure_ascii=False,indent=2))
        return int(any(result["status"]=="FAILED" for result in results))
    except (ValueError,KeyError,TypeError,OSError) as exc:
        p.exit(1,f"probe configuration error ({type(exc).__name__})\n")


if __name__=="__main__":
    raise SystemExit(main())
