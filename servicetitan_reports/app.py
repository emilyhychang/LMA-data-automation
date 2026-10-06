"""Restricted ServiceTitan cleaner backed by persistent shared datasets."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from servicetitan_reports.pipeline import load_settings  # noqa: E402
from servicetitan_reports.storage import PersistentDatasetStore  # noqa: E402


SETTINGS = load_settings(ROOT / "config" / "settings.json")


def secret_list(section: str, key: str) -> set[str]:
    return {
        str(value).strip().lower()
        for value in st.secrets.get(section, {}).get(key, [])
        if str(value).strip()
    }


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


st.set_page_config(page_title="ServiceTitan File Cleaner", layout="centered")
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
        st.success("Both shared datasets were saved. A backup of the prior version was retained.")
    except ValueError as error:
        st.error(f"Nothing was saved: {error}")
    except Exception:
        st.error("Nothing was saved. The import could not complete; retry or contact an administrator.")

result = st.session_state.get("import_result")
if result:
    first, second, third, fourth = st.columns(4)
    first.metric("Valid records", f"{result.cleaned_rows:,}")
    second.metric("Added to master", f"{result.appended_rows:,}")
    third.metric("Duplicates skipped", f"{result.skipped_duplicate_rows:,}")
    fourth.metric("New customers", f"{result.new_customers:,}")

    raw_stem = st.session_state["raw_stem"]
    st.subheader("Import downloads")
    st.download_button(
        "Download cleaned export", result.cleaned_csv,
        f"{raw_stem}_cleaned.csv", "text/csv"
    )
    st.download_button(
        "Download period comparison", result.comparison_csv,
        f"{raw_stem}_period_comparison.csv", "text/csv"
    )

try:
    master, leads, current_status = store.downloads()
    st.subheader("Latest shared datasets")
    left, right = st.columns(2)
    left.download_button("Download Master", master, "lma_master.csv", "text/csv")
    right.download_button(
        "Download New Leads Master", leads, "lma_new_leads_only.csv", "text/csv"
    )
    st.caption(f"Current dataset version: {current_status.version:,}")
except Exception:
    st.warning("The latest shared datasets could not be prepared for download.")
