"""Export published live candidates and evaluate their three-day touches offline."""

import argparse
from bisect import bisect_right
from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

import psycopg

from modules.strategy_hermes.adapter import BOARD, READ_ONLY_OBSERVATION, unique_object
from modules.strategy_hermes.ledger import encode, fingerprint
from modules.strategy_hermes.simulator import CHINA, CODE, Calendar, moment
from modules.strategy_hermes.stage2.etf_daily_history import read_prices
from modules.strategy_hermes.stage2.inputs import trading_days
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings
from modules.strategy_hermes.three_day_touch import LIMIT, SCHEMA, evaluate
from shared_kernel.temp_manager import BudgetError, TempManager, store_file

EXPORT_SCHEMA = "stock-v4-live-touch-export-v1"
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
NAMESPACES = {"production": "stock-v4:prod:", "development": "stock-v4:dev:"}


def _read(source):
    raw = source.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("live touch evidence exceeds 16 MiB")
    return json.loads(raw, object_pairs_hook=unique_object)


def _candidate_samples(report_id, report, event_sha, day):
    raw = report["raw"]
    original = json.loads(raw, object_pairs_hook=unique_object)
    at = moment(report["at"])
    if (report["id"] != report_id or original["report_id"] != report_id
            or at.date() != day or original["trade_date"] != day.isoformat()
            or original.get("purpose") == READ_ONLY_OBSERVATION
            or original.get("schema_version") != "hermes_sector_rotation_v1"):
        raise ValueError("published report identity does not match its live round")
    report_sha = sha256(raw.encode()).hexdigest()
    for etf, choices in report["candidates"].items():
        if not CODE.fullmatch(etf):
            raise ValueError("invalid published candidate ETF")
        for candidate in choices:
            sector_id = candidate["sector_id"]
            current = report["sectors"][sector_id]
            if current["state"] != "UP":
                continue
            sector = original["sectors"][current["row"]]
            signal = sector["observation_state"]
            refs = sector["sources"]
            if ("kaipanla:" + sector["kaipanla_plate_id"] != sector_id
                    or not isinstance(signal, str) or not signal.strip()
                    or not isinstance(refs, list) or not refs
                    or not all(isinstance(ref, str) and ref.strip() for ref in refs)
                    or not isinstance(candidate["mapping_ref"], str) or not candidate["mapping_ref"]):
                raise ValueError("published candidate lacks original signal lineage")
            yield {
                "report_id": report_id, "original_signal": signal, "etf": etf,
                "published_at": at.isoformat(), "sources": sorted(set(refs) | {sector_id}),
                "sector_id": sector_id, "mapping_ref": candidate["mapping_ref"],
                "report_sha256": report_sha, "event_sha256": event_sha,
            }


def export_samples(settings, day, *, now=None):
    prefix = NAMESPACES.get(settings.env)
    if not prefix or not settings.namespace.startswith(prefix):
        raise ValueError("live samples require a production or development namespace")
    ledger = PostgresLedger(settings, "LIVE")
    now = now or datetime.now(CHINA)
    if day > now.date():
        raise ValueError("export day must not be in the future")
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = conn.execute(
            """SELECT t.report_id,e.fingerprint,e.payload
               FROM round_tasks t JOIN runs r ON r.ns=t.ns AND r.origin='LIVE'
               JOIN events e ON (e.ns,e.id)=(t.ns,t.report_id) AND e.kind='report'
               WHERE t.ns=%s AND t.trade_date=%s AND t.state='PUBLISHED' ORDER BY t.slot""",
            (ledger.ns, day),
        ).fetchall()
    samples = [sample for rid, digest, payload in rows
               for sample in _candidate_samples(rid, json.loads(payload), digest, day)]
    return dict(schema_version=EXPORT_SCHEMA, input_kind="LIVE", environment=settings.env,
                namespace=settings.namespace, trade_date=day.isoformat(), exported_at=now.isoformat(),
                reports_examined=len(rows), samples=samples)


def _validate_export(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("live sample export must be a JSON object")
    prefix = NAMESPACES.get(bundle.get("environment"))
    if (bundle.get("schema_version") != EXPORT_SCHEMA or bundle.get("input_kind") != "LIVE"
            or not prefix or not bundle.get("namespace", "").startswith(prefix)
            or not isinstance(bundle.get("samples"), list) or type(bundle.get("reports_examined")) is not int
            or bundle["reports_examined"] < 0):
        raise ValueError("invalid live sample export")
    day = date.fromisoformat(bundle["trade_date"])
    exported_at = moment(bundle["exported_at"])
    if exported_at.date() < day:
        raise ValueError("live export precedes its trading day")
    for row in bundle["samples"]:
        at = moment(row["published_at"])
        if (at.date() != day or at > exported_at or not BOARD.fullmatch(row["sector_id"])
                or not DIGEST.fullmatch(row["report_sha256"]) or not DIGEST.fullmatch(row["event_sha256"])
                or row["sector_id"] not in row["sources"] or not row["mapping_ref"]):
            raise ValueError("live sample has invalid original report lineage")
    if len({row["report_id"] for row in bundle["samples"]}) > bundle["reports_examined"]:
        raise ValueError("live sample count exceeds the exported reports")


def build_bundle(exports, days, as_of, prices):
    for export in exports:
        _validate_export(export)
    calendar = Calendar(days).days
    samples = [row for export in exports for row in export["samples"]]
    windows = {code: set() for code in {row["etf"] for row in samples}}
    for row in samples:
        published = moment(row["published_at"])
        after = bisect_right(calendar, published.date())
        windows[row["etf"]].update(calendar[after:after + 3])
    selected = [row for row in prices
                if date.fromisoformat(row["trade_date"]) in windows.get(row["etf"], ())]
    return dict(schema_version=SCHEMA, as_of=as_of.isoformat(), calendar=days, samples=samples, prices=selected,
                live_exports=[{k:v for k,v in export.items() if k != "samples"} | {"sha256": fingerprint(export)}
                              for export in exports])


def _local_output(output):
    project = Path(__file__).resolve().parents[1]
    temp = Path(os.environ["APP_TMP_ROOT"])
    if (not output.is_absolute() or output.resolve() != output or not output.is_dir()
            or output.is_relative_to(project) or output.stat().st_mode & 0o077
            or temp.resolve().is_relative_to(project) or temp.resolve().is_relative_to(output)):
        raise ValueError("use private distinct temporary and persistent directories outside the project")
    return TempManager(temp, persistent=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    export = modes.add_parser("export")
    export.add_argument("--day", required=True, type=date.fromisoformat)
    export.add_argument("--env-file", type=Path, help="optional private file for a development live trial")
    export.add_argument("--dev-namespace", help="explicit development trial namespace")
    run = modes.add_parser("evaluate")
    run.add_argument("--env-file", required=True, type=Path, help="isolated test database for saved daily bars")
    run.add_argument("--calendar", required=True, type=Path)
    run.add_argument("--output-dir", required=True, type=Path)
    run.add_argument("--as-of", type=moment, help="defaults to current Asia/Shanghai time")
    run.add_argument("--batch", action="append", default=[], help="repeat for each existing fund_daily batch ID")
    run.add_argument("exports", nargs="+", type=Path, help="sanitized JSON export files, or - for stdin")
    args = parser.parse_args(argv)
    try:
        if args.mode == "export":
            settings = Settings.read(args.env_file) if args.env_file else Settings.from_values(os.environ)
            if args.env_file and settings.env != "development":
                raise ValueError("the export env file is limited to development")
            if args.dev_namespace:
                if settings.env != "development" or not args.dev_namespace.startswith(settings.redis_prefix):
                    raise ValueError("the export namespace override is limited to development")
                settings = replace(settings, namespace=args.dev_namespace)
            print(encode(export_samples(settings, args.day)))
            return 0
        settings = Settings.read(args.env_file)
        if settings.env != "test":
            raise ValueError("local ETF daily bars must be read from the isolated test database")
        exports = []
        if args.exports.count(Path("-")) > 1:
            raise ValueError("stdin may be used for one live export")
        for path in args.exports:
            if path == Path("-"):
                exports.append(_read(sys.stdin.buffer))
            else:
                with path.open("rb") as source:
                    exports.append(_read(source))
        for item in exports:
            _validate_export(item)
        if not any(item["samples"] for item in exports):
            print(encode({"status": "NO_ELIGIBLE_SAMPLES", "samples": 0,
                          "reports_examined": sum(item["reports_examined"] for item in exports)}))
            return 0
        as_of = args.as_of or datetime.now(CHINA)
        if as_of > datetime.now(CHINA):
            raise ValueError("evaluation time must not be in the future")
        store = PostgresLedger(settings, "FROZEN")
        prices = [row for batch in dict.fromkeys(args.batch) for row in read_prices(store,batch)]
        bundle = build_bundle(exports, trading_days(args.calendar), as_of, prices)
        result = evaluate(bundle)
        path = store_file(_local_output(args.output_dir), args.output_dir,
                          "three-day-live-" + fingerprint(result)[:24] + ".json",
                          encode({"input": bundle, "result": result}).encode())
        print(encode({"path": path, "samples": len(result["samples"]),
                      "by_original_signal": result["by_original_signal"]}))
        return 0
    except psycopg.Error as exc:
        parser.exit(1, f"three-day live evidence: database {type(exc).__name__}\n")
    except (ArithmeticError, BudgetError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        parser.exit(1, f"three-day live evidence: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
