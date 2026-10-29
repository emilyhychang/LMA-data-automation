import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import psycopg

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from servicetitan_reports.startup import open_dataset_store, StorageStartupError
from servicetitan_reports.storage import PersistentDatasetStore, DatasetStatus


class StartupTest(unittest.TestCase):
    def test_ready_database_uses_only_bounded_reads_even_for_admin(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        row = (35, datetime(2026, 10, 29, tzinfo=timezone.utc), "admin")
        cursor.fetchone.return_value = row
        store = PersistentDatasetStore("unused", {}, connect=lambda _: connection)
        store.ensure_schema = Mock()
        self.assertEqual(open_dataset_store(store, allow_schema_setup=True), DatasetStatus(*row))
        store.ensure_schema.assert_not_called()
        queries = [call.args[0].strip() for call in cursor.execute.call_args_list]
        self.assertTrue(all(q.startswith(("SELECT", "SET LOCAL")) for q in queries))
        self.assertEqual(sum("LIMIT 0" in q for q in queries), 4)
        self.assertTrue(any("statement_timeout" in q for q in queries))
        self.assertTrue(any("lock_timeout" in q for q in queries))

    def test_temporary_connection_failure_recovers_without_schema_writes(self):
        store = Mock()
        expected = object()
        store.startup_status.side_effect = [psycopg.OperationalError("connection lost"), expected]
        sleep = Mock()
        self.assertIs(open_dataset_store(store, sleep=sleep), expected)
        self.assertEqual(store.startup_status.call_count, 2)
        sleep.assert_called_once_with(0.5)
        store.ensure_schema.assert_not_called()

    def test_busy_database_retries_are_bounded_and_do_not_leak_credentials(self):
        store = Mock()
        secret = "postgresql://private-user:private-password@example/db"
        store.startup_status.side_effect = psycopg.errors.LockNotAvailable(secret)
        sleep = Mock()
        with self.assertLogs("servicetitan_reports.startup", level="WARNING") as logs:
            with self.assertRaises(StorageStartupError) as raised:
                open_dataset_store(store, sleep=sleep)
        self.assertEqual(store.startup_status.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("55P03", str(raised.exception))
        self.assertIn("temporarily", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(secret, " ".join(logs.output))
        store.ensure_schema.assert_not_called()

    def test_credentials_and_permissions_fail_without_retries(self):
        for error, message in [
            (psycopg.errors.InvalidPassword("secret"), "credentials"),
            (psycopg.errors.InsufficientPrivilege("secret"), "permission"),
        ]:
            with self.subTest(error=type(error).__name__):
                store, sleep = Mock(), Mock()
                store.startup_status.side_effect = error
                with self.assertRaisesRegex(StorageStartupError, message):
                    open_dataset_store(store, allow_schema_setup=True, sleep=sleep)
                store.startup_status.assert_called_once()
                store.ensure_schema.assert_not_called()
                sleep.assert_not_called()

    def test_admin_can_initialize_or_upgrade_only_a_missing_schema(self):
        for error in [psycopg.errors.UndefinedTable(), psycopg.errors.UndefinedColumn()]:
            with self.subTest(error=type(error).__name__):
                store = Mock()
                store.startup_status.side_effect = [error, None]
                self.assertIsNone(open_dataset_store(store, allow_schema_setup=True))
                store.ensure_schema.assert_called_once()
                self.assertEqual(store.startup_status.call_count, 2)

    def test_non_admin_cannot_trigger_migrations(self):
        store = Mock()
        store.startup_status.side_effect = psycopg.errors.UndefinedTable()
        with self.assertRaisesRegex(StorageStartupError, "schema needs"):
            open_dataset_store(store)
        store.ensure_schema.assert_not_called()

    def test_empty_ready_database_does_not_run_migrations(self):
        store = Mock()
        store.startup_status.return_value = None
        self.assertIsNone(open_dataset_store(store, allow_schema_setup=True))
        store.ensure_schema.assert_not_called()

    def test_unexpected_errors_are_not_retried(self):
        store, sleep = Mock(), Mock()
        store.startup_status.side_effect = ValueError("private detail")
        with self.assertRaisesRegex(StorageStartupError, "ValueError"):
            open_dataset_store(store, sleep=sleep)
        sleep.assert_not_called()
        store.ensure_schema.assert_not_called()
