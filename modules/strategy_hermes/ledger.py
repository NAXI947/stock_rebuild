"""Offline stage ledger: evidence, event identity, orders, fills and recovery."""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
from itertools import groupby
import json
from pathlib import Path
import re
import sqlite3
from decimal import Decimal, localcontext

from .adapter import normalize
from .rotation_chain import COUNTED_PHASES, advance, archive_phase, describe
from .signal_cycle import apply_report, create_cycle, fill, view
from .simulator import CODE, PRECISION, Calendar, Rules, decimal, integer, moment

DDL = """
CREATE TABLE IF NOT EXISTS runs(ns TEXT PRIMARY KEY, config TEXT NOT NULL, watermark TEXT);
CREATE TABLE IF NOT EXISTS rule_versions(ns TEXT NOT NULL, version TEXT NOT NULL, snapshot TEXT NOT NULL,
    PRIMARY KEY(ns,version), FOREIGN KEY(ns) REFERENCES runs);
CREATE TABLE IF NOT EXISTS events(ns TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL,
    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(ns,kind,id),
    FOREIGN KEY(ns) REFERENCES runs);
CREATE TABLE IF NOT EXISTS cycles(ns TEXT NOT NULL, id TEXT NOT NULL, etf TEXT NOT NULL,
    opened_at TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(ns,id), FOREIGN KEY(ns) REFERENCES runs);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_cycle ON cycles(ns,etf)
    WHERE status IN ('WAIT_BUY','OPEN','WAIT_EXIT');
CREATE TABLE IF NOT EXISTS rotation(ns TEXT NOT NULL, sector_id TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(ns,sector_id), FOREIGN KEY(ns) REFERENCES runs);
CREATE TABLE IF NOT EXISTS latest(ns TEXT NOT NULL, etf TEXT NOT NULL, quote TEXT NOT NULL,
    PRIMARY KEY(ns,etf), FOREIGN KEY(ns) REFERENCES runs);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":"), allow_nan=False)


def fingerprint(value):
    return sha256(encode(value).encode()).hexdigest()


def event_pair(raw):
    kind = raw["kind"]
    if kind == "report":
        item = normalize(raw)
    elif kind == "quote":
        item = dict(raw)
        qid, code, source = raw["id"], raw["etf"], raw["source"]
        observed = moment(raw["observed_at"])
        received = moment(raw.get("received_at", raw["observed_at"]))
        if "price_cny" in raw and ("price" in raw or "price_unit" in raw):
            raise ValueError("quote price unit specified twice")
        if "price_cny" in raw:
            price = decimal(raw["price_cny"])
        else:
            unit = raw.get("price_unit")
            if unit not in ("CNY","FEN"):
                raise ValueError("quote requires CNY or FEN")
            price = decimal(raw["price"]) / (100 if unit=="FEN" else 1)
        if (not isinstance(qid,str) or not qid or not CODE.fullmatch(code) or not isinstance(source,str)
                or not source.strip() or received<observed or type(raw["tradeable"]) is not bool
                or price<=0):
            raise ValueError("invalid frozen quote")
        item.update(at=received.isoformat(), observed_at=observed.isoformat(), received_at=received.isoformat(),
                    price_cny=str(price), revision=integer(raw.get("revision",0),minimum=0))
    else:
        raise ValueError("event must be report or quote")
    return item["at"], kind, item, fingerprint(raw)


class Ledger:
    def __init__(self, directory):
        directory = Path(directory)
        if not directory.is_absolute() or directory.resolve()!=directory or not directory.is_dir() or directory.is_symlink():
            raise ValueError("ledger requires a dedicated, existing absolute state directory")
        if directory.stat().st_mode & 0o077:
            raise ValueError("ledger directory must be private (0700)")
        self.path = directory / "stage1.sqlite3"
        if self.path.is_symlink() or any(self.path.with_name(self.path.name+s).is_symlink() for s in ("-shm","-wal","-journal")):
            raise ValueError("ledger path contains a symbolic link")
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(DDL)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA temp_store=MEMORY")
            yield db
        finally:
            db.close()

    def replay(self, bundle):
        ns = bundle["namespace"]
        if not isinstance(ns,str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}",ns):
            raise ValueError("invalid replay namespace")
        if bundle.get("schema")!="stock-v4-frozen-replay-v1" or bundle.get("input_kind")!="FROZEN":
            raise ValueError("Stage 1 accepts only explicitly frozen bundles")
        units = bundle["instruments"]
        if not isinstance(units,dict) or not units or any(not CODE.fullmatch(c) for c in units):
            raise ValueError("invalid instrument registry")
        units={c:integer(n) for c,n in units.items()}
        calendar=Calendar(bundle["calendar"])
        run_config=encode(dict(instruments=units,calendar=bundle["calendar"]))
        rules=Rules(**bundle.get("rules",{}))
        frozen=encode(asdict(rules))
        events=sorted((event_pair(ev) for ev in bundle["events"]),key=lambda e:(e[0],e[1],e[2]["id"]))
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO runs(ns,config) VALUES (?,?)",(ns,run_config))
            config,watermark=db.execute("SELECT config,watermark FROM runs WHERE ns=?",(ns,)).fetchone()
            if config!=run_config:
                raise ValueError("calendar or instrument registry changed; use a new research namespace")
            db.execute("INSERT OR IGNORE INTO rule_versions VALUES (?,?,?)",(ns,rules.version,frozen))
            if db.execute("SELECT snapshot FROM rule_versions WHERE ns=? AND version=?",(ns,rules.version)).fetchone()[0]!=frozen:
                raise ValueError("rule parameters changed without a new version")
            for at,group in groupby(events,key=lambda e:e[0]):
                fresh=[]
                for _,kind,ev,digest in group:
                    past=db.execute("SELECT fingerprint FROM events WHERE ns=? AND kind=? AND id=?",(ns,kind,ev["id"])).fetchone()
                    if past:
                        if past[0]!=digest:
                            raise ValueError(f"event ID collision: {kind}:{ev['id']}")
                        continue
                    if watermark and at<=watermark:
                        raise ValueError("late event batch; use a new frozen replay namespace")
                    db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",(ns,kind,ev["id"],at,digest,encode(ev)))
                    fresh.append((kind,ev))
                if fresh:
                    self._apply(db,ns,fresh,calendar,units,rules)
                    for kind,ev in fresh:
                        if kind=="report":
                            db.execute("UPDATE events SET payload=? WHERE ns=? AND kind='report' AND id=?",(encode(ev),ns,ev["id"]))
                    db.execute("UPDATE runs SET watermark=? WHERE ns=?",(at,ns))
                    watermark=at
            db.commit()
        return self.inspect(ns)

    @staticmethod
    def _save(db,ns,cid,cycle,opened):
        db.execute("""INSERT INTO cycles VALUES (?,?,?,?,?,?)
            ON CONFLICT(ns,id) DO UPDATE SET status=excluded.status,body=excluded.body""",
            (ns,cid,cycle["etf"],opened,cycle["status"],encode(cycle)))

    def _apply(self,db,ns,events,calendar,units,rules):
        rows = db.execute("SELECT etf,id,body,opened_at FROM cycles WHERE ns=? AND status IN ('WAIT_BUY','OPEN','WAIT_EXIT')",(ns,))
        active = {code:(cid,json.loads(body),opened) for code,cid,body,opened in rows}
        reports=[v for k,v in events if k=="report"]
        self._advance_rotation(db,ns,reports)
        blocked=set(active)
        for code,(_,cycle,_) in active.items():
            # Use the original primary sector. Full retreat outranks a simultaneous weakening.
            present=[r for r in reports if cycle["primary_sector"] in r["sectors"]]
            choices=present or reports
            if choices:
                priority={"EXIT":0,"WEAK":1,"HOLD":2,"UP":3,"UNKNOWN":4}
                chosen=min(choices,key=lambda r:(priority[r["sectors"].get(cycle["primary_sector"],{}).get("state","UNKNOWN")],r["id"]))
                apply_report(cycle,chosen)
        if reports:
            new={}
            for report in reports:
                for code,choices in report["candidates"].items():
                    if code in units and code not in blocked:
                        new.setdefault(code,[]).extend((report,c) for c in choices if report["sectors"][c["sector_id"]]["state"]=="UP")
            for code,pairs in new.items():
                eligible=[(r,c) for r,c in pairs if not any(other["sectors"].get(c["sector_id"],{}).get("state") in ("WEAK","EXIT") for other in reports)]
                if not eligible:
                    continue
                anchor = getattr(db, "etf_anchor", {})
                report_ids = {r["id"] for r, _ in eligible}
                facts = anchor.get(next(iter(report_ids)), {}) if len(report_ids) == 1 and len(eligible) > 1 else {}
                report, choice=min(eligible,key=lambda p:(
                    facts.get(p[1]["sector_id"], {}).get(code, {}).get("exposure_tier") != "CORE_REPRESENTATIVE",
                    p[1]["rank"],p[0]["id"],p[1]["sector_id"],p[1]["mapping_ref"]))
                cid=fingerprint([ns,code,report["id"]])
                sources=[dict(c,report_id=r["id"]) for r,c in eligible]
                active[code]=(cid,create_cycle(code,units[code],rules,report,choice,sources),report["at"])
        quotes=[v for k,v in events if k=="quote" and v["etf"] in units]
        # For simultaneous arrivals, prefer the newest observed quote and highest revision.
        for code,items in groupby(sorted(quotes,key=lambda q:q["etf"]),key=lambda q:q["etf"]):
            quote=max(items,key=lambda q:(q["observed_at"],q["revision"],q["id"]))
            if not calendar.legal(moment(quote["observed_at"])):
                continue
            old=db.execute("SELECT quote FROM latest WHERE ns=? AND etf=?",(ns,code)).fetchone()
            if old:
                prior=json.loads(old[0])
                if (quote["observed_at"],quote["revision"]) <= (prior["observed_at"],prior["revision"]):
                    continue
            db.execute("""INSERT INTO latest VALUES (?,?,?)
                ON CONFLICT(ns,etf) DO UPDATE SET quote=excluded.quote""",(ns,code,encode(quote)))
            if code in active:
                fill(active[code][1],quote,calendar)
        for cid,cycle,opened in active.values():
            self._save(db,ns,cid,cycle,opened)

    @staticmethod
    def _rotation_state(db, ns, sid, saved):
        state = json.loads(saved)
        phase = state["phase"]
        if phase == "STRENGTHEN_X2":
            return dict(state, phase="STRENGTHEN")
        if phase not in COUNTED_PHASES or state["streak"] > 0:
            return state

        # The initial ledger only counted strengthening. Recover the other
        # streaks from committed report rows when reading an older chain.
        sector_path = '$.sectors."' + sid + '"'
        rows = db.execute(
            """SELECT json_extract(payload, ?) FROM events
               WHERE ns=? AND kind='report'
                 AND at <= (SELECT at FROM events WHERE ns=? AND kind='report' AND id=?)
                 AND json_extract(payload, ?) IS NOT NULL
               ORDER BY at DESC,id DESC""",
            (sector_path, ns, ns, state["report_id"], sector_path),
        )
        streak = 0
        for (payload,) in rows:
            observed = archive_phase(json.loads(payload))
            if observed is None:
                continue
            if observed != phase:
                break
            streak += 1
        return dict(state, streak=max(1, streak))

    @staticmethod
    def _advance_rotation(db,ns,reports):
        priority={"RETREAT":0,"WEAKEN":1,"MAINTAIN":2,"NEW":3,"STRENGTHEN":4,"UNKNOWN":5}
        sectors=sorted({sid for r in reports for sid in r["sectors"]})
        for sid in sectors:
            candidates=sorted((r for r in reports if sid in r["sectors"]),
                              key=lambda r:(priority[r["sectors"][sid]["phase"]],r["id"]))
            winner=candidates[0]
            stored=db.execute("SELECT body FROM rotation WHERE ns=? AND sector_id=?",(ns,sid)).fetchone()
            previous=Ledger._rotation_state(db, ns, sid, stored[0]) if stored else None
            updated,effective=advance(previous,winner,sid,winner["sectors"][sid])
            for r in candidates:
                row=r["sectors"][sid]
                r["sectors"][sid]=dict(effective if r is winner else dict(row, state="UNKNOWN",phase="UNKNOWN",
                    reason="COINCIDENT_REPORT_SUPERSEDED"), declared_phase=row["phase"],declared_state=row["state"])
            if updated is not None:
                db.execute("""INSERT INTO rotation VALUES (?,?,?)
                    ON CONFLICT(ns,sector_id) DO UPDATE SET body=excluded.body""",(ns,sid,encode(updated)))

    @staticmethod
    def _totals(cycles):
        with localcontext(PRECISION):
            totals={"profit_statistics":{}}
            for basis in ("GROSS","NET"):
                profits=[decimal(c["valuation"]["profit"]) for c in cycles
                         if c["status"]=="CLOSED" and c["valuation"]["basis"]==basis
                         and c["valuation"]["profit"] is not None]
                wins=sum(x>0 for x in profits)
                gains=[x for x in profits if x>0]
                losses=[x for x in profits if x<0]
                average_gain=sum(gains)/len(gains) if gains else None
                average_loss=sum(losses)/len(losses) if losses else None
                totals[basis]={"closed":len(profits),"wins":wins,"win_pct":str(
                    (Decimal(wins)*100/len(profits)).quantize(Decimal("0.0001"))) if profits else None}
                totals["profit_statistics"][basis]=dict(average_profit=str(average_gain) if average_gain is not None else None,
                    average_loss=str(average_loss) if average_loss is not None else None,
                    payoff_ratio=str(average_gain/abs(average_loss)) if average_gain is not None and average_loss else None)
            totals["open_or_pending"]=sum(c["status"] in ("WAIT_BUY","OPEN","WAIT_EXIT") for c in cycles)
            totals["cancelled_before_entry"]=sum(c["status"]=="CANCELLED" for c in cycles)
            return totals

    def inspect(self,ns):
        with self.connection() as db:
            db.execute("BEGIN")
            record=db.execute("SELECT watermark FROM runs WHERE ns=?",(ns,)).fetchone()
            if not record:
                raise ValueError("unknown replay namespace")
            latest={code:json.loads(q) for code,q in db.execute("SELECT etf,quote FROM latest WHERE ns=?",(ns,))}
            cycles=[dict(id=cid,**view(json.loads(body),latest.get(code),moment(record[0])))
                    for cid,code,body in db.execute("SELECT id,etf,body FROM cycles WHERE ns=? ORDER BY opened_at,id",(ns,))]
            rotation={}
            for sid,body in db.execute("SELECT sector_id,body FROM rotation WHERE ns=? ORDER BY sector_id",(ns,)):
                state=self._rotation_state(db, ns, sid, body)
                state.pop("seen")
                rotation[sid]=dict(state, **describe(state["phase"], state["streak"]))
            counts=dict(db.execute("SELECT kind,count(*) FROM events WHERE ns=? GROUP BY kind",(ns,)))
        return dict(namespace=ns,watermark=record[0],cycles=cycles,
                    rotation={k:v for k,v in rotation.items() if v["member"]},
                    retired_directions={k:v for k,v in rotation.items() if not v["member"]},
                    evaluation=self._totals(cycles),evidence_counts=counts,input_kind="FROZEN")

    def report(self,ns,rid):
        with self.connection() as db:
            row=db.execute("SELECT payload FROM events WHERE ns=? AND kind='report' AND id=?",(ns,rid)).fetchone()
        if not row:
            raise ValueError("unknown archived report")
        return json.loads(row[0])
