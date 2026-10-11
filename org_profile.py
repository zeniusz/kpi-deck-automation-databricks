"""Loads the organisation profile (config/org_profile.toml) — the company-specific data rules.

The engine (derivation.py, excel_ingestion.py) contains no organisation names, domains or countries;
it reads them from here. Point ORG_PROFILE_PATH at a different TOML file to run the app for another
organisation. See config/org_profile.toml for the schema, documented inline.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROFILE_PATH = Path(
    os.environ.get("ORG_PROFILE_PATH") or Path(__file__).parent / "config" / "org_profile.toml"
)

with open(PROFILE_PATH, "rb") as _fh:
    _RAW = tomllib.load(_fh)

ORG_NAME: str = _RAW["organisation"]["name"]
DEFAULT_COUNTRY: str = _RAW["organisation"]["default_country"]
UNKNOWN_COMPANY: str = _RAW["organisation"].get("unknown_company", "Unknown")

COUNTRY_BY_ISO: dict[str, str] = dict(_RAW["iso_countries"])
LOCAL_COUNTRY_NAMES: dict[str, str] = dict(_RAW["local_country_names"])

DOMAIN_COUNTRY: dict[str, str] = {d: v["country"] for d, v in _RAW["domains"].items()}
DOMAIN_COMPANY: dict[str, str] = {
    d: v["company"] for d, v in _RAW["domains"].items() if "company" in v
}

ALIAS_DOMAINS: list[set[str]] = [set(g["domains"]) for g in _RAW["alias_groups"]]
ALIAS_PRIORITY: dict[str, int] = dict(_RAW["alias_priority"])
LOOKUP_ORDER: list[str] = list(_RAW["lookup"]["order"])

COMPANY_CONTAINS_COUNTRY: list[tuple[str, str]] = [
    (row["needle"], row["country"]) for row in _RAW.get("company_contains", [])
]
NAME_REPAIRS: dict[str, str] = dict(_RAW.get("name_repairs", {}))
