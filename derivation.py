"""The enrichment engine: fill in columns the source files don't have.

This is the work that used to be done by hand / by Genie / by the notebooks. Per the user
(2026-07-27): *"sometimes jobs added title when missing, or country was added from other files where
it was available — taking info from other files."* So there are TWO mechanisms, tried in order:

    1. LOOKUP  — copy the value from a related table that already has it, joined on email
                 (with domain-alias matching, or the join silently misses).
    2. DERIVE  — compute it: company bracket ISO code → email domain → the default country.
    3. LEAVE NULL — if neither works. Never invent a value; Job_Title has no derivation.

And never overwrite a value that is already present — every notebook uses `WHERE country IS NULL`.

Why the chain has three steps: most listing rows join to the community table by email and inherit an
identical Job_Title (copied, not coincidence). Rows that don't match still get a Country derived from
the domain, but usually no Job_Title — exactly the lookup → derive → leave-null chain.

Ordering matters: analytics_community is the enrichment SOURCE for the others, so it must be loaded
before teams_listing and guild in a run. See `SOURCE_ORDER`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd

import org_profile as _profile
from excel_ingestion import CATALOG, BRONZE, COUNTRY_BY_ISO, DEFAULT_COUNTRY

# ── Domain → country / company maps, alias groups ──────────────────────────────────────────────
# All organisation-specific data lives in config/org_profile.toml (loaded by org_profile.py); this
# module only holds the matching logic. Quirks worth knowing are documented next to the data there.
DOMAIN_COUNTRY: dict[str, str] = _profile.DOMAIN_COUNTRY

# Domain aliases: the SAME person across the organisation's domains. Without this the email join
# misses and you get duplicate people.
ALIAS_DOMAINS: list[set[str]] = _profile.ALIAS_DOMAINS


def _merge_alias_groups(groups: list[set[str]]) -> dict[str, int]:
    """Flatten the groups into domain → group-id, MERGING any that share a domain.

    `cloud.contoso.example` appears in two groups in the profile. A naive lookup using
    `ALIAS_DOMAINS.index(first group containing dom)` would resolve `northwind-at.example` to group 2
    while `cloud.contoso.example` resolved to group 0 — the two never compared equal and the same person survived
    deduplication twice. Overlapping groups are transitively merged here instead, so listing an
    overlap is a feature rather than a silent bug.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for group in groups:
        members = sorted(group)
        for dom in members:
            parent.setdefault(dom, dom)
        root = find(members[0])
        for dom in members[1:]:
            parent[find(dom)] = root

    roots = sorted({find(d) for d in parent})
    return {d: roots.index(find(d)) for d in parent}


_ALIAS_GROUP_ID = _merge_alias_groups(ALIAS_DOMAINS)

# Guide rule 10: when the same person appears under several aliases, which row wins.
# `cloud.contoso.example` > `contoso.example` > subsidiary. Lower number = higher priority; unlisted = subsidiary.
_ALIAS_PRIORITY: dict[str, int] = _profile.ALIAS_PRIORITY
_SUBSIDIARY_PRIORITY = 9


def alias_priority(email: str | None) -> int:
    """Rule 10's ordering: 0 for the cloud identity, 1 for the holding domains, 9 for a subsidiary."""
    dom = email_domain(email)
    return _ALIAS_PRIORITY.get(dom, _SUBSIDIARY_PRIORITY) if dom else _SUBSIDIARY_PRIORITY
DOMAIN_PRIORITY = _profile.LOOKUP_ORDER

_ISO_BRACKET = re.compile(r"\(([A-Z]{2})\)")


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in str(email):
        return None
    return str(email).split("@")[-1].strip().lower()


def local_part(email: str | None) -> str | None:
    if not email or "@" not in str(email):
        return None
    return str(email).split("@")[0].strip().lower()


def country_from_domain(email: str | None) -> str | None:
    d = email_domain(email)
    return DOMAIN_COUNTRY.get(d) if d else None


# Rule 6: the domain lookup yields BOTH Country and Company. The company strings come from the
# profile verbatim, not tidied-up versions: these land in table cells, so "Fabrikam Logistics CZ" and
# "Fabrikam Logistics" would be two different companies on a dashboard.
DOMAIN_COMPANY: dict[str, str] = _profile.DOMAIN_COMPANY

# Guide §9b step 3: `COALESCE(m.company, 'Unknown')` — an unmapped domain gets this, not NULL.
UNKNOWN_COMPANY = _profile.UNKNOWN_COMPANY


def company_from_domain(email: str | None, default: str | None = None) -> str | None:
    """Guide rule 6 / §9b: email domain → Company.

    `default` is what an unmapped domain returns. The guide's SQL uses `'Unknown'`; callers that
    would rather leave a real blank alone pass None.
    """
    d = email_domain(email)
    return DOMAIN_COMPANY.get(d, default) if d else default


# Guide §7 + §9b step 4: external consultants are NOT resolved by domain but by the COMPANY NAME
# containing a keyword — the domain may be a client's or a personal one.
COMPANY_CONTAINS_COUNTRY: list[tuple[str, str]] = _profile.COMPANY_CONTAINS_COUNTRY


def country_from_company_name(company: str | None) -> str | None:
    """Fallback for external consultants: 'contains ultratendency' → Germany (guide §9b step 4)."""
    if not company:
        return None
    low = str(company).lower()
    for needle, country in COMPANY_CONTAINS_COUNTRY:
        if needle in low:
            return country
    return None


def country_from_company(company: str | None) -> str | None:
    """Bracket ISO code in the company name: 'Alfa (HU)' → Hungary (guide Rule 7)."""
    if not company:
        return None
    m = _ISO_BRACKET.search(str(company))
    if m:
        return COUNTRY_BY_ISO.get(m.group(1))
    return None


def alias_key(email: str | None) -> str | None:
    """A join key that treats Contoso domain aliases as the same person.

    the cloud / contoso / corp domains all collapse to the same key, so
    `anna.schmidt@corp.contoso.example` matches `anna.schmidt@cloud.contoso.example`.
    """
    lp, dom = local_part(email), email_domain(email)
    if not lp:
        return None
    if dom and dom in _ALIAS_GROUP_ID:
        return f"{lp}@__alias{_ALIAS_GROUP_ID[dom]}"
    return f"{lp}@{dom}" if dom else lp


# Known mangled names seen in the real Guild/Teams exports, where an accented letter arrived as
# '?'. Restoring by whole-token match is safe; guessing per-character is not.
_MOJIBAKE_NAMES: dict[str, str] = _profile.NAME_REPAIRS


def clean_name(value: str | None) -> str | None:
    """Guide's "fix names, encodings, edge cases" cleanup (Guild notebook, Rule 8).

    Handles: 'Last, First' comma format → 'First Last'; mojibake placeholders from broken
    encodings; collapsed whitespace. Returns None for empty input so it stays NULL, not "".

    IMPORTANT: a '?' INSIDE a word is a mangled accented letter ('Sch?fer' = 'Schäfer'), so we must
    NOT delete it — that would silently corrupt the name to 'Schfer'. We restore the common
    Central-European cases (see `name_repairs` in the org profile) and
    otherwise leave the '?' visible so the bad row is findable instead of quietly wrong.
    """
    if value is None:
        return None
    s = str(value).replace("�", "?").strip()
    s = re.sub(r"\s+", " ", s)
    for bad, good in _MOJIBAKE_NAMES.items():
        s = s.replace(bad, good)
    # A trailing/standalone '?' carries no letter — safe to drop.
    s = re.sub(r"\s*\?\s*$", "", s).strip()
    if "," in s:
        last, _, first = s.partition(",")
        if first.strip() and last.strip():
            s = f"{first.strip()} {last.strip()}"
    return s or None


def split_name(value: str | None) -> tuple[str | None, str | None]:
    """'Anna Schmidt' → ('Anna', 'Schmidt'). Guild's source has one `Name` column but
    the table needs First_name/Last_name."""
    cleaned = clean_name(value)
    if not cleaned:
        return None, None
    bits = cleaned.split(" ")
    if len(bits) == 1:
        return bits[0], None
    return bits[0], " ".join(bits[1:])


def dedup_within_period(
    df: pd.DataFrame, email_col: str, period_col: str | None = None
) -> tuple[pd.DataFrame, int]:
    """Guide Rule 8: dedup by email WITHIN each reporting period.

    Across periods the same person SHOULD appear (that's how membership over time is tracked), so
    the period is part of the key. Alias-aware, and NULL emails are never collapsed together
    (there are 15 NULL-email rows in analytics_events_all that are distinct people).
    """
    if email_col not in df.columns:
        return df, 0
    out = df.copy()
    out["__k"] = out[email_col].map(alias_key)
    keyed = out["__k"].notna()
    subset = ["__k"] + ([period_col] if period_col and period_col in out.columns else [])
    # RULE 10 priority: cloud > contoso > subsidiary. `keep="first"` alone kept whatever row
    # happened to come first in the file, so the subsidiary address could win. Sort by priority
    # first (stable, so the original order still breaks ties) and the right row survives.
    out["__p"] = out[email_col].map(alias_priority)
    ordered = out[keyed].sort_values("__p", kind="stable")
    deduped = pd.concat([
        ordered.drop_duplicates(subset=subset, keep="first"),
        out[~keyed],  # NULL/blank emails pass through untouched
    ]).sort_index()
    removed = len(out) - len(deduped)
    return deduped.drop(columns=["__k", "__p"]), removed


@dataclass
class EnrichReport:
    """What the engine actually did — surfaced in the UI so the work is visible, not magic."""

    looked_up: dict[str, int] = field(default_factory=dict)
    derived: dict[str, int] = field(default_factory=dict)
    left_null: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        for col, n in sorted(self.looked_up.items()):
            if n:
                out.append(f"{col}: {n} value(s) looked up from a related table.")
        for col, n in sorted(self.derived.items()):
            if n:
                out.append(f"{col}: {n} value(s) derived (company bracket → email domain → default country).")
        for col, n in sorted(self.left_null.items()):
            if n:
                out.append(f"{col}: {n} row(s) left empty — no source to copy from and no rule to derive it.")
        return out + self.notes


def _blank(series: pd.Series) -> pd.Series:
    return series.isna() | (series.astype(str).str.strip().isin(["", "nan", "None"]))


def load_lookup_table(connection, table: str, columns: list[str]) -> pd.DataFrame:
    """Read a small enrichment source (e.g. analytics_community) into a frame keyed by alias_key."""
    cur = connection.cursor()
    try:
        cols = ", ".join(f"`{c}`" for c in columns)
        cur.execute(f"SELECT {cols} FROM {CATALOG}.{BRONZE}.`{table}`")
        rows = cur.fetchall()
    finally:
        cur.close()
    return pd.DataFrame([tuple(r) for r in rows], columns=columns)


def enrich(
    df: pd.DataFrame,
    *,
    email_col: str | None = None,
    company_col: str | None = None,
    name_col: str | None = None,
    lookup: pd.DataFrame | None = None,
    lookup_email_col: str | None = None,
    lookup_name_cols: list[str] | None = None,
    lookup_map: dict[str, str] | None = None,
    derive_country_into: str | None = "Country",
    german_country: bool = False,
) -> tuple[pd.DataFrame, EnrichReport]:
    """Fill blanks using LOOKUP → DERIVE → leave NULL. Existing values are never overwritten.

    lookup_map maps {target column in df: source column in lookup} — e.g.
    {"Job_Title": "Job_title", "Country": "Country"} to copy both from analytics_community.

    Matching runs in the guide's three tiers: exact email → domain alias (both via `alias_key`,
    needs `email_col`/`lookup_email_col`) → NAME (needs `name_col` + `lookup_name_cols`, e.g.
    ["First_name", "Last_name"]).
    """
    out = df.copy()
    rep = EnrichReport()

    # ── 1. LOOKUP from the related table, joined on the alias-aware email key ────────────────────
    if lookup is not None and lookup_map and email_col and email_col in out.columns:
        lk_email = lookup_email_col or "Email"
        if lk_email in lookup.columns:
            src = lookup.copy()
            src["__k"] = src[lk_email].map(alias_key)
            # Prefer the higher-priority domain when the same person appears twice (guide Rule 8).
            def _prio(e) -> int:
                d = email_domain(e)
                return DOMAIN_PRIORITY.index(d) if d in DOMAIN_PRIORITY else len(DOMAIN_PRIORITY)

            src["__prio"] = src[lk_email].map(_prio)
            src = src.sort_values("__prio").drop_duplicates(subset="__k", keep="first")
            src = src.set_index("__k")
            keys = out[email_col].map(alias_key)

            for target, source in lookup_map.items():
                if source not in src.columns:
                    continue
                if target not in out.columns:
                    out[target] = None
                mask = _blank(out[target]) & keys.notna()
                if not mask.any():
                    continue
                mapped = keys[mask].map(src[source])
                filled = mapped.notna()
                if filled.any():
                    out.loc[mask[mask].index[filled.values], target] = mapped[filled].values
                    rep.looked_up[target] = int(filled.sum())

    # ── 1b. NAME-BASED FALLBACK (guide Rule 8, third tier) ───────────────────────────────────────
    # After exact email and domain-alias matching, the guide falls back to matching on NAME — it
    # catches people whose email format differs entirely (firstname.lastname@ vs lastname@).
    # Measured worth on real exports: a handful of extra rows resolve through this tier.
    if (
        lookup is not None and lookup_map and name_col and name_col in out.columns
        and lookup_name_cols
    ):
        src = lookup.copy()
        parts = [src[c].fillna("").astype(str) for c in lookup_name_cols if c in src.columns]
        if parts:
            joined = parts[0]
            for extra in parts[1:]:
                joined = joined + " " + extra
            src["__n"] = joined.str.strip().str.lower().str.replace(r"\s+", " ", regex=True)
            src = src[src["__n"] != ""].drop_duplicates(subset="__n", keep="first").set_index("__n")
            names = (
                out[name_col].fillna("").astype(str)
                .str.strip().str.lower().str.replace(r"\s+", " ", regex=True)
            )
            for target, source in lookup_map.items():
                if source not in src.columns or target not in out.columns:
                    continue
                mask = _blank(out[target]) & (names != "")
                if not mask.any():
                    continue
                mapped = names[mask].map(src[source])
                filled = mapped.notna()
                if filled.any():
                    out.loc[mask[mask].index[filled.values], target] = mapped[filled].values
                    rep.looked_up[target] = rep.looked_up.get(target, 0) + int(filled.sum())
                    rep.notes.append(
                        f"{target}: {int(filled.sum())} more matched by NAME after email failed."
                    )

    # ── 2. DERIVE what's still blank (country only — Job_Title has no derivation) ────────────────
    if derive_country_into:
        col = next((c for c in out.columns if c.lower() == derive_country_into.lower()), None)
        if col is None:
            out[derive_country_into] = None
            col = derive_country_into
        if german_country:
            from excel_ingestion import normalize_german_country

            non_blank = ~_blank(out[col])
            if non_blank.any():
                out.loc[non_blank, col] = out.loc[non_blank, col].map(normalize_german_country)
                rep.notes.append("Normalized German country names → English.")

        mask = _blank(out[col])
        if mask.any():
            comp = company_col if company_col and company_col in out.columns else None
            mail = email_col if email_col and email_col in out.columns else None

            def _derive(row):
                return (
                    (country_from_company(row.get(comp)) if comp else None)
                    or (country_from_domain(row.get(mail)) if mail else None)
                    or DEFAULT_COUNTRY
                )

            out.loc[mask, col] = out[mask].apply(_derive, axis=1)
            rep.derived[col] = int(mask.sum())

    # ── 3. Report what is STILL empty — we leave it rather than invent a value ───────────────────
    for target in (lookup_map or {}):
        if target in out.columns:
            n = int(_blank(out[target]).sum())
            if n:
                rep.left_null[target] = n

    return out, rep


# Load order: enrichment SOURCES must be current before the tables that read from them.
# analytics_community feeds Country/Job_Title into teams_listing and guild; the event tables feed
# Events_Attended into guild. Part 1 sorts a multi-file upload by this.
SOURCE_ORDER: list[str] = [
    "analytics_community",                        # enrichment source for everything below
    "analytics_event_number_1_launch",
    "analytics_event_number_2",
    "analytics_event_number_3",
    "analytics_event_number_4",
    "analytics_events_all",
    "analytics_teams_listing",                    # needs community
    "lakehouse_guild",                        # needs community + events
    "registration_for_databricks_trainings",
    "atlas_training_completions",
    "beacon_training_completions",
]


def order_index(table: str | None) -> int:
    """Sort key so a multi-file upload loads enrichment sources first."""
    if not table:
        return len(SOURCE_ORDER)
    return SOURCE_ORDER.index(table) if table in SOURCE_ORDER else len(SOURCE_ORDER)
