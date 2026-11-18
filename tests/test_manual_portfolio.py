import unittest

from modules.strategy_hermes.manual_portfolio import SCHEMA, replay
from test_stage1 import D1, D2, D3, D4, DAYS, ETF, ETF2, quote, stamp


def node(name, etf, day, hour, action, mode, value=None):
    return dict(id=name, etf=etf, at=stamp(day, hour), action=action, size_mode=mode, size=value)


def scenario(nodes, quotes, **overrides):
    return dict(schema_version=SCHEMA, calendar=DAYS, cutoff=stamp(D3, "15:00"),
                instruments={ETF:100, ETF2:100}, nodes=nodes, quotes=quotes, **overrides)


class ManualPortfolio(unittest.TestCase):
    def test_v14_reference_return_with_assumed_prices(self):
        nodes = [
            node("buy",ETF,D1,"09:35","BUY","SHARES",5000),
            node("add",ETF,D2,"09:35","ADD","SHARES",4000),
            node("reduce",ETF,D2,"10:35","REDUCE","SHARES",4500),
            node("sell",ETF,D3,"09:35","SELL","ALL"),
        ]
        quotes = [
            quote("q1",D1,"09:36","1"),
            quote("q2",D2,"09:36","1.1"),
            quote("q3",D2,"10:36","1.2"),
            quote("q4",D3,"09:36","1.1"),
        ]
        for item in quotes:
            item.update(price_kind="ASSUMED",source="frozen:assumption")
        result = replay(scenario(nodes, quotes, price_mode="ASSUMPTION"))
        self.assertEqual((result["cash"],result["profit"],result["return_pct"]),("10950.00","950.00","9.5000"))
        self.assertEqual([item["quantity"] for item in result["fills"]],[5000,4000,4500,4500])
        self.assertEqual(result["price_mode"],"ASSUMPTION")
        with self.assertRaisesRegex(ValueError,"assumed quote"):
            replay(scenario(nodes,quotes))

    def test_shared_cash_and_partial_t1_reuse_proceeds(self):
        nodes = [
            node("buy",ETF,D1,"09:35","BUY","SHARES",200),
            node("add",ETF,D2,"09:35","ADD","SHARES",800),
            node("reduce",ETF,D2,"10:35","REDUCE","POSITION_RATIO","0.50"),
            node("buy-other",ETF2,D3,"10:35","BUY","CASH_RATIO","0.50"),
        ]
        quotes = [
            quote("buy",D1,"09:36","10"),quote("add",D2,"09:36","10"),
            quote("same-day",D2,"10:36","11"),quote("next-day",D3,"09:36","12"),
            quote("other",D3,"10:36","10",code=ETF2),
        ]
        result = replay(scenario(nodes,quotes))
        reduce = result["nodes"][2]
        self.assertEqual((reduce["target_quantity"],reduce["sellable_at_intent"]),(500,200))
        self.assertEqual([(x["quantity"],x["deferred_t1"]) for x in reduce["fills"]],[(200,False),(300,True)])
        self.assertEqual(result["nodes"][3]["cash_at_intent"],"5800.00")
        self.assertEqual(result["fills"][-1]["quantity"],200)
        self.assertEqual((result["cash"],result["profit"],result["realized"],result["unrealized"]),
                         ("3800.00","1800.00","800.00","1000.00"))

    def test_conflicts_and_missing_prices_keep_the_draft(self):
        nodes = [
            node("buy",ETF,D1,"09:35","BUY","AMOUNT","9000"),
            node("overspend",ETF2,D1,"10:35","BUY","AMOUNT","2000"),
            node("wait",ETF,D1,"11:15","REDUCE","SHARES",500),
            node("oversell",ETF,D1,"11:25","REDUCE","SHARES",500),
            node("missing",ETF2,D1,"13:05","BUY","AMOUNT","1000"),
        ]
        result = replay(scenario(nodes,[quote("buy",D1,"09:36","10")]))
        self.assertEqual([item.get("reason") for item in result["nodes"]],
                         [None,"INSUFFICIENT_CASH","WAIT_PRICE","INSUFFICIENT_UNRESERVED_POSITION","WAIT_PRICE"])
        self.assertEqual(result["status"],"CONFLICT")
        self.assertFalse(result["comparable"])
        self.assertEqual(result["cash"],"1000.00")

    def test_exit_supersedes_pending_sell_and_ignores_future_revision(self):
        nodes = [
            node("buy",ETF,D1,"09:35","BUY","SHARES",1000),
            node("reduce",ETF,D1,"10:35","REDUCE","SHARES",500),
            node("exit",ETF,D1,"13:05","SELL","ALL"),
        ]
        quotes = [
            quote("buy",D1,"09:36","10"),
            quote("same",D1,"13:06","11"),
            quote("exit",D2,"09:31","12"),
            quote("late-correction",D2,"09:31","999",received=stamp(D4,"09:32"),revision=1),
        ]
        result = replay(scenario(nodes,quotes))
        self.assertEqual(result["nodes"][1]["status"],"SUPERSEDED")
        self.assertEqual(result["nodes"][2]["fills"][0]["quantity"],1000)
        self.assertEqual(result["profit"],"2000.00")


    def test_dividend_tracks_record_close_receivable_and_paid_cash(self):
        nodes = [node("buy", ETF, D1, "09:35", "BUY", "SHARES", 1000),
                 node("exit", ETF, D2, "09:35", "SELL", "ALL")]
        quotes = [quote("q1", D1, "09:36", "10"), quote("ex", D2, "09:36", "9.50")]
        event = dict(id="div1", etf=ETF, kind="CASH_DIVIDEND", source="frozen:issuer-announcement",
                     published_at=stamp(D1, "08:30"), record_date=D1, ex_date=D2, pay_date=D3,
                     cash_per_share_cny="0.50")
        bundle = scenario(nodes, quotes, corporate_actions=[event])
        waiting = replay(dict(bundle, cutoff=stamp(D2, "15:00")))
        self.assertEqual((waiting["cash"], waiting["distribution_receivable_cny"], waiting["equity"]),
                         ("9500.00", "500.00", "10000.00"))
        self.assertEqual(waiting["corporate_actions"][0]["status"], "RECEIVABLE")
        paid = replay(bundle)
        self.assertEqual((paid["cash"], paid["profit"], paid["realized"]), ("10000.00", "0.00", "0.00"))
        self.assertEqual(paid["corporate_actions"][0]["status"], "PAID")
        self.assertEqual(paid["fills"][-1]["price"], "9.50")
        self.assertEqual(paid["rules"]["version"], "manual_portfolio_v4")

    def test_split_preserves_fifo_basis_and_rejects_preconversion_pending_order(self):
        nodes = [node("buy", ETF, D1, "09:35", "BUY", "SHARES", 1000),
                 node("old-order", ETF, D1, "13:35", "SELL", "ALL"),
                 node("after", ETF, D2, "09:35", "SELL", "ALL")]
        quotes = [quote("q1", D1, "09:36", "10"), quote("new-units", D2, "09:36", "5.10"),
                  quote("delayed-old", D1, "14:30", "10", received=stamp(D2, "09:31"))]
        event = dict(id="split1", etf=ETF, kind="SHARE_SPLIT", source="frozen:issuer-announcement",
                     published_at=stamp(D1, "17:00"), effective_date=D2, numerator=2, denominator=1)
        result = replay(scenario(nodes, quotes, corporate_actions=[event]))
        self.assertEqual(result["nodes"][1]["reason"], "UNIT_CONVERSION_REQUIRES_NEW_ORDER")
        self.assertIsNone(result["nodes"][2]["reference_quote"])
        self.assertEqual(result["corporate_actions"][0]["shares_after"], 2000)
        self.assertEqual((result["fills"][-1]["basis"], result["fills"][-1]["quantity"]), ("10000.00", 2000))
        with self.assertRaisesRegex(ValueError, "fractional lots"):
            replay(scenario(nodes, quotes, corporate_actions=[dict(event, numerator=1, denominator=3)]))
