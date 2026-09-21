#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# importing automation
from servicetitan_reports.pipeline import load_settings, run_import  # noqa: E402

# validate dates
def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD.") from error


def main() -> None:
    settings = load_settings(ROOT / "config/settings.json")
    parser = argparse.ArgumentParser(description="Clean ServiceTitan CSV, update the master dataset.")
    parser.add_argument("csv", type=Path, 
                        help="Path to new ServiceTitan CSV export")
    parser.add_argument("--period-start", 
                        type=iso_date, 
                        help="Override inferred start date (YYYY-MM-DD)")
    parser.add_argument("--period-end", 
                        type=iso_date, 
                        help="Override inferred end date (YYYY-MM-DD)")
    parser.add_argument("--master",
                        type=Path,
                        default=ROOT / "data-imports/master" / settings["master_filename"],
                        help="Master job-mix CSV to update",
)
    parser.add_argument("--output-dir", 
                        type=Path, 
                        help="Where this import's report ready CSV files are saved")
    args = parser.parse_args()
    if not args.csv.is_file():
        parser.error(f"CSV not found: {args.csv}")
    output_dir = args.output_dir or ROOT / "output" / args.csv.stem
    result = run_import(args.csv, args.master, settings, output_dir,
                        args.period_start, args.period_end)
    print(f"Imported {result.appended_rows} records ({result.skipped_duplicate_rows} duplicate records skipped).")
    print(f"New customers: {result.new_customers}. Period: {result.period_start} through {result.period_end}.")
    print(f"Report-ready files: {result.output_dir}")


if __name__ == "__main__":
    main()
