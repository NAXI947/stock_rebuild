"""Run, inspect or copy a manual ETF backtest from the operator CLI."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from shared_kernel.temp_manager import BudgetError, TempManager
from .adapter import unique_object
from .manual_cases import ManualCases, copy_auto


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("bundle", type=Path)
    run.add_argument("--parent")
    commands.add_parser("show").add_argument("revision")
    copy = commands.add_parser("copy-auto")
    copy.add_argument("--env-file", required=True, type=Path)
    copy.add_argument("--input-kind", required=True, choices=("FROZEN","LIVE"))
    copy.add_argument("--auto-namespace")
    copy.add_argument("--cutoff")
    copy.add_argument("cycle_id")
    args = parser.parse_args(argv)
    try:
        cases = ManualCases(args.state_dir)
        if args.command == "show":
            result = cases.get(args.revision)
        else:
            tmp = Path(os.environ["APP_TMP_ROOT"])
            project = Path(__file__).resolve().parents[2]
            if tmp.resolve().is_relative_to(project) or tmp.resolve().is_relative_to(cases.directory):
                raise ValueError("temporary root must be separate from the project and revision store")
            manager = TempManager(tmp, persistent=cases.directory)
            if args.command == "run":
                with manager.task() as work, args.bundle.open("rb") as source:
                    staged = work.stage("manual-input.json", source)
                    if staged.stat().st_size > 16 * 1024 * 1024:
                        raise ValueError("manual scenario bundle exceeds 16 MiB")
                    bundle = json.loads(staged.read_bytes(), object_pairs_hook=unique_object)
                result = cases.save(manager, bundle, parent=args.parent)
            else:
                from .stage2.postgres import PostgresLedger
                from .stage2.settings import Settings
                import psycopg

                settings = Settings.read(args.env_file)
                if args.auto_namespace:
                    if not args.auto_namespace.startswith(settings.redis_prefix):
                        raise ValueError("automatic namespace outside the configured environment")
                    settings = replace(settings, namespace=args.auto_namespace)
                try:
                    bundle = copy_auto(PostgresLedger(settings,args.input_kind), args.cycle_id, cutoff=args.cutoff)
                except psycopg.Error:
                    raise ValueError("automatic ledger unavailable") from None
                result = cases.save(manager, bundle)
    except (ArithmeticError, BudgetError, OSError, TypeError, ValueError, KeyError) as exc:
        print(f"manual backtest: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
