"""Regression test for the Data Ingestion Guide's non-negotiable rules.

Run:  python -m pytest test_ingestion_rules.py   (or: python test_ingestion_rules.py)

The guide states that violating these rules corrupts the data, and four of them WERE being
violated silently (wrong domain map → everything Austria; German names normalised in bronze; no
Company derivation; email lookups on Atlas/Beacon). None of those raised an error — they just produced
wrong rows. So they get a test rather than a promise. Plus the worked examples from §9b, since those
are exact strings that land in table cells.

No Databricks connection needed: everything here is the pure cleaning/derivation layer.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp

import pandas as pd

import derivation as dv
import excel_ingestion as ei
import kpis as kp
import learning_metadata as lm
import pptx_refresher as pr
import refresh_targets as rt

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if not ok:
        FAILED.append(name)


def _xlsx(dirpath: Path, name: str, data: dict) -> Path:
    pd.DataFrame(data).to_excel(dirpath / name, index=False)
    return dirpath / name


def main() -> int:
    d = Path(mkdtemp(prefix="ingest_rules_"))

    learning = _xlsx(d, "Atlas_DataGovernance_Basics.xlsx",
                     {"Company": ["Fabrikam Logistics (CZ)"], "Status": ["Passed"], "Country": [None]})
    plan = ei.build_plan(learning, month_fallback="2026.07")

    # R1 — archive to the Volume first; the plan must name a volume path.
    check("R1 volume-first", bool(plan.volume_target) and "/Volumes/" in plan.volume_target,
          plan.volume_target[len(ei.VOLUME_ROOT):])

    # R2 — APPEND tags reporting_month = YYYY.MM
    unified = _xlsx(d, "beacon_training_completions_2026_07.xlsx",
                    {"Name": ["a"], "Course": ["x"]})
    up = ei.build_plan(unified)
    uframe = ei.load_full_frame(unified)
    check("R2 append+tag",
          up.route.mode is ei.WriteMode.APPEND
          and uframe["reporting_month"].iloc[0] == "2026.07",
          f"{up.route.mode.value}, reporting_month={uframe['reporting_month'].iloc[0]}")

    # R3 — FULL REPLACE only where the SOURCE FILE is itself cumulative.
    # The curated GOLD table is owned by a notebook, so the app archives the file and refuses the
    # table write (CUMULATIVE_MANUAL). `employee-numbers` is also full-replace — its sheet
    # accumulates every quarter — but the guide gives it no notebook and no derivation ("just check
    # the columns are present"), so the app may write it directly. It is therefore expected NOT to be
    # in the refuse list; anything else that is would be a rule-3 violation.
    replace = {t.table for _, t in ei.ROUTES if t.mode is ei.WriteMode.FULL_REPLACE}
    direct_write = replace - set(ei.CUMULATIVE_MANUAL)
    # Four tables are full-replace but NOT notebook-owned, so the app may write them itself:
    #   analytics_community                          — deduplicated cumulative Forms registrations
    #   employee-numbers                       — reference data, no notebook, no derivation
    #   registrations_for_teams_lakehouse_guild  — the Guild JOIN form; the guide does not mention
    #                                                this table at all, so no notebook owns it
    #   registration_for_databricks_trainings  — raw cumulative Forms responses; newest export
    #                                                safely replaces the older one
    check("R3 full-replace scope",
          direct_write == {"analytics_community", "employee-numbers",
                           "registration_for_databricks_trainings",
                           "registrations_for_teams_lakehouse_guild"},
          f"refused (notebook-owned): {sorted(replace & set(ei.CUMULATIVE_MANUAL))}; "
          f"written directly (no notebook, no derivation): {sorted(direct_write)}")
    check("R3 events_all is append-only",
          all(t.mode is ei.WriteMode.APPEND
              for _, t in ei.ROUTES if t.table == "analytics_events_all")
          and "analytics_events_all" not in ei.CUMULATIVE_MANUAL,
          "each new event's rows are appended; earlier events never rewritten")

    # R4 — learning tables: NEW TABLE per period with _YYYY_MM
    check("R4 new-table-per-period",
          plan.route.mode is ei.WriteMode.NEW_TABLE_PERIOD
          and plan.full_table.endswith("_2026_07"),
          plan.full_table.split(".")[-1])

    # R5 — the UNION hardcodes platform/course_name/language per source table
    sql = lm.build_union_sql(
        "Atlas", "2026.07", "cat", "brz",
        known=["atlas_datagovernance_basics_2026_07",
               "atlas_german_datagovernance_basics_2026_07"])
    check("R5 metadata stamped",
          "'Atlas' AS platform" in sql and "'German' AS language" in sql
          and "'Data Governance Basics' AS course_name" in sql,
          "platform/course_name/language hardcoded per source")
    mp = lm.build_union_sql("PlanTrack", "2026.07", "cat", "brz",
                            known=["plantrack_analytics_trainings_2026_07"])
    check("R5 PlanTrack course from column",
          mp is not None and "`Learning_Name_Module` AS course_name" in mp,
          "course_name read from the data, not the table name")

    # R6 — domains giving BOTH Country and Company, from config/org_profile.toml.
    examples = [("jan.novak@fabrikam-cz.example", "Czech Republic", "Fabrikam Logistics CZ"),
                ("anna.schmidt@cloud.contoso.example", "Germany", "Contoso Group (DE)"),
                ("peter@woodgrove-nl.example", "Netherlands", "Woodgrove Bank NL"),
                ("lena@tailspin-es.example", "Spain", "Tailspin Energy ES"),
                ("unknown@gmail.com", ei.DEFAULT_COUNTRY, "Unknown")]
    bad = [e for e, c, co in examples
           if ei.derive_country(None, e) != c
           or dv.company_from_domain(e, dv.UNKNOWN_COMPANY) != co]
    check("R6 domain→country+company",
          len(dv.DOMAIN_COUNTRY) >= 15 and not bad,
          f"{len(dv.DOMAIN_COUNTRY)} country / {len(dv.DOMAIN_COMPANY)} company domains; "
          f"examples: {'all match' if not bad else bad}")
    check("R6 consultant CONTAINS rule",
          ei.derive_country("Proseware GmbH", "x@personal.com") == "Germany"
          and ei.derive_country("Lucerne Consulting", "x@personal.com") == "Austria",
          "resolved by company name when the domain is unknown")

    # R7 — bracket code → Country; bracket KEPT in bronze, stripped only for display.
    lframe = ei.load_full_frame(learning, "2026.07")
    check("R7 bracket→country, kept in bronze",
          lframe["Country"].iloc[0] == "Czech Republic"
          and lframe["Company"].iloc[0] == "Fabrikam Logistics (CZ)"
          and ei.strip_company_bracket("Fabrikam Logistics (CZ)") == "Fabrikam Logistics",
          f"bronze Company='{lframe['Company'].iloc[0]}', display='Fabrikam Logistics'")

    # R8 — PlanTrack German country names stay German in BRONZE.
    plantrack = _xlsx(d, "PlanTrack_Analytics_Trainings.xlsx",
                       {"Company": ["Fabrikam Logistics (CZ)"], "Learning_Name_Module": ["Data Literacy"],
                        "Country": ["Tschechien"], "Status": ["Passed"]})
    mframe = ei.load_full_frame(plantrack, "2026.07")
    guide_de = {"Österreich": "Austria", "Deutschland": "Germany", "Schweiz": "Switzerland",
                "Niederlande": "Netherlands", "Tschechien": "Czechia",
                "Tschechische Republik": "Czechia", "Polen": "Poland", "Spanien": "Spain"}
    de_bad = [k for k, v in guide_de.items() if ei.normalize_german_country(k) != v]
    check("R8 German kept in bronze", mframe["Country"].iloc[0] == "Tschechien",
          f"bronze holds '{mframe['Country'].iloc[0]}' unchanged")
    check("R8 dashboard mapping complete", not de_bad,
          f"all {len(guide_de)} profile rows map correctly")

    # R9 — never an email-based derivation on Atlas/Beacon, even if an email-ish column exists.
    with_email = _xlsx(d, "Atlas_German_DataGovernance_Basics.xlsx",
                       {"Company": [None], "Status": ["Passed"], "Country": [None],
                        "Email": ["x@am.contoso.example"]})
    hframe = ei.load_full_frame(with_email, "2026.07")
    check("R9 no email lookup on learning tables",
          hframe["Country"].iloc[0] == ei.DEFAULT_COUNTRY,
          f"Email column ignored → {hframe['Country'].iloc[0]} (not Netherlands)")

    # R10 — alias-aware dedup within a period, honouring cloud > parent > subsidiary.
    aliases = pd.DataFrame({
        "Email": ["j@northwind-at.example", "j@contoso.example", "j@cloud.contoso.example",
                  "other@fabrikam-pl.example"],
        "reporting_month": ["2026.07"] * 4,
        "tag": ["subsidiary", "contoso", "cloud", "different person"]})
    deduped, removed = dv.dedup_within_period(aliases, "Email", "reporting_month")
    across = pd.DataFrame({"Email": ["j@cloud.contoso.example"] * 2,
                           "reporting_month": ["2026.06", "2026.07"]})
    _, removed_across = dv.dedup_within_period(across, "Email", "reporting_month")
    check("R10 alias dedup + priority",
          removed == 2 and deduped["tag"].iloc[0] == "cloud"
          and len(deduped) == 2 and removed_across == 0,
          f"3 aliases → kept '{deduped['tag'].iloc[0]}'; across periods removed {removed_across}")
    pairs = [("sam@cloud.contoso.example", "sam@contoso.example"),
             ("eva@corp.contoso.example", "eva@cloud.contoso.example"),
             ("max@cloud.contoso.example", "max@northwind-at.example"),
             ("kim@woodgrove-de.example", "kim@woodgrove-nl.example")]
    check("R10 all documented alias pairs",
          all(dv.alias_key(a) == dv.alias_key(b) for a, b in pairs),
          "cloud↔contoso, corp→cloud, cloud↔northwind-at (overlapping group), woodgrove-de↔woodgrove-nl")

    # Monthly learning exports are full snapshots. Dashboard totals must default to the newest
    # snapshot, never `All` (which would add June + July as if they were different learners).
    dashboard = {"pages": [{"layout": [
        {"widget": {"queries": [{"query": {"datasetName": "unified_learning_monthly"}}],
                    "spec": {"frame": {"title": "Reporting Month"}}}},
        {"widget": {"queries": [{"query": {"datasetName": "reporting_month_group"}}],
                    "spec": {"frame": {"title": "Reporting Month"}}}},
    ]}]}
    changed = rt._set_learning_month_defaults(dashboard, "2026-07")
    date_default = dashboard["pages"][0]["layout"][0]["widget"]["spec"]["selection"]
    string_default = dashboard["pages"][0]["layout"][1]["widget"]["spec"]["selection"]
    check("dashboard defaults to latest snapshot",
          changed == 2
          and dashboard["pages"][0]["layout"][0]["widget"]["spec"]["disallowAll"] is True
          and dashboard["pages"][0]["layout"][0]["widget"]["spec"]["widgetType"] ==
              "filter-single-select"
          and date_default["defaultSelection"]["values"] == {
              "dataType": "DATE", "values": [{"value": "2026-07-01"}]}
          and string_default["defaultSelection"]["values"] == {
              "dataType": "STRING", "values": [{"value": "2026-07"}]},
          "every Reporting Month filter defaults to July; historical months stay selectable")
    latest_only_source = (
        "WITH raw AS (SELECT DATE('2026-06-01') reporting_month)\nSELECT *\nFROM raw\n"
        "WHERE reporting_month = (SELECT max(reporting_month) FROM raw)"
    )
    history_source = rt._allow_learning_month_history(latest_only_source)
    check("Learning history remains selectable",
          history_source.endswith("FROM raw")
          and "WHERE reporting_month" not in history_source.split("SELECT *", 1)[1],
          "dataset exposes every month; single-select/no-All chooses exactly one")

    plantrack_carry = rt._plantrack_carry_forward(
        ["plantrack_analytics_trainings_2026_06"], ["2026-06", "2026-07"]
    )
    plantrack_rollover = rt._plantrack_carry_forward(
        ["plantrack_analytics_trainings_2026_06",
         "plantrack_analytics_trainings_2026_08"],
        ["2026-06", "2026-07", "2026-08", "2026-09"],
    )
    check("PlanTrack snapshot carries forward until its next update",
          plantrack_carry == [
              ("plantrack_analytics_trainings_2026_06", "2026-06"),
              ("plantrack_analytics_trainings_2026_06", "2026-07"),
          ]
          and plantrack_rollover[-2:] == [
              ("plantrack_analytics_trainings_2026_08", "2026-08"),
              ("plantrack_analytics_trainings_2026_08", "2026-09"),
          ],
          "June's rows remain in July; a future snapshot takes over without rewriting history")

    community_dashboard = {
        "pages": [{"layout": [{"widget": {
            "queries": [{"query": {"datasetName": "community_members"}}],
            "spec": {"frame": {"title": "Total TEAMS Members"},
                     "widgetType": "counter", "encodings": {"value": {}}},
        }}]}],
        "datasets": [
            {"name": "community_members", "config": {"measures": [
                {"name": "total_members", "expr": "COUNT(DISTINCT User)"},
            ]}},
            {"name": "teams_growth", "config": {
                "source": ("SELECT reporting_month, COUNT(DISTINCT User) FROM t "
                           "WHERE User IS NOT NULL GROUP BY 1"),
            }},
            {"name": "teams_by_country", "queryLines": [
                "SELECT Country, COUNT(DISTINCT User) AS members FROM t GROUP BY 1"
            ]},
        ],
    }
    raw_changes = rt._set_community_raw_member_count(community_dashboard)
    check("Community KPI uses raw snapshot rows",
          raw_changes == 4
          and community_dashboard["datasets"][0]["config"]["measures"][0]["expr"] ==
              "COUNT(*)"
          and "COUNT(*)" in community_dashboard["datasets"][1]["config"]["source"]
          and "WHERE User IS NOT NULL" not in
              community_dashboard["datasets"][1]["config"]["source"]
          and community_dashboard["pages"][0]["layout"][0]["widget"]["spec"]
              ["encodings"]["value"]["format"]["abbreviation"] == "none"
          and "COUNT(*)" in "".join(community_dashboard["datasets"][2]["queryLines"]),
          "headline and growth count blank-email rows; counters show exact integers")

    guild_dashboard = {
        "pages": [{"layout": [{"widget": {"spec": {
            "frame": {"title": "Reporting Month"}, "widgetType": "filter-single-select"
        }}}]}],
        "datasets": [{"name": "guild_members", "config": {"measures": [
            {"name": "member_count", "expr": "COUNT(DISTINCT Email)"},
        ]}}],
    }
    guild_changes = rt._set_databricks_training_snapshot_rules(
        guild_dashboard, "2026.07"
    )
    check("Guild latest-only KPIs and blank-email members rule",
          guild_changes == 3
          and guild_dashboard["datasets"][0]["config"]["measures"][0]["expr"] ==
              "COUNT(*)"
          and not guild_dashboard["pages"][0]["layout"]
          and "WHERE Reporting_Month = '2026.07'" in
              guild_dashboard["datasets"][0]["config"]["source"],
          "every July row counts as a member; filters cannot combine snapshots; growth keeps history")

    guild_company_dashboard = {"datasets": [{
        "name": "guild_by_company",
        "config": {"source": """WITH guild_co AS (
  SELECT Reporting_Month, company, country, COUNT(*) AS guild_members FROM old
),
emp AS (
  SELECT country, SUM(total_employees) AS total_employees FROM employees GROUP BY country
)
SELECT
  c.Reporting_Month,
  c.company,
  c.country,
  c.guild_members,
  e.total_employees
FROM guild_co c
LEFT JOIN emp e ON c.country = e.country"""},
    }]}
    rt._set_databricks_training_snapshot_rules(guild_company_dashboard, "2026.07")
    guild_company_sql = guild_company_dashboard["datasets"][0]["config"]["source"]
    check("Guild company chart has resolvable latest-month SQL",
          "c.Reporting_Month" not in guild_company_sql
          and "WHERE Reporting_Month = '2026.07'" in guild_company_sql
          and "SELECT\n  c.company," in guild_company_sql,
          "latest month is filtered inside guild_co; outer query uses only projected columns")

    check("Presentation month labels roll forward together",
          pr.build_month_replacements("2026.06", "2026.07") == {
              "2026.06": "2026.07", "2026-06": "2026-07", "June 2026": "July 2026"},
          "cover, source labels and chart captions use the live report month")

    mixed_counters = {"pages": [{"layout": [
        {"widget": {"spec": {"widgetType": "counter", "frame": {"title": "Total Learners"},
                              "encodings": {"value": {"format": {
                                  "type": "number-plain", "abbreviation": "compact"}}}}}},
        {"widget": {"spec": {"widgetType": "counter", "frame": {"title": "Completion Rate %"},
                              "encodings": {"value": {"format": {
                                  "type": "number-plain", "abbreviation": "none",
                                  "decimalPlaces": {"type": "exact", "places": 1}}}}}}},
    ]}]}
    exact_changes = rt._set_exact_count_counter_formats(mixed_counters)
    check("All dashboard count KPIs use exact values",
          exact_changes == 1
          and mixed_counters["pages"][0]["layout"][0]["widget"]["spec"]
              ["encodings"]["value"]["format"]["abbreviation"] == "none"
          and mixed_counters["pages"][0]["layout"][1]["widget"]["spec"]
              ["encodings"]["value"]["format"]["decimalPlaces"]["places"] == 1,
          "counts are full integers; percentages retain their decimal formatting")

    live_presentation_keys = {
        "source_month", "report_month_label", "academy_total_learners", "academy_countries",
        "academy_companies", "academy_courses", "academy_languages", "learning_enrolled",
        "learning_in_progress", "community_total_registrations", "community_teams_members",
        "db_participants", "db_guild_members", "events_count", "events_total_participants",
        "events_unique_participants",
    }
    scalar_by_key = {item["key"]: item for item in kp.SCALARS}
    check("Presentation core KPIs are live, not demo fallbacks",
          all(scalar_by_key[key].get("sql") for key in live_presentation_keys),
          f"{len(live_presentation_keys)} core presentation tags have Databricks SQL")

    # R11 — table naming: lowercase, underscores, documented prefixes.
    check("R11 naming",
          all(s == s.lower() and " " not in s for s in lm.REGISTRY)
          and all(s.startswith(("atlas_", "beacon_elearning_", "plantrack_"))
                  for s in lm.REGISTRY),
          f"all {len(lm.REGISTRY)} registry stems lowercase with correct prefixes")
    live = ["atlas_data_basics_and_awareness_part_1_2026_06",
            "atlas_datagovernance_basics_2026_06",
            "atlas_german_datagovernance_basics_2026_06",
            "beacon_elearning_data_basic_awareness_part1_2026_06",
            "beacon_elearning_data_governance_basics_part2_2026_06",
            "beacon_elearning_german_data_basic_awareness_part2_2026_06",
            "plantrack_analytics_trainings_2026_06"]
    unmapped = [t for t in live if lm.derive_metadata(t) is None
                or lm.derive_metadata(t).guessed]
    check("R11 live tables all registered", not unmapped,
          f"{len(live)} live table names resolve from the registry (no guesses)")

    # R11b — the REAL raw LMS filenames. Both platforms export as `<id>_export_eLearning…`, so the
    # numeric id prefix is the only thing separating them: matching on the word "eLearning" sent the
    # Atlas file into the Beacon table. An unknown id must stay unrouted rather than guess.
    lms = [
        ("100200_export_eLearningDataBasicsAwarenessPart1_20260803_0838.xlsx",
         "beacon_elearning_data_basic_awareness_part1", "2026.08"),
        ("100200_export_eLearningDataBasicsAwarenessTeil2_20260803_0838.xlsx",
         "beacon_elearning_german_data_basic_awareness_part2", "2026.08"),
        ("300400_export_eLearningDataBasicsAwarenessTeil2_20260720_1039.xlsx",
         "atlas_german_data_basics_and_awareness_part_2", "2026.07"),
        ("300400_export_eLearningDataGovernanceBasics_20260720_1039.xlsx",
         "atlas_datagovernance_basics", "2026.07"),
        ("99999999_export_eLearningDataBasicsAwarenessPart1_20260803.xlsx", None, "2026.08"),
    ]
    wrong = [(f, lm.table_stem_for_filename(f), want) for f, want, _ in lms
             if lm.table_stem_for_filename(f) != want]
    check("R11b raw LMS names route by id prefix", not wrong,
          "100200→Beacon, 300400→Atlas, unknown id→unrouted" if not wrong else str(wrong))
    # An LMS filename's YYYYMMDD is the EXPORT TIMESTAMP, not the reporting period — you run the export
    # in August for the July period. So it must NOT be treated as the period, or the user's picked month
    # is silently overridden (a July upload landed in August). It is still readable for display.
    leaks = [(f, ei.infer_reporting_month(f)) for f, _, _ in lms
             if ei.infer_reporting_month(f) is not None]
    check("R2b export timestamp does NOT set the period", not leaks,
          "the picked month wins over an export-date filename" if not leaks else str(leaks))
    check("R2b export timestamp still readable",
          ei.export_timestamp_month(
              "100200_export_eLearningDataBasicsAwarenessPart1_20260803_0838.xlsx") == "2026.08",
          "shown as context so the date is not silently ignored")
    check("R2b a REAL period in the name still wins",
          ei.infer_reporting_month("Atlas_training_completions_2026_06.xlsx") == "2026.06"
          and ei.infer_reporting_month("Databricks_Guild_2026.07.xlsx") == "2026.07",
          "`_2026_06` is a period, not a timestamp")
    check("R2c reversed MM.YYYY resolves",
          ei.infer_reporting_month("Sales_Admin_12.2025.xlsx") == "2025.12",
          "the employee-numbers sheet is named MM.YYYY, not YYYY.MM")

    # Registrations dedup on (person, LEVEL) — a repeat at a NEW level is real progression.
    reg = pd.DataFrame({
        "ID": [1, 2, 3, 4],
        "Completion time": ["2026-01-01", "2026-03-01", "2026-04-01", "2026-06-01"],
        "Email": ["anonymous"] * 4,
        "Your work email": ["a@contoso.example", "a@contoso.example", "b@contoso.example", "b@contoso.example"],
        "Have you ever participated in a Contoso Databricks Level before? If yes, which level?":
            ["Yes, Learn (Level 2)", "Yes, Learn (Level 2)", "No", "Yes, Apply (Level 3)"],
        "What Contoso company are you from?": ["Contoso (AT)"] * 4,
    })
    deduped_reg, reg_notes = ei._dedup_registrations(reg)
    kept_a = deduped_reg[deduped_reg["Your work email"] == "a@contoso.example"]
    check("registrations dedup on (person, level)",
          len(deduped_reg) == 3 and len(kept_a) == 1
          and str(kept_a["Completion time"].iloc[0]) == "2026-03-01",
          "same level twice → collapsed (newest kept); a new level → kept as progression")

    training_reg = _xlsx(d, "Registration for Databricks Trainings(1-4).xlsx", reg)
    training_reg_plan = ei.build_plan(training_reg)
    training_reg_frame = ei.load_full_frame(training_reg)
    check("training registrations preserve every response",
          training_reg_plan.route.table == "registration_for_databricks_trainings"
          and training_reg_plan.route.table not in ei.CUMULATIVE_MANUAL
          and len(training_reg_frame) == 4,
          f"route={training_reg_plan.route.table}; rows={len(training_reg_frame)}")

    community_form = pd.DataFrame({
        "ID": [1], "Start time": ["2026-08-01"], "Completion time": ["2026-08-01"],
        "Email": ["anonymous"], "Email2": ["real.person@contoso.example"],
        "First name": ["Real"], "Last name": ["Person"], "Company": ["Contoso"],
        "Department": ["Data"], "Job title": ["Analyst"],
        "What contents and formats are you especially interested in?": ["Training"],
        "Country": ["Austria"],
    })
    community_live_columns = [
        "Id", "Startzeit", "Fertigstellungszeit", "First_name", "Last_name", "Email",
        "Company", "Department", "Job_title",
        "What_contents_and_formats_are_you_especially_interested_in", "Country",
    ]
    aligned_community, _ = ei.align_to_table_schema(
        community_form, "analytics_community", community_live_columns
    )
    check("Community direct write preserves the real Forms email",
          list(aligned_community.columns) == community_live_columns
          and aligned_community.loc[0, "Email"] == "real.person@contoso.example"
          and aligned_community.loc[0, "Startzeit"] == "2026-08-01",
          "Email2 wins over the anonymous Email placeholder; English timestamps map to live schema")

    # Routing regression: a file named for the Guild JOIN form also contains "guild". The generic Guild
    # snapshot rule used to come first, which made the join-form alternative unreachable.
    join = ei.route_for("Registration for Lakehouse Guild(1-40).xlsx")
    snap = ei.route_for("Lakehouse Guild TEAMS listing 2026.07.xlsx")
    plain = ei.route_for("Lakehouse_Guild_2026.07.xlsx")
    check("routing: Guild join form is not shadowed by the snapshot rule",
          join is not None and join.table == "registrations_for_teams_lakehouse_guild"
          and snap is not None and snap.table == "lakehouse_guild"
          and plain is not None and plain.table == "lakehouse_guild",
          f"join form → {join.table if join else None}; listing → {snap.table if snap else None}")

    # R12 — the APP cleans (no manual Genie step): Country/Company filled + month tagged pre-write.
    teams = _xlsx(d, "Analytics_Community_2026_07.xlsx",
                  {"User": ["a@fabrikam-cz.example"], "Name": ["A"], "Company": [None], "Country": [None]})
    tplan = ei.build_plan(teams)
    check("R12 app cleans before write",
          tplan.reporting_month == "2026.07" and bool(tplan.warnings),
          f"month={tplan.reporting_month}, cleaning reported {len(tplan.warnings)} action(s)")

    class _NoLookup:
        def cursor(self):
            raise AssertionError("Community listing must not query email recovery sources")

    untouched, filled, removed = ei.enrich_run_listing_emails(
        [pd.DataFrame({"User": [None], "Name": ["Person Without Email"]})],
        [(teams, tplan)], _NoLookup(),
    )
    check("R12a blank Community emails stay blank and counted",
          len(untouched[0]) == 1 and pd.isna(untouched[0].loc[0, "User"])
          and not filled and not removed,
          "no lookup, no removal: the blank-email row remains one member")

    # Both exports contain "TEAMS listing", so Guild has to win by routing precedence.  Otherwise
    # its ~200 members are unioned into the Analytics Community count (the live 1.03K regression).
    guild_listing = _xlsx(d, "Lakehouse Guild TEAMS listing 2026.07.xlsx", {
        "DisplayName": ["Ada Lovelace"], "Email": ["ada@contoso.example"], "Roles": ["member"],
        "Company": [None], "Country": [None],
    })
    cplan = ei.build_plan(guild_listing)
    cframe = ei.load_full_frame(guild_listing)
    check("R12b Guild TEAMS listing stays separate",
          cplan.route.table == "lakehouse_guild"
          and cplan.route.cleaning == "guild"
          and list(cframe.columns) == ["Email", "First_name", "Last_name", "Role", "Company",
                                      "Country", "Reporting_Month"]
          and cframe.loc[0, "First_name"] == "Ada"
          and cframe.loc[0, "Last_name"] == "Lovelace",
          f"route={cplan.route.table}; columns={list(cframe.columns)}")
    recovery_sources = {s[0] for s in ei._LISTING_EMAIL_SOURCES}
    # The implementation also consults prior Teams/Guild snapshots; this source-set assertion is
    # intentionally lightweight because the full function needs a Databricks connection.
    check("R12c Guild missing-email recovery uses membership history",
          {"analytics_community", "registration_for_databricks_trainings",
           "registrations_for_teams_lakehouse_guild", "analytics_teams_listing",
           "lakehouse_guild"}.issubset(recovery_sources),
          f"lookup tables={sorted(recovery_sources)}")

    # Cross-cutting: no circular imports. This bit once — excel_ingestion imported derivation at
    # module level while derivation imports excel_ingestion, so `import derivation` alone crashed.
    import subprocess
    import sys
    mods = ["derivation", "excel_ingestion", "learning_metadata", "content_routing",
            "refresh_targets", "change_summary", "run_manifest"]
    broken = [m for m in mods
              if subprocess.run([sys.executable, "-c", f"import {m}"],
                                capture_output=True, cwd=Path(__file__).parent.parent).returncode]
    check("no circular imports", not broken,
          f"all {len(mods)} modules import standalone" if not broken else f"broken: {broken}")

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILURE(S): {', '.join(FAILED)}")
        return 1
    print("ALL CHECKS PASS")
    return 0


def _collect_results() -> list[tuple[str, bool, str]]:
    """Run every check once (at collection time) so pytest can report each rule individually."""
    results: list[tuple[str, bool, str]] = []
    global check
    original = check

    def recording_check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))

    check = recording_check
    try:
        main()
    finally:
        check = original
    return results


try:  # pytest is optional: the file still runs as a plain script without it.
    import pytest

    _RESULTS = _collect_results()

    @pytest.mark.parametrize("name,ok,detail", _RESULTS, ids=[r[0] for r in _RESULTS])
    def test_rule(name: str, ok: bool, detail: str) -> None:
        assert ok, f"{name}: {detail}"
except ImportError:  # pragma: no cover
    pass


if __name__ == "__main__":
    raise SystemExit(main())
