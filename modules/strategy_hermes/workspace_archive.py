"""Bounded workspace reads and keyset navigation over durable evidence."""
import json
from decimal import Decimal
from urllib.parse import parse_qs

from .signal_cycle import view
from .simulator import moment
from .rotation_chain import describe


def totals(conn, ns, first=None, last=None):
    params=[ns]
    where="ns=%s"
    if first is not None:
        where+=" AND substring(body->'book'->'fills'->0->>'filled_at',1,10) BETWEEN %s AND %s"
        params.extend((first,last))
    rows=conn.execute(f"""SELECT status,
        CASE WHEN body->'rule'->>'fee_rate' IS NULL THEN 'GROSS' ELSE 'NET' END,
        count(*), count(*) FILTER(WHERE (body->'book'->>'realized')::numeric>0),
        avg((body->'book'->>'realized')::numeric) FILTER(WHERE (body->'book'->>'realized')::numeric>0),
        avg((body->'book'->>'realized')::numeric) FILTER(WHERE (body->'book'->>'realized')::numeric<0)
        FROM cycles WHERE {where} GROUP BY 1,2""",params).fetchall()
    result={basis:dict(closed=0,wins=0,win_pct=None) for basis in ("GROSS","NET")}
    result.update(open_or_pending=0,cancelled_before_entry=0,entered=0,closed=0,profit_statistics={})
    for status,basis,count,wins,gain,loss in rows:
        if status=="CLOSED":
            result[basis]=dict(closed=count,wins=wins,win_pct=str((Decimal(wins)*100/count).quantize(Decimal(".0001"))))
            result["closed"]+=count
            result["profit_statistics"][basis]=dict(average_profit=str(gain) if gain is not None else None,
                average_loss=str(loss) if loss is not None else None,payoff_ratio=str(gain/abs(loss)) if gain is not None and loss else None)
        if status in ("WAIT_BUY","OPEN","WAIT_EXIT"):
            result["open_or_pending"]+=count
        if status=="CANCELLED":
            result["cancelled_before_entry"]+=count
        if status in ("OPEN","WAIT_EXIT","CLOSED"):
            result["entered"]+=count
    return result


def snapshot(ledger, calendar):
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        record=conn.execute("SELECT watermark FROM runs WHERE ns=%s",(ledger.ns,)).fetchone()
        if not record or not record[0]:
            return {}
        now=moment(record[0])
        rows=conn.execute("""SELECT c.id,c.body,l.quote FROM cycles c LEFT JOIN latest l ON (c.ns,c.etf)=(l.ns,l.etf)
            WHERE c.ns=%s AND (c.status IN ('WAIT_BUY','OPEN','WAIT_EXIT') OR c.id IN
                (SELECT id FROM cycles WHERE ns=%s ORDER BY opened_at DESC,id DESC LIMIT 50))
            ORDER BY c.opened_at,c.id""",(ledger.ns,ledger.ns)).fetchall()
        cycles=[dict(id=rid,**view(json.loads(body),json.loads(quote) if quote else None,now)) for rid,body,quote in rows]
        rotation={}
        for sid,body in conn.execute("SELECT sector_id,body FROM rotation WHERE ns=%s",(ledger.ns,)):
            with ledger.connection() as adapter:
                state=ledger._rotation_state(adapter,ledger.ns,sid,body)
            state.pop("seen")
            if state["member"]:
                rotation[sid]=dict(state,**describe(state["phase"],state["streak"]))
        days=[day for day in calendar if day<=now.date().isoformat()][-20:]
        recent=dict(totals(conn,ledger.ns,days[0],days[-1]),start=days[0],end=days[-1],as_of=record[0]) if days else None
        return dict(watermark=record[0],cycles=cycles,rotation=rotation,evaluation=totals(conn,ledger.ns),evaluation_recent=recent)


def archive(ledger, query):
    args=parse_qs(query,max_num_fields=6)
    get=lambda key,default="":args.get(key,[default])[0]
    kind=get("kind","reports")
    definitions={"reports":("events","at","kind='report'"),"drafts":("manual_drafts","created_at","true"),"cycles":("cycles","opened_at","true")}
    if kind not in definitions:
        raise ValueError("unknown archive kind")
    table,stamp,where=definitions[kind]
    where+=" AND ns=%s"; params=[ledger.ns]
    if get("cursor"):
        at,rid=json.loads(get("cursor"))
        moment(at)
        where+=f" AND ({stamp},id)<(%s,%s)";params.extend((at,rid))
    if get("date"):
        from datetime import date
        date.fromisoformat(get("date"))
        where+=f" AND substring({stamp}::text,1,10)=%s";params.append(get("date"))
    if get("id"):
        where+=" AND id=%s";params.append(get("id"))
    if get("etf") and kind=="cycles":
        where+=" AND etf=%s";params.append(get("etf"))
    with ledger._connect() as conn:
        rows=conn.execute(f"SELECT id,{stamp} FROM {table} WHERE {where} ORDER BY {stamp} DESC,id DESC LIMIT 51",params).fetchall()
    items=[dict(id=rid,at=at.isoformat() if hasattr(at,"isoformat") else at) for rid,at in rows[:50]]
    cursor=json.dumps([items[-1]["at"],items[-1]["id"]]) if len(rows)>50 else None
    return dict(items=items,next_cursor=cursor)


def cycle_detail(ledger, identifier):
    with ledger._connect() as conn:
        row=conn.execute("""SELECT c.id,c.body,l.quote,r.watermark FROM cycles c
            JOIN runs r ON r.ns=c.ns LEFT JOIN latest l ON (c.ns,c.etf)=(l.ns,l.etf)
            WHERE c.ns=%s AND c.id=%s""",(ledger.ns,identifier)).fetchone()
    if not row:
        raise ValueError("cycle not found")
    rid,body,quote,stamp=row
    return dict(id=rid,**view(json.loads(body),json.loads(quote) if quote else None,moment(stamp)))
