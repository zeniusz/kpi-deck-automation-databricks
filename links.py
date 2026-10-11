"""Where everything lives: SharePoint, the dashboards, the Genie space, Databricks objects.

One place for every external destination, so the app can link out instead of you hunting for URLs,
and so there is a single source of truth if one ever moves.

The four dashboard URLs here are the SAME ids as `kpis.DATABRICKS_DASHBOARDS` (verified 2026-07-27) —
that dict drives the screenshot capture, this one drives the UI links.
"""

from __future__ import annotations

from pathlib import Path

from settings import (
    BRONZE_SCHEMA,
    CATALOG,
    DECKS_LABEL,
    DECKS_URL,
    GENIE_SPACE_ID,
    GENIE_SPACE_NAME,
    GOLD_SCHEMA,
    VOLUME_ROOT,
    WORKSPACE_ORG_ID,
    WORKSPACE_URL,
    dashboard_url,
)

WORKSPACE = WORKSPACE_URL
ORG = f"?o={WORKSPACE_ORG_ID}" if WORKSPACE_ORG_ID else ""

# ── Where the finished presentations are stored ──────────────────────────────────────────────────
SHAREPOINT_DECKS = DECKS_URL
SHAREPOINT_LABEL = DECKS_LABEL

# ── Dashboards (published views) ─────────────────────────────────────────────────────────────────
DASHBOARDS: dict[str, str] = {
    "Analytics Community": dashboard_url("community"),
    "Learnings / Trainings": dashboard_url("learnings"),
    "Community Events": dashboard_url("events"),
    "Databricks Trainings": dashboard_url("databricks_trainings"),
}

# ── Genie space ──────────────────────────────────────────────────────────────────────────────────
GENIE_SPACES: dict[str, str] = {
    GENIE_SPACE_NAME: f"{WORKSPACE}/genie/rooms/{GENIE_SPACE_ID}{ORG}",
}

# ── Databricks data objects ──────────────────────────────────────────────────────────────────────
BRONZE_SCHEMA_URL = f"{WORKSPACE}/explore/data/{CATALOG}/{BRONZE_SCHEMA}{ORG}"
GOLD_SCHEMA_URL = f"{WORKSPACE}/explore/data/{CATALOG}/{GOLD_SCHEMA}{ORG}"
VOLUMES_ROOT = VOLUME_ROOT

# The local hand-off folder the app writes the finished deck into, ready to copy to the shared drive.
LOCAL_HANDOFF_DIR = str(Path(__file__).parent / "sharepoint_upload")  # real folder
LOCAL_HANDOFF_LABEL = "sharepoint_upload/"  # shown in the UI (no machine-specific path)


def render(st) -> None:
    """Render the links section as four separate zones. Takes `st` so this module stays
    import-light and UI-agnostic."""
    from ui_theme import zone_close, zone_open

    # ── Presentations ───────────────────────────────────────────────────────────────────────────
    zone_open(
        "📁 Presentations",
        "Where the finished decks are stored. The app also writes each refreshed deck to "
        f"<code>{LOCAL_HANDOFF_LABEL}</code> — copy it into the library below.",
    )
    st.link_button(SHAREPOINT_LABEL, SHAREPOINT_DECKS, use_container_width=True)
    zone_close()

    # ── Dashboards ──────────────────────────────────────────────────────────────────────────────
    zone_open("📊 Dashboards", "The sources the deck's charts are captured from.")
    cols = st.columns(2)
    for i, (name, url) in enumerate(DASHBOARDS.items()):
        with cols[i % 2]:
            st.link_button(name, url, use_container_width=True)
    zone_close()

    # ── Genie ───────────────────────────────────────────────────────────────────────────────────
    zone_open("💬 Genie space", "Ask questions in natural language against these tables.")
    for name, url in GENIE_SPACES.items():
        st.link_button(name, url, use_container_width=True)
    zone_close()

    # ── Databricks data ─────────────────────────────────────────────────────────────────────────
    zone_open(
        "🗄️ Databricks data",
        f"Raw source files are archived under <code>{VOLUMES_ROOT}</code> — "
        "this app never deletes them.",
    )
    d1, d2 = st.columns(2)
    with d1:
        st.link_button("Bronze schema", BRONZE_SCHEMA_URL, use_container_width=True)
    with d2:
        st.link_button("Gold schema", GOLD_SCHEMA_URL, use_container_width=True)
    zone_close()
