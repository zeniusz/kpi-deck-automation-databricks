"""Overview page: what the two services do, current state, and what to do next."""

from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ask_widget import render_ask_widget  # noqa: E402
import links  # noqa: E402
from run_manifest import last_run  # noqa: E402
from session_status import session_state  # noqa: E402
from ui_theme import (  # noqa: E402
    card_link,
    inject_css,
    section_close,
    section_open,
    sidebar,
    title,
)

inject_css()
sidebar(
    "What you can do",
    [
        "Full Monthly Run — both services in one click.",
        "Files → Databricks — upload the new sheets.",
        "Presentation Update — refresh dashboards + deck.",
        "Reports & Summaries — download what changed.",
        "Links to SharePoint, dashboards and Genie.",
    ],
)
title("Analytics", "KPI Self-Service", "")

section_open(1, "Pick what you want to do — click a card")
c1, c2 = st.columns(2)
with c1:
    card_link(
        "load", "SERVICE 1", "📥 Files → Databricks",
        [
            "Upload one or more Excel sheets",
            "Raw file archived into its UC Volume folder — <b>never deleted</b>",
            "Cleaned; missing columns looked up from related tables, then derived",
            "Rows <b>appended</b>, tagged by reporting month",
            "Genie spaces + dashboards refreshed at the end",
        ],
        min_height=300,
    )
with c2:
    card_link(
        "presentation", "SERVICE 2", "📊 Presentation Update",
        [
            "Detects what new data landed in Databricks",
            "Refreshes the dashboards at the newest month",
            "Captures each visual and inserts it proportionally",
            "Updates the tagged template — <b>all other content kept</b>",
            "Downloads the finished deck",
        ],
        min_height=300,
    )

c3, c4 = st.columns(2)
with c3:
    card_link(
        "full-run", "⚡ FULL RUN", "Run everything at once",
        [
            "Both services back-to-back, in the required order",
            "Load the sheets, then rebuild the deck from them",
            "One progress bar, one set of downloads",
        ],
        min_height=230,
    )
with c4:
    card_link(
        "reports", "📄 REPORTS", "Summary of what changed",
        [
            "Pick the month and the programmes",
            "See how each KPI grew, with % change",
            "Download as a Word document or Markdown",
        ],
        min_height=230,
    )

section_close()

section_open(2, "How it works")
st.markdown(
    "<div class='small-note'>"
    "The two services are linked only by <b>Databricks itself</b> — Service 2 rebuilds the deck "
    "from whatever is already loaded, so it never waits for Service 1. That means you can load new "
    "sheets today and rebuild the deck next week, or rebuild it right now from last month's data."
    "<br><br>"
    "<b>Archived files are never deleted.</b> Each upload is archived to its UC Volume folder (the same "
    "filename replaces its own archived copy) and rows are appended to the table, tagged with their "
    "reporting month. Re-running the same month replaces just that month rather than duplicating it."
    "<br><br>"
    "<b>Missing columns are filled in the order the notebooks use:</b> first looked up from a "
    "related table (matched on email, including <code>cloud.contoso.example</code> ↔ <code>contoso.example</code> "
    "aliases), then derived from the company bracket code or email domain, and otherwise left "
    "empty — never invented."
    "<br><br>"
    "<b>⚡ Full Monthly Run</b> does both services in one click, in the required order (load first, "
    "then build the deck — otherwise the dashboards would still show last month). "
    "<b>📄 Reports &amp; Summaries</b> is read-only: pick a month and the programmes, and download "
    "a written report of what changed."
    "</div>",
    unsafe_allow_html=True,
)

prev = last_run()
sess = session_state()
notes = []
if prev:
    notes.append(
        f"Last load: <b>{html.escape(prev.timestamp)}</b> · {prev.total_rows:,} rows · "
        f"<code>{html.escape(', '.join(t.split('.')[-1] for t in prev.tables) or '—')}</code>"
    )
if not sess.exists or sess.looks_stale:
    notes.append(
        "<span class='warn'>●</span> Dashboard screenshots need a Databricks sign-in — there is a "
        "button inside <b>Presentation Update</b> and <b>Full Monthly Run</b> (no terminal needed)."
    )
for n in notes:
    st.markdown(f"<div class='small-note' style='margin-top:.7rem'>{n}</div>",
                unsafe_allow_html=True)
section_close()

section_open(3, "Where everything lives")
links.render(st)
section_close()


# ── Floating "Ask the data" bubble, bottom-right on every page ──────────────────────────────
render_ask_widget("0_home")
