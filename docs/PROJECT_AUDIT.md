# Project Audit

Status of this document: **Phase 1 (read-only audit) of the portfolio preparation.**
Nothing in this repository has been declared safe to publish yet — see [§7](#7-anonymization-status).

Audit method: every Python module was inventoried (imports, size, who imports it); the upload →
Databricks path was read line by line; the existing tests were run on Windows / Python 3.12 with
**no Databricks connection**. Anything that needs the real workspace was *not* executed and is
marked accordingly. No production system was contacted.

Status legend used throughout:

| Tag | Meaning |
|---|---|
| **A** | Implemented and verified (a test I ran passed, or I traced the code path end to end) |
| **B** | Implemented but depends on private infrastructure — code reviewed, not executed |
| **C** | Incomplete or unverified |
| **D** | Recommended future enhancement |

---

## 1. What the application is

A Streamlit app (~13k lines of Python, 6 pages) that supports a monthly "Data & AI enablement KPI"
reporting cycle at a large company:

1. **Load** – a user uploads monthly Excel exports; the app routes each to a Databricks (Unity
   Catalog) table, cleans it, fills missing columns, archives the raw file to a Volume, and writes it.
2. **Refresh** – the app reads KPIs from Databricks SQL, optionally screenshots dashboard tiles with
   Playwright, and replaces *named* shapes in a PowerPoint template (`kpi:<key>`, `chart:<key>`).
3. **Report / ask** – a change summary between months (Markdown/Word) and a natural-language Q&A page
   backed by a Databricks Genie space.

| Page (`app.py` navigation) | File | Purpose |
|---|---|---|
| Overview | `services/0_Home.py` | landing, links, status |
| Files → Databricks | `services/1_Load_to_Databricks.py` | upload, route, clean, write |
| Presentation Update | `services/2_Update_Presentation.py` | refresh template from Databricks |
| Full Monthly Run | `services/3_Full_Run.py` | 1 + 2 back to back |
| Reports & Summaries | `services/4_Reports.py` | month-over-month change summary |
| Ask the Data | `services/5_Ask_the_Data.py` | Genie Q&A |

Entry point: `streamlit run app.py`. Services 1 and 2 are independent; their only link is Databricks
itself plus `.run_manifest.json` written by `run_manifest.py`.

## 2. Module inventory

| Module | Lines | Role | Referenced by |
|---|---:|---|---|
| `excel_ingestion.py` | 1753 | filename → table routing, cleaning, month inference, SQL write | pages 1, 3; most tests |
| `refresh_targets.py` | 1305 | post-load: rebuild tables, patch & publish Lakeview dashboards, Genie refresh | pages 1, 3 |
| `dashboard_capture.py` | 968 | Playwright screenshots of dashboard tiles | pages 2, 3 |
| `change_summary.py` | 846 | month-over-month movements → Markdown/DOCX | pages 2, 3, 4 |
| `kpis.py` | 644 | **config**: KPI/chart keys → SQL, dashboard URLs, tile map | pages 2, 3; tests |
| `genie_chat.py` | 528 | Genie client + built-in canned answers | ask widget, page 5 |
| `derivation.py` | 530 | lookup → derive → leave-null enrichment (country, company) | ingestion |
| `ui_theme.py` | 485 | CSS, widgets, folder constants | all pages |
| `ask_widget.py` | 410 | floating Q&A bubble | pages 0–4 |
| `learning_metadata.py` | 371 | platform/course/language registry | ingestion, refresh |
| `content_routing.py` | 330 | content-based table suggestion when filename doesn't match | page 1 |
| `pptx_refresher.py` | 311 | **template-tagging engine** (shape-name replacement) | pages 2, 3 |
| `session_status.py` | 258 | SSO session check, launches login helper subprocess | pages 0, 2, 3 |
| `run_manifest.py` | 228 | records what each load wrote | pages 0–4 |
| `template_tagger.py` | 136 | one-off tool that tags a fresh deck | *no importer – CLI only* |
| `databricks_client.py` | 167 | SQL connection + auth, scalar/chart queries | pages, ingestion |
| `month_check.py` | 94 | read-only "which months exist already" note | page 1 |
| `links.py` | 92 | external URLs | pages, `build_reference_html` |
| `chart_renderer.py` | 76 | local matplotlib chart fallback | pages 2, 3 |
| `build_reference_html.py` | 452 | generates an HTML rules reference | *no importer – CLI only* |
| `_login_auto.py`, `_login_helper.py`, `dashboard_login.py` | 106/87/94 | SSO login helpers | `_login_helper` is launched by `session_status`; `_login_auto` has no reference |
| `_probe_filter.py`, `_recapture_bar.py`, `_recapture_map.py` | 51/107/52 | one-off debugging scripts | **no references** |
| `test_ingestion_rules.py`, `test_dashboard_mappings.py` | 518/124 | `test_ingestion_rules.py` is a custom script (0 tests collected by pytest); `test_dashboard_mappings.py` is `unittest` (7 collected) | – |

Dependencies (`requirements.txt`, unpinned `>=`): streamlit, python-pptx, databricks-sql-connector,
databricks-sdk, pandas, numpy, openpyxl, pyarrow, matplotlib, pillow, python-docx, playwright.
All are imported somewhere; none looked removable on inspection (not verified by a tool).

## 3. Verification baseline (what I actually ran)

| Check | Result |
|---|---|
| `test_ingestion_rules.py` (routing, cleaning, month inference, derivation, mapping rules) | **All checks pass** (Windows, Py 3.12, offline) |
| `test_dashboard_mappings.py` | **7 tests: 1 failure, 1 error** — see below |
| Anything touching a live Databricks workspace | **Not run** (no access, by design) |
| Streamlit UI end-to-end | **Not run yet** |

The two failures in `test_dashboard_mappings.py` are existing defects, not regressions:

* `test_manual_slides_are_never_refreshed` asserts `MANUAL_SLIDE_NUMBERS == {9, 10, 17}` but
  `pptx_refresher.py` now defines `{9, 10, 17, 22, 23, 24, 25, 26}` — a **stale test**.
* `test_untagged_community_leader_cards_are_inferred` reads a private input deck
  (`presentations/input/…2026.06 (3).pptx`) that is not in the repo — the test **cannot run without
  confidential input**; it needs a synthetic fixture.

## 4. Feature-by-feature status

### 4.1 Upload → Databricks (core feature)

Traced in `services/1_Load_to_Databricks.py` and `excel_ingestion.py`:

1. `st.file_uploader` accepts `.xlsx`/`.xls`, multiple files. Bytes are written to `excel_input/<name>`.
2. Reporting month: inferred from filename (`infer_reporting_month`); if absent the UI asks, defaulting to *last* month.
3. `build_plan` → `route_for(filename)`: first checks a learning-course registry, then an ordered list
   of ~20 regexes (`ROUTES`), first match wins. Result: table, **write mode** (`FULL_REPLACE`, `APPEND`,
   `NEW_TABLE_PERIOD`, `NEW_TABLE_EVENT`, `ARCHIVE_ONLY`), Volume sub-folder, cleaning routine.
4. Unrouted files: `content_routing.suggest_tables` inspects columns/values and offers candidates.
5. Files are sorted so enrichment sources load first; frames cleaned (`apply_cleaning`), months tagged.
6. Preview toggle is **off by default**; nothing is written unless "Write to Databricks" is on.
7. `execute_write`: archive raw file to Volume → write table (replace / delete-then-insert / create) →
   INSERT via Parquet `read_files()` above 300 rows, else batched `VALUES` (500/row batch).
8. `refresh_all` rebuilds derived tables, patches dashboards, refreshes Genie; `record_run` stores a manifest.

| Capability | Tag | Evidence / caveat |
|---|---|---|
| Filename routing & write-mode selection | **A** | exercised by `test_ingestion_rules.py` |
| Month inference incl. export-timestamp vs period | **A** | tested (R2b/R2c) |
| Cleaning, dedup, country/company derivation | **A** | tested offline on synthetic frames |
| Content-based table suggestion | **A/C** | code reviewed; limited direct tests |
| Preview-only dry run | **A** (by code) | write requires explicit toggle |
| Volume archive, table writes, bulk Parquet load | **B** | needs workspace + grants; never executed here |
| Dashboard patching/publishing (`refresh_targets.py`) | **B** | mutates Lakeview dashboards via SDK; unverified |
| Re-run safety (delete month, then insert) | **C** | logic exists, but see risks below |

**Findings on robustness (from code reading — not reproduced against a live system):**

1. **Not atomic.** For `APPEND`, the month's rows are `DELETE`d and then `INSERT`ed as separate
   statements. A failure between them (warehouse stops, parquet staging fails and the fallback also
   fails) leaves the month **empty**. A retry fixes it, but the intermediate state is real data loss.
2. **Archive failures are swallowed.** `archive_to_volume` errors are logged as a warning and the table
   write continues; the run is still recorded as successful. The "raw file is always preserved" claim
   in the UI/docs is therefore not guaranteed.
3. **Docs say "never overwritten", code overwrites.** UI text and docstrings state the archive is "a NEW
   file, nothing overwritten", but `w.files.upload(..., overwrite=True)` is used, and the path is
   `<subpath>/<month>/<original filename>` — re-uploading the same name for the same month replaces it.
   The staging parquet (`_staging/<stem>.parquet`) is also overwritten and never cleaned up.
4. **SQL value escaping is incomplete.** The small-frame path builds `VALUES` strings by doubling single
   quotes only. Spark SQL string literals also treat a backslash as an escape, so a cell containing
   `\` (e.g. a trailing backslash) can break or alter the statement. Table/column identifiers come from
   internal route tables (low risk) but column names come from the uploaded file and are only
   backtick-wrapped, not escaped. The month-delete `DELETE … WHERE col = ?` is parameterised.
5. **Uploaded filename used as a path.** `EXCEL_DIR / xf.name` — Streamlit supplies the client's file
   name. Path-traversal behaviour of that value was **not verified**; `Path(name).name` should be applied.
6. **Uploaded files persist on disk** in `excel_input/` after the run (they contain personal data in the
   original setup) and are only excluded by `.gitignore`.
7. **No file-content validation beyond parsing.** Extension filter is client-side; no size limit, no
   magic-byte check, no schema check before routing (schema alignment happens only for `FULL_REPLACE`).
8. **Errors shown raw** (`st.error(f"Load failed: {exc}")`), which can include SQL text and host names.
9. **Orphaned UI state:** `st.session_state["manual_routes"]` is written when a user picks a suggested
   table for an unrouted file, but nothing reads it — the pick has **no effect**; the file is still
   skipped with "no matching table rule". The content-suggestion feature is therefore incomplete (**C**).
10. **Duplicate-upload handling** is per-month replacement (APPEND), per-source-file replacement
    (period/event tables) or full replacement — reasonable and tested for the cleaning logic, but the
    destructive replace happens before the insert (see 1).

### 4.2 Authentication & configuration (**B**)

* `databricks_client._connect`: token from `st.secrets` if present, otherwise the Databricks CLI OAuth
  profile. Good: no token handling in code. Weak: a **hardcoded profile name and SQL-warehouse path are
  the defaults**, and `excel_ingestion.py` / `genie_chat.py` / `databricks_client.py` each hardcode the
  profile again instead of using the one resolved by `_connect` (4 separate literals) – so secrets/env
  overrides apply to SQL but not to Files-API archive, Parquet staging or Genie.
* No role/permission model in the app: any user who can open the page can trigger writes (the toggle is
  not a control, only a safety prompt). Access control is entirely delegated to the Databricks identity
  of the host machine.

### 4.3 Template-tagging presentation refresh (**A** for the engine, **B** for the data feeding it)

`pptx_refresher.py` replaces shapes by name (`kpi:<key>` text, `chart:<key>` picture), preserves every
other shape, protects "manual" slides (`MANUAL_SLIDE_NUMBERS`) and supports placement overrides
(`CHART_BOX_OVERRIDES`). `template_tagger.py` applies a hardcoded slide→key map to an untagged deck.
`kpis.py` binds each key to SQL or a demo value; demo mode runs without Databricks.

* Engine behaviour is exercised partly through `test_ingestion_rules.py` (imports `pptx_refresher`).
  I have **not yet** run an end-to-end refresh on a synthetic deck (**C**).
* Two tag syntaxes exist: shape *names* (`kpi:key`, documented) and `{{kpi:key}}` text tokens
  (regexes `TAG_KPI`/`TAG_CHART` in `pptx_refresher.py`). Whether the second is still used by any
  shipped template is **unverified**.
* The shipped templates are the real company decks – see §7.

### 4.4 Dashboard screenshot capture (**B**)

Playwright automation of Lakeview tiles behind SSO/MFA; session stored in `.databricks_session.json`
(git-ignored). Entirely tied to the private workspace and to dashboard DOM structure; cannot be run or
demonstrated outside it. Candidate for being kept as clearly-labelled optional/private-infra code.

### 4.5 Reports & Ask-the-Data (**B**)

`change_summary.py` (Markdown/DOCX) and `genie_chat.py` need live tables / a Genie space. `genie_chat`
also contains a hand-written "wiki" of the company's schema used for canned answers – company-specific.

## 5. Hardcoded, environment-specific values (categories & locations)

Real values are deliberately **not** reproduced here.

| Category | Where | Notes |
|---|---|---|
| Databricks workspace host / org id | `kpis.py`, `links.py`, `dashboard_login.py`, `.streamlit/secrets.toml.example` | also embedded in dashboard URLs |
| Dashboard ids, Genie space id | `kpis.py`, `links.py` | |
| SQL warehouse path | `databricks_client.py` | default constant |
| CLI profile name | `databricks_client.py`, `excel_ingestion.py` (×2), `genie_chat.py` (×2), `HANDOVER.md` | profile names a real dev workspace |
| Catalog / bronze / gold schema, table names | `excel_ingestion.py`, `kpis.py`, `links.py`, `genie_chat.py`, `change_summary.py`, `refresh_targets.py` | hundreds of f-string SQL references |
| Volume root & sub-folder names | `excel_ingestion.py` (`ROUTES`), `genie_chat.py` | sub-folder names mirror internal SharePoint structure |
| SharePoint site URL | `links.py`, `change_summary.py` | company tenant + site name |
| Company / entity names, email-domain → country map, aliases | `derivation.py`, `excel_ingestion.py`, `kpis.py`, `test_ingestion_rules.py`, `ui_theme.py` | the **business rules themselves** encode the corporate structure (62 hits in `derivation.py` alone) |
| Person names / usernames | `HANDOVER.md`, `MANAGE_APP.md`, `session_status.py` (paths), `.vscode/` (none), template metadata | |
| WSL home paths | `session_status.py`, docs | |

Scan result on the working copy (case-insensitive: company names, workspace identifiers, usernames,
SharePoint, emails): **326 hits in 30 files**. This is a *screening* count, not proof of completeness.

## 6. Code health observations

* **No logging.** No module uses `logging`; diagnostics go to Streamlit UI or are swallowed by broad
  `except Exception: # noqa: BLE001` (many sites). Swallowed errors are the main debuggability risk.
* **Large multi-purpose modules** (`excel_ingestion` 1.7k, `refresh_targets` 1.3k, `dashboard_capture`
  1k): routing data, cleaning, SQL generation and I/O live together. `execute_write` is ~280 lines.
* **Tests**: `test_ingestion_rules.py` is script-style with a custom `FAILED` list and a `__main__`
  runner (pytest collects nothing from it); `test_dashboard_mappings.py` is `unittest`. No CI, no coverage of `execute_write` SQL generation, no UI tests.
* **Obsolete/unreferenced files:** `_probe_filter.py`, `_recapture_bar.py`, `_recapture_map.py`,
  `_login_auto.py` have no references; `build_reference_html.py` and `template_tagger.py` are CLI-only
  (still useful). Removal candidates need owner confirmation per the "trace before deleting" rule.
* **Docs drift:** `README.md` still describes "v2", old folder names and a different app title;
  `HANDOVER.md` describes six pages; the `.vscode` setting points to a Linux interpreter path.
* **Repo hygiene:** no git history exists in the provided folder (so there is no history to scrub, but
  also none to inspect). A 600 MB Linux `.venv` was copied into the folder – excluded from the portfolio copy.
  `.gitignore` covers secrets and generated decks but **not** `.env`, `excel_input/*.xls`, `logs`, or `.vscode`.

## 7. Anonymization status

**Not complete. The repository must not be published in its current state.**

Known blockers found so far:

1. Real workspace/tenant/catalog/profile identifiers in source and docs (§5).
2. `templates/*.pptx` – the main deck contains real KPI values, real headcount figures, 40+ embedded chart
   images from real dashboards, and document metadata with the names of colleagues (author and
   last-modified-by). It must be **replaced by a synthetic template**, not edited. The second template
   is described in the code as fake demo data but carries the author's name in metadata and company
   branding in text; it needs the same treatment.
3. Company-specific business rules (entity names, email domains, country mapping, course names) —
   structure is identifiable even after renaming; needs a **synthetic organisation model** and
   configuration-driven rules, per the brief.
4. Test fixtures that assert real figures (e.g. member counts, entity names).
5. `HANDOVER.md`/`MANAGE_APP.md` contain machine paths, usernames and operational details.
6. Screenshots do not exist yet; any real-app screenshot must be taken from the synthetic demo only.

## 8. Reusability assessment (Phase 5 summary)

| Layer | Org-specific? | Reusable as |
|---|---|---|
| Route table → (table, write mode, folder) | yes (patterns, names) | **config-driven routing rules** (YAML/JSON) + generic engine |
| Write-mode semantics (replace / append-by-month / period tables / event-keyed) | no | core of a "monthly file ingestion" library |
| Month inference & month-format matching | partly | reusable utilities |
| Cleaning, dedup, lookup→derive→null enrichment | rules are org-specific, engine is not | engine + pluggable rule set |
| Template tagging (`kpi:`/`chart:` shape names) | no | reusable as-is; independent of Databricks |
| Dashboard screenshot capture | yes (DOM + SSO) | optional plug-in only |
| Genie canned "wiki" | yes | remove or make config |
| UI (Streamlit pages) | partly | keep; swap labels/links via config |

Proposed enhancements (benefit / effort / risk) — each addresses a limitation observed above:

| # | Enhancement | Benefit | Effort | Risk |
|---|---|---|---|---|
| 1 | Local **demo mode**: write to a local SQLite/DuckDB/Parquet "warehouse" behind the same interface as the Databricks writer | App runs and is demonstrable by any reviewer; makes the write path testable | M | Two backends to keep in sync; keep interface tiny |
| 2 | Move `ROUTES`, catalog/schema/volume names into config + `.env` | Removes identifiers; generic reuse | M | Regex order semantics must be preserved — protect with tests |
| 3 | Make destructive replace safer: write to staging then swap/`MERGE`, or `INSERT` then `DELETE` old by batch id | Eliminates empty-month window | M | Needs live-workspace verification, which I cannot do |
| 4 | Fix: sanitize upload filename; escape/parametrise values; don't swallow archive failure | Closes findings 4, 5, 2 | S | Low |
| 5 | Wire up `manual_routes` or remove the suggestion UI | Removes dead/misleading feature | S | Low |
| 6 | Replace script tests with pytest; add fixtures with synthetic data; add SQL-generation tests with a fake cursor | Reviewer confidence; CI | M | Low |
| 7 | Introduce `logging` with levels; replace broad `except` where safe | Debuggability | S–M | Behaviour changes if errors stop being swallowed — document |
| 8 | Split `excel_ingestion.py` (routing / cleaning / writer) | Maintainability | M | Regression risk; do only after tests (6) |

Not proposed: new functionality beyond the above; the template mechanism stays as an existing
capability (it is in scope by the owner's decision), not a new feature.

## 9. Proposed order of work

1. Decide config model + synthetic organisation (needs owner input on how generic the rules should be).
2. Build synthetic demo data and a synthetic tagged template; add pytest suite around current behaviour.
3. Externalise identifiers; add `.env.example`; remove/replace real assets; local demo backend.
4. Apply the small fixes in §8 rows 4–5; then conservative refactors with tests green.
5. Re-run the identifier scan on the *final* tree **and** a binary-metadata check on every non-text asset;
   only then write the "safe to publish" statement.
6. README, `ARCHITECTURE.md`, screenshots from the demo.

---

## 9. Cleanup status (portfolio copy)

The original folder is untouched; every change below was made in this sibling copy.

### Fixes applied (behaviour changes, all deliberate)
| Change | Why |
|---|---|
| Guild route moved ahead of the broader listing route | The broader regex shadowed it; the guild file was routed to the wrong table. Regression test added. |
| `quote_ident` / `sql_literal` helpers used for all generated SQL | Backslashes and backticks in cell values/headers were not escaped. Booleans now render as `TRUE`/`FALSE`. |
| `safe_stem` for the Parquet staging filename | Quotes or odd characters in a filename ended up inside a SQL string and a volume path. |
| Parquet staging file deleted after load (best effort) | Staged copies accumulated in the volume. |
| Archive failure surfaced as `archive_ok` in the result and run manifest, and logged | It was only a log line; a failed archive looked like success. |
| `links.py` Bronze/Gold buttons use schema URLs; dead "Ingestion guide" link removed | The buttons were passed schema *names* as URLs. |
| UI wording "never deleted" → "this app never deletes" | The app cannot promise what other users do in the workspace. |

Visual design (colours, layout) is unchanged from the original.

### Not verified
Nothing that needs a live workspace has been run: Volume archive, `read_files()` bulk load, SQL
writes, Lakeview/Genie calls, dashboard screenshot capture. They are unit-tested only where logic
could be separated from I/O.

### Phase 4 — upload workflow review (no production contact)
Findings 1–10 above stand; items fixed are in the table. Remaining risk: month-replace is
delete-then-insert without a transaction, so a failure between the two steps leaves a month empty.
Effort to fix (stage → `MERGE`/`REPLACE WHERE`): medium; benefit: high; risk: medium (needs a real workspace to test).

### Phase 5 — reusability proposals
| Proposal | Benefit | Effort | Risk |
|---|---|---|---|
| Move `ROUTES` into a TOML/YAML file | Reuse for other file families without code edits | Medium | Low (route tests guard order) |
| Atomic month replace (`REPLACE WHERE`) | No empty-month window | Medium | Medium |
| Split `excel_ingestion.py` (1.7k lines) into routes / cleaning / writer | Readability, easier tests | Medium | Low–medium |
| Type-hint and test the page modules | Catch UI regressions | High | Low |
| Generic template tagger UI | Non-developers can tag decks | Medium | Low |

### Publication status
Automated scans of text files and of the XML inside the pptx/xlsx assets found no remaining real
hostnames, ids, company names or emails. **This is not a sign-off.** A human review of the final tree
(including your own check of `config/org_profile.toml` and the demo data) is still required before
publishing.

### 9.x Follow-up fixes (documentation pass)
| Finding | Change |
|---|---|
| Chat cache is meant to be cleared after a load (comment in `genie_chat.py`) but `clear_cache()` was never called | Called after a successful load in `1_Load_to_Databricks` and `3_Full_Run` |
| UI and chat said archived files are "never overwritten"; `archive_to_volume` uploads with `overwrite=True`, so the same filename replaces its own copy | Wording corrected (app, home page, chat knowledge base). Behaviour unchanged |
| Inline code in chat answers was unreadable (light text on light chip); uploaded-file chips were white-on-white; static tables low contrast | CSS fixes in `ui_theme.py` |
| Template Tagging inventory used a canvas data grid | Replaced with a static table in a scrollable container |
Tests: 73 passing. Not re-verified against a live workspace.
