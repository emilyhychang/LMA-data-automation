from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any


MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class ImportResult:
    cleaned_rows: int
    appended_rows: int
    updated_rows: int
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
    updated_rows: int
    skipped_duplicate_rows: int
    new_customers: int
    period_start: date
    period_end: date
    cleaned_csv: bytes
    master_csv: bytes
    new_leads_csv: bytes
    analysis_csv: bytes
    zip_year: int | None
    zip_revenue_rows: list[dict[str, str | int | float]]
    zip_new_lead_rows: list[dict[str, str | int | float]]


@dataclass(frozen=True)
class ProcessedImport:
    cleaned_rows: list[dict[str, str]]
    master_rows: list[dict[str, str]]
    new_customer_rows: list[dict[str, str]]
    new_leads_rows: list[dict[str, str]]
    category_comparisons: dict[str, list[dict[str, str | int]]]
    analysis_csv: bytes
    zip_year: int | None
    zip_revenue_rows: list[dict[str, str | int | float]]
    zip_new_lead_rows: list[dict[str, str | int | float]]
    appended_rows: int
    updated_rows: int
    skipped_rows: int
    period_start: date
    period_end: date


def load_settings(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def clean_text(value: object | None) -> str:
    return "" if value is None else " ".join(str(value).strip().split())


def normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


@lru_cache(maxsize=65_536)
def _parse_date_cached(value: str, formats: tuple[str, ...]) -> date | None:
    if not value:
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        pass

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def parse_date(value: str, formats: list[str]) -> date | None:
    return _parse_date_cached(clean_text(value), tuple(formats))


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
    prefix: str,
    day: date | None,
) -> None:
    if not day:
        return

    values = {
        f"{prefix} Year": str(day.year),
        f"{prefix} Qtr": str(((day.month - 1) // 3) + 1),
        f"{prefix} Mo": MONTH_NAMES[day.month],
        f"{prefix} Wk": str(day.isocalendar().week),
    }

    for column, value in values.items():
        if not row.get(column):
            row[column] = value


def standardise_row(
    raw: dict[str | None, str | None],
    settings: dict[str, Any],
    header_destinations: dict[str, str],
) -> dict[str, str]:
    """Convert one raw ServiceTitan row into the master CSV layout."""

    row = dict.fromkeys(settings["master_columns"], "")

    for header, value in raw.items():
        if not header:
            continue

        destination = header_destinations.get(header)
        if destination:
            row[destination] = clean_text(value)

    created_day = parse_date(row["Created Date"], settings["date_formats"])
    completion_day = parse_date(row["Completion Date"], settings["date_formats"])
    if created_day:
        row["Created Date"] = created_day.isoformat()
    if completion_day:
        row["Completion Date"] = completion_day.isoformat()

    row["Total"] = parse_amount(row["Total"])

    if row["Opportunity"].lower() in {"true", "false"}:
        row["Opportunity"] = row["Opportunity"].capitalize()

    add_calendar_fields(row, "C", created_day)
    # The existing master defines B calendar fields from Created Date.
    add_calendar_fields(row, "B", created_day)

    return row


def has_required_values(row: dict[str, str], settings: dict[str, Any]) -> bool:
    return all(clean_text(row.get(column, "")) for column in settings["required_columns"])


def read_and_clean_export(
    source: Path,
    settings: dict[str, Any],
    dataset_name: str = "CSV",
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    """
    Read a ServiceTitan export and keep only valid job rows.

    A valid row must have a Customer ID. This removes blank rows,
    section/header rows, and other non-customer records from all
    downstream analysis.
    """

    if allow_empty and not source.exists():
        return []

    with source.open(encoding="utf-8-sig", newline="") as handle:
        return clean_csv_reader(
            csv.DictReader(handle), settings, dataset_name, allow_empty
        )


def write_csv(
    path: Path,
    rows: list[dict[str, str]],
    columns: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as handle:
        write_csv_rows(handle, rows, columns)


def clean_csv_reader(
    reader: csv.DictReader,
    settings: dict[str, Any],
    dataset_name: str,
    allow_empty: bool = False,
) -> list[dict[str, str]]:
    if not reader.fieldnames:
        raise ValueError(f"{dataset_name} has no header row.")

    aliases = {
        normalise_header(source): destination
        for source, destination in settings["column_aliases"].items()
    }
    columns = set(settings["master_columns"])
    header_destinations = {}
    for header in reader.fieldnames:
        if not header:
            continue
        destination = aliases.get(normalise_header(header), header.strip())
        if destination in columns:
            header_destinations[header] = destination

    rows = []
    for raw in reader:
        row = standardise_row(raw, settings, header_destinations)
        customer_id = clean_text(row.get("Customer ID", ""))
        if customer_id and has_required_values(row, settings):
            row["Customer ID"] = customer_id
            rows.append(row)

    if not rows and not allow_empty:
        raise ValueError(
            f"{dataset_name} has no valid job rows with all required values."
        )
    return rows


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

    return clean_csv_reader(
        csv.DictReader(io.StringIO(text)), settings, dataset_name, allow_empty
    )


def write_csv_rows(
    handle: Any,
    rows: list[dict[str, str]],
    columns: list[str],
) -> None:
    writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)


def csv_bytes(rows: list[dict[str, str]], columns: list[str]) -> bytes:
    """Create a CSV download in memory, without creating a local file."""

    buffer = io.StringIO(newline="")
    write_csv_rows(buffer, rows, columns)
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


def comparison_periods(
    start: date,
    end: date,
) -> list[tuple[str, date, date]]:
    """Return the current, immediately previous, and prior-year periods."""

    period_days = (end - start).days
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days)
    try:
        last_year_start = start.replace(year=start.year - 1)
        last_year_end = end.replace(year=end.year - 1)
    except ValueError:
        last_year_start = start.replace(year=start.year - 1, day=28)
        last_year_end = end.replace(year=end.year - 1, day=28)
    return [
        ("Current Period", start, end),
        ("Previous Period", previous_start, previous_end),
        ("Same Period Last Year", last_year_start, last_year_end),
    ]


def stable_row_preference(
    row: dict[str, str],
    settings: dict[str, Any],
    category_map: dict[str, str] | None = None,
) -> tuple[int, int, tuple[str, ...]]:
    """Rank same-day lead rows without depending on their CSV order."""

    category = invoice_category(row, settings, category_map)
    category_rank = {"HVAC": 0, "Plumbing": 1}.get(category, 2)
    return (
        0 if clean_text(row.get("Opportunity", "")) else 1,
        category_rank,
        tuple(
            clean_text(row.get(column, "")).casefold()
            for column in settings["master_columns"]
        ),
    )


def first_seen_customer_rows(
    rows: list[dict[str, str]],
    settings: dict[str, Any],
) -> list[dict[str, str]]:
    """Return one earliest-dated row per customer from the complete master."""

    selected: dict[
        str,
        tuple[date, tuple[int, int, tuple[str, ...]], dict[str, str]],
    ] = {}
    category_map = invoice_category_mapping(settings)
    for row in rows:
        customer_id = clean_text(row.get("Customer ID", ""))
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        if not customer_id or day is None:
            continue
        current = selected.get(customer_id)
        if current is not None and day > current[0]:
            continue
        preference = stable_row_preference(row, settings, category_map)
        if current is None or (day, preference) < (current[0], current[1]):
            selected[customer_id] = (day, preference, row)

    return [
        row
        for _, _, row in sorted(
            selected.values(),
            key=lambda item: (item[0], item[2].get("Customer ID", ""), item[1]),
        )
    ]


def row_identity(
    row: dict[str, str],
    settings: dict[str, Any],
) -> tuple[str, ...]:
    return tuple(
        clean_text(row.get(column, ""))
        for column in settings["job_identity_columns"]
    )


def row_snapshot(
    row: dict[str, str],
    settings: dict[str, Any],
) -> tuple[str, ...]:
    return tuple(
        clean_text(row.get(column, ""))
        for column in settings["master_columns"]
    )


def merge_master_rows(
    master_rows: list[dict[str, str]],
    incoming_rows: list[dict[str, str]],
    settings: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]], int, int]:
    """Replace uploaded job-group snapshots and report added/updated/skipped rows."""

    incoming_groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in incoming_rows:
        incoming_groups[row_identity(row, settings)].append(row)

    incoming_keys = set(incoming_groups)
    existing_groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    merged_rows = []
    known_customers = set()
    for row in master_rows:
        customer_id = row.get("Customer ID")
        if customer_id:
            known_customers.add(customer_id)
        identity = row_identity(row, settings)
        if identity in incoming_keys:
            existing_groups[identity].append(row)
        else:
            merged_rows.append(row)
    appended_rows = 0
    updated_rows = 0
    skipped_rows = 0
    for identity, replacement_rows in incoming_groups.items():
        existing_rows = existing_groups.get(identity)
        if not existing_rows:
            appended_rows += len(replacement_rows)
        elif Counter(
            row_snapshot(row, settings) for row in existing_rows
        ) == Counter(
            row_snapshot(row, settings) for row in replacement_rows
        ):
            skipped_rows += len(replacement_rows)
        else:
            updated_rows += len(replacement_rows)
        merged_rows.extend(replacement_rows)

    new_customer_rows = [
        row for row in first_seen_customer_rows(incoming_rows, settings)
        if row["Customer ID"] not in known_customers
    ]
    return merged_rows, new_customer_rows, updated_rows, skipped_rows


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


def invoice_category_mapping(settings: dict[str, Any]) -> dict[str, str]:
    return {
        clean_text(invoice_type).casefold(): category
        for invoice_type, category in settings.get(
            "invoice_type_categories", {}
        ).items()
    }


def invoice_category(
    row: dict[str, str],
    settings: dict[str, Any],
    category_map: dict[str, str] | None = None,
) -> str:
    """Map an Invoice Business Unit to its reporting category."""

    mappings = category_map or invoice_category_mapping(settings)
    invoice_type = clean_text(row.get("Invoice Business Unit", "")).casefold()
    return mappings.get(invoice_type, "")


def category_period_comparisons(
    master_rows: list[dict[str, str]],
    start: date,
    end: date,
    settings: dict[str, Any],
    first_seen_rows: list[dict[str, str]] | None = None,
) -> dict[str, list[dict[str, str | int]]]:
    """Build internally consistent, mutually exclusive category comparisons."""

    categories = ("HVAC", "Plumbing")
    comparisons: dict[str, list[dict[str, str | int]]] = {
        category: [] for category in categories
    }
    if first_seen_rows is None:
        first_seen_rows = first_seen_customer_rows(master_rows, settings)
    first_seen: dict[str, date] = {}
    for row in first_seen_rows:
        customer_id = clean_text(row.get("Customer ID", ""))
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        if customer_id and day is not None:
            first_seen[customer_id] = day
    history_start = min(first_seen.values(), default=None)

    periods = comparison_periods(start, end)
    rows_by_period: list[list[dict[str, str]]] = [[] for _ in periods]
    for row in master_rows:
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        if day is None:
            continue
        for index, (_, period_start, period_end) in enumerate(periods):
            if period_start <= day <= period_end:
                rows_by_period[index].append(row)
                break

    category_map = invoice_category_mapping(settings)
    for (label, period_start, period_end), period_rows in zip(
        periods, rows_by_period
    ):
        customer_categories: dict[
            str,
            tuple[date, tuple[int, int, tuple[str, ...]], str],
        ] = {}
        for row in period_rows:
            customer_id = clean_text(row.get("Customer ID", ""))
            category = invoice_category(row, settings, category_map)
            day = parse_date(row.get("Created Date", ""), settings["date_formats"])
            if not customer_id or category not in categories or day is None:
                continue
            preference = stable_row_preference(row, settings, category_map)
            current = customer_categories.get(customer_id)
            candidate = (day, preference, category)
            if current is None or candidate < current:
                customer_categories[customer_id] = candidate

        for category in categories:
            assigned_customers = {
                customer_id
                for customer_id, (_, _, assigned_category) in customer_categories.items()
                if assigned_category == category
            }
            category_rows = [
                row for row in period_rows
                if row.get("Customer ID") in assigned_customers
                and invoice_category(row, settings, category_map) == category
            ]
            if history_start is None or history_start >= period_start:
                new_customers: int | str = "Insufficient history"
            else:
                new_customers = sum(
                    period_start <= first_seen[customer_id] <= period_end
                    for customer_id in assigned_customers
                )
            comparisons[category].append({
                "Period": label,
                "Start": period_start.isoformat(),
                "End": period_end.isoformat(),
                **metric_summary(category_rows),
                "New Customers": new_customers,
            })

    for category_comparisons in comparisons.values():
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
    category_map = invoice_category_mapping(settings)

    for row, day in dated_rows:
        if day.year not in counts:
            continue
        week = day.isocalendar().week
        if day.year == current_year and week > last_week:
            continue
        if week > 52:
            continue
        if invoice_category(row, settings, category_map).casefold() != category.casefold():
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


def process_import_rows(
    cleaned_rows: list[dict[str, str]],
    master_rows: list[dict[str, str]],
    settings: dict[str, Any],
    period_start: date | None = None,
    period_end: date | None = None,
) -> ProcessedImport:
    """Apply one import consistently for both file and web entry points."""

    inferred_start, inferred_end = infer_period(cleaned_rows, settings)
    start = period_start or inferred_start
    end = period_end or inferred_end
    if end < start:
        raise ValueError("Period end must be on or after period start.")

    final_master, new_customers, updated, skipped = merge_master_rows(
        master_rows, cleaned_rows, settings
    )
    new_leads_rows = first_seen_customer_rows(final_master, settings)
    category_comparisons = category_period_comparisons(
        final_master, start, end, settings, new_leads_rows
    )
    # Import locally to keep the core CSV helpers usable without Google packages.
    from .zip_maps import zip_code_summaries

    zip_year, zip_revenue_rows, zip_new_lead_rows = zip_code_summaries(
        final_master, new_leads_rows, settings
    )
    analysis_rows, analysis_columns = job_status_analysis(cleaned_rows)
    return ProcessedImport(
        cleaned_rows=cleaned_rows,
        master_rows=final_master,
        new_customer_rows=new_customers,
        new_leads_rows=new_leads_rows,
        category_comparisons=category_comparisons,
        analysis_csv=analysis_csv_bytes(
            category_comparisons, analysis_rows, analysis_columns
        ),
        zip_year=zip_year,
        zip_revenue_rows=zip_revenue_rows,
        zip_new_lead_rows=zip_new_lead_rows,
        appended_rows=len(cleaned_rows) - updated - skipped,
        updated_rows=updated,
        skipped_rows=skipped,
        period_start=start,
        period_end=end,
    )


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
    master_rows = read_and_clean_export(
        master_path, settings, "Master dataset", allow_empty=True
    )

    processed = process_import_rows(
        cleaned_rows, master_rows, settings, period_start, period_end
    )

    # Persist only valid Customer ID rows in the one continuing master dataset.
    write_csv(master_path, processed.master_rows, columns)

    output_dir.mkdir(parents=True, exist_ok=True)

    cleaned_file = output_dir / f"{source.stem}_cleaned.csv"

    write_csv(cleaned_file, processed.cleaned_rows, columns)
    write_csv(output_dir / "new_customers.csv", processed.new_customer_rows, columns)

    new_leads_path = master_path.parent / settings["new_leads_filename"]

    write_csv(
        new_leads_path,
        processed.new_leads_rows,
        columns,
    )
    (output_dir / "analysis.csv").write_bytes(processed.analysis_csv)

    (output_dir / "report_data.json").write_text(
        json.dumps({
            "period": {
                "start": processed.period_start.isoformat(),
                "end": processed.period_end.isoformat(),
            },
            "category_comparisons": processed.category_comparisons,
            "new_customer_count": len(processed.new_customer_rows),
        }, indent=2),
        encoding="utf-8",
    )

    return ImportResult(
        cleaned_rows=len(processed.cleaned_rows),
        appended_rows=processed.appended_rows,
        updated_rows=processed.updated_rows,
        skipped_duplicate_rows=processed.skipped_rows,
        new_customers=len(processed.new_customer_rows),
        period_start=processed.period_start,
        period_end=processed.period_end,
        output_dir=output_dir,
    )


def run_uploaded_import(
    source_contents: bytes,
    master_contents: bytes,
    settings: dict[str, Any],
    period_start: date | None = None,
    period_end: date | None = None,
) -> UploadedImportResult:
    """Process an uploaded report against the stored master in memory."""

    columns = settings["master_columns"]
    cleaned_rows = read_and_clean_csv_bytes(source_contents, settings, "New ServiceTitan export")
    master_rows = read_and_clean_csv_bytes(
        master_contents, settings, "Master dataset", allow_empty=True
    )
    processed = process_import_rows(
        cleaned_rows, master_rows, settings, period_start, period_end
    )

    return UploadedImportResult(
        cleaned_rows=len(processed.cleaned_rows),
        appended_rows=processed.appended_rows,
        updated_rows=processed.updated_rows,
        skipped_duplicate_rows=processed.skipped_rows,
        new_customers=len(processed.new_customer_rows),
        period_start=processed.period_start,
        period_end=processed.period_end,
        cleaned_csv=csv_bytes(processed.cleaned_rows, columns),
        master_csv=csv_bytes(processed.master_rows, columns),
        new_leads_csv=csv_bytes(processed.new_leads_rows, columns),
        analysis_csv=processed.analysis_csv,
        zip_year=processed.zip_year,
        zip_revenue_rows=processed.zip_revenue_rows,
        zip_new_lead_rows=processed.zip_new_lead_rows,
    )
