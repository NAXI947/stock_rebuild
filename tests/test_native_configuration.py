"""Deployment configuration keeps native tool submission within reviewed budgets."""
import unittest

from tools.configure_octop_native import agent_body, system_prompt
from tools.configure_octop_maintenance import bundle as maintenance_bundle


class NativeConfiguration(unittest.TestCase):
    def test_maintenance_review_loop_adds_no_schedule_and_keeps_model_call_cap(self):
        config=maintenance_bundle('provider/model')
        self.assertEqual([job['trigger'] for job in config['jobs']],['cron:0 10 * * mon-fri','cron:15 14 * * mon-fri'])
        prompt=config['agent']['system_prompt']
        for text in ('最多24次模型调用','stock_etf_verification_context','stock_etf_verification_submit',
                     'mode=verify','NEEDS_MATERIALS','不通过新ID重置预算'):
            self.assertIn(text,prompt)
        self.assertEqual(config['agent']['max_input_length'],128000)

    def test_reviewed_native_budget_and_fact_contract_are_deployed_together(self):
        body = agent_body("provider/model", system_prompt())
        self.assertEqual(body["max_tokens"], 28000)
        self.assertEqual(body["max_input_length"], 128000)
        self.assertEqual(body["max_iters"], 768)
        self.assertIn("最多20次模型调用", body["system_prompt"])
        self.assertIn("时限120秒", body["system_prompt"])
        self.assertIn("scoped-facts-v1", body["system_prompt"])
        self.assertIn("explicit-etfs-v1", body["system_prompt"])
        self.assertEqual(body["mcp_servers"], ["stock-v4-rounds"])


if __name__ == "__main__":
    unittest.main()
