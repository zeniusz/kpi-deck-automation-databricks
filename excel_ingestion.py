"""Excel → Databricks bronze ingestion, following a documented set of ingestion rules.

This module replaces the manual Databricks web file-upload UI. It reads an Excel file, figures
out which bronze table it belongs to (by filename), picks the CORRECT write mode per the guide's
decision tree (NOT a single global mode), applies the documented cleaning, and can either:
  - DRY RUN (default): return a plan + cleaned preview WITHOUT touching Databricks, or
  - WRITE: upload the source file to the UC Volume archive first, then write the table.

The rules below come from the (private) ingestion guide of the original deployment. Key rules
encoded here:
  * ALWAYS archive the raw file to the UC Volume first, THEN write the table.
  * FULL_REPLACE   — cumulative source exports (community, databricks registrations).
  * APPEND         — monthly snapshots, tagged with reporting_month (teams listing, guild,
                     unified completions). Never overwrite (would lose history).
  * NEW_TABLE_PERIOD — individual learning snapshots get a _YYYY_MM suffixed table per period.
  * NEW_TABLE_EVENT  — a new event gets analytics_event_number_N, then events_all is rebuilt.
  * Country: bracket ISO code in Company "(AT)" → primary; email domain → fallback; missing →
    the default country. Events: drop attendance ≤ 1 min; dedup keep longer duration.

Write path uses databricks-sql-connector (same lib the app already depends on). Nothing here
writes unless mode="write" is passed AND a connection is available.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

import pandas as pd

import org_profile
from settings import (
    BRONZE_SCHEMA as BRONZE,
    CATALOG,
    DATABRICKS_PROFILE,
    GOLD_SCHEMA as GOLD,
    VOLUME_ROOT,
)

logger = logging.getLogger(__name__)

DEFAULT_COUNTRY = org_profile.DEFAULT_COUNTRY  # missing country → the organisation's HQ country


class WriteMode(str, Enum):
    FULL_REPLACE = "full_replace"          # cumulative source exports
    APPEND = "append"                      # monthly snapshots (reporting_month tag)
    NEW_TABLE_PERIOD = "new_table_period"  # learning snapshots, _YYYY_MM table per period
    NEW_TABLE_EVENT = "new_table_event"    # new community event
    ARCHIVE_ONLY = "archive_only"          # derived output: file is kept, no table is written


# ── Country derivation (guide §5.1, Rule 7) ────────────────────────────────────────────────
COUNTRY_BY_ISO = org_profile.COUNTRY_BY_ISO
# The domain → country mapping lives in the org profile. There used to be a small map here AND a larger
# one in derivation.py, and ingestion used the small one — so many subsidiary domains silently fell
# through to the default-country fallback.
#
# The map is NOT duplicated here. `derivation` imports this module (for CATALOG/COUNTRY_BY_ISO), so a
# module-level import back would be a circular import — it appeared to work only because
# excel_ingestion happened to be imported first, and `import derivation` alone crashed. The lookup is
# resolved lazily in `_domain_country()` instead.
def _domain_country() -> dict[str, str]:
    """The one authoritative domain→country map, imported lazily to avoid a circular import."""
    from derivation import DOMAIN_COUNTRY

    return DOMAIN_COUNTRY
# PlanTrack German country names → English. This is the DLMV's CASE WHEN table, verbatim from the
# guide (§5b "Country Normalization"), all 19 rows. Applied at the DASHBOARD/report layer only —
# never in bronze (rule 8). NOTE the guide normalises Tschechien to "Czechia", not "Czech Republic":
# those are different strings and mixing them splits a dashboard grouping, so follow the guide.
GERMAN_COUNTRY = org_profile.LOCAL_COUNTRY_NAMES
_ISO_BRACKET = re.compile(r"\(([A-Z]{2})\)")


def derive_country(company: str | None, email: str | None) -> str:
    """Primary = ISO code in Company brackets (rule 7); fallback = email domain (rule 6); the default country.

    The bracket regex is case-sensitive on purpose — `(CZ)` is a country code, `(cz)` is not — but the
    DOMAIN lookup is lowercased, because source files really do write `Contoso.example` capitalised and
    a case-sensitive miss there means a silent default-country fallback.
    """
    if company:
        m = _ISO_BRACKET.search(str(company))
        if m and m.group(1) in COUNTRY_BY_ISO:
            return COUNTRY_BY_ISO[m.group(1)]
    if email and "@" in str(email):
        domain = str(email).split("@")[-1].strip().lower()
        hit = _domain_country().get(domain)
        if hit:
            return hit
    # Guide §9b step 4: external consultants resolve by COMPANY NAME contains, because their email
    # domain may be a client's or a personal one. Runs before the default-country fallback.
    from derivation import country_from_company_name

    return country_from_company_name(company) or DEFAULT_COUNTRY


def derive_company(company: str | None, email: str | None) -> str | None:
    """Rule 6: the domain lookup gives Company as well as Country. Existing value always wins."""
    if company and str(company).strip():
        return str(company).strip()
    from derivation import company_from_domain

    return company_from_domain(email)


def normalize_german_country(value: str | None) -> str:
    if value is None or str(value).strip() == "":
        return DEFAULT_COUNTRY
    return GERMAN_COUNTRY.get(str(value).strip(), str(value).strip())


def strip_company_bracket(company: str | None) -> str | None:
    """'Fabrikam (CZ)' → 'Fabrikam'. The display rule that pairs with derive_country().

    ORDER MATTERS: the bracket is the primary source for Country, so it must be read BEFORE it is
    stripped — strip first and every row falls through to the default-country fallback. Callers do the country
    fill first, then this.
    """
    if company is None:
        return None
    return _ISO_BRACKET.sub("", str(company)).strip() or None


# ── German meeting-duration parsing + event cleaning (guide §5.4, event-clean notebook) ─────
def parse_duration_minutes(text: str | None) -> float:
    """'1h 30m 45s' / '45m' / '59s' → minutes. Missing → 0."""
    if not text:
        return 0.0
    s = str(text)
    h = re.search(r"(\d+)h", s)
    m = re.search(r"(\d+)m", s)
    sec = re.search(r"(\d+)s", s)
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0) + (
        (int(sec.group(1)) / 60.0) if sec else 0
    )


# ── File → table routing (guide §3 inventory) ──────────────────────────────────────────────
@dataclass
class TableRoute:
    table: str                      # bronze table (short name, no catalog/schema)
    mode: WriteMode
    volume_subpath: str             # where the raw file is archived under VOLUME_ROOT
    schema: str = BRONZE
    cleaning: str = "none"          # which cleaning routine to apply
    note: str = ""


# Ordered (pattern, route) — first regex match on the lowercased filename wins.
ROUTES: list[tuple[re.Pattern, TableRoute]] = [
    # Event 1 is the LAUNCH event and its table name carries that suffix. Matched on `\b1\b` (or the
    # word "launch"), NOT `.*1`, which also matched "Event Number 12" and routed it to event 1.
    (re.compile(r"event\D*\b1\b|event.*launch"), TableRoute(
        "analytics_event_number_1_launch", WriteMode.NEW_TABLE_EVENT,
        "communityevents/Analytics Teams Events", cleaning="event",
        note="One-time event table; also rebuild analytics_events_all + rerun Guild enrichment.")),
    # Events 2..N. One pattern rather than a hardcoded 2/3/4: the guide's Scenario C is "when Event 5
    # happens", and an enumerated list would silently NOT match Event 5 — it would fall through to the
    # events_all rule and append a single event's rows into the consolidated table. The event number
    # is captured from the filename by `_event_number()`.
    (re.compile(r"event.*number.*(\d+)"), TableRoute(
        "analytics_event_number_N", WriteMode.NEW_TABLE_EVENT,
        "communityevents/Analytics Teams Events", cleaning="event",
        note="One-time event table; then append the new rows to analytics_events_all and rerun the "
             "Guild enrichment so Events_Attended is updated.")),
    # NOT full replace (user, 2026-07-28): "here no full replace, just always adding upcoming event
    # to this stuff". Each event happened once and its rows never change, so a new event's attendees
    # are APPENDED to the consolidated table. Replacing it would rewrite four events' history to
    # re-derive rows that are already correct, and rule 3 lists only the three cumulative-source
    # tables as replace exceptions — this is not one of them.
    (re.compile(r"events?_all|events all"), TableRoute(
        "analytics_events_all", WriteMode.APPEND,
        "communityevents/Analytics Teams Events", cleaning="event",
        note="Consolidated events — the new event's rows are APPENDED; earlier events untouched.")),
    # ORDER MATTERS: "Unregistered_Members_2026_07.xlsx" is a DERIVED output (Teams members who never
    # registered), not a source table. It must be caught before the community/teams patterns, which
    # would otherwise append it into analytics_teams_listing and double-count those people.
    (re.compile(r"unregistered"), TableRoute(
        "", WriteMode.ARCHIVE_ONLY,
        "registrations/Analytics TEAMS Community/TEAMS listing", cleaning="none",
        note="Derived report (Teams members not in the registration form) — archived to the Volume "
             "only. It has no table of its own; it is regenerated by comparing the listing against "
             "analytics_community.")),
    # A Lakehouse Guild export is also named "... TEAMS listing ...".  It must be caught before
    # the generic Teams-listing rule below; otherwise its members are appended to the Analytics
    # Community snapshot and both dashboards report the union instead of the real Community
    # population.  Keep this pattern specific to a listing so Guild registration
    # forms continue to use their own cumulative table.
    (re.compile(r"guild.*teams.?listing"), TableRoute(
        "lakehouse_guild", WriteMode.APPEND,
        "registrations/Lakehouse Guild", cleaning="guild",
        note="Monthly Lakehouse Guild snapshot → APPEND with Reporting_Month tag; kept separate "
             "from the Analytics Community TEAMS listing.")),
    # The TEAMS-listing monthly snapshot. Checked BEFORE the community pattern because
    # "Analytics Community 2026.08.xlsx" / "Analytics_Community_2026_07.xlsx" is a listing
    # export, not the registration form — the bare `YYYY.MM` / `YYYY_MM` suffix (no parens, no
    # day) is what distinguishes it from the dated registration-form archive copy
    # "Analytics Community (2026.07.03).xlsx", which has a day and is wrapped in parens.
    # The old pattern required an underscore right before the year (`community_20\d\d`), which
    # missed the real space-separated filename "Analytics Community 2026.08.xlsx" — it fell
    # through to the registration-form pattern below and got FULL_REPLACE'd instead of APPENDed.
    (re.compile(r"teams.?listing|teams channel"
                r"|analytics[\s&_-]*communit\w*[\s_-]+20\d\d[._-]\d\d(?!\.\d)"),
     TableRoute("analytics_teams_listing", WriteMode.APPEND,
        "registrations/Analytics TEAMS Community/TEAMS listing", cleaning="teams_listing",
        note="Monthly snapshot → APPEND with reporting_month tag; Country + Company derived from "
             "the email domain.")),
    # The registration Forms export. Also matches the dated archive copies
    # "Analytics Community (2026.07.03).xlsx" — the parenthesised date made the old pattern miss.
    # `[\s&_-]*` between the words, so all three real spellings match: "Full Analytics Community",
    # "Analytics Community (2026.07.03)" and "analytics_community". A single `.?` missed the " & ".
    (re.compile(r"communit(y|ies).*(registration|full|form)|full.*communit"
                r"|analytics[\s&_-]*communit"),
     TableRoute("analytics_community", WriteMode.FULL_REPLACE,
        "registrations/Analytics TEAMS Community", cleaning="community",
        note="Cumulative Forms export → FULL REPLACE (dedup by Email, keep newest submission).")),
    # GOLD: the curated participants list. Checked BEFORE the registration pattern, because
    # "Databricks Trainings Participants.xlsx" contains both words and would otherwise be written into
    # the bronze registration table — a different schema entirely (Level/Start/End/Trainers).
    (re.compile(r"participants?"), TableRoute(
        "databricks_trainings_participants", WriteMode.FULL_REPLACE,
        "trainings/Databricks Trainings", schema=GOLD, cleaning="none",
        note="GOLD table. Source Excel is cumulative (a new cohort is added as extra rows), so it is "
             "a FULL REPLACE — rebuilt by its notebook, not written here.")),
    # ── The two Databricks registration forms, which are easy to confuse ────────────────────────
    # "Registration for Lakehouse COMMUNITY(1-40).xlsx"  → joining the Guild
    # "Registration for Databricks TRAININGS(1-89).xlsx"  → signing up for a training level
    # They are different forms with different schemas (the Guild one has an Interests question and no
    # level/manager questions), and different tables. The old single pattern matched
    # `registration.*databricks` and sent BOTH to the trainings table — so the Guild registrations
    # would have overwritten it, since it is FULL REPLACE. The Guild form is matched FIRST because
    # its name can also contain "databricks".
    (re.compile(r"registrations?.*lakehouse.*communit|registrations?.*guild|guild.*registration"), TableRoute(
        "registrations_for_teams_lakehouse_guild", WriteMode.FULL_REPLACE,
        "registrations/Lakehouse Guild", cleaning="registration",
        note="Guild JOIN form (cumulative Forms export) → FULL REPLACE. NOTE this is the "
             "registration form for joining the Lakehouse Guild — not the trainings sign-up, and "
             "not the monthly `lakehouse_guild` membership snapshot.")),
    # Generic Guild snapshot. Deliberately AFTER the join-form rule above: a file named
    # "Lakehouse Guild registration…" contains "guild" too, and matching this broader rule first made
    # the join-form alternative unreachable (the form would have been appended to the monthly snapshot).
    (re.compile(r"guild"), TableRoute(
        "lakehouse_guild", WriteMode.APPEND,
        "registrations/Lakehouse Guild", cleaning="guild",
        note="Monthly snapshot → APPEND with Reporting_Month tag.")),
    (re.compile(r"registration.*databricks|databricks.*registration|1-\d+"), TableRoute(
        "registration_for_databricks_trainings", WriteMode.FULL_REPLACE,
        "registrations/Databricks Trainings", cleaning="registration",
        note="Trainings sign-up form (cumulative Forms export) → FULL REPLACE; then rebuild gold "
             "participants.")),
    # Standalone reference data: Contoso headcount/FTE per entity. NOT part of the community flow — no
    # domain mapping, no dedup. The sheet itself is cumulative (a new quarter is added as extra rows),
    # so re-uploading the whole file is a FULL REPLACE. Volume folders are quarters: `2025 Q4/`.
    (re.compile(r"sales.?admin|employee.?numbers|headcount|fte"), TableRoute(
        "employee-numbers", WriteMode.FULL_REPLACE,
        "employee_numbers", cleaning="none",
        note="Reference data (headcount/FTE per entity, all periods in one cumulative sheet) → FULL "
             "REPLACE. Only check the columns are present; no derivation or dedup. NOTE the table "
             "name uses HYPHENS, so it always needs backticks in SQL.")),
    (re.compile(r"plantrack"), TableRoute(
        "plantrack_analytics_trainings", WriteMode.NEW_TABLE_PERIOD,
        "trainings/Plan Track", cleaning="plantrack",
        note="New table per period (_YYYY_MM). German country names normalized.")),
    (re.compile(r"atlas.*completion"), TableRoute(
        "atlas_training_completions", WriteMode.APPEND,
        "trainings/Atlas", cleaning="learning",
        note="Unified completions → APPEND new period rows.")),
    (re.compile(r"beacon.*completion"), TableRoute(
        "beacon_training_completions", WriteMode.APPEND,
        "trainings/Beacon Platform", cleaning="learning",
        note="Unified completions → APPEND new period rows.")),
    (re.compile(r"atlas"), TableRoute(
        "atlas", WriteMode.NEW_TABLE_PERIOD, "trainings/Atlas", cleaning="learning",
        note="Individual course snapshot → NEW table per period (_YYYY_MM).")),
    (re.compile(r"beacon|international"), TableRoute(
        "beacon", WriteMode.NEW_TABLE_PERIOD, "trainings/Beacon Platform", cleaning="learning",
        note="Individual course snapshot → NEW table per period (_YYYY_MM).")),
    # Revenue demo table. APPEND monthly: each month's Excel adds ~50 new rows below the
    # existing ones (2026.07, 2026.08, 2026.09 …), tagged in the Reporting_Month column that this
    # table already uses. Target is the *_with_reporting_month table (the monthly one), NOT the
    # *_50_rows_with_activity snapshot.
    (re.compile(r"revenue|company.*data|fake_company"), TableRoute(
        "fake_company_data_with_reporting_month", WriteMode.APPEND,
        "testing", cleaning="none",
        note="Revenue dashboard source (fake demo data). APPEND monthly — new rows tagged with Reporting_Month.")),
]


@dataclass
class IngestionPlan:
    filename: str
    route: TableRoute | None
    reporting_month: str | None
    rows_in: int
    rows_out: int
    columns: list[str]
    warnings: list[str] = field(default_factory=list)
    preview: pd.DataFrame | None = None

    @property
    def full_table(self) -> str | None:
        if not self.route or self.route.mode == WriteMode.ARCHIVE_ONLY:
            # No table at all — the file is archived to the Volume and nothing is written. Returning
            # a name here produced '<catalog>.<schema>.' (trailing dot) for the derived
            # Unregistered_Members report.
            return None
        name = self.route.table
        if self.route.mode == WriteMode.NEW_TABLE_PERIOD and self.reporting_month:
            name = f"{name}_{self.reporting_month.replace('.', '_')}"
        return f"{CATALOG}.{self.route.schema}.{name}"

    @property
    def volume_target(self) -> str | None:
        if not self.route:
            return None
        sub = self.route.volume_subpath
        if self.reporting_month:
            sub = f"{sub}/{self.reporting_month.replace('.', '_')}"
        return f"{VOLUME_ROOT}/{sub}/{self.filename}"


_EVENT_NUMBER = re.compile(r"event\D*(\d+)", re.I)


def event_number(filename: str) -> int | None:
    """The event number in a filename: 'Analytics Event Number 5.xlsx' → 5."""
    m = _EVENT_NUMBER.search(str(filename))
    return int(m.group(1)) if m else None


def route_for(filename: str) -> TableRoute | None:
    """The bronze table + write mode for an uploaded file.

    Per-course learning exports are resolved FIRST, against the registry in learning_metadata: each
    course has its own 3-column table (`atlas_datagovernance_basics_2026_07`), so they must not
    fall through to the generic `atlas`/`beacon` patterns below, which would pile every course into
    one table and lose the platform/course/language identity that only the table name carries.
    """
    from learning_metadata import (
        PLANTRACK,
        derive_metadata,
        platform_from_export_id,
        table_stem_for_filename,
    )

    stem = table_stem_for_filename(filename)
    if stem:
        meta = derive_metadata(stem)
        sub = {"Atlas": "trainings/Atlas",
               "Beacon": "trainings/Beacon Platform",
               PLANTRACK: "trainings/Plan Track"}[meta.platform]
        return TableRoute(
            stem, WriteMode.NEW_TABLE_PERIOD, sub,
            cleaning="plantrack" if meta.platform == PLANTRACK else "learning",
            # Say WHY this platform was chosen. Both platforms' raw exports are `…_export_eLearning…`,
            # so if the id prefix is what decided it, showing that makes a misroute visible in the
            # preview instead of after the write.
            note=(f"{meta.platform} · {meta.course_name or 'course per row'} · {meta.language}"
                  + (f" (id prefix {filename.split('_')[0]} → {meta.platform})"
                     if platform_from_export_id(filename) else "")
                  + " — own per-period table; metadata is stamped when the unified completions "
                    "table is rebuilt."),
        )

    low = filename.lower()
    for pattern, route in ROUTES:
        if pattern.search(low):
            if route.table == "analytics_event_number_N":
                # Resolve the placeholder to the real event number. `replace()` returns a COPY —
                # ROUTES holds one shared TableRoute per pattern, so mutating it would leak event 5's
                # table name into every later upload in the same process.
                n = event_number(filename)
                if n is None:
                    return None
                return replace(route, table=f"analytics_event_number_{n}")
            return route
    return None


_MONTH_NAMES = {
    "january": "01", "february": "02", "march": "03", "april": "04", "may": "05",
    "june": "06", "july": "07", "august": "08", "september": "09", "october": "10",
    "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06", "jul": "07",
    "aug": "08", "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
}


_EXPORT_STAMP = re.compile(r"(20\d{2})[._\- ]?(\d{2})[._\- ]?(\d{2})[_\- ]\d{3,4}(?!\d)")


def export_timestamp_month(filename: str) -> str | None:
    """The month inside an LMS export TIMESTAMP (`..._20260803_0838.xlsx` → '2026.08').

    Deliberately separate from `infer_reporting_month`: this is when the file was GENERATED, which is
    usually the month AFTER the period it reports on. Only used to inform the user, never to set the
    period.
    """
    m = _EXPORT_STAMP.search(str(filename))
    return f"{m.group(1)}.{m.group(2)}" if m else None


def infer_reporting_month(filename: str) -> str | None:
    """Detect the reporting PERIOD from the filename, as YYYY.MM. Handles:
      - numeric:    '2026_06' / '2026.06' / '2026-06'   → '2026.06'
      - reversed:   '12.2025'                           → '2025.12'
      - month name: 'September_2026' / 'Sep 2026'        → '2026.09'

    An LMS export timestamp (`..._20260803_0838.xlsx`) returns None on purpose — see below.
    """
    # A full YYYYMMDD stamp is the EXPORT TIMESTAMP, not the reporting period — the LMS names its files
    # `..._20260803_0838.xlsx` meaning "generated 3 Aug at 08:38". The guide is explicit that the period
    # is the DATA PERIOD ("re-running in August for July data → 2026.07"), so a timestamp must NOT
    # override the month the user picked. It is only a last-resort hint, returned by
    # `export_timestamp_month()` and used solely when nothing else is known.
    #
    # Returning it from here made a July upload land in August: the picker is passed as a fallback, and
    # a filename match always beat it.
    m = re.search(r"(20\d{2})[._\- ]?(\d{2})[._\- ]?(\d{2})[_\- ]\d{3,4}(?!\d)", filename)
    if m:
        return None  # an export timestamp — the caller's picked month wins
    m = re.search(r"(20\d{2})[._\- ]?(\d{2})(?!\d)", filename)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}.{m.group(2)}"
    # REVERSED, MM.YYYY — the employee-numbers sheet is named `Sales_Admin_12.2025.xlsx`.
    m = re.search(r"(?<!\d)(0[1-9]|1[0-2])[._\- ](20\d{2})(?!\d)", filename)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    low = filename.lower()
    name = re.search(r"(january|february|march|april|may|june|july|august|september|october|"
                     r"november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)", low)
    year = re.search(r"(20\d{2})", filename)
    if name and year:
        return f"{year.group(1)}.{_MONTH_NAMES[name.group(1)]}"
    return None


# ── Cleaning routines (return cleaned df + warnings) ────────────────────────────────────────
def _clean_event(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    dur_col = next((c for c in df.columns if "Dauer" in c or "duration" in c.lower()), None)
    if dur_col is None:
        return df, ["No duration column found — skipped ≤1min removal."]
    mins = df[dur_col].map(parse_duration_minutes)
    keep = mins > 1
    removed = int((~keep).sum())
    df2 = df[keep].copy()
    warnings.append(f"Removed {removed} attendee(s) with duration ≤ 1 minute.")
    # Dedup: keep the longest-duration row per person (by email if there is a usable one, else Name).
    # find_email_column() rather than an exact header match — it checks the VALUES really are
    # addresses, so an "anonymous" column can't be chosen (that is what made the community export
    # look like N copies of one person).
    key = find_email_column(df2) \
        or next((c for c in df2.columns if c.lower() == "name"), None)
    if key is not None:
        df2 = df2.assign(_min=mins[keep].values).sort_values("_min", ascending=False)
        before = len(df2)
        # Rows with a BLANK key are never collapsed — drop_duplicates treats every empty string as
        # the same value, so blank-email attendees would fold into one person. Such rows
        # exist in real exports and they are distinct people.
        blank = df2[key].isna() | (df2[key].astype(str).str.strip() == "")
        deduped = df2[~blank].drop_duplicates(subset=[key], keep="first")
        df2 = pd.concat([deduped, df2[blank]]).sort_index().drop(columns="_min")
        if before - len(df2):
            warnings.append(f"Deduplicated {before - len(df2)} repeat attendee(s) (kept longest).")
        if int(blank.sum()):
            warnings.append(
                f"{int(blank.sum())} attendee(s) have no {key} — kept as-is (no identity to "
                "de-duplicate on)."
            )
    return df2, warnings


def _as_object(series):
    """Cast to object dtype if it isn't already, so strings can be assigned into it.

    An all-empty column (e.g. `Country` in the per-course Atlas/Beacon exports, which ship
    Company/Status/Country with Country blank) reads back from Excel as float64 all-NaN. Assigning
    derived country strings into a float column raises TypeError on pandas ≥2.2 and killed the whole
    upload, so every fill site casts first.
    """
    return series if series.dtype == object else series.astype(object)


def _clean_listing(df: pd.DataFrame, period: str | None,
                   label: str) -> tuple[pd.DataFrame, list[str]]:
    """Clean a monthly membership listing: Teams listing and Lakehouse Guild (guide §5.2, §5.3).

    Derives Country and Company from the email domain. No deduplication — the source export is
    already deduplicated by the time it is uploaded, so removing rows here would silently drop
    real members (e.g. an alias-aware dedup removing one row from a file that was already unique).
    """
    from derivation import UNKNOWN_COMPANY, company_from_domain

    warnings: list[str] = []
    out = df.copy()
    email = find_email_column(out)
    if email is None:
        return out, [f"⚠ {label}: no email column recognised — Country/Company cannot be derived "
                     "and duplicates cannot be removed. Check the export's headers."]
    warnings.append(f"{label}: using `{email}` as the email column.")
    missing_email = int(out[email].isna().sum() + (out[email].notna() &
                        (out[email].astype(str).str.strip() == "")).sum())
    if missing_email:
        count_note = (
            "included in the raw snapshot total"
            if label == "Teams listing"
            else "excluded from unique-member KPI counts"
        )
        warnings.append(
            f"{label}: kept {missing_email} row(s) with no email for audit/history; they are "
            f"{count_note}."
        )

    country = next((c for c in out.columns if c.lower() == "country"), None) or "Country"
    company = next((c for c in out.columns if c.lower() == "company"), None) or "Company"
    for col in (country, company):
        if col not in out.columns:
            # None, NOT pd.NA: `if company:` inside derive_country raises
            # "boolean value of NA is ambiguous" on a pd.NA, killing the whole upload.
            out[col] = None

    # Country — domain first, then the consultant CONTAINS rule, else the default country.
    cmask = out[country].isna() | (out[country].astype(str).str.strip() == "")
    if cmask.any():
        out[country] = _as_object(out[country])
        out.loc[cmask, country] = out[cmask].apply(
            lambda r: derive_country(r.get(company), r.get(email)), axis=1)
        spread = ", ".join(f"{k} {v}" for k, v in
                          out.loc[cmask, country].value_counts().head(6).items())
        warnings.append(f"{label}: derived Country for {int(cmask.sum())} row(s) → {spread}.")

    # Company — the same domain lookup. 'Unknown' for an unmapped domain (guide §9b step 3).
    omask = out[company].isna() | (out[company].astype(str).str.strip() == "")
    if omask.any():
        out[company] = _as_object(out[company])
        out.loc[omask, company] = out[omask].apply(
            lambda r: company_from_domain(r.get(email), UNKNOWN_COMPANY), axis=1)
        known = int((out.loc[omask, company] != UNKNOWN_COMPANY).sum())
        warnings.append(
            f"{label}: derived Company for {known} of {int(omask.sum())} row(s) from the email "
            f"domain (the rest are '{UNKNOWN_COMPANY}' — shared or unmapped domain)."
        )

    return out, warnings


def _clean_community(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    # Use find_email_column(), NOT `== "email"`. The real Forms export has TWO mail columns:
    # `Email` is literally "anonymous" on every row (anonymous form) and the addresses live in
    # `Email2`. Matching the header exactly picked the anonymous one, so every row looked identical:
    # dedup found N-1 "duplicates", and after that was fixed it found none at all — while genuine
    # duplicate registrations sat in Email2 untouched.
    email = find_email_column(df)
    company = next((c for c in df.columns if c.lower() == "company"), None)
    country = next((c for c in df.columns if c.lower() == "country"), None)
    df2 = df.copy()
    if country is None:
        df2["Country"] = ""
        country = "Country"
    mask = df2[country].isna() | (df2[country].astype(str).str.strip() == "")
    if mask.any():
        df2[country] = _as_object(df2[country])
        df2.loc[mask, country] = df2[mask].apply(
            lambda r: derive_country(r.get(company), r.get(email)), axis=1)
        warnings.append(f"Derived Country for {int(mask.sum())} row(s) (bracket→email→default).")
    # Dedup by Email, keeping the newest submission (guide §5.1).
    #
    # BLANK EMAILS ARE NEVER COLLAPSED. `drop_duplicates(subset=[email])` treats every empty string as
    # the SAME value, so a Forms export whose Email column is empty (anonymous responses) collapsed
    # every real person into 1 row — silent, catastrophic data loss. Rows without an email have no
    # identity to de-duplicate on, so they pass through untouched.
    #
    # Matching is alias-aware for the same reason as the Guild: cloud / contoso / corp domains is
    # one person (guide rule 10), so a plain string compare would keep three copies of them.
    if email is not None:
        from derivation import alias_key, alias_priority

        fin = next((c for c in df2.columns if "Fertigstellung" in c or "completion" in c.lower()),
                   None)
        before = len(df2)
        df2["__k"] = df2[email].map(alias_key)          # None when there is no usable email
        keyed = df2["__k"].notna()
        n_blank = int((~keyed).sum())

        dupes = df2[keyed]
        # Newest submission first, then the highest-priority alias domain, so the survivor is the row
        # the guide says to keep. `kind="stable"` keeps file order as the final tie-break.
        if fin is not None:
            dupes = dupes.sort_values(fin, ascending=False, kind="stable")
        dupes = dupes.assign(__p=dupes[email].map(alias_priority)).sort_values(
            "__p", kind="stable")
        dupes = dupes.drop_duplicates(subset=["__k"], keep="first").drop(columns="__p")

        df2 = pd.concat([dupes, df2[~keyed]]).sort_index().drop(columns="__k")
        removed = before - len(df2)
        if removed:
            warnings.append(
                f"Deduplicated {removed} row(s) by Email (kept the newest submission; "
                "cloud/contoso/corp domains treated as one person)."
            )
        if n_blank:
            warnings.append(
                f"{n_blank} row(s) have no email — kept as-is, since there is no identity to "
                "de-duplicate on. Check the export included the email column."
            )
    return df2, warnings


# ── Forms question text → the column name the LIVE table already uses ───────────────────────────
# Verified against the live schemas 2026-08-03. A Microsoft Forms export names its columns after the
# QUESTION ("Your work email", "What contents and formats are you especially interested in?"), but the
# bronze tables were created with tidy names. Writing the raw headers into
# `registrations_for_teams_lakehouse_guild` would ALTER TABLE ADD 12 near-duplicate columns
# (`Work_Email` AND `Your work email`) and leave the originals NULL — the table would still "work",
# which is what makes it dangerous.
#
# Matched on a normalised form (lowercase, non-alphanumerics collapsed) so trailing newlines and
# punctuation in the question text do not matter.
_FORMS_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "ID":              ("id",),
    "Start_Time":      ("start time", "startzeit"),
    "Completion_Time": ("completion time", "fertigstellungszeit"),
    "Email":           ("email",),
    "Work_Email":      ("your work email", "work email"),
    "First_Name":      ("first name",),
    "Last_Name":       ("last name",),
    "Company":         ("what company are you from", "company"),
    "Department":      ("your department", "department"),
    "Job_Title":       ("job title",),
    "Interests":       ("what contents and formats are you especially interested in",),
}

# The Community form's live table predates the English Forms export and therefore uses two German
# timestamp headers. More importantly, Forms writes the useful address to `Email2` while its plain
# `Email` column contains the literal word "anonymous". These priorities must run before generic
# exact-name matching or a direct replacement would discard every real address.
_TABLE_FORMS_COLUMN_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "analytics_community": {
        "Startzeit": ("start time",),
        "Fertigstellungszeit": ("completion time",),
        "Email": ("email2",),
    },
}


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def align_to_table_schema(df: pd.DataFrame, table: str,
                          live_columns: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Rename Forms question-text columns to the names `table` already uses, and drop the rest.

    Only applied when the live column list is known. Anything with no counterpart in the table is
    dropped rather than added: these are cumulative Forms exports whose schema is fixed, so an
    unexpected column means the form changed and that is worth reporting, not silently absorbing.
    """
    if not live_columns:
        return df, []
    live_by_norm = {_norm_header(c): c for c in live_columns}
    rename: dict[str, str] = {}
    claimed_targets: set[str] = set()
    preferred_sources: set[str] = set()

    # Resolve table-specific source priorities first. A claimed target prevents a lower-priority
    # exact-name source (notably the anonymous `Email`) from being retained alongside it.
    for wanted, aliases in _TABLE_FORMS_COLUMN_ALIASES.get(table, {}).items():
        live = next((c for c in live_columns if c.lower() == wanted.lower()), None)
        source = next((c for c in df.columns if _norm_header(c) in aliases), None)
        if live and source:
            rename[source] = live
            claimed_targets.add(live)
            preferred_sources.add(source)

    ignored_sources: set[str] = set()
    for col in df.columns:
        if col in preferred_sources:
            continue
        n = _norm_header(col)
        if n in live_by_norm:
            target = live_by_norm[n]
            if target in claimed_targets:
                ignored_sources.add(col)
            else:
                rename[col] = target
            continue
        for target, aliases in _FORMS_COLUMN_ALIASES.items():
            if n in aliases and target.lower() in {c.lower() for c in live_columns}:
                live = next(c for c in live_columns if c.lower() == target.lower())
                if live not in claimed_targets:
                    rename[col] = live
                break
    out = df.drop(columns=list(ignored_sources)).rename(columns=rename)
    # Preserve the established live-table order as well as its names. This keeps a FULL REPLACE
    # schema stable even when Microsoft changes the order of questions in the export.
    keep = [c for c in live_columns if c in set(out.columns)]
    dropped = [c for c in out.columns if c not in set(live_columns)]
    notes = []
    if rename:
        notes.append(f"Renamed {len(rename)} column(s) to match `{table}`'s existing schema.")
    if ignored_sources:
        notes.append(
            "Ignored lower-priority Forms placeholder column(s): "
            + ", ".join(sorted(ignored_sources)) + "."
        )
    if dropped:
        notes.append(
            f"⚠ {len(dropped)} column(s) in the file have no counterpart in `{table}` and were "
            f"dropped: {', '.join(str(d)[:40] for d in dropped[:4])}"
            + ("…" if len(dropped) > 4 else "")
            + ". If the form gained a question, the table needs the column adding first."
        )
    missing = [c for c in live_columns if c not in set(out.columns)]
    if missing:
        notes.append(f"Table column(s) not present in this file: {', '.join(missing)}.")
    return out[keep], notes


def _status_breakdown(df: pd.DataFrame) -> list[str]:
    """Explain what the row count MEANS for a learning export.

    "rows: 43 → 43" is ambiguous on its own — it could look like nothing happened. For a learning file
    every row is one learner's status, and only the passed ones are a completion, so the KPI figure is
    a SUBSET of the row count. Spelling that out stops the total being read as the completions number.
    """
    col = next((c for c in df.columns if c.lower().strip() == "status"), None)
    if col is None:
        return []
    counts = df[col].fillna("(blank)").astype(str).str.strip().value_counts()
    parts = ", ".join(f"{v} {k}" for k, v in counts.items())
    passed = int(sum(v for k, v in counts.items() if str(k).lower().startswith("pass")))
    note = f"{len(df)} learner(s): {parts}."
    if passed and passed != len(df):
        note += (f" Only the {passed} passed count as completions — the other "
                 f"{len(df) - passed} are in the table too, and the dashboard filters on Status.")
    return [note]


def _dedup_registrations(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Databricks-training registrations: dedup on (person, LEVEL) — not on person alone.

    Someone genuinely registers more than once as they progress through the ladder, so collapsing by
    email would erase a completed training from the KPI count:

        j.doe@…     2026-04-21  previously completed: "No"
        j.doe@…     2026-06-11  previously completed: "Yes, Apply (Level 3)"

    But signing up TWICE for the same level IS a duplicate (a repeat sign-up:
    the same person submitted "Yes, Learn (Level 2)" in March and again in April). So the
    key is the pair, and the newest submission of a pair wins.
    """
    email = find_email_column(df)
    if email is None:
        return df, []
    level = next((c for c in df.columns if "level" in c.lower()), None)
    completed = next((c for c in df.columns
                      if "completion" in c.lower() or "last modified" in c.lower()), None)

    from derivation import alias_key

    out = df.copy()
    out["__k"] = out[email].map(alias_key)
    # A blank level is its own bucket ("" rather than NaN), otherwise two blank-level rows for the
    # same person would not compare equal and the duplicate would survive.
    out["__lvl"] = (out[level].fillna("").astype(str).str.strip().str.lower()
                    if level else "")
    keyed = out["__k"].notna()

    dupes = out[keyed]
    if completed is not None:
        dupes = dupes.sort_values(completed, ascending=False, kind="stable")
    before = len(out)
    dupes = dupes.drop_duplicates(subset=["__k", "__lvl"], keep="first")
    out = pd.concat([dupes, out[~keyed]]).sort_index().drop(columns=["__k", "__lvl"])

    removed = before - len(out)
    warnings: list[str] = []
    # The message has to match which key was actually used. There are TWO registration forms: the
    # trainings sign-up has a level question (so a repeat at a NEW level is real progression and must
    # be kept), while the Guild JOIN form has no levels at all — there, one person joining twice is
    # simply a duplicate. Reporting "same person AND same level" for a form with no level column was
    # both wrong and self-contradictory next to the "no level column found" warning.
    if level is not None:
        if removed:
            warnings.append(
                f"Removed {removed} duplicate registration(s) — same person AND same level "
                f"(`{level}`), newest kept. Re-registrations for a DIFFERENT level are kept: they "
                "are real progression up the ladder, not duplicates."
            )
        else:
            warnings.append("No duplicate registrations (same person + same level) found.")
    else:
        if removed:
            warnings.append(
                f"Removed {removed} duplicate registration(s) — same person, newest submission "
                "kept. (This form has no training-level question, so one person appearing twice is "
                "a duplicate rather than progression.)"
            )
        else:
            warnings.append("No duplicate registrations found (keyed on the person's email).")
    return out, warnings


def find_company_column(df: pd.DataFrame) -> str | None:
    """The column holding the company name — WITH its bracket country code.

    The Forms exports do not use a tidy "Company" header: the registration file calls it
    "What Contoso company are you from?" and the values look like "Contoso Group (DE)".
    Matching only `== "company"` missed it entirely, so every row fell through to the the default country
    default. Match on a substring, preferring the exact header when present.
    """
    exact = next((c for c in df.columns if c.lower().strip() == "company"), None)
    if exact:
        return exact
    return next((c for c in df.columns if "company" in c.lower()), None)


def find_email_column(df: pd.DataFrame) -> str | None:
    """The column holding a REAL person's email address.

    Order matters. A Forms export has several mail-ish columns and the first one is often useless:
      * `Email`            → literally "anonymous" for anonymous forms
      * `Your work email`  → the actual address (what we want)
      * `Manager's email`  → someone ELSE's address; using it would mis-assign the country
    So we prefer work/personal addresses, explicitly skip manager columns, and reject a column
    whose values are not addresses at all.
    """
    def usable(col: str) -> bool:
        vals = df[col].dropna().astype(str).head(20)
        return bool(len(vals)) and (vals.str.contains("@").mean() > 0.5)

    cands = [c for c in df.columns if "mail" in c.lower()]
    if not cands:
        # The Teams/Guild exports name the email column `User` (or `UPN`/`Login`) — no "mail" in the
        # header at all. Returning None here is what made every Guild row default to the default country:
        # every member, whatever their domain, collapsed to the HQ country. Fall back to any column whose
        # VALUES are addresses, which is the reliable signal regardless of the header.
        named = [c for c in df.columns
                 if c.lower().strip() in ("user", "upn", "login", "username", "user principal name")]
        cands = [c for c in named if usable(c)] or [c for c in df.columns if usable(c)]
        return cands[0] if cands else None

    preferred = [c for c in cands if "work" in c.lower() or "your" in c.lower()]
    neutral = [c for c in cands if "manager" not in c.lower() and c not in preferred]
    managers = [c for c in cands if "manager" in c.lower()]
    for group in (preferred, neutral, managers):
        for col in group:
            if usable(col):
                return col
    return None


def _fill_country_generic(df: pd.DataFrame, german: bool = False,
                          no_email: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Fill Country (and Company where derivable) per guide rules 6 and 7.

    `no_email` — GUIDE RULE 9: the Atlas/Beacon tables have only Company/Status/Country, no email at
    all. Never attempt an email-based lookup on them; Country comes from the company bracket code
    (rule 7) alone.
    """
    warnings: list[str] = []
    company = find_company_column(df)
    email = None if no_email else find_email_column(df)
    country = next((c for c in df.columns if c.lower() == "country"), None)
    df2 = df.copy()
    if country is None:
        df2["Country"] = ""
        country = "Country"
    if german:
        # GUIDE RULE 8: PlanTrack country names are German (Österreich, Ungarn…) and must be
        # normalised **at the dashboard level, NOT in the bronze table**. Bronze mirrors the source.
        # This used to call normalize_german_country() here, which rewrote the values on the way in —
        # a rule violation, and it made bronze disagree with the archived file. The mapping still
        # exists (normalize_german_country / GERMAN_COUNTRY) for the dashboard + report layer.
        n_german = int(df2[country].astype(str).str.strip().isin(GERMAN_COUNTRY).sum())
        if n_german:
            warnings.append(
                f"{n_german} German country name(s) kept AS-IS in bronze (rule 8 — normalised to "
                "English at the dashboard level, not on ingest)."
            )
    mask = df2[country].isna() | (df2[country].astype(str).str.strip() == "")
    if mask.any():
        df2[country] = _as_object(df2[country])
        df2.loc[mask, country] = df2[mask].apply(
            lambda r: derive_country(r.get(company), r.get(email) if email else None), axis=1)
        # Report WHAT it derived from and the resulting spread, so a silent all-default-country fallback is
        # visible instead of looking like a correct result.
        filled = df2.loc[mask, country]
        spread = ", ".join(
            f"{c} {n}" for c, n in filled.value_counts().head(6).items()
        )
        src = []
        if company:
            src.append(f"company brackets (`{company}`)")
        if email:
            src.append(f"email domain (`{email}`)")
        warnings.append(
            f"Derived Country for {int(mask.sum())} row(s) from "
            + (" then ".join(src) if src else "no usable source — defaulted to the default country")
            + f" → {spread}."
        )
        # Say so when NOTHING was actually derived. "Derived Country … → default country 43" reads like a
        # result, but if every row hit the default then no country was determined at all — the
        # Atlas SAP export has no bracket codes and no email, so all 43 rows fell through.
        n_default = int((filled == DEFAULT_COUNTRY).sum())
        if n_default == int(mask.sum()) and n_default > 1:
            reason = ("this export has no bracket country code in `%s` and no email column"
                      % company) if company else "no company or email column was recognised"
            warnings.append(
                f"⚠ All {n_default} of those are the **{DEFAULT_COUNTRY} default**, not a real "
                f"lookup ({reason}). Fine if the course was single-country — otherwise the country "
                "for this file is unknown, so treat a per-country split of it as unreliable."
            )
        if company is None and email is None:
            warnings.append(
                "⚠ No company or email column recognised, so every row defaulted to the default country — "
                "check the source file's headers."
            )

    # Rule 6's second half: the domain lookup yields Company too. Only fills blanks — an existing
    # company name is never overwritten — and only where the domain maps to one unambiguous entity.
    if company and email:
        cmask = df2[company].isna() | (df2[company].astype(str).str.strip() == "")
        if cmask.any():
            df2[company] = _as_object(df2[company])
            filled_co = df2[cmask].apply(lambda r: derive_company(None, r.get(email)), axis=1)
            n_co = int(filled_co.notna().sum())
            if n_co:
                df2.loc[cmask, company] = filled_co
                warnings.append(f"Derived Company for {n_co} row(s) from the email domain.")
    return df2, warnings


# ── F1/F2/F3: the three policy decisions the user made 2026-07-27 ───────────────────────────────
# F1 — MONTH FORMAT: each table keeps ITS OWN existing format. analytics_teams_listing already holds
# '2026-06' (hyphen); everything else uses '2026.06' (dot). We do NOT rewrite existing rows, so the
# writer must match whatever the target table already uses or filters would split into two buckets.
# `detect_month_format()` confirms this against the live table at write time; this map is the
# fallback when the table is empty/unreadable.
_HYPHEN_MONTH_TABLES = {"analytics_teams_listing"}

# F2 — the completions tables have NO period column. We ADD `reporting_month` and backfill the
# existing rows with the only period loaded so far, so appends become filterable and re-runnable.
ENSURE_PERIOD_COLUMN: dict[str, str] = {
    "atlas_training_completions": "2026.06",
    "beacon_training_completions": "2026.06",
}

# F3 — these tables' sources are CUMULATIVE (each export already contains every row ever), so they
# must never be appended.  The two raw Forms registration tables are safe for the app to FULL REPLACE
# after their deterministic cleanup; doing that directly makes a new export visible immediately.
# Only the curated GOLD participants table remains notebook-owned.
CUMULATIVE_MANUAL: dict[str, str] = {
    # analytics_events_all is deliberately NOT here: the user's rule is "no full replace, just always
    # adding the upcoming event". Its source is not cumulative — each export is ONE event — so the new
    # event's rows are appended and the earlier events are left alone.
    "databricks_trainings_participants":
        "Gold table is rebuilt from the latest registration source by its notebook.",
}


def month_format_for(table: str, sample: str | None = None) -> str:
    """Return the separator this table uses for reporting months: '-' or '.' (F1).

    Tables in `_HYPHEN_MONTH_TABLES` always use hyphens — the static map wins unconditionally so
    a table that was accidentally written with dots once does not lock in the wrong format for
    every subsequent upload. The `sample` check is kept only for tables NOT in the map.
    """
    if table in _HYPHEN_MONTH_TABLES:
        return "-"
    if sample:
        return "-" if "-" in sample else "."
    return "."


def format_month(month: str | None, table: str, sample: str | None = None) -> str | None:
    """Rewrite 'YYYY.MM' into whatever separator `table` actually uses (F1)."""
    if not month:
        return month
    sep = month_format_for(table, sample)
    return month.replace(".", sep) if sep != "." else month.replace("-", ".")


def detect_month_format(cur, table: str, month_col: str) -> str | None:
    """Read one existing month value from the table so we can match its format exactly."""
    try:
        cur.execute(f"SELECT max(`{month_col}`) FROM {table}")
        row = cur.fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001 — table may be new/empty; caller falls back to the static map
        return None


# Which spelling of the reporting-month column each APPEND table actually uses in Databricks.
# Verified against the live schemas 2026-07-27 — these are NOT guesses:
#   lakehouse_guild                       -> "Reporting_Month"  (capitalised)
#   fake_company_data_with_reporting_month  -> "Reporting_Month"  (capitalised)
#   analytics_teams_listing                   -> "reporting_month"  (lowercase)
# Used by BOTH build_plan (preview) and load_full_frame (the frame actually written) so the two can
# never drift apart — they previously disagreed for the revenue table and only worked by accident of
# Databricks' case-insensitive column matching.
_CAPITALISED_MONTH_TABLES = {"lakehouse_guild", "fake_company_data_with_reporting_month"}

# Stamped onto rows in the per-period tables (beacon_2026_07, atlas_2026_07, plantrack_…). Those
# tables have no reporting_month column — the period is in the table NAME — so there is nothing to
# de-duplicate on when the same course export is uploaded twice. Recording the source filename gives
# a re-run something to replace.
_SOURCE_FILE_COL = "source_file"

# APPEND tables whose rows belong to an EVENT rather than a reporting month. Two events can happen in
# the same month, so a month-keyed dedup DELETE could wipe event 4's rows while re-uploading event 5.
# These key on the source filename instead.
_EVENT_KEYED_APPEND = {"analytics_events_all"}


def month_tag_column(table: str) -> str:
    return "Reporting_Month" if table in _CAPITALISED_MONTH_TABLES else "reporting_month"


def _tag_reporting_month(df: pd.DataFrame, col: str, month: str | None) -> tuple[pd.DataFrame, list[str]]:
    if not month:
        return df, [f"APPEND mode but no reporting month detected — {col} left unset. "
                    "Rename file with YYYY_MM or set the month manually."]
    df2 = df.copy()
    df2[col] = month
    return df2, [f"Tagged all rows with {col} = {month}."]


_READ_CACHE: dict[tuple[str, float, int], pd.DataFrame] = {}


def read_excel_cached(path: str | Path) -> pd.DataFrame:
    """`pd.read_excel`, memoised on (path, mtime, size).

    Measured: parsing a ~1000-row community workbook takes hundreds of ms — most of the whole per-file
    cost, against a few ms for all the cleaning. A single upload previously parsed it three times (the
    preview, the write, and load_full_frame's own inner build_plan call), so the parse dominated
    everything. Keyed on mtime+size so re-uploading a changed file with the same name still re-reads.

    A copy is returned: callers mutate their frame, and handing out the cached object would corrupt it
    for the next caller.
    """
    p = Path(path)
    try:
        st = p.stat()
        key = (str(p.resolve()), st.st_mtime, st.st_size)
    except OSError:
        return pd.read_excel(p)
    hit = _READ_CACHE.get(key)
    if hit is None:
        hit = pd.read_excel(p)
        # Keep the cache small: these frames are held in RAM and a session can touch many files.
        if len(_READ_CACHE) > 12:
            _READ_CACHE.clear()
        _READ_CACHE[key] = hit
    return hit.copy()


def apply_cleaning(df: pd.DataFrame, route: TableRoute,
                   period: str | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Run the cleaning routine for a route. ONE definition, used by both build_plan (preview) and
    load_full_frame (what is actually written).

    They each had their own copy of this dispatch and had already drifted once (the month-tag column),
    which only failed to corrupt data by accident. A preview that doesn't match the write is worse
    than no preview.
    """
    if route.cleaning == "event":
        return _clean_event(df)
    if route.cleaning == "community":
        return _clean_community(df)
    if route.cleaning == "plantrack":
        # PlanTrack: Company / Learning_Name_Module / Country / Status — no email column (rule 9),
        # and its German country names stay German in bronze (rule 8).
        return _fill_country_generic(df, german=True, no_email=True)
    if route.cleaning == "learning":
        # RULE 9: Atlas/Beacon carry only Company, Status, Country — never an email lookup.
        out, w = _fill_country_generic(df, no_email=True)
        return out, w + _status_breakdown(out)
    if route.cleaning == "registration":
        out, w = _fill_country_generic(df)
        if route.table == "registration_for_databricks_trainings":
            # This table represents submitted registration forms, not unique people.  Repeated email
            # addresses, repeated levels and blank emails are still distinct responses and must all
            # be counted.  The filename's `(1-89)` total must therefore remain 89 after cleaning.
            return out, w + [
                f"Training registrations: preserved all {len(out)} form response(s), including "
                "repeat or blank emails."
            ]
        out, w2 = _dedup_registrations(out)
        return out, w + w2
    if route.cleaning == "guild":
        out, warnings = _clean_listing(df, period, "Guild")
        # Microsoft now exports the Guild with Teams/Graph headers (DisplayName, Email, Roles),
        # while the established bronze table and dashboard use Email, First_name, Last_name, Role.
        # Shape the frame to that contract instead of adding parallel columns that the dashboard
        # never reads.  The calculated columns are deliberately omitted; enrich_guild() fills them
        # after the snapshot is written.
        from derivation import split_name

        email = find_email_column(out)
        display = next((c for c in out.columns
                        if c.lower() in {"displayname", "display_name", "name"}), None)
        first = next((c for c in out.columns if c.lower() in {"first_name", "firstname"}), None)
        last = next((c for c in out.columns if c.lower() in {"last_name", "lastname"}), None)
        role = next((c for c in out.columns if c.lower() in {"roles", "role"}), None)
        month = next((c for c in out.columns if c.lower() == "reporting_month"), None)

        shaped = pd.DataFrame(index=out.index)
        shaped["Email"] = out[email] if email else None
        if first or last:
            shaped["First_name"] = out[first] if first else None
            shaped["Last_name"] = out[last] if last else None
        elif display:
            parts = out[display].map(split_name)
            shaped["First_name"] = parts.map(lambda pair: pair[0])
            shaped["Last_name"] = parts.map(lambda pair: pair[1])
        else:
            shaped["First_name"] = None
            shaped["Last_name"] = None
        shaped["Role"] = out[role] if role else None
        shaped["Company"] = out["Company"] if "Company" in out.columns else None
        shaped["Country"] = out["Country"] if "Country" in out.columns else None
        shaped["Reporting_Month"] = out[month] if month else period
        warnings.append(
            "Guild: mapped the Teams export to the established Guild schema "
            "(DisplayName→First_name/Last_name, Roles→Role)."
        )
        return shaped.reset_index(drop=True), warnings
    if route.cleaning == "teams_listing":
        out, warnings = _clean_listing(df, period, "Teams listing")
        # Preserve the established bronze/dashboard contract. New Microsoft exports renamed these
        # headers, and appending them verbatim created parallel Email/DisplayName/Roles columns while
        # every existing dashboard still queried User/Name/Role — making a successful month invisible.
        mapping = {"Email": "User", "DisplayName": "Name", "Roles": "Role"}
        renamed = {old: new for old, new in mapping.items()
                   if old in out.columns and new not in out.columns}
        if renamed:
            out = out.rename(columns=renamed)
            warnings.append(
                "Teams listing: mapped current export headers to the established table schema "
                "(Email→User, DisplayName→Name, Roles→Role)."
            )
        if "Role" in out.columns:
            # Microsoft's Teams/Graph export leaves Roles BLANK for regular members — only
            # Owners get an explicit value. Copying that verbatim makes every member's Role
            # NULL, and the dashboard's `role IN ('member')` filter then excludes them all
            # (the live symptom: 2026-08 dropped to 0 while 2026-06/07, from the old export
            # format that always wrote "Member" explicitly, kept working).
            blank = out["Role"].isna() | (out["Role"].astype(str).str.strip() == "")
            if blank.any():
                out.loc[blank, "Role"] = "Member"
                warnings.append(
                    f"Teams listing: {int(blank.sum())} row(s) had a blank Role (Microsoft leaves "
                    "this blank for regular members) — filled as 'Member'."
                )
        return out, warnings
    return df, []


def this_month() -> str:
    """Current month as YYYY.MM — the sensible default for a fresh monthly export."""
    from datetime import date

    t = date.today()
    return f"{t.year}.{t.month:02d}"


def previous_month() -> str:
    """LAST month as YYYY.MM — the safe default for a snapshot export.

    GUIDE (⚠️ CRITICAL: Guild Reporting_Month Rules): the month must be the DATA PERIOD, not today's
    calendar month. The guide exists because someone tagged a June snapshot as `2026.08` and destroyed
    the history. A snapshot you upload is nearly always for a period that has CLOSED, so defaulting to
    the current month is the one default guaranteed to reproduce that mistake.
    """
    from datetime import date

    t = date.today()
    return f"{t.year - 1}.12" if t.month == 1 else f"{t.year}.{t.month - 1:02d}"


def month_options(default: str | None = None, back: int = 18) -> list[str]:
    """`default` (or LAST month) first, then earlier months descending.

    Lives here rather than in the pages so Service 1 and Full Run offer the identical list — they
    write to the same tables, and two different month pickers would be a way to split a period.
    """
    y, m = (int(x) for x in (default or previous_month()).split("."))
    out: list[str] = []
    for _ in range(back):
        out.append(f"{y}.{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def needs_manual_month(filenames: list[str]) -> list[str]:
    """Which of these files APPEND (or make a period table) but have no month in their name.

    Those are the unsafe ones: an untagged append cannot be filtered by a dashboard and the dedup
    guard has nothing to match, so a re-upload duplicates the rows instead of replacing them.
    """
    out = []
    for name in filenames:
        route = route_for(name)
        if (route and route.mode in (WriteMode.APPEND, WriteMode.NEW_TABLE_PERIOD)
                and not infer_reporting_month(name)):
            out.append(name)
    return out


def build_plan(file_path: str | Path, reporting_month: str | None = None,
               month_fallback: str | None = None) -> IngestionPlan:
    """Read the Excel, route it, apply cleaning, and return a plan (NO Databricks write).

    `reporting_month` FORCES the period (it wins over the filename). `month_fallback` is used only
    when the filename carries no month — that is what the UI's month picker passes, so a file named
    `..._2026_06.xlsx` keeps June even if the picker shows July.
    """
    path = Path(file_path)
    df = read_excel_cached(path)
    route = route_for(path.name)
    month = reporting_month or infer_reporting_month(path.name) or month_fallback
    rows_in = len(df)
    warnings: list[str] = []

    if route is None:
        return IngestionPlan(path.name, None, month, rows_in, rows_in, list(df.columns),
                             ["No routing rule matched this filename — cannot map to a table. "
                              "Rename to include the program keyword (community/event/guild/"
                              "atlas/beacon/plantrack/registration/revenue)."], df.head(20))

    cleaned, w = apply_cleaning(df, route, month)
    warnings += w

    if route.mode == WriteMode.APPEND:
        cleaned, w = _tag_reporting_month(cleaned, month_tag_column(route.table), month); warnings += w

    return IngestionPlan(path.name, route, month, rows_in, len(cleaned),
                         list(cleaned.columns), warnings, cleaned.head(20))


def load_full_frame(file_path: str | Path, reporting_month: str | None = None,
                    month_fallback: str | None = None) -> pd.DataFrame:
    """Read + clean the FULL frame (build_plan only keeps a 20-row preview). Used by writes.

    It used to call build_plan() first, purely to reuse its route and month — but build_plan reads AND
    cleans the whole workbook to produce its preview, so every file was parsed and cleaned twice here
    and once more by the caller: three full passes per upload. The route and month are cheap to compute
    directly (filename only), so they are derived here instead.
    """
    path = Path(file_path)
    route = route_for(path.name)
    month = reporting_month or infer_reporting_month(path.name) or month_fallback
    df = read_excel_cached(path)
    if route is None:
        return df
    df, _ = apply_cleaning(df, route, month)
    if route.mode == WriteMode.APPEND:
        df, _ = _tag_reporting_month(df, month_tag_column(route.table), month)
    return df


def quote_ident(name) -> str:
    """Backtick-quote a Spark SQL identifier, escaping embedded backticks."""
    return "`" + str(name).replace("`", "``") + "`"


def sql_literal(v) -> str:
    """Render a Python value as a Spark SQL literal (backslashes and quotes escaped)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def safe_stem(filename: str) -> str:
    """Filename stem reduced to characters that are safe in a volume path and a SQL string."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).strip("._") or "upload"


def _spark_type(series) -> str:
    """Map a pandas column dtype to a Spark SQL type for the CREATE TABLE, so numeric source
    columns stay numeric (bigint/double) instead of everything landing as STRING."""
    import pandas.api.types as pdt

    if pdt.is_integer_dtype(series):
        return "BIGINT"
    if pdt.is_float_dtype(series):
        return "DOUBLE"
    if pdt.is_bool_dtype(series):
        return "BOOLEAN"
    if pdt.is_datetime64_any_dtype(series):
        return "TIMESTAMP"
    return "STRING"


def archive_to_volume(plan: IngestionPlan, file_path: str | Path, profile: str = DATABRICKS_PROFILE) -> str:
    """Guide step 1: upload the raw Excel to its UC Volume archive (this app never deletes it). Uses the SDK
    Files API over the same CLI OAuth login. Returns the volume path written, or raises."""
    from databricks.sdk import WorkspaceClient

    if not plan.volume_target:
        raise ValueError("No volume target for this plan.")
    w = WorkspaceClient(profile=profile)
    with open(file_path, "rb") as fh:
        w.files.upload(plan.volume_target, fh, overwrite=True)
    return plan.volume_target


def prepare_run_frames(
    items: list[tuple[str | Path, IngestionPlan]],
) -> tuple[list[pd.DataFrame], dict[int, int]]:
    """Load all files for the run. Each file's rows are already deduped within that file by
    `_clean_listing`; no cross-file deduplication is done here.

    Cross-file dedup was removed because each uploaded file is a SEPARATE monthly snapshot — they
    belong to different periods and the same person SHOULD appear in both June and July exports.
    Deduplicating across files for the same month also fired when users uploaded one Teams listing
    alongside other file types in the same batch, silently dropping real members.
    """
    frames = [
        load_full_frame(path, plan.reporting_month)
        if plan.route and plan.route.mode != WriteMode.ARCHIVE_ONLY else pd.DataFrame()
        for path, plan in items
    ]
    return frames, {}


_LISTING_EMAIL_SOURCES: tuple[tuple[str, str, str | None, str], ...] = (
    ("analytics_community", "First_name", "Last_name", "Email"),
    ("registration_for_databricks_trainings", "First name", "Last name",
     "Your work email"),
    ("registrations_for_teams_lakehouse_guild", "First_Name", "Last_Name", "Work_Email"),
    ("analytics_teams_listing", "Name", None, "User"),
    ("lakehouse_guild", "First_name", "Last_name", "Email"),
)


def enrich_run_listing_emails(
    frames: list[pd.DataFrame], items: list[tuple[str | Path, IngestionPlan]], connection,
) -> tuple[list[pd.DataFrame], dict[int, int], dict[int, int]]:
    """Fill missing Guild emails from trusted Databricks tables by unambiguous full name.

    Analytics Community Teams-listing emails are deliberately left exactly as supplied: every raw
    row is a member, including blank-email rows. Guild enrichment remains available because its
    calculated person-level fields require an identity.
    """
    from derivation import alias_key, alias_priority

    out = [f.copy() for f in frames]
    if not any(plan.route and plan.route.cleaning == "guild" for _, plan in items):
        return out, {}, {}

    def name_key(value) -> str:
        return re.sub(r"[^\w]", "", str(value or "").strip().lower())

    candidates: dict[str, list[str]] = {}
    # Use every trusted people table available in this workspace, including prior monthly snapshots.
    # A new Teams export can omit an address even when the same name had one in June.  The lookup is
    # still conservative: a name is filled only when all sources resolve to one alias-aware person.
    cur = connection.cursor()
    try:
        for table, first, last, email in _LISTING_EMAIL_SOURCES:
            try:
                last_expr = f"`{last}`" if last else "NULL"
                cur.execute(
                    f"SELECT `{first}`, {last_expr}, `{email}` "
                    f"FROM {CATALOG}.{BRONZE}.`{table}` "
                    f"WHERE `{email}` IS NOT NULL AND trim(cast(`{email}` AS STRING)) <> ''"
                )
                for f, l, address in cur.fetchall():
                    key = name_key(f"{f or ''} {l or ''}")
                    if key and alias_key(address):
                        candidates.setdefault(key, []).append(str(address).strip())
            except Exception:  # noqa: BLE001 — one unavailable source must not block the upload
                continue
    finally:
        cur.close()

    resolved: dict[str, str] = {}
    for key, addresses in candidates.items():
        identities = {alias_key(a) for a in addresses if alias_key(a)}
        if len(identities) == 1:
            resolved[key] = min(addresses, key=alias_priority)

    filled_by_file: dict[int, int] = {}
    for pos, (_, plan) in enumerate(items):
        if not plan.route or plan.route.cleaning != "guild":
            continue
        email = find_email_column(out[pos])
        name = next((c for c in out[pos].columns
                     if c.lower() in {"displayname", "display_name", "name"}), None)
        if name:
            names = out[pos][name]
        else:
            first = next((c for c in out[pos].columns
                          if c.lower() in {"first_name", "firstname"}), None)
            last = next((c for c in out[pos].columns
                         if c.lower() in {"last_name", "lastname"}), None)
            names = ((out[pos][first].fillna("").astype(str).str.strip() if first else "")
                     + " "
                     + (out[pos][last].fillna("").astype(str).str.strip() if last else ""))
        if not email or (not name and not first and not last):
            continue
        blank = out[pos][email].isna() | (out[pos][email].astype(str).str.strip() == "")
        mapped = names.loc[blank].map(lambda v: resolved.get(name_key(v)))
        good = mapped.notna()
        if good.any():
            out[pos].loc[mapped.index[good], email] = mapped[good]
            filled_by_file[pos] = int(good.sum())

    # Cross-file dedup deliberately removed: each file is an independent monthly snapshot and the
    # same person appearing in June AND July exports is correct, not a duplicate.
    return out, filled_by_file, {}


def execute_write(plan: IngestionPlan, file_path: str | Path, connection,
                  archive: bool = True, replace_month: bool = True,
                  result: dict | None = None,
                  written: set[tuple[str, str]] | None = None,
                  frame_override: pd.DataFrame | None = None) -> list[str]:
    """ACTUALLY write to Databricks per the plan's mode. Caller passes a live SQL connection
    (databricks-sql-connector). Returns log lines. Only call when the user opted into writing
    AND write grants + a running warehouse are confirmed.

    Guide 2-step process: (1) archive the raw file to the UC Volume, (2) write the table.
    For APPEND tables, `replace_month` first DELETEs any rows already tagged with this
    reporting month, so re-running the same file replaces that month instead of duplicating it.

    `written` — pass ONE set shared across every file in a run. It records the (table, month) pairs
    already cleared, so the month-replace DELETE fires only for the FIRST file targeting that table
    and month. Without it, uploading several files into the same table (e.g. two Beacon course exports
    for July) makes each file's DELETE wipe the rows the previous file just inserted: the table ends
    up holding only the last file, silently. Callers that pass nothing keep the old
    one-file-per-call behaviour.

    Pass a dict as `result` to receive STRUCTURED facts about the write (filename, table, mode,
    reporting_month, rows, volume_path, bulk) alongside the human log lines. run_manifest.py turns
    these into the record that lets the presentation half detect that new data landed.
    """
    if not plan.route:
        raise ValueError("No route — cannot write.")

    # ── ARCHIVE_ONLY: a derived report (e.g. Unregistered_Members) — keep the file, write no table.
    # Handled before anything else so we never build a frame or a table name for it.
    if plan.route.mode == WriteMode.ARCHIVE_ONLY:
        log = []
        if archive:
            try:
                path = archive_to_volume(plan, file_path)
                if result is not None:
                    result["volume_path"] = path
                    result["archive_ok"] = True
                log.append(f"Archived to Volume: {path}")
            except Exception as exc:  # noqa: BLE001
                if result is not None:
                    result["archive_ok"] = False
                logger.exception("Volume archive failed for %s", plan.filename)
                log.append(f"⚠ Volume archive failed ({str(exc)[:120]}).")
        log.append(f"⛔ No table write for {plan.filename} — {plan.route.note}")
        if result is not None:
            result.update(filename=plan.filename, table="", mode=plan.route.mode.value,
                          reporting_month=plan.reporting_month, rows=0, bulk=False,
                          skipped=True)
        return log

    if not plan.full_table:
        raise ValueError("No target table — cannot write.")
    # Multi-file monthly listings are cleaned as one snapshot by prepare_run_frames().  Accepting
    # that prepared slice here prevents the files being independently deduplicated a second time.
    df = (frame_override.copy() if frame_override is not None
          else load_full_frame(file_path, plan.reporting_month))
    table = plan.full_table
    log: list[str] = []
    if result is not None:
        result.update(
            filename=plan.filename, table=table, mode=plan.route.mode.value,
            reporting_month=plan.reporting_month, rows=len(df),
            volume_path=None, bulk=False, archive_ok=None,
        )

    # ── Step 1: archive the raw file to the Volume (best-effort — don't block the table write) ─
    # Runs even for the F3 cumulative tables: archiving is never destructive, so the file is always
    # preserved even when we then refuse the table write.
    if archive:
        try:
            path = archive_to_volume(plan, file_path)
            if result is not None:
                result["volume_path"] = path
                result["archive_ok"] = True
            log.append(f"Archived source file to Volume: {path}")
        except Exception as exc:  # noqa: BLE001
            if result is not None:
                result["archive_ok"] = False
            logger.exception("Volume archive failed for %s", plan.filename)
            log.append(f"⚠ Volume archive failed ({str(exc)[:120]}) — continuing with table write.")

    # ── F3: cumulative sources are NOT written here (user decision: append only the snapshot
    # tables). The file is archived above; the table stays the notebooks' job so we can never
    # duplicate a cumulative export or overwrite history.
    if plan.route.table in CUMULATIVE_MANUAL:
        log.append(
            f"⛔ Skipped the table write for {table} — {CUMULATIVE_MANUAL[plan.route.table]}"
        )
        if result is not None:
            result["rows"] = 0
            result["skipped"] = True
        return log

    cur = connection.cursor()
    try:
        if plan.route.mode in (WriteMode.FULL_REPLACE, WriteMode.NEW_TABLE_PERIOD,
                               WriteMode.NEW_TABLE_EVENT):
            # A Forms export names its columns after the QUESTION, but the existing table uses tidy
            # names (`Work_Email`, not `Your work email`). CREATE OR REPLACE would silently redefine
            # the table with the question text as headers, breaking every dashboard query against it.
            # So for FULL_REPLACE, match the schema that is already there.
            if plan.route.mode == WriteMode.FULL_REPLACE:
                try:
                    cur.execute(f"DESCRIBE {table}")
                    live_cols = [r[0] for r in cur.fetchall()
                                 if r[0] and not r[0].startswith("#")]
                except Exception:  # noqa: BLE001 — table may not exist yet; then the file defines it
                    live_cols = []
                if live_cols:
                    df, notes = align_to_table_schema(df, plan.route.table, live_cols)
                    log.extend(notes)
            # Recreate the table from the frame's schema, mapping pandas dtypes to Spark types so
            # numeric columns (e.g. "Annual Revenue (EUR)") stay numeric and dashboard SUM()s work.
            # REPLACE for cumulative/one-time; CREATE for new period/event tables.
            verb = "CREATE OR REPLACE TABLE" if plan.route.mode == WriteMode.FULL_REPLACE else "CREATE TABLE IF NOT EXISTS"
            coldefs = ", ".join(f"{quote_ident(c)} {_spark_type(df[c])}" for c in df.columns)
            # Delta column mapping so headers with spaces/parens/German chars are allowed as-is.
            cur.execute(
                f"{verb} {table} ({coldefs}) "
                "TBLPROPERTIES ('delta.columnMapping.mode'='name', "
                "'delta.minReaderVersion'='2', 'delta.minWriterVersion'='5')"
            )
            log.append(f"{verb} {table} ({len(df.columns)} cols)")

            # A period table is shared by every file for that period: several Beacon course exports all
            # land in `beacon_2026_07`. IF NOT EXISTS means the second file INSERTs into the first
            # file's table, which is what we want — but its columns may differ (a different course
            # export carries different headers), so add whatever is missing rather than letting the
            # INSERT fail or silently drop data.
            if plan.route.mode in (WriteMode.NEW_TABLE_PERIOD, WriteMode.NEW_TABLE_EVENT):
                cur.execute(f"DESCRIBE {table}")
                have = {r[0].lower() for r in cur.fetchall() if r[0] and not r[0].startswith("#")}
                for c in df.columns:
                    if c.lower() not in have:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMNS ({quote_ident(c)} {_spark_type(df[c])})")
                        log.append(f"Added missing column `{c}` to {table}.")
                # Re-running the SAME file must not double its rows. The period tables have no
                # month column to delete on (the period IS the table name), so key on the source
                # filename: stamp it and clear this file's previous rows first.
                if _SOURCE_FILE_COL.lower() not in have:
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMNS (`{_SOURCE_FILE_COL}` STRING)"
                    )
                    log.append(f"Added `{_SOURCE_FILE_COL}` to {table} (tracks which file each row "
                               "came from, so a re-run replaces instead of duplicating).")
                df[_SOURCE_FILE_COL] = plan.filename
                if replace_month:
                    cur.execute(
                        f"DELETE FROM {table} WHERE `{_SOURCE_FILE_COL}` = ?", [plan.filename]
                    )
                    log.append(f"Cleared any previous rows from {plan.filename} "
                               "(re-run replaces this file's rows).")
        else:  # APPEND — new rows added below existing data (July+Aug+Sep coexist)
            # ── F2: the completions tables have no period column at all. Add it and backfill the
            # rows already there with the period they came from, so existing data stays
            # identifiable and this month's append can be filtered / re-run safely.
            backfill = ENSURE_PERIOD_COLUMN.get(plan.route.table)
            if backfill:
                cur.execute(f"DESCRIBE {table}")
                cols_now = {r[0].lower() for r in cur.fetchall()
                            if r[0] and not r[0].startswith("#")}
                if "reporting_month" not in cols_now:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMNS (`reporting_month` STRING)")
                    cur.execute(
                        f"UPDATE {table} SET `reporting_month` = ? "
                        "WHERE `reporting_month` IS NULL",
                        [backfill],
                    )
                    log.append(
                        f"F2: added `reporting_month` to {table} and backfilled existing rows "
                        f"as {backfill} (they were untagged)."
                    )

            # Add any columns the incoming frame has but the table lacks (e.g. reporting_month
            # on a table that didn't track it before) so nothing is silently dropped.
            cur.execute(f"DESCRIBE {table}")
            # Databricks column names are case-INSENSITIVE — compare lowercased so we don't try
            # to ADD a column that already exists under different casing (e.g. Reporting_Month).
            existing = {r[0].lower() for r in cur.fetchall() if r[0] and not r[0].startswith("#")}
            for c in df.columns:
                if c.lower() not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMNS ({quote_ident(c)} {_spark_type(df[c])})")
                    log.append(f"Added missing column `{c}` to {table}.")

            # ── F1: match the month FORMAT this table already uses. teams_listing holds '2026-06'
            # (hyphen) while everything else uses dots; writing the wrong separator would split
            # every dashboard filter into two buckets. We read a real value and follow it.
            month_col_now = next((c for c in df.columns if c.lower() == "reporting_month"), None)
            if month_col_now and plan.reporting_month:
                sample = detect_month_format(cur, table, month_col_now)
                wanted = format_month(plan.reporting_month, plan.route.table, sample)
                if wanted and wanted != plan.reporting_month:
                    df[month_col_now] = wanted
                    log.append(
                        f"F1: wrote month as {wanted} to match this table's existing format "
                        f"(sample {sample})."
                    )
                    if result is not None:
                        result["reporting_month"] = wanted
            # Dedup guard: if this reporting month is already in the table, remove it first so a
            # re-run REPLACES that month's rows instead of duplicating them.
            # NOTE it must delete the month in the SAME format we are about to write (F1) —
            # deleting '2026.07' from a table that stores '2026-07' would match nothing and the
            # re-run would silently duplicate the month.
            written_month = (result or {}).get("reporting_month") or plan.reporting_month

            # ── analytics_events_all: keyed by EVENT, not by month ────────────────────────────────
            # The user's rule is "no full replace, just always adding the upcoming event". Its rows
            # belong to one event, and two events can fall in the same month — so a month-keyed
            # DELETE could wipe event 4 while re-uploading event 5. Key on the source file instead.
            event_keyed = plan.route.table in _EVENT_KEYED_APPEND
            if event_keyed:
                if _SOURCE_FILE_COL.lower() not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMNS (`{_SOURCE_FILE_COL}` STRING)")
                    log.append(f"Added `{_SOURCE_FILE_COL}` to {table} so each event's rows stay "
                               "identifiable and a re-upload replaces only that event.")
                df[_SOURCE_FILE_COL] = plan.filename
                if replace_month:
                    cur.execute(
                        f"DELETE FROM {table} WHERE `{_SOURCE_FILE_COL}` = ?", [plan.filename]
                    )
                    log.append(f"Cleared any previous rows from {plan.filename} — earlier events "
                               "untouched (append-only, never full replace).")

            # Only the FIRST file for this (table, month) clears the month — see `written` in the
            # docstring. Later files in the same run append alongside it, so several course exports
            # can land in one completions table without deleting each other.
            already_cleared = written is not None and (table, written_month) in written
            if not event_keyed and replace_month and written_month and not already_cleared:
                cur.execute(f"DESCRIBE {table}")
                real_cols = [r[0] for r in cur.fetchall() if r[0] and not r[0].startswith("#")]
                month_col = next((c for c in real_cols if c.lower() == "reporting_month"), None)
                if month_col:
                    cur.execute(f"DELETE FROM {table} WHERE `{month_col}` = ?", [written_month])
                    log.append(f"Removed existing rows for {written_month} (re-run replaces the month).")
                    # Also clean up the alternate separator variant so a previously mis-formatted
                    # upload (e.g. '2026.07' stored in a hyphen table) doesn't linger alongside
                    # the correctly-formatted new rows.
                    alt_month = (written_month.replace("-", ".") if "-" in written_month
                                 else written_month.replace(".", "-"))
                    if month_col and alt_month != written_month:
                        cur.execute(f"DELETE FROM {table} WHERE `{month_col}` = ?", [alt_month])
                        log.append(f"Also removed any rows stored as {alt_month} (format normalisation).")
            elif already_cleared and not event_keyed:
                log.append(
                    f"Appending alongside an earlier file in this run "
                    f"({written_month} already cleared once) — not deleting it again."
                )
            if written is not None and written_month and not event_keyed:
                written.add((table, written_month))
            if event_keyed:
                log.append(f"APPEND into existing {table} (keyed by event, not month).")
            else:
                log.append(f"APPEND into existing {table}"
                           + (f" (reporting_month={written_month})" if written_month else ""))

        cols = ", ".join(quote_ident(c) for c in df.columns)
        # FAST PATH for large frames: stage the data as a Parquet file in the volume and have
        # Databricks bulk-load it server-side with one INSERT ... SELECT read_files(). This sends
        # ONE file + ONE statement instead of thousands of VALUES over the wire, and preserves
        # types exactly. Falls back to a batched VALUES insert for small frames / if staging fails.
        used_bulk = False
        if len(df) >= BULK_THRESHOLD:
            try:
                n = _bulk_load_parquet(plan, df, cur, table, cols, log)
                log.append(f"Bulk-loaded {n} row(s) into {table} via Parquet read_files().")
                used_bulk = True
                if result is not None:
                    result["bulk"] = True
            except Exception as exc:  # noqa: BLE001
                log.append(f"⚠ Bulk load failed ({str(exc)[:100]}) — falling back to batched INSERT.")

        if not used_bulk:
            rows = [tuple(None if pd.isna(v) else v for v in r) for r in df.itertuples(index=False)]
            inserted = 0
            for i in range(0, len(rows), INSERT_BATCH):
                chunk = rows[i:i + INSERT_BATCH]
                values = ", ".join("(" + ", ".join(sql_literal(v) for v in r) + ")" for r in chunk)
                cur.execute(f"INSERT INTO {table} ({cols}) VALUES {values}")
                inserted += len(chunk)
            log.append(f"Inserted {inserted} row(s) into {table} (batched VALUES).")
    finally:
        cur.close()
    return log


BULK_THRESHOLD = 300   # rows: above this, use the Parquet bulk-load fast path
INSERT_BATCH = 500     # rows per batched VALUES statement (small-frame fallback)


def _bulk_load_parquet(plan: IngestionPlan, df, cur, table: str, cols: str, log: list[str]) -> int:
    """Stage `df` as a Parquet file in the plan's volume folder and INSERT ... SELECT it in one
    server-side operation. Returns the row count. Scales to thousands of rows across many sheets."""
    import tempfile

    from databricks.sdk import WorkspaceClient

    # A staging area under the same volume (kept separate from the archived source file).
    base = plan.volume_target.rsplit("/", 1)[0] if plan.volume_target else \
        f"{VOLUME_ROOT}/{plan.route.volume_subpath}"
    stem = safe_stem(plan.filename)
    remote = f"{base}/_staging/{stem}.parquet"

    w = WorkspaceClient(profile=DATABRICKS_PROFILE)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            tmp_path = tmp.name
        df.to_parquet(tmp_path, index=False)
        with open(tmp_path, "rb") as fh:
            w.files.upload(remote, fh, overwrite=True)

        # read_files infers the parquet schema; select our exact columns into the table.
        select_cols = ", ".join(quote_ident(c) for c in df.columns)
        cur.execute(
            f"INSERT INTO {table} ({cols}) "
            f"SELECT {select_cols} FROM read_files('{remote}', format => 'parquet')"
        )
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        try:  # staging copy is only needed for the load; remove it best-effort
            w.files.delete(remote)
        except Exception as exc:  # noqa: BLE001
            log.append(f"Note: could not remove staging file {remote} ({str(exc)[:80]}).")
    return len(df)


def explain_plan(plan: IngestionPlan) -> str:
    """Human-readable summary of what WOULD happen (dry run)."""
    if not plan.route:
        return f"❌ {plan.filename}: no matching table rule."
    lines = [
        f"📄 File: {plan.filename}",
        f"🎯 Target table: {plan.full_table}",
        f"✍️  Write mode: {plan.route.mode.value.upper()}",
        f"🗄️  Archive to Volume: {plan.volume_target}",
        f"🔢 Rows: {plan.rows_in} in → {plan.rows_out} after cleaning",
    ]
    if plan.reporting_month:
        lines.append(f"🗓️  Reporting month: {plan.reporting_month}")
    if plan.route.note:
        lines.append(f"ℹ️  {plan.route.note}")
    for w in plan.warnings:
        lines.append(f"   • {w}")
    return "\n".join(lines)
