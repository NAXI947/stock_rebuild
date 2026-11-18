"""Verify and archive an isolated OCTOP report receipt without publishing a round."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from shared_kernel.temp_manager import TempManager
from ..adapter import READ_ONLY_OBSERVATION, normalize
from .build_sources import store_file
from .inputs import document

MAX_RECEIPT_BYTES = 256 * 1024


def verify(receipt, preview_bytes, skill_bytes):
    preview = document(preview_bytes)
    if (preview.get("schema_version") != "stock-v4-octop-preview-v1"
            or preview.get("purpose") != READ_ONLY_OBSERVATION):
        raise ValueError("unsupported OCTOP preview input")
    if (receipt.get("input_sha256") != sha256(preview_bytes).hexdigest()
            or receipt.get("skill_sha256") != sha256(skill_bytes).hexdigest()
            or receipt.get("snapshot_id") != preview["snapshot_id"]):
        raise ValueError("OCTOP receipt does not match the snapshot and skill")
    if (receipt.get("requests") != 1 or receipt.get("http_status") != [200]
            or receipt.get("tools_exposed") != [] or receipt.get("tool_calls") != 0):
        raise ValueError("unexpected OCTOP request or tool activity")

    raw = receipt["output"]
    report = document(raw)
    normalized = normalize({"report_json": raw}, allow_read_only=True)
    expected = {
        "purpose": READ_ONLY_OBSERVATION,
        "report_id": receipt["report_id"],
        "generated_at": receipt["generated_at"],
        "trade_date": preview["trade_date"],
        "data_as_of": preview["observed_at"],
        "snapshot_id": preview["snapshot_id"],
        "mapping_version": preview["mapping_version"],
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("OCTOP report metadata differs from the supplied evidence")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise ValueError("OCTOP report requires a summary")
    for key in ("judgments", "candidates", "orders"):
        if report.get(key):
            raise ValueError("read-only OCTOP report contains action instructions")

    board = preview["board"]
    sector_id = board["sector_id"]
    sectors = report["sectors"]
    if (len(sectors) != 1 or sectors[0]["sector_name"] != board["sector_name"]
            or set(normalized["sectors"]) != {sector_id}
            or normalized["diagnostics"]):
        raise ValueError("OCTOP report sector differs from the supplied evidence")
    sector = sectors[0]
    if (normalized["sectors"][sector_id]["state"] != "UNKNOWN"
            or sector.get("observation_state") != "待核实"
            or sector.get("signal") != "UNCERTAIN"
            or normalized["status"] != "PARTIAL"):
        raise ValueError("isolated observation requires a partial, unconfirmed direction")
    for items in (report["warnings"], sector["reasons"], sector["risks"]):
        if not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items):
            raise ValueError("invalid OCTOP evidence text")

    etf = report["etf_observation"]
    product = next((item for item in preview["penetration"] if item["etf"] == etf["etf"]), None)
    if product is None or not product.get("quote"):
        raise ValueError("OCTOP report ETF is missing from the supplied evidence")
    quote = product["quote"]
    source_refs = [
        f"{sector_id}@{board['observed_at']}#{board['source_batch']}",
        f"{quote['source']}:{product['etf']}@{quote['observed_at']}",
    ]
    if (sector.get("sources") != source_refs or etf.get("observed_at") != quote["observed_at"]
            or etf.get("weight_as_of") != product["weight_as_of"]
            or etf.get("rejection_reasons") != product["rejection_reasons"]):
        raise ValueError("OCTOP report source references or ETF evidence do not match")
    return {
        "normalized": {key: value for key, value in normalized.items() if key != "raw"},
        "source_facts": {
            "sector_id": sector_id,
            "board_observed_at": board["observed_at"],
            "etf": product["etf"],
            "etf_observed_at": quote["observed_at"],
            "membership_as_of": preview["membership"]["as_of_date"],
            "weight_as_of": product["weight_as_of"],
            "sector_exposure": product["metrics"]["E_sector"],
            "constituent_quote_coverage": product["metrics"]["Q"],
            "mainboard_weight": product["metrics"]["M"],
            "quality": product["quality"],
            "rejection_reasons": product["rejection_reasons"],
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output_dir
    if not output.is_absolute() or output.resolve() != output or not output.is_dir() or output.stat().st_mode & 0o077:
        parser.error("output requires a private absolute directory")

    receipt_bytes = sys.stdin.buffer.read(MAX_RECEIPT_BYTES + 1)
    if len(receipt_bytes) > MAX_RECEIPT_BYTES:
        parser.error("OCTOP receipt exceeds the size limit")
    receipt = document(receipt_bytes)
    record = {
        "schema_version": "stock-v4-octop-readonly-test-v1",
        "purpose": READ_ONLY_OBSERVATION,
        "original_response": receipt,
    }
    try:
        record.update(verify(receipt, args.preview.read_bytes(), args.skill.read_bytes()))
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        record.update(acceptance="FAILED", failure={"type": type(exc).__name__, "message": str(exc)})
    else:
        record["acceptance"] = "PASSED"

    content = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
    digest = sha256(content).hexdigest()
    manager = TempManager(os.environ["APP_TMP_ROOT"], persistent=output)
    path = store_file(manager, output, f"octop-readonly-test-{digest[:24]}.json", content)
    print(json.dumps({"path": path, "sha256": digest, "acceptance": record["acceptance"],
                      "failure": record.get("failure")}, ensure_ascii=False))
    return 0 if record["acceptance"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
