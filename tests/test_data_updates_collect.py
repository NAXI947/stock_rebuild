"""Original five-cadence providers, with HTTP fixtures and isolated PostgreSQL."""
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes.data_updates_collect import (
    UpdateTushare, collect_reference, execute_update, normalize_reference, planned_units, publish_reference,
    reference_pages, reference_scopes)
from modules.strategy_hermes.bulk_eod_collect import BulkTushare, collect_day
from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings

ENV = os.environ.get("STAGE2_TEST_ENV_FILE")
STAMP = datetime(2026,11,14,20,tzinfo=CHINA)
DAY = date(2002,1,4)


def body(mode, **changes):
    return dict(mode=mode,latest_business_date=str(DAY),submitted_at=STAMP.isoformat(),
                trade_dates=[str(DAY)],weekly_dates=[str(DAY)],snapshot_date=str(DAY),month="2002-01",year=2025) | changes


class OriginalUpdateContracts(unittest.TestCase):
    def test_market_classification_is_preserved_as_source_data_or_unknown(self):
        original=dict(ts_code="995099.SH",symbol="995099",name="分类资料",market=" 主板 ",
                      list_status="L",list_date="20000101",delist_date=None)
        rows,_,_=normalize_reference("stock_basic",[original],DAY,body("basic"),STAMP)
        self.assertEqual(rows[0]["body"]["market"],"主板")
        rows,_,_=normalize_reference("stock_basic",[original|dict(market=None)],DAY,body("basic"),STAMP)
        self.assertIsNone(rows[0]["body"]["market"])

    def test_reference_archives_preserve_retired_alphanumeric_stock_identities(self):
        stocks=[dict(ts_code=code,symbol=code.split('.')[0],name="上港集箱(退)",
                     list_status="D",list_date="20000719",delist_date="20061020")
                for code in ("T600018.SH","TS0018.SH","600018.SH")]
        rows,details,raw=normalize_reference("stock_basic",stocks,DAY,body("basic"),STAMP)
        self.assertEqual([r["entity_key"] for r in rows],[r["ts_code"] for r in stocks])
        self.assertEqual(raw,stocks)
        member=dict(ts_code="T00018.SH",name="上港集箱(退市)",in_date="20000719",out_date=None)
        rows,_,_=normalize_reference("index_member_all",[member],DAY,body("basic"),STAMP)
        self.assertEqual(rows[0]["body"]["ts_code"],"T00018.SH")
        for invalid in (None,"", "600018", "600018.XX", "600018.SH "):
            with self.subTest(invalid=invalid),self.assertRaisesRegex(ValueError,"SOURCE_STOCK_CODE_MISSING"):
                normalize_reference("stock_basic",[stocks[0]|dict(ts_code=invalid)],DAY,body("basic"),STAMP)
        with self.assertRaisesRegex(ValueError,"SOURCE_STOCK_CODE_MISSING"):
            normalize_reference("share_float",[dict(ts_code="T600018.SH",float_date="20020120")],
                                date(2002,1,1),body("monthly"),STAMP)

    def test_original_five_modes_and_date_scope_are_not_expanded(self):
        self.assertEqual(planned_units(body("daily")), [(name,str(DAY)) for name in ("daily","adj_factor","daily_basic","cyq_perf")])
        self.assertEqual(planned_units(body("weekly")), [("weekly",str(DAY))])
        self.assertEqual([name for name,_ in planned_units(body("basic"))], ["stock_basic","trade_cal","index_member_all"])
        self.assertEqual(planned_units(body("monthly")), [("dc_member",str(DAY)),("share_float","2002-01-01")])
        self.assertEqual(planned_units(body("yearly")), [("fina_indicator","2025-12-31")])
        scopes=reference_scopes("trade_cal",DAY,body("basic"))
        self.assertEqual(scopes,[('trade_cal',dict(exchange=x,start_date="20250101",end_date="20271231")) for x in ("SSE","SZSE")])
        self.assertEqual(reference_scopes("fina_indicator",date(2025,12,31),body("yearly")),
                         [("fina_indicator_vip",dict(period="20251231"))])

    def test_financial_percent_units_future_announcement_and_scope(self):
        row=dict(ts_code="995001.SZ",ann_date="20260320",end_date="20251231",roe="12.3",debt_to_assets="42.1")
        rows,details,raw=normalize_reference("fina_indicator",[row,row|dict(end_date="20250930")],date(2025,12,31),body("yearly"),STAMP)
        self.assertEqual(rows[0]["body"]["roe"],"12.3")
        self.assertEqual(rows[0]["body"]["ratio_unit"],"percent")
        self.assertEqual(details,dict(report_year=2025,upstream_rows=2,excluded_out_of_scope=1))
        self.assertEqual(raw,[row])
        with self.assertRaisesRegex(ValueError,"SOURCE_FUTURE_ANNOUNCEMENT"):
            normalize_reference("fina_indicator",[row|dict(ann_date="20280101")],date(2025,12,31),body("yearly"),STAMP)

    def test_original_month_scope_is_applied_before_row_validation(self):
        retained=dict(ts_code="995001.SZ",ann_date=None,float_date="20020120",float_share="123",float_ratio="12.5",holder_name="甲",share_type="A")
        excluded=retained|dict(ts_code=None,float_date="20020220")
        rows,details,raw=normalize_reference("share_float",[retained,excluded],date(2002,1,1),body("monthly"),STAMP)
        self.assertEqual((len(rows),details["excluded_out_of_scope"],raw),(1,1,[retained]))
        self.assertIsNone(rows[0]["body"]["ann_date"])


@unittest.skipUnless(ENV,"requires isolated PostgreSQL STAGE2_TEST_ENV_FILE")
class OriginalUpdateStorage(unittest.TestCase):
    def setUp(self):
        settings=Settings.read(ENV);self.assertEqual(settings.env,"test")
        self.store=PostgresLedger(replace(settings,namespace=settings.redis_prefix+"original-updates:"+uuid4().hex),"FROZEN")
        self.store.migrate()
        self.directory=TemporaryDirectory(dir=os.environ.get("STAGE1_TEST_ROOT"));self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/"provider.json"
        self.config.write_text(json.dumps(dict(token="original-update-fixture-"+uuid4().hex,base_url="https://fixtures.invalid")))
        self.config.chmod(0o600);self.client=UpdateTushare(self.config)
        self.calls=[];self.records=[];self.batches=set();self.addCleanup(self.clean)

    def clean(self):
        source=self.client.identity("daily")
        with self.store._connect() as conn,conn.transaction():
            batches=set(self.batches)
            batches.update(r[0] for r in conn.execute("SELECT batch_id FROM supplier_tushare_eod_runs WHERE source_key=%s",(source,)))
            # Each test uses unique source identity and nonoverlapping fixture instruments.
            if batches:
                for table in ("supplier_reference_rows","supplier_daily_bars","supplier_stock_catalog","supplier_trade_calendar","supplier_lr_features"):
                    conn.execute("DELETE FROM "+table+" WHERE batch_id=ANY(%s)",(list(batches),))
                conn.execute("DELETE FROM supplier_tushare_eod_runs WHERE batch_id=ANY(%s)",(list(batches),))
                conn.execute("DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)",(list(batches),))
                conn.execute("DELETE FROM supplier_batches WHERE batch_id=ANY(%s)",(list(batches),))
            conn.execute("DELETE FROM supplier_tushare_pages WHERE source_key=%s",(source,))

    def request(self, source, scope, operation):
        self.calls.append((source,scope))
        try:return dict(status="SAVED",receipt=operation())
        except (ValueError,OSError) as error:return dict(status="FAILED",error_code=type(error).__name__)

    def record(self, dataset, day, receipt):
        self.records.append((dataset,day,receipt))
        if receipt.get("batch_id"):self.batches.add(receipt["batch_id"])

    def reference(self,dataset,day,mode,rows):
        def post(_url,payload,**_kwargs):
            fields=payload["fields"].split(",")
            values=rows(payload) if callable(rows) else rows
            return dict(code=0,data=dict(fields=fields,items=[[r.get(f) for f in fields] for r in values]))
        with patch("modules.strategy_hermes.data_updates_collect.post_json",side_effect=post):
            result=collect_reference(self.store,self.client,dataset,day,body(mode),self.request,lambda:None)
        self.batches.add(result["batch_id"])
        return result

    def test_basic_all_listing_statuses_shared_calendar_and_industry(self):
        def post(_url,payload,**_kwargs):
            api,scope=payload["api_name"],payload["params"]
            if api=="stock_basic":
                code={"L":"995001.SZ","D":"995002.SZ","P":"995003.SZ"}[scope["list_status"]]
                rows=[dict(ts_code=code,symbol=code[:6],name="原模板股票",industry="行业",list_date="19950101",delist_date=None,list_status=scope["list_status"])]
            elif api=="trade_cal":
                self.assertEqual((scope["start_date"],scope["end_date"]),("20250101","20271231"))
                rows=[dict(exchange=scope["exchange"],cal_date="20260105",is_open=1)]
            else:
                self.assertEqual((scope["is_new"],scope["limit"]),("Y",2000))
                rows=[dict(ts_code="995001.SZ",name="原模板股票",l1_name="一级",l2_name="二级",in_date="20200101",out_date=None)]
            fields=payload["fields"].split(",")
            return dict(code=0,data=dict(fields=fields,items=[[r.get(f) for f in fields] for r in rows]))
        with patch("modules.strategy_hermes.data_updates_collect.post_json",side_effect=post):
            result=execute_update(self.store,body("basic"),self.config,None,self.request,lambda:None,self.record)
        self.assertEqual((result["completed"],result["failed"]),(3,0),self.records)
        self.assertEqual(len(self.calls),6)
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM supplier_stock_catalog WHERE batch_id=ANY(%s)",(list(self.batches),)).fetchone()[0],3)
            self.assertEqual(conn.execute("SELECT count(*) FROM supplier_trade_calendar WHERE batch_id=ANY(%s)",(list(self.batches),)).fetchone()[0],2)
            row=conn.execute("SELECT body FROM supplier_reference_rows WHERE dataset='index_member_all' AND batch_id=ANY(%s)",(list(self.batches),)).fetchone()
            self.assertEqual(json.loads(row[0])["l2_name"],"二级")

    def test_stock_market_publication_updates_known_value_and_retains_it_when_missing(self):
        original=dict(ts_code="995099.SH",symbol="995099",name="分类资料",market="主板",
                      list_status="L",list_date="20000101",delist_date=None)
        for offset,market,expected in ((0,"主板","主板"),(1,None,"主板"),(2,"科创板","科创板")):
            raw=[original|dict(market=market)]
            observed=STAMP+timedelta(minutes=offset)
            rows,details,_=normalize_reference("stock_basic",raw,DAY,body("basic"),observed)
            receipt=publish_reference(self.store,"stock_basic",DAY,uuid4().hex,raw,rows,observed,details)
            self.batches.add(receipt["batch_id"])
            with self.store._connect() as conn:
                saved=conn.execute("SELECT market,batch_id FROM supplier_stock_catalog WHERE code=%s",("995099.SH",)).fetchone()
            self.assertEqual(saved,(expected,receipt["batch_id"]))

    def test_monthly_unlock_scope_units_and_valid_empty_publish(self):
        rows=[dict(ts_code="995010.SZ",ann_date="20011220",float_date="20020120",float_share="123",float_ratio="12.5",holder_name="甲",share_type="A"),
              dict(ts_code="995011.SZ",ann_date="20011220",float_date="20020220",float_share="456",float_ratio="25",holder_name="乙",share_type="A")]
        receipt=self.reference("share_float",date(2002,1,1),"monthly",rows)
        self.assertEqual(receipt["row_count"],1)
        self.assertEqual(receipt["details"]["excluded_out_of_scope"],1)
        with self.store._connect() as conn:
            saved=json.loads(conn.execute("SELECT body FROM supplier_market_batches WHERE batch_id=%s",(receipt["batch_id"],)).fetchone()[0])
            self.assertEqual(len(saved["raw"]),1)
            self.assertEqual(saved["rows"][0]["body"]["float_ratio"],"0.125")
        # Distinct source prevents the completed reference range cache from hiding empty input.
        self.client.config["token"]+="-empty"
        empty=self.reference("share_float",date(2002,1,1),"monthly",[])
        self.assertEqual((empty["status"],empty["row_count"]),("EMPTY",0))
        with self.store._connect() as conn:
            self.assertEqual(conn.execute("SELECT row_count FROM supplier_market_batches WHERE batch_id=%s",(empty["batch_id"],)).fetchone()[0],0)
            conn.execute("DELETE FROM supplier_tushare_pages WHERE source_key=%s",(self.client.identity("daily"),))
        self.client.config["token"]=self.client.config["token"][:-6]

    def test_retired_reference_identifiers_are_stored_without_aborting_the_batch(self):
        codes=("T600018.SH","TS0018.SH","995001.SH")
        def stocks(payload):
            if payload["params"]["list_status"]!="D":return []
            return [dict(ts_code=c,symbol=c.split('.')[0],name="退市资料",list_status="D",
                         list_date="20000719",delist_date="20061020") for c in codes]
        receipt=self.reference("stock_basic",DAY,"basic",stocks)
        self.assertEqual((receipt["status"],receipt["row_count"]),("OK",3))
        member=self.reference("index_member_all",DAY,"basic",[
            dict(ts_code="T00018.SH",name="退市资料",l1_name="交通运输",l2_name="航运港口",in_date="20000719",out_date=None)])
        with self.store._connect() as conn:
            saved=conn.execute("SELECT code FROM supplier_stock_catalog WHERE batch_id=%s ORDER BY code",(receipt["batch_id"],)).fetchall()
            self.assertEqual([r[0] for r in saved],sorted(codes))
            row=conn.execute("SELECT entity_key FROM supplier_reference_rows WHERE batch_id=%s",(member["batch_id"],)).fetchone()
            self.assertEqual(row[0],"T00018.SH")

    def test_monthly_concept_uses_selected_snapshot_and_requires_rows(self):
        receipt=self.reference("dc_member",DAY,"monthly",[dict(trade_date="20020104",ts_code="BK001",con_code="995001.SZ",name="概念")])
        self.assertEqual(receipt["row_count"],1)
        self.assertEqual(self.calls[0][1]["trade_date"],"20020104")
        with self.assertRaisesRegex(ValueError,"SOURCE_EMPTY_REQUIRED_DATASET"):
            self.reference("fina_indicator",date(2025,12,31),"yearly",[])

    def test_successful_reference_pages_and_original_times_reused(self):
        receipt=self.reference("dc_member",DAY,"monthly",[dict(trade_date="20020104",ts_code="BK001",con_code="995001.SZ",name="概念")])
        with patch("modules.strategy_hermes.data_updates_collect.post_json",side_effect=AssertionError("no new request")):
            reused=collect_reference(self.store,self.client,"dc_member",DAY,body("monthly"),self.request,lambda:None)
        self.assertEqual(receipt,reused)
        self.assertEqual(len(self.calls),1)

    def test_reference_refresh_after_six_hours_gets_a_new_observation(self):
        rows=[dict(trade_date="20020104",ts_code="BK001",con_code="995001.SZ",name="概念")]
        original=self.reference("dc_member",DAY,"monthly",rows)
        with self.store._connect() as conn:
            conn.execute("UPDATE supplier_market_batches SET collected_at=clock_timestamp()-interval '7 hours' WHERE batch_id=%s",(original["batch_id"],))
            conn.execute("UPDATE supplier_tushare_pages SET collected_at=clock_timestamp()-interval '7 hours' WHERE source_key=%s",(self.client.identity("daily"),))
        refreshed=self.reference("dc_member",DAY,"monthly",rows)
        self.assertNotEqual(original["batch_id"],refreshed["batch_id"])
        self.assertGreater(refreshed["observed_at"],original["observed_at"])
        self.assertEqual(len(self.calls),2)

    def test_daily_updates_refresh_after_six_hours_while_legacy_lr_reuses(self):
        row=dict(ts_code="995001.SZ",trade_date="20020104",open="10",high="13",low="9",close="11",pre_close="10",vol="123",amount="45",pct_chg="10")
        def post(_url,payload,**_kwargs):
            fields=payload["fields"].split(",")
            return dict(code=0,data=dict(fields=fields,items=[[row[f] for f in fields]]))
        def collect():
            result=collect_day(self.store,self.client,"daily",DAY,{},self.request,lambda:None)
            self.batches.add(result["batch"])
            return result
        with patch("modules.strategy_hermes.bulk_eod_collect.post_json",side_effect=post):
            original=collect()
        with self.store._connect() as conn:
            conn.execute("UPDATE supplier_batches SET collected_at=clock_timestamp()-interval '7 hours' WHERE batch_id=%s",(original["batch"],))
            conn.execute("UPDATE supplier_tushare_pages SET collected_at=clock_timestamp()-interval '7 hours' WHERE source_key=%s",(self.client.identity("daily"),))
        legacy=BulkTushare(self.config)
        with patch("modules.strategy_hermes.bulk_eod_collect.post_json",side_effect=AssertionError("legacy LR reuses stored history")):
            reused=collect_day(self.store,legacy,"daily",DAY,{},self.request,lambda:None)
        self.assertEqual(reused["batch"],original["batch"])
        row["close"]="12"
        with patch("modules.strategy_hermes.bulk_eod_collect.post_json",side_effect=post):
            refreshed=collect()
            self.assertEqual(collect()["batch"],refreshed["batch"])
        self.assertNotEqual(original["batch"],refreshed["batch"])
        self.assertEqual(len(self.calls),2)
        with self.store._connect() as conn:
            close,batch=conn.execute("SELECT close,batch_id FROM supplier_daily_bars WHERE code='995001.SZ' AND trade_date=%s",(DAY,)).fetchone()
            manifest=conn.execute("SELECT batch_id FROM supplier_tushare_eod_runs WHERE source_key=%s AND api='daily'",(self.client.identity("daily"),)).fetchone()[0]
        self.assertEqual((str(close),batch,manifest),("12",refreshed["batch"],refreshed["batch"]))

    def test_reference_pagination_rejects_repeated_pages(self):
        repeated=[dict(ts_code=f"{990000+i:06d}.SZ",name="行业",l1_name="一级",l2_name="二级",in_date=None,out_date=None) for i in range(2000)]
        def post(_url,payload,**_kwargs):
            fields=payload["fields"].split(",")
            return dict(code=0,data=dict(fields=fields,items=[[r.get(f) for f in fields] for r in repeated]))
        with patch("modules.strategy_hermes.data_updates_collect.post_json",side_effect=post):
            with self.assertRaisesRegex(ValueError,"SOURCE_PAGINATION_STALLED"):
                reference_pages(self.store,self.client,"index_member_all",dict(is_new="Y"),self.request,lambda:None)
        self.assertEqual([scope["offset"] for _,scope in self.calls],[0,2000])

    def test_daily_and_weekly_use_original_fields_units_and_no_implicit_warmup(self):
        seed="calendar-"+uuid4().hex;self.batches.add(seed)
        monday=DAY-timedelta(days=DAY.weekday())
        with self.store._connect() as conn,conn.transaction():
            conn.execute("INSERT INTO supplier_batches VALUES(%s,'trade_cal','{}',%s,7,'[]','[]')",(seed,STAMP))
            for n in range(7):conn.execute("INSERT INTO supplier_trade_calendar VALUES('SSE',%s,%s,%s)",(monday+timedelta(days=n),n<5,seed))
        def post(_url,payload,**_kwargs):
            scope=payload["params"];self.assertEqual(scope["trade_date"],"20020104")
            row=dict(ts_code="995001.SZ",trade_date="20020104",open="10",high="12",low="9",close="11",pre_close="10",vol="123",amount="45",pct_chg="0.1",adj_factor="1",turnover_rate="2",volume_ratio="1",circ_mv="100",winner_rate="75",cost_50pct="10",cost_85pct="12")
            fields=payload["fields"].split(",")
            return dict(code=0,data=dict(fields=fields,items=[[row[f] for f in fields]]))
        with patch("modules.strategy_hermes.bulk_eod_collect.post_json",side_effect=post):
            daily=execute_update(self.store,body("daily"),self.config,None,self.request,lambda:None,self.record)
            weekly=execute_update(self.store,body("weekly",calendar={str(DAY):False}),self.config,None,self.request,lambda:None,self.record)
        self.assertEqual((daily["completed"],weekly["completed"],len(self.calls)),(4,1,5),self.records)
        with self.store._connect() as conn:
            features={api:json.loads(raw) for api,raw in conn.execute("SELECT api,body FROM supplier_lr_features WHERE batch_id=ANY(%s)",(list(self.batches),))}
        self.assertEqual(features["weekly"]["volume_shares"],"123")
        self.assertEqual(features["weekly"]["amount_cny"],"45")
        self.assertEqual(features["weekly"]["return_ratio"],"0.1")
        self.assertEqual(features["cyq_perf"]["winner_ratio"],"0.75")
        self.assertEqual(features["cyq_perf"]["cost50"],"10")

    def test_weekly_can_use_planner_frozen_calendar_on_empty_database(self):
        day=date(2003,1,10);monday=day-timedelta(days=day.weekday())
        calendar={str(monday+timedelta(days=n)):n<5 for n in range(7)}
        def post(_url,payload,**_kwargs):
            row=dict(ts_code="995001.SZ",trade_date="20030110",open="10",high="12",low="9",close="11",vol="123",amount="45",pct_chg="0.1")
            fields=payload["fields"].split(",")
            return dict(code=0,data=dict(fields=fields,items=[[row[f] for f in fields]]))
        with patch("modules.strategy_hermes.bulk_eod_collect.post_json",side_effect=post):
            result=execute_update(self.store,body("weekly",weekly_dates=[str(day)],calendar=calendar),self.config,None,self.request,lambda:None,self.record)
        self.assertEqual((result["completed"],result["failed"]),(1,0),self.records)
        original=self.records[-1][2]
        with self.store._connect() as conn:
            conn.execute("UPDATE supplier_market_batches SET collected_at=clock_timestamp()-interval '7 hours' WHERE batch_id=%s",(original["batch_id"],))
            conn.execute("UPDATE supplier_tushare_pages SET collected_at=clock_timestamp()-interval '7 hours' WHERE source_key=%s",(self.client.identity("weekly"),))
        with patch("modules.strategy_hermes.bulk_eod_collect.post_json",side_effect=post):
            refreshed=execute_update(self.store,body("weekly",weekly_dates=[str(day)],calendar=calendar),self.config,None,self.request,lambda:None,self.record)
        self.assertEqual((refreshed["completed"],refreshed["failed"],len(self.calls)),(1,0,2),self.records)
        self.assertNotEqual(self.records[-1][2]["batch_id"],original["batch_id"])

    def test_yearly_financial_batch_preserves_percent_units_and_recompute_date(self):
        seed="daily-"+uuid4().hex;self.batches.add(seed)
        with self.store._connect() as conn,conn.transaction():
            conn.execute("INSERT INTO supplier_batches VALUES(%s,'daily','{}',%s,1,'[]','[]')",(seed,STAMP))
            conn.execute("INSERT INTO supplier_daily_bars VALUES('995001.SZ',%s,10,12,9,11,10,100,1000,0.1,%s)",(DAY,seed))
            expected=conn.execute("SELECT max(trade_date) FROM supplier_daily_bars").fetchone()[0].isoformat()
        def post(_url,payload,**_kwargs):
            self.assertEqual(payload["api_name"],"fina_indicator_vip")
            self.assertEqual(payload["params"],dict(period="20251231",limit=4000,offset=0))
            rows=[dict(ts_code="995001.SZ",ann_date="20260320",end_date="20251231",roe="12.3",debt_to_assets="42.1")]
            fields=payload["fields"].split(",")
            return dict(code=0,data=dict(fields=fields,items=[[row[field] for field in fields] for row in rows]))
        with patch("modules.strategy_hermes.data_updates_collect.post_json",side_effect=post):
            result=execute_update(self.store,body("yearly"),self.config,None,self.request,lambda:None,self.record)
        self.assertEqual(result,dict(completed=1,failed=0,recompute_date=expected),self.records)
        self.assertEqual([r[2]["status"] for r in self.records],["RUNNING","OK"])
        with self.store._connect() as conn:
            row=json.loads(conn.execute("SELECT body FROM supplier_reference_rows WHERE dataset='fina_indicator' AND batch_id=ANY(%s)",(list(self.batches),)).fetchone()[0])
        self.assertEqual((row["roe"],row["debt_to_assets"],row["ratio_unit"]),("12.3","42.1","percent"))

    def test_one_reference_failure_records_failure_and_other_datasets_continue(self):
        with patch("modules.strategy_hermes.data_updates_collect.collect_reference",side_effect=[ValueError("SOURCE_STOCK_NAME_MISSING"),dict(status="OK",row_count=1),dict(status="OK",row_count=1)]):
            result=execute_update(self.store,body("basic"),self.config,None,self.request,lambda:None,self.record)
        self.assertEqual((result["completed"],result["failed"]),(2,1))
        self.assertEqual(self.records[1][2]["error_code"],"SOURCE_STOCK_NAME_MISSING")
