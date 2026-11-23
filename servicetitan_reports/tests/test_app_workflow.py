import json
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

import streamlit as st
from streamlit.testing.v1 import AppTest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from servicetitan_reports.pipeline import csv_bytes, load_settings
from servicetitan_reports.storage import (
    PersistentDatasetStore, DatasetStatus, ImportHistoryEntry, DuplicateImportHistoryEntry,
)


class AppWorkflowTest(unittest.TestCase):
    def setUp(self):
        settings = load_settings(PROJECT / "config/settings.json")
        master = csv_bytes([{"Customer ID": "test", "Created Date": "2026-09-21",
                             "Invoice Business Unit": "HVAC Service", "Job Type": "Repair",
                             "Opportunity": "True", "Total": "100"}], settings["master_columns"])
        self.master = master
        self.settings = settings
        self.now = datetime.now(timezone.utc)
        status = DatasetStatus(35, self.now, "test@example.com")
        self.history = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(st, "user", SimpleNamespace(
            is_logged_in=True, to_dict=lambda: {"email": "test@example.com", "email_verified": True})))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "connect", side_effect=AssertionError("No real database in UI tests")))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "startup_status", return_value=status))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "status", return_value=status))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "downloads", return_value=(master, master, status)))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "history_listing", return_value=self.history))
        self.stack.enter_context(patch.object(PersistentDatasetStore, 'load_saved_report',
                                             side_effect=lambda record_id, **kwargs: self.restored(record_id, 100)))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "map_status", return_value=None))
        self.at = AppTest.from_file(str(PROJECT / "app.py"), default_timeout=20)
        self.at.secrets["database"] = {"url": "unused"}
        self.at.secrets["access"] = {"admin_emails": ["test@example.com"]}
        self.at.secrets["looker_studio"] = {"revenue_embed_url": "", "new_leads_embed_url": ""}

    def entry(self, record_id, *, missing=False):
        return ImportHistoryEntry(record_id, record_id, self.now, "test", f"report-{record_id}.csv",
                                  "same", None if missing else b"cleaned", None if missing else b"analysis")

    def test_normal_rerun_does_not_download_master_again(self):
        status = DatasetStatus(35, self.now, 'test')
        downloads = self.stack.enter_context(patch.object(PersistentDatasetStore, 'downloads', return_value=(self.master, self.master, status)))
        probe = self.stack.enter_context(patch.object(PersistentDatasetStore, 'status', return_value=status))
        self.at.run()
        self.at.run()
        self.assertFalse(self.at.exception)
        downloads.assert_called_once()
        newer = DatasetStatus(36, self.now, 'another user')
        probe.return_value = newer
        downloads.return_value = (self.master, self.master, newer)
        self.at.run()
        self.assertEqual(downloads.call_count, 2)

    def test_account_change_discards_cached_datasets(self):
        status = DatasetStatus(35, self.now, 'test')
        downloads = self.stack.enter_context(patch.object(PersistentDatasetStore, 'downloads', return_value=(self.master, self.master, status)))
        self.at.run()
        self.at.secrets['access'] = {'admin_emails': ['other@example.com']}
        self.stack.enter_context(patch.object(st, 'user', SimpleNamespace(is_logged_in=True,
            to_dict=lambda: {'email': 'other@example.com', 'email_verified': True})))
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertEqual(downloads.call_count, 2)

    def test_history_csvs_load_only_when_requested(self):
        self.history.append(ImportHistoryEntry(1, 35, self.now, 'test', 'report.csv', 'hash', None, None,
                                              outputs_loaded=False, has_cleaned=True, has_analysis=True))
        load = self.stack.enter_context(patch.object(PersistentDatasetStore, 'history_outputs', return_value=(b'cleaned', b'analysis')))
        self.at.run()
        load.assert_not_called()
        self.at.button(key='load-outputs-1').click().run()
        self.assertFalse(self.at.exception)
        load.assert_called_once_with(1)
        self.at.run()
        load.assert_called_once()

    def restored(self, version, amount):
        from datetime import date
        snapshot = self.master.replace(b',100,', f',{amount},'.encode())
        return SimpleNamespace(dataset_version=version, source_name=f'report-{version}.csv',
                               cleaned_rows=1, appended_rows=1, updated_rows=0, skipped_duplicate_rows=0,
                               new_customers=1, period_start=date(2026, 9, 21), period_end=date(2026, 9, 21),
                               cleaned_csv=snapshot, master_csv=snapshot, analysis_csv=b'corrected',
                               original_analysis_csv=b'original', category_comparisons=None,
                               new_leads_csv=snapshot, new_lead_rows=None)

    def test_latest_report_restores_on_fresh_session_and_old_selection_uses_snapshot(self):
        self.history.extend([self.entry(2), self.entry(1)])
        self.stack.enter_context(patch.object(PersistentDatasetStore, 'load_saved_report',
                                             side_effect=lambda record_id, **kwargs: self.restored(35 if record_id == 2 else 34, 200 if record_id == 2 else 50)))
        for fresh in (self.at, AppTest.from_file(str(PROJECT / 'app.py'), default_timeout=20)):
            fresh.secrets['database'] = {'url': 'unused'}
            fresh.secrets['access'] = {'admin_emails': ['test@example.com']}
            fresh.secrets['looker_studio'] = {'revenue_embed_url': '', 'new_leads_embed_url': ''}
            fresh.run()
            self.assertFalse(fresh.exception)
            self.assertEqual(len(fresh.metric), 5)
            self.assertEqual(fresh.dataframe[0].value.iloc[0]['Revenue'], 200)
        self.at.selectbox(key='saved_report_id').select(1).run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.at.dataframe[0].value.iloc[0]['Revenue'], 50)
        self.assertTrue(any(e.label == 'Download original saved analysis' for e in self.at.get('download_button')))

    def test_saved_report_reuses_precomputed_period_comparisons(self):
        self.history.append(self.entry(1))
        result = self.restored(35, 200)
        comparison = {
            "Period": "Current Period",
            "Start": "2026-09-21",
            "End": "2026-09-21",
            "Records": 1,
            "Unique Customers": 1,
            "New Customers": 1,
            "New Customer Gain/Loss": "—",
            "Revenue": "999",
        }
        result.category_comparisons = {
            "HVAC": [comparison],
            "Plumbing": [comparison],
        }
        self.stack.enter_context(patch.object(
            PersistentDatasetStore,
            "load_saved_report",
            return_value=result,
        ))

        self.at.run()

        self.assertFalse(self.at.exception)
        self.assertEqual(
            self.at.dataframe[0].value.iloc[0]["Revenue"],
            999,
        )

    def test_current_upload_stays_selected_instead_of_loading_latest_saved_report(self):
        self.history.append(self.entry(1))
        uploaded = self.restored(35, 777)
        uploaded.source_name = 'current-upload.csv'
        self.at.session_state['current_upload_result'] = uploaded
        self.at.session_state['current_upload_stem'] = 'current-upload'
        self.at.session_state['show_current_upload'] = True
        self.at.session_state['saved_report_id'] = '__current_upload__'

        self.at.run()

        self.assertFalse(self.at.exception)
        self.assertEqual(
            self.at.selectbox(key='saved_report_id').value,
            '__current_upload__',
        )
        self.assertTrue(any(
            'Saved report: current-upload' in caption.value
            for caption in self.at.caption
        ))
        self.assertEqual(self.at.dataframe[0].value.iloc[0]['Revenue'], 777)
        self.assertTrue(any(
            button.label == 'Download analysis'
            for button in self.at.get('download_button')
        ))

    def test_top_twenty_rankings_show_only_zip_lists_and_refresh_does_not_import(self):
        rows = [{'Customer ID': str(i), 'Created Date': '2026-09-21',
                 'Invoice Business Unit': 'HVAC Service', 'Job Type': 'Repair',
                 'Location Zip': f'{i:05d}', 'Total': str(i), 'Opportunity': 'True'} for i in range(1, 23)]
        master = csv_bytes(rows, self.settings['master_columns'])
        status = DatasetStatus(35, self.now, 'test')
        self.stack.enter_context(patch.object(PersistentDatasetStore, 'downloads', return_value=(master, master, status)))
        refresh = self.stack.enter_context(patch.object(PersistentDatasetStore, 'refresh_maps', return_value={}))
        save = self.stack.enter_context(patch.object(PersistentDatasetStore, 'import_report'))
        self.at.run()
        self.assertFalse(self.at.exception)
        zip_lists = [element.value.splitlines() for element in self.at.text]
        self.assertEqual(len(zip_lists), 2)
        self.assertTrue(all(len(zip_list) == 20 for zip_list in zip_lists))
        self.assertEqual(zip_lists[0][0], '00001')
        self.assertEqual(zip_lists[1][0], '00022')
        next(button for button in self.at.button if button.label == 'Refresh map sources').click().run()
        refresh.assert_called_once()
        save.assert_not_called()

    def test_selecting_saved_report_automatically_refreshes_its_map_sources(self):
        self.history.append(self.entry(1))
        result = self.restored(35, 200)
        result.zip_year = 2026
        result.zip_revenue_rows = [{"Zip": "92109", "Revenue": 200.0}]
        result.zip_new_lead_rows = [{"Zip": "92037", "New leads": 1}]
        self.stack.enter_context(patch.object(
            PersistentDatasetStore, "load_saved_report", return_value=result
        ))
        refresh = self.stack.enter_context(patch.object(
            PersistentDatasetStore,
            "refresh_maps",
            return_value={
                "attempt_version": 35,
                "success_version": 35,
                "refreshed_at": "2026-11-05T10:00:00-08:00",
                "error": None,
            },
        ))

        self.at.run()

        self.assertFalse(self.at.exception)
        refresh.assert_called_once_with(
            ANY,
            prepared=result,
            allow_historical=True,
        )
        self.assertTrue(any(
            "Both map sources refreshed for the selected report"
            in message.value
            for message in self.at.success
        ))

    def test_import_publishes_map_sources_once(self):
        result = self.restored(36, 200)
        result.zip_year = 2026
        result.zip_revenue_rows = [{"Zip": "92109", "Revenue": 200.0}]
        result.zip_new_lead_rows = [{"Zip": "92037", "New leads": 1}]
        result.new_lead_rows = []
        imported_status = DatasetStatus(36, self.now, "test@example.com")
        self.stack.enter_context(patch.object(
            PersistentDatasetStore, "status", return_value=imported_status
        ))
        save = self.stack.enter_context(patch.object(
            PersistentDatasetStore, "import_report", return_value=result
        ))
        refresh = self.stack.enter_context(patch.object(
            PersistentDatasetStore,
            "refresh_maps",
            return_value={
                "attempt_version": 36,
                "success_version": 36,
                "refreshed_at": "2026-11-05T10:00:00-08:00",
                "error": None,
            },
        ))

        self.at.run()
        self.at.file_uploader(key="raw_upload").set_value(
            ("new.csv", self.master, "text/csv")
        ).run()
        next(
            button
            for button in self.at.button
            if button.label == "Clean and save report"
        ).click().run()

        self.assertFalse(self.at.exception)
        save.assert_called_once()
        refresh.assert_called_once_with(
            ANY,
            prepared=result,
            allow_historical=True,
        )

    def test_master_downloads_exist_before_upload_and_charts_have_fixed_domain(self):
        self.at.run()
        self.assertFalse(self.at.exception)
        elements = list(self.at)
        downloads = [(i, e.label) for i, e in enumerate(elements) if e.type == "download_button"]
        self.assertEqual(
            [label for _, label in downloads],
            [
                "Download Master",
                "Download New Leads Master",
            ],
        )
        chart_index = next(i for i, e in enumerate(elements) if e.type == "vega_lite_chart")
        self.assertTrue(all(i < chart_index for i, _ in downloads))
        charts = self.at.get("vega_lite_chart")
        self.assertEqual(len(charts), 2)
        for chart in charts:
            spec = json.loads(chart.proto.spec)
            self.assertEqual(spec["encoding"]["x"]["scale"]["domain"], [0.5, 52.5])
            self.assertEqual(spec["encoding"]["x"]["axis"]["values"], list(range(1, 53)))
            self.assertNotIn("params", spec)
            self.assertNotIn("selection", spec)

    def test_history_over_25_entries_and_missing_outputs_render(self):
        self.history.extend(self.entry(i, missing=(i == 1)) for i in range(1, 32))
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertFalse(self.at.warning)
        self.assertEqual(sum(button.label == "×" for button in self.at.button), 31)
        self.assertTrue(any("Cleaned export is no longer available" in caption.value for caption in self.at.caption))

    def test_non_admin_cannot_use_either_history_deletion_control(self):
        self.history.append(self.entry(1))
        self.at.secrets["access"] = {"allowed_emails": ["test@example.com"], "admin_emails": []}
        self.at.run()
        self.assertFalse(self.at.exception)
        self.assertFalse(any(button.label in {"×", "Remove Duplicates", "Confirm Removal"} for button in self.at.button))

    def test_duplicate_confirmation_hides_only_ids_actually_removed(self):
        self.history.extend(self.entry(i) for i in (1, 2, 3))
        preview = [DuplicateImportHistoryEntry(self.history[i], self.history[2]) for i in (0, 1)]
        self.stack.enter_context(patch.object(PersistentDatasetStore, "duplicate_import_history", return_value=preview))
        remove = self.stack.enter_context(patch.object(PersistentDatasetStore, "remove_duplicate_import_history", return_value={1}))
        self.at.run()
        self.at.button(key="preview-history-duplicates").click().run()
        self.at.checkbox(key="confirm_history_duplicate_removal").check().run()
        self.at.button(key="confirm-history-duplicate-removal").click().run()
        self.assertFalse(self.at.exception)
        remove.assert_called_once()
        self.assertEqual([entry.record_id for entry in self.history], [2, 3])
        self.assertTrue(any("Removed 1 duplicate" in message.value for message in self.at.success))
