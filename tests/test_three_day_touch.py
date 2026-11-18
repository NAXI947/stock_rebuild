import copy
import unittest

from modules.strategy_hermes.three_day_touch import SCHEMA, evaluate
from test_stage1 import D1, D2, D3, D4, DAYS, ETF, ETF2, stamp


def fixture():
    prices = [
        dict(etf=code,trade_date=day,available_at=stamp(day,"15:10"),price_basis="UNADJUSTED",
             source="frozen:daily:"+code+":"+day,open_cny="10.000",high_cny=high)
        for code, highs in [(ETF,["10.010","10.000","10.000"]),(ETF2,["10.000"]*3)]
        for day,high in zip((D2,D3,D4),highs)
    ]
    rows = [
        dict(report_id="report-a",original_signal="加强×2",etf=code,published_at=stamp(D1,"14:30"),sources=[sector])
        for code,sector in [(ETF,"kaipanla:A"),(ETF,"kaipanla:B"),(ETF2,"kaipanla:A")]
    ]
    return dict(schema_version=SCHEMA,as_of=stamp(D4,"16:00"),calendar=DAYS,prices=prices,samples=rows)


class ThreeDayTouch(unittest.TestCase):
    def test_deduplication_next_trading_day_and_strict_touch(self):
        result = evaluate(fixture())
        self.assertEqual(len(result["samples"]),2)
        touched = next(row for row in result["samples"] if row["etf"] == ETF)
        self.assertEqual(touched["window"],[D2,D3,D4])
        self.assertEqual((touched["status"],touched["touch_day"]),("TOUCHED",D2))
        self.assertEqual(touched["sources"],["kaipanla:A","kaipanla:B"])
        self.assertEqual(result["by_original_signal"]["加强×2"]["touch_pct"],"50.0000")

    def test_wait_window_and_missing_price_do_not_enter_denominator(self):
        early = fixture()
        early["as_of"] = stamp(D4,"14:45")
        self.assertEqual(evaluate(early)["by_original_signal"]["加强×2"]["wait_window"],2)
        delayed = copy.deepcopy(fixture())
        for price in delayed["prices"]:
            if price["etf"] == ETF and price["trade_date"] == D4:
                price["available_at"] = stamp(D4,"16:01")
        stats = evaluate(delayed)["by_original_signal"]["加强×2"]
        self.assertEqual((stats["valid"],stats["missing_price"],stats["touch_pct"]),(1,1,"0.0000"))
