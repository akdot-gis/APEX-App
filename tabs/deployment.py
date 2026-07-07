### =============================================================================
### PROJECT DEPLOYMENT TAB
### =============================================================================

import streamlit as st
from datetime import datetime
import time
from typing import Dict, Any, Optional, List, Tuple

from agol.agol_util import (
    AGOLDataLoader,
    select_record,
)

from agol.agol_payloads import (
    manage_deployment_payload,
)

from util.input_util import (
    widget_key,
)

# -----------------------------------------------------------------------------
# Helper: fetch active project record
# -----------------------------------------------------------------------------

def _get_project_record():
    apex_guid = st.session_state.get("apex_guid")
    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")

    child_layers = {
        "Location": st.session_state.get("locations_layer"),
        "Site": st.session_state.get("sites_layer"),
        "Route": st.session_state.get("routes_layer"),
        "Boundary": st.session_state.get("boundaries_layer"),
    }

    if not (apex_guid and url and layer is not None):
        return None

    # Main APEX record
    recs = select_record(
        url=url,
        layer=layer,
        id_field="globalid",
        id_value=apex_guid,
        fields="*",
        return_geometry=False,
    )

    if not recs:
        return None

    project_record = {
        "apex": recs[0]["attributes"]
    }

    # Child records
    for name, child_layer in child_layers.items():
        if child_layer is None:
            project_record[name] = None
            continue

        child_recs = select_record(
            url=url,
            layer=child_layer,
            id_field="parentglobalid",
            id_value=apex_guid,
            fields="*",
            return_geometry=False,
        )

        project_record[name] = child_recs[0]["attributes"] if child_recs else None

    return project_record


# -----------------------------------------------------------------------------
# Build package_out for deployment update
# -----------------------------------------------------------------------------

def _build_deployment_package() -> dict:
    """Build a parent package containing the main APEX record package plus
    child packages for Location, Site, Route, and Boundary.

    The main package keeps the existing field names.
    Child packages use prefixed field names like:
    Location_Database_Status, Site_Database_Status, etc.
    """

    base_values = {
        "database_status": st.session_state.get("database_status"),
        "target_applications": st.session_state.get("target_applications")
    }

    def _prefixed_package(prefix: str) -> dict:
        return {
            f"{prefix}_Database_Status": base_values["database_status"],
            f"{prefix}_Target_Applications": base_values["target_applications"],
        }

    return {
        "apex": base_values,
        "Location": _prefixed_package("Location"),
        "Site": _prefixed_package("Site"),
        "Route": _prefixed_package("Route"),
        "Boundary": _prefixed_package("Boundary"),
    }


# -----------------------------------------------------------------------------
# Internal helpers for nested deployment structure
# -----------------------------------------------------------------------------

def _get_objectid(record: Optional[dict]) -> Optional[int]:
    """Return OBJECTID/objectid from a record attributes dict."""
    if not isinstance(record, dict):
        return None

    for key, value in record.items():
        if str(key).lower() == "objectid":
            return value

    return None


def _inject_record_objectids(package_out: Dict[str, Any], project: Dict[str, Any]) -> Dict[str, Any]:
    """Attach objectid to each package that has a matching record."""
    if not isinstance(package_out, dict):
        return package_out

    if isinstance(package_out.get("apex"), dict):
        apex_oid = _get_objectid(project.get("apex"))
        if apex_oid is not None:
            package_out["apex"]["objectid"] = apex_oid

    if isinstance(package_out.get("Location"), dict):
        location_oid = _get_objectid(project.get("Location"))
        if location_oid is not None:
            package_out["Location"]["objectid"] = location_oid

    for child_name in ("Site", "Route", "Boundary"):
        if isinstance(package_out.get(child_name), dict):
            child_oid = _get_objectid(project.get(child_name))
            if child_oid is not None:
                package_out[child_name]["objectid"] = child_oid

    return package_out


def _get_active_child_type(project: Dict[str, Any]) -> Optional[str]:
    """Return whichever one of Site / Route / Boundary exists."""
    for child_name in ("Site", "Route", "Boundary"):
        if isinstance(project.get(child_name), dict):
            return child_name
    return None


def _build_deployment_update_plan(
    package_out: Dict[str, Any],
    project: Dict[str, Any],
) -> List[Tuple[str, int, Dict[str, Any]]]:
    """Build the per-layer update plan:
    - apex always if objectid exists
    - Location if objectid exists
    - one of Site/Route/Boundary if objectid exists
    """
    layer_map = {
        "apex": st.session_state.get("projects_layer"),
        "Location": st.session_state.get("locations_layer"),
        "Site": st.session_state.get("sites_layer"),
        "Route": st.session_state.get("routes_layer"),
        "Boundary": st.session_state.get("boundaries_layer"),
    }

    plan: List[Tuple[str, int, Dict[str, Any]]] = []

    # Main apex
    apex_pkg = package_out.get("apex")
    apex_layer = layer_map.get("apex")
    if isinstance(apex_pkg, dict) and apex_pkg.get("objectid") is not None and apex_layer is not None:
        plan.append(("apex", apex_layer, apex_pkg))

    # Location
    location_pkg = package_out.get("Location")
    location_layer = layer_map.get("Location")
    if isinstance(location_pkg, dict) and location_pkg.get("objectid") is not None and location_layer is not None:
        plan.append(("Location", location_layer, location_pkg))

    # Only one of Site / Route / Boundary should be updated
    active_child = _get_active_child_type(project)
    if active_child:
        child_pkg = package_out.get(active_child)
        child_layer = layer_map.get(active_child)
        if isinstance(child_pkg, dict) and child_pkg.get("objectid") is not None and child_layer is not None:
            plan.append((active_child, child_layer, child_pkg))

    return plan


# -----------------------------------------------------------------------------
# DEPLOY to AGOL
# -----------------------------------------------------------------------------

def _deploy_to_agol_deployment(
    payload: Dict[str, Any],
    edit_type: str,
    layer_idx: Optional[int] = None,
) -> Dict[str, Any]:

    base_url = st.session_state.get("apex_url")
    if layer_idx is None:
        layer_idx = st.session_state.get("projects_layer")

    if base_url is None or layer_idx is None:
        st.error("AGOL Projects layer is not configured.")
        return {"success": False}

    loader = AGOLDataLoader(base_url, layer_idx)

    # UI progress is handled by the caller (manage_deployment) so this function
    # stays UI-neutral.
    return loader.update_features(payload)


# -----------------------------------------------------------------------------
# Default seeding from project record
# -----------------------------------------------------------------------------

def _seed_database_defaults(project: dict, version: str, is_awp: bool, *, force: bool = False):
    """Seed widget defaults from the AGOL project record.

    Defaults are pulled from parent APEX record only:
      - Database_Status (single value for the selectbox)
      - Target_Applications (comma-and-space separated text field -> multiselect list)

    If force=True, existing widget state is overwritten.
    """

    apex_record = project.get("apex", {}) if isinstance(project, dict) else {}

    status_opts = st.session_state.get("database_status_vals", [])
    target_opts = st.session_state.get("target_applications_vals", [])

    status_key = widget_key("database_status", version, is_awp)
    target_key = widget_key("target_applications", version, is_awp)

    # -------------------------
    # Deployment Status
    # -------------------------
    record_status = apex_record.get("Database_Status")

    if force or status_key not in st.session_state:
        if record_status in status_opts:
            st.session_state[status_key] = record_status
        elif status_opts:
            st.session_state[status_key] = status_opts[0]
        else:
            st.session_state[status_key] = None

    # -------------------------
    # Target Applications
    # -------------------------
    record_targets = apex_record.get("Target_Applications")

    if force or target_key not in st.session_state:
        if isinstance(record_targets, str) and record_targets.strip():
            parsed = [v.strip() for v in record_targets.split(",") if v.strip()]
            st.session_state[target_key] = [v for v in parsed if v in target_opts]
        elif target_opts:
            st.session_state[target_key] = [target_opts[0]]
        else:
            st.session_state[target_key] = []


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------

def manage_deployment():

    project = _get_project_record()
    if not project:
        return

    version = st.session_state.get("version")
    is_awp = False  # database never AWP-driven

    status_key = widget_key("database_status", version, is_awp)
    target_key = widget_key("target_applications", version, is_awp)

    # -------------------------------------------------------------
    # Seed defaults ON FIRST LOAD (or if widget keys were cleared)
    # -------------------------------------------------------------
    if status_key not in st.session_state or target_key not in st.session_state:
        _seed_database_defaults(project, version, is_awp)

    # -------------------------------------------------------------
    # Widgets
    # -------------------------------------------------------------
    st.selectbox(
        "Database Status",
        options=st.session_state.get("database_status_vals", []),
        key=status_key,
    )

    st.multiselect(
        "Target Applications",
        options=st.session_state.get("target_applications_vals", []),
        key=target_key,
    )

    # Mirror widget selections into stable keys used by payload builder.
    st.session_state["database_status"] = st.session_state.get(status_key)
    st.session_state["target_applications"] = st.session_state.get(target_key)

    st.write('')

    clicked = st.button(
        "UPDATE DEPLOYMENT",
        type="primary",
        use_container_width=True,
    )

    # Progress UI sits directly under the button.
    progress_slot = st.empty()

    if clicked:
        bar = progress_slot.progress(0, text="Preparing deployment package…")

        package_out = _build_deployment_package()
        package_out = _inject_record_objectids(package_out, project)
        update_plan = _build_deployment_update_plan(package_out, project)

        if not update_plan:
            bar.progress(100, text="No deployment records available to update.")
            st.error("Deployment update failed.")
            return

        total_steps = len(update_plan) + 3
        current_step = 1

        # Build + send updates per layer/package
        for record_name, layer_idx, record_package in update_plan:
            progress_pct = int((current_step / total_steps) * 100)
            bar.progress(progress_pct, text=f"Building AGOL payload for {record_name}…")

            payload = manage_deployment_payload(record_package, "updates")
        
            current_step += 1
            progress_pct = int((current_step / total_steps) * 100)
            bar.progress(progress_pct, text=f"Updating AGOL deployment for {record_name}…")

            result = _deploy_to_agol_deployment(
                payload,
                "updates",
                layer_idx=layer_idx,
            )

            if not result.get("success"):
                bar.progress(100, text=f"Update failed for {record_name}.")
                st.error("Deployment update failed.")
                return

            current_step += 1

        bar.progress(80, text="Refreshing project record…")

        # -------------------------------------------------------------
        # Re-pull project record and rebuild widget defaults
        # -------------------------------------------------------------
        project = _get_project_record()

        if not project:
            bar.progress(100, text="Refresh failed.")
            st.error("Unable to refresh project record after update.")
            return

        # Remove ONLY the current widget state + mirrored stable keys.
        status_key = widget_key("database_status", version, is_awp)
        target_key = widget_key("target_applications", version, is_awp)

        for k in (status_key, target_key, "database_status", "target_applications"):
            if k in st.session_state:
                del st.session_state[k]

        # Reseed defaults from updated parent record (force overwrite)
        _seed_database_defaults(project, version, is_awp, force=True)

        bar.progress(100, text="Complete.")
        time.sleep(0.4)
        progress_slot.empty()

        # Rerun so widgets reflect reseeded defaults.
        st.rerun()