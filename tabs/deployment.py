"""
Project Deployment Tab.

This module defines the Streamlit user interface and supporting logic for the
Project Deployment tab in the larger APEX Streamlit application.

The file retrieves the active APEX project record and related child records from
ArcGIS Online, seeds deployment-related widget defaults from the current project
record, builds deployment update packages, injects required object IDs, creates
per-layer update plans, and submits deployment updates back to AGOL.

Session state values are read and written according to the existing application
workflow. Existing widget keys, stable mirrored keys, AGOL layer references,
displayed text, button labels, and update behavior are preserved exactly.
"""

# =============================================================================
# Imports
# =============================================================================

from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

from agol.agol_payloads import manage_deployment_payload
from agol.agol_util import AGOLDataLoader, select_record
from util.input_util import widget_key


# =============================================================================
# Session State Value Access
# =============================================================================
#
# This file uses Streamlit session state throughout several functions.
# Session state reads are intentionally kept inside the functions where they are
# used because widget state and mirrored deployment values are updated during the
# page execution flow. Moving those reads to module scope could capture stale
# values and change the behavior of the deployment workflow.
#
# This cleanup does not initialize, reset, overwrite, or assign default values to
# session state keys outside of the existing application logic below.


# =============================================================================
# Project Record Helpers
# =============================================================================

def _get_project_record():
    """
    Fetch the active APEX project record and related child records from AGOL.

    The active project is identified using the current ``apex_guid`` from
    Streamlit session state. The function retrieves the main APEX record from
    the configured projects layer and then attempts to retrieve related child
    records for Location, Site, Route, and Boundary layers using the same parent
    global ID.

    Returns:
        dict | None: A nested project record dictionary containing the main APEX
        attributes and available child record attributes. Returns ``None`` when
        required configuration values are missing or when no main APEX record is
        found.
    """
    # Pull the current project and layer configuration from session state.
    apex_guid = st.session_state.get("apex_guid")
    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")

    # Group child layer indexes by the output record name used downstream.
    child_layers = {
        "Location": st.session_state.get("locations_layer"),
        "Site": st.session_state.get("sites_layer"),
        "Route": st.session_state.get("routes_layer"),
        "Boundary": st.session_state.get("boundaries_layer"),
    }

    # Required values must exist before querying the main APEX record.
    if not (apex_guid and url and layer is not None):
        return None

    # Retrieve the main APEX project record by global ID.
    recs = select_record(
        url=url,
        layer=layer,
        id_field="globalid",
        id_value=apex_guid,
        fields="*",
        return_geometry=False,
    )

    # The deployment tab cannot continue without the parent APEX record.
    if not recs:
        return None

    project_record = {
        "apex": recs[0]["attributes"]
    }

    # Retrieve each available child record using the active APEX global ID.
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

        # Store the first related child record when one exists.
        project_record[name] = child_recs[0]["attributes"] if child_recs else None

    return project_record


# =============================================================================
# Deployment Package Builders
# =============================================================================

def _build_deployment_package() -> dict:
    """
    Build the nested deployment package used for AGOL deployment updates.

    The package contains parent APEX deployment values and child packages for
    Location, Site, Route, and Boundary. The main APEX package keeps the existing
    field names, while child packages use prefixed field names such as
    ``Location_Database_Status`` and ``Site_Target_Applications``.

    Returns:
        dict: A nested deployment package containing parent and child deployment
        update values.
    """
    # Pull the stable mirrored deployment values used by the payload builder.
    base_values = {
        "database_status": st.session_state.get("database_status"),
        "target_applications": st.session_state.get("target_applications")
    }

    def _prefixed_package(prefix: str) -> dict:
        """
        Build a deployment package using child-layer prefixed field names.

        Args:
            prefix (str): The child record prefix used to build field names.

        Returns:
            dict: A child deployment package with prefixed database status and
            target application field names.
        """
        # Child deployment fields use layer-specific prefixes.
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


def _get_objectid(record: Optional[dict]) -> Optional[int]:
    """
    Return the OBJECTID value from a record attributes dictionary.

    The lookup is case-insensitive so that either ``OBJECTID`` or ``objectid``
    can be detected without changing downstream behavior.

    Args:
        record (dict | None): A record attributes dictionary.

    Returns:
        int | None: The object ID value when present, otherwise ``None``.
    """
    # Only dictionaries can be searched for an object ID.
    if not isinstance(record, dict):
        return None

    # Match OBJECTID using a case-insensitive key comparison.
    for key, value in record.items():
        if str(key).lower() == "objectid":
            return value

    return None


def _inject_record_objectids(package_out: Dict[str, Any], project: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attach object IDs from the project record to matching deployment packages.

    Each AGOL update package must include the object ID for the record that will
    be updated. This function looks up the object ID from the fetched project
    record and injects it into the corresponding package when available.

    Args:
        package_out (dict): The nested deployment package to update.
        project (dict): The current project record containing APEX and child
            record attributes.

    Returns:
        dict: The same deployment package dictionary with available object IDs
        injected into matching nested packages.
    """
    # Preserve the original value if the package is not a dictionary.
    if not isinstance(package_out, dict):
        return package_out

    # Attach the parent APEX object ID when the parent package is available.
    if isinstance(package_out.get("apex"), dict):
        apex_oid = _get_objectid(project.get("apex"))
        if apex_oid is not None:
            package_out["apex"]["objectid"] = apex_oid

    # Attach the Location object ID when a Location package and record exist.
    if isinstance(package_out.get("Location"), dict):
        location_oid = _get_objectid(project.get("Location"))
        if location_oid is not None:
            package_out["Location"]["objectid"] = location_oid

    # Attach the active child object ID for available Site, Route, and Boundary packages.
    for child_name in ("Site", "Route", "Boundary"):
        if isinstance(package_out.get(child_name), dict):
            child_oid = _get_objectid(project.get(child_name))
            if child_oid is not None:
                package_out[child_name]["objectid"] = child_oid

    return package_out


def _get_active_child_type(project: Dict[str, Any]) -> Optional[str]:
    """
    Return the active child record type for the current project.

    The deployment workflow updates only one of Site, Route, or Boundary. This
    function returns the first child type that exists as a dictionary in the
    project record.

    Args:
        project (dict): The current project record containing possible child
            records.

    Returns:
        str | None: The active child type name, or ``None`` when no active child
        record exists.
    """
    # Only one child type should be active for this deployment update path.
    for child_name in ("Site", "Route", "Boundary"):
        if isinstance(project.get(child_name), dict):
            return child_name

    return None


def _build_deployment_update_plan(
    package_out: Dict[str, Any],
    project: Dict[str, Any],
) -> List[Tuple[str, int, Dict[str, Any]]]:
    """
    Build the per-layer AGOL deployment update plan.

    The update plan determines which deployment packages should be sent to which
    AGOL layers. The parent APEX record is included when it has an object ID,
    Location is included when it has an object ID, and only one active child
    record among Site, Route, and Boundary is included when available.

    Args:
        package_out (dict): The nested deployment package with injected object
            IDs.
        project (dict): The current project record used to determine the active
            child record type.

    Returns:
        list[tuple[str, int, dict]]: A list of update plan entries containing the
        record name, AGOL layer index, and deployment package for each update.
    """
    # Pull the layer indexes needed to route each package to the correct AGOL layer.
    layer_map = {
        "apex": st.session_state.get("projects_layer"),
        "Location": st.session_state.get("locations_layer"),
        "Site": st.session_state.get("sites_layer"),
        "Route": st.session_state.get("routes_layer"),
        "Boundary": st.session_state.get("boundaries_layer"),
    }

    plan: List[Tuple[str, int, Dict[str, Any]]] = []

    # Add the main APEX update when both package object ID and layer index exist.
    apex_pkg = package_out.get("apex")
    apex_layer = layer_map.get("apex")
    if isinstance(apex_pkg, dict) and apex_pkg.get("objectid") is not None and apex_layer is not None:
        plan.append(("apex", apex_layer, apex_pkg))

    # Add the Location update when both package object ID and layer index exist.
    location_pkg = package_out.get("Location")
    location_layer = layer_map.get("Location")
    if isinstance(location_pkg, dict) and location_pkg.get("objectid") is not None and location_layer is not None:
        plan.append(("Location", location_layer, location_pkg))

    # Add only the active child update for Site, Route, or Boundary.
    active_child = _get_active_child_type(project)
    if active_child:
        child_pkg = package_out.get(active_child)
        child_layer = layer_map.get(active_child)
        if isinstance(child_pkg, dict) and child_pkg.get("objectid") is not None and child_layer is not None:
            plan.append((active_child, child_layer, child_pkg))

    return plan


# =============================================================================
# AGOL Deployment Update
# =============================================================================

def _deploy_to_agol_deployment(
    payload: Dict[str, Any],
    edit_type: str,
    layer_idx: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Submit a deployment update payload to the configured AGOL layer.

    Args:
        payload (dict): The AGOL feature update payload to submit.
        edit_type (str): The edit type supplied by the caller. This parameter is
            preserved for existing function signature compatibility.
        layer_idx (int | None): Optional AGOL layer index. When not provided,
            the configured projects layer from session state is used.

    Returns:
        dict: The result returned by ``AGOLDataLoader.update_features``. Returns
        ``{"success": False}`` when required AGOL configuration is missing.
    """
    # Pull the AGOL service URL and fallback layer index from session state.
    base_url = st.session_state.get("apex_url")
    if layer_idx is None:
        layer_idx = st.session_state.get("projects_layer")

    # Stop the update if the AGOL target layer is not configured.
    if base_url is None or layer_idx is None:
        st.error("AGOL Projects layer is not configured.")
        return {"success": False}

    # Create the AGOL loader for the target layer.
    loader = AGOLDataLoader(base_url, layer_idx)

    # UI progress is handled by the caller so this helper remains UI-neutral.
    return loader.update_features(payload)


# =============================================================================
# Widget Default Seeding
# =============================================================================

def _seed_database_defaults(project: dict, version: str, is_awp: bool, *, force: bool = False):
    """
    Seed deployment widget defaults from the AGOL project record.

    Defaults are pulled from the parent APEX record only. ``Database_Status`` is
    used for the single-value selectbox, and ``Target_Applications`` is parsed
    from a comma-and-space separated text field into a list for the multiselect.

    Args:
        project (dict): The active project record containing the parent APEX
            attributes.
        version (str): The current application version used to build widget keys.
        is_awp (bool): Flag used by ``widget_key`` to build deployment widget
            keys. The deployment database workflow is not AWP-driven.
        force (bool): When ``True``, existing widget state is overwritten using
            values from the project record.

    Returns:
        None
    """
    # Read the parent APEX record attributes used to seed widget defaults.
    apex_record = project.get("apex", {}) if isinstance(project, dict) else {}

    # Pull configured option lists from session state.
    status_opts = st.session_state.get("database_status_vals", [])
    target_opts = st.session_state.get("target_applications_vals", [])

    # Build the widget keys that Streamlit uses for this version/context.
    status_key = widget_key("database_status", version, is_awp)
    target_key = widget_key("target_applications", version, is_awp)

    # -------------------------------------------------------------------------
    # Deployment Status
    # -------------------------------------------------------------------------

    record_status = apex_record.get("Database_Status")

    # Seed the selectbox value only when needed or when forced after refresh.
    if force or status_key not in st.session_state:
        if record_status in status_opts:
            st.session_state[status_key] = record_status
        elif status_opts:
            st.session_state[status_key] = status_opts[0]
        else:
            st.session_state[status_key] = None

    # -------------------------------------------------------------------------
    # Target Applications
    # -------------------------------------------------------------------------

    record_targets = apex_record.get("Target_Applications")

    # Seed the multiselect value only when needed or when forced after refresh.
    if force or target_key not in st.session_state:
        if isinstance(record_targets, str) and record_targets.strip():
            parsed = [v.strip() for v in record_targets.split(",") if v.strip()]
            st.session_state[target_key] = [v for v in parsed if v in target_opts]
        elif target_opts:
            st.session_state[target_key] = [target_opts[0]]
        else:
            st.session_state[target_key] = []


# =============================================================================
# Main Streamlit Layout
# =============================================================================

def manage_deployment():
    """
    Render and manage the Project Deployment tab.

    The function retrieves the active project record, seeds deployment widget
    defaults when needed, renders the deployment status and target application
    widgets, mirrors widget values into stable session state keys used by the
    payload builder, and handles the AGOL deployment update button workflow.

    Returns:
        None
    """
    # Retrieve the active project before rendering deployment controls.
    project = _get_project_record()
    if not project:
        return

    # Pull the current version from session state and preserve database behavior.
    version = st.session_state.get("version")
    is_awp = False  # database never AWP-driven

    # Build widget keys for the deployment controls.
    status_key = widget_key("database_status", version, is_awp)
    target_key = widget_key("target_applications", version, is_awp)

    # -------------------------------------------------------------------------
    # Seed Defaults on First Load
    # -------------------------------------------------------------------------

    # Seed defaults if either widget key is missing from session state.
    if status_key not in st.session_state or target_key not in st.session_state:
        _seed_database_defaults(project, version, is_awp)

    # -------------------------------------------------------------------------
    # Deployment Widgets
    # -------------------------------------------------------------------------

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

    # Mirror widget selections into stable keys used by the payload builder.
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

    # -------------------------------------------------------------------------
    # Deployment Update Handling
    # -------------------------------------------------------------------------

    if clicked:
        bar = progress_slot.progress(0, text="Preparing deployment package…")

        # Build the deployment package, attach object IDs, and determine updates.
        package_out = _build_deployment_package()
        package_out = _inject_record_objectids(package_out, project)
        update_plan = _build_deployment_update_plan(package_out, project)

        # Stop when no records are available for update.
        if not update_plan:
            bar.progress(100, text="No deployment records available to update.")
            st.error("Deployment update failed.")
            return

        total_steps = len(update_plan) + 3
        current_step = 1

        # Build and submit AGOL update payloads for each planned layer/package.
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

            # Stop immediately if any layer update fails.
            if not result.get("success"):
                bar.progress(100, text=f"Update failed for {record_name}.")
                st.error("Deployment update failed.")
                return

            current_step += 1

        bar.progress(80, text="Refreshing project record…")

        # ---------------------------------------------------------------------
        # Post-Update Refresh and Widget Reseeding
        # ---------------------------------------------------------------------

        # Re-pull the project record after successful AGOL updates.
        project = _get_project_record()

        if not project:
            bar.progress(100, text="Refresh failed.")
            st.error("Unable to refresh project record after update.")
            return

        # Rebuild widget keys before clearing current widget and mirrored values.
        status_key = widget_key("database_status", version, is_awp)
        target_key = widget_key("target_applications", version, is_awp)

        # Remove ONLY the current widget state + mirrored stable keys.
        for k in (status_key, target_key, "database_status", "target_applications"):
            if k in st.session_state:
                del st.session_state[k]

        # Reseed defaults from updated parent record using the existing force behavior.
        _seed_database_defaults(project, version, is_awp, force=True)

        bar.progress(100, text="Complete.")
        time.sleep(0.4)
        progress_slot.empty()

        # Rerun so widgets reflect reseeded defaults.
        st.rerun()