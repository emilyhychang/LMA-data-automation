"""Restricted ServiceTitan cleaner backed by persistent shared datasets."""

from __future__ import annotations

import importlib
import hashlib
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
from servicetitan_reports.startup import open_dataset_store, StorageStartupError  # noqa: E402
from servicetitan_reports.workflow import latest_dashboard, imported_dashboard, weekly_charts  # noqa: E402
from servicetitan_reports.zip_maps import (  # noqa: E402
    publish_zip_summaries,
    zip_code_summaries,
)

# Streamlit can rerun this file while retaining an older imported module.
history_fields = storage_module.ImportHistoryEntry.__dataclass_fields__
if (
    "master_csv" not in history_fields
    or "new_leads_csv" not in history_fields
    or not hasattr(storage_module.PersistentDatasetStore, "history_snapshots")
    or not hasattr(storage_module.PersistentDatasetStore, "startup_status")
):
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


def render_zip_ranking(rows, metric) -> None:
    """Render the top 20 ZIPs with the values that determine their rank."""
    if not rows:
        st.info("No ZIP data is available.")
        return
    st.dataframe(
        [{"Rank": index, **row} for index, row in enumerate(rows[:20], 1)],
        hide_index=True, width="stretch",
        column_config={"Zip": st.column_config.TextColumn("ZIP"),
                       metric: st.column_config.NumberColumn(metric, format="$%.2f" if metric == "Revenue" else "%d")},
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

scope = hashlib.sha256((email + database_url + json.dumps(SETTINGS, sort_keys=True) + 'workflow-v1').encode()).hexdigest()
if st.session_state.get('workflow_scope') not in (None, scope):
    st.session_state.clear()
st.session_state['workflow_scope'] = scope
workflow_cache = st.session_state.setdefault('workflow_cache', {})

try:
    store = PersistentDatasetStore(database_url, SETTINGS)
    if st.session_state.get('startup_checked'):
        try:
            dataset_status = store.status()
        except Exception:
            dataset_status = open_dataset_store(store, allow_schema_setup=is_admin)
    else:
        dataset_status = open_dataset_store(store, allow_schema_setup=is_admin)
        st.session_state['startup_checked'] = True
except StorageStartupError as error:
    st.error(str(error))
    st.button("Retry database connection")
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
# Initialize optional map tracking only for an administrator; failures must not
# prevent viewing reports or saving the core datasets.
try:
    if not st.session_state.get('map_setup_checked'):
        map_setup = store.map_status(allow_setup=is_admin)
        if not map_setup or not map_setup.get('setup_required'):
            st.session_state['map_setup_checked'] = True
except Exception:
    pass
with st.expander("Historical imports and corrections"):
    allow_historical = st.checkbox("Allow a historical import spanning more than 14 days")
    allow_correction = st.checkbox(
        "Intentionally reapply a previous file or replace complete job groups",
        help="Only enable after checking the export contains every job for each customer/date/business-unit/job-type group. Missing rows in replaced groups will be removed; the previous master is backed up.",
    )
if st.button("Clean and save report", type="primary", disabled=raw_upload is None):
    try:
        with st.spinner("Cleaning the report and updating both shared datasets…"):
            workflow_cache.clear()
            st.session_state.pop('import_result', None)
            st.session_state.pop('loaded_report_id', None)
            result = store.import_report(
                raw_upload.getvalue(), raw_upload.name, email,
                allow_historical=allow_historical, allow_correction=allow_correction,
            )
        st.session_state["import_result"] = result
        st.session_state["raw_stem"] = Path(raw_upload.name).stem
        st.session_state.pop("saved_report_id", None)
        st.session_state.pop("loaded_report_id", None)
        imported_status = storage_module.DatasetStatus(result.dataset_version, datetime.now(ZoneInfo('UTC')), email)
        imported_dashboard(workflow_cache, result, imported_status)
        try:
            dataset_status = store.status()
        except Exception:
            dataset_status = imported_status
        st.session_state['just_imported_version'] = result.dataset_version
        try:
            store.refresh_maps(refresh_zip_map_sources, prepared=result)
        except Exception:
            st.warning("The report was saved, but map refresh could not complete. Use Refresh map sources below.")
        st.success(
            "Both shared datasets were saved. A backup of the prior version was retained."
        )
    except ValueError as error:
        st.error(f"Nothing was saved: {error}")
    except Exception:
        st.error("Nothing was saved. The import could not complete; retry or contact an administrator.")

master = leads = current_status = dashboard = None
render_rows = None
try:
    dashboard, render_rows = latest_dashboard(workflow_cache, store, dataset_status, SETTINGS)
    master, leads, current_status = dashboard['master'], dashboard['leads'], dashboard['status']
except Exception:
    st.warning("The latest shared datasets could not be prepared.")

history = []
try:
    history = store.history_listing()
    reports = [(entry.record_id, entry.dataset_version, entry.source_name, entry.imported_at) for entry in history]
    if reports:
        report_labels = {record_id: f"{created:%Y-%m-%d} · {name} · version {version}"
                         for record_id, version, name, created in reports}
        if st.session_state.get("saved_report_id") not in report_labels:
            st.session_state.pop("saved_report_id", None)
        selected_report = st.selectbox("Saved report", list(report_labels),
                                       format_func=report_labels.get, key="saved_report_id")
        if st.session_state.get("loaded_report_id") != selected_report:
            selected_version = next(version for record_id, version, _, _ in reports if record_id == selected_report)
            if st.session_state.get('just_imported_version') == selected_version and st.session_state.get('import_result'):
                restored = st.session_state['import_result']
            else:
                st.session_state.pop("import_result", None)
                snapshot = (current_status.version, master, render_rows) if current_status else None
                restored = store.load_saved_report(selected_report, cached_snapshot=snapshot)
            st.session_state["import_result"] = restored
            st.session_state["raw_stem"] = Path(restored.source_name).stem
            st.session_state["loaded_report_id"] = selected_report
            st.session_state.pop('just_imported_version', None)
except Exception:
    st.warning("Saved report details could not be loaded. Refresh to try again.")

# Fragment closures retain this script's globals; release temporary full-master
# rows after report restoration rather than retaining them for the session.
render_rows = snapshot = None

result = st.session_state.get("import_result")
if result:
    period_label = f"{result.period_start:%Y-%m-%d} to {result.period_end:%Y-%m-%d}" if result.period_start else "Original date range unavailable"
    st.caption(f"Saved report: {st.session_state['raw_stem']} · {period_label}")
    if hasattr(result, "dataset_version"):
        st.caption("Metrics are the original saved import counts. Comparisons and recalculated analysis use this report's retained master snapshot, not today's master.")
    first, second, third, fourth, fifth = st.columns(5)
    first.metric("Valid records", f"{result.cleaned_rows:,}")
    second.metric("Added to master", f"{result.appended_rows:,}")
    third.metric("Updated records", f"{getattr(result, 'updated_rows', 0):,}")
    fourth.metric("Unchanged records", f"{result.skipped_duplicate_rows:,}")
    fifth.metric("New customers", f"{result.new_customers:,}")
    st.caption("Added, updated and unchanged count incoming rows. Exact matches are unchanged; remaining rows are matched within each job group.")
    if getattr(result, "removed_rows", 0):
        st.warning(f"{result.removed_rows:,} prior rows were removed by the confirmed group replacement. The previous master is retained in history/backups.")

    raw_stem = st.session_state["raw_stem"]

st.subheader("Import downloads")
if result:
    if result.cleaned_csv is not None:
        st.download_button("Download cleaned export", result.cleaned_csv, f"{raw_stem}_cleaned.csv", "text/csv", on_click="ignore")
    if result.analysis_csv is not None:
        st.download_button("Download recalculated analysis", result.analysis_csv, f"{raw_stem}_analysis_recalculated.csv", "text/csv", on_click="ignore")
    if getattr(result, "original_analysis_csv", None) is not None:
        st.download_button("Download original saved analysis", result.original_analysis_csv, f"{raw_stem}_analysis_original.csv", "text/csv", on_click="ignore")
    if result.analysis_csv is None:
        st.info("Recalculated analysis is unavailable because the original export or master snapshot is no longer retained.")

if master is not None and leads is not None and current_status is not None:
    left, right = st.columns(2)
    left.download_button("Download Master", master, "lma_master.csv", "text/csv", on_click="ignore")
    right.download_button("Download New Leads Master", leads, "lma_new_leads_only.csv", "text/csv", on_click="ignore")
    st.caption(f"Current dataset version: {current_status.version:,}")
else:
    st.warning("The latest shared datasets are unavailable for download.")

fresh_comparisons = getattr(result, 'category_comparisons', None) if result else None
if result and result.period_start and fresh_comparisons is None and result.master_csv is not None:
    report_rows = read_and_clean_csv_bytes(result.master_csv, SETTINGS, 'Report master', allow_empty=True)
    fresh_comparisons = category_period_comparisons(report_rows, result.period_start, result.period_end, SETTINGS)
    # Reuse the comparison on widget reruns even for an older in-session result.
    if hasattr(result, '__dict__'):
        object.__setattr__(result, 'category_comparisons', fresh_comparisons)
    report_rows = None
if fresh_comparisons:
    st.subheader("Period comparisons")
    st.caption(
        "Jobs and revenue are counted in their own category. Customers using both "
        "services appear in both unique-customer totals; new customers are assigned "
        "to one category using their earliest qualifying job in the period."
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

derived_lead_rows = dashboard['lead_rows'] if dashboard else []

if dashboard and dashboard['has_rows']:
    zip_year, zip_revenue_rows, zip_new_lead_rows = dashboard['zip_summary']
    st.subheader(f"Top ZIP codes and heatmaps — {zip_year}")
    st.caption(
        "Tables show the top 20 ZIPs for the latest master year. Maps include all valid ZIPs. "
        "Selecting an older report does not change these latest-master rankings or map sources."
    )
    try:
        if st.button("Refresh map sources", help="Republish the latest master to Google Sheets without importing or saving the report again."):
            with st.spinner("Refreshing both map sources…"):
                store.refresh_maps(refresh_zip_map_sources)
            st.rerun()
        map_status = store.map_status()
        if map_status and map_status.get("setup_required"):
            st.info("An administrator needs to open the app once to enable map refresh tracking.")
        elif not map_status:
            st.info("Map freshness has not been recorded yet. Use Refresh map sources.")
        elif map_status["error"]:
            st.warning(map_status["error"])
        elif current_status and map_status["success_version"] == current_status.version:
            st.success(f"Both map sources refreshed for dataset version {map_status['success_version']} at {map_status['refreshed_at']}.")
        else:
            st.warning("Map sources are behind the latest master. Use Refresh map sources.")
        if map_status and map_status.get("refreshed_at"):
            st.caption(f"Last successful refresh: version {map_status['success_version']} · {map_status['refreshed_at']}")
        st.caption("Looker Studio may cache its display. Check the map's ‘Map data generated at’ time against the successful refresh time. A refresh of one spreadsheet cannot guarantee the other succeeded.")
    except Exception:
        st.warning("Map freshness could not be checked or refreshed. Try again; the saved report is unchanged.")
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
        leads_list, leads_map = st.columns([2, 3], gap="small")
        with leads_list:
            render_zip_ranking(zip_new_lead_rows, "New leads")
        with leads_map:
            if leads_embed_url:
                components.iframe(leads_embed_url, height=540, scrolling=False)
            else:
                st.info("The new-lead heatmap embed URL is not configured yet.")

    with revenue_section:
        st.write("**Revenue**")
        revenue_list, revenue_map = st.columns([2, 3], gap="small")
        with revenue_list:
            render_zip_ranking(zip_revenue_rows, "Revenue")
        with revenue_map:
            if revenue_embed_url:
                components.iframe(revenue_embed_url, height=540, scrolling=False)
            else:
                st.info("The revenue heatmap embed URL is not configured yet.")

    st.subheader("Weekly opportunities — YTD")
    st.caption(
        "Count of nonblank Opportunity values for each customer's first master "
        "record. Weeks and year labels use the ISO week calendar, including week 53."
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
    charts = weekly_charts(workflow_cache, dashboard, SETTINGS, selected_statuses)
    for category in ("Plumbing", "HVAC"):
        chart_rows, years = charts[category]
        st.write(f"**{category}**")
        if years and chart_rows:
            st.vega_lite_chart(
                chart_rows,
                {
                    "transform": [{"fold": [str(years[0]), str(years[1])], "as": ["Year", "Opportunities"]}],
                    "mark": "line",
                    "encoding": {
                        "x": {"field": "Week", "type": "quantitative", "scale": {"domain": [0, 54], "nice": False}, "axis": {"title": "ISO week"}},
                        "y": {"field": "Opportunities", "type": "quantitative"},
                        "color": {"field": "Year", "type": "nominal"},
                        "tooltip": [{"field": "Week"}, {"field": "Year"}, {"field": "Opportunities"}],
                    },
                },
                width="stretch",
            )
        else:
            st.info(f"No {category} opportunity history is available yet.")

@st.fragment
def render_file_history(history):
    """Refresh only file history on interaction; reuse already loaded downloads."""

    def remove_entry(entry):
        if not is_admin:
            return
        try:
            removed = store.remove_import_history_entry(entry)
        except Exception:
            st.session_state["history_delete_error"] = "The run could not be deleted. Try again."
            return
        if not removed:
            st.session_state["history_delete_error"] = (
                "The run was already removed or changed. Refresh the page and try again."
            )
            return
        history[:] = [item for item in history if item.record_id != entry.record_id]
        st.session_state.pop('history_loaded_outputs', None)
        st.session_state.pop("history_duplicate_preview", None)
        loaded_version = st.session_state.get(
            "history_loaded_snapshots", (None, None, None)
        )[0]
        if loaded_version == entry.dataset_version:
            st.session_state.pop("history_loaded_snapshots", None)

    error = st.session_state.pop("history_delete_error", None)
    if error:
        st.error(error)
    try:
        if history:
            st.subheader("Generated file history")
            st.caption("All retained reports are listed. Deleting a listing leaves master data, generated files and backups intact. Files removed by older retention rules cannot be recreated here.")
            if is_admin and st.button("Remove Duplicates", key="preview-history-duplicates"):
                st.session_state["history_duplicate_preview"] = (
                    store.duplicate_import_history()
                )
                st.session_state["confirm_history_duplicate_removal"] = False

            duplicate_preview = st.session_state.get("history_duplicate_preview")
            if is_admin and duplicate_preview is not None:
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
                        removed_ids = store.remove_duplicate_import_history(
                            item.duplicate.record_id for item in duplicate_preview
                        )
                        history[:] = [
                            entry for entry in history
                            if entry.record_id not in removed_ids
                        ]
                        st.session_state.pop("history_loaded_snapshots", None)
                        st.session_state.pop('history_loaded_outputs', None)
                        st.session_state.pop("history_duplicate_preview", None)
                        st.success(
                            f"Removed {len(removed_ids)} duplicate upload-history record(s)."
                        )

            for entry in history:
                source_stem = Path(entry.source_name).stem
                date_label = entry.imported_at.strftime("%Y-%m-%d %H:%M %Z")
                listing, close = st.columns([0.96, 0.04], vertical_alignment="top")
                if is_admin:
                    close.button(
                        "×", key=f"remove-history-{entry.record_id}",
                        help="Delete this run from file history",
                        on_click=remove_entry, args=(entry,),
                    )
                with listing.expander(f"{date_label} · {entry.source_name}"):
                    st.caption(
                        f"Dataset version {entry.dataset_version:,} · Generated by "
                        f"{entry.imported_by}"
                    )
                    loaded_version, master_snapshot, leads_snapshot = st.session_state.get(
                        "history_loaded_snapshots", (None, None, None)
                    )
                    if loaded_version != entry.dataset_version:
                        master_snapshot = leads_snapshot = None
                        if st.button("Load master downloads", key=f"load-masters-{entry.record_id}"):
                            with st.spinner("Preparing this upload's master downloads…"):
                                master_snapshot, leads_snapshot = store.history_snapshots(entry.dataset_version)
                            st.session_state["history_loaded_snapshots"] = (
                                entry.dataset_version, master_snapshot, leads_snapshot
                            )
                            loaded_version = entry.dataset_version
                    master_download, leads_download, cleaned, analysis = st.columns(4)
                    cleaned_bytes, analysis_bytes = entry.cleaned_csv, entry.analysis_csv
                    outputs_loaded = getattr(entry, 'outputs_loaded', True)
                    if not outputs_loaded:
                        loaded_id, files = st.session_state.get('history_loaded_outputs', (None, (None, None)))
                        if loaded_id != entry.record_id and (entry.has_cleaned or entry.has_analysis):
                            if st.button('Load report downloads', key=f'load-outputs-{entry.record_id}'):
                                files = store.history_outputs(entry.record_id)
                                st.session_state['history_loaded_outputs'] = (entry.record_id, files)
                                loaded_id = entry.record_id
                        if loaded_id == entry.record_id:
                            cleaned_bytes, analysis_bytes = files
                            outputs_loaded = True
                    if master_snapshot is not None:
                        master_download.download_button(
                            "Download master from this upload",
                            master_snapshot,
                            f"lma_master_after_{source_stem}.csv",
                            "text/csv",
                            key=f"history-master-{entry.dataset_version}",
                            on_click="ignore",
                        )
                    else:
                        if loaded_version == entry.dataset_version:
                            master_download.caption("Master snapshot is no longer retained.")
                    if leads_snapshot is not None:
                        leads_download.download_button(
                            "Download new leads master from this upload",
                            leads_snapshot,
                            f"lma_new_leads_after_{source_stem}.csv",
                            "text/csv",
                            key=f"history-new-leads-{entry.dataset_version}",
                            on_click="ignore",
                        )
                    else:
                        if loaded_version == entry.dataset_version:
                            leads_download.caption("New leads snapshot is no longer retained.")
                    if cleaned_bytes is not None:
                        cleaned.download_button(
                            "Download cleaned export",
                            cleaned_bytes,
                            f"{source_stem}_cleaned.csv",
                            "text/csv",
                            key=f"history-cleaned-{entry.record_id}",
                            on_click="ignore",
                        )
                    elif outputs_loaded or not entry.has_cleaned:
                        cleaned.caption("Cleaned export is no longer available.")
                    if analysis_bytes is not None:
                        analysis.download_button(
                            "Download analysis",
                            analysis_bytes,
                            f"{source_stem}_analysis.csv",
                            "text/csv",
                            key=f"history-analysis-{entry.record_id}",
                            on_click="ignore",
                        )
                    elif outputs_loaded or not entry.has_analysis:
                        analysis.caption("Analysis is no longer available.")
        else:
            st.subheader("Generated file history")
            st.info("No saved reports are listed yet.")
    except Exception:
        st.warning("Generated file history could not be loaded.")


try:
    render_file_history(history)
except Exception:
    st.warning("Generated file history could not be loaded.")
