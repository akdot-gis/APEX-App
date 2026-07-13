"""
information.py
==============

Streamlit tab module for managing the PROJECT INFORMATION step of the APEX
Project Manager application.

This module renders and drives the "Manage Project Information" experience for
an active APEX project. It supports two data source modes:

    1. AASHTOWare Database (AWP): fields are populated from the AASHTOWare
       connection table and are rendered as read-only widgets.
    2. User Input: fields are seeded from the AGOL project record and are
       fully editable by the user.

Key responsibilities:
    * Resolve the current data source mode and display the corresponding
      Project Data Source summary when that section is enabled.
    * Render an inline AASHTOWare project selector for the CONNECT / CHANGE /
      RECONNECT flows and switch the form to AWP mode when a selection is
      made.
    * Build applyEdits payloads for the Project Information, Footprint,
      Traffic Impacts, and Locations layers, and deploy them to AGOL with
      an in-place Streamlit progress indicator.
    * Coordinate a "flagged AWP update" side effect against the traffic form
      layer when requested.

Notes:
    * This file participates in a modular Streamlit app. It reads many keys
      from ``st.session_state`` but does not centralize those reads at module
      scope because the values are written and updated across Streamlit reruns.
    * Session state reads occur inside callback functions and inside
      :func:`manage_information` where they must remain because they respond
      to runtime state changes across Streamlit reruns.
    * All UI strings, widget keys, session_state key names, function names,
      variable names, and existing execution behavior are preserved.
"""

# =============================================================================
# Imports
# =============================================================================

# Standard library
import json
from typing import Any, Dict, Optional, Union

# Third-party
import streamlit as st

# Local application: AGOL access and payload builders
from agol.agol_payloads import (
    manage_information_payload,
    manage_project_name_update,
)
from agol.agol_util import (
    AGOLDataLoader,
    select_record,
)

# Local application: input formatting and widget key helpers
from util.input_util import (
    fmt_agol_date,
    fmt_currency,
    fmt_date,
    fmt_date_or_none,
    fmt_int,
    fmt_int_or_none,
    fmt_string,
    widget_key,
)

# Local application: read-only widget helper
from util.read_only_util import ro_widget

# Local application: Streamlit/AASHTOWare selector helpers
from util.streamlit_util import session_selectbox


# =============================================================================
# Constants / Configuration
# =============================================================================

# Session-state flag keys used to coordinate the AWP connect/change flow with
# the button row at the bottom of the page. These names are consumed elsewhere
# in the app and must not change.
#
# _awp_value should ONLY use AASHTOWare-loaded session values when the user
# explicitly triggered an AWP load via CONNECT/CHANGE. Otherwise, it should
# display values from the AGOL project record.
INFO_AWP_TRIGGER_KEY = "info_awp_trigger_active"
INFO_PENDING_SOURCE_ACTION_KEY = "info_pending_source_action"


# Fallback mapping used when an external mapping is not provided in session_state.
# Keys are the UI/state keys used throughout this file; values are the
# session_state keys populated by _apply_awp_attrs_to_state().
AWP_FIELDS_FALLBACK = {
    # Project name/description
    "awp_proj_name": "awp_proj_name",
    "proj_name": "awp_proj_name",
    "awp_proj_desc": "awp_proj_desc",
    "proj_desc": "awp_proj_desc",

    # Phase & IDs
    "phase": "awp_phase",
    "iris": "awp_iris",
    "stip": "awp_stip",
    "fed_proj_num": "awp_fed_proj_num",

    # Funding & practice
    "fund_type": "awp_fund_type",
    "proj_prac": "awp_proj_prac",

    # Dates
    "anticipated_start": "awp_anticipated_start",
    "anticipated_end": "awp_anticipated_end",
    "award_date": "awp_award_date",
    "award_fiscal_year": "awp_award_fiscal_year",
    "tenadd": "awp_tenadd",

    # Award information
    "awarded_amount": "awp_awarded_amount",
    "current_contract_amount": "awp_current_contract_amount",
    "amount_paid_to_date": "awp_amount_paid_to_date",

    # Contractor
    "contractor": "awp_contractor",

    # Contact
    "contact_name": "awp_contact_name",
    "contact_email": "awp_contact_email",
    "contact_phone": "awp_contact_phone",

    # Web link
    "proj_web": "awp_proj_web",

    # Preconstruction
    "awp_preconstruction": "preconstruction",
}


# =============================================================================
# Session State Access Notes
# =============================================================================

# NOTE: This file intentionally does NOT centralize st.session_state reads into
# a single module-level block. All session_state access happens inside callback
# functions and inside manage_information() because those values are written and
# updated across Streamlit reruns through callbacks, widget events, and AWP loads.
# Reading them once at module import time would freeze stale values and break the
# connect/change/update flows. Comments are placed at in-function read sites
# where the timing is meaningful.


# =============================================================================
# Helper Functions
# =============================================================================

# -----------------------------------------------------------------------------
# Active Project Record Helpers
# -----------------------------------------------------------------------------

def _get_project_record():
    """Return the active APEX project's attribute dictionary from AGOL.

    Reads the currently active APEX GUID and the AGOL projects layer
    configuration from ``st.session_state`` and issues a single-record
    ``select_record`` query against the projects layer.

    Returns:
        dict | None: The ``attributes`` dictionary of the matching project
            record, or ``None`` if the required session_state keys are missing
            or the record cannot be found.
    """
    # Pull the identifiers needed to locate the project record. These are read
    # here because they are populated by earlier workflow steps and can change
    # between reruns.
    apex_guid = st.session_state.get("apex_guid")
    url = st.session_state.get("apex_url")
    layer = st.session_state.get("projects_layer")

    # Bail out early if any required identifier is missing.
    if not (apex_guid and url and layer is not None):
        return None

    # Query AGOL for the single project record by globalid.
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
# AWP Value Resolution Helpers
# -----------------------------------------------------------------------------

def _awp_value(state_key: str, project: dict, project_field: str):
    """Resolve a displayed value for AWP-backed read-only widgets.

    Rules:
        * If the user is actively connecting/changing AASHTOWare, read values
          from the AWP-loaded session_state keys.
        * Otherwise, on normal page load or after UPDATE, display values from
          the current AGOL project record.

    This prevents blank/None displays when the page is loaded without an active
    AWP selection in session_state.

    Args:
        state_key (str): UI/state key for the field being resolved.
        project (dict): AGOL project attribute dictionary.
        project_field (str): AGOL attribute name to fall back to when AWP values
            are not currently active.

    Returns:
        Any: The resolved display value, or ``None`` if no value is available.
    """
    # Normalize a missing project dict so subsequent .get() calls are safe.
    project = project or {}

    # Only use AWP session values when the user explicitly triggered the AWP flow.
    if not st.session_state.get(INFO_AWP_TRIGGER_KEY, False):
        return project.get(project_field)

    # If an external mapping is provided elsewhere in the app, honor it.
    awp_fields = st.session_state.get("awp_fields")
    if isinstance(awp_fields, dict):
        mapped_key = awp_fields.get(state_key, state_key)
    else:
        mapped_key = AWP_FIELDS_FALLBACK.get(state_key, state_key)

    # Use the AWP-loaded value; if it does not exist, fall back to the project record.
    return st.session_state.get(mapped_key, project.get(project_field))


def _resolve_is_awp(project_attrs: dict) -> bool:
    """Resolve whether the active form should render in AASHTOWare mode.

    This matches the details_form logic by preferring the active source
    selection from session state and otherwise falling back to the presence of
    ``AWP_Contract_ID`` on the AGOL project record.

    Args:
        project_attrs (dict): Active AGOL project attributes.

    Returns:
        bool: True when the form should render in AASHTOWare Database mode;
            otherwise, False.
    """
    # Prefer an explicit selection made by the user in this session; the two
    # keys mirror how details_form.py tracks the current source choice.
    details_type = st.session_state.get("details_type") or st.session_state.get("info_option")

    if details_type in ("AASHTOWare Database", "User Input"):
        return details_type == "AASHTOWare Database"

    # Fallback: infer AWP mode from the AGOL record itself.
    return bool(project_attrs.get("AWP_Contract_ID"))



# -----------------------------------------------------------------------------
# Default Seeding Helpers
# -----------------------------------------------------------------------------

def _coerce_to_option(value, options):
    """Return an option entry that equals ``value`` by direct or string match.

    Streamlit selectboxes require the current value to be present in the
    ``options`` list. This helper attempts a direct match first, then falls
    back to a string comparison so numeric/string mismatches between the AGOL
    attribute type and the option list type do not cause the widget to lose
    its default.

    Args:
        value: Candidate value to align with an entry in ``options``.
        options: Iterable of allowed selectbox option entries.

    Returns:
        Any: A matching entry from ``options`` when found, otherwise the
            original ``value`` unchanged.
    """
    if value is None or not options:
        return value

    if value in options:
        return value

    # Fall back to string comparison so int/float/str variants still match.
    str_val = str(value)
    for opt in options:
        if str(opt) == str_val:
            return opt

    return value


def _seed_default(key: str, project: dict, project_field: str, fmt=None):
    """Seed a session_state value from the project record when it is blank.

    Args:
        key (str): Session state key to seed.
        project (dict): Active AGOL project attributes.
        project_field (str): AGOL project attribute to read.
        fmt (Callable | None): Optional formatter to apply before storing.

    Returns:
        None: Session state is updated only when the target key is missing or blank.
    """
    # Only seed the value when nothing has been set yet; never overwrite an
    # existing user-entered value.
    if key not in st.session_state or st.session_state.get(key) in (None, ""):
        raw = project.get(project_field)
        st.session_state[key] = fmt(raw) if fmt else raw


def _seed_select_default(key: str, project: dict, project_field: str, options_key: str):
    """Seed a selectbox backing value from the project record.

    The project attribute value is coerced to an entry found in the selectbox
    options when possible.

    Args:
        key (str): Session state key backing the selectbox.
        project (dict): Active AGOL project attributes.
        project_field (str): AGOL project attribute to read.
        options_key (str): Session state key containing selectbox options.

    Returns:
        None: Session state is updated only when the target key is missing or blank.
    """
    # Only seed the selection when it is not already present; this ensures
    # returning to the page does not clobber a user's active choice.
    if key not in st.session_state or st.session_state.get(key) in (None, ""):
        options = st.session_state.get(options_key, [])
        raw = project.get(project_field)
        st.session_state[key] = _coerce_to_option(raw, options)


# -----------------------------------------------------------------------------
# Payload Builders
# -----------------------------------------------------------------------------

def _build_information_package(is_awp) -> dict:
    """Build the Project Information update package from current form values.

    Args:
        is_awp: Boolean-like value indicating whether the current form is in
            AASHTOWare-backed mode.

    Returns:
        dict: Package of current Project Information values used by
            ``manage_information_payload``. In AWP mode, both AWP and public
            keys are included. In User Input mode, AWP-only keys are omitted.
    """
    # AWP mode: emit both AWP-mirrored keys and public keys so the payload builder
    # can populate both sides of the record correctly.
    if is_awp == True:
        return {
            # 1. Project Name
            "construction_year": st.session_state.get("construction_year"),
        }

    # User Input mode: omit AWP-only keys so the update does not stomp on
    # AASHTOWare-managed fields.
    elif is_awp == False:
        return {
            # 1. Project Name
            "proj_name": st.session_state.get("proj_name"),

            # 2. Construction Year, Phase, & IDs
            "construction_year": st.session_state.get("construction_year"),
            "phase": st.session_state.get("phase"),
            "iris": st.session_state.get("iris"),
            "stip": st.session_state.get("stip"),
            "fed_proj_num": st.session_state.get("fed_proj_num"),

            # 3. Funding Type & Practice
            "fund_type": st.session_state.get("fund_type"),
            "proj_prac": st.session_state.get("proj_prac"),

            # 4. Start & End Date
            "anticipated_start": st.session_state.get("anticipated_start"),
            "anticipated_end": st.session_state.get("anticipated_end"),

            # 5. Award Information
            "award_date": st.session_state.get("award_date"),
            "award_fiscal_year": st.session_state.get("award_fiscal_year"),
            "contractor": st.session_state.get("contractor"),
            "awarded_amount": st.session_state.get("awarded_amount"),
            "current_contract_amount": st.session_state.get("current_contract_amount"),
            "amount_paid_to_date": st.session_state.get("amount_paid_to_date"),
            "tenadd": st.session_state.get("tenadd"),

            # 6. Description
            "proj_desc": st.session_state.get("proj_desc"),

            # 7. Contact
            "contact_name": st.session_state.get("contact_name"),
            "contact_email": st.session_state.get("contact_email"),
            "contact_phone": st.session_state.get("contact_phone"),

            # 8. Web Link
            "proj_web": st.session_state.get("proj_web"),
        }


def _build_project_name_payload():
    """Build a project-name package for related layer updates.

    Returns:
        dict: Package containing the AASHTOWare and public project name fields
            used to update footprint, traffic impacts, and locations records.
    """
    # Only the project name fields are needed to propagate name changes to the
    # footprint, traffic impacts, and locations layers.
    return {
        # 1. Project Name
        "awp_proj_name": st.session_state.get("awp_proj_name"),
        "proj_name": st.session_state.get("proj_name"),
    }


def _reset_information_form_state_after_update():
    """Reset Information-tab state after a successful update.

    NOTE:
        Do not call ``st.rerun()`` here because callbacks already trigger rerun.

    Returns:
        None: Session state is cleared or updated in place so the next script run
            behaves like a first load.
    """
    # Return to normal project-record display mode.
    st.session_state[INFO_AWP_TRIGGER_KEY] = False
    st.session_state[INFO_PENDING_SOURCE_ACTION_KEY] = None

    # Hide the AWP selector.
    st.session_state["info_show_awp_selector"] = False

    # Remove mode overrides so _resolve_is_awp() falls back to AGOL record logic.
    for k in ("details_type", "info_option", "is_awp"):
        if k in st.session_state:
            del st.session_state[k]

    # Clear widget-backed field values so defaults will be re-seeded from the
    # AGOL record.
    field_keys = [
        "proj_name",
        "proj_desc",
        "construction_year",
        "phase",
        "iris",
        "stip",
        "fed_proj_num",
        "fund_type",
        "proj_prac",
        "anticipated_start",
        "anticipated_end",
        "award_date",
        "award_fiscal_year",
        "contractor",
        "awarded_amount",
        "current_contract_amount",
        "amount_paid_to_date",
        "tenadd",
        "contact_name",
        "contact_email",
        "contact_phone",
        "proj_web",
    ]

    for k in field_keys:
        if k in st.session_state:
            del st.session_state[k]

    # Also clear display-only source markers so they are re-derived on next render.
    for k in ("id_source", "info_last_updated", "info_source"):
        if k in st.session_state:
            del st.session_state[k]

    # Bump version so widget keys regenerate and force a clean rebuild.
    st.session_state["form_version"] = int(st.session_state.get("form_version", 0)) + 1


def _on_update_information(is_awp):
    """Handle the UPDATE INFORMATION button action.

    Builds the required project and related-layer update payloads, deploys them
    to AGOL, updates the Streamlit progress placeholder during deployment, and
    resets the tab state when deployment succeeds.

    Args:
        is_awp: Boolean-like value indicating whether the current form is in
            AASHTOWare-backed mode.

    Returns:
        None: Results are handled through Streamlit UI state and messages.
    """
    # Snapshot the pending source action and current field values.
    pending_source_action = st.session_state.get(INFO_PENDING_SOURCE_ACTION_KEY)
    package = _build_information_package(is_awp)
    project_name_package = _build_project_name_payload()

    # Include OBJECTID for updates when available.
    if "apex_object_id" in st.session_state:
        package["objectid"] = st.session_state.apex_object_id

    # Build the AGOL applyEdits payload for updates.
    payload = manage_information_payload(package, "updates")

    # If the user chose to remove the AASHTOWare connection, null out the
    # AWP-specific attributes on the project record itself.
    if pending_source_action == "remove_connection":
        updates = payload.get("updates") or []
        if updates:
            attrs = updates[0].setdefault("attributes", {})
            attrs["AWP_Contract_ID"] = None
            attrs["AWP_Proj_Name"] = None
            attrs["AWP_Proj_Desc"] = None

    # Build Footprint Payload.
    # Resolve the correct footprint layer index based on the project's geometry type.
    proj_type = st.session_state["apex_proj_type"]

    if proj_type == "Site":
        footprint_layer = st.session_state["sites_layer"]
    elif proj_type == "Route":
        footprint_layer = st.session_state["routes_layer"]
    elif proj_type == "Boundary":
        footprint_layer = st.session_state["boundaries_layer"]

    footprint_payload = manage_project_name_update(
        st.session_state["apex_url"],
        layer=footprint_layer,
        id_field="parentglobalid",
        guid=st.session_state["apex_guid"],
        package_out=project_name_package,
        edit_type="updates",
    )

    # Build Traffic Impacts Payload.
    traffic_impact_payload = manage_project_name_update(
        st.session_state["traffic_impact_url"],
        layer=st.session_state["traffic_impacts_layer"],
        id_field="APEX_GUID",
        guid=st.session_state["apex_guid"],
        package_out=project_name_package,
        edit_type="updates",
    )

    # Build Locations Payload.
    locations_layer = st.session_state["locations_layer"]
    locations_payload = manage_project_name_update(
        st.session_state["apex_url"],
        layer=locations_layer,
        id_field="parentglobalid",
        guid=st.session_state["apex_guid"],
        package_out=project_name_package,
        edit_type="updates",
    )

    # Progress placeholder is stored by the UI section under the buttons.
    progress_ph = st.session_state.get("info_progress_placeholder")

    # Deploy to AGOL with in-place progress updates.
    result = deploy_to_agol_information(
        payload,
        footprint_layer,
        footprint_payload,
        traffic_impact_payload,
        locations_layer,
        locations_payload,
        "updates",
        progress_placeholder=progress_ph,
    )

    # Clear the progress bar after completion, success or failure.
    try:
        if progress_ph is not None:
            progress_ph.empty()
    except Exception:
        pass

    # Only reset the tab state on a fully successful deployment.
    if isinstance(result, dict) and result.get("success") is True:
        _reset_information_form_state_after_update()


# -----------------------------------------------------------------------------
# AGOL Deployment
# -----------------------------------------------------------------------------

def deploy_to_agol_information(
    payload: Dict[str, Any],
    footprint_layer: int,
    footprint_payload: Dict[str, Any],
    traffic_impacts_payload: Dict[str, Any],
    locations_layer: int,
    locations_payload: Dict[str, Any],
    edit_type: str,
    *,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None,
) -> Dict[str, Any]:
    """Submit Project Information and related-layer updates to AGOL.

    This deployment function submits applyEdits payloads to AGOL for:
        1. Project Information.
        2. Footprint layer.
        3. Traffic Impacts layer.
        4. Locations layer.
        5. Flagged AWP update when requested.

    This function supports only ``"updates"`` and normalizes OBJECTID casing as
    needed before update requests are submitted.

    Args:
        payload (Dict[str, Any]): Project Information update payload.
        footprint_layer (int): Footprint layer index for the active project type.
        footprint_payload (Dict[str, Any]): Footprint update payload.
        traffic_impacts_payload (Dict[str, Any]): Traffic Impacts update payload.
        locations_layer (int): Locations layer index.
        locations_payload (Dict[str, Any]): Locations update payload.
        edit_type (str): Edit type. Existing behavior supports only ``"updates"``.
        progress_placeholder (Optional[st.delta_generator.DeltaGenerator]):
            Optional Streamlit placeholder used to show progress in place.

    Returns:
        Dict[str, Any]: Structured success or failure result containing layer-level
            edit responses where available.
    """
    # Read AGOL layer configuration from session_state. Kept in-function so a
    # reconfiguration between reruns is picked up on the next deploy.
    base_url = st.session_state.get("apex_url")
    projects_layer_idx = st.session_state.get("projects_layer")

    traffic_impact_url = st.session_state.get("traffic_impact_url")
    traffic_impacts_layer = st.session_state.get("traffic_impacts_layer")

    # Verify configuration before attempting any writes.
    if base_url is None or projects_layer_idx is None:
        st.error("AGOL Projects layer is not configured.")
        return {"success": False, "message": "Projects layer not configured"}

    if edit_type != "updates":
        return {"success": False, "message": "Only 'updates' are supported"}

    def _progress(frac: float, text: str):
        """Update the caller-provided progress placeholder, or draw inline.

        Args:
            frac (float): Progress fraction to display.
            text (str): Progress text to display.

        Returns:
            None.
        """
        if progress_placeholder is not None:
            progress_placeholder.progress(frac, text=text)
        else:
            st.progress(frac, text=text)

    def _normalize_objectid_updates(p: Dict[str, Any]) -> None:
        """Coerce objectId/objectid variants into AGOL-expected OBJECTID casing.

        Args:
            p (Dict[str, Any]): Payload that may contain update records.

        Returns:
            None: Payload records are modified in place when needed.
        """
        if not isinstance(p, dict):
            return

        for rec in p.get("updates", []) or []:
            attrs = rec.get("attributes", {})
            if "OBJECTID" not in attrs and "objectId" in attrs:
                attrs["OBJECTID"] = attrs.pop("objectId")
            elif "OBJECTID" not in attrs and "objectid" in attrs:
                attrs["OBJECTID"] = attrs.pop("objectid")

    def _reject_non_updates(p: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
        """Return an error dictionary if a payload contains adds or deletes.

        Args:
            p (Dict[str, Any]): Payload to inspect.
            label (str): Label identifying the payload's layer or purpose.

        Returns:
            Optional[Dict[str, Any]]: Error dictionary when adds/deletes are
                present; otherwise, None.
        """
        if p.get("adds"):
            return {"success": False, "message": f"{label} payload contains adds"}

        if p.get("deletes"):
            return {"success": False, "message": f"{label} payload contains deletes"}

        return None

    def _as_bool(value: Any) -> bool:
        """Convert common session_state value types to a boolean.

        Args:
            value (Any): Value that may be bool, numeric, string, None, or another
                object type.

        Returns:
            bool: Lenient boolean interpretation of the provided value.
        """
        if isinstance(value, bool):
            return value

        if value is None:
            return False

        if isinstance(value, (int, float)):
            return value != 0

        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "y", "on"}

        return bool(value)

    def _coerce_objectid(value: Any) -> Any:
        """Normalize an OBJECTID candidate to an integer when possible.

        Args:
            value (Any): OBJECTID candidate value.

        Returns:
            Any: Integer OBJECTID when conversion is possible, None for blank
                values, or the original value when conversion is not applicable.
        """
        if value is None:
            return None

        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return None
            if value.isdigit():
                return int(value)

        return value

    _progress(0.0, "Submitting updates to AGOL…")

    try:
        # ---------------------------------------------------------------------
        # 1. Project Information
        # ---------------------------------------------------------------------
        _progress(0.2, "Updating Project Information…")

        _normalize_objectid_updates(payload)
        project_loader = AGOLDataLoader(base_url, projects_layer_idx)
        project_result = project_loader.update_features(payload)

        # If the primary project update fails, abort the remaining steps.
        if project_result.get("success") is False:
            return {
                "success": False,
                "message": "Project update failed",
                "project": project_result,
                "footprint": None,
                "traffic_impacts": None,
                "locations": None,
                "flagged_awp": None,
            }

        # ---------------------------------------------------------------------
        # 2. Footprint Layer
        # ---------------------------------------------------------------------
        footprint_result = None

        if footprint_payload.get("updates"):
            # Only updates are supported here; reject other edit intents.
            err = _reject_non_updates(footprint_payload, "Footprint")
            if err:
                err["project"] = project_result
                err["footprint"] = None
                err["traffic_impacts"] = None
                err["locations"] = None
                err["flagged_awp"] = None
                return err

            if footprint_layer is None:
                return {
                    "success": False,
                    "message": "Footprint layer index not provided",
                    "project": project_result,
                    "footprint": None,
                    "traffic_impacts": None,
                    "locations": None,
                    "flagged_awp": None,
                }

            _progress(0.6, "Updating Footprint Layer…")

            _normalize_objectid_updates(footprint_payload)
            footprint_loader = AGOLDataLoader(base_url, footprint_layer)
            footprint_result = footprint_loader.update_features(footprint_payload)

            # Abort on footprint failure and surface the partial results.
            if footprint_result.get("success") is False:
                return {
                    "success": False,
                    "message": "Footprint update failed",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": None,
                    "locations": None,
                    "flagged_awp": None,
                }

        # ---------------------------------------------------------------------
        # 3. Traffic Impacts Layer
        # ---------------------------------------------------------------------
        traffic_impacts_result = None

        if traffic_impacts_payload.get("updates"):
            err = _reject_non_updates(traffic_impacts_payload, "Traffic Impacts")
            if err:
                err["project"] = project_result
                err["footprint"] = footprint_result
                err["traffic_impacts"] = None
                err["locations"] = None
                err["flagged_awp"] = None
                return err

            if not traffic_impact_url or traffic_impacts_layer is None:
                return {
                    "success": False,
                    "message": "Traffic Impacts layer not configured",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": None,
                    "locations": None,
                    "flagged_awp": None,
                }

            _progress(0.85, "Updating Traffic Impacts Layer…")

            _normalize_objectid_updates(traffic_impacts_payload)
            traffic_loader = AGOLDataLoader(
                traffic_impact_url,
                traffic_impacts_layer,
            )
            traffic_impacts_result = traffic_loader.update_features(
                traffic_impacts_payload
            )

            if traffic_impacts_result.get("success") is False:
                return {
                    "success": False,
                    "message": "Traffic impacts update failed",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": traffic_impacts_result,
                    "locations": None,
                    "flagged_awp": None,
                }

        # ---------------------------------------------------------------------
        # 4. Locations Layer
        # ---------------------------------------------------------------------
        locations_result = None

        if locations_payload.get("updates"):
            err = _reject_non_updates(locations_payload, "Locations")
            if err:
                err["project"] = project_result
                err["footprint"] = footprint_result
                err["traffic_impacts"] = traffic_impacts_result
                err["locations"] = None
                err["flagged_awp"] = None
                return err

            if locations_layer is None:
                return {
                    "success": False,
                    "message": "Locations layer not configured",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": traffic_impacts_result,
                    "locations": None,
                    "flagged_awp": None,
                }

            _progress(0.95, "Updating Locations Layer…")

            _normalize_objectid_updates(locations_payload)
            locations_loader = AGOLDataLoader(base_url, locations_layer)
            locations_result = locations_loader.update_features(locations_payload)

            if locations_result.get("success") is False:
                return {
                    "success": False,
                    "message": "Locations update failed",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": traffic_impacts_result,
                    "locations": locations_result,
                    "flagged_awp": None,
                }

        # ---------------------------------------------------------------------
        # 5. Flagged AWP Update
        # ---------------------------------------------------------------------
        flagged_awp_result = None

        # Only run this side effect when the caller has explicitly flagged it.
        flagged_awp_requested = _as_bool(st.session_state.get("flagged_awp_update"))

        if flagged_awp_requested:
            flagged_objectid = _coerce_objectid(st.session_state.get("flagged_objectid"))
            traffic_form_url = st.session_state.get("traffic_form_url")
            traffic_form_layer = st.session_state.get("traffic_form_layer")

            if flagged_objectid is None:
                return {
                    "success": False,
                    "message": "Flagged AWP update requested but flagged_objectid is missing",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": traffic_impacts_result,
                    "locations": locations_result,
                    "flagged_awp": None,
                }

            if not traffic_form_url or traffic_form_layer is None:
                return {
                    "success": False,
                    "message": "Flagged AWP update requested but traffic form layer is not configured",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": traffic_impacts_result,
                    "locations": locations_result,
                    "flagged_awp": None,
                }

            # Lazy import: datetime is only needed for this optional side effect.
            from datetime import datetime, timezone

            # AGOL Date fields usually expect epoch milliseconds.
            flagged_awp_payload = {
                "updates": [
                    {
                        "attributes": {
                            "OBJECTID": flagged_objectid,
                            "AWP_Update_Flag": "No",
                            "AWP_Update_Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                            "AWP_Update_Status": "Complete",
                        }
                    }
                ]
            }

            _progress(0.98, "Updating flagged AWP record…")

            _normalize_objectid_updates(flagged_awp_payload)
            flagged_awp_loader = AGOLDataLoader(
                traffic_form_url,
                traffic_form_layer,
            )
            flagged_awp_result = flagged_awp_loader.update_features(flagged_awp_payload)

            if flagged_awp_result.get("success") is False:
                return {
                    "success": False,
                    "message": "Flagged AWP update failed",
                    "project": project_result,
                    "footprint": footprint_result,
                    "traffic_impacts": traffic_impacts_result,
                    "locations": locations_result,
                    "flagged_awp": flagged_awp_result,
                }

        _progress(1.0, "Done")

        # Aggregated success result across all layers touched.
        return {
            "success": True,
            "project": project_result,
            "footprint": footprint_result,
            "traffic_impacts": traffic_impacts_result,
            "locations": locations_result,
            "flagged_awp": flagged_awp_result,
        }

    except Exception as e:
        # Any unhandled exception is captured as a structured failure result so
        # the UI can render a consistent message and clear its progress bar.
        return {
            "success": False,
            "message": str(e),
            "project": None,
            "footprint": None,
            "traffic_impacts": None,
            "locations": None,
            "flagged_awp": None,
        }


# =============================================================================
# Main Streamlit Entrypoint
# =============================================================================

def manage_information():
    """Render the Manage Project Information tab.

    This is the module's public entry point. It:
        * Loads the active project record from AGOL.
        * Determines whether the form is in AASHTOWare or User Input mode.
        * Seeds User Input defaults from the project record without overwriting
          values the user may have already entered.
        * Renders the Project Information sections as read-only or editable
          widgets depending on the current source mode.
        * Renders the UPDATE INFORMATION button and progress placeholder.

    Returns:
        None: Streamlit components are rendered directly to the page.
    """
    # Default: show AGOL project record values unless AWP connect/change is active.
    # This preserves existing first-load behavior.
    st.session_state.setdefault(INFO_AWP_TRIGGER_KEY, False)

    # -------------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------------
    st.markdown("##### MANAGE PROJECT INFORMATION")
    st.caption(
        "View project information for the active APEX project. "
        "If the project is connected to AASHTOWare, data is read-only and can only be updated by reconnecting to the current project or switching to a different one. "
        "If the data source is changed to a user-input project, all fields become fully editable and can be updated manually."
    )
    st.write("")

    # -------------------------------------------------------------------------
    # Load Active Project
    # -------------------------------------------------------------------------
    project = _get_project_record()

    if not project:
        st.warning("No project loaded.")
        return

    # Match details_form mode and key behavior.
    # NOTE: session_state reads for form_version and mode resolution stay here
    # because they may change during render as a side effect of AWP loading below.
    version = st.session_state.get("form_version", 0)
    is_awp = _resolve_is_awp(project)

    # Always seed Construction Year so session_selectbox can resolve default.
    _seed_select_default(
        "construction_year",
        project,
        "Construction_Year",
        "construction_years",
    )

    # -------------------------------------------------------------------------
    # Seed Defaults for User Input Mode
    # -------------------------------------------------------------------------
    if not is_awp:
        # 1. Project Name
        _seed_default("proj_name", project, "Proj_Name", fmt=fmt_string)

        # 2. Construction Year, Phase, & IDs
        _seed_select_default("construction_year", project, "Construction_Year", "construction_years")
        _seed_select_default("phase", project, "Phase", "phase_list")
        _seed_default("iris", project, "IRIS", fmt=fmt_string)
        _seed_default("stip", project, "STIP", fmt=fmt_string)
        _seed_default("fed_proj_num", project, "Fed_Proj_Num", fmt=fmt_string)

        # 3. Funding Type & Practice
        _seed_select_default("fund_type", project, "Fund_Type", "funding_list")
        _seed_select_default("proj_prac", project, "Proj_Prac", "practice_list")

        # 4. Start & End Date
        _seed_default("anticipated_start", project, "Anticipated_Start", fmt=fmt_date_or_none)
        _seed_default("anticipated_end", project, "Anticipated_End", fmt=fmt_date_or_none)

        # 5. Award Information
        _seed_default("award_date", project, "Award_Date", fmt=fmt_date_or_none)
        _seed_select_default("award_fiscal_year", project, "Award_Fiscal_Year", "years")
        _seed_default("contractor", project, "Contractor", fmt=fmt_string)

        # Numerics are kept raw in state; widgets render them below.
        if "awarded_amount" not in st.session_state or st.session_state.get("awarded_amount") in (None, ""):
            st.session_state["awarded_amount"] = fmt_int_or_none(project.get("Awarded_Amount"))

        if "current_contract_amount" not in st.session_state or st.session_state.get("current_contract_amount") in (None, ""):
            st.session_state["current_contract_amount"] = fmt_int_or_none(project.get("Current_Contract_Amount"))

        if "amount_paid_to_date" not in st.session_state or st.session_state.get("amount_paid_to_date") in (None, ""):
            st.session_state["amount_paid_to_date"] = fmt_int_or_none(project.get("Amount_Paid_to_Date"))

        _seed_default("tenadd", project, "TenAdd", fmt=fmt_date_or_none)

        # 6. Description
        _seed_default("proj_desc", project, "Proj_Desc", fmt=fmt_string)

        # 7. Contact
        _seed_default("contact_name", project, "Contact_Name", fmt=fmt_string)
        _seed_default("contact_email", project, "Contact_Email", fmt=fmt_string)
        _seed_default("contact_phone", project, "Contact_Phone", fmt=fmt_string)

        # 8. Web Link
        _seed_default("proj_web", project, "Proj_Web", fmt=fmt_string)

    # =========================================================================
    # PROJECT DATA SOURCE (commented out for now; restore as needed)
    # =========================================================================
    # st.markdown("###### PROJECT DATA SOURCE")
    # with st.container(border=True):
    #     # If the user pressed CONNECT/CHANGE: hide the summary, show the AWP selector
    #     if st.session_state.get("info_show_awp_selector", False):
    #         st.markdown("###### SELECT AASHTOWARE PROJECT", unsafe_allow_html=True)
    #         # Seed default selection if not present yet (e.g., from project->AWP_Contract_ID)
    #         if not any(st.session_state.get(k) for k in ("awp_id", "awp_guid", "aashto_id")):
    #             _seed_awp_default_from_project(project)
    #
    #         # Render the dropdown (this will also populate awp_* keys on selection)
    #         aashtoware_project()
    #
    #         # After render, if a selection exists, load via CONTRACT_Id and flip form to AWP view
    #         _load_awp_by_contract_id_and_switch()
    #     else:
    #         # AWP-connected display: show Source / Contract ID / Last Updated.
    #         if is_awp:
    #             c1, c2, c3 = st.columns(3)
    #             with c1:
    #                 ro_widget(
    #                     "info_source",
    #                     "Source",
    #                     "AASHTOWare",
    #                 )
    #             with c2:
    #                 ro_widget(
    #                     "id_source",
    #                     "Contract ID",
    #                     _current_awp_contract_id(project),
    #                 )
    #
    #             with c3:
    #                 ro_widget(
    #                     "info_last_updated",
    #                     "Last Updated",
    #                     fmt_agol_date(project.get("EditDate")),
    #                 )
    #
    #             ro_widget("info_awp_proj_name", "AASHTOWare Project Name", project.get("AWP_Proj_Name"))
    #
    #
    #         else:
    #             # User Input display: only Source and Last Updated.
    #             c1, c2 = st.columns(2)
    #             with c1:
    #                 ro_widget(
    #                     "info_source",
    #                     "Source",
    #                     "User Input",
    #                 )
    #             with c2:
    #                 ro_widget(
    #                     "info_last_updated",
    #                     "Last Updated",
    #                     fmt_agol_date(project.get("EditDate")),
    #                 )
    #
    # st.write("")

    # =========================================================================
    # Project Information
    # =========================================================================

    # Re-evaluate AWP mode after potential selection.
    is_awp = _resolve_is_awp(project)

    st.markdown("###### PROJECT INFORMATION")

    with st.container(border=True):
        # ---------------------------------------------------------------------
        # 1. Project Name
        # ---------------------------------------------------------------------
        st.markdown("<h6>1. PROJECT NAME</h6>", unsafe_allow_html=True)

        if is_awp:
            # Read-only display of the AWP and Public project names.
            c1, c2 = st.columns(2)

            with c1:
                ro_widget(
                    "awp_proj_name",
                    "AASHTOWare Project Name",
                    fmt_string(_awp_value("awp_proj_name", project, "AWP_Proj_Name")),
                )

            with c2:
                ro_widget(
                    "proj_name",
                    "Public Project Name",
                    fmt_string(_awp_value("proj_name", project, "Proj_Name")),
                )

        else:
            # User Input: editable Public Project Name.
            st.session_state["proj_name"] = st.text_input(
                "Public Project Name ⮜",
                value=st.session_state.get("proj_name", project.get("Proj_Name", "")),
                key=widget_key("proj_name", version, is_awp),
                help="Provide the project name that will be displayed publicly.",
            )

        st.write("")

        # ---------------------------------------------------------------------
        # 2. Construction Year, Phase, & IDs
        # ---------------------------------------------------------------------
        st.markdown("<h6>2. CONSTRUCTION YEAR, PHASE, & IDS</h6>", unsafe_allow_html=True)

        col1, col2 = st.columns(2)

        # Construction Year.
        with col1:
            if is_awp:
                st.session_state["construction_year"] = session_selectbox(
                    key="construction_year",
                    label="Construction Year",
                    help="The planned construction year for this project.",
                    options=(st.session_state.get("construction_years", [])),
                    is_awp=True,
                )
            else:
                st.session_state["construction_year"] = session_selectbox(
                    key="construction_year",
                    label="Construction Year",
                    help="The planned construction year for this project.",
                    options=(st.session_state.get("construction_years", [])),
                    is_awp=False,
                )

        # Phase.
        with col2:
            if is_awp:
                ro_widget(
                    "phase",
                    "Phase",
                    fmt_string(_awp_value("phase", project, "Phase")),
                )
            else:
                st.session_state["phase"] = session_selectbox(
                    key="phase",
                    label="Phase",
                    help="Indicates the construction phase scheduled for this project in the current year.",
                    options=(st.session_state.get("phase_list", [])),
                    is_awp=False,
                )

        col3, col4, col5 = st.columns(3)

        # IRIS.
        with col3:
            if is_awp:
                ro_widget(
                    "iris",
                    "IRIS",
                    fmt_string(_awp_value("iris", project, "IRIS")),
                )
            else:
                st.session_state["iris"] = st.text_input(
                    label="IRIS",
                    key=widget_key("awp_iris", version, is_awp),
                    value=st.session_state.get("iris", project.get("IRIS", "")),
                )

        # STIP.
        with col4:
            if is_awp:
                ro_widget(
                    "stip",
                    "STIP",
                    fmt_string(_awp_value("stip", project, "STIP")),
                )
            else:
                st.session_state["stip"] = st.text_input(
                    label="STIP",
                    key=widget_key("awp_stip", version, is_awp),
                    value=st.session_state.get("stip", project.get("STIP", "")),
                )

        # Federal Project Number.
        with col5:
            if is_awp:
                ro_widget(
                    "fed_proj_num",
                    "Federal Project Number",
                    fmt_string(_awp_value("fed_proj_num", project, "Fed_Proj_Num")),
                )
            else:
                st.session_state["fed_proj_num"] = st.text_input(
                    label="Federal Project Number",
                    key=widget_key("awp_fed_proj_num", version, is_awp),
                    value=st.session_state.get("fed_proj_num", project.get("Fed_Proj_Num", "")),
                )

        st.write("")
        st.write("")

        # ---------------------------------------------------------------------
        # 3. Funding Type & Practice
        # ---------------------------------------------------------------------
        st.markdown("<h6>3. FUNDING TYPE & PRACTICE</h6>", unsafe_allow_html=True)

        col13, col14 = st.columns(2)

        if is_awp:
            with col13:
                ro_widget(
                    "fund_type",
                    "Funding Type",
                    fmt_string(_awp_value("fund_type", project, "Fund_Type")),
                )

            with col14:
                ro_widget(
                    "proj_prac",
                    "Project Practice",
                    fmt_string(_awp_value("proj_prac", project, "Proj_Prac")),
                )

        else:
            with col13:
                st.session_state["fund_type"] = session_selectbox(
                    key="fund_type",
                    label="Funding Type",
                    help="",
                    options=(st.session_state.get("funding_list", [])),
                    is_awp=False,
                )

            with col14:
                st.session_state["proj_prac"] = session_selectbox(
                    key="proj_prac",
                    label="Project Practice",
                    help="",
                    options=st.session_state.get("practice_list", []),
                    is_awp=False,
                )

        st.write("")
        st.write("")

        # ---------------------------------------------------------------------
        # 4. Start & End Date
        # ---------------------------------------------------------------------
        st.markdown("<h6>4. START & END DATE</h6>", unsafe_allow_html=True)

        col10, col11 = st.columns(2)

        if is_awp:
            with col10:
                ro_widget(
                    "anticipated_start",
                    "Anticipated Start",
                    fmt_date(_awp_value("anticipated_start", project, "Anticipated_Start")),
                )

            with col11:
                ro_widget(
                    "anticipated_end",
                    "Anticipated End",
                    fmt_date(_awp_value("anticipated_end", project, "Anticipated_End")),
                )

        else:
            with col10:
                st.session_state["anticipated_start"] = st.date_input(
                    label="Anticpated Start",
                    format="MM/DD/YYYY",
                    value=st.session_state.get(
                        "anticipated_start",
                        fmt_date_or_none(project.get("Anticipated_Start")),
                    ),
                    key=widget_key("anticipated_start", version, is_awp),
                )

            with col11:
                st.session_state["anticipated_end"] = st.date_input(
                    label="Anticpated End",
                    format="MM/DD/YYYY",
                    value=st.session_state.get(
                        "anticipated_end",
                        fmt_date_or_none(project.get("Anticipated_End")),
                    ),
                    key=widget_key("anticipated_end", version, is_awp),
                )

        st.write("")
        st.write("")

        # ---------------------------------------------------------------------
        # 5. Award Information
        # ---------------------------------------------------------------------
        st.markdown("<h6>5. AWARD INFORMATION</h6>", unsafe_allow_html=True)

        if is_awp:
            # Read-only award fields sourced from AWP where possible.
            col12, col13 = st.columns(2)

            with col12:
                ro_widget(
                    "award_date",
                    "Award Date",
                    fmt_agol_date(_awp_value("award_date", project, "Award_Date")),
                )

            with col13:
                ro_widget(
                    "award_fiscal_year",
                    "Awarded Fiscal Year",
                    fmt_int(_awp_value("award_fiscal_year", project, "Award_Fiscal_Year"), year=True),
                )

            ro_widget(
                "contractor",
                "Awarded Contractor",
                fmt_string(_awp_value("contractor", project, "Contractor")),
            )

            col15, col16, col17 = st.columns(3)

            with col15:
                ro_widget(
                    "awarded_amount",
                    "Awarded Amount",
                    fmt_currency(_awp_value("awarded_amount", project, "Awarded_Amount")),
                )

            with col16:
                ro_widget(
                    "current_contract_amount",
                    "Current Contract Amount",
                    fmt_currency(_awp_value("current_contract_amount", project, "Current_Contract_Amount")),
                )

            with col17:
                ro_widget(
                    "amount_paid_to_date",
                    "Amount Paid to Date",
                    fmt_currency(_awp_value("amount_paid_to_date", project, "Amount_Paid_to_Date")),
                )

            ro_widget(
                "tenadd",
                "Tentative Advertise Date",
                fmt_date(_awp_value("tenadd", project, "TenAdd")),
            )

        else:
            # User Input award fields: fully editable.
            col12, col13 = st.columns(2)

            with col12:
                st.session_state["award_date"] = st.date_input(
                    label="Award Date",
                    format="MM/DD/YYYY",
                    value=st.session_state.get(
                        "award_date",
                        fmt_date_or_none(project.get("Award_Date")),
                    ),
                    key=widget_key("award_date", version, is_awp),
                )

            with col13:
                st.session_state["award_fiscal_year"] = session_selectbox(
                    key="award_fiscal_year",
                    label="Awarded Fiscal Year",
                    options=st.session_state.get("years", []),
                    force_str=is_awp,  # keep original behavior
                    is_awp=False,
                    help="The fiscal year for the award date",
                )

            st.session_state["contractor"] = st.text_input(
                label="Awarded Contractor",
                key=widget_key("contractor", version, is_awp),
                value=st.session_state.get("contractor", project.get("Contractor", "")),
            )

            col15, col16, col17 = st.columns(3)

            with col15:
                # Resolve the initial value from state, then project, then 0.
                _val_awarded = st.session_state.get("awarded_amount")
                if _val_awarded is None:
                    _val_awarded = fmt_int_or_none(project.get("Awarded_Amount"))
                if _val_awarded is None:
                    _val_awarded = 0

                st.session_state["awarded_amount"] = st.number_input(
                    label="Awarded Amount",
                    key=widget_key("awarded_amount", version, is_awp),
                    value=_val_awarded,
                )

            with col16:
                # Resolve the initial value from state, then project, then 0.
                _val_current = st.session_state.get("current_contract_amount")
                if _val_current is None:
                    _val_current = fmt_int_or_none(project.get("Current_Contract_Amount"))
                if _val_current is None:
                    _val_current = 0

                st.session_state["current_contract_amount"] = st.number_input(
                    label="Current Contract Amount",
                    key=widget_key("current_contract_amount", version, is_awp),
                    value=_val_current,
                )

            with col17:
                # Resolve the initial value from state, then project, then 0.
                _val_paid = st.session_state.get("amount_paid_to_date")
                if _val_paid is None:
                    _val_paid = fmt_int_or_none(project.get("Amount_Paid_to_Date"))
                if _val_paid is None:
                    _val_paid = 0

                st.session_state["amount_paid_to_date"] = st.number_input(
                    label="Amount Paid to Date",
                    key=widget_key("amount_paid_to_date", version, is_awp),
                    value=_val_paid,
                )

            st.session_state["tenadd"] = st.date_input(
                label="Tentative Advertise Date",
                format="MM/DD/YYYY",
                value=st.session_state.get(
                    "tenadd",
                    fmt_date_or_none(project.get("TenAdd")),
                ),
                key=widget_key("tenadd", version, is_awp),
            )

        st.write("")
        st.write("")

        # ---------------------------------------------------------------------
        # 6. Description
        # ---------------------------------------------------------------------
        st.markdown("<h6>6. DESCRIPTION</h6>", unsafe_allow_html=True)

        if is_awp:
            # Read-only descriptions: AWP-authored and public-facing.
            ro_widget(
                "awp_proj_desc",
                "AASHTOWare Description",
                fmt_string(_awp_value("awp_proj_desc", project, "AWP_Proj_Desc")),
                textarea=True,
            )

            ro_widget(
                "proj_desc",
                "Public Description",
                fmt_string(_awp_value("proj_desc", project, "Proj_Desc")),
                textarea=True,
            )

        else:
            # Editable public description with a hard character cap.
            st.session_state["proj_desc"] = st.text_area(
                "Public Description ⮜",
                height=200,
                max_chars=8000,
                value=st.session_state.get("proj_desc", project.get("Proj_Desc", "")),
                key=widget_key("proj_desc", version, is_awp),
            )

        st.write("")
        st.write("")

        # ---------------------------------------------------------------------
        # 7. Contact
        # ---------------------------------------------------------------------
        st.markdown("<h6>7. CONTACT</h6>", unsafe_allow_html=True)

        if is_awp:
            ro_widget(
                "contact_name",
                "Contact",
                fmt_string(_awp_value("contact_name", project, "Contact_Name")),
            )

            col18, col19 = st.columns(2)

            with col18:
                ro_widget(
                    "contact_email",
                    "Email",
                    fmt_string(_awp_value("contact_email", project, "Contact_Email")),
                )

            with col19:
                ro_widget(
                    "contact_phone",
                    "Phone",
                    fmt_string(_awp_value("contact_phone", project, "Contact_Phone")),
                )

        else:
            st.session_state["contact_name"] = st.text_input(
                label="Name",
                key=widget_key("contact_name", version, is_awp),
                value=st.session_state.get("contact_name", project.get("Contact_Name", "")),
            )

            col18, col19 = st.columns(2)

            with col18:
                st.session_state["contact_email"] = st.text_input(
                    label="Email",
                    key=widget_key("awp_contact_email", version, is_awp),
                    value=st.session_state.get("contact_email", project.get("Contact_Email", "")),
                )

            with col19:
                st.session_state["contact_phone"] = st.text_input(
                    label="Phone",
                    key=widget_key("contact_phone", version, is_awp),
                    value=st.session_state.get("contact_phone", project.get("Contact_Phone", "")),
                )

        st.write("")
        st.write("")

        # ---------------------------------------------------------------------
        # 8. Web Link
        # ---------------------------------------------------------------------
        st.markdown("<h6>8. WEB LINK</h6>", unsafe_allow_html=True)

        if is_awp:
            ro_widget(
                "proj_web",
                "Project Website",
                fmt_string(_awp_value("proj_web", project, "Proj_Web")),
            )

        else:
            st.session_state["proj_web"] = st.text_input(
                label="Project Website",
                key=widget_key("proj_web", version, is_awp),
                value=st.session_state.get("proj_web", project.get("Proj_Web", "")),
            )

    # -------------------------------------------------------------------------
    # Buttons and Progress Placeholder
    # -------------------------------------------------------------------------
    information_buttons = st.container(border=False)

    with information_buttons:
        # Update Button.
        st.button(
            "UPDATE INFORMATION",
            type="primary",
            use_container_width=True,
            on_click=lambda: _on_update_information(is_awp),
        )

        # Progress bar placeholder.
        # Placed directly below the buttons and spans the width of this container.
        progress_placeholder = st.empty()

        # Store a handle in session_state so the callback can update and clear it.
        st.session_state["info_progress_placeholder"] = progress_placeholder
