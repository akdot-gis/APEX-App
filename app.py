"""Main Streamlit entry point for the APEX Application.

This module configures the Streamlit page, manages top-level navigation,
routes users into the Loader or Manager application modules, and provides a
public helper for cross-module navigation requests.

Existing behavior, displayed UI text, session state keys, imports, function
names, and execution flow are preserved.
"""

from typing import Optional

import streamlit as st
from streamlit_option_menu import option_menu

from init_session import init_session_state
from util.container_util import (
    subheader_markdown,
    header_markdown,
    title_markdown,
    section_markdown,
)


# =============================================================================
# Page Configuration Constants
# =============================================================================

PAGE_CONFIG = {
    "page_title": "APEX Application",
    "page_icon": "🧭",
    "layout": "centered",
    "initial_sidebar_state": "collapsed",
}


# =============================================================================
# Sidebar Navigation Constants
# =============================================================================

NAVIGATION_OPTIONS = ["Home", "Loader App", "Manager App"]
NAVIGATION_ICONS = ["house", "box-seam", "tools"]
NAVIGATION_STYLES = {
    "menu-title": {"font-size": "16px", "font-weight": '700'},
    "menu-icon": {"font-size": "18px"},
    "nav-link": {"font-size": "14px"},
    "nav-link-selected": {"font-size": "14px"},
}


# =============================================================================
# Public Navigation Helper
# =============================================================================

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

    Parameters
    ----------
    version : str
        Target app version requested by the caller.
    guid : Optional[str]
        Optional GUID value to include in the navigation request metadata.
    set_year : Optional[str]
        Optional year value to include in the navigation request metadata.
    init_run : bool
        Existing flag used to indicate whether session initialization should run.
    suppress_loader_once : bool
        Existing parameter preserved for compatibility.
    reset_loader_step : bool
        Existing parameter preserved for compatibility.

    Returns
    -------
    None
        This function preserves the existing behavior and does not return a value.
    """
    nav = {"version": version, "init_run": bool(init_run)}

    if set_year is not None:
        nav["set_year"] = set_year

    if guid:
        nav["guid"] = guid.strip("{}")


# =============================================================================
# Loader Routing Helpers
# =============================================================================

def _consume_loader_suppression() -> bool:
    """
    Return True if loader routing should be suppressed once, then consume the flag.

    Returns
    -------
    bool
        True when the one-time loader suppression flag exists in session state;
        otherwise False.
    """
    if st.session_state.get("__suppress_loader_once"):
        del st.session_state["__suppress_loader_once"]
        return True

    return False


# =============================================================================
# Main Application
# =============================================================================

def run_main_app() -> None:
    """
    Run the main APEX Application entry point.

    This function configures the Streamlit page, hydrates selected session state
    values from query parameters, handles cross-module navigation requests,
    initializes session state when required, renders the sidebar navigation, and
    routes the user to the selected application module.
    """

    # -------------------------------------------------------------------------
    # Page Configuration
    # -------------------------------------------------------------------------
    # This must remain before any other Streamlit UI calls.
    st.set_page_config(**PAGE_CONFIG)

    # _apply_global_styles()

    # -------------------------------------------------------------------------
    # Query Parameter Handling
    # -------------------------------------------------------------------------
    query_params = st.query_params  # modern Streamlit (1.30+)
    app_username = str(query_params.get("app_username", "")).strip() or None
    st.session_state["app_username"] = app_username

    # -------------------------------------------------------------------------
    # Session State Read Review
    # -------------------------------------------------------------------------
    # Pull existing session state values into local variables only where doing so
    # does not change behavior. Values that may be updated later in this function
    # remain read near their point of use.
    has_nav_request = "__nav_request" in st.session_state

    # NOTE:
    # - "init_run" is read after navigation handling because a navigation request
    #   can update this value before initialization gating.
    # - "version" is read after sidebar selection because sidebar actions can
    #   update this value before routing.
    # - "__suppress_loader_once" remains inside _consume_loader_suppression()
    #   because that helper both reads and consumes the flag.

    # -------------------------------------------------------------------------
    # Cross-Module Navigation Request Handling
    # -------------------------------------------------------------------------
    if has_nav_request:
        nav = dict(st.session_state["__nav_request"])
        del st.session_state["__nav_request"]  # consume it

        # Apply request to session state.
        st.session_state["version"] = nav.get("version")

        if "set_year" in nav:
            st.session_state["set_year"] = nav["set_year"]

        if "guid" in nav:
            st.session_state["guid"] = str(nav["guid"]).strip("{}")

        # Control whether init runs on this pass.
        st.session_state["init_run"] = bool(nav.get("init_run", True))

    # -------------------------------------------------------------------------
    # Session Initialization Gating
    # -------------------------------------------------------------------------
    if st.session_state.get("init_run", True):
        init_session_state()
    else:
        # Skip init once, then restore to True for subsequent runs.
        st.session_state["init_run"] = True

    # -------------------------------------------------------------------------
    # Sidebar Navigation
    # -------------------------------------------------------------------------
    with st.sidebar:
        current_version = st.session_state.get("version")

        options = NAVIGATION_OPTIONS
        icons = NAVIGATION_ICONS
        default_index = 0 if current_version is None else (1 if current_version == "loader" else 2)

        selection = option_menu(
            menu_title="NAVIGATION",
            options=options,
            icons=icons,
            menu_icon="list",
            default_index=default_index,
            styles=NAVIGATION_STYLES,
        )

    # -------------------------------------------------------------------------
    # Sidebar Selection Mapping
    # -------------------------------------------------------------------------
    # Map sidebar selection to session state without changing main content layout.
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

    elif selection == "Manager App":
        if st.session_state.get("version") != "manager":
            st.session_state["version"] = "manager"
            st.rerun()

    # -------------------------------------------------------------------------
    # Application Routing
    # -------------------------------------------------------------------------
    # Read the preselected app version after sidebar handling because the sidebar
    # selection above can update this value.
    version = st.session_state.get("version")

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

    # -------------------------------------------------------------------------
    # Home Chooser
    # -------------------------------------------------------------------------
    # Render only if no valid version has been selected for routing.
    with st.container():
        title_markdown("APEX APP")

        st.markdown(" ", unsafe_allow_html=True)
        # st.markdown('<div style="height: 8px;"></div>', unsafe_allow_html=True)

        if st.button("📦 **LOAD A NEW PROJECT TO APEX**", use_container_width=True, key="btn_loader", type='primary'):
            st.session_state["version"] = "loader"
            st.rerun()

        if st.button("🛠️ **MANAGE AN EXISTING PROJECT IN APEX**", use_container_width=True, key="btn_manager", type='primary'):
            st.session_state["version"] = "manager"
            st.rerun()


# =============================================================================
# Script Entry Point
# =============================================================================

# Run when launched by `streamlit run app.py`.
if __name__ == "__main__":
    run_main_app()