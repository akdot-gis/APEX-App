"""Geometry loading workflow for the APEX Loader Application.

This module renders the Streamlit controls used to select a project geometry
source, load point/route/boundary geometry, run related geography queries, and
submit the project footprint for downstream loader steps.

Existing behavior, Streamlit UI text, labels, messages, button text, field
names, variable names, function names, session state key names, imports, and
logic are preserved.
"""

import streamlit as st

from util.geometry_util import (
    aashtoware_path,
    aashtoware_point,
    draw_boundary,
    draw_line,
    draw_point,
    enter_latlng,
    point_shapefile,
    polygon_shapefile,
    polyline_shapefile,
)
from util.streamlit_util import (
    handle_project_type_change,
    handle_upload_method_change,
    render_geographies_expander,
    run_queries_if_geometry_changed,
    segmented_with_safe_default,
)

# =============================================================================
# Geometry Signature Helpers
# =============================================================================

def _geometry_signature(point_val, route_val, boundary_val):
    """Create a stable signature for the currently selected geometry values."""
    import json

    def _safe_dump(x):
        """Serialize a geometry value to a stable string representation."""
        try:
            return json.dumps(x, sort_keys=True, default=str)
        except Exception:
            return str(x)

    if point_val is not None:
        return ("point", _safe_dump(point_val))
    if route_val is not None:
        return ("route", _safe_dump(route_val))
    if boundary_val is not None:
        return ("boundary", _safe_dump(boundary_val))
    return (None, None)


# =============================================================================
# ENTRYPOINT: GEOMETRY SELECTION / UPLOAD ROUTER (container-based, no forms)
# =============================================================================
def load_geometry_app():
    """
    Primary Streamlit UI entrypoint for selecting and loading project geometry.

    The function preserves the existing segmented-control workflow for project
    type, geometry source, geometry capture, geography query execution, and
    footprint submission.
    """

    # -------------------------------------------------------------------------
    # Session State Setup and Read Review
    # -------------------------------------------------------------------------
    # Existing setdefault calls below are part of the current geometry workflow
    # behavior and are preserved. Most session state values in this function are
    # intentionally read near their point of use because widgets and utility
    # calls can update them during the same Streamlit run. Centralizing those
    # reads could make values stale and change behavior.

    # --------------------------
    # Versioned container key support
    # --------------------------
    st.session_state.setdefault("geometry_form_version", 0)
    st.session_state.setdefault("prev_geometry_option", None)
    st.session_state.setdefault("prev_geometry_project_type", None)

    # NEW/CHANGED: Initialize submission and snapshots
    st.session_state.setdefault("footprint_submitted", False)
    st.session_state.setdefault("submitted_project_type", None)
    st.session_state.setdefault("submitted_option", None)
    st.session_state.setdefault("submitted_geom_sig", None)
    st.session_state.setdefault("map_reset_counter", 0)

    # -------------------------------------------------------------------------
    # Choose Site / Route / Boundary project type
    # -------------------------------------------------------------------------
    st.markdown("###### CHOOSE PROJECT TYPE\n", unsafe_allow_html=True)

    # Display Project Type
    options = ["Site Project", "Route Project"]
    if st.session_state['is_awp'] == False:
        options.append("Boundary Project")

    st.session_state["project_type"] = st.segmented_control(
        "Select Project Type:",
        options,
        default=st.session_state.get("project_type", None),
    )

    # Clears geometry + computed outputs when project type changes
    handle_project_type_change()
    st.write("")

    # Only render upload options once a project type is selected.
    project_type = st.session_state.get("project_type")
    if not project_type:
        # If project type was cleared and we had a submission before, invalidate it
        if st.session_state.get("footprint_submitted"):
            st.session_state["footprint_submitted"] = False
        return

    st.markdown("###### CHOOSE GEOSPATIAL SOURCE\n", unsafe_allow_html=True)

    # -------------------------------------------------------------------------
    # Initialize flags (default to not showing options)
    # -------------------------------------------------------------------------
    show_awp_point_option = False
    show_awp_route_option = False
    points = st.session_state.get("awp_geometry_points")

    # -------------------------------------------------------------------------
    # Determine whether AASHTOWare option should be offered (Site)
    # AWP Site option is available if the *list* has at least one Midpoint.
    # (No lat/lon validation here; handled downstream.)
    # -------------------------------------------------------------------------
    if project_type.startswith("Site"):
        if points:
            if isinstance(points, list):
                # NEW/CHANGED: list-of-dicts format
                show_awp_point_option = any(
                    isinstance(p, dict)
                    and str(p.get("type", "")).strip().upper() == "MIDPOINT"
                    for p in points
                )
            elif isinstance(points, dict):
                # Backward compatibility for legacy dict format
                mid = points.get("Midpoint") or points.get("MIDPOINT") or points.get("midpoint")
                show_awp_point_option = bool(mid)

    # -------------------------------------------------------------------------
    # Determine whether AASHTOWare option should be offered (Route)
    # AWP Route option is available if the *list* contains at least one BOP
    # and at least one EOP. (No lat/lon validation here.)
    # -------------------------------------------------------------------------
    if project_type.startswith("Route"):
        if points:
            if isinstance(points, list):
                # list-of-dicts format
                types_present = {
                    str(p.get("type", "")).strip().upper()
                    for p in points
                    if isinstance(p, dict)
                }
                show_awp_route_option = ("BOP" in types_present) and ("EOP" in types_present)
            elif isinstance(points, dict):
                # Backward compatibility for legacy dict format
                bop = points.get("BOP") or points.get("bop")
                eop = points.get("EOP") or points.get("eop")
                show_awp_route_option = bool(bop) and bool(eop)

    # -------------------------------------------------------------------------
    # Upload method SEGMENTED CONTROL (OUTSIDE the container)
    # -------------------------------------------------------------------------
    if project_type.startswith("Site"):
        options = ["Upload Shapefile", "Enter Latitude/Longitude", "Select Point on Map"]
        if show_awp_point_option:
            options = ["AASHTOWare"] + options
        # Don't override user's previous option on every render
        st.session_state.setdefault("option", "AASHTOWare")
        option = segmented_with_safe_default("Choose Upload Method:", options, "option")
        handle_upload_method_change(option, clear_boundary=False)

    elif project_type.startswith("Route"):
        options = ["Upload Shapefile", "Draw Route on Map"]
        if show_awp_route_option:
            options = ["AASHTOWare"] + options
        # Don't override user's previous option on every render
        st.session_state.setdefault("option", "AASHTOWare")
        option = segmented_with_safe_default("Choose Upload Method:", options, "option")
        handle_upload_method_change(option, clear_boundary=False)

    else:  # Boundary
        options = ["Upload Shapefile", "Draw Boundary on Map"]
        option = segmented_with_safe_default("Choose Upload Method:", options, "option")
        handle_upload_method_change(option, clear_boundary=True)

    # If the controls differ from what was submitted, invalidate submission
    if st.session_state.get("footprint_submitted"):
        if (
            project_type != st.session_state.get("submitted_project_type")
            or option != st.session_state.get("submitted_option")
        ):
            st.session_state["footprint_submitted"] = False

    # -------------------------------------------------------------------------
    # VERSION BUMP LOGIC for the container (mirrors details form pattern)
    # - Bump when Project Type or Upload Method changes to regenerate widgets.
    # -------------------------------------------------------------------------
    current_opt = option
    prev_opt = st.session_state.get("prev_geometry_option")
    if current_opt != prev_opt:
        st.session_state["geometry_form_version"] = st.session_state.get("geometry_form_version", 0) + 1
        st.session_state["prev_geometry_option"] = current_opt
        # Also invalidate previous submission on option change (defensive)
        if st.session_state.get("footprint_submitted"):
            st.session_state["footprint_submitted"] = False

    current_pt = project_type
    prev_pt = st.session_state.get("prev_geometry_project_type")
    if current_pt != prev_pt:
        st.session_state["geometry_form_version"] = st.session_state.get("geometry_form_version", 0) + 1
        st.session_state["prev_geometry_project_type"] = current_pt
        # Also invalidate previous submission on project type change (defensive)
        if st.session_state.get("footprint_submitted"):
            st.session_state["footprint_submitted"] = False

    # --------------------------
    # Construct the custom container key (versioned)
    # --------------------------
    version = st.session_state.get("geometry_form_version", 0)
    container_key = f"geometry_upload_container_{version}"

    # =============================================================================
    # BEGIN CONTAINER (everything after the segmented control lives inside here)
    # =============================================================================
    geo_container = st.container(key=container_key, border=True)
    with geo_container:
        # ---------------------------------------------------------------------
        # Rehydrate previously submitted geometry so the map
        # components can repaint when the user returns to this page.
        # ---------------------------------------------------------------------
        if st.session_state.get("footprint_submitted") and st.session_state.get("project_geometry") is not None:
            gt = st.session_state.get("geom_type")
            if gt == "point" and st.session_state.get("selected_point") is None:
                st.session_state["selected_point"] = st.session_state["project_geometry"]
            elif gt == "line" and st.session_state.get("selected_route") is None:
                st.session_state["selected_route"] = st.session_state["project_geometry"]
            elif gt == "polygon" and st.session_state.get("selected_boundary") is None:
                st.session_state["selected_boundary"] = st.session_state["project_geometry"]

        # ------------------------------
        # Route to selected mechanism
        # ------------------------------
        # SUBMIT-GUARD: never invoke geometry input functions after submit.
        submitted_now = bool(st.session_state.get("footprint_submitted", False))

        if project_type.startswith("Site"):
            if option == "AASHTOWare":
                aashtoware_point(
                    points,
                    container=geo_container,
                )
            elif option == "Upload Shapefile":
                point_shapefile(container=geo_container)
            elif option == "Select Point on Map":
                draw_point(container=geo_container)
            elif option == "Enter Latitude/Longitude":
                enter_latlng(container=geo_container)

        elif project_type.startswith("Route"):
            if option == "AASHTOWare":
                aashtoware_path(
                    points,
                    container=geo_container,
                )
            elif option == "Upload Shapefile":
                polyline_shapefile(container=geo_container)

            elif option == "Draw Route on Map":
                draw_line(container=geo_container)


        elif project_type.startswith("Boundary"):
            if option == "Upload Shapefile":
                polygon_shapefile(container=geo_container)

            elif option == "Draw Boundary on Map":
                    draw_boundary(container=geo_container)

        # ---------------------------------------------------------------------
        # Read canonical geometry keys & check for presence
        # ---------------------------------------------------------------------
        point_val = st.session_state.get("selected_point")
        route_val = st.session_state.get("selected_route")
        boundary_val = st.session_state.get("selected_boundary")

        # Invalidate if geometry changed after submit
        cur_geom_sig = _geometry_signature(point_val, route_val, boundary_val)
        if st.session_state.get("footprint_submitted") and (
            cur_geom_sig != st.session_state.get("submitted_geom_sig")
        ):
            st.session_state["footprint_submitted"] = False

        missing = (
            (project_type.startswith("Site") and point_val is None) or
            (project_type.startswith("Route") and route_val is None) or
            (project_type.startswith("Boundary") and boundary_val is None)
        )
        if missing:
            # Geometry was cleared (e.g., via your Clear button). Hide success and expander.
            st.session_state["footprint_submitted"] = False
            st.session_state["just_submitted_geometry"] = False
            # Clear any previously computed geographies so the expander won't show stale/blank values.
            for k in ("house_string", "senate_string", "borough_string", "region_string"):
                if k in st.session_state:
                    st.session_state[k] = None
        else:
            # Run queries only when geometry changes
            run_queries_if_geometry_changed(point_val, route_val, boundary_val)

        # Read computed geography strings
        house_val = st.session_state.get("house_string")
        senate_val = st.session_state.get("senate_string")
        borough_val = st.session_state.get("borough_string")
        region_val = st.session_state.get("region_string")

        # Keep existing variable (not used for gating the button anymore)
        has_any_geography = any([house_val, senate_val, borough_val, region_val])

        if project_type.startswith("Site") and point_val is not None:
            st.write('')
            render_geographies_expander(show_routes=False)
        elif project_type.startswith("Route") and route_val is not None:
            st.write('')
            render_geographies_expander(show_routes=False)
        elif project_type.startswith("Boundary") and boundary_val is not None:
            st.write('')
            render_geographies_expander(show_routes=False)

        # ---------------------------------------------------------------------
        # Always show the submit button whenever geometry exists,
        # regardless of whether any geography values were returned.
        # ---------------------------------------------------------------------
        geometry_ready = (
            (project_type.startswith("Site") and point_val is not None) or
            (project_type.startswith("Route") and route_val is not None) or
            (project_type.startswith("Boundary") and boundary_val is not None)
        )

        if geometry_ready:
            # --- Single-click submit via placeholder swap (no behavior changes elsewhere) ---
            submitted = bool(st.session_state.get("footprint_submitted", False))

            btn_ph = st.empty()

            def _render_submit_button(is_done: bool):
                """Render the live or completed submit button in the existing placeholder."""
                label = "SUBMIT FOOTPRINT ✔" if is_done else "SUBMIT FOOTPRINT"
                suffix = "done" if is_done else "live"
                return btn_ph.button(
                    label,
                    use_container_width=True,
                    key=f"submit_footprint_{version}_{suffix}",
                    disabled=is_done,
                )

            clicked = _render_submit_button(submitted)

            if clicked and not submitted:
                # Determine canonical geometry & type based on current selection
                geom = None
                geom_type = None

                if project_type.startswith("Site") and point_val is not None:
                    geom = point_val
                    geom_type = "point"
                elif project_type.startswith("Route") and route_val is not None:
                    geom = route_val
                    geom_type = "line"
                elif project_type.startswith("Boundary") and boundary_val is not None:
                    geom = boundary_val
                    geom_type = "polygon"
                else:
                    st.session_state["footprint_submitted"] = False
                    st.error("Project type and footprint are inconsistent. Please reselect and submit again.")

                # Canonical storage
                st.session_state["project_geom"] = geom
                st.session_state["project_geom_type"] = geom_type

                # Persist user selections so controls reopen the same way
                st.session_state["project_type"] = project_type
                st.session_state["option"] = option

                # Keep selected_* so map components can repaint on return
                st.session_state["selected_point"] = point_val
                st.session_state["selected_route"] = route_val
                st.session_state["selected_boundary"] = boundary_val

                # Mark success
                st.session_state["footprint_submitted"] = True
                st.session_state["just_submitted_geometry"] = True

                # Snapshot what was submitted so we can detect changes later
                st.session_state["submitted_project_type"] = project_type
                st.session_state["submitted_option"] = option
                st.session_state["submitted_geom_sig"] = cur_geom_sig

                # Swap the live button with a disabled ✅ button immediately
                btn_ph.button(
                    "SUBMIT FOOTPRINT ✔",
                    use_container_width=True,
                    key=f"submit_footprint_{version}_done",
                    disabled=True,
                )
            else:
                st.session_state["footprint_submitted"] = st.session_state.get("footprint_submitted", False)