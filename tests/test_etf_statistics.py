from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from decimal import Decimal

from modules.strategy_hermes.simulator import CHINA
from modules.strategy_hermes.stage2 import etf_statistics as stats
from modules.strategy_hermes.stage2.tushare_bootstrap import SourceFailure

CODE = "512800.SH"
DAYS = [(date(2026,10,1)+timedelta(days=i)).isoformat() for i in range(21)]
AT = datetime(2026,10,22,10,35,tzinfo=CHINA)
AVAILABLE = "2026-10-21T16:00:00+08:00"


def inputs():
    daily = [dict(code=CODE,trade_date=day,close_cny=str(i+1),amount_cny="40000000",
                  available_at=AVAILABLE,batch="daily") for i,day in enumerate(DAYS)]
    factors = [dict(code=CODE,trade_date=day,adj_factor="1",
                    available_at=AVAILABLE,batch="adj") for day in DAYS]
    return daily,factors


class EtfStatistics(TestCase):
    def test_unverified_catalog_product_can_collect_research_without_execution_grant(self):
        from modules.strategy_hermes.stage2.penetration import instrument_units
        mapping = {'products': {CODE: dict(index_code='399986.SZ', list_status='L',
                    rule=dict(verified=False, reason='PRODUCT_REFERENCE_REQUIRES_VERIFICATION'))}}
        self.assertEqual(instrument_units(mapping, AT), {})
        operations = []
        class Budget:
            new_calls = 0
            def call(self, source, scope, operation, *, limit):
                operations.append((source, scope, limit))
                return operation()
        def request(source, scope, operation):
            return dict(status='SAVED', receipt=dict(status='STORED', batch=source, rows=len(DAYS)))
        with patch.object(stats.Path, 'is_file', return_value=True), \
             patch.object(stats, '_stored_window', return_value={CODE:{}}), \
             patch.object(stats, 'RequestLog', return_value=Budget()):
            result = stats.refresh(SimpleNamespace(ns='test'), 'private', mapping, DAYS+['2026-10-22'],
                AT.replace(hour=10, minute=0), symbols=[CODE], request=request)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(len(operations), 2)
        self.assertTrue(all(limit == stats.MAX_PRODUCTS for _, _, limit in operations))
        self.assertEqual(instrument_units(mapping, AT), {})

    def test_research_target_rotation_uses_unverified_catalog_with_same_batch_limit(self):
        from contextlib import nullcontext
        codes = [f'51200{i}.SH' for i in range(8)]
        mapping = {'products': {code:dict(index_code='index-'+str(i), rule=dict(verified=False))
                                for i,code in enumerate(codes)}}
        def execute(sql, params=None):
            return SimpleNamespace(fetchall=lambda:[])
        conn = SimpleNamespace(transaction=lambda:nullcontext(), execute=execute)
        ledger = SimpleNamespace(ns='test', _connect=lambda:nullcontext(conn))
        with patch.object(stats, '_stored_window', return_value={code:{} for code in codes}):
            targets = stats.select_targets(ledger, mapping, DAYS+['2026-10-22'], AT, limit=3)
        self.assertEqual(len(targets), 3)
        self.assertTrue(set(targets).issubset(codes))

    def test_exact_completed_window_and_factor_basis(self):
        daily,factors=inputs()
        result=stats.derive(CODE,daily,factors,DAYS,AT)
        self.assertEqual(result["adv20"],"40000000")
        self.assertEqual(result["adv5"],"40000000")
        self.assertEqual(result["ma5_raw_prev5"],"19")
        self.assertEqual(result["prior_close_cny"],"21")
        self.assertTrue(result["complete20"])
        self.assertEqual(result["history_days"],20)
        self.assertEqual(result["ma5_basis_status"],"VERIFIED")
        self.assertEqual(result["history_available_at"],AVAILABLE)
        self.assertEqual(result["history_batches"],["adj","daily"])

    def test_future_available_current_day_and_missing_rows_do_not_fill_history(self):
        daily,factors=inputs()
        incomplete=stats.derive(CODE,daily[:-1],factors,DAYS,AT)
        self.assertIsNone(incomplete["adv20"])
        self.assertIsNone(incomplete["adv5"])
        self.assertIsNone(incomplete["ma5_raw_prev5"])
        future=deepcopy(daily)
        future[-1]["available_at"]="2026-10-22T11:00:00+08:00"
        self.assertIsNone(stats.derive(CODE,future,factors,DAYS,AT)["adv20"])
        current=deepcopy(daily)
        current.append(dict(current[-1],trade_date="2026-10-22",close_cny="999"))
        result=stats.derive(CODE,current,factors,DAYS+["2026-10-22"],AT)
        self.assertEqual(result["ma5_raw_prev5"],"19")
        current[-2]["available_at"]="2026-10-21T14:00:00+08:00"
        self.assertIsNone(stats.derive(CODE,current,factors,DAYS,AT)["ma5_raw_prev5"])

    def test_factors_missing_changed_and_future_are_distinct(self):
        daily,factors=inputs()
        self.assertEqual(stats.derive(CODE,daily,[],DAYS,AT)["ma5_basis_status"],"UNVERIFIED")
        factors[-1]["adj_factor"]="1.01"
        self.assertEqual(stats.derive(CODE,daily,factors,DAYS,AT)["ma5_basis_status"],"CORP_ACTION_WINDOW")
        factors[-1]["available_at"]="2026-10-22T11:00:00+08:00"
        self.assertEqual(stats.derive(CODE,daily,factors,DAYS,AT)["ma5_basis_status"],"UNVERIFIED")

    def test_normalization_uses_supplier_units_once_and_rejects_bad_identity(self):
        row=dict(ts_code=CODE,trade_date="20261021",open="10",high="11",low="9",close="10",
                 pre_close="10",vol="100",amount="1000")
        result=stats.normalize_daily([row],CODE,{date(2026,10,21)})
        self.assertEqual(result[0]["amount_cny"],"1000000")
        self.assertEqual(result[0]["volume_units"],"10000")
        self.assertEqual(stats.normalize_adjustment([dict(ts_code=CODE,trade_date="20261021",adj_factor="1")],
                          CODE,{date(2026,10,21)})[0]["adj_factor"],"1")
        for invalid in ([row,row],[dict(row,vol="-1")],[dict(row,ts_code="512480.SH")]):
            with self.subTest(invalid=invalid),self.assertRaises(SourceFailure):
                stats.normalize_daily(invalid,CODE,{date(2026,10,21)})

    def test_refresh_one_completed_date_range_per_source_in_bounded_window(self):
        now=AT.replace(hour=10,minute=0)
        mapping={"products":{CODE:{}}}
        budget=SimpleNamespace(new_calls=0)
        calls=[]
        def bounded(source,scope,operation,*,limit):
            calls.append((source,scope,limit))
            return {"status":"STORED","batch":"b","rows":len(DAYS)}
        budget.call=bounded
        with patch.object(stats,"research_products",return_value={CODE:100}),patch.object(stats.Path,"is_file",return_value=True),\
             patch.object(stats,"RequestLog",return_value=budget) as factory, \
             patch.object(stats,"select_targets",return_value=[CODE]), \
             patch.object(stats,"_stored_window",return_value={CODE:{}}):
            self.assertEqual(stats.refresh(None,"private",mapping,DAYS+["2026-10-22"],AT)["status"],"OUTSIDE_WINDOW")
            factory.assert_not_called()
            result=stats.refresh(SimpleNamespace(ns="test"),"private",mapping,DAYS+["2026-10-22"],now)
        self.assertEqual(result["status"],"READY")
        self.assertEqual(len(calls),2)
        self.assertEqual({call[0] for call in calls},{"tushare:etf_statistics_daily","tushare:etf_statistics_adj"})
        self.assertTrue(all(call[1]["ts_code"]==CODE and call[1]["start_date"]=="20261001" and call[1]["end_date"]=="20261021" and call[2]==stats.MAX_PRODUCTS for call in calls))


    def test_retired_poller_cannot_bypass_unified_queue_in_any_trigger_mode(self):
        with patch.object(stats,'refresh') as refresh:
            with self.assertRaisesRegex(ValueError,'POLLING_RETIRED_USE_MAINTENANCE_QUEUE'):
                stats.StatisticsRefresh(object(),'config','calendar','mapping')
            refresh.assert_not_called()

    def test_provider_failure_retains_typed_safe_details_without_source_text(self):
        class Budget:
            new_calls=0
            def call(self,source,scope,operation,*,limit):return operation()
        response=dict(status='FAILED',error_code='SourceFailure',error_kind='HTTP_STATUS',
                      error_details=dict(http_status=503,url='https://private.invalid/token'))
        with patch.object(stats,'research_products',return_value={CODE:100}), \
             patch.object(stats.Path,'is_file',return_value=True), \
             patch.object(stats,'_stored_window',return_value={CODE:{}}), \
             patch.object(stats,'RequestLog',return_value=Budget()):
            result=stats.refresh(SimpleNamespace(ns='test'),'private',{'products':{}},DAYS+['2026-10-22'],
                AT.replace(hour=10,minute=0),symbols=[CODE],request=lambda *args:response)
        self.assertEqual(result['status'],'PARTIAL')
        for item in result['items']:
            self.assertEqual(item['error_kind'],'HTTP_STATUS')
            self.assertEqual(item['error_details'],{'http_status':503})
            self.assertNotIn('reason',item)
        self.assertNotIn('private.invalid',str(result))

    def test_only_two_maintenance_opportunities_and_trading_calendar(self):
        calendar = DAYS + ["2026-10-22"]
        for hour,minute,expected in ((8,45,None),(9,35,None),(10,0,"market"),(10,14,"market"),
                                     (10,15,None),(14,5,None),(14,15,"retry"),(14,26,"retry"),
                                     (14,27,None),(14,30,None),(18,0,None),(21,0,None)):
            self.assertEqual(stats.maintenance_attempt(calendar,AT.replace(hour=hour,minute=minute)),expected)
        self.assertIsNone(stats.maintenance_attempt(DAYS,AT.replace(hour=10,minute=0)))

    def test_complete_window_is_reused_and_does_not_consume_external_budget(self):
        existing={endpoint:dict(status="ALREADY_STORED",batch=endpoint,rows=20,complete=True)
                  for endpoint in (stats.DAILY,stats.ADJUSTMENT)}
        with patch.object(stats,"research_products",return_value={CODE:100}), \
             patch.object(stats.Path,"is_file",return_value=True), \
             patch.object(stats,"_stored_window",return_value={CODE:existing}), \
             patch.object(stats,"RequestLog") as factory:
            factory.return_value.new_calls=0
            result=stats.refresh(SimpleNamespace(ns="test"),"private",{"products":{}},DAYS+["2026-10-22"],
                                 AT.replace(hour=14,minute=15),symbols=[CODE],attempt="retry",request=lambda *a:self.fail("HTTP"))
        self.assertEqual(result["status"],"READY")
        self.assertEqual(result["new_requests"],0)
        factory.return_value.call.assert_not_called()

    def test_failed_market_can_retry_once_but_duplicate_retry_is_bounded(self):
        history={}
        operations=[]
        class Budget:
            def __init__(self,ledger,key):self.key,self.new_calls=key,0
            def call(self,source,scope,operation,*,limit):
                key=(self.key,source,str(scope))
                if key in history:
                    if history[key] is None:raise stats.ProbeError("already attempted")
                    return history[key]
                history[key]=None
                self.new_calls+=1
                value=operation();history[key]=value
                return value
        def request(source,scope,operation):
            operations.append((source,scope["attempt"]))
            if scope["attempt"]=="market":return dict(status="FAILED",error_code="TEMPORARY")
            return dict(status="SAVED",receipt=dict(status="STORED",batch="b",rows=len(DAYS)))
        with patch.object(stats,"research_products",return_value={CODE:100}), \
             patch.object(stats.Path,"is_file",return_value=True), \
             patch.object(stats,"_stored_window",return_value={CODE:{}}),patch.object(stats,"RequestLog",Budget):
            run=lambda hour:stats.refresh(SimpleNamespace(ns="test"),"private",{"products":{}},
                       DAYS+["2026-10-22"],AT.replace(hour=hour,minute=0 if hour==10 else 15),symbols=[CODE],request=request)
            self.assertEqual(run(10)["status"],"PARTIAL")
            self.assertEqual(run(10)["new_requests"],0)
            self.assertEqual(run(14)["status"],"READY")
            self.assertEqual(run(14)["new_requests"],0)
        self.assertEqual(len(operations),4)

    def test_expanded_universe_targets_include_unheld_products_after_first_three(self):
        from contextlib import nullcontext
        from json import dumps
        codes=[f"51200{i}.SH" for i in range(8)]
        def execute(sql,params=None):
            if "SELECT etf FROM cycles" in sql:
                self.assertEqual(params[1],["OPEN","WAIT_BUY","WAIT_EXIT"])
                return SimpleNamespace(fetchall=lambda:[(codes[0],)])
            if "SELECT payload FROM events" in sql:
                return SimpleNamespace(fetchall=lambda:[(dumps({"candidates":{codes[1]:[]}}),),
                                                        (dumps({"candidates":{codes[2]:[]}}),)])
            return SimpleNamespace(fetchall=lambda:[])
        conn=SimpleNamespace(transaction=lambda:nullcontext(),execute=execute)
        ledger=SimpleNamespace(ns="test",_connect=lambda:nullcontext(conn))
        with patch.object(stats,"research_products",return_value=dict.fromkeys(codes,100)), \
             patch.object(stats,"_stored_window",return_value={code:{} for code in codes}):
            a=stats.select_targets(ledger,{"products":{}},DAYS+["2026-10-22"],AT.replace(hour=10,minute=0),limit=3)
            b=stats.select_targets(ledger,{"products":{}},DAYS+["2026-10-22","2026-10-23"],
                                   AT.replace(day=23,hour=10,minute=0),limit=3)
        self.assertEqual(a[0],codes[0])
        self.assertEqual(a[1],codes[1])
        self.assertNotEqual(a[-1],b[-1])
        self.assertTrue(set(a+b)-set(codes[:3]))
        with patch.object(stats,"research_products",return_value=dict.fromkeys(codes,100)), \
             patch.object(stats,"_stored_window",return_value={code:{} for code in codes}):
            full=stats.select_targets(ledger,{"products":{}},DAYS+["2026-10-22"],AT.replace(hour=10,minute=0),limit=8)
        self.assertEqual(full[0],codes[0])
        self.assertEqual(set(full[1:3]),{codes[1],codes[2]})

    def test_overlay_preserves_original_candidate_statistics(self):
        from contextlib import nullcontext
        from json import dumps
        daily,factors=inputs()
        records=[
            (stats.DAILY,dumps({"rows":daily}),datetime.fromisoformat(AVAILABLE),"daily"),
            (stats.ADJUSTMENT,dumps({"rows":factors}),datetime.fromisoformat(AVAILABLE),"adj"),
        ]
        conn = SimpleNamespace(transaction=lambda:nullcontext(),
                               execute=lambda *a: SimpleNamespace(fetchall=lambda:records))
        ledger=SimpleNamespace(_connect=lambda:nullcontext(conn))
        original={"products":{CODE:{"stats":{"adv20":"7","aum_yuan":"200000000"}}}}
        enriched=stats.overlay_mapping(ledger,original,DAYS,AT)
        self.assertEqual(original["products"][CODE]["stats"]["adv20"],"7")
        self.assertEqual(enriched["products"][CODE]["stats"]["adv20"],"7")
        self.assertEqual(enriched["products"][CODE]["recommendation_stats"]["adv20"],"40000000")
        self.assertNotIn("recommendation_stats",original["products"][CODE])
