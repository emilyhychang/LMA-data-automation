import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from servicetitan_reports.pipeline import (
    csv_bytes, load_settings, run_uploaded_import, validate_upload, weekly_opportunity_ytd,
)
from servicetitan_reports.storage import PersistentDatasetStore, _compressed


class ImportSafetyTest(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(PROJECT / "config/settings.json")
        self.row = {"Customer ID": "1", "Created Date": "2026-09-21",
                    "Invoice Business Unit": "HVAC Service", "Job Type": "Repair",
                    "Total": "100", "Opportunity": "False"}

    def csv(self, rows):
        return csv_bytes(rows, self.settings["master_columns"])

    def store(self, master, *, repeat=False):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(1,) if repeat else None,
                                       (35, _compressed(self.csv(master)), _compressed(b"leads"))]
        store = PersistentDatasetStore("unused", self.settings, connect=lambda _: connection)
        return store, cursor

    def test_validation_rejects_bad_dates_future_dates_and_bad_amounts(self):
        for changes in ({"Created Date": "bad"}, {"Created Date": "2099-01-01"},
                        {"Completion Date": "bad"}, {"Total": "NaN"}, {"Total": "oops"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_upload([{**self.row, **changes}], self.settings, today=date(2026, 10, 29))

    def test_biweekly_boundary_and_explicit_historical_override(self):
        validate_upload([self.row, {**self.row, "Created Date": "2026-10-04"}], self.settings)
        longer = [self.row, {**self.row, "Created Date": "2026-10-05"}]
        with self.assertRaisesRegex(ValueError, "longer than 14 days"):
            validate_upload(longer, self.settings)
        validate_upload(longer, self.settings, allow_historical=True)

    def test_invalid_upload_never_opens_database(self):
        connect = MagicMock(side_effect=AssertionError("Must validate before connecting"))
        store = PersistentDatasetStore("unused", self.settings, connect=connect)
        with self.assertRaises(ValueError):
            store.import_report(self.csv([{**self.row, "Created Date": "bad"}]), "bad.csv", "test")
        connect.assert_not_called()

    def test_exact_repeat_is_checked_under_lock_without_writes(self):
        store, cursor = self.store([self.row], repeat=True)
        with self.assertRaisesRegex(ValueError, "already saved"):
            store.import_report(self.csv([self.row]), "renamed.csv", "test")
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("pg_advisory_xact_lock", statements[0])
        self.assertTrue(all(statement.lstrip().startswith("SELECT") for statement in statements))

    def test_equivalent_upload_noop_creates_no_backup_or_version(self):
        store, cursor = self.store([self.row])
        with self.assertRaisesRegex(ValueError, "already match"):
            store.import_report(self.csv([self.row]), "different-format.csv", "test")
        self.assertTrue(all(call.args[0].lstrip().startswith("SELECT") for call in cursor.execute.call_args_list))

    def test_explicit_correction_can_reapply_old_file_and_retains_all_history(self):
        store, cursor = self.store([{**self.row, "Total": "200"}], repeat=True)
        result = store.import_report(self.csv([self.row]), "old.csv", "test", allow_correction=True)
        self.assertEqual(result.updated_rows, 1)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("INSERT INTO lma_private.dataset_backups" in sql for sql in statements))
        self.assertTrue(any("INSERT INTO lma_private.import_outputs" in sql for sql in statements))
        self.assertFalse(any(sql.lstrip().startswith("DELETE") for sql in statements))

    def test_group_growth_counts_exact_matches_updates_and_additions(self):
        before = [self.row, {**self.row, "Total": "200"}]
        after = [self.row, {**self.row, "Total": "250"}, {**self.row, "Total": "300"}]
        with self.assertRaisesRegex(ValueError, "Multiple jobs"):
            run_uploaded_import(self.csv(after), self.csv(before), self.settings)
        result = run_uploaded_import(self.csv(after), self.csv(before), self.settings, allow_group_replacement=True)
        self.assertEqual((result.appended_rows, result.updated_rows, result.skipped_duplicate_rows, result.removed_rows), (1, 1, 1, 0))
        self.assertEqual(result.cleaned_rows, result.appended_rows + result.updated_rows + result.skipped_duplicate_rows)

    def test_group_shrink_requires_confirmation_and_reports_removed_rows(self):
        before = [self.row, {**self.row, "Total": "200"}]
        with self.assertRaisesRegex(ValueError, "Multiple jobs"):
            run_uploaded_import(self.csv([self.row]), self.csv(before), self.settings)
        result = run_uploaded_import(self.csv([self.row]), self.csv(before), self.settings, allow_group_replacement=True)
        self.assertEqual((result.appended_rows, result.updated_rows, result.skipped_duplicate_rows, result.removed_rows), (0, 0, 1, 1))

    def test_identical_new_rows_are_not_silently_deleted_or_added(self):
        incoming = self.csv([self.row, self.row])
        with self.assertRaisesRegex(ValueError, "identical job rows"):
            run_uploaded_import(incoming, self.csv([]), self.settings)
        result = run_uploaded_import(incoming, self.csv([]), self.settings, allow_group_replacement=True)
        self.assertEqual(result.appended_rows, 2)

    def test_iso_week_53_and_new_year_are_not_dropped_or_misassigned(self):
        rows = [{**self.row, "Created Date": day} for day in ("2020-12-31", "2021-01-01", "2021-01-04")]
        chart, years = weekly_opportunity_ytd(rows, "HVAC", self.settings)
        self.assertEqual(years, (2020, 2021))
        self.assertEqual(chart[-1], {"Week": 53, "2020": 2, "2021": None})
        self.assertEqual(chart[0]["2021"], 1)
        self.assertEqual(sum(row["2020"] for row in chart), 2)

    def test_history_has_no_default_limit_and_keeps_missing_output_entries(self):
        store, cursor = self.store([])
        cursor.fetchall.return_value = [(1, 2, None, "user", "old.csv", None, None, None, None, None)]
        history = store.import_history(include_snapshots=False)
        self.assertEqual(cursor.execute.call_args.args[1], (None,))
        self.assertIn("LEFT JOIN", cursor.execute.call_args.args[0])
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].cleaned_csv)
        self.assertEqual(store._duplicate_history_entries(history), [])
