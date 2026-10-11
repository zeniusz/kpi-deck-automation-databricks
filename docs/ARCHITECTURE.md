# Architecture

```
Excel exports ──► Service 1: Files → Databricks ──► bronze tables + UC Volume archive
                                                          │
                                                          ▼  (Databricks is the only link)
Dashboards ◄── refresh ◄── Service 2: Presentation Update ──► refreshed .pptx + change summary
```
The two services never call each other. Service 2 reads whatever is already in Databricks, so it works
after a UI upload or a notebook run too. `run_manifest.py` records what Service 1 wrote so Service 2 can
flag new data without a warehouse query.

## Modules
| Area | Modules |
|---|---|
| UI | `app.py` (navigation), `services/0_Home … 6_Template_Tagging`, `ui_theme.py`, `ask_widget.py`, `links.py` |
| Settings | `settings.py` (env vars, `.env`), `org_profile.py` + `config/org_profile.toml`, `config/deck_layout.json` |
| Ingestion | `excel_ingestion.py` (routes, cleaning, write logic), `derivation.py`, `learning_metadata.py`, `content_routing.py`, `month_check.py` |
| Deck | `kpis.py` (KPI/chart definitions, SQL), `pptx_refresher.py`, `template_tagger.py`, `chart_renderer.py`, `refresh_targets.py`, `dashboard_capture.py` |
| Databricks | `databricks_client.py` (SQL connector), SDK Files API / Lakeview / Genie |
| Reporting | `change_summary.py`, `genie_chat.py`, `build_reference_html.py` |

## Ingestion flow
1. `build_plan` matches the filename against ordered `ROUTES` (first match wins) → table + `WriteMode` + reporting month.
2. `load_full_frame` cleans and enriches; `prepare_run_frames` de-duplicates across files of one run.
3. `explain_plan` shows the dry run. Nothing is written unless the user enables writing.
4. `execute_write`: archive raw file to the Volume (failure is reported via `archive_ok`, table write continues) → delete this month's rows once per run (`written` set) → insert. Above 300 rows a Parquet file is staged and loaded with `read_files()`; staging is removed afterwards. Smaller frames use batched `VALUES` with escaped identifiers/literals.

## Template tagging
Shapes named `kpi:<key>` (text) or `chart:<key>` (picture) are the only things the refresher touches.
`template_tagger.py` applies those names from a JSON spec so tagging is repeatable; the in-app Template Tagging page lists a deck's shapes, takes the spec and returns the tagged deck (`tag_bytes`). Slides listed as
manual are never modified. Pictures keep their tag so the same deck can be refreshed again next month.

## Testing
`tests/` is offline: routing and cleaning rules, every demo file's route/mode, tagger and refresher,
SQL escaping helpers.

## Diagrams
`docs/diagrams/diagrams.html` is the source of the four diagrams in `docs/screenshots/10…13`
(ingestion pipeline, write modes, deck refresh + tagging, chat flow). They are drawn from the code, not
from a live run.
