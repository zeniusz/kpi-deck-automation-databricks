"""The hand-off between the two halves of the app.

Part 1 (Load to Databricks) records WHAT it wrote. Part 2 (Update Presentation) reads that to
DETECT that new data landed, without the two ever having to run in the same session.

Two independent detection sources, because ingestion can also happen outside this app (someone
uploads via the Databricks UI, or runs a notebook):

  1. `read_manifest()`  — what THIS app wrote (fast, no warehouse needed, but only sees our runs).
  2. `live_table_state()` — queries max(Reporting_Month)/row counts straight from Databricks
     (authoritative; sees every change however it was made, but needs a running warehouse).

Part 2 shows both and lets the user refresh regardless — it never *blocks* on detection, because
the deck must be refreshable from whatever is already in Databricks.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from settings import BRONZE_FQN, GOLD_FQN

MANIFEST_PATH = Path(__file__).parent / ".run_manifest.json"
MAX_RUNS = 50  # keep the file small; oldest runs are dropped


@dataclass
class WriteRecord:
    """One file → one table write."""

    filename: str
    table: str
    mode: str
    reporting_month: str | None
    rows: int
    volume_path: str | None = None
    bulk: bool = False
    archive_ok: bool | None = None  # False = the raw-file archive failed (table write still ran)


@dataclass
class RunRecord:
    """One Part-1 run (possibly several files)."""

    timestamp: str
    writes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def tables(self) -> list[str]:
        return sorted({w["table"] for w in self.writes})

    @property
    def months(self) -> list[str]:
        return sorted({w["reporting_month"] for w in self.writes if w.get("reporting_month")})

    @property
    def total_rows(self) -> int:
        return sum(int(w.get("rows") or 0) for w in self.writes)


def read_manifest() -> list[RunRecord]:
    """All recorded runs, newest first. Never raises — a missing/corrupt file means 'no runs'."""
    try:
        raw = json.loads(MANIFEST_PATH.read_text())
        runs = [RunRecord(**r) for r in raw.get("runs", [])]
        return sorted(runs, key=lambda r: r.timestamp, reverse=True)
    except Exception:  # noqa: BLE001 — absent, empty, or malformed: all mean "nothing recorded"
        return []


def record_run(writes: list[WriteRecord], warnings: list[str] | None = None) -> RunRecord:
    """Append a run to the manifest. Best-effort: a write failure must never break ingestion."""
    run = RunRecord(
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        writes=[asdict(w) for w in writes],
        warnings=list(warnings or []),
    )
    try:
        existing = read_manifest()
        payload = {"runs": [asdict(r) for r in ([run] + existing)[:MAX_RUNS]]}
        MANIFEST_PATH.write_text(json.dumps(payload, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return run


def last_run() -> RunRecord | None:
    runs = read_manifest()
    return runs[0] if runs else None


# ── Live detection straight from Databricks ────────────────────────────────────────────────────
# Tables worth watching, and the column that carries the period (None = no period column).
# NOTE per the data-quality audit: the two real period tables DISAGREE on format —
# analytics_teams_listing stores '2026-06' (hyphen) while lakehouse_guild stores '2026.06' (dot).
# We therefore report the raw value and never parse/compare across tables.
WATCHED_TABLES: list[tuple[str, str | None]] = [
    # NOTE fake_company_data_* is deliberately NOT here — it is pipeline test data
    # ("Alpine Analytics" etc.), never a Analytics KPI, so it must not appear in detection.
    ("analytics_teams_listing", "reporting_month"),
    ("lakehouse_guild", "Reporting_Month"),
    ("analytics_community", None),
    ("atlas_training_completions", None),
    ("beacon_training_completions", None),
    ("registration_for_databricks_trainings", None),
    ("analytics_events_all", None),
]

CATALOG_SCHEMA = BRONZE_FQN
GOLD_SCHEMA = GOLD_FQN

# Tables that live OUTSIDE the bronze schema, or whose name changes each period, so they cannot be
# hardcoded in WATCHED_TABLES above:
#   * the gold participants table lives in the gold schema
#   * PlanTrack / individual course snapshots get a NEW `_YYYY_MM` table per period (guide Rule 3)
EXTRA_WATCHED: list[tuple[str, str]] = [
    (f"{GOLD_SCHEMA}.databricks_trainings_participants", "databricks_trainings_participants (gold)"),
]
PERIOD_TABLE_PREFIXES = ("plantrack_", "atlas_data", "atlas_german", "beacon_elearning_")


def _period_tables(cur) -> list[tuple[str, str]]:
    """Discover the newest `*_YYYY_MM` snapshot tables (PlanTrack + individual courses).

    These are invisible to a hardcoded list because the table NAME carries the period. We report the
    newest period's tables so a missing new period is obvious.
    """
    try:
        cur.execute(f"SHOW TABLES IN {CATALOG_SCHEMA}")
        names = [r[1] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []
    hits = [
        n for n in names
        if re.search(r"_\d{4}_\d{2}$", n) and n.startswith(PERIOD_TABLE_PREFIXES)
    ]
    if not hits:
        return []
    newest = max(n[-7:] for n in hits)  # "_YYYY_MM"
    return [(f"{CATALOG_SCHEMA}.`{n}`", n) for n in sorted(hits) if n.endswith(newest)]


@dataclass
class TableState:
    table: str
    rows: int
    latest_month: str | None
    months: int


def live_table_state(tables: list[tuple[str, str | None]] | None = None) -> list[TableState]:
    """Query row counts + newest period per watched table. Needs a running SQL warehouse.

    SPEED: all watched tables are read in ONE round-trip via a UNION ALL instead of 2 queries per
    table (was 8 tables → up to 16 serial statements, each with warehouse latency; now 1).
    Raises on connection failure — the caller decides whether that's fatal (Service 2 treats it as
    'detection unavailable' and still lets the user refresh the deck).
    """
    from databricks_client import _connect

    watched = tables or WATCHED_TABLES
    parts = []
    for table, month_col in watched:
        safe = table.replace("'", "''")
        if month_col:
            parts.append(
                f"SELECT '{safe}' AS t, count(*) AS n, max(`{month_col}`) AS m, "
                f"count(DISTINCT `{month_col}`) AS c FROM {CATALOG_SCHEMA}.`{table}`"
            )
        else:
            parts.append(
                f"SELECT '{safe}' AS t, count(*) AS n, CAST(NULL AS STRING) AS m, 0 AS c "
                f"FROM {CATALOG_SCHEMA}.`{table}`"
            )
    sql = " UNION ALL ".join(parts)

    order = {t: i for i, (t, _) in enumerate(watched)}
    out: list[TableState] = []
    conn = _connect()
    try:
        cur = conn.cursor()
        try:
            try:
                cur.execute(sql)
                for r in cur.fetchall():
                    out.append(TableState(str(r[0]), int(r[1]), r[2], int(r[3] or 0)))
            except Exception:
                # One bad table (e.g. the broken metric view) fails the whole UNION — fall back to
                # per-table queries so the rest still report.
                out = []
                for table, month_col in watched:
                    try:
                        if month_col:
                            cur.execute(
                                f"SELECT count(*), max(`{month_col}`), "
                                f"count(DISTINCT `{month_col}`) FROM {CATALOG_SCHEMA}.`{table}`"
                            )
                            row = cur.fetchone()
                            out.append(TableState(table, int(row[0]), row[1], int(row[2])))
                        else:
                            cur.execute(f"SELECT count(*) FROM {CATALOG_SCHEMA}.`{table}`")
                            out.append(TableState(table, int(cur.fetchone()[0]), None, 0))
                    except Exception:  # noqa: BLE001 — never let one table kill the sweep
                        out.append(TableState(table, -1, None, 0))
        finally:
            cur.close()
        out = _append_extra(conn, out)
    finally:
        conn.close()
    out.sort(key=lambda s: order.get(s.table, 99))
    return out


def _append_extra(conn, out: list) -> list:
    """Add the gold table and the newest per-period snapshot tables to the sweep."""
    cur = conn.cursor()
    try:
        for fq, label in EXTRA_WATCHED + _period_tables(cur):
            try:
                cur.execute(f"SELECT count(*) FROM {fq}")
                out.append(TableState(label, int(cur.fetchone()[0]), None, 0))
            except Exception:  # noqa: BLE001
                out.append(TableState(label, -1, None, 0))
    finally:
        cur.close()
    return out
