from datetime import datetime
from unittest import TestCase
from modules.strategy_hermes.stage2.market_bootstrap import normalize_tickflow
from modules.strategy_hermes.stage2.notifications import _etf_sections

class HybridPresentation(TestCase):
    def test_execution_failure_does_not_appear_as_confirmed_qq_recommendation(self):
        sid="kaipanla:801027"
        report={"input_meta":{"etf_recommendations":{"sectors":{sid:{"products":[
            {"etf":"512800.SH","recommendation_status":"CONFIRMED","execution_status":"FAILED","representative_current":True},
            {"etf":"512480.SH","recommendation_status":"PENDING","execution_status":"PARTIAL"}
        ]}}}}}
        candidates, references, blocked=_etf_sections(report,[sid],{sid:"银行"})
        self.assertEqual(candidates,[])
        self.assertIn("待核验",references[0])
        self.assertIn("交易条件未通过",blocked[0])
    def test_optional_verification_is_visible_for_confirmed_representative(self):
        sid="kaipanla:801027"
        report={"input_meta":{"etf_recommendations":{"sectors":{sid:{"products":[
            {"etf":"512800.SH","recommendation_status":"CONFIRMED","execution_status":"PARTIAL","representative_current":True}
        ]}}}}}
        candidates, references, blocked=_etf_sections(report,[sid],{sid:"银行"})
        self.assertIn("交易条件部分未核验",candidates[0])
        self.assertEqual(references+blocked,[])
    def test_quote_units_range_and_suspension_survive_normalization(self):
        now=datetime.fromisoformat("2026-11-13T14:05:00+08:00")
        quote={"symbol":"512800.SH","timestamp":int(now.timestamp()*1000),"last_price":"1.2","prev_close":"1.1",
               "volume":"100","amount":"12000","high":"1.21","low":"1.1","trading_status":"suspended"}
        rows,issues=normalize_tickflow({"data":[quote]},None,now)
        row=rows[0]
        self.assertEqual((row["volume_shares"],row["amount_cny"]),("10000","12000"))
        self.assertEqual((row["high_cny"],row["low_cny"],row["trading_status"]),("1.21","1.1","SUSPENDED"))
        self.assertTrue(row["volume_unit_verified"] and row["amount_unit_verified"])
        self.assertEqual(issues,{})
