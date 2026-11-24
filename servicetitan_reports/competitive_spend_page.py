"""One-file competitive-spend analysis page."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from servicetitan_reports.competitive_spend import (  # noqa: E402
    MONTH_NAMES,
    TARGET_MEDIA_TYPES,
    analysis_csv_bytes,
    available_months,
    available_years,
    chart_share_rows,
    period_label,
    read_competitive_spend_workbook,
    spend_share_rows,
)


def secret_list(section: str, key: str) -> set[str]:
    return {
        str(value).strip().casefold()
        for value in st.secrets.get(section, {}).get(key, [])
        if str(value).strip()
    }


def require_approved_user() -> str:
    if not getattr(st.user, "is_logged_in", False):
        st.title("Competitive spend")
        st.write("Sign in with an approved account to continue.")
        st.button("Sign in", on_click=st.login, type="primary")
        st.stop()

    user = st.user.to_dict()
    email = str(
        user.get("email") or user.get("preferred_username") or ""
    ).strip().casefold()
    allowed = secret_list("access", "allowed_emails")
    allowed.update(secret_list("access", "admin_emails"))
    if (
        user.get("email_verified") is False
        or not email
        or email not in allowed
    ):
        st.error("This account is not approved to use this app.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
    return email


@st.cache_data(max_entries=5, show_spinner=False)
def parse_workbook(contents: bytes):
    return read_competitive_spend_workbook(contents)


@st.cache_data(max_entries=5, show_spinner=False)
def build_analysis_download(records):
    return analysis_csv_bytes(records)


def render_share_chart(
    records,
    *,
    media_type: str,
    dimension: str,
    heading: str,
    year: int,
    month: int | None,
    period: str,
) -> None:
    rows = spend_share_rows(
        records,
        media_type=media_type,
        dimension=dimension,
        year=year,
        month=month,
    )
    st.write(f"**{heading}**")
    if not rows:
        st.info(f"No {heading.casefold()} spend is available for {period}.")
        return

    chart_rows = chart_share_rows(rows)
    st.echarts_chart(
        {
            "tooltip": {
                "trigger": "item",
                "formatter": "{b}<br/>${c} ({d}%)",
            },
            "series": [{
                "type": "pie",
                "radius": "58%",
                "center": ["50%", "48%"],
                "avoidLabelOverlap": True,
                "minShowLabelAngle": 0,
                "label": {
                    "show": True,
                    "position": "outside",
                    "formatter": "{b}\n{d}%",
                    "fontSize": 11,
                    "lineHeight": 15,
                    "width": 130,
                    "overflow": "break",
                },
                "labelLine": {
                    "show": True,
                    "length": 18,
                    "length2": 12,
                    "smooth": 0.2,
                },
                "labelLayout": {
                    "hideOverlap": True,
                    "moveOverlap": "shiftY",
                },
                "data": [
                    {"name": row["Name"], "value": row["Spend"]}
                    for row in chart_rows
                ],
            }],
        },
        width="stretch",
        height=480,
        renderer="svg",
        key=f"competitive-{media_type}-{dimension}",
        alt=f"{media_type} spend share by {heading.casefold()} for {period}",
    )
    st.caption("The chart shows the top 10 entries; remaining spend is Other.")

    total = sum(float(row["Spend"]) for row in rows)
    table_rows = [*rows, {
        "Name": "Total",
        "Spend": total,
        "Share of spend": 1.0,
    }]
    with st.expander("View all spend and percentages"):
        st.dataframe(
            table_rows,
            hide_index=True,
            width="stretch",
            alt=f"{media_type} spend and share by {heading.casefold()} for {period}",
            column_config={
                "Spend": st.column_config.NumberColumn(
                    "Spend", format="$%.2f"
                ),
                "Share of spend": st.column_config.NumberColumn(
                    "Share of spend", format="percent"
                ),
            },
        )


email = require_approved_user()

st.title("Competitive spend")
top_left, top_right = st.columns([4, 1])
top_left.caption(f"Signed in as {email}")
top_right.button("Sign out", on_click=st.logout)

st.write(
    "Upload the original competitive-spend Excel workbook. The Detail View "
    "sheet supplies every media type, parent advertiser, media outlet, month, "
    "and spend amount needed for the charts."
)
upload = st.file_uploader(
    "Competitive-spend workbook",
    type=["xlsx"],
    key="competitive_spend_workbook",
)
if upload is None:
    st.info("Upload BH CS Master Log.xlsx to generate the report.")
    st.stop()

try:
    with st.spinner("Reading competitive-spend records…"):
        dataset = parse_workbook(upload.getvalue())
except ValueError as error:
    st.error(str(error))
    st.stop()

years = available_years(dataset.records)
if not years:
    st.error("No TV, Cable, Radio, or OOH records were found.")
    st.stop()

controls = st.container(horizontal=True, vertical_alignment="bottom")
with controls:
    selected_year = st.selectbox(
        "Year",
        years,
        key="competitive_spend_year",
    )
    view_mode = st.segmented_control(
        "Period",
        ["YTD", "Monthly"],
        default="YTD",
        key="competitive_spend_period",
    )

months = available_months(dataset.records, selected_year)
selected_month = None
if view_mode == "Monthly":
    selected_month = st.selectbox(
        "Month",
        tuple(reversed(months)),
        format_func=lambda value: MONTH_NAMES[value - 1],
        key="competitive_spend_month",
    )
    selected_period = f"{MONTH_NAMES[selected_month - 1]} {selected_year}"
else:
    selected_period = period_label(selected_year, months)

st.caption(selected_period)
if dataset.blank_spend_rows:
    st.warning(
        f"{dataset.blank_spend_rows:,} Detail View row(s) have blank AdRev "
        "values and were excluded from spend calculations."
    )
if dataset.invalid_spend_rows or dataset.invalid_period_rows:
    st.warning(
        f"{dataset.invalid_spend_rows + dataset.invalid_period_rows:,} row(s) "
        "with invalid spend or period values were excluded."
    )

metric_columns = st.columns(len(TARGET_MEDIA_TYPES))
for column, media_type in zip(metric_columns, TARGET_MEDIA_TYPES):
    rows = spend_share_rows(
        dataset.records,
        media_type=media_type,
        dimension="parent",
        year=selected_year,
        month=selected_month,
    )
    column.metric(
        f"{media_type} spend",
        f"${sum(float(row['Spend']) for row in rows):,.0f}",
    )

st.download_button(
    "Download spend-share analysis",
    build_analysis_download(dataset.records),
    f"competitive_spend_analysis_{selected_year}.csv",
    "text/csv",
    on_click="ignore",
)

for media_type in TARGET_MEDIA_TYPES:
    st.subheader(media_type)
    parent_column, outlet_column = st.columns(2, gap="large")
    with parent_column:
        render_share_chart(
            dataset.records,
            media_type=media_type,
            dimension="parent",
            heading="Parent advertiser",
            year=selected_year,
            month=selected_month,
            period=selected_period,
        )
    with outlet_column:
        render_share_chart(
            dataset.records,
            media_type=media_type,
            dimension="media_outlet",
            heading="Media outlet",
            year=selected_year,
            month=selected_month,
            period=selected_period,
        )
