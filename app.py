"""Analytics KPI Self-Service — entry point and navigation.

TWO SERVICES that run independently, in any order, in different sessions:

  Service 1 · services/1_Load_to_Databricks.py  "Files → Databricks"
      Upload Excel → archive the raw file into the UC Volume (never deleted) →
      clean + derive the missing columns → APPEND rows to the bronze table → refresh Genie +
      dashboards. Records what it wrote to .run_manifest.json.

  Service 2 · services/2_Update_Presentation.py "Presentation Update"
      Detect what new data landed → refresh the dashboards at the newest month → capture the tile
      images → update the tagged template's numbers and charts, leaving everything else untouched →
      download the deck and (optionally) a written summary of what changed.

The only link between them is Databricks itself: Service 2 reads whatever Service 1 (or a notebook,
or the Databricks UI) has already loaded. That is why Service 2 never *requires* Service 1 to run.

Navigation is declared with st.navigation so the pages get proper names — otherwise Streamlit
labels this file "app" in the sidebar.
"""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="Analytics KPI Self-Service", page_icon="📊", layout="wide",
    initial_sidebar_state="expanded",
)

# Explicit url_path keeps the slugs clean and ASCII — the auto-generated ones came out as
# "Files_→_Databricks" / "Reports_&_Summaries", which are awkward to link to from the cards.
# The default page is always served at "/" and its url_path is IGNORED — setting one here made
# /overview look like a valid route when it actually rendered Streamlit's "Page not found" dialog.
home = st.Page("services/0_Home.py", title="Overview", icon="🏠", default=True)
load = st.Page("services/1_Load_to_Databricks.py", title="Files → Databricks", icon="📥",
               url_path="load")
deck = st.Page("services/2_Update_Presentation.py", title="Presentation Update", icon="📊",
               url_path="presentation")
full = st.Page("services/3_Full_Run.py", title="Full Monthly Run", icon="⚡", url_path="full-run")
templ = st.Page("services/6_Template_Tagging.py", title="Template Tagging (beta)", icon="🏷️", url_path="tagging")
reports = st.Page("services/4_Reports.py", title="Reports & Summaries", icon="📄",
                  url_path="reports")
chat = st.Page("services/5_Ask_the_Data.py", title="Ask the Data", icon="💬", url_path="ask")

st.navigation(
    {
        "Overview": [home],
        "Run everything": [full],
        "Individual services": [load, deck],
        "Templates": [templ],
        "Reporting": [reports, chat],
    }
).run()
