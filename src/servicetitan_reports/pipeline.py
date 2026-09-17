from __future__ import annotations
import csv
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# automation columns
INTERNAL_COLUMNS = [
    "_record_fingerprint", "_source_file", "_imported_at", "First Seen Date",
    "Is First Seen Row", "Is New Customer",
]

@dataclass(frozen=True)
class ImportResult:
    cleaned_rows: int
    appended_rows: int
    skipped_duplicate_rows: int
    new_customers: int
    period_start: date
    period_end: date
    output_dir: Path


"""data cleaning"""
def load_settings(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)

def normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()

def canonical_column(header: str, aliases: dict[str, str]) -> str:
    """return stable column name while keeping unmapped export columns"""
    cleaned = normalise_header(header)
    return aliases.get(cleaned, header.strip())

def clean_text(value: str | None) -> str:
    return "" if value is None else " ".join(value.strip().split())

def parse_date(value: str, formats: list[str]) -> date | None:
    value = clean_text(value)
    if not value:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None

"""convert common currency strings to decimal string; preserve text otherwise"""
def parse_amount(value: str) -> str:
    raw = clean_text(value)
    if not raw:
        return ""
    candidate = raw.replace("$", "").replace(",", "")
    if candidate.startswith("(") and candidate.endswith(")"):
        candidate = "-" + candidate[1:-1]
    try:
        return format(Decimal(candidate).quantize(Decimal("0.01")), "f")
    except InvalidOperation:
        return raw

"""read servicetitan CSV, standardize known fields w/o dropping unknown columns"""
def read_and_clean_export(source: Path, settings: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    aliases = {normalise_header(k): v for k, v in settings["column_aliases"].items()}
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("The CSV has no header row.")
        rename = {header: canonical_column(header, aliases) for header in reader.fieldnames}
        fields = list(dict.fromkeys(rename.values()))
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {rename[key]: clean_text(value) for key, value in raw.items() if key is not None}
            if not any(row.values()):
                continue
            date_column = settings["activity_date_column"]
            parsed = parse_date(row.get(date_column, ""), settings["date_formats"])
            if parsed:
                row[date_column] = parsed.isoformat()
            if "Revenue" in row:
                row["Revenue"] = parse_amount(row["Revenue"])
            rows.append(row)
    customer_column = settings["customer_id_column"]
    if customer_column not in fields:
        raise ValueError(
            f"Required customer ID column '{customer_column}' was not found. "
            "Add its export header to config/settings.json > column_aliases."
        )
    return rows, fields

def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])

def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

"""rows w customer ID, lead date, lead source, revenue, etc produce same fingerprint"""
def fingerprint(row: dict[str, str], fields: list[str]) -> str:
    payload = "\x1f".join(f"{field}={row.get(field, '')}" for field in sorted(fields))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

"""finds earliest & latest cleaned lead date in new upload"""
def infer_period(rows: list[dict[str, str]], date_column: str) -> tuple[date, date]:
    dates = [date.fromisoformat(row[date_column]) for row in rows if row.get(date_column)]
    if not dates:
        raise ValueError(
            f"No parseable values were found in '{date_column}'. Pass --period-start and --period-end, "
            "or add the export's date format/header in config/settings.json."
        )
    return min(dates), max(dates)

"""calculate metric reports"""
def metric_summary(rows: list[dict[str, str]], settings: dict[str, Any]) -> dict[str, Any]:
    customer = settings["customer_id_column"]
    revenue_total = Decimal("0")
    for row in rows:
        try:
            revenue_total += Decimal(row.get("Revenue", "") or "0")
        except InvalidOperation:
            continue
    return {
        "Records": len(rows),
        "Unique Customers": len({row.get(customer) for row in rows if row.get(customer)}),
        "New Customers": sum(row.get("Is New Customer") == "Yes" for row in rows),
        "Revenue": format(revenue_total, "f"),
    }

"""compare periods: current, previous, same period last year"""
def compare_periods(master_rows: list[dict[str, str]], start: date, end: date, settings: dict[str, Any]) -> list[dict[str, Any]]:
    date_column = settings["activity_date_column"]

    def in_range(row: dict[str, str], left: date, right: date) -> bool:
        try:
            day = date.fromisoformat(row.get(date_column, ""))
        except ValueError:
            return False
        return left <= day <= right

    days = (end - start).days
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days)
    try:
        last_year_start = start.replace(year=start.year - 1)
        last_year_end = end.replace(year=end.year - 1)
    except ValueError:  # Feb 29
        last_year_start = start.replace(year=start.year - 1, day=28)
        last_year_end = end.replace(year=end.year - 1, day=28)

    windows = [
        ("Current Period", start, end),
        ("Previous Period", previous_start, previous_end),
        ("Same Period Last Year", last_year_start, last_year_end),
    ]
    comparison: list[dict[str, Any]] = []
    for label, left, right in windows:
        summary = metric_summary([row for row in master_rows if in_range(row, left, right)], settings)
        comparison.append({"Period": label, "Start": left.isoformat(), "End": right.isoformat(), **summary})
    return comparison

def refresh_first_seen_fields(rows: list[dict[str, str]], settings: dict[str, Any]) -> None:
    """Make master flags consistent after a late historical/backfill import."""
    customer_column = settings["customer_id_column"]
    date_column = settings["activity_date_column"]
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        customer_id = clean_text(row.get(customer_column))
        if customer_id:
            grouped.setdefault(customer_id, []).append(row)
    for customer_rows in grouped.values():
        dated = [row for row in customer_rows if parse_date(row.get(date_column, ""), settings["date_formats"])]
        ordered = sorted(
            dated or customer_rows,
            key=lambda row: row.get(date_column, "9999-12-31") or "9999-12-31",
        )
        first = ordered[0]
        first_date = first.get(date_column, "")
        for row in customer_rows:
            is_first = row is first
            row["First Seen Date"] = first_date
            row["Is First Seen Row"] = "Yes" if is_first else "No"
            row["Is New Customer"] = "Yes" if is_first else "No"
def run_import(
    source: Path,
    master_path: Path,
    settings: dict[str, Any],
    output_dir: Path,
    period_start: date | None = None,
    period_end: date | None = None,
) -> ImportResult:
    cleaned_rows, import_fields = read_and_clean_export(source, settings)
    if not cleaned_rows:
        raise ValueError("The CSV contained no data rows.")
    inferred_start, inferred_end = infer_period(cleaned_rows, settings["activity_date_column"])
    start, end = period_start or inferred_start, period_end or inferred_end
    if end < start:
        raise ValueError("period end must be on or after period start")

    master_rows, master_fields = read_csv(master_path)
    all_business_fields = list(dict.fromkeys(
        field for field in master_fields + import_fields if field not in INTERNAL_COLUMNS
    ))
    existing_fingerprints = {row.get("_record_fingerprint") for row in master_rows if row.get("_record_fingerprint")}
    customer_column = settings["customer_id_column"]
    known_customer_ids = {clean_text(row.get(customer_column)) for row in master_rows if clean_text(row.get(customer_column))}

    # first-seen state is based on master dataset, plus earlier rows in this upload. sorting makes a dated export deterministic.
    first_seen: dict[str, date] = {}
    for row in master_rows:
        customer_id = clean_text(row.get(customer_column))
        parsed = parse_date(row.get("First Seen Date", ""), settings["date_formats"])
        if customer_id and parsed:
            first_seen[customer_id] = min(first_seen.get(customer_id, parsed), parsed)
    new_rows: list[dict[str, str]] = []
    upload_seen_customers: set[str] = set()
    now = datetime.now().isoformat(timespec="seconds")
    for row in sorted(cleaned_rows, key=lambda item: (item.get(settings["activity_date_column"], "9999-12-31"), item.get(customer_column, ""))):
        record_hash = fingerprint(row, all_business_fields)
        if record_hash in existing_fingerprints:
            continue
        customer_id = clean_text(row.get(customer_column))
        activity_day = parse_date(row.get(settings["activity_date_column"], ""), settings["date_formats"])
        previously_known = customer_id in known_customer_ids
        first_day = first_seen.get(customer_id) or activity_day
        if customer_id and first_day:
            first_seen[customer_id] = first_day
        is_first_row = bool(customer_id and not previously_known and customer_id not in upload_seen_customers)
        if customer_id:
            upload_seen_customers.add(customer_id)
            known_customer_ids.add(customer_id)
        row.update({
            "_record_fingerprint": record_hash,
            "_source_file": source.name,
            "_imported_at": now,
            "First Seen Date": first_day.isoformat() if first_day else "",
            "Is First Seen Row": "Yes" if is_first_row else "No",
            "Is New Customer": "Yes" if is_first_row else "No",
        })
        new_rows.append(row)
        existing_fingerprints.add(record_hash)

    final_rows = master_rows + new_rows
    refresh_first_seen_fields(final_rows, settings)
    master_output_fields = list(dict.fromkeys(all_business_fields + INTERNAL_COLUMNS))
    write_csv(master_path, final_rows, master_output_fields)

    output_dir.mkdir(parents=True, exist_ok=True)
    cleaned_fields = list(dict.fromkeys(all_business_fields + INTERNAL_COLUMNS))
    write_csv(output_dir / "cleaned_upload.csv", new_rows, cleaned_fields)
    write_csv(output_dir / "new_customers.csv", [r for r in new_rows if r["Is New Customer"] == "Yes"], cleaned_fields)
    comparison = compare_periods(final_rows, start, end, settings)
    comparison_fields = ["Period", "Start", "End", "Records", "Unique Customers", "New Customers", "Revenue"]
    write_csv(output_dir / "period_comparison.csv", comparison, comparison_fields)
    (output_dir / "report_data.json").write_text(json.dumps({
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "comparison": comparison,
        "new_customer_count": sum(r["Is New Customer"] == "Yes" for r in new_rows),
    }, indent=2), encoding="utf-8")
    return ImportResult(len(cleaned_rows), len(new_rows), len(cleaned_rows) - len(new_rows),
                        sum(r["Is New Customer"] == "Yes" for r in new_rows), start, end, output_dir)
