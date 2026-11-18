import copy
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from modules.strategy_hermes.adapter import normalize
from modules.strategy_hermes.ledger import Ledger
from modules.strategy_hermes.signal_cycle import apply_report, create_cycle, fill
from modules.strategy_hermes.simulator import Calendar, Rules, buy, new_book, sell, valuation

D1, D2, D3, D4 = "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"
DAYS=[D1,D2,D3,D4]
A,B="kaipanla:A","kaipanla:B"
ETF,ETF2="510001.SH","159001.SZ"


def stamp(day,hour):
    return f"{day}T{hour}:00+08:00"


def report(id,day,hour,states=(),selected=(),*,evidence=None,coverage="0.60",published=None):
    rows=[]
    evidence=evidence or id
    for sid,state in states:
        signal={"新增":"STRENGTHENING","加强":"STRENGTHENING","维持":"STABLE",
                "减弱":"WEAKENING","撤回":"WEAKENING","退潮":"WEAKENING","本轮未覆盖":"UNCERTAIN"}[state]
        rows.append(dict(kaipanla_plate_id=sid.split(":")[1], sector_name=sid,
              observation_state="撤回" if state=="退潮" else state,signal=signal,risk_level="MEDIUM",
              reasons=["尚未出现退潮"] if state=="维持" else ["冻结示例"],
              risks=[],sources=[f"frozen:{evidence}:{sid}"],evidence_status="PARTIAL",
              support_withdrawn=state in ("撤回","退潮"),validation_status="VERIFIED",
              identity_status="FROZEN_VERIFIED",evidence_valid=True))
    document=dict(schema_version="hermes_sector_rotation_v1",report_id=id,trade_date=day,
                  generated_at=stamp(day,hour),data_as_of=stamp(day,"09:34"),expires_at=stamp(day,"15:00"),
                  status="PARTIAL",summary="冻结样本",warnings=["IOPV_MISSING","CHAIN_GAP"],
                  sectors=rows,research_context={"evidence_key":evidence},coverage_ratio=coverage)
    event=dict(kind="report",report_json=json.dumps(document,ensure_ascii=False,indent=2)+"\n",
        candidates=[dict(etf=c,sector_id=s,rank=rank,mapping_ref="frozen:penetration:v1") for c,s,rank in selected],
        judgments=[dict(sector_id=s,phase="RETREAT",evidence_ref=f"frozen:{evidence}:{s}")
                   for s,phase in states if phase=="退潮"])
    if published:
        event["published_at"]=stamp(day,published)
    return event


def quote(id,day,hour,price,*,code=ETF,tradeable=True,received=None,revision=0):
    return dict(kind="quote",id=id,etf=code,observed_at=stamp(day,hour),
                received_at=received or stamp(day,hour),price_cny=price,source="frozen:test",
                tradeable=tradeable,revision=revision)


def bundle(events=(),ns="stage1:test",rules=None):
    b=dict(schema="stock-v4-frozen-replay-v1",input_kind="FROZEN",namespace=ns,
           calendar=DAYS,instruments={ETF:100,ETF2:100},events=list(events))
    if rules is not None:
        b["rules"]=rules
    return b


def start():
    return [report("entry",D1,"09:35",[(A,"新增")],[(ETF,A,1)]),quote("entry-price",D1,"09:36","10.00")]


class StageOne(unittest.TestCase):
    def setUp(self):
        self.runtime=TemporaryDirectory(dir=Path(os.environ["STAGE1_TEST_ROOT"]))
        self.addCleanup(self.runtime.cleanup)
        self.home=Path(self.runtime.name)
        self.state=self.home/"ledger"
        self.tmp=self.home/"tmp"
        self.state.mkdir(mode=0o700)
        self.tmp.mkdir(mode=0o700)
        self.ledger=Ledger(self.state)

    def replay(self, events, **kw):
        return self.ledger.replay(bundle(events,**kw))

    def test_b01_single_reduce_then_exit(self):
        result=self.replay(start()+[
            report("weak",D2,"09:35",[(A,"减弱")]),quote("sell1",D2,"09:36","11"),
            report("recover",D2,"10:35",[(A,"加强")],[(ETF,A,1)]),
            report("weak-again",D2,"13:05",[(A,"减弱")]),quote("still-hold",D2,"13:06","11"),
            report("retreat",D3,"14:05",[(A,"退潮")]),quote("sell2",D3,"14:06","12")])
        c,=result["cycles"]
        self.assertEqual([f["quantity"] for f in c["book"]["fills"]],[1000,500,500])
        self.assertEqual([o["action"] for o in c["orders"]],["BUY","REDUCE","EXIT"])
        self.assertEqual((c["status"],c["valuation"]["profit"],c["valuation"]["basis"]),("CLOSED","1500.00","GROSS"))
        self.assertEqual(result["retired_directions"][A]["phase"],"RETREAT")
        self.assertNotIn(A,result["rotation"])
        self.assertEqual(result["evaluation"]["GROSS"],{"closed":1,"wins":1,"win_pct":"100.0000"})

    def test_b02_t1_uses_next_trading_session_price(self):
        r=self.replay(start()+[report("weak",D1,"10:35",[(A,"减弱")]),quote("same-day",D1,"10:36","15")])
        c,=r["cycles"]
        self.assertEqual(c["pending_reason"],"WAIT_T1")
        self.assertEqual(c["orders"][-1]["eligible_at"],stamp(D2,"09:30"))
        self.assertEqual(len(c["book"]["fills"]),1)
        r=self.replay([quote("t2-price",D2,"09:30","9.50")])
        c,=r["cycles"]
        self.assertEqual((c["book"]["fills"][-1]["quantity"],c["book"]["fills"][-1]["raw_price"]),(500,"9.50"))

    def test_b03_exit_removes_chain_before_t1_and_overrides_weak(self):
        r=self.replay(start()+[report("weak",D1,"10:35",[(A,"减弱")]),report("exit",D1,"13:05",[(A,"退潮")])])
        c,=r["cycles"]
        self.assertNotIn(A,r["rotation"])
        self.assertEqual((c["status"],c["pending_reason"]),("WAIT_EXIT","WAIT_T1"))
        self.assertEqual((c["orders"][1]["status"],c["orders"][2]["target"]),("SUPERSEDED",1000))
        r=self.replay([quote("liquidation",D2,"09:31","10")])
        self.assertEqual([f["quantity"] for f in r["cycles"][0]["book"]["fills"]],[1000,1000])

    def test_direct_exit_without_first_weak(self):
        r=self.replay(start()+[report("direct",D2,"09:35",[(A,"退潮")]),quote("exit",D2,"09:36","10")])
        self.assertEqual([o["action"] for o in r["cycles"][0]["orders"]],["BUY","EXIT"])

    def mixed_lots(self,old,new):
        rule=Rules()
        entry=normalize(start()[0])
        c=create_cycle(ETF,100,rule,entry,entry["candidates"][ETF][0],entry["candidates"][ETF])
        c["orders"][0]["status"]="FILLED"
        c["status"]="OPEN"
        buy(c["book"],rule,old,Decimal(1),date.fromisoformat(D1),date.fromisoformat(D2))
        buy(c["book"],rule,new,Decimal(1),date.fromisoformat(D2),date.fromisoformat(D3))
        apply_report(c,normalize(report("mixed-weak",D2,"10:35",[(A,"减弱")])))
        fill(c,quote("t1",D2,"10:36","1"),Calendar(DAYS))
        return c

    def test_b04_old_200_new_800_freezes_and_finishes_300(self):
        c=self.mixed_lots(200,800)
        self.assertEqual(c["orders"][-1]["remaining"],300)
        fill(c,quote("next",D3,"09:35","1"),Calendar(DAYS))
        self.assertEqual([f["quantity"] for f in c["book"]["fills"]],[200,300])
        self.assertEqual(c["orders"][-1]["target"],500)

    def test_b05_old_500_unlocks_exactly_500(self):
        c=self.mixed_lots(500,500)
        self.assertEqual(c["orders"][-1]["status"],"FILLED")
        self.assertEqual([lot["remaining"] for lot in c["book"]["lots"]],[0,500])

    def test_b06_appendix_cashflow_and_b07_reuse_proceeds(self):
        r=Rules()
        book=new_book(r)
        day,next_day,last=map(date.fromisoformat,[D1,D2,D3])
        buy(book,r,5000,Decimal("1.000"),day,next_day)
        buy(book,r,4000,Decimal("1.100"),next_day,last)
        sell(book,r,4500,Decimal("1.200"),next_day)
        sell(book,r,4500,Decimal("1.100"),last)
        summary=valuation(book,r)
        self.assertEqual((summary["cash"],summary["profit"],summary["return_pct"]),("10950.00","950.00","9.5000"))
        fresh=new_book(r)
        buy(fresh,r,1000,Decimal(10),day,next_day)
        sell(fresh,r,1000,Decimal(10),next_day)
        buy(fresh,r,1000,Decimal(10),next_day,last)
        self.assertEqual(fresh["cash"],"0.00")
        with self.assertRaisesRegex(ValueError,"INSUFFICIENT_T1"):
            sell(fresh,r,100,Decimal(10),next_day)

    def test_b08_retry_and_write_conflict_are_atomic(self):
        original=start()
        result=self.replay(original)
        self.assertEqual(self.replay(original),result)
        conflict=copy.deepcopy(original[1]); conflict["price_cny"]="999"
        with self.assertRaisesRegex(ValueError,"collision"):
            self.replay([report("new-event",D2,"09:35",[(A,"维持")]), conflict])
        self.assertEqual(self.ledger.inspect("stage1:test"),result)
        second=quote("duplicate",D2,"09:36","9")
        collision=dict(second,price_cny="8")
        with self.assertRaisesRegex(ValueError,"collision"):
            self.replay([report("new-event",D2,"09:35",[(A,"维持")]), second,collision])
        self.assertEqual(self.ledger.inspect("stage1:test"),result)

    def test_b10_b11_no_direction_or_price_does_not_close_position(self):
        r=self.replay(start()+[report("no-news",D2,"09:35"),quote("lower",D2,"09:36","8.0")])
        c,=r["cycles"]
        self.assertEqual((c["last_decision"],c["status"],c["valuation"]["unrealized"]),("UNKNOWN","OPEN","-2000.00"))
        self.assertGreater(c["position_duration_seconds"],0)
        r=self.replay([report("new-day-no-price",D3,"10:35")])
        self.assertIsNone(r["cycles"][0]["valuation"]["profit"])

    def test_publication_time_and_valid_trading_price(self):
        r=self.replay([report("delayed",D1,"10:35",[(A,"新增")],[(ETF,A,1)],published="10:38"),
             quote("before-publication",D1,"10:37","9"),quote("same-second",D1,"10:38","9"),
             quote("halt",D1,"10:39","9",tradeable=False),quote("lunch",D1,"12:10","9")])
        self.assertEqual(r["cycles"][0]["pending_reason"],"WAIT_PRICE")
        r=self.replay([quote("first-valid",D1,"13:02","10")])
        self.assertEqual(r["cycles"][0]["book"]["fills"][0]["quote_id"],"first-valid")

    def test_structured_phases_distinct_evidence_and_removal(self):
        r=self.replay([report("first",D1,"09:35",[(A,"新增")],[(ETF,A,1)]),
                       report("steady",D1,"10:35",[(A,"维持")]),
                       report("strong1",D1,"13:05",[(A,"加强")],evidence="unique1"),
                       report("same",D1,"14:05",[(A,"加强")],evidence="unique1")])
        self.assertEqual(r["rotation"][A]["phase"],"STRENGTHEN")
        self.assertEqual(r["rotation"][A]["streak"],1)
        self.assertTrue(self.ledger.report("stage1:test","same")["sectors"][A]["duplicate_evidence"])
        r=self.replay([report("strong2",D1,"14:30",[(A,"加强")],evidence="unique2")])
        self.assertEqual(r["rotation"][A]["phase"],"STRENGTHEN")
        self.assertEqual(r["rotation"][A]["label"],"加强×2")
        r=self.replay([report("strong3",D2,"09:30",[(A,"加强")],evidence="unique3")])
        self.assertEqual(r["rotation"][A]["label"],"加强×3")
        r=self.replay([report("gone",D2,"09:35",[(A,"退潮")]),
                       report("not-new",D2,"10:35",[(A,"加强")],[(ETF,A,1)])])
        self.assertNotIn(A,r["rotation"])
        self.assertEqual(r["cycles"][0]["status"],"CANCELLED")
        r=self.replay([report("reentry",D2,"13:05",[(A,"新增")],[(ETF,A,1)])])
        self.assertTrue(r["rotation"][A]["member"])
        self.assertEqual([c["status"] for c in r["cycles"]],["CANCELLED","WAIT_BUY"])

    def test_weakening_streak_advances_without_reducing_again(self):
        result=self.replay(start()+[
            report("weak1",D2,"09:35",[(A,"减弱")],evidence="weak-a"),
            quote("first-reduction",D2,"09:36","11"),
            report("duplicate",D2,"10:35",[(A,"减弱")],evidence="weak-a"),
            report("weak2",D2,"13:05",[(A,"减弱")]),
            report("weak3",D2,"14:05",[(A,"减弱")]),
        ])
        self.assertEqual(result["rotation"][A]["label"],"减弱×3")
        duplicate=self.ledger.report("stage1:test","duplicate")["sectors"][A]
        second=self.ledger.report("stage1:test","weak2")["sectors"][A]
        self.assertEqual((duplicate["streak"],duplicate["state"]),(1,"HOLD"))
        self.assertEqual((second["label"],second["state"]),("减弱×2","WEAK"))
        cycle,=result["cycles"]
        self.assertEqual([o["action"] for o in cycle["orders"]],["BUY","REDUCE"])
        self.assertEqual(cycle["valuation"]["shares"],500)
        result=self.replay([
            report("hold1",D3,"09:35",[(A,"维持")]),
            report("hold2",D3,"10:35",[(A,"维持")]),
        ])
        self.assertEqual(result["rotation"][A]["label"],"维持×2")
        result=self.replay([report("weak-again",D3,"13:05",[(A,"减弱")])])
        self.assertEqual(result["rotation"][A]["label"],"减弱")
        self.assertEqual([o["action"] for o in result["cycles"][0]["orders"]],["BUY","REDUCE"])

    def test_previous_stage1_ledger_recovers_weakening_streak(self):
        self.replay(start()+[
            report("weak1",D2,"09:35",[(A,"减弱")]),
            report("weak2",D2,"10:35",[(A,"减弱")]),
        ])
        with sqlite3.connect(self.ledger.path) as db:
            db.execute("UPDATE rotation SET body=json_set(body,'$.streak',0)")
            for rid in ("weak1","weak2"):
                old=db.execute("SELECT payload FROM events WHERE id=?",(rid,)).fetchone()[0]
                payload=json.loads(old)
                row=payload["sectors"][A]
                row.pop("streak")
                row.pop("label")
                db.execute("UPDATE events SET payload=? WHERE id=?",(json.dumps(payload),rid))
        self.ledger=Ledger(self.state)
        self.assertEqual(self.ledger.inspect("stage1:test")["rotation"][A]["label"],"减弱×2")
        result=self.replay([report("weak3",D2,"13:05",[(A,"减弱")])])
        self.assertEqual(result["rotation"][A]["label"],"减弱×3")
        self.assertNotIn("label",self.ledger.report("stage1:test","weak2")["sectors"][A])

    def test_d04_withdrawal_negation_and_bad_etf_localized(self):
        initial=report("entry",D1,"09:35",[(A,"新增"),(B,"新增")],[(ETF,A,1),("坏代码",A,2),(ETF2,B,1)])
        r=self.replay([initial,quote("buy-a",D1,"09:36","10"),
            report("keep",D1,"10:35",[(A,"维持")]),report("legacy-withdraw",D1,"13:05",[(A,"撤回")])])
        first=next(c for c in r["cycles"] if c["etf"]==ETF)
        self.assertEqual((len(r["cycles"]),first["status"],len(first["orders"])),(2,"OPEN",1))
        archive=self.ledger.report("stage1:test","legacy-withdraw")
        self.assertEqual(archive["sectors"][A]["phase"],"UNKNOWN")
        self.assertEqual(self.ledger.report("stage1:test","entry")["raw"],initial["report_json"])

    def test_d06_fen_conversion_and_explicit_net_costs(self):
        first=quote("entry-price",D1,"09:36","10.00")
        first.pop("price_cny")
        first.update(price="1000",price_unit="FEN")
        r=self.replay([start()[0],first,report("exit",D2,"09:35",[(A,"退潮")]),
                       quote("sale",D2,"09:36","11")],
                      rules={"version":"frozen_cost_example_v1","fee_rate":"0",
                             "minimum_fee":"2","slippage_bps":"0"})
        c,=r["cycles"]
        self.assertEqual([f["quantity"] for f in c["book"]["fills"]],[900,900])
        self.assertEqual(c["valuation"]["profit"],"896.00")
        self.assertEqual(c["valuation"]["basis"],"NET")
        self.assertEqual(r["evaluation"]["NET"]["closed"],1)

    def test_below_one_lot_weak_order_stays_visible(self):
        r=self.replay(start()+[report("weak",D2,"09:35",[(A,"减弱")]),
                               quote("still-holding",D2,"09:36","10")],
                      rules={"version":"frozen_small_cash_v1","initial_cash":"1000"})
        order=r["cycles"][0]["orders"][-1]
        self.assertEqual((order["theoretical"],order["target"],order["status"]),("50.00",0,"BELOW_TRADING_UNIT"))
        self.assertEqual(len(r["cycles"][0]["book"]["fills"]),1)

    def test_same_timestamp_retreat_outranks_weakness(self):
        r=self.replay(start()+[report("a-weak",D2,"09:35",[(A,"减弱")]),
                               report("b-retreat",D2,"09:35",[(A,"退潮")]),
                               quote("closing",D2,"09:36","10")])
        c,=r["cycles"]
        self.assertEqual([o["action"] for o in c["orders"]],["BUY","EXIT"])
        self.assertEqual([f["quantity"] for f in c["book"]["fills"]],[1000,1000])
        self.assertNotIn(A,r["rotation"])

    def test_d06_late_quote_does_not_replace_newer_observation(self):
        r=self.replay(start()+[quote("latest",D2,"09:36","9"),
            quote("old",D2,"09:35","50",received=stamp(D2,"09:37"))])
        self.assertEqual(r["cycles"][0]["valuation"]["unrealized"],"-1000.00")

    def test_d08_two_simultaneous_sources_and_primary_is_frozen(self):
        reports=[report("r1",D1,"09:35",[(A,"新增")],[(ETF,A,1)]),
                 report("r2",D1,"09:35",[(B,"新增")],[(ETF,B,2)])]
        r=self.replay(reports+[quote("price",D1,"09:36","10"),
                               report("wrong-sector-retreat",D2,"09:35",[(B,"退潮")])])
        c,=r["cycles"]
        self.assertEqual((c["primary_sector"],c["status"],len(c["entry_sources"])),(A,"OPEN",2))
        with sqlite3.connect(self.ledger.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO cycles SELECT ns,'duplicate',etf,opened_at,status,body FROM cycles")

    def test_d09_rules_frozen_across_restart(self):
        self.replay(start())
        self.ledger=Ledger(self.state)
        r=self.replay([report("weak",D2,"09:35",[(A,"减弱")]),quote("t1",D2,"09:36","11")],
                      rules={"version":"changed_research_v2","reduce_ratio":"0.25"})
        c,=r["cycles"]
        self.assertEqual((c["rule"]["version"],c["orders"][-1]["target"]),("hermes_signal_cycle_v1",500))
        with self.assertRaisesRegex(ValueError,"without a new version"):
            self.replay([],rules={"version":"changed_research_v2","reduce_ratio":"0.30"})

    def test_legacy_exporter_fixture_roundtrips_without_fabricating_id(self):
        raw=(Path(__file__).parent/"fixtures/legacy_report_partial.json").read_text()
        r=self.replay([{"kind":"report","report_json":raw}])
        self.assertEqual(r["cycles"],[])
        archive=self.ledger.report("stage1:test",json.loads(raw)["report_id"])
        self.assertEqual(archive["raw"],raw)
        self.assertIn("UNRESOLVED_SECTOR:0",archive["diagnostics"])
