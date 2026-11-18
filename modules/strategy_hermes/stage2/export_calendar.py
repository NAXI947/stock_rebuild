"""Export explicitly stored SSE dates, retaining gaps as coverage metadata."""
import argparse
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from shared_kernel.temp_manager import TempManager, store_file

from ..ledger import encode
from ..simulator import CHINA, Calendar
from .postgres import PostgresLedger
from .settings import Settings
from .reference_runtime import calendar_days


def export_calendar(ledger, batch_id):
    with ledger._connect() as conn, conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(
            """SELECT normalized, collected_at FROM supplier_batches
               WHERE api = 'trade_cal' AND batch_id = %s""", (batch_id,),
        ).fetchone()
    if row is None:
        raise ValueError("stored calendar batch not found")
    records, collected_at = row
    days = calendar_days(records)
    Calendar(sorted(day for day, is_open in days.items() if is_open))
    first, last = min(days), max(days)
    missing = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1 - len(days)
    return dict(schema_version="hotspot_trade_calendar_v1", source="quant.trade_calendar",
                exchange="SSE", generated_at=collected_at.astimezone(CHINA).isoformat(),
                range_start=first, range_end=last, days=days, source_batch=batch_id,
                coverage_complete=missing == 0, missing_day_count=missing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{64}", args.batch):
        parser.error("expected a stored batch ID")
    directory = args.output_dir
    if not directory.is_absolute() or directory.resolve() != directory or not directory.is_dir():
        parser.error("expected an existing absolute output directory")
    settings = Settings.read(args.env_file)
    ledger = PostgresLedger(settings, "FROZEN" if settings.env == "test" else "LIVE")
    content = encode(export_calendar(ledger, args.batch)).encode()
    checksum = sha256(content).hexdigest()
    manager = TempManager(os.environ["APP_TMP_ROOT"], persistent=directory)
    path = store_file(manager, directory, "calendar-" + args.batch[:24] + ".json", content)
    print(encode(dict(path=path, sha256=checksum)))


if __name__ == "__main__":
    main()
