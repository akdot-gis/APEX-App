"""Impacted communities management for the APEX Streamlit manager page.

This module renders and manages the Impacted Communities tab in the larger APEX
Streamlit application. It retrieves existing impacted community records from
ArcGIS Online, stages those records into Streamlit session state, renders each
community record in a tabbed interface, and supports adding, updating, deleting,
or clearing impacted community selections.

The module also deploys already-built impacted community applyEdits payloads to
the configured AGOL impacted communities layer. Existing Streamlit UI text,
widget keys, session_state key names, function names, variable names, imports,
payload fields, and execution behavior are intentionally preserved.
"""

# =============================================================================
# Imports
# =============================================================================

# Standard library
import hashlib
import json
from typing import Any, Dict, List, Optional

# Third-party
import streamlit as st

# Local application: AGOL access and payload helpers
from agol.agol_payloads import manage_communities_payloads
from agol.agol_util import AGOLDataLoader
from agol.agol_util import select_record

# Local application: community selector and map helpers
from util.geometry_util import select_community


# =============================================================================
# Session State Access Notes
# =============================================================================

# This file reads and writes Streamlit session state throughout the impacted
# communities workflow. Session state access remains inside the functions where
# values are used because community records, tab state, selected points, field
# values, and AGOL deployment results are updated during Streamlit reruns and
# button callbacks. Moving those reads to module scope could capture stale values
# and change behavior.


# =============================================================================
# Existing Impacted Community Loading
# =============================================================================

def fetch_impacted_communities(force: bool = False) -> List[Dict[str, Any]]:
    """Fetch existing impacted communities for the active APEX project.

    The active project is identified using ``apex_guid`` from Streamlit session
    state. Records are queried from the configured impacted communities layer,
    normalized into a consistent structure, and staged into session state for the
    tab UI and community selector workflow.

    Populates:
        st.session_state["existing_communities_records"]:
            Normalized impacted community records.
        st.session_state["impacted_communities_list"]:
            Working list used by the selector and payload workflow.
        st.session_state["_communities_loaded_for_guid"]:
            The project GUID that was last loaded.

    Args:
        force (bool): When True, reload records from AGOL even if the current
            project GUID was already loaded.

    Returns:
        List[Dict[str, Any]]: Normalized impacted community records.
    """
    # Read active project and impacted communities layer configuration.
    apex_guid = st.session_state.get("apex_guid")
    apex_url = st.session_state.get("apex_url")
    lyr_idx = st.session_state.get("impact_comms_layer")

    # Canonical fields from the feature layer.
    fields = (
        st.session_state.get("impacted_communities_fields")
        or ["Community_Name", "Community_Contact", "Community_Contact_Email", "Community_Contact_Phone"]
    )

    # If required configuration is missing, clear staged records and stop.
    if not (apex_guid and apex_url is not None and lyr_idx is not None):
        st.session_state["existing_communities_records"] = []
        st.session_state["impacted_communities_list"] = []
        st.session_state["_communities_loaded_for_guid"] = None
        return []

    # Reuse previously loaded records unless a forced refresh is requested.
    loaded_for = st.session_state.get("_communities_loaded_for_guid")
    if not force and loaded_for == apex_guid:
        return st.session_state.get("existing_communities_records", []) or []

    def _as_point_dict(pt_geom: Optional[dict]) -> Optional[Dict[str, float]]:
        """Normalize Esri point geometry into the app's point dictionary shape.

        Args:
            pt_geom (Optional[dict]): Esri point geometry with ``x`` and ``y``.

        Returns:
            Optional[Dict[str, float]]: Point dictionary containing lonlat, lat, and lng
                values, or None when geometry is missing or incomplete.
        """
        if not pt_geom:
            return None

        x = pt_geom.get("x")
        y = pt_geom.get("y")

        if x is None or y is None:
            return None

        # NOTE: Do not reorder; the app treats x=lng, y=lat.
        return {"lonlat": [x, y], "lat": y, "lng": x}

    # Query server-side attributes and geometry, then retain only canonical fields.
    features = select_record(
        url=apex_url,
        layer=lyr_idx,
        id_field="parentglobalid",
        id_value=apex_guid,
        fields="*",
        return_geometry=True,
    ) or []

    records: List[Dict[str, Any]] = []

    for feat in features:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}

        # STRICT: Only carry the four canonical fields into attributes for UI/state.
        picked_attributes = {
            f: attrs.get(f)
            for f in ["Community_Name", "Community_Contact", "Community_Contact_Email", "Community_Contact_Phone"]
        }

        rec = {
            "objectid": attrs.get("objectid"),
            "globalid": attrs.get("globalid"),
            "attributes": picked_attributes,
            "point": _as_point_dict(geom),
        }

        # STRICT: the record's display name is the Community_Name field from attributes.
        rec["name"] = picked_attributes.get("Community_Name") or None

        records.append(rec)

    # Stage normalized records for the UI and payload workflow.
    st.session_state["existing_communities_records"] = records
    st.session_state["impacted_communities_list"] = [
        {
            "objectid": r.get("objectid"),
            "globalid": r.get("globalid"),
            "attributes": dict(r.get("attributes") or {}),
            "point": r.get("point"),
        }
        for r in records
    ]
    st.session_state["_communities_loaded_for_guid"] = apex_guid

    return records


# =============================================================================
# AGOL Deployment
# =============================================================================

def _deploy_to_agol_communities(
    payload: Dict[str, Any],
    edit_type: str,
    *,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None,
) -> Dict[str, Any]:
    """Deploy an impacted communities applyEdits payload to AGOL.

    This function expects a ready payload from ``manage_communities_payloads``.
    It does not build payloads internally. The payload is submitted to the
    configured impacted communities layer using the requested edit type.

    Args:
        payload (Dict[str, Any]): Ready applyEdits payload for the impacted
            communities layer.
        edit_type (str): Edit operation type. Expected values are ``"adds"``,
            ``"updates"``, or ``"deletes"``.
        progress_placeholder (Optional[st.delta_generator.DeltaGenerator]):
            Optional Streamlit placeholder used to render progress in the UI.

    Returns:
        Dict[str, Any]: Existing function signature indicates a dictionary
            return. Existing execution behavior is preserved as authored.
    """
    # Resolve the impacted communities target layer from session state.
    base_url = st.session_state.get("apex_url")
    lyr_idx = st.session_state.get("impact_comms_layer")

    if base_url is None or lyr_idx is None:
        st.error("AGOL layer is not configured")
        return {"community": {"success": False}}

    loader = AGOLDataLoader(base_url, lyr_idx)

    def _progress(frac, text):
        """Render deployment progress in the provided placeholder or inline.

        Args:
            frac: Progress fraction passed to Streamlit.
            text: Progress text displayed to the user.

        Returns:
            None.
        """
        if progress_placeholder is not None:
            progress_placeholder.progress(frac, text=text)
        else:
            st.progress(frac, text=text)

    _progress(0.0, f"Submitting {edit_type} to AGOL…")

    # Submit exactly what the payload builder produced.
    if edit_type == "adds":
        res = loader.add_features(payload)
    elif edit_type == "updates":
        # Normalize OBJECTID just in case.
        if isinstance(payload, dict) and "updates" in payload:
            for rec in payload.get("updates") or []:
                attrs = rec.get("attributes", {})
                if "OBJECTID" not in attrs and "objectId" in attrs:
                    attrs["OBJECTID"] = attrs.pop("objectId")

        res = loader.update_features(payload)
    elif edit_type == "deletes":
        res = loader.delete_features(payload)
    else:
        st.error(f"Unknown edit_type: {edit_type}")
        return {"community": {"success": False, "message": f"Unknown {edit_type}"}}

    _progress(1.0, "Done")
    return res


# =============================================================================
# Main Streamlit Page Rendering
# =============================================================================

def manage_impacted_communities():
    """Render and manage impacted communities for the active APEX project.

    This function loads existing impacted community records, initializes local
    tab state, renders one tab per community, delegates community selection and
    field entry to ``select_community``, and handles ADD, UPDATE, DELETE, and
    CLEAR actions.

    Returns:
        None: Streamlit components are rendered directly to the page.
    """
    # -------------------------------------------------------------------------
    # Reset Per-Project State When GUID Changes
    # -------------------------------------------------------------------------

    curr_guid = st.session_state.get("guid")
    prev_guid = st.session_state.get("_ic_guid")

    if curr_guid is not None and prev_guid != curr_guid:
        st.session_state.pop("communities", None)
        st.session_state.pop("community_next_id", None)
        st.session_state.pop("impacted_communities_list", None)
        st.session_state["_communities_loaded_for_guid"] = None
        st.session_state["_ic_guid"] = curr_guid

    # -------------------------------------------------------------------------
    # Module Persistent State
    # -------------------------------------------------------------------------

    st.session_state.setdefault("communities", [])
    st.session_state.setdefault("community_next_id", 1)
    st.session_state.setdefault("impacted_communities_list", [])

    # Canonical fields. Community_Name is hidden in UI but used for tabs/payloads.
    fields = (
        st.session_state.get("impacted_communities_fields")
        or ["Community_Name", "Community_Contact", "Community_Contact_Email", "Community_Contact_Phone"]
    )

    # -------------------------------------------------------------------------
    # Local Event Model Helpers
    # -------------------------------------------------------------------------

    def _new_community(label: str = "") -> dict:
        """Create a new local impacted community state dictionary.

        Args:
            label (str): Tab label for the local community entry.

        Returns:
            dict: New local community record state.
        """
        cid = st.session_state["community_next_id"]
        st.session_state["community_next_id"] += 1

        return {
            "community_id": cid,
            "label": label,  # set from record on load; stays static for new until re-pull
            "field_values": {f: None for f in fields},
            "selected_point": None,
            "initialized_from_record": False,
            "objectid": None,
            "globalid": None,
        }

    def _community_from_record(rec) -> dict:
        """Create a local community state dictionary from an AGOL record.

        Args:
            rec (dict): Normalized impacted community record.

        Returns:
            dict: Local community state initialized from the record.
        """
        # rec["name"] was set to attributes["Community_Name"] during fetch.
        ev = _new_community(label=(rec.get("name") or ""))
        ev["field_values"] = dict(rec.get("attributes") or {})
        ev["selected_point"] = rec.get("point")
        ev["objectid"] = rec.get("objectid")
        ev["globalid"] = rec.get("globalid")
        ev["initialized_from_record"] = True

        return ev

    # -------------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------------

    title_text = "##### MANAGE IMPACTED COMMUNITIES"
    title_col, btn_col = st.columns([5.5, 2.5], vertical_alignment="center")

    with title_col:
        st.markdown(f"{title_text}\n")

    with btn_col:
        if st.button("✚ **ADD COMMUNITY**", key="btn_add_community_header", use_container_width=True):
            st.session_state["communities"].append(_new_community())
            st.rerun()

    st.caption(
        "Review all communities affected by this project and perform management actions, including adding new communities, "
        "updating existing records, or deleting entries that are no longer applicable."
    )
    st.write("")

    # -------------------------------------------------------------------------
    # Load Existing Communities
    # -------------------------------------------------------------------------

    pulled_records = fetch_impacted_communities(force=False)
    existing_records = bool(pulled_records)

    if pulled_records and not st.session_state["communities"]:
        st.session_state["communities"] = [_community_from_record(r) for r in pulled_records]

    communities = st.session_state["communities"]

    # -------------------------------------------------------------------------
    # Local Package and State Helpers
    # -------------------------------------------------------------------------

    def _resolve_package_for_community(ev) -> dict:
        """Build the community selector package for a local community entry.

        The package includes the current community fields, selected point, object
        ID, and the configured communities lookup context required by the picker.

        Args:
            ev (dict): Local community state dictionary.

        Returns:
            dict: Package passed into ``select_community``.
        """
        base = {
            "objectid": ev.get("objectid"),
            "attributes": dict(ev.get("field_values") or {}),
            "point": ev.get("selected_point"),
        }

        # Provide communities dataset context explicitly to the picker.
        base["communities_list"] = (
            st.session_state.get("communities_list")
            or st.session_state.get("dcced_communities_list")
            or []
        )
        base["communities_url"] = (
            st.session_state.get("communities_url")
            or st.session_state.get("dcced_communities_url")
            or ""
        )
        base["communities_layer"] = (
            st.session_state.get("communities_layer")
            or st.session_state.get("dcced_communities_layer")
            or 7
        )
        base["communities_id_field"] = (
            st.session_state.get("communities_id_field")
            or st.session_state.get("dcced_communities_id_field")
            or "DCCED_CommunityId"
        )

        # Persist prior dropdown selection for the tab so it restores.
        if ev.get("selected_community_id") is not None:
            base["selected_community_id"] = ev.get("selected_community_id")

        if ev.get("selected_community_name") is not None:
            base["selected_community_name"] = ev.get("selected_community_name")

        return base

    def _clear_tab_selection(key_prefix: str, ev: dict):
        """Clear field and map selection state for a community tab.

        Args:
            key_prefix (str): Community-specific widget key prefix.
            ev (dict): Local community state dictionary to clear.

        Returns:
            None: Event state and related session_state keys are cleared in place.
        """
        ev["field_values"] = {f: None for f in fields}
        ev["selected_point"] = None

        # IMPORTANT: Do not touch ev["label"]; tab titles remain unchanged until re-pull.
        ev["objectid"] = None
        ev["globalid"] = None

        # Clear transient map/input state keys if present.
        st.session_state.pop(f"{key_prefix}folium", None)
        st.session_state.pop(f"{key_prefix}Community_Contact", None)
        st.session_state.pop(f"{key_prefix}Community_Contact_Phone", None)
        st.session_state.pop(f"{key_prefix}Community_Contact_Email", None)

    # -------------------------------------------------------------------------
    # Community Tab Rendering
    # -------------------------------------------------------------------------

    def _render_community_tab(ev):
        """Render one impacted community tab.

        Args:
            ev (dict): Local impacted community state dictionary.

        Returns:
            None: Streamlit components are rendered directly in the active tab.
        """
        key_prefix = f"cm{ev['community_id']}_"
        is_existing = bool(ev.get("initialized_from_record"))
        package_in = _resolve_package_for_community(ev)
        container = st.container(border=False)

        # Use the picker: it renders the text inputs and map internally.
        try:
            package_out = select_community(
                container,
                key_prefix=key_prefix,
                is_existing=is_existing,
                package=package_in,
            )
        except TypeError:
            # Fallback signatures kept for compatibility with older helper versions.
            try:
                package_out = select_community(
                    container,
                    key_prefix=key_prefix,
                    is_existing=is_existing,
                )
            except TypeError:
                package_out = package_in

        # Persist returned data from the picker.
        if isinstance(package_out, dict):
            # 1) Point is directly replaced if present.
            if package_out.get("point"):
                ev["selected_point"] = package_out["point"]

            # 2) Fields/attributes from picker are authoritative.
            out_fields = package_out.get("fields") or package_out.get("attributes") or {}
            if isinstance(out_fields, dict):
                ev["field_values"] = dict(out_fields)

            # 3) Remember dropdown selection for this tab so it restores next render.
            if "selected_community_id" in package_out:
                ev["selected_community_id"] = package_out["selected_community_id"]

            if "selected_community_name" in package_out:
                ev["selected_community_name"] = package_out["selected_community_name"]

        # NOTE: DO NOT sync label from Community_Name here.
        # Tabs are only updated after submit + re-pull.

        # Build the package used for validation.
        package_final = {
            "objectid": ev.get("objectid"),
            "attributes": dict(ev.get("field_values") or {}),
            "point": ev.get("selected_point"),
        }

        btn_col1, btn_col2 = st.columns([1, 1])
        progress_placeholder = st.empty()

        if is_existing:
            # =================================================================
            # UPDATE
            # =================================================================
            with btn_col1:
                if st.button(
                    "UPDATE",
                    use_container_width=True,
                    type="primary",
                    key=f"{key_prefix}btn_update",
                ):
                    missing_contact = not (package_final.get("attributes") or {}).get("Community_Contact")
                    missing_phone = not (package_final.get("attributes") or {}).get("Community_Contact_Phone")
                    missing_email = not (package_final.get("attributes") or {}).get("Community_Contact_Email")
                    missing_point = not package_final.get("point")

                    if missing_contact or missing_phone or missing_email or missing_point:
                        st.warning("Provide Contact, Phone, Email, and a map location before updating.")
                    else:
                        # Build payload directly from package_out.
                        payload = manage_communities_payloads(package_out, "updates")
                        _ = _deploy_to_agol_communities(
                            payload,
                            edit_type="updates",
                            progress_placeholder=progress_placeholder,
                        )

                        # Refresh from AGOL after update.
                        pulled = fetch_impacted_communities(force=True)
                        st.session_state["communities"] = []
                        st.session_state["community_next_id"] = 1

                        for rec in (pulled or []):
                            st.session_state["communities"].append(_community_from_record(rec))

                        st.rerun()

            # =================================================================
            # DELETE
            # =================================================================
            with btn_col2:
                if st.button(
                    "DELETE",
                    use_container_width=True,
                    key=f"{key_prefix}btn_delete",
                ):
                    if not package_final.get("objectid"):
                        st.warning("Missing OBJECTID for delete.")
                    else:
                        # Build payload directly from package_out.
                        payload = manage_communities_payloads(package_out, "deletes")
                        delete_result = _deploy_to_agol_communities(
                            payload,
                            edit_type="deletes",
                            progress_placeholder=progress_placeholder,
                        )

                        if not delete_result.get("success", False):
                            st.warning("AGOL delete may have failed. Check logs.")

                        # Refresh from AGOL after delete.
                        pulled = fetch_impacted_communities(force=True)
                        st.session_state["communities"] = []
                        st.session_state["community_next_id"] = 1

                        for rec in (pulled or []):
                            st.session_state["communities"].append(_community_from_record(rec))

                        st.rerun()

        else:
            # =================================================================
            # ADD
            # =================================================================
            with btn_col1:
                if st.button(
                    "ADD",
                    use_container_width=True,
                    type="primary",
                    key=f"{key_prefix}btn_add",
                ):
                    missing_contact = not (package_final.get("attributes") or {}).get("Community_Contact")
                    missing_phone = not (package_final.get("attributes") or {}).get("Community_Contact_Phone")
                    missing_email = not (package_final.get("attributes") or {}).get("Community_Contact_Email")
                    missing_point = not package_final.get("point")

                    if missing_contact or missing_phone or missing_email or missing_point:
                        st.warning("Provide Contact, Phone, Email, and a map location before adding.")
                    else:
                        # Build payload directly from package_out.
                        payload = manage_communities_payloads(package_out, "adds")
                        add_result = _deploy_to_agol_communities(
                            payload,
                            edit_type="adds",
                            progress_placeholder=progress_placeholder,
                        )

                        if not add_result.get("success", False):
                            st.warning("AGOL add may have failed. Check logs.")

                        # Refresh from AGOL after add.
                        pulled = fetch_impacted_communities(force=True)
                        st.session_state["communities"] = []
                        st.session_state["community_next_id"] = 1

                        for rec in (pulled or []):
                            st.session_state["communities"].append(_community_from_record(rec))

                        st.rerun()

            # =================================================================
            # CLEAR
            # =================================================================
            with btn_col2:
                if st.button(
                    "CLEAR",
                    use_container_width=True,
                    key=f"{key_prefix}btn_clear",
                ):
                    _clear_tab_selection(key_prefix, ev)
                    st.rerun()

    # -------------------------------------------------------------------------
    # Tabs
    # -------------------------------------------------------------------------

    # STRICT: Tab title does NOT update from in-tab edits. It is fixed:
    # - existing: from record name at load
    # - new: "New Community" until submit + re-pull.
    tab_labels = [
        (ev.get("label") or "New Community")
        for i, ev in enumerate(communities)
    ]

    if communities:
        tabs = st.tabs(tab_labels)

        for tab, ev in zip(tabs, communities):
            with tab:
                _render_community_tab(ev)
    else:
        if existing_records:
            st.info("Existing Impacted Communities were found, but could not initialize tabs.")
        else:
            st.info("No Impacted Communities were found for this project.")