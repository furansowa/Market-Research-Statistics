"""Multipage app entry point — Day Session + Gyration Legs + Gyrations v2.0.

Run with: .venv/Scripts/streamlit.exe run src/app/app.py

Uses function-reference st.Page (not path-string) deliberately: `dashboard`/
`legs_page`/`gyrations_v2_page` are imported once, by plain top-level name,
exactly the same way `legs_page.py`/`gyrations_v2_page.py` themselves import
`dashboard` (`from dashboard import ...`) -- keeping every page's functions
under one consistent module identity so st.cache_resource's cache key (keyed
on func.__module__) doesn't fragment into independently-cached DB
connections. Path-string st.Page (or mixing `import dashboard` here with
`from app.dashboard import ...` elsewhere) would reintroduce exactly that bug.
"""

import streamlit as st

import dashboard
import legs_page
import gyrations_v2_page

st.set_page_config(page_title="DOW Session Lookup Engine", layout="wide")

pg = st.navigation([
    st.Page(lambda: dashboard.main(standalone=False), title="Day Session", url_path="day-session"),
    st.Page(lambda: legs_page.main(standalone=False), title="Gyration Legs", url_path="gyration-legs"),
    st.Page(lambda: gyrations_v2_page.main(standalone=False), title="Gyrations v2.0", url_path="gyrations-v2"),
])
pg.run()
