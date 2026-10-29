import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from servicetitan_reports.pipeline import csv_bytes, load_settings, run_uploaded_import
from servicetitan_reports.storage import DatasetStatus, PersistentDatasetStore, _compressed
from servicetitan_reports.workflow import latest_dashboard, imported_dashboard, weekly_charts


class WorkflowCacheTest(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(PROJECT / 'config/settings.json')
        self.master = csv_bytes([{'Customer ID': '1', 'Created Date': '2026-09-21',
            'Invoice Business Unit': 'HVAC Service', 'Job Type': 'Repair',
            'Total': '100', 'Location Zip': '92109', 'Opportunity': 'True'}], self.settings['master_columns'])
        self.status = DatasetStatus(35, datetime.now(timezone.utc), 'test')

    def test_same_version_reuses_dashboard_but_changed_version_reloads(self):
        store = MagicMock()
        store.downloads.return_value = (self.master, self.master, self.status)
        cache = {}
        first, rows = latest_dashboard(cache, store, self.status, self.settings)
        again, no_rows = latest_dashboard(cache, store, self.status, self.settings)
        self.assertIs(first, again)
        self.assertIsNone(no_rows)
        self.assertNotIn('rows', first)
        store.downloads.assert_called_once()
        next_status = DatasetStatus(36, self.status.updated_at, 'other user')
        store.downloads.return_value = (self.master, self.master, next_status)
        new, _ = latest_dashboard(cache, store, next_status, self.settings)
        self.assertIsNot(first, new)
        self.assertEqual(store.downloads.call_count, 2)

    def test_racing_import_caches_downloaded_version_not_earlier_probe(self):
        store = MagicMock()
        later = DatasetStatus(36, self.status.updated_at, 'other user')
        store.downloads.return_value = (self.master, self.master, later)
        cache = {}
        dashboard, _ = latest_dashboard(cache, store, self.status, self.settings)
        self.assertEqual(dashboard['status'].version, 36)
        latest_dashboard(cache, store, later, self.settings)
        store.downloads.assert_called_once()

    def test_import_result_primes_dashboard_without_download_or_reparse(self):
        result = run_uploaded_import(self.master, csv_bytes([], self.settings['master_columns']), self.settings)
        cache, store = {}, MagicMock()
        imported_dashboard(cache, result, self.status)
        dashboard, rows = latest_dashboard(cache, store, self.status, self.settings)
        self.assertIsNone(rows)
        store.downloads.assert_not_called()
        self.assertEqual(dashboard['zip_summary'][1], result.zip_revenue_rows)
        with patch('servicetitan_reports.workflow.weekly_opportunity_ytd', return_value=([], (2025, 2026))) as calculate:
            weekly_charts(cache, dashboard, self.settings, [])
            weekly_charts(cache, dashboard, self.settings, [])
            self.assertEqual(calculate.call_count, 2)
            weekly_charts(cache, dashboard, self.settings, ['Completed'])
            self.assertEqual(calculate.call_count, 4)

    def test_history_metadata_and_on_demand_files_are_separate_queries(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        store = PersistentDatasetStore('unused', self.settings, connect=lambda _: connection)
        cursor.fetchall.return_value = [(1, 35, self.status.updated_at, 'test', 'report.csv', 'hash', True, True)]
        listing = store.history_listing()
        self.assertFalse(listing[0].outputs_loaded)
        self.assertIsNone(listing[0].cleaned_csv)
        self.assertIn('outputs.cleaned_csv IS NOT NULL', cursor.execute.call_args.args[0])
        cursor.fetchone.return_value = (_compressed(b'cleaned'), _compressed(b'analysis'))
        self.assertEqual(store.history_outputs(1), (b'cleaned', b'analysis'))
        self.assertEqual(cursor.execute.call_args.args[1], (1,))

    def test_map_refresh_reuses_only_matching_committed_import_version(self):
        from dataclasses import replace
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        store = PersistentDatasetStore('unused', self.settings, connect=lambda _: connection)
        result = replace(run_uploaded_import(self.master, csv_bytes([], self.settings['master_columns']), self.settings), dataset_version=35)
        publish = MagicMock(return_value='2026-10-29T12:00:00Z')
        cursor.fetchone.side_effect = [(35, None), (35, 35, 'time', None)]
        store.refresh_maps(publish, prepared=result)
        self.assertIs(publish.call_args.args[0], result)
        cursor.fetchone.side_effect = [(36, _compressed(self.master)), (36, 36, 'time', None)]
        store.refresh_maps(publish, prepared=result)
        self.assertIsNot(publish.call_args.args[0], result)
