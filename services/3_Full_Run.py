"""FULL RUN — both services in one click.

The two services stay independent (that is the architecture), but the normal monthly job is to do
both back-to-back: load the new sheets, then rebuild the deck from the data that just landed. This
page chains them in the correct order and shows one progress bar:

    Excel → Volume archive → clean/derive → append rows
      → refresh Genie + dashboards
        → capture the tiles at the newest month
          → update the tagged deck
            → download deck + change summary

Ordering matters and is enforced here: the deck must be built AFTER the load, otherwise the
dashboards and the live KPI queries (which follow max(Reporting_Month)) would still show last
month. Enrichment sources are also loaded before the tables that read from them.
"""

from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ask_widget import render_ask_widget  # noqa: E402
import links  # noqa: E402
from change_summary import summarise, to_docx, to_markdown  # noqa: E402
from chart_renderer import render_chart  # noqa: E402
from dashboard_capture import SessionMissingError, capture_dashboard_images  # noqa: E402
from databricks_client import (  # noqa: E402
    run_chart_queries,
    run_scalar_queries,
)
from derivation import order_index  # noqa: E402
from genie_chat import clear_cache  # noqa: E402
from excel_ingestion import (  # noqa: E402
    build_plan, enrich_run_listing_emails, execute_write, prepare_run_frames,
)
from kpis import (  # noqa: E402
    CHARTS,
    DASHBOARD_TILES,
    DATABRICKS_DASHBOARDS,
    LINK_KPI_FALLBACKS,
    REPORT_MONTH,
    SCALARS,
    TEMPLATE_MONTH,
)
from pptx_refresher import build_month_replacements, refresh_powerpoint  # noqa: E402
from refresh_targets import refresh_all  # noqa: E402
from run_manifest import WriteRecord, record_run  # noqa: E402
from session_status import launch_login, login_result, session_state, verify_session  # noqa: E402
from ui_theme import (  # noqa: E402
    CHART_DIR,
    EXCEL_DIR,
    INPUT_DIR,
    OUTPUT_DIR,
    SHAREPOINT_DIR,
    deck_tags,
    file_row,
    inject_css,
    progress_ui,
    section_close,
    section_open,
    sidebar,
    title,
)

inject_css()
sidebar(
    "Full run · both services",
    [
        "Upload the Excel sheet(s) + the tagged deck.",
        "Load into Databricks (archive → clean → append).",
        "Refresh Genie + dashboards.",
        "Capture the visuals and update the deck.",
        "Download the deck + the change summary.",
    ],
)
title(
    "Full",
    "Monthly Run",
    "Both services in one click, in the right order: load the new sheets, then rebuild the deck "
    "from the data that just landed.",
)

for key in ("fr_output", "fr_name", "fr_stats", "fr_paths", "fr_summary"):
    st.session_state.setdefault(key, None)


# ── Step 1: Everything you need to upload ───────────────────────────────────────────────────────
section_open(1, "Upload the new sheets and the deck")
u1, u2 = st.columns(2)
with u1:
    st.caption("New Excel sheet(s) — leave empty to only refresh the deck")
    excel_files = st.file_uploader(
        "XLSX", type=["xlsx", "xls"], accept_multiple_files=True, label_visibility="collapsed"
    )
    for xf in excel_files or []:
        file_row("📊", xf.name, f"{xf.size/1024:.0f} KB · ready")
with u2:
    st.caption("The tagged template deck (.pptx) — leave empty to only load data")
    uploaded = st.file_uploader("PPTX", type=["pptx"], label_visibility="collapsed")
    if uploaded:
        file_row("📄", uploaded.name, f"{uploaded.size/1024/1024:.1f} MB · ready")
section_close()


# ── Step 2: Plans + options ─────────────────────────────────────────────────────────────────────
section_open(2, "What will happen")
plans: list = []

# Same month fallback as Service 1: an APPEND with no reporting month can't be filtered on a
# dashboard and can't be de-duplicated on a re-run, so ask rather than write untagged rows.
month_override: str | None = None
if excel_files:
    from excel_ingestion import infer_reporting_month, month_options, needs_manual_month

    needs_month = needs_manual_month([xf.name for xf in excel_files])
    found = sorted({m for m in (infer_reporting_month(x.name) for x in excel_files) if m})
    if needs_month:
        st.markdown(
            "<div class='small-note'>⚠️ <b>No reporting month in the filename</b> for "
            f"<code>{html.escape(', '.join(needs_month))}</code> — pick the period these rows "
            "belong to. Files that carry their own month keep it.</div>",
            unsafe_allow_html=True,
        )
    month_override = st.columns([1, 3])[0].selectbox(
        "Reporting month", month_options(found[-1] if found else None), index=0
    )

    for xf in excel_files:
        tmp = EXCEL_DIR / xf.name
        tmp.write_bytes(xf.getvalue())
        try:
            plans.append((tmp, build_plan(tmp, month_fallback=month_override)))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not read {xf.name}: {exc}")
    plans.sort(key=lambda tp: order_index(tp[1].route.table if tp[1].route else None))
    prepared_frames, cross_file_removed = prepare_run_frames(plans)
    for plan_pos, (_, plan) in enumerate(plans):
        mode = plan.route.mode.value.upper().replace("_", " ") if plan.route else "NO MATCH"
        final_rows = len(prepared_frames[plan_pos]) if plan.route else plan.rows_in
        cross_note = cross_file_removed.get(plan_pos, 0)
        st.markdown(
            f"<div class='plan-box'><b>{html.escape(plan.filename)}</b> "
            f"<span class='mode-tag'>{mode}</span><br>"
            f"🎯 {html.escape(plan.full_table or ('— archived to the Volume only' if plan.route and plan.route.mode.value == 'archive_only' else '— no matching rule'))}"
            + (f" &nbsp;·&nbsp; 🗓️ {plan.reporting_month}" if plan.reporting_month else "")
            + f"<br>🔢 {plan.rows_in} → {final_rows} rows"
            + (f"<br><span class='small-note'>• {cross_note} additional cross-file duplicate(s) "
               "removed by email.</span>" if cross_note else "")
            + ("<br><span class='small-note'>• File-preview rows are not the member KPI. The write "
               "leaves Community email cells exactly as uploaded—including blanks—and every row "
               "counts as a member. Guild remains separate.</span>"
               if plan.route and plan.route.cleaning in {"teams_listing", "guild"} else "")
            + "</div>",
            unsafe_allow_html=True,
        )

deck_chart_keys: set[str] = set()
if uploaded:
    try:
        n_kpi = len(deck_tags(uploaded.getvalue(), "kpi"))
        deck_chart_keys = set(deck_tags(uploaded.getvalue(), "chart"))
        if n_kpi + len(deck_chart_keys) == 0:
            st.error(
                "⚠ This deck has **no `kpi:` / `chart:` tags** — nothing would be updated. "
                "Upload the **tagged** template."
            )
        else:
            matched = len(deck_chart_keys & set(DASHBOARD_TILES))
            st.markdown(
                f"<div class='small-note ok'>✔ Deck has {n_kpi} KPI and "
                f"{len(deck_chart_keys)} chart tag(s) — {matched} map to a dashboard tile.</div>",
                unsafe_allow_html=True,
            )
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not inspect the deck: {exc}")

o1, o3 = st.columns(2)
with o1:
    write_to_dbx = st.toggle("Write to Databricks", value=False,
                             help="OFF = preview the load only. ON = archive + append for real.")
with o3:
    use_screenshots = st.toggle("Dashboard screenshots", value=False,
                                help="Capture the real Databricks tiles (needs a sign-in).")

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
        if st.button("✓ Check session", use_container_width=True):
            with st.spinner("Testing the saved session…"):
                ok, msg = verify_session()
            (st.success if ok else st.warning)(msg)
    with lc2:
        tone = "warn" if (not sess.exists or sess.looks_stale) else "ok"
        st.markdown(
            f"<div class='small-note'><span class='{tone}'>●</span> {html.escape(sess.message)}</div>",
            unsafe_allow_html=True,
        )
section_close()


# ── Step 3: Run everything ──────────────────────────────────────────────────────────────────────
section_open(3, "Run the full monthly job")
can_run = bool(plans or uploaded)
run = st.button("▶ Run everything", type="primary", disabled=not can_run,
                use_container_width=True)
if not can_run:
    st.caption("Upload at least an Excel sheet or a deck to enable the run.")
section_close()

if run:
    step, log_ok, done = progress_ui()
    conn = None
    try:
        from databricks_client import _connect

        step(0.03, "Connecting to Databricks (the warehouse may auto-start, ~30s)…")
        conn = _connect()
        log_ok("Connected to Databricks.")

        # ── 1. LOAD ─────────────────────────────────────────────────────────────────────────────
        records: list[WriteRecord] = []
        skipped: list[str] = []
        if plans and write_to_dbx and conn is not None:
            total = len(plans)
            # One (table, month) is cleared once per run — see the note in services/1.
            cleared: set[tuple[str, str]] = set()
            prepared, email_filled, post_fill_removed = enrich_run_listing_emails(
                prepared_frames, plans, conn
            )
            for i, (tmp, plan) in enumerate(plans, 1):
                if not plan.route:
                    msg = f"Skipped {plan.filename}: no matching table rule."
                    log_ok(msg)
                    skipped.append(msg)
                    continue
                step(0.05 + 0.30 * (i - 1) / total,
                     f"Loading {plan.filename} → {plan.route.table}…")
                facts: dict = {}
                if email_filled.get(i - 1):
                    log_ok(f"Filled {email_filled[i - 1]} missing email(s) by unique name match.")
                if post_fill_removed.get(i - 1):
                    log_ok(f"Removed {post_fill_removed[i - 1]} newly revealed duplicate(s).")
                removed = cross_file_removed.get(i - 1, 0)
                if removed:
                    log_ok(f"Removed {removed} cross-file duplicate row(s) by email for this month.")
                for line in execute_write(
                    plan, tmp, conn, result=facts, written=cleared,
                    frame_override=prepared[i - 1],
                ):
                    log_ok(line)
                if facts:
                    facts.pop("skipped", None)
                    records.append(WriteRecord(**facts))
            if records:
                rec = record_run(records, warnings=skipped)
                log_ok(f"Recorded the load ({rec.total_rows:,} rows).")
        elif plans:
            for _, plan in plans:
                log_ok(f"Preview only — {plan.filename} would go to {plan.full_table}.")

        # ── 2. COMBINE the learning tables, then REFRESH Genie + dashboards ─────────────────────
        # Only write the metadata when data was actually loaded: without a period there is nothing
        # new to stamp, and re-running the combine would just redo last month.
        if conn is not None:
            loaded_period = next(
                (p.reporting_month for _, p in plans if p.reporting_month), None
            ) if (plans and write_to_dbx) else None
            step(0.38, "Combining learning tables, refreshing Genie spaces and dashboards…")
            guild_loaded = bool(plans) and write_to_dbx and any(
                p.route and p.route.table == "lakehouse_guild" for _, p in plans
            )
            loaded_tables = {p.route.table for _, p in plans if p.route}
            for line in refresh_all(
                conn, period=loaded_period, guild_loaded=guild_loaded,
                loaded_tables=loaded_tables,
            ).lines:
                log_ok(line)
            clear_cache()  # chat answers cached before this load would now be stale

        # ── 3. DECK ─────────────────────────────────────────────────────────────────────────────
        if uploaded:
            step(0.45, "Loading KPI values…")
            # A deck-only full run must still refresh the latest-only learning semantic source.
            from refresh_targets import update_learning_dashboard_sources, publish_related_dashboards

            for line in update_learning_dashboard_sources(conn):
                log_ok(line)
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

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_chart_dir = CHART_DIR / timestamp

            if use_screenshots:
                tiles = {
                    k: v for k, v in DASHBOARD_TILES.items()
                    if k in deck_chart_keys
                }
                n_dash = len({t["dashboard"] for t in tiles.values()}) or 1
                step(0.55, f"Capturing {len(tiles)} tile(s) from {n_dash} dashboard(s)…")
                if tiles:
                    chart_images, warns = capture_dashboard_images(
                        tiles,
                        DATABRICKS_DASHBOARDS,
                        run_chart_dir / "screenshots",
                        on_progress=lambda completed, total, key: step(
                            0.55 + 0.20 * completed / max(total, 1),
                            f"Captured {completed}/{total}: {key}",
                        ),
                    )
                    for w in warns:
                        st.warning(w)
                else:
                    chart_images = {}
                    log_ok("No dashboard tiles match this deck's chart tags.")
                missing_captures = set(tiles) - set(chart_images)
                if missing_captures:
                    raise RuntimeError(
                        "Deck was not generated because every chart must be captured. Missing exact "
                        "Databricks screenshot(s): " + ", ".join(sorted(missing_captures))
                    )
                log_ok(f"Captured {len(chart_images)} exact dashboard tile(s) at the newest month.")
            else:
                step(0.60, "Rendering charts locally…")
                chart_images = {
                    c["key"]: render_chart(c, chart_data[c["key"]], run_chart_dir) for c in CHARTS
                }

            step(0.80, "Updating the presentation…")
            source_month = str(scalar_values.get("source_month", "")).strip()
            month_replacements = (
                build_month_replacements(TEMPLATE_MONTH, source_month)
                if source_month and source_month != TEMPLATE_MONTH else {}
            )
            output_bytes, stats = refresh_powerpoint(
                uploaded.getvalue(), scalar_config=SCALARS, scalar_values=scalar_values,
                chart_images=chart_images, link_kpi_fallbacks=LINK_KPI_FALLBACKS,
                month_replacements=month_replacements,
            )
            output_name = f"Analytics KPIs {source_month or REPORT_MONTH}.pptx"
            (INPUT_DIR / uploaded.name).write_bytes(uploaded.getvalue())
            (OUTPUT_DIR / output_name).write_bytes(output_bytes)
            (SHAREPOINT_DIR / output_name).write_bytes(output_bytes)
            st.session_state.fr_output = output_bytes
            st.session_state.fr_name = output_name
            st.session_state.fr_stats = stats
            st.session_state.fr_paths = {
                "output": str(OUTPUT_DIR / output_name),
                "sharepoint": str(SHAREPOINT_DIR / output_name),
                "charts": str(run_chart_dir),
            }
            log_ok(f"Deck updated: {stats.get('kpi_updates', 0)} KPI, "
                   f"{stats.get('chart_updates', 0)} chart placeholder(s).")

        # ── 4. SUMMARY ──────────────────────────────────────────────────────────────────────────
        step(0.93, "Building the change summary…")
        try:
            if conn is None:
                from databricks_client import _connect

                conn = _connect()
            loaded = [
                f"{w.filename} → {w.table.split('.')[-1]} ({w.rows:,} rows)" for w in records
            ]
            st.session_state.fr_summary = summarise(conn, loaded=loaded)
            log_ok("Change summary ready.")
        except Exception as exc:  # noqa: BLE001 — summary is a bonus, never fail the run for it
            log_ok(f"Skipped the summary ({str(exc)[:80]}).")

        done("Full run complete.")
        st.success("Everything finished — download below.")
    except SessionMissingError as exc:
        st.error(
            f"Databricks sign-in required for screenshots: {exc}\n\n"
            "Use **🔐 Sign in to Databricks** above, or turn off Dashboard screenshots."
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Full run failed: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# ── Results ─────────────────────────────────────────────────────────────────────────────────────
if st.session_state.fr_stats or st.session_state.fr_summary:
    section_open(4, "Download")
    d1, d2, d3 = st.columns(3)
    with d1:
        if st.session_state.fr_output:
            st.download_button(
                "⬇ Refreshed PowerPoint", data=st.session_state.fr_output,
                file_name=st.session_state.fr_name,
                mime="application/vnd.openxmlformats-officedocument."
                     "presentationml.presentation",
                use_container_width=True,
            )
    summary = st.session_state.fr_summary
    stamp = (summary.current_period or "summary").replace(".", "_") if summary else "summary"
    with d2:
        if summary:
            try:
                st.download_button(
                    "⬇ Summary (Word)", data=to_docx(summary),
                    file_name=f"Analytics KPI changes {stamp}.docx",
                    mime="application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document",
                    use_container_width=True,
                )
            except Exception as exc:  # noqa: BLE001
                st.caption(f"Word export unavailable ({exc}).")
    with d3:
        if summary:
            st.download_button(
                "⬇ Summary (Markdown)", data=to_markdown(summary),
                file_name=f"Analytics KPI changes {stamp}.md",
                mime="text/markdown", use_container_width=True,
            )
    if summary:
        st.markdown(f"**{html.escape(summary.headline())}**")
        for m in summary.movements[:10]:
            st.markdown(f"<div class='small-note'>• {html.escape(m.phrase())}</div>",
                        unsafe_allow_html=True)
        for n in summary.notes[:6]:
            st.markdown(f"<div class='small-note'>★ {html.escape(n)}</div>",
                        unsafe_allow_html=True)
    st.link_button(f"📁 Upload the deck to {links.SHAREPOINT_LABEL}", links.SHAREPOINT_DECKS,
                   use_container_width=True)
    paths = st.session_state.fr_paths or {}
    if paths:
        st.code("\n".join(f"{k.title():11} {v}" for k, v in paths.items()))
    section_close()


# ── Floating "Ask the data" bubble, bottom-right on every page ──────────────────────────────
render_ask_widget("3_full_run")
