"""Private, upload-only ServiceTitan cleaner.

The app processes CSVs in the current browser session and exposes results as
downloads. It does not save uploaded client files to disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from servicetitan_reports.pipeline import load_settings, run_uploaded_import  # noqa: E402

SETTINGS = load_settings(ROOT / "config" / "settings.json")

st.set_page_config(page_title="ServiceTitan File Cleaner", layout="centered")
st.title("ServiceTitan File Cleaner")
st.write(
    "Upload a ServiceTitan export and the two datasets you want to update. "
    "Download the cleaned and updated files when processing finishes."
)

st.info(
    "Privacy: this app processes files only in the current session. "
    "It does not save uploaded customer files or updated datasets on the server."
)

with st.expander("Required files"):
    st.write(
        "**New ServiceTitan export** is the CSV to clean. "
        "**Master dataset** is the complete historical dataset. "
        "**New-leads-only dataset** contains first-time customers found previously."
    )
    st.write(
        "Rows must include Customer ID, Created Date, Invoice Business Unit, "
        "and Job Type. Blank rows, section labels, and incomplete rows are removed."
    )

raw_upload = st.file_uploader("1. New ServiceTitan export", type=["csv"], key="raw_upload")
master_upload = st.file_uploader("2. Current master dataset", type=["csv"], key="master_upload")
leads_upload = st.file_uploader("3. Current new-leads-only dataset", type=["csv"], key="leads_upload")

if not all([raw_upload, master_upload, leads_upload]):
    st.info("Upload all three CSV files to continue.")
    st.stop()

if st.button("Clean file and update datasets", type="primary"):
    try:
        with st.spinner("Cleaning records and preparing updated downloads..."):
            result = run_uploaded_import(
                source_contents=raw_upload.getvalue(),
                source_name=raw_upload.name,
                master_contents=master_upload.getvalue(),
                new_leads_contents=leads_upload.getvalue(),
                settings=SETTINGS,
            )

        st.session_state["import_result"] = result
        st.session_state["raw_stem"] = Path(raw_upload.name).stem
        st.session_state["master_name"] = Path(master_upload.name).name
        st.session_state["leads_name"] = Path(leads_upload.name).name
    except Exception as error:
        st.error(f"No files were changed: {error}")

result = st.session_state.get("import_result")

if result:
    st.success("Finished. Download the files below to keep the changes.")
    first, second, third, fourth = st.columns(4)
    first.metric("Valid records", f"{result.cleaned_rows:,}")
    second.metric("Added to master", f"{result.appended_rows:,}")
    third.metric("Duplicates skipped", f"{result.skipped_duplicate_rows:,}")
    fourth.metric("New customers", f"{result.new_customers:,}")

    raw_stem = st.session_state["raw_stem"]
    master_name = st.session_state["master_name"]
    leads_name = st.session_state["leads_name"]

    st.subheader("Downloads")
    st.download_button("Download cleaned export", data=result.cleaned_csv, file_name=f"{raw_stem}_cleaned.csv", mime="text/csv")
    st.download_button("Download updated master dataset", data=result.master_csv, file_name=f"updated_{master_name}", mime="text/csv")
    st.download_button("Download updated new-leads-only dataset", data=result.new_leads_csv, file_name=f"updated_{leads_name}", mime="text/csv")
    st.download_button("Download period comparison", data=result.comparison_csv, file_name=f"{raw_stem}_period_comparison.csv", mime="text/csv")

    st.caption(
        f"Reporting period: {result.period_start.isoformat()} through "
        f"{result.period_end.isoformat()}. Download the updated master and "
        "new-leads-only files before leaving the page."
    )
