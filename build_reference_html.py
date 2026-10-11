"""Generate a standalone HTML reference for the ingestion rules — FROM THE CODE.

    PYTHONPATH=. .venv/bin/python build_reference_html.py     → ingestion_reference.html

Why generate rather than hand-write: a hand-written page is a second copy of the rules that silently
goes stale the moment a route or a mapping changes, and then it's worse than nothing — you'd trust it.
Everything here is read out of `excel_ingestion`, `learning_metadata`, `derivation` and `links` at
build time, so the page always describes what the program actually does. Re-run it after any change.

The output is one self-contained file (inline CSS, no assets, no network) so it can be opened from
disk, mailed, or dropped into SharePoint.
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

import derivation as dv
import excel_ingestion as ei
import learning_metadata as lm
import links

OUT = Path(__file__).parent / "ingestion_reference.html"

# The TL;DR rules, each paired with where it lives in the code so the page
# is a map into the implementation rather than a restatement of the prose.
RULES: list[tuple[str, str]] = [
    ("ALWAYS upload the source file to the Volume FIRST (archive), THEN write the table. "
     "Volume files are NEVER deleted.",
     "excel_ingestion.archive_to_volume() runs before any table write, and still runs when the "
     "table write is refused."),
    ("APPEND new data as extra rows — never overwrite. Tag rows with reporting_month = 'YYYY.MM'.",
     "WriteMode.APPEND + _tag_reporting_month(). Re-running one month replaces just that month."),
    ("FULL REPLACE only for analytics_community, registration_for_databricks_trainings and "
     "databricks_trainings_participants (gold) — their exports are already cumulative.",
     "CUMULATIVE_MANUAL: the file is archived, the table write is refused and left to the notebooks."),
    ("Learning tables: a NEW TABLE per period with a _YYYY_MM suffix. Never overwrite a previous "
     "period.",
     "WriteMode.NEW_TABLE_PERIOD; IngestionPlan.full_table appends the period."),
    ("Unified completions: APPEND period rows with platform, course_name and language hardcoded "
     "per source table.",
     "learning_metadata.build_union_sql() + refresh_targets.rebuild_completions()."),
    ("Monthly learning exports are complete snapshots: dashboard totals use exactly ONE period, "
     "default to the NEWEST reporting month, and do not offer an All option that adds snapshots.",
     "refresh_targets._set_learning_month_defaults() advances every Reporting Month filter after "
     "a successful load; dashboard_capture._select_newest_month() enforces it for deck images."),
    ("Country from the email domain: 50+ hardcoded domains giving BOTH Country and Company. "
     "Default Austria.",
     "derivation.DOMAIN_COUNTRY and DOMAIN_COMPANY; consultants fall back to a company-name match."),
    ("Country from bracket codes on learning tables: 'Fabrikam (CZ)' → Czech Republic. Strip the bracket "
     "for display, KEEP it in bronze.",
     "derive_country() reads the bracket; strip_company_bracket() is display-only."),
    ("PlanTrack country names are GERMAN — normalise to English at the DASHBOARD level, NOT in "
     "bronze.",
     "Bronze keeps 'Tschechien'; GERMAN_COUNTRY is applied by the report/dashboard layer."),
    ("Atlas/Beacon have NO email column — only Company, Status, Country. Never attempt an "
     "email-based join.",
     "apply_cleaning() passes no_email=True for the learning and plantrack routes."),
    ("Deduplicate by email within each period. the cloud / contoso / corp domains are one person. "
     "Priority cloud &gt; contoso &gt; subsidiary.",
     "derivation.alias_key() + dedup_within_period(), ordered by alias_priority()."),
    ("Table naming: lowercase, underscores, no special characters. Learning tables "
     "&lt;platform&gt;_&lt;course&gt;_YYYY_MM.",
     "learning_metadata.REGISTRY holds the exact stems; route_for() resolves a file to one."),
    ("After upload the data must be CLEANED — missing Country/Company added, duplicates removed, "
     "reporting_month tagged. The data lives in the TABLE.",
     "This app does it on upload; no manual Genie step. See apply_cleaning()."),
]

CSS = """
:root{--brand-accent:#E4002B;--brand-accent-70:#ef4d68;--bg:#0b0d12;--panel:#151922;--panel-2:#1b2130;
  --text:#e8ecf4;--muted:#95a1b8;--border:#242c3d;--ok:#7CFFB2;--warn:#ffc86b;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:2.5rem 1.25rem 5rem;}
h1{font-size:2.4rem;letter-spacing:-.02em;margin:0 0 .35rem;font-weight:800;}
h1 .accent{background:linear-gradient(90deg,var(--brand-accent),var(--brand-accent-70));
  -webkit-background-clip:text;background-clip:text;color:transparent;}
.sub{color:var(--muted);max-width:70ch;margin:0 0 .5rem;}
.meta{color:var(--muted);font-size:.82rem;margin-bottom:2rem;}
h2{font-size:1.35rem;margin:2.5rem 0 .85rem;letter-spacing:-.01em;}
h2 .n{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;
  background:var(--brand-accent);color:#fff;border-radius:50%;font-size:.82rem;margin-right:10px;
  font-weight:800;vertical-align:2px;}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:1.1rem 1.25rem;
  margin-bottom:1rem;}
table{width:100%;border-collapse:collapse;font-size:.88rem;background:var(--panel);
  border:1px solid var(--border);border-radius:12px;overflow:hidden;}
th{text-align:left;background:var(--panel-2);padding:.6rem .75rem;font-size:.78rem;
  text-transform:uppercase;letter-spacing:.04em;color:var(--muted);border-bottom:1px solid var(--border);}
td{padding:.55rem .75rem;border-bottom:1px solid var(--border);vertical-align:top;}
tr:last-child td{border-bottom:none}
code{background:var(--panel-2);border:1px solid var(--border);border-radius:5px;
  padding:.08rem .35rem;font-size:.85em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
pre{background:var(--panel-2);border:1px solid var(--border);border-radius:10px;padding:.85rem 1rem;
  overflow-x:auto;font-size:.82rem;line-height:1.5;}
pre code{background:none;border:none;padding:0}
.tag{display:inline-block;padding:.12rem .5rem;border-radius:999px;font-size:.72rem;font-weight:700;
  letter-spacing:.03em;white-space:nowrap;}
.t-append{background:rgba(124,255,178,.14);color:var(--ok);border:1px solid rgba(124,255,178,.3);}
.t-replace{background:rgba(255,200,107,.14);color:var(--warn);border:1px solid rgba(255,200,107,.3);}
.t-new{background:rgba(228,0,43,.16);color:var(--brand-accent-70);border:1px solid rgba(228,0,43,.35);}
.t-archive{background:var(--panel-2);color:var(--muted);border:1px solid var(--border);}
.rule{display:grid;grid-template-columns:30px 1fr;gap:.75rem;padding:.7rem 0;
  border-bottom:1px solid var(--border);}
.rule:last-child{border-bottom:none}
.rule .num{width:24px;height:24px;border-radius:50%;background:var(--brand-accent);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:800;}
.rule .where{color:var(--muted);font-size:.82rem;margin-top:.2rem;}
.note{color:var(--muted);font-size:.85rem;}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem;}
a{color:var(--brand-accent-70)}
.pill{display:inline-block;background:var(--panel-2);border:1px solid var(--border);
  border-radius:999px;padding:.3rem .7rem;margin:.15rem .2rem .15rem 0;font-size:.8rem;}
.warn-box{background:rgba(255,200,107,.08);border:1px solid rgba(255,200,107,.28);
  border-radius:12px;padding:.9rem 1.1rem;margin:1rem 0;font-size:.88rem;}
footer{margin-top:3.5rem;padding-top:1.25rem;border-top:1px solid var(--border);
  color:var(--muted);font-size:.8rem;}
"""

_MODE_CLASS = {
    "append": "t-append", "full_replace": "t-replace", "new_table_period": "t-new",
    "new_table_event": "t-new", "archive_only": "t-archive",
}


def _mode_tag(mode: str) -> str:
    label = mode.replace("_", " ").upper()
    return f"<span class='tag {_MODE_CLASS.get(mode, 't-archive')}'>{label}</span>"


def _e(text: object) -> str:
    return html.escape(str(text))


def _routes_table() -> str:
    """Every routing rule, in match order — the real ROUTES list, not a description of it."""
    rows = []
    for pattern, route in ei.ROUTES:
        refused = route.table in ei.CUMULATIVE_MANUAL
        target = _e(route.table) if route.table else "<i>none</i>"
        if route.table == "analytics_event_number_N":
            # A placeholder, not a real table: route_for() substitutes the number from the filename.
            target = ("analytics_event_number_<b>N</b> "
                      "<span class='note'>(N from the filename)</span>")
        if refused:
            target += " <span class='tag t-archive'>notebook-owned</span>"
        rows.append(
            f"<tr><td><code>{_e(pattern.pattern)}</code></td>"
            f"<td>{target}</td>"
            f"<td>{_mode_tag(route.mode.value)}</td>"
            f"<td><code>{_e(route.cleaning)}</code></td>"
            f"<td class='note'>{_e(route.volume_subpath)}</td></tr>"
        )
    return (
        "<table><tr><th>Filename matches</th><th>Table</th><th>Mode</th><th>Cleaning</th>"
        "<th>Volume folder</th></tr>" + "".join(rows) + "</table>"
    )


def _registry_table() -> str:
    """The 13-row table-name → (platform, course, language) map."""
    rows = []
    for stem, meta in lm.REGISTRY.items():
        course = (f"<i>from <code>{_e(meta.course_from_column)}</code></i>"
                  if meta.course_from_column else _e(meta.course_name))
        rows.append(
            f"<tr><td><code>{_e(stem)}</code>_YYYY_MM</td><td>{_e(meta.platform)}</td>"
            f"<td>{course}</td><td>{_e(meta.language)}</td></tr>"
        )
    return ("<table><tr><th>Source table</th><th>platform</th><th>course_name</th>"
            "<th>language</th></tr>" + "".join(rows) + "</table>")


def _domain_table() -> str:
    """Domain → Country/Company, grouped by country so it reads like the guide's own listing."""
    by_country: dict[str, list[str]] = {}
    for dom, country in sorted(dv.DOMAIN_COUNTRY.items()):
        by_country.setdefault(country, []).append(dom)
    rows = []
    for country, doms in sorted(by_country.items()):
        pills = "".join(
            f"<span class='pill'>{_e(d)}"
            + (f" → {_e(dv.DOMAIN_COMPANY[d])}" if d in dv.DOMAIN_COMPANY else "")
            + "</span>"
            for d in doms
        )
        rows.append(f"<tr><td><b>{_e(country)}</b></td><td>{pills}</td></tr>")
    return ("<table><tr><th>Country</th><th>Domains → Company</th></tr>"
            + "".join(rows) + "</table>")


def _alias_table() -> str:
    groups: dict[int, list[str]] = {}
    for dom, gid in dv._ALIAS_GROUP_ID.items():
        groups.setdefault(gid, []).append(dom)
    rows = []
    for gid, doms in sorted(groups.items()):
        ranked = sorted(doms, key=dv.alias_priority)
        pills = "".join(
            f"<span class='pill'>{_e(d)}"
            + (" <b>← kept</b>" if i == 0 else "")
            + "</span>" for i, d in enumerate(ranked)
        )
        rows.append(f"<tr><td>Group {gid}</td><td>{pills}</td></tr>")
    return ("<table><tr><th>Alias group</th><th>Domains (highest priority first)</th></tr>"
            + "".join(rows) + "</table>")


def _german_table() -> str:
    rows = "".join(
        f"<tr><td>{_e(de)}</td><td>{_e(en)}</td></tr>"
        for de, en in sorted(ei.GERMAN_COUNTRY.items())
    )
    return f"<table><tr><th>German (source)</th><th>English (dashboard)</th></tr>{rows}</table>"


# ── Sections from the fuller guide that are prose, not code ─────────────────────────────────────
# These describe things the code cannot introspect (folder layout, per-source quirks, the operator's
# step-by-step). Kept short and pointed at the code that implements each step.
VOLUME_TREE = """registrations/
├── Analytics TEAMS Community/
│   ├── Full Analytics Community.xlsx        ← latest registration export (working copy)
│   ├── Analytics Community (2026.07.03).xlsx  ← dated archive copies
│   └── TEAMS listing/
│       ├── Analytics_Community_2026_06.xlsx ← Teams channel member export
│       └── Unregistered_Members_2026_06.xlsx  ← in Teams but NOT registered (derived)
├── Lakehouse Guild/
│   ├── Databricks_Guild.xlsx                 ← latest Guild member list
│   └── Databricks_Guild_2026.06.xlsx         ← dated archive
└── Databricks Trainings/
    └── 2026_07/Registration for Databricks Trainings(1-87).xlsx

trainings/
├── Atlas/                                 ← typo in the folder name, kept as-is
│   └── 2026_06/  atlas_*_2026_06.xlsx  +  atlas_training_completions.xlsx
├── Beacon Platform/
│   └── 2026_06/  beacon_elearning_*_2026_06.xlsx  +  beacon_training_completions.xlsx
├── Plan Track/
│   └── 2026_06/  plantrack_analytics_trainings_2026_06.xlsx
└── Databricks Trainings/
    └── 2026_06/  Databricks Trainings Participants.xlsx

communityevents/
└── Analytics Teams Events/
    ├── Analytics Event Number 1 (LAUNCH).xlsx … Number 4.xlsx
    └── analytics_events_all.xlsx                ← consolidated, APPEND per new event

employee_numbers/
└── 2025 Q4/Sales_Admin_12.2025.xlsx           ← quarter folders, not months"""

# (source, what's missing from the file, how the app fills it, dedup key)
DERIVED_COLUMNS: list[tuple[str, str, str, str]] = [
    ("analytics_community",
     "Country",
     "Bracket ISO code in Company → email domain → Austria. Company keeps its bracket.",
     "Email — newest submission by <code>Fertigstellungszeit</code>"),
    ("analytics_teams_listing",
     "Country, Company, reporting_month",
     "Both from the email domain (the column is called <code>User</code>, not Email).",
     "User/email, alias-aware, within the month"),
    ("lakehouse_guild",
     "Country, Company, Reporting_Month, Is_Community_Registered, Events_Attended",
     "Domain lookup for Country/Company; the two calculated columns are filled after the load by "
     "<code>refresh_targets.enrich_guild()</code>.",
     "Email, alias-aware, within the snapshot"),
    ("analytics_event_number_*",
     "nothing — Country is not derived for events",
     "Attendance ≤ 1 minute dropped; the longest-duration row per person kept.",
     "E-Mail per event"),
    ("atlas_* / beacon_elearning_*",
     "Country (sometimes), platform, course_name, language",
     "Country from the Company bracket code only — these tables have NO email (rule 9). The three "
     "metadata columns are stamped by the UNION.",
     "not applicable — no per-person identifier"),
    ("plantrack_*",
     "platform, language",
     "Country is present but GERMAN and stays German in bronze (rule 8). course_name comes from "
     "<code>Learning_Name_Module</code>.",
     "not applicable"),
    ("employee-numbers",
     "nothing",
     "No derivation at all — only a check that the columns are present.",
     "none"),
]

SCENARIOS: list[tuple[str, list[str]]] = [
    ("New Atlas / Beacon / PlanTrack period", [
        "Upload every course export together — the app sorts them and shows the order.",
        "Each file is archived to its platform folder under <code>YYYY_MM/</code>.",
        "Each becomes its OWN per-course table with the <code>_YYYY_MM</code> suffix; previous "
        "periods are untouched.",
        "Country is filled from the Company bracket code (no email on these tables).",
        "The unified <code>*_training_completions</code> tables get the new period APPENDED with "
        "platform / course_name / language stamped per source.",
    ]),
    ("New community event", [
        "Upload the Teams attendance export — any event number is recognised.",
        "It becomes <code>analytics_event_number_N</code>.",
        "Its rows are APPENDED to <code>analytics_events_all</code>, keyed by source file so earlier "
        "events are never rewritten.",
        "Upload a Guild snapshot too and <code>Events_Attended</code> is recounted.",
    ]),
    ("Monthly Teams listing / Guild", [
        "Upload the export. If the filename has no month, the app asks for one.",
        "Country and Company are derived from the email domain.",
        "Duplicates are removed within that period only, keeping the highest-priority alias.",
        "Rows are APPENDED with the month tag in the format that table already uses.",
    ]),
    ("Cumulative exports (community, registrations, gold participants)", [
        "Upload as usual — the file is archived to the Volume.",
        "The table write is deliberately REFUSED: these sources are cumulative and are owned by "
        "their notebooks, so writing them here could duplicate or overwrite history.",
        "The app tells you which notebook to run.",
    ]),
]


def _volume_tree() -> str:
    return f"<pre><code>{_e(VOLUME_TREE)}</code></pre>"


def _derived_table() -> str:
    rows = "".join(
        f"<tr><td><code>{_e(src)}</code></td><td>{_e(missing)}</td><td>{how}</td>"
        f"<td class='note'>{dedup}</td></tr>"
        for src, missing, how, dedup in DERIVED_COLUMNS
    )
    return ("<table><tr><th>Source</th><th>Not in the file</th><th>How the app fills it</th>"
            f"<th>Dedup key</th></tr>{rows}</table>")


def _scenarios() -> str:
    out = []
    for title, steps in SCENARIOS:
        items = "".join(f"<li>{s}</li>" for s in steps)
        out.append(
            f"<div class='card'><b>{_e(title)}</b>"
            f"<ol style='margin:.6rem 0 0;padding-left:1.2rem;font-size:.88rem;line-height:1.7'>"
            f"{items}</ol></div>"
        )
    return "".join(out)


def _links_section() -> str:
    items = [(links.SHAREPOINT_LABEL, links.SHAREPOINT_DECKS)]
    items += [(f"Dashboard — {n}", u) for n, u in links.DASHBOARDS.items()]
    items += [(f"Genie — {n}", u) for n, u in links.GENIE_SPACES.items()]
    return "".join(
        f"<div class='card'><a href='{_e(u)}' target='_blank' rel='noopener'>{_e(n)}</a></div>"
        for n, u in items
    )


def build() -> Path:
    rules_html = "".join(
        f"<div class='rule'><div class='num'>{i}</div><div><div>{rule}</div>"
        f"<div class='where'>{where}</div></div></div>"
        for i, (rule, where) in enumerate(RULES, 1)
    )
    example_sql = lm.build_union_sql(
        "Atlas", "2026.07", ei.CATALOG, ei.BRONZE,
        known=["atlas_datagovernance_basics_2026_07",
               "atlas_german_datagovernance_basics_2026_07"],
    ) or ""

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Analytics Ingestion Reference — Contoso</title>
<style>{CSS}</style></head><body><div class="wrap">

<h1>Analytics <span class="accent">Ingestion Reference</span></h1>
<p class="sub">What happens to every file you upload: where it is archived, which table it reaches,
which write mode applies, and which columns are derived. Generated from the code, so it describes
what the program actually does.</p>
<p class="meta">Generated {date.today().isoformat()} ·
Catalog <code>{_e(ei.CATALOG)}</code> ·
Bronze <code>{_e(ei.BRONZE)}</code> · Gold <code>{_e(ei.GOLD)}</code> ·
Volumes <code>{_e(links.VOLUMES_ROOT)}</code></p>

<h2><span class="n">1</span>The 13 non-negotiable rules</h2>
<div class="card">{rules_html}</div>

<h2><span class="n">2</span>Volume layout — where files are archived</h2>
<p class="note">Step 1 of every upload, always. Date folders are <code>YYYY_MM</code> (quarters for the
employee numbers). Nothing here is ever deleted, and a filename is never reused for different data.</p>
{_volume_tree()}

<h2><span class="n">3</span>File → table routing</h2>
<p class="note">Match order matters and is preserved below: the first pattern that matches a filename
wins. Three orderings are load-bearing — <code>unregistered</code> before the community and listing
patterns, the TEAMS listing before the community form, and participants before registrations.</p>
{_routes_table()}

<h2><span class="n">4</span>Columns the source files DON'T have</h2>
<p class="note">Most exports are missing something the dashboards need. These are derived on upload —
looked up, then derived, then left empty. An existing value is never overwritten, and a country is
never invented beyond the documented Austria default.</p>
{_derived_table()}

<h2><span class="n">5</span>Learning metadata — the combining rule</h2>
<p class="note">Each per-course source table holds only <code>Company</code>, <code>Status</code>,
<code>Country</code>. Which platform, course and language it represents exists <b>only in the table
name</b>, so the UNION hardcodes those three columns per source. Course names are literals, not
derived: Atlas writes “Data Basic<b>s</b> &amp; Awareness”, Beacon writes “Data Basic Awareness”.</p>
{_registry_table()}
<p class="note" style="margin-top:1rem">The generated SQL, for one period:</p>
<pre><code>{_e(example_sql)}</code></pre>

<h2><span class="n">6</span>Email domain → Country and Company</h2>
<p class="note">{len(dv.DOMAIN_COUNTRY)} domains mapped to a country,
{len(dv.DOMAIN_COMPANY)} of which also resolve a company. An unmapped domain defaults to
<b>{_e(ei.DEFAULT_COUNTRY)}</b> / <b>{_e(dv.UNKNOWN_COMPANY)}</b>. External consultants are matched on
the company NAME instead, because their address may be a client's or a personal one.</p>
{_domain_table()}

<h2><span class="n">7</span>Domain aliases and deduplication</h2>
<p class="note">The same person appears under several Contoso domains. Rows are de-duplicated by email
<b>within a period only</b> — across periods the same person should reappear, which is how membership
over time is tracked. When aliases collide, the highest-priority address survives.</p>
{_alias_table()}

<h2><span class="n">8</span>PlanTrack German country names</h2>
<div class="warn-box"><b>Rule 8:</b> these are normalised at the <b>dashboard</b> level, never in the
bronze table. Bronze mirrors the archived file, so a bronze query returns
<code>Tschechien</code>.</div>
{_german_table()}

<h2><span class="n">9</span>What to do when new data arrives</h2>
{_scenarios()}

<h2><span class="n">10</span>Where everything lives</h2>
<div class="grid2">{_links_section()}</div>

<footer>Generated by <code>build_reference_html.py</code> from
<code>excel_ingestion.py</code>, <code>learning_metadata.py</code>, <code>derivation.py</code> and
<code>links.py</code>. Re-run it after changing a route or a mapping so this page cannot go stale.
</footer>
</div></body></html>"""

    OUT.write_text(doc, encoding="utf-8")
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path} ({path.stat().st_size / 1024:.0f} KB)")
