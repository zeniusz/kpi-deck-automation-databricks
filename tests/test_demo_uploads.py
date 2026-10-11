"""Every synthetic demo upload must route to its intended table and mode (dry run, no Databricks)."""

from pathlib import Path

import pytest

import excel_ingestion as ei

EXCEL = Path(__file__).resolve().parent.parent / "demo_data" / "excel"

# filename → (target table, write mode, reporting month the plan should carry)
EXPECTED = {
    "Analytics Community 2026.07.xlsx": ("analytics_teams_listing", ei.WriteMode.APPEND, "2026.07"),
    "Analytics Community (2026.07.03).xlsx": ("analytics_community", ei.WriteMode.FULL_REPLACE, "2026.07"),
    "Lakehouse Guild TEAMS listing 2026.07.xlsx": ("lakehouse_guild", ei.WriteMode.APPEND, "2026.07"),
    "Atlas_DataGovernance_Basics.xlsx": ("atlas_datagovernance_basics", ei.WriteMode.NEW_TABLE_PERIOD, "2026.07"),
    "300400_export_eLearningDataBasicsAwarenessPart1_20260803_0838.xlsx":
        ("atlas_data_basics_and_awareness_part_1", ei.WriteMode.NEW_TABLE_PERIOD, "2026.07"),
    "Analytics Event Number 5.xlsx": ("analytics_event_number_5", ei.WriteMode.NEW_TABLE_EVENT, "2026.07"),
    "Revenue_2026.07.xlsx": ("fake_company_data_with_reporting_month", ei.WriteMode.APPEND, "2026.07"),
}


def test_every_demo_file_is_covered():
    assert {p.name for p in EXCEL.glob("*.xlsx")} == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_route_and_mode(name):
    table, mode, month = EXPECTED[name]
    plan = ei.build_plan(EXCEL / name, month_fallback="2026.07")
    assert plan.route is not None, f"{name} did not route"
    assert plan.route.table == table
    assert plan.route.mode is mode
    assert plan.reporting_month == month
    assert plan.full_table.startswith(ei.CATALOG + ".")


def test_listing_country_and_company_are_derived_from_the_domain():
    frame = ei.load_full_frame(EXCEL / "Analytics Community 2026.07.xlsx", "2026.07")
    assert frame["Country"].notna().all() and frame["Company"].notna().all()
    assert set(frame["Country"]) <= set(ei.org_profile.COUNTRY_BY_ISO.values()) | {ei.DEFAULT_COUNTRY}


def test_registration_form_uses_the_real_address_and_drops_the_repeat():
    raw_rows = len(__import__("pandas").read_excel(EXCEL / "Analytics Community (2026.07.03).xlsx"))
    frame = ei.load_full_frame(EXCEL / "Analytics Community (2026.07.03).xlsx", "2026.07")
    assert len(frame) == raw_rows - 1  # the duplicated submission collapsed
    email_col = ei.find_email_column(frame)
    assert email_col is not None and frame[email_col].str.contains("@").all()


def test_event_attendance_drops_short_visits_and_repeats():
    import pandas as pd

    raw = pd.read_excel(EXCEL / "Analytics Event Number 5.xlsx")
    frame = ei.load_full_frame(EXCEL / "Analytics Event Number 5.xlsx", "2026.07")
    assert len(frame) < len(raw)
    assert "Brief Visitor" not in set(frame["Name"])
