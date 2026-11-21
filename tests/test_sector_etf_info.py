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


def mapping(sectors, products, weights):
    rows = lambda codes: [dict(code=code, weight=str(Decimal(1) / len(codes))) for code in codes]
    return dict(schema_version="stock-v4-etf-mapping-v1",
                sectors={sid: dict(sector_name=name, members=members, as_of_date="2026-11-12") for sid, (name, members) in sectors.items()},
                products={code: dict(name=name, index_code=index) for code, (name, index) in products.items()},
                weights={index: dict(as_of_date="2026-10-30", complete=True, rows=rows(codes)) for index, codes in weights.items()})


class MappingStore(Store):
    def __init__(self, versions):
        super().__init__()
        self.versions = versions
        self.bodies = []

    def execute(self, sql, args=()):
        if "maintained_mappings" not in sql:
            return super().execute(sql, args)
        assert sql.strip().startswith("SELECT") and args[0] == self.ns
        if "body" in sql:
            self.bodies.append(sorted(args[1]))
            self.rows = [(version, json.dumps(body)) for version, _, _, body in self.versions if version in args[1]]
        else:
            self.rows = [(version, at, json.dumps(summary)) for version, at, summary, _ in reversed(self.versions)][:args[2]]
        return self


class SectorEtfPenetrationTest(TestCase):
    NOW = datetime.fromisoformat("2026-11-21T10:00:00+08:00")

    def setUp(self):
        info._REACH.clear()
        products = {"512800.SH": ("银行ETF", "399986.SZ"), "512480.SH": ("半导体ETF", "H30184.CSI"),
                    "158001.SZ": ("宽基ETF", "000905.SH"), "158003.SZ": (None, "000905.SH"), "158005.SZ": ("无指数ETF", None)}
        old = mapping({BANK: ("银行", ["600036.SH"])}, products, {"399986.SZ": ["600036.SH", "601398.SH"]})
        new = mapping({BANK: ("银行", ["600036.SH"]), CHIP: ("芯片", ["688256.SH"]), CLOUD: ("AI应用", [])}, products,
                      {"399986.SZ": ["600036.SH", "601398.SH"], "H30184.CSI": ["688256.SH"], "931151.CSI": ["300750.SZ"]})
        new["products"]["159999.SZ"] = dict(name="电池ETF", index_code="931151.CSI")
        self.store = MappingStore([
            ("maintained-old", "2026-11-18T10:00:00+08:00", {"status": "OK"}, old),
            ("maintained-new", "2026-11-20T14:18:00+08:00",
             {"status": "PARTIAL", "message": "资料更新0项", "retry_queue": {"pending_count": 3, "review_required": 1}}, new)])

    def test_current_full_recent_and_gaps_follow_the_saved_report_and_mapping(self):
        view = info.read_penetration(self.store, self.NOW)
        self.assertEqual(view["current"], dict(
            report_id="saved-report-old", report_at=REPORT_AT, sectors=3, evaluated=3, matched=2, entry=1, observe=0, rejected=1,
            reasons=[dict(reason="PRODUCT_RULE_UNVERIFIED", etfs=1)], empty_sectors=[]))
        self.assertEqual(view["mapping"], dict(
            version="maintained-new", available_at="2026-11-20T14:18:00+08:00", catalog=6, weighted=3, reachable=2,
            sectors=3, indexes=5, weighted_indexes=3, weight_dates=["2026-10-30"], status="PARTIAL", message="资料更新0项",
            pending=3, review_required=1))
        self.assertEqual(view["recent"], [dict(
            version="maintained-new", available_at="2026-11-20T14:18:00+08:00", etf_count=1,
            etfs=[dict(code="512480.SH", name="半导体ETF", index="H30184.CSI", sectors=["芯片"])],
            sectors=[dict(id=CHIP, name="芯片"), dict(id=CLOUD, name="AI应用")], indexes=["931151.CSI", "H30184.CSI"])])
        self.assertEqual((view["recent_since"], view["recent_etf_count"]), ("2026-11-18T10:00:00+08:00", 1))
        self.assertEqual(view["gaps"], dict(
            index_count=2, etf_count=3, sectors=[dict(id=CLOUD, name="AI应用")],
            indexes=[dict(index="000905.SH", etfs=2, samples=[dict(code="158001.SZ", name="宽基ETF"), dict(code="158003.SZ", name=None)]),
                     dict(index=None, etfs=1, samples=[dict(code="158005.SZ", name="无指数ETF")])]))
        self.assertEqual(view["metadata_issues"], [])

    def test_report_sectors_without_products_are_named_and_bodies_are_read_once(self):
        saved = report()
        saved["input_meta"]["penetration"]["sectors"][CLOUD] = {"products": [], "reason": "SECTOR_MEMBERS_UNAVAILABLE"}
        self.store.latest = ("saved-report-old", REPORT_AT, json.dumps(saved))
        view = info.read_penetration(self.store, self.NOW)
        saved["input_meta"]["penetration"]["sectors"]["kaipanla:801464"] = {"products": []}
        saved["input_meta"]["penetration"]["sectors"][BANK]["products"].append(
            {"etf": "512480.SH", "matched": True, "rejection_reasons": [], "representativeness": {"entry_allowed": False}})
        self.store.latest = ("saved-report-old", REPORT_AT, json.dumps(saved))
        view = info.read_penetration(self.store, self.NOW)
        self.assertEqual(view["current"]["empty_sectors"], [
            dict(id="kaipanla:801464", name=None, in_mapping=False, reason=None),
            dict(id=CLOUD, name="AI应用", in_mapping=False, reason="SECTOR_MEMBERS_UNAVAILABLE")])
        self.assertEqual([view["current"][key] for key in ("evaluated", "matched", "entry", "observe", "rejected", "reasons")],
                         [2, 2, 1, 1, 0, []])
        info.read_penetration(self.store, self.NOW)
        self.assertEqual(self.store.bodies, [["maintained-new", "maintained-old"]])

    def test_explicit_relations_without_a_constituent_match_stay_inside_the_total(self):
        saved = report()
        saved["input_meta"]["penetration"]["sectors"][CLOUD]["products"] = [
            {"etf": "158001.SZ", "matched": [], "rejection_reasons": [], "representativeness": {"entry_allowed": True}},
            {"etf": "158003.SZ", "matched": [], "rejection_reasons": ["USABLE_PRICE_UNAVAILABLE"]}]
        self.store.latest = ("saved-report-old", REPORT_AT, json.dumps(saved))
        current = info.read_penetration(self.store, self.NOW)["current"]
        self.assertEqual([current[key] for key in ("evaluated", "matched", "entry", "observe", "rejected")], [4, 3, 2, 0, 1])
        self.assertEqual(current["reasons"], [dict(reason="PRODUCT_RULE_UNVERIFIED", etfs=1)])

    def test_a_full_cache_drops_other_versions_and_keeps_the_listed_history(self):
        info.read_penetration(self.store, self.NOW)
        info._REACH.update({("other", str(n)): {} for n in range(4 * info.MAPPING_VERSIONS)})
        body = copy.deepcopy(self.store.versions[1][3])
        body["products"]["512800.SH"]["index_code"] = None
        self.store.versions.append(("maintained-next", "2026-11-21T09:00:00+08:00", {}, body))
        view = info.read_penetration(self.store, self.NOW)
        self.assertEqual([row["version"] for row in view["recent"]], ["maintained-new"])
        self.assertEqual((view["mapping"]["version"], view["mapping"]["reachable"]), ("maintained-next", 1))
        self.assertEqual(sorted(version for _, version in info._REACH), ["maintained-new", "maintained-next", "maintained-old"])

    def test_no_mapping_no_report_and_invalid_versions_stay_empty(self):
        self.store.versions[1] = ("maintained-new", "2026-11-20T14:18:00+08:00", {}, {"sectors": []})
        self.store.latest = None
        view = info.read_penetration(self.store, self.NOW)
        self.assertEqual(view["current"], dict(report_id=None, report_at=None, sectors=0, evaluated=0, matched=0, entry=0, observe=0,
                                               rejected=0, reasons=[], empty_sectors=[]))
        self.assertEqual((view["mapping"]["version"], view["mapping"]["reachable"], view["recent"]), ("maintained-old", 1, []))
        self.assertEqual(view["metadata_issues"], ["MAPPING_VERSION_INVALID"])
        self.store.versions = []
        info._REACH.clear()
        view = info.read_penetration(self.store, self.NOW)
        self.assertEqual((view["mapping"], view["recent"], view["recent_since"], view["gaps"]["index_count"]), (None, [], None, 0))

    def test_weights_dated_after_now_do_not_count(self):
        self.store.versions[1][3]["weights"]["H30184.CSI"]["as_of_date"] = "2026-11-30"
        view = info.read_penetration(self.store, self.NOW)
        self.assertEqual((view["mapping"]["weighted"], view["mapping"]["reachable"], view["recent_etf_count"]), (2, 1, 0))
