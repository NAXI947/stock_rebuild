"""PostgreSQL automatic cycle ledger and publishing transaction."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from itertools import groupby
import json
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import set_json_loads

from ..ledger import Ledger, encode, event_pair, fingerprint
from ..simulator import CODE, Calendar, Rules, integer, moment
from .execution_calendar import execution_calendar
from .execution_units import apply_candidate_blocks, eligible_report, execution_units
from .entry_admission import candidate_codes, filter_report, read_states, refresh_waiting

MIGRATIONS = Path(__file__).with_name("migrations")


@dataclass(frozen=True)
class RoundClaim:
    day: str
    slot: str
    token: str


class LeaseLost(RuntimeError):
    pass


class BoundConnection:
    """Reuse the Stage 1 SQL calls with Psycopg placeholders."""
    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, args=()):
        if query == "BEGIN":
            return self.connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return self.connection.execute(query.replace("?", "%s"), args)


class PostgresLedger(Ledger):
    def __init__(self, settings, origin):
        if origin not in ("FROZEN", "LIVE"):
            raise ValueError("unknown Stage 2 input kind")
        if settings.env == "test" and origin != "FROZEN":
            raise ValueError("the test database accepts only frozen inputs")
        self.settings = settings
        self.ns = settings.namespace
        self.origin = origin

    @contextmanager
    def _connect(self):
        with psycopg.connect(self.settings.dsn, connect_timeout=5, autocommit=True) as connection:
            set_json_loads(lambda raw: raw.decode("utf-8"), context=connection)
            connection.execute("SET search_path TO stock_v4_stage2, pg_catalog")
            connection.execute("SET statement_timeout TO '20s'")
            connection.execute("SET lock_timeout TO '5s'")
            yield connection

    @contextmanager
    def connection(self):
        with self._connect() as connection:
            yield BoundConnection(connection)

    def migrate(self):
        with self._connect() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(548273196)")
            connection.execute("CREATE SCHEMA IF NOT EXISTS stock_v4_stage2")
            connection.execute("""CREATE TABLE IF NOT EXISTS schema_revisions
                (revision text PRIMARY KEY, digest text NOT NULL,
                 applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
            for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9].sql")):
                ddl = migration.read_text(encoding="utf-8")
                digest = fingerprint(ddl)
                previous = connection.execute("SELECT digest FROM schema_revisions WHERE revision=%s",
                                              (migration.stem,)).fetchone()
                if previous:
                    if previous[0] != digest:
                        raise ValueError("applied migration was modified")
                    continue
                connection.execute(ddl)
                connection.execute("INSERT INTO schema_revisions(revision,digest) VALUES (%s,%s)",
                                   (migration.stem,digest))

    @staticmethod
    def _save(db, ns, cid, cycle, opened):
        Ledger._save(db, ns, cid, cycle, opened)
        for order in cycle["orders"]:
            db.execute(
                """INSERT INTO simulation_orders VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT (ns,cycle_id,order_id) DO UPDATE SET status=excluded.status,
                target_quantity=excluded.target_quantity, remaining_quantity=excluded.remaining_quantity,
                evidence=excluded.evidence""",
                (ns,cid,order["id"],order["action"],order["requested_at"],order["status"],
                 order["target"],order["remaining"],encode(order)),
            )
        for fill in cycle["book"]["fills"]:
            db.execute(
                """INSERT INTO simulation_fills VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (ns,cycle_id,quote_id) DO NOTHING""",
                (ns,cid,fill["quote_id"],fill["order_id"],fill["filled_at"],fill["quantity"],
                 fill["price"],fill["amount"],fill["fee"],encode(fill)),
            )

    def refresh_entry_admission(self):
        """Update waiting-buy reasons without fetching quotes or changing nodes."""
        if self.origin != "LIVE":
            return 0
        with self._connect() as connection, connection.transaction():
            return refresh_waiting(self, connection)

    def _apply(self, db, ns, events, calendar, units, rules):
        if self.origin != "LIVE":
            return super()._apply(db, ns, events, calendar, units, rules)
        reports = [(kind, event) for kind, event in events if kind == "report"]
        quotes = [(kind, event) for kind, event in events if kind == "quote"]
        if reports:
            states = getattr(db, 'entry_states', None)
            if states is None:
                states = read_states(db.connection, ns, [code for _, event in reports for code in candidate_codes(event)])
            for _, event in reports:
                filtered, _ = filter_report(event, states, units)
                event['candidates'] = filtered['candidates']
            super()._apply(db, ns, reports, calendar, units, rules)
        # A report and a quote can share one input transaction. Newly created
        # WAIT_BUY cycles are checked before the quote can reach fill().
        refresh_waiting(self, db.connection)
        if quotes:
            super()._apply(db, ns, quotes, calendar, units, rules)

    def _store(self, db, raw_events, days, instruments, rule):
        calendar = Calendar(days)
        units = {code: integer(unit) for code, unit in instruments.items() if CODE.fullmatch(code)}
        if len(units) != len(instruments):
            raise ValueError("invalid ETF code in instrument registry")
        config = encode(dict(calendar=days, instruments=units))
        frozen_rule = encode(asdict(rule))
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(?,0))", (self.ns,))
        db.execute("INSERT INTO runs VALUES (?,?,NULL,?) ON CONFLICT DO NOTHING", (self.ns, config, self.origin))
        saved, watermark, origin = db.execute("SELECT config,watermark,origin FROM runs WHERE ns=?", (self.ns,)).fetchone()
        if origin != self.origin:
            raise ValueError("run namespace belongs to a different input kind")
        if saved != config:
            before = json.loads(saved)
            if any(units.get(code) != unit for code,unit in before["instruments"].items()):
                raise ValueError("previous instrument units must be present and unchanged")
            if not set(before["calendar"]).issubset(days):
                raise ValueError("published calendar days must remain available")
            if watermark and any(d <= moment(watermark).date().isoformat() and d not in before["calendar"] for d in days):
                raise ValueError("calendar update inserts an already processed trading day")
            db.execute("UPDATE runs SET config=? WHERE ns=?", (config,self.ns))
        db.execute("INSERT INTO rule_versions VALUES (?,?,?) ON CONFLICT DO NOTHING", (self.ns,rule.version,frozen_rule))
        saved_rule,=db.execute("SELECT snapshot FROM rule_versions WHERE ns=? AND version=?", (self.ns,rule.version)).fetchone()
        if frozen_rule != saved_rule:
            raise ValueError("changed rules require a new version")

        last_report,=db.execute("SELECT max(at) FROM events WHERE ns=? AND kind='report'", (self.ns,)).fetchone()
        committed_reports = []
        new_quote_ids = []
        pairs = sorted((event_pair(event) for event in raw_events), key=lambda pair:(pair[0], pair[1], pair[2]["id"]))
        for at, group in groupby(pairs, key=lambda pair:pair[0]):
            fresh = []
            for _, kind, event, digest in group:
                if kind == "quote":
                    # Receiver timestamps may differ when the same source observation is retried.
                    digest = fingerprint({k:v for k,v in event.items() if k not in ("at","received_at")})
                old = db.execute("SELECT fingerprint FROM events WHERE ns=? AND kind=? AND id=?", (self.ns,kind,event["id"])).fetchone()
                if old:
                    if old[0] != digest:
                        raise ValueError("event ID collides with different evidence")
                    continue
                if kind == "report" and last_report and at <= last_report:
                    raise ValueError("report publication must advance the run clock")
                if kind == "report":
                    last_report = at
                if kind == "quote":
                    new_quote_ids.append(event["id"])
                    observed = moment(event["observed_at"])
                    if (event["tradeable"] and calendar.legal(observed)
                            and calendar.next(observed.date()) is None):
                        # Keep the quote for audit without opening lots of unknown T+1 eligibility.
                        event["tradeable"] = False
                        event["execution_block"] = "NEXT_TRADING_DAY_UNKNOWN"
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                           (self.ns,kind,event["id"],at,digest,encode(event)))
                fresh.append((kind,event))
            if fresh:
                self._apply(db,self.ns,fresh,calendar,units,rule)
                for kind,event in fresh:
                    if kind == "report":
                        db.execute("UPDATE events SET payload=? WHERE ns=? AND kind='report' AND id=?",
                                   (encode(event),self.ns,event["id"]))
                        committed_reports.append(event)
                watermark = max(at, watermark) if watermark else at
        db.execute("UPDATE runs SET watermark=? WHERE ns=?", (watermark,self.ns))
        return committed_reports, new_quote_ids

    def apply_quotes(self, quotes, days, instruments, rule=None):
        if any(event.get("kind") != "quote" for event in quotes):
            raise ValueError("quote intake requires quote events")
        if self.origin=="LIVE" and any(
            moment(event["observed_at"]).date()!=moment(event["received_at"]).date()
            for event in quotes
        ):
            raise ValueError("historical quotes require a frozen replay")
        with self._connect() as connection, connection.transaction():
            execution_days = execution_calendar(connection, self.ns, self.origin, days)
            units = execution_units(connection, self.ns, self.origin, instruments)
            if self.origin == "LIVE":
                refresh_waiting(self, connection)
            _, new_ids=self._store(BoundConnection(connection),quotes,execution_days,units,rule or Rules())
            if new_ids:
                report=connection.execute(
                    """SELECT id FROM events WHERE ns=%s AND kind='report'
                       ORDER BY at DESC,id DESC LIMIT 1""",(self.ns,),
                ).fetchone()
                if report:
                    payload={"input_kind":self.origin,"report_id":report[0],"quote_events":len(new_ids)}
                    connection.execute(
                        """INSERT INTO outbox(ns,report_id,event_key,target,payload)
                           VALUES (%s,%s,%s,'redis',%s) ON CONFLICT DO NOTHING""",
                        (self.ns,report[0],"quotes:"+fingerprint(sorted(new_ids)),encode(payload)),
                    )

    def claim_round(self, day, slot, planned, *, lease_seconds=120, max_attempts=2):
        with self._connect() as conn, conn.transaction():
            return self._claim_round(conn, day, slot, planned,
                                     lease_seconds=lease_seconds, max_attempts=max_attempts)

    def _claim_round(self, conn, day, slot, planned, *, lease_seconds=120, max_attempts=2):
        """Claim within the caller's transaction without resetting call budgets."""
        token = str(uuid4())
        row = conn.execute(
            """INSERT INTO round_tasks (ns,trade_date,slot,planned_at,state,lease_token,lease_until,attempts)
                   VALUES (%s,%s,%s,%s,'RUNNING',%s,clock_timestamp() + %s * interval '1 second',1)
                   ON CONFLICT (ns,trade_date,slot) DO UPDATE
                     SET state='RUNNING',lease_token=excluded.lease_token,lease_until=excluded.lease_until,
                         attempts=round_tasks.attempts+1,error_code=NULL
                   WHERE round_tasks.attempts < %s AND
                     ((round_tasks.state='FAILED' AND round_tasks.error_code IS DISTINCT FROM 'OctopRejected')
                       OR (round_tasks.state='RUNNING' AND round_tasks.lease_until<=clock_timestamp()))
                   RETURNING attempts""",
            (self.ns,day,slot,planned,token,lease_seconds,max_attempts),
        ).fetchone()
        return RoundClaim(day,slot,token) if row else None

    def take_call(self, claim, service, limit):
        with self._connect() as conn, conn.transaction():
            changed=conn.execute(
                """UPDATE round_tasks SET calls=jsonb_set(calls,ARRAY[%s],
                     to_jsonb(COALESCE((calls->>%s)::integer,0)+1))
                   WHERE ns=%s AND trade_date=%s AND slot=%s AND state='RUNNING' AND lease_token=%s
                     AND lease_until>clock_timestamp() AND COALESCE((calls->>%s)::integer,0)<%s
                   RETURNING attempts""",
                (service,service,self.ns,claim.day,claim.slot,claim.token,service,limit),
            ).fetchone()
        return changed is not None

    def fail_round(self, claim, error_code):
        with self._connect() as conn:
            conn.execute(
                """UPDATE round_tasks SET state='FAILED',error_code=%s,lease_token=NULL,lease_until=NULL
                   WHERE ns=%s AND trade_date=%s AND slot=%s AND state='RUNNING' AND lease_token=%s""",
                (error_code,self.ns,claim.day,claim.slot,claim.token),
            )

    def publish(self, claim, report, days, instruments, input_meta, *, targets=("redis",), rule=None,
                etf_projection=None):
        if report.get("kind") != "report" or set(targets) - {"redis","octop"}:
            raise ValueError("invalid report or destination")
        with self._connect() as conn, conn.transaction():
            task=conn.execute(
                """SELECT planned_at FROM round_tasks WHERE ns=%s AND trade_date=%s AND slot=%s
                   AND state='RUNNING' AND lease_token=%s AND lease_until>clock_timestamp() FOR UPDATE""",
                (self.ns,claim.day,claim.slot,claim.token),
            ).fetchone()
            if not task:
                raise LeaseLost("round no longer owns its publishing lease")
            _,_,decoded,_=event_pair(report)
            if moment(decoded["at"]).date().isoformat()!=claim.day or moment(decoded["at"])<task[0]:
                raise ValueError("report publication does not belong to this round")
            db=BoundConnection(conn)
            execution_days = execution_calendar(conn, self.ns, self.origin, days)
            units = execution_units(conn, self.ns, self.origin, instruments)
            if self.origin == "LIVE":
                states = read_states(conn, self.ns, candidate_codes(report))
                report, blocked = filter_report(report, states, units)
                report, unit_blocks = eligible_report(report, instruments, units)
                blocked += unit_blocks
                input_meta = apply_candidate_blocks(input_meta, blocked)
                input_meta = {**input_meta, 'entry_admission': states}
                # Reuse this transaction's locked admission result for the report
                # projection so a second read failure cannot silently contradict
                # the persisted QQ/workbench candidate explanation.
                db.entry_states = states
            if input_meta.get('manual_recovery'):
                from .native_recovery import validate_publication
                validate_publication(self, conn, claim, input_meta['manual_recovery'])
            if etf_projection is not None and etf_projection["policy"]["policy_mode"] == "V3_ENFORCE":
                db.etf_anchor = {decoded["id"]: etf_projection["pairs"]}
            committed,_=self._store(db,[report],execution_days,units,rule or Rules())
            if len(committed)!=1:
                raise ValueError("report ID already belongs to a committed round")
            published=committed[0]
            if etf_projection is not None:
                from .etf_support_facts import finalize_support_facts
                if etf_projection["policy"]["policy_mode"] == "V3_SHADOW":
                    from copy import deepcopy
                    try:
                        with conn.transaction(savepoint_name="v3_shadow_sp"):
                            projected = finalize_support_facts(conn, self.ns, deepcopy(etf_projection),
                                deepcopy(published), deepcopy(etf_projection["recommendations"]))
                            shadow = {(row["sector_id"], row["etf"])
                                      for row in etf_projection["recommendations"]["candidates"]}
                            actual = {(row["sector_id"], code) for code, rows in published["candidates"].items()
                                      for row in rows}
                            projected["candidate_differences"] = dict(
                                removed=[list(pair) for pair in sorted(actual-shadow)],
                                added=[list(pair) for pair in sorted(shadow-actual)])
                            encode(projected)
                    except (psycopg.OperationalError, psycopg.InterfaceError, LeaseLost):
                        raise
                    except Exception as exc:
                        input_meta = {**input_meta, "representation_shadow_error":
                                      dict(stage="FINALIZE", error=type(exc).__name__)}
                    else:
                        input_meta = {**input_meta, "representation_shadow": projected}
                else:
                    input_meta = {**input_meta, "representation": finalize_support_facts(
                        conn, self.ns, etf_projection, published, input_meta["etf_recommendations"])}
            published["input_meta"]=input_meta
            published["planned_at"]=task[0].isoformat()
            published["round_slot"]=claim.slot
            db.execute("UPDATE events SET payload=? WHERE ns=? AND kind='report' AND id=?",
                       (encode(published),self.ns,published["id"]))
            payload={"input_kind":self.origin,"report_id":published["id"],"published_at":published["at"],
                     "trade_date":claim.day,"round_slot":claim.slot}
            for target in targets:
                target_payload = payload
                if target == "octop":
                    from ..qq_templates import MESSAGE_FORMAT
                    from .notifications import visual_brief_sections
                    orders = conn.execute(
                        """SELECT c.etf,o.action,o.remaining_quantity,o.status,o.evidence->>'eligible_at',
                                  (o.evidence->>'report_id'=%s) AS is_new,o.evidence->>'last_block'
                           FROM simulation_orders o JOIN cycles c ON (c.ns,c.id)=(o.ns,o.cycle_id)
                           WHERE o.ns=%s AND (o.status='PENDING' OR o.evidence->>'report_id'=%s)
                           ORDER BY is_new DESC,o.requested_at DESC,c.etf,o.action""",
                        (published["id"],self.ns,published["id"])).fetchall()
                    target_payload = {**payload, "format":MESSAGE_FORMAT, "template_kind":"research",
                                      "sections":visual_brief_sections(claim.slot,published,orders)}
                conn.execute(
                    """INSERT INTO outbox(ns,report_id,event_key,target,payload)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (self.ns,published["id"],"report:"+published["id"],target,encode(target_payload)),
                )
            changed=conn.execute(
                """UPDATE round_tasks SET state='PUBLISHED',report_id=%s,lease_token=NULL,lease_until=NULL
                   WHERE ns=%s AND trade_date=%s AND slot=%s AND state='RUNNING' AND lease_token=%s
                   AND lease_until>clock_timestamp() RETURNING report_id""",
                (published["id"],self.ns,claim.day,claim.slot,claim.token),
            ).fetchone()
            if not changed:
                raise LeaseLost("round publishing lease expired")
        return published

    def inspect(self, ns=None):
        ns = self.ns if ns is None else ns
        if ns != self.ns:
            raise ValueError("requested namespace outside configured run")
        view=super().inspect(ns)
        with self._connect() as conn:
            origin,=conn.execute("SELECT origin FROM runs WHERE ns=%s",(ns,)).fetchone()
        view["input_kind"]=origin
        return view

    def report(self, rid):
        return super().report(self.ns,rid)
