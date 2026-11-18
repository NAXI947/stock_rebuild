"""Validate archived macro data and keep unknown source time explicit."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.stage2.macro_background import normalize, read_background


def capture():
    return {"id": 72, "专属接口链接": "fixture-secret", "直调返回": {
        "ok": True, "http_status": 200, "url": "fixture-secret", "return_json": {
            "errcode": "0", "ttag": "0.0025", "CYWWZS": [
                {"code": "SPX", "prod_name": "标普 500", "last_px": "5800.50", "increase_rate": "1.28%"},
                {"code": "HSI", "prod_name": "恒生指数", "last_px": "0", "increase_rate": ""},
            ], "RMGP": [{"code": "AAPL", "prod_name": "Excluded company"}],
        },
    }}


class MacroArchive(TestCase):
    def test_live_refresh_keeps_collection_clock_separate_and_does_not_relabel_old_file(self):
        from modules.strategy_hermes.stage2.macro_refresh import normalize_live
        live=normalize_live(capture()["直调返回"]["return_json"],"2026-11-14T14:49:48+08:00")
        self.assertEqual(live["input_kind"],"LIVE")
        self.assertEqual(live["collected_at"],"2026-11-14T14:49:48+08:00")
        self.assertIsNone(live["observed_at"])
        self.assertIn("历史接口样例",normalize(capture(),"f"*64)["source"])

    def test_archive_keeps_only_market_groups_and_never_invents_clock(self):
        body = normalize(capture(), "f"*64)
        self.assertIsNone(body["observed_at"])
        self.assertEqual(body["observation_status"], "SOURCE_TIME_UNAVAILABLE")
        self.assertEqual(body["items"][0]["return_ratio"], "0.0128")
        self.assertIsNone(body["items"][0]["observed_at"])
        self.assertIsNone(body["items"][1]["price"])
        self.assertIsNone(body["items"][1]["return_ratio"])
        self.assertEqual([row["code"] for row in body["items"]], ["SPX", "HSI"])
        self.assertNotIn("fixture-secret", encode(body))
        with TemporaryDirectory() as directory:
            path = Path(directory, "saved.json")
            path.write_text(encode(body))
            self.assertEqual(read_background(path)["items"], body["items"])

    def test_invalid_rows_and_duplicate_codes_are_rejected(self):
        captured = capture()
        rows = captured["直调返回"]["return_json"]["CYWWZS"]
        rows.append(rows[0])
        with self.assertRaisesRegex(ValueError, "background identity"):
            normalize(captured, "f"*64)
