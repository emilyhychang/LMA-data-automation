import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.pipeline import load_settings
from servicetitan_reports.zip_maps import (
    normalize_zip,
    publish_zip_summaries,
    zip_code_summaries,
)


class ZipMapTest(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(PROJECT / "config/settings.json")

    def test_normalize_zip_accepts_common_csv_forms(self):
        self.assertEqual(normalize_zip("92109"), "92109")
        self.assertEqual(normalize_zip("92109-1234"), "92109")
        self.assertEqual(normalize_zip("92109.0"), "92109")
        self.assertEqual(normalize_zip("not a zip"), "")

    def test_summaries_use_latest_year_and_sort_descending(self):
        master = [
            {"Created Date": "2025-12-31", "Location Zip": "92109", "Total": "999"},
            {"Created Date": "2026-01-01", "Location Zip": "92109", "Total": "100.25"},
            {"Created Date": "2026-01-02", "Location Zip": "92109-1234", "Total": "2.75"},
            {"Created Date": "2026-01-03", "Location Zip": "92037", "Total": "250"},
            {"Created Date": "2026-01-04", "Location Zip": "invalid", "Total": "500"},
        ]
        leads = [
            {"Created Date": "2025-01-01", "Location Zip": "92037"},
            {"Created Date": "2026-01-01", "Location Zip": "92109"},
            {"Created Date": "2026-01-02", "Location Zip": "92109.0"},
            {"Created Date": "2026-01-03", "Location Zip": "92037"},
        ]

        year, revenue, new_leads = zip_code_summaries(
            master, leads, self.settings
        )

        self.assertEqual(year, 2026)
        self.assertEqual(revenue, [
            {"Zip": "92037", "Revenue": 250.0},
            {"Zip": "92109", "Revenue": 103.0},
        ])
        self.assertEqual(new_leads, [
            {"Zip": "92109", "New leads": 2},
            {"Zip": "92037", "New leads": 1},
        ])

    def test_publish_replaces_both_source_sheets(self):
        class Worksheet:
            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append(("clear",))

            def update(self, **kwargs):
                self.calls.append(("update", kwargs))

        class Spreadsheet:
            def __init__(self, worksheet):
                self._worksheet = worksheet

            def worksheet(self, name):
                self._worksheet.calls.append(("worksheet", name))
                return self._worksheet

        class Client:
            def __init__(self):
                self.books = {}

            def open_by_key(self, key):
                worksheet = self.books.setdefault(key, Worksheet())
                return Spreadsheet(worksheet)

        client = Client()
        publish_zip_summaries(
            [{"Zip": "92037", "Revenue": 10.5}],
            [{"Zip": "92109", "New leads": 2}],
            revenue_spreadsheet_id="revenue",
            new_leads_spreadsheet_id="leads",
            worksheet_name="Sheet1",
            client=client,
        )

        self.assertEqual(client.books["revenue"].calls[-1][1]["values"], [
            ["Zip", "Revenue"], ["92037", 10.5]
        ])
        self.assertEqual(client.books["leads"].calls[-1][1]["values"], [
            ["Zip", "New leads"], ["92109", 2]
        ])


if __name__ == "__main__":
    unittest.main()
