"""A floating "Ask AI" chat, pinned bottom-right on every page.

Rendered as a `st.popover` that CSS moves into a fixed corner bubble, so it is always reachable but
never competes with the page content. Backed by the same Genie space as the full page — see
`genie_chat.py`.

Streamlit has no native floating widget, so the trick is: render the popover LAST on the page and
let CSS pin that element into a fixed corner bubble.
"""

from __future__ import annotations

import html

import streamlit as st
import streamlit.components.v1 as components

from genie_chat import ask

# ── The assistant avatar ────────────────────────────────────────────────────────────────────────
# A sparkle on a red disc. The bar-chart version read as a corporate analytics logo rather than an
# assistant. Kept as vector art rather than the ✨ emoji so it takes the brand colour, renders the
# same on every OS (the emoji showed as a flat yellow blob) and stays sharp at any size.
_MARK_SVG = """
<svg viewBox='0 0 40 40' fill='none' xmlns='http://www.w3.org/2000/svg' aria-hidden='true'>
  <defs>
    <linearGradient id='askG' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0%' stop-color='#ff4d6d'/><stop offset='100%' stop-color='#c40026'/>
    </linearGradient>
  </defs>
  <guild cx='20' cy='20' r='19' fill='url(#askG)'/>
  <guild cx='20' cy='20' r='19' fill='none' stroke='rgba(255,255,255,.30)' stroke-width='1.4'/>
  <!-- a four-point sparkle: the ✨ shape, but as vector art so it takes the brand colour, renders
       identically on every OS, and stays sharp at 24px and 34px -->
  <path d='M20 9.6c1.1 5.6 2.9 7.4 8.5 8.5-5.6 1.1-7.4 2.9-8.5 8.5-1.1-5.6-2.9-7.4-8.5-8.5
           5.6-1.1 7.4-2.9 8.5-8.5z' fill='#fff'/>
  <path d='M27.4 24.0c.5 2.3 1.2 3.0 3.5 3.5-2.3.5-3.0 1.2-3.5 3.5-.5-2.3-1.2-3.0-3.5-3.5
           2.3-.5 3.0-1.2 3.5-3.5z' fill='#fff' fill-opacity='.85'/>
</svg>
"""


def _mark(css_class: str) -> str:
    """The assistant mark wrapped in a sized container."""
    return f"<span class='{css_class}'>{_MARK_SVG}</span>"

WIDGET_CSS = """
<style>
/* ── Support-desk style launcher: an "✨ Ask AI" pill pinned bottom-right, opening a branded panel
   above it. Streamlit has no floating-widget API, so we pin the popover element itself (there is
   exactly one on these pages and it renders last). ────────────────────────────────────────────── */
div[data-testid="stPopover"]:last-of-type{
  position:fixed !important;right:26px;bottom:26px;z-index:9990;width:auto !important;}
/* The trigger: a branded red pill reading "✨ Ask AI". Streamlit's own button styles are more
   specific than a bare class, so every property here needs !important. */
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"],
div[data-testid="stPopover"]:last-of-type button[kind="secondary"]{
  background:linear-gradient(135deg,var(--brand-accent),#ff2d55) !important;color:#fff !important;
  border:none !important;border-radius:30px !important;
  height:56px !important;min-height:56px !important;padding:0 1.35rem !important;
  display:flex !important;align-items:center;justify-content:center;width:auto !important;
  box-shadow:0 10px 30px rgba(0,0,0,.55),0 0 0 6px rgba(228,0,43,.13) !important;
  transition:transform .15s,box-shadow .15s,filter .15s;}
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"]:hover{
  filter:brightness(1.08);transform:translateY(-2px) scale(1.03);
  box-shadow:0 16px 40px rgba(0,0,0,.62),0 0 0 10px rgba(228,0,43,.20) !important;}
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"]:active{transform:scale(.97);}
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"] p,
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"] div,
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"] span{
  font-size:1rem !important;font-weight:800 !important;color:#fff !important;
  line-height:1 !important;margin:0 !important;white-space:nowrap !important;}
/* The pill's own mark. st.popover's label must be a plain string (no HTML), so the glyph is drawn
   as a ::before on the label — three ascending bars, matching the avatar SVG. Target the button by
   its OWN test id: it is not a direct grandchild of stPopover (there are wrapper divs between). */
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"] p::before{
  content:'';display:inline-block;width:15px;height:14px;margin-right:.5rem;
  vertical-align:-2px;background:#fff;
  -webkit-mask:var(--askBars) center/contain no-repeat;
  mask:var(--askBars) center/contain no-repeat;}
:root{--askBars:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cpath d='M13 2c1.4 7 3.6 9.2 10.6 10.6C16.6 14 14.4 16.2 13 23.2 11.6 16.2 9.4 14 2.4 12.6 9.4 11.2 11.6 9 13 2z'/%3E%3Cpath d='M24.6 19c.6 2.9 1.5 3.8 4.4 4.4-2.9.6-3.8 1.5-4.4 4.4-.6-2.9-1.5-3.8-4.4-4.4 2.9-.6 3.8-1.5 4.4-4.4z' opacity='.85'/%3E%3C/svg%3E");}
/* A soft attention pulse so it reads as "chat with us" */
@keyframes askPulse{0%{box-shadow:0 10px 30px rgba(0,0,0,.55),0 0 0 0 rgba(228,0,43,.45);}
  70%{box-shadow:0 10px 30px rgba(0,0,0,.55),0 0 0 16px rgba(228,0,43,0);}
  100%{box-shadow:0 10px 30px rgba(0,0,0,.55),0 0 0 0 rgba(228,0,43,0);}}
div[data-testid="stPopover"]:last-of-type button[data-testid="stPopoverButton"]{animation:askPulse 3.2s ease-out 2;}
/* The panel: a tall support-chat card */
div[data-testid="stPopoverBody"]{background:var(--panel) !important;
  border:1px solid var(--border) !important;border-radius:16px !important;
  box-shadow:0 24px 60px rgba(0,0,0,.7) !important;
  width:min(620px,94vw) !important;max-height:88vh;
  overflow-y:auto;padding:1rem !important;}
div[data-testid="stPopoverBody"] *{color:var(--text);}
/* ── Header: a compact title bar — logo + "AI Assistant" on the left, icon controls on the right,
   separated from the conversation by a hairline. ─────────────────────────────────────────────── */
.ask-head-in{padding:.1rem 0 .1rem;}
.ask-head-in .t{font-weight:850;font-size:1.02rem;display:flex;align-items:center;gap:.5rem;
  color:var(--text);line-height:1.2;}
.ask-head-in .s{font-size:.75rem;color:var(--muted);margin-top:.05rem;}
/* The logo mark in the header — the SVG carries its own colour, so no background here */
.ask-logo{flex:0 0 24px;width:24px;height:24px;display:inline-flex;align-items:center;
  justify-content:center;}
.ask-logo svg{width:24px;height:24px;display:block;
  filter:drop-shadow(0 2px 6px rgba(228,0,43,.5));}
/* Hairline under the whole header row */
.ask-rule{border-top:1px solid var(--border);margin:.15rem 0 .3rem;}
/* Icon controls (⤢ 🗑 ✕): quiet by default, coloured only on hover — like the reference.
   NOTE the DESCENDANT selector. With help= set, Streamlit wraps the button in
   stTooltipHoverTarget > stTooltipIcon divs, so `div[data-testid="stButton"] > button` (a direct
   child) matches NOTHING and the button falls back to the browser default white. */
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button{
  padding:0 !important;min-height:32px !important;height:32px !important;
  border-radius:8px !important;font-size:.95rem !important;}
/* Streamlit renders small icon buttons WHITE by default, so target every button inside the panel.
   Borderless + transparent so they read as icons, not buttons. Descendant selectors throughout —
   see the note above about the tooltip wrapper divs. */
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button{
  background:transparent !important;color:var(--muted) !important;
  border:1px solid transparent !important;box-shadow:none !important;}
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button:hover{
  background:rgba(228,0,43,.16) !important;border-color:var(--brand-accent) !important;
  color:#fff !important;}
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button:focus,
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button:focus-visible,
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button:active{
  background:rgba(228,0,43,.16) !important;box-shadow:none !important;outline:none !important;}
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button p,
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button span,
div[data-testid="stPopoverBody"] div[data-testid="stButton"] button div{color:inherit !important;}
/* The tooltip wrappers themselves must not paint a background either */
div[data-testid="stPopoverBody"] div[data-testid="stTooltipHoverTarget"],
div[data-testid="stPopoverBody"] div[data-testid="stTooltipIcon"]{
  background:transparent !important;}

/* ── Real chat layout. Rather than fight Streamlit's nested wrappers with flex:auto (brittle), the
   TRANSCRIPT gets a fixed scrollable height via st.container(height=…) and the input simply follows
   it — so the input always sits at the foot of the panel and long threads scroll above it. ───── */
div[data-testid="stPopoverBody"] div[data-testid="stTextInput"]{
  padding-top:.6rem;border-top:1px solid var(--border);margin-bottom:0;}
.ask-head .live{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:#7CFFB2;box-shadow:0 0 0 3px rgba(124,255,178,.25);}
/* Assistant avatar + a greeting that types itself in, so opening the panel feels like a chat
   starting rather than a static form. */
.ask-greet{display:flex;gap:.7rem;align-items:flex-start;margin:.2rem 0 .9rem;}
/* Chat avatar. The mark is an SVG guild already, so this is just a sized box — the old version
   painted its own red disc behind the emoji, which double-stacked two different reds. */
.ask-av{flex:0 0 34px;width:34px;height:34px;display:flex;align-items:center;
  justify-content:center;}
.ask-av svg{width:34px;height:34px;display:block;
  filter:drop-shadow(0 3px 10px rgba(228,0,43,.42));}
.ask-bubble{background:var(--panel-2);border:1px solid var(--border);border-radius:4px 12px 12px 12px;
  padding:.65rem .85rem;font-size:.9rem;color:var(--text);max-width:100%;}
/* Type-on effect: the text reveals left-to-right, and nothing is left behind when it finishes.
   Two earlier attempts got this wrong, both for the same underlying reason — the reveal was done by
   animating `width` to `100%`, which resolves against the PARENT BUBBLE (296px) and not the text
   (120px). So the box ended ~176px wider than the sentence, and a caret pinned to its right edge
   sailed off past the full stop.
   The fix: `width:max-content` makes the box hug the text exactly, and the reveal is a clip-path
   wiping in from the left. No caret — with a clipped box a trailing caret can only appear at the
   very end, and a stray red bar was the complaint in the first place. */
.ask-type{display:inline-block;width:max-content;max-width:100%;white-space:nowrap;
  animation:askType 1.15s steps(26,end) forwards;}
@keyframes askType{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
/* The three-dot "assistant is typing" indicator shown while a question is in flight */
.ask-dots span{display:inline-block;width:7px;height:7px;margin-right:4px;border-radius:50%;
  background:var(--brand-accent-70);animation:askDot 1.2s infinite ease-in-out;}
.ask-dots span:nth-child(2){animation-delay:.18s}
.ask-dots span:nth-child(3){animation-delay:.36s}
@keyframes askDot{0%,80%,100%{transform:translateY(0);opacity:.45}
  40%{transform:translateY(-5px);opacity:1}}
.ask-a{background:var(--panel-2);border:1px solid var(--border);border-left:3px solid var(--brand-accent);
  border-radius:10px;padding:.6rem .8rem;margin:.4rem 0;font-size:.88rem;}
/* User's own question, right-aligned like a chat */
.ask-q{background:rgba(228,0,43,.14);border:1px solid rgba(228,0,43,.32);
  border-radius:12px 4px 12px 12px;padding:.5rem .75rem;margin:.9rem 0 .5rem auto;
  font-size:.88rem;color:var(--text);max-width:85%;width:fit-content;}
.ask-sub{color:var(--muted);font-size:.82rem;margin:.35rem 0 .7rem;line-height:1.45;}
/* Scrollable transcript: the panel is a fixed height, so history scrolls inside it instead of
   pushing the input off-screen. */
.ask-hist-title{font-size:.78rem;font-weight:800;color:var(--muted);text-transform:uppercase;
  letter-spacing:.04em;border-top:1px solid var(--border);padding-top:.7rem;margin-top:.4rem;}
div[data-testid="stPopoverBody"] div[data-testid="stTextInput"] input{
  background:var(--panel-2) !important;border:1px solid var(--border) !important;}
/* Inline `code` in an answer. Streamlit gives it a near-WHITE background inside the popover, and the
   panel's own `* {color:var(--text)}` rule then painted near-white text on it — every table name in a
   schema answer came out invisible. Needs !important: Streamlit's rule is more specific than the
   theme's bare `code{}`. */
div[data-testid="stPopoverBody"] code,
div[data-testid="stPopoverBody"] p code,
div[data-testid="stPopoverBody"] li code{
  background:rgba(228,0,43,.12) !important;color:var(--brand-accent-70) !important;
  border:1px solid rgba(228,0,43,.22) !important;border-radius:5px !important;
  padding:.05rem .3rem !important;font-size:.85em !important;}
/* The auto-scroll helper is a components.html iframe. Streamlit still lays out a block for it, which
   would show as a gap under the input — collapse it entirely. */
div[data-testid="stPopoverBody"] iframe[title="streamlit_component_v1"],
div[data-testid="stPopoverBody"] div[data-testid="stCustomComponentV1"]{
  height:0 !important;min-height:0 !important;display:block !important;border:0 !important;
  margin:0 !important;}
div[data-testid="stPopoverBody"] div[data-testid="stElementContainer"]:has(
  iframe[title="streamlit_component_v1"]){height:0 !important;min-height:0 !important;
  margin:0 !important;padding:0 !important;overflow:hidden !important;}
</style>
"""


def _render_thread(pending: str | None = None) -> None:
    """Draw the conversation: greeting when empty, then each exchange oldest-first.

    `pending` is a question that has been asked but not yet answered. It is drawn at the END of the
    transcript — the user's own bubble followed by the typing indicator — so the wait happens where a
    chat's wait belongs: in the thread, above the input.
    """
    if not st.session_state.ask_log and not pending:
        st.markdown(
            f"<div class='ask-greet'>{_mark('ask-av')}"
            "<div class='ask-bubble'><span class='ask-type'>Hi — how can I help?</span><br>"
            "<span class='small-note'>Ask about the KPI data, or about how this app works.</span>"
            "</div></div>",
            unsafe_allow_html=True,
        )
        return

    # Oldest first, so the newest exchange ends up nearest the input — like any chat app.
    for q, a in st.session_state.ask_log[-25:]:
        st.markdown(f"<div class='ask-q'>{html.escape(q)}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='ask-greet' style='margin-bottom:.15rem'>{_mark('ask-av')}"
            "<div style='flex:1'></div></div>",
            unsafe_allow_html=True,
        )
        if a.error:
            st.warning(a.error[:200])
        if a.text:
            # A bordered container so the markdown (bold, lists, code) renders properly — a raw
            # HTML div would print the asterisks literally.
            with st.container(border=True):
                st.markdown(a.text)
                if a.about_app:
                    st.caption("About this app")
                elif a.cached:
                    st.caption("From cache — instant")
        if a.rows and a.columns:
            head_md = "| " + " | ".join(a.columns) + " |"
            sep_md = "|" + "|".join(["---"] * len(a.columns)) + "|"
            body_md = "\n".join("| " + " | ".join(r) + " |" for r in a.rows[:20])
            st.markdown(f"{head_md}\n{sep_md}\n{body_md}")
            if len(a.rows) > 20:
                st.caption(f"First 20 of {len(a.rows)} rows — open Ask the Data for all.")
        if a.sql:
            with st.expander("SQL"):
                st.code(a.sql, language="sql")

    if pending:
        # The question the user just sent, then "…is typing". Both inside the transcript, so the
        # input below stays put and the newest content is what you are looking at.
        st.markdown(f"<div class='ask-q'>{html.escape(pending)}</div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='ask-greet' id='ask-typing'>{_mark('ask-av')}"
            "<div class='ask-bubble'><span class='ask-dots'>"
            "<span></span><span></span><span></span></span>"
            "<span class='small-note' style='margin-left:.45rem'>"
            "Writing SQL and querying Databricks…</span></div></div>",
            unsafe_allow_html=True,
        )


def _scroll_to_bottom() -> None:
    """Keep the newest message in view inside the fixed-height transcript.

    st.container(height=…) renders a plain scrollable div that starts at the TOP, so a growing thread
    would leave the user staring at the greeting. There is no Streamlit API for this, hence a tiny
    component that reaches into the parent document and pins every popover scroll box to its foot.
    NOTE: components.html, not st.iframe. Streamlit deprecates the former, but st.iframe REJECTS
    height=0 ("Height must be a positive integer, 'stretch', or 'content'") and a zero-height
    iframe is exactly what this needs — it is a script carrier with nothing to display. Switching
    raised StreamlitInvalidHeightError and broke every page.
    """
    components.html(
        """
        <script>
        const doc = window.parent.document;
        const pin = () => {
          doc.querySelectorAll('div[data-testid="stPopoverBody"] div[data-testid="stVerticalBlock"]')
             .forEach(el => { if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight; });
          doc.querySelectorAll('div[data-testid="stPopoverBody"] *').forEach(el => {
             const s = window.parent.getComputedStyle(el);
             if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight)
               el.scrollTop = el.scrollHeight;
          });
        };
        pin(); setTimeout(pin, 60); setTimeout(pin, 240); setTimeout(pin, 600);
        </script>
        """,
        height=0,
    )


def render_ask_widget(key: str = "dock") -> None:
    """Call at the END of a page. Renders the floating Ask AI chat.

    Chat layout: header with ✕, a fixed-height scrolling transcript, then the input at the foot of
    the panel. Enter sends — there is no Ask button.
    """
    st.markdown(WIDGET_CSS, unsafe_allow_html=True)
    st.session_state.setdefault("ask_log", [])       # [(question, Answer)] oldest-first
    st.session_state.setdefault("ask_conv", None)
    st.session_state.setdefault("ask_pending", None)  # a question injected by a suggestion click
    st.session_state.setdefault("ask_inflight", None)  # asked, drawn, not yet answered
    st.session_state.setdefault(f"ask_n_{key}", 0)    # bumped to reset the input widget
    st.session_state.setdefault("ask_big", False)     # ⤢ expanded to the large panel
    st.session_state.setdefault("ask_shut", False)    # ✕ pressed — close on this run

    if st.session_state.ask_big:
        st.markdown(
            "<style>div[data-testid='stPopoverBody']{width:min(860px,96vw) !important;}</style>",
            unsafe_allow_html=True,
        )
    if st.session_state.ask_shut:
        # Streamlit exposes no way to close a popover, so click its trigger from the page: that is
        # exactly what dismissing it normally does, and it leaves the conversation intact.
        st.session_state.ask_shut = False
        components.html(
            """
            <script>
            const doc = window.parent.document;
            const pops = doc.querySelectorAll('div[data-testid="stPopover"]');
            const btn = pops.length ? pops[pops.length - 1].querySelector('button') : null;
            if (btn) btn.click();
            </script>
            """,
            height=0,
        )

    with st.popover("Ask AI", use_container_width=False,   # the mark comes from CSS ::before
                    help="Ask about the data or how this app works"):
        # ── Title bar: logo + name on the left, icon controls on the right ──────────────────────
        h1, h2, h3, h4 = st.columns([7, 1, 1, 1], vertical_alignment="center")
        with h1:
            st.markdown(
                "<div class='ask-head-in'>"
                f"<div class='t'>{_mark('ask-logo')}AI Assistant</div>"
                "<div class='s'><span class='live'></span> Analytics KPI assistant · online"
                "</div></div>",
                unsafe_allow_html=True,
            )
        with h2:
            # Expand: swaps the panel to a large size. Two sizes only — a drag handle would need a
            # custom component, and this covers the actual need ("show me more history").
            grown = st.session_state.ask_big
            if st.button("⤢", key=f"ask_big_{key}",
                         help="Shrink" if grown else "Expand the window"):
                st.session_state.ask_big = not grown
                st.rerun()
        with h3:
            if st.button("🗑", key=f"ask_clear_{key}", help="Clear the conversation"):
                st.session_state.ask_log = []
                st.session_state.ask_conv = None
                st.session_state.ask_inflight = None
                st.session_state[f"ask_n_{key}"] += 1   # new input key ⇒ box comes back empty
                st.rerun()
        with h4:
            # A real close. Streamlit has no API for it, so we click the popover's own trigger — the
            # same thing your next outside click would do, just on demand.
            if st.button("✕", key=f"ask_x_{key}", help="Close"):
                st.session_state.ask_shut = True
                st.rerun()
        st.markdown("<div class='ask-rule'></div>", unsafe_allow_html=True)

        # ── Transcript: a FIXED-HEIGHT scroll region. This is what keeps the input at the bottom —
        # the thread can grow without ever pushing the input out of view. The in-flight question and
        # the typing indicator are drawn INSIDE it, at the end. ─────────────────────────────────
        inflight = st.session_state.ask_inflight
        with st.container(height=620 if st.session_state.ask_big else 430, border=False):
            _render_thread(pending=inflight)

        # ── Input at the foot. Enter submits; no Ask button. ────────────────────────────────────
        # A text_input rather than st.chat_input: chat_input has container placement restrictions
        # and is not reliable inside a popover. text_input reruns on Enter with the new value; the
        # key carries a nonce so a sent question clears the box instead of re-firing.
        nonce = st.session_state[f"ask_n_{key}"]
        typed = st.text_input(
            "Your question", key=f"ask_input_{key}_{nonce}", label_visibility="collapsed",
            placeholder="Ask a question and press Enter…",
            disabled=bool(inflight),
        )
        _scroll_to_bottom()

        # Two-phase send. Phase 1: record the question and rerun immediately, so the user sees their
        # own bubble and "…is typing" straight away. Phase 2 (below) actually calls Genie. Without
        # this split, the 20–40s Genie call blocks the run that would have drawn them.
        if not inflight:
            question = typed.strip() or (st.session_state.ask_pending or "").strip()
            st.session_state.ask_pending = None
            if question:
                st.session_state.ask_inflight = question
                st.session_state[f"ask_n_{key}"] += 1   # fresh, empty input on the next run
                st.rerun()
        else:
            from genie_chat import answer_about_app

            if answer_about_app(inflight):
                answer = ask(inflight)          # instant, local knowledge
            else:
                answer = ask(inflight, conversation_id=st.session_state.ask_conv)
            if answer.conversation_id:
                st.session_state.ask_conv = answer.conversation_id
            st.session_state.ask_log.append((inflight, answer))
            st.session_state.ask_inflight = None
            st.rerun()   # redraw so the new exchange replaces the typing indicator
