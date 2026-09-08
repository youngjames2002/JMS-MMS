import streamlit as st


def require_auth():
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


def stop_if_empty(df, message: str):
    if df is None or df.empty:
        st.info(message)
        st.stop()
    return df


def page_setup(title: str):
    # config first - streamlit wants set_page_config before anything else draws
    st.set_page_config(page_title="JMS Material Management", page_icon="🔩", layout="wide")
    require_auth()
    logo_col, title_col = st.columns([1, 4])
    title_col.title(title)
    render_logo(logo_col)
