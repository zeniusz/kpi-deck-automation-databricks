"""Build a plain-language "what changed" summary: KPI movement, growth, and what was loaded.

Answers "what changed from what" — e.g. Total Revenue 25.83B → 27.15B (+5.1%). Two outputs:
  * `summarise()` → a `Summary` you can render in the UI
  * `to_docx()` / `to_markdown()` → a downloadable Word / text file

The comparison is period-over-period straight from Databricks: for any table with a reporting-month
column we compare the newest month against the previous one. Nothing is hardcoded, so it keeps
working as new months land.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from settings import BRONZE_FQN, DECKS_URL, GOLD_FQN

CATALOG_SCHEMA = BRONZE_FQN

# Where the full KPI pack lives (same URL as links.SHAREPOINT_DECKS; imported from settings so this
# module keeps no UI dependency).
SHAREPOINT_URL = DECKS_URL


@dataclass
class Movement:
    """One metric, previous vs current."""

    label: str
    previous: float | None
    current: float | None
    unit: str = ""

    @property
    def delta(self) -> float | None:
        if self.previous is None or self.current is None:
            return None
        return self.current - self.previous

    @property
    def pct(self) -> float | None:
        prev, curr = self.previous, self.current
        if prev is None or curr is None or prev == 0:
            return None
        return (curr - prev) / abs(prev) * 100.0

    @property
    def direction(self) -> str:
        d = self.delta
        if d is None:
            return "—"
        if d > 0:
            return "grew"
        if d < 0:
            return "fell"
        return "unchanged"

    def phrase(self) -> str:
        """Human sentence: 'Total revenue grew from 25.83B to 27.15B (+5.1%).'"""
        if self.current is None:
            return f"{self.label}: no data."
        cur = _fmt(self.current, self.unit)
        if self.previous is None:
            # No comparison available (single period, or a metric with no period column at all) —
            # state it as a current figure rather than implying a missing comparison.
            return f"{self.label}: {cur}."
        prev = _fmt(self.previous, self.unit)
        if self.delta == 0:
            return f"{self.label}: unchanged at {cur}."
        pct = f" ({self.pct:+.1f}%)" if self.pct is not None else ""
        return f"{self.label} {self.direction} from {prev} to {cur}{pct}."


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    v = float(value)
    if unit == "eur":
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(v) >= div:
                return f"{v/div:.2f}{suf}"
        return f"{v:,.0f}"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.1f}"


@dataclass
class Breakdown:
    """A detail table for the report — e.g. Atlas completions by course × language × status."""

    title: str
    columns: list[str]
    rows: list[list[str]]
    note: str = ""


@dataclass
class Summary:
    generated: str
    current_period: str | None = None
    previous_period: str | None = None
    movements: list[Movement] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    programmes: list[str] = field(default_factory=list)
    totals: list[Movement] = field(default_factory=list)
    breakdowns: list[Breakdown] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def headline(self) -> str:
        if self.current_period and self.previous_period:
            return f"Changes from {self.previous_period} to {self.current_period}"
        if self.current_period:
            return f"State as of {self.current_period}"
        return "Data summary"


def _latest_plantrack_table(cur) -> str | None:
    """PlanTrack gets a NEW table per period (`..._YYYY_MM`), so pick the newest one."""
    try:
        cur.execute(f"SHOW TABLES IN {CATALOG_SCHEMA} LIKE 'plantrack*'")
        names = sorted((r[1] for r in cur.fetchall()), reverse=True)
        return names[0] if names else None
    except Exception:  # noqa: BLE001
        return None


def _scalar(cur, sql: str, params: list | None = None):
    try:
        cur.execute(sql, params) if params else cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001 — a missing table must not break the whole summary
        return None


def _member_count_sql(cur, table: str) -> str:
    """Count identified Teams members, not rows whose email is missing.

    The bronze schema has used both `Email` and `User` over time, so discover the live column rather
    than baking one generation of the export into every report query.
    """
    if table != "analytics_teams_listing":
        return "count(*)"
    try:
        cur.execute(f"DESCRIBE {CATALOG_SCHEMA}.`{table}`")
        cols = {str(r[0]).lower(): str(r[0]) for r in cur.fetchall()}
        email_cols = [cols[x] for x in ("email", "user") if x in cols]
    except Exception:  # noqa: BLE001
        email_cols = []
    if not email_cols:
        return "count(*)"
    identity = "coalesce(" + ", ".join(f"cast(`{c}` AS STRING)" for c in email_cols) + ")"
    return f"count(CASE WHEN trim({identity}) <> '' THEN 1 END)"


# The selectable programmes. Keys are what the Reports page offers; each maps to the metric groups
# `summarise()` builds. The demo revenue table is NOT here — it is test data, never a KPI.
PROGRAMMES: dict[str, str] = {
    "atlas": "Atlas",
    "beacon": "Beacon",
    "plantrack": "PlanTrack",
    "db_registrations": "DB registrations",
    "db_participants": "DB participants",
    "community": "Community",
    "teams_listing": "Teams members",
    "guild": "Guild",
    "events": "Events",
}

# Full names for the report itself — the short labels above are only for the picker chips, which
# truncate badly (two "Databricks trainings…" chips looked identical).
PROGRAMME_NAMES: dict[str, str] = {
    "atlas": "Atlas e-learnings",
    "beacon": "Beacon Platform (Beacon) e-learnings",
    "plantrack": "PlanTrack learnings",
    "db_registrations": "Databricks trainings — registrations",
    "db_participants": "Databricks trainings — participants",
    "community": "Analytics Community — registrations",
    "teams_listing": "Analytics Community — Teams channel members",
    "guild": "Lakehouse Guild members",
    "events": "Community events — attendance",
}
ALL_PROGRAMMES = list(PROGRAMMES)


def summarise(
    connection,
    loaded: list[str] | None = None,
    programmes: list[str] | None = None,
) -> Summary:
    """Summarise the selected Analytics KPI programmes: Academy learning, Databricks trainings,
    community, Teams listing, Guild and events. Period-over-period where a period column exists;
    current state otherwise.

    `programmes` selects which to include (keys of PROGRAMMES); None = all. The
    `fake_company_data_*` revenue table is deliberately NOT reachable here — it is pipeline test
    data ("Alpine Analytics" etc.), never a Analytics KPI.
    """
    s = Summary(generated=datetime.now().strftime("%Y-%m-%d %H:%M"), loaded=list(loaded or []))
    want = set(programmes if programmes is not None else ALL_PROGRAMMES)
    s.programmes = [PROGRAMME_NAMES[k] for k in ALL_PROGRAMMES if k in want]
    cur = connection.cursor()
    try:
        # ── Analytics Academy: learning completions (Atlas + Beacon) ──────────────────────────────
        for table, platform in [
            (tbl, plat) for tbl, plat, key in (
                ("atlas_training_completions", "Atlas", "atlas"),
                ("beacon_training_completions", "Beacon", "beacon"),
            ) if key in want
        ]:
            t = f"{CATALOG_SCHEMA}.`{table}`"
            passed = _scalar(cur, f"SELECT count(*) FROM {t} WHERE lower(Status)='passed'")
            total = _scalar(cur, f"SELECT count(*) FROM {t}")
            if total:
                s.movements.append(Movement(f"{platform} — completed (Passed)", None, _num(passed)))
                s.movements.append(Movement(f"{platform} — learning records", None, _num(total)))
                if passed is not None:
                    s.notes.append(
                        f"{platform}: {int(passed)} of {int(total)} records completed "
                        f"({passed / total * 100:.0f}% pass rate)."
                    )
            comps = _scalar(cur, f"SELECT count(DISTINCT Company) FROM {t}")
            if comps:
                s.movements.append(Movement(f"{platform} — companies reached", None, _num(comps)))

        # ── PlanTrack learnings (completed learners only; one table per period) ──────────────────
        if "plantrack" in want:
            mp = _latest_plantrack_table(cur)
            if mp:
                t = f"{CATALOG_SCHEMA}.`{mp}`"
                n = _scalar(cur, f"SELECT count(*) FROM {t}")
                if n:
                    s.movements.append(Movement("PlanTrack — completed learnings", None, _num(n)))
                    s.movements.append(Movement(
                        "PlanTrack — companies reached", None,
                        _num(_scalar(cur, f"SELECT count(DISTINCT Company) FROM {t}"))))
                    s.movements.append(Movement(
                        "PlanTrack — countries", None,
                        _num(_scalar(cur, f"SELECT count(DISTINCT Country) FROM {t}"))))
                    mods = _scalar(cur, f"SELECT count(DISTINCT Learning_Name_Module) FROM {t}")
                    if mods:
                        s.notes.append(
                            f"PlanTrack: {int(n)} completed learnings across {int(mods)} module(s) "
                            f"(source table {mp})."
                        )

        # ── Databricks trainings ─────────────────────────────────────────────────────────────────
        rt = f"{CATALOG_SCHEMA}.registration_for_databricks_trainings"
        regs = _scalar(cur, f"SELECT count(*) FROM {rt}") if "db_registrations" in want else None
        if regs:
            s.movements.append(Movement("Databricks training registrations", None, _num(regs)))
            s.movements.append(Movement(
                "Databricks trainings — countries", None,
                _num(_scalar(cur, f"SELECT count(DISTINCT country) FROM {rt}"))))
        part = _scalar(
            cur,
            "SELECT count(*) FROM "
            "" + GOLD_FQN + ".databricks_trainings_participants",
        ) if "db_participants" in want else None
        if part:
            s.movements.append(Movement("Databricks trainings — participants", None, _num(part)))

        # ── Community ────────────────────────────────────────────────────────────────────────────
        ct = f"{CATALOG_SCHEMA}.analytics_community"
        comm = _scalar(cur, f"SELECT count(*) FROM {ct}") if "community" in want else None
        if comm:
            s.movements.append(Movement("Community registrations", None, _num(comm)))
            s.movements.append(Movement(
                "Community — countries", None,
                _num(_scalar(cur, f"SELECT count(DISTINCT Country) FROM {ct}"))))
            top = _scalar(
                cur, f"SELECT Country FROM {ct} GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
            if top:
                s.notes.append(f"Largest community country: {top}.")

        # ── Teams channel listing + Guild: the two tables WITH a period column ───────────────────
        _period_wanted = [
            x for x in (
                ("analytics_teams_listing", "reporting_month", "Teams channel members",
                 "teams_listing"),
                ("lakehouse_guild", "Reporting_Month", "Lakehouse Guild members", "guild"),
            ) if x[3] in want
        ]
        for table, month_col, label, _key in _period_wanted:
            t = f"{CATALOG_SCHEMA}.`{table}`"
            count_sql = _member_count_sql(cur, table)
            months = []
            try:
                cur.execute(f"SELECT DISTINCT `{month_col}` FROM {t} ORDER BY 1 DESC")
                months = [r[0] for r in cur.fetchall() if r[0]]
            except Exception:  # noqa: BLE001
                pass
            if not months:
                continue
            cm = months[0]
            pm = months[1] if len(months) > 1 else None
            s.current_period = s.current_period or cm
            if pm:
                s.previous_period = s.previous_period or pm
            c = _scalar(cur, f"SELECT {count_sql} FROM {t} WHERE `{month_col}` = ?", [cm])
            p = _scalar(cur, f"SELECT {count_sql} FROM {t} WHERE `{month_col}` = ?", [pm]) if pm else None
            s.movements.append(Movement(label, _num(p), _num(c)))
            if not pm:
                s.notes.append(
                    f"{label}: only one period ({cm}) is loaded, so there is nothing to compare "
                    "yet — next month's load will show the change."
                )

        # Guild enrichment outcomes
        circ = f"{CATALOG_SCHEMA}.lakehouse_guild"
        reg_in_comm = (_scalar(cur, f"SELECT sum(Is_Community_Registered) FROM {circ}")
                       if "guild" in want else None)
        if reg_in_comm is not None:
            s.movements.append(Movement(
                "Guild members also in the community", None, _num(reg_in_comm)))

        # ── Events ───────────────────────────────────────────────────────────────────────────────
        et = f"{CATALOG_SCHEMA}.analytics_events_all"
        att = _scalar(cur, f"SELECT count(*) FROM {et}") if "events" in want else None
        if att:
            s.movements.append(Movement("Event attendances (all events)", None, _num(att)))
            nev = _scalar(cur, f"SELECT count(DISTINCT event_number) FROM {et}")
            latest = _scalar(
                cur, f"SELECT event_name FROM {et} ORDER BY event_number DESC LIMIT 1")
            prev_n = _scalar(
                cur,
                f"SELECT count(*) FROM {et} WHERE event_number = "
                f"(SELECT max(event_number)-1 FROM {et})")
            last_n = _scalar(
                cur,
                f"SELECT count(*) FROM {et} WHERE event_number = "
                f"(SELECT max(event_number) FROM {et})")
            if last_n is not None:
                s.movements.append(Movement(
                    f"Attendance at the latest event ({latest})", _num(prev_n), _num(last_n)))
            if nev:
                s.notes.append(f"{int(nev)} community events held so far.")

        # ── Detail breakdowns: exactly WHICH course, WHICH language, WHICH companies ──────────────
        for table, platform, key in (
            ("atlas_training_completions", "Atlas", "atlas"),
            ("beacon_training_completions", "Beacon", "beacon"),
        ):
            if key not in want:
                continue
            t = f"{CATALOG_SCHEMA}.`{table}`"
            try:
                cur.execute(
                    f"SELECT course_name, language, Status, count(*) FROM {t} "
                    "GROUP BY 1,2,3 ORDER BY 1,2,3"
                )
                rows = [[str(r[0]), str(r[1]), str(r[2]), f"{int(r[3]):,}"] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                s.breakdowns.append(Breakdown(
                    f"{platform} — completions by course, language and status",
                    ["Course", "Language", "Status", "Learners"], rows))
            # Which companies took them
            try:
                cur.execute(
                    f"SELECT Company, count(*) c, "
                    f"sum(CASE WHEN lower(Status)='passed' THEN 1 ELSE 0 END) p "
                    f"FROM {t} GROUP BY 1 ORDER BY c DESC"
                )
                crows = [[str(r[0]), f"{int(r[1]):,}", f"{int(r[2]):,}"] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                crows = []
            if crows:
                s.breakdowns.append(Breakdown(
                    f"{platform} — companies reached",
                    ["Company", "Learning records", "Completed"], crows))

        if "plantrack" in want:
            mp = _latest_plantrack_table(cur)
            if mp:
                t = f"{CATALOG_SCHEMA}.`{mp}`"
                try:
                    cur.execute(
                        f"SELECT Learning_Name_Module, Country, count(*) FROM {t} "
                        "GROUP BY 1,2 ORDER BY 1,2"
                    )
                    rows = [[str(r[0]), str(r[1]), f"{int(r[2]):,}"] for r in cur.fetchall()]
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    s.breakdowns.append(Breakdown(
                        "PlanTrack — completed learnings by module and country",
                        ["Module (language prefix)", "Country", "Learners"], rows,
                        note="The EN-/DE- prefix in the module name carries the language.",
                    ))

        if "db_registrations" in want:
            try:
                cur.execute(
                    f"SELECT country, count(*) FROM {CATALOG_SCHEMA}."
                    "registration_for_databricks_trainings GROUP BY 1 ORDER BY 2 DESC"
                )
                rows = [[str(r[0]), f"{int(r[1]):,}"] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                s.breakdowns.append(Breakdown(
                    "Databricks trainings — registrations by country",
                    ["Country", "Registrations"], rows))

        if "db_participants" in want:
            try:
                cur.execute(
                    "SELECT Level, count(*) FROM " + GOLD_FQN + "."
                    "databricks_trainings_participants GROUP BY 1 ORDER BY 2 DESC"
                )
                rows = [[str(r[0]), f"{int(r[1]):,}"] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                s.breakdowns.append(Breakdown(
                    "Databricks trainings — participants by level",
                    ["Level", "Participants"], rows))

        if "community" in want:
            try:
                cur.execute(
                    f"SELECT Country, count(*) FROM {CATALOG_SCHEMA}.analytics_community "
                    "GROUP BY 1 ORDER BY 2 DESC"
                )
                rows = [[str(r[0]), f"{int(r[1]):,}"] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                s.breakdowns.append(Breakdown(
                    "Analytics Community — registrations by country",
                    ["Country", "Registrations"], rows))

        if "events" in want:
            try:
                cur.execute(
                    f"SELECT event_name, count(*) FROM {CATALOG_SCHEMA}.analytics_events_all "
                    "GROUP BY 1, event_number ORDER BY event_number"
                )
                rows = [[str(r[0]), f"{int(r[1]):,}"] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                s.breakdowns.append(Breakdown(
                    "Community events — attendance per event",
                    ["Event", "Attendees"], rows,
                    note="Attendees of 1 minute or less are excluded, per the event cleaning rule.",
                ))

        # ── Current totals across everything ─────────────────────────────────────────────────────
        for table, label, _k in (
            ("analytics_community", "Community registrations", "community"),
            ("analytics_teams_listing", "Teams channel members", "teams_listing"),
            ("lakehouse_guild", "Lakehouse Guild members", "guild"),
            ("analytics_events_all", "Event attendances", "events"),
            ("atlas_training_completions", "Atlas learning records", "atlas"),
            ("beacon_training_completions", "Beacon learning records", "beacon"),
            ("registration_for_databricks_trainings",
             "Databricks training registrations", "db_registrations"),
        ):
            if _k not in want:
                continue
            count_sql = _member_count_sql(cur, table)
            n = _scalar(cur, f"SELECT {count_sql} FROM {CATALOG_SCHEMA}.`{table}`")
            if n is not None:
                s.totals.append(Movement(label, None, _num(n)))
    finally:
        cur.close()
    return s


# Tables that carry a period column, and which column it is. NOTE the format differs per table
# (teams_listing uses '2026-06', the others '2026.06') — see the F1 decision.
PERIOD_TABLES: list[tuple[str, str]] = [
    ("analytics_teams_listing", "reporting_month"),
    ("lakehouse_guild", "Reporting_Month"),
    ("atlas_training_completions", "reporting_month"),
    ("beacon_training_completions", "reporting_month"),
]


def available_periods(connection) -> list[str]:
    """Every reporting period present anywhere, newest first — drives the month picker.

    Normalised to dots for display/sorting so '2026-06' and '2026.06' don't appear twice;
    `summarise_between` converts back to each table's own format when querying.
    """
    found: set[str] = set()
    cur = connection.cursor()
    try:
        for table, col in PERIOD_TABLES:
            try:
                cur.execute(f"SELECT DISTINCT `{col}` FROM {CATALOG_SCHEMA}.`{table}`")
                for r in cur.fetchall():
                    if r[0]:
                        found.add(str(r[0]).replace("-", "."))
            except Exception:  # noqa: BLE001 — table may lack the column yet
                continue
    finally:
        cur.close()
    return sorted(found, reverse=True)


def _period_variants(period: str) -> list[str]:
    """Both spellings of a period, so a query matches whichever the table stores."""
    dot = period.replace("-", ".")
    return [dot, dot.replace(".", "-")]


def summarise_between(
    connection,
    period: str,
    compare_to: str,
    loaded: list[str] | None = None,
    programmes: list[str] | None = None,
) -> Summary:
    """Compare two explicitly chosen periods across every table that has a period column.

    Used by the Reports page, where the user picks the month rather than always getting the newest.
    Falls back to `summarise()`'s current-state metrics for programmes with no period column, so the
    report is complete either way.
    """
    s = summarise(connection, loaded=loaded, programmes=programmes)
    s.current_period, s.previous_period = period, compare_to
    # Rebuild movements as true period-vs-period where possible.
    s.movements = []
    want = set(programmes if programmes is not None else ALL_PROGRAMMES)
    key_for = {
        "analytics_teams_listing": "teams_listing",
        "lakehouse_guild": "guild",
        "atlas_training_completions": "atlas",
        "beacon_training_completions": "beacon",
    }
    cur = connection.cursor()
    try:
        for table, col in PERIOD_TABLES:
            if table.startswith("fake_company_data"):
                continue  # demo test data — never reported
            if key_for.get(table) not in want:
                continue
            label = {
                "analytics_teams_listing": "Teams channel members",
                "lakehouse_guild": "Lakehouse Guild members",
                "atlas_training_completions": "Atlas learning records",
                "beacon_training_completions": "Beacon learning records",
                }.get(table, table)
            count_sql = _member_count_sql(cur, table)
            vals: dict[str, float | None] = {}
            for key, p in (("cur", period), ("prev", compare_to)):
                n = None
                for variant in _period_variants(p):
                    n = _scalar(
                        cur,
                        f"SELECT {count_sql} FROM {CATALOG_SCHEMA}.`{table}` WHERE `{col}` = ?",
                        [variant],
                    )
                    if n:
                        break
                # A period with NO rows is "not loaded", NOT a real zero — reporting 0 would read as
                # "fell to nothing" when the truth is the month simply isn't in that table.
                vals[key] = _num(n) if n else None
            if vals["cur"] is not None or vals["prev"] is not None:
                s.movements.append(Movement(label, vals["prev"], vals["cur"]))
                if vals["cur"] is None:
                    s.notes.append(
                        f"{label}: no rows for {period} (that period is not loaded in this table)."
                    )

        # Guild enrichment for the chosen period
        for variant in (_period_variants(period) if "guild" in want else []):
            reg = _scalar(
                cur,
                f"SELECT sum(Is_Community_Registered) FROM {CATALOG_SCHEMA}.lakehouse_guild "
                "WHERE `Reporting_Month` = ?", [variant])
            if reg:
                prev_reg = None
                for pv in _period_variants(compare_to):
                    prev_reg = _scalar(
                        cur,
                        "SELECT sum(Is_Community_Registered) FROM "
                        f"{CATALOG_SCHEMA}.lakehouse_guild WHERE `Reporting_Month` = ?", [pv])
                    if prev_reg:
                        break
                s.movements.append(Movement(
                    "Guild members also in the community", _num(prev_reg), _num(reg)))
                break

    finally:
        cur.close()

    if not s.movements:
        s.notes.insert(0, (
            f"No table holds rows for both {period} and {compare_to}, so no comparison was "
            "possible — the current totals below still apply."
        ))
    return s


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def to_markdown(s: Summary) -> str:
    out = [f"# {s.headline()}", "", f"_Generated {s.generated}_", ""]
    if s.programmes:
        out += [f"_Programmes covered: {', '.join(s.programmes)}_", ""]
    if s.movements:
        grouped: dict[str, list[Movement]] = {}
        for m in s.movements:
            grouped.setdefault(_group_of(m.label), []).append(m)
        for group, items in grouped.items():
            out += [f"## {group}", ""]
            out += [f"- {m.phrase()}" for m in items]
            out.append("")
    if s.notes:
        out += ["## Highlights", ""] + [f"- {n}" for n in s.notes] + [""]
    if s.totals:
        out += ["## Current totals", ""]
        out += [f"- {m.label}: {_fmt(m.current, m.unit)}" for m in s.totals]
        out.append("")
    if s.breakdowns:
        out += ["## Detail", ""]
        for bd in s.breakdowns:
            out += [f"### {bd.title}", ""]
            out.append("| " + " | ".join(bd.columns) + " |")
            out.append("|" + "|".join(["---"] * (len(bd.columns) - 1) + ["---:"]) + "|")
            out += ["| " + " | ".join(r) + " |" for r in bd.rows]
            if bd.note:
                out += ["", f"_{bd.note}_"]
            out.append("")
    if s.loaded:
        out += ["## Loaded this run", ""] + [f"- {x}" for x in s.loaded] + [""]
    out += [
        "## Full detail", "",
        "This is a short summary. For the complete Analytics KPI pack — full slides, per-country "
        "and per-company breakdowns and the historic months — see the **Enablement KPIs** folder "
        "in SharePoint:", "",
        f"<{SHAREPOINT_URL}>", "",
        "Live dashboards: Analytics Community · Learnings/Trainings · Community Events · "
        "Databricks Trainings. Ask questions in the \"Analytics Department Activities\" Genie space.", "",
    ]
    return "\n".join(out)


def _group_of(label: str) -> str:
    """Which programme heading a metric belongs under, for a readable report."""
    low = label.lower()
    if low.startswith("atlas"):
        return "Atlas e-learnings"
    if low.startswith("beacon"):
        return "Beacon Platform (Beacon)"
    if low.startswith("plantrack"):
        return "PlanTrack learnings"
    if "databricks training" in low or low.startswith("databricks trainings"):
        return "Databricks trainings"
    if low.startswith("community") or "community registrations" in low:
        return "Analytics Community"
    if low.startswith("teams channel"):
        return "Teams channel membership"
    if "guild" in low:
        return "Lakehouse Guild"
    if "event" in low:
        return "Community events"
    return "Other"


def to_docx(s: Summary) -> bytes:
    """A presentable Word report: cover block, per-programme sections with bold figures, a
    comparison table only when there is something to compare, and the source notes. Returns bytes."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    ACCENT = RGBColor(0x2D, 0x9C, 0xDB)
    GREY = RGBColor(0x69, 0x70, 0x8A)

    doc = Document()
    # Body font
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)

    # ── Cover block ─────────────────────────────────────────────────────────────────────────────
    kicker = doc.add_paragraph()
    kr = kicker.add_run("CONTOSO ANALYTICS")
    kr.bold = True
    kr.font.size = Pt(8.5)
    kr.font.color.rgb = ACCENT

    heading = doc.add_heading(s.headline(), level=0)
    for run in heading.runs:
        run.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)

    meta = doc.add_paragraph()
    mr = meta.add_run(f"Generated {s.generated}")
    mr.font.size = Pt(9)
    mr.font.color.rgb = GREY
    if s.programmes:
        pr = meta.add_run("\nProgrammes covered: " + ", ".join(s.programmes))
        pr.font.size = Pt(9)
        pr.font.color.rgb = GREY

    # ── Headline figures ────────────────────────────────────────────────────────────────────────
    comparable = [m for m in s.movements if m.previous is not None and m.pct is not None]
    if comparable:
        up = [m for m in comparable if m.pct and m.pct > 0]
        down = [m for m in comparable if m.pct and m.pct < 0]
        line = doc.add_paragraph()
        lr = line.add_run(
            f"{len(up)} metric(s) up · {len(down)} metric(s) down · "
            f"biggest move {max(comparable, key=lambda m: abs(m.pct or 0)).pct:+.1f}%"
        )
        lr.bold = True
        lr.font.size = Pt(10)

    # ── Per-programme sections ──────────────────────────────────────────────────────────────────
    grouped: dict[str, list[Movement]] = {}
    for m in s.movements:
        grouped.setdefault(_group_of(m.label), []).append(m)

    for group, items in grouped.items():
        doc.add_heading(group, level=1)
        for m in items:
            para = doc.add_paragraph(style="List Bullet")
            # "Label: " normal, then the figure in bold so it reads at a glance
            para.add_run(f"{m.label}: ")
            val = para.add_run(_fmt(m.current, m.unit))
            val.bold = True
            if m.previous is not None and m.pct is not None:
                trend = para.add_run(
                    f"  ({m.direction} from {_fmt(m.previous, m.unit)}, {m.pct:+.1f}%)"
                )
                trend.font.size = Pt(9)
                trend.font.color.rgb = ACCENT if (m.pct or 0) < 0 else GREY

    # ── Comparison table (only when a real comparison exists) ───────────────────────────────────
    if comparable:
        doc.add_heading("Period comparison", level=1)
        table = doc.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cell, head in zip(table.rows[0].cells,
                              ["Metric", s.previous_period or "Previous",
                               s.current_period or "Current", "Change"]):
            cell.text = head
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
        for m in comparable:
            row = table.add_row().cells
            row[0].text = m.label
            row[1].text = _fmt(m.previous, m.unit)
            row[2].text = _fmt(m.current, m.unit)
            row[3].text = f"{m.pct:+.1f}%" if m.pct is not None else "—"
            row[3].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    # ── Notes / highlights ──────────────────────────────────────────────────────────────────────
    if s.notes:
        doc.add_heading("Notes", level=1)
        for n in s.notes:
            doc.add_paragraph(n, style="List Bullet")

    if s.totals:
        doc.add_heading("Current totals", level=1)
        table = doc.add_table(rows=1, cols=2)
        table.style = "Light List Accent 1"
        for cell, head in zip(table.rows[0].cells, ["Metric", "Value"]):
            cell.text = head
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
        for m in s.totals:
            row = table.add_row().cells
            row[0].text = m.label
            row[1].text = _fmt(m.current, m.unit)
            row[1].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    # ── Detail: exactly which learning, which language, which companies ─────────────────────────
    if s.breakdowns:
        doc.add_heading("Detail", level=1)
        for bd in s.breakdowns:
            doc.add_heading(bd.title, level=2)
            table = doc.add_table(rows=1, cols=len(bd.columns))
            table.style = "Light Grid Accent 1"
            for cell, head in zip(table.rows[0].cells, bd.columns):
                cell.text = head
                for para in cell.paragraphs:
                    for r in para.runs:
                        r.bold = True
            for row in bd.rows:
                cells = table.add_row().cells
                for i, val in enumerate(row):
                    cells[i].text = val
                    if i == len(row) - 1:  # numeric last column
                        cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            if bd.note:
                n = doc.add_paragraph(bd.note)
                n.runs[0].font.size = Pt(8)
                n.runs[0].font.color.rgb = GREY

    if s.loaded:
        doc.add_heading("Files loaded", level=1)
        for x in s.loaded:
            doc.add_paragraph(x, style="List Bullet")

    # ── Where to go for the full picture ────────────────────────────────────────────────────────
    doc.add_heading("Full detail", level=1)
    fp = doc.add_paragraph()
    fp.add_run(
        "This is a short summary. For the complete Analytics KPI pack — full slides, per-country "
        "and per-company breakdowns and the historic months — see the "
    )
    b = fp.add_run("Enablement KPIs")
    b.bold = True
    fp.add_run(" folder in SharePoint (Analytics Analytics → General → Business Enablement).")
    lp = doc.add_paragraph()
    lr = lp.add_run(SHAREPOINT_URL)
    lr.font.size = Pt(8)
    lr.font.color.rgb = ACCENT
    dp = doc.add_paragraph()
    dr = dp.add_run(
        "Live dashboards: Analytics Community · Learnings/Trainings · Community Events · "
        "Databricks Trainings. Ask questions in the “Analytics Department Activities” Genie space."
    )
    dr.font.size = Pt(8)
    dr.font.color.rgb = GREY

    footer = doc.add_paragraph()
    fr = footer.add_run(
        "Source: " + BRONZE_FQN + " (bronze) and " + GOLD_FQN + " (gold), "
        "queried live from Databricks."
    )
    fr.font.size = Pt(8)
    fr.font.color.rgb = GREY

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
