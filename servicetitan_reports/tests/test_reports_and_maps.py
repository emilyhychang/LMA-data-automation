import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from servicetitan_reports.pipeline import csv_bytes, load_settings
from servicetitan_reports.storage import PersistentDatasetStore, _compressed
from servicetitan_reports.zip_maps import publish_zip_summaries, _replace_worksheet, _retry, map_error_message


class ReportsAndMapsTest(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(PROJECT / 'config/settings.json')
        self.rows = [
            {'Customer ID': 'old', 'Created Date': '2025-01-01', 'Invoice Business Unit': 'HVAC Service', 'Job Type': 'Repair'},
            {'Customer ID': '1', 'Created Date': '2026-09-21', 'Invoice Business Unit': 'HVAC Service', 'Job Type': 'Repair', 'Total': '100', 'Location Zip': '01234'},
            {'Customer ID': '1', 'Created Date': '2026-09-22', 'Invoice Business Unit': 'Plumbing', 'Job Type': 'Repair', 'Total': '200', 'Location Zip': '92109'},
        ]
        self.master = csv_bytes(self.rows, self.settings['master_columns'])
        self.cleaned = csv_bytes(self.rows[1:], self.settings['master_columns'])
        self.connection = MagicMock()
        self.cursor = self.connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.store = PersistentDatasetStore('unused', self.settings, connect=lambda _: self.connection)

    def saved_row(self, master=True, cleaned=True):
        return (35, 'report.csv', 2, 1, 0, 1, 1,
                _compressed(self.cleaned) if cleaned else None,
                _compressed(b'original analysis'), _compressed(self.master) if master else None)

    def test_restore_preserves_original_metrics_and_regenerates_from_snapshot(self):
        self.cursor.fetchone.return_value = self.saved_row()
        result = self.store.load_saved_report(10)
        self.assertEqual((result.cleaned_rows, result.appended_rows, result.skipped_duplicate_rows), (2, 1, 1))
        self.assertEqual(result.original_analysis_csv, b'original analysis')
        self.assertIn(b'100', result.analysis_csv)
        self.assertIn(b'200', result.analysis_csv)
        self.assertEqual(result.category_comparisons['HVAC'][0]['Records'], 1)
        self.assertEqual(result.category_comparisons['Plumbing'][0]['Records'], 1)
        self.assertTrue(all(call.args[0].lstrip().startswith('SELECT') for call in self.cursor.execute.call_args_list))

    def test_missing_snapshot_never_substitutes_current_master(self):
        self.cursor.fetchone.return_value = self.saved_row(master=False)
        result = self.store.load_saved_report(10)
        self.assertIsNone(result.analysis_csv)
        self.assertIsNone(result.category_comparisons)
        self.assertEqual(result.original_analysis_csv, b'original analysis')

    def test_missing_cleaned_file_still_restores_saved_counts(self):
        self.cursor.fetchone.return_value = self.saved_row(cleaned=False)
        result = self.store.load_saved_report(10)
        self.assertEqual(result.cleaned_rows, 2)
        self.assertIsNone(result.period_start)
        self.assertIsNone(result.analysis_csv)

    def test_refresh_uses_locked_current_version_and_saves_success(self):
        self.cursor.fetchone.side_effect = [(36, _compressed(self.master)), (36, 36, 'timestamp', None)]
        publish = MagicMock(return_value='2026-10-29T12:00:00-07:00')
        status = self.store.refresh_maps(publish)
        self.assertEqual(status['success_version'], 36)
        payload = publish.call_args.args[0]
        self.assertEqual(payload.zip_revenue_rows, [{'Zip': '92109', 'Revenue': 200.0}, {'Zip': '01234', 'Revenue': 100.0}])
        sql = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertIn('pg_advisory_xact_lock', sql[1])
        self.assertIn('SELECT version, CASE WHEN version', sql[3])
        self.assertFalse(any('UPDATE lma_private.dataset_state' in statement for statement in sql))

    def test_partial_or_failed_refresh_is_persisted_without_false_success(self):
        self.cursor.fetchone.side_effect = [(36, _compressed(self.master)), (36, 35, 'old timestamp', 'failed')]
        publish = MagicMock(side_effect=PermissionError('secret credential must not leak'))
        result = self.store.refresh_maps(publish)
        params = self.cursor.execute.call_args.args[1]
        self.assertEqual(params[:3], (36, None, None))
        self.assertIn('Editor', params[3])
        self.assertNotIn('secret', params[3])
        self.assertEqual(result['success_version'], 35)

    def test_atomic_replacement_clears_tail_and_preserves_zip_as_text(self):
        book = MagicMock()
        sheet = SimpleNamespace(id=7, row_count=100, col_count=3)
        _replace_worksheet(book, sheet, ['Zip', 'Revenue'], [{'Zip': '01234', 'Revenue': 20}])
        request = book.batch_update.call_args.args[0]['requests'][0]['updateCells']
        self.assertEqual(request['range']['endRowIndex'], 100)
        self.assertEqual(request['rows'][1]['values'][0]['userEnteredValue'], {'stringValue': '01234'})
        self.assertEqual(request['fields'], 'userEnteredValue')
        self.assertEqual(book.batch_update.call_count, 1)

    def test_atomic_replacement_expands_small_sheet_in_same_batch(self):
        book = MagicMock()
        sheet = SimpleNamespace(id=7, row_count=1, col_count=1)
        _replace_worksheet(book, sheet, ['Zip', 'Revenue'], [{'Zip': '01234', 'Revenue': 20}])
        requests = book.batch_update.call_args.args[0]['requests']
        self.assertEqual(requests[0]['updateSheetProperties']['properties']['gridProperties'], {'rowCount': 2, 'columnCount': 2})
        self.assertIn('updateCells', requests[1])
        self.assertEqual(book.batch_update.call_count, 1)

    def test_both_destinations_are_resolved_before_any_write(self):
        client = MagicMock()
        revenue = MagicMock()
        client.open_by_key.side_effect = [revenue, PermissionError('denied')]
        with self.assertRaises(PermissionError):
            publish_zip_summaries([], [], revenue_spreadsheet_id='a', new_leads_spreadsheet_id='b', generated_at='time', client=client)
        revenue.batch_update.assert_not_called()

    def test_transient_retries_are_bounded_but_permission_failures_are_not_retried(self):
        from requests.exceptions import Timeout
        operation = MagicMock(side_effect=[Timeout(), Timeout(), 'ok'])
        sleep = MagicMock()
        self.assertEqual(_retry(operation, sleep), 'ok')
        self.assertEqual(operation.call_count, 3)
        operation = MagicMock(side_effect=PermissionError('private'))
        with self.assertRaises(PermissionError):
            _retry(operation, sleep)
        self.assertEqual(operation.call_count, 1)
        operation = MagicMock(side_effect=Timeout())
        with self.assertRaises(Timeout):
            _retry(operation, sleep)
        self.assertEqual(operation.call_count, 3)

    def test_error_messages_never_include_raw_credentials(self):
        for error in (ValueError('secret'), PermissionError('secret'), RuntimeError('secret')):
            self.assertNotIn('secret', map_error_message(error))

    def test_missing_optional_tracking_does_not_require_nonadmin_schema_writes(self):
        import psycopg
        self.cursor.execute.side_effect = psycopg.errors.UndefinedTable()
        self.assertEqual(self.store.map_status(), {'setup_required': True})
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_admin_sets_up_only_missing_map_tracking_with_timeouts(self):
        import psycopg
        self.cursor.execute.side_effect = [psycopg.errors.UndefinedTable(), None, None, None, None]
        self.cursor.fetchone.return_value = None
        self.assertIsNone(self.store.map_status(allow_setup=True))
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertIn('lock_timeout', statements[1])
        self.assertIn('statement_timeout', statements[2])
        self.assertIn('CREATE TABLE IF NOT EXISTS lma_private.map_refresh_state', statements[3])
        self.assertNotIn('dataset_state', ''.join(statements))

    def test_refresh_never_publishes_when_tracking_cannot_be_persisted(self):
        import psycopg
        self.cursor.execute.side_effect = [None, None, psycopg.errors.UndefinedTable()]
        publish = MagicMock()
        with self.assertRaises(psycopg.errors.UndefinedTable):
            self.store.refresh_maps(publish)
        publish.assert_not_called()

    def test_second_sheet_failure_is_reported_not_hidden_as_success(self):
        revenue, leads = MagicMock(), MagicMock()
        for book in (revenue, leads):
            book.worksheet.return_value = SimpleNamespace(id=0, row_count=100, col_count=3)
        leads.batch_update.side_effect = PermissionError('denied')
        client = MagicMock()
        client.open_by_key.side_effect = [revenue, leads]
        with self.assertRaises(PermissionError):
            publish_zip_summaries([], [], revenue_spreadsheet_id='a', new_leads_spreadsheet_id='b', generated_at='time', client=client)
        revenue.batch_update.assert_called_once()
        leads.batch_update.assert_called_once()
