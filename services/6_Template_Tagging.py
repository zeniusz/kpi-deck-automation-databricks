"""Template tagging (beta): prepare a PowerPoint deck so the app knows which shapes to refresh."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import streamlit as st
from pptx import Presentation

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import template_tagger as tt  # noqa: E402
from ask_widget import render_ask_widget  # noqa: E402
from ui_theme import inject_css, section_close, section_open, sidebar, title  # noqa: E402

DEMO_SPEC = Path(__file__).resolve().parent.parent / "demo_data" / "demo_template_spec.json"

inject_css()
sidebar(
    "Template tagging",
    [
        "Upload an untagged deck.",
        "See every text box and picture per slide.",
        "Describe which shape is which KPI or chart.",
        "Download the tagged template.",
    ],
)
title("Template", "Tagging", "Beta — name the shapes the refresher is allowed to change.")

section_open(1, "Upload the untagged deck")
deck_file = st.file_uploader("PowerPoint (.pptx)", type=["pptx"], label_visibility="collapsed")
section_close()

if deck_file is not None:
    deck_bytes = deck_file.getvalue()
    try:
        prs = Presentation(io.BytesIO(deck_bytes))
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read this deck: {exc}")
        st.stop()

    section_open(2, "Shapes you can tag")
    st.caption("Text boxes and large pictures, by slide. Copy the exact text into the spec below.")
    with st.container(height=380):
        st.table(tt.inventory(prs))
    section_close()

    section_open(3, "Tagging spec")
    st.caption(
        "chart_tags: picture keys per slide, assigned left to right. "
        "kpi_tags: [visible text, key] pairs; the text must match the shape exactly."
    )
    default = DEMO_SPEC.read_text(encoding="utf-8") if DEMO_SPEC.exists() else '{"chart_tags": {}, "kpi_tags": {}}'
    spec_text = st.text_area("Spec (JSON)", value=default, height=280)
    uploaded_spec = st.file_uploader("…or upload a spec file", type=["json"])
    if uploaded_spec is not None:
        spec_text = uploaded_spec.getvalue().decode("utf-8")
    section_close()

    section_open(4, "Tag and download")
    if st.button("Tag the deck", type="primary"):
        try:
            spec = json.loads(spec_text)
            tagged, n_kpi, n_chart, unmatched = tt.tag_bytes(deck_bytes, spec)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            st.error(f"The spec is not valid: {exc}")
        else:
            st.success(f"Tagged {n_kpi} KPI text box(es) and {n_chart} chart picture(s).")
            for note in unmatched:
                st.warning(note)
            st.download_button(
                "Download tagged template", tagged,
                file_name=f"{Path(deck_file.name).stem}_tagged.pptx",
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
    section_close()

render_ask_widget("6_tagging")
