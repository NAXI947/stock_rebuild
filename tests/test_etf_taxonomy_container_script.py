from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import MagicMock, patch

from modules.strategy_hermes.stage2.settings import Settings
from tools.etf_taxonomy_container_script import container_script

STORE = "modules.strategy_hermes.stage2.maintenance_store."
SID, INDEX = "kaipanla:801004", "931719.CSI"


class ContainerScriptTests(unittest.TestCase):
    def setUp(self):
        self.mapping = dict(version="maintained-1", sectors={SID: {}}, products={"159840.SZ": {"index_code": INDEX}},
                            etf_taxonomy=dict(version="taxonomy-r1", rows=[]))
        row = dict(sector_id=SID, index_code=INDEX, family_id="battery", canonical_branch_id="lithium_battery",
                   available_at="2026-11-20T12:38:53+08:00", evidence_refs=["sha256", "https://example.invalid/m.pdf"])
        self.script = container_script(json.dumps(dict(version="taxonomy-r2", rows=[row]), ensure_ascii=False))

    def run_script(self, mode):
        settings = MagicMock(env="production")
        # PUBLISH replaces Settings.read for its one-shot process; keep that out of the other tests.
        with patch.dict("os.environ", {"TAXONOMY_MODE": mode}), patch.object(Settings, "read", Settings.__dict__["read"]), \
                patch("modules.strategy_hermes.stage2.settings.Settings.from_values", return_value=settings), \
                patch("modules.strategy_hermes.stage2.postgres.PostgresLedger") as ledger, \
                patch(STORE + "live_mapping", return_value=self.mapping), \
                patch(STORE + "publish_mapping") as publish, redirect_stdout(StringIO()) as out:
            ledger.return_value.ns = "stock-v4:prod:baseline"
            exec(compile(self.script, "container_script", "exec"), {"__name__": "container"})
        return json.loads(out.getvalue()), publish, ledger

    def test_dry_run_validates_against_the_live_mapping_without_publishing(self):
        result, publish, ledger = self.run_script("DRY")
        self.assertEqual(result, dict(status="DRY_OK", env="production", namespace="stock-v4:prod:baseline",
                                      mapping_version="maintained-1", previous_taxonomy="taxonomy-r1", previous_rows=0,
                                      new_taxonomy="taxonomy-r2", new_rows=1))
        ledger.assert_called_once_with(unittest.mock.ANY, "LIVE")
        publish.assert_not_called()

    def test_publish_runs_the_repository_tool_with_the_container_environment(self):
        result, publish, _ = self.run_script("PUBLISH")
        self.assertEqual((result["status"], result["taxonomy_version"], result["pairs"]), ("PUBLISHED", "taxonomy-r2", 1))
        updated = publish.call_args.args[1]
        self.assertEqual((updated["etf_taxonomy"]["version"], updated["parent_mapping_version"], updated["version"]),
                         ("taxonomy-r2", "maintained-1", result["mapping_version"]))
        self.assertEqual(publish.call_args.kwargs["expected_parent"], "maintained-1")

    def test_unknown_mode_and_unreviewed_rows_stop_before_any_write(self):
        with self.assertRaises(SystemExit):
            self.run_script("APPLY")
        self.mapping["sectors"] = {}
        for mode in ("DRY", "PUBLISH"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "unverified or duplicate"):
                self.run_script(mode)


if __name__ == "__main__":
    unittest.main()
