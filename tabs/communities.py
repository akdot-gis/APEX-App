import streamlit as st
from typing import Optional  # (ensure present at top of file)
import json
import hashlib
from typing import Optional, Dict, Any, List

# Mapping libs
import folium
from streamlit_folium import st_folium
# Your helpers
from util.map_util import (
    geometry_to_folium,
    set_bounds_point
)
from util.geometry_util import select_community  # <-- uses the updated function
# AGOL helpers you already have
from agol.agol_util import select_record
from agol.agol_util import AGOLDataLoader  # applyEdits wrapper (add/update/delete)
from agol.agol_payloads import manage_communities_payloads

# -----------------------------------------------------------------------------
# Small utility: stable fingerprint for change detection (kept for future use)
# -----------------------------------------------------------------------------
def _fingerprint(obj) -> str:
    try:
        return hashlib.md5(
            json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except Exception:
        return f"{type(obj).__name__}:{str(obj)[:200]}"

# -----------------------------------------------------------------------------
# Fetch existing Impacted Communities for the current project
# -----------------------------------------------------------------------------
def fetch_impacted_communities(force: bool = False) -> List[Dict[str, Any]]:
    apex_guid = st.session_state.get("apex_guid")
    apex_url = st.session_state.get("apex_url")
    lyr_idx = st.session_state.get("impact_comms_layer")
    # Canonical fields from the feature layer
    fields = (
        st.session_state.get("impacted_communities_fields")
        or ["Community_Name", "Community_Contact", "Community_Contact_Email", "Community_Contact_Phone"]
    )

    if not (apex_guid and apex_url is not None and lyr_idx is not None):
        st.session_state["existing_communities_records"] = []
        st.session_state["impacted_communities_list"] = []
        st.session_state["_communities_loaded_for_guid"] = None
        return []

    loaded_for = st.session_state.get("_communities_loaded_for_guid")
    if not force and loaded_for == apex_guid:
        return st.session_state.get("existing_communities_records", []) or []

    def _as_point_dict(pt_geom: Optional[dict]) -> Optional[dict]:
        if not pt_geom:
            return None
        x = pt_geom.get("x")
        y = pt_geom.get("y")
        if x is None or y is None:
            return None
        # NOTE: Do not reorder; your app treats x=lng, y=lat
        return {"lonlat": [x, y], "lat": y, "lng": x}

    # Query (server-side "*" for attributes), then pick the exact fields we care about
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
        # STRICT: Only carry the four canonical fields into attributes for UI/state
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
        # STRICT: the record's display name is the Community_Name field from attributes
        rec["name"] = (picked_attributes.get("Community_Name") or None)
        records.append(rec)

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

# -----------------------------------------------------------------------------
# DEPLOY (expects a ready applyEdits payload; no internal payload building)
# -----------------------------------------------------------------------------
def _deploy_to_agol_communities(
    payload: Dict[str, Any],
    edit_type: str,
    *,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None,
) -> Dict[str, Any]:
    base_url = st.session_state.get("apex_url")
    lyr_idx = st.session_state.get("impact_comms_layer")
    if base_url is None or lyr_idx is None:
        st.error("AGOL layer is not configured")
        return {"community": {"success": False}}

    loader = AGOLDataLoader(base_url, lyr_idx)

    def _progress(frac, text):
        if progress_placeholder is not None:
            progress_placeholder.progress(frac, text=text)
        else:
            st.progress(frac, text=text)

    _progress(0.0, f"Submitting {edit_type} to AGOL…")

    # Submit exactly what the payload builder produced
    if edit_type == "adds":
        res = loader.add_features(payload)
    elif edit_type == "updates":
        # Normalize OBJECTID just in case (does not alter UI/behavior)
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

# -----------------------------------------------------------------------------
# UI: Manage Impacted Communities (tabs)
# -----------------------------------------------------------------------------
def manage_impacted_communities():
    curr_guid = st.session_state.get("guid")
    prev_guid = st.session_state.get("_ic_guid")
    if curr_guid is not None and prev_guid != curr_guid:
        st.session_state.pop("communities", None)
        st.session_state.pop("community_next_id", None)
        st.session_state.pop("impacted_communities_list", None)
        st.session_state["_communities_loaded_for_guid"] = None
        st.session_state["_ic_guid"] = curr_guid

    st.session_state.setdefault("communities", [])
    st.session_state.setdefault("community_next_id", 1)
    st.session_state.setdefault("impacted_communities_list", [])

    # Canonical fields (Community_Name is hidden in UI but used for tabs/payloads)
    fields = (
        st.session_state.get("impacted_communities_fields")
        or ["Community_Name", "Community_Contact", "Community_Contact_Email", "Community_Contact_Phone"]
    )

    def _new_community(label: str = "") -> dict:
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
        # rec["name"] was set to attributes["Community_Name"] during fetch
        ev = _new_community(label=(rec.get("name") or ""))
        ev["field_values"] = dict(rec.get("attributes") or {})
        ev["selected_point"] = rec.get("point")
        ev["objectid"] = rec.get("objectid")
        ev["globalid"] = rec.get("globalid")
        ev["initialized_from_record"] = True
        return ev

    # ----- HEADER -----
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

    # ----- LOAD EXISTING -----
    pulled_records = fetch_impacted_communities(force=False)
    existing_records = bool(pulled_records)
    if pulled_records and not st.session_state["communities"]:
        st.session_state["communities"] = [_community_from_record(r) for r in pulled_records]

    communities = st.session_state["communities"]

    # ---- helper to build package for the picker (now includes communities context) ----
    def _resolve_package_for_community(ev) -> dict:
        base = {
            "objectid": ev.get("objectid"),
            "attributes": dict(ev.get("field_values") or {}),
            "point": ev.get("selected_point"),
        }
        # Provide communities dataset context (explicitly pass to the picker)
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
        # Persist any prior dropdown selection for the tab (so it restores)
        if ev.get("selected_community_id") is not None:
            base["selected_community_id"] = ev.get("selected_community_id")
        if ev.get("selected_community_name") is not None:
            base["selected_community_name"] = ev.get("selected_community_name")
        return base

    def _clear_tab_selection(key_prefix: str, ev: dict):
        ev["field_values"] = {f: None for f in fields}
        ev["selected_point"] = None
        # IMPORTANT: Do not touch ev["label"]; tab titles remain unchanged until re-pull
        ev["objectid"] = None
        ev["globalid"] = None
        # clear any transient map/input state keys if present
        st.session_state.pop(f"{key_prefix}folium", None)
        st.session_state.pop(f"{key_prefix}Community_Contact", None)
        st.session_state.pop(f"{key_prefix}Community_Contact_Phone", None)
        st.session_state.pop(f"{key_prefix}Community_Contact_Email", None)

    # ---- RENDER TAB ----
    def _render_community_tab(ev):
        key_prefix = f"cm{ev['community_id']}_"
        is_existing = bool(ev.get("initialized_from_record"))
        package_in = _resolve_package_for_community(ev)
        container = st.container(border=False)

        # ---- Use the picker: it now renders the text inputs + map internally ----
        try:
            package_out = select_community(
                container,
                key_prefix=key_prefix,
                is_existing=is_existing,
                package=package_in,  # explicitly pass enriched package
            )
        except TypeError:
            # Fallback signatures (kept for compatibility with older helper versions)
            try:
                package_out = select_community(
                    container,
                    key_prefix=key_prefix,
                    is_existing=is_existing,
                )
            except TypeError:
                package_out = package_in  # last resort: keep the inbound package

        # ---- Persist returned data from the picker (authoritative fields now)
        if isinstance(package_out, dict):
            # 1) point (direct replace if present)
            if package_out.get("point"):
                ev["selected_point"] = package_out["point"]
            # 2) fields/attributes: accept fields from picker as authoritative
            out_fields = package_out.get("fields") or package_out.get("attributes") or {}
            if isinstance(out_fields, dict):
                ev["field_values"] = dict(out_fields)
            # 3) remember dropdown selection for this tab so it's restored on next render
            if "selected_community_id" in package_out:
                ev["selected_community_id"] = package_out["selected_community_id"]
            if "selected_community_name" in package_out:
                ev["selected_community_name"] = package_out["selected_community_name"]

        # NOTE: DO NOT sync label from Community_Name here.
        # Tabs are only updated after submit + re-pull.

        # Build the package used for validation (UI behavior unchanged)
        package_final = {
            "objectid": ev.get("objectid"),
            "attributes": dict(ev.get("field_values") or {}),
            "point": ev.get("selected_point"),
        }

        btn_col1, btn_col2 = st.columns([1, 1])
        progress_placeholder = st.empty()

        if is_existing:
            # ===================== UPDATE =====================
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
                        # ✅ build payload directly from package_out (not package_final)
                        payload = manage_communities_payloads(package_out, "updates")
                        _ = _deploy_to_agol_communities(
                            payload,
                            edit_type="updates",
                            progress_placeholder=progress_placeholder,
                        )
                        # Refresh from AGOL
                        pulled = fetch_impacted_communities(force=True)
                        st.session_state["communities"] = []
                        st.session_state["community_next_id"] = 1
                        for rec in (pulled or []):
                            st.session_state["communities"].append(_community_from_record(rec))
                        st.rerun()

            # ===================== DELETE =====================
            with btn_col2:
                if st.button(
                    "DELETE",
                    use_container_width=True,
                    key=f"{key_prefix}btn_delete",
                ):
                    if not package_final.get("objectid"):
                        st.warning("Missing OBJECTID for delete.")
                    else:
                        # ✅ build payload directly from package_out (not package_final)
                        payload = manage_communities_payloads(package_out, "deletes")
                        delete_result = _deploy_to_agol_communities(
                            payload,
                            edit_type="deletes",
                            progress_placeholder=progress_placeholder,
                        )
                        if not delete_result.get("community", {}).get("success", False):
                            st.warning("AGOL delete may have failed. Check logs.")
                        # Refresh from AGOL
                        pulled = fetch_impacted_communities(force=True)
                        st.session_state["communities"] = []
                        st.session_state["community_next_id"] = 1
                        for rec in (pulled or []):
                            st.session_state["communities"].append(_community_from_record(rec))
                        st.rerun()
        else:
            # ===================== ADD =====================
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
                        # ✅ build payload directly from package_out (not package_final)
                        payload = manage_communities_payloads(package_out, "adds")
                        add_result = _deploy_to_agol_communities(
                            payload,
                            edit_type="adds",
                            progress_placeholder=progress_placeholder,
                        )
                        if not add_result.get("community", {}).get("success", False):
                            st.warning("AGOL add may have failed. Check logs.")
                        # Refresh from AGOL
                        pulled = fetch_impacted_communities(force=True)
                        st.session_state["communities"] = []
                        st.session_state["community_next_id"] = 1
                        for rec in (pulled or []):
                            st.session_state["communities"].append(_community_from_record(rec))
                        st.rerun()

            # ===================== CLEAR =====================
            with btn_col2:
                if st.button(
                    "CLEAR",
                    use_container_width=True,
                    key=f"{key_prefix}btn_clear",
                ):
                    _clear_tab_selection(key_prefix, ev)
                    st.rerun()

    # ---- Tabs ----
    # STRICT: Tab title does NOT update from in-tab edits. It is fixed:
    # - existing: from record name at load
    # - new: "New Community" until submit + re-pull
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