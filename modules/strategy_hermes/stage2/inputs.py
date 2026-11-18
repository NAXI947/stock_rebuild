"""Read shared state and versioned reference files, with no supplier calls."""
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re

from redis import Redis
from redis.exceptions import RedisError

from ..ledger import encode
from ..simulator import CODE, Calendar, decimal, moment, session

MAX_INPUT = 32 * 1024 * 1024
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
CLOSED_STATUS = frozenset(("SUSPENDED", "CLOSED", "AUCTION", "BREAK", "PREOPEN"))


def document(data):
    if not data or len(data)>MAX_INPUT:
        raise ValueError("input document missing or exceeds size limit")
    value=json.loads(data,parse_float=str)
    if not isinstance(value,dict):
        raise ValueError("expected a JSON object")
    return value


def read_file(path):
    with Path(path).open("rb") as stream:
        return document(stream.read(MAX_INPUT+1))


def trading_days(path):
    data=read_file(path)
    if (data.get("schema_version")!="hotspot_trade_calendar_v1"
            or data.get("source")!="quant.trade_calendar" or data.get("exchange")!="SSE"):
        raise ValueError("unsupported shared trade calendar")
    first, last = date.fromisoformat(data["range_start"]), date.fromisoformat(data["range_end"])
    for day, is_open in data["days"].items():
        if not first<=date.fromisoformat(day)<=last or type(is_open) is not bool:
            raise ValueError("invalid calendar day")
    days=sorted(day for day,is_open in data["days"].items() if is_open)
    Calendar(days)
    return days


def mapping_file(path):
    data=read_file(path)
    if (data.get("schema_version")!="stock-v4-etf-mapping-v1" or
            not isinstance(data.get("version"),str) or not VERSION.fullmatch(data["version"])):
        raise ValueError("unsupported or unversioned ETF mapping")
    for key in ("sectors","products","weights"):
        if not isinstance(data.get(key),dict) or not data[key]:
            raise ValueError("missing mapping section " + key)
    return data


def cache_client(settings):
    return Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=3,
                          retry_on_timeout=False)


@dataclass(frozen=True)
class SharedMarket:
    body: dict
    observed_at: str
    revision: int
    snapshot_id: str

    @classmethod
    def read(cls, body):
        protocol=body.get("schema_version")
        if protocol not in ("market_read_v1","hotspot_market_snapshot_v1"):
            raise ValueError("unsupported shared market protocol")
        raw_version=body.get("snapshot_id",body.get("revision"))
        version=str(raw_version)
        if not isinstance(raw_version,(str,int)) or isinstance(raw_version,bool) or not VERSION.fullmatch(version):
            raise ValueError("invalid shared snapshot ID")
        observed=moment(body["observed_at"])
        if observed.date().isoformat()!=body.get("trade_date") or not isinstance(body.get("quotes"),dict):
            raise ValueError("invalid shared snapshot date or quotes")
        revision=body.get("revision_seq",0)
        if type(revision) is not int or revision<0:
            raise ValueError("snapshot revision sequence must be a nonnegative integer")
        return cls(body,observed.isoformat(),revision,version)

    def quote_events(self, instruments, received):
        events, issues=[],{}
        for code in instruments:
            row=self.body["quotes"].get(code)
            if row is None:
                continue
            try:
                if not CODE.fullmatch(code) or not isinstance(row,dict):
                    raise ValueError("invalid ETF quote")
                observed=moment(row["observed_at"])
                if (observed>received or observed.date().isoformat()!=self.body["trade_date"]
                        or row.get("trade_date",self.body["trade_date"])!=self.body["trade_date"]):
                    raise ValueError("ETF observation outside snapshot day")
                price=decimal(row.get("price_cny",row.get("last")))
                if price<=0:
                    raise ValueError("price must be positive")
                source=row.get("source",self.body.get("source"))
                if not isinstance(source,str) or not source.strip():
                    raise ValueError("quote source missing")
                revision=row.get("revision_seq",0)
                if type(revision) is not int or revision<0:
                    raise ValueError("invalid quote revision")
                tradeable=(self.body.get("mode")!="EOD" and session(observed)
                           and row.get("trading_status") not in CLOSED_STATUS
                           and row.get("tradeable") is not False)
                ident=sha256(encode([code,source,observed.isoformat(),revision]).encode()).hexdigest()
                events.append(dict(kind="quote",id=ident,etf=code,price_cny=str(price),
                    observed_at=observed.isoformat(),received_at=received.isoformat(),source=source,
                    tradeable=tradeable,revision=revision))
            except (ValueError,KeyError,TypeError):
                issues[code]="QUOTE_INVALID_OR_UNVERIFIED"
        return events, issues


def shared_market(ledger, cache, *, key=None, path=None):
    """Save the newest source snapshot as the PostgreSQL read fallback."""
    if (key is None)==(path is None):
        raise ValueError("configure exactly one Redis snapshot key or local file")
    fresh=None
    if key:
        if not key.startswith(ledger.settings.redis_prefix):
            raise ValueError("snapshot key outside the configured Redis environment")
        try:
            data=cache.get(key)
        except RedisError:
            data=None
        if data is not None:
            fresh=SharedMarket.read(document(data))
    else:
        try:
            fresh=SharedMarket.read(read_file(path))
        except FileNotFoundError:
            pass
    with ledger._connect() as connection, connection.transaction():
        if fresh:
            connection.execute(
                """INSERT INTO snapshot_state VALUES (%s,'market',%s,%s,%s)
                   ON CONFLICT (ns,dataset) DO UPDATE SET observed_at=excluded.observed_at,
                        revision=excluded.revision,body=excluded.body
                   WHERE (snapshot_state.observed_at,snapshot_state.revision)
                        <(excluded.observed_at,excluded.revision)""",
                (ledger.ns,fresh.observed_at,fresh.revision,encode(fresh.body)),
            )
        row=connection.execute("SELECT body FROM snapshot_state WHERE ns=%s AND dataset='market'",
                               (ledger.ns,)).fetchone()
    return SharedMarket.read(document(row[0])) if row else None
