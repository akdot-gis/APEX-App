from __future__ import annotations
import streamlit as st
from agol.agol_util import AGOLDataLoader, format_guid, delete_cascade_by_globalid
from agol.agol_payloads import (
    communities_payload,
    geography_payload,
    geometry_payload,
    project_payload,
    location_payload,
    parent_traffic_impact_payload,
    child_traffic_impact_payload,
    awp_apex_update_payload
)

# -----------------------------------------------------------------------------
# Helper: record a structured failure with step name and message
# -----------------------------------------------------------------------------
def _record_failure(step: str, message: str) -> None:
    st.session_state.setdefault("step_failures", [])
    st.session_state["step_failures"].append({"step": step, "message": str(message)})

# -----------------------------------------------------------------------------
# OPTIONAL trigger helpers (use from your button code if you want explicit arming)
# -----------------------------------------------------------------------------
def request_load_project_once() -> None:
    """
    Arm the loader to run exactly once on the next rerun.
    Usage from any page/button:
        if st.button("Load Project"):
            request_load_project_once()
            st.rerun()
    """
    st.session_state["__load_project_request__"] = True

def reset_load_project_state() -> None:
    """
    Fully clear loader state (useful for debugging or hard resets).
    """
    for k in (
        "__load_project_request__", "__load_project_lock__", "__load_project_has_executed__",
        "upload_complete", "apex_globalid", "step_failures",
        "traffic_impact_bootstrap", "load_ti_guid",
    ):
        st.session_state.pop(k, None)


def load_project_apex() -> None:
    """
    Upload the current Streamlit session's project and related records into APEX.

    Fire-once & explicit-trigger behavior:
      - If a one-shot suppression flag is set, skip immediately.
      - If this function already executed once in this session, it will NO-OP on reruns
        unless re-armed via `request_load_project_once()` (sets __load_project_request__).
      - A re-entrancy lock prevents duplicate execution within a single rerun burst.

    Returns: None (UI + st.session_state side effects only).
    """

    # -------------------------------------------------------------------------
    # 0) One-shot suppression (respected by your return_navigation flow)
    # -------------------------------------------------------------------------
    if st.session_state.pop("suppress_loader_once", False) or st.session_state.pop("__suppress_loader_once__", False):
        return

    # -------------------------------------------------------------------------
    # 1) If we've definitively completed once, do not run the flow again.
    #     (Keep the success UI block if you want those actions visible on the loader page.)
    # -------------------------------------------------------------------------
    if st.session_state.get("upload_complete") and st.session_state.get("apex_globalid"):
        st.success("LOAD PROJECT: SUCCESS ✅")
        # Post-upload actions (no URLs; use centralized return_navigation)
        from app import return_navigation  # safe: app has no side-effects on import

        guid_val = st.session_state.get("apex_globalid", "")
        guid_clean = str(guid_val).strip("{}")
        set_year = st.session_state.get("set_year")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("RETURN TO LOADER", use_container_width=True):
                return_navigation(
                    version="loader",
                    set_year=set_year,
                    init_run=True,
                    suppress_loader_once=True,
                    hard_reset=True,         # << clear entire session
                    reset_loader_step=True   # << force loader_step = 1
                )
                st.stop()
        with c2:
            if st.button("MANAGE PROJECT", use_container_width=True):
                return_navigation(
                    version="manager",
                    guid=guid_clean,
                    init_run=True,
                    hard_reset=True          # << clear entire session
                )
                st.stop()
        return  # prevent any further execution

    # -------------------------------------------------------------------------
    # 2) Fire-once arming + one-time execution guard
    #    - Backward compatible: if this function is called for the first time
    #      and no arming token is present, we allow the run (as before).
    #    - After the first execution in this session, we will NO-OP unless an
    #      explicit re-arm token (__load_project_request__) is provided.
    # -------------------------------------------------------------------------
    armed = bool(st.session_state.pop("__load_project_request__", False))
    already_executed = bool(st.session_state.get("__load_project_has_executed__", False))
    if already_executed and not armed:
        # Not explicitly requested, and we've already executed once this session.
        # Do nothing on dev saves or other page reruns.
        return

    # Re-entrancy lock (avoid double runs within same rerun burst)
    if st.session_state.get("__load_project_lock__", False):
        return
    st.session_state["__load_project_lock__"] = True
    st.session_state["__load_project_has_executed__"] = True

    try:
        # ====================== original function continues below =====================
        spinner_container = st.empty()

        # -------------------------------------------------------------------------
        # STEP 1: UPLOAD PROJECT (HARD STOP ON FAILURE)
        # -------------------------------------------------------------------------
        with spinner_container, st.spinner("Loading Project to APEX..."):
            try:
                payload_project = project_payload()
                projects_layer = st.session_state["projects_layer"]
                load_project = (
                    AGOLDataLoader(
                        url=st.session_state["apex_url"], layer=projects_layer
                    ).add_features(payload_project)
                    if payload_project
                    else {"success": False, "message": "Failed to Load Project to APEX DB"}
                )
            except Exception as e:
                load_project = {"success": False, "message": f"Project payload error: {e}"}

        spinner_container.empty()
        if not load_project.get("success"):
            error_msg = load_project.get("message", "Unknown error")
            st.error(f"LOAD PROJECT: FAILURE ❌ {error_msg}")
            st.markdown(payload_project)
            _record_failure("Project", error_msg)
            st.stop()

        # Project success
        st.session_state["apex_globalid"] = format_guid(load_project["globalids"])
        st.success("LOAD PROJECT: SUCCESS ✅")

        # -------------------------------------------------------------------------
        # STEP 2: UPLOAD GEOMETRY (MAY BE MULTIPLE GEOMETRIES)
        # -------------------------------------------------------------------------
        with spinner_container, st.spinner("Loading Project Geometry to APEX..."):
            failures = []
            try:
                payload_geometries = geometry_payload()

                # Determine which layer to load based on the selection flags.
                if st.session_state.get("selected_point"):
                    geometry_layer = st.session_state["sites_layer"]
                    geometry_type = "Point Geometry"
                elif st.session_state.get("selected_route"):
                    geometry_layer = st.session_state["routes_layer"]
                    geometry_type = "Route Geometry"
                elif st.session_state.get("selected_boundary"):
                    geometry_layer = st.session_state["boundaries_layer"]
                    geometry_type = "Boundary Geometry"
                else:
                    raise ValueError("No geometry type selected.")

                loader = AGOLDataLoader(
                    url=st.session_state["apex_url"], layer=geometry_layer
                )

                geometries = (
                    payload_geometries
                    if isinstance(payload_geometries, list)
                    else [payload_geometries]
                )
                for idx, geom in enumerate(geometries, start=1):
                    step_name = f"{geometry_type} #{idx}" if len(geometries) > 1 else geometry_type
                    if not geom:
                        msg = f"{step_name}: Empty geometry payload."
                        failures.append(msg)
                        _record_failure(step_name, msg)
                        continue
                    result = loader.add_features(geom)
                    if not result.get("success"):
                        msg = f"{step_name}: {result.get('message', 'Unknown geometry upload failure.')}"
                        failures.append(msg)
                        _record_failure(step_name, msg)
            except Exception as e:
                msg = f"Project Geometry payload error: {e}"
                failures.append(msg)
                _record_failure("Geometry", msg)

        spinner_container.empty()
        if not failures:
            st.success("LOAD GEOMETRY: SUCCESS ✅")
        else:
            st.error("LOAD GEOMETRY: FAILURE ❌")
            for msg in failures:
                st.error(f"• {msg}")

        # -------------------------------------------------------------------------
        # STEP 3: UPLOAD GEOGRAPHY (OPTIONAL; GATED BY SESSION_STATE LIST PRESENCE)
        # -------------------------------------------------------------------------
        with spinner_container, st.spinner("Loading Geography to APEX..."):
            geography_layers = {
                "region": st.session_state["region_layer"],
                "borough": st.session_state["bor_layer"],
                "senate": st.session_state["senate_layer"],
                "house": st.session_state["house_layer"],
            }
            load_results = {}
            try:
                for name, layer_id in geography_layers.items():
                    if f"{name}_list" in st.session_state:
                        payload = geography_payload(name)
                        if payload is None:
                            load_results[name] = None
                        else:
                            load_results[name] = AGOLDataLoader(
                                url=st.session_state["apex_url"], layer=layer_id
                            ).add_features(payload)
            except Exception as e:
                load_results["__error__"] = {
                    "success": False,
                    "message": f"Geography payload error: {e}",
                }

        spinner_container.empty()
        failed_layers = []
        fail_messages = []
        for name, result in load_results.items():
            if name == "__error__":
                msg = result.get("message", "Unknown geography error.")
                failed_layers.append("GEOGRAPHY")
                fail_messages.append(msg)
                _record_failure("Geography", msg)
                continue
            if result is not None and not result.get("success", True):
                step_name = f"Geography: {name.upper()}"
                msg = result.get("message", f"{step_name} failed.")
                failed_layers.append(name.upper())
                fail_messages.append(msg)
                _record_failure(step_name, msg)

        if failed_layers:
            st.error(
                "LOAD LEGISLATIVE AND ADMINISTRATIVE BOUNDARIES: FAILURE ❌\n"
                f"Failed layers: {', '.join(failed_layers)}\n"
                f"Messages: {', '.join(fail_messages)}"
            )
        else:
            st.success("LOAD LEGISLATIVE AND ADMINISTRATIVE BOUNDARIES: SUCCESS ✅")

        # -------------------------------------------------------------------------
        # STEP 4 (SILENT): LOCATION UPDATE
        # -------------------------------------------------------------------------
        try:
            payload_location = location_payload()
            location_layer = st.session_state.get("locations_layer")  # adjust if needed
            if payload_location is None:
                load_location = None
            else:
                loader = AGOLDataLoader(url=st.session_state['apex_url'], layer=location_layer)
                load_location = loader.add_features(payload_location)

            # Validate loader response shapes (dict or truthy/falsey)
            if load_location is not None:
                if isinstance(load_location, dict):
                    if not load_location.get("success", False):
                        _record_failure(
                            "Locations",
                            load_location.get("message", "Unknown error")
                        )
                else:
                    if not bool(load_location):
                        _record_failure("Locations", "Unknown loader response")
        except Exception as e:
            _record_failure("Location Apex", f"Location payload error: {e}")
            load_location = {"success": False, "message": f"Location payload error: {e}"}

        # (OPTIONAL) record this step in diagnostics
        st.session_state["step6_uploads"] = {
            "location": load_location if "load_location" in locals() else None,
        }

        # -------------------------------------------------------------------------
        # STEP 5 (SILENT): TRAFFIC IMPACT
        # -------------------------------------------------------------------------
        ti_parent_result = None
        ti_child_results = {}
        try:
            ti_url = st.session_state.get("traffic_impact_url")
            ti_layers = {
                'parent': st.session_state['traffic_impacts_layer'],
                'route' : st.session_state['traffic_impact_routes_layer'],
                'start': st.session_state['traffic_impact_start_points_layer'],
                'end': st.session_state['traffic_impact_end_points_layer']
            }

            # Build and add parent TI feature
            parent_payload = parent_traffic_impact_payload()
            if parent_payload:
                parent_layer = ti_layers.get("parent")
                parent_loader = AGOLDataLoader(url=ti_url, layer=parent_layer)
                ti_parent_result = parent_loader.add_features(parent_payload)
            else:
                ti_parent_result = {"success": False, "message": "Empty parent TI payload."}

            # If parent succeeded, persist its GlobalID for child payloads
            if isinstance(ti_parent_result, dict) and ti_parent_result.get("success"):
                try:
                    st.session_state["load_ti_guid"] = format_guid(ti_parent_result["globalids"])
                except Exception as _e:
                    _record_failure("Traffic Impact (Parent)", f"Missing GlobalID in response: {_e}")
            else:
                _record_failure(
                    "Traffic Impact (Parent)",
                    (ti_parent_result or {}).get("message", "Unknown error adding parent TI")
                )

            # Only attempt children when we have parent guid
            if st.session_state.get("load_ti_guid"):
                for key, label in [
                    ("route", "Traffic Impact (Child - Route)"),
                    ("start", "Traffic Impact (Child - Start)"),
                    ("end", "Traffic Impact (Child - End)"),
                ]:
                    try:
                        child_payload = child_traffic_impact_payload()
                        if child_payload:
                            child_loader = AGOLDataLoader(url=ti_url, layer=ti_layers.get(key))
                            res = child_loader.add_features(child_payload)
                            ti_child_results[key] = res
                            if not res.get("success"):
                                _record_failure(label, res.get("message", "Unknown error adding TI child"))
                        else:
                            ti_child_results[key] = None
                            _record_failure(label, "Empty child TI payload.")
                    except Exception as e:
                        ti_child_results[key] = {"success": False, "message": str(e)}
                        _record_failure(label, f"TI child payload error: {e}")

        except Exception as e:
            _record_failure("Traffic Impact", f"Upload error: {e}")

        # Stash diagnostics for later inspection
        st.session_state["traffic_impact_bootstrap"] = {
            "parent": ti_parent_result,
            "children": ti_child_results,
        }


        # -------------------------------------------------------------------------
        # STEP 6 (SILENT): UPDATE AWP APEX CONNECTOR
        # -------------------------------------------------------------------------
        if st.session_state['is_awp']:
            
            awp_result = None

            try:
                # Set URL and Layer
                awp_apex_url = st.session_state.get("awp_url")
                awp_apex_layer = st.session_state.get("awp_contracts_layer")

                # Get AWP Contract ID
                awp_id = st.session_state.get("awp_guid", None)

                # Build Update Payload
                if awp_id:
                    awp_apex_payload = awp_apex_update_payload(awp_id)
                    
                    if awp_apex_payload:
                        awp_apex_loader = AGOLDataLoader(url=awp_apex_url, layer=awp_apex_layer)
                        awp_apex_result = awp_apex_loader.update_features(awp_apex_payload)
                    else:
                        awp_apex_result = {"success": False, "message": "Empty AWP to APEX payload."}
                        
            except Exception as e:
                _record_failure("AWP to APEX Update", f"Update error: {e}")

            # Stash diagnostics for later inspection
            st.session_state["awp_apex_update"] = {
                "awp_apex_update": awp_apex_result,
            }

        # -------------------------------------------------------------------------
        # FINALization: CLEANUP ON FAILURE OR MARK COMPLETE
        # -------------------------------------------------------------------------
        if st.session_state.get("step_failures"):
            st.session_state["upload_complete"] = False
            st.error("UPLOAD FAILED ❌ One or more steps failed.")
            with st.expander("Failure details", expanded=True):
                for failure in st.session_state["step_failures"]:
                    if isinstance(failure, dict):
                        step = failure.get("step", "Unknown step")
                        msg = failure.get("message", "No message provided")
                    else:
                        step = "Unknown step"
                        msg = str(failure)
                    st.markdown(f"- **{step}**: {msg}")

            if st.session_state.get("apex_globalid"):
                try:
                    cleaned = delete_cascade_by_globalid(
                        url=st.session_state['apex_url'],
                        main_layer=st.session_state['projects_layer'],
                        related_layers=[
                            st.session_state["sites_layer"],
                            st.session_state["routes_layer"],
                            st.session_state["boundaries_layer"],
                            st.session_state["locations_layer"],
                            st.session_state["region_layer"],
                            st.session_state["bor_layer"],
                            st.session_state["senate_layer"],
                            st.session_state["house_layer"]
                        ],
                        globalid_field='GlobalID',
                        globalid_value=st.session_state['apex_globalid'],
                        parent_field='parentglobalid',
                    )
                    if cleaned:
                        st.warning(
                            "Partial uploads were cleaned up (placeholder). "
                            "Please address the errors and try again."
                        )
                    else:
                        st.warning(
                            "Cleanup attempted but did not complete (placeholder). "
                            "Check logs or try again."
                        )
                except Exception as e:
                    st.error(f"Cleanup (placeholder) encountered an error: {e}")
            else:
                st.info(
                    "The project record was never created, so no cleanup was required. "
                    "Please correct the above issue(s) and try again."
                )

        else:
            st.session_state["upload_complete"] = True
            st.write("")

            # -----------------------------
            # ACTION BUTTONS — use return_navigation (hard reset)
            # -----------------------------
            from app import return_navigation
            guid_val = st.session_state.get("apex_globalid", "")
            guid_clean = str(guid_val).strip("{}")
            set_year = st.session_state.get("set_year")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("RETURN TO LOADER", use_container_width=True):
                    return_navigation(
                        version="loader",
                        set_year=set_year,
                        init_run=True,
                        suppress_loader_once=True,
                        hard_reset=True,       # << clear entire session
                        reset_loader_step=True # << force loader_step = 1
                    )
                    st.stop()
            with c2:
                if st.button("MANAGE PROJECT", use_container_width=True):
                    return_navigation(
                        version="manager",
                        guid=guid_clean,
                        init_run=True,
                        hard_reset=True        # << clear entire session
                    )
                    st.stop()

    finally:
        # Always release lock
        st.session_state["__load_project_lock__"] = False