"""Traffic impact work extent management for the APEX Streamlit manager page.

This module renders and manages the Traffic Impacts tab in the larger APEX
Streamlit application. It supports loading existing Traffic Impact Events from
ArcGIS Online, staging those records into Streamlit session state, rendering a
single selected impact event at a time, and adding, updating, deleting, or
clearing traffic impact work extents.

The module also builds and deploys Traffic Impact Event payloads for the parent
polygon, impacted route, start point, and end point layers. Existing Streamlit
UI text, widget keys, session_state key names, function names, variable names,
payload fields, and execution behavior are intentionally preserved.
"""

# =============================================================================
# Imports
# =============================================================================

# Standard library
import hashlib
import json
from typing import Callable, Optional

# Third-party
import streamlit as st

# Local application: AGOL access and deployment helpers
from agol.agol_payloads import manage_traffic_impact_payloads
from agol.agol_util import AGOLDataLoader, select_record

# Local application: geometry and geospatial helpers
from util.geometry_util import (
    geometry_to_folium,
    select_route_and_points,
)
from util.geospatial_util import create_buffers


# =============================================================================
# Session State Access Notes
# =============================================================================

# This file reads and writes Streamlit session state throughout the Traffic
# Impact Event workflow. Session state reads remain inside the functions where
# they are used because traffic impact records, selector state, map state, and
# deployment values are updated during Streamlit reruns and callbacks. Moving
# those reads to module scope could capture stale values and change behavior.


# =============================================================================
# Utility Helpers
# =============================================================================

def _fingerprint(obj) -> str:
    """Return a stable fingerprint string for a Python object.

    The fingerprint is used for change detection so the project impact buffer
    can be recomputed only when the underlying project area or buffer parameters
    change.

    Args:
        obj: Object to serialize and hash.

    Returns:
        str: MD5 hash of the JSON-serialized object when possible; otherwise a
            fallback string based on the object type and truncated string value.
    """
    try:
        return hashlib.md5(
            json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except Exception:
        # Fall back to a readable type/value signature when JSON serialization fails.
        return f"{type(obj).__name__}:{str(obj)[:200]}"


# =============================================================================
# Traffic Impact Record Loading
# =============================================================================

def fetch_traffic_impacts(
    force: bool = False,
    progress_cb: Optional[Callable[[int, str], None]] = None,
):
    """Fetch existing Traffic Impact Events for the active APEX project.

    Pulls all impacted route records for the current ``apex_guid`` and stages
    normalized records into Streamlit session state.

    Populates:
        st.session_state["existing_tie_records"]:
            List of normalized Traffic Impact Event records.
        st.session_state["traffic_impacts_list"]:
            Working list used by the Traffic Impacts UI and selector flow.
        st.session_state["_tie_loaded_for_apex_guid"]:
            GUID marker used to avoid reloading the same records repeatedly.

    Args:
        force (bool): When True, reload records even if the active project was
            already loaded.
        progress_cb (Optional[Callable[[int, str], None]]): Optional callback
            used to report progress percentage and progress text.

    Returns:
        listNormalized Traffic Impact Event records.
    """
    # Read active project and Traffic Impact layer configuration from session state.
    apex_guid = st.session_state.get("apex_guid")
    ti_url = st.session_state.get("traffic_impact_url")
    lyr_parent = st.session_state.get("traffic_impacts_layer")
    lyr_routes = st.session_state.get("traffic_impact_routes_layer")
    lyr_start = st.session_state.get("traffic_impact_start_points_layer")
    lyr_end = st.session_state.get("traffic_impact_end_points_layer")

    # If not fully wired, clear staged records and stop.
    if not (
        apex_guid
        and ti_url
        and lyr_parent is not None
        and lyr_routes is not None
        and lyr_start is not None
        and lyr_end is not None
    ):
        st.session_state["existing_tie_records"] = []
        st.session_state["traffic_impacts_list"] = []
        st.session_state["_tie_loaded_for_apex_guid"] = None

        if progress_cb:
            progress_cb(100, "No traffic impact configuration found")

        return []

    # Reuse previously pulled data unless force=True or apex_guid changed.
    loaded_for = st.session_state.get("_tie_loaded_for_apex_guid")
    if not force and loaded_for == apex_guid:
        if progress_cb:
            progress_cb(100, "Traffic impacts already loaded")

        return st.session_state.get("existing_tie_records", []) or []

    # -------------------------------------------------------------------------
    # Geometry Normalization Helpers
    # -------------------------------------------------------------------------

    def _as_lonlat_list(lines_geom):
        """Return flattened lon/lat coordinate pairs from line or polygon geometry.

        Args:
            lines_geom: Esri geometry dictionary containing ``paths`` or ``rings``.

        Returns:
            list: Flattened list of ``[x, y]`` coordinate pairs.
        """
        if not lines_geom:
            return []

        paths = lines_geom.get("paths")
        if isinstance(paths, list) and paths:
            coords = []
            for part in paths:
                if isinstance(part, list):
                    for xy in part:
                        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                            coords.append([xy[0], xy[1]])
            return coords

        rings = lines_geom.get("rings")
        if isinstance(rings, list) and rings:
            coords = []
            for ring in rings:
                if isinstance(ring, list):
                    for xy in ring:
                        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                            coords.append([xy[0], xy[1]])
            return coords

        return []

    def _as_point_dict(pt_geom):
        """Return a normalized point dictionary from Esri point geometry.

        Args:
            pt_geom: Esri point geometry dictionary containing ``x`` and ``y``.

        Returns:
            dict | None: Dictionary containing lonlat, lat, and lng values, or
                None when the geometry is missing or incomplete.
        """
        if not pt_geom:
            return None

        x = pt_geom.get("x")
        y = pt_geom.get("y")

        if x is None or y is None:
            return None

        return {"lonlat": [x, y], "lat": y, "lng": x}

    # -------------------------------------------------------------------------
    # Query Parent Features
    # -------------------------------------------------------------------------

    if progress_cb:
        progress_cb(20, "Querying impacted routes…")

    parent_features = select_record(
        url=ti_url,
        layer=lyr_parent,
        id_field="APEX_GUID",
        id_value=apex_guid,
        fields=["globalid", "objectid", "Event_Name", "Route_ID", "Route_Name"],
        return_geometry=True,
    ) or []

    # -------------------------------------------------------------------------
    # Query Child Features for Each Parent Event
    # -------------------------------------------------------------------------

    total = max(1, len(parent_features))
    records = []

    for idx, feat in enumerate(parent_features, start=1):
        if progress_cb:
            pct = 20 + int(60 * (idx - 1) / total)
            progress_cb(pct, f"Loading event {idx}/{total}… (parent)")

        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}

        ti_guid = attrs.get("globalid")
        ti_objectid = attrs.get("objectid")

        # NAME NORMALIZATION:
        # Existing Traffic Impact Events named "Blank Event" are shown as
        # "New Impact" before being passed into the UI.
        raw_name = attrs.get("Event_Name")
        ti_event_name = (
            "New Impact" if isinstance(raw_name, str) and raw_name.strip() == "Blank Event"
            else raw_name
        )

        ti_route_id = attrs.get("Route_ID")
        ti_route_name = attrs.get("Route_Name")
        ti_impact_area = geom.get("rings")

        # ---------------------------------------------------------------------
        # Route Child Record
        # ---------------------------------------------------------------------

        if progress_cb:
            progress_cb(pct + 5, f"Loading event {idx}/{total}… (route)")

        route_features = select_record(
            url=ti_url,
            layer=lyr_routes,
            id_field="parentglobalid",
            id_value=ti_guid,
            fields=["objectid", "globalid"],
            return_geometry=True,
        ) or []

        route_geom_lonlat, route_objectid = [], None

        if route_features:
            rf = route_features[0]
            route_objectid = (rf.get("attributes") or {}).get("objectid")
            route_geom_lonlat = _as_lonlat_list(rf.get("geometry") or {})

        # ---------------------------------------------------------------------
        # Start Point Child Record
        # ---------------------------------------------------------------------

        if progress_cb:
            progress_cb(pct + 15, f"Loading event {idx}/{total}… (start)")

        start_features = select_record(
            url=ti_url,
            layer=lyr_start,
            id_field="parentglobalid",
            id_value=ti_guid,
            fields=["objectid", "globalid"],
            return_geometry=True,
        ) or []

        start_point, start_objectid = None, None

        if start_features:
            sf = start_features[0]
            start_objectid = (sf.get("attributes") or {}).get("objectid")
            start_point = _as_point_dict(sf.get("geometry") or {})

        # ---------------------------------------------------------------------
        # End Point Child Record
        # ---------------------------------------------------------------------

        if progress_cb:
            progress_cb(pct + 25, f"Loading event {idx}/{total}… (end)")

        end_features = select_record(
            url=ti_url,
            layer=lyr_end,
            id_field="parentglobalid",
            id_value=ti_guid,
            fields=["objectid", "globalid"],
            return_geometry=True,
        ) or []

        end_point, end_objectid = None, None

        if end_features:
            ef = end_features[0]
            end_objectid = (ef.get("attributes") or {}).get("objectid")
            end_point = _as_point_dict(ef.get("geometry") or {})

        # Build normalized event record used by the UI and deployment helpers.
        rec = {
            "name": ti_event_name,
            "area": ti_impact_area,
            "route_id": ti_route_id,
            "route_name": ti_route_name,
            "route_geom": route_geom_lonlat or None,
            "start_point": start_point,
            "end_point": end_point,
            # Useful IDs if you later wire Update/Delete to AGOL
            "ti_guid": ti_guid,
            "ti_objectid": ti_objectid,
            "route_objectid": route_objectid,
            "start_objectid": start_objectid,
            "end_objectid": end_objectid,
        }
        records.append(rec)

    # -------------------------------------------------------------------------
    # Stage Results into Session State
    # -------------------------------------------------------------------------

    if progress_cb:
        progress_cb(85, "Staging events…")

    st.session_state["existing_tie_records"] = records

    # Keep IDs in the working list so they flow into the selector for existing events.
    st.session_state["traffic_impacts_list"] = [
        {
            "name": r.get("name"),
            "area": r.get("area"),
            "route_id": r.get("route_id"),
            "route_name": r.get("route_name"),
            "route_geom": r.get("route_geom"),
            "start_point": r.get("start_point"),
            "end_point": r.get("end_point"),
            "objectid": r.get("ti_objectid"),
            "route_objectid": r.get("route_objectid"),
            "start_objectid": r.get("start_objectid"),
            "end_objectid": r.get("end_objectid"),
        }
        for r in records
    ]
    st.session_state["_tie_loaded_for_apex_guid"] = apex_guid

    if progress_cb:
        progress_cb(95, "Finalizing…")
        progress_cb(100, "Done")

    return records


# =============================================================================
# AGOL Deployment Helper
# =============================================================================

def _deploy_to_agol(
    package: dict,
    edit_type: str,
    *,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None,
) -> dict:
    """Deploy Traffic Impact Event edits to AGOL.

    Builds the four layer payloads from ``package`` and submits them to AGOL
    with progress messaging. Supports ``adds``, ``updates``, and ``deletes``.

    For adds, the parent polygon is created first. Its returned global ID is
    stored in ``st.session_state["traffic_impact_globalid"]`` so child route,
    start point, and end point records can be created using that value as their
    ``parentglobalid``.

    For updates and deletes, all payloads are built in a single pass.

    Args:
        package (dict): Traffic Impact Event package produced by the selector.
        edit_type (str): AGOL edit type. Expected values are ``"adds"``,
            ``"updates"``, or ``"deletes"``.
        progress_placeholder (Optional[st.delta_generator.DeltaGenerator]):
            Optional Streamlit placeholder used to render progress below the
            action buttons.

    Returns:
        dict: AGOL edit results keyed by payload section: parent, route, start,
            and end.
    """
    # -------------------------------------------------------------------------
    # Layer Loaders
    # -------------------------------------------------------------------------

    base_url = st.session_state.get("traffic_impact_url")
    lyr_parent = st.session_state.get("traffic_impacts_layer")
    lyr_route = st.session_state.get("traffic_impact_routes_layer")
    lyr_start = st.session_state.get("traffic_impact_start_points_layer")
    lyr_end = st.session_state.get("traffic_impact_end_points_layer")

    loaders = {
        "parent": AGOLDataLoader(base_url, lyr_parent),
        "route": AGOLDataLoader(base_url, lyr_route),
        "start": AGOLDataLoader(base_url, lyr_start),
        "end": AGOLDataLoader(base_url, lyr_end),
    }

    step_names = {
        "parent": "Parent polygon",
        "route": "Impacted route",
        "start": "Start point",
        "end": "End point",
    }

    # -------------------------------------------------------------------------
    # Progress and Payload Adaptation Helpers
    # -------------------------------------------------------------------------

    def _progress_init(initial_frac: float, text: str):
        """Create a Streamlit progress bar in the supplied placeholder.

        Args:
            initial_frac (float): Initial progress value.
            text (str): Initial progress text.

        Returns:
            DeltaGenerator: Streamlit progress object.
        """
        if progress_placeholder is not None:
            return progress_placeholder.progress(initial_frac, text=text)

        return st.progress(initial_frac, text=text)

    def _adapt_for_loader(et: str, section: dict) -> tuple[str, dict]:
        """Return loader mode and adapted payload for ``AGOLDataLoader``.

        Args:
            et (str): Edit type being deployed.
            section (dict): Payload section for one Traffic Impact layer.

        Returns:
            tuple[str, dict]: Loader mode and adapted payload dictionary.

        Raises:
            ValueError: If ``et`` is not supported.
        """
        if et == "adds":
            return "adds", {"adds": section.get("adds", [])}

        if et == "updates":
            upd_items = []

            for rec in section.get("updates", []) or []:
                rec = dict(rec) if isinstance(rec, dict) else {}
                attrs = dict(rec.get("attributes", {}))

                # Normalize objectId -> OBJECTID for loader pre-check.
                if "OBJECTID" not in attrs and "objectId" in attrs:
                    attrs["OBJECTID"] = attrs.pop("objectId")

                rec["attributes"] = attrs
                upd_items.append(rec)

            return "updates", {"updates": upd_items}

        if et == "deletes":
            ids = section.get("deletes") or []

            if not isinstance(ids, (list, tuple)):
                ids = [ids]

            updates = [{"attributes": {"OBJECTID": oid}} for oid in ids if oid not in (None, "")]
            return "deletes", {"updates": updates}

        raise ValueError(f"Unsupported edit_type: {et}")

    def _extract_new_globalid(result: dict) -> Optional[str]:
        """Extract a newly created parent global ID from an AGOL add result.

        Handles common response shapes:
            * ``{"globalids": ["{GUID}", ...], "success": True}``
            * ``{"addResults": [{"success": True, "globalId": "{GUID}"}]}``

        Args:
            result (dict): AGOL add response.

        Returns:
            OptionalNew global ID when available; otherwise, None.
        """
        if not isinstance(result, dict):
            return None

        gids = result.get("globalids") or result.get("globalIds") or result.get("globalIDs")
        if isinstance(gids, list) and gids:
            return gids[0]

        add_results = result.get("addResults")
        if isinstance(add_results, list) and add_results:
            maybe = add_results[0]
            if isinstance(maybe, dict) and maybe.get("success"):
                return maybe.get("globalId") or maybe.get("globalid")

        return None

    results: dict = {}

    # -------------------------------------------------------------------------
    # Adds: Two-Phase Parent then Children
    # -------------------------------------------------------------------------

    if edit_type == "adds":
        total = 4
        progress = _progress_init(0, text="Starting adds...")

        # Phase 1: build and add parent through the payload builder.
        st.session_state["traffic_impact_globalid"] = None
        parent_only = manage_traffic_impact_payloads(package, edit_type="adds", which="parent")
        parent_section = parent_only.get("parent", {})

        progress.progress(0 / total, text=f"{step_names['parent']}: preparing...")
        mode, adapted = _adapt_for_loader("adds", parent_section)
        res_parent = loaders["parent"].add_features(adapted)
        results["parent"] = res_parent
        progress.progress(1 / total, text=f"{step_names['parent']}: {'OK' if res_parent.get('success') else 'FAILED'}")

        if not (res_parent and res_parent.get("success")):
            st.error("Parent add failed; child features were not created.")
            return results

        # Capture new GUID and store it in session state for child payloads.
        new_guid = _extract_new_globalid(res_parent)
        if not new_guid:
            st.error("Parent add did not return a globalid; cannot create child features.")
            return results

        st.session_state["traffic_impact_globalid"] = new_guid

        # Phase 2: build and add children now that parent GUID is known.
        children_only = manage_traffic_impact_payloads(package, edit_type="adds", which="children")
        for i, key in enumerate(("route", "start", "end"), start=2):
            progress.progress((i - 1) / total, text=f"{step_names[key]}: preparing...")
            mode, adapted = _adapt_for_loader("adds", children_only.get(key, {}))
            res = loaders[key].add_features(adapted)
            results[key] = res
            progress.progress(i / total, text=f"{step_names[key]}: {'OK' if res.get('success') else 'FAILED'}")

        # Caller decides whether to clear the progress bar.
        return results

    # -------------------------------------------------------------------------
    # Updates / Deletes: Single-Pass Deployment
    # -------------------------------------------------------------------------

    order = (
        ["parent", "route", "start", "end"]
        if edit_type in ("adds", "updates")
        else ["start", "end", "route", "parent"]
    )
    payloads = manage_traffic_impact_payloads(package, edit_type=edit_type, which="all")
    progress = _progress_init(0, text=f"Starting {edit_type}...")
    total = len(order)

    for i, key in enumerate(order, start=1):
        progress.progress((i - 1) / total, text=f"{step_names[key]}: preparing...")
        mode, adapted = _adapt_for_loader(edit_type, payloads.get(key, {}))

        if mode == "adds":
            res = loaders[key].add_features(adapted)
        elif mode == "updates":
            res = loaders[key].update_features(adapted)
        elif mode == "deletes":
            res = loaders[key].delete_features(adapted)
        else:
            res = {"success": False, "message": f"Unknown mode {mode}", "globalids": []}

        results[key] = res
        progress.progress(i / total, text=f"{step_names[key]}: {'OK' if res.get('success') else 'FAILED'}")

    return results


# =============================================================================
# Event Model Helpers
# =============================================================================

def _new_event(label="New Impact", impact_area_default=None):
    """Create a new local Traffic Impact Event state dictionary.

    Args:
        label (str): Display label for the event selector.
        impact_area_default: Default impact area geometry to assign to the event.

    Returns:
        dict: New local event dictionary with route, extent, and initialization
            values populated.
    """
    eid = st.session_state["tie_next_id"]
    st.session_state["tie_next_id"] += 1

    return {
        "event_id": eid,
        "label": label,
        "selected_impact_area": impact_area_default,
        "selected_route_geom": None,
        "selected_route_id": None,
        "selected_route_name": None,
        "selected_start_point": None,
        "selected_end_point": None,
        "initialized_from_record": False,
    }


def _event_from_record(rec, impact_area_default=None):
    """Create a local event dictionary from an existing AGOL Traffic Impact record.

    Args:
        rec (dict): Normalized Traffic Impact Event record.
        impact_area_default: Default impact area geometry to use when the record
            does not provide an area value.

    Returns:
        dict: Local event dictionary initialized from the existing record.
    """
    ev = _new_event(
        label=(rec.get("name") or f"Traffic Impact @ {rec.get('route_name') or '—'}"),
        impact_area_default=impact_area_default,
    )

    ev["selected_route_geom"] = rec.get("route_geom")
    ev["selected_start_point"] = rec.get("start_point")
    ev["selected_end_point"] = rec.get("end_point")
    ev["selected_route_id"] = rec.get("route_id")
    ev["selected_route_name"] = rec.get("route_name")
    ev["initialized_from_record"] = True

    if rec.get("area"):
        ev["selected_impact_area"] = rec.get("area")

    return ev


# =============================================================================
# Main Streamlit Page Rendering
# =============================================================================

def manage_traffic_impacts():
    """Render and manage Traffic Impact Events.

    This function uses a segmented-control selector instead of tabs. Each
    selection renders the same route/points selector in a single container,
    preventing the Folium map viewport from resetting when switching selections.
    Buttons for LOAD/CLEAR for new events and UPDATE/DELETE for existing events
    remain below the map.

    Returns:
        None: Streamlit components are rendered directly to the page.
    """
    # -------------------------------------------------------------------------
    # Reset Per-Project State When GUID Changes
    # -------------------------------------------------------------------------

    curr_guid = st.session_state.get("guid")
    prev_guid = st.session_state.get("_ti_guid")

    if curr_guid is not None and prev_guid != curr_guid:
        st.session_state.pop("tie_events", None)
        st.session_state.pop("tie_next_id", None)
        st.session_state.pop("traffic_impacts_list", None)
        st.session_state.pop("existing_tie_records", None)
        st.session_state["_tie_loaded_for_apex_guid"] = None
        st.session_state["_ti_guid"] = curr_guid

    # -------------------------------------------------------------------------
    # Module Persistent State
    # -------------------------------------------------------------------------

    st.session_state.setdefault("tie_events", [])
    st.session_state.setdefault("tie_next_id", 1)
    st.session_state.setdefault("traffic_impacts_list", [])

    # -------------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------------

    title_text = "##### MANAGE TRAFFIC IMPACT WORK EXTENTS"
    title_col, btn_col = st.columns([6, 2], vertical_alignment="center")

    with title_col:
        st.markdown(f"{title_text}\n")

    with btn_col:
        if st.button("✚ **ADD IMPACT**", key="btn_add_event_header", use_container_width=True):
            st.session_state["tie_events"].append(
                _new_event(impact_area_default=st.session_state.get("impact_area"))
            )
            st.rerun()

    st.caption(
        "Manage routes affected by this project’s traffic impacts and define their work extents. "
        "Add new traffic impacts by selecting an impacted route and setting the extent with a start and end point. "
        "Update existing traffic impacts work extent based on current traffic impact information, or remove routes that are no longer affected."
    )

    # -------------------------------------------------------------------------
    # Impact Buffer and Event Loading Progress
    # -------------------------------------------------------------------------

    # Ephemeral progress bar, full-width just above the events area.
    progress_holder = st.empty()
    progress = progress_holder.progress(0, text="Initializing…")

    def _step(pct: int, label: str):
        """Update the page-level progress bar defensively.

        Args:
            pct (int): Progress percentage.
            label (str): Progress text.

        Returns:
            None.
        """
        try:
            progress.progress(max(0, min(100, pct)), text=label)
        except Exception:
            try:
                progress.progress(max(0, min(100, pct)))
            except Exception:
                pass

    try:
        _step(5, "Preparing project context…")
        proj_area = st.session_state.get("apex_proj_area")
        buffer_params = ("polygon", 2000)

        _step(12, "Computing project impact buffer…")
        impact_sig = _fingerprint([proj_area, buffer_params])

        if st.session_state.get("_impact_area_sig") != impact_sig:
            impact_area = create_buffers(proj_area, buffer_params[0], buffer_params[1])

            if not impact_area:
                # Fail early with a clear message; progress bar is cleared in finally.
                raise RuntimeError("Buffering the current APEX project area produced no output.")

            st.session_state["impact_area"] = impact_area
            st.session_state["_impact_area_sig"] = impact_sig

            # Propagate refreshed area into already-initialized events.
            for ev in st.session_state.get("tie_events", []):
                ev["selected_impact_area"] = impact_area

        impact_area = st.session_state.get("impact_area")

        # ---------------------------------------------------------------------
        # Pull and Stage Existing Traffic Impact Events
        # ---------------------------------------------------------------------

        _step(22, "Loading traffic impact events…")

        def _fetch_step(p: int, msg: str):
            """Map fetch progress into the page-level progress range.

            Args:
                p (int): Fetch progress percentage.
                msg (str): Fetch progress message.

            Returns:
                None.
            """
            # Clamp into [22..85] for visual continuity with the outer steps.
            p = max(22, min(85, p))
            _step(p, msg)

        pulled_records = fetch_traffic_impacts(force=False, progress_cb=_fetch_step)
        existing_records = bool(pulled_records)

        # ---------------------------------------------------------------------
        # Initialize Event List from Existing Records
        # ---------------------------------------------------------------------

        _step(88, "Staging page UI…")

        if pulled_records and not st.session_state["tie_events"]:
            st.session_state["tie_events"] = [
                _event_from_record(r, impact_area_default=impact_area) for r in pulled_records
            ]

        events = st.session_state["tie_events"]

        # ---------------------------------------------------------------------
        # Local UI Helpers
        # ---------------------------------------------------------------------

        def _resolve_package_for_event(ev) -> dict:
            """Resolve the Traffic Impact package associated with an event.

            Args:
                ev (dict): Local event state dictionary.

            Returns:
                dict: Existing package from ``traffic_impacts_list`` when a match
                    is found; otherwise, a new package built from event state.
            """
            impacts = st.session_state.get("traffic_impacts_list", []) or []
            rid = ev.get("selected_route_id")
            sp = ev.get("selected_start_point")
            ep = ev.get("selected_end_point")

            for p in impacts:
                if p.get("route_id") == rid and p.get("start_point") == sp and p.get("end_point") == ep:
                    p.setdefault("name", ev.get("label"))
                    return p

            return {
                "route_id": ev.get("selected_route_id"),
                "route_name": ev.get("selected_route_name"),
                "route_geom": ev.get("selected_route_geom"),
                "start_point": ev.get("selected_start_point"),
                "end_point": ev.get("selected_end_point"),
                "area": st.session_state.get("impact_area"),
            }

        def _clear_tab_selection(key_prefix: str):
            """Clear selector and map state for a Traffic Impact Event panel.

            Args:
                key_prefix (str): Event-specific key prefix used by selector widgets.

            Returns:
                None: Session state keys are cleared or reset in place.
            """
            ti_key = f"{key_prefix}traffic_impact"
            sel_id_key = f"{key_prefix}selected_route_id"
            sel_name_key = f"{key_prefix}selected_route_name"
            sel_geom_key = f"{key_prefix}selected_route_geom"
            seg_key = f"{key_prefix}place_mode_v2"
            fit_geom_key = f"{key_prefix}fit_bounds_geom"
            map_key = f"{key_prefix}route_map"

            # 1) Clear the working TI dict with route and point values.
            st.session_state.setdefault(ti_key, {})
            st.session_state[ti_key].update(
                {"route_id": None, "route_name": None, "route_geom": None, "start_point": None, "end_point": None}
            )

            # 2) Drop selector caches that re-seed the UI.
            st.session_state.pop(sel_id_key, None)
            st.session_state.pop(sel_name_key, None)
            st.session_state.pop(sel_geom_key, None)
            st.session_state.pop(f"{key_prefix}selected_start_point", None)
            st.session_state.pop(f"{key_prefix}selected_end_point", None)

            # 3) Reset segmented control back to the first step.
            st.session_state.pop(seg_key, None)

            # 4) Remove sticky fit-bounds geometry so viewport recalculates.
            st.session_state.pop(fit_geom_key, None)

            # 5) One-shot flag so the selector skips seeding from any passed-in package.
            st.session_state[f"{key_prefix}__ti_just_cleared"] = True

            # 6) Reset map interaction state.
            try:
                st.session_state.setdefault(map_key, {})
                st.session_state[map_key]["last_clicked"] = None
            except Exception:
                pass

        def _render_event_panel(ev, impact_area_current):
            """Render the selector and action buttons for one Traffic Impact Event.

            Args:
                ev (dict): Local event state dictionary.
                impact_area_current: Current project impact area geometry.

            Returns:
                None: Streamlit components are rendered directly to the page.
            """
            if ev["selected_impact_area"] is None:
                ev["selected_impact_area"] = impact_area_current

            key_prefix = f"ev{ev['event_id']}_"
            package_in = _resolve_package_for_event(ev)

            is_existing = bool(ev.get("initialized_from_record"))

            if not is_existing:
                impacts = st.session_state.get("traffic_impacts_list") or []
                is_existence_alias = any(package_in is p for p in impacts)
                if is_existence_alias:
                    is_existing = True

            # For existing events, buffer the area by 2000 m for display/query,
            # but do NOT mutate the stored area in package_in.
            package_for_selector = package_in

            if is_existing:
                try:
                    pkg_area = (package_in or {}).get("area")
                    if pkg_area:
                        buffered_area = create_buffers(pkg_area, distance_meters=2000)
                        if buffered_area:
                            # Use a shallow copy so the incoming dictionary is unchanged.
                            package_for_selector = dict(package_in)
                            package_for_selector["area"] = buffered_area
                except Exception:
                    # On any failure, fall back to the unbuffered package.
                    package_for_selector = package_in

            # Render only the selector in its dedicated container.
            selector_container = st.container(border=False)

            with selector_container:
                try:
                    package_out = select_route_and_points(
                        selector_container,
                        key_prefix=key_prefix,
                        is_existing=is_existing,
                        package=package_for_selector,
                    )
                except TypeError:
                    package_out = select_route_and_points(selector_container, key_prefix=key_prefix)

            # Persist returned selector data to event state.
            src = package_out if isinstance(package_out, dict) else {}

            if "route_geom" in src:
                ev["selected_route_geom"] = src.get("route_geom")
            if "start_point" in src:
                ev["selected_start_point"] = src.get("start_point")
            if "end_point" in src:
                ev["selected_end_point"] = src.get("end_point")
            if "route_id" in src:
                ev["selected_route_id"] = src.get("route_id")
            if "route_name" in src:
                ev["selected_route_name"] = src.get("route_name")

            # Buttons row directly below the selector container.
            btn_col1, btn_col2 = st.columns([1, 1])

            if is_existing:
                # If this existing event's label is "New Impact", show LOAD
                # while preserving the update behavior on click.
                primary_label = "LOAD" if (ev.get("label") == "New Impact") else "UPDATE"

                with btn_col1:
                    update_clicked = st.button(
                        primary_label,
                        use_container_width=True,
                        type="primary",
                        key=f"{key_prefix}btn_update",
                    )

                with btn_col2:
                    delete_clicked = st.button(
                        "DELETE",
                        use_container_width=True,
                        key=f"{key_prefix}btn_delete",
                    )

                # Single full-width progress slot below both buttons.
                progress_row_placeholder = st.empty()

                if update_clicked:
                    if not package_out:
                        st.warning("Set a route and both Start/End points before updating.")
                    else:
                        _ = _deploy_to_agol(
                            package_out,
                            edit_type="updates",
                            progress_placeholder=progress_row_placeholder,
                        )

                        # Force-refresh from AGOL so label picks up updated Event_Name.
                        impact_area_local = st.session_state.get("impact_area")
                        pulled = fetch_traffic_impacts(force=True)
                        st.session_state["tie_events"] = []
                        st.session_state["tie_next_id"] = 1

                        for rec in (pulled or []):
                            st.session_state["tie_events"].append(
                                _event_from_record(rec, impact_area_default=impact_area_local)
                            )

                        st.rerun()

                if delete_clicked:
                    if not package_out:
                        st.warning("A valid package from the selector is required to delete.")
                    else:
                        try:
                            delete_result = _deploy_to_agol(
                                package_out,
                                edit_type="deletes",
                                progress_placeholder=progress_row_placeholder,
                            )
                        except Exception as ex:
                            st.error(f"Failed to delete on AGOL: {ex}")
                            st.stop()

                        ok_sections = [k for k, v in (delete_result or {}).items() if v.get("success")]
                        fail_sections = [k for k, v in (delete_result or {}).items() if not v.get("success")]

                        if fail_sections:
                            st.warning(f"AGOL delete completed with partial failures: {', '.join(fail_sections)}")

                        # Remove locally, then rebuild from AGOL.
                        try:
                            st.session_state["tie_events"].remove(ev)
                        except ValueError:
                            pass

                        impact_area_local = st.session_state.get("impact_area")
                        pulled = fetch_traffic_impacts(force=True)
                        st.session_state["tie_events"] = []
                        st.session_state["tie_next_id"] = 1

                        for rec in (pulled or []):
                            st.session_state["tie_events"].append(
                                _event_from_record(rec, impact_area_default=impact_area_local)
                            )

                        st.rerun()

            else:
                with btn_col1:
                    load_clicked = st.button(
                        "LOAD",
                        use_container_width=True,
                        type="primary",
                        key=f"{key_prefix}btn_load",
                    )

                with btn_col2:
                    clear_clicked = st.button(
                        "CLEAR",
                        use_container_width=True,
                        key=f"{key_prefix}btn_clear",
                    )

                # Single full-width progress slot below both buttons.
                progress_row_placeholder = st.empty()

                if load_clicked:
                    if not package_out:
                        st.warning("Select a route and set both Start and End points before loading.")
                    else:
                        try:
                            add_result = _deploy_to_agol(
                                package_out,
                                edit_type="adds",
                                progress_placeholder=progress_row_placeholder,
                            )
                        except Exception as ex:
                            st.error(f"Failed to add on AGOL: {ex}")
                            st.stop()

                        ok_sections = [k for k, v in (add_result or {}).items() if v.get("success")]
                        fail_sections = [k for k, v in (add_result or {}).items() if not v.get("success")]

                        if fail_sections:
                            st.warning(f"AGOL add completed with partial failures: {', '.join(fail_sections)}")

                        impact_area_local = st.session_state.get("impact_area")
                        pulled = fetch_traffic_impacts(force=True)
                        st.session_state["tie_events"] = []
                        st.session_state["tie_next_id"] = 1

                        for rec in (pulled or []):
                            st.session_state["tie_events"].append(
                                _event_from_record(rec, impact_area_default=impact_area_local)
                            )

                        st.rerun()

                if clear_clicked:
                    _clear_tab_selection(key_prefix)
                    ev["label"] = "New Impact"
                    ev["selected_impact_area"] = st.session_state.get("impact_area")
                    st.rerun()

        # ---------------------------------------------------------------------
        # Segmented Control
        # ---------------------------------------------------------------------

        _step(94, "Preparing selector…")

        if events:
            # Build unique option values and display labels separately so
            # duplicate labels can still be selected independently.
            labels = [
                (ev["label"] if (isinstance(ev.get("label"), str) and ev["label"].strip()) else f"Event {i + 1}")
                for i, ev in enumerate(events)
            ]
            values = [f"event-{i}" for i in range(len(events))]

            # Keep a stable, index-based selector so switching does not
            # reconstruct every map.
            default_value = st.session_state.get("ti_event_selector")

            if default_value not in values:
                default_value = values[0]
                st.session_state["ti_event_selector"] = default_value

            selection = st.segmented_control(
                "Traffic Impact Events",
                options=values,
                format_func=lambda value: next((label for idx, label in enumerate(labels) if values[idx] == value), value),
                default=default_value,
                key="ti_event_selector",
                width="stretch",
                label_visibility="hidden",
            )

            with st.container(border=True):
                # Render only the selected event in one container.
                idx = values.index(selection)
                _render_event_panel(events[idx], impact_area)

        else:
            if existing_records:
                st.info("Existing Traffic Impact Events were found, but could not initialize tabs.")
            else:
                st.info("No existing Traffic Impact Events were found for this project.")

        _step(100, "Done")

    finally:
        # Always clear the progress bar so it disappears when complete or on error.
        try:
            progress_holder.empty()
        except Exception:
            pass