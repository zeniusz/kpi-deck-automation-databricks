"""PART 2 — Update the presentation.

Runs COMPLETELY INDEPENDENTLY of the ingestion half: no Excel is required. It refreshes the deck
from WHATEVER IS ALREADY IN DATABRICKS.

Flow: detect that new data landed → refresh the dashboards (auto-filtered to the newest month) →
capture the tile images → update the tagged template's KPI numbers and chart images, keeping every
other bit of the template untouched → download.

Detection is informational only. It never blocks the refresh, because the deck must always be
re-buildable from the current state of Databricks (data may have been loaded by a notebook or the
Databricks UI, not by Part 1).
"""

from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chart_renderer import render_chart  # noqa: E402
from dashboard_capture import SessionMissingError, capture_dashboard_images  # noqa: E402
from databricks_client import (  # noqa: E402
    run_chart_queries,
    run_scalar_queries,
)
from kpis import (  # noqa: E402
    CHARTS,
    DASHBOARD_TILES,
    DATABRICKS_DASHBOARDS,
    LINK_KPI_FALLBACKS,
    LOCAL_RENDER_CHART_KEYS,
    REPORT_MONTH,
    SCALARS,
    TEMPLATE_MONTH,
)
from pptx_refresher import build_month_replacements, refresh_powerpoint  # noqa: E402
from ask_widget import render_ask_widget  # noqa: E402
import links  # noqa: E402
from change_summary import summarise, to_docx, to_markdown  # noqa: E402
from run_manifest import last_run, live_table_state  # noqa: E402
from session_status import launch_login, login_result, session_state, verify_session  # noqa: E402
from ui_theme import (  # noqa: E402
    CHART_DIR,
    INPUT_DIR,
    OUTPUT_DIR,
    SHAREPOINT_DIR,
    inject_css,
    deck_tags,
    file_row,
    fmt_preview,
    progress_ui,
    section_close,
    section_open,
    sidebar,
    title,
)

inject_css()
sidebar(
    "Service 2 · Presentation Update",
    [
        "Check what new data landed.",
        "Upload the tagged template deck.",
        "Refresh dashboards + capture the visuals.",
        "Update the deck and download it.",
    ],
)
title(
    "Presentation",
    "Update",
    "Detects new data in Databricks → refreshes the dashboards → updates the tagged template's "
    "numbers and chart images. Everything else in the deck is left exactly as it is.",
)

for key in ("output_bytes", "output_name", "stats", "saved_paths", "preview_values", "summary"):
    st.session_state.setdefault(key, None)


# ── Step 1: Detect new data ─────────────────────────────────────────────────────────────────────
section_open(1, "What's new in Databricks")
d1, d2 = st.columns([1.1, 2.9])
with d1:
    check_live = st.toggle(
        "Check Databricks live",
        value=False,
        help="Query row counts and the newest reporting month per table. Needs a running SQL "
        "warehouse (~30s if it must start).",
    )
with d2:
    prev = last_run()
    if prev:
        st.markdown(
            f"<div class='small-note'>Last load by this app: <b>{html.escape(prev.timestamp)}</b> · "
            f"{prev.total_rows:,} rows · months: <code>{html.escape(', '.join(prev.months) or '—')}</code><br>"
            f"tables: <code>{html.escape(', '.join(prev.tables) or '—')}</code></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='small-note'>No load recorded by this app yet — that's fine, the deck is "
            "built from whatever is already in Databricks.</div>",
            unsafe_allow_html=True,
        )

@st.cache_data(ttl=300, show_spinner="Querying Databricks…")
def _cached_table_state():
    """Cached for 5 minutes: toggling a switch must not re-query the warehouse."""
    return live_table_state()


if check_live:
    try:
        states = _cached_table_state()
        # Rendered as MARKDOWN, not st.dataframe: the data grid draws blank on this dark theme, and
        # a column mixing ints with an "—" placeholder crashes pyarrow outright.
        if not states:
            st.info("No tables reported — check the SQL warehouse is running.")
        else:
            body = "\n".join(
                f"| `{s.table}` | {'unavailable' if s.rows < 0 else f'{s.rows:,}'} | "
                f"{s.latest_month or '—'} | {s.months if s.months else '—'} |"
                for s in states
            )
            st.markdown(
                "| Table | Rows | Newest month | Periods loaded |\n"
                "|---|---:|---|---:|\n" + body
            )
        rc1, rc2 = st.columns([1.1, 2.9])
        with rc1:
            if st.button("🔄 Refresh now", use_container_width=True):
                _cached_table_state.clear()
                st.rerun()
        with rc2:
            st.markdown(
                "<div class='small-note'>Cached for 5 minutes. The dashboards and the live KPI "
                "queries both follow max(Reporting_Month), so <b>Newest month</b> is what lands in "
                "the deck. A “—” means that table has no reporting-month column, so it holds one "
                "current snapshot rather than a history.</div>",
                unsafe_allow_html=True,
            )
    except Exception as exc:  # noqa: BLE001 — detection is optional, never fatal
        st.warning(f"Could not read Databricks ({exc}). You can still refresh the deck.")
section_close()


# ── Step 2: Upload the tagged template ──────────────────────────────────────────────────────────
section_open(2, "Upload the tagged template deck")
st.caption("Last month's deck / the tagged template (.pptx)")
uploaded = st.file_uploader("PPTX", type=["pptx"], label_visibility="collapsed")
deck_chart_keys: set[str] = set()
if uploaded:
    file_row("📄", uploaded.name, f"{uploaded.size/1024/1024:.1f} MB · ready")
    try:
        kpi_keys = deck_tags(uploaded.getvalue(), "kpi")
        deck_chart_keys = set(deck_tags(uploaded.getvalue(), "chart"))
        n_tags = len(kpi_keys) + len(deck_chart_keys)
        if n_tags == 0:
            st.error(
                "⚠ This deck has **no `kpi:` / `chart:` tags**, so nothing would be updated. "
                "Upload the **tagged** template (e.g. `Revenue_template_tagged.pptx`), not the "
                "plain design file."
            )
        else:
            matched = sorted(deck_chart_keys & set(DASHBOARD_TILES))
            st.markdown(
                f"<div class='small-note ok'>✔ {n_tags} tagged placeholder(s): "
                f"{len(kpi_keys)} KPI, {len(deck_chart_keys)} chart "
                f"— {len(matched)} chart(s) map to a dashboard tile.</div>",
                unsafe_allow_html=True,
            )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not inspect the deck: {exc}")
section_close()


# ── Step 3: Refresh options ─────────────────────────────────────────────────────────────────────
section_open(3, "Refresh dashboards &amp; update the deck")
rc2, rc3 = st.columns([1.2, 2])
with rc2:
    use_screenshots = st.toggle(
        "Dashboard screenshots",
        value=False,
        help="Insert the exact Databricks tile images (auto-filtered to the newest month) instead "
        "of locally drawn charts. Sign in below — no terminal needed.",
    )
with rc3:
    st.markdown(
        "<div class='small-note'>Output month: <b>automatically detected from Databricks</b> · "
        f"images saved under <code>charts/output</code>.</div>",
        unsafe_allow_html=True,
    )

# Screenshots need a saved Databricks browser session. Handle that IN THE APP — no terminal.
if use_screenshots:
    sess = session_state()
    res = login_result()
    if res:
        # ok=None means the user simply closed the window — show that as a neutral note, not a red
        # error. A cancelled sign-in changed nothing.
        ok = res.get("ok")
        render = st.success if ok else (st.error if ok is False else st.info)
        render(res.get("message", ""))
    lc1, lc2 = st.columns([1.1, 2.9])
    with lc1:
        if st.button("🔐 Sign in to Databricks", use_container_width=True):
            ok, msg = launch_login()
            (st.info if ok else st.error)(msg)
            st.caption("Finish signing in, then press **Check session** — it saves itself.")
        if st.button("✓ Check session", use_container_width=True):
            with st.spinner("Opening a dashboard to test the session…"):
                ok, msg = verify_session()
            (st.success if ok else st.warning)(msg)
    with lc2:
        tone = "warn" if (not sess.exists or sess.looks_stale) else "ok"
        st.markdown(
            f"<div class='small-note'><span class='{tone}'>●</span> {html.escape(sess.message)}"
            "<br>Sign-in opens a real browser for SSO + MFA (it cannot be automated), but it "
            "saves the session automatically — you never need a terminal.</div>",
            unsafe_allow_html=True,
        )

run = st.button(
    "▶ Refresh dashboards + update presentation",
    type="primary",
    disabled=uploaded is None,
    use_container_width=True,
)
section_close()


if run and uploaded:
    step, log_ok, done = progress_ui()
    try:
        # ── KPI values ─────────────────────────────────────────────────────────────────────────
        step(0.10, "Loading KPI values…")
        # Re-assert the latest-only learning source before any KPI or screenshot is read. This
        # makes Presentation Update safe even when it runs independently of ingestion.
        from databricks_client import _connect
        from refresh_targets import update_learning_dashboard_sources, publish_related_dashboards

        conn = _connect()
        try:
            for line in update_learning_dashboard_sources(conn):
                log_ok(line)
        finally:
            conn.close()
        for line in publish_related_dashboards(
            {"atlas_training_completions", "beacon_training_completions"},
            refresh_caches=False,
        ):
            log_ok(line)

        query_warnings: list[str] = []
        scalar_values = run_scalar_queries(SCALARS, warnings=query_warnings)
        chart_data = run_chart_queries(CHARTS, warnings=query_warnings)
        for warning in query_warnings:
            st.warning(warning)
        log_ok("Loaded live KPI values from Databricks (newest reporting month).")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_chart_dir = CHART_DIR / timestamp

        # ── Dashboards → chart images ──────────────────────────────────────────────────────────
        if use_screenshots:
            # Only capture tiles this deck actually references, so a revenue deck opens 1 dashboard
            # instead of all 5.
            tiles_to_capture = {
                k: v for k, v in DASHBOARD_TILES.items()
                if k in deck_chart_keys and k not in LOCAL_RENDER_CHART_KEYS
            }
            n_dash = len({t["dashboard"] for t in tiles_to_capture.values()}) or 1
            step(
                0.30,
                f"Refreshing {n_dash} dashboard(s) and capturing {len(tiles_to_capture)} tile(s) "
                f"(~{20*n_dash}–{45*n_dash}s)…",
            )
            if not tiles_to_capture:
                log_ok("No dashboard tiles match this deck's chart tags — skipping capture.")
                chart_images, capture_warnings = {}, []
            else:
                chart_images, capture_warnings = capture_dashboard_images(
                    tiles_to_capture,
                    DATABRICKS_DASHBOARDS,
                    run_chart_dir / "screenshots",
                    on_progress=lambda completed, total, key: step(
                        0.30 + 0.45 * completed / max(total, 1),
                        f"Captured {completed}/{total}: {key}",
                    ),
                )
            for warning in capture_warnings:
                st.warning(warning)
            log_ok(f"Captured {len(chart_images)} dashboard tile(s) at the newest month.")
            if not chart_images and tiles_to_capture:
                st.info(
                    "No tiles captured — the template's existing images are left as-is. "
                    "Check DASHBOARD_TILES in kpis.py."
                )
            # Only keys that were supposed to be screenshotted (in tiles_to_capture) are required.
            # Keys in LOCAL_RENDER_CHART_KEYS and keys absent from DASHBOARD_TILES are rendered
            # locally later and must NOT be counted as missing captures.
            missing_captures = set(tiles_to_capture) - set(chart_images)
            if missing_captures:
                raise RuntimeError(
                    "Deck was not generated because every chart must be captured. Missing exact "
                    "Databricks screenshot(s): " + ", ".join(sorted(missing_captures))
                )
            log_ok(f"Captured all {len(chart_images)} exact dashboard visual(s).")

            # Wide Lakeview donut exports do not fit small PowerPoint cards reliably. Render the
            # configured exceptions from the exact same live SQL data and merge them with the
            # dashboard captures. This keeps the full donut visible without clipping or distortion.
            chart_specs = {chart["key"]: chart for chart in CHARTS}
            local_keys = LOCAL_RENDER_CHART_KEYS & set(deck_chart_keys)
            for key in sorted(local_keys):
                chart_images[key] = render_chart(
                    chart_specs[key], chart_data[key], run_chart_dir / "live_renders"
                )
            if local_keys:
                log_ok(
                    "Rendered full-card live chart(s): " + ", ".join(sorted(local_keys)) + "."
                )
        else:
            step(0.40, "Rendering charts locally…")
            chart_images = {
                chart["key"]: render_chart(chart, chart_data[chart["key"]], run_chart_dir)
                for chart in CHARTS
            }
            log_ok(f"Rendered {len(chart_images)} chart(s) locally.")

        # ── Update the deck ────────────────────────────────────────────────────────────────────
        step(0.80, "Updating the presentation (KPIs + chart images)…")
        source_month = str(scalar_values.get("source_month", "")).strip()
        month_replacements = (
            build_month_replacements(TEMPLATE_MONTH, source_month)
            if source_month and source_month != TEMPLATE_MONTH else {}
        )
        output_bytes, stats = refresh_powerpoint(
            uploaded.getvalue(),
            scalar_config=SCALARS,
            scalar_values=scalar_values,
            chart_images=chart_images,
            link_kpi_fallbacks=LINK_KPI_FALLBACKS,
            month_replacements=month_replacements,
        )

        step(0.95, "Saving the deck…")
        output_month = source_month or REPORT_MONTH
        output_name = f"Analytics KPIs {output_month}.pptx"
        input_path = INPUT_DIR / uploaded.name
        output_path = OUTPUT_DIR / output_name
        sharepoint_path = SHAREPOINT_DIR / output_name
        input_path.write_bytes(uploaded.getvalue())
        output_path.write_bytes(output_bytes)
        sharepoint_path.write_bytes(output_bytes)

        st.session_state.output_bytes = output_bytes
        st.session_state.output_name = output_name
        st.session_state.stats = stats
        st.session_state.saved_paths = {
            "input": str(input_path), "output": str(output_path),
            "sharepoint": str(sharepoint_path), "charts": str(run_chart_dir),
        }
        st.session_state.preview_values = scalar_values
        st.session_state.updated_kpi_keys = deck_tags(uploaded.getvalue(), "kpi")

        done("Done.")
        if not stats.get("kpi_updates") and not stats.get("chart_updates"):
            st.warning(
                "Ran, but 0 placeholders were updated — the uploaded deck likely has no "
                "`kpi:`/`chart:` tags. Upload the tagged template."
            )
        else:
            st.success("Presentation updated successfully.")
    except SessionMissingError as exc:
        st.error(
            f"Databricks login required for dashboard screenshots: {exc}\n\n"
            "Run `.venv/bin/python dashboard_login.py` in a terminal, sign in once, then retry — "
            "or turn off **Dashboard screenshots**."
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Update failed: {exc}")


# ── Results ─────────────────────────────────────────────────────────────────────────────────────
if st.session_state.stats:
    section_open(4, "Results")
    stats = st.session_state.stats
    cards = [
        ("KPI/text boxes", stats.get("kpi_updates", 0)),
        ("Charts/graphs", stats.get("chart_updates", 0)),
        ("Linked text", stats.get("linked_text_updates", 0)),
        ("Marker text", stats.get("marker_updates", 0)),
    ]
    for col, (card_title, value) in zip(st.columns(4), cards):
        with col:
            st.markdown(
                f"<div class='kpi-card'><div class='kpi-title'>{card_title}</div>"
                f"<div class='kpi-value'>{value}</div>"
                f"<div class='small-note'>updated in PowerPoint</div></div>",
                unsafe_allow_html=True,
            )
    paths = st.session_state.saved_paths or {}
    st.code(
        f"Input:      {paths.get('input')}\n"
        f"Output:     {paths.get('output')}\n"
        f"SharePoint: {paths.get('sharepoint')}\n"
        f"Charts:     {paths.get('charts')}"
    )
    st.caption("📤 The deck is ready in the hand-off folder — upload it to the SharePoint library:")
    st.link_button(f"📁 {links.SHAREPOINT_LABEL}", links.SHAREPOINT_DECKS,
                   use_container_width=True)
    section_close()

if st.session_state.preview_values:
    section_open(5, "Preview of selected KPIs")
    updated_keys = set(st.session_state.get("updated_kpi_keys") or [])
    if updated_keys & {"revenue_total", "revenue_biggest_country"}:
        preview_keys = ["revenue_biggest_country", "revenue_avg", "revenue_total", "revenue_month_label"]
    else:
        preview_keys = [
            "academy_total_learners", "db_participants",
            "community_total_registrations", "events_total_participants",
        ]
    titles = {item["key"]: item["title"] for item in SCALARS}
    for col, key in zip(st.columns(4), preview_keys):
        with col:
            st.markdown(
                f"<div class='kpi-card'><div class='kpi-title'>{titles.get(key, key)}</div>"
                f"<div class='kpi-value'>"
                f"{fmt_preview(st.session_state.preview_values.get(key, ''))}</div></div>",
                unsafe_allow_html=True,
            )
    section_close()

if st.session_state.output_bytes:
    section_open(6, "Download refreshed presentation")
    st.download_button(
        "⬇ Download Refreshed PowerPoint",
        data=st.session_state.output_bytes,
        file_name=st.session_state.output_name,
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        use_container_width=True,
    )
    section_close()

# ── Optional: a written summary of what actually changed, period over period ─────────────────────
section_open(7, "Optional: summary of what changed")
sc1, sc2 = st.columns([1.1, 2.9])
with sc1:
    build = st.button("📝 Build change summary", use_container_width=True)
with sc2:
    st.markdown(
        "<div class='small-note'>Compares the newest reporting month against the previous one "
        "(e.g. <i>Total revenue grew from 27.15B to 29.01B, +6.8%</i>) and lists the current "
        "programme totals. Download as Word or Markdown.</div>",
        unsafe_allow_html=True,
    )

if build:
    try:
        from databricks_client import _connect

        prev_run = last_run()
        loaded = [
            f"{w['filename']} → {w['table'].split('.')[-1]} ({w.get('rows', 0):,} rows)"
            for w in (prev_run.writes if prev_run else [])
        ]
        with st.spinner("Comparing periods in Databricks…"):
            conn = _connect()
            try:
                st.session_state.summary = summarise(conn, loaded=loaded)
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not build the summary: {exc}")

summary = st.session_state.get("summary")
if summary:
    st.markdown(f"**{html.escape(summary.headline())}**")
    for m in summary.movements:
        st.markdown(f"<div class='small-note'>• {html.escape(m.phrase())}</div>",
                    unsafe_allow_html=True)
    for n in summary.notes:
        st.markdown(f"<div class='small-note'>★ {html.escape(n)}</div>", unsafe_allow_html=True)
    if summary.totals:
        st.markdown("#### Current totals")
        rows = "\n".join(
            f"| {m.label} | {int(m.current):,} |" if m.current else f"| {m.label} | — |"
            for m in summary.totals
        )
        st.markdown(f"| Metric | Value |\n|---|---:|\n{rows}")
    stamp = (summary.current_period or "summary").replace(".", "_")
    d1, d2 = st.columns(2)
    with d1:
        try:
            st.download_button(
                "⬇ Download summary (Word)",
                data=to_docx(summary),
                file_name=f"Analytics KPI changes {stamp}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Word export unavailable ({exc}).")
    with d2:
        st.download_button(
            "⬇ Download summary (Markdown)",
            data=to_markdown(summary),
            file_name=f"Analytics KPI changes {stamp}.md",
            mime="text/markdown",
            use_container_width=True,
        )
section_close()


# ── Floating "Ask the data" bubble, bottom-right on every page ──────────────────────────────
render_ask_widget("2_update_presentation")
