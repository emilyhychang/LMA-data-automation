from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImportResult:
    cleaned_rows: int
    appended_rows: int
    skipped_duplicate_rows: int
    new_customers: int
    period_start: date
    period_end: date
    output_dir: Path


def load_settings(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def clean_text(value: object | None) -> str:
    return "" if value is None else " ".join(str(value).strip().split())


def normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def parse_date(value: str, formats: list[str]) -> date | None:
    value = clean_text(value)

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def parse_amount(value: str) -> str:
    value = clean_text(value)

    if not value:
        return ""

    value = value.replace("$", "").replace(",", "")

    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]

    try:
        return format(Decimal(value).quantize(Decimal("0.01")), "f")
    except InvalidOperation:
        return value


def add_calendar_fields(
    row: dict[str, str],
    date_column: str,
    prefix: str,
    settings: dict[str, Any],
) -> None:
    day = parse_date(row.get(date_column, ""), settings["date_formats"])

    if not day:
        return

    values = {
        f"{prefix} Year": str(day.year),
        f"{prefix} Qtr": str(((day.month - 1) // 3) + 1),
        f"{prefix} Mo": day.strftime("%B"),
        f"{prefix} Wk": str(day.isocalendar().week),
    }

    for column, value in values.items():
        if not row.get(column):
            row[column] = value


def standardise_row(
    raw: dict[str | None, str | None],
    settings: dict[str, Any],
) -> dict[str, str]:
    """Convert one raw ServiceTitan row into the master CSV layout."""

    aliases = {
        normalise_header(source): destination
        for source, destination in settings["column_aliases"].items()
    }

    row = {
        column: ""
        for column in settings["master_columns"]
    }

    for header, value in raw.items():
        if not header:
            continue

        destination = aliases.get(
            normalise_header(header),
            header.strip(),
        )

        if destination in row:
            row[destination] = clean_text(value)

    for column in ("Created Date", "Completion Date"):
        parsed = parse_date(row[column], settings["date_formats"])

        if parsed:
            row[column] = parsed.isoformat()

    row["Total"] = parse_amount(row["Total"])

    add_calendar_fields(row, "Created Date", "C", settings)
    add_calendar_fields(row, "Completion Date", "B", settings)

    return row


def read_and_clean_export(
    source: Path,
    settings: dict[str, Any],
) -> list[dict[str, str]]:
    """
    Read a ServiceTitan export.

    Rows such as 'Invoice Business Unit: HVAC Install' are ignored because
    they do not contain a Customer ID.
    """

    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        if not reader.fieldnames:
            raise ValueError("The CSV has no header row.")

        rows = []

        for raw in reader:
            row = standardise_row(raw, settings)

            if row["Customer ID"]:
                rows.append(row)

    if not rows:
        raise ValueError(
            "No job rows with a Customer ID were found in the CSV."
        )

    return rows


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(
    path: Path,
    rows: list[dict[str, str]],
    columns: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


def fingerprint(row: dict[str, str], columns: list[str]) -> str:
    values = "\x1f".join(
        f"{column}={row.get(column, '')}"
        for column in columns
    )

    return hashlib.sha256(values.encode("utf-8")).hexdigest()


def infer_period(
    rows: list[dict[str, str]],
    settings: dict[str, Any],
) -> tuple[date, date]:
    dates = [
        parse_date(row["Created Date"], settings["date_formats"])
        for row in rows
    ]

    valid_dates = [day for day in dates if day]

    if not valid_dates:
        raise ValueError("No parseable Created Date values were found.")

    return min(valid_dates), max(valid_dates)


def rows_in_period(
    rows: list[dict[str, str]],
    start: date,
    end: date,
    settings: dict[str, Any],
) -> list[dict[str, str]]:
    matching_rows = []

    for row in rows:
        day = parse_date(row["Created Date"], settings["date_formats"])

        if day and start <= day <= end:
            matching_rows.append(row)

    return matching_rows


def metric_summary(
    rows: list[dict[str, str]],
) -> dict[str, str | int]:
    total = Decimal("0")

    for row in rows:
        try:
            total += Decimal(row["Total"] or "0")
        except InvalidOperation:
            continue

    return {
        "Records": len(rows),
        "Unique Customers": len({
            row["Customer ID"]
            for row in rows
            if row["Customer ID"]
        }),
        "Revenue": format(total, "f"),
    }


def compare_periods(
    master_rows: list[dict[str, str]],
    new_customer_rows: list[dict[str, str]],
    start: date,
    end: date,
    settings: dict[str, Any],
) -> list[dict[str, str | int]]:
    period_days = (end - start).days

    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)

    try:
        last_year_start = start.replace(year=start.year - 1)
        last_year_end = end.replace(year=end.year - 1)
    except ValueError:
        last_year_start = start.replace(year=start.year - 1, day=28)
        last_year_end = end.replace(year=end.year - 1, day=28)

    comparisons = []

    for label, period_start, period_end in [
        ("Current Period", start, end),
        ("Previous Period", previous_start, previous_end),
        ("Same Period Last Year", last_year_start, last_year_end),
    ]:
        period_rows = rows_in_period(
            master_rows,
            period_start,
            period_end,
            settings,
        )

        new_rows = rows_in_period(
            new_customer_rows,
            period_start,
            period_end,
            settings,
        )

        comparisons.append({
            "Period": label,
            "Start": period_start.isoformat(),
            "End": period_end.isoformat(),
            **metric_summary(period_rows),
            "New Customers": len(new_rows),
        })

    return comparisons


def run_import(
    source: Path,
    master_path: Path,
    settings: dict[str, Any],
    output_dir: Path,
    period_start: date | None = None,
    period_end: date | None = None,
) -> ImportResult:
    columns = settings["master_columns"]

    cleaned_rows = read_and_clean_export(source, settings)

    inferred_start, inferred_end = infer_period(
        cleaned_rows,
        settings,
    )

    start = period_start or inferred_start
    end = period_end or inferred_end

    if end < start:
        raise ValueError(
            "period end must be on or after period start"
        )

    master_rows = [
        {
            column: clean_text(row.get(column, ""))
            for column in columns
        }
        for row in read_csv(master_path)
    ]

    existing_records = {
        fingerprint(row, columns)
        for row in master_rows
    }

    known_customers = {
        row["Customer ID"]
        for row in master_rows
        if row["Customer ID"]
    }

    upload_customers: set[str] = set()

    appended_rows = []
    new_customer_rows = []

    for row in sorted(
        cleaned_rows,
        key=lambda item: (
            item["Created Date"],
            item["Customer ID"],
        ),
    ):
        record_hash = fingerprint(row, columns)

        if record_hash in existing_records:
            continue

        customer_id = row["Customer ID"]

        if (
            customer_id not in known_customers
            and customer_id not in upload_customers
        ):
            new_customer_rows.append(row)

        existing_records.add(record_hash)
        known_customers.add(customer_id)
        upload_customers.add(customer_id)
        appended_rows.append(row)

    final_master_rows = master_rows + appended_rows

    output_dir.mkdir(parents=True, exist_ok=True)

    cleaned_file = output_dir / f"{source.stem}_cleaned.csv"

    write_csv(cleaned_file, appended_rows, columns)
    write_csv(output_dir / "new_customers.csv", new_customer_rows, columns)

    new_leads_path = master_path.parent / settings["new_leads_filename"]

    existing_new_leads = read_csv(new_leads_path)

    write_csv(
        new_leads_path,
        existing_new_leads + new_customer_rows,
        columns,
    )
    comparison = compare_periods(
        final_master_rows,
        new_customer_rows,
        start,
        end,
        settings,
    )

    comparison_columns = [
        "Period",
        "Start",
        "End",
        "Records",
        "Unique Customers",
        "New Customers",
        "Revenue",
    ]

    write_csv(
        output_dir / "period_comparison.csv",
        comparison,
        comparison_columns,
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "report_data.json").write_text(
        json.dumps({
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "comparison": comparison,
            "new_customer_count": len(new_customer_rows),
        }, indent=2),
        encoding="utf-8",
    )

    return ImportResult(
        cleaned_rows=len(cleaned_rows),
        appended_rows=len(appended_rows),
        skipped_duplicate_rows=len(cleaned_rows) - len(appended_rows),
        new_customers=len(new_customer_rows),
        period_start=start,
        period_end=end,
        output_dir=output_dir,
    )