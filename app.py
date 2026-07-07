import streamlit as st
from streamlit_option_menu import option_menu
from init_session import init_session_state
from typing import Optional
from util.container_util import (
    subheader_markdown, 
    header_markdown,
    title_markdown,
    section_markdown
)



# ---------------------------------------------------------
# Public helper: other modules call this to navigate back
# ---------------------------------------------------------
def return_navigation(
    *,
    version: str,
    guid: Optional[str] = None,
    set_year: Optional[str] = None,
    init_run: bool = True,
    suppress_loader_once: bool = False,
    reset_loader_step: bool = False
) -> None:
    """
    Issue a navigation request handled by app.py on the next cycle.
    No URL parameters are used. Everything flows through session_state.
    """

    nav = {"version": version, "init_run": bool(init_run)}
    if set_year is not None:
        nav["set_year"] = set_year
    if guid:
        nav["guid"] = guid.strip("{}")

    
# ---------------------------------------
# One-time suppression of loader routing
# ---------------------------------------
def _consume_loader_suppression() -> bool:
    """Return True if we should suppress routing into loader once, and consume the flag."""
    if st.session_state.get("__suppress_loader_once"):
        del st.session_state["__suppress_loader_once"]
        return True
    return False



# -----------------------------
# Main app body (callable)
# -----------------------------
def run_main_app() -> None:
    
    # ---- Set page config FIRST (before any st.* calls) ----
    st.set_page_config(
        page_title="APEX Manager Application",
        page_icon="🧭",
        layout="centered",
        initial_sidebar_state="collapsed"
    )

    #_apply_global_styles()

    # ---- Read query params and hydrate session_state ----
    query_params = st.query_params  # modern Streamlit (1.30+)
    app_username = str(query_params.get("app_username", "")).strip() or None
    st.session_state["app_username"] = app_username

    # ---- Handle cross-module navigation requests (no URLs) ----
    if "__nav_request" in st.session_state:
        nav = dict(st.session_state["__nav_request"])
        del st.session_state["__nav_request"]  # consume it

        # Apply request to session state
        st.session_state["version"] = nav.get("version")
        if "set_year" in nav:
            st.session_state["set_year"] = nav["set_year"]
        if "guid" in nav:
            st.session_state["guid"] = str(nav["guid"]).strip("{}")

        # Control whether init runs on this pass
        st.session_state["init_run"] = bool(nav.get("init_run", True))

    # ---- Session init gating (defaults to True) ----
    if st.session_state.get("init_run", True):
        init_session_state()
    else:
        # Skip init once, then restore to True for subsequent runs
        st.session_state["init_run"] = True


    # ---- Sidebar navigation (collapsed by default) ----
    with st.sidebar:
        current_version = st.session_state.get("version")

        options = ["Home", "Loader App", "Manage App"]
        icons = ["house", "box-seam", "tools"]
        default_index = 0 if current_version is None else (1 if current_version == "loader" else 2)

        selection = option_menu(
            menu_title="NAVIGATION",
            options=options,
            icons=icons,
            menu_icon="list",
            default_index=default_index,
            styles={
                "menu-title": {"font-size": "16px", "font-weight": '700'},
                "menu-icon": {"font-size": "18px"},
                "nav-link": {"font-size": "14px"},
                "nav-link-selected": {"font-size": "14px"},
            },
        )

    # Map sidebar selection to session state (no layout change to main content)
    if selection == "Home":
        if st.session_state.get("version") is not None:
            st.session_state["version"] = None
            st.session_state["step"] = 1
            st.session_state["upload_clicked"] = False
            st.rerun()
    elif selection == "Loader App":
        if st.session_state.get("version") != "loader":
            st.session_state["version"] = "loader"
            st.rerun()
    elif selection == "Manage App":
        if st.session_state.get("version") != "manager":
            st.session_state["version"] = "manager"
            st.rerun()

    # ---- Read the preselected app version, if any ----
    version = st.session_state.get("version")


    # ---- If a valid version is already set, immediately route and stop ----
    if version in ("loader", "manager"):
        if version == "loader":
            if _consume_loader_suppression():
                pass  # fall through to Home chooser below
            else:
                from applications.loader_app import run_loader_app
                run_loader_app()
                st.stop()
        if version == "manager":
            from applications.manager_app import run_manager_app
            run_manager_app()
            st.stop()

    # ---- Home chooser (only if no valid version chosen) ----
    with st.container():
        title_markdown("APEX MANAGER APPLICATION")

        st.markdown(" ", unsafe_allow_html=True)
        #st.markdown('<div style="height: 8px;"></div>', unsafe_allow_html=True)

        if st.button("📦 **LOAD A NEW PROJECT TO APEX**", use_container_width=True, key="btn_loader", type='primary'):
            st.session_state["version"] = "loader"
            st.rerun()

        if st.button("🛠️ **MANAGE AN EXISTING PROJECT IN APEX**", use_container_width=True, key="btn_manager", type='primary'):
            st.session_state["version"] = "manager"
            st.rerun()


# Run when launched by `streamlit run app.py`
if __name__ == "__main__":
    run_main_app()
