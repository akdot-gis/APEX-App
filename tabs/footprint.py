"""Footprint management tab for the APEX Streamlit manager page.

This module renders and manages the FOOTPRINT tab in the APEX Project Manager
Streamlit application. It loads the active project record, retrieves the
currently stored footprint, geography, and location records from AGOL, displays
the project footprint on a Folium map, displays legislative and administrative
boundary values, and supports the UPDATE FOOTPRINT workflow.

The update workflow allows the user to enter footprint update mode, load new
geometry through the existing geometry loader step, build staged AGOL payloads,
delete old footprint/location/geography records, add new footprint/location/
geography records, update the parent project record, update connected traffic
impact records, and reset the tab after a successful deployment.

This cleanup is limited to organization, documentation, and comments. Existing
Streamlit UI text, widget keys, session_state key names, function names,
variable names, imports, payload fields, and execution behavior are preserved.
"""

# =============================================================================
# Imports
# =============================================================================

# Standard library
import json
from typing import Any, Dict, List, Optional

# Third-party
import folium
import streamlit as st
from shapely.geometry import LineString, Point
from streamlit_folium import st_folium

# Local application: AGOL access and payload builders
from agol.agol_payloads import (
    geography_payload,
    geometry_payload,
    location_payload,
    manage_footprint_deletes_payload,
    manage_footprint_project_payload,
)
from agol.agol_util import (
    AGOLDataLoader,
    select_record,
)

# Local application: AASHTOWare geometry helper
from agol.agol_util import aashtoware_geometry  # (kept for side effects elsewhere if needed)

# Local application: geometry loading step
from steps.load_geometry import load_geometry_app

# Local application: map utilities
from util.map_util import (
    add_small_geocoder,
    geometry_to_folium,
    set_bounds_boundary,
    set_bounds_point,
    set_bounds_route,
    set_zoom,
)

# Local application: Streamlit selector helper
from util.streamlit_util import session_selectbox


# =============================================================================
# Existing Session State Initialization
# =============================================================================

# NOTE:
# These session_state initializations are existing executable behavior in this
# file and are intentionally preserved. They are not new cleanup defaults.

# -----------------------------------------------------------------------------
# Initialize Keys
# -----------------------------------------------------------------------------
if "update_footprint_mode" not in st.session_state:
    st.session_state["update_footprint_mode"] = False


# -----------------------------------------------------------------------------
# Initialize AWP Session Keys
# -----------------------------------------------------------------------------
if "is_awp" not in st.session_state:
    st.session_state["is_awp"] = False

if "awp_id" not in st.session_state:
    st.session_state["awp_id"] = None

if "awp_geometry_points" not in st.session_state:
    st.session_state["awp_geometry_points"] = None


# =============================================================================
# Helper Functions
# =============================================================================

# -----------------------------------------------------------------------------
# AASHTOWare Helpers
# -----------------------------------------------------------------------------

def _is_valid_awp_contract_id(value: Any) -> bool:
    """Return whether an AASHTOWare Contract ID value is present and usable.

    Args:
        value (Any): Candidate AASHTOWare contract identifier.

    Returns:
        bool: True when the value is not blank, zero, or a known null-like
            string; otherwise, False.
    """
    if value is None:
        return False

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return False
        if s.upper() in {"N/A", "NA", "NONE", "NULL"}:
            return False
        return True

    if isinstance(value, (int, float)):
        return value != 0

    return True


# -----------------------------------------------------------------------------
# Active Project Record Helpers
# -----------------------------------------------------------------------------

def _get_project_record():
    """Return the active APEX project's attribute dictionary from AGOL.

    Reads the active APEX GUID, APEX service URL, and Projects layer index from
    Streamlit session state, then queries the Projects layer for the matching
    globalid.

    Returns:
        dict | None: Attribute dictionary for the active project record when
            found; otherwise, None.
    """
    # Read runtime project identifiers from session_state at the point of use.
    apex_guid = st.session_state.get("apex_guid")
    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")

    # Stop if the active project or Projects layer configuration is unavailable.
    if not (apex_guid and url and layer is not None):
        return None

    # Query the configured Projects layer for the active project record.
    recs = select_record(
        url=url,
        layer=layer,
        id_field="globalid",
        id_value=apex_guid,
        fields="*",
        return_geometry=False,
    )

    return recs[0]["attributes"] if recs else None


# -----------------------------------------------------------------------------
# AGOL Feature / OBJECTID Helpers
# -----------------------------------------------------------------------------

def _normalize_features(maybe_rec: Any) -> List[Dict[str, Any]]:
    """Normalize a select_record() return value into a list of feature dicts.

    Args:
        maybe_rec (Any): Value returned by ``select_record``. This may be None,
            a feature dictionary, a list of feature dictionaries, or a dictionary
            containing a ``features`` list.

    Returns:
        List[Dict[str, Any]]: Normalized list of feature dictionaries.
    """
    if maybe_rec is None:
        return []

    if isinstance(maybe_rec, dict) and isinstance(maybe_rec.get("features"), list):
        return maybe_rec.get("features") or []

    if isinstance(maybe_rec, list):
        return maybe_rec

    if isinstance(maybe_rec, dict) and ("attributes" in maybe_rec or "geometry" in maybe_rec):
        return [maybe_rec]

    return []


def _get_objectid_from_attributes(attrs: Dict[str, Any]) -> Optional[int]:
    """Return an OBJECTID from an attributes dictionary.

    Handles the common OBJECTID casing variants returned by AGOL or payload
    builders.

    Args:
        attrs (Dict[str, Any]): Feature attributes dictionary.

    Returns:
        Optional[int]: OBJECTID converted to an integer when available and
            convertible; otherwise, None.
    """
    if not isinstance(attrs, dict):
        return None

    # Search known OBJECTID casing variants.
    for k in ("OBJECTID", "objectid", "objectId", "ObjectId", "ObjectID"):
        if k in attrs and attrs.get(k) is not None:
            try:
                return int(attrs.get(k))
            except Exception:
                return None

    return None


def _ensure_objectid_key(attrs: Dict[str, Any]) -> None:
    """Ensure an attributes dictionary contains an ``OBJECTID`` key when possible.

    Args:
        attrs (Dict[str, Any]): Feature attributes dictionary to normalize.

    Returns:
        None: The attributes dictionary is modified in place when an object ID
            variant is found.
    """
    if not isinstance(attrs, dict):
        return

    if "OBJECTID" in attrs and attrs.get("OBJECTID") is not None:
        return

    # Copy the first available objectid variant into OBJECTID.
    for k in ("objectid", "objectId", "ObjectId", "ObjectID"):
        if k in attrs and attrs.get(k) is not None:
            attrs["OBJECTID"] = attrs.get(k)
            return


def _collect_objectids_from_features(features: List[Dict[str, Any]]) -> List[int]:
    """Collect OBJECTIDs from a feature list.

    This helper also normalizes each feature's attributes in place so downstream
    payload builders can rely on the standard ``OBJECTID`` casing.

    Args:
        features (List[Dict[str, Any]]): Feature dictionaries to inspect.

    Returns:
        ListOBJECTIDs found in the feature list.
    """
    out: List[int] = []

    for feat in features or []:
        attrs = (feat or {}).get("attributes") or {}
        _ensure_objectid_key(attrs)
        oid = _get_objectid_from_attributes(attrs)

        if oid is not None:
            out.append(oid)

    return out


# -----------------------------------------------------------------------------
# Project Type / Layer Resolution Helpers
# -----------------------------------------------------------------------------

def _resolve_new_project_type() -> Optional[str]:
    """Resolve the new project type based on the active geometry selector.

    Returns:
        Optional``"Site"``, ``"Route"``, or ``"Boundary"`` when a new
            geometry selection is active; otherwise, None.
    """
    if st.session_state.get("selected_point") is not None:
        return "Site"

    if st.session_state.get("selected_route") is not None:
        return "Route"

    if st.session_state.get("selected_boundary") is not None:
        return "Boundary"

    return None


def _project_type_to_footprint_layer(proj_type: Optional[str]) -> Optional[int]:
    """Map a project type to the configured footprint layer index.

    Args:
        proj_type (Optional[str]): Project type value such as ``"Site"``,
            ``"Route"``, or ``"Boundary"``.

    Returns:
        Optional[int]: Matching footprint layer index from session_state, or None
            when the project type is not recognized.
    """
    if proj_type == "Site":
        return st.session_state.get("sites_layer")

    if proj_type == "Route":
        return st.session_state.get("routes_layer")

    if proj_type == "Boundary":
        return st.session_state.get("boundaries_layer")

    return None


# -----------------------------------------------------------------------------
# Value Normalization Helpers
# -----------------------------------------------------------------------------

def _first_nonempty(mapping: dict, keys: list):
    """Return the first non-empty value from a mapping for any key in keys.

    Args:
        mapping (dict): Dictionary to inspect.
        keys (list): Candidate keys in priority order.

    Returns:
        Any: First non-empty value found, or None when no candidate value exists.
    """
    for k in keys:
        if k in mapping:
            v = mapping.get(k)

            if v is None:
                continue

            if isinstance(v, str) and not v.strip():
                continue

            return v

    return None


def _as_list(value):
    """Normalize a value into a list.

    This is used for ``*_list`` session keys that may originate as lists,
    tuples, sets, comma-delimited strings, single strings, or scalar values.

    Args:
        value (Any): Value to normalize.

    Returns:
        list: Clean list representation of the provided value.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v is not None and (not isinstance(v, str) or v.strip())]

    if isinstance(value, str):
        s = value.strip()

        if not s:
            return []

        # Support comma-delimited or JSON-like list strings.
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]

        return [s]

    return [value]


# -----------------------------------------------------------------------------
# Payload Builder State Helpers
# -----------------------------------------------------------------------------

def _seed_payload_builder_state_from_project(project_rec: dict) -> None:
    """Seed session_state keys expected by footprint payload builders.

    The footprint/location/geography payload builders expect several project and
    geography values to already exist in ``st.session_state``. In manager context,
    this helper fills those keys from the parent Projects record only when the
    session_state key is currently None.

    Args:
        project_rec (dict): Parent Projects layer attributes dictionary.

    Returns:
        None: Required session_state keys are populated in place when missing.
    """
    if not isinstance(project_rec, dict) or not project_rec:
        return

    # Parent globalid used by child layers as parentglobalid.
    if st.session_state.get("apex_globalid") is None:
        st.session_state["apex_globalid"] = (
            project_rec.get("globalid")
            or project_rec.get("GlobalID")
            or st.session_state.get("apex_guid")
        )

    # Project names used by downstream payload builders.
    if st.session_state.get("proj_name") is None:
        st.session_state["proj_name"] = _first_nonempty(
            project_rec,
            [
                "Proj_Name",
                "Project_Name",
                "ProjectName",
                "proj_name",
                "project_name",
                "Name",
                "Title",
            ],
        )

    if st.session_state.get("awp_proj_name") is None:
        st.session_state["awp_proj_name"] = _first_nonempty(
            project_rec,
            [
                "AWP_Proj_Name",
                "Awp_Proj_Name",
                "awp_proj_name",
                "AWP_Project_Name",
                "ContractID",
                "awp_id",
            ],
        )

    # Administrative boundary strings used by geometry/location payloads.
    if st.session_state.get("region_string") is None:
        st.session_state["region_string"] = _first_nonempty(
            project_rec,
            [
                "DOT_PF_Region",
                "Proj_DOT_PF_Region",
                "Region",
                "region_string",
            ],
        )

    if st.session_state.get("borough_string") is None:
        st.session_state["borough_string"] = _first_nonempty(
            project_rec,
            [
                "Borough_Census_Area",
                "Proj_Borough_Census_Area",
                "Borough",
                "borough_string",
            ],
        )

    if st.session_state.get("senate_string") is None:
        st.session_state["senate_string"] = _first_nonempty(
            project_rec,
            [
                "Senate_District",
                "Proj_Senate_District",
                "senate_string",
            ],
        )

    if st.session_state.get("house_string") is None:
        st.session_state["house_string"] = _first_nonempty(
            project_rec,
            [
                "House_District",
                "Proj_House_District",
                "house_string",
            ],
        )

    # geography_payload(...) expects {name}_list keys:
    # region_list, borough_list, senate_list, and house_list.
    if st.session_state.get("region_list") is None:
        st.session_state["region_list"] = _as_list(
            _first_nonempty(project_rec, ["List_DOT_PF_Region", "region_list"])
        )

    if st.session_state.get("borough_list") is None:
        st.session_state["borough_list"] = _as_list(
            _first_nonempty(project_rec, ["List_Borough_Census_Area", "borough_list"])
        )

    if st.session_state.get("senate_list") is None:
        st.session_state["senate_list"] = _as_list(
            _first_nonempty(project_rec, ["List_Senate_District", "senate_list"])
        )

    if st.session_state.get("house_list") is None:
        st.session_state["house_list"] = _as_list(
            _first_nonempty(project_rec, ["List_House_District", "house_list"])
        )


# -----------------------------------------------------------------------------
# State Reset Helpers
# -----------------------------------------------------------------------------

def _clear_footprint_and_load_geometry_state():
    """Clear session_state keys created by footprint.py and load_geometry.

    Returns:
        None: Configured keys are removed from session_state when present.
    """
    keys_to_clear = {
        # --- footprint.py-created keys ---
        "update_footprint_mode",
        "is_awp",
        "awp_id",
        "awp_geometry_points",
        "footprint_item",
        "deploy_objectids",
        "last_footprint_deploy_result",
        "locations_raw_record",
        "locations_records",
        "geography_raw_records",
        "geography_records",
        "house_records",
        "senate_records",
        "region_records",
        "borough_records",
        "List_House_District",
        "List_Senate_District",
        "List_DOT_PF_Region",
        "List_Borough_Census_Area",

        # --- payload-builder seed keys (manager context) ---
        "apex_globalid",
        "proj_name",
        "awp_proj_name",
        "region_list",
        "borough_list",
        "senate_list",
        "house_list",
        "center",

        # --- load_geometry-created keys (from load_geometry step) ---
        "footprint_submitted",
        "just_submitted_geometry",
        "project_geometry",
        "project_geom_type",
        "project_geom",
        "selected_point",
        "selected_route",
        "selected_boundary",
        "option",
        "prev_geometry_option",
        "prev_geometry_project_type",
        "submitted_geom_sig",
        "submitted_option",
        "submitted_project_type",
        "geometry_form_version",
        "project_type",
        "geom_type",
        "house_string",
        "senate_string",
        "region_string",
        "borough_string",
    }

    for k in keys_to_clear:
        st.session_state.pop(k, None)


def _reset_to_fresh_run_after_deploy():
    """Clear footprint/load-geometry state and rerun the script from the top.

    Returns:
        None: Session state is cleared, then Streamlit rerun is requested.
    """
    _clear_footprint_and_load_geometry_state()
    st.rerun()


# -----------------------------------------------------------------------------
# Payload Builder Helpers
# -----------------------------------------------------------------------------

def build_project_update_payload(project_rec: Dict[str, Any], new_proj_type: str) -> Dict[str, Any]:
    """Build the applyEdits update payload for the main Projects layer.

    Args:
        project_rec (Dict[str, Any]): Parent project attributes dictionary.
        new_proj_type (str): New project type to store on the parent project.

    Returns:
        Dict[str, Any]: AGOL update payload for the Projects layer.
    """
    if not project_rec:
        return {"updates": []}

    # Requires OBJECTID so AGOL can target the correct feature for update.
    attrs = {
        "OBJECTID": project_rec.get("OBJECTID") or project_rec.get("objectId") or project_rec.get("objectid"),
        "Proj_Type": new_proj_type,
    }
    attrs = {k: v for k, v in attrs.items() if v is not None}

    return {"updates": [{"attributes": attrs}]}


def build_delete_payload_from_rec(maybe_rec: Any) -> Dict[str, Any]:
    """Build an applyEdits deletes payload from a select_record() return value.

    Args:
        maybe_rec (Any): Raw feature result returned by ``select_record``.

    Returns:
        Dict[str, Any]: AGOL deletes payload containing collected OBJECTIDs.
    """
    features = _normalize_features(maybe_rec)
    oids = _collect_objectids_from_features(features)

    return {"deletes": oids}


def build_footprint_add_payload(apex_guid: str, esri_geom: Dict[str, Any]) -> Dict[str, Any]:
    """Build the applyEdits adds payload for the new footprint geometry.

    Args:
        apex_guid (str): Parent project globalid.
        esri_geom (Dict[str, Any]): Esri geometry dictionary to add.

    Returns:
        Dict[str, Any]: AGOL adds payload for the new footprint geometry.
    """
    if not apex_guid or not esri_geom:
        return {"adds": []}

    # NOTE: Keep this minimal on purpose. Additional attributes are added later.
    attrs = {"parentglobalid": apex_guid}

    return {"adds": [{"attributes": attrs, "geometry": esri_geom}]}


def build_geography_add_payloads(apex_guid: str) -> Dict[str, Dict[str, Any]]:
    """Build placeholder add payloads for geography layers.

    Args:
        apex_guid (str): Parent project globalid.

    Returns:
        Dict[str, Dict[str, Any]]: Placeholder add payload dictionary for house,
            senate, region, and borough geography layers.
    """
    return {
        "house": {"adds": []},
        "senate": {"adds": []},
        "region": {"adds": []},
        "borough": {"adds": []},
    }


def build_traffic_impacts_update_payload(
    apex_guid: str,
    update_attrs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an update payload for Traffic Impact records connected to a project.

    This function:
        1. Uses the project GUID to find connected Traffic Impact records.
        2. Collects the OBJECTIDs from those records.
        3. Creates an updates payload for the Traffic Impacts layer.
        4. Allows the caller to pass additional attributes to update on each record.

    Required session_state keys:
        traffic_impact_url: Traffic Impacts service URL.
        traffic_impacts_layer: Traffic Impacts layer index.

    Args:
        apex_guid (str): Active APEX project GUID.
        update_attrs (Optional[Dict[str, Any]]): Optional attributes to merge into
            each traffic impact update record.

    Returns:
        Dict[str, Any]: AGOL updates payload for connected Traffic Impact records.
    """
    if not apex_guid:
        return {"updates": []}

    # Read Traffic Impacts layer configuration at deployment time.
    url = st.session_state.get("traffic_impact_url")
    traffic_impacts_layer = st.session_state.get("traffic_impacts_layer")

    if not url or traffic_impacts_layer is None:
        return {"updates": []}

    # Query Traffic Impact records linked to this APEX project.
    traffic_impact_recs = select_record(
        url=url,
        layer=traffic_impacts_layer,
        id_field="APEX_GUID",
        id_value=apex_guid,
        fields="*",
        return_geometry=False,
    )

    features = _normalize_features(traffic_impact_recs)
    objectids = _collect_objectids_from_features(features)

    if not objectids:
        return {"updates": []}

    attrs_to_apply = update_attrs or {}

    updates = []
    for oid in objectids:
        attrs = {
            "OBJECTID": oid,
            "DOT_Region": st.session_state.get("region_string", None),
        }

        # Merge optional caller-provided attributes into each update record.
        attrs.update(attrs_to_apply)

        updates.append({
            "attributes": attrs
        })

    return {
        "updates": updates
    }


# -----------------------------------------------------------------------------
# Deployment Result Helpers
# -----------------------------------------------------------------------------

def summarize_deploy_results(results: Dict[str, Any]) -> Dict[str, Any]:
    """Return a single success/failure summary for a staged deploy run.

    Args:
        results (Dict[str, Any]): Deployment results keyed by workflow step.

    Returns:
        Dict[str, Any]: Summary dictionary containing success flag, message, and
            failed step names.
    """
    failed_steps: List[str] = []

    def _is_acceptable_result(step_result: Any) -> bool:
        """Return whether a deployment step result is successful or skipped.

        Args:
            step_result (Any): Individual step result to inspect.

        Returns:
            bool: True when the step explicitly succeeded or was skipped.
        """
        if not isinstance(step_result, dict):
            return False

        if step_result.get("success") is True:
            return True

        if step_result.get("skipped") is True:
            return True

        return False

    # Inspect each deployment step, including nested geography results.
    for step_name, step_result in (results or {}).items():
        if step_name == "geography_deletes" or step_name == "geography_adds":
            if isinstance(step_result, dict):
                for sub_key, sub_result in step_result.items():
                    if isinstance(sub_result, dict) and sub_result.get("success") is False:
                        failed_steps.append(f"{step_name}.{sub_key}")
                    elif _is_acceptable_result(sub_result):
                        continue
            continue

        if isinstance(step_result, dict):
            if step_result.get("success") is False:
                failed_steps.append(step_name)
            elif _is_acceptable_result(step_result):
                continue

    if failed_steps:
        return {
            "success": False,
            "message": "Deployment failed.",
            "failed_steps": failed_steps,
        }

    return {
        "success": True,
        "message": "Deployment completed successfully.",
        "failed_steps": [],
    }


def _update_deploy_progress(
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator],
    *,
    step: int,
    total_steps: int,
    message: str,
) -> None:
    """Update the deployment progress bar with a percentage and text message.

    Args:
        progress_placeholder (Optional[st.delta_generator.DeltaGenerator]):
            Streamlit placeholder used to render progress.
        step (int): Current deployment step number.
        total_steps (int): Total number of deployment steps.
        message (str): Progress text displayed to the user.

    Returns:
        None.
    """
    if progress_placeholder is None:
        return

    percent = int((step / total_steps) * 100) if total_steps else 100
    progress_placeholder.progress(percent, text=message)


# -----------------------------------------------------------------------------
# AGOL Deployment
# -----------------------------------------------------------------------------

def deploy_to_agol_footprint_update(
    *,
    project_payload: Dict[str, Any],
    old_footprint_layer: Optional[int],
    old_footprint_delete_payload: Dict[str, Any],
    locations_delete_payload: Dict[str, Any],
    new_footprint_layer: Optional[int],
    new_footprint_add_payload: Any,
    new_locations_add_payload: Any,
    geo_delete_payloads: Dict[str, Dict[str, Any]],
    geo_add_payloads: Dict[str, Any],
    traffic_impacts_update_payload: Optional[Dict[str, Any]] = None,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None,
) -> Dict[str, Any]:
    """Deploy the UPDATE FOOTPRINT workflow to AGOL.

    This function applies the footprint update workflow across the configured
    AGOL services and layers.

    APEX edits are sent through the APEX loader/service:
        * Projects
        * Existing footprint delete
        * Existing Locations delete
        * New footprint add
        * New Locations add
        * Geography deletes
        * Geography adds

    Traffic Impact edits are sent through the Traffic Impacts loader/service:
        * Connected Traffic Impact record updates

    The Traffic Impacts URL and layer are resolved from ``st.session_state``
    inside this function, consistent with the rest of the configured layer
    access in this file.

    Args:
        project_payload (Dict[str, Any]): Projects layer update payload.
        old_footprint_layer (Optional[int]): Layer index for the existing
            footprint geometry.
        old_footprint_delete_payload (Dict[str, Any]): Delete payload for the
            old footprint record.
        locations_delete_payload (Dict[str, Any]): Delete payload for existing
            locations records.
        new_footprint_layer (Optional[int]): Layer index for the new footprint
            geometry.
        new_footprint_add_payload (Any): Add payload or payload list for the new
            footprint geometry.
        new_locations_add_payload (Any): Add payload for new location records.
        geo_delete_payloads (Dict[str, Dict[str, Any]]): Geography delete payloads
            keyed by geography type.
        geo_add_payloads (Dict[str, Any]): Geography add payloads keyed by
            geography type.
        traffic_impacts_update_payload (Optional[Dict[str, Any]]): Optional update
            payload for connected Traffic Impact records.
        progress_placeholder (Optional[st.delta_generator.DeltaGenerator]):
            Optional Streamlit placeholder used to display deployment progress.

    Returns:
        Dict[str, Any]: Deployment results keyed by workflow step.
    """
    results: Dict[str, Any] = {}
    total_steps = 8
    current_step = 0

    # Read service URLs and layer indices from session_state at deployment time.
    apex_url = st.session_state.get("apex_url")
    traffic_impacts_url = st.session_state.get("traffic_impact_url")

    projects_layer = st.session_state.get("projects_layer")
    locations_layer = st.session_state.get("locations_layer")

    house_layer = st.session_state.get("house_layer")
    senate_layer = st.session_state.get("senate_layer")
    region_layer = st.session_state.get("region_layer")
    borough_layer = st.session_state.get("borough_layer")

    traffic_impacts_layer = st.session_state.get("traffic_impacts_layer")

    # -------------------------------------------------------------------------
    # Update parent Projects record
    # -------------------------------------------------------------------------
    current_step += 1
    _update_deploy_progress(
        progress_placeholder,
        step=current_step,
        total_steps=total_steps,
        message="Updating project record...",
    )

    if isinstance(project_payload, dict) and project_payload.get("updates"):
        parent_loader = AGOLDataLoader(url=apex_url, layer=projects_layer)
        results["project_update"] = parent_loader.update_features(
            payload=project_payload,
        )
    else:
        results["project_update"] = {
            "skipped": True,
            "reason": "No project update payload was built.",
        }

    # -------------------------------------------------------------------------
    # Delete old footprint record
    # -------------------------------------------------------------------------
    current_step += 1
    _update_deploy_progress(
        progress_placeholder,
        step=current_step,
        total_steps=total_steps,
        message="Deleting existing footprint record...",
    )

    if old_footprint_layer is not None and isinstance(old_footprint_delete_payload, dict):
        if old_footprint_delete_payload.get("deletes"):
            delete_footprint_loader = AGOLDataLoader(url=apex_url, layer=old_footprint_layer)
            results["old_footprint_delete"] = delete_footprint_loader.delete_features(
                payload=old_footprint_delete_payload,
            )
        else:
            results["old_footprint_delete"] = {
                "skipped": True,
                "reason": "No old footprint OBJECTIDs were found to delete.",
            }
    else:
        results["old_footprint_delete"] = {
            "skipped": True,
            "reason": "Old footprint layer or delete payload was not available.",
        }

    # -------------------------------------------------------------------------
    # Delete existing Location records
    # -------------------------------------------------------------------------
    current_step += 1
    _update_deploy_progress(
        progress_placeholder,
        step=current_step,
        total_steps=total_steps,
        message="Deleting existing location records...",
    )

    if isinstance(locations_delete_payload, dict) and locations_delete_payload.get("deletes"):
        delete_locations_loader = AGOLDataLoader(url=apex_url, layer=locations_layer)
        results["locations_delete"] = delete_locations_loader.delete_features(
            payload=locations_delete_payload,
        )
    else:
        results["locations_delete"] = {
            "skipped": True,
            "reason": "No existing location records were found to delete.",
        }

    # -------------------------------------------------------------------------
    # Delete existing Geography records
    # -------------------------------------------------------------------------
    results["geography_deletes"] = {}

    if isinstance(geo_delete_payloads, dict) and geo_delete_payloads:
        geography_delete_layer_map = {
            "house": house_layer,
            "senate": senate_layer,
            "region": region_layer,
            "borough": borough_layer,
        }

        for geo_key, delete_payload in geo_delete_payloads.items():
            target_layer = geography_delete_layer_map.get(geo_key)

            if target_layer is not None and isinstance(delete_payload, dict) and delete_payload.get("deletes"):
                _update_deploy_progress(
                    progress_placeholder,
                    step=current_step,
                    total_steps=total_steps,
                    message=f"Deleting existing {geo_key} geography records...",
                )

                geography_delete_loader = AGOLDataLoader(url=apex_url, layer=target_layer)
                results["geography_deletes"][geo_key] = geography_delete_loader.delete_features(
                    payload=delete_payload,
                )
            else:
                results["geography_deletes"][geo_key] = {
                    "skipped": True,
                    "reason": f"No existing {geo_key} geography records were found to delete.",
                }
    else:
        results["geography_deletes"] = {
            "skipped": True,
            "reason": "No geography delete payloads were provided.",
        }

    # -------------------------------------------------------------------------
    # Add new footprint record
    # -------------------------------------------------------------------------
    current_step += 1
    _update_deploy_progress(
        progress_placeholder,
        step=current_step,
        total_steps=total_steps,
        message="Adding new footprint record...",
    )

    if new_footprint_layer is not None:
        payloads = new_footprint_add_payload if isinstance(new_footprint_add_payload, list) else []

        if payloads:
            for new_payload in payloads:
                if isinstance(new_payload, dict) and new_payload.get("adds"):
                    new_footprint_loader = AGOLDataLoader(url=apex_url, layer=new_footprint_layer)
                    results["new_footprint_add"] = new_footprint_loader.add_features(
                        payload=new_payload,
                    )
                else:
                    results["new_footprint_add"] = {
                        "skipped": True,
                        "reason": "No new footprint add payload was built.",
                    }
        else:
            results["new_footprint_add"] = {
                "skipped": True,
                "reason": "New footprint layer or add payload was not available.",
            }
    else:
        results["new_footprint_add"] = {
            "skipped": True,
            "reason": "New footprint layer or add payload was not available.",
        }

    # -------------------------------------------------------------------------
    # Add new Location records
    # -------------------------------------------------------------------------
    current_step += 1
    _update_deploy_progress(
        progress_placeholder,
        step=current_step,
        total_steps=total_steps,
        message="Adding new location records...",
    )

    if isinstance(new_locations_add_payload, dict) and new_locations_add_payload.get("adds"):
        new_locations_loader = AGOLDataLoader(url=apex_url, layer=locations_layer)
        results["locations_add"] = new_locations_loader.add_features(
            payload=new_locations_add_payload,
        )
    else:
        results["locations_add"] = {
            "skipped": True,
            "reason": "No new location add payload was built.",
        }

    # -------------------------------------------------------------------------
    # Add new Geography records
    # -------------------------------------------------------------------------
    results["geography_adds"] = {}

    if isinstance(geo_add_payloads, dict) and geo_add_payloads:
        geography_add_layer_map = {
            "house": house_layer,
            "senate": senate_layer,
            "region": region_layer,
            "borough": borough_layer,
        }

        for geo_key, add_payload in geo_add_payloads.items():
            target_layer = geography_add_layer_map.get(geo_key)

            if target_layer is not None and isinstance(add_payload, dict) and add_payload.get("adds"):
                _update_deploy_progress(
                    progress_placeholder,
                    step=current_step,
                    total_steps=total_steps,
                    message=f"Adding new {geo_key} geography records...",
                )

                geography_add_loader = AGOLDataLoader(url=apex_url, layer=target_layer)
                results["geography_adds"][geo_key] = geography_add_loader.add_features(
                    payload=add_payload,
                )
            else:
                results["geography_adds"][geo_key] = {
                    "skipped": True,
                    "reason": f"No new {geo_key} geography add payload was built.",
                }
    else:
        results["geography_adds"] = {
            "skipped": True,
            "reason": "No geography add payloads were provided.",
        }

    # -------------------------------------------------------------------------
    # Update connected Traffic Impact records
    # -------------------------------------------------------------------------
    current_step += 1
    _update_deploy_progress(
        progress_placeholder,
        step=current_step,
        total_steps=total_steps,
        message="Updating connected traffic impact records...",
    )

    if isinstance(traffic_impacts_update_payload, dict) and traffic_impacts_update_payload.get("updates"):
        traffic_impacts_update_loader = AGOLDataLoader(url=traffic_impacts_url, layer=traffic_impacts_layer)
        results["traffic_impacts_update"] = traffic_impacts_update_loader.update_features(
            payload=traffic_impacts_update_payload,
        )
    else:
        results["traffic_impacts_update"] = {
            "skipped": True,
            "reason": "No traffic impact update payload was built.",
        }

    # -------------------------------------------------------------------------
    # Finish deploy
    # -------------------------------------------------------------------------
    if progress_placeholder is not None:
        progress_placeholder.progress(100, text="Footprint update deployed successfully.")

    return results


# =============================================================================
# Streamlit Page Rendering
# =============================================================================

def manage_footprint():
    """Render and manage the Project Footprint tab.

    This function loads the active project, detects whether an AASHTOWare
    connection exists, retrieves the stored footprint/geography/location records,
    displays the current footprint and boundary values, and manages the update
    workflow for loading and deploying replacement geometry.

    Returns:
        None: Streamlit components are rendered directly to the page.
    """
    # -------------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------------
    st.markdown("##### MANAGE PROJECT FOOTPRINT")
    st.caption(
        "This tab displays the existing footprint for the selected project and allows users to review it for accuracy. "
        "Users may update the footprint as needed to reflect current project conditions or scope."
    )

    # -------------------------------------------------------------------------
    # Session State Access: AGOL Layer Configuration
    # -------------------------------------------------------------------------

    # APEX URL
    base_url = st.session_state.get("apex_url")

    # Projects Layer
    projects_layer = st.session_state.get("projects_layer")

    # Footprint Layers
    sites_layer = st.session_state.get("sites_layer")
    routes_layer = st.session_state.get("routes_layer")
    boundaries_layer = st.session_state.get("boundaries_layer")

    # Geography Layers
    region_layer = st.session_state.get("region_layer")
    bor_layer = st.session_state.get("bor_layer")
    senate_layer = st.session_state.get("senate_layer")
    house_layer = st.session_state.get("house_layer")

    # Validate required layer configuration and surface existing UI messages.
    if base_url is None or projects_layer is None:
        st.error("AGOL Projects layer is not configured (missing apex_url or projects_layer).")

    if sites_layer is None or routes_layer is None or boundaries_layer is None:
        st.error("AGOL Footprints layers are not configured (UPDATE THIS).")

    if region_layer is None or bor_layer is None or senate_layer is None or house_layer is None:
        st.error("AGOL Geospatial layers are not configured (UPDATE THIS).")

    # -------------------------------------------------------------------------
    # Load Active Project Record
    # -------------------------------------------------------------------------

    # Pull Footprint Information from Project Record.
    rec = _get_project_record()

    # -------------------------------------------------------------------------
    # AWP Geometry Lookup
    # -------------------------------------------------------------------------

    # If the project has a valid AWP_Contract_ID, pull AWP geometry.
    awp_id = rec.get("AWP_Contract_ID") if rec else None
    st.session_state["awp_id"] = awp_id

    if _is_valid_awp_contract_id(awp_id):
        st.session_state["is_awp"] = True
        st.session_state["awp_geometry_points"] = aashtoware_geometry(awp_id)
    else:
        st.session_state["is_awp"] = False
        st.session_state["awp_geometry_points"] = None

    # -------------------------------------------------------------------------
    # Resolve Existing Project Type
    # -------------------------------------------------------------------------

    proj_type = rec.get("Proj_Type")

    # -------------------------------------------------------------------------
    # Pull Existing Footprint Record
    # -------------------------------------------------------------------------

    footprint_rec = None

    if proj_type == "Site":
        footprint_rec = select_record(
            url=base_url,
            layer=sites_layer,
            id_field="parentglobalid",
            id_value=st.session_state["apex_guid"],
            fields="*",
            return_geometry=True,
        )
    elif proj_type == "Route":
        footprint_rec = select_record(
            url=base_url,
            layer=routes_layer,
            id_field="parentglobalid",
            id_value=st.session_state["apex_guid"],
            fields="*",
            return_geometry=True,
        )
    elif proj_type == "Boundary":
        footprint_rec = select_record(
            url=base_url,
            layer=boundaries_layer,
            id_field="parentglobalid",
            id_value=st.session_state["apex_guid"],
            fields="*",
            return_geometry=True,
        )

    # -------------------------------------------------------------------------
    # Store Existing Geography Summary Values
    # -------------------------------------------------------------------------

    st.session_state["List_House_District"] = rec.get("List_House_District")
    st.session_state["List_Senate_District"] = rec.get("List_Senate_District")
    st.session_state["List_Borough_Census_Area"] = rec.get("List_Borough_Census_Area")
    st.session_state["List_DOT_PF_Region"] = rec.get("List_DOT_PF_Region")

    # -------------------------------------------------------------------------
    # Pull Existing Geography Records
    # -------------------------------------------------------------------------

    # Pull stored geography layer records by Project GUID. These are not the
    # intersect services; these are the APEX geography layers.
    apex_guid = st.session_state.get("apex_guid")
    geography_raw = {"region": None, "borough": None, "senate": None, "house": None}

    if apex_guid:
        geography_raw["region"] = select_record(
            url=base_url,
            layer=region_layer,
            id_field="parentglobalid",
            id_value=apex_guid,
            fields="*",
            return_geometry=False,
        )
        geography_raw["borough"] = select_record(
            url=base_url,
            layer=bor_layer,
            id_field="parentglobalid",
            id_value=apex_guid,
            fields="*",
            return_geometry=False,
        )
        geography_raw["senate"] = select_record(
            url=base_url,
            layer=senate_layer,
            id_field="parentglobalid",
            id_value=apex_guid,
            fields="*",
            return_geometry=False,
        )
        geography_raw["house"] = select_record(
            url=base_url,
            layer=house_layer,
            id_field="parentglobalid",
            id_value=apex_guid,
            fields="*",
            return_geometry=False,
        )

    # Store raw and normalized feature lists for downstream payload/deletes.
    st.session_state["geography_raw_records"] = geography_raw
    st.session_state["geography_records"] = {k: _normalize_features(v) for k, v in geography_raw.items()}
    st.session_state["region_records"] = st.session_state["geography_records"]["region"]
    st.session_state["borough_records"] = st.session_state["geography_records"]["borough"]
    st.session_state["senate_records"] = st.session_state["geography_records"]["senate"]
    st.session_state["house_records"] = st.session_state["geography_records"]["house"]

    # -------------------------------------------------------------------------
    # Pull Existing Location Records
    # -------------------------------------------------------------------------

    locations_rec = None
    locations_layer = st.session_state.get("locations_layer")

    if apex_guid and locations_layer is not None:
        locations_rec = select_record(
            url=base_url,
            layer=locations_layer,
            id_field="parentglobalid",
            id_value=apex_guid,
            fields="*",
            return_geometry=False,
        )

    # Store raw and normalized location records for downstream payload/deletes.
    st.session_state["locations_raw_record"] = locations_rec
    st.session_state["locations_records"] = _normalize_features(locations_rec)

    # -------------------------------------------------------------------------
    # Package Existing Footprint Geometry
    # -------------------------------------------------------------------------

    # Package footprint coordinates into a single item by project type:
    # Site -> points, Route -> lines, Boundary -> polygons.
    geom_type_map = {
        "Site": "point",
        "Route": "line",
        "Boundary": "polygon",
    }
    packed_geom_type = geom_type_map.get(proj_type)

    def _extract_geometries(geom: Dict[str, Any]):
        """Return geometry coordinate sets from an Esri geometry object.

        All geometry is normalized to a list of distinct geometries so downstream
        code can treat it consistently:

            * point/multipoint -> [[[x, y], ...]]
            * polyline -> [[[x, y], ...], ...]
            * polygon -> [[[x, y], ...], ...]

        Key rule for Sites:
            A single point or a list-of-one-point is converted to ``[[x, y]]``
            so ``geometry_to_folium(feature_type="point")`` works with the
            existing ``for geom_coords in geoms:`` loop.

        Notes:
            ArcGIS may return point geometry as ``{"x": x, "y": y}`` or
            multipoint geometry as ``{"points": [...]}``. Geometry may also be
            serialized JSON, which is handled here.

        Args:
            geom (Dict[str, Any]): Esri geometry object or geometry-like value.

        Returns:
            list: Normalized list of geometry coordinate sets.
        """
        out: List[Any] = []

        # ArcGIS can sometimes hand us geometry as a JSON string.
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                return out

        # Defensive: if geom is already a coordinate container.
        if not isinstance(geom, dict):
            if isinstance(geom, (list, tuple)) and len(geom) > 0:
                # Case: [x, y]
                if len(geom) >= 2 and all(isinstance(v, (int, float)) for v in geom[:2]):
                    x, y = geom[0], geom[1]
                    out.append([[x, y]])
                    return out

                # Case: [[x, y], ...]
                if all(isinstance(pt, (list, tuple)) and len(pt) >= 2 for pt in geom):
                    pts: List[List[float]] = []
                    for pt in geom:
                        x, y = pt[0], pt[1]
                        if x is not None and y is not None:
                            pts.append([x, y])
                    if pts:
                        out.append(pts)
                    return out

            return out

        # Point: {x, y} -> [[[x, y]]]
        if "x" in geom and "y" in geom:
            x = geom.get("x")
            y = geom.get("y")
            if x is not None and y is not None:
                out.append([[x, y]])
            return out

        # Multipoint: {points:[[x,y],...]} -> [[[x,y],[x,y],...]]
        pts = geom.get("points")
        if isinstance(pts, list):
            gathered: List[List[float]] = []

            for pt in pts:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    x, y = pt[0], pt[1]
                    if x is not None and y is not None:
                        gathered.append([x, y])

            if gathered:
                out.append(gathered)
            return out

        # Polyline: {paths:[[[x,y],...], ...]} -> one geometry per path.
        paths = geom.get("paths")
        if isinstance(paths, list):
            for path in paths:
                if not isinstance(path, list):
                    continue

                coords: List[List[float]] = []
                for pt in path:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        coords.append([pt[0], pt[1]])

                if coords:
                    out.append(coords)

            return out

        # Polygon: {rings:[[[x,y],...], ...]} -> one geometry per ring.
        rings = geom.get("rings")
        if isinstance(rings, list):
            for ring in rings:
                if not isinstance(ring, list):
                    continue

                coords: List[List[float]] = []
                for pt in ring:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        coords.append([pt[0], pt[1]])

                if coords:
                    out.append(coords)

            return out

        return out

    geometries = []

    if footprint_rec is not None:
        # select_record may return a list of features, a single feature, or a
        # dictionary containing a features list.
        if isinstance(footprint_rec, dict) and isinstance(footprint_rec.get("features"), list):
            features = footprint_rec.get("features") or []
        else:
            features = footprint_rec if isinstance(footprint_rec, list) else [footprint_rec]

        for feat in features:
            geom = (feat or {}).get("geometry")

            if geom is not None:
                geometries.extend(_extract_geometries(geom))

    footprint_item = {
        "type": proj_type,
        "geometry_type": packed_geom_type,
        "geometries": geometries,
    }

    # Store packaged footprint so the map section can read it on this rerun.
    st.session_state["footprint_item"] = footprint_item
    st.write("")

    # -------------------------------------------------------------------------
    # Footprint Display / Update Geometry Loader
    # -------------------------------------------------------------------------

    footprint_container = st.container(border=False)

    with footprint_container:
        # When UPDATE FOOTPRINT is selected, replace the display with the geometry loader.
        if st.session_state.get("update_footprint_mode", False):
            with st.container(border=False):
                load_geometry_app()
                st.write("")
        else:
            st.markdown("###### CONSTRUCTION FOOTPRINT")

            with st.container(border=True):
                # Prefer the freshly-built item; fall back to session_state if needed.
                item = footprint_item or (st.session_state.get("footprint_item") or {})
                geom_kind = item.get("geometry_type")
                geoms = item.get("geometries", [])

                if geoms:
                    m = folium.Map(
                        location=[63.833333, -152.0],  # Alaska center (fixed anchor)
                        zoom_start=4,
                        control_scale=True,
                    )

                    # Add each geometry to the Folium map using the existing map helper.
                    for geom_coords in geoms:
                        layer = geometry_to_folium(
                            geom_coords,
                            feature_type={
                                "point": "point",
                                "line": "polyline",
                                "polygon": "polygon",
                            }.get(geom_kind),
                            color="#00bcd4",
                            weight=6,
                            opacity=0.85,
                            fill=(geom_kind == "polygon"),
                            fill_color="#00bcd4",
                            fill_opacity=0.25,
                            point_shape="circle",
                            point_radius=8,
                            point_color="#00bcd4",
                            point_weight=3,
                            point_fill_color="#00bcd4",
                            point_fill_opacity=0.85,
                        )
                        layer.add_to(m)

                    def _fallback_bounds_from_geoms(_geom_kind, _geoms):
                        """Build fallback Folium bounds directly from geometry coordinates.

                        Args:
                            _geom_kind: Current geometry type string.
                            _geoms: Geometry coordinate list.

                        Returns:
                            list | None: Bounds in ``[[min_lat, min_lon], [max_lat, max_lon]]``
                                format when coordinates are available; otherwise, None.
                        """
                        pts = []

                        for g in _geoms or []:
                            if _geom_kind == "point":
                                if isinstance(g, (list, tuple)) and len(g) >= 2:
                                    x, y = g[0], g[1]
                                    if x is not None and y is not None:
                                        pts.append((y, x))  # (lat, lon)
                            else:
                                if isinstance(g, list):
                                    for pt in g:
                                        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                                            x, y = pt[0], pt[1]
                                            if x is not None and y is not None:
                                                pts.append((y, x))  # (lat, lon)

                        if not pts:
                            return None

                        lats = [p[0] for p in pts]
                        lons = [p[1] for p in pts]

                        return [[min(lats), min(lons)], [max(lats), max(lons)]]

                    # Fit bounds based on geometry type.
                    bounds = None

                    try:
                        if geom_kind == "point":
                            bounds = set_bounds_point(geoms)
                        elif geom_kind == "line":
                            bounds = set_bounds_route(geoms)
                        elif geom_kind == "polygon":
                            bounds = set_bounds_boundary(geoms)
                    except TypeError:
                        # Some implementations may expect the map instance as
                        # the first argument.
                        try:
                            if geom_kind == "point":
                                bounds = set_bounds_point(m, geoms)
                            elif geom_kind == "line":
                                bounds = set_bounds_route(m, geoms)
                            elif geom_kind == "polygon":
                                bounds = set_bounds_boundary(m, geoms)
                        except Exception:
                            bounds = None

                    if not bounds:
                        bounds = _fallback_bounds_from_geoms(geom_kind, geoms)

                    if bounds:
                        try:
                            m.fit_bounds(bounds)
                        except Exception:
                            pass

                    st_folium(
                        m,
                        height=420,
                        width=None,
                        returned_objects=[],  # prevent reruns on pan/zoom
                        key="footprint_map",
                    )

                else:
                    st.caption("No footprint geometry is available for this project.")

            st.write("")

            # -----------------------------------------------------------------
            # Legislative and Administrative Boundaries
            # -----------------------------------------------------------------
            st.markdown("###### LEGISLATIVE AND ADMINISTRATIVE BOUNDARIES")

            with st.container(border=True):
                house_val = st.session_state["List_House_District"]
                senate_val = st.session_state["List_Senate_District"]
                borough_val = st.session_state["List_Borough_Census_Area"]
                region_val = st.session_state["List_DOT_PF_Region"]

                col1, col2 = st.columns(2)

                col1.markdown(f"**House Districts:** {house_val or '—'}")
                col2.markdown(f"**Senate Districts:** {senate_val or '—'}")
                col1.markdown(f"**Boroughs:** {borough_val or '—'}")
                col2.markdown(f"**Regions:** {region_val or '—'}")

    # -------------------------------------------------------------------------
    # Inner Workflow Helpers
    # -------------------------------------------------------------------------

    def _reset_load_geometry_state():
        """Reset load_geometry session state so the loader starts fresh.

        Returns:
            None: load_geometry-related session_state keys are reset in place.
        """
        # Reset load_geometry submission flags.
        st.session_state["footprint_submitted"] = False
        st.session_state["just_submitted_geometry"] = False

        # Clear previously-submitted geometry.
        st.session_state["project_geometry"] = None
        st.session_state["project_geom_type"] = None
        st.session_state["project_geom"] = None

        # Clear selection and widget-tracking state used by load_geometry_app.
        st.session_state["selected_point"] = None
        st.session_state["selected_route"] = None
        st.session_state["selected_boundary"] = None
        st.session_state["option"] = None
        st.session_state["prev_geometry_option"] = None
        st.session_state["prev_geometry_project_type"] = None
        st.session_state["submitted_geom_sig"] = None
        st.session_state["submitted_option"] = None
        st.session_state["submitted_project_type"] = None

        # Bump the version so widget keys inside load_geometry_app rebuild.
        st.session_state["geometry_form_version"] = int(st.session_state.get("geometry_form_version", 0)) + 1

        def _clear_footprint_and_load_geometry_state():
            """Clear session_state keys created by footprint.py and load_geometry.

            Returns:
                None: Keys are removed from session_state when present.
            """
            keys_to_clear = {
                # --- footprint.py-created keys ---
                "update_footprint_mode",
                "is_awp",
                "awp_id",
                "awp_geometry_points",
                "footprint_item",
                "deploy_objectids",
                "last_footprint_deploy_result",
                "locations_raw_record",
                "locations_records",
                "geography_raw_records",
                "geography_records",
                "house_records",
                "senate_records",
                "region_records",
                "borough_records",
                "List_House_District",
                "List_Senate_District",
                "List_DOT_PF_Region",
                "List_Borough_Census_Area",

                # --- load_geometry-created keys (from load_geometry step) ---
                "footprint_submitted",
                "just_submitted_geometry",
                "project_geometry",
                "project_geom_type",
                "project_geom",
                "selected_point",
                "selected_route",
                "selected_boundary",
                "option",
                "prev_geometry_option",
                "prev_geometry_project_type",
                "submitted_geom_sig",
                "submitted_option",
                "submitted_project_type",
                "geometry_form_version",
                "project_type",
                "geom_type",
                "house_string",
                "senate_string",
                "region_string",
                "borough_string",
            }

            for k in keys_to_clear:
                st.session_state.pop(k, None)

    def _reset_to_fresh_run_after_deploy():
        """Clear state and rerun the script from the top.

        Returns:
            None: Session state is cleared through the outer helper, then
                Streamlit rerun is requested.
        """
        _clear_footprint_and_load_geometry_state()
        st.rerun()

    def _enter_update_footprint_mode():
        """Enter the UPDATE FOOTPRINT workflow.

        Returns:
            None: Session state is updated and load_geometry state is reset.
        """
        st.session_state["update_footprint_mode"] = True
        _reset_load_geometry_state()

    def _deploy_footprint_update(
        progress_placeholder: st.delta_generator.DeltaGenerator,
        project_rec: Dict[str, Any],
        footprint_rec_any: Any,
        locations_rec_any: Any,
    ) -> None:
        """Build staged payloads and run the footprint deployment helper.

        Active payloads:
            * Project payload.
            * Old footprint deletes payload.
            * Locations deletes payload.
            * Geography deletes payloads.
            * New footprint add payload.
            * New location add payload.
            * Geography add payloads.
            * Traffic Impacts update payload.

        This function also captures and persists OBJECTIDs for project,
        footprint, locations, and geography records.

        Args:
            progress_placeholder (st.delta_generator.DeltaGenerator): Streamlit
                progress placeholder.
            project_rec (Dict[str, Any]): Active parent project attributes.
            footprint_rec_any (Any): Raw existing footprint record result.
            locations_rec_any (Any): Raw existing locations record result.

        Returns:
            None: Deployment results are stored in session_state and rendered
                through Streamlit when failures occur.
        """
        apex_guid = st.session_state.get("apex_guid")

        old_proj_type = (project_rec or {}).get("Proj_Type")
        new_proj_type = _resolve_new_project_type() or old_proj_type

        old_footprint_layer = _project_type_to_footprint_layer(old_proj_type)
        new_footprint_layer = _project_type_to_footprint_layer(new_proj_type)

        # ---------------------------------------------------------------------
        # OBJECTID CAPTURE
        # ---------------------------------------------------------------------
        project_objectid = _get_objectid_from_attributes(project_rec or {})

        footprint_features = _normalize_features(footprint_rec_any)
        footprint_objectids = _collect_objectids_from_features(footprint_features)

        locations_features = _normalize_features(locations_rec_any)
        locations_objectids = _collect_objectids_from_features(locations_features)

        geo_records = st.session_state.get("geography_records") or {}
        if not isinstance(geo_records, dict) or not geo_records:
            geo_records = {
                "house": st.session_state.get("house_records"),
                "senate": st.session_state.get("senate_records"),
                "region": st.session_state.get("region_records"),
                "borough": st.session_state.get("borough_records"),
            }

        geography_objectids: Dict[str, List[int]] = {}
        for layer in ("house", "senate", "borough", "region"):
            feats = _normalize_features(geo_records.get(layer))
            geography_objectids[layer] = _collect_objectids_from_features(feats)

        geography_objectids_all = (
            geography_objectids.get("house", [])
            + geography_objectids.get("senate", [])
            + geography_objectids.get("borough", [])
            + geography_objectids.get("region", [])
        )

        st.session_state["deploy_objectids"] = {
            "project": project_objectid,
            "footprint": footprint_objectids,
            "locations": locations_objectids,
            "geography": geography_objectids,
            "geography_all": geography_objectids_all,
        }

        # ---------------------------------------------------------------------
        # Payloads
        # ---------------------------------------------------------------------

        # Build parent project update payload.
        project_payload = manage_footprint_project_payload(project_objectid)

        # Resolve new project type from the project payload when available.
        proj_type_from_payload = None
        try:
            upd0 = (project_payload.get("updates") or [])[0] if isinstance(project_payload, dict) else {}
            attrs0 = (upd0 or {}).get("attributes") or {}
            proj_type_from_payload = (
                attrs0.get("proj_type")
                or attrs0.get("Proj_Type")
                or attrs0.get("PROJ_TYPE")
                or attrs0.get("ProjType")
            )
        except Exception:
            proj_type_from_payload = None

        if proj_type_from_payload:
            new_proj_type = proj_type_from_payload

        # Re-resolve new footprint layer based on new project type.
        new_footprint_layer = _project_type_to_footprint_layer(new_proj_type)

        # Delete payloads for old footprint, existing locations, and geography records.
        old_footprint_delete_payload = manage_footprint_deletes_payload(footprint_objectids)
        locations_delete_payload = manage_footprint_deletes_payload(locations_objectids)

        geography_delete_payloads: Dict[str, Dict[str, Any]] = {}
        for layer in ("house", "senate", "borough", "region"):
            geography_delete_payloads[layer] = manage_footprint_deletes_payload(
                geography_objectids.get(layer, [])
            )

        # Ensure payload-builder session-state keys exist in manager context.
        _seed_payload_builder_state_from_project(project_rec)

        # Add payloads for new locations and footprint geometry.
        new_location_payload = location_payload()
        new_footprint_payload = geometry_payload()

        geography_add_payloads: Dict[str, Dict[str, Any]] = {}
        for layer in ("house", "senate", "borough", "region"):
            geography_add_payloads[layer] = geography_payload(layer)

        # Build update payload for connected Traffic Impact records.
        traffic_impacts_update_payload = build_traffic_impacts_update_payload(
            apex_guid=apex_guid,
        )

        # ---------------------------------------------------------------------
        # Deploy
        # ---------------------------------------------------------------------
        deploy_result = deploy_to_agol_footprint_update(
            project_payload=project_payload,
            old_footprint_layer=old_footprint_layer,
            old_footprint_delete_payload=old_footprint_delete_payload,
            locations_delete_payload=locations_delete_payload,
            new_footprint_layer=new_footprint_layer,
            new_footprint_add_payload=new_footprint_payload,
            new_locations_add_payload=new_location_payload,
            geo_delete_payloads=geography_delete_payloads,
            geo_add_payloads=geography_add_payloads,
            traffic_impacts_update_payload=traffic_impacts_update_payload,
            progress_placeholder=progress_placeholder,
        )

        summary = summarize_deploy_results(deploy_result or {})

        # Persist last result for review/debug. Payloads are not printed inline.
        st.session_state["last_footprint_deploy_result"] = {
            **(deploy_result or {}),
            "summary": summary,
            "objectids": st.session_state.get("deploy_objectids"),
            "old_proj_type": old_proj_type,
            "new_proj_type": new_proj_type,
            "old_footprint_layer": old_footprint_layer,
            "new_footprint_layer": new_footprint_layer,
            "apex_guid": apex_guid,
        }

        if summary.get("success") is True:
            _reset_to_fresh_run_after_deploy()
            return

        st.error(summary.get("message", "Deployment failed."))

        with st.expander("Deployment results", expanded=False):
            st.markdown(project_payload)
            st.markdown(new_footprint_layer)
            st.markdown(new_footprint_payload)
            st.json(st.session_state["last_footprint_deploy_result"])

    # -------------------------------------------------------------------------
    # Update Button Logic
    # -------------------------------------------------------------------------

    in_update_mode = bool(st.session_state.get("update_footprint_mode", False))

    has_loaded_geometry = st.session_state.get("project_geometry") is not None

    submitted_ok = (
        bool(st.session_state.get("footprint_submitted"))
        or bool(st.session_state.get("just_submitted_geometry"))
        or bool(st.session_state.get("submitted_geom_sig"))
    )

    can_update = bool(has_loaded_geometry and submitted_ok)

    if not in_update_mode:
        st.button(
            "UPDATE FOOTPRINT",
            key="enter_update_footprint_btn",
            type="primary",
            use_container_width=True,
            on_click=_enter_update_footprint_mode,
        )
    else:
        clicked = st.button(
            "UPDATE FOOTPRINT",
            key="deploy_update_footprint_btn",
            type="primary",
            use_container_width=True,
            disabled=not can_update,
        )

        # Progress bar placeholder is below the update button.
        progress_placeholder = st.empty()

        if clicked and can_update:
            _deploy_footprint_update(progress_placeholder, rec or {}, footprint_rec, locations_rec)

        st.button(
            "CANCEL",
            key="cancel_update_footprint_btn",
            use_container_width=True,
            on_click=lambda: st.session_state.__setitem__("update_footprint_mode", False),
        )