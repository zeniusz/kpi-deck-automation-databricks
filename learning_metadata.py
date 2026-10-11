"""The table-name → (platform, course_name, language) map for the learning sources.

THE CORE PROBLEM this solves (user-provided spec, verified against the live schemas 2026-07-28):

Each per-course source table holds only **3 columns** — `Company`, `Status`, `Country`. There is NO
column saying which platform, course or language the rows belong to. That information exists *only in
the table name*:

    atlas_german_data_basics_and_awareness_part_1_2026_06
    ^^^^^^^^ ^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^
    platform language          course                 period

So when the unified completions tables (6 columns: platform, course_name, language, Company, Status,
Country) are built by UNION ALL over those per-course tables, the three metadata columns must be
**hardcoded per source table**. That mapping is what this module owns.

Rules, in the user's words:
  * `german_` anywhere in the table name → language = 'German', otherwise 'English'
  * the prefix decides the platform: `atlas_` / `beacon_elearning_` / `plantrack_`
  * course_name is the human-readable form of the remaining name
  * PlanTrack is special: it carries its OWN `Learning_Name_Module` column for the course name, and
    its Country values are German (normalised to English downstream)

Why a table of literals rather than a clever parser: the course names are NOT mechanical
transformations of the table names. Atlas writes "Data Basics & Awareness Pt. 1" while Beacon writes
"Data Basic Awareness Pt. 1" (no 's', no '&'), and `datagovernance` is one word in the Atlas
tables but two in the Beacon ones. A parser that guessed these would produce course names that don't
match the existing rows, splitting every dashboard grouping. `derive_metadata()` still falls back to
parsing for a table nobody has registered yet, and flags it as a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── Platforms ───────────────────────────────────────────────────────────────────────────────────
ATLAS = "Atlas"
BEACON = "Beacon"
PLANTRACK = "PlanTrack"

# The unified table each platform's per-course rows are UNIONed into.
UNIFIED_TABLE = {
    ATLAS: "atlas_training_completions",
    BEACON: "beacon_training_completions",
}

# The three metadata columns, in the order the live unified tables declare them.
METADATA_COLUMNS = ("platform", "course_name", "language")


@dataclass(frozen=True)
class LearningMetadata:
    platform: str
    course_name: str | None      # None for PlanTrack: read per row from Learning_Name_Module
    language: str
    course_from_column: str | None = None   # the column to read course_name from, if any
    guessed: bool = False        # True when derived by parsing, not from the registry

    def as_dict(self) -> dict[str, str | None]:
        return {"platform": self.platform, "course_name": self.course_name,
                "language": self.language}


# ── The registry: table-name stem → metadata ────────────────────────────────────────────────────
# Keys are the table name WITHOUT the trailing period suffix (`_2026_06`), so one entry serves every
# month. Values are exactly the strings the existing unified tables use — do not "tidy" them.
REGISTRY: dict[str, LearningMetadata] = {
    # Atlas — English
    "atlas_data_basics_and_awareness_part_1":
        LearningMetadata(ATLAS, "Data Basics & Awareness Pt. 1", "English"),
    "atlas_data_basics_and_awareness_part_2":
        LearningMetadata(ATLAS, "Data Basics & Awareness Pt. 2", "English"),
    "atlas_datagovernance_basics":
        LearningMetadata(ATLAS, "Data Governance Basics", "English"),
    # Atlas — German (same course names, language differs)
    "atlas_german_data_basics_and_awareness_part_1":
        LearningMetadata(ATLAS, "Data Basics & Awareness Pt. 1", "German"),
    "atlas_german_data_basics_and_awareness_part_2":
        LearningMetadata(ATLAS, "Data Basics & Awareness Pt. 2", "German"),
    "atlas_german_datagovernance_basics":
        LearningMetadata(ATLAS, "Data Governance Basics", "German"),
    # Beacon — English. NOTE "Basic" not "Basics", and no "&" — Beacon names its courses differently and
    # that difference is intentional, not a typo to fix.
    "beacon_elearning_data_basic_awareness_part1":
        LearningMetadata(BEACON, "Data Basic Awareness Pt. 1", "English"),
    "beacon_elearning_data_basic_awareness_part2":
        LearningMetadata(BEACON, "Data Basic Awareness Pt. 2", "English"),
    "beacon_elearning_data_governance_basics_part1":
        LearningMetadata(BEACON, "Data Governance Basics Pt. 1", "English"),
    "beacon_elearning_data_governance_basics_part2":
        LearningMetadata(BEACON, "Data Governance Basics Pt. 2", "English"),
    # Beacon — German
    "beacon_elearning_german_data_basic_awareness_part1":
        LearningMetadata(BEACON, "Data Basic Awareness Pt. 1", "German"),
    "beacon_elearning_german_data_basic_awareness_part2":
        LearningMetadata(BEACON, "Data Basic Awareness Pt. 2", "German"),
    # Beacon governance, GERMAN. The registry only had the two English parts, so the real export
    # `..._eLearningDataGovernanceBasicsDeutsch_...` matched nothing. Verified against the live
    # workspace: there is no German Beacon governance table yet, so this one is created on first upload
    # (NEW TABLE per period, like every other per-course table).
    "beacon_elearning_german_data_governance_basics_part1":
        LearningMetadata(BEACON, "Data Governance Basics Pt. 1", "German"),
    # PlanTrack — course name comes from the row, not the table name.
    "plantrack_analytics_trainings":
        LearningMetadata(PLANTRACK, None, "English",
                         course_from_column="Learning_Name_Module"),
}

_PERIOD_SUFFIX = re.compile(r"_(20\d{2})[._]?(\d{2})$")


def strip_period(table: str) -> tuple[str, str | None]:
    """Split 'atlas_datagovernance_basics_2026_06' → ('atlas_datagovernance_basics', '2026.06').

    Returns (stem, period-or-None). Accepts a fully-qualified name and drops catalog/schema.
    """
    name = table.split(".")[-1].strip().lower()
    m = _PERIOD_SUFFIX.search(name)
    if not m:
        return name, None
    return name[: m.start()], f"{m.group(1)}.{m.group(2)}"


# ── Fallback parsing, for a course table not in the registry yet ─────────────────────────────────
_PLATFORM_PREFIX = (
    ("beacon_elearning_", BEACON),
    ("beacon_", BEACON),
    ("atlas_", ATLAS),
    ("plantrack_", PLANTRACK),
)

# Words in a table name that are structural, not part of the course title.
_DROP_WORDS = {"elearning", "german", "trainings", "training", "completions"}


def _humanise_course(stem: str, platform: str) -> str:
    """Best-effort human course name from the leftover table-name words.

    Only used for a table nobody has registered. Deliberately conservative: it title-cases and
    expands `part1` → `Part 1`, but it does NOT try to guess whether this platform says "Basic" or
    "Basics" — that is exactly the sort of silent mismatch the registry exists to prevent, so the
    result is marked `guessed=True` for the caller to surface.
    """
    body = stem
    for prefix, _ in _PLATFORM_PREFIX:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    body = re.sub(r"\bpart[_]?(\d)\b", r"part \1", body)
    words = [w for w in body.split("_") if w and w not in _DROP_WORDS]
    # 'datagovernance' is one token in the Atlas tables — split it so it reads normally.
    out = []
    for w in words:
        if w == "datagovernance":
            out += ["data", "governance"]
        else:
            out.append(w)
    return " ".join(w if w.isdigit() else w.capitalize() for w in out)


def derive_metadata(table: str) -> LearningMetadata | None:
    """(platform, course_name, language) for a per-course source table.

    Registry first — those strings match the live unified tables exactly. Falls back to parsing the
    name for an unregistered table, with `guessed=True` so the UI can warn rather than quietly
    inventing a course name that won't group with the existing rows. Returns None if the table is not
    a learning source at all.
    """
    stem, _ = strip_period(table)
    hit = REGISTRY.get(stem)
    if hit:
        return hit

    platform = next((p for prefix, p in _PLATFORM_PREFIX if stem.startswith(prefix)), None)
    if platform is None:
        return None
    # The one rule that is purely mechanical, so it is safe to apply to unknown tables too.
    language = "German" if "german" in stem else "English"
    if platform == PLANTRACK:
        return LearningMetadata(PLANTRACK, None, language,
                                course_from_column="Learning_Name_Module", guessed=True)
    return LearningMetadata(platform, _humanise_course(stem, platform) or None, language,
                            guessed=True)


def language_of(table: str) -> str:
    """'German' when the table name contains `german_`, else 'English' (the user's rule verbatim)."""
    stem, _ = strip_period(table)
    return "German" if "german" in stem else "English"


def sources_for(platform: str, period: str | None = None,
                known: list[str] | None = None) -> list[str]:
    """Registry table stems for one platform, optionally suffixed with a period.

    `known` filters to tables that actually exist (pass the live table list), so a UNION is never
    built over a month that was never loaded.
    """
    out = []
    for stem, meta in REGISTRY.items():
        if meta.platform != platform:
            continue
        name = f"{stem}_{period.replace('.', '_')}" if period else stem
        if known is None or name in known:
            out.append(name)
    return sorted(out)


def table_stem_for_filename(filename: str) -> str | None:
    """Which per-course table an uploaded export belongs in, from its FILENAME.

    The per-course sources each have their own table (`atlas_datagovernance_basics_2026_07`), so an
    upload must be matched to the right one rather than dumped into a generic `atlas_<month>`.
    Matching is done on a normalised form of the name — case, spaces, hyphens and the `part1`/`part_1`
    split all vary between exports — against the registry stems.
    """
    norm = _normalise_for_match(filename)
    if not norm:
        return None
    best: tuple[int, str] | None = None
    for stem in REGISTRY:
        key = _normalise_for_match(stem)
        if key and key in norm:
            # Longest match wins: 'atlas_german_data_basics…' must beat 'atlas_data_basics…'
            # when both appear as substrings of the same normalised filename.
            if best is None or len(key) > best[0]:
                best = (len(key), stem)
    if best:
        return best[1]
    # Substring matching fails on the REAL LMS export names, which are camelCase with no separators
    # and use German words rather than a `german_` prefix:
    #   100200_export_eLearningDataBasicsAwarenessPart1_20260803_0838.xlsx
    #   100200_export_eLearningDataGovernanceBasicsDeutsch_20260803_0837.xlsx
    # Note 'DataBasicsAwareness' (with the s) vs the Beacon stem 'data_basic_awareness' (without) — the
    # platforms spell the same course differently, so a literal substring can never line up. Fall back
    # to matching on the FEATURES that identify a course.
    return _match_by_features(filename)


# ── The export-ID prefix decides the platform (user, 2026-08-03) ────────────────────────────────
# Both platforms name their files `<id>_export_eLearning…`, so the word "eLearning" does NOT identify
# the platform — matching on it sent the Atlas file
# `300400_export_eLearningDataBasicsAwarenessTeil2_…` into the Beacon table. The leading numeric ID is
# the course catalogue id from the LMS, and it is what actually distinguishes them.
EXPORT_ID_PLATFORM: dict[str, str] = {
    "100200": BEACON,
    "300400": ATLAS,
}

_EXPORT_ID = re.compile(r"^\s*(\d{4,})[_\-]")


def platform_from_export_id(filename: str) -> str | None:
    """'100200_export_…' → Beacon, '300400_export_…' → Atlas. None if the prefix is unknown."""
    m = _EXPORT_ID.match(str(filename))
    return EXPORT_ID_PLATFORM.get(m.group(1)) if m else None


# Words that mark a German-language export. The LMS uses the German course title itself, so there is
# no `german_` in the filename at all.
_GERMAN_MARKERS = ("german", "deutsch", "teil")

# (course fingerprint, part number or None) → the registry key fragment.
_COURSE_MARKERS: list[tuple[tuple[str, ...], str]] = [
    (("governance",), "governance"),
    (("basic", "awareness"), "awareness"),   # 'basics awareness' and 'basic awareness' both hit
]


def _match_by_features(filename: str) -> str | None:
    """Identify a course from platform + course + language + part, rather than an exact name.

    Used when the filename is a raw LMS export. Returns None unless all the pieces needed to pick ONE
    registry row are present — a half-identified course must stay unrouted so the content-based
    suggestion in the UI can ask, instead of guessing a table.
    """
    low = re.sub(r"[^a-z0-9]+", " ", str(filename).lower())
    flat = low.replace(" ", "")

    # The numeric export id wins: it is the only thing that separates the two platforms' raw LMS
    # filenames, which are otherwise both `<id>_export_eLearning…`.
    platform = platform_from_export_id(filename)
    if platform is None:
        if "atlas" in flat or "horizinot" in flat:
            platform = ATLAS
        elif "plantrack" in flat:
            platform = PLANTRACK
        elif "beacon" in low.split() or "internationalplatform" in flat:
            platform = BEACON
        elif "elearning" in flat:
            # An eLearning export with an UNKNOWN id. Do not guess a platform — writing Atlas rows
            # into the Beacon table (or vice versa) corrupts both. Leave it unrouted so the UI asks.
            return None
    if platform is None:
        return None
    if platform == PLANTRACK:
        return "plantrack_analytics_trainings"

    course = next((tag for markers, tag in _COURSE_MARKERS
                   if all(m in flat for m in markers)), None)
    if course is None:
        return None
    german = any(m in flat for m in _GERMAN_MARKERS)
    m = re.search(r"(?:part|teil|pt)\s*(\d)", low)
    part = m.group(1) if m else None
    # An Beacon course with NO part number in the name means Part 1. Verified against the live workspace:
    # `..._eLearningDataGovernanceBasics_...` holds 20 rows (11 Passed, 9 In progress), an exact match
    # for `beacon_elearning_data_governance_basics_part1_2026_06`; part2 holds 1 row. Previously these
    # files matched nothing, so both governance exports fell through to the content-based suggestion,
    # which offered to dump them straight into `beacon_training_completions` — skipping the per-course
    # table entirely and losing the Part 1 / Part 2 split.
    if part is None and platform == BEACON:
        part = "1"

    for stem, meta in REGISTRY.items():
        if meta.platform != platform:
            continue
        if (meta.language == "German") != german:
            continue
        if course == "governance" and "governance" not in stem:
            continue
        if course == "awareness" and "awareness" not in stem:
            continue
        # The part number must agree. Atlas's governance course has no parts, so a governance
        # export with no part number matches it; Beacon splits governance into part1/part2.
        stem_part = re.search(r"part_?(\d)", stem)
        if part and stem_part and stem_part.group(1) != part:
            continue
        if part and not stem_part:
            continue
        if not part and stem_part:
            continue
        return stem
    return None


def _normalise_for_match(text: str) -> str:
    """Lowercase alphanumeric-only, with `part 1` / `part_1` / `part1` collapsed to `part1`.

    Reduces 'Beacon eLearning Data Basic Awareness Part 1.xlsx' and
    'beacon_elearning_data_basic_awareness_part1' to the same string.
    """
    low = re.sub(r"\.(xlsx|xls)$", "", str(text).lower())
    low = re.sub(r"_(20\d{2})[._]?(\d{2})\b", " ", low)     # drop a period suffix
    low = re.sub(r"\bpart[\s_]*(\d)\b", r"part\1", low)
    return re.sub(r"[^a-z0-9]+", "", low)


def build_union_sql(platform: str, period: str, catalog: str, schema: str,
                    known: list[str] | None = None) -> str | None:
    """The UNION ALL that stamps the metadata onto each 3-column source table.

    This is the SQL form of the combining rule: every source contributes
    `SELECT '<platform>', '<course>', '<language>', Company, Status, Country`, so the unified table
    gains the three columns the sources lack. PlanTrack reads its course from
    `Learning_Name_Module` instead of a literal.
    """
    parts: list[str] = []
    for stem in sources_for(platform, period, known):
        meta = REGISTRY[strip_period(stem)[0]]
        course = (f"`{meta.course_from_column}`" if meta.course_from_column
                  else "'" + (meta.course_name or "").replace("'", "''") + "'")
        parts.append(
            f"SELECT '{meta.platform}' AS platform, {course} AS course_name, "
            f"'{meta.language}' AS language, `Company`, `Status`, `Country` "
            f"FROM {catalog}.{schema}.{stem}"
        )
    if not parts:
        return None
    return "\nUNION ALL\n".join(parts)
