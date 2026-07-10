"""Project data source management for the APEX Streamlit manager page.

This module supports the Source tab in the larger Streamlit application. It
retrieves the active AGOL project record, manages the displayed source mode,
coordinates AASHTOWare connection state, builds AGOL update payloads, deploys
source-related updates to AGOL layers, and renders the Streamlit controls used
for viewing, changing, updating, canceling, or removing a project data source.

The cleanup in this file is limited to documentation and organization. Existing
Streamlit UI text, session state keys, function names, variable names, payload
fields, and execution behavior are intentionally preserved.
"""

# =============================================================================
# Imports
# =============================================================================
# Standard library
from typing import Any, Dict, Optional

# Third-party
import streamlit as st

# Local application: AGOL access + payload builders
from agol.agol_payloads import (
    manage_information_payload,
    manage_project_name_update,
)
from agol.agol_util import (
    AGOLDataLoader,
    select_record,
)

# Local application: AASHTOWare connection UI + formatting + read-only widgets
from util.aashtoware_util import manage_aashtoware_connection
from util.input_util import fmt_agol_date
from util.read_only_util import ro_widget


# =============================================================================
# Constants
# =============================================================================
# Session-state flag used across the Source and Information tabs to indicate
# that the user is actively inside a connect/change AASHTOWare flow. Consumers
# elsewhere in the app rely on this key name, so it must not be renamed.
INFO_AWP_TRIGGER_KEY = "info_awp_trigger_active"


# =============================================================================
# Session State Access Notes
# =============================================================================
# This file reads and mutates Streamlit session state throughout callback and UI
# execution. Reads intentionally remain near their original usage points because
# several values are updated later in the same request cycle, and centralizing
# those reads would risk changing behavior.


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

# -----------------------------------------------------------------------------
# Active Project Helpers
# -----------------------------------------------------------------------------
def _get_project_record():
    """Return the active AGOL project record attributes for the current APEX GUID.

    Returns:
        dict | None: Attribute dictionary for the selected project record when the
            required session state values are available and a matching AGOL record is
            found; otherwise, None.
    """
    # Pull the active project identifiers from session state at the point of use.
    apex_guid = st.session_state.get("apex_guid")
    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")
    if not (apex_guid and url and layer is not None):
        return None

    # Query AGOL for the active project record without returning geometry.
    recs = select_record(
        url=url,
        layer=layer,
        id_field="globalid",
        id_value=apex_guid,
        fields="*",
        return_geometry=False,
    )
    return recs[0]["attributes"] if recs else None


def _sync_manage_existing_id_from_project(project: dict):
    """
    Keep the manage-selector's 'current APEX connection' marker aligned with the
    AWP_Contract_ID that is actually saved on the AGOL project record.
    """
    project = project or {}
    # The "committed" AWP id is whatever is currently persisted on the AGOL record.
    committed_awp_id = project.get("AWP_Contract_ID") or None

    # Store the committed AGOL connection marker used by the manage selector.
    st.session_state["awp_manage_existing_id"] = committed_awp_id

    # Mirror the committed id into apex_awp_id so downstream consumers can
    # detect the currently-linked project. Clear it when no connection exists.
    if committed_awp_id:
        st.session_state["apex_awp_id"] = committed_awp_id
    else:
        st.session_state.pop("apex_awp_id", None)


# -----------------------------------------------------------------------------
# Data Source Mode Helpers
# -----------------------------------------------------------------------------
def manage_source():
    """Render and manage the Source tab for the active Streamlit project.

    This function preserves the existing Source tab workflow, including source
    summary rendering, AASHTOWare connection controls, read-only widget handling,
    and update/remove action routing.

    Returns:
        None: Streamlit components are rendered directly to the page.
    """
    # NOTE: The setdefault calls here reflect existing behavior for first-load
    # defaults and are left unchanged. They only create keys if they do not
    # already exist; they never overwrite user- or workflow-provided values.
    st.session_state.setdefault(INFO_AWP_TRIGGER_KEY, False)
    st.session_state.setdefault("awp_manage_show_details", False)
    st.session_state.setdefault("awp_selection_made", False)


def _resolve_is_awp(project_attrs: dict) -> bool:
    """
    Prefer the active source selection from session state, otherwise fall back to
    the current AGOL record.
    """
    # An active user selection (from this session) always wins over the record.
    details_type = st.session_state.get("details_type") or st.session_state.get("info_option")
    # Respect the active source selection before falling back to saved AGOL data.
    if details_type in ("AASHTOWare Database", "User Input"):
        return details_type == "AASHTOWare Database"
    return bool((project_attrs or {}).get("AWP_Contract_ID"))


def _current_awp_contract_id(project: dict):
    """
    Show the currently selected AASHTOWare contract while connect/change is active,
    otherwise show the contract already saved on the AGOL project record.
    """
    project = project or {}

    # Outside the connection workflow, display the contract committed to AGOL.
    if not st.session_state.get(INFO_AWP_TRIGGER_KEY, False):
        return project.get("AWP_Contract_ID") or ""

    # During connect/change, prefer the staged/selector values in priority order.
    return (
        st.session_state.get("awp_manage_id")
        or st.session_state.get("awp_manage_guid")
        or st.session_state.get("apex_awp_id")
        or st.session_state.get("awp_id")
        or st.session_state.get("awp_guid")
        or st.session_state.get("aashto_id")
        or project.get("AWP_Contract_ID")
        or ""
    )


def _clear_manage_awp_state():
    """Clear temporary AASHTOWare manage-selection session state.

    The existing persistent source-management keys are preserved while transient
    ``awp_manage_`` keys are removed and the selection flag is restored to its
    unselected state when it had been set.

    Returns:
        None.
    """
    # Preserve the source-management keys that must survive selector resets.
    keep_keys = {
        INFO_AWP_TRIGGER_KEY,
        "info_show_awp_selector",
        "awp_manage_existing_id",
    }
    # Remove all transient awp_manage_* keys not on the keep list.
    for key in list(st.session_state.keys()):
        if key.startswith("awp_manage_") and key not in keep_keys:
            try:
                del st.session_state[key]
            except Exception:
                # Deletion may race with Streamlit reruns; suppress silently to
                # preserve the original defensive behavior.
                pass

    # Reset the selection flag only if it was previously toggled True.
    if st.session_state.get("awp_selection_made") == True:
        st.session_state["awp_selection_made"] = False


def _show_awp_selector():
    """
    Flip session flags so the source summary is hidden and the manage AWP selector shows.
    """
    st.session_state["info_show_awp_selector"] = True
    st.session_state["awp_manage_show_details"] = True


def _seed_awp_manage_default_from_project(project: dict):
    """
    Seed the manage AASHTOWare selector from the current project's saved contract id.
    """
    project = project or {}
    awp_contract_id = project.get("AWP_Contract_ID")
    # Only seed when the project actually carries a saved contract id.
    if awp_contract_id:
        st.session_state["awp_manage_id"] = awp_contract_id
        st.session_state["awp_manage_guid"] = awp_contract_id


# -----------------------------------------------------------------------------
# Selected AASHTOWare Helpers
# -----------------------------------------------------------------------------
def _get_selected_manage_awp_id() -> Optional[str]:
    """
    Return the currently selected AASHTOWare project identifier.

    Returns:
        str | None: The first available selected AASHTOWare identifier from the
            existing session state key sequence, or None when no identifier is set.
    """
    # Maintain the existing priority order across possible selector state keys.
    selected_id = (
        st.session_state.get("awp_manage_id")
        or st.session_state.get("awp_manage_guid")
        or st.session_state.get("apex_awp_id")
        or st.session_state.get("awp_id")
        or st.session_state.get("awp_guid")
        or st.session_state.get("aashto_id")
    )
    if selected_id in (None, ""):
        return None
    return str(selected_id)


def _get_selected_manage_awp_attrs() -> Dict[str, Any]:
    """
    Resolve the currently selected AASHTOWare project's attributes from session.
    This supports the existing manage_aashtoware_connection() flow without changing
    that utility.
    """
    # First inspect structured records already captured by the selector workflow.
    candidates = [
        st.session_state.get("awp_manage_attrs"),
        st.session_state.get("awp_manage_record"),
        st.session_state.get("awp_manage_selected_record"),
        st.session_state.get("info_awp_attrs"),
    ]

    # Return the first candidate that looks like an AGOL feature or attribute dict.
    for candidate in candidates:
        if isinstance(candidate, dict):
            if isinstance(candidate.get("attributes"), dict):
                return candidate["attributes"]
            return candidate

    attrs: Dict[str, Any] = {}
    # Fall back to individual session state fields populated by existing utilities.
    fallback_map = {
        "Id": ["awp_manage_id", "awp_manage_guid", "apex_awp_id"],
        "ProjectName": ["awp_manage_proj_name", "awp_manage_project_name", "awp_proj_name", "proj_name"],
        "Description": ["awp_manage_proj_desc", "awp_manage_project_desc", "awp_proj_desc", "proj_desc"],
        "Phase": ["awp_manage_phase", "awp_phase", "phase"],
        "FundingType": ["awp_manage_fund_type", "awp_fund_type", "fund_type"],
        "ProjectPractice": ["awp_manage_proj_prac", "awp_proj_prac", "proj_prac"],
        "IRIS": ["awp_manage_iris", "awp_iris", "iris"],
        "STIP": ["awp_manage_stip", "awp_stip", "stip"],
        "FederalProjectNumber": ["awp_manage_fed_proj_num", "awp_fed_proj_num", "fed_proj_num"],
        "AnticipatedStart": ["awp_manage_anticipated_start", "awp_anticipated_start", "anticipated_start"],
        "AnticipatedEnd": ["awp_manage_anticipated_end", "awp_anticipated_end", "anticipated_end"],
        "AwardDate": ["awp_manage_award_date", "awp_award_date", "award_date"],
        "AwardFiscalYear": ["awp_manage_award_fiscal_year", "awp_award_fiscal_year", "award_fiscal_year"],
        "TentativeAdvertiseDate": ["awp_manage_tenadd", "awp_tenadd", "tenadd"],
        "AwardedAmount": ["awp_manage_awarded_amount", "awp_awarded_amount", "awarded_amount"],
        "CurrentContractAmount": ["awp_manage_current_contract_amount", "awp_current_contract_amount", "current_contract_amount"],
        "AmountPaidToDate": ["awp_manage_amount_paid_to_date", "awp_amount_paid_to_date", "amount_paid_to_date"],
        "Contractor": ["awp_manage_contractor", "contractor"],
        "ContactName": ["awp_manage_contact_name", "awp_contact_name", "contact_name"],
        "ContactEmail": ["awp_manage_contact_email", "awp_contact_email", "contact_email"],
        "ContactPhone": ["awp_manage_contact_phone", "awp_contact_phone", "contact_phone"],
        "ProjectWebsite": ["awp_manage_proj_web", "awp_proj_web", "proj_web"],
    }

    # For each AWP attribute, pick the first populated key in the fallback list.
    for attr_name, keys in fallback_map.items():
        for key in keys:
            value = st.session_state.get(key)
            if value not in (None, ""):
                attrs[attr_name] = value
                break

    return attrs


def _selected_awp_value(attrs: Dict[str, Any], *names: str):
    """Return the first non-empty AASHTOWare attribute value from the provided names.

    Args:
        attrs (Dict[str, Any]): Attribute dictionary to inspect.
        *names (str): Candidate attribute names in priority order.

    Returns:
        Any: The first non-empty value found, or None when no value is available.
    """
    # Return the first populated attribute in the requested priority order.
    for name in names:
        if name in attrs and attrs.get(name) not in (None, ""):
            return attrs.get(name)
    return None


# -----------------------------------------------------------------------------
# Payload Builders and AGOL Deployment Helpers
# -----------------------------------------------------------------------------
def _build_selected_awp_information_package(selected_awp_id: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the same package shape used by the old Information flow, but sourced
    from the currently selected AASHTOWare project in the source-management flow.
    """
    # Preserve the payload field names expected by the existing Information flow.
    return {
        "awp_proj_name": _selected_awp_value(attrs, "ProjectName"),
        "proj_name": _selected_awp_value(attrs, "ProjectName"),
        "phase": _selected_awp_value(attrs, "Phase"),
        "iris": _selected_awp_value(attrs, "IRIS"),
        "stip": _selected_awp_value(attrs, "STIP"),
        "fed_proj_num": _selected_awp_value(attrs, "FederalProjectNumber"),
        "fund_type": _selected_awp_value(attrs, "FundingType"),
        "proj_prac": _selected_awp_value(attrs, "ProjectPractice"),
        "anticipated_start": _selected_awp_value(attrs, "AnticipatedStart"),
        "anticipated_end": _selected_awp_value(attrs, "AnticipatedEnd"),
        "award_date": _selected_awp_value(attrs, "AwardDate"),
        "award_fiscal_year": _selected_awp_value(attrs, "AwardFiscalYear"),
        "contractor": _selected_awp_value(attrs, "Contractor", "ContractorName"),
        "awarded_amount": _selected_awp_value(attrs, "AwardedAmount"),
        "current_contract_amount": _selected_awp_value(attrs, "CurrentContractAmount"),
        "amount_paid_to_date": _selected_awp_value(attrs, "AmountPaidToDate"),
        "tenadd": _selected_awp_value(attrs, "TentativeAdvertiseDate"),
        "awp_proj_desc": _selected_awp_value(attrs, "Description"),
        "proj_desc": _selected_awp_value(attrs, "Description"),
        "contact_name": _selected_awp_value(attrs, "ContactName"),
        "contact_email": _selected_awp_value(attrs, "ContactEmail"),
        "contact_phone": _selected_awp_value(attrs, "ContactPhone"),
        "proj_web": _selected_awp_value(attrs, "ProjectWebsite"),
        "awp_contract_id": selected_awp_id,
    }


def _build_selected_awp_project_name_payload(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Build the project-name payload for related AGOL layer updates.

    Args:
        attrs (Dict[str, Any]): Selected AASHTOWare attribute dictionary.

    Returns:
        Dict[str, Any]: Payload containing the AASHTOWare and APEX project name
            fields expected by the existing AGOL payload builder.
    """
    # Only the project name is required to propagate name changes across layers.
    return {
        "awp_proj_name": _selected_awp_value(attrs, "ProjectName"),
        "proj_name": _selected_awp_value(attrs, "ProjectName"),
    }


def _normalize_objectid_updates(payload: Dict[str, Any]) -> None:
    """Normalize object ID casing inside an AGOL update payload.

    Args:
        payload (Dict[str, Any]): Payload dictionary that may contain update records.

    Returns:
        None: The payload is modified in place when object ID keys need casing
            normalization.
    """
    # Ignore non-dictionary payloads to match the defensive existing behavior.
    if not isinstance(payload, dict):
        return
    # AGOL requires the OBJECTID key in a specific casing; rewrite variants in place.
    for rec in payload.get("updates", []) or []:
        attrs = rec.get("attributes", {})
        if "OBJECTID" not in attrs and "objectId" in attrs:
            attrs["OBJECTID"] = attrs.pop("objectId")
        elif "OBJECTID" not in attrs and "objectid" in attrs:
            attrs["OBJECTID"] = attrs.pop("objectid")


def _payload_has_updates(payload: Optional[Dict[str, Any]]) -> bool:
    """Return whether an AGOL payload contains update records.

    Args:
        payload (Optional[Dict[str, Any]]): Payload to inspect.

    Returns:
        bool: True when the payload is a dictionary with at least one update record;
            otherwise, False.
    """
    # Treat any non-empty updates list as deployable work.
    return bool(isinstance(payload, dict) and (payload.get("updates") or []))


def _result_succeeded(result: Any) -> bool:
    """Return whether an AGOL edit response indicates success.

    Args:
        result (Any): AGOL edit response object to inspect.

    Returns:
        bool: True when all returned update result objects report success; otherwise,
            False.
    """
    if not isinstance(result, dict):
        return False

    # A top-level success flag short-circuits the check when present.
    if result.get("success") is True:
        return True

    # Otherwise, walk any updateResults arrays and require all to succeed.
    for key in ("updateResults", "update_results"):
        update_results = result.get(key)
        if isinstance(update_results, list) and update_results:
            return all(bool(r.get("success")) for r in update_results if isinstance(r, dict))

    return False


def deploy_to_agol_source_connection(
    payload: Dict[str, Any],
    footprint_layer: int,
    footprint_payload: Dict[str, Any],
    traffic_impacts_payload: Dict[str, Any],
    locations_layer: int,
    locations_payload: Dict[str, Any],
    *,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None,
) -> Dict[str, Any]:
    """Deploy Source tab update payloads to the configured AGOL layers.

    Args:
        payload (Dict[str, Any]): Project-layer update payload.
        footprint_layer (int): Footprint layer index associated with the active
            project type.
        footprint_payload (Dict[str, Any]): Footprint-layer update payload.
        traffic_impacts_payload (Dict[str, Any]): Traffic impacts update payload.
        locations_layer (int): Locations layer index.
        locations_payload (Dict[str, Any]): Locations update payload.
        progress_placeholder (Optional[st.delta_generator.DeltaGenerator]): Optional
            Streamlit placeholder used to display deployment progress messages.

    Returns:
        Dict[str, Any]: Dictionary containing individual AGOL edit results for each
            attempted layer update.
    """
    # Read AGOL connection settings from session state immediately before deploy.
    base_url = st.session_state.get("apex_url")
    projects_layer_idx = st.session_state.get("projects_layer")
    traffic_impact_url = st.session_state.get("traffic_impact_url")
    traffic_impacts_layer = st.session_state.get("traffic_impacts_layer")

    # Bail out early if the primary projects layer is not configured.
    if base_url is None or projects_layer_idx is None:
        st.error("AGOL Projects layer is not configured.")
        return {"success": False, "message": "Projects layer not configured"}

    def _progress(frac: float, text: str):
        """Update the caller-provided progress placeholder, or draw inline."""
        if progress_placeholder is not None:
            progress_placeholder.progress(frac, text=text)
        else:
            st.progress(frac, text=text)

    # Sequence of deployment steps: each tuple is (message, loader, payload).
    steps = [
        ("Updating APEX project information...", AGOLDataLoader(base_url, projects_layer_idx), payload),
        ("Updating linked footprint records...", AGOLDataLoader(base_url, footprint_layer), footprint_payload),
        ("Updating traffic impacts records...", AGOLDataLoader(traffic_impact_url, traffic_impacts_layer), traffic_impacts_payload),
        ("Updating locations records...", AGOLDataLoader(base_url, locations_layer), locations_payload),
    ]

    # Count only steps that will actually issue updates so the progress bar is accurate.
    total_steps = sum(1 for _, _, p in steps if _payload_has_updates(p))
    if total_steps == 0:
        return {"success": True, "message": "Nothing to update"}

    completed_steps = 0
    for message, loader, step_payload in steps:
        # Skip steps whose payload contains no updates.
        if not _payload_has_updates(step_payload):
            continue

        # This deploy path supports only 'updates'; reject adds/deletes explicitly.
        if step_payload.get("adds"):
            return {"success": False, "message": f"Unexpected adds found for step: {message}"}
        if step_payload.get("deletes"):
            return {"success": False, "message": f"Unexpected deletes found for step: {message}"}

        _normalize_objectid_updates(step_payload)
        _progress(completed_steps / total_steps, text=message)

        try:
            result = loader.update_features(step_payload)
        except Exception as e:
            # Surface transport-level errors to the UI and abort the sequence.
            st.error(f"{message} failed: {e}")
            return {"success": False, "message": str(e)}

        # AGOL may return HTTP 200 with per-record failure; enforce all-success.
        if not _result_succeeded(result):
            st.error(f"{message} failed.")
            return {"success": False, "message": f"Failed during step: {message}", "result": result}

        completed_steps += 1
        _progress(completed_steps / total_steps, text=message)

    return {"success": True}


def _sync_manager_project_header_after_source_update(
    selected_awp_id: Optional[str],
    selected_awp_attrs: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Keep the manager page header in sync after the Source tab updates the project.

    The manager header is built from cached project list/session values. When the
    Source tab changes the AASHTOWare source, Proj_Name can also be updated in
    AGOL. Without refreshing these values, the old project name can remain at the
    top of the app until the app cache is cleared or a different project is loaded.
    """
    selected_awp_attrs = selected_awp_attrs or {}
    # Resolve the best available project name from the incoming AWP attributes.
    updated_project_name = _selected_awp_value(
        selected_awp_attrs,
        "ProjectName",
        "Proj_Name",
        "AWP_Proj_Name",
    )

    # Refresh cached header/name values used elsewhere in the manager UI.
    if updated_project_name:
        st.session_state["apex_proj_name"] = updated_project_name
        st.session_state["apex_awp_name"] = updated_project_name

    # Reflect the new committed contract id (or clear it) into shared state.
    if selected_awp_id:
        st.session_state["apex_awp_id"] = selected_awp_id
        st.session_state["awp_manage_existing_id"] = selected_awp_id
    else:
        st.session_state.pop("apex_awp_id", None)
        st.session_state["awp_manage_existing_id"] = None

    # Patch the in-memory cached project record so the header renders correctly
    # even before the next reload from AGOL.
    project_record = st.session_state.get("project_record")
    if project_record and isinstance(project_record, list):
        try:
            attrs = project_record[0].setdefault("attributes", {})
            if updated_project_name:
                attrs["Proj_Name"] = updated_project_name
                attrs["AWP_Proj_Name"] = updated_project_name
            attrs["AWP_Contract_ID"] = selected_awp_id
        except Exception:
            # Defensive: never let a header refresh break the update flow.
            pass

    # Force manager_app._get_projects_cache() to reload the project list on the
    # next rerun so the title/dropdown use the updated AGOL project name.
    st.session_state.pop("_manager_projects_cache", None)
    st.session_state.pop("_manager_projects_cache_meta", None)


def _reset_source_state_after_update(
    selected_awp_id: Optional[str],
    selected_awp_attrs: Optional[Dict[str, Any]] = None,
):
    """Reset Source tab session state after a successful source update.

    Args:
        selected_awp_id (Optional[str]): Selected AASHTOWare contract identifier.
        selected_awp_attrs (Optional[Dict[str, Any]]): Selected AASHTOWare attribute
            dictionary used to synchronize manager header values.

    Returns:
        None.
    """
    # Return the Source tab to summary mode after the update workflow completes.
    st.session_state[INFO_AWP_TRIGGER_KEY] = False
    st.session_state["info_show_awp_selector"] = False
    st.session_state["awp_manage_show_details"] = False

    # Drop transient mode/selection markers so the next render re-derives them.
    for key in ("details_type", "info_option", "is_awp", "info_awp_active_id", "info_last_awp_loaded", "info_awp_attrs"):
        st.session_state.pop(key, None)

    # Push the newly committed values into the manager header cache.
    _sync_manager_project_header_after_source_update(selected_awp_id, selected_awp_attrs)

    # Clear transient selector state and flag the tab as freshly updated.
    _clear_manage_awp_state()
    st.session_state["source_connection_updated"] = True


# -----------------------------------------------------------------------------
# Streamlit Action Handlers
# -----------------------------------------------------------------------------
def _on_change_aashtoware_connection():
    """Prepare the Source tab to change the current AASHTOWare connection.

    Returns:
        None: Session state flags are updated for the existing Streamlit callback
            flow.
    """
    st.session_state[INFO_AWP_TRIGGER_KEY] = True
    # Refresh the committed project record before changing selector state.
    project = _get_project_record() or {}
    _sync_manage_existing_id_from_project(project)
    _clear_manage_awp_state()
    _show_awp_selector()
    _seed_awp_manage_default_from_project(project)


def _on_connect_to_aashtoware_project():
    """Prepare the Source tab to connect the project to an AASHTOWare record.

    Returns:
        None: Session state flags are updated for the existing Streamlit callback
            flow.
    """
    st.session_state[INFO_AWP_TRIGGER_KEY] = True
    # Load the project so we can preseed the selector with its committed id.
    project = _get_project_record() or {}
    _sync_manage_existing_id_from_project(project)
    _clear_manage_awp_state()
    _show_awp_selector()
    _seed_awp_manage_default_from_project(project)


def _on_cancel_aashtoware_connection():
    """
    Reset the source tab back to its original state and keep the Source tab selected.
    This should mirror the other source actions by restoring the committed project state.
    """
    project = _get_project_record() or {}
    committed_awp_id = project.get("AWP_Contract_ID") or None

    # Realign the manage selector with the committed record before resetting.
    _sync_manage_existing_id_from_project(project)
    _reset_source_state_after_update(committed_awp_id)

    st.session_state["awp_selection_made"] = False

    # Restore the display mode based on whether the record has a connection.
    if committed_awp_id:
        st.session_state["details_type"] = "AASHTOWare Database"
        st.session_state["info_option"] = "AASHTOWare Database"
        st.session_state["is_awp"] = True
    else:
        st.session_state["details_type"] = "User Input"
        st.session_state["info_option"] = "User Input"
        st.session_state["is_awp"] = False

    st.session_state["source_connection_updated"] = True



def _on_update_aashtoware_connection():
    """Update the active project with the selected AASHTOWare connection.

    Returns:
        None: Success and error messages are rendered directly through Streamlit, and
            related session state values are updated by the existing workflow.
    """
    project = _get_project_record() or {}
    selected_awp_id = _get_selected_manage_awp_id()
    selected_awp_attrs = _get_selected_manage_awp_attrs()

    # Stop before building payloads if the selector has not produced an ID.
    if not selected_awp_id:
        st.error("No AASHTOWare project is selected.")
        return

    # Attributes are required to populate the Information payload correctly.
    if not selected_awp_attrs:
        st.error("Unable to read the selected AASHTOWare project details.")
        return

    # Build the same information package shape used by the existing update flow.
    package = _build_selected_awp_information_package(selected_awp_id, selected_awp_attrs)
    project_name_package = _build_selected_awp_project_name_payload(selected_awp_attrs)

    # Prefer the active object ID from session state, then fall back to AGOL attributes.
    if "apex_object_id" in st.session_state:
        package["objectid"] = st.session_state.apex_object_id
    else:
        objectid = (
            project.get("OBJECTID")
            or project.get("objectid")
            or project.get("objectId")
        )
        if objectid is not None:
            package["objectid"] = objectid

    # Convert the Information package into an AGOL applyEdits payload.
    payload = manage_information_payload(package, "updates")

    proj_type = st.session_state.get("apex_proj_type")
    # Route footprint updates to the layer that matches the active project type.
    if proj_type == "Site":
        footprint_layer = st.session_state["sites_layer"]
    elif proj_type == "Route":
        footprint_layer = st.session_state["routes_layer"]
    elif proj_type == "Boundary":
        footprint_layer = st.session_state["boundaries_layer"]
    else:
        st.error("Unable to determine the footprint layer for the active project type.")
        return

    # Build the propagation payloads that push the project name onto related layers.
    footprint_payload = manage_project_name_update(
        st.session_state["apex_url"],
        layer=footprint_layer,
        id_field="parentglobalid",
        guid=st.session_state["apex_guid"],
        package_out=project_name_package,
        edit_type="updates",
    )

    traffic_impact_payload = manage_project_name_update(
        st.session_state["traffic_impact_url"],
        layer=st.session_state["traffic_impacts_layer"],
        id_field="APEX_GUID",
        guid=st.session_state["apex_guid"],
        package_out=project_name_package,
        edit_type="updates",
    )

    locations_layer = st.session_state["locations_layer"]
    locations_payload = manage_project_name_update(
        st.session_state["apex_url"],
        layer=locations_layer,
        id_field="parentglobalid",
        guid=st.session_state["apex_guid"],
        package_out=project_name_package,
        edit_type="updates",
    )

    # Retrieve the progress placeholder rendered by the UI below the buttons.
    progress_ph = st.session_state.get("source_progress_placeholder")

    # Deploy all built payloads to AGOL in a single coordinated sequence.
    result = deploy_to_agol_source_connection(
        payload,
        footprint_layer,
        footprint_payload,
        traffic_impact_payload,
        locations_layer,
        locations_payload,
        progress_placeholder=progress_ph,
    )

    # Clear the progress bar after completion (success or failure).
    try:
        if progress_ph is not None:
            progress_ph.empty()
    except Exception:
        pass

    # Only reset the Source tab state on a fully successful deployment.
    if isinstance(result, dict) and result.get("success") is True:
       _reset_source_state_after_update(selected_awp_id, selected_awp_attrs)


def _on_remove_aashtoware_connection():
    """
    Remove the AASHTOWare linkage from the AGOL project record.
    """
    project = _get_project_record() or {}
    base_url = st.session_state.get("apex_url")
    projects_layer_idx = st.session_state.get("projects_layer")

    # Resolve the OBJECTID for the record from session state or the record itself.
    objectid = (
        st.session_state.get("apex_object_id")
        or project.get("OBJECTID")
        or project.get("objectid")
        or project.get("objectId")
    )

    # Guard against missing AGOL configuration before attempting the write.
    if not base_url or projects_layer_idx is None:
        st.error("Unable to remove connection: missing AGOL base_url or projects_layer index.")
        return

    if not objectid:
        st.error("Unable to remove connection: missing project OBJECTID.")
        return

    # Null out all AASHTOWare-linked attributes on the project record.
    payload = {
        "updates": [
            {
                "attributes": {
                    "OBJECTID": objectid,
                    "AWP_Contract_ID": None,
                    "AWP_Proj_Name": None,
                    "AWP_Proj_Desc": None,
                }
            }
        ]
    }

    try:
        loader = AGOLDataLoader(base_url, projects_layer_idx)
        result = loader.update_features(payload)
    except Exception as e:
        st.error(f"Remove connection failed: {e}")
        return

    # Some AGOL responses succeed at the HTTP layer but report success=False.
    if isinstance(result, dict) and result.get("success") is False:
        st.error("Remove connection failed (AGOL returned success=False).")
        st.session_state["info_remove_connection_result"] = result
        return

    # Clear every session key that referenced the previously-linked AWP project.
    for k in (
        INFO_AWP_TRIGGER_KEY,
        "info_show_awp_selector",
        "details_type",
        "info_option",
        "is_awp",
        "apex_awp_id",
        "awp_id",
        "awp_guid",
        "aashto_id",
        "info_awp_active_id",
        "info_last_awp_loaded",
        "info_awp_attrs",
        "awp_proj_name",
        "awp_proj_desc",
        "awp_manage_show_details",
        "awp_manage_mode",
        "awp_manage_existing_id",
    ):
        if k in st.session_state:
            del st.session_state[k]

    _clear_manage_awp_state()


# =============================================================================
# Streamlit Page Rendering
# =============================================================================
def manage_source():
    """Render and manage the Source tab for the active Streamlit project.

    This function preserves the existing Source tab workflow, including source
    summary rendering, AASHTOWare connection controls, read-only widget handling,
    and update/remove action routing.

    Returns:
        None: Streamlit components are rendered directly to the page.
    """
    # NOTE: The setdefault calls here reflect existing behavior for first-load
    # defaults and are left unchanged. They only create keys if they do not
    # already exist; they never overwrite user- or workflow-provided values.
    st.session_state.setdefault(INFO_AWP_TRIGGER_KEY, False)
    st.session_state.setdefault("awp_manage_show_details", False)

    # ---------------------------------------------------------------------
    # Header
    # ---------------------------------------------------------------------
    st.markdown("##### MANAGE PROJECT DATA SOURCE")
    st.caption(
        "Connect, update, change, or remove the project’s data source for the APEX. "
        "Changing the connection will refresh and synchronize project information from AASHTOWare, while removing the connection "
        "will convert the project into a user-managed (manual input) project."
    )
    st.write("")

    # ---------------------------------------------------------------------
    # Load active project; short-circuit render when nothing is loaded.
    # ---------------------------------------------------------------------
    project = _get_project_record()
    if not project:
        st.warning("No project loaded.")
        return

    # Realign the manage-selector marker with the committed AGOL record.
    _sync_manage_existing_id_from_project(project)

    # Resolve current source mode and the staged AWP id used by the summary widgets.
    is_awp = _resolve_is_awp(project)
    staged_awp_id = _current_awp_contract_id(project) or ""

    # =========================================================================
    # PROJECT DATA SOURCE SECTION
    # =========================================================================
    st.markdown("###### PROJECT DATA SOURCE")
    with st.container(border=True):
        # ---------------------------------------------------------------------
        # Selector mode: the user has pressed CONNECT / CHANGE
        # ---------------------------------------------------------------------
        if st.session_state.get("info_show_awp_selector", False):
            st.markdown("###### SELECT AASHTOWARE PROJECT", unsafe_allow_html=True)

            # Re-sync the committed marker in case the record changed since load.
            _sync_manage_existing_id_from_project(project)

            # Seed the selector default only if the manage keys are empty.
            if not any(st.session_state.get(k) for k in ("awp_manage_id", "awp_manage_guid")):
                _seed_awp_manage_default_from_project(project)

            # Display AASHTOWARE Project List
            manage_aashtoware_connection()

            # Always show the selector; only show the action button after a fresh selection is made
            selected_awp_id = _get_selected_manage_awp_id()
            selected_awp_attrs = _get_selected_manage_awp_attrs()

            # Render action buttons only when a full selection is available.
            if selected_awp_id and selected_awp_attrs:
                # Distinguish RE-CONNECT vs. UPDATE by comparing to committed id.
                current_awp_id = str(project.get("AWP_Contract_ID") or "")
                button_label = (
                    "RE-CONNECT AASHTOWARE PROJECT"
                    if current_awp_id and selected_awp_id == current_awp_id
                    else "UPDATE AASHTOWARE CONNECTION"
                )

                # Progress placeholder consumed by _on_update_aashtoware_connection().
                st.session_state["source_progress_placeholder"] = st.empty()

                reconnect_update_container = st.container()
                with reconnect_update_container:
                        # Pre-selection state: only CANCEL is available.
                        if st.session_state.get('awp_selection_made') == False:
                            st.button(
                                "CANCEL",
                                use_container_width=True,
                                type="primary",
                                on_click=_on_cancel_aashtoware_connection,
                            )

                        # Post-selection state: show UPDATE/RE-CONNECT and CANCEL.
                        elif st.session_state.get('awp_selection_made') == True:
                            btn1, btn2 = st.columns(2)

                            with btn1:
                                st.button(
                                    button_label,
                                    use_container_width=True,
                                    type="primary",
                                    on_click=_on_update_aashtoware_connection,
                                )

                            with btn2:
                                st.button(
                                    "CANCEL",
                                    use_container_width=True,
                                    type="primary",
                                    on_click=_on_cancel_aashtoware_connection,
                                )
            # Selector mode owns the tab; skip the summary/action rendering below.
            return


        # ---------------------------------------------------------------------
        # Summary mode: show the current data source read-only
        # ---------------------------------------------------------------------
        if is_awp:
            # AWP-connected: display Source / Contract ID / Last Updated + AWP name.
            c1, c2, c3 = st.columns(3)
            with c1:
                ro_widget("info_source", "Source", "AASHTOWare")
            with c2:
                ro_widget("id_source", "Contract ID", staged_awp_id)
            with c3:
                ro_widget("info_last_updated", "Last Updated", fmt_agol_date(project.get("EditDate")))
            ro_widget("info_awp_proj_name", "AASHTOWare Project Name", project.get("AWP_Proj_Name"))
            st.write("")
        else:
            # User Input: only Source and Last Updated are shown.
            c1, c2 = st.columns(2)
            with c1:
                ro_widget("info_source", "Source", "User Input")
            with c2:
                ro_widget("info_last_updated", "Last Updated", fmt_agol_date(project.get("EditDate")))
            st.write("")

        # ---------------------------------------------------------------------
        # Action buttons
        # ---------------------------------------------------------------------
        if is_awp:
            # Two possible workflows: change the connection, or remove it.
            actions_container = st.empty()
            with actions_container.container():
                col_src1, col_src2 = st.columns(2, gap="small")
                with col_src1:
                    change_connection = st.button(
                        "RE-CONNECT/CHANGE CONNECTION",
                        use_container_width=True,
                        type="primary",
                        on_click=_on_change_aashtoware_connection,
                    )

                    # Force a rerun so the selector renders immediately.
                    if change_connection:
                        st.rerun()

                with col_src2:
                    remove = st.button(
                        "REMOVE CONNECTION",
                        use_container_width=True,
                        type="primary"
                    )

                    # First click surfaces a two-step confirm/cancel row.
                    if remove:
                        actions_container.empty()
                        with actions_container.container():
                            btn3, btn4 = st.columns(2)

                            with btn3:
                                confirm_remove = st.button(
                                "CONFIRM REMOVE CONNECTION?",
                                key="confirm_remove_btn",
                                use_container_width=True,
                                type="primary",
                                on_click=_on_remove_aashtoware_connection
                            )

                                if confirm_remove:
                                    st.rerun()

                            with btn4:
                                st.button(
                                "CANCEL",
                                use_container_width=True,
                                type="primary",
                                on_click=_on_cancel_aashtoware_connection,
                            )

        else:
            # No connection yet: single CTA to begin the connect flow.
            st.button(
                "CONNECT TO AASHTOWARE PROJECT",
                use_container_width=True,
                type="primary",
                on_click=_on_connect_to_aashtoware_project,
            )