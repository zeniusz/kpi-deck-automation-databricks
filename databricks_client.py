from __future__ import annotations

import os
import json
import re

import pandas as pd
import streamlit as st
from databricks import sql

from settings import DATABRICKS_HTTP_PATH, DATABRICKS_PROFILE

# Defaults come from settings.py (env vars / .env). st.secrets["databricks"] still overrides them.
DEFAULT_HTTP_PATH = DATABRICKS_HTTP_PATH
DEFAULT_PROFILE = DATABRICKS_PROFILE


def _connect():
    """Open a Databricks SQL connection.

    Two auth paths, in order:
      1. If st.secrets has a full [databricks] block with a token → use PAT auth (classic).
      2. Otherwise reuse the Databricks CLI's OAuth login (profile from DATABRICKS_CONFIG_PROFILE) via the SDK's
         Config credential provider — no token to paste, same identity as the `databricks` CLI.
    Requires the target SQL warehouse to be RUNNING (start it in the Databricks UI, or it will
    auto-start on first query if the warehouse allows it).
    """
    # st.secrets raises (not returns {}) when no secrets.toml exists anywhere — guard it.
    try:
        secrets = dict(st.secrets.get("databricks", {}))
    except Exception:
        secrets = {}

    # Path 1: explicit token in secrets.toml
    if secrets.get("token") and secrets.get("server_hostname"):
        return sql.connect(
            server_hostname=secrets["server_hostname"],
            http_path=secrets.get("http_path", DEFAULT_HTTP_PATH),
            access_token=secrets["token"],
        )

    # Path 2: reuse the CLI OAuth login (no token needed)
    from databricks.sdk.core import Config

    profile = secrets.get("profile") or os.environ.get("DATABRICKS_CONFIG_PROFILE") or DEFAULT_PROFILE
    cfg = Config(profile=profile)
    http_path = secrets.get("http_path") or os.environ.get("DATABRICKS_HTTP_PATH") or DEFAULT_HTTP_PATH
    host = cfg.host.replace("https://", "").replace("http://", "").rstrip("/")
    return sql.connect(
        server_hostname=host,
        http_path=http_path,
        credentials_provider=lambda: cfg.authenticate,
    )


def run_scalar_queries(
    scalars: list[dict], warnings: list[str] | None = None,
) -> dict[str, object]:
    """Run each scalar's SQL against Databricks. KPIs with NO sql (most are demo-only today)
    fall back to their demo_value instead of hard-failing the whole pipeline — so live mode can
    still write Excel + capture dashboards while KPI text uses the configured demo numbers."""
    values: dict[str, object] = {}
    has_sql = [item for item in scalars if item.get("sql") or item.get("dashboard_dataset")]
    if not has_sql:
        return {item["key"]: item.get("demo_value") for item in scalars}

    connection = _connect()
    cursor = connection.cursor()
    try:
        dashboard_sources: dict[tuple[str, str], str] = {}
        for item in scalars:
            sql_text = item.get("sql")
            if item.get("dashboard_dataset"):
                dashboard_key = item.get("dashboard", "learnings")
                dataset_name = item["dashboard_dataset"]
                cache_key = (dashboard_key, dataset_name)
                if cache_key not in dashboard_sources:
                    from databricks.sdk import WorkspaceClient
                    from kpis import DATABRICKS_DASHBOARDS

                    dashboard_id = re.search(
                        r"/dashboardsv3/([^/]+)/", DATABRICKS_DASHBOARDS[dashboard_key]
                    ).group(1)
                    model = json.loads(
                        WorkspaceClient(profile=DEFAULT_PROFILE).lakeview.get(
                            dashboard_id
                        ).serialized_dashboard
                    )
                    dataset = next(d for d in model["datasets"] if d["name"] == dataset_name)
                    dashboard_sources[cache_key] = (
                        dataset.get("config", {}).get("source")
                        or "".join(dataset.get("queryLines", []))
                    )
                source = dashboard_sources[cache_key]
                where = item.get("dashboard_where", "")
                # The dataset's SQL can end in a `--` line comment; without a trailing newline
                # before the closing paren, that comment swallows the paren and everything after.
                sql_text = (
                    f"SELECT {item['dashboard_expression']} FROM ({source}\n) dashboard_data "
                    + (f"WHERE {where}" if where else "")
                    + (f" ORDER BY {item['dashboard_order']}" if item.get("dashboard_order") else "")
                    + (f" LIMIT {int(item['dashboard_limit'])}" if item.get("dashboard_limit") else "")
                )
            if not sql_text:
                values[item["key"]] = item.get("demo_value")
                continue
            try:
                cursor.execute(sql_text)
                row = cursor.fetchone()
                values[item["key"]] = row[0] if row is not None else item.get("demo_value")
            except Exception as exc:  # one optional KPI must not abort the whole presentation
                values[item["key"]] = item.get("demo_value")
                if warnings is not None:
                    warnings.append(
                        f"KPI '{item['key']}' used its fallback value ({str(exc)[:120]})."
                    )
    finally:
        cursor.close()
        connection.close()
    return values


def run_chart_queries(
    charts: list[dict], warnings: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Run each chart's SQL. Charts with no sql fall back to their demo_data (so live mode with
    dashboard screenshots doesn't fail on charts that have no query yet)."""
    has_sql = [item for item in charts if item.get("sql")]
    if not has_sql:
        return {item["key"]: pd.DataFrame(item.get("demo_data", []), columns=["label", "value"])
                for item in charts}

    data: dict[str, pd.DataFrame] = {}
    connection = _connect()
    cursor = connection.cursor()
    try:
        for item in charts:
            if not item.get("sql"):
                data[item["key"]] = pd.DataFrame(item.get("demo_data", []), columns=["label", "value"])
                continue
            try:
                cursor.execute(item["sql"])
                rows = cursor.fetchall()
                columns = [column[0] for column in cursor.description]
                data[item["key"]] = pd.DataFrame(rows, columns=columns)
            except Exception as exc:  # keep the deck build alive if one optional chart fails
                data[item["key"]] = pd.DataFrame(
                    item.get("demo_data", []), columns=["label", "value"]
                )
                if warnings is not None:
                    warnings.append(
                        f"Chart '{item['key']}' used fallback data ({str(exc)[:120]})."
                    )
    finally:
        cursor.close()
        connection.close()
    return data
