"""Restricted ServiceTitan cleaner backed by persistent shared datasets."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from servicetitan_reports.pipeline import (  # noqa: E402
    PERIOD_COMPARISON_COLUMNS,
    category_period_comparisons,
    first_seen_customer_rows,
    load_settings,
    read_and_clean_csv_bytes,
    weekly_opportunity_ytd,
)
from servicetitan_reports import storage as storage_module  # noqa: E402
from servicetitan_reports.zip_maps import (  # noqa: E402
    publish_zip_summaries,
    zip_code_summaries,
)

# Streamlit can rerun this file while retaining an older imported module.
history_fields = storage_module.ImportHistoryEntry.__dataclass_fields__
if "master_csv" not in history_fields or "new_leads_csv" not in history_fields:
    storage_module = importlib.reload(storage_module)
PersistentDatasetStore = storage_module.PersistentDatasetStore


SETTINGS = load_settings(ROOT / "config" / "settings.json")

REVENUE_SPREADSHEET_ID = "19IdcStsC024mIoOEoel-I-tZO-dBSby3jwHcUl80Pgo"
NEW_LEADS_SPREADSHEET_ID = "1QAtvpIVhxOPc72Px3A1j6l9exDA4dPgVrsQUWcvFa2A"
REVENUE_HEATMAP_URL = (
    "https://datastudio.google.com/embed/reporting/"
    "e537e267-faf6-4518-aa6c-a28e1e35c6e6/page/S0t7F"
)
NEW_LEADS_HEATMAP_URL = (
    "https://datastudio.google.com/embed/reporting/"
    "939c1d6e-32bf-4f88-a26d-d32bedad3dcc/page/ixt7F"
)


def secret_list(section: str, key: str) -> set[str]:
    return {
        str(value).strip().lower()
        for value in st.secrets.get(section, {}).get(key, [])
        if str(value).strip()
    }


def refresh_zip_map_sources(result) -> str | None:
    """Publish ZIP summaries when service-account credentials are configured."""

    sheet_settings = dict(st.secrets.get("google_sheets", {}))
    account_json = str(sheet_settings.get("service_account_json", "")).strip()
    account = (
        json.loads(account_json)
        if account_json
        else dict(st.secrets.get("google_service_account", {}))
    )
    credentials_file = str(sheet_settings.get("credentials_file", "")).strip()
    if not account and not credentials_file:
        return None

    generated_at = datetime.now(
        ZoneInfo("America/Los_Angeles")
    ).isoformat(timespec="seconds")
    publish_zip_summaries(
        result.zip_revenue_rows,
        result.zip_new_lead_rows,
        revenue_spreadsheet_id=str(
            sheet_settings.get("revenue_spreadsheet_id", REVENUE_SPREADSHEET_ID)
        ).strip(),
        new_leads_spreadsheet_id=str(
            sheet_settings.get("new_leads_spreadsheet_id", NEW_LEADS_SPREADSHEET_ID)
        ).strip(),
        generated_at=generated_at,
        worksheet_name=str(sheet_settings.get("worksheet_name", "Sheet1")),
        service_account_info=account or None,
        credentials_file=credentials_file or None,
    )
    return generated_at


def render_zip_ranking(rows) -> None:
    """Render a compact top-20 ZIP list beside a heatmap."""

    zip_codes = [str(row.get("Zip", "")) for row in rows[:20]]
    if not zip_codes:
        st.info("No ZIP data is available.")
        return
    st.markdown(
        '<div class="zip-ranking">'
        + "".join(f"<div>{zip_code}</div>" for zip_code in zip_codes)
        + "</div>",
        unsafe_allow_html=True,
    )


def require_approved_user() -> tuple[str, bool]:
    """Require OIDC login and return the normalized email and admin status."""

    if not getattr(st.user, "is_logged_in", False):
        st.title("ServiceTitan File Cleaner")
        st.write("Sign in with an approved account to continue.")
        st.button("Sign in", on_click=st.login, type="primary")
        st.stop()

    user = st.user.to_dict()
    email = str(user.get("email") or user.get("preferred_username") or "").lower()
    allowed = secret_list("access", "allowed_emails")
    admins = secret_list("access", "admin_emails")
    allowed.update(admins)
    if user.get("email_verified") is False or not email or email not in allowed:
        st.error("This account is not approved to use this app.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
    return email, email in admins


st.set_page_config(page_title="ServiceTitan File Cleaner", layout="wide")
email, is_admin = require_approved_user()

database_url = str(st.secrets.get("database", {}).get("url", "")).strip()
if not database_url:
    st.error("Persistent storage is not configured. An administrator must add the database URL.")
    st.stop()

try:
    store = PersistentDatasetStore(database_url, SETTINGS)
    store.ensure_schema()
    dataset_status = store.status()
except Exception:
    st.error("Persistent storage could not be opened. Check the database configuration.")
    st.stop()

st.title("ServiceTitan File Cleaner")
top_left, top_right = st.columns([4, 1])
top_left.caption(f"Signed in as {email}")
top_right.button("Sign out", on_click=st.logout)

if dataset_status is None:
    st.warning("The shared datasets need to be initialized once before imports can begin.")
    if not is_admin:
        st.info("Ask an administrator to load the initial master datasets.")
        st.stop()

    st.subheader("One-time setup")
    st.write("Upload the current complete master and new-leads-only master.")
    initial_master = st.file_uploader(
        "Current master dataset", type=["csv"], key="initial_master"
    )
    initial_leads = st.file_uploader(
        "Current new-leads-only dataset", type=["csv"], key="initial_leads"
    )
    if st.button(
        "Initialize shared datasets",
        type="primary",
        disabled=not (initial_master and initial_leads),
    ):
        try:
            with st.spinner("Validating and saving the shared datasets…"):
                store.seed(initial_master.getvalue(), initial_leads.getvalue(), email)
            st.success("Shared datasets initialized.")
            st.rerun()
        except ValueError as error:
            st.error(f"Nothing was saved: {error}")
        except Exception:
            st.error("Nothing was saved. The database update failed.")
    st.stop()

st.write(
    "Upload only the new ServiceTitan export. The saved master and new-leads "
    "datasets will be updated together automatically."
)
st.caption(
    f"Dataset version {dataset_status.version:,} · Last updated "
    f"{dataset_status.updated_at:%Y-%m-%d %H:%M %Z} by {dataset_status.updated_by}"
)

raw_upload = st.file_uploader("New ServiceTitan export", type=["csv"], key="raw_upload")
if st.button("Clean and save report", type="primary", disabled=raw_upload is None):
    try:
        with st.spinner("Cleaning the report and updating both shared datasets…"):
            result = store.import_report(raw_upload.getvalue(), raw_upload.name, email)
        st.session_state["import_result"] = result
        st.session_state["raw_stem"] = Path(raw_upload.name).stem
        try:
            map_refresh_time = refresh_zip_map_sources(result)
            st.session_state["zip_map_refresh"] = (
                "refreshed" if map_refresh_time else "not_configured"
            )
            if map_refresh_time:
                st.session_state["zip_map_refresh_at"] = map_refresh_time
        except Exception:
            st.session_state["zip_map_refresh"] = "failed"
        st.success(
            "Both shared datasets were saved. A backup of the prior version was retained."
        )
        if st.session_state["zip_map_refresh"] == "refreshed":
            refresh_label = datetime.fromisoformat(
                st.session_state["zip_map_refresh_at"]
            ).strftime("%B %-d, %Y at %-I:%M:%S %p %Z")
            st.success(
                f"The revenue and new-lead map source data was generated {refresh_label}."
            )
        elif st.session_state["zip_map_refresh"] == "not_configured":
            st.warning(
                "The report was saved, but the map source sheets were not refreshed "
                "because the deployed app is missing its Google service-account credential."
            )
        else:
            st.warning(
                "The datasets were saved, but the map data could not be refreshed. "
                "Check the Google Sheets sharing and service-account settings."
            )
    except ValueError as error:
        st.error(f"Nothing was saved: {error}")
    except Exception:
        st.error("Nothing was saved. The import could not complete; retry or contact an administrator.")

result = st.session_state.get("import_result")
if result:
    first, second, third, fourth, fifth = st.columns(5)
    first.metric("Valid records", f"{result.cleaned_rows:,}")
    second.metric("Added to master", f"{result.appended_rows:,}")
    third.metric("Updated records", f"{getattr(result, 'updated_rows', 0):,}")
    fourth.metric("Unchanged records", f"{result.skipped_duplicate_rows:,}")
    fifth.metric("New customers", f"{result.new_customers:,}")

    raw_stem = st.session_state["raw_stem"]
    st.subheader("Import downloads")
    st.download_button(
        "Download cleaned export", result.cleaned_csv,
        f"{raw_stem}_cleaned.csv", "text/csv"
    )
    st.download_button(
        "Download analysis", result.analysis_csv,
        f"{raw_stem}_analysis.csv", "text/csv"
    )

master = leads = current_status = None
master_rows = []
try:
    master, leads, current_status = store.downloads()
    master_rows = read_and_clean_csv_bytes(
        master, SETTINGS, "Master dataset", allow_empty=True
    )
except Exception:
    st.warning("The latest shared datasets could not be prepared.")

if result and master_rows:
    st.subheader("Period comparisons")
    st.caption("HVAC and Plumbing results for the imported reporting period.")
    fresh_comparisons = category_period_comparisons(
        master_rows, result.period_start, result.period_end, SETTINGS
    )
    for category in ("HVAC", "Plumbing"):
        st.write(f"**{category}**")
        display_rows = [
            {
                column: (
                    float(row[column] or 0)
                    if column == "Revenue"
                    else row[column]
                )
                for column in PERIOD_COMPARISON_COLUMNS
            }
            for row in fresh_comparisons[category]
        ]
        st.dataframe(
            display_rows,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Revenue": st.column_config.NumberColumn(
                    "Revenue", format="$%.2f"
                ),
            },
        )

derived_lead_rows = first_seen_customer_rows(master_rows, SETTINGS) if master_rows else []

if master_rows:
    zip_year, zip_revenue_rows, zip_new_lead_rows = zip_code_summaries(
        master_rows, derived_lead_rows, SETTINGS
    )
    st.subheader(f"Top ZIP codes and heatmaps — {zip_year}")
    st.caption(
        "Revenue and first-time customers are ranked by ZIP code for the latest year. "
        "The heatmaps refresh from the two connected Google Sheets."
    )
    map_refresh_time = st.session_state.get("zip_map_refresh_at")
    if map_refresh_time:
        refresh_label = datetime.fromisoformat(map_refresh_time).strftime(
            "%B %-d, %Y at %-I:%M:%S %p %Z"
        )
        st.info(
            f"Latest map source: {refresh_label}. The maps are current when their "
            "displayed ‘Map data generated at’ time matches this time."
        )
    st.markdown(
        """
        <style>
        .zip-ranking {
            padding: 0.15rem 0 0 0.2rem;
        }
        .zip-ranking div {
            font-size: 0.92rem;
            line-height: 1.55;
            white-space: nowrap;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    leads_section, revenue_section = st.columns(2, gap="large")
    looker_settings = dict(st.secrets.get("looker_studio", {}))
    revenue_embed_url = str(
        looker_settings.get("revenue_embed_url", REVENUE_HEATMAP_URL)
    ).strip()
    leads_embed_url = str(
        looker_settings.get("new_leads_embed_url", NEW_LEADS_HEATMAP_URL)
    ).strip()

    with leads_section:
        st.write("**New leads**")
        leads_list, leads_map = st.columns([1, 4], gap="small")
        with leads_list:
            render_zip_ranking(zip_new_lead_rows)
        with leads_map:
            if leads_embed_url:
                components.iframe(leads_embed_url, height=540, scrolling=False)
            else:
                st.info("The new-lead heatmap embed URL is not configured yet.")

    with revenue_section:
        st.write("**Revenue**")
        revenue_list, revenue_map = st.columns([1, 4], gap="small")
        with revenue_list:
            render_zip_ranking(zip_revenue_rows)
        with revenue_map:
            if revenue_embed_url:
                components.iframe(revenue_embed_url, height=540, scrolling=False)
            else:
                st.info("The revenue heatmap embed URL is not configured yet.")

    st.subheader("Weekly opportunities — YTD")
    st.caption(
        "Count of nonblank Opportunity values for each customer's first master "
        "record, matching the new-leads Excel pivot without historical gaps."
    )
    available_statuses = sorted({
        row["Job Status"] for row in derived_lead_rows if row.get("Job Status")
    }, key=str.casefold)
    selected_statuses = st.multiselect(
        "Job Status",
        available_statuses,
        default=available_statuses,
        help="Select the same statuses used by the Job Status filter in Excel.",
    )
    for category in ("Plumbing", "HVAC"):
        chart_rows, years = weekly_opportunity_ytd(
            derived_lead_rows, category, SETTINGS, set(selected_statuses)
        )
        st.write(f"**{category}**")
        if years and chart_rows:
            st.line_chart(
                chart_rows,
                x="Week",
                y=[str(years[0]), str(years[1])],
                x_label="Week",
                y_label="Opportunities",
            )
        else:
            st.info(f"No {category} opportunity history is available yet.")

try:
    history = store.import_history()
    if history:
        st.subheader("Generated file history")
        st.caption("The 25 most recent sets of generated files are retained.")
        if st.button("Remove Duplicates", key="preview-history-duplicates"):
            st.session_state["history_duplicate_preview"] = (
                store.duplicate_import_history()
            )
            st.session_state["confirm_history_duplicate_removal"] = False

        duplicate_preview = st.session_state.get("history_duplicate_preview")
        if duplicate_preview is not None:
            if not duplicate_preview:
                st.info("No duplicate upload-history records were found.")
            else:
                st.write(
                    f"{len(duplicate_preview)} duplicate history record(s) "
                    "will be removed:"
                )
                st.dataframe(
                    [{
                        "Remove upload": item.duplicate.imported_at.strftime(
                            "%Y-%m-%d %H:%M %Z"
                        ),
                        "File": item.duplicate.source_name,
                        "Dataset version": item.duplicate.dataset_version,
                        "Keep latest successful upload": (
                            item.keeper.imported_at.strftime("%Y-%m-%d %H:%M %Z")
                        ),
                    } for item in duplicate_preview],
                    hide_index=True,
                    width="stretch",
                )
                st.warning(
                    "This removes duplicate upload-history entries only. Master "
                    "CSVs, backups, generated analytics, and map data are not changed."
                )
                confirmed = st.checkbox(
                    "I confirm that I want to remove these duplicate history records.",
                    key="confirm_history_duplicate_removal",
                )
                if st.button(
                    "Confirm Removal",
                    type="primary",
                    disabled=not confirmed,
                    key="confirm-history-duplicate-removal",
                ):
                    removed = store.remove_duplicate_import_history(
                        item.duplicate.record_id for item in duplicate_preview
                    )
                    removed_ids = {
                        item.duplicate.record_id for item in duplicate_preview
                    }
                    history = [
                        entry for entry in history
                        if entry.record_id not in removed_ids
                    ]
                    st.session_state.pop("history_duplicate_preview", None)
                    st.success(
                        f"Removed {removed} duplicate upload-history record(s)."
                    )

        for entry in history:
            source_stem = Path(entry.source_name).stem
            date_label = entry.imported_at.strftime("%Y-%m-%d %H:%M %Z")
            with st.expander(f"{date_label} · {entry.source_name}"):
                st.caption(
                    f"Dataset version {entry.dataset_version:,} · Generated by "
                    f"{entry.imported_by}"
                )
                master_download, leads_download, cleaned, analysis = st.columns(4)
                master_snapshot = getattr(entry, "master_csv", None)
                if master_snapshot is not None:
                    master_download.download_button(
                        "Download master from this upload",
                        master_snapshot,
                        f"lma_master_after_{source_stem}.csv",
                        "text/csv",
                        key=f"history-master-{entry.dataset_version}",
                    )
                else:
                    master_download.caption("Master snapshot is no longer retained.")
                leads_snapshot = getattr(entry, "new_leads_csv", None)
                if leads_snapshot is not None:
                    leads_download.download_button(
                        "Download new leads master from this upload",
                        leads_snapshot,
                        f"lma_new_leads_after_{source_stem}.csv",
                        "text/csv",
                        key=f"history-new-leads-{entry.dataset_version}",
                    )
                else:
                    leads_download.caption(
                        "New leads snapshot is no longer retained."
                    )
                cleaned.download_button(
                    "Download cleaned export",
                    entry.cleaned_csv,
                    f"{source_stem}_cleaned.csv",
                    "text/csv",
                    key=f"history-cleaned-{entry.dataset_version}",
                )
                analysis.download_button(
                    "Download analysis",
                    entry.analysis_csv,
                    f"{source_stem}_analysis.csv",
                    "text/csv",
                    key=f"history-analysis-{entry.dataset_version}",
                )
except Exception:
    st.warning("Generated file history could not be loaded.")

if master is not None and leads is not None and current_status is not None:
    st.subheader("Latest shared datasets")
    left, right = st.columns(2)
    left.download_button("Download Master", master, "lma_master.csv", "text/csv")
    right.download_button(
        "Download New Leads Master", leads, "lma_new_leads_only.csv", "text/csv"
    )
    st.caption(f"Current dataset version: {current_status.version:,}")
else:
    st.warning("The latest shared datasets are unavailable for download.")
