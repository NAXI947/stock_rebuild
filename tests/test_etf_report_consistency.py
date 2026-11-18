"""Report coverage, execution, and priority survive the real 13:05 failure shapes."""
from copy import deepcopy
import json
from unittest import TestCase

from test_notifications import ETF, SID, STOCK, report
from modules.strategy_hermes.qq_templates import render_message
from modules.strategy_hermes.stage2.notifications import _rationale_summary, visual_brief_sections


class ReportConsistency(TestCase):
    def test_long_explanation_keeps_whole_scoped_numeric_facts(self):
        first = '银行上涨14/42只，比例33.33%，中位涨幅-0.30%。'
        second = '已采集并对齐的A股上涨2538/5466只，比例46.43%。'
        explanation = dict(qualitative_rationale='权重行走弱。', fact_evidence=[
            {'text': first}, {'text': '其他详细证据。' * 40}, {'text': second}])
        summary = _rationale_summary(explanation, 100)
        self.assertLessEqual(len(summary), 100)
        self.assertIn(first, summary)
        self.assertIn(second, summary)
        self.assertIn('其余依据见工作台报告', summary)
        self.assertNotIn('其他详细证据', summary)

    def test_catalog_and_board_gaps_are_separate_from_distinct_etf_rejections(self):
        entry = report()
        entry['input_meta']['etf_recommendations'] = dict(sectors={SID:dict(
            coverage_gaps={'weights': {'products': 1351}},
            products=[dict(etf=code, recommendation_status='REJECTED', execution_status='PARTIAL')
                      for code in ('588100.SH', ETF, '588100.SH')])})
        other = 'kaipanla:801001'
        raw = json.loads(entry['raw'])
        raw['sectors'].append({'sector_name': '芯片'})
        entry['raw'] = json.dumps(raw)
        entry['sectors'][other] = dict(entry['sectors'][SID], row=len(raw['sectors']) - 1)
        entry['input_meta']['etf_recommendations']['sectors'][other] = dict(
            reason='SECTOR_MEMBERS_UNAVAILABLE', products=[])
        before = deepcopy(entry)
        fields = visual_brief_sections('13:05', entry, [])
        self.assertIn('1351只ETF缺成分权重', fields['etf_gaps'])
        self.assertIn('1个板块成员资料待补', fields['etf_gaps'])
        self.assertNotIn('1351', fields['etf_blocked'])
        self.assertIn('共2只ETF/3条板块关联', fields['etf_blocked'])
        self.assertEqual(entry, before)

    def test_ai_selected_waiting_rule_is_specific_and_unselected_is_observation(self):
        entry = report()
        entry['input_meta']['etf_recommendations'] = dict(method='etf-direction-first-v2', sectors={SID:dict(
            representatives=[ETF], products=[
                dict(etf=ETF,ai_selected=True,recommendation_status='PENDING',execution_eligibility='PENDING',
                     execution_status='PARTIAL',execution_reasons=['PRODUCT_RULE_UNVERIFIED','TRADE_UNIT_UNVERIFIED']),
                dict(etf='588100.SH',ai_selected=False,recommendation_status='OBSERVE',execution_eligibility='BLOCKED',
                     execution_status='FAILED',execution_reasons=['PRODUCT_OUTSIDE_SCOPE'])])})
        fields = visual_brief_sections('13:05', entry, [])
        self.assertIn('交易规则待核验', fields['etf_blocked'])
        self.assertIn('交易单位待核验', fields['etf_blocked'])
        self.assertIn('588100.SH（关联观察）', fields['etf_watch'])
        self.assertNotIn('588100.SH', fields['etf_blocked'])
        self.assertEqual(fields['etf_candidates'], '本轮无')

    def test_held_reduction_explanation_precedes_new_breadth_only_direction(self):
        entry = report()
        entry['input_meta']['octop'] = dict(tracked_sectors=[SID], research_explanations={SID:dict(
            source='AI_WITH_FACTS',rationale='银行成分多数走弱。',counter_evidence_status='NOT_CHECKED')})
        raw = json.loads(entry['raw'])
        for index in range(4):
            sid = f'kaipanla:00000{index}'
            raw['sectors'].append({'sector_name': f'新方向{index}'})
            entry['sectors'][sid] = dict(entry['sectors'][SID], row=len(raw['sectors'])-1, state='UP', phase='NEW')
            raw.setdefault('source_facts', {})[sid] = {'breadth': {}}
        entry['raw'] = json.dumps(raw)
        entry['sectors'][SID].update(state='WEAK',phase='WEAKEN')
        fields = visual_brief_sections('13:05', entry, [(ETF,'REDUCE',5500,'PENDING',entry['at'],True)])
        self.assertIn('银行', fields['details'][0])
        self.assertIn('银行成分多数走弱', fields['details'][0])
        self.assertIn('等待节点后新报价', fields['simulation'])
        self.assertIn('银行', render_message('research',fields)['text'])

    def test_cash_block_is_not_mislabeled_waiting_quote(self):
        fields = visual_brief_sections('13:05', report(), [(ETF,'BUY',None,'PENDING',None,False,'INSUFFICIENT_CASH_FOR_ONE_LOT')])
        self.assertIn('资金不足一手', fields['simulation'])
        self.assertNotIn('等待节点后新报价', fields['simulation'])

    def test_entry_pause_reason_matches_recommendation_and_pending_order(self):
        for reason, label in (
            ('RULE_REVIEW_REQUIRED', '产品规则待复核，暂停买入'),
            ('ENTRY_ADMISSION_UNAVAILABLE', '准入资料暂不可用，暂停买入'),
            ('ENTRY_REFERENCE_UNPUBLISHED', '准入资料尚未发布，暂停买入'),
        ):
            with self.subTest(reason=reason):
                entry = report()
                entry['input_meta']['etf_recommendations'] = dict(method='etf-direction-first-v2', sectors={SID:dict(
                    representatives=[], products=[dict(etf=ETF, ai_selected=True, recommendation_status='PENDING',
                    execution_reasons=[reason])])})
                fields = visual_brief_sections('13:05', entry, [(ETF,'BUY',None,'PENDING',None,False,reason)])
                self.assertIn(label, fields['etf_blocked'])
                self.assertIn(label, fields['simulation'])
                self.assertNotIn('等待节点后新报价', fields['simulation'])
                self.assertEqual(fields['etf_candidates'], '本轮无')

    def test_verified_index_relation_is_visible_while_weight_detail_is_missing(self):
        entry = report()
        entry['input_meta']['etf_recommendations'] = dict(method='etf-direction-first-v2', sectors={SID:dict(
            representatives=[ETF], coverage_gaps={'weights': {'products': 1351}}, products=[
                dict(etf=ETF,ai_selected=True,recommendation_status='CONFIRMED',execution_eligibility='READY',
                     execution_status='COMPLETE',representative_current=True,association_status='VERIFIED',
                     association_basis='EXPLICIT_INDEX_RELATION',matched=[],quality=['WEIGHTS_UNVERIFIED'])])})
        fields = visual_brief_sections('13:05', entry, [])
        self.assertIn(ETF, fields['etf_candidates'])
        self.assertIn('1351只ETF', fields['etf_gaps'])
        self.assertEqual(fields['etf_blocked'],'')
