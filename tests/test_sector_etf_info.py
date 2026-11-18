"""Saved sector/ETF pagination, provenance and exact presentation arithmetic."""
from contextlib import contextmanager
import copy
from datetime import datetime
from decimal import Decimal
import json
from unittest import TestCase
from unittest.mock import patch

from modules.strategy_hermes import sector_etf_info as info

SNAPSHOT_AT = "2026-11-13T14:30:12+08:00"
QUOTE_AT = "2026-11-13T14:23:27+08:00"
REPORT_AT = "2026-10-20T10:35:00+08:00"
CHIP, BANK, CLOUD = "kaipanla:801001", "kaipanla:801027", "kaipanla:803023"


def fixture():
    quote = dict(price_cny="1.01", pre_close_cny="1", amount_cny="100.01", volume_shares="1000",
                 observed_at=QUOTE_AT, source="tickflow")
    return dict(snapshot_id="saved-market-1", trade_date="2026-11-13", observed_at=SNAPSHOT_AT,
                mode="PARTIAL_REALTIME", source="tickflow+kaipanla", quality={"etf_quote_count": 6, "board_count": 3},
                sectors=[dict(sector_id=CHIP, sector_name="芯片", return_ratio="-0.02754", strength="4521", observed_at=QUOTE_AT),
                         dict(sector_id=BANK, sector_name="银行", return_ratio="0", strength="0", observed_at=None),
                         dict(sector_id=CLOUD, sector_name="AI应用", return_ratio=None, strength=None, observed_at=QUOTE_AT)],
                quotes={"512800.SH": dict(quote, name="不采用行情附带名"),
                        "512480.SH": dict(quote, price_cny="0.99", amount_cny="0"),
                        "158000.SZ": dict(quote, price_cny="1", amount_cny=None),
                        "158001.SZ": dict(quote, price_cny="1.02", observed_at=None),
                        "158003.SZ": dict(quote, price_cny="1.02", pre_close_cny="0", amount_cny="NaN"),
                        "158005.SZ": dict(quote, price_cny="1.02", amount_cny="Infinity")})


def report():
    return dict(candidates={"512800.SH": [dict(sector_id=BANK)], "512480.SH": [dict(sector_id=CHIP)]},
                sectors={BANK: {"row": 0}, CHIP: {"row": 1}},
                raw=json.dumps({"sectors": [{"sector_name": "银行旧名称"}, {"sector_name": "芯片"}]}),
                input_meta={"penetration": {"sectors": {
                    BANK: {"products": [{"etf": "512800.SH", "matched": [{"code": "600036.SH"}], "rejection_reasons": []}]},
                    CHIP: {"products": [{"etf": "512480.SH", "matched": [{"code": "688256.SH"}], "rejection_reasons": ["PRODUCT_RULE_UNVERIFIED"]}]},
                    CLOUD: {"products": [{"etf": "158001.SZ", "matched": [], "rejection_reasons": []}]}}}})


class Store:
    ns = "stock-v4:test:sector-info"

    def __init__(self):
        self.latest = ("saved-report-old", REPORT_AT, json.dumps(report()))
        self.catalog = [("512800.SH", json.dumps({"csname": "华宝中证银行ETF"}))]
        self.sql = []
        self.rows = []

    @contextmanager
    def _connect(self):
        yield self

    def execute(self, sql, args=()):
        assert sql.strip().startswith("SELECT"), sql
        self.sql.append((sql, args))
        if "FROM events" in sql:
            assert "ns=%s" in sql and args == (self.ns,)
            assert "ORDER BY at DESC,id DESC LIMIT 1" in sql
            self.rows = [self.latest] if self.latest else []
        elif "supplier_etf_catalog" in sql:
            self.rows = [row for row in self.catalog if row[0] in args[0]]
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows[:]


class SectorEtfInfoTest(TestCase):
    def setUp(self):
        self.market = fixture()
        self.store = Store()
        self.market_patch = patch.object(info, "read_market", side_effect=lambda ledger, fallback: copy.deepcopy(self.market))
        self.market_patch.start()
        self.addCleanup(self.market_patch.stop)

    def read(self, query="", **kwargs):
        return info.read_info(self.store, query, **kwargs)

    def test_defaults_sector_counts_and_unfiltered_summary(self):
        result = self.read("q=银行")
        self.assertEqual((result["kind"], result["page"], result["page_size"]), ("sectors", 1, 20))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["id"], BANK)
        self.assertEqual(result["summary"]["counts"], {"up": 0, "down": 1, "flat": 1, "unknown": 1})
        self.assertEqual(result["summary"]["etf_amount_cny"], "200.02")
        self.assertEqual(result["summary"]["etf_amount_count"], 3)

    def test_pagination_is_stable_and_response_size_bounded(self):
        first = self.read("kind=etfs&sort=code_asc&page=1&page_size=2")
        second = self.read("kind=etfs&sort=code_asc&page=2&page_size=2")
        self.assertEqual((first["total"], first["pages"]), (6, 3))
        self.assertEqual([r["code"] for r in first["items"]], ["158000.SZ", "158001.SZ"])
        self.assertEqual([r["code"] for r in second["items"]], ["158003.SZ", "158005.SZ"])
        self.assertEqual(self.read("kind=etfs&page=999&page_size=100")["items"], [])

    def test_search_uses_saved_catalog_names_and_codes(self):
        value = self.read("kind=etfs&q=银行")
        self.assertEqual([row["code"] for row in value["items"]], ["512800.SH"])
        self.assertEqual(self.read("kind=etfs&q=15800")["total"], 4)
        self.assertEqual(self.read("kind=etfs&q=不采用行情附带名")["total"], 0)

    def test_sector_search_ignores_shared_provider_prefix_unless_explicit(self):
        value = self.read("q=AI")
        self.assertEqual(value["total"], 1)
        self.assertEqual(value["items"][0]["id"], CLOUD)
        self.assertEqual(self.read("q=801027")["items"][0]["id"], BANK)
        self.assertEqual(self.read("q=kaipanla%3A801027")["items"][0]["id"], BANK)

    def test_missing_prices_and_amounts_sort_last_but_zero_is_preserved(self):
        value = self.read("kind=etfs&sort=amount_desc")
        self.assertEqual([row["code"] for row in value["items"]],
                         ["158001.SZ", "512800.SH", "512480.SH", "158000.SZ", "158003.SZ", "158005.SZ"])
        zero = next(row for row in value["items"] if row["code"] == "512480.SH")
        self.assertEqual(zero["amount_cny"], "0")
        self.assertIsNone(value["items"][-1]["amount_cny"])

    def test_ratios_are_exact_and_ties_use_codes(self):
        descending = self.read("kind=etfs&sort=return_desc")["items"]
        self.assertEqual([row["code"] for row in descending],
                         ["158001.SZ", "158005.SZ", "512800.SH", "158000.SZ", "512480.SH", "158003.SZ"])
        self.assertEqual(descending[0]["return_ratio"], "0.02")
        self.assertEqual(self.read("kind=etfs&sort=return_asc")["items"][-1]["code"], "158003.SZ")
        self.market["quotes"]["158001.SZ"]["price_cny"] = "1.000000000000000001"
        self.market["quotes"]["158005.SZ"]["price_cny"] = "1.000000000000000002"
        ordered = self.read("kind=etfs&sort=return_desc")["items"]
        self.assertLess([r["code"] for r in ordered].index("158005.SZ"), [r["code"] for r in ordered].index("158001.SZ"))

    def test_repeating_decimal_ratio_remains_sortable(self):
        self.market["quotes"]["158001.SZ"].update(price_cny="4", pre_close_cny="3")
        first = self.read("kind=etfs&sort=return_desc")["items"][0]
        self.assertEqual(first["code"], "158001.SZ")
        self.assertGreater(Decimal(first["return_ratio"]), Decimal("0.3333333333"))

    def test_finite_json_floats_are_decimalized_and_nonfinite_are_missing(self):
        self.market["sectors"][0].update(return_ratio=0.015, strength=4521.5)
        self.market["quotes"]["158001.SZ"].update(price_cny=1.2, pre_close_cny=1.0, amount_cny=100.5)
        sector = self.read("q=芯片")["items"][0]
        self.assertEqual((sector["return_ratio"], sector["strength"]), ("0.015", "4521.5"))
        quote = self.read("kind=etfs&q=158001")["items"][0]
        self.assertEqual((quote["return_ratio"], quote["amount_cny"]), ("0.2", "100.5"))
        self.market["quotes"]["158001.SZ"]["amount_cny"] = float("nan")
        self.assertIsNone(self.read("kind=etfs&q=158001")["items"][0]["amount_cny"])

    def test_old_saved_report_relationship_keeps_original_provenance(self):
        result = self.read()
        bank = next(row for row in result["items"] if row["id"] == BANK)
        chip = next(row for row in result["items"] if row["id"] == CHIP)
        self.assertEqual(bank["etfs"], [{"code": "512800.SH", "name": "华宝中证银行ETF",
                                       "report_id": "saved-report-old", "report_at": REPORT_AT}])
        self.assertEqual(chip["etfs"], [])
        etf = self.read("kind=etfs&q=512800")["items"][0]
        self.assertEqual(etf["sectors"][0]["name"], "银行")
        self.assertEqual(etf["sectors"][0]["report_at"], REPORT_AT)
        self.assertEqual(result["summary"]["relationship_report_at"], REPORT_AT)

    def test_matched_products_link_without_candidates_and_rejected_do_not(self):
        saved = report()
        saved["candidates"] = {}
        products = saved["input_meta"]["penetration"]["sectors"][CLOUD]["products"]
        products[0]["matched"] = True
        products.append({"etf": "158005.SZ", "matched": True, "recommendation_status": "REJECTED"})
        self.store.latest = ("r2", REPORT_AT, json.dumps(saved))
        cloud = self.read("q=AI")["items"][0]
        self.assertEqual([row["code"] for row in cloud["etfs"]], ["158001.SZ"])

    def test_latest_report_without_associations_does_not_recover_older_links(self):
        self.store.latest = ("new-report", SNAPSHOT_AT, json.dumps({"sectors": {}, "candidates": {}}))
        value = self.read()
        self.assertTrue(all(not row["etfs"] for row in value["items"]))
        self.assertEqual(value["summary"]["relationship_report_id"], "new-report")

    def test_mapping_names_are_fallback_and_never_create_relations(self):
        mapping = {"schema_version": "stock-v4-etf-mapping-v1", "products": {
            "512800.SH": {"name": "映射旧名"}, "512480.SH": {"name": "半导体ETF"}, "158001.SZ": {"name": "银行主题ETF"}}}
        with patch.object(info, "read_file", return_value=mapping):
            result = self.read("kind=etfs&sort=name_asc", mapping_file="mapping.json")
        names = {row["code"]: row["name"] for row in result["items"]}
        self.assertEqual(names["512800.SH"], "华宝中证银行ETF")
        self.assertEqual(names["512480.SH"], "半导体ETF")
        self.assertIsNone(names["158000.SZ"])
        self.assertEqual(result["summary"]["named_etf_count"], 3)
        self.assertEqual(next(row for row in result["items"] if row["code"] == "158001.SZ")["sectors"], [])
        self.assertIsNone(result["items"][-1]["name"])

    def test_missing_mapping_and_invalid_mapping_are_distinguished(self):
        with patch.object(info, "read_file", side_effect=FileNotFoundError):
            value = self.read(mapping_file="missing.json")
        self.assertEqual(value["summary"]["metadata_issues"], [])
        with patch.object(info, "read_file", side_effect=ValueError("bad JSON")):
            value = self.read(mapping_file="invalid.json")
        self.assertIn("MAPPING_FILE_INVALID", value["summary"]["metadata_issues"])
        self.assertEqual(value["summary"]["named_etf_count"], 1)

    def test_row_timestamps_are_never_replaced_by_snapshot_timestamp(self):
        rows = {row["code"]: row for row in self.read("kind=etfs")["items"]}
        self.assertEqual(rows["512800.SH"]["observed_at"], QUOTE_AT)
        self.assertIsNone(rows["158001.SZ"]["observed_at"])
        bank = self.read("q=银行")["items"][0]
        self.assertIsNone(bank["observed_at"])

    def test_invalid_parameters_are_rejected_before_any_database_access(self):
        invalid = ["kind=other", "kind=etfs&sort=strength_desc", "kind=sectors&sort=amount_desc", "q=" + "长" * 81,
                   "page=0", "page=-1", "page=1.2", "page_size=101", "page_size=0", "page=１", "page=1&page=2", "extra=yes"]
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.read(query)
        self.assertEqual(self.store.sql, [])

    def test_only_read_queries_and_no_provider_calls(self):
        with patch("modules.strategy_hermes.stage2.market_bootstrap.fetch", side_effect=AssertionError("no collection")), \
             patch("modules.strategy_hermes.stage2.http_json.get_json", side_effect=AssertionError("no collection")), \
             patch("modules.strategy_hermes.stage2.penetration.explain_sector", side_effect=AssertionError("do not recompute associations")):
            self.read()
        self.assertEqual(len(self.store.sql), 2)
        self.assertTrue(all(sql.strip().startswith("SELECT") for sql, _ in self.store.sql))

    def test_bad_numeric_values_stay_missing_and_empty_snapshot_is_valid(self):
        self.market["quotes"]["512800.SH"].update(price_cny=True, pre_close_cny="0", amount_cny="-1", volume_shares="NaN")
        row = self.read("kind=etfs&q=512800")["items"][0]
        self.assertTrue(all(row[key] is None for key in ("price_cny", "return_ratio", "amount_cny", "volume_shares")))
        self.market = None
        self.store.latest = None
        result = self.read()
        self.assertEqual(result["items"], [])
        self.assertEqual((result["total"], result["pages"]), (0, 1))
        self.assertIsNone(result["summary"]["etf_amount_cny"])
