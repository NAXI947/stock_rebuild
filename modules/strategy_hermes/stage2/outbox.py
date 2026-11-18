"""Database-backed delivery, with an independent lease and retry budget."""
from dataclasses import dataclass
import json
from random import uniform
from uuid import uuid4

from ..ledger import encode
from .market_notices import NOTICE_LUA, notice_body, notice_keys


class DeliveryPermanent(RuntimeError):
    def __init__(self, message, *, receipt=None):
        super().__init__(message)
        self.receipt = receipt


class DeliveryUncertain(RuntimeError):
    def __init__(self, message, *, receipt=None):
        super().__init__(message)
        self.receipt = receipt


@dataclass(frozen=True)
class Delivery:
    id: int
    target: str
    event_key: str
    payload: dict
    token: str
    attempt: int


def claim(ledger, target, *, max_attempts=3, lease_seconds=45):
    token=str(uuid4())
    # An expired QQ lease can conceal an accepted proactive message.
    if target == "octop":
        with ledger._connect() as conn:
            conn.execute(
                """UPDATE outbox SET state='FAILED',error_code='DeliveryUncertain',
                     lease_token=NULL,lease_until=NULL
                   WHERE ns=%s AND target=%s AND state='SENDING'
                     AND lease_until<=clock_timestamp()
                     AND (payload ? 'text' OR coalesce(payload->>'format','') NOT IN ('octop-markdown-v1','qq-template-v2'))""",
                (ledger.ns,target))
    with ledger._connect() as conn, conn.transaction():
        conn.execute(
            """UPDATE outbox SET state='FAILED',error_code='DELIVERY_LEASE_EXPIRED'
               WHERE ns=%s AND target=%s AND state='SENDING'
                 AND lease_until<=clock_timestamp() AND attempts >= %s""",
            (ledger.ns,target,max_attempts),
        )
        row=conn.execute(
            """WITH due AS (
                   SELECT id FROM outbox WHERE ns=%s AND target=%s AND attempts<%s
                     AND ((state='PENDING' AND next_attempt<=clock_timestamp()) OR
                          (state='SENDING' AND lease_until<=clock_timestamp()
                           AND (target<>'octop' OR (payload->>'format' IN ('octop-markdown-v1','qq-template-v2') AND NOT (payload ? 'text')))))
                   ORDER BY next_attempt,id FOR UPDATE SKIP LOCKED LIMIT 1
               )
               UPDATE outbox SET state='SENDING',attempts=attempts+1,lease_token=%s,
                    lease_until=clock_timestamp()+%s*interval '1 second'
               FROM due WHERE outbox.id=due.id
               RETURNING outbox.id,target,event_key,payload,attempts""",
            (ledger.ns,target,max_attempts,token,lease_seconds),
        ).fetchone()
    if not row:
        return None
    id,target,key,payload,attempt=row
    return Delivery(id,target,key,json.loads(payload),token,attempt)


def save_qq_text(ledger, event, text, template, *, fallback=False):
    """Freeze the rendered body under the current send lease before the first QQ POST."""
    with ledger._connect() as conn:
        row = conn.execute(
            """UPDATE outbox SET payload=payload || %s::jsonb
               WHERE ns=%s AND id=%s AND target='octop' AND state='SENDING'
                 AND lease_token=%s AND lease_until>clock_timestamp() AND NOT (payload ? 'text')
               RETURNING id""",
            (encode({"text": text, "template": template, "workspace_template_invalid": fallback,
                     "template_fallback": fallback}), ledger.ns, event.id, event.token),
        ).fetchone()
    return row is not None


def finish(ledger, event, error, *, max_attempts=3, retry_delay=10, terminal=False, receipt=None):
    state="SENT" if error is None else "FAILED" if terminal or event.attempt>=max_attempts else "PENDING"
    with ledger._connect() as conn:
        row=conn.execute(
            """UPDATE outbox SET state=%s,lease_token=NULL,lease_until=NULL,error_code=%s,
                 delivered_at=CASE WHEN %s='SENT' THEN clock_timestamp() ELSE NULL END,
                 next_attempt=clock_timestamp()+%s*interval '1 second',delivery_receipt=%s
               WHERE ns=%s AND id=%s AND state='SENDING' AND lease_token=%s
                   AND lease_until>clock_timestamp() RETURNING id""",
            (state,error,state,retry_delay,encode(receipt) if receipt is not None else None,ledger.ns,event.id,event.token),
        ).fetchone()
    return state if row else "LEASE_LOST"


def dispatch(ledger, target, deliver, *, max_attempts=3, retry_delay=10):
    event=claim(ledger,target,max_attempts=max_attempts)
    if not event:
        return "EMPTY"
    receipt = None
    terminal = False
    try:
        receipt = deliver(event)
    except Exception as exc:
        error=type(exc).__name__[:80]
        terminal=isinstance(exc, (DeliveryPermanent, DeliveryUncertain))
        receipt=getattr(exc, "receipt", None)
    else:
        error=None
    return finish(ledger,event,error,max_attempts=max_attempts,
                   retry_delay=uniform(.5, 1.5) * min(60, retry_delay * 2**(event.attempt-1)),terminal=terminal,receipt=receipt)


REDIS_PUBLISH = NOTICE_LUA + """
local old = redis.call('GET', KEYS[1])
if old and old > ARGV[1] then return 0 end
local views = #KEYS - 2
for index=2,views do redis.call('SET',KEYS[index],ARGV[index],'EX',ARGV[views+1]) end
redis.call('SET',KEYS[1],ARGV[1],'EX',ARGV[views+1])
notice(KEYS[views+1],KEYS[views+2],ARGV[views+2],ARGV[views+3])
return 1
"""


def redis_delivery(ledger, cache, event):
    """Publish the committed view even when an old notification is retried."""
    view=ledger.inspect()
    with ledger._connect() as conn:
        row=conn.execute(
            """SELECT payload FROM events WHERE ns=%s AND kind='report'
               ORDER BY at DESC,id DESC LIMIT 1""",(ledger.ns,),
        ).fetchone()
    report=json.loads(row[0])
    version=view["watermark"]
    # A simultaneous newer report may have appeared after the first read.
    # Retrying produces a consistent replacement.
    if report["at"] > version:
        raise RuntimeError("committed view advanced during Redis delivery")
    prefix=ledger.settings.redis_prefix
    # Research/test runs have distinct Redis keys. The configured baseline retains the documented names.
    if ledger.origin=="FROZEN" or ledger.ns != prefix+"baseline":
        prefix+=ledger.ns.removeprefix(prefix)+":"
    keys=[prefix+"snap:hermes:watermark",prefix+"snap:hermes:penetration_board",
          prefix+"snap:hermes:cycle_positions",prefix+"snap:hermes:accuracy_stats",
          prefix+"snap:sector:chain_1_5d"]
    metadata=dict(input_kind=ledger.origin,version=version,report_id=report["id"],
                  source_observed_at=report["data_as_of"],publication_at=report["at"])
    values=[version,encode(dict(metadata=metadata,board=report["input_meta"].get("penetration"))),
            encode(dict(metadata=metadata,positions=view["cycles"])),
            encode(dict(metadata=metadata,evaluation=view["evaluation"])),
            encode(dict(metadata=metadata,rotation=view["rotation"],
                        retired=view["retired_directions"],rounds=report["input_meta"].get("rotation_window",[]))),
            86400]
    keys.extend(notice_keys(ledger.ns,"report"))
    notice=notice_body("report","etf.rotation.results.v1",report["id"],report["at"])
    cache.eval(REDIS_PUBLISH,len(keys),*keys,*values,report["id"],notice)
