"""Ask-the-data chat, backed by the Databricks Genie space.

We do NOT bolt on a separate LLM. Databricks Genie already has a space configured over these exact
tables ("Analytics Department Activities"), it is governed by the same Unity Catalog permissions, and it
answers by generating and running real SQL — so the numbers cannot drift from the dashboards. The
SDK we already depend on exposes it (`WorkspaceClient.genie`).

Each answer carries the SQL Genie ran, which matters: you can verify a figure instead of trusting it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from settings import BRONZE_FQN, DATABRICKS_PROFILE, GENIE_SPACE_ID, GENIE_SPACE_NAME, GOLD_FQN, VOLUME_ROOT

# The "Analytics Department Activities" space — same id as links.GENIE_SPACES.
DEFAULT_SPACE_ID = GENIE_SPACE_ID
DEFAULT_TIMEOUT_S = 240

# ── Questions about the APP itself ──────────────────────────────────────────────────────────────
# Genie only knows the Databricks tables, so "how does this app work?" would get a confused answer
# or none at all. Those questions are answered locally from this knowledge base — no LLM needed, no
# risk of inventing behaviour the app does not have. Keyed by topic with the trigger words that
# should match it.
#
# Matching is token-based, not literal-substring: a trigger of "how does this work" used to MISS
# "how does this app work" (extra word in the middle) while a bare trigger word like "help" wrongly
# captured the data question "help me count the learnings". So each topic now carries `strong`
# phrases/words and `weak` supporting words, matched against a typo-corrected token stream, and a
# data-shaped question vetoes the local answer entirely. See `answer_about_app`.
APP_KNOWLEDGE: list[tuple[tuple[str, ...], str, str]] = [
    (
        ("what is this", "what does this app do", "what does it do", "what can i do",
         "how does this work", "how does this app work", "how this app works", "what is the app",
         "how do i use", "how to use", "getting started", "tell me about this app",
         "overview", "purpose"),
        "What this app does",
        "It has two services that work **independently**:\n\n"
        "- **Files → Databricks** — upload the new Excel sheets. Each file is archived into its UC "
        "Volume folder, cleaned, missing columns are filled in, and the rows are **appended** to "
        "the bronze table.\n"
        "- **Presentation Update** — refreshes the dashboards at the newest month, captures each "
        "visual, and updates the tagged PowerPoint template.\n\n"
        "**Full Monthly Run** does both in the right order, **Reports & Summaries** downloads a "
        "written summary of what changed, and this chat answers questions.",
    ),
    (
        ("delete", "deleted", "overwrite", "overwritten", "lose data", "lose my data",
         "is it safe", "destroy", "data loss", "duplicate rows", "append or replace"),
        "Archived files are never deleted",
        "Every upload is **archived** to its UC Volume folder and archived files are never deleted "
        "(re-uploading the same filename replaces that archived copy). Rows are **appended** to the table and tagged with their reporting "
        "month, so each period coexists.\n\n"
        "Re-uploading the *same* month replaces just that month's rows (so a re-run can't "
        "duplicate), and the four cumulative-source tables aren't written by the app at all — "
        "they're left to the existing notebooks.",
    ),
    (
        ("missing column", "missing columns", "missing value", "missing data", "derive", "derived",
         "derivation", "enrich", "enriched", "enrichment", "lookup", "look up", "filled in",
         "fill missing", "country derived", "country come from", "country filled",
         "job title derived", "job title filled"),
        "How missing columns are filled",
        "In the same order the notebooks use:\n\n"
        "1. **Lookup** — copy the value from a related table, matched on email (including "
        "`cloud.contoso.example` ↔ `contoso.example` ↔ `northwind-at.example` aliases, then a name-based fallback).\n"
        "2. **Derive** — from the company's bracket code like `(AT)`, then the email domain, then "
        "default to Austria.\n"
        "3. **Leave empty** — never invent a value. Job title has no derivation rule, so it stays "
        "blank when there's no source to copy from.\n\n"
        "An existing value is never overwritten.",
    ),
    (
        ("load order", "which order", "what order", "which first", "which one first",
         "load first", "upload order", "sequence", "in what sequence", "does order matter"),
        "Why load order matters",
        "`analytics_community` is the enrichment **source** for the Teams listing and the Guild, and "
        "the event tables feed the Guild's `Events_Attended`. So community must load first — the "
        "app sorts a multi-file upload automatically and shows you the order.\n\n"
        "For the same reason the deck is built **after** the load: the dashboards and the live KPI "
        "queries follow `max(Reporting_Month)`, so building first would show last month.",
    ),
    (
        ("presentation", "deck", "pptx", "powerpoint", "template", "tagged", "tags",
         "chart image", "slide", "slides", "placeholder"),
        "How the presentation is updated",
        "Upload the **tagged** template (shapes named `kpi:…` / `chart:…`). The app fills those "
        "placeholders with new numbers and freshly captured dashboard images and leaves **everything "
        "else in the deck untouched**.\n\n"
        "Images are inserted proportionally — centred, never stretched. If a chart has no captured "
        "image, the template's existing picture stays. An untagged deck updates nothing, and the "
        "app warns you about that before you run.",
    ),
    (
        ("download a report", "download report", "word document", "word doc", "docx",
         "what changed", "generate a report", "monthly report", "reports page",
         "reports and summaries", "export a summary"),
        "Reports & Summaries",
        "Pick a month, pick which programmes to cover, and download a **Word** or **Markdown** "
        "report. It shows how each KPI moved with % change, plus detail tables of exactly which "
        "course, language, status and company.\n\n"
        "It's a short summary by design — the full KPI pack lives in the **Enablement KPIs** "
        "SharePoint folder, which the report links to.",
    ),
    (
        ("screenshot", "screenshots", "sign in", "signin", "log in", "login", "logged in",
         "authenticate", "sso", "mfa", "expired session", "session expired", "capture the dashboard",
         "capture fails"),
        "Dashboard screenshots and sign-in",
        "Capturing the real dashboard tiles needs a saved Databricks browser session. There's a "
        "**Sign in to Databricks** button inside *Presentation Update* and *Full Monthly Run* — it "
        "opens a real browser for SSO + MFA (which can't be automated) and saves the session "
        "itself. No terminal needed. Sessions expire, so re-run it if capture starts failing.",
    ),
    (
        ("sharepoint", "where is the deck", "where are the files", "where are files stored",
         "where is everything", "which folder", "volume path", "dashboard link",
         "dashboard links", "link to the dashboard", "genie space", "where do i find"),
        "Where everything lives",
        "The **Overview** page has a *Where everything lives* section with links to the Enablement "
        "KPIs SharePoint folder, all four dashboards, the Genie space, and the bronze/gold schemas. "
        "Raw source files are archived under "
        "`" + VOLUME_ROOT + "` and are never deleted.",
    ),
    (
        ("reporting month", "month format", "month tag", "tagged with", "hyphen", "dot or hyphen",
         "date format", "period column", "which month is used"),
        "Reporting months",
        "New rows are tagged with the reporting month, and the app **matches the format that table "
        "already uses** — `analytics_teams_listing` stores `2026-06` with a hyphen while the others "
        "use `2026.06` with a dot. Existing rows are never rewritten, so no dashboard filter "
        "splits.",
    ),
]


# Words that mean "compute something over the rows". If a question contains any of these it is a
# DATA question and must go to Genie, even if it also mentions an app concept — "how many people are
# in the Guild after June?" is not a question about load order. This veto is what stops the local
# knowledge base from hijacking real queries.
# Matched on WORD BOUNDARIES, not as substrings: "count" as a substring also matches "country",
# which sent "how is country derived?" to Genie. Likewise "sum" is inside "summary".
_DATA_SIGNALS = (
    "how many", "how much", "count", "counts", "total", "totals", "sum", "average", "averages",
    "avg", "median", "percentage", "percent", "top", "most", "least", "highest", "lowest", "rank",
    "ranked", "per country", "per company", "per month", "per course", "breakdown by", "group by",
    "list all", "show me the", "which people", "who", "attendance", "compare", "trend",
)
_DATA_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in _DATA_SIGNALS) + r")\b"
)

# Common typos/shorthand seen in real use, normalised before matching. "thos"/"tihs" for "this" is
# the one that sent "how thos app works?" to Genie.
_TYPOS = {
    "thos": "this", "tihs": "this", "htis": "this", "ths": "this", "tis": "this",
    "wroks": "works", "wrok": "work", "owrks": "works", "workd": "works",
    "ap": "app", "aap": "app", "appp": "app",
    "hwo": "how", "hoe": "how", "waht": "what", "wht": "what", "wat": "what",
    "dose": "does", "doe": "does", "od": "of",
    "dashbaord": "dashboard", "dashbroad": "dashboard", "dahsboard": "dashboard",
    "presntation": "presentation", "presentaton": "presentation", "pesentation": "presentation",
    "databrikcs": "databricks", "datbaricks": "databricks", "databrciks": "databricks",
    "donwload": "download", "downlaod": "download", "sumamry": "summary", "sumary": "summary",
    "repoort": "report", "reprot": "report", "wrod": "word",
    "uplaod": "upload", "upalod": "upload", "delte": "delete", "delet": "delete",
}


def _normalise(question: str) -> str:
    """Lowercase, strip punctuation, fix common typos. Returns a space-padded token stream."""
    words = re.sub(r"[^a-z0-9 ]+", " ", question.lower()).split()
    return " " + " ".join(_TYPOS.get(w, w) for w in words) + " "


def _phrase_score(trigger: str, text: str) -> int:
    """Score one trigger against the normalised text.

    A multi-word trigger matches if all its words appear IN ORDER, allowing up to two filler words
    between them — so "how does this work" still matches "how does this app work", which literal
    substring matching missed. Exact adjacency scores higher so the most specific topic wins.
    """
    if trigger in text:
        return 3
    parts = trigger.split()
    if len(parts) == 1:
        return 2 if f" {parts[0]} " in text else 0
    pattern = r"\s+(?:\w+\s+){0,2}".join(re.escape(p) for p in parts)
    return 2 if re.search(rf"\s{pattern}\s", text) else 0


def answer_about_app(question: str) -> tuple[str, str] | None:
    """Answer a question about the APP from local knowledge, or None if it isn't one.

    Returns None for anything data-shaped so it reaches Genie. Otherwise scores each topic and
    requires a real signal — a single weak word is not enough to claim the question.
    """
    text = _normalise(question)

    # STRUCTURAL questions first — "what tables are there", "which columns", "what is the DLMV".
    # These must be checked BEFORE the data veto: "how many tables are there" contains "how many" but
    # is a question about the schema, and Genie answers it with either a guess or a flat refusal.
    for triggers, title, body_fn in CODE_KNOWLEDGE:
        if max(_phrase_score(t, text) for t in triggers) >= 2:
            try:
                return (title, body_fn())
            except Exception:  # noqa: BLE001 — never let a knowledge helper break the chat
                break

    # A data question always goes to Genie, whatever else it mentions.
    if _DATA_RE.search(text):
        return None

    best: tuple[int, str, str] | None = None
    for triggers, title, body in APP_KNOWLEDGE:
        score = max(_phrase_score(t, text) for t in triggers)
        # Sum a small bonus for additional matching triggers, so "can it delete or overwrite my
        # data?" beats a topic matched by one incidental word.
        extra = sum(1 for t in triggers if _phrase_score(t, text) >= 2) - 1
        score += max(0, extra)
        if score >= 2 and (best is None or score > best[0]):
            best = (score, title, body)
    return (best[1], best[2]) if best else None


# ── Structural knowledge, generated FROM THE CODE ───────────────────────────────────────────────
# Genie writes SQL over ROWS. It cannot answer "what tables are there", "which columns does the Guild
# have", or "what is the DLMV" — it either guesses or returns "your question is unrelated to the
# database schema", which is what the user saw. Those are answered here instead.
#
# Built by reading `excel_ingestion.ROUTES`, `learning_metadata.REGISTRY` and `derivation` at call
# time rather than by hand: a hardcoded table list would drift from the routing the moment a route
# changes, and a confidently wrong schema answer is worse than no answer.


def _schema_answer() -> str:
    """The table inventory, grouped by how it is written."""
    from excel_ingestion import ROUTES, CUMULATIVE_MANUAL, WriteMode
    import learning_metadata as lm

    # Excluded on purpose:
    #  * `analytics_event_number_N` — a placeholder, not a table (the number comes from the filename).
    #  * `fake_company_data_*` — DEMO/revenue test data. The user was explicit that it must never be
    #    presented as a Analytics KPI, so it does not belong in an inventory the chat reads out.
    #  * bare `atlas` / `beacon` — fallback routes for a learning file whose course can't be
    #    identified. No such table exists; naming them would send someone looking for it.
    hidden = {"analytics_event_number_N", "atlas", "beacon"}
    by_mode: dict[str, list[str]] = {}
    for _, route in ROUTES:
        if not route.table or route.table in hidden or route.table.startswith("fake_company"):
            continue
        label = route.mode.value.replace("_", " ")
        tag = f"`{route.table}`"
        if route.table in CUMULATIVE_MANUAL:
            tag += " *(notebook-owned)*"
        by_mode.setdefault(label, []).append(tag)
    lines = [f"- **{mode}** — {', '.join(sorted(set(tables)))}"
             for mode, tables in sorted(by_mode.items())]
    per_course = len(lm.REGISTRY)
    return (
        "Bronze is `" + BRONZE_FQN + "`, gold is `" + GOLD_FQN + "`.\n\n"
        + "\n".join(lines)
        + f"\n\nPlus **{per_course} per-course learning tables** that get a `_YYYY_MM` suffix per "
        "period (`atlas_…`, `beacon_elearning_…`, `plantrack_…`), and the two unified "
        "`*_training_completions` tables they are combined into.\n\n"
        "Ask me for row counts or figures and I'll query Databricks for the real numbers."
    )


def _columns_answer() -> str:
    """Which columns each source actually has, and which the app derives."""
    return (
        "The shapes that matter:\n\n"
        "- **Atlas / Beacon per-course** — only `Company`, `Status`, `Country`. **No email at all**, "
        "so no individual-level joins; the platform/course/language live in the *table name*.\n"
        "- **PlanTrack** — `Company`, `Learning_Name_Module`, `Country`, `Status`. Country is in "
        "**German** and stays German in bronze.\n"
        "- **Unified completions** — the 3 columns above plus `platform`, `course_name`, `language`.\n"
        "- **Teams listing / Guild** — the export has `User` (the email), `Name`, `Role`. "
        "`Country` and `Company` are derived from the email domain; the Guild also gets "
        "`Is_Community_Registered` and `Events_Attended` calculated after the load.\n"
        "- **Community / registrations** — Forms exports, with `Email` and a `Company` that keeps its "
        "bracket code like `(AT)`.\n\n"
        "Anything the source is missing is looked up, then derived, then left empty — never invented."
    )


def _glossary_answer() -> str:
    return (
        "- **Bronze / gold** — the bronze schema holds the raw-but-cleaned rows; the gold schema holds "
        "curated tables (currently `databricks_trainings_participants`).\n"
        "- **Volume** — the Unity Catalog file archive under "
        "`" + VOLUME_ROOT + "`. Every upload is stored there first and **never "
        "deleted**.\n"
        "- **DLMV** — the dashboard's unified learning view: it UNIONs the per-course tables, stamps "
        "platform/course/language, normalises German country names to English and strips the company "
        "bracket for display.\n"
        "- **reporting_month** — the `YYYY.MM` tag that says which period a row belongs to, so every "
        "month coexists in one table. The Guild spells it `Reporting_Month`.\n"
        "- **Atlas / Beacon / PlanTrack** — the three learning platforms. Atlas and Beacon each run "
        "the same courses in English and German; PlanTrack carries its own course column."
    )


def _domains_answer() -> str:
    from derivation import DOMAIN_COMPANY, DOMAIN_COUNTRY
    from org_profile import DEFAULT_COUNTRY

    n_countries = len(set(DOMAIN_COUNTRY.values()))
    return (
        f"{len(DOMAIN_COUNTRY)} email domains are mapped across {n_countries} countries, "
        f"{len(DOMAIN_COMPANY)} of which also resolve the company name — so one address gives both.\n\n"
        "The mapping lives in `config/org_profile.toml`, so it can be changed without touching code. "
        f"An unmapped domain defaults to {DEFAULT_COUNTRY} / 'Unknown'. External consultants are "
        "matched on the company *name*, because their address may be a client's or a personal one."
    )


# Question triggers → the generator that answers them. Same matching machinery as APP_KNOWLEDGE.
CODE_KNOWLEDGE: list[tuple[tuple[str, ...], str, "callable"]] = [
    (("what tables", "which tables", "list the tables", "table inventory", "how many tables",
      "what data is there", "what is in databricks", "which schema", "what schema",
      "what catalog", "bronze schema", "gold schema", "table list"),
     "The tables", _schema_answer),
    (("what columns", "which columns", "what fields", "schema of", "structure of",
      "columns does", "what is in the table"),
     "The columns", _columns_answer),
    (("what is a volume", "what is the dlmv", "what is dlmv", "what does bronze mean",
      "what is bronze", "what is gold", "glossary", "what does reporting month mean",
      "what is atlas", "what is beacon", "what is plantrack", "what platforms",
      "explain the terms"),
     "Glossary", _glossary_answer),
    (("which domains", "what domains", "domain mapping", "how many domains",
      "list the domains", "which countries are mapped", "domain list"),
     "Email domain mapping", _domains_answer),
]


SUGGESTED_QUESTIONS = [
    # Instant, answered from the code — a good first click because it needs no warehouse.
    "What tables are there?",
    "Which columns do the Atlas tables have?",
    # These go to Genie and query the live rows.
    "How many Atlas learning records are there, and how many passed?",
    "Which companies have the most completed learnings?",
    "How many community members are there per country?",
    "What was the attendance at each Analytics event?",
]


@dataclass
class Answer:
    text: str = ""
    sql: str = ""
    description: str = ""
    rows: list[list[str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    conversation_id: str | None = None
    error: str = ""
    about_app: bool = False   # answered from local knowledge, not Genie
    cached: bool = False      # served from the answer cache


_CLIENT = None
_ANSWER_CACHE: dict[str, Answer] = {}


def _client(profile: str = DATABRICKS_PROFILE):
    """One reused WorkspaceClient. Rebuilding it per question cost ~0.7s each time."""
    global _CLIENT
    if _CLIENT is None:
        from databricks.sdk import WorkspaceClient

        _CLIENT = WorkspaceClient(profile=profile)
    return _CLIENT


def _friendly_error(raw: str) -> str:
    """Turn an SDK exception into something actionable, keeping the original for diagnosis.

    A raw `ValueError: default auth: databricks-cli: cannot get access token…` traceback in the chat
    bubble tells the user nothing about what to do. The commonest causes each have a clear fix.
    """
    low = raw.lower()
    if "access token" in low or "auth" in low or "refresh token" in low or "401" in low:
        return ("Not signed in to Databricks any more — the session token expired. Run "
                f"`databricks auth login --profile {DATABRICKS_PROFILE}`, then ask again.\n\n"
                f"<small>{raw[:160]}</small>")
    if "timeout" in low or "timed out" in low:
        return ("That took longer than Genie allows. The SQL warehouse was probably cold — the first "
                "question after an idle period is the slow one. Try again.")
    if "permission" in low or "403" in low or "does not have" in low:
        return ("Databricks refused the query — your account may not have access to that table. "
                f"\n\n<small>{raw[:160]}</small>")
    if "space" in low and "not found" in low:
        return ("The Genie space id no longer resolves. Check `DEFAULT_SPACE_ID` in `genie_chat.py` "
                "against the space in Databricks.")
    return raw


def _cache_key(question: str, conversation_id: str | None) -> str:
    # Only cache FIRST questions: a follow-up's meaning depends on conversation history.
    return f"{'' if conversation_id else 'new'}::{question.strip().lower()}"


def warm_up() -> None:
    """Build the client and wake the warehouse in the background, so the first question is faster.

    Measured: `start_conversation_and_wait` takes ~27s cold and a follow-up ~18s. Almost all of that
    is Genie planning + warehouse execution, which we cannot shortcut — but the warehouse start is
    part of it, and that we CAN do before the user types.
    """
    try:
        w = _client()
        w.genie.get_space(DEFAULT_SPACE_ID)  # cheap call: authenticates + warms the connection
    except Exception:  # noqa: BLE001 — purely opportunistic
        pass


def ask(
    question: str,
    space_id: str = DEFAULT_SPACE_ID,
    conversation_id: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    fetch_rows: bool = True,
) -> Answer:
    """Answer a question — locally if it's about the app, otherwise via Genie.

    Questions about how the app behaves are answered from APP_KNOWLEDGE instantly and offline: Genie
    only knows the Databricks tables and would either fail or guess. Data questions go to Genie,
    continuing `conversation_id` so follow-ups keep context.
    """
    local = answer_about_app(question)
    if local:
        title, body = local
        return Answer(text=f"**{title}**\n\n{body}", about_app=True,
                      conversation_id=conversation_id)

    # Repeat questions return instantly. Safe because the underlying tables only change when a load
    # runs, and a load clears this cache (see clear_cache()).
    ck = _cache_key(question, conversation_id)
    hit = _ANSWER_CACHE.get(ck)
    if hit is not None:
        cached = Answer(**{**hit.__dict__})
        cached.cached = True
        return cached

    # Genie occasionally returns MessageStatus.FAILED for a question it answers fine on a second
    # attempt (observed: the same "per country" question failed once, then succeeded twice). Retry
    # once before reporting an error, otherwise the chat looks broken when it isn't.
    msg = None
    last_error = ""
    for attempt in (1, 2):
        try:
            w = _client()
            timeout = timedelta(seconds=timeout_s)
            if conversation_id:
                msg = w.genie.create_message_and_wait(
                    space_id, conversation_id, question, timeout=timeout
                )
            else:
                msg = w.genie.start_conversation_and_wait(space_id, question, timeout=timeout)
            break
        except Exception as exc:  # noqa: BLE001 — surface it in the UI, never crash the page
            last_error = str(exc)[:300]
            if attempt == 2:
                return Answer(error=_friendly_error(last_error))
    if msg is None:
        return Answer(error=_friendly_error(last_error) if last_error
                      else "Genie didn't respond in time. The SQL warehouse may still be starting — "
                           "try again in a moment.")

    out = Answer(conversation_id=msg.conversation_id)
    for att in msg.attachments or []:
        if getattr(att, "text", None) and att.text.content:
            out.text = (out.text + "\n\n" + att.text.content).strip()
        query = getattr(att, "query", None)
        if query:
            out.sql = query.query or out.sql
            out.description = query.description or out.description
            # Pull the result table so the answer shows the numbers, not just prose.
            if fetch_rows and att.attachment_id:
                try:
                    res = w.genie.get_message_attachment_query_result(
                        space_id, msg.conversation_id, msg.id, att.attachment_id
                    )
                    sr = getattr(getattr(res, "statement_response", None), "result", None)
                    manifest = getattr(
                        getattr(res, "statement_response", None), "manifest", None
                    )
                    if manifest and manifest.schema and manifest.schema.columns:
                        out.columns = [c.name or "" for c in manifest.schema.columns]
                    if sr and sr.data_array:
                        out.rows = [[("" if v is None else str(v)) for v in r]
                                    for r in sr.data_array[:200]]
                except Exception:  # noqa: BLE001 — the prose answer is still useful without rows
                    pass
    if not out.text and not out.sql:
        out.error = out.error or f"Genie returned no answer (status {msg.status})."
    if not out.error:
        _ANSWER_CACHE[ck] = out
    return out


def clear_cache() -> None:
    """Drop cached answers — call after a load, so figures reflect the new data."""
    _ANSWER_CACHE.clear()
