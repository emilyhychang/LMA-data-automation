import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.pipeline import compare_periods, csv_bytes, load_settings, run_import


class PipelineTest(unittest.TestCase):
    
    def test_first_seen_and_reimport_are_idempotent(self):
        settings = load_settings(PROJECT / "config/settings.json")
        # Legacy fixtures intentionally contain only the minimum lead fields.
        settings["required_columns"] = ["Customer ID", "Created Date"]
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            period_1 = temp_path / "period_1.csv"
            period_2 = temp_path / "period_2.csv"
            period_1.write_bytes(csv_bytes([
                {"Customer ID": "1001", "Created Date": "09/01/2026"},
                {"Customer ID": "1001", "Created Date": "09/02/2026"},
                {"Customer ID": "1002", "Created Date": "09/03/2026"},
            ], settings["master_columns"]))
            period_2.write_bytes(csv_bytes([
                {"Customer ID": "1002", "Created Date": "09/04/2026"},
                {"Customer ID": "1003", "Created Date": "09/05/2026"},
                {"Customer ID": "1003", "Created Date": "09/06/2026"},
            ], settings["master_columns"]))

            first = run_import(period_1, temp_path / "master.csv", settings, temp_path / "out1")
            self.assertEqual((first.appended_rows, first.new_customers), (3, 2))
            with (temp_path / "out1/period_1_cleaned.csv").open() as handle:
                cleaned = list(csv.DictReader(handle))

            self.assertEqual(
                list(cleaned[0]),
                settings["master_columns"],
            )
            second = run_import(period_2, temp_path / "master.csv", settings, temp_path / "out2")
            self.assertEqual((second.appended_rows, second.new_customers), (3, 1))
            repeat = run_import(period_2, temp_path / "master.csv", settings, temp_path / "out3")
            self.assertEqual((repeat.appended_rows, repeat.new_customers), (0, 0))
            repeat_first = run_import(period_1, temp_path / "master.csv", settings, temp_path / "out4")
            self.assertEqual((repeat_first.appended_rows, repeat_first.new_customers), (0, 0))
            with (temp_path / "out2/new_customers.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Customer ID"] for row in rows], ["1003"])

    def test_comparison_new_customers_are_period_relative(self):
        settings = load_settings(PROJECT / "config/settings.json")

        def row(customer_id, created_date):
            return {
                "Customer ID": customer_id,
                "Created Date": created_date,
                "Total": "100",
            }

        master_rows = [
            row("old", "09/06/2025"),
            row("old", "09/10/2025"),
            row("last-year-new", "09/10/2025"),
            row("before-previous", "08/23/2026"),
            row("before-previous", "08/25/2026"),
            row("previous-new", "08/25/2026"),
            row("previous-new", "09/08/2026"),
            row("current-new", "09/08/2026"),
            row("current-new", "09/09/2026"),
        ]

        # Reproduce the old failure: this list contains only a current-period lead.
        comparison = compare_periods(
            master_rows,
            [master_rows[-2]],
            date(2026, 9, 7),
            date(2026, 9, 20),
            settings,
        )

        self.assertEqual(
            {item["Period"]: item["New Customers"] for item in comparison},
            {
                "Current Period": 1,
                "Previous Period": 1,
                "Same Period Last Year": 1,
            },
        )

    def test_comparison_marks_insufficient_history(self):
        settings = load_settings(PROJECT / "config/settings.json")
        master_rows = [
            {"Customer ID": "first-known", "Created Date": "09/07/2025", "Total": "100"},
            {"Customer ID": "later", "Created Date": "09/08/2026", "Total": "100"},
        ]

        comparison = compare_periods(
            master_rows, [], date(2026, 9, 7), date(2026, 9, 20), settings
        )

        self.assertEqual(comparison[2]["New Customers"], "Insufficient history")


if __name__ == "__main__":
    unittest.main()
