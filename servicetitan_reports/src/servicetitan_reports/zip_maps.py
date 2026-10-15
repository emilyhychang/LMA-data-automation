"""ZIP-code summaries and Google Sheets publishing for Looker Studio maps."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence, Union

from .pipeline import clean_text, parse_date


ZipMetricRow = dict[str, Union[str, int, float]]


def normalize_zip(value: object | None) -> str:
    """Return a five-digit US ZIP code, or an empty string when invalid."""

    text = clean_text(value)
    match = re.fullmatch(r"(\d{5})(?:-\d{4}|\.0)?", text)
    return match.group(1) if match else ""


def _latest_year(
    rows: Sequence[dict[str, str]], settings: Mapping[str, Any]
) -> int | None:
    years = [
        day.year
        for row in rows
        if (day := parse_date(
            row.get("Created Date", ""), settings["date_formats"]
        ))
    ]
    return max(years, default=None)


def zip_code_summaries(
    master_rows: Sequence[dict[str, str]],
    new_lead_rows: Sequence[dict[str, str]],
    settings: Mapping[str, Any],
) -> tuple[int | None, list[ZipMetricRow], list[ZipMetricRow]]:
    """Aggregate latest-year revenue and new leads for every valid ZIP code."""

    year = _latest_year(master_rows, settings)
    if year is None:
        return None, [], []

    revenue_by_zip: dict[str, Decimal] = defaultdict(Decimal)
    for row in master_rows:
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        zip_code = normalize_zip(row.get("Location Zip", ""))
        if day is None or day.year != year or not zip_code:
            continue
        try:
            revenue_by_zip[zip_code] += Decimal(clean_text(row.get("Total", "")) or "0")
        except InvalidOperation:
            continue

    leads_by_zip: dict[str, int] = defaultdict(int)
    for row in new_lead_rows:
        day = parse_date(row.get("Created Date", ""), settings["date_formats"])
        zip_code = normalize_zip(row.get("Location Zip", ""))
        if day is not None and day.year == year and zip_code:
            leads_by_zip[zip_code] += 1

    revenue_rows: list[ZipMetricRow] = [
        {"Zip": zip_code, "Revenue": float(amount)}
        for zip_code, amount in sorted(
            revenue_by_zip.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    lead_rows: list[ZipMetricRow] = [
        {"Zip": zip_code, "New leads": count}
        for zip_code, count in sorted(
            leads_by_zip.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return year, revenue_rows, lead_rows


def _service_account_client(
    service_account_info: Mapping[str, Any] | None = None,
    credentials_file: str | Path | None = None,
):
    import gspread

    if service_account_info:
        return gspread.service_account_from_dict(dict(service_account_info))
    if credentials_file:
        return gspread.service_account(filename=str(credentials_file))
    raise ValueError("Google service-account credentials are not configured.")


def _replace_worksheet(
    client: Any,
    spreadsheet_id: str,
    worksheet_name: str,
    headers: Sequence[str],
    rows: Sequence[Mapping[str, str | int | float]],
) -> None:
    worksheet = client.open_by_key(spreadsheet_id).worksheet(worksheet_name)
    values = [list(headers)] + [
        [row.get(column, "") for column in headers]
        for row in rows
    ]
    worksheet.clear()
    worksheet.update(values=values, range_name="A1", value_input_option="RAW")


def publish_zip_summaries(
    revenue_rows: Sequence[Mapping[str, str | int | float]],
    new_lead_rows: Sequence[Mapping[str, str | int | float]],
    *,
    revenue_spreadsheet_id: str,
    new_leads_spreadsheet_id: str,
    worksheet_name: str = "Sheet1",
    service_account_info: Mapping[str, Any] | None = None,
    credentials_file: str | Path | None = None,
    client: Any | None = None,
) -> None:
    """Replace both Looker Studio source sheets with current ZIP summaries."""

    google_client = client or _service_account_client(
        service_account_info, credentials_file
    )
    _replace_worksheet(
        google_client,
        revenue_spreadsheet_id,
        worksheet_name,
        ("Zip", "Revenue"),
        revenue_rows,
    )
    _replace_worksheet(
        google_client,
        new_leads_spreadsheet_id,
        worksheet_name,
        ("Zip", "New leads"),
        new_lead_rows,
    )
