"""Shared UI for the two-part pipeline app: the dark "Analytics Wiki" theme plus the small helpers
both pages use.

The app is split into two programs that run INDEPENDENTLY (see app.py):
  Part 1 — pages/1_Load_to_Databricks.py : Excel → UC Volume archive → clean/derive → append rows
  Part 2 — pages/2_Update_Presentation.py: detect new data → refresh dashboards → update the deck

Everything visual lives here so both pages look identical without duplicating the CSS.
"""

from __future__ import annotations

import html
import io
import threading
from pathlib import Path

import streamlit as st

BRAND_RED = "#E4002B"

BASE = Path(__file__).parent
INPUT_DIR = BASE / "presentations" / "input"
OUTPUT_DIR = BASE / "presentations" / "output"
CHART_DIR = BASE / "charts" / "output"
EXCEL_DIR = BASE / "excel_input"
# Dedicated hand-off folder: the final, correctly-named deck always lands here, ready to be
# copied into the SharePoint "Enablement KPIs/2026" library (no local SharePoint sync exists).
SHAREPOINT_DIR = BASE / "sharepoint_upload"


def ensure_dirs() -> None:
    for folder in [INPUT_DIR, OUTPUT_DIR, CHART_DIR, SHAREPOINT_DIR, EXCEL_DIR, BASE / "templates"]:
        folder.mkdir(parents=True, exist_ok=True)


CSS = """
<style>
:root{--brand-accent:#E4002B;--brand-accent-70:#ef4d68;--bg:#0b0d12;--panel:#151922;--panel-2:#1b2130;
  --border:#262d3b;--text:#e8ecf4;--muted:#95a1b8;--ok:#39d98a;--warn:#f5b84b;}
.stApp{background:var(--bg);color:var(--text);}
/* Kill the default white Streamlit header/toolbar bar at the top */
header[data-testid="stHeader"]{background:transparent !important;height:0 !important;}
div[data-testid="stToolbar"]{display:none !important;}
#MainMenu,footer{visibility:hidden;}
.block-container{padding-top:1.2rem;max-width:1200px;}
section[data-testid="stSidebar"]{background:#0e1117;border-right:1px solid var(--border);}
/* Streamlit's auto page-nav renders near-invisible on a dark sidebar (its default link colour is
   a dim grey). Force readable text + a clear active state, and give the links real hit areas. */
section[data-testid="stSidebarNav"]{padding-top:.4rem;}
section[data-testid="stSidebarNav"] a,
section[data-testid="stSidebarNav"] a span,
div[data-testid="stSidebarNav"] a,
div[data-testid="stSidebarNav"] a span{color:var(--text) !important;opacity:1 !important;
  font-weight:650 !important;font-size:.95rem !important;}
section[data-testid="stSidebarNav"] a,div[data-testid="stSidebarNav"] a{
  border-radius:10px;padding:.5rem .6rem !important;margin:.15rem 0;border:1px solid transparent;}
section[data-testid="stSidebarNav"] a:hover,div[data-testid="stSidebarNav"] a:hover{
  background:var(--panel-2) !important;border-color:var(--border);}
section[data-testid="stSidebarNav"] a[aria-current="page"],
div[data-testid="stSidebarNav"] a[aria-current="page"]{
  background:rgba(228,0,43,.16) !important;border-color:rgba(228,0,43,.45);}
section[data-testid="stSidebarNav"] a[aria-current="page"] span{color:#fff !important;}
/* The collapse chevron and sidebar text generally */
section[data-testid="stSidebar"] *{color:var(--text);}
section[data-testid="stSidebar"] .side-step{color:var(--muted);}
.app-title{font-size:2.3rem;font-weight:850;color:var(--text);letter-spacing:-.02em;}
.app-title .accent{background:linear-gradient(90deg,var(--brand-accent),var(--brand-accent-70));
  -webkit-background-clip:text;background-clip:text;color:transparent;}
.subtitle{font-size:1rem;color:var(--muted);margin-bottom:1.6rem;}
.section{border:1px solid var(--border);border-radius:16px;padding:1.2rem 1.35rem;margin-bottom:1.1rem;
  background:var(--panel);box-shadow:0 6px 24px rgba(0,0,0,.28);}
.step-title{font-weight:800;color:var(--text);margin-bottom:.85rem;font-size:1.06rem;display:flex;align-items:center;}
.step-num{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;
  background:var(--brand-accent);color:#fff;border-radius:50%;font-size:.82rem;margin-right:10px;font-weight:800;}
.file-row{display:flex;gap:14px;align-items:center;margin:.7rem 0;padding:.75rem .9rem;border:1px solid var(--border);
  border-radius:12px;background:var(--panel-2);}
.file-icon{font-size:1.5rem;}.file-name{font-weight:700;color:var(--text);}
.file-size{color:var(--muted);font-size:.83rem;}
.kpi-card{border:1px solid var(--border);border-radius:14px;padding:1rem;background:var(--panel-2);min-height:120px;}
.kpi-title{font-weight:700;color:var(--muted);font-size:.86rem;}
.kpi-value{font-size:1.7rem;font-weight:850;color:var(--text);margin-top:.3rem;}
.small-note{font-size:.83rem;color:var(--muted);}
.ok{color:var(--ok);font-weight:700;}.warn{color:var(--warn);font-weight:700;}
.brand{font-size:1.2rem;font-weight:850;color:var(--text);line-height:1.25;margin:6px 0 22px;}
.brand .accent{color:var(--brand-accent);}
.side-heading{font-weight:800;color:var(--text);margin:18px 0 10px;font-size:.95rem;}
.side-step{color:var(--muted);font-size:.9rem;margin:9px 0;}
.side-step .n{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;
  background:var(--brand-accent);color:#fff;border-radius:50%;font-size:.72rem;margin-right:8px;}
.help{background:var(--panel-2);border:1px solid var(--border);border-radius:12px;padding:14px;
  color:var(--muted);font-size:.86rem;margin-top:22px;}
.plan-box{border:1px solid var(--border);border-left:3px solid var(--brand-accent);border-radius:10px;
  padding:.8rem 1rem;margin:.5rem 0;background:var(--panel-2);font-size:.9rem;}
.plan-box b{color:var(--text);}
.mode-tag{display:inline-block;padding:2px 9px;border-radius:20px;font-size:.72rem;font-weight:800;
  background:rgba(228,0,43,.16);color:var(--brand-accent-70);border:1px solid rgba(228,0,43,.35);}
/* A bounded sub-zone inside a section, so grouped link sets read as separate blocks instead of
   one continuous wall of buttons. */
.zone{border:1px solid var(--border);border-radius:12px;padding:.9rem 1rem .55rem;
  margin:.55rem 0 .9rem;background:rgba(255,255,255,.015);}
.zone-title{font-weight:800;font-size:.9rem;color:var(--text);margin-bottom:.15rem;
  display:flex;align-items:center;gap:.45rem;}
.zone-title .dot{width:7px;height:7px;border-radius:50%;background:var(--brand-accent);
  display:inline-block;}
.zone-sub{font-size:.8rem;color:var(--muted);margin-bottom:.6rem;}
.part-card{border:1px solid var(--border);border-radius:16px;padding:1.3rem;background:var(--panel);
  min-height:210px;}
/* No grey flash while a page is switching: Streamlit dims "stale" elements during a rerun. */
[data-stale="true"],.stale-element,[data-testid="stAppViewContainer"] [data-stale="true"]{
  opacity:1 !important;transition:none !important;filter:none !important;}
div[data-testid="stSkeleton"],div[data-testid="stAppSkeleton"]{display:none !important;}
/* Whole-card links: the entire card is the click target — no button, no "Open" label. The card
   itself signals interactivity: red glow + lift on hover, and it presses IN on click. */
a.card-link{display:block;text-decoration:none !important;color:inherit !important;height:100%;
  -webkit-tap-highlight-color:transparent;}
a.card-link .part-card{height:100%;cursor:pointer;position:relative;overflow:hidden;
  transition:border-color .18s,background .18s,transform .12s,box-shadow .18s;}
a.card-link:hover .part-card{border-color:var(--brand-accent);background:var(--panel-2);
  transform:translateY(-3px);
  box-shadow:0 12px 32px rgba(0,0,0,.45),0 0 0 1px var(--brand-accent),
             0 0 28px rgba(228,0,43,.35);}
a.card-link:hover .part-card h3{color:#fff;}
a.card-link:hover .part-card .num{background:var(--brand-accent);color:#fff;
  border-color:var(--brand-accent);}
/* A red sheen sweeps across on hover so it reads as "live" */
a.card-link .part-card::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(120deg,transparent 40%,rgba(228,0,43,.10) 50%,transparent 60%);
  opacity:0;transition:opacity .25s;}
a.card-link:hover .part-card::after{opacity:1;}
/* Pressed state: sinks in and flashes brighter, so the click is unmistakable */
a.card-link:active .part-card{transform:translateY(0) scale(.985);
  background:rgba(228,0,43,.16);
  box-shadow:inset 0 2px 10px rgba(0,0,0,.5),0 0 0 2px var(--brand-accent);}
a.card-link:focus-visible .part-card{outline:none;border-color:var(--brand-accent);
  box-shadow:0 0 0 3px rgba(228,0,43,.45);}
a.card-link .part-card h3,a.card-link .part-card li,a.card-link .part-card b{color:inherit;}
.part-card h3{margin:0 0 .5rem;font-size:1.1rem;color:var(--text);}
.part-card .num{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.72rem;font-weight:800;
  background:rgba(228,0,43,.16);color:var(--brand-accent-70);border:1px solid rgba(228,0,43,.35);margin-bottom:.6rem;}
.part-card ul{margin:.4rem 0 0 1.1rem;padding:0;color:var(--muted);font-size:.88rem;}
.flagbox{border:1px solid rgba(245,184,75,.4);border-left:3px solid var(--warn);border-radius:10px;
  padding:.7rem .95rem;margin:.45rem 0;background:rgba(245,184,75,.07);font-size:.87rem;color:var(--text);}
div.stButton>button,div.stDownloadButton>button{background:var(--brand-accent);color:#fff;border:none;
  border-radius:10px;font-weight:750;width:100%;padding:.7rem;}
div.stButton>button:hover,div.stDownloadButton>button:hover{background:#c40026;color:#fff;}
div.stButton>button:disabled{background:#3a2530;color:#9b8;}
code{color:var(--brand-accent-70);background:var(--panel-2);}
[data-testid="stMarkdownContainer"] code,[data-testid="stChatMessage"] code{color:var(--brand-accent-70) !important;background:var(--panel-2) !important;}
.stCode,pre{background:#0e1117 !important;border:1px solid var(--border);border-radius:10px;}

/* ── Widgets Streamlit renders WHITE by default on a dark background ───────────────────────── */
/* File uploader (the big white box) */
section[data-testid="stFileUploaderDropzone"],div[data-testid="stFileUploaderDropzone"]{
  background:var(--panel-2) !important;border:1px dashed var(--border) !important;
  border-radius:12px !important;color:var(--text) !important;}
section[data-testid="stFileUploaderDropzone"] *,div[data-testid="stFileUploaderDropzone"] *{
  color:var(--text) !important;}
section[data-testid="stFileUploaderDropzone"] button,
div[data-testid="stFileUploaderDropzone"] button{
  background:var(--brand-accent) !important;color:#fff !important;border:none !important;
  border-radius:8px !important;width:auto !important;padding:.45rem 1rem !important;}
div[data-testid="stFileUploaderFile"]{background:var(--panel-2) !important;color:var(--text) !important;
  border:1px solid var(--border);border-radius:10px;}
div[data-testid="stFileUploaderFile"] *{color:var(--text) !important;}
[data-testid="stFileChip"]{background:var(--panel-2) !important;border:1px solid var(--border) !important;}
/* Dataframes / tables. The grid is a canvas-backed glide-data-grid with its OWN light theme, so
   the container styling alone leaves it white — these variables recolour the grid itself. */
div[data-testid="stDataFrame"],div[data-testid="stTable"]{background:var(--panel-2) !important;
  border:1px solid var(--border);border-radius:10px;}
div[data-testid="stDataFrame"] *,div[data-testid="stTable"] *{color:var(--text) !important;}
div[data-testid="stTable"] th{color:var(--muted) !important;}
div[data-testid="stTable"] table{background:transparent !important;}
div[data-testid="stDataFrame"] .gdg-cell,div[data-testid="stDataFrame"] .dvn-scroller{
  background:var(--panel-2) !important;}
div[data-testid="stDataFrame"]{
  --gdg-bg-cell:#1b2130;--gdg-bg-cell-medium:#1f2636;--gdg-bg-header:#151922;
  --gdg-bg-header-hovered:#20283a;--gdg-bg-header-has-focus:#20283a;
  --gdg-text-dark:#e8ecf4;--gdg-text-medium:#c7cfdd;--gdg-text-light:#95a1b8;
  --gdg-text-header:#95a1b8;--gdg-text-header-selected:#ffffff;
  --gdg-border-color:#262d3b;--gdg-atlasal-border-color:#262d3b;
  --gdg-accent-color:#E4002B;--gdg-accent-light:rgba(228,0,43,.14);
  --gdg-bg-bubble:#1b2130;--gdg-bg-search-result:rgba(228,0,43,.22);}
/* Alerts (info/warning/success/error) */
div[data-testid="stAlert"]{background:var(--panel-2) !important;border:1px solid var(--border);
  border-radius:10px;color:var(--text) !important;}
div[data-testid="stAlert"] *{color:var(--text) !important;}
/* Expanders. The SUMMARY row is painted white by Streamlit and needs its own rule — theming the
   details element alone leaves a white header bar. */
details,div[data-testid="stExpander"],div[data-testid="stExpander"] > details{
  background:var(--panel-2) !important;border:1px solid var(--border) !important;
  border-radius:10px !important;}
div[data-testid="stExpander"] *,details summary{color:var(--text) !important;}
details > summary,div[data-testid="stExpander"] summary,
div[data-testid="stExpanderHeader"],div[data-testid="stExpanderDetails"]{
  background:var(--panel-2) !important;color:var(--text) !important;border-radius:10px !important;}
details > summary:hover{background:var(--panel) !important;}
div[data-testid="stExpander"] svg{fill:var(--text) !important;color:var(--text) !important;}
/* Text/number inputs + selects */
div[data-baseweb="input"],div[data-baseweb="select"]>div,input,textarea{
  background:var(--panel-2) !important;color:var(--text) !important;
  border-color:var(--border) !important;}
div[data-baseweb="popover"],ul[data-baseweb="menu"]{background:var(--panel-2) !important;
  border:1px solid var(--border) !important;}
ul[data-baseweb="menu"] li{color:var(--text) !important;}
/* Spinner + progress + tooltips */
div[data-testid="stSpinner"] *{color:var(--muted) !important;}
div[data-testid="stProgress"] div[role="progressbar"]>div{background:var(--brand-accent) !important;}
div[data-baseweb="tooltip"],div[role="tooltip"]{background:#000 !important;color:#fff !important;}
/* Streamlit's own "Deploy"/status widgets that flash white */
div[data-testid="stStatusWidget"]{background:var(--panel-2) !important;color:var(--text) !important;}
div[data-testid="stNotificationContentInfo"],
div[data-testid="stNotificationContentWarning"],
div[data-testid="stNotificationContentSuccess"],
div[data-testid="stNotificationContentError"]{color:var(--text) !important;}
/* The nav's auto-generated "app" entry is renamed via st.Page titles; keep it tidy regardless */
section[data-testid="stSidebarNav"] ul{padding-left:0 !important;}

/* st.page_link renders as dim grey text — make it an obvious clickable button */
div[data-testid="stPageLink"] a,a[data-testid="stPageLink-NavLink"]{
  display:flex !important;align-items:center;justify-content:center;
  background:var(--brand-accent) !important;border-radius:10px !important;
  padding:.65rem .9rem !important;margin-top:.2rem;text-decoration:none !important;
  border:none !important;transition:background .15s;}
div[data-testid="stPageLink"] a *,a[data-testid="stPageLink-NavLink"] *{
  color:#fff !important;opacity:1 !important;font-weight:750 !important;font-size:.95rem !important;}
div[data-testid="stPageLink"] a:hover,a[data-testid="stPageLink-NavLink"]:hover{
  background:#c40026 !important;}

/* ── Chat: the input bar sits in a fixed bottom bar that Streamlit paints WHITE, and the message
   bubbles default to light grey. Recolour the whole surface. ───────────────────────────────── */
div[data-testid="stBottom"],div[data-testid="stBottomBlockContainer"],
div[data-testid="stBottom"] > div{background:var(--bg) !important;}
div[data-testid="stChatInput"],div[data-testid="stChatInput"] > div{
  background:var(--panel-2) !important;border:1px solid var(--border) !important;
  border-radius:12px !important;}
div[data-testid="stChatInput"] textarea{background:transparent !important;color:var(--text) !important;}
div[data-testid="stChatInput"] textarea::placeholder{color:var(--muted) !important;}
div[data-testid="stChatInput"] button{background:var(--brand-accent) !important;border:none !important;}
div[data-testid="stChatInput"] button svg{fill:#fff !important;color:#fff !important;}
div[data-testid="stChatMessage"]{background:var(--panel) !important;
  border:1px solid var(--border) !important;border-radius:12px !important;margin:.4rem 0;}
div[data-testid="stChatMessage"] *{color:var(--text);}
/* The assistant/user avatars come out as bright orange blocks — tone them to the theme */
div[data-testid="stChatMessageAvatarUser"],
div[data-testid="stChatMessageAvatarAssistant"]{background:var(--panel-2) !important;
  border:1px solid var(--border) !important;color:var(--text) !important;}

/* Suggested-question chips: quiet outlined buttons, not loud red bars. A wrapper <div class> does
   NOT survive Streamlit's DOM (each widget is its own container), so scope by ancestor instead:
   any button inside an expander or a popover is a secondary action here.
   Use a DESCENDANT selector, not `div.stButton>button`: when a button has help= set, Streamlit
   wraps it in stTooltipHoverTarget > stTooltipIcon divs, so a direct-child selector silently
   matches nothing and the button renders browser-default white. */
div[data-testid="stExpander"] div[data-testid="stButton"] button,
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button{
  background:var(--panel-2) !important;color:var(--text) !important;
  border:1px solid var(--border) !important;font-weight:600 !important;text-align:left !important;
  justify-content:flex-start !important;padding:.55rem .8rem !important;font-size:.86rem !important;
  box-shadow:none !important;}
div[data-testid="stExpander"] div[data-testid="stButton"] button:hover,
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button:hover{
  border-color:var(--brand-accent) !important;background:rgba(228,0,43,.10) !important;}
/* …except an explicit primary action (the Ask button) stays red */
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button[kind="primary"]{
  background:var(--brand-accent) !important;color:#fff !important;border:none !important;
  text-align:center !important;justify-content:center !important;font-weight:800 !important;}

/* st.link_button — outlined, so it reads as "go elsewhere" rather than "run something" */
div[data-testid="stLinkButton"] a{background:var(--panel-2) !important;color:var(--text) !important;
  border:1px solid var(--border) !important;border-radius:10px !important;font-weight:650 !important;
  padding:.6rem .9rem !important;text-decoration:none !important;}
div[data-testid="stLinkButton"] a:hover{border-color:var(--brand-accent) !important;
  background:rgba(228,0,43,.10) !important;}
div[data-testid="stLinkButton"] a *{color:var(--text) !important;}

/* Multiselect chips (programme picker) render as light-grey pills, and baseweb truncates the text
   with an ellipsis at a fixed width — which made two different "Databricks trainings…" chips look
   identical. Let them size to their content instead. */
span[data-baseweb="tag"]{background:var(--brand-accent) !important;color:#fff !important;
  border-radius:8px !important;max-width:none !important;}
span[data-baseweb="tag"] *{color:#fff !important;}
span[data-baseweb="tag"] span[title],span[data-baseweb="tag"] div{max-width:none !important;
  overflow:visible !important;text-overflow:clip !important;white-space:nowrap !important;}
</style>
"""


def prewarm_warehouse() -> None:
    """Kick the SQL warehouse awake in a BACKGROUND thread, once per session.

    A cold X-Small warehouse takes ~30s to start, and that wait used to land on whatever the user
    clicked first. Firing a trivial query as soon as the app opens means it is usually already
    running by the time it is needed. Never blocks and never raises — purely opportunistic.
    """
    if st.session_state.get("_warehouse_prewarmed"):
        return
    st.session_state["_warehouse_prewarmed"] = True

    def _warm() -> None:
        try:
            # Genie's client build + auth also happens here, so the first question is quicker.
            try:
                from genie_chat import warm_up

                warm_up()
            except Exception:  # noqa: BLE001
                pass
            from databricks_client import _connect

            conn = _connect()
            try:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                finally:
                    cur.close()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — best-effort; the UI must not care
            pass

    threading.Thread(target=_warm, daemon=True).start()


def inject_css() -> None:
    """Call at the top of every page. Page CONFIG is owned by app.py's st.navigation, so this only
    injects the theme (calling set_page_config again from a page would raise)."""
    st.markdown(CSS, unsafe_allow_html=True)
    ensure_dirs()
    prewarm_warehouse()


def apply_theme(page_title: str, icon: str = "📊") -> None:
    """Back-compat for running a page file directly (streamlit run pages/1_....py)."""
    try:
        st.set_page_config(page_title=page_title, page_icon=icon, layout="wide")
    except Exception:  # noqa: BLE001 — already configured by app.py; harmless
        pass
    inject_css()


def sidebar(active: str, steps: list[str]) -> None:
    """Shared sidebar: brand block + the steps of the CURRENT part only."""
    with st.sidebar:
        st.markdown(
            "<div class='brand'>📊 Analytics<br><span class='accent'>KPI Pipeline</span></div>",
            unsafe_allow_html=True,
        )
        st.markdown(f"<div class='side-heading'>{html.escape(active)}</div>", unsafe_allow_html=True)
        for idx, text in enumerate(steps, 1):
            st.markdown(
                f"<div class='side-step'><span class='n'>{idx}</span>{text}</div>",
                unsafe_allow_html=True,
            )


def title(main: str, accent: str, subtitle: str) -> None:
    st.markdown(
        f"<div class='app-title'>{main} <span class='accent'>{accent}</span></div>",
        unsafe_allow_html=True,
    )
    if subtitle:
        st.markdown(f"<div class='subtitle'>{subtitle}</div>", unsafe_allow_html=True)


def section_open(num, title_text: str) -> None:
    st.markdown(
        f"<div class='section'><div class='step-title'><span class='step-num'>{num}</span>{title_text}</div>",
        unsafe_allow_html=True,
    )


def section_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


def zone_open(title: str, subtitle: str = "") -> None:
    """Start a bounded sub-zone inside a section. Pair with zone_close()."""
    sub = f"<div class='zone-sub'>{subtitle}</div>" if subtitle else ""
    st.markdown(
        f"<div class='zone'><div class='zone-title'><span class='dot'></span>{title}</div>{sub}",
        unsafe_allow_html=True,
    )


def zone_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


def card_link(url_path: str, badge: str, heading: str, bullets: list[str],
              min_height: int = 210) -> None:
    """A whole card that IS the link — click anywhere on it to open that page.

    Streamlit's page_link renders a small text link, and an arbitrary container cannot be made
    clickable, so the card is emitted as a real <a> wrapping the card markup. `url_path` must match
    the `url_path=` given to st.Page in app.py.
    """
    items = "".join(f"<li>{b}</li>" for b in bullets)
    st.markdown(
        f"<a class='card-link' href='/{url_path}' target='_self'>"
        f"<div class='part-card' style='min-height:{min_height}px'>"
        f"<span class='num'>{badge}</span>"
        f"<h3>{heading}</h3><ul>{items}</ul>"
        f"</div></a>",
        unsafe_allow_html=True,
    )


def fmt_preview(value):
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def file_row(icon: str, name: str, note: str) -> None:
    st.markdown(
        f"<div class='file-row'><div class='file-icon'>{icon}</div><div>"
        f"<div class='file-name'>{html.escape(name)}</div>"
        f"<div class='file-size'>{html.escape(note)}</div></div></div>",
        unsafe_allow_html=True,
    )


def flag(text_html: str) -> None:
    """Render a data-quality / decision flag (see the data-quality audit)."""
    st.markdown(f"<div class='flagbox'>{text_html}</div>", unsafe_allow_html=True)


def progress_ui():
    """Create the progress bar + status line + log area, and return (step, log_ok, done).

    Both pages need the same live-progress surface. The engines
    (`excel_ingestion.execute_write`, `dashboard_capture.capture_dashboard_images`) stay
    UI-agnostic — they RETURN log lines/warnings, and the caller feeds them to `log_ok`.
    """
    bar = st.progress(0.0, text="Starting…")
    status = st.empty()
    log_area = st.container()

    def step(pct: float, msg: str) -> None:
        bar.progress(min(max(pct, 0.0), 1.0), text=msg)
        status.markdown(
            f"<div class='small-note'>⏳ {html.escape(msg)}</div>", unsafe_allow_html=True
        )

    def log_ok(msg: str) -> None:
        with log_area:
            st.markdown(f"<span class='ok'>✔</span> {html.escape(msg)}", unsafe_allow_html=True)

    def done(msg: str = "Done.") -> None:
        step(1.0, msg)
        status.empty()

    return step, log_ok, done


def deck_tags(file_bytes: bytes, prefix: str) -> list[str]:
    """All `<prefix>:` tag values in a PPTX (e.g. deck_tags(b, "chart") -> ["revenue_by_country"]).

    """
    from pptx import Presentation

    from pptx_refresher import (
        MANUAL_CHART_KEYS,
        MANUAL_SLIDE_NUMBERS,
        detect_tag,
        walk_shapes,
    )

    deck = Presentation(io.BytesIO(file_bytes))
    tags = [
        detect_tag(shape, prefix)
        for slide_number, slide in enumerate(deck.slides, start=1)
        if slide_number not in MANUAL_SLIDE_NUMBERS
        for shape in walk_shapes(slide.shapes)
        if detect_tag(shape, prefix)
    ]
    if prefix == "chart":
        tags = [tag for tag in tags if tag not in MANUAL_CHART_KEYS]
    return tags
