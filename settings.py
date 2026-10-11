"""Environment-specific settings — the ONE place infrastructure names live.

Everything here is read from environment variables (optionally loaded from a local `.env`, see
`.env.example`). The defaults are harmless placeholders so the app imports, the tests run and the demo
works with no Databricks workspace at all.

Nothing in this module talks to Databricks; it only builds names and URLs.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv is optional: without it, plain environment variables still work.
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env")
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


# ── Databricks connection ────────────────────────────────────────────────────────────────────────
# Auth is never configured here: a token (if used) lives in .streamlit/secrets.toml, otherwise the
# Databricks CLI's OAuth profile named below is reused.
DATABRICKS_PROFILE = _env("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
DATABRICKS_HTTP_PATH = _env("DATABRICKS_HTTP_PATH", "/sql/1.0/warehouses/YOUR_WAREHOUSE_ID")
WORKSPACE_URL = _env("DATABRICKS_HOST", "https://your-workspace.cloud.databricks.com").rstrip("/")
WORKSPACE_ORG_ID = _env("DATABRICKS_ORG_ID", "")

# ── Unity Catalog layout ─────────────────────────────────────────────────────────────────────────
CATALOG = _env("DBX_CATALOG", "demo_catalog")
BRONZE_SCHEMA = _env("DBX_BRONZE_SCHEMA", "bronze")
GOLD_SCHEMA = _env("DBX_GOLD_SCHEMA", "gold")
BRONZE_FQN = f"{CATALOG}.{BRONZE_SCHEMA}"      # catalog.schema, ready to prefix a table name
GOLD_FQN = f"{CATALOG}.{GOLD_SCHEMA}"
VOLUME_ROOT = _env("DBX_VOLUME_ROOT", f"/Volumes/{CATALOG}/{BRONZE_SCHEMA}")

# ── Published dashboards / Genie / document storage (links only) ────────────────────────────────
DASHBOARD_IDS: dict[str, str] = {
    "community": _env("DASHBOARD_ID_COMMUNITY", "YOUR_COMMUNITY_DASHBOARD_ID"),
    "learnings": _env("DASHBOARD_ID_LEARNINGS", "YOUR_LEARNINGS_DASHBOARD_ID"),
    "events": _env("DASHBOARD_ID_EVENTS", "YOUR_EVENTS_DASHBOARD_ID"),
    "databricks_trainings": _env("DASHBOARD_ID_TRAININGS", "YOUR_TRAININGS_DASHBOARD_ID"),
    "revenue": _env("DASHBOARD_ID_REVENUE", "YOUR_REVENUE_DASHBOARD_ID"),
}
GENIE_SPACE_ID = _env("GENIE_SPACE_ID", "YOUR_GENIE_SPACE_ID")
GENIE_SPACE_NAME = _env("GENIE_SPACE_NAME", "Analytics Department Activities")
DECKS_URL = _env("DECKS_URL", "https://example.sharepoint.com/sites/your-site/Shared%20Documents")
DECKS_LABEL = _env("DECKS_LABEL", "KPI decks (shared drive)")
SCHEDULE_TIMEZONE = _env("SCHEDULE_TIMEZONE", "UTC")


def dashboard_url(key: str) -> str:
    """Published-dashboard URL for a key in DASHBOARD_IDS."""
    org = f"?o={WORKSPACE_ORG_ID}" if WORKSPACE_ORG_ID else ""
    return f"{WORKSPACE_URL}/dashboardsv3/{DASHBOARD_IDS[key]}/published{org}"
