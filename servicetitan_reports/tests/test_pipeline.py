import csv
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.pipeline import load_settings, run_import


class PipelineTest(unittest.TestCase):
    
    def test_first_seen_and_reimport_are_idempotent(self):
        settings = load_settings(PROJECT / "config/settings.json")
        # Legacy fixtures intentionally contain only the minimum lead fields.
        settings["required_columns"] = ["Customer ID", "Created Date"]
        fixtures = PROJECT / "fake_data"
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            first = run_import(fixtures / "period_1.csv", temp_path / "master.csv", settings, temp_path / "out1")
            self.assertEqual((first.appended_rows, first.new_customers), (3, 2))
            with (temp_path / "out1/period_1_cleaned.csv").open() as handle:
                cleaned = list(csv.DictReader(handle))

            self.assertEqual(
                list(cleaned[0]),
                settings["master_columns"],
            )
            second = run_import(fixtures / "period_2.csv", temp_path / "master.csv", settings, temp_path / "out2")
            self.assertEqual((second.appended_rows, second.new_customers), (3, 2))
            repeat = run_import(fixtures / "period_2.csv", temp_path / "master.csv", settings, temp_path / "out3")
            self.assertEqual((repeat.appended_rows, repeat.new_customers), (3, 2))
            repeat_first = run_import(fixtures / "period_1.csv", temp_path / "master.csv", settings, temp_path / "out4")
            self.assertEqual((repeat_first.appended_rows, repeat_first.new_customers), (3, 2))
            with (temp_path / "out2/new_customers.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Customer ID"] for row in rows], ["1001", "1003"])


if __name__ == "__main__":
    unittest.main()
