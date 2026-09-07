# entry point for the app, and the login page
# streamlit's own openid connect support does the work: st.login sends the user to microsoft
# and st.user carries who came back. it needs [auth] and [auth.microsoft] in secrets.toml
import streamlit as st

from ui import current_user, render_logo

st.set_page_config(page_title="JMS Material Management", page_icon="🔩", layout="wide")

logo_col, title_col = st.columns([1, 4])
title_col.title("JMS Material Management")
render_logo(logo_col)

if st.user.is_logged_in:
    st.success(f"Logged in as {current_user()}")
    st.write("Use the sidebar to navigate the app.")
    st.button("Log out", on_click=st.logout)
elif st.secrets.get("TEST_MODE", False):
    st.info("Test mode — login skipped")
    st.write("Use the sidebar to navigate the app.")
else:
    st.login("microsoft")
