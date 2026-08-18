"""Multipage app entry point — Day Session + Gyration Legs + Gyrations v2.0 +
OpenNormalisation v1.0.

Run with: .venv/Scripts/streamlit.exe run src/app/app.py

Uses function-reference st.Page (not path-string) deliberately: `dashboard`/
`legs_page`/`gyrations_v2_page`/`open_normalization_page` are imported once, by
plain top-level name, exactly the same way each page module itself imports
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
import open_normalization_page
import gyr_waves_page
import gyr_time_page
import range_page
import day_templates_page
import hour_composite_page
import gyr_stats_page
import time_waves_page

st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")

pg = st.navigation([
    st.Page(lambda: dashboard.main(standalone=False), title="Day Session", url_path="day-session"),
    st.Page(lambda: legs_page.main(standalone=False), title="Gyration Legs", url_path="gyration-legs"),
    st.Page(lambda: gyrations_v2_page.main(standalone=False), title="Gyrations v2.0", url_path="gyrations-v2"),
    st.Page(lambda: open_normalization_page.main(standalone=False), title="OpenNormalisation v1.0", url_path="open-normalisation"),
    st.Page(lambda: gyr_waves_page.main(standalone=False), title="Gyrational Waves v1.0", url_path="gyrational-waves"),
    st.Page(lambda: gyr_time_page.main(standalone=False), title="Gyrational Time v1.0", url_path="gyrational-time"),
    st.Page(lambda: range_page.main(standalone=False), title="Gyrational Range v1.0", url_path="gyrational-range"),
    st.Page(lambda: day_templates_page.main(standalone=False), title="Day Templates v1.0", url_path="day-templates"),
    st.Page(lambda: hour_composite_page.main(standalone=False), title="Hourly Composite v1.0", url_path="hourly-composite"),
    st.Page(lambda: gyr_stats_page.main(standalone=False), title="Gyrational Stats v1.0", url_path="gyrational-stats"),
    st.Page(lambda: time_waves_page.main(standalone=False), title="Time Waves v1.0", url_path="time-waves"),
])
pg.run()
