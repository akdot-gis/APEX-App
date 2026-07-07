import streamlit as st
import re
from agol.agol_district_queries import run_district_queries
from util.read_only_util import ro_widget, ro_widget_taglist  # <- added ro_widget_taglist
from util.input_util import fmt_string
from agol.agol_util import (
    get_multiple_fields,
    select_record
)
from util.input_util import (
    fmt_currency, 
    fmt_date, 
    fmt_date_or_none, 
    fmt_int, 
    fmt_int_or_none, 
    fmt_string,
    fmt_agol_date,
    widget_key
)


# =============================================================================
# GENERIC SESSION-BASED WIDGET HELPERS
# =============================================================================
# session_selectbox:
#   A standard selectbox wrapper that defaults to prior session_state values,
#   with optional string coercion and source-specific widget keys.
# =============================================================================
def session_selectbox(
    key: str,
    label: str,
    help: str,
    options: list,
    default_key: str = None,
    force_str: bool = False,
    is_awp: bool = False,
):
    """
    Render a Streamlit selectbox that defaults to the current session_state value
    or to another session_state key passed in as default_key. If the default value
    is not in options, it will be added. Optionally convert the default value to str.
    Uses source-specific, versioned widget keys to allow hard resets on source/project switches.
    """
    version = st.session_state.get("form_version", 0)

    # Resolve default value robustly
    if default_key and default_key in st.session_state:
        default_value = st.session_state.get(default_key)
    else:
        default_value = st.session_state.get(key, options[0] if options else "")
    if force_str and default_value is not None:
        default_value = str(default_value)

    # Normalize options to ensure the default exists and can be indexed
    normalized_options = [str(opt) if force_str else opt for opt in options]
    if default_value not in normalized_options and default_value is not None:
        normalized_options = [default_value] + normalized_options
    default_index = normalized_options.index(default_value) if default_value in normalized_options else 0

    # Use source-specific widget key
    st.session_state[key] = st.selectbox(
        label,
        normalized_options,
        index=default_index,
        key=widget_key(key, version, is_awp),
        help=help
    )
    return st.session_state[key]





def impacted_comms_select(container=None, label="Select community:"):
    """
    Simple dropdown that PRESERVES original population behavior and can be mounted into a container.

    Args:
        container: A Streamlit container-like target to render into (e.g., st.sidebar, col1, st.container()).
                   If None, defaults to st.
        label:     Widget label.

    Returns:
        {"name": <str>, "id": <any>} or None
    """
    # version for widget_key
    version = st.session_state.get("form_version", 0)

    # Source configuration (same keys you already use)
    comms_url = (
        st.session_state.get("communities_url")
        or st.session_state.get("dcced_communities_url")
        or None
    )
    lyr_idx = int(
        st.session_state.get("communities_layer")
        or st.session_state.get("dcced_communities_layer")
        or 7
    )
    id_field = (
        st.session_state.get("communities_id_field")
        or st.session_state.get("dcced_communities_id_field")
        or "DCCED_CommunityId"
    )

    # Build options as (name, id)
    options = []  # list[(str name, any id)]
    if isinstance(comms_url, str) and comms_url:
        try:
            # get_multiple_fields(url, layer_index, fields_list) -> list[dict]
            rows = get_multiple_fields(comms_url, lyr_idx, ["OverallName", id_field]) or []
            for c in rows:
                name = c.get("OverallName")
                # tolerate field-name variance but still prefer configured id_field
                cid = c.get(id_field) or c.get("DCCED_CommunityId") or c.get("DCCED_CommunityID")
                if name and cid is not None:
                    options.append((name, cid))
        except Exception as e:
            st.warning(f"Communities list not loaded: {e}")

    # Fallback to any preloaded list
    if not options:
        fallback = (
            st.session_state.get("dcced_communities_list")
            or st.session_state.get("communities_list")
            or []
        )
        for c in fallback:
            name = c.get("OverallName")
            cid = c.get(id_field) or c.get("DCCED_CommunityId") or c.get("DCCED_CommunityID")
            if name and cid is not None:
                options.append((name, cid))

    names = [n for (n, _cid) in options]
    name_to_id = dict(options)
    id_to_name = {cid: n for (n, cid) in options}

    # decide where to render
    target = container if container is not None else st

    # if no options, draw disabled selectbox and bail
    if not names:
        target.selectbox(
            label,
            options=["— no communities available —"],
            key=f"{widget_key('impact_comm', version)}::empty",
            disabled=True,
        )
        return None

    # use prior id ONLY to set default index (no writes)
    prev_id = st.session_state.get("impact_comm_id")
    default_name = id_to_name.get(prev_id)
    index = names.index(default_name) if (default_name in names) else 0

    # make key change when options change to avoid stale UI
    opt_fingerprint = f"{len(names)}::{hash(tuple(names[:10]))}"
    select_key = f"{widget_key('impact_comm', version)}::{opt_fingerprint}"

    selected_name = target.selectbox(
        label,
        options=names,
        index=index,
        key=select_key,
        help="Choose the community impacted by the project.",
    )
    selected_id = name_to_id.get(selected_name)
    return {"name": selected_name, "id": selected_id}



def segmented_with_safe_default(label: str, options: list[str], state_key: str) -> str:
    """
    Render a segmented control and persist the selection to session state.

    This helper ensures the selection is always valid for the provided options:
    - If the previous session value is present and still valid, it is reused.
    - Otherwise, the first item in `options` becomes the default selection.

    Args:
        label: UI label displayed above the segmented control.
        options: Allowed option strings presented to the user.
        state_key: Session-state key used to store the selected option.

    Returns:
        The selected option string written to st.session_state[state_key].

    Side Effects:
        - Writes to st.session_state[state_key].
    """
    prev = st.session_state.get(state_key)
    if prev not in options:
        prev = options[0]
    st.session_state[state_key] = st.segmented_control(label, options, default=prev)
    return st.session_state[state_key]


def clear_geography_outputs() -> None:
    """
    Clear computed geography/district output strings.

    These values are displayed in the "PROJECT GEOGRAPHIES" expander and should
    be reset when the project type changes to avoid stale/mismatched results.

    Side Effects:
        - Sets house_string/senate_string/borough_string/region_string to "".
    """
    st.session_state.house_string = ""
    st.session_state.senate_string = ""
    st.session_state.borough_string = ""
    st.session_state.region_string = ""


def clear_geometry(*, point=False, route=False, boundary=False) -> None:
    """
    Clear selected geometry values in session state.

    Args:
        point: If True, clears st.session_state.selected_point.
        route: If True, clears st.session_state.selected_route.
        boundary: If True, clears st.session_state.selected_boundary.

    Side Effects:
        - Sets selected_* keys to None depending on flags.
    """
    if point:
        st.session_state.selected_point = None
    if route:
        st.session_state.selected_route = None
    if boundary:
        st.session_state.selected_boundary = None


def handle_project_type_change() -> None:
    """
    Handle a change in project type.

    When a user switches project types (Site/Route/Boundary), previously selected
    geometry and computed geographies can become invalid. This routine clears:
      - Geography output strings
      - Selected geometry values (point/route/boundary)
      - Upload method selection ("option")
    and updates the tracker key 'prev_project_type'.

    Side Effects:
        - Mutates multiple st.session_state keys.
    """
    if st.session_state.get("prev_project_type") != st.session_state.get("project_type"):
        clear_geography_outputs()
        clear_geometry(point=True, route=True, boundary=True)
        st.session_state["option"] = None
        st.session_state.prev_project_type = st.session_state.get("project_type")


def handle_upload_method_change(option: str, *, clear_boundary: bool = False) -> None:
    """
    Handle a change in upload method.

    Different upload methods write to the same canonical geometry keys
    (selected_point/selected_route/selected_boundary). To prevent cross-method
    bleed (e.g., a previously drawn line persisting when switching to shapefile),
    the prior geometry is cleared when the upload method changes.

    Args:
        option: Newly selected upload method string.
        clear_boundary: If True, also clears selected_boundary (used by Boundary projects).

    Side Effects:
        - Clears selected geometry keys (point/route, and possibly boundary).
        - Writes st.session_state.geo_option to the new option.
    """
    if st.session_state.get("geo_option") != option:
        clear_geometry(point=True, route=True, boundary=clear_boundary)
        st.session_state.geo_option = option


def ensure_prev_geometry_trackers() -> None:
    """
    Ensure that "previous geometry" trackers exist in session state.

    These keys are used to detect geometry changes between reruns and avoid
    expensive district queries unless necessary.

    Side Effects:
        - Initializes prev_selected_point/route/boundary to None if absent.
    """
    if "prev_selected_point" not in st.session_state:
        st.session_state.prev_selected_point = None
    if "prev_selected_route" not in st.session_state:
        st.session_state.prev_selected_route = None
    if "prev_selected_boundary" not in st.session_state:
        st.session_state.prev_selected_boundary = None


def run_queries_if_geometry_changed(point_val, route_val, boundary_val) -> None:
    """
    Run district/geography queries only when the selected geometry changes.

    Query calls may be expensive; this function compares current selected geometry
    to "prev_selected_*" values and triggers run_district_queries() only when:
      - the value is not None, AND
      - the value differs from the previous value.

    Args:
        point_val: Current st.session_state.selected_point value.
        route_val: Current st.session_state.selected_route value.
        boundary_val: Current st.session_state.selected_boundary value.

    Side Effects:
        - May call run_district_queries().
        - Updates prev_selected_point/route/boundary when a change is detected.
    """
    ensure_prev_geometry_trackers()

    point_changed = point_val is not None and point_val != st.session_state.prev_selected_point
    route_changed = route_val is not None and route_val != st.session_state.prev_selected_route
    boundary_changed = boundary_val is not None and boundary_val != st.session_state.prev_selected_boundary

    if point_changed or route_changed or boundary_changed:
        run_district_queries(sections = ['house', 'senate', 'borough', 'region'], message = "Querying against the geography layers...")
        st.session_state.prev_selected_point = point_val
        st.session_state.prev_selected_route = route_val
        st.session_state.prev_selected_boundary = boundary_val


def render_geographies_expander(*, show_routes: bool = False) -> None:
    """
    Render the "PROJECT GEOGRAPHIES" expander section.

    This is shown only when:
      - a geometry exists for the selected project type, AND
      - at least one geography output string is present.

    Args:
        show_routes: If True, also display route IDs and names (Route/Boundary flows).

    Side Effects:
        - Renders Streamlit UI elements (expander, columns, markdown).
    """
    house_val = st.session_state.get("house_string")
    senate_val = st.session_state.get("senate_string")
    borough_val = st.session_state.get("borough_string")
    region_val = st.session_state.get("region_string")

    with st.expander("**LEGISLATIVE AND ADMINISTRATIVE BOUNDARIES**", expanded=True):
        col1, col2 = st.columns(2)
        col1.markdown(f"**House Districts:** {house_val or '—'}")
        col2.markdown(f"**Senate Districts:** {senate_val or '—'}")
        col1.markdown(f"**Boroughs:** {borough_val or '—'}")
        col2.markdown(f"**Regions:** {region_val or '—'}")

        if show_routes:
            route_ids = st.session_state.get("route_ids", None)
            route_names = st.session_state.get("route_names", None)
            st.markdown(f"**Route IDs:** {route_ids}")
            st.markdown(f"**Route Names:** {route_names} ")
