"""PART 1 — Load Excel into Databricks.

Runs COMPLETELY INDEPENDENTLY of the presentation half: no deck is required, nothing here touches
PowerPoint. Upload one or more Excel files → each is archived into its UC Volume (never deleted; the same
filename replaces its own archived copy) → cleaned/derived per the ingestion guide → rows APPENDED to the target
bronze table.

What it records: every write is written to .run_manifest.json (run_manifest.py) so Part 2 can detect
that new data landed even in a different session, days later.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from content_routing import suggest_tables, taxonomy_breakdown  # noqa: E402
from derivation import order_index  # noqa: E402
from genie_chat import clear_cache  # noqa: E402
from excel_ingestion import (  # noqa: E402
    build_plan, enrich_run_listing_emails, execute_write, prepare_run_frames, read_excel_cached,
)
from refresh_targets import refresh_all  # noqa: E402
from run_manifest import WriteRecord, last_run, record_run  # noqa: E402
from ask_widget import render_ask_widget  # noqa: E402
from ui_theme import (  # noqa: E402
    EXCEL_DIR,
    file_row,
    flag,
    progress_ui,
    section_close,
    section_open,
    sidebar,
    title,
)
from ui_theme import inject_css  # noqa: E402

inject_css()
sidebar(
    "Service 1 · Files → Databricks",
    [
        "Upload the new Excel sheet(s).",
        "Check the routing plan (table + write mode).",
        "Archive into the UC Volume + append the rows.",
        "Genie spaces + dashboards refreshed at the end.",
    ],
)
title(
    "Files →",
    "Databricks",
    "Excel → UC Volume archive → clean &amp; derive the missing columns → <b>append</b> rows to the "
    "bronze table → refresh Genie + dashboards. Archived files are never deleted.",
)

st.session_state.setdefault("excel_plans", None)



prev = last_run()
if prev:
    st.markdown(
        f"<div class='small-note'>Last load: <b>{html.escape(prev.timestamp)}</b> · "
        f"{len(prev.writes)} file(s) · {prev.total_rows:,} rows · "
        f"tables: <code>{html.escape(', '.join(prev.tables) or '—')}</code></div>",
        unsafe_allow_html=True,
    )


# ── Step 1: Upload the Excel files ──────────────────────────────────────────────────────────────
section_open(1, "Upload the new Excel sheet(s)")
st.caption("Community, trainings, events, guild, revenue… — multiple files allowed")
# The raw LMS exports are named `<id>_export_eLearning…` on BOTH platforms, so the word "eLearning"
# cannot tell them apart — only the leading id can. Worth stating, because renaming a file or a new
# course id appearing is exactly when a load would silently go to the wrong platform's table.
st.markdown(
    "<div class='small-note'>Raw LMS exports are recognised by their <b>id prefix</b>: "
    "<code>100200_…</code> → <b>Beacon</b>, <code>300400_…</code> → <b>Atlas</b>. "
    "Both platforms name their files <code>…_export_eLearning…</code>, so the id is the only thing "
    "that tells them apart — keep it in the filename. <code>Teil</code>/<code>Deutsch</code> marks "
    "the German version. The <code>YYYYMMDD</code> in the name is the <b>export date</b>, not the "
    "reporting period — you run the export in August for the July period — so the "
    "<b>reporting month you pick below wins</b>.</div>",
    unsafe_allow_html=True,
)
excel_files = st.file_uploader(
    "XLSX", type=["xlsx", "xls"], accept_multiple_files=True, label_visibility="collapsed"
)
for xf in excel_files or []:
    file_row("📊", xf.name, f"{xf.size/1024:.0f} KB · ready")

# ── Reporting month ─────────────────────────────────────────────────────────────────────────────
# Real Beacon/Atlas/PlanTrack exports often carry NO date in the filename ("Beacon_Data_Awareness.xlsx"),
# and an APPEND with no month is genuinely unsafe: the rows land untagged, dashboards can't filter
# them, and the dedup guard has nothing to match, so re-uploading duplicates them. So when any file
# needs a month and none can be inferred, ask here rather than writing untagged rows.
month_override: str | None = None
if excel_files:
    from excel_ingestion import infer_reporting_month, month_options, needs_manual_month

    needs_month = needs_manual_month([xf.name for xf in excel_files])
    inferred = {xf.name: infer_reporting_month(xf.name) for xf in excel_files}
    found = sorted({m for m in inferred.values() if m})
    if needs_month:
        st.markdown(
            "<div class='small-note'>⚠️ <b>No reporting month in the filename</b> for "
            f"<code>{html.escape(', '.join(needs_month))}</code>. Pick the month these rows "
            "belong to — appended rows are tagged with it, and re-running the same month "
            "replaces it instead of duplicating.</div>",
            unsafe_allow_html=True,
        )
        # GUIDE (⚠️ CRITICAL: Guild Reporting_Month Rules): the month is the DATA PERIOD, not today's.
        # The guide exists because a June snapshot was tagged 2026.08 and the history was destroyed.
        # The default is therefore LAST month, and this states the rule where the choice is made.
        st.markdown(
            "<div class='small-note' style='margin-top:-.35rem'>Use the month the data is "
            "<b>a snapshot of</b>, not today's month — a June export tagged as August overwrites "
            "August and loses June. Default below is last month for that reason.</div>",
            unsafe_allow_html=True,
        )
    mc1, mc2 = st.columns([1, 3])
    with mc1:
        month_override = st.selectbox(
            "Reporting month", month_options(found[-1] if found else None),
            index=0,
            help="The period the data is a SNAPSHOT OF — not today's month. Applied only to files "
                 "with no month in their name; files that carry their own keep it.",
        )
    with mc2:
        # Show WHY a file uses the picked month. An LMS filename contains a date, so "uses the picked
        # month" alone looked like the app had ignored it — say that the date is the export timestamp.
        from excel_ingestion import export_timestamp_month

        bits = []
        for n, m in inferred.items():
            if m:
                bits.append(f"<code>{html.escape(n)}</code> → <b>{m}</b> (from the name)")
            else:
                stamp = export_timestamp_month(n)
                why = (f" — its <code>{stamp.replace('.', '')}…</code> is the <i>export date</i>, "
                       "not the period" if stamp else "")
                bits.append(f"<code>{html.escape(n)}</code> → <b>{month_override}</b>{why}")
        st.markdown(f"<div class='small-note'>{' · '.join(bits)}</div>", unsafe_allow_html=True)

    # Re-running a month REPLACES it. That is what makes a re-run safe, and also what destroys a
    # snapshot if the month is wrong — so show which periods are already in each target table before
    # anything is written. Cheap, cached, and read-only.
    from month_check import existing_periods_note

    note = existing_periods_note(
        [(xf.name, inferred.get(xf.name) or month_override) for xf in excel_files]
    )
    if note:
        st.markdown(note, unsafe_allow_html=True)
section_close()


# ── Step 2: The plan ────────────────────────────────────────────────────────────────────────────
section_open(2, "Routing plan &amp; write mode")
oc1, oc2 = st.columns([1.1, 2.9])
with oc1:
    write_to_dbx = st.toggle(
        "Write to Databricks",
        value=False,
        help="OFF = preview only (nothing is written). ON = archive to the Volume and write the "
        "bronze table. Needs write grants and a running SQL warehouse.",
    )
with oc2:
    st.markdown(
        "<div class='small-note'>Each file is routed to its bronze table with the correct mode "
        "(replace / append / new-table) from the <i>Data Ingestion Guide</i>. "
        "<b>Nothing is written unless the toggle is on.</b></div>",
        unsafe_allow_html=True,
    )

plans: list = []
if excel_files:
    for xf in excel_files:
        tmp = EXCEL_DIR / xf.name
        tmp.write_bytes(xf.getvalue())
        try:
            # The picked month is only a FALLBACK: build_plan prefers a month found in the
            # filename, so a correctly-named file always keeps its own period.
            plans.append((tmp, build_plan(tmp, month_fallback=month_override)))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not read {xf.name}: {exc}")

    # Enrichment sources must be loaded BEFORE the tables that read from them: analytics_community
    # feeds Country/Job_Title into teams_listing and guild, and the event tables feed
    # Events_Attended into guild. Ordering a multi-file upload is not optional.
    plans.sort(key=lambda tp: order_index(tp[1].route.table if tp[1].route else None))
    # Build the same run-level view used by the writer now, so the preview does not misleadingly
    # show only each workbook's internal dedup count (e.g. 200 when 6 more overlap another file).
    prepared_frames, cross_file_removed = prepare_run_frames(plans)
    if len(plans) > 1:
        st.markdown(
            "<div class='small-note'>Load order (enrichment sources first): "
            + " → ".join(f"<code>{html.escape(p.route.table)}</code>" if p.route
                         else f"<code>{html.escape(p.filename)}</code>" for _, p in plans)
            + "</div>",
            unsafe_allow_html=True,
        )

    for plan_pos, (tmp, plan) in enumerate(plans):
        mode = plan.route.mode.value.upper().replace("_", " ") if plan.route else "NO MATCH"
        # ARCHIVE_ONLY files (the derived Unregistered_Members report) legitimately have no table —
        # distinguish that from a file we simply could not route.
        if plan.route and plan.route.mode.value == "archive_only":
            target = "— archived to the Volume only (derived report, no table)"
        else:
            target = plan.full_table or "— (no matching table rule)"
        final_rows = len(prepared_frames[plan_pos]) if plan.route else plan.rows_in
        rows = f"{plan.rows_in} → {final_rows}" if plan.route else str(plan.rows_in)
        body = (
            f"<div class='plan-box'><b>{html.escape(plan.filename)}</b> "
            f"<span class='mode-tag'>{mode}</span><br>"
            f"🎯 <b>{html.escape(target)}</b><br>"
            f"🗄️ archive → <code>{html.escape(plan.volume_target or '—')}</code><br>"
            f"🔢 rows: {rows}"
            + (f" &nbsp;·&nbsp; 🗓️ {plan.reporting_month}" if plan.reporting_month else "")
        )
        if plan.route and plan.route.note:
            body += f"<br><span class='small-note'>ℹ️ {html.escape(plan.route.note)}</span>"
        for w in plan.warnings:
            body += f"<br><span class='small-note'>• {html.escape(w)}</span>"
        if plan.route and plan.route.cleaning in {"teams_listing", "guild"}:
            body += (
                "<br><span class='small-note'>• This is the file-cleaning preview, not the member "
                "KPI. Community email cells are left exactly as uploaded—including blanks—and every "
                "row counts as a member. Guild remains separate.</span>"
            )
        body += "</div>"
        st.markdown(body, unsafe_allow_html=True)

        # ── The filename did not match a rule: look INSIDE the file and let the user choose ──────
        if not plan.route:
            try:
                df_peek = read_excel_cached(tmp)
                sugg = suggest_tables(df_peek, plan.filename)
            except Exception as exc:  # noqa: BLE001
                sugg, df_peek = [], None
                st.warning(f"Could not inspect {plan.filename}: {exc}")
            if sugg:
                st.markdown(
                    "<div class='small-note'>The filename didn't match a rule, but the "
                    "<b>contents</b> suggest these targets — pick one:</div>",
                    unsafe_allow_html=True,
                )
                opts = [
                    f"{s.table}  ·  {s.mode.value.replace('_', ' ')}  ·  {s.confidence}% — {s.reason}"
                    for s in sugg
                ] + ["(skip this file)"]
                picked = st.radio(
                    f"Target table for {plan.filename}", opts, index=0,
                    key=f"route_{plan.filename}", label_visibility="collapsed",
                )
                if not picked.startswith("(skip"):
                    chosen = sugg[opts.index(picked)]
                    st.session_state.setdefault("manual_routes", {})[plan.filename] = chosen.table
                    st.markdown(
                        f"<div class='small-note ok'>✔ Will write to "
                        f"<code>{html.escape(chosen.full_table)}</code></div>",
                        unsafe_allow_html=True,
                    )
            elif df_peek is not None:
                st.markdown(
                    "<div class='small-note warn'>Nothing in the contents matched a known table "
                    "shape either — rename the file to include a programme keyword "
                    "(community / event / guild / atlas / beacon / plantrack / registration).</div>",
                    unsafe_allow_html=True,
                )

        # ── Show WHICH platform / course / language / status this file actually contains ─────────
        try:
            df_full = read_excel_cached(tmp)
            bd = taxonomy_breakdown(df_full, plan.filename)
        except Exception:  # noqa: BLE001
            bd = None
        if bd is not None and len(bd):
            with st.expander(
                f"📚 What this file contains — platform, course, language ({len(bd)} groups)"
            ):
                cols = list(bd.columns)
                head = "| " + " | ".join(cols) + " |"
                sep = "|" + "|".join(["---"] * (len(cols) - 1) + ["---:"]) + "|"
                rows_md = "\n".join(
                    "| " + " | ".join(str(v) for v in r) + " |"
                    for r in bd.itertuples(index=False)
                )
                st.markdown(f"{head}\n{sep}\n{rows_md}")
                st.caption(
                    "Derived from the file's own columns (or its name). Course names are "
                    "normalised to each platform's canonical spelling, and en-dash/hyphen "
                    "variants are collapsed so the same course isn't counted twice."
                )
    st.session_state.excel_plans = plans

    # Surface the audited landmines for the specific tables in play (see the data-quality memo).
    targeted = {p.route.table for _, p in plans if p.route}
    if "analytics_teams_listing" in targeted:
        flag(
            "ℹ <b>Month format.</b> <code>analytics_teams_listing</code> stores months with a "
            "<b>hyphen</b> (<code>2026-06</code>) unlike every other table. Per your decision the "
            "writer reads the table's existing format and matches it, so no filter will split."
        )
    if targeted & {"atlas_training_completions", "beacon_training_completions"}:
        flag(
            "ℹ <b>Period column will be added.</b> The completions tables have no "
            "<code>reporting_month</code>; on write it is added and the existing rows are "
            "backfilled as <code>2026.06</code>, so appends stay filterable and re-runnable."
        )
else:
    st.caption("Upload Excel sheets above to see the load plan.")
section_close()


# ── Step 3: Run ─────────────────────────────────────────────────────────────────────────────────
section_open(3, "Load into Databricks")
run = st.button(
    "▶ Archive + load into Databricks",
    type="primary",
    disabled=not plans,
    use_container_width=True,
)
section_close()

if run and plans:
    step, log_ok, done = progress_ui()
    try:
        if not write_to_dbx:
            for _, plan in plans:
                log_ok(f"Preview only — {plan.filename} would go to {plan.full_table}.")
            done("Preview complete (nothing written).")
            st.info("**Write to Databricks** is off, so nothing was written. Turn it on to load.")
        else:
            from databricks_client import _connect

            step(0.05, "Connecting to Databricks (the warehouse may auto-start, ~30s)…")
            conn = _connect()
            records: list[WriteRecord] = []
            skipped: list[str] = []
            try:
                total = len(plans)
                # Shared across every file in this run: whichever file reaches a (table, month)
                # first clears that month, the rest append alongside it. Without this, two exports
                # for the same table and month delete each other's rows.
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
                    step(
                        0.05 + 0.90 * (i - 1) / total,
                        f"Writing {plan.filename} → {plan.route.table} ({plan.route.mode.value})…",
                    )
                    facts: dict = {}
                    if email_filled.get(i - 1):
                        log_ok(
                            f"Filled {email_filled[i - 1]} missing email(s) from Databricks by "
                            "an unambiguous full-name match."
                        )
                    if post_fill_removed.get(i - 1):
                        log_ok(
                            f"Removed {post_fill_removed[i - 1]} duplicate(s) revealed by the "
                            "recovered email(s)."
                        )
                    for line in execute_write(
                        plan, tmp, conn, result=facts, written=cleared,
                        frame_override=prepared[i - 1],
                    ):
                        log_ok(line)
                    if facts:
                        facts.pop("skipped", None)  # not part of the manifest record
                        records.append(WriteRecord(**facts))

                # ── Final step: rebuild learnings, refresh Genie + dashboards ────────────────
                # Use the month picker value as the period — it's always set and is the right
                # period regardless of whether learning files were in this batch.
                loaded_period = month_override or next(
                    (p.reporting_month for _, p in plans if p.reporting_month), None
                )
                step(0.95, "Rebuilding learnings tables, refreshing Genie spaces and dashboards…")
                guild_loaded = any(
                    p.route and p.route.table == "lakehouse_guild" for _, p in plans
                )
                loaded_tables = {p.route.table for _, p in plans if p.route}
                refresh = refresh_all(
                    conn, period=loaded_period, guild_loaded=guild_loaded,
                    loaded_tables=loaded_tables,
                )
                for line in refresh.lines:
                    log_ok(line)
                clear_cache()  # chat answers cached before this load would now be stale
            finally:
                conn.close()

            if records:
                rec = record_run(records, warnings=skipped)
                log_ok(
                    f"Recorded this load ({rec.total_rows:,} rows) — Part 2 will now detect it."
                )
            done("Load complete.")
            st.success(
                f"Loaded {len(records)} file(s) into Databricks. "
                "Related Genie sources and dashboards were refreshed automatically. "
                "Open **2 · Update Presentation** only when you also want to rebuild the deck."
            )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Load failed: {exc}")




# ── Floating "Ask the data" bubble, bottom-right on every page ──────────────────────────────
render_ask_widget("1_load_to_databricks")
