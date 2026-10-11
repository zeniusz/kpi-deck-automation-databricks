"""Show which reporting months a target table ALREADY holds, before anything is written.

Why this exists: re-running a month deliberately REPLACES it, which is what makes a re-run safe — and
also what destroys a snapshot if the month is wrong. The guide has a section titled
"⚠️ CRITICAL: Lakehouse Guild — Reporting_Month Rules" written precisely because a June snapshot was
uploaded tagged `2026.08`, which wiped the real August rows and left the dashboard's month filter with
a single misleading value. Its own checklist says to verify with:

    SELECT DISTINCT Reporting_Month, COUNT(*) FROM … GROUP BY 1 ORDER BY 1;

but that is an AFTER-the-fact check. Running it BEFORE the write turns an irreversible mistake into a
visible one: if the table already has rows for the month you are about to load, you see that while
the write toggle is still off.

Read-only, cached, and never raises — if Databricks is unreachable the caller simply gets no note.
"""

from __future__ import annotations

import html

import streamlit as st

from excel_ingestion import CATALOG, BRONZE, WriteMode, month_tag_column, route_for


@st.cache_data(ttl=120, show_spinner=False)
def period_counts(table: str, month_col: str) -> dict[str, int] | None:
    """`{reporting_month: rows}` for one table, or None if it cannot be read.

    Cached briefly: the picker re-renders on every widget interaction and this must not turn the page
    into a round-trip per keystroke.
    """
    try:
        from databricks_client import _connect

        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT `{month_col}` AS m, count(*) AS n "
                f"FROM {CATALOG}.{BRONZE}.`{table}` GROUP BY 1 ORDER BY 1"
            )
            return {str(r[0]): int(r[1]) for r in cur.fetchall() if r[0] is not None}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — advisory only; never block an upload on it
        return None


def existing_periods_note(files: list[tuple[str, str | None]]) -> str | None:
    """An HTML note listing, per APPEND target, the months already present and what will happen.

    `files` is [(filename, resolved_month)]. Only APPEND tables are checked: those are the ones where a
    month collides. FULL_REPLACE tables replace everything by design, and per-period tables carry the
    period in their name.
    """
    seen: dict[str, str | None] = {}
    for name, month in files:
        route = route_for(name)
        if route and route.mode is WriteMode.APPEND and route.table:
            seen.setdefault(route.table, month)
    if not seen:
        return None

    rows: list[str] = []
    for table, month in sorted(seen.items()):
        counts = period_counts(table, month_tag_column(table))
        if counts is None:
            continue
        if not counts:
            rows.append(f"<code>{html.escape(table)}</code> is empty — this will be its first month.")
            continue
        listed = ", ".join(f"{m} ({n:,})" for m, n in sorted(counts.items())[-6:])
        if month and month in counts:
            rows.append(
                f"⚠️ <code>{html.escape(table)}</code> already holds <b>{counts[month]:,} rows for "
                f"{html.escape(month)}</b> — loading will <b>REPLACE</b> them. Correct if you are "
                f"re-running that period; wrong month = that snapshot is lost. Months present: "
                f"{listed}."
            )
        else:
            rows.append(
                f"<code>{html.escape(table)}</code> → {html.escape(str(month or '—'))} is "
                f"<b>new</b>, added alongside: {listed}."
            )
    if not rows:
        return None
    body = "<br>".join(rows)
    return (
        "<div class='small-note' style='margin-top:.6rem;padding:.6rem .8rem;"
        "border:1px solid rgba(255,200,107,.28);background:rgba(255,200,107,.06);"
        f"border-radius:10px'><b>Already in Databricks</b><br>{body}</div>"
    )
