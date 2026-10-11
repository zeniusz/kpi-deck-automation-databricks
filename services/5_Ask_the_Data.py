"""ASK THE DATA — a chat over the Databricks tables, powered by the Genie space.

Not a bolted-on chatbot: it asks the "Analytics Department Activities" Genie space, which is configured
over these exact tables, governed by the same Unity Catalog permissions, and answers by generating
and running real SQL. So the figures cannot drift from the dashboards, and every answer shows the
SQL it ran — you can verify a number rather than trust it.
"""

from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st
from settings import BRONZE_FQN, GOLD_FQN

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import links  # noqa: E402
from genie_chat import SUGGESTED_QUESTIONS, ask  # noqa: E402
from ui_theme import inject_css, section_close, section_open, sidebar, title  # noqa: E402

inject_css()
sidebar(
    "Ask the data",
    [
        "Type your question at the bottom.",
        "Genie writes and runs the SQL.",
        "Open “SQL Genie ran” to verify any number.",
    ],
)
title(
    "Ask the",
    "Data",
    "Ask questions in plain language about the Analytics programmes. Answers come from the "
    "Databricks Genie space over the live tables — the same numbers as the dashboards.",
)

st.session_state.setdefault("chat", [])          # list of (role, Answer|str)
st.session_state.setdefault("conversation_id", None)
st.session_state.setdefault("pending_q", None)


# ── Suggested questions ─────────────────────────────────────────────────────────────────────────
st.markdown(
    "<div class='small-note'>Type your question in the box at the bottom of the page. Follow-ups "
    "keep the conversation's context, and the first question can take ~20–40s while the warehouse "
    "answers.</div>",
    unsafe_allow_html=True,
)

with st.expander("Need ideas? Some questions that work well"):
    st.markdown("<div class='suggest-row'>", unsafe_allow_html=True)
    cols = st.columns(2)
    for i, q in enumerate(SUGGESTED_QUESTIONS):
        with cols[i % 2]:
            if st.button(q, key=f"sq_{i}", use_container_width=True):
                # Rerun so the question is picked up below on this same interaction, instead of
                # waiting for the user's next click.
                st.session_state.pending_q = q
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
    st.caption(
        "You can ask anything about the programmes — these are only examples. Genie writes the SQL, "
        "so phrase it however you like."
    )

if st.session_state.chat:
    if st.button("🗑 Clear conversation"):
        st.session_state.chat = []
        st.session_state.conversation_id = None
        st.rerun()


# ── The conversation ────────────────────────────────────────────────────────────────────────────
typed = st.chat_input("Ask about learnings, trainings, community, Guild or events…")
question = typed or st.session_state.pending_q
st.session_state.pending_q = None

for role, payload in st.session_state.chat:
    with st.chat_message(role):
        if role == "user":
            st.markdown(payload)
        else:
            if payload.error:
                st.warning(f"Genie couldn't answer that: {payload.error}")
            if payload.text:
                st.markdown(payload.text)
            if payload.rows and payload.columns:
                head = "| " + " | ".join(payload.columns) + " |"
                sep = "|" + "|".join(["---"] * len(payload.columns)) + "|"
                body = "\n".join("| " + " | ".join(r) + " |" for r in payload.rows[:50])
                st.markdown(f"{head}\n{sep}\n{body}")
                if len(payload.rows) > 50:
                    st.caption(f"Showing the first 50 of {len(payload.rows)} rows.")
            if payload.sql:
                with st.expander("SQL Genie ran"):
                    st.code(payload.sql, language="sql")
                    if payload.description:
                        st.caption(payload.description)

if question:
    st.session_state.chat.append(("user", question))
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Genie is querying Databricks…"):
            answer = ask(question, conversation_id=st.session_state.conversation_id)
        if answer.conversation_id:
            st.session_state.conversation_id = answer.conversation_id
        if answer.error:
            st.warning(f"Genie couldn't answer that: {answer.error}")
        if answer.text:
            st.markdown(answer.text)
        if answer.rows and answer.columns:
            head = "| " + " | ".join(answer.columns) + " |"
            sep = "|" + "|".join(["---"] * len(answer.columns)) + "|"
            body = "\n".join("| " + " | ".join(r) + " |" for r in answer.rows[:50])
            st.markdown(f"{head}\n{sep}\n{body}")
        if answer.sql:
            with st.expander("SQL Genie ran"):
                st.code(answer.sql, language="sql")
                if answer.description:
                    st.caption(answer.description)
    st.session_state.chat.append(("assistant", answer))


section_open(1, "Where this comes from")
st.markdown(
    "<div class='small-note'>Answers are generated by the <b>Analytics Department Activities</b> Genie "
    "space over <code>" + BRONZE_FQN + "</code> and "
    "<code>" + GOLD_FQN + "</code>. Genie writes SQL and runs it, so the numbers match the dashboards "
    "exactly. For the full KPI pack see the SharePoint folder; to explore visually, open a "
    "dashboard.</div>",
    unsafe_allow_html=True,
)
g1, g2 = st.columns(2)
with g1:
    for name, url in links.GENIE_SPACES.items():
        st.link_button(f"Open {name} in Databricks", url, use_container_width=True)
with g2:
    st.link_button(links.SHAREPOINT_LABEL, links.SHAREPOINT_DECKS, use_container_width=True)
section_close()
