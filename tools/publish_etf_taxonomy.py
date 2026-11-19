"""Publish reviewed sector/index taxonomy using the ETF mapping transaction."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from modules.strategy_hermes.ledger import fingerprint
from modules.strategy_hermes.simulator import CHINA, moment
from modules.strategy_hermes.stage2.maintenance_store import live_mapping, publish_mapping
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.stage2.tushare_bootstrap import INDEX_CODE


def reviewed_taxonomy(value, mapping, at):
    version, rows = value["version"], value["rows"]
    if not isinstance(version, str) or not version or not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        raise ValueError("invalid taxonomy version or row count")
    indexes = {p["index_code"] for p in mapping["products"].values() if "index_code" in p}
    seen = set()
    for row in rows:
        sector, index = row["sector_id"], row["index_code"]
        key = sector, index
        if (sector not in mapping["sectors"] or index not in indexes or not INDEX_CODE.fullmatch(index)
                or key in seen or moment(row["available_at"]) > at
                or not all(isinstance(row[field], str) and row[field].strip()
                           for field in ("family_id", "canonical_branch_id"))
                or not isinstance(row["evidence_refs"], list) or not row["evidence_refs"]
                or any(not isinstance(ref, str) or not ref.strip() for ref in row["evidence_refs"])):
            raise ValueError("unverified or duplicate taxonomy mapping")
        seen.add(key)
    return dict(version=version, rows=sorted(rows, key=lambda row:(row["sector_id"], row["index_code"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment-file", required=True)
    parser.add_argument("--mapping-file", type=Path, required=True)
    parser.add_argument("--taxonomy-file", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.read(args.environment_file)
    ledger = PostgresLedger(settings, "LIVE" if settings.env == "production" else "FROZEN")
    at = datetime.now(CHINA)
    current = live_mapping(ledger, args.mapping_file, at)
    taxonomy = reviewed_taxonomy(json.loads(args.taxonomy_file.read_text(encoding="utf-8")), current, at)
    previous = current.get("etf_taxonomy") or {}
    if previous.get("version") == taxonomy["version"]:
        if previous != taxonomy:
            raise ValueError("changed taxonomy requires a new version")
        print(json.dumps(dict(status="CURRENT", mapping_version=current["version"], taxonomy_version=taxonomy["version"])))
        return
    updated = dict(current, etf_taxonomy=taxonomy, parent_mapping_version=current["version"])
    updated["version"] = "taxonomy-" + fingerprint(updated)[:32]
    summary = dict(taxonomy_version=taxonomy["version"], pairs=len(taxonomy["rows"]))
    publish_mapping(ledger, updated, summary, at, expected_parent=current["version"])
    print(json.dumps(dict(status="PUBLISHED", mapping_version=updated["version"], **summary)))


if __name__ == "__main__":
    main()
