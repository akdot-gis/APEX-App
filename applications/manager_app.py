"""APEX Manager Application.

This module runs the Streamlit workflow for selecting and managing an existing
APEX project. The manager page loads project context, displays the selected
project, and routes the user to the selected management category.

Existing behavior, Streamlit UI text, labels, messages, headings, button text,
help text, field names, variable names, function names, session state key names,
imports, and logic are preserved.
"""

import streamlit as st

# Removed: global Folium map creation here (see note below)
from agol.agol_util import select_record, get_multiple_fields
from tabs.traffic_impacts import manage_traffic_impacts
from tabs.communities import manage_impacted_communities
from tabs.information import manage_information
from tabs.deployment import manage_deployment
from tabs.footprint import manage_footprint
from tabs.source import manage_source
from util.container_util import (
    title_markdown
)


# =============================================================================
# Application Constants
# =============================================================================

PROJECT_PLACEHOLDER_LABEL = "— Select a project —"
YEAR_PLACEHOLDER_LABEL = "— All Construction Years —"

TAB_OPTIONS = [
    "SOURCE",
    "INFORMATION",
    "FOOTPRINT",
    "TRAFFIC IMPACTS",
    "COMMUNITIES",
    "DEPLOYMENT",
]

APEX_PROJECT_FIELDS = [
    "globalid",
    "objectid",
    "AWP_Contract_ID",
    "Proj_Type",
    "AWP_Proj_Name",
    "Proj_Name",
    "Database_Status",
]

PROJECT_LIST_FIELDS = [
    "Proj_Name",
    "globalid",
    "Construction_Year",
]

GEOMETRY_FIELDS = [
    "globalid",
    "objectid",
]


# =============================================================================
# Record and Query Parameter Helpers
# =============================================================================

def get_object_id_from_record(record):
    """Extract objectid from a select_record() response list of features."""
    if not record or not isinstance(record, list):
        return None

    feature = record[0]

    if "attributes" not in feature:
        return None

    return feature["attributes"].get("objectid")


def normalize_qp_value(v):
    """Handle st.query_params values that might be str or list[str]."""
    if v is None:
        return None

    if isinstance(v, (list, tuple)):
        return v[0] if v else None

    return v


def normalize_guid_in_state():
    """Normalize any existing GUID in session_state to lowercase."""
    g = st.session_state.get("guid")

    if isinstance(g, str):
        st.session_state["guid"] = g.lower()


def _gid_matches(gid_value, guid_value):
    """Compare GlobalID values consistently as lowercase strings."""
    if gid_value is None or guid_value is None:
        return False

    try:
        return str(gid_value).lower() == str(guid_value).lower()
    except Exception:
        return False


def _normalize_year(v):
    """Normalize construction year values for filtering and display."""
    if v is None:
        return None

    s = str(v).strip()

    return s if s else None


def _year_sort_key(v):
    """Return a construction year sort key, preferring numeric sort when possible."""
    # Prefer numeric sort when possible (e.g., 2024 < 2025)
    try:
        return (0, int(v))
    except Exception:
        return (1, v)


# =============================================================================
# Project State Helpers
# =============================================================================

def update_project_record():
    """Fetch and store project record/objectid whenever GUID is set/changed."""
    guid = st.session_state.get("guid")
    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")

    if not guid or not url or layer is None:
        st.session_state["project_record"] = None
        st.session_state["objectid"] = None
        return

    try:
        rec = select_record(
            url,
            layer,
            "globalid",
            guid,
            fields="*",
            return_geometry=True
        )

        st.session_state["project_record"] = rec
        st.session_state["objectid"] = get_object_id_from_record(rec)

    except Exception as e:
        st.error(f"Project lookup failed for GUID {guid}: {e}")
        st.session_state["project_record"] = None
        st.session_state["objectid"] = None


def _reset_per_project_state():
    """
    Clear state tied to the currently selected project.

    Keep this minimal and explicit to avoid nuking unrelated app state.
    """
    # Core linkage
    st.session_state["guid"] = None
    st.session_state["project_record"] = None
    st.session_state["objectid"] = None
    st.session_state["_last_guid"] = None

    # APEX context / per-project cache
    st.session_state.pop("apex_guid", None)
    st.session_state.pop("apex_awp_name", None)
    st.session_state.pop("apex_proj_name", None)
    st.session_state.pop("apex_region_string", None)
    st.session_state.pop("apex_database_status", None)
    st.session_state.pop("apex_awp_id", None)
    st.session_state.pop("apex_object_id", None)
    st.session_state.pop("apex_proj_type", None)
    st.session_state.pop("apex_proj_area", None)
    st.session_state.pop("apex_ready", None)
    st.session_state.pop("apex_error", None)

    # Geometry related aggregates
    st.session_state.pop("apex_geom", None)
    st.session_state.pop("geom_ready", None)
    st.session_state.pop("geom_error", None)

    # Reset project selector widget so placeholder shows again
    st.session_state.pop("project_selector", None)


def _remove_guid_from_url():
    """Remove 'guid' from current URL query params, preserving any others."""
    params = {}

    try:
        params = dict(st.query_params)

        if "guid" in params:
            params.pop("guid", None)

        st.query_params.clear()

        for k, v in params.items():
            st.query_params[k] = v

    except Exception:
        try:
            st.experimental_set_query_params(**params)
        except Exception:
            pass


def change_project():
    """Clear project selection and dependent state, then rerun to show dropdown."""
    _reset_per_project_state()
    _remove_guid_from_url()
    st.rerun()


# =============================================================================
# APEX Context Helpers
# =============================================================================

def _set_apex_context_from_record(rec_list):
    """
    Parse the APEX record list and update session state.

    Updates:
    - apex_guid
    - apex_awp_id
    - apex_object_id
    - apex_proj_type
    - apex_proj_area
    """
    if not rec_list or not isinstance(rec_list, list):
        raise ValueError("Empty APEX response")

    feature = rec_list[0]
    attrs = feature.get("attributes", {})

    st.session_state["apex_guid"] = attrs.get("globalid")
    st.session_state['apex_awp_name'] = attrs.get('AWP_Proj_Name')
    st.session_state['apex_proj_name'] = attrs.get('Proj_Name')
    st.session_state['apex_region_string'] = attrs.get('List_DOT_PF_Region')
    st.session_state['apex_database_status'] = attrs.get("Database_Status")
    st.session_state["apex_awp_id"] = attrs.get("AWP_Contract_ID")
    st.session_state["apex_object_id"] = attrs.get("objectid")
    st.session_state["apex_proj_type"] = attrs.get("Proj_Type")

    geom = feature.get("geometry", {}) or {}
    st.session_state["apex_proj_area"] = geom.get("rings")


def _set_geom_context_from_records(rec_list, proj_type):
    """
    Aggregate related geometry features and preserve Esri [x, y] == [lon, lat].
    """
    if not rec_list or not isinstance(rec_list, list):
        st.session_state["apex_geom"] = {"type": "", "globalids": [], "objectids": [], "geoms": []}
        return

    globalids, objectids, geoms = [], [], []

    for feature in rec_list:
        if not isinstance(feature, dict):
            continue

        attrs = feature.get("attributes", {}) or {}
        globalids.append(attrs.get("globalid"))
        objectids.append(attrs.get("objectid"))

        g = feature.get("geometry", {}) or {}

        if proj_type == "Site":
            if "x" in g and "y" in g:
                geoms.append([g["x"], g["y"]])
            if isinstance(g.get("points"), list):
                geoms.extend(g.get("points") or [])
            if isinstance(g.get("rings"), list):
                geoms.extend(g.get("rings") or [])
            if isinstance(g.get("paths"), list):
                geoms.extend(g.get("paths") or [])

        elif proj_type == "Route":
            if isinstance(g.get("paths"), list):
                geoms.extend(g.get("paths") or [])

        elif proj_type == "Boundary":
            if isinstance(g.get("rings"), list):
                geoms.extend(g.get("rings") or [])

        else:
            if isinstance(g.get("paths"), list):
                geoms.extend(g.get("paths") or [])
            if isinstance(g.get("rings"), list):
                geoms.extend(g.get("rings") or [])
            if isinstance(g.get("points"), list):
                geoms.extend(g.get("points") or [])
            if "x" in g and "y" in g:
                geoms.append([g["x"], g["y"]])

    st.session_state["apex_geom"] = {
        "type": proj_type,
        "globalids": globalids,
        "objectids": objectids,
        "geoms": geoms,
    }


def fetch_apex_context():
    """Fetch minimal APEX fields and related geometry for the current GUID."""
    st.session_state["apex_ready"] = False
    st.session_state["apex_error"] = None
    st.session_state["geom_ready"] = False
    st.session_state["geom_error"] = None

    guid = st.session_state.get("guid")

    if not guid:
        return

    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")

    if not url or layer is None:
        st.session_state["apex_error"] = "Missing APEX URL or layer for record fetch."
        return

    try:
        apex_rec = select_record(
            url,
            layer,
            "globalid",
            guid,
            fields=APEX_PROJECT_FIELDS,
            return_geometry=True
        )

        _set_apex_context_from_record(apex_rec)
        st.session_state["apex_ready"] = True

    except Exception as e:
        st.session_state["apex_error"] = f"APEX record fetch failed: {e}"
        st.session_state["apex_guid"] = None
        st.session_state["apex_awp_name"] = None
        st.session_state["apex_proj_name"] = None
        st.session_state["apex_region_string"] = None
        st.session_state['apex_database_status'] = None
        st.session_state["apex_awp_id"] = None
        st.session_state["apex_object_id"] = None
        st.session_state["apex_proj_type"] = None
        st.session_state["apex_proj_area"] = None
        st.session_state["apex_ready"] = False
        return

    try:
        if st.session_state.get("apex_guid") and st.session_state.get("apex_proj_type"):
            proj_type = st.session_state["apex_proj_type"]

            if proj_type == "Site":
                geom_layer = st.session_state.get("sites_layer")
            elif proj_type == "Route":
                geom_layer = st.session_state.get("routes_layer")
            elif proj_type == "Boundary":
                geom_layer = st.session_state.get("boundaries_layer")
            else:
                geom_layer = None

            if geom_layer is None:
                raise ValueError(f"Missing related layer in session_state for proj_type '{proj_type}'")

            geom_rec = select_record(
                url,
                geom_layer,
                "parentglobalid",
                guid,
                fields=GEOMETRY_FIELDS,
                return_geometry=True
            )

            _set_geom_context_from_records(geom_rec, proj_type)
            st.session_state["geom_ready"] = True

    except Exception as e:
        st.session_state["geom_error"] = f"APEX Geom record fetch failed: {e}"
        st.session_state["apex_geom"] = {"type": '', "globalids": [], "objectids": [], "geoms": []}
        st.session_state["geom_ready"] = False


def _current_project_label_from_session():
    """
    Resolve the title shown at the top of the manager page from the freshest
    in-session project state first.

    This lets Source-tab updates immediately change the header after Proj_Name
    is updated.
    """
    project_record = st.session_state.get("project_record")
    attrs = {}

    if project_record and isinstance(project_record, list):
        try:
            attrs = project_record[0].get("attributes", {}) or {}
        except Exception:
            attrs = {}

    name = (
        attrs.get("Proj_Name")
        or st.session_state.get("apex_proj_name")
        or st.session_state.get("apex_awp_name")
    )

    year = attrs.get("Construction_Year")

    if not name:
        return None

    return f"{name} [{year}]" if year not in (None, "") else str(name)


# =============================================================================
# Project List Cache Helpers
# =============================================================================

def _get_projects_cache(projects_url, projects_layer):
    """
    Load or reuse the cached project list for the current project service context.

    The cache is refreshed when the project URL or project layer changes.
    """
    cache_key = "_manager_projects_cache"
    meta_key = "_manager_projects_cache_meta"
    meta = (projects_url, projects_layer)

    if st.session_state.get(meta_key) != meta or cache_key not in st.session_state:
        try:
            st.session_state[cache_key] = get_multiple_fields(
                projects_url,
                projects_layer,
                PROJECT_LIST_FIELDS
            )
        except Exception as e:
            st.error(f"Failed to load project list: {e}")
            st.session_state[cache_key] = []

        st.session_state[meta_key] = meta

    return st.session_state.get(cache_key) or []


# =============================================================================
# Widget Callback Helpers
# =============================================================================

def _on_year_filter_change(placeholder_label):
    """Reset project selection when the construction year filter changes."""
    # Reset project selector so the placeholder shows again after filtering.
    st.session_state["project_selector"] = placeholder_label
    st.session_state["guid"] = None
    st.session_state["project_record"] = None
    st.session_state["objectid"] = None


def on_select_project(label_to_gid, placeholder_label):
    """Update selected project state from the project selector widget."""
    label = st.session_state.get("project_selector")

    if label and label != placeholder_label:
        gid = label_to_gid.get(label)
        st.session_state["guid"] = str(gid).lower() if gid is not None else None  # ensure lowercase
        update_project_record()
    else:
        st.session_state["guid"] = None
        st.session_state["project_record"] = None
        st.session_state["objectid"] = None


def _on_manager_tab_change():
    """Track segmented control changes by incrementing the existing counter."""
    st.session_state["manager_tab_change_counter"] = st.session_state.get("manager_tab_change_counter", 0) + 1


# =============================================================================
# Tab Rendering Helpers
# =============================================================================

def _tab_source():
    """Render the Source management tab."""
    with st.container(border=True):
        manage_source()


def _tab_information():
    """Render the Information management tab."""
    with st.container(border=True):
        manage_information()


def _tab_footprint():
    """Render the Footprint management tab."""
    with st.container(border=True):
        manage_footprint()


def _tab_traffic_impacts():
    """Render the Traffic Impacts management tab."""
    with st.container(border=True):
        manage_traffic_impacts()


def _tab_communities():
    """Render the Communities management tab."""
    with st.container(border=True):
        manage_impacted_communities()


def _tab_deployment():
    """Render the Deployment management tab."""
    with st.container(border=True):
        manage_deployment()


TAB_DISPATCH = {
    "SOURCE": _tab_source,
    "INFORMATION": _tab_information,
    "FOOTPRINT": _tab_footprint,
    "TRAFFIC IMPACTS": _tab_traffic_impacts,
    "COMMUNITIES": _tab_communities,
    "DEPLOYMENT": _tab_deployment,
}


# =============================================================================
# Main Manager Application
# =============================================================================

def run_manager_app():
    """Run the APEX Manager Application workflow."""

    # -------------------------------------------------------------------------
    # Page config
    # -------------------------------------------------------------------------
    st.set_page_config(
        page_title="APEX Manager Application",
        page_icon="🛠️",
        layout="centered",
        initial_sidebar_state="collapsed"
    )

    # -------------------------------------------------------------------------
    # Existing Session State Setup
    # -------------------------------------------------------------------------
    # Existing behavior is preserved. This is not a new cleanup default.
    # (Optional) initialize debug counter once; don't reset on every rerun
    st.session_state.setdefault('debug', 0)

    # ⬇️ Normalize any pre-existing GUID in session state
    normalize_guid_in_state()

    # -------------------------------------------------------------------------
    # Session State Read Review
    # -------------------------------------------------------------------------
    # Pull existing session state values into local variables only where doing so
    # does not change behavior. Values that are updated later in this file remain
    # read near their point of use so they do not become stale.
    projects_url = st.session_state.get("apex_url")
    projects_layer = st.session_state.get('projects_layer')

    # NOTE:
    # - "guid" is read repeatedly because it can be set by URL parameters,
    #   project selection, project reset actions, or other app modules.
    # - "project_record", "objectid", "_last_guid", and APEX context keys are
    #   updated throughout project selection and context-fetch logic.
    # - Widget-backed keys such as "construction_year_filter",
    #   "project_selector", and the dynamic manager tab key remain read near the
    #   related widget logic.

    # -------------------------------------------------------------------------
    # Sync GUID from URL into session_state and refresh record
    # -------------------------------------------------------------------------
    guid_param = None

    try:
        qp = st.query_params

        if qp and "guid" in qp:
            guid_param = normalize_qp_value(qp.get("guid"))

            if isinstance(guid_param, str):
                guid_param = guid_param.lower()  # ✅ normalize before compare

    except Exception:
        guid_param = None

    if guid_param and guid_param != st.session_state.get("guid"):
        st.session_state["guid"] = guid_param  # already lowercased above
        update_project_record()
        _remove_guid_from_url()  # ✅ consume the param to prevent loop
        st.rerun()

    # -------------------------------------------------------------------------
    # Determine whether to show the dropdown
    # -------------------------------------------------------------------------
    show_list = True

    if st.session_state.get("guid"):
        show_list = False

    # -------------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------------
    title_markdown("APEX APP: PROJECT MANAGER")
    st.write('')

    # -------------------------------------------------------------------------
    # Load project list when project selection is needed
    # -------------------------------------------------------------------------
    label_to_gid = {}
    labels_with_placeholder = []
    placeholder_label = PROJECT_PLACEHOLDER_LABEL

    if show_list:
        if not projects_url or projects_layer is None:
            st.error("Missing `apex_url` and/or `projects_layer` in session state. Initialize app session before opening Manager.")
            return

        projects = _get_projects_cache(projects_url, projects_layer)

        # --- Construction Year filter (uses already-pulled data; no refetch) ---
        year_placeholder = YEAR_PLACEHOLDER_LABEL

        years = sorted(
            {
                _normalize_year(p.get("Construction_Year"))
                for p in projects
                if _normalize_year(p.get("Construction_Year")) is not None
            },
            key=_year_sort_key
        )

        year_options = [year_placeholder] + years

        # Default the filter from session_state.set_year (if provided); otherwise no selection.
        if "construction_year_filter" not in st.session_state:
            sy = _normalize_year(st.session_state.get("set_year"))
            st.session_state["construction_year_filter"] = sy if sy in years else year_placeholder

        st.markdown("<h5>SELECT AN APEX PROJECT</h5>", unsafe_allow_html=True)

        st.selectbox(
            "Construction Year",
            year_options,
            key="construction_year_filter",
            on_change=_on_year_filter_change,
            args=(placeholder_label,)
        )

        selected_year = st.session_state.get("construction_year_filter")
        selected_year = None if selected_year == year_placeholder else selected_year

        if selected_year:
            filtered_projects = [
                p for p in projects
                if _normalize_year(p.get("Construction_Year")) == selected_year
            ]
        else:
            filtered_projects = projects

        # Build labels like "(CONSTRUCTION_YEAR) - NAME OF PROJECT"
        label_to_gid = {}

        for p in filtered_projects:
            name = p.get("Proj_Name")
            gid = p.get("globalid")

            if not name or gid is None:
                continue

            y = _normalize_year(p.get("Construction_Year")) or "—"
            label = f"[{y}] - {name}"

            # Disambiguate duplicate labels if needed.
            if label in label_to_gid and str(label_to_gid[label]).lower() != str(gid).lower():
                label = f"{label} [{str(gid)[:8]}]"

            label_to_gid[label] = gid

        labels = sorted(label_to_gid.keys())
        labels_with_placeholder = [placeholder_label] + labels

    # -------------------------------------------------------------------------
    # Project selection UI / Current project display
    # -------------------------------------------------------------------------
    if show_list:
        st.selectbox(
            "Select a project",
            labels_with_placeholder,
            index=0,
            key="project_selector",
            on_change=on_select_project,
            args=(label_to_gid, placeholder_label)
        )

        st.info("Select an APEX project to view and edit project information.")

        # Early return: nothing else to show until a project is chosen
        st.stop()

    else:
        # Resolve and show the project name.
        # Prefer in-session project state first because Source-tab updates can
        # change Proj_Name before the manager project-list cache is rebuilt.
        current_label = _current_project_label_from_session()

        if not current_label and projects_url and projects_layer is not None:
            try:
                projects = _get_projects_cache(projects_url, projects_layer)
                guid = st.session_state.get("guid")

                if guid:
                    rec = next((p for p in projects if _gid_matches(p.get("globalid"), guid)), None)

                    if rec:
                        current_label = f"{rec.get('Proj_Name')} [{rec.get('Construction_Year')}]"

            except Exception:
                current_label = None

        if current_label:
            # Inner columns: title grows, button stays compact
            col_title, col_btn = st.columns([7, 1], vertical_alignment="center")

            with col_title:
                # ⬅️ UPDATED: force uppercase in the displayed project title
                st.markdown(f"<h3 style='margin:0'>{current_label.upper()}</h3>", unsafe_allow_html=True)  # ⬅️ UPDATED

            with col_btn:
                # ⬅️ UPDATED: make the change-project button primary
                if st.button("↺", key="btn_change_project", help="Change Project", type="primary"):  # ⬅️ UPDATED
                    change_project()

        else:
            st.warning("Selected GUID not found in project list.")

            if st.button("↺ Change Project", key="btn_change_project_nf", use_container_width=False):
                change_project()

    # -------------------------------------------------------------------------
    # Re-fetch if GUID changed elsewhere in the app
    # Also (re)load APEX context used by the segmented tabs
    # -------------------------------------------------------------------------
    if st.session_state.get("guid") != st.session_state.get("_last_guid"):
        st.session_state["_last_guid"] = st.session_state.get("guid")
        update_project_record()
        fetch_apex_context()

    # -------------------------------------------------------------------------
    # Segmented control (tabs) – render ONLY the selected tab content and stop
    # -------------------------------------------------------------------------
    if st.session_state.get("guid"):
        if st.session_state.get("apex_error"):
            st.error(st.session_state["apex_error"])
            st.stop()

        elif st.session_state.get("apex_ready"):
            tabs_key = f"manager_tabs_{st.session_state.get('guid')}"

            st.write('')
            st.markdown("<h5>CHOOSE A CATEGORY TO MANAGE</h5>", unsafe_allow_html=True)

            # Available tabs (labels) – unchanged
            options = TAB_OPTIONS

            # ✅ NEW: Preselect from session state's `manager_tab` (if valid).
            # This runs BEFORE rendering the segmented control and only updates
            # the widget value when `manager_tab` changes, so we don't override
            # the user's manual selection on later reruns.
            mt_raw = st.session_state.get("manager_tab")

            if isinstance(mt_raw, str):
                mt = mt_raw.strip().upper()

                if mt in options:
                    if st.session_state.get("_last_manager_tab") != mt:
                        st.session_state["_last_manager_tab"] = mt
                        st.session_state[tabs_key] = mt

            # Keep the same "methodology": on_change only bumps a counter (causes rerun)
            choice = st.segmented_control(
                "Select a Category",
                options=options,
                key=tabs_key,
                width='stretch',
                on_change=_on_manager_tab_change
            )

            # Execute ONLY the chosen tab's function, then STOP the script.
            func = TAB_DISPATCH.get(choice)

            if func is not None:
                func()
                st.stop()

            else:
                # ⬇️ Updated language per request
                st.info("Please choose a category to access the project management options.")
                st.stop()