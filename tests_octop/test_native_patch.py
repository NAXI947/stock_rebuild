"""Validate the scoped final configuration with OCTOP's real config classes."""
from types import SimpleNamespace
import textwrap
import unittest

from tools.prepare_octop_native_patch import CONFIG_INSERT


class NativePatchTests(unittest.TestCase):
    def final_config(self, native):
        from octop_harness.config import HarnessAgentConfig
        from octop_harness.security.models import SecurityPolicy
        config = HarnessAgentConfig(system_prompt="original", model_retry_enabled=True)
        applied = SecurityPolicy().apply_to_config(config)
        namespace = dict(applied=applied, cfg={"stock_native_rounds": native}, row=SimpleNamespace(system_prompt="stable rules"))
        exec(textwrap.dedent(CONFIG_INSERT), namespace)
        return applied

    def test_native_controls_are_final_after_security_policy(self):
        applied = self.final_config(True)
        self.assertFalse(applied.model_retry_enabled)
        self.assertFalse(applied.bootstrap_enabled)
        self.assertFalse(applied.memory_enabled)
        self.assertFalse(applied.subagents_auto_load)
        self.assertFalse(applied.team_enabled)
        self.assertIn("task", applied.tools_disabled)
        self.assertEqual(applied.system_prompt, "stable rules")
        self.assertEqual(applied.tool_search_mode, "eager")

    def test_other_experts_keep_original_settings(self):
        applied = self.final_config(False)
        self.assertTrue(applied.model_retry_enabled)
        self.assertEqual(applied.system_prompt, "original")


if __name__ == "__main__":
    unittest.main()
