"""CLI for the offline frozen replay and its persisted evidence."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from shared_kernel.temp_manager import TempManager, BudgetError
from .ledger import Ledger


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir",required=True,type=Path,help="private persistent runtime directory")
    sub=p.add_subparsers(dest="cmd",required=True)
    sub.add_parser("replay").add_argument("bundle",type=Path)
    sub.add_parser("inspect").add_argument("namespace")
    report=sub.add_parser("report")
    report.add_argument("namespace")
    report.add_argument("report_id")
    sub.add_parser("recover-temp")
    args=p.parse_args(argv)
    try:
        manager=None
        state=args.state_dir
        source=Path(__file__).resolve().parents[2]
        if state.resolve().is_relative_to(source):
            raise ValueError("state directory must be outside the source tree")
        if args.cmd in ("replay","recover-temp"):
            tmp=Path(os.environ["APP_TMP_ROOT"])
            if (tmp.resolve().is_relative_to(source) or tmp.resolve().is_relative_to(state.resolve())
                    or state.resolve().is_relative_to(tmp.resolve())):
                raise ValueError("source, temporary and state directories must be separate")
            manager=TempManager(tmp,
                total=int(os.environ.get("APP_TMP_BUDGET_BYTES",2**31)),
                per_task=int(os.environ.get("APP_TMP_TASK_BUDGET_BYTES",2**28)),
                entries=int(os.environ.get("APP_TMP_ENTRY_LIMIT",20000)),
                min_free=int(os.environ.get("STORAGE_MIN_FREE_BYTES",5*2**30)),persistent=state)
        if args.cmd=="recover-temp":
            result=manager.collect()
        elif args.cmd=="replay":
            manager.collect()
            with manager.task() as work:
                with args.bundle.open("rb") as f:
                    staged=work.stage("bundle.json",f)
                # Keep inputs exact and avoid an unbounded parser allocation.
                if staged.stat().st_size>16*2**20:
                    raise ValueError("frozen replay bundle exceeds 16 MiB")
                with staged.open(encoding="utf-8") as f:
                    bundle=json.load(f)
                result=Ledger(state).replay(bundle)
        elif args.cmd=="inspect":
            result=Ledger(state).inspect(args.namespace)
        else:
            result=Ledger(state).report(args.namespace,args.report_id)
    except (OSError,ValueError,KeyError,TypeError,ArithmeticError,sqlite3.Error,BudgetError) as exc:
        print(f"stage1: {exc}",file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if args.cmd=="recover-temp" and result["failed"] else 0


if __name__=="__main__":
    raise SystemExit(main())
