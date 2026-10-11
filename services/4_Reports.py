"""REPORTS & SUMMARIES — pick a month, download a report.

Standalone: it does not load data and does not touch the deck. Use it any time to answer
"what changed over the month, how did everything grow?" and hand someone a Word/Markdown file.

Two modes:
  * Compare two periods — pick the month and what to compare it against (defaults to the one
    before it). Produces "X grew from A to B (+n%)" lines.
  * Current state — everything as it stands right now, for programmes that have no period column.

The month list is read from Databricks, so it always offers exactly the periods that exist.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from change_summary import (  # noqa: E402
    PROGRAMME_NAMES,
    PROGRAMMES,
    available_periods,
    summarise,
    summarise_between,
    to_docx,
    to_markdown,
)
from ask_widget import render_ask_widget  # noqa: E402
import links  # noqa: E402
from run_manifest import read_manifest  # noqa: E402
from ui_theme import (  # noqa: E402
    fmt_preview,
    inject_css,
    section_close,
    section_open,
    sidebar,
    title,
)

inject_css()
sidebar(
    "Reports",
    [
        "Pick the month to report on.",
        "Choose what to compare it against.",
        "Download as Word or Markdown.",
    ],
)
title(
    "Reports &amp;",
    "Summaries",
    "Pick a month and download a written report of what changed and how much everything grew. "
    "Nothing is loaded or modified — this only reads.",
)

st.session_state.setdefault("rep_summary", None)


@st.cache_data(ttl=300, show_spinner="Reading the reporting periods in Databricks…")
def _load_periods() -> list[str]:
    """The months available to report on. Cached for 5 minutes so the page loads instantly on
    every rerun (toggling a chip must not re-query the warehouse)."""
    from databricks_client import _connect

    conn = _connect()
    try:
        return available_periods(conn)
    finally:
        conn.close()


# ── Step 1: Pick the period ─────────────────────────────────────────────────────────────────────
section_open(1, "Choose the reporting month")

periods: list[str] = []
try:
    periods = _load_periods()
except Exception as exc:  # noqa: BLE001 — warehouse may be down; the page must still render
    st.warning(f"Could not read the reporting periods ({exc}).")
    if st.button("🔄 Retry"):
        _load_periods.clear()
        st.rerun()

pc1, pc2, pc3 = st.columns([1.2, 1.2, 1.6])
month = compare_to = None
if not periods:
    st.caption("No reporting periods found yet — load some data first.")
else:
    with pc1:
        month = st.selectbox("Reporting month", periods, index=0)
    with pc2:
        others = ["(current state only)"] + [p for p in periods if p != month]
        default_prev = 1 if len(others) > 1 else 0
        pick = st.selectbox("Compare against", others, index=default_prev)
        compare_to = None if pick.startswith("(") else pick
    with pc3:
        st.markdown(
            f"<div class='small-note'>Reporting on <b>{html.escape(str(month))}</b>"
            + (f" vs <b>{html.escape(compare_to)}</b> — the report will show growth and % change."
               if compare_to else
               " — current state only (no comparison).")
            + "</div>",
            unsafe_allow_html=True,
        )
section_close()


# ── Step 2: Build ───────────────────────────────────────────────────────────────────────────────
section_open(2, "Build the report")
st.caption("Which programmes should the report cover?")
labels = list(PROGRAMMES.values())
chosen_labels = st.multiselect(
    "Programmes", labels, default=labels, label_visibility="collapsed",
    help="Short names keep the chips readable. Pick one, several, or leave all selected.",
)
chosen = [k for k, v in PROGRAMMES.items() if v in chosen_labels]
if chosen:
    st.markdown(
        "<div class='small-note'>Covering: "
        + " · ".join(html.escape(PROGRAMME_NAMES[k]) for k in chosen)
        + "</div>",
        unsafe_allow_html=True,
    )

b1, b2 = st.columns([1.2, 2.8])
with b1:
    build = st.button("📝 Build report", type="primary", use_container_width=True,
                      disabled=not periods or not chosen)
with b2:
    if not chosen:
        st.markdown("<div class='small-note warn'>Select at least one programme.</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown(
            f"<div class='small-note'>Reporting on <b>{len(chosen)}</b> of "
            f"{len(PROGRAMMES)} programmes.</div>",
            unsafe_allow_html=True,
        )

if build:
    try:
        from databricks_client import _connect

        runs = read_manifest()
        loaded = [
            f"{w['filename']} → {w['table'].split('.')[-1]} ({w.get('rows', 0):,} rows)"
            for r in runs[:1] for w in r.writes
        ]
        with st.spinner("Querying Databricks…"):
            conn = _connect()
            try:
                if compare_to:
                    st.session_state.rep_summary = summarise_between(
                        conn, month, compare_to, loaded=loaded, programmes=chosen)
                else:
                    st.session_state.rep_summary = summarise(
                        conn, loaded=loaded, programmes=chosen)
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not build the report: {exc}")
section_close()


# ── Step 3: Read + download ─────────────────────────────────────────────────────────────────────
summary = st.session_state.rep_summary
if summary:
    section_open(3, "The report")
    st.markdown(f"### {html.escape(summary.headline())}")
    st.caption(f"Generated {summary.generated}")

    grew = [m for m in summary.movements if m.pct is not None and m.pct > 0]
    fell = [m for m in summary.movements if m.pct is not None and m.pct < 0]
    if grew or fell:
        k1, k2, k3 = st.columns(3)
        for col, label, val in (
            (k1, "Metrics up", len(grew)),
            (k2, "Metrics down", len(fell)),
            (k3, "Biggest move",
             f"{max(summary.movements, key=lambda m: abs(m.pct or 0)).pct:+.1f}%"
             if any(m.pct is not None for m in summary.movements) else "—"),
        ):
            with col:
                st.markdown(
                    f"<div class='kpi-card'><div class='kpi-title'>{label}</div>"
                    f"<div class='kpi-value'>{val}</div></div>",
                    unsafe_allow_html=True,
                )

    st.markdown("#### What changed")
    for m in summary.movements:
        st.markdown(f"<div class='small-note'>• {html.escape(m.phrase())}</div>",
                    unsafe_allow_html=True)
    if summary.notes:
        st.markdown("#### Highlights")
        for n in summary.notes:
            st.markdown(f"<div class='small-note'>★ {html.escape(n)}</div>",
                        unsafe_allow_html=True)

    comparable = [m for m in summary.movements if m.previous is not None]
    if comparable:
        st.markdown("#### Period comparison")
        rows = "\n".join(
            f"| {m.label} | {fmt_preview(m.previous)} | {fmt_preview(m.current)} | "
            f"{f'{m.pct:+.1f}%' if m.pct is not None else '—'} |"
            for m in comparable
        )
        st.markdown(
            f"| Metric | {summary.previous_period or 'Previous'} | "
            f"{summary.current_period or 'Current'} | Change |\n"
            f"|---|---:|---:|---:|\n{rows}"
        )
    if summary.totals:
        # Rendered as markdown rather than st.dataframe: the data grid comes out blank/white here
        # and a mixed int/"—" column crashes pyarrow outright.
        st.markdown("#### Current totals")
        rows = "\n".join(
            f"| {m.label} | {fmt_preview(m.current) if m.current is not None else '—'} |"
            for m in summary.totals
        )
        st.markdown(f"| Metric | Value |\n|---|---:|\n{rows}")

    if summary.breakdowns:
        st.markdown("#### Detail — exactly which learning, language and company")
        for bd in summary.breakdowns:
            with st.expander(f"{bd.title}  ({len(bd.rows)} rows)"):
                head = "| " + " | ".join(bd.columns) + " |"
                sep = "|" + "|".join(["---"] * (len(bd.columns) - 1) + ["---:"]) + "|"
                body = "\n".join("| " + " | ".join(r) + " |" for r in bd.rows)
                st.markdown(f"{head}\n{sep}\n{body}")
                if bd.note:
                    st.caption(bd.note)

    st.markdown(
        "<div class='small-note' style='margin-top:1rem'>This is a <b>short summary</b>. For the "
        "full Analytics KPI pack — complete slides, per-country and per-company detail and the "
        "historic months — open the <b>Enablement KPIs</b> folder in SharePoint.</div>",
        unsafe_allow_html=True,
    )
    st.link_button(f"📁 {links.SHAREPOINT_LABEL}", links.SHAREPOINT_DECKS,
                   use_container_width=True)

    stamp = (summary.current_period or "report").replace(".", "_").replace("-", "_")
    d1, d2 = st.columns(2)
    with d1:
        try:
            st.download_button(
                "⬇ Download report (Word)", data=to_docx(summary),
                file_name=f"Analytics KPI report {stamp}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Word export unavailable ({exc}).")
    with d2:
        st.download_button(
            "⬇ Download report (Markdown)", data=to_markdown(summary),
            file_name=f"Analytics KPI report {stamp}.md",
            mime="text/markdown", use_container_width=True,
        )
    section_close()


# ── Floating "Ask the data" bubble, bottom-right on every page ──────────────────────────────
render_ask_widget("4_reports")
