import csv
import io
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.pipeline import csv_bytes, load_settings
from servicetitan_reports.storage import (
    ImportHistoryEntry,
    PersistentDatasetStore,
    _compressed,
    prepare_seed,
)


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
        self.assertEqual(rows(clean_leads)[0]["Created Date"], "2026-09-01")

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

        self.assertEqual(len(statements), 8)
        self.assertTrue(all("lma_private" in statement for statement in statements))
        self.assertTrue(any("content_hash" in statement for statement in statements))

    def test_duplicate_preview_keeps_latest_success_and_supports_legacy_rows(self):
        def entry(record_id, minute, content_hash, cleaned):
            return ImportHistoryEntry(
                record_id=record_id,
                dataset_version=record_id,
                imported_at=datetime(2026, 10, 20, 12, minute, tzinfo=timezone.utc),
                imported_by="admin@example.com",
                source_name=f"report-{record_id}.csv",
                content_hash=content_hash,
                cleaned_csv=cleaned,
                analysis_csv=b"analysis",
            )

        history = [
            entry(2, 2, "raw-hash", b"same-new"),
            entry(1, 1, "raw-hash", b"same-old"),
            entry(4, 4, None, b"same-legacy"),
            entry(3, 3, None, b"same-legacy"),
            entry(5, 5, "unique", b"unique"),
        ]
        preview = PersistentDatasetStore._duplicate_history_entries(history)

        self.assertEqual(
            {(item.duplicate.record_id, item.keeper.record_id) for item in preview},
            {(1, 2), (3, 4)},
        )

    def test_import_history_includes_master_snapshot_for_each_upload(self):
        imported_at = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        statements = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, parameters=None):
                statements.append((statement, parameters))

            def fetchall(self):
                return [(
                    7,
                    28,
                    imported_at,
                    "user@example.com",
                    "period.csv",
                    "hash",
                    _compressed(b"cleaned"),
                    _compressed(b"analysis"),
                    _compressed(b"master-at-version-28"),
                )]

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

        history = store.import_history()

        self.assertEqual(history[0].master_csv, b"master-at-version-28")
        self.assertIn("dataset_backups", statements[0][0])
        self.assertIn("dataset_state", statements[0][0])

    def test_confirmed_removal_deletes_only_still_duplicate_log_rows(self):
        imported_at = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        history_rows = [
            (1, 10, imported_at, "user", "old.csv", "same", _compressed(b"clean"), _compressed(b"analysis")),
            (2, 11, imported_at, "user", "new.csv", "same", _compressed(b"clean"), _compressed(b"analysis")),
            (3, 12, imported_at, "user", "unique.csv", "unique", _compressed(b"other"), _compressed(b"analysis")),
        ]
        statements = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, statement, parameters=None):
                statements.append((statement, parameters))

            def fetchall(self):
                statement, parameters = statements[-1]
                if "DELETE FROM lma_private.import_log" in statement:
                    return [(record_id,) for record_id in parameters[0]]
                return history_rows

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
        removed = store.remove_duplicate_import_history([1, 3, 999])

        self.assertEqual(removed, 1)
        delete_statements = [
            (statement, parameters)
            for statement, parameters in statements
            if statement.lstrip().startswith("DELETE")
        ]
        self.assertEqual(len(delete_statements), 1)
        self.assertIn("lma_private.import_log", delete_statements[0][0])
        self.assertEqual(delete_statements[0][1], ([1],))


if __name__ == "__main__":
    unittest.main()
