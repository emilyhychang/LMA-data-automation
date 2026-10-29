import json
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
        self.now = datetime.now(timezone.utc)
        status = DatasetStatus(35, self.now, "test@example.com")
        self.history = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(st, "user", SimpleNamespace(
            is_logged_in=True, to_dict=lambda: {"email": "test@example.com", "email_verified": True})))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "connect", side_effect=AssertionError("No real database in UI tests")))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "startup_status", return_value=status))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "downloads", return_value=(master, master, status)))
        self.stack.enter_context(patch.object(PersistentDatasetStore, "import_history", return_value=self.history))
        self.at = AppTest.from_file(str(PROJECT / "app.py"), default_timeout=20)
        self.at.secrets["database"] = {"url": "unused"}
        self.at.secrets["access"] = {"admin_emails": ["test@example.com"]}
        self.at.secrets["looker_studio"] = {"revenue_embed_url": "", "new_leads_embed_url": ""}

    def entry(self, record_id, *, missing=False):
        return ImportHistoryEntry(record_id, record_id, self.now, "test", f"report-{record_id}.csv",
                                  "same", None if missing else b"cleaned", None if missing else b"analysis")

    def test_master_downloads_exist_before_upload_and_charts_have_fixed_domain(self):
        self.at.run()
        self.assertFalse(self.at.exception)
        elements = list(self.at)
        downloads = [(i, e.label) for i, e in enumerate(elements) if e.type == "download_button"]
        self.assertEqual([label for _, label in downloads], ["Download Master", "Download New Leads Master"])
        chart_index = next(i for i, e in enumerate(elements) if e.type == "vega_lite_chart")
        self.assertTrue(all(i < chart_index for i, _ in downloads))
        charts = self.at.get("vega_lite_chart")
        self.assertEqual(len(charts), 2)
        for chart in charts:
            spec = json.loads(chart.proto.spec)
            self.assertEqual(spec["encoding"]["x"]["scale"]["domain"], [0, 54])
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
