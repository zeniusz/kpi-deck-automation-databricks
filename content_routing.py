"""Suggest a target table by looking INSIDE the file, not just at its name.

Filename routing (`excel_ingestion.route_for`) fails whenever a file arrives with an export-tool
name like `300400_export_eLearning….xlsx` or a hand-made name like
`Data_Training_Participation_Sheets.xlsx`. Rather than refuse it, we inspect the columns and values
and propose candidate tables with a confidence and a reason — the user then confirms or overrides.

It also DERIVES the learning taxonomy from the content:
    platform (Atlas / Beacon / PlanTrack) · course_name · language
which is exactly what the unified `*_training_completions` tables need and what the source course
files lack (they only carry Company/Status/Country — the course identity lives in the filename).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

from excel_ingestion import CATALOG, BRONZE, GOLD, WriteMode

# ── Canonical taxonomy ──────────────────────────────────────────────────────────────────────────
# Platform names as the TABLES use them, mapped from every spelling seen in the wild.
PLATFORM_ALIASES: dict[str, str] = {
    "atlas": "Atlas", "horizon": "Atlas", "horizinot": "Atlas",
    "international learning platform": "Beacon", "international platform": "Beacon",
    "beacon": "Beacon", "elearning": "Beacon",
    "plantrack": "PlanTrack", "master plan": "PlanTrack",
}

# Course identity → the canonical `course_name` each platform's table uses. Atlas and Beacon name
# the SAME course differently and that is intentional (see the source-structure notes), so the
# canonical name depends on the platform.
COURSE_CANON: dict[tuple[str, str], str] = {
    ("Atlas", "basics1"): "Data Basics & Awareness Pt. 1",
    ("Atlas", "basics2"): "Data Basics & Awareness Pt. 2",
    ("Atlas", "gov"): "Data Governance Basics",
    ("Beacon", "basics1"): "Data Basic Awareness Pt. 1",
    ("Beacon", "basics2"): "Data Basic Awareness Pt. 2",
    ("Beacon", "gov1"): "Data Governance Basics Pt. 1",
    ("Beacon", "gov2"): "Data Governance Basics Pt. 2",
}


def normalise_text(value: str | None) -> str:
    """Collapse the punctuation variants that create phantom duplicates.

    The combined export contains BOTH 'e-Learning – Data Basics…' (en-dash) and
    'e-Learning - Data Basics…' (hyphen), which made 10 'distinct' courses out of 5.
    """
    if value is None:
        return ""
    s = str(value)
    for dash in ("–", "—", "−"):  # en, em, minus
        s = s.replace(dash, "-")
    return re.sub(r"\s+", " ", s).strip()


def detect_platform(text: str | None) -> str | None:
    low = normalise_text(text).lower()
    if not low:
        return None
    for alias, canon in PLATFORM_ALIASES.items():
        if alias in low:
            return canon
    return None


def detect_language(text: str | None) -> str | None:
    """English vs German. 'Teil' is the German part-marker; 'german'/'de' also appear."""
    low = normalise_text(text).lower()
    if not low:
        return None
    if "german" in low or "deutsch" in low or re.search(r"\bteil\b", low) or re.search(r"\bde\b", low):
        return "German"
    if "english" in low or re.search(r"\ben\b", low) or re.search(r"\bpart\b", low):
        return "English"
    return None


def detect_course_key(text: str | None) -> str | None:
    """Which course this is, platform-independent: basics1 / basics2 / gov / gov1 / gov2."""
    low = normalise_text(text).lower()
    if not low:
        return None
    if "governance" in low:
        if re.search(r"(part|teil|pt\.?)\s*2", low):
            return "gov2"
        if re.search(r"(part|teil|pt\.?)\s*1", low):
            return "gov1"
        return "gov"
    if "basic" in low or "awareness" in low:
        if re.search(r"(part|teil|pt\.?)\s*2", low):
            return "basics2"
        return "basics1"
    return None


def canonical_course(platform: str | None, text: str | None) -> str | None:
    """Canonical `course_name` for the platform's table, from any spelling of the course."""
    key = detect_course_key(text)
    if not platform or not key:
        return None
    exact = COURSE_CANON.get((platform, key))
    if exact:
        return exact
    # Beacon splits governance into parts, Atlas does not — fall back sensibly either way.
    if key == "gov" and platform == "Beacon":
        return COURSE_CANON[("Beacon", "gov1")]
    if key in ("gov1", "gov2") and platform == "Atlas":
        return COURSE_CANON[("Atlas", "gov")]
    return None


# ── Table suggestions ───────────────────────────────────────────────────────────────────────────
@dataclass
class Suggestion:
    table: str                 # short table name
    mode: WriteMode
    confidence: int            # 0-100
    reason: str
    schema: str = BRONZE
    derived: dict[str, str] = field(default_factory=dict)   # platform/course/language if detected

    @property
    def full_table(self) -> str:
        return f"{CATALOG}.{self.schema}.{self.table}"


# Column fingerprints → candidate table. Checked against the file's actual headers.
FINGERPRINTS: list[tuple[set[str], str, WriteMode, str]] = [
    ({"platform", "course_name", "language", "company", "status"},
     "atlas_training_completions", WriteMode.APPEND,
     "has the unified completions shape (platform + course_name + language + Status)"),
    ({"company", "status", "country"},
     "atlas_training_completions", WriteMode.APPEND,
     "a single course export (Company/Status/Country) — course identity comes from the filename"),
    ({"company", "learning_name_module", "country", "status"},
     "plantrack_analytics_trainings", WriteMode.NEW_TABLE_PERIOD,
     "has Learning_Name_Module, the PlanTrack shape"),
    ({"user", "name", "role"},
     "lakehouse_guild", WriteMode.APPEND,
     "Teams owners+members export (User/Name/Role) — the Guild source shape"),
    ({"user", "name", "role", "country", "company", "job_title"},
     "analytics_teams_listing", WriteMode.APPEND,
     "Teams channel member listing with Country/Company/Job_Title"),
    ({"id", "startzeit", "fertigstellungszeit", "email", "company"},
     "analytics_community", WriteMode.FULL_REPLACE,
     "Forms registration export (German timestamps + Email + Company)"),
    ({"name", "dauer der besprechung", "e-mail"},
     "analytics_event_number", WriteMode.NEW_TABLE_EVENT,
     "Teams meeting attendance export (German duration column)"),
    ({"what company are you from?", "your work email"},
     "registration_for_databricks_trainings", WriteMode.FULL_REPLACE,
     "Databricks trainings registration form"),
    ({"level", "course id", "trainers"},
     "databricks_trainings_participants", WriteMode.FULL_REPLACE,
     "processed participants list (Level/Course ID/Trainers)"),
]


def platforms_present(df: pd.DataFrame, filename: str = "") -> list[str]:
    """Every distinct platform the file's rows belong to.

    A combined export mixes platforms (the participation sheet holds both Atlas and Beacon), so a
    single answer would mis-file most of it. Falls back to the filename for single-platform files.
    """
    cols = {normalise_text(c).lower(): c for c in df.columns}
    plat_col = next((cols[c] for c in cols if "platform" in c), None)
    found: list[str] = []
    if plat_col:
        for raw in df[plat_col].dropna().astype(str).unique():
            p = detect_platform(raw)
            if p and p not in found:
                found.append(p)
    if not found:
        # Some exports name the platform only in a "Source Dataset" style column or the filename.
        src_col = next((cols[c] for c in cols if "source" in c or "dataset" in c), None)
        if src_col:
            for raw in df[src_col].dropna().astype(str).unique():
                p = detect_platform(raw)
                if p and p not in found:
                    found.append(p)
    if not found:
        p = detect_platform(filename)
        if p:
            found.append(p)
    return found


def _norm_cols(df: pd.DataFrame) -> set[str]:
    return {normalise_text(c).lower().replace(" ", "_").strip("_") for c in df.columns} | {
        normalise_text(c).lower() for c in df.columns
    }


def suggest_tables(df: pd.DataFrame, filename: str = "") -> list[Suggestion]:
    """Candidate tables for this file, best first. Never raises; may return []."""
    cols = _norm_cols(df)
    out: list[Suggestion] = []

    for needed, table, mode, why in FINGERPRINTS:
        matched = {c for c in needed if c in cols or c.replace(" ", "_") in cols}
        if not matched:
            continue
        coverage = len(matched) / len(needed)
        if coverage < 0.6:
            continue
        out.append(Suggestion(table, mode, int(coverage * 100), why))

    # Learning files: figure out platform from the content, and point at the right platform's table.
    derived = derive_learning_taxonomy(df, filename)
    platform = derived.get("platform")

    # A COMBINED export can hold several platforms at once (the participation sheet carries both
    # Atlas and Beacon rows). Suggest a table for each so the file is split, not mis-filed whole.
    platforms = platforms_present(df, filename)
    if len(platforms) > 1:
        out = [s for s in out if "training_completions" not in s.table]
        for i, plat in enumerate(platforms):
            tbl = ("atlas_training_completions" if plat == "Atlas"
                   else "beacon_training_completions" if plat == "Beacon"
                   else "plantrack_analytics_trainings")
            mode = (WriteMode.NEW_TABLE_PERIOD if plat == "PlanTrack" else WriteMode.APPEND)
            out.insert(i, Suggestion(
                tbl, mode, 95,
                f"file contains {plat} rows too — it mixes {', '.join(platforms)}, so each "
                "platform's rows go to its own table",
                derived={"platform": plat}))
        best_multi: dict[str, Suggestion] = {}
        for s in out:
            if s.table not in best_multi or s.confidence > best_multi[s.table].confidence:
                best_multi[s.table] = s
        return sorted(best_multi.values(), key=lambda s: -s.confidence)

    if platform in ("Atlas", "Beacon"):
        table = ("atlas_training_completions" if platform == "Atlas"
                 else "beacon_training_completions")
        out = [s for s in out if "training_completions" not in s.table]
        bits = ", ".join(f"{k}={v}" for k, v in derived.items())
        out.insert(0, Suggestion(
            table, WriteMode.APPEND, 95,
            f"content says this is {platform} learning data ({bits})", derived=derived))
    elif platform == "PlanTrack":
        out = [s for s in out if "training_completions" not in s.table]
        out.insert(0, Suggestion(
            "plantrack_analytics_trainings", WriteMode.NEW_TABLE_PERIOD, 95,
            "content says this is PlanTrack learning data", derived=derived))

    if any(s.table == "databricks_trainings_participants" for s in out):
        for s in out:
            if s.table == "databricks_trainings_participants":
                s.schema = GOLD

    # De-dup, keep the highest confidence per table.
    best: dict[str, Suggestion] = {}
    for s in out:
        if s.table not in best or s.confidence > best[s.table].confidence:
            best[s.table] = s
    return sorted(best.values(), key=lambda s: -s.confidence)


def derive_learning_taxonomy(df: pd.DataFrame, filename: str = "") -> dict[str, str]:
    """Work out platform / course / language for a learning file, from columns then filename.

    This is what turns "which platform, which language, which course was completed" into data the
    unified completions tables can hold — the individual course exports carry none of it.
    """
    out: dict[str, str] = {}
    cols = {normalise_text(c).lower(): c for c in df.columns}

    def first_value(col: str) -> str | None:
        vals = df[col].dropna().astype(str)
        return normalise_text(vals.iloc[0]) if len(vals) else None

    # Platform: an explicit column wins, else the filename.
    plat_col = next((cols[c] for c in cols if "platform" in c), None)
    if plat_col:
        out["platform"] = detect_platform(first_value(plat_col)) or ""
    if not out.get("platform"):
        out["platform"] = detect_platform(filename) or ""

    # Course + language: an explicit column wins, else the filename.
    course_col = next((cols[c] for c in cols if c in ("course", "course_name")), None)
    src = first_value(course_col) if course_col else filename
    lang_col = next((cols[c] for c in cols if "language" in c), None)
    lang = detect_language(first_value(lang_col)) if lang_col else None
    out["language"] = lang or detect_language(src) or detect_language(filename) or ""

    canon = canonical_course(out.get("platform") or None, src)
    if canon:
        out["course_name"] = canon
    return {k: v for k, v in out.items() if v}


def taxonomy_breakdown(df: pd.DataFrame, filename: str = "") -> pd.DataFrame | None:
    """Per-row platform/course/language/status counts — 'who completed what, in which language'.

    Used for the upload preview so the derivation is visible BEFORE anything is written.
    """
    cols = {normalise_text(c).lower(): c for c in df.columns}
    plat_col = next((cols[c] for c in cols if "platform" in c), None)
    course_col = next((cols[c] for c in cols if c in ("course", "course_name")), None)
    lang_col = next((cols[c] for c in cols if "language" in c), None)
    status_col = next((cols[c] for c in cols if c == "status"), None)
    if not (course_col or plat_col):
        return None

    work = pd.DataFrame()
    work["Platform"] = (
        df[plat_col].map(detect_platform) if plat_col
        else (detect_platform(filename) or "—")
    )
    if course_col:
        plats = work["Platform"]
        work["Course"] = [
            canonical_course(p, c) or normalise_text(c)
            for p, c in zip(plats, df[course_col])
        ]
    else:
        work["Course"] = derive_learning_taxonomy(df, filename).get("course_name", "—")
    work["Language"] = (
        df[lang_col].map(detect_language) if lang_col
        else (derive_learning_taxonomy(df, filename).get("language") or "—")
    )
    if status_col:
        work["Status"] = df[status_col].astype(str)
    group = [c for c in ("Platform", "Course", "Language", "Status") if c in work.columns]
    return work.groupby(group, dropna=False).size().reset_index(name="Records")
