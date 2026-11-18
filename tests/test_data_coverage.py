"""Coverage calendar semantics, bounded read-only API and namespace separation."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from io import BytesIO
import json
import os
import unittest
from uuid import uuid4

from modules.strategy_hermes.data_coverage import assemble, parse_query, read_calendar
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.workspace_web import API, application

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
NOW = datetime(2026, 11, 16, 10, tzinfo=CHINA)
TOKEN = "calendar-fixture-test-token-1234567890"


class CalendarSemantics(unittest.TestCase):
    def setUp(self):
        start = date(2026,10,26)
        self.calendar = {start+timedelta(days=i): (start+timedelta(days=i)).weekday()<5 for i in range(42)}
        self.first,self.last = date(2026,11,1),date(2026,11,30)

    def render(self, records=None, view="daily", calendar=None):
        days=assemble(self.first,self.last,view,self.calendar if calendar is None else calendar,records or {},NOW)
        return {row["date"]:row for row in days}

    def test_partial_available_holiday_and_pending_are_distinct(self):
        day=date(2026,11,13)
        records={(api,day):dict(row_count=5571) for api in ("daily","adj_factor","daily_basic")}
        self.assertEqual(self.render(records)[str(day)]["status"],"PARTIAL")
        records[("cyq_perf",day)]=dict(row_count=5500)
        self.assertEqual(self.render(records)[str(day)]["status"],"AVAILABLE")
        self.assertEqual(self.render()["2026-11-12"]["status"],"MISSING")
        self.assertEqual(self.render()["2026-11-14"]["status"],"CLOSED")
        self.assertEqual(self.render()["2026-11-16"]["status"],"PENDING")
        self.assertEqual(self.render(calendar={})["2026-11-13"]["status"],"UNKNOWN")

    def test_weekly_uses_actual_last_session_including_cross_month_week(self):
        self.calendar[date(2026,11,13)]=False
        days=self.render(view="weekly")
        self.assertEqual(days["2026-11-12"]["status"],"MISSING")
        self.assertTrue(days["2026-11-12"]["is_week_end"])
        self.assertEqual(days["2026-11-11"]["status"],"NOT_DUE")
        self.assertEqual(days["2026-11-13"]["status"],"CLOSED")
        unknown=dict(self.calendar);del unknown[date(2026,11,15)]
        self.assertEqual(self.render(view="weekly",calendar=unknown)["2026-11-12"]["status"],"UNKNOWN")
        # A month-end Monday is the week's last session when next month's
        # remaining weekdays are all explicit exchange holidays.
        for number in range(1,5):self.calendar[date(2026,12,number)]=False
        days=assemble(self.first,self.last,"weekly",self.calendar,{},datetime(2026,12,7,tzinfo=CHINA))
        self.assertTrue(days[-1]["is_week_end"])
        self.assertEqual(days[-1]["status"],"MISSING")

    def test_close_boundary_uses_china_time(self):
        day=date(2026,11,16)
        for value,expected in (("2026-11-16T07:34:59+00:00","PENDING"),
                               ("2026-11-16T07:35:00+00:00","MISSING")):
            row=assemble(day,day,"daily",self.calendar,{},datetime.fromisoformat(value))[0]
            self.assertEqual(row["status"],expected)

    def test_missing_scores_do_not_erase_saved_assessments_and_partial_chunks_visible(self):
        day=date(2026,11,13)
        records={("daily",day):dict(row_count=10),
                 ("daily_assessment",day):dict(row_count=20,ready_count=0)}
        row=self.render(records,"assessment")[str(day)]
        self.assertEqual(row["status"],"AVAILABLE")
        self.assertEqual(next(r for r in row["datasets"] if r["dataset"]=="daily_assessment")["ready_count"],0)
        records[("daily_assessment",day)]["row_count"]=10
        self.assertEqual(self.render(records,"assessment")[str(day)]["status"],"PARTIAL")

    def test_low_frequency_snapshots_do_not_make_daily_coverage_complete(self):
        day=date(2026,11,13)
        records={("stock_basic",day):dict(row_count=5924),
                 ("share_float",day):dict(row_count=0,details=dict(valid_empty=True))}
        row=self.render(records)[str(day)]
        self.assertEqual(row["status"],"MISSING")
        details={item["dataset"]:item for item in row["datasets"]}
        self.assertEqual(details["stock_basic"]["status"],"AVAILABLE")
        self.assertEqual(details["share_float"]["status"],"EMPTY")
        self.assertNotIn("fina_indicator",details)

    def test_month_bounds_leap_year_and_invalid_queries(self):
        first,last,view=parse_query("month=2024-02&view=weekly",NOW)
        self.assertEqual((str(first),str(last),view),("2024-02-01","2024-02-29","weekly"))
        for query in ("month=2026-13","month=2026-1","month=0001-01","month=9999-12",
                      "month=2026-11&month=2026-10","view=basic","month=2026-11&extra=1","view"):
            with self.subTest(query=query),self.assertRaisesRegex(ValueError,"INVALID_COVERAGE_QUERY"):
                parse_query(query,NOW)


@unittest.skipUnless(ENV,"requires isolated PostgreSQL STAGE2_TEST_ENV_FILE")
class CalendarAPI(unittest.TestCase):
    def setUp(self):
        settings=Settings.read(ENV);self.assertEqual(settings.env,"test")
        self.store=PostgresLedger(replace(settings,namespace=settings.redis_prefix+"calendar-api:"+uuid4().hex),"FROZEN")
        self.store.migrate()
        self.day=date(1994,1,3)
        self.calendar=[str(self.day+timedelta(days=i)) for i in range(12) if (self.day+timedelta(days=i)).weekday()<5]
        self.app=application(self.store,TOKEN,calendar=self.calendar)
        with self.store._connect() as c:
            c.execute("""INSERT INTO data_coverage(scope,dataset,business_date,row_count,ready_count,source_at)
                VALUES(%s,'daily_assessment',%s,100,0,'1994-01-04T06:00:00+08:00')""",(self.store.ns,self.day))
        self.addCleanup(self.clean)

    def clean(self):
        with self.store._connect() as c:
            c.execute("DELETE FROM data_coverage WHERE scope=%s",(self.store.ns,))
            c.execute("DELETE FROM supplier_trade_calendar WHERE batch_id=%s",(self.store.ns,))
            c.execute("DELETE FROM supplier_batches WHERE batch_id=%s",(self.store.ns,))

    def request(self, query, auth=True, method="GET"):
        env=dict(REQUEST_METHOD=method,PATH_INFO=API+"/data-coverage",QUERY_STRING=query,
                 CONTENT_LENGTH="0",**{"wsgi.input":BytesIO(b"")})
        if auth:env["HTTP_AUTHORIZATION"]="Bearer "+TOKEN
        status=[]
        raw=b"".join(self.app(env,lambda value,headers:status.append(int(value.split()[0]))))
        return status[0],json.loads(raw)

    def test_authenticated_get_reads_saved_counters_and_other_namespaces_stay_separate(self):
        code,data=self.request("month=1994-01&view=assessment")
        self.assertEqual(code,200,data)
        day=next(r for r in data["days"] if r["date"]==str(self.day))
        row=next(r for r in day["datasets"] if r["dataset"]=="daily_assessment")
        self.assertEqual((day["status"],row["row_count"],row["ready_count"]),("AVAILABLE",100,0))
        self.assertEqual(row["source_at"],"1994-01-04T06:00:00+08:00")
        with self.store._connect() as c:
            before=c.execute("SELECT count(*) FROM collection_requests").fetchone()[0]
        readonly=PostgresLedger(replace(self.store.settings,namespace=self.store.ns+"other"),"FROZEN")
        result=read_calendar(readonly,self.calendar,"month=1994-01&view=assessment")
        self.assertEqual(result["days"][2]["status"],"MISSING")
        with self.store._connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM collection_requests").fetchone()[0],before)
        self.assertEqual(self.request("month=1994-01",auth=False)[0],401)
        self.assertEqual(self.request("month=1994-01&view=bad")[0],400)
        self.assertEqual(self.request("",method="POST")[0],404)

    def test_database_closed_session_overrides_file_calendar_and_latest_day(self):
        with self.store._connect() as c:
            c.execute("""INSERT INTO supplier_batches VALUES
                (%s,'trade_cal','{}','1994-01-14T06:00:00+08:00',1,'[]','[]')""",(self.store.ns,))
            c.execute("INSERT INTO supplier_trade_calendar VALUES('SSE','1994-01-14',false,%s)",(self.store.ns,))
        result=read_calendar(self.store,self.calendar,"month=1994-01",
                             now=datetime(1994,1,14,16,tzinfo=CHINA))
        self.assertEqual(result['latest_business_date'],'1994-01-13')
        self.assertEqual(result['days'][13]['status'],'CLOSED')

    def test_api_uses_read_only_connection_and_repeated_reads_keep_original_timestamp(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        original=self.store._connect
        queries=[]
        class ReadConnection:
            def __init__(self,conn):self.conn=conn
            def transaction(self):return self.conn.transaction()
            def execute(self,sql,args=()):
                queries.append(sql)
                return self.conn.execute(sql,args)
        @contextmanager
        def readonly():
            with original() as c:
                c.execute("SET default_transaction_read_only=on")
                yield ReadConnection(c)
        with patch.object(self.store,"_connect",readonly):
            first=read_calendar(self.store,self.calendar,"month=1994-01&view=assessment")
            second=read_calendar(self.store,self.calendar,"month=1994-01&view=assessment")
        self.assertEqual(first,second)
        self.assertTrue(any("FROM data_coverage" in sql for sql in queries))
        for table in ("supplier_daily_bars","supplier_lr_features","supplier_reference_rows","left_right_assessments"):
            self.assertFalse(any(table in sql for sql in queries),table)
