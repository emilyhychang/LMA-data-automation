"""Restricted ServiceTitan cleaner backed by persistent shared datasets."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from servicetitan_reports.pipeline import (  # noqa: E402
    PERIOD_COMPARISON_COLUMNS,
    category_period_comparisons,
    ensure_categorized_csv_bytes,
    load_settings,
    period_new_leads_xlsx_bytes,
    read_canonical_csv_bytes,
)
from servicetitan_reports.storage import (  # noqa: E402
    DatasetStatus,
    PersistentDatasetStore,
)
from servicetitan_reports.startup import open_dataset_store, StorageStartupError  # noqa: E402
from servicetitan_reports.workflow import latest_dashboard, imported_dashboard, weekly_charts  # noqa: E402
from servicetitan_reports.zip_maps import (  # noqa: E402
    publish_zip_summaries,
)

SETTINGS = load_settings(ROOT / "config" / "settings.json")
CURRENT_UPLOAD_REPORT = "__current_upload__"


@st.cache_data(max_entries=50, show_spinner=False)
def cleaned_download_bytes(contents: bytes) -> bytes:
    """Ensure current and legacy cleaned downloads expose service category."""

    try:
        return ensure_categorized_csv_bytes(contents, SETTINGS)
    except ValueError:
        return contents


@st.cache_data(max_entries=20, show_spinner=False)
def period_new_leads_download_bytes(
    new_leads_csv: bytes,
    period_start,
    period_end,
) -> bytes:
    """Build one report-period workbook from its saved new-leads snapshot."""

    new_lead_rows = read_canonical_csv_bytes(
        new_leads_csv,
        SETTINGS,
        "Saved New Leads Master",
        allow_empty=True,
    )
    return period_new_leads_xlsx_bytes(
        new_lead_rows,
        period_start,
        period_end,
        SETTINGS,
    )


@st.cache_data(max_entries=50, show_spinner=False)
def report_category_comparisons(
    master_csv: bytes,
    new_leads_csv: bytes,
    period_start,
    period_end,
):
    """Recalculate comparisons from the report currently shown on screen."""

    report_rows = read_canonical_csv_bytes(
        master_csv,
        SETTINGS,
        "Report master",
        allow_empty=True,
    )
    new_lead_rows = read_canonical_csv_bytes(
        new_leads_csv,
        SETTINGS,
        "Saved New Leads Master",
        allow_empty=True,
    )
    return category_period_comparisons(
        report_rows,
        period_start,
        period_end,
        SETTINGS,
        new_lead_rows,
    )

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
        str(value).strip().casefold()
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
    if not rows:
        st.info("No ZIP data is available.")
        return

    st.text("\n".join(str(row["Zip"]) for row in rows[:20]))


def refreshed_embed_url(url: str, report_key: tuple, refreshed_at: object) -> str:
    """Force a fresh iframe document for one report/map publication."""

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["map_report"] = "-".join(str(value) for value in report_key)
    query["map_refresh"] = str(refreshed_at or "pending")
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    ))


def require_approved_user() -> tuple[str, bool]:
    """Require OIDC login and return the normalized email and admin status."""

    if not getattr(st.user, "is_logged_in", False):
        st.title("ServiceTitan File Cleaner")
        st.write("Sign in with an approved account to continue.")
        st.button("Sign in", on_click=st.login, type="primary")
        st.stop()

    user = st.user.to_dict()
    email = str(
        user.get("email") or user.get("preferred_username") or ""
    ).strip().casefold()
    email_verified = user.get("email_verified") is not False
    allowed = secret_list("access", "allowed_emails")
    admins = secret_list("access", "admin_emails")
    allowed.update(admins)
    if not email_verified or not email or email not in allowed:
        st.error("This account is not approved to use this app.")
        if email:
            st.caption(f"Google signed you in as: `{email}`")
        else:
            st.caption("Google did not return an email address for this account.")
        if not email_verified:
            st.caption("Google reported that this email address is not verified.")
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

if st.button("Clean and save report", type="primary", disabled=raw_upload is None):
    try:
        with st.spinner("Cleaning the report and updating both shared datasets…"):
            workflow_cache.clear()
            st.session_state.pop('import_result', None)
            st.session_state.pop('loaded_report_id', None)
            upload_contents = raw_upload.getvalue()
            result = store.import_report(
                upload_contents,
                raw_upload.name,
                email,
                allow_historical=True,
            )
        st.session_state["import_result"] = result
        st.session_state["raw_stem"] = Path(raw_upload.name).stem
        st.session_state["current_upload_result"] = result
        st.session_state["current_upload_stem"] = Path(raw_upload.name).stem
        st.session_state["show_current_upload"] = True
        st.session_state["saved_report_id"] = CURRENT_UPLOAD_REPORT
        st.session_state["loaded_report_id"] = CURRENT_UPLOAD_REPORT
        imported_status = DatasetStatus(
            result.dataset_version,
            datetime.now(ZoneInfo("UTC")),
            email,
        )
        imported_dashboard(workflow_cache, result, imported_status)
        try:
            dataset_status = store.status()
        except Exception:
            dataset_status = imported_status
        # The map section below performs the single automatic publication for
        # this selected upload and records its session success key.
        if (
            result.appended_rows == 0
            and result.updated_rows == 0
            and result.removed_rows == 0
        ):
            st.success(
                "The report was reprocessed successfully. The shared datasets "
                "already matched, so no duplicate rows were added."
            )
        else:
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
    current_upload = st.session_state.get("current_upload_result")
    show_current_upload = bool(
        st.session_state.get("show_current_upload") and current_upload is not None
    )
    if reports or show_current_upload:
        report_labels = {
            record_id: f"{created:%Y-%m-%d} · {name} · version {version}"
            for record_id, version, name, created in reports
        }
        report_options = list(report_labels)
        if show_current_upload:
            current_stem = st.session_state.get(
                "current_upload_stem", "Current upload"
            )
            report_labels[CURRENT_UPLOAD_REPORT] = (
                f"Current upload · {current_stem} · version "
                f"{current_upload.dataset_version}"
            )
            report_options.insert(0, CURRENT_UPLOAD_REPORT)
        if st.session_state.get("saved_report_id") not in report_options:
            st.session_state["saved_report_id"] = report_options[0]
        selected_report = st.selectbox(
            "Saved report",
            options=report_options,
            format_func=lambda record_id: report_labels[record_id],
            key="saved_report_id",
        )
        if selected_report == CURRENT_UPLOAD_REPORT:
            st.session_state["import_result"] = current_upload
            st.session_state["raw_stem"] = st.session_state.get(
                "current_upload_stem", "current_upload"
            )
            st.session_state["loaded_report_id"] = CURRENT_UPLOAD_REPORT
        elif st.session_state.get("loaded_report_id") != selected_report:
            snapshot = (current_status.version, master, render_rows) if current_status else None
            restored = store.load_saved_report(selected_report, cached_snapshot=snapshot)
            st.session_state["import_result"] = restored
            st.session_state["raw_stem"] = Path(restored.source_name).stem
            st.session_state["loaded_report_id"] = selected_report
except Exception:
    st.warning("Saved report details could not be loaded. Refresh to try again.")

# Fragment closures retain this script's globals; release temporary full-master
# rows after report restoration rather than retaining them for the session.
render_rows = snapshot = None

result = st.session_state.get("import_result")
if result:
    period_label = f"{result.period_start:%Y-%m-%d} to {result.period_end:%Y-%m-%d}" if result.period_start else "Original date range unavailable"
    st.caption(f"Saved report: {st.session_state['raw_stem']} · {period_label}")
    first, second, third, fourth, fifth = st.columns(5)
    first.metric("Valid records", f"{result.cleaned_rows:,}")
    second.metric("Added to master", f"{result.appended_rows:,}")
    third.metric("Updated records", f"{getattr(result, 'updated_rows', 0):,}")
    fourth.metric("Unchanged records", f"{result.skipped_duplicate_rows:,}")
    fifth.metric("New customers", f"{result.new_customers:,}")
    if getattr(result, "removed_rows", 0):
        st.warning(f"{result.removed_rows:,} prior rows were removed by the confirmed group replacement. The previous master is retained in history/backups.")

    raw_stem = st.session_state["raw_stem"]

st.subheader("Import downloads")

col1, col2, col3, col4, col5 = st.columns(5, gap="small")

with col1:
    if result and result.cleaned_csv is not None:
        st.download_button(
            "Download cleaned export",
            cleaned_download_bytes(result.cleaned_csv),
            f"{raw_stem}_cleaned.csv",
            "text/csv",
            on_click="ignore",
        )

with col2:
    analysis_download = None
    if result:
        analysis_download = (
            getattr(result, "analysis_csv", None)
            or getattr(result, "original_analysis_csv", None)
        )
    if analysis_download is not None:
        st.download_button(
            "Download analysis",
            analysis_download,
            f"{raw_stem}_analysis.csv",
            "text/csv",
            on_click="ignore",
        )
    original_analysis = (
        getattr(result, "original_analysis_csv", None) if result else None
    )
    if original_analysis is not None and original_analysis != analysis_download:
        st.download_button(
            "Download original saved analysis",
            original_analysis,
            f"{raw_stem}_analysis_original.csv",
            "text/csv",
            on_click="ignore",
        )

with col3:
    if master is not None and current_status is not None:
        st.download_button(
            "Download Master",
            master,
            "lma_master.csv",
            "text/csv",
            on_click="ignore",
        )

with col4:
    if leads is not None and current_status is not None:
        st.download_button(
            "Download New Leads Master",
            leads,
            "lma_new_leads_only.csv",
            "text/csv",
            on_click="ignore",
        )

with col5:
    period_new_leads_csv = (
        getattr(result, "new_leads_csv", None) if result else None
    )
    if (
        period_new_leads_csv is not None
        and getattr(result, "period_start", None)
        and getattr(result, "period_end", None)
    ):
        period_filename = (
            f"New leads {result.period_start:%Y-%m-%d} to "
            f"{result.period_end:%Y-%m-%d}.xlsx"
        )
        st.download_button(
            "Download period new leads",
            period_new_leads_download_bytes(
                period_new_leads_csv,
                result.period_start,
                result.period_end,
            ),
            period_filename,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            on_click="ignore",
            help=(
                "Downloads one dated Excel workbook with separate Plumbing and "
                "HVAC sheets containing only new leads from this report period."
            ),
        )

if current_status is not None:
    st.caption(f"Current dataset version: {current_status.version:,}")
else:
    st.warning("The latest shared datasets are unavailable for download.")

fresh_comparisons = (
    getattr(result, "category_comparisons", None) if result else None
)
if fresh_comparisons is None and (
    result
    and result.period_start
    and result.master_csv is not None
    and getattr(result, "new_leads_csv", None) is not None
):
    fresh_comparisons = report_category_comparisons(
        result.master_csv,
        result.new_leads_csv,
        result.period_start,
        result.period_end,
    )
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
            width="stretch",
            alt=f"{category} current, previous, and prior-year period comparison",
            column_config={
                "Revenue": st.column_config.NumberColumn(
                    "Revenue", format="$%.2f"
                ),
            },
        )

derived_lead_rows = dashboard['lead_rows'] if dashboard else []

if dashboard and dashboard['has_rows']:
    selected_zip_summary = None
    if result and getattr(result, "period_start", None) and hasattr(result, "zip_revenue_rows"):
        selected_zip_summary = (
            getattr(result, "zip_year", result.period_end.year),
            result.zip_revenue_rows,
            result.zip_new_lead_rows,
        )
    zip_year, zip_revenue_rows, zip_new_lead_rows = selected_zip_summary or dashboard['zip_summary']
    zip_period_label = (
        f"{result.period_start:%Y-%m-%d} to {result.period_end:%Y-%m-%d}"
        if selected_zip_summary else str(zip_year)
    )
    st.subheader(f"Top ZIP codes and heatmaps — {zip_period_label}")
    st.caption(
        "Tables show the top 20 ZIPs for the selected report period. Maps include all valid ZIPs "
        "from that same period."
    )
    selected_map_key = (
        st.session_state.get("loaded_report_id"),
        getattr(result, "dataset_version", None) if selected_zip_summary else current_status.version,
        result.period_start.isoformat() if selected_zip_summary else zip_year,
        result.period_end.isoformat() if selected_zip_summary else zip_year,
    )
    map_status = None
    try:
        map_status = store.map_status()
        session_match = st.session_state.get("map_success_key") == selected_map_key
        # The database's legacy refresh record identifies only a dataset
        # version, not a report date range. Refresh each selected report once
        # per browser session even when two reports share one dataset version.
        needs_refresh = bool(selected_zip_summary and not session_match)

        # A saved-report selection changes the ZIP lists immediately. Publish
        # that same period once in this browser session so the maps cannot be
        # left showing a different report until the user notices the warning.
        if (
            needs_refresh
            and not (map_status and map_status.get("setup_required"))
            and st.session_state.get("map_auto_attempt_key") != selected_map_key
        ):
            st.session_state["map_auto_attempt_key"] = selected_map_key
            with st.spinner("Refreshing both map sources…"):
                map_status = store.refresh_maps(
                    refresh_zip_map_sources,
                    prepared=result,
                    allow_historical=True,
                )
            if map_status and not map_status.get("error"):
                st.session_state["map_success_key"] = selected_map_key
                needs_refresh = False

        if st.button(
            "Refresh map sources",
            help="Republish the selected report period to Google Sheets without importing or saving the report again.",
            type="primary" if needs_refresh else "secondary",
        ):
            with st.spinner("Refreshing both map sources…"):
                map_status = store.refresh_maps(
                    refresh_zip_map_sources,
                    prepared=result if selected_zip_summary else None,
                    allow_historical=bool(selected_zip_summary),
                )
            if map_status and not map_status.get("error"):
                st.session_state["map_success_key"] = selected_map_key
                st.session_state["map_auto_attempt_key"] = selected_map_key
                needs_refresh = False

        if map_status and map_status.get("setup_required"):
            st.info("An administrator needs to open the app once to enable map refresh tracking.")
        elif not map_status:
            st.info("Map freshness has not been recorded yet. Use Refresh map sources.")
        elif map_status["error"]:
            st.warning(map_status["error"])
        elif not needs_refresh:
            st.success(f"Both map sources refreshed for the selected report at {map_status['refreshed_at']}.")
        else:
            st.warning("Map sources do not match the selected report. Use Refresh map sources.")
        if map_status and map_status.get("refreshed_at"):
            st.caption(f"Last successful refresh: version {map_status['success_version']} · {map_status['refreshed_at']}")
        st.caption("Looker Studio may cache its display. Check the map's ‘Map data generated at’ time against the successful refresh time. A refresh of one spreadsheet cannot guarantee the other succeeded.")
    except Exception:
        st.warning("Map freshness could not be checked or refreshed. Try again; the saved report is unchanged.")
    leads_section, revenue_section = st.columns(2, gap="large")
    looker_settings = dict(st.secrets.get("looker_studio", {}))
    revenue_embed_url = str(
        looker_settings.get("revenue_embed_url", REVENUE_HEATMAP_URL)
    ).strip()
    leads_embed_url = str(
        looker_settings.get("new_leads_embed_url", NEW_LEADS_HEATMAP_URL)
    ).strip()
    embed_refresh_time = map_status.get("refreshed_at") if map_status else None
    if leads_embed_url:
        leads_embed_url = refreshed_embed_url(
            leads_embed_url, selected_map_key, embed_refresh_time
        )
    if revenue_embed_url:
        revenue_embed_url = refreshed_embed_url(
            revenue_embed_url, selected_map_key, embed_refresh_time
        )

    with leads_section:
        st.write("**New leads**")
        leads_list, leads_map = st.columns([1, 8], gap="small")
        with leads_list:
            render_zip_ranking(zip_new_lead_rows)
        with leads_map:
            if leads_embed_url:
                st.iframe(
                    leads_embed_url,
                    height=430,
                    alt=f"New leads ZIP heatmap for {zip_period_label}",
                )
            else:
                st.info("The new-lead heatmap embed URL is not configured yet.")

    with revenue_section:
        st.write("**Revenue**")
        revenue_list, revenue_map = st.columns([1, 8], gap="small")
        with revenue_list:
            render_zip_ranking(zip_revenue_rows)
        with revenue_map:
            if revenue_embed_url:
                st.iframe(
                    revenue_embed_url,
                    height=430,
                    alt=f"Revenue ZIP heatmap for {zip_period_label}",
                )
            else:
                st.info("The revenue heatmap embed URL is not configured yet.")

    st.subheader("Weekly opportunities — YTD")
    st.caption(
        "Count of nonblank Opportunity values for each customer's first master "
        "record. Every available ISO year remains as its own line and legend item; "
        "charts end at week 52."
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
                    "transform": [{
                        "fold": [str(year) for year in years],
                        "as": ["Year", "Opportunities"],
                    }],
                    "mark": "line",
                    "encoding": {
                        "x": {
                            "field": "Week",
                            "type": "quantitative",
                            "scale": {"domain": [0.5, 52.5], "nice": False},
                            "axis": {"title": "ISO week", "values": list(range(1, 53))},
                        },
                        "y": {"field": "Opportunities", "type": "quantitative"},
                        "color": {"field": "Year", "type": "nominal"},
                        "tooltip": [{"field": "Week"}, {"field": "Year"}, {"field": "Opportunities"}],
                    },
                },
                width="stretch",
                alt=(
                    f"Weekly {category} opportunities for "
                    f"{', '.join(str(year) for year in years)}"
                ),
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
                        alt="Duplicate upload-history entries selected for removal",
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
                            cleaned_download_bytes(cleaned_bytes),
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
