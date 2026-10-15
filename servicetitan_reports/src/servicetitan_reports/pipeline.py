from __future__ import annotations

import csv
import hashlib
import io
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

@dataclass(frozen=True)
class UploadedImportResult:
    """Results and download-ready CSV files for one private browser session."""

    cleaned_rows: int
    appended_rows: int
    skipped_duplicate_rows: int
    new_customers: int
    period_start: date
    period_end: date
    cleaned_csv: bytes
    master_csv: bytes
    new_leads_csv: bytes
    analysis_csv: bytes
    category_comparisons: dict[str, list[dict[str, str | int]]]


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
        amount = Decimal(value).quantize(Decimal("0.01"))
        return format(amount.normalize(), "f") if amount else "0"
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

    if row["Opportunity"].lower() in {"true", "false"}:
        row["Opportunity"] = row["Opportunity"].capitalize()

    add_calendar_fields(row, "Created Date", "C", settings)
    # The existing master defines B calendar fields from Created Date.
    add_calendar_fields(row, "Created Date", "B", settings)

    return row


def has_required_values(row: dict[str, str], settings: dict[str, Any]) -> bool:
    return all(clean_text(row.get(column, "")) for column in settings["required_columns"])


def read_and_clean_export(
    source: Path,
    settings: dict[str, Any],
) -> list[dict[str, str]]:
    """
    Read a ServiceTitan export and keep only valid job rows.

    A valid row must have a Customer ID. This removes blank rows,
    section/header rows, and other non-customer records from all
    downstream analysis.
    """

    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        if not reader.fieldnames:
            raise ValueError("The CSV has no header row.")

        rows = []

        for raw in reader:
            row = standardise_row(raw, settings)

            # Ignore rows without a Customer ID.
            customer_id = clean_text(row.get("Customer ID", ""))

            if not customer_id or not has_required_values(row, settings):
                continue

            row["Customer ID"] = customer_id
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


def read_and_clean_csv_bytes(
    contents: bytes,
    settings: dict[str, Any],
    dataset_name: str,
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    """Clean an uploaded CSV entirely in memory; nothing is written to disk."""

    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{dataset_name} must be saved as a UTF-8 CSV.") from error

    reader = csv.DictReader(io.StringIO(text))

    if not reader.fieldnames:
        raise ValueError(f"{dataset_name} has no header row.")

    rows = []
    for raw in reader:
        row = standardise_row(raw, settings)
        customer_id = clean_text(row.get("Customer ID", ""))
        if not customer_id or not has_required_values(row, settings):
            continue
        row["Customer ID"] = customer_id
        rows.append(row)

    if not rows and not allow_empty:
        raise ValueError(f"{dataset_name} has no valid job rows with all required values.")

    return rows


def csv_bytes(rows: list[dict[str, str]], columns: list[str]) -> bytes:
    """Create a CSV download in memory, without creating a local file."""

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


PERIOD_COMPARISON_COLUMNS = [
    "Period",
    "Start",
    "End",
    "Records",
    "Unique Customers",
    "New Customers",
    "New Customer Gain/Loss",
    "Revenue",
]


def analysis_csv_bytes(
    category_comparisons: dict[str, list[dict[str, str | int]]],
    job_status_rows: list[dict[str, str | int]],
    job_status_columns: list[str],
) -> bytes:
    """Create the multi-section analysis CSV used by both import workflows."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)

    for category in ("HVAC", "Plumbing"):
        writer.writerow([f"{category} Period Comparison"])
        writer.writerow(PERIOD_COMPARISON_COLUMNS)
        for comparison in category_comparisons.get(category, []):
            writer.writerow([
                comparison.get(column, "")
                for column in PERIOD_COMPARISON_COLUMNS
            ])
        writer.writerow([])

    writer.writerow(["Job Type Status Analysis"])
    writer.writerow(job_status_columns)
    for row in job_status_rows:
        writer.writerow([row.get(column, "") for column in job_status_columns])

    return buffer.getvalue().encode("utf-8")


def write_analysis_csv(
    path: Path,
    category_comparisons: dict[str, list[dict[str, str | int]]],
    job_status_rows: list[dict[str, str | int]],
    job_status_columns: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(analysis_csv_bytes(
        category_comparisons,
        job_status_rows,
        job_status_columns,
    ))


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


def first_seen_customer_rows(
    rows: list[dict[str, str]],
    settings: dict[str, Any],
) -> list[dict[str, str]]:
    """Return one earliest-dated row per customer from the complete master."""

    selected: dict[str, tuple[date, int, dict[str, str]]] = {}
    for position, row in enumerate(rows):
        customer_id = clean_text(row.get("Customer ID", ""))
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        if not customer_id or day is None:
            continue
        current = selected.get(customer_id)
        if current is None or (day, position) < (current[0], current[1]):
            selected[customer_id] = (day, position, row)

    return [
        row
        for _, _, row in sorted(
            selected.values(),
            key=lambda item: (item[0], item[2].get("Customer ID", ""), item[1]),
        )
    ]


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


def invoice_category(
    row: dict[str, str],
    settings: dict[str, Any],
) -> str:
    """Map an Invoice Business Unit to its reporting category."""

    mappings = {
        clean_text(invoice_type).casefold(): category
        for invoice_type, category in settings.get(
            "invoice_type_categories", {}
        ).items()
    }
    invoice_type = clean_text(row.get("Invoice Business Unit", "")).casefold()
    return mappings.get(invoice_type, "")


def compare_periods(
    master_rows: list[dict[str, str]],
    new_customer_rows: list[dict[str, str]],
    start: date,
    end: date,
    settings: dict[str, Any],
    customer_history_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, str | int]]:
    # Retained for compatibility; comparison newness now comes from first-seen
    # dates in the master rather than an import-specific new-leads subset.
    _ = new_customer_rows
    period_days = (end - start).days

    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)

    try:
        last_year_start = start.replace(year=start.year - 1)
        last_year_end = end.replace(year=end.year - 1)
    except ValueError:
        last_year_start = start.replace(year=start.year - 1, day=28)
        last_year_end = end.replace(year=end.year - 1, day=28)

    dated_customers = [
        (row["Customer ID"], day)
        for row in customer_history_rows or master_rows
        if row.get("Customer ID")
        and (day := parse_date(row.get("Created Date", ""), settings["date_formats"]))
    ]
    history_start = min((day for _, day in dated_customers), default=None)
    first_seen: dict[str, date] = {}
    for customer_id, day in dated_customers:
        first_seen[customer_id] = min(day, first_seen.get(customer_id, day))

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

        if history_start is None or history_start >= period_start:
            new_customers: int | str = "Insufficient history"
        else:
            period_customer_ids = {
                row["Customer ID"] for row in period_rows if row.get("Customer ID")
            }
            new_customers = sum(
                customer_id in period_customer_ids
                and period_start <= first_day <= period_end
                for customer_id, first_day in first_seen.items()
            )

        comparisons.append({
            "Period": label,
            "Start": period_start.isoformat(),
            "End": period_end.isoformat(),
            **metric_summary(period_rows),
            "New Customers": new_customers,
        })

    return comparisons


def category_period_comparisons(
    master_rows: list[dict[str, str]],
    start: date,
    end: date,
    settings: dict[str, Any],
) -> dict[str, list[dict[str, str | int]]]:
    """Build separate period comparisons for HVAC and Plumbing."""

    comparisons = {}
    for category in ("HVAC", "Plumbing"):
        category_rows = [
            row
            for row in master_rows
            if invoice_category(row, settings).casefold() == category.casefold()
        ]
        comparisons[category] = compare_periods(
            category_rows, [], start, end, settings, customer_history_rows=master_rows
        )

    period_days = (end - start).days
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)
    try:
        last_year_start = start.replace(year=start.year - 1)
        last_year_end = end.replace(year=end.year - 1)
    except ValueError:
        last_year_start = start.replace(year=start.year - 1, day=28)
        last_year_end = end.replace(year=end.year - 1, day=28)

    periods = [
        ("Current Period", start, end),
        ("Previous Period", previous_start, previous_end),
        ("Same Period Last Year", last_year_start, last_year_end),
    ]
    exclusive_counts: dict[str, dict[str, int]] = {}
    exclusive_new_counts: dict[str, dict[str, int]] = {}
    first_seen: dict[str, date] = {}
    for row in master_rows:
        customer_id = clean_text(row.get("Customer ID", ""))
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        if customer_id and day is not None:
            first_seen[customer_id] = min(day, first_seen.get(customer_id, day))
    for label, period_start, period_end in periods:
        customer_categories: dict[str, tuple[date, str]] = {}
        for row in rows_in_period(master_rows, period_start, period_end, settings):
            customer_id = clean_text(row.get("Customer ID", ""))
            category = invoice_category(row, settings)
            day = parse_date(row.get("Created Date", ""), settings["date_formats"])
            if not customer_id or not category or day is None:
                continue
            current = customer_categories.get(customer_id)
            if current is None or day < current[0]:
                customer_categories[customer_id] = (day, category)
        exclusive_counts[label] = {
            category: sum(
                assigned_category == category
                for _, assigned_category in customer_categories.values()
            )
            for category in ("HVAC", "Plumbing")
        }
        exclusive_new_counts[label] = {
            category: sum(
                assigned_category == category
                and period_start <= first_seen[customer_id] <= period_end
                for customer_id, (_, assigned_category) in customer_categories.items()
            )
            for category in ("HVAC", "Plumbing")
        }

    for category, category_comparisons in comparisons.items():
        for comparison in category_comparisons:
            comparison["Unique Customers"] = exclusive_counts[
                str(comparison["Period"])
            ][category]
            if comparison["New Customers"] != "Insufficient history":
                comparison["New Customers"] = exclusive_new_counts[
                    str(comparison["Period"])
                ][category]

        current_new_customers = category_comparisons[0]["New Customers"]
        category_comparisons[0]["New Customer Gain/Loss"] = "—"
        for comparison in category_comparisons[1:]:
            comparison_new_customers = comparison["New Customers"]
            if (
                not isinstance(current_new_customers, int)
                or not isinstance(comparison_new_customers, int)
                or comparison_new_customers == 0
            ):
                comparison["New Customer Gain/Loss"] = "N/A"
            else:
                change = (
                    (current_new_customers - comparison_new_customers)
                    / comparison_new_customers
                    * 100
                )
                comparison["New Customer Gain/Loss"] = f"{change:+.1f}%"
    return comparisons


def job_status_analysis(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str | int]], list[str]]:
    """Pivot job statuses into count columns, with one row per job type."""

    counts: dict[str, dict[str, int]] = {}
    statuses: set[str] = set()
    for row in rows:
        job_type = clean_text(row.get("Job Type", "")) or "Unspecified"
        status = clean_text(row.get("Job Status", "")) or "Unspecified"
        statuses.add(status)
        type_counts = counts.setdefault(job_type, {})
        type_counts[status] = type_counts.get(status, 0) + 1

    status_columns = sorted(statuses, key=str.casefold)
    analysis_rows: list[dict[str, str | int]] = []
    for job_type in sorted(counts, key=str.casefold):
        analysis_rows.append({
            "Job Type": job_type,
            **{
                status: counts[job_type].get(status, 0)
                for status in status_columns
            },
        })
    return analysis_rows, ["Job Type", *status_columns]


def weekly_opportunity_ytd(
    rows: list[dict[str, str]],
    category: str,
    settings: dict[str, Any],
    job_statuses: set[str] | None = None,
) -> tuple[list[dict[str, int | None]], tuple[int, int] | None]:
    """Return weekly nonblank-opportunity counts for the latest and prior year."""

    dated_rows = [
        (row, day)
        for row in rows
        if (day := parse_date(row.get("Created Date", ""), settings["date_formats"]))
    ]
    if not dated_rows:
        return [], None

    current_year = max(day.year for _, day in dated_rows)
    previous_year = current_year - 1
    latest_current_day = max(
        day for _, day in dated_rows if day.year == current_year
    )
    last_week = latest_current_day.isocalendar().week
    counts = {
        current_year: {week: 0 for week in range(1, last_week + 1)},
        previous_year: {week: 0 for week in range(1, 53)},
    }

    for row, day in dated_rows:
        if day.year not in counts:
            continue
        week = day.isocalendar().week
        if day.year == current_year and week > last_week:
            continue
        if week > 52:
            continue
        if invoice_category(row, settings).casefold() != category.casefold():
            continue
        if not clean_text(row.get("Opportunity", "")):
            continue
        if job_statuses is not None and clean_text(
            row.get("Job Status", "")
        ) not in job_statuses:
            continue
        counts[day.year][week] += 1

    chart_rows = [
        {
            "Week": week,
            str(previous_year): counts[previous_year][week],
            str(current_year): (
                counts[current_year][week] if week <= last_week else None
            ),
        }
        for week in range(1, 53)
    ]
    return chart_rows, (previous_year, current_year)


def run_import(
    source: Path,
    master_path: Path,
    settings: dict[str, Any],
    output_dir: Path,
    period_start: date | None = None,
    period_end: date | None = None,
) -> ImportResult:
    columns = settings["master_columns"]
    dedupe_columns = settings["dedupe_columns"]

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

    master_rows = []

    for row in read_csv(master_path):
        cleaned_row = {
            column: clean_text(row.get(column, ""))
            for column in columns
        }

        # Never allow blank/non-customer rows from the master
        # into comparisons, duplicate detection, or reporting.
        if not has_required_values(cleaned_row, settings):
            continue

        master_rows.append(cleaned_row)

    existing_records = {
        fingerprint(row, dedupe_columns)
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
        record_hash = fingerprint(row, dedupe_columns)

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

    # Persist only valid Customer ID rows in the one continuing master dataset.
    write_csv(master_path, final_master_rows, columns)

    output_dir.mkdir(parents=True, exist_ok=True)

    cleaned_file = output_dir / f"{source.stem}_cleaned.csv"

    write_csv(cleaned_file, cleaned_rows, columns)
    write_csv(output_dir / "new_customers.csv", new_customer_rows, columns)

    new_leads_path = master_path.parent / settings["new_leads_filename"]

    write_csv(
        new_leads_path,
        first_seen_customer_rows(final_master_rows, settings),
        columns,
    )
    category_comparisons = category_period_comparisons(
        final_master_rows, start, end, settings
    )
    analysis_rows, analysis_columns = job_status_analysis(cleaned_rows)

    write_analysis_csv(
        output_dir / "analysis.csv",
        category_comparisons,
        analysis_rows,
        analysis_columns,
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "report_data.json").write_text(
        json.dumps({
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            "category_comparisons": category_comparisons,
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

def run_uploaded_import(
    source_contents: bytes,
    source_name: str,
    master_contents: bytes,
    new_leads_contents: bytes,
    settings: dict[str, Any],
    period_start: date | None = None,
    period_end: date | None = None,
) -> UploadedImportResult:
    """Process three uploaded CSVs in memory for the private web app."""

    # Kept in the public interface for compatibility with stored datasets.
    # The output is rebuilt from the complete master to repair historical gaps.
    _ = new_leads_contents
    columns = settings["master_columns"]
    dedupe_columns = settings["dedupe_columns"]
    cleaned_rows = read_and_clean_csv_bytes(source_contents, settings, "New ServiceTitan export")
    master_rows = read_and_clean_csv_bytes(
        master_contents, settings, "Master dataset", allow_empty=True
    )
    inferred_start, inferred_end = infer_period(cleaned_rows, settings)
    start = period_start or inferred_start
    end = period_end or inferred_end
    if end < start:
        raise ValueError("Period end must be on or after period start.")

    existing_records = {fingerprint(row, dedupe_columns) for row in master_rows}
    known_customers = {row["Customer ID"] for row in master_rows if row["Customer ID"]}
    upload_customers: set[str] = set()
    appended_rows = []
    new_customer_rows = []

    for row in sorted(cleaned_rows, key=lambda item: (item["Created Date"], item["Customer ID"])):
        record_hash = fingerprint(row, dedupe_columns)
        if record_hash in existing_records:
            continue

        customer_id = row["Customer ID"]
        if customer_id not in known_customers and customer_id not in upload_customers:
            new_customer_rows.append(row)

        existing_records.add(record_hash)
        known_customers.add(customer_id)
        upload_customers.add(customer_id)
        appended_rows.append(row)

    final_master_rows = master_rows + appended_rows
    final_new_leads_rows = first_seen_customer_rows(final_master_rows, settings)

    category_comparisons = category_period_comparisons(
        final_master_rows, start, end, settings
    )
    analysis_rows, analysis_columns = job_status_analysis(cleaned_rows)

    return UploadedImportResult(
        cleaned_rows=len(cleaned_rows),
        appended_rows=len(appended_rows),
        skipped_duplicate_rows=len(cleaned_rows) - len(appended_rows),
        new_customers=len(new_customer_rows),
        period_start=start,
        period_end=end,
        cleaned_csv=csv_bytes(cleaned_rows, columns),
        master_csv=csv_bytes(final_master_rows, columns),
        new_leads_csv=csv_bytes(final_new_leads_rows, columns),
        analysis_csv=analysis_csv_bytes(
            category_comparisons,
            analysis_rows,
            analysis_columns,
        ),
        category_comparisons=category_comparisons,
    )
