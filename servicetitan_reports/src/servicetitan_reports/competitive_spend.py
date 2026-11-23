"""Read and summarize the competitive-spend workbook without Excel pivots."""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import load_workbook


TARGET_MEDIA_TYPES = ("TV", "Cable", "Radio")
MONTH_NAMES = (
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
MONTH_LOOKUP = {
    name.casefold(): index for index, name in enumerate(MONTH_NAMES, start=1)
}
MONTH_LOOKUP.update({
    name[:3].casefold(): index for index, name in enumerate(MONTH_NAMES, start=1)
})


@dataclass(frozen=True)
class CompetitiveSpendRecord:
    media_outlet: str
    parent: str
    spend: float
    month: int
    year: int
    media_type: str


@dataclass(frozen=True)
class CompetitiveSpendDataset:
    records: tuple[CompetitiveSpendRecord, ...]
    source_rows: int
    blank_spend_rows: int
    invalid_spend_rows: int
    invalid_period_rows: int
    missing_dimension_rows: int


def _clean_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _header_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean_text(value).casefold())


def _parse_spend(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)

    cleaned = _clean_text(value).replace("$", "").replace(",", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    try:
        return float(Decimal(cleaned))
    except InvalidOperation:
        raise ValueError(f"Invalid AdRev value: {value!r}") from None


def _parse_month(value: object) -> int | None:
    if isinstance(value, (datetime, date)):
        return value.month
    if isinstance(value, (int, float)) and 1 <= int(value) <= 12:
        return int(value)
    return MONTH_LOOKUP.get(_clean_text(value).casefold())


def _parse_year(value: object) -> int | None:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 2000 <= year <= 2100 else None


def _media_type_lookup(workbook) -> dict[str, str]:
    """Read the workbook's outlet-to-media lookup as a formula-cache fallback."""

    for worksheet in workbook.worksheets:
        headers = [
            _header_key(worksheet.cell(1, column).value)
            for column in range(1, min(worksheet.max_column, 20) + 1)
        ]
        if "media" not in headers or "mediatype" not in headers:
            continue
        media_column = headers.index("media") + 1
        type_column = headers.index("mediatype") + 1
        return {
            _clean_text(worksheet.cell(row, media_column).value).casefold():
                _clean_text(worksheet.cell(row, type_column).value)
            for row in range(2, worksheet.max_row + 1)
            if _clean_text(worksheet.cell(row, media_column).value)
        }
    return {}


def read_competitive_spend_workbook(contents: bytes) -> CompetitiveSpendDataset:
    """Load the raw Detail View records saved inside the Excel workbook."""

    try:
        workbook = load_workbook(
            io.BytesIO(contents), read_only=True, data_only=True
        )
    except Exception as error:
        raise ValueError("The upload is not a readable Excel workbook.") from error

    sheet_name = next(
        (name for name in workbook.sheetnames if name.casefold() == "detail view"),
        None,
    )
    if sheet_name is None:
        raise ValueError("The workbook must contain a sheet named 'Detail View'.")

    worksheet = workbook[sheet_name]
    rows = worksheet.iter_rows(values_only=True)
    try:
        raw_headers = next(rows)
    except StopIteration:
        raise ValueError("The Detail View sheet is empty.") from None

    header_positions = {
        _header_key(header): index for index, header in enumerate(raw_headers)
    }
    aliases = {
        "media_outlet": ("mediaoutlet",),
        "parent": ("parent", "parentadvertiser"),
        "spend": ("adrev", "adrevenue"),
        "month": ("month",),
        "year": ("year",),
        "media_type": ("mtype", "mediatype"),
    }
    positions: dict[str, int] = {}
    missing: list[str] = []
    for field, options in aliases.items():
        position = next(
            (header_positions[option] for option in options if option in header_positions),
            None,
        )
        if position is None:
            missing.append(options[0])
        else:
            positions[field] = position
    if missing:
        raise ValueError(
            "Detail View is missing required columns: " + ", ".join(missing)
        )

    media_lookup = _media_type_lookup(workbook)
    records: list[CompetitiveSpendRecord] = []
    source_rows = blank_spend_rows = invalid_spend_rows = 0
    invalid_period_rows = missing_dimension_rows = 0

    for values in rows:
        if not any(value not in (None, "") for value in values):
            continue
        source_rows += 1

        def value(field: str):
            position = positions[field]
            return values[position] if position < len(values) else None

        media_outlet = _clean_text(value("media_outlet"))
        parent = _clean_text(value("parent"))
        if not media_outlet or not parent:
            missing_dimension_rows += 1

        try:
            spend = _parse_spend(value("spend"))
        except ValueError:
            invalid_spend_rows += 1
            continue
        if spend is None:
            blank_spend_rows += 1
            continue

        month = _parse_month(value("month"))
        year = _parse_year(value("year"))
        if month is None or year is None:
            invalid_period_rows += 1
            continue

        media_type = _clean_text(value("media_type"))
        if not media_type:
            media_type = media_lookup.get(media_outlet.casefold(), "")
        if not media_type:
            continue

        records.append(CompetitiveSpendRecord(
            media_outlet=media_outlet,
            parent=parent,
            spend=spend,
            month=month,
            year=year,
            media_type=media_type,
        ))

    if not records:
        raise ValueError("No usable competitive-spend rows were found.")

    return CompetitiveSpendDataset(
        records=tuple(records),
        source_rows=source_rows,
        blank_spend_rows=blank_spend_rows,
        invalid_spend_rows=invalid_spend_rows,
        invalid_period_rows=invalid_period_rows,
        missing_dimension_rows=missing_dimension_rows,
    )


def available_years(
    records: tuple[CompetitiveSpendRecord, ...],
    media_types: tuple[str, ...] = TARGET_MEDIA_TYPES,
) -> tuple[int, ...]:
    allowed = {value.casefold() for value in media_types}
    return tuple(sorted({
        record.year for record in records
        if record.media_type.casefold() in allowed
    }, reverse=True))


def available_months(
    records: tuple[CompetitiveSpendRecord, ...],
    year: int,
    media_types: tuple[str, ...] = TARGET_MEDIA_TYPES,
) -> tuple[int, ...]:
    allowed = {value.casefold() for value in media_types}
    return tuple(sorted({
        record.month for record in records
        if record.year == year and record.media_type.casefold() in allowed
    }))


def spend_share_rows(
    records: tuple[CompetitiveSpendRecord, ...],
    *,
    media_type: str,
    dimension: str,
    year: int,
    month: int | None = None,
) -> list[dict[str, object]]:
    """Aggregate spend and percentage share for one chart and table."""

    if dimension not in {"parent", "media_outlet"}:
        raise ValueError("dimension must be 'parent' or 'media_outlet'")

    totals: defaultdict[str, float] = defaultdict(float)
    for record in records:
        if record.media_type.casefold() != media_type.casefold():
            continue
        if record.year != year or (month is not None and record.month != month):
            continue
        name = getattr(record, dimension).strip()
        if name:
            totals[name] += record.spend

    period_total = sum(totals.values())
    if not period_total:
        return []
    return [
        {
            "Name": name,
            "Spend": spend,
            "Share of spend": spend / period_total,
        }
        for name, spend in sorted(
            totals.items(), key=lambda item: (-item[1], item[0].casefold())
        )
    ]


def chart_share_rows(
    rows: list[dict[str, object]], limit: int = 10
) -> list[dict[str, object]]:
    """Keep the largest slices readable and combine the remainder as Other."""

    if len(rows) <= limit:
        return rows
    leading = [dict(row) for row in rows[:limit]]
    other_spend = sum(float(row["Spend"]) for row in rows[limit:])
    other_share = sum(float(row["Share of spend"]) for row in rows[limit:])
    leading.append({
        "Name": "Other",
        "Spend": other_spend,
        "Share of spend": other_share,
    })
    return leading


def period_label(year: int, months: tuple[int, ...]) -> str:
    if months == tuple(range(1, 13)):
        return f"Full year {year}"
    if months:
        return f"{year} YTD through {MONTH_NAMES[max(months) - 1]}"
    return str(year)


def analysis_csv_bytes(
    records: tuple[CompetitiveSpendRecord, ...],
) -> bytes:
    """Export monthly and YTD spend shares for every requested breakdown."""

    output = io.StringIO(newline="")
    columns = [
        "Media Type",
        "Breakdown",
        "Name",
        "Year",
        "Period Type",
        "Period",
        "Spend",
        "Share of Spend",
    ]
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    years = available_years(records)
    for year in sorted(years):
        months = available_months(records, year)
        for media_type in TARGET_MEDIA_TYPES:
            for dimension, breakdown in (
                ("parent", "Parent advertiser"),
                ("media_outlet", "Media outlet"),
            ):
                for row in spend_share_rows(
                    records,
                    media_type=media_type,
                    dimension=dimension,
                    year=year,
                ):
                    writer.writerow({
                        "Media Type": media_type,
                        "Breakdown": breakdown,
                        "Name": row["Name"],
                        "Year": year,
                        "Period Type": "YTD" if len(months) < 12 else "Annual",
                        "Period": period_label(year, months),
                        "Spend": f"{float(row['Spend']):.2f}",
                        "Share of Spend": f"{float(row['Share of spend']):.6f}",
                    })
                for month in months:
                    for row in spend_share_rows(
                        records,
                        media_type=media_type,
                        dimension=dimension,
                        year=year,
                        month=month,
                    ):
                        writer.writerow({
                            "Media Type": media_type,
                            "Breakdown": breakdown,
                            "Name": row["Name"],
                            "Year": year,
                            "Period Type": "Monthly",
                            "Period": MONTH_NAMES[month - 1],
                            "Spend": f"{float(row['Spend']):.2f}",
                            "Share of Spend": f"{float(row['Share of spend']):.6f}",
                        })
    return output.getvalue().encode("utf-8")
