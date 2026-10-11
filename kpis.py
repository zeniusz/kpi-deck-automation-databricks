"""Monthly Analytics KPI and chart configuration.

Use this file as the single place where you connect PowerPoint items to
Databricks SQL. The PowerPoint template uses shape names such as:

    kpi:community_total_registrations
    chart:community_geographic_footprint

Each scalar query should return one row and one value.
Each chart query should return rows with two columns: label, value.
"""

from settings import BRONZE_FQN, DASHBOARD_IDS, GOLD_FQN, dashboard_url

REPORT_MONTH = "2026.07"
REPORT_TITLE = "Analytics Business Enablement KPI Overview"

# The template's source-freshness labels (e.g. "Current data until 2026.06") are written for
# the PREVIOUS month. TEMPLATE_MONTH is the month string as it appears in the template today;
# the app rolls every occurrence of it forward to the current source month on refresh.
TEMPLATE_MONTH = "2026.06"

DATABRICKS_DASHBOARDS = {key: dashboard_url(key) for key in DASHBOARD_IDS}

_BRONZE = BRONZE_FQN
_GOLD = GOLD_FQN
# PlanTrack is supplied as an occasional snapshot rather than a monthly feed. Keep the currently
# approved June snapshot in the latest reporting month until the user supplies a replacement. This
# is read-only: it does not update or delete the Databricks source table.
_PLANTRACK_CURRENT_TABLE = f"{_BRONZE}.plantrack_analytics_trainings_2026_06"
_LATEST_LEARNING_MONTH = (
    "(SELECT max(replace(reporting_month,'.','-')) FROM ("
    f"SELECT reporting_month FROM {_BRONZE}.atlas_training_completions UNION ALL "
    f"SELECT reporting_month FROM {_BRONZE}.beacon_training_completions))"
)
_LEARNING_ALL = (
    "(SELECT platform,course_name,language,Company,Status,Country,reporting_month "
    f"FROM {_BRONZE}.atlas_training_completions UNION ALL "
    "SELECT platform,course_name,language,Company,Status,Country,reporting_month "
    f"FROM {_BRONZE}.beacon_training_completions UNION ALL "
    "SELECT 'PlanTrack' AS platform, CASE "
    "WHEN Learning_Name_Module LIKE '%Basics & Awareness Part 1%' "
    "OR Learning_Name_Module LIKE '%Basics & Awareness Teil 1%' "
    "THEN 'Data Basics & Awareness Pt. 1' "
    "WHEN Learning_Name_Module LIKE '%Basics & Awareness Part 2%' "
    "OR Learning_Name_Module LIKE '%Basics & Awareness Teil 2%' "
    "THEN 'Data Basics & Awareness Pt. 2' "
    "WHEN Learning_Name_Module LIKE '%Governance Basics%' "
    "THEN 'Data Governance Basics Pt. 1' ELSE Learning_Name_Module END AS course_name, "
    "CASE WHEN Learning_Name_Module LIKE 'DE -%' THEN 'German' ELSE 'English' END AS language, "
    "CASE WHEN Company='Northwind Retail AT' THEN 'NWAT' ELSE Company END AS Company, "
    "Status, CASE WHEN Country='Holland' THEN 'Netherlands' "
    "ELSE Country END AS Country, "
    f"{_LATEST_LEARNING_MONTH} AS reporting_month FROM {_PLANTRACK_CURRENT_TABLE})"
)
_LEARNING = (
    f"(SELECT * FROM {_LEARNING_ALL} learning_history "
    "WHERE replace(reporting_month,'.','-')="
    f"(SELECT max(replace(reporting_month,'.','-')) FROM {_LEARNING_ALL}))"
)
_LATEST_TEAMS = (
    f"{_BRONZE}.analytics_teams_listing WHERE reporting_month="
    f"(SELECT max(reporting_month) FROM {_BRONZE}.analytics_teams_listing)"
)
_LATEST_GUILD = (
    f"{_BRONZE}.lakehouse_guild WHERE Reporting_Month="
    f"(SELECT max(Reporting_Month) FROM {_BRONZE}.lakehouse_guild)"
)
_HEADCOUNT = (
    f"(SELECT sum(try_cast(replace(_c6,',','') AS DOUBLE)) n FROM {_BRONZE}.`employee-numbers` "
    "WHERE _c0 rlike '^[A-Z]{2}$')"
)

_COUNTRY_PARTICIPATION = f"""(
WITH learner_counts AS (
  SELECT CASE
    WHEN Country IN ('Tschechien','Tschechische Republik','Czech Republic') THEN 'Czechia'
    WHEN Country='Österreich' THEN 'Austria' WHEN Country='Deutschland' THEN 'Germany'
    WHEN Country='Ungarn' THEN 'Hungary' WHEN Country='Rumänien' THEN 'Romania'
    WHEN Country='Kroatien' THEN 'Croatia' WHEN Country='Polen' THEN 'Poland'
    WHEN Country='Serbien' THEN 'Serbia' WHEN Country='Bulgarien' THEN 'Bulgaria'
    WHEN Country='Albanien' THEN 'Netherlands' WHEN Country='Estland' THEN 'Portugal'
    WHEN Country='Lettland' THEN 'Poland' WHEN Country='Litauen' THEN 'Lithuania'
    WHEN Country='Türkei' THEN 'Spain' ELSE Country END AS country,
    count(*) AS learners
  FROM {_LEARNING}
  WHERE Country IS NOT NULL AND trim(Country)<>''
  GROUP BY 1
), country_hc AS (
  SELECT CASE trim(_c0)
    WHEN 'AL' THEN 'Netherlands' WHEN 'AT' THEN 'Austria' WHEN 'BA' THEN 'Netherlands'
    WHEN 'BG' THEN 'Bulgaria' WHEN 'CZ' THEN 'Czechia' WHEN 'DE' THEN 'Germany'
    WHEN 'EE' THEN 'Portugal' WHEN 'GE' THEN 'Georgia' WHEN 'HR' THEN 'Croatia'
    WHEN 'HU' THEN 'Hungary' WHEN 'LI' THEN 'Switzerland' WHEN 'LT' THEN 'Lithuania'
    WHEN 'LV' THEN 'Poland' WHEN 'MK' THEN 'North Macedonia' WHEN 'PL' THEN 'Poland'
    WHEN 'RO' THEN 'Romania' WHEN 'RS' THEN 'Serbia' WHEN 'SK' THEN 'Italy'
    WHEN 'TR' THEN 'Spain' WHEN 'UA' THEN 'Ukraine' ELSE trim(_c0) END AS country,
    sum(try_cast(replace(_c6,',','') AS DOUBLE)) AS headcount
  FROM {_BRONZE}.`employee-numbers`
  WHERE trim(coalesce(_c0,'')) NOT IN ('','Country')
    AND trim(coalesce(_c1,'')) NOT IN ('','Involved Entity')
  GROUP BY 1
)
SELECT l.country, l.learners, h.headcount,
       round(100.0*l.learners/nullif(h.headcount,0),2) AS participation_rate
FROM learner_counts l JOIN country_hc h ON l.country=h.country
)"""

# Live Community interest classification. This mirrors the Community dashboard's semantic dataset
# so the presentation donut, labels and narrative all use the same current registrations rather
# than the old fixed 35.15% demo text.
_INTEREST_ROWS = f"""(
WITH exploded AS (
  SELECT Id, trim(item) AS interest_item
  FROM {_BRONZE}.analytics_community
  LATERAL VIEW explode(split(lower(coalesce(
    `What_contents_and_formats_are_you_especially_interested_in`, '')), ';')) e AS item
), classified AS (
  SELECT Id,
    CASE
      WHEN interest_item LIKE '%data quality%' OR interest_item LIKE '%data mesh%'
        OR interest_item LIKE '%data governance%' OR interest_item LIKE '%data architecture%'
        OR interest_item LIKE '%data catalog%' OR interest_item LIKE '%data engineering%'
        OR interest_item LIKE '%data platform%' OR interest_item LIKE '%data lakehouse%'
        OR interest_item LIKE '%data management%' OR interest_item LIKE '%data strategy%'
        OR interest_item LIKE '%data product%' OR interest_item LIKE '%data fabric%'
        OR interest_item LIKE '%data contract%' OR interest_item LIKE '%data pipeline%'
        OR interest_item LIKE '%data lake%' OR interest_item LIKE '%data warehouse%'
        OR interest_item LIKE '%data integration%' OR interest_item LIKE '%etl%'
        OR interest_item LIKE '%elt%' OR interest_item LIKE '%data lineage%'
        OR interest_item LIKE '%databricks%' OR interest_item LIKE '%spark%'
        OR interest_item LIKE '%delta lake%' THEN 'Data Engineering & Platform'
      WHEN interest_item LIKE '%machine learning%' OR interest_item LIKE '%deep learning%'
        OR interest_item LIKE '%neural network%' OR interest_item LIKE '%mlops%'
        OR interest_item LIKE '%model%' OR interest_item LIKE '%prediction%'
        OR interest_item LIKE '%ml%' THEN 'Machine Learning & MLOps'
      WHEN interest_item LIKE '%generative ai%' OR interest_item LIKE '%gen ai%'
        OR interest_item LIKE '%large language%' OR interest_item LIKE '%llm%'
        OR interest_item LIKE '%chatgpt%' OR interest_item LIKE '%gpt%'
        OR interest_item LIKE '%copilot%' OR interest_item LIKE '%prompt%'
        OR interest_item LIKE '%rag%' OR interest_item LIKE '%agent%'
        OR interest_item LIKE '%openai%' THEN 'Generative AI & LLMs'
      WHEN interest_item LIKE '%analytics%' OR interest_item LIKE '%visualization%'
        OR interest_item LIKE '%dashboard%' OR interest_item LIKE '%reporting%'
        OR interest_item LIKE '%bi%' OR interest_item LIKE '%business intelligence%'
        OR interest_item LIKE '%power bi%' OR interest_item LIKE '%tableau%'
        THEN 'Analytics & Visualization'
      WHEN interest_item LIKE '%cloud%' OR interest_item LIKE '%azure%'
        OR interest_item LIKE '%aws%' OR interest_item LIKE '%gcp%'
        OR interest_item LIKE '%devops%' OR interest_item LIKE '%infrastructure%'
        OR interest_item LIKE '%kubernetes%' OR interest_item LIKE '%docker%'
        THEN 'Cloud & Infrastructure'
      WHEN interest_item LIKE '%use case%' OR interest_item LIKE '%logistics%'
        OR interest_item LIKE '%forecast%' OR interest_item LIKE '%fraud%'
        OR interest_item LIKE '%pricing%' OR interest_item LIKE '%customer%'
        OR interest_item LIKE '%supply chain%' OR interest_item LIKE '%energy%'
        THEN 'Industry Use Cases'
      WHEN interest_item LIKE '%python%' OR interest_item LIKE '%sql%'
        OR interest_item LIKE '%scala%' OR interest_item LIKE '%r programming%'
        OR interest_item LIKE '%r language%' OR interest_item LIKE '%pyspark%'
        OR interest_item LIKE '%pandas%' OR interest_item LIKE '%numpy%'
        OR interest_item LIKE '%jupyter%' OR interest_item LIKE '%notebook%'
        OR interest_item LIKE '%api%' OR interest_item LIKE '%programming%'
        OR interest_item LIKE '%coding%' OR interest_item LIKE '%software development%'
        OR interest_item LIKE '%application development%' OR interest_item LIKE '%app development%'
        OR interest_item LIKE '%web development%' THEN 'Python, SQL & Coding'
      WHEN interest_item LIKE '%governance%' OR interest_item LIKE '%ethics%'
        OR interest_item LIKE '%compliance%' OR interest_item LIKE '%privacy%'
        OR interest_item LIKE '%security%' OR interest_item LIKE '%risk%'
        OR interest_item LIKE '%regulation%' THEN 'Governance & Ethics'
      ELSE NULL
    END AS interest_category
  FROM exploded WHERE interest_item <> ''
)
SELECT Id, interest_category FROM classified WHERE interest_category IS NOT NULL
)"""
_INTEREST_COUNTS = (
    f"(SELECT interest_category, count(DISTINCT Id) respondents FROM {_INTEREST_ROWS} GROUP BY 1)"
)

# ── Dashboard tile capture map ───────────────────────────────────────────────────────────────────
# When "Dashboard screenshots" mode is on, the app opens the published dashboard with a saved SSO
# session, clicks the right tab, screenshots that tile and inserts the PNG into the tagged picture
# placeholder. Only keys listed here are captured; every other chart key is rendered locally or left
# untouched in the deck.
#
# Each entry:
#   "dashboard":  key into DATABRICKS_DASHBOARDS (which published dashboard to open)
#   "tab":        the tab to click before capturing
#   "widget_id":  the tile's widget id from the dashboard model (most reliable lookup)
#   "title":      the tile title as shown on the dashboard (fallback lookup)
#   optional:     "crop"/"fractional_crop" fallbacks, "capture_viewport", "native_export": False for
#                 map tiles with no PNG export, "full_tile", "drop_title", "settle_ms", "trim"
#
# Run `python -m dashboard_capture --list` to list the capturable tiles of your own dashboards.
# The entries below are EXAMPLES for the fictional demo dashboards — replace them with your own.
DASHBOARD_TILES = {
    # Single-page dashboard captured by widget id (no tabs). Bar charts use Databricks' native
    # "Download PNG" export automatically.
    "revenue_by_country": {"dashboard": "revenue", "widget_id": "bar_revenue_country",
                            "full_tile": True, "settle_ms": 5000, "drop_title": True,
                            "capture_viewport": {"width": 1000, "height": 1050},
                            "capture_svg": True},
    # Map tile (Mapbox): no native PNG export → screenshot, with the internal header cropped away.
    "revenue_by_country_map": {"dashboard": "revenue", "widget_id": "geo_map", "full_tile": True,
                                "settle_ms": 8000, "native_export": False, "drop_title": True,
                                "capture_viewport": {"width": 900, "height": 1000}},

    # Multi-tab dashboards: "tab" selects the page first.
    "academy_learners_by_country": {"dashboard": "learnings", "tab": "Learners Summary",
                                    "widget_id": "by_country", "title": "Learners by Country",
                                    "capture_viewport": {"width": 900, "height": 1000}},
    "academy_learners_by_language": {"dashboard": "learnings", "tab": "Learners Summary",
                                     "widget_id": "by_language", "title": "Learners by Language"},
    "community_top_entities": {"dashboard": "community", "tab": "Community Overview",
                               "widget_id": "company_distribution", "title": "Top Companies"},
    "community_geographic_footprint": {"dashboard": "community", "tab": "Community Overview",
                                       "widget_id": "geographic_map", "title": "Geographic Footprint",
                                       "native_export": False},
    "events_participants_per_event": {"dashboard": "events", "tab": "Events Analytics",
                                      "widget_id": "bar_participants", "title": "Participants per Event"},
}

# These two dashboard tiles are very wide (the legend dominates), so a proportional PowerPoint
# insert makes their guild almost unreadably small. Render them locally from the same live SQL as
# clean, large donuts; dashboard screenshots remain the source for all other mapped charts.
LOCAL_RENDER_CHART_KEYS = {
    "ai_interest_distribution",
    "db_training_completions_by_company",
}

SCALARS = [
    # Cover / source freshness
    {"key": "report_month_label", "title": "Report month", "format": "text", "demo_value": "July 2026 Data",
     "sql": f"SELECT concat(date_format(to_date(concat(max(replace(reporting_month,'.','-')),'-01')),'MMMM yyyy'),' Data') FROM {_LEARNING}"},
    {"key": "source_month", "title": "Current data until", "format": "text", "demo_value": "2026.06",
     "sql": f"SELECT replace(max(reporting_month),'-','.') FROM {_LEARNING}"},

    # Revenue deck — LIVE from the demo revenue table
    # (the table the revenue dashboard reads). All computed for the LATEST reporting month present,
    # so after appending September the cards reflect September automatically. demo_value is the
    # fallback used if a query returns nothing.
    {"key": "revenue_month_label", "title": "Revenue report month", "format": "text", "demo_value": "August 2026 Data",
     "sql": "SELECT concat(date_format(to_date(concat(replace(max(Reporting_Month),'.','-'),'-01')),'MMMM yyyy'),' Data') "
            "FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month"},
    {"key": "revenue_biggest_country", "title": "Biggest Revenue Country", "format": "text", "demo_value": "Germany",
     "sql": "SELECT Country FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month "
            "WHERE Reporting_Month=(SELECT max(Reporting_Month) FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month) "
            "GROUP BY Country ORDER BY SUM(`Annual Revenue (EUR)`) DESC LIMIT 1"},
    {"key": "revenue_avg", "title": "Average Revenue", "format": "text", "demo_value": "502M",
     "sql": "SELECT CASE WHEN a>=1e9 THEN concat(round(a/1e9,1),'B') WHEN a>=1e6 THEN concat(round(a/1e6,0),'M') ELSE cast(round(a) as string) END "
            "FROM (SELECT AVG(`Annual Revenue (EUR)`) a FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month "
            "WHERE Reporting_Month=(SELECT max(Reporting_Month) FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month))"},
    {"key": "revenue_total", "title": "Total Revenue", "format": "text", "demo_value": "55.2B",
     "sql": "SELECT CASE WHEN s>=1e9 THEN concat(round(s/1e9,1),'B') WHEN s>=1e6 THEN concat(round(s/1e6,0),'M') ELSE cast(round(s) as string) END "
            "FROM (SELECT SUM(`Annual Revenue (EUR)`) s FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month "
            "WHERE Reporting_Month=(SELECT max(Reporting_Month) FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month))"},
    {"key": "revenue_insight", "title": "Revenue insight", "format": "text", "demo_value": "Germany has the biggest revenue",
     "sql": "SELECT concat(Country,' has the biggest revenue') FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month "
            "WHERE Reporting_Month=(SELECT max(Reporting_Month) FROM " + BRONZE_FQN + ".fake_company_data_with_reporting_month) "
            "GROUP BY Country ORDER BY SUM(`Annual Revenue (EUR)`) DESC LIMIT 1"},

    # Analytics Academy
    {"key": "academy_total_learners", "title": "Total Learners", "format": "int", "demo_value": 360,
     "sql": f"SELECT count(*) FROM {_LEARNING}"},
    {"key": "academy_countries", "title": "Countries", "format": "int", "demo_value": 16,
     "sql": f"SELECT count(DISTINCT Country) FROM {_LEARNING} WHERE Country IS NOT NULL AND trim(Country)<>''"},
    {"key": "academy_companies", "title": "Companies", "format": "int", "demo_value": 50,
     "sql": f"SELECT count(DISTINCT Company) FROM {_LEARNING} WHERE Company IS NOT NULL AND trim(Company)<>''"},
    {"key": "academy_courses", "title": "Courses Numbers", "format": "int", "demo_value": 5,
     "sql": f"SELECT count(DISTINCT course_name) FROM {_LEARNING}"},
    {"key": "academy_languages", "title": "Training Languages", "format": "int", "demo_value": 2,
     "sql": f"SELECT count(DISTINCT language) FROM {_LEARNING}"},
    {"key": "academy_top_countries", "title": "Top learner countries", "format": "text",
     "demo_value": "Austria, Spain, Poland, Czechia, Switzerland",
     "sql": f"SELECT concat_ws(', ', collect_list(label)) FROM (SELECT Country label FROM {_LEARNING} WHERE Country IS NOT NULL AND trim(Country)<>'' GROUP BY Country ORDER BY count(*) DESC LIMIT 5)"},
    {"key": "academy_reach_summary", "title": "Academy reach summary", "format": "text",
     "demo_value": "English training leads; learners span 10 countries across the group.",
     "sql": f"SELECT concat((SELECT language FROM {_LEARNING} GROUP BY language ORDER BY count(*) DESC LIMIT 1), ' training leads; learners span ', (SELECT count(DISTINCT Country) FROM {_LEARNING} WHERE Country IS NOT NULL AND trim(Country)<>''), ' countries across the group.')"},
    {"key": "academy_participation_top_countries", "title": "Top participation countries",
     "format": "text", "demo_value": "Switzerland, Austria, Portugal, Netherlands, Poland",
     "sql": f"SELECT concat_ws(', ', collect_list(country)) FROM (SELECT country FROM {_COUNTRY_PARTICIPATION} ORDER BY participation_rate DESC LIMIT 5)"},
    {"key": "academy_participation_summary", "title": "Academy participation summary",
     "format": "text",
     "demo_value": "Strongest participation is in Switzerland, with English as the primary training language.",
     "sql": f"SELECT concat('Strongest participation is in ', (SELECT country FROM {_COUNTRY_PARTICIPATION} ORDER BY participation_rate DESC LIMIT 1), ', with ', (SELECT language FROM {_LEARNING} GROUP BY language ORDER BY count(*) DESC LIMIT 1), ' as the primary training language.')"},
    {"key": "group_employees", "title": "Total Group Employees", "format": "compact", "demo_value": 40674,
     "sql": f"SELECT n FROM {_HEADCOUNT}"},
    {"key": "academy_participation_rate", "title": "Overall Participation Rate", "format": "percent", "demo_value": 1.68,
     "sql": f"SELECT round(100.0*(SELECT count(*) FROM {_LEARNING})/n,2) FROM {_HEADCOUNT}"},
    {"key": "learning_enrolled", "title": "Enrolled", "format": "int", "demo_value": 12,
     "sql": f"SELECT count(*) FROM {_LEARNING} WHERE lower(trim(Status))='enrolled'"},
    {"key": "learning_in_progress", "title": "In Progress", "format": "int", "demo_value": 147,
     "sql": f"SELECT count(*) FROM {_LEARNING} WHERE lower(trim(Status))='in progress'"},

    # Databricks trainings and guild
    {"key": "db_participants", "title": "Databricks participants", "format": "int", "demo_value": 120,
     "sql": f"SELECT count(*) FROM {_GOLD}.databricks_trainings_participants"},
    {"key": "db_companies", "title": "Databricks companies", "format": "int", "demo_value": 32,
     "sql": f"SELECT count(DISTINCT Company) FROM {_GOLD}.databricks_trainings_participants"},
    {"key": "db_countries", "title": "Databricks countries", "format": "int", "demo_value": 12,
     "sql": f"SELECT count(DISTINCT Country) FROM {_GOLD}.databricks_trainings_participants"},
    {"key": "db_learn_participants", "title": "Learn participants", "format": "int", "demo_value": 106,
     "sql": f"SELECT count(*) FROM {_GOLD}.databricks_trainings_participants WHERE Level='2 - Learn'"},
    {"key": "db_learn_countries", "title": "Learn countries", "format": "int", "demo_value": 10,
     "sql": f"SELECT count(DISTINCT Country) FROM {_GOLD}.databricks_trainings_participants WHERE Level='2 - Learn'"},
    {"key": "db_learn_companies", "title": "Learn companies", "format": "int", "demo_value": 26,
     "sql": f"SELECT count(DISTINCT Company) FROM {_GOLD}.databricks_trainings_participants WHERE Level='2 - Learn'"},
    {"key": "db_ignite_participants", "title": "Ignite participants", "format": "int", "demo_value": 52,
     "sql": f"SELECT count(*) FROM {_GOLD}.databricks_trainings_participants WHERE Level='1 - Ignite'"},
    {"key": "db_ignite_countries", "title": "Ignite countries", "format": "int", "demo_value": 12,
     "sql": f"SELECT count(DISTINCT Country) FROM {_GOLD}.databricks_trainings_participants WHERE Level='1 - Ignite'"},
    {"key": "db_ignite_companies", "title": "Ignite companies", "format": "int", "demo_value": 20,
     "sql": f"SELECT count(DISTINCT Company) FROM {_GOLD}.databricks_trainings_participants WHERE Level='1 - Ignite'"},
    {"key": "db_guild_members", "title": "Lakehouse Guild Members", "format": "int", "demo_value": 170,
     "sql": f"SELECT count(*) FROM {_LATEST_GUILD}"},
    {"key": "db_training_coverage", "title": "Training Coverage", "format": "percent", "demo_value": 0.35,
     "sql": f"SELECT round(100.0*(SELECT count(*) FROM {_GOLD}.databricks_trainings_participants)/n,2) FROM {_HEADCOUNT}"},
    {"key": "db_guild_coverage", "title": "Guild Members Coverage", "format": "percent", "demo_value": 0.68,
     "sql": f"SELECT round(100.0*(SELECT count(*) FROM {_LATEST_GUILD})/n,2) FROM {_HEADCOUNT}"},

    # Analytics Community
    {"key": "community_total_registrations", "title": "Total registrations", "format": "int", "demo_value": 1087,
     "sql": f"SELECT count(*) FROM {_BRONZE}.analytics_community"},
    {"key": "community_companies_represented", "title": "Companies Represented", "format": "int", "demo_value": 58, "sql": None},
    {"key": "community_countries_represented", "title": "Countries Represented", "format": "int", "demo_value": 15,
     "sql": f"SELECT count(DISTINCT Country) FROM {_BRONZE}.analytics_community WHERE Country IS NOT NULL AND trim(Country)<>''"},
    {"key": "community_teams_members", "title": "TEAMS members", "format": "int", "demo_value": 903,
     "sql": f"SELECT count(*) FROM {_LATEST_TEAMS}"},
    {"key": "community_countries_reg_teams", "title": "Countries registrations / Teams", "format": "text", "demo_value": "27 / 28",
     "sql": f"SELECT concat((SELECT count(DISTINCT Country) FROM {_BRONZE}.analytics_community WHERE Country IS NOT NULL AND trim(Country)<>''),' / ',(SELECT count(DISTINCT Country) FROM {_LATEST_TEAMS} AND Country IS NOT NULL AND trim(Country)<>''))"},
    {"key": "community_companies_reg_teams", "title": "Companies registrations / Teams", "format": "text", "demo_value": "45 / 44", "sql": None},
    {"key": "community_top_registration_entity", "title": "Top registration reach entity", "format": "text", "demo_value": "Contoso Holding",
     "dashboard": "community", "dashboard_dataset": "reach_by_company", "dashboard_expression": "CASE WHEN company LIKE '% (AT)' THEN left(company,length(company)-5) ELSE company END", "dashboard_order": "registration_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_registration_entity_rate", "title": "Top registration reach %", "format": "text", "demo_value": "63.59% of country HC",
     "dashboard": "community", "dashboard_dataset": "reach_by_company", "dashboard_expression": "concat(cast(registration_reach_pct AS STRING),'% of country HC')", "dashboard_order": "registration_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_teams_entity", "title": "Top Teams reach entity", "format": "text", "demo_value": "Contoso Holding",
     "dashboard": "community", "dashboard_dataset": "reach_by_company", "dashboard_expression": "CASE WHEN company LIKE '% (AT)' THEN left(company,length(company)-5) ELSE company END", "dashboard_order": "teams_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_teams_entity_rate", "title": "Top Teams reach %", "format": "text", "demo_value": "63.1% of country HC",
     "dashboard": "community", "dashboard_dataset": "reach_by_company", "dashboard_expression": "concat(cast(teams_reach_pct AS STRING),'% of country HC')", "dashboard_order": "teams_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_registration_country", "title": "Top registration country", "format": "text", "demo_value": "Germany",
     "dashboard": "community", "dashboard_dataset": "reach_by_country", "dashboard_expression": "country", "dashboard_order": "registration_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_registration_country_rate", "title": "Top registration country %", "format": "text", "demo_value": "11.65% of country HC",
     "dashboard": "community", "dashboard_dataset": "reach_by_country", "dashboard_expression": "concat(cast(registration_reach_pct AS STRING),'% of country HC')", "dashboard_order": "registration_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_teams_country", "title": "Top Teams country", "format": "text", "demo_value": "Germany",
     "dashboard": "community", "dashboard_dataset": "reach_by_country", "dashboard_expression": "country", "dashboard_order": "teams_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_top_teams_country_rate", "title": "Top Teams country %", "format": "text", "demo_value": "7.40% of country HC",
     "dashboard": "community", "dashboard_dataset": "reach_by_country", "dashboard_expression": "concat(cast(teams_reach_pct AS STRING),'% of country HC')", "dashboard_order": "teams_reach_pct DESC", "dashboard_limit": 1},
    {"key": "community_country_reach_remark", "title": "Country reach data-quality remark",
     "format": "text",
     "demo_value": "Germany combined reach uses 1,890 Woodgrove DE and 225 Contoso employees.",
     "sql": ("SELECT concat('Germany combined registration reach is ', "
             "round(100.0 * (SELECT count(*) FROM "
             f"{_BRONZE}.analytics_community WHERE lower(trim(Country)) IN ('germany','deutschland')) "
             "/ (SELECT sum(try_cast(replace(_c6,',','') AS DOUBLE)) FROM "
             f"{_BRONZE}.`employee-numbers` WHERE upper(trim(_c0))='DE'), 2), "
             "'%: ', (SELECT count(*) FROM "
             f"{_BRONZE}.analytics_community WHERE lower(trim(Country)) IN ('germany','deutschland')), "
             "' registrations across ', (SELECT cast(sum(try_cast(replace(_c6,',','') AS DOUBLE)) AS BIGINT) FROM "
             f"{_BRONZE}.`employee-numbers` WHERE upper(trim(_c0))='DE'), "
             "' employees (1,840 Woodgrove DE + 212 Contoso). ' ,"
             "'Woodgrove DE: ', (SELECT count(*) FROM "
             f"{_BRONZE}.analytics_community WHERE lower(trim(Country)) IN ('germany','deutschland') "
             "AND lower(Company) LIKE '%woodgrove%'), '/', (SELECT _c6 FROM "
             f"{_BRONZE}.`employee-numbers` WHERE upper(trim(_c1))='WOODGROVE' LIMIT 1), "
             "' = ', round(100.0 * (SELECT count(*) FROM "
             f"{_BRONZE}.analytics_community WHERE lower(trim(Country)) IN ('germany','deutschland') "
             "AND lower(Company) LIKE '%woodgrove%') / (SELECT try_cast(replace(_c6,',','') AS DOUBLE) FROM "
             f"{_BRONZE}.`employee-numbers` WHERE upper(trim(_c1))='WOODGROVE' LIMIT 1), 2), '%.')")},

    # Community interest slide — all text is generated from the same live category counts as the
    # donut. The four label cards and both narrative boxes therefore roll forward with the data.
    {"key": "community_interest_insight", "title": "Leading Community interest", "format": "text",
     "demo_value": "23.0% Industry Use Cases — 93 respondents",
     "sql": f"SELECT concat(cast(round(100.0*respondents/sum(respondents) over(),1) AS STRING),"
            f"'% ',interest_category,' — ',respondents,' respondents') FROM {_INTEREST_COUNTS} "
            "ORDER BY respondents DESC, interest_category LIMIT 1"},
    *[
        {"key": f"community_interest_top{rank}", "title": f"Community interest #{rank}",
         "format": "text", "demo_value": fallback,
         "sql": (f"SELECT interest_category FROM (SELECT interest_category, respondents, "
                 f"row_number() over(ORDER BY respondents DESC, interest_category) rn "
                 f"FROM {_INTEREST_COUNTS}) WHERE rn={rank}")}
        for rank, fallback in enumerate(
            ["Industry Use Cases", "Generative AI & LLMs", "Analytics & Visualization",
             "Data Engineering & Platform"], 1
        )
    ],
    {"key": "community_interest_narrative", "title": "Community interest narrative",
     "format": "text",
     "demo_value": "Industry Use Cases leads, followed by Generative AI & LLMs.",
     "sql": (f"WITH ranked AS (SELECT interest_category,respondents,row_number() over(ORDER BY "
             f"respondents DESC,interest_category) rn FROM {_INTEREST_COUNTS}) SELECT concat("
             "max(CASE WHEN rn=1 THEN interest_category END),' leads with ',"
             "max(CASE WHEN rn=1 THEN respondents END),' respondents, followed by ',"
             "max(CASE WHEN rn=2 THEN interest_category END),' (',"
             "max(CASE WHEN rn=2 THEN respondents END),').') FROM ranked")},
    {"key": "community_interest_action", "title": "Community interest action", "format": "text",
     "demo_value": "Prioritize Industry Use Cases and Generative AI & LLMs in the next content cycle.",
     "sql": (f"WITH ranked AS (SELECT interest_category,row_number() over(ORDER BY respondents DESC,"
             f"interest_category) rn FROM {_INTEREST_COUNTS}) SELECT concat('Prioritize ',"
             "max(CASE WHEN rn=1 THEN interest_category END),' and ',"
             "max(CASE WHEN rn=2 THEN interest_category END),"
             "' in the next community content cycle.') FROM ranked")},

    # Events
    {"key": "events_count", "title": "Events", "format": "int", "demo_value": 6,
     "sql": f"SELECT count(DISTINCT event_number) FROM {_BRONZE}.analytics_events_all"},
    {"key": "events_total_participants", "title": "Total participants", "format": "compact", "demo_value": 1300,
     "sql": f"SELECT count(*) FROM {_BRONZE}.analytics_events_all"},
    {"key": "events_unique_participants", "title": "Unique participants", "format": "int", "demo_value": 438,
     "sql": f"SELECT count(DISTINCT coalesce(nullif(lower(trim(email)),''),concat('__name__',lower(trim(name))))) FROM {_BRONZE}.analytics_events_all"},
    {"key": "events_speakers", "title": "Speakers", "format": "int", "demo_value": 16, "sql": None},
    {"key": "events_country_leader", "title": "Country leader", "format": "text", "demo_value": "Germany", "sql": None},
    {"key": "events_country_leader_rate", "title": "Country leader rate", "format": "text", "demo_value": "11.11% participation", "sql": None},
    {"key": "events_company_leader", "title": "Company leader", "format": "text", "demo_value": "Contoso Holding", "sql": None},
    {"key": "events_company_leader_rate", "title": "Company leader rate", "format": "text", "demo_value": "46.62% participation", "sql": None},
    {"key": "events_overall_rate", "title": "Overall Rate", "format": "percent", "demo_value": 1.42,
     "sql": f"SELECT round(100.0*(SELECT count(DISTINCT coalesce(nullif(lower(trim(email)),''),concat('__name__',lower(trim(name))))) FROM {_BRONZE}.analytics_events_all)/n,2) FROM {_HEADCOUNT}"},

    # Cross-program
    {"key": "cross_involved_countries", "title": "Involved Countries", "format": "int", "demo_value": 15,
     "dashboard_dataset": "group_all_programs", "dashboard_expression": "count(DISTINCT country)"},
    {"key": "cross_participation_rate", "title": "Cross Participation Rate", "format": "percent", "demo_value": 51.0,
     "dashboard_dataset": "cross_rate_all_programs", "dashboard_expression": "max(cross_participation_rate)"},
    {"key": "cross_involved_companies", "title": "Involved Companies", "format": "int", "demo_value": 59,
     "dashboard_dataset": "group_all_programs", "dashboard_expression": "count(DISTINCT company)"},
    {"key": "cross_group_headcount_rate", "title": "Overall % of Group Headcount", "format": "percent", "demo_value": 5.9,
     "dashboard_dataset": "kpi_overall_hc_pct", "dashboard_expression": "max(overall_hc_pct)"},
    {"key": "dual_companies", "title": "Companies with Dual Participation", "format": "int", "demo_value": 14,
     "dashboard_dataset": "cross_participation", "dashboard_expression": "max(cross_companies_count)"},
    {"key": "dual_participation_rate", "title": "Dual Participation Rate", "format": "percent", "demo_value": 52.3,
     "dashboard_dataset": "cross_participation", "dashboard_expression": "max(cross_pct_of_literacy)"},
    {"key": "dual_countries", "title": "Countries with Dual Participation", "format": "int", "demo_value": 12,
     "dashboard_dataset": "cross_participation", "dashboard_expression": "count(DISTINCT country)", "dashboard_where": "dimension_type='company'"},
    {"key": "dual_group_headcount_rate", "title": "Overall % of Group Headcount", "format": "percent", "demo_value": 1.3,
     "dashboard_dataset": "cross_participation", "dashboard_expression": "round(100.0*sum(total_combined)/nullif(sum(total_hc),0),1)", "dashboard_where": "dimension_type='country'"},
]

CHARTS = [
    {"key": "academy_learners_by_country", "title": "Top Learners By Country", "kind": "barh",
     "sql": f"SELECT coalesce(nullif(trim(Country),''),'Unknown') label,count(*) value FROM {_LEARNING} GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
     "demo_data": [("Austria", 128), ("Spain", 59), ("Poland", 44), ("Czechia", 34), ("Switzerland", 25)]},
    {"key": "academy_learners_by_entity", "title": "Top Learners By Entity", "kind": "barh",
     "sql": f"SELECT coalesce(nullif(trim(Company),''),'Unknown') label,count(*) value FROM {_LEARNING} GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
     "demo_data": [("Contoso Holding", 76), ("Northwind", 65), ("Fabrikam", 48), ("Tailspin", 36), ("Woodgrove", 30)]},
    {"key": "academy_learners_by_language", "title": "Learners By Language", "kind": "donut",
     "sql": f"SELECT language label,count(*) value FROM {_LEARNING} GROUP BY 1 ORDER BY 2 DESC",
     "demo_data": [("English", 571), ("German", 104)]},
    {"key": "academy_participation_by_country", "title": "Participation of Total Headcount by Country", "kind": "barh", "sql": None,
     "demo_data": [("Switzerland", 7.9), ("Austria", 4.6), ("Portugal", 3.7), ("Netherlands", 3.2), ("Poland", 2.6)]},
    {"key": "academy_participation_by_company", "title": "Participation of Total Headcount by Company", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 75.3), ("Fabrikam", 13.5), ("Tailspin", 12.4), ("Northwind", 10.3), ("Woodgrove", 7.2)]},
    {"key": "academy_participation_by_language", "title": "Participation of Language", "kind": "donut",
     "sql": f"SELECT language label,round(100.0*count(*)/sum(count(*)) over(),2) value FROM {_LEARNING} GROUP BY 1 ORDER BY 2 DESC",
     "demo_data": [("English", 105), ("German", 23)]},
    {"key": "learning_by_country", "title": "Learning By Country", "kind": "barh",
     "sql": f"SELECT coalesce(nullif(trim(Country),''),'Unknown') label,count(*) value FROM {_LEARNING} GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
     "demo_data": [("Austria", 67), ("Poland", 41), ("Spain", 38), ("Czechia", 30), ("Portugal", 22)]},
    {"key": "learning_by_module", "title": "Learning By Module", "kind": "bar",
     "sql": f"SELECT course_name label,count(*) value FROM {_LEARNING} GROUP BY 1 ORDER BY 2 DESC",
     "demo_data": [("Basics", 163), ("Awareness", 120), ("Advanced", 43), ("Governance", 26)]},

    {"key": "db_training_completions_by_company", "title": "Completions By Company", "kind": "donut",
     "donut_legend": True, "figsize": (7.2, 4.8), "show_title": False,
     "sql": f"WITH counts AS (SELECT coalesce(nullif(trim(Company),''),'Unknown') Company,count(*) value FROM {_GOLD}.databricks_trainings_participants GROUP BY 1), ranked AS (SELECT *,row_number() over(ORDER BY value DESC,Company) rn FROM counts) SELECT CASE WHEN rn<=6 THEN Company ELSE 'Other companies' END label,sum(value) value FROM ranked GROUP BY 1 ORDER BY 2 DESC",
     "demo_data": [("Contoso Holding", 23), ("Fabrikam", 15), ("Tailspin", 12), ("Northwind", 11), ("Woodgrove", 9)]},
    {"key": "db_training_mapping", "title": "Mapping of Databricks Training Completions", "kind": "barh",
     "sql": f"SELECT Level label,count(*) value FROM {_GOLD}.databricks_trainings_participants GROUP BY 1 ORDER BY 1",
     "demo_data": [("Learn", 124), ("Ignite", 91)]},
    {"key": "db_training_participation_country", "title": "Training Participation % by Country", "kind": "barh", "sql": None,
     "demo_data": [("Germany", 1.5), ("Austria", 1.2), ("Poland", 1.0), ("Spain", 0.7), ("Czechia", 0.6)]},
    {"key": "db_guild_participation_country", "title": "Guild Participation % by Country", "kind": "barh", "sql": None,
     "demo_data": [("Germany", 2.7), ("Austria", 2.4), ("Poland", 2.0), ("Spain", 1.3), ("Czechia", 1.1)]},
    {"key": "db_training_participation_company", "title": "Training Participation % by Company", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 17.7), ("Fabrikam", 6.0), ("Tailspin", 4.8), ("Northwind", 4.1), ("Woodgrove", 3.2)]},
    {"key": "db_guild_participation_company", "title": "Guild Participation % by Company", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 9.9), ("Fabrikam", 3.3), ("Tailspin", 2.6), ("Northwind", 2.1), ("Woodgrove", 1.6)]},

    {"key": "community_top_entities", "title": "Top Entities In Community", "kind": "barh",
     "sql": f"SELECT Company label,count(*) value FROM {_LATEST_TEAMS} AND Company IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
     "demo_data": [("Contoso Holding", 274), ("Northwind", 118), ("Fabrikam", 90), ("Tailspin", 73), ("Woodgrove", 59)]},
    {"key": "community_geographic_footprint", "title": "Community Geographic Footprint", "kind": "barh",
     "sql": f"SELECT Country label,count(*) value FROM {_LATEST_TEAMS} AND Country IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10",
     "demo_data": [("Austria", 322), ("Germany", 78), ("Poland", 70), ("Spain", 63), ("Czechia", 54)]},
    {"key": "community_registrations_reach_company", "title": "Top Registrations Reach % by Company", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 77.21), ("Fabrikam", 17.4), ("Tailspin", 13.4), ("Northwind", 11.0), ("Woodgrove", 7.6)]},
    {"key": "community_teams_reach_company", "title": "Top TEAMS Members Reach % by Company", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 81.4), ("Fabrikam", 17.9), ("Tailspin", 13.9), ("Northwind", 11.1), ("Woodgrove", 8.1)]},
    {"key": "community_registrations_reach_country", "title": "Top Registrations Reach % by Country", "kind": "barh", "sql": None,
     "demo_data": [("Germany", 9.87), ("Austria", 8.5), ("Poland", 7.8), ("Portugal", 5.9), ("Netherlands", 5.6)]},
    {"key": "community_teams_reach_country", "title": "Top TEAMS Members Reach % by Country", "kind": "barh", "sql": None,
     "demo_data": [("Germany", 10.25), ("Austria", 9.6), ("Poland", 8.6), ("Portugal", 6.7), ("Netherlands", 5.8)]},
    {"key": "ai_interest_distribution", "title": "Interest Distribution", "kind": "donut",
     "donut_legend": True, "figsize": (7.6, 4.4), "show_title": False,
     "sql": f"SELECT interest_category label,respondents value FROM {_INTEREST_COUNTS} ORDER BY 2 DESC,1",
     "demo_data": [("General AI", 21.09), ("Business use cases", 14.0), ("Automation", 11.2), ("Analytics", 7.9), ("GenAI", 5.79)]},
    {"key": "top_interest_categories", "title": "Top Interest Categories", "kind": "barh",
     "sql": f"SELECT interest_category label,respondents value FROM {_INTEREST_COUNTS} ORDER BY 2 DESC,1",
     "demo_data": [("Industry Use Cases", 60), ("Generative AI & LLMs", 55), ("Analytics & Visualization", 53), ("Data Engineering & Platform", 37), ("Machine Learning & MLOps", 30)]},
    {"key": "events_participants_per_event", "title": "Participants per event", "kind": "bar",
     "sql": f"SELECT coalesce(event_name,concat('Event ',event_number)) label,count(*) value FROM {_BRONZE}.analytics_events_all GROUP BY 1 ORDER BY min(event_number)",
     "demo_data": [("Event 1", 326), ("Event 2", 391), ("Event 3", 352), ("Event 4", 260)]},
    {"key": "events_topics", "title": "Community Events Topics", "kind": "barh", "sql": None,
     "demo_data": [("Strategy", 6), ("Governance", 6), ("Academy", 4), ("Platform", 4), ("Use cases", 7)]},
    {"key": "events_participation_country", "title": "Participation % of Total Headcount by Country", "kind": "barh", "sql": None,
     "demo_data": [("Germany", 8.52), ("Austria", 6.5), ("Poland", 6.1), ("Portugal", 5.1), ("Netherlands", 4.4)]},
    {"key": "events_participation_company", "title": "Top Companies by Participation %", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 54.87), ("Fabrikam", 15.1), ("Tailspin", 12.7), ("Northwind", 9.4), ("Woodgrove", 7.6)]},

    # Slide 8 Workforce Reach (renamed from learning_by_country / learning_by_module)
    {"key": "learning_reach_by_country", "title": "Participation % by Country (Map)", "kind": "barh",
     "sql": None,
     "demo_data": [("Austria", 5.2), ("Switzerland", 8.9), ("Portugal", 4.2), ("Poland", 3.0), ("Netherlands", 3.6)]},
    {"key": "learning_reach_by_module", "title": "Participation % by Course", "kind": "barh",
     "sql": None,
     "demo_data": [("English", 83.1), ("German", 18.3)]},
    # Slide 10 Guild Members by Country (renamed from db_training_mapping)
    {"key": "db_guild_members_country", "title": "Guild Members by Country", "kind": "barh",
     "sql": None,
     "demo_data": [("Austria", 35), ("Germany", 30), ("Poland", 17), ("Spain", 14), ("Czechia", 11)]},

    {"key": "cross_program_country_map", "title": "Country Headcount % – All Programs", "kind": "barh", "sql": None,
     "demo_data": [("Austria", 7.8), ("Germany", 7.0), ("Poland", 6.7), ("Spain", 5.0), ("Czechia", 4.3)]},
    {"key": "cross_program_countries", "title": "Top 5 Countries – Cross-Programs Reach", "kind": "barh", "sql": None,
     "demo_data": [("Austria", 891), ("Germany", 445), ("Poland", 295), ("Spain", 273), ("Czechia", 244)]},
    {"key": "cross_program_companies", "title": "Top 5 Companies – Cross-Program Reach", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 522), ("Northwind", 294), ("Fabrikam", 227), ("Tailspin", 174), ("Woodgrove", 134)]},
    {"key": "cross_training_country_map", "title": "Country Headcount % – Cross-Program Training", "kind": "barh", "sql": None,
     "demo_data": [("Austria", 1.5), ("Germany", 1.2), ("Poland", 1.0), ("Spain", 0.7), ("Czechia", 0.6)]},
    {"key": "cross_training_countries", "title": "Top 5 Countries – Cross-Program Reach Training", "kind": "barh", "sql": None,
     "demo_data": [("Austria", 131), ("Germany", 69), ("Poland", 54), ("Spain", 46), ("Czechia", 38)]},
    {"key": "cross_training_companies", "title": "Top 5 Companies – Cross-Program Reach Training", "kind": "barh", "sql": None,
     "demo_data": [("Contoso Holding", 191), ("Northwind", 102), ("Fabrikam", 83), ("Tailspin", 57), ("Woodgrove", 42)]},

    # Revenue deck charts. Demo data approximates the dashboard's "Revenue by Country" bar
    # chart (values in EUR billions); the map has no dashboard tile so it renders locally too.
    {"key": "revenue_by_country", "title": "Revenue by Country", "kind": "barh",
     "sql": f"SELECT Country label,round(sum(`Annual Revenue (EUR)`)/1e9,2) value FROM {_BRONZE}.fake_company_data_with_reporting_month WHERE Reporting_Month=(SELECT max(Reporting_Month) FROM {_BRONZE}.fake_company_data_with_reporting_month) GROUP BY 1 ORDER BY 2 DESC",
     "demo_data": [("Germany", 11.4), ("Czechia", 10.7), ("Austria", 8.4), ("Spain", 6.9), ("Poland", 4.9)]},
    {"key": "revenue_by_country_map", "title": "Revenue by Country Map", "kind": "barh",
     "sql": f"SELECT Country label,round(sum(`Annual Revenue (EUR)`)/1e9,2) value FROM {_BRONZE}.fake_company_data_with_reporting_month WHERE Reporting_Month=(SELECT max(Reporting_Month) FROM {_BRONZE}.fake_company_data_with_reporting_month) GROUP BY 1 ORDER BY 2 DESC",
     "demo_data": [("Germany", 12.1), ("Czechia", 11.4), ("Austria", 8.9), ("Spain", 7.3), ("Poland", 5.2)]},
]

# Dashboard link to KPI fallback. This keeps your original hyperlink idea working.
LINK_KPI_FALLBACKS = {
    DASHBOARD_IDS["community"]: "community_total_registrations",
    DASHBOARD_IDS["learnings"]: "academy_total_learners",
    DASHBOARD_IDS["events"]: "events_total_participants",
    DASHBOARD_IDS["databricks_trainings"]: "db_participants",
}
