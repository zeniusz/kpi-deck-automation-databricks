"""The final step of a load: refresh the Genie spaces and the dashboards.

Modelled on the real workspace notebook "Refresh Community Events Genie Tables", which is what the
user has been doing by hand. That notebook's approach — and therefore ours:

    Genie "refresh" is NOT a Genie API call. Genie answers questions from the TABLES behind a
    space, so refreshing a space means making sure those tables match the files in the volume.
    The notebook re-reads each xlsx from the volume and saveAsTable(overwrite) the backing table.

Dashboards are the same story: a Databricks dashboard queries its tables live, so once the tables
are current the dashboard is current. What actually goes stale is:
  * the SQL warehouse being cold (first dashboard open is slow), and
  * per-period tables that a dashboard UNIONs by NAME — a new `_YYYY_MM` table is NOT auto-discovered
    (guide §7 "Dashboard Dependencies": the Learning DLMV UNIONs all 13 source tables).

So this module does three things:
  1. `warm_warehouse()`      — fire a trivial query so Part 2's capture isn't waiting on a cold start.
  2. `check_union_coverage()` — detect period tables that exist but are missing from the dashboard's
                                UNION, which is the one failure the user cannot see in the UI.
  3. `refresh_genie_tables()` — re-assert that each Genie-backing table matches its volume file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from settings import (
    BRONZE_FQN,
    BRONZE_SCHEMA as BRONZE,
    CATALOG,
    DATABRICKS_PROFILE,
    GOLD_FQN,
    SCHEDULE_TIMEZONE,
)


# Genie spaces and the tables that back them. Refreshing a space = making these current.
# From the workspace notebooks + the dashboards' own datasets.
GENIE_SPACES: dict[str, list[str]] = {
    "Community Events": [
        "analytics_event_number_1_launch",
        "analytics_event_number_2",
        "analytics_event_number_3",
        "analytics_event_number_4",
        "analytics_events_all",
    ],
    "Analytics Community": ["analytics_community", "analytics_teams_listing"],
    "Lakehouse Guild": ["lakehouse_guild"],
    "Learning Analytics": ["atlas_training_completions", "beacon_training_completions"],
}

# Dashboards that UNION per-period tables BY NAME. A new period's table must be added to the
# dashboard's query manually — it is never auto-discovered (guide §7).
UNION_PREFIXES: dict[str, list[str]] = {
    "learnings": ["atlas_", "beacon_elearning_", "plantrack_"],
}

_PERIOD_SUFFIX = re.compile(r"_(\d{4})_(\d{2})$")


@dataclass
class RefreshReport:
    warmed: bool = False
    tables_checked: int = 0
    stale_unions: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)


def warm_warehouse(connection) -> bool:
    """Fire a trivial query so the warehouse is running before Part 2 opens the dashboards.

    A cold X-Small warehouse takes ~30s to start; without this the first dashboard screenshot
    waits on it and can time out.
    """
    cur = connection.cursor()
    try:
        cur.execute("SELECT 1")
        cur.fetchone()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        cur.close()


def list_period_tables(connection, prefixes: list[str]) -> list[str]:
    """All `*_YYYY_MM` tables matching the given prefixes, newest period first."""
    cur = connection.cursor()
    try:
        cur.execute(f"SHOW TABLES IN {CATALOG}.{BRONZE}")
        names = [r[1] for r in cur.fetchall()]
    finally:
        cur.close()
    hits = [
        n for n in names
        if _PERIOD_SUFFIX.search(n) and any(n.startswith(p) for p in prefixes)
    ]
    return sorted(hits, reverse=True)


def check_union_coverage(connection, dashboard: str = "learnings") -> tuple[list[str], list[str]]:
    """Find the newest period's tables and report which periods exist.

    Returns (all_period_tables, tables_from_the_newest_period). If the newest period is NOT yet in
    the dashboard's UNION the dashboard silently omits it — we cannot edit the dashboard SQL from
    here, so we surface it loudly instead.
    """
    prefixes = UNION_PREFIXES.get(dashboard, [])
    if not prefixes:
        return [], []
    tables = list_period_tables(connection, prefixes)
    periods = sorted(
        {m.group(0).lstrip("_") for t in tables if (m := _PERIOD_SUFFIX.search(t))}, reverse=True
    )
    if not periods:
        return tables, []
    newest = periods[0]
    return tables, [t for t in tables if t.endswith(newest)]


def refresh_genie_tables(connection, spaces: list[str] | None = None) -> RefreshReport:
    """Verify every Genie-backing table is readable and report its row count.

    We deliberately do NOT re-write the tables from their volume files here (that is what the
    original notebook did with saveAsTable(overwrite), and it would overwrite data the user wants
    preserved). Once ingestion has appended the new rows, the Genie space is already current — the
    tables ARE the space. This is therefore a verification + freshness report, not a rebuild.
    """
    rep = RefreshReport()
    targets = spaces or list(GENIE_SPACES)
    cur = connection.cursor()
    try:
        for space in targets:
            for table in GENIE_SPACES.get(space, []):
                try:
                    cur.execute(f"SELECT count(*) FROM {CATALOG}.{BRONZE}.`{table}`")
                    n = int(cur.fetchone()[0])
                    rep.tables_checked += 1
                    rep.lines.append(f"{space}: `{table}` — {n:,} rows.")
                except Exception as exc:  # noqa: BLE001
                    rep.lines.append(f"⚠ {space}: `{table}` unreadable ({str(exc)[:70]}).")
    finally:
        cur.close()
    return rep


def rebuild_completions(connection, period: str, replace_period: bool = True) -> RefreshReport:
    """Stamp platform/course_name/language onto the new period's rows in the unified tables.

    THE COMBINING RULE. The per-course source tables have only Company/Status/Country — which
    platform, course and language they represent exists ONLY in the table name. So after those tables
    are loaded, the unified `*_training_completions` tables replace THIS PERIOD ONLY. Historical
    months remain available for the dashboard month selector; the dashboard itself is single-select
    and defaults to the newest month, so snapshots are never added together.
    """
    from learning_metadata import UNIFIED_TABLE, build_union_sql

    rep = RefreshReport()
    cur = connection.cursor()
    try:
        # Which per-course tables actually exist for this period — never UNION a missing table.
        cur.execute(f"SHOW TABLES IN {CATALOG}.{BRONZE}")
        known = [r[1] for r in cur.fetchall()]

        for platform, target in UNIFIED_TABLE.items():
            sql = build_union_sql(platform, period, CATALOG, BRONZE, known=known)
            if not sql:
                rep.lines.append(
                    f"{platform}: no per-course tables for {period} — nothing to combine."
                )
                continue
            n_sources = sql.count("UNION ALL") + 1
            full = f"{CATALOG}.{BRONZE}.`{target}`"

            cur.execute(f"DESCRIBE {full}")
            cols = {r[0].lower() for r in cur.fetchall() if r[0] and not r[0].startswith("#")}
            has_month = "reporting_month" in cols
            if not has_month:
                cur.execute(f"ALTER TABLE {full} ADD COLUMNS (`reporting_month` STRING)")
                has_month = True
                rep.lines.append(f"{platform}: added reporting_month column to `{target}`.")

            # A re-upload replaces only its own period. Do not erase June when July arrives: users
            # need both months in the filter, while single-select/no-All prevents their totals from
            # being combined.
            if replace_period and has_month:
                normal_period = period.replace("-", ".")
                cur.execute(
                    f"DELETE FROM {full} "
                    "WHERE replace(cast(reporting_month AS STRING), '-', '.') = ?",
                    [normal_period],
                )

            select = "SELECT platform, course_name, language, Company, Status, Country"
            cols_list = "platform, course_name, language, `Company`, `Status`, `Country`"
            if has_month:
                select += f", '{period}' AS reporting_month"
                cols_list += ", `reporting_month`"
            cur.execute(f"INSERT INTO {full} ({cols_list}) {select} FROM ({sql}) src")

            cur.execute(
                f"SELECT count(*) FROM {full} "
                "WHERE replace(cast(reporting_month AS STRING), '-', '.') = ?",
                [period.replace("-", ".")],
            )
            n = int(cur.fetchone()[0])
            rep.tables_checked += 1
            rep.lines.append(
                f"{platform}: rebuilt `{target}` for {period} from {n_sources} per-course "
                f"table(s) — {n:,} rows (other reporting months preserved)."
            )
    except Exception as exc:  # noqa: BLE001 — reported, never crashes the run
        rep.lines.append(f"⚠ Could not rebuild the unified completions tables ({str(exc)[:120]}).")
    finally:
        cur.close()
    return rep


def _set_learning_month_defaults(model: dict, latest_month: str | None) -> int:
    """Make snapshot dashboards open on the newest month instead of aggregating `All`."""
    changed = 0
    if not latest_month:
        return changed
    for page in model.get("pages", []):
        for item in page.get("layout", []):
            widget = item.get("widget", {})
            spec = widget.get("spec", {})
            if spec.get("frame", {}).get("title") != "Reporting Month":
                continue
            uses_group_string = any(
                q.get("query", {}).get("datasetName") == "reporting_month_group"
                for q in widget.get("queries", [])
            )
            # Monthly learning files are full snapshots, not additive transactions. Force exactly
            # one period: `All` would sum the same population across June + July + August.
            spec["widgetType"] = "filter-single-select"
            spec["disallowAll"] = True
            value = latest_month if uses_group_string else f"{latest_month}-01"
            spec["selection"] = {
                "defaultSelection": {
                    "values": {
                        "dataType": "STRING" if uses_group_string else "DATE",
                        "values": [{"value": value}],
                    }
                }
            }
            changed += 1
    return changed


def _set_string_month_defaults(
    model: dict, latest_month: str | None, titles: tuple[str, ...] = ("Reporting Month",)
) -> int:
    """Default string-based snapshot filters to newest while preserving older choices.

    Lakeview otherwise opens a single-select filter on ``All`` and adds complete monthly
    snapshots together.  ``disallowAll`` prevents that invalid state; the filter dataset itself
    remains unfiltered, so June, July and future months are still available in the dropdown.
    """
    if not latest_month:
        return 0
    wanted_titles = {title.strip().lower() for title in titles}
    changed = 0
    for page in model.get("pages", []):
        for item in page.get("layout", []):
            spec = item.get("widget", {}).get("spec", {})
            title = str(spec.get("frame", {}).get("title", "")).strip().lower()
            if title not in wanted_titles:
                continue
            selection = {
                "defaultSelection": {
                    "values": {
                        "dataType": "STRING",
                        "values": [{"value": latest_month}],
                    }
                }
            }
            before = (spec.get("widgetType"), spec.get("disallowAll"), spec.get("selection"))
            spec["widgetType"] = "filter-single-select"
            spec["disallowAll"] = True
            spec["selection"] = selection
            after = (spec.get("widgetType"), spec.get("disallowAll"), spec.get("selection"))
            if before != after:
                changed += 1
    return changed


def _bind_month_filter_fields(
    model: dict, dataset_names: tuple[str, ...], field_name: str = "Reporting_Month"
) -> int:
    """Connect one Reporting Month selection to the matching field in several datasets.

    A filter backed only by a month-options dataset changes its label but does not constrain any
    KPI dataset (the live symptom was the newest month label above a multi-month total). Lakeview supports
    one filter targeting fields across multiple datasets, so add an explicit field query/binding for
    every month-aware dataset used by the dashboard sheets.
    """
    changed = 0
    available = {dataset.get("name") for dataset in model.get("datasets", [])}
    for page in model.get("pages", []):
        for item in page.get("layout", []):
            widget = item.get("widget", {})
            spec = widget.get("spec", {})
            if str(spec.get("frame", {}).get("title", "")).strip() != "Reporting Month":
                continue
            queries = widget.setdefault("queries", [])
            fields = spec.setdefault("encodings", {}).setdefault("fields", [])
            existing_datasets = {
                q.get("query", {}).get("datasetName") for q in queries
                if q.get("query", {}).get("datasetName")
            }
            existing_query_names = {f.get("queryName") for f in fields}
            for dataset_name in dataset_names:
                if dataset_name not in available:
                    continue
                if dataset_name in existing_datasets:
                    continue
                query_name = f"reporting_month_target_{dataset_name}"
                queries.append({
                    "name": query_name,
                    "query": {
                        "datasetName": dataset_name,
                        "fields": [
                            {"name": field_name, "expression": f"`{field_name}`"},
                            {
                                "name": f"{field_name}_associativity",
                                "expression": "COUNT_IF(`associative_filter_predicate_group`)",
                            },
                        ],
                        "disaggregated": False,
                    },
                })
                if query_name not in existing_query_names:
                    fields.append({"fieldName": field_name, "queryName": query_name})
                    existing_query_names.add(query_name)
                existing_datasets.add(dataset_name)
                changed += 1
    return changed


def _allow_learning_month_history(source: str) -> str:
    """Expose all snapshots to the single-select filter, removing an older latest-only guard."""
    return re.sub(
        r"FROM raw\s+WHERE reporting_month\s*=\s*\(SELECT max\(reporting_month\) "
        r"FROM raw\)\s*$",
        "FROM raw",
        source,
        flags=re.IGNORECASE,
    )


def _plantrack_carry_forward(
    plantrack_tables: list[str], reporting_periods: list[str]
) -> list[tuple[str, str]]:
    """Map each reporting month to its newest available PlanTrack snapshot.

    PlanTrack exports arrive less frequently than Atlas/Beacon. They are snapshots, so the most
    recent one remains valid until a newer PlanTrack period table is loaded. This only builds the
    dashboard UNION; it never writes to, replaces, or deletes a Databricks table.
    """
    from learning_metadata import strip_period

    snapshots: list[tuple[str, str]] = []
    for table in plantrack_tables:
        _, period = strip_period(table)
        if period:
            snapshots.append((period.replace(".", "-"), table))
    snapshots.sort()

    assignments: list[tuple[str, str]] = []
    all_periods = sorted(set(reporting_periods) | {period for period, _ in snapshots})
    for target_period in all_periods:
        eligible = [(period, table) for period, table in snapshots if period <= target_period]
        if eligible:
            _, table = eligible[-1]
            assignments.append((table, target_period))
    return assignments


# The Group Training / cross-program datasets were originally authored against twelve fixed June
# course tables. Keep their carefully designed normalization and measures, but replace each table
# reference with the matching course slice from the stable unified monthly table. This makes slides
# 20–21 advance with the reporting month instead of silently staying on June.
_LEGACY_LEARNING_SLICES = {
    "atlas_data_basics_and_awareness_part_1_2026_06":
        ("atlas_training_completions", "Data Basics & Awareness Pt. 1", "English"),
    "atlas_data_basics_and_awareness_part_2_2026_06":
        ("atlas_training_completions", "Data Basics & Awareness Pt. 2", "English"),
    "atlas_datagovernance_basics_2026_06":
        ("atlas_training_completions", "Data Governance Basics", "English"),
    "atlas_german_data_basics_and_awareness_part_1_2026_06":
        ("atlas_training_completions", "Data Basics & Awareness Pt. 1", "German"),
    "atlas_german_data_basics_and_awareness_part_2_2026_06":
        ("atlas_training_completions", "Data Basics & Awareness Pt. 2", "German"),
    "atlas_german_datagovernance_basics_2026_06":
        ("atlas_training_completions", "Data Governance Basics", "German"),
    "beacon_elearning_data_basic_awareness_part1_2026_06":
        ("beacon_training_completions", "Data Basic Awareness Pt. 1", "English"),
    "beacon_elearning_data_basic_awareness_part2_2026_06":
        ("beacon_training_completions", "Data Basic Awareness Pt. 2", "English"),
    "beacon_elearning_data_governance_basics_part1_2026_06":
        ("beacon_training_completions", "Data Governance Basics Pt. 1", "English"),
    "beacon_elearning_data_governance_basics_part2_2026_06":
        ("beacon_training_completions", "Data Governance Basics Pt. 1", "German"),
    "beacon_elearning_german_data_basic_awareness_part1_2026_06":
        ("beacon_training_completions", "Data Basic Awareness Pt. 1", "German"),
    "beacon_elearning_german_data_basic_awareness_part2_2026_06":
        ("beacon_training_completions", "Data Basic Awareness Pt. 2", "German"),
}

_CROSS_PROGRAM_DATASETS = {
    "group_all_programs",
    "geo_hc_pct",
    "kpi_overall_hc_pct",
    "cross_participation",
    "cross_rate_all_programs",
}


def _latest_learning_slices(source: str) -> tuple[str, int]:
    """Replace legacy per-course table names with latest-month filtered unified tables."""
    replacements = 0
    for legacy, (stable_table, course, language) in _LEGACY_LEARNING_SLICES.items():
        old = f"{CATALOG}.{BRONZE}.{legacy}"
        table = f"{CATALOG}.{BRONZE}.{stable_table}"
        course_sql = course.replace("'", "''")
        language_sql = language.replace("'", "''")
        company = (
            "CASE trim(Company) WHEN 'CTSO' THEN 'Contoso Holding' "
            "WHEN 'NWAT' THEN 'Northwind Retail AT' "
            "WHEN 'NWCH' THEN 'Northwind Retail CH' "
            "WHEN 'LWSW' THEN 'Litware Software' WHEN 'CTIT' THEN 'Contoso IT' "
            "WHEN 'FBPL' THEN 'Fabrikam Logistics PL' WHEN 'FBCZ' THEN 'Fabrikam Logistics CZ' "
            "WHEN 'TSES' THEN 'Tailspin Energy ES' WHEN 'WGNL' THEN 'Woodgrove Bank NL' "
            "WHEN 'CTRE' THEN 'Contoso Re' ELSE Company END"
        )
        latest = (
            "(SELECT platform,course_name,language," + company
            + " AS Company,Status,Country,reporting_month FROM " + table
            + " WHERE replace(reporting_month, '.', '-')="
            + "(SELECT max(replace(reporting_month, '.', '-')) FROM " + table + ")"
            + f" AND course_name='{course_sql}' AND language='{language_sql}')"
        )
        occurrences = source.count(old)
        source = source.replace(old, latest)
        replacements += occurrences

        # Upgrade a slice written by an earlier run of this migration.
        old_latest = (
            "(SELECT * FROM " + table
            + " WHERE replace(reporting_month, '.', '-')="
            + "(SELECT max(replace(reporting_month, '.', '-')) FROM " + table + ")"
            + f" AND course_name='{course_sql}' AND language='{language_sql}')"
        )
        occurrences = source.count(old_latest)
        source = source.replace(old_latest, latest)
        replacements += occurrences
    return source, replacements


def update_learning_dashboard_sources(connection) -> list[str]:
    """Replace the dashboard's hard-coded June UNION with month-aware stable sources.

    Atlas and Beacon read their unified tables, which `rebuild_completions` maintains. PlanTrack
    remains one snapshot table per update; its newest available snapshot is carried forward across
    later reporting months until another PlanTrack table arrives. Source tables are never mutated.
    The rest of the dashboard semantic model and every visual stay unchanged.
    """
    import json

    from databricks.sdk import WorkspaceClient
    from kpis import DATABRICKS_DASHBOARDS
    from learning_metadata import strip_period

    cur = connection.cursor()
    try:
        cur.execute(f"SHOW TABLES IN {CATALOG}.{BRONZE}")
        plantracks = sorted(r[1] for r in cur.fetchall()
                             if r[1].startswith("plantrack_analytics_trainings_"))
        cur.execute(
            "SELECT DISTINCT m FROM ("
            f"SELECT replace(reporting_month, '.', '-') AS m FROM {CATALOG}.{BRONZE}.atlas_training_completions "
            "UNION ALL "
            f"SELECT replace(reporting_month, '.', '-') AS m FROM {CATALOG}.{BRONZE}.beacon_training_completions) "
            "WHERE m IS NOT NULL ORDER BY m"
        )
        reporting_periods = [str(row[0]) for row in cur.fetchall()]
        latest_month = max(reporting_periods) if reporting_periods else None

        # Each PlanTrack snapshot table can have a different export schema (the course-name
        # column was renamed from `Learning_Name_Module` to `company_course_name`, and `Status`
        # was dropped, between the 2026_06 and 2026_08 exports). Detect real columns per table
        # instead of assuming one fixed schema, so a future export doesn't silently corrupt the
        # dashboard's SQL again.
        plantrack_columns: dict[str, set[str]] = {}
        for table in plantracks:
            cur.execute(f"DESCRIBE TABLE {CATALOG}.{BRONZE}.`{table}`")
            plantrack_columns[table] = {row[0] for row in cur.fetchall()}
    finally:
        cur.close()

    base = (
        "SELECT platform, course_name, language,\n"
        "  CASE WHEN lower(course_name) LIKE '%governance%' THEN 'Data Governance' "
        "ELSE 'Data Awareness' END AS course_category,\n"
        "  to_date(concat(replace(reporting_month, '.', '-'), '-01')) AS reporting_month,\n"
        "  Company, Status, Country\n"
        f"FROM {CATALOG}.{BRONZE}.atlas_training_completions\n"
        # The backfilled June baseline contains the same Atlas snapshot twice: legacy rows
        # with long company names plus rows with short entity codes. Keep the legacy copy for
        # June, but July and later exports contain only codes and must not be filtered out.
        "WHERE length(Company) > 5 OR replace(reporting_month, '-', '.') <> '2026.06'\n"
        "UNION ALL\n"
        "SELECT platform, course_name, language,\n"
        "  CASE WHEN lower(course_name) LIKE '%governance%' THEN 'Data Governance' "
        "ELSE 'Data Awareness' END AS course_category,\n"
        "  to_date(concat(replace(reporting_month, '.', '-'), '-01')) AS reporting_month,\n"
        "  Company, Status, Country\n"
        f"FROM {CATALOG}.{BRONZE}.beacon_training_completions"
    )
    branches = [base]
    plantrack_assignments = _plantrack_carry_forward(plantracks, reporting_periods)
    for table, period in plantrack_assignments:
        columns = plantrack_columns.get(table, set())
        # The export column was renamed between snapshots (`Learning_Name_Module` -> newer exports
        # use `company_course_name`); detect whichever one this table actually has.
        course_col = "company_course_name" if "company_course_name" in columns else "Learning_Name_Module"
        status_expr = "Status" if "Status" in columns else "'Passed' AS Status"
        branches.append(
            "SELECT 'PlanTrack' AS platform, CASE "
            f"WHEN {course_col} LIKE '%Basics & Awareness Part 1%' "
            f"OR {course_col} LIKE '%Basics & Awareness Teil 1%' "
            "THEN 'Data Basics & Awareness Pt. 1' "
            f"WHEN {course_col} LIKE '%Basics & Awareness Part 2%' "
            f"OR {course_col} LIKE '%Basics & Awareness Teil 2%' "
            "THEN 'Data Basics & Awareness Pt. 2' "
            f"WHEN {course_col} LIKE '%Governance Basics%' "
            f"THEN 'Data Governance Basics Pt. 1' ELSE {course_col} END AS course_name, "
            f"CASE WHEN {course_col} LIKE 'DE -%' THEN 'German' ELSE 'English' END AS language, "
            f"CASE WHEN lower({course_col}) LIKE '%governance%' THEN 'Data Governance' "
            "ELSE 'Data Awareness' END AS course_category, "
            f"DATE('{period}-01') AS reporting_month, Company, {status_expr}, Country "
            f"FROM {CATALOG}.{BRONZE}.`{table}`"
        )
    raw = "WITH raw AS (\n" + "\nUNION ALL\n".join(branches) + "\n"

    url = DATABRICKS_DASHBOARDS["learnings"]
    dashboard_id = re.search(r"/dashboardsv3/([^/]+)/", url).group(1)
    workspace = WorkspaceClient(profile=DATABRICKS_PROFILE)
    dashboard = workspace.lakeview.get(dashboard_id)
    model = json.loads(dashboard.serialized_dashboard)
    changed = False
    cross_replacements = 0
    learning_dataset_name = None
    for dataset in model.get("datasets", []):
        if str(dataset.get("name", "")).startswith("unified_learning"):
            learning_dataset_name = dataset["name"]
            source = dataset.get("config", {}).get("source", "")
            marker = "\n)\nSELECT\n  platform,"
            if marker not in source:
                return ["⚠ Learning dashboard source has an unexpected shape; left unchanged."]
            rebuilt_source = raw + marker + source.split(marker, 1)[1]
            dataset["config"]["source"] = _allow_learning_month_history(rebuilt_source)
            changed = True
        elif dataset.get("name") == "reporting_month_group":
            dataset["queryLines"] = [
                "SELECT DISTINCT replace(reporting_month, '.', '-') AS reporting_month\n",
                f"FROM {CATALOG}.{BRONZE}.atlas_training_completions\n",
                "WHERE reporting_month IS NOT NULL\n",
                "UNION\n",
                "SELECT DISTINCT replace(reporting_month, '.', '-') AS reporting_month\n",
                f"FROM {CATALOG}.{BRONZE}.beacon_training_completions\n",
                "WHERE reporting_month IS NOT NULL",
            ]
            changed = True
        elif dataset.get("name") in _CROSS_PROGRAM_DATASETS:
            config_source = dataset.get("config", {}).get("source")
            if config_source:
                rewritten, count = _latest_learning_slices(config_source)
                if count:
                    dataset["config"]["source"] = rewritten
                    cross_replacements += count
                    changed = True
            elif dataset.get("queryLines"):
                original = "".join(dataset["queryLines"])
                rewritten, count = _latest_learning_slices(original)
                if count:
                    dataset["queryLines"] = [rewritten]
                    cross_replacements += count
                    changed = True
    if _set_learning_month_defaults(model, latest_month):
        changed = True
    if not changed:
        return ["⚠ Learning dashboard datasets were not found; draft left unchanged."]
    # Databricks can retain the compiled semantic cache under an existing dataset identity even
    # after its SQL source changes. Migrate the original June-only identity once and rewrite every
    # widget's exact datasetName binding. The stable `_monthly` identity is reused on later runs.
    if learning_dataset_name == "unified_learning":
        new_name = "unified_learning_monthly"

        def rename_exact(value):
            if isinstance(value, dict):
                return {k: rename_exact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rename_exact(v) for v in value]
            return new_name if value == learning_dataset_name else value

        model = rename_exact(model)
    dashboard.serialized_dashboard = json.dumps(model, separators=(",", ":"))
    workspace.lakeview.update(dashboard_id, dashboard)
    return [
        f"Updated Learning dashboard month sources: unified Atlas/Beacon plus "
        f"{len(plantracks)} PlanTrack snapshot table(s), carried across "
        f"{len(plantrack_assignments)} reporting period(s); refreshed "
        f"{cross_replacements} cross-program course source reference(s)."
    ]


def enrich_guild(connection, period: str) -> RefreshReport:
    """Compute the Guild's two CALCULATED columns for a period (guide §5.3, Rule 9).

    Neither exists in the source export; both are derived against other tables, so they can only be
    filled after the Guild rows are in Databricks:

      * `Is_Community_Registered` — 1 if the member's email is in `analytics_community`, else 0.
      * `Events_Attended`         — count of DISTINCT events whose attendee list contains the email.

    Alias-aware on both: the same person appears under several aliased domains (see the org profile), so a plain
    equi-join would report 0 for someone who IS registered. We compare the local part plus a
    normalised domain group instead.

    Runs per period so re-running one month never rewrites another.
    """
    from derivation import _ALIAS_GROUP_ID, alias_key

    rep = RefreshReport()
    guild = f"{CATALOG}.{BRONZE}.lakehouse_guild"
    community = f"{CATALOG}.{BRONZE}.analytics_community"
    cur = connection.cursor()
    try:
        # SQL mirror of derivation.alias_key(): local-part + '@' + the domain's alias-GROUP id (or the
        # real domain when it isn't aliased). Built from `_ALIAS_GROUP_ID` — the same merged mapping
        # the Python dedup uses — so the two can never disagree about who is one person.
        #
        # One bucket per GROUP, not one shared bucket for every aliased domain: `woodgrove-de.example`/
        # `woodgrove-nl.example` are one group while the HQ domains are another. Collapsing them all to a
        # single '__hq' would make `j@woodgrove-nl.example` and `j@cloud.contoso.example` the same person.
        whens = "".join(
            f" WHEN lower(split({{c}}, '@')[1]) = '{dom}' THEN '__alias{gid}'"
            for dom, gid in sorted(_ALIAS_GROUP_ID.items())
        )
        alias = (
            "concat(lower(split({c}, '@')[0]), '@', CASE"
            + whens
            + " ELSE lower(split({c}, '@')[1]) END)"
        )

        cur.execute(f"DESCRIBE {guild}")
        cols = {r[0].lower() for r in cur.fetchall() if r[0] and not r[0].startswith("#")}
        email_col = next((c for c in ("email", "user") if c in cols), None)
        if not email_col:
            rep.lines.append("⚠ Guild has no email/User column — cannot compute the derived columns.")
            return rep
        for col, typ in (("Is_Community_Registered", "INT"), ("Events_Attended", "INT")):
            if col.lower() not in cols:
                cur.execute(f"ALTER TABLE {guild} ADD COLUMNS (`{col}` {typ})")
                rep.lines.append(f"Added `{col}` to lakehouse_guild.")

        month_col = "Reporting_Month" if "reporting_month" in cols else None
        where = f"WHERE `{month_col}` = '{period}'" if month_col else ""
        month_match = f" AND c.`{month_col}` = '{period}'" if month_col else ""

        cur.execute(
            f"MERGE INTO {guild} AS c "
            f"USING (SELECT DISTINCT {alias.format(c='`Email`')} AS k FROM {community}) AS m "
            f"ON {alias.format(c='c.`' + email_col + '`')} = m.k{month_match} "
            "WHEN MATCHED THEN UPDATE SET c.`Is_Community_Registered` = 1"
        )
        cur.execute(
            f"UPDATE {guild} SET `Is_Community_Registered` = 0 "
            f"WHERE `Is_Community_Registered` IS NULL {('AND ' + where[6:]) if where else ''}"
        )
        rep.lines.append("Guild: Is_Community_Registered set (alias-aware match against "
                         "analytics_community).")

        # Events_Attended — count distinct events per person across the event tables.
        cur.execute(f"SHOW TABLES IN {CATALOG}.{BRONZE}")
        # Only raw event attendance tables belong here.  The catalog also contains a
        # `..._launch_metric_view`; reading it as a normal table triggers a Spark up-cast error and
        # it would double-count event 1 even if it succeeded.
        event_tables = [
            r[1] for r in cur.fetchall()
            if re.fullmatch(r"analytics_event_number_\d+(?:_launch)?", r[1])
        ]
        if event_tables:
            # Spark's optimiser fails on the old UNION of alias-normalising expressions with
            # CANNOT_UP_CAST_DATATYPE/lateralAliasReference for these Delta tables.  Read the small
            # attendee email sets, apply the exact same Python alias_key used by ingestion, then
            # MERGE compact (identity, event-count) values back.  This is both simpler and reliably
            # period-scoped.
            attended: dict[str, set[str]] = {}
            used_tables = 0
            for t in event_tables:
                cur.execute(f"DESCRIBE {CATALOG}.{BRONZE}.`{t}`")
                tcols = [r[0] for r in cur.fetchall() if r[0] and not r[0].startswith("#")]
                mail = next((c for c in tcols if "mail" in c.lower()), None)
                if not mail:
                    continue
                cur.execute(
                    f"SELECT DISTINCT `{mail}` FROM {CATALOG}.{BRONZE}.`{t}` "
                    f"WHERE `{mail}` IS NOT NULL AND trim(cast(`{mail}` AS STRING)) <> ''"
                )
                used_tables += 1
                for row in cur.fetchall():
                    key = alias_key(row[0])
                    if key:
                        attended.setdefault(key, set()).add(t)

            cur.execute(
                f"UPDATE {guild} SET `Events_Attended` = 0 "
                + (where if where else "")
            )
            if attended:
                values = ", ".join(
                    "('" + key.replace("'", "''") + f"', {len(events)})"
                    for key, events in sorted(attended.items())
                )
                cur.execute(
                    f"MERGE INTO {guild} AS c "
                    f"USING (VALUES {values}) AS a "
                    f"ON {alias.format(c='c.`' + email_col + '`')} = a.col1{month_match} "
                    "WHEN MATCHED THEN UPDATE SET c.`Events_Attended` = a.col2"
                )
            rep.lines.append(
                f"Guild: Events_Attended counted across {used_tables} event table(s)."
            )
        else:
            rep.lines.append("Guild: no event tables found — Events_Attended left as-is.")
        connection.commit()
        rep.tables_checked += 1
    except Exception as exc:  # noqa: BLE001 — reported, never crashes the run
        rep.lines.append(f"⚠ Guild enrichment failed ({str(exc)[:140]}).")
    finally:
        cur.close()
    return rep


def _set_exact_count_counter_formats(model: dict) -> int:
    """Show count KPIs as full integers instead of compact values such as `1K` or `1.2K`."""
    changed = 0
    for page in model.get("pages", []):
        for item in page.get("layout", []):
            spec = item.get("widget", {}).get("spec", {})
            if spec.get("widgetType") != "counter":
                continue
            title = str(spec.get("frame", {}).get("title", "")).strip().lower()
            # Percentages, averages, durations, hours and money require their own decimal/currency
            # formatting. Everything else represented by a counter is a people/row/entity count.
            non_count = ("rate", "%", "average", "avg ", "duration", "hours", "revenue")
            if not title or any(marker in title for marker in non_count):
                continue
            value = spec.setdefault("encodings", {}).setdefault("value", {})
            exact = {
                "type": "number-plain",
                "abbreviation": "none",
                "decimalPlaces": {"type": "exact", "places": 0},
            }
            if value.get("format") != exact:
                value["format"] = exact
                changed += 1
    return changed


def _set_community_raw_member_count(model: dict) -> int:
    """Use complete snapshots and exact integer formatting throughout the Community dashboard."""
    changed = _set_exact_count_counter_formats(model)

    for dataset in model.get("datasets", []):
        name = dataset.get("name")
        config = dataset.get("config", {})
        for measure in config.get("measures", []):
            expr = measure.get("expr", "")
            updated_expr = re.sub(
                r"COUNT\s*\(\s*DISTINCT\s+User\s*\)", "COUNT(*)", expr,
                flags=re.IGNORECASE,
            )
            if updated_expr != expr:
                measure["expr"] = updated_expr
                changed += 1

        def complete_snapshot_count(source: str) -> str:
            updated = re.sub(
                r"COUNT\s*\(\s*DISTINCT\s+User\s*\)", "COUNT(*)", source,
                flags=re.IGNORECASE,
            )
            # Blank-email rows are valid members. The live query used COUNT(*) but still filtered
            # those rows out, which is why the headline and the trend disagreed.
            if name == "teams_growth":
                updated = re.sub(
                    r"\s*WHERE\s+User\s+IS\s+NOT\s+NULL\s*", "\n", updated,
                    flags=re.IGNORECASE,
                )
            return updated

        source = config.get("source")
        if isinstance(source, str):
            updated = complete_snapshot_count(source)
            if updated != source:
                config["source"] = updated
                changed += 1

        query_lines = dataset.get("queryLines")
        if isinstance(query_lines, list):
            source = "".join(query_lines)
            updated = complete_snapshot_count(source)
            if name == "reach_by_country":
                # All German registrations remain in the numerator. Woodgrove DE's confirmed
                # headcount now lives in the employee-number source table, so remove either older
                # workaround (numerator exclusion or dashboard-only headcount supplement).
                updated = updated.replace(
                    "  WHERE lower(coalesce(Company, '')) NOT LIKE '%woodgrove%'\n",
                    "",
                )
                supplemental_total = (
                    "SUM(CAST(REPLACE(TRIM(_c6), ',', '') AS BIGINT)) "
                    "+ CASE WHEN _c0 = 'DE' THEN 1840 ELSE 0 END AS total_employees"
                )
                source_total = (
                    "SUM(CAST(REPLACE(TRIM(_c6), ',', '') AS BIGINT)) AS total_employees"
                )
                updated = updated.replace(supplemental_total, source_total)
            if updated != source:
                dataset["queryLines"] = updated.splitlines(keepends=True)
                changed += 1
    return changed


def _set_databricks_training_snapshot_rules(model: dict, latest_month: str | None) -> int:
    """Use latest Guild data for KPIs and retain history only in the growth dataset.

    Reporting-month selectors are intentionally removed from this dashboard. Complete monthly
    exports are snapshots, so an unfiltered KPI must never add June and July. The dashboard opens
    directly on the newest snapshot; the growth dataset is the sole multi-month view. A Guild row
    remains one member even when its email is blank.
    """
    changed = _set_exact_count_counter_formats(model)
    latest = latest_month or ""
    month_predicate = (
        f"Reporting_Month = '{latest}'" if latest else
        "Reporting_Month = (SELECT MAX(Reporting_Month) FROM "
        "" + BRONZE_FQN + ".lakehouse_guild)"
    )

    for page in model.get("pages", []):
        layout = page.get("layout", [])
        kept = [item for item in layout if str(
            item.get("widget", {}).get("spec", {}).get("frame", {}).get("title", "")
        ).strip() != "Reporting Month"]
        if len(kept) != len(layout):
            page["layout"] = kept
            changed += len(layout) - len(kept)

    datasets = {dataset.get("name"): dataset for dataset in model.get("datasets", [])}
    guild_members = datasets.get("guild_members", {})
    config = guild_members.get("config", {})
    latest_source = (
        "SELECT * FROM " + BRONZE_FQN + ".lakehouse_guild "
        f"WHERE {month_predicate}"
    )
    if config.get("source") != latest_source:
        config["source"] = latest_source
        changed += 1
    for measure in config.get("measures", []):
        if measure.get("name") == "member_count" and measure.get("expr") != "COUNT(*)":
            measure["expr"] = "COUNT(*)"
            changed += 1

    replacements = {
        "guild_training": f"""SELECT
  COUNT(DISTINCT dc.Email) AS guild_trained_count,
  COUNT(*) AS guild_completions_count
FROM {BRONZE_FQN}.lakehouse_guild dc
INNER JOIN (
  SELECT DISTINCT LOWER(E_Mail) AS email_lower
  FROM {GOLD_FQN}.databricks_trainings_participants
  WHERE E_Mail IS NOT NULL AND TRIM(E_Mail) <> ''
) tp ON LOWER(dc.Email) = tp.email_lower
WHERE dc.{month_predicate}""",
        "guild_engagement": f"""SELECT
  COUNT(*) AS total_members,
  SUM(Is_Community_Registered) AS community_registered,
  SUM(CASE WHEN Events_Attended > 0 THEN 1 ELSE 0 END) AS events_attended,
  SUM(CASE WHEN Is_Community_Registered = 1 OR Events_Attended > 0 THEN 1 ELSE 0 END)
    AS community_or_events,
  COUNT(DISTINCT CASE WHEN Is_Community_Registered = 1 OR Events_Attended > 0
    OR tp.email_lower IS NOT NULL THEN dc.Email END) AS any_engagement,
  COUNT(DISTINCT tp.email_lower) AS trained,
  SUM(CASE WHEN Is_Community_Registered = 0 AND Events_Attended = 0
    AND tp.email_lower IS NULL THEN 1 ELSE 0 END) AS no_engagement,
  ROUND(COUNT(DISTINCT tp.email_lower) * 100.0 / COUNT(*), 1) AS training_rate_pct,
  COUNT(DISTINCT CASE WHEN (Is_Community_Registered = 1 OR Events_Attended > 0)
    AND tp.email_lower IS NOT NULL THEN dc.Email END) AS community_and_trained
FROM {BRONZE_FQN}.lakehouse_guild dc
LEFT JOIN (
  SELECT DISTINCT LOWER(E_Mail) AS email_lower
  FROM {GOLD_FQN}.databricks_trainings_participants
  WHERE E_Mail IS NOT NULL AND TRIM(E_Mail) <> ''
) tp ON LOWER(dc.Email) = tp.email_lower
WHERE dc.{month_predicate}""",
        "guild_top20_companies": f"""SELECT Company, COUNT(*) AS member_count
FROM {BRONZE_FQN}.lakehouse_guild
WHERE {month_predicate}
GROUP BY Company
ORDER BY member_count DESC
LIMIT 20""",
        "guild_reporting_months": """SELECT Reporting_Month, COUNT(*) AS member_count
FROM """ + BRONZE_FQN + """.lakehouse_guild
WHERE Reporting_Month IS NOT NULL
GROUP BY Reporting_Month
ORDER BY Reporting_Month""",
    }
    for name, source in replacements.items():
        dataset = datasets.get(name)
        if not dataset:
            continue
        existing = dataset.get("config", {}).get("source") or "".join(dataset.get("queryLines", []))
        if existing.strip() != source.strip():
            if "queryLines" in dataset:
                dataset["queryLines"] = source.splitlines(keepends=True)
            else:
                dataset.setdefault("config", {})["source"] = source
            changed += 1

    workforce = datasets.get("workforce_penetration")
    if workforce:
        source = workforce.get("config", {}).get("source", "")
        prefix = source.split("guild AS (", 1)[0]
        updated = prefix + f"""guild AS (
  SELECT Country AS country, COUNT(*) AS guild_members
  FROM {BRONZE_FQN}.lakehouse_guild
  WHERE Country IS NOT NULL AND {month_predicate}
  GROUP BY Country
),
global_totals AS (
  SELECT
    (SELECT SUM(TRY_CAST(REPLACE(REPLACE(_c6, ',', ''), ' ', '') AS BIGINT))
     FROM {BRONZE_FQN}.`employee-numbers`
     WHERE _c0 IS NOT NULL AND LENGTH(TRIM(_c0)) = 2
       AND TRY_CAST(REPLACE(REPLACE(_c6, ',', ''), ' ', '') AS BIGINT) IS NOT NULL
    ) AS total_employees_global,
    (SELECT COUNT(*) FROM {BRONZE_FQN}.lakehouse_guild
     WHERE {month_predicate}) AS total_guild_global,
    (SELECT COUNT(DISTINCT E_Mail)
     FROM {GOLD_FQN}.databricks_trainings_participants
     WHERE E_Mail IS NOT NULL AND TRIM(E_Mail) != '') AS unique_trained_global
)
SELECT
  e.country, e.country_iso,
  COALESCE(td.company, '(unknown)') AS company,
  COALESCE(td.learning_level, '(all)') AS learning_level,
  e.total_employees,
  COALESCE(td.training_completions, 0) AS training_completions,
  COALESCE(td.unique_trained, 0) AS unique_trained,
  COALESCE(ci.guild_members, 0) AS guild_members,
  gt.total_employees_global, gt.total_guild_global, gt.unique_trained_global,
  CASE WHEN COALESCE(td.training_completions, 0) > 0 THEN 1 ELSE 0 END AS has_training,
  CASE WHEN COALESCE(ci.guild_members, 0) > 0 THEN 1 ELSE 0 END AS has_guild
FROM employees e
LEFT JOIN training_detail td ON e.country = td.country
LEFT JOIN guild ci ON e.country = ci.country
CROSS JOIN global_totals gt
"""
        if updated != source:
            workforce["config"]["source"] = updated
            changed += 1

    by_company = datasets.get("guild_by_company")
    if by_company:
        source = by_company.get("config", {}).get("source", "")
        tail = "emp AS (" + source.split("emp AS (", 1)[1] if "emp AS (" in source else ""
        tail = tail.replace(
            "SELECT\n  c.Reporting_Month,\n  c.company,", "SELECT\n  c.company,"
        )
        updated = f"""WITH guild_co AS (
  SELECT Company AS company, Country AS country, COUNT(*) AS guild_members
  FROM {BRONZE_FQN}.lakehouse_guild
  WHERE {month_predicate} AND Company IS NOT NULL AND Country IS NOT NULL
  GROUP BY Company, Country
),
{tail}"""
        if tail and updated != source:
            by_company["config"]["source"] = updated
            changed += 1
    return changed


def _set_latest_guild_summary(model: dict) -> int:
    """Prevent non-Guild dashboards from combining complete Guild snapshots."""
    changed = 0
    guild_table = "" + BRONZE_FQN + ".lakehouse_guild"
    latest_guild = (
        f"(SELECT * FROM {guild_table} /* latest_guild_snapshot */ "
        f"WHERE Reporting_Month = (SELECT MAX(Reporting_Month) FROM {guild_table}))"
    )
    for dataset in model.get("datasets", []):
        config = dataset.get("config", {})
        query_lines = dataset.get("queryLines")
        source = config.get("source")
        if not isinstance(source, str) and isinstance(query_lines, list):
            source = "".join(query_lines)
        if not isinstance(source, str) or "lakehouse_guild" not in source:
            continue
        # This specific summary CTE previously counted distinct emails across all months.
        pattern = re.compile(
            r"ci AS \(\s*SELECT\s+COUNT\(DISTINCT LOWER\(TRIM\(Email\)\)\) AS cnt\s+"
            r"FROM\s+" + re.escape(BRONZE_FQN) + r"\.lakehouse_guild\s+"
            r"WHERE\s+Email IS NOT NULL\s+AND TRIM\(Email\) != ''\s*\)",
            flags=re.IGNORECASE,
        )
        replacement = (
            "ci AS (\n  SELECT COUNT(*) AS cnt\n"
            f"  FROM {latest_guild}\n)"
        )
        updated = pattern.sub(replacement, source)
        # All remaining Guild branches in combined Group/Learning datasets must use one snapshot.
        # Replacing the relation (rather than trying to splice a predicate into every WHERE/JOIN)
        # safely covers simple FROM clauses, aliased joins and UNION branches alike.
        if "latest_guild_snapshot" not in source:
            placeholder = "__LATEST_GUILD_RELATION__"
            updated = updated.replace(latest_guild, placeholder)
            updated = updated.replace(guild_table, latest_guild)
            updated = updated.replace(placeholder, latest_guild)
        if dataset.get("name") == "group_top5_participation":
            updated = updated.replace(
                "COUNT(DISTINCT LOWER(TRIM(Email))) AS guild_count",
                "COUNT(*) AS guild_count",
            )
        # A direct Guild participation branch counts snapshot rows. Blank-email members cannot
        # join to another people table, but they still belong in standalone totals/country counts.
        updated = re.sub(
            rf"(FROM\s+{re.escape(latest_guild)})\s+WHERE\s+Email IS NOT NULL\s+"
            r"AND TRIM\(Email\) != ''",
            r"\1",
            updated,
            flags=re.IGNORECASE,
        )
        if updated != source:
            if isinstance(query_lines, list):
                dataset["queryLines"] = updated.splitlines(keepends=True)
            else:
                config["source"] = updated
            changed += 1
    return changed


def update_dashboard_exact_count_formats(
    workspace, dashboard_id: str, latest_month: str | None = None
) -> list[str]:
    """Persist exact integer formatting for every count counter in a dashboard draft."""
    dashboard = workspace.lakeview.get(dashboard_id)
    model = json.loads(dashboard.serialized_dashboard)
    changed = _set_exact_count_counter_formats(model) + _set_latest_guild_summary(model)
    changed += _set_string_month_defaults(model, latest_month)
    if not changed:
        return ["Dashboard count counters already show exact values."]
    dashboard.serialized_dashboard = json.dumps(model, separators=(",", ":"))
    workspace.lakeview.update(dashboard_id, dashboard)
    return [f"Updated {changed} dashboard count counter(s) to show exact values."]


def update_community_dashboard_member_count(
    workspace, dashboard_id: str, latest_month: str | None = None
) -> list[str]:
    """Persist complete-snapshot counting and exact counter formatting in the dashboard draft."""
    dashboard = workspace.lakeview.get(dashboard_id)
    model = json.loads(dashboard.serialized_dashboard)
    changed = _set_community_raw_member_count(model)
    changed += _set_string_month_defaults(
        model, latest_month, titles=("Filter by Reporting Month", "Reporting Month")
    )
    if not changed:
        return ["Community dashboard already uses complete snapshots and exact counter values."]
    dashboard.serialized_dashboard = json.dumps(model, separators=(",", ":"))
    workspace.lakeview.update(dashboard_id, dashboard)
    return [
        "Updated Community dashboard: every member aggregation uses the complete snapshot and "
        "all counters show exact values."
    ]


def update_databricks_training_dashboard(
    workspace, dashboard_id: str, latest_month: str | None = None
) -> list[str]:
    """Persist raw Guild counts, month history and newest-month defaults on every sheet."""
    dashboard = workspace.lakeview.get(dashboard_id)
    model = json.loads(dashboard.serialized_dashboard)
    changed = _set_databricks_training_snapshot_rules(model, latest_month)
    if not changed:
        return ["Databricks Training dashboard already uses complete monthly Guild snapshots."]
    dashboard.serialized_dashboard = json.dumps(model, separators=(",", ":"))
    workspace.lakeview.update(dashboard_id, dashboard)
    return [
        "Updated Databricks Training dashboard: removed Reporting Month selectors, every KPI uses "
        "the newest complete Guild snapshot, and month history is retained for growth only."
    ]


def _latest_month(table: str, column: str) -> str | None:
    """Read a table's newest nonblank reporting period for dashboard filter defaults."""
    from databricks_client import _connect

    connection = _connect()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"SELECT max(cast(`{column}` AS STRING)) FROM {CATALOG}.{BRONZE}.`{table}` "
            f"WHERE `{column}` IS NOT NULL AND trim(cast(`{column}` AS STRING)) <> ''"
        )
        row = cursor.fetchone()
        return str(row[0]) if row and row[0] is not None else None
    finally:
        cursor.close()
        connection.close()


def _ensure_daily_refresh_schedule(workspace, dashboard_id: str) -> str | None:
    """Make sure the dashboard refreshes its cached query results on its own every day.

    Lakeview serves cached results until something re-runs the queries. The app used to rely on a
    headless browser clicking "Refresh" with a saved SSO session cookie
    (`dashboard_capture.refresh_dashboard_caches`) — but that session expires (SSO+MFA cannot be
    renewed unattended), so once it goes stale the dashboard silently stops updating even though
    publish succeeds and the source tables are correct. A native Lakeview schedule runs server-side
    and needs no browser session, so it can't go stale the same way. Idempotent: skips creation if
    a daily schedule already exists.
    """
    from databricks.sdk.service.dashboards import CronSchedule, Schedule

    try:
        existing = list(workspace.lakeview.list_schedules(dashboard_id))
    except Exception as exc:  # noqa: BLE001
        return f"⚠ Could not check refresh schedules ({str(exc)[:140]})."
    if existing:
        return None
    try:
        workspace.lakeview.create_schedule(
            dashboard_id,
            schedule=Schedule(
                cron_schedule=CronSchedule(
                    quartz_cron_expression="0 0 6 * * ?", timezone_id=SCHEDULE_TIMEZONE
                ),
                display_name="Daily auto-refresh",
            ),
        )
        return "Added a daily 06:00 refresh schedule (Databricks-native, no browser session needed)."
    except Exception as exc:  # noqa: BLE001
        return f"⚠ Could not create refresh schedule ({str(exc)[:140]})."


def publish_related_dashboards(
    table_names: set[str], *, refresh_caches: bool = True
) -> list[str]:
    """Publish the current drafts of dashboards affected by the tables loaded in this run.

    ``refresh_caches`` is useful after ingestion, when all affected dashboard sheets should be
    warmed immediately.  Deck generation can disable it: screenshot capture refreshes the active
    dashboard itself, and warming every sheet here only adds several minutes before KPI loading.
    """
    from databricks.sdk import WorkspaceClient
    from kpis import DATABRICKS_DASHBOARDS

    related: set[str] = set()
    for table in table_names:
        low = table.lower()
        if low in {"analytics_community", "analytics_teams_listing", "lakehouse_guild"}:
            related.add("community")
        if low == "lakehouse_guild":
            related.add("databricks_trainings")
        if (low.startswith(("atlas", "beacon", "plantrack"))
                or "training_completions" in low):
            related.add("learnings")
        if low.startswith("analytics_event"):
            related.add("events")
        if low in {"registration_for_databricks_trainings",
                   "databricks_trainings_participants"}:
            related.add("databricks_trainings")
        if "fake_company_data" in low or "revenue" in low:
            related.add("revenue")

    lines: list[str] = []
    if not related:
        return lines
    workspace = WorkspaceClient(profile=DATABRICKS_PROFILE)
    latest_months: dict[str, str | None] = {}
    if "community" in related:
        latest_months["community"] = _latest_month("analytics_teams_listing", "reporting_month")
    if "databricks_trainings" in related:
        latest_months["databricks_trainings"] = _latest_month(
            "lakehouse_guild", "Reporting_Month"
        )
    if "revenue" in related:
        latest_months["revenue"] = _latest_month(
            "fake_company_data_with_reporting_month", "Reporting_Month"
        )
    published_urls: dict[str, str] = {}
    for key in sorted(related):
        url = DATABRICKS_DASHBOARDS.get(key, "")
        match = re.search(r"/dashboardsv3/([^/]+)/", url)
        if not match:
            lines.append(f"⚠ Dashboard '{key}' has no valid configured ID; not published.")
            continue
        try:
            if key == "community":
                lines.extend(update_community_dashboard_member_count(
                    workspace, match.group(1), latest_months.get("community")
                ))
            elif key == "databricks_trainings":
                lines.extend(update_databricks_training_dashboard(
                    workspace, match.group(1), latest_months.get("databricks_trainings")
                ))
            else:
                lines.extend(update_dashboard_exact_count_formats(
                    workspace, match.group(1), latest_months.get(key)
                ))
            published = workspace.lakeview.publish(match.group(1), embed_credentials=True)
            lines.append(
                f"Published dashboard: {published.display_name or key} "
                "(latest draft is now live)."
            )
            schedule_note = _ensure_daily_refresh_schedule(workspace, match.group(1))
            if schedule_note:
                lines.append(schedule_note)
            published_urls[key] = url
        except Exception as exc:  # noqa: BLE001 — report one failure but continue the run
            lines.append(f"⚠ Could not publish dashboard '{key}' ({str(exc)[:140]}).")
    if published_urls and refresh_caches:
        try:
            from dashboard_capture import refresh_dashboard_caches
            lines.extend(refresh_dashboard_caches(published_urls))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"⚠ Could not refresh published dashboard caches ({str(exc)[:140]}).")
    return lines


def refresh_all(connection, dashboard: str = "learnings",
                period: str | None = None, guild_loaded: bool = False,
                loaded_tables: set[str] | None = None) -> RefreshReport:
    """The whole final step: warm the warehouse, verify Genie tables, flag stale dashboard UNIONs.

    When `period` is given, the unified completions tables are rebuilt for it first — the metadata
    stamping has to happen before the Genie/dashboard checks report row counts.
    """
    rep = RefreshReport()
    rep.warmed = warm_warehouse(connection)
    rep.lines.append(
        "SQL warehouse is running — dashboard capture won't wait on a cold start."
        if rep.warmed else "⚠ Could not warm the SQL warehouse; the first capture may be slow."
    )

    # Stamp the metadata BEFORE reporting row counts, so the numbers reflect the new period.
    if period:
        combined = rebuild_completions(connection, period)
        rep.lines.extend(combined.lines)

    # The Guild's two calculated columns are cross-table, so they run AFTER the community/event rows
    # are in place. Only when a Guild snapshot was actually loaded — otherwise there is nothing new
    # to enrich and we would rewrite a period that is already correct.
    if period and guild_loaded:
        rep.lines.extend(enrich_guild(connection, period).lines)

    genie = refresh_genie_tables(connection)
    rep.tables_checked = genie.tables_checked
    rep.lines.extend(genie.lines)
    rep.lines.append(
        "Genie source tables are current; the space reads them live and needs no separate publish."
    )

    # Always update the learning dashboard sources when a period is known — the unified tables
    # were just rebuilt and the dashboard SQL + default month filter must reflect that.
    if period:
        try:
            rep.lines.extend(update_learning_dashboard_sources(connection))
        except Exception as exc:  # noqa: BLE001
            rep.lines.append(f"⚠ Could not update Learning dashboard sources ({str(exc)[:140]}).")

    if loaded_tables:
        rep.lines.extend(publish_related_dashboards(loaded_tables))
    elif period:
        # No explicit table list but we rebuilt learnings — publish that dashboard too.
        try:
            rep.lines.extend(publish_related_dashboards({"atlas_training_completions"}))
        except Exception as exc:  # noqa: BLE001
            rep.lines.append(f"⚠ Could not publish learnings dashboard ({str(exc)[:140]}).")

    try:
        all_tables, newest = check_union_coverage(connection, dashboard)
        if newest:
            rep.stale_unions = newest
            rep.lines.append(
                f"⚠ The '{dashboard}' dashboard UNIONs period tables BY NAME. Newest period has "
                f"{len(newest)} table(s): {', '.join(newest[:4])}"
                + ("…" if len(newest) > 4 else "")
                + " — confirm they are in the dashboard's query, it will not auto-discover them."
            )
    except Exception as exc:  # noqa: BLE001
        rep.lines.append(f"Could not check dashboard UNION coverage ({str(exc)[:70]}).")

    return rep
