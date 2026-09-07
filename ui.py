# shared page furniture: the auth gate, the logo, and the header row every page starts with
# this module deliberately renders nothing when imported - login.py used to hold these helpers,
# and importing it from a page re-ran its whole body, so the login screen appeared on top of
# whichever page happened to import it first
import streamlit as st


def require_auth():
    # every page calls this, so a page reached by url rather than the sidebar is still gated
    # TEST_MODE lives only in local secrets.toml; deployed secrets omit it, so auth runs live
    if st.secrets.get("TEST_MODE", False):
        return
    if not st.user.is_logged_in:
        st.warning("🔒 Please log in first.")
        st.switch_page("login.py")
        st.stop()


def current_user() -> str:
    # what to record against a stock take or a movement
    if not st.user.is_logged_in:
        return "test mode"
    return st.user.name or st.user.email


def render_logo(col):
    col.image("assets/logo.jpg", width=500)


def page_setup(title: str):
    # config first - streamlit wants set_page_config before anything else draws
    st.set_page_config(page_title="JMS Material Management", page_icon="🔩", layout="wide")
    require_auth()
    logo_col, title_col = st.columns([1, 4])
    title_col.title(title)
    render_logo(logo_col)
