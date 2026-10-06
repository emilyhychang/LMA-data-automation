import csv
import io
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.pipeline import csv_bytes, load_settings
from servicetitan_reports.storage import PersistentDatasetStore, prepare_seed


def rows(contents):
    return list(csv.DictReader(io.StringIO(contents.decode())))


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(PROJECT / "config/settings.json")

    def record(self, customer_id, created_date, job_type="Repair"):
        return {
            "Customer ID": customer_id,
            "Created Date": created_date,
            "Invoice Business Unit": "HVAC",
            "Job Type": job_type,
            "Total": "100",
        }

    def test_seed_normalizes_and_deduplicates_both_datasets(self):
        master_row = self.record("customer-1", "09/01/2026")
        master = csv_bytes([master_row, master_row], self.settings["master_columns"])
        leads = csv_bytes([
            self.record("customer-1", "09/01/2026"),
            self.record("customer-1", "09/02/2026"),
        ], self.settings["master_columns"])

        clean_master, clean_leads = prepare_seed(master, leads, self.settings)

        self.assertEqual(len(rows(clean_master)), 1)
        self.assertEqual(len(rows(clean_leads)), 1)
        self.assertEqual(rows(clean_leads)[0]["Created Date"], "2026-09-02")

    def test_schema_is_created_outside_the_public_api_schema(self):
        statements = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, parameters=None):
                statements.append(statement)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return Cursor()

        store = PersistentDatasetStore(
            "postgresql://example", self.settings, connect=lambda _: Connection()
        )
        store.ensure_schema()

        self.assertEqual(len(statements), 4)
        self.assertTrue(all("lma_private" in statement for statement in statements))


if __name__ == "__main__":
    unittest.main()
