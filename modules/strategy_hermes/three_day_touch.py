"""Calculate report-based three-trading-day reference-price touches offline."""
import argparse
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
import json
import os
from pathlib import Path
import sys

from shared_kernel.temp_manager import BudgetError, TempManager, store_file
from .adapter import unique_object
from .ledger import encode, fingerprint
from .simulator import CHINA, CODE, Calendar, decimal, moment

SCHEMA = "stock-v4-three-day-touch-v1"
LIMIT = 16 * 1024 * 1024


def _samples(rows, as_of):
    reports, samples = {}, {}
    for row in rows:
        rid, signal, code = row["report_id"], row["original_signal"], row["etf"]
        published = moment(row["published_at"])
        if (not isinstance(rid,str) or not rid.strip() or not isinstance(signal,str)
                or not signal.strip() or not CODE.fullmatch(code) or published > as_of):
            raise ValueError("invalid three-day report sample")
        if reports.setdefault(rid,published) != published:
            raise ValueError("report ID has conflicting publication times")
        sources = row["sources"]
        if not isinstance(sources,list) or not sources or not all(isinstance(item,str) and item.strip() for item in sources):
            raise ValueError("three-day sample requires source references")
        key = (rid,signal,code)
        samples.setdefault(key,set()).update(sources)
    return [(key,reports[key[0]],sorted(sources)) for key,sources in sorted(samples.items())]


def _prices(rows, days, as_of):
    latest = {}
    for row in rows:
        code, day = row["etf"], date.fromisoformat(row["trade_date"])
        if not CODE.fullmatch(code) or day not in days or row.get("price_basis") != "UNADJUSTED":
            raise ValueError("invalid three-day price identity or basis")
        available = moment(row["available_at"])
        if available.date() < day or available > as_of:
            continue
        source = row["source"]
        if not isinstance(source,str) or not source.strip():
            raise ValueError("three-day price requires a source")
        opening = decimal(row["open_cny"]) if row.get("open_cny") is not None else None
        high = decimal(row["high_cny"]) if row.get("high_cny") is not None else None
        if (opening is not None and opening <= 0 or high is not None and high <= 0
                or opening is not None and high is not None and high < opening):
            raise ValueError("invalid three-day open or high")
        key = (code,day)
        fact = dict(source=source,available_at=available.isoformat(),
                    open_cny=str(opening) if opening is not None else None,
                    high_cny=str(high) if high is not None else None)
        existing = latest.get(key)
        if not existing or available > moment(existing["available_at"]):
            latest[key] = fact
        elif available == moment(existing["available_at"]) and fact != existing:
            raise ValueError("conflicting three-day prices at the same source time")
    return latest


def evaluate(bundle):
    if bundle.get("schema_version") != SCHEMA:
        raise ValueError("unsupported three-day input schema")
    calendar = Calendar(bundle["calendar"])
    days, as_of = calendar.days, moment(bundle["as_of"])
    prices = _prices(bundle["prices"],set(days),as_of)
    results = []
    counts = defaultdict(lambda: dict(valid=0,touched=0,wait_window=0,missing_price=0,missing_calendar=0))
    for (rid,signal,code),published,sources in _samples(bundle["samples"],as_of):
        index = bisect_right(days,published.date())
        window = days[index:index+3]
        row = dict(report_id=rid,original_signal=signal,etf=code,published_at=published.isoformat(),
                   sources=sources,window=[day.isoformat() for day in window],touch_day=None)
        total = counts[signal]
        if published.date() < days[0] or len(window) < 3:
            row["status"] = "MISSING_CALENDAR"
            total["missing_calendar"] += 1
        elif as_of < datetime.combine(window[-1],time(15),tzinfo=CHINA):
            row["status"] = "WAIT_WINDOW"
            total["wait_window"] += 1
        else:
            evidence = [prices.get((code,day)) for day in window]
            row["price_evidence"] = evidence
            if (not evidence[0] or evidence[0]["open_cny"] is None
                    or any(not item or item["high_cny"] is None for item in evidence)):
                row["status"] = "MISSING_PRICE"
                total["missing_price"] += 1
            else:
                reference = decimal(evidence[0]["open_cny"])
                first_hit = next((day.isoformat() for day,item in zip(window,evidence)
                                  if decimal(item["high_cny"]) > reference),None)
                row.update(status="TOUCHED" if first_hit else "NOT_TOUCHED",touch_day=first_hit,
                           reference_open_cny=str(reference))
                total["valid"] += 1
                total["touched"] += int(first_hit is not None)
        results.append(row)
    for total in counts.values():
        total["touch_pct"] = (str((Decimal(total["touched"])*100/total["valid"]).quantize(Decimal("0.0001")))
                              if total["valid"] else None)
    return dict(schema_version="stock-v4-three-day-touch-result-v1",
                as_of=as_of.isoformat(),metric="REFERENCE_WINDOW_TOUCH",source_sha256=fingerprint(bundle),
                samples=results,by_original_signal=dict(counts))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",required=True,type=Path)
    parser.add_argument("bundle",type=Path)
    args = parser.parse_args(argv)
    try:
        output = args.output_dir
        project = Path(__file__).resolve().parents[2]
        if (not output.is_absolute() or output.resolve()!=output or not output.is_dir()
                or output.is_relative_to(project) or output.stat().st_mode & 0o077):
            raise ValueError("three-day output requires a private directory outside the project")
        temporary = Path(os.environ["APP_TMP_ROOT"])
        if temporary.resolve().is_relative_to(project) or temporary.resolve().is_relative_to(output):
            raise ValueError("temporary root must be separate from the project and results")
        manager = TempManager(temporary,persistent=output)
        with args.bundle.open("rb") as source:
            raw = source.read(LIMIT+1)
        if len(raw)>LIMIT:
            raise ValueError("three-day input exceeds 16 MiB")
        bundle = json.loads(raw,object_pairs_hook=unique_object)
        result = evaluate(bundle)
        record = encode({"input":bundle,"result":result}).encode()
        path = store_file(manager,output,"three-day-"+fingerprint(result)[:24]+".json",record)
        print(encode({"path":path,"samples":len(result["samples"]),"by_original_signal":result["by_original_signal"]}))
        return 0
    except (ArithmeticError,BudgetError,ValueError,TypeError,KeyError,AttributeError,OSError) as exc:
        print(f"three-day touch: {exc}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
