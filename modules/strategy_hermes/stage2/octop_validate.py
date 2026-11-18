"""Build and score controlled OCTOP decision cases against frozen expectations."""
import argparse
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from shared_kernel.temp_manager import TempManager
from ..ledger import encode
from .build_sources import store_file
from .inputs import document, read_file
from .octop_decision import DECISION_PROTOCOL, PROMPT, render

FROZEN = (
    ("new", False, None, ("0.032", "0.81", "0.027", "74"), None, "NEW", "UP"),
    ("maintain", True, ("0.018", "0.72", "0.016", "60"), ("0.018", "0.72", "0.016", "60"), None, "MAINTAIN", "HOLD"),
    ("strengthen-low-coverage", True, ("0.009", "0.61", "0.008", "41"), ("0.028", "0.83", "0.024", "77"), None, "STRENGTHEN", "UP"),
    ("weaken-held", True, ("0.031", "0.83", "0.027", "78"), ("0.008", "0.58", "0.006", "37"), "催化仍在兑现，主导成分未出现集中下破", "WEAKEN", "WEAK"),
    ("retreat-held", True, ("0.021", "0.74", "0.019", "65"), ("-0.047", "0.11", "-0.039", "9"), "连续三个可比时点仅一成成分上涨，主要成分持续跌破前低；支撑该方向的主事件已终止", "RETREAT", "EXIT"),
    ("not-covered-held", True, ("0.017", "0.72", "0.016", "58"), None, None, "NOT_COVERED", "UNKNOWN"),
)


def _frozen_rows(values, key, at, note=None):
    if values is None:
        return []
    board, breadth, etf, strength = values
    rows = [
        dict(ref=f"frozen:{key}:board", observed_at=at, board_return_ratio=board,
             covered_constituents_up_ratio=breadth, relative_strength_percentile=strength),
        dict(ref=f"frozen:{key}:etf", observed_at=at, etf_return_ratio=etf),
    ]
    if note:
        rows.append(dict(ref=f"frozen:{key}:evidence", observed_at=at, observed_event=note))
    return rows


def cases(preview):
    day = preview["trade_date"]
    board = preview["board"]
    product, = preview["penetration"]
    quote = product["quote"]
    metrics = product["metrics"]
    etf_return = (Decimal(quote["price_cny"]) / Decimal(quote["pre_close_cny"])) - 1
    actual = {
        "input_kind": "OBSERVED_AFTER_HOURS",
        "sector_id": board["sector_id"], "sector_name": board["sector_name"],
        "trade_date": day, "data_as_of": preview["observed_at"], "prior_active": None,
        "previous": [],
        "current": [
            dict(ref=f"{board['sector_id']}@{board['observed_at']}#{board['source_batch']}",
                 observed_at=board["observed_at"], board_return_ratio=board["return_ratio"]),
            dict(ref=f"{quote['source']}:{product['etf']}@{quote['observed_at']}",
                 observed_at=quote["observed_at"], etf_return_ratio=str(etf_return)),
        ],
        "facts": {
            "etf": product["etf"], "board_return_ratio": board["return_ratio"],
            "etf_return_ratio": str(etf_return), "board_observed_at": board["observed_at"],
            "etf_observed_at": quote["observed_at"], "weight_as_of": product["weight_as_of"],
            "membership_as_of": preview["membership"]["as_of_date"],
            "sector_exposure": metrics["E_sector"], "constituent_quote_coverage": metrics["Q"],
            "mainboard_weight": metrics["M"], "quality": product["quality"],
            "rejection_reasons": product["rejection_reasons"],
        },
    }
    result = {"actual-conflict": (actual, "UNKNOWN", "UNKNOWN")}
    for name, active, previous, current, note, phase, state in FROZEN:
        prior_time = f"{day}T10:35:00+08:00"
        current_time = f"{day}T13:05:00+08:00"
        exposure = "0.91"
        coverage = "0.60" if name == "strengthen-low-coverage" else "0.93"
        facts = dict(sector_exposure=exposure, constituent_quote_coverage=coverage, mainboard_weight="0.71",
                     quality=["IOPV_MISSING"])
        if coverage == "0.60":
            facts["quality"].append("LOW_COVERAGE")
        if current:
            facts.update(board_return_ratio=current[0], etf_return_ratio=current[2])
        else:
            facts.update(constituent_quote_coverage=None)
        evidence = {
            "input_kind": "FROZEN",
            "sector_id": "kaipanla:TEST01", "sector_name": "冻结样本板块",
            "trade_date": day, "data_as_of": current_time, "prior_active": active,
            "previous": _frozen_rows(previous, "previous", prior_time),
            "current": _frozen_rows(current, "current", current_time, note),
            "facts": facts,
        }
        result[name] = (evidence, phase, state)
    return result


def score(cases_by_name, receipts, skill_sha256):
    if not isinstance(receipts, list) or len(receipts) != len(cases_by_name):
        raise ValueError("incomplete OCTOP validation batch")
    by_name = {receipt["case"]: receipt for receipt in receipts}
    if set(by_name) != set(cases_by_name):
        raise ValueError("OCTOP validation batch case IDs differ")
    results = []
    for name, (evidence, expected_phase, expected_state) in cases_by_name.items():
        receipt = by_name[name]
        result = dict(case=name, evidence=evidence, expected_phase=expected_phase,
                      expected_state=expected_state, original_response=receipt)
        try:
            if (receipt.get("requests") != 1 or receipt.get("http_status") != [200]
                    or receipt.get("tool_calls") != 0 or receipt.get("tools_exposed") != []
                    or receipt.get("skill_sha256") != skill_sha256
                    or receipt.get("input_sha256") != sha256(encode(evidence).encode()).hexdigest()):
                raise ValueError("OCTOP receipt does not match the case or request budget")
            result.update(render(receipt, evidence))
            decision = document(receipt["output"])
            refs = set(decision["evidence_refs"])
            if name == "actual-conflict" and refs != {row["ref"] for row in evidence["current"]}:
                raise ValueError("conflicting direction must cite both observed sources")
            if name == "retreat-held" and not {"frozen:current:board", "frozen:current:evidence"} <= refs:
                raise ValueError("retreat case must cite both market and termination evidence")
            state = result["normalized"][evidence["sector_id"]]["state"]
            result["passed"] = (decision["phase"], state) == (expected_phase, expected_state)
        except (ValueError, TypeError, KeyError, IndexError, ArithmeticError) as exc:
            result.update(passed=False, failure={"type": type(exc).__name__, "message": str(exc)})
        results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--skill", type=Path)
    args = parser.parse_args(argv)
    batch = cases(read_file(args.preview))
    if args.output_dir is None:
        print(encode({"protocol": DECISION_PROTOCOL, "prompt": PROMPT,
                      "inputs": {name: encode(case[0]) for name, case in batch.items()}}))
        return 0
    if args.skill is None:
        parser.error("--skill is required to verify the archived receipts")
    output = args.output_dir
    if not output.is_absolute() or output.resolve() != output or not output.is_dir() or output.stat().st_mode & 0o077:
        parser.error("output requires a private absolute directory")
    incoming = sys.stdin.buffer.read(256 * 1024 + 1)
    if len(incoming) > 256 * 1024:
        parser.error("OCTOP validation responses exceed the size limit")
    payload = document(incoming)
    if payload.get("protocol") != DECISION_PROTOCOL:
        parser.error("unexpected OCTOP validation protocol")
    records = score(batch, payload["receipts"], sha256(args.skill.read_bytes()).hexdigest())
    accepted = sum(row["passed"] for row in records)
    raw = (json.dumps({"protocol": DECISION_PROTOCOL, "results": records}, ensure_ascii=False, indent=2) + "\n").encode()
    digest = sha256(raw).hexdigest()
    manager = TempManager(os.environ["APP_TMP_ROOT"], persistent=output)
    path = store_file(manager, output, f"octop-direction-validation-{digest[:24]}.json", raw)
    print(encode(dict(path=path, passed=accepted, total=len(records),
                      results={row["case"]: row["passed"] for row in records})))
    return 0 if accepted == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
