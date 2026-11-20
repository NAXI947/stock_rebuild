from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import MagicMock, patch

from tools.etf_taxonomy_container_script import container_script

STORE = "modules.strategy_hermes.stage2.maintenance_store."
SID, INDEX = "kaipanla:801004", "931719.CSI"
ROW = dict(sector_id=SID, index_code=INDEX, family_id="battery", canonical_branch_id="lithium_battery",
           available_at="2026-11-20T12:38:53+08:00", evidence_refs=["sha256", "https://example.invalid/m.pdf"])


class ContainerScriptTests(unittest.TestCase):
    def setUp(self):
        self.mapping = dict(version="maintained-1", sectors={SID: {}}, products={"159840.SZ": {"index_code": INDEX}},
                            etf_taxonomy=dict(version="taxonomy-r1", rows=[]))

    def run_script(self, mode, taxonomy=None):
        script = container_script(json.dumps(taxonomy or dict(version="taxonomy-r2", rows=[ROW]), ensure_ascii=False))
        settings = MagicMock(env="production")
        with patch.dict("os.environ", {"TAXONOMY_MODE": mode}), \
                patch("modules.strategy_hermes.stage2.settings.Settings.from_values", return_value=settings), \
                patch("modules.strategy_hermes.stage2.postgres.PostgresLedger") as ledger, \
                patch(STORE + "live_mapping", return_value=self.mapping), \
                patch(STORE + "publish_mapping") as publish, \
                patch("tempfile.NamedTemporaryFile", side_effect=AssertionError("unmanaged temporary file")), \
                redirect_stdout(StringIO()) as out:
            ledger.return_value.ns = "stock-v4:prod:baseline"
            try:
                exec(compile(script, "container_script", "exec"), {"__name__": "container"})
            finally:
                self.publish, self.ledger = publish, ledger
        return json.loads(out.getvalue())

    def test_dry_run_reports_the_publication_without_writing(self):
        dry = self.run_script("DRY")
        self.publish.assert_not_called()
        self.ledger.assert_called_once_with(unittest.mock.ANY, "LIVE")
        published = self.run_script("PUBLISH")
        self.assertEqual(dry, dict(status="DRY_OK", env="production", namespace="stock-v4:prod:baseline",
                                   mapping_version=published["mapping_version"], parent_mapping_version="maintained-1",
                                   previous_taxonomy="taxonomy-r1", previous_pairs=0,
                                   taxonomy_version="taxonomy-r2", pairs=1))

    def test_publish_adds_one_mapping_version_on_the_current_parent(self):
        result = self.run_script("PUBLISH")
        self.assertEqual((result["status"], result["taxonomy_version"], result["pairs"]), ("PUBLISHED", "taxonomy-r2", 1))
        updated = self.publish.call_args.args[1]
        self.assertEqual((updated["etf_taxonomy"], updated["parent_mapping_version"], updated["version"]),
                         (dict(version="taxonomy-r2", rows=[ROW]), "maintained-1", result["mapping_version"]))
        self.assertEqual(self.publish.call_args.kwargs["expected_parent"], "maintained-1")

    def test_dry_run_fails_wherever_the_publication_would(self):
        self.mapping["etf_taxonomy"] = dict(version="taxonomy-r2", rows=[ROW])
        for mode in ("DRY", "PUBLISH"):
            with self.subTest(mode=mode, case="unchanged"):
                self.assertEqual(self.run_script(mode)["status"], "CURRENT")
            changed = dict(version="taxonomy-r2", rows=[dict(ROW, family_id="new_energy")])
            with self.subTest(mode=mode, case="changed rows, same version"), \
                    self.assertRaisesRegex(ValueError, "requires a new version"):
                self.run_script(mode, changed)
            self.publish.assert_not_called()
        self.mapping["sectors"] = {}
        for mode in ("DRY", "PUBLISH"):
            with self.subTest(mode=mode, case="unknown sector"), self.assertRaisesRegex(ValueError, "unverified"):
                self.run_script(mode, dict(version="taxonomy-r3", rows=[ROW]))
            self.publish.assert_not_called()

    def test_unknown_mode_stops_before_touching_the_database(self):
        with self.assertRaises(SystemExit):
            self.run_script("APPLY")
        self.ledger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
