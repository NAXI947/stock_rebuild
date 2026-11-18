"""QQ visual fields preserve saved facts, unknowns and simulation conditions."""
from copy import deepcopy
import json
from pathlib import Path
from unittest import TestCase

from modules.strategy_hermes.stage2.notifications import brief_sections, visual_brief_sections
from test_notifications import report, SID, ETF, STOCK


def fixture():
    entry = report()
    raw = json.loads(entry['raw'])
    raw['source_facts'] = {SID: {'breadth': dict(
        status='PARTIAL', member_count=40, observed_count=30, up=24, down=5, flat=1,
        coverage='.75', up_ratio='.8', median_return='.012',
        oldest_at='2026-11-13T14:30:12+08:00', newest_at='2026-11-13T14:30:18+08:00')}}
    entry['raw'] = json.dumps(raw)
    entry['input_meta'].update(source_observed_at='2026-11-13T14:30:18+08:00',
        market_scope=dict(board_count=20, board_counts=dict(up=10, down=5, flat=3, unknown=2),
                          etf_count=12, mapped_count=3, analyzed_count=1,
                          stock_quotes='AVAILABLE', iopv='UNAVAILABLE',
                          gainers=[dict(name='银行',return_ratio='.02')],
                          decliners=[dict(name='煤炭',return_ratio='-.03')]),
        stock_watchlist={SID:[STOCK]}, rotation_window=[dict(slot='14:05')],
        octop=dict(receipt={'status':'READY'}, market_evidence={SID:{'board_return_ratio':'.02'}},
                   research_explanations={SID:dict(rationale='当前上涨家数较多，但新增量能尚未确认。',
                       counter_evidence_status='NOT_CHECKED',
                       invalidation_condition='若上涨不足六成且中位数转负，则不能维持强势判断。',
                       key_unknown='成交增量与权重股承接尚待核实。')}))
    return entry


class QQVisualReport(TestCase):
    def test_manual_recovery_identifies_original_slot_fact_cutoff_and_real_publication(self):
        entry = fixture()
        entry['input_meta']['manual_recovery'] = dict(retry_of='failed-run',
            source_cutoff='2026-11-17T10:35:14+08:00', executed_at='2026-11-17T11:15:25+08:00',
            decision_hash='saved-decision-hash')
        original = deepcopy(entry)
        for render in (brief_sections, visual_brief_sections):
            fields = render('10:35', entry, [])
            self.assertIn('10:35轮·手动恢复', fields['title'])
            self.assertIn('事实截止 11-17 10:35:14', fields['clock'])
            self.assertIn('恢复发布 11-17 11:15:25', fields['clock'])
            self.assertNotIn('14:30:18', fields['clock'])
            self.assertNotIn('saved-decision-hash', str(fields))
        self.assertEqual(entry, original)

    def test_unknown_direction_shows_actual_gaps_without_changing_saved_signal(self):
        entry = fixture()
        entry['sectors'][SID].update(state='UNKNOWN', phase='UNKNOWN', label='待核实')
        raw = json.loads(entry['raw'])
        raw['sectors'][0]['risks'] = ['SECTOR_MEMBERS_UNAVAILABLE', 'STOCK_QUOTES_MISSING']
        entry['raw'] = json.dumps(raw)
        original = deepcopy(entry)
        for render in (brief_sections, visual_brief_sections):
            boards = render('14:30', entry, [])['boards']
            self.assertIn('缺成分名单', boards)
            self.assertIn('缺个股行情', boards)
            self.assertNotIn('缺可比前序', boards)
        self.assertEqual(entry, original)

    def test_read_only_rendering_keeps_old_fields_and_business_facts(self):
        entry=fixture(); original=deepcopy(entry)
        old=brief_sections('14:30',entry,[])
        fields=visual_brief_sections('14:30',entry,[])
        self.assertEqual(entry,original)
        self.assertEqual(brief_sections('14:30',entry,[]),old)
        self.assertEqual(set(fields),set('title clock market leaders boards etf_candidates etf_watch etf_blocked etf_gaps stocks simulation iopv active_directions details report_id'.split()))
        self.assertIn('14:30:18',fields['clock'])
        self.assertIn('（北京时间）',fields['clock'])
        self.assertIn('⚠️部分证据',fields['clock'])
        self.assertIn('🟢 银行 (加强×2)',fields['boards'])

    def test_market_bar_uses_all_boards_including_flat_and_unknown(self):
        fields=visual_brief_sections('14:30',fixture(),[])
        self.assertIn('涨10/20 ▰▰▰▰▰▱▱▱▱▱',fields['market'])
        self.assertIn('跌5 (平3·未知2)',fields['market'])
        self.assertEqual(len(fields['market'].splitlines()),2)
        self.assertIn('🚀 领涨：银行 +2.00%\n💧 领跌：煤炭 -3.00%',fields['leaders'])

    def test_small_positive_ratio_may_round_to_zero_cells_without_losing_counts(self):
        entry=fixture()
        entry['input_meta']['market_scope'].update(board_count=243,board_counts=dict(up=10,down=233,flat=0,unknown=0))
        value=visual_brief_sections('14:30',entry,[])['market']
        self.assertIn('涨10/243 ▱▱▱▱▱▱▱▱▱▱ 跌233',value)

    def test_unknown_market_count_is_not_rendered_as_zero_or_a_valid_scale(self):
        entry=fixture()
        del entry['input_meta']['market_scope']['board_counts']['up']
        value=visual_brief_sections('14:30',entry,[])['market']
        self.assertIn('涨未知/20 待核实',value)
        self.assertNotIn('▱',value)
        entry['input_meta'].pop('market_scope')
        fields=visual_brief_sections('14:30',entry,[])
        self.assertEqual(fields['market'],'市场覆盖待核实')
        self.assertEqual(fields['iopv'],'待核实')

    def test_partial_breadth_retains_observed_and_total_denominators_with_source_clock(self):
        card=visual_brief_sections('14:30',fixture(),[])['details'][0]
        self.assertEqual(len(card.splitlines()),4)
        self.assertIn('涨24/30 (▰▰▰▰▰▰▰▰▱▱ 80%)',card)
        self.assertIn('覆盖30/40(75%)',card)
        self.assertIn('中位+1.20%',card)
        self.assertIn('源14:30:12–14:30:18',card)
        self.assertNotIn('涨24/40',card)

    def test_unknown_breadth_and_counter_evidence_remain_unknown(self):
        entry=fixture();raw=json.loads(entry['raw'])
        raw['source_facts'][SID]['breadth'].update(status='UNKNOWN',up=None,coverage=None,up_ratio=None)
        entry['raw']=json.dumps(raw)
        card=visual_brief_sections('14:30',entry,[])['details'][0]
        self.assertIn('宽度 待核验',card)
        self.assertIn('⚡ 反证：待核实',card)
        self.assertNotIn('反证：暂无',card)
        self.assertNotIn('▱',card)

    def test_known_absence_and_found_counter_evidence_are_distinct(self):
        entry=fixture();meta=entry['input_meta']['octop']
        item=meta['research_explanations'][SID]
        item['counter_evidence_status']='NOT_FOUND'
        self.assertIn('⚡ 反证：暂无（已核实）',visual_brief_sections('14:30',entry,[])['details'][0])
        item.update(counter_evidence_status='FOUND',counter_evidence_refs=['stock-ref'])
        meta['research_explanation_evidence']={SID:{'stock-ref':dict(code=STOCK,return_ratio='-.021',observed_at='2026-11-13T14:29:01+08:00')}}
        card=visual_brief_sections('14:30',entry,[])['details'][0]
        self.assertIn('⚡ 反证：14:29 600000.SH -2.10%',card)
        self.assertNotIn('反证：暂无',card)

    def test_longer_than_twenty_characters_keeps_negation_and_invalidation_condition(self):
        entry=fixture();item=entry['input_meta']['octop']['research_explanations'][SID]
        self.assertGreater(len(item['invalidation_condition']),20)
        card=visual_brief_sections('14:30',entry,[])['details'][0]
        for key in ('rationale','invalidation_condition','key_unknown'):
            self.assertIn(item[key],card)

    def test_simulated_nodes_keep_t_plus_one_calendar_units_and_prior_marker(self):
        entry=fixture()
        orders=[(ETF,'REDUCE',500,'PENDING','2026-11-16T09:30:00+08:00',False),
                (ETF,'EXIT',500,'PENDING',None,True),
                (ETF,'BUY',50,'BELOW_TRADING_UNIT',entry['at'],True)]
        value=visual_brief_sections('14:30',entry,orders)['simulation']
        for part in ('转弱减500份 [待T+1]（前轮）','退潮清500份 [待交易日历]','买入50份 [低于最小单位]'):
            self.assertIn(part,value)
        self.assertNotIn('已成交',value)

    def test_visual_etf_categories_keep_existing_representative_order_and_conditions(self):
        entry=fixture()
        entry['input_meta']['etf_recommendations']={'sectors':{SID:{
            'representatives':['512900.SH',ETF],
            'products':[dict(etf=ETF,recommendation_status='CONFIRMED',execution_status='PARTIAL',representative_current=True),
                        dict(etf='512900.SH',recommendation_status='CONFIRMED',execution_status='COMPLETE',representative_current=True),
                        dict(etf='510300.SH',recommendation_status='STATIC_ONLY',execution_status='UNKNOWN'),
                        dict(etf='512700.SH',recommendation_status='CONFIRMED',execution_status='FAILED')]}}}
        fields=visual_brief_sections('14:30',entry,[])
        self.assertLess(fields['etf_candidates'].index('512900.SH'),fields['etf_candidates'].index(ETF))
        self.assertIn('条件待核',fields['etf_candidates'])
        self.assertIn('510300.SH（静态匹配）',fields['etf_watch'])
        self.assertIn('512700.SH（交易条件未通过）',fields['etf_blocked'])
        self.assertNotIn('512700.SH',fields['etf_candidates'])

    def test_three_atomic_cards_and_omission_hint_cover_larger_report(self):
        entry=json.loads((Path(__file__).with_name('fixtures')/'qq_discovery_ten_boards.json').read_text())
        original=deepcopy(entry)
        fields=visual_brief_sections('14:30',entry,[])
        self.assertEqual(len(fields['details']),4)
        self.assertTrue(all(len(card.splitlines())==4 for card in fields['details'][:3]))
        self.assertEqual(fields['details'][-1],'⋯ 其余详见工作台报告')
        self.assertIn('等10项',fields['boards'])
        self.assertEqual(entry,original)

    def test_preview_uses_actual_saved_source_clock_without_activating_directions(self):
        entry=fixture();entry['at']='2026-11-13T16:00:00+08:00'
        fields=visual_brief_sections('14:30',entry,[],preview=True)
        self.assertIn('盘后试发',fields['title'])
        self.assertIn('快照 14:30:18 · 试发 16:00:00',fields['clock'])
        self.assertEqual(fields['active_directions'],'')
        self.assertEqual(fields['simulation'],'本次试发无节点')

    def test_available_iopv_is_data_availability_not_an_execution_approval(self):
        entry=fixture();entry['input_meta']['market_scope']['iopv']='AVAILABLE'
        self.assertEqual(visual_brief_sections('14:30',entry,[])['iopv'],'有数据')

