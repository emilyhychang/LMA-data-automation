"""Authenticated reporting tools for ServiceTitan and competitive spend."""

from __future__ import annotations

import streamlit as st


st.set_page_config(page_title="LMA reporting", layout="wide")

page = st.navigation(
    [
        st.Page(
            "servicetitan_page.py",
            title="ServiceTitan cleaner",
            icon=":material/cleaning_services:",
            default=True,
        ),
        st.Page(
            "competitive_spend_page.py",
            title="Competitive spend",
            icon=":material/donut_large:",
        ),
    ],
    position="top",
)
page.run()
