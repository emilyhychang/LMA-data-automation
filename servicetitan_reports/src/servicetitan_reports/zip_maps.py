"""ZIP-code summaries and Google Sheets publishing for Looker Studio maps."""

from __future__ import annotations

import re
import time
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
        client = gspread.service_account_from_dict(dict(service_account_info))
    elif credentials_file:
        client = gspread.service_account(filename=str(credentials_file))
    else:
        raise ValueError("Google service-account credentials are not configured.")
    client.set_timeout(20)
    return client


def map_error_message(error: Exception) -> str:
    """Return actionable diagnostics without exposing credentials or raw responses."""
    code = getattr(getattr(error, "response", None), "status_code", None)
    name = type(error).__name__
    if code in (401, 403) or name in {"PermissionError", "RefreshError"}:
        return "Google denied access. Check the deployed service-account key, Sheets API access, and Editor sharing on both source spreadsheets."
    if code == 404 or name == "SpreadsheetNotFound":
        return "A source spreadsheet was not found or is not shared with the service account. Check both spreadsheet IDs and sharing."
    if name == "WorksheetNotFound":
        return "The source worksheet name was not found. Check the configured tab name in both spreadsheets."
    if isinstance(error, (ValueError, FileNotFoundError, KeyError)):
        return "Map configuration is incomplete or invalid. Configure the deployed Google service-account credential and both source spreadsheet IDs."
    return "Map refresh failed or timed out. The two maps may differ in freshness. Retry Refresh map sources; if it persists, check the server diagnostics."


def _retry(operation, sleep=time.sleep):
    from requests.exceptions import ConnectionError, Timeout
    from google.auth.exceptions import TransportError
    for attempt in range(3):
        try:
            return operation()
        except Exception as error:
            code = getattr(getattr(error, "response", None), "status_code", None)
            if attempt == 2 or not (code in (429, 500, 502, 503, 504) or isinstance(error, (ConnectionError, Timeout, TransportError))):
                raise
            sleep(0.5 * (2 ** attempt))


def _replace_worksheet(
    spreadsheet: Any,
    worksheet: Any,
    headers: Sequence[str],
    rows: Sequence[Mapping[str, str | int | float]],
) -> None:
    values = [list(headers)] + [
        [row.get(column, "") for column in headers]
        for row in rows
    ]
    row_count = max(worksheet.row_count, len(values))
    requests = []
    if len(values) > worksheet.row_count or worksheet.col_count < len(headers):
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": worksheet.id, "gridProperties": {
                "rowCount": row_count, "columnCount": max(worksheet.col_count, len(headers))}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount",
        }})
    requests.append({"updateCells": {
        "range": {"sheetId": worksheet.id, "startRowIndex": 0, "endRowIndex": row_count,
                  "startColumnIndex": 0, "endColumnIndex": len(headers)},
        "rows": [{"values": [{"userEnteredValue": {
            "numberValue" if isinstance(value, (int, float)) else "stringValue": value
        }} for value in row]} for row in values],
        "fields": "userEnteredValue",
    }})
    # Sheets applies this batch atomically: replace values AND clear obsolete tail
    # cells in one request, without a destructive standalone clear operation.
    _retry(lambda: spreadsheet.batch_update({"requests": requests}))


def publish_zip_summaries(
    revenue_rows: Sequence[Mapping[str, str | int | float]],
    new_lead_rows: Sequence[Mapping[str, str | int | float]],
    *,
    revenue_spreadsheet_id: str,
    new_leads_spreadsheet_id: str,
    generated_at: str,
    worksheet_name: str = "Sheet1",
    service_account_info: Mapping[str, Any] | None = None,
    credentials_file: str | Path | None = None,
    client: Any | None = None,
) -> None:
    """Replace both Looker Studio source sheets with current ZIP summaries."""

    google_client = client or _service_account_client(
        service_account_info, credentials_file
    )
    if not revenue_spreadsheet_id or not new_leads_spreadsheet_id or revenue_spreadsheet_id == new_leads_spreadsheet_id:
        raise ValueError("Two different map source spreadsheets are required.")
    # Resolve both destinations before writing either one.
    revenue_book = _retry(lambda: google_client.open_by_key(revenue_spreadsheet_id))
    leads_book = _retry(lambda: google_client.open_by_key(new_leads_spreadsheet_id))
    revenue_sheet = _retry(lambda: revenue_book.worksheet(worksheet_name))
    leads_sheet = _retry(lambda: leads_book.worksheet(worksheet_name))
    timestamp_column = "Map data generated at"
    timestamped_revenue_rows = [
        {**row, timestamp_column: generated_at}
        for row in revenue_rows
    ]
    timestamped_new_lead_rows = [
        {**row, timestamp_column: generated_at}
        for row in new_lead_rows
    ]
    _replace_worksheet(
        revenue_book,
        revenue_sheet,
        ("Zip", "Revenue", timestamp_column),
        timestamped_revenue_rows,
    )
    _replace_worksheet(
        leads_book,
        leads_sheet,
        ("Zip", "New leads", timestamp_column),
        timestamped_new_lead_rows,
    )
