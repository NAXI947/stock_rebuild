from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch,Mock

from modules.strategy_hermes.qq_templates import FIELDS,PACKAGED,render_message,validate_template


class QQTemplates(unittest.TestCase):
    def fields(self,kind):
        return {key:key for key in FIELDS[kind].split()}

    def test_user_layout_accepts_multiple_fields_and_multiline_cards(self):
        fields=self.fields('research')
        fields.update(simulation='无待执行',iopv='缺数据',market='涨10\n243板块',
                      details=['【银行】\n✅ 依据：上涨\n⚡ 反证：待核实\n🛡️ 风控：条件未满足'])
        before=deepcopy(fields)
        text=render_message('research',fields)['text']
        self.assertIn('🤖 节点：无待执行 ｜ 📐 IOPV：缺数据',text)
        self.assertIn('涨10\n243板块',text)
        self.assertIn('反证：待核实',text)
        self.assertEqual(fields,before)

    def test_all_five_approved_templates_render_and_have_exact_fields(self):
        for kind in FIELDS:
            with self.subTest(kind=kind):
                raw=(PACKAGED/(kind+'.txt')).read_bytes()
                validate_template(kind,raw)
                result=render_message(kind,self.fields(kind))
                self.assertTrue(result['text'])
                self.assertNotIn('{{',result['text'])
                self.assertLessEqual(len(result['text']),1400 if kind=='maintenance' else 1500)

    def test_managed_template_is_used_and_invalid_edit_falls_back(self):
        with TemporaryDirectory() as tmp:
            path=Path(tmp)/'status.txt'
            original=(PACKAGED/'status.txt').read_text(encoding='utf-8')
            path.write_text(original.replace('⚙️','📣'),encoding='utf-8')
            first=render_message('status',self.fields('status'),template_dir=tmp)
            self.assertTrue(first['text'].startswith('📣'))
            self.assertIn(':managed:',first['template'])
            path.write_text('{{task_name}}',encoding='utf-8')
            second=render_message('status',self.fields('status'),template_dir=tmp)
            self.assertTrue(second['template_fallback'])
            self.assertTrue(second['text'].startswith('⚙️'))

    def test_long_cards_drop_whole_card_and_keep_reference_and_unknown(self):
        fields=self.fields('research')
        cards=[f'【板块{i}】\n✅ '+'证据'*120+'\n⚡ 反证：待核实\n🛡️ 条件未满足' for i in range(8)]
        fields['details']=cards
        fields['report_id']='stable-report-id'
        result=render_message('research',fields)
        self.assertLessEqual(len(result['text']),1500)
        self.assertIn('stable-report-id',result['text'])
        self.assertIn('其余详见工作台报告',result['text'])
        for i,card in enumerate(cards):
            if f'【板块{i}】' in result['text']:self.assertIn(card,result['text'])

    def test_empty_optional_single_line_hidden_and_shared_line_is_retained(self):
        fields=self.fields('research');fields.update(etf_watch='',iopv='')
        text=render_message('research',fields)['text']
        self.assertNotIn('👀 观察：',text)
        self.assertIn('📐 IOPV：—',text)

    def test_fields_are_never_interpreted_as_template_or_code(self):
        fields=self.fields('status');fields['reason']='{{task_id}} ${secret} <script>'
        text=render_message('status',fields)['text']
        self.assertIn('{{task_id}} ${secret} <script>',text)
        with self.assertRaises(ValueError):render_message('status',dict(fields,unknown='x'))
        with self.assertRaises(ValueError):render_message('../status',fields)


if __name__=='__main__':unittest.main()
