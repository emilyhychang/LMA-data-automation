import csv
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook
import streamlit as st
from streamlit.testing.v1 import AppTest


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.competitive_spend import (
    analysis_csv_bytes,
    available_months,
    available_years,
    chart_share_rows,
    period_label,
    read_competitive_spend_workbook,
    spend_share_rows,
)


def workbook_bytes(rows, sheet_name="Detail View"):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append([
        "Media Outlet",
        "Market",
        "Account",
        "Parent",
        "Category",
        "Instances",
        "AdRev®",
        "Qtr",
        "Month",
        "Year",
        "Mtype",
    ])
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


class CompetitiveSpendTest(unittest.TestCase):
    def setUp(self):
        self.contents = workbook_bytes([
            ["TV-1", "San Diego", "A", "Parent A", "Home", 1, 60, 1, "Jan", 2026, "TV"],
            ["TV-2", "San Diego", "B", "Parent B", "Home", 1, 40, 1, "Jan", 2026, "TV"],
            ["TV-1", "San Diego", "A", "Parent A", "Home", 1, 50, 1, "Feb", 2026, "TV"],
            ["Cable-1", "San Diego", "C", "Parent C", "Home", 1, 25, 1, "Jan", 2026, "Cable"],
            ["Radio-1", "San Diego", "D", "Parent D", "Home", 1, 15, 1, "Jan", 2026, "Radio"],
            ["Radio-1", "San Diego", "D", "Parent D", "Home", 1, 10, 1, "Jan", 2025, "Radio"],
            ["Radio-2", "San Diego", "E", "Parent E", "Home", 1, None, 1, "Jan", 2025, "Radio"],
        ])

    def test_reads_detail_view_and_tracks_blank_spend(self):
        dataset = read_competitive_spend_workbook(self.contents)

        self.assertEqual(len(dataset.records), 6)
        self.assertEqual(dataset.source_rows, 7)
        self.assertEqual(dataset.blank_spend_rows, 1)
        self.assertEqual(available_years(dataset.records), (2026, 2025))
        self.assertEqual(available_months(dataset.records, 2026), (1, 2))
        self.assertEqual(period_label(2026, (1, 2)), "2026 YTD through February")

    def test_aggregates_parent_and_outlet_share(self):
        dataset = read_competitive_spend_workbook(self.contents)

        parent = spend_share_rows(
            dataset.records,
            media_type="TV",
            dimension="parent",
            year=2026,
        )
        january_outlet = spend_share_rows(
            dataset.records,
            media_type="TV",
            dimension="media_outlet",
            year=2026,
            month=1,
        )

        self.assertEqual(parent[0]["Name"], "Parent A")
        self.assertEqual(parent[0]["Spend"], 110)
        self.assertAlmostEqual(parent[0]["Share of spend"], 110 / 150)
        self.assertEqual([row["Spend"] for row in january_outlet], [60, 40])

    def test_chart_combines_small_entries_and_download_includes_percentages(self):
        rows = [
            {"Name": str(index), "Spend": 12 - index, "Share of spend": (12 - index) / 78}
            for index in range(12)
        ]
        chart = chart_share_rows(rows, limit=10)
        self.assertEqual(len(chart), 11)
        self.assertEqual(chart[-1]["Name"], "Other")

        dataset = read_competitive_spend_workbook(self.contents)
        download = list(csv.DictReader(
            io.StringIO(analysis_csv_bytes(dataset.records).decode())
        ))
        self.assertTrue(download)
        self.assertIn("Share of Spend", download[0])
        self.assertEqual(
            {row["Period Type"] for row in download},
            {"Monthly", "YTD"},
        )

    def test_rejects_workbook_without_detail_view(self):
        with self.assertRaisesRegex(ValueError, "Detail View"):
            read_competitive_spend_workbook(
                workbook_bytes([], sheet_name="Other")
            )

    def test_page_generates_six_charts_from_one_workbook(self):
        with patch.object(st, "user", SimpleNamespace(
            is_logged_in=True,
            to_dict=lambda: {
                "email": "test@example.com",
                "email_verified": True,
            },
        )):
            app = AppTest.from_file(
                str(PROJECT / "competitive_spend_page.py"),
                default_timeout=20,
            )
            app.secrets["access"] = {"admin_emails": ["test@example.com"]}
            app.run()
            app.file_uploader(key="competitive_spend_workbook").set_value((
                "competitive-spend.xlsx",
                self.contents,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )).run()

        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("echarts_chart")), 6)
        self.assertEqual(
            [metric.label for metric in app.metric],
            ["TV spend", "Cable spend", "Radio spend"],
        )
        self.assertTrue(any(
            button.label == "Download spend-share analysis"
            for button in app.get("download_button")
        ))


if __name__ == "__main__":
    unittest.main()
