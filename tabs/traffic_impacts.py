import streamlit as st
import json
import hashlib
from typing import Optional, Callable
from util.geometry_util import (
    select_route_and_points,  # injected selector; returns updated package (or None)
    geometry_to_folium,
)
from util.geospatial_util import create_buffers
from agol.agol_util import select_record
# NEW imports for deployment helper
from agol.agol_util import AGOLDataLoader  # AGOL applyEdits wrapper (add/update/delete)
from agol.agol_payloads import manage_traffic_impact_payloads  # builds the 4 payloads from the package


# ------------------------------------------------------------
# Small utility: stable fingerprint for change detection
# ------------------------------------------------------------
def _fingerprint(obj) -> str:
    try:
        return hashlib.md5(
            json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    except Exception:
        return f"{type(obj).__name__}:{str(obj)[:200]}"


# ------------------------------------------------------------
# Helper: fetch existing Traffic Impact Events for a project
# - Now supports an optional progress callback for granular steps.
# ------------------------------------------------------------
def fetch_traffic_impacts(
    force: bool = False,
    progress_cb: Optional[Callable[[int, str], None]] = None
):
    """
    Pull all Impacted Routes for the current APEX project (by apex_guid),
    and populate:
      - st.session_state["existing_tie_records"] : list[dict] (normalized records)
      - st.session_state["traffic_impacts_list"] : list[dict] (same shape used across UI)

    Returns
    -------
    list[dict]
    """
    apex_guid = st.session_state.get("apex_guid")
    ti_url = st.session_state.get("traffic_impact_url")
    lyr_parent = st.session_state.get("traffic_impacts_layer")
    lyr_routes = st.session_state.get("traffic_impact_routes_layer")
    lyr_start = st.session_state.get("traffic_impact_start_points_layer")
    lyr_end = st.session_state.get("traffic_impact_end_points_layer")

    # If not fully wired, clear and bail.
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

    # Reuse previously pulled data unless force=True or apex_guid changed
    loaded_for = st.session_state.get("_tie_loaded_for_apex_guid")
    if not force and loaded_for == apex_guid:
        if progress_cb:
            progress_cb(100, "Traffic impacts already loaded")
        return st.session_state.get("existing_tie_records", []) or []

    # --- helpers for geometry normalization ---
    def _as_lonlat_list(lines_geom):
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
        if not pt_geom:
            return None
        x = pt_geom.get("x")
        y = pt_geom.get("y")
        if x is None or y is None:
            return None
        return {"lonlat": [x, y], "lat": y, "lng": x}

    # ---------------- Query parent features ----------------
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

    # ---------------- Query children per parent ----------------
    total = max(1, len(parent_features))
    records = []
    for idx, feat in enumerate(parent_features, start=1):
        if progress_cb:
            pct = 20 + int(60 * (idx - 1) / total)
            progress_cb(pct, f"Loading event {idx}/{total}… (parent)")
        attrs = feat.get("attributes") or {}
        geom = feat.get('geometry') or {}
        ti_guid = attrs.get("globalid")
        ti_objectid = attrs.get("objectid")

        # NAME NORMALIZATION:
        # If an existing Traffic Impact Event has Event_Name == "Blank Event",
        # rewrite it to "New Impact" before passing forward.
        raw_name = attrs.get("Event_Name")
        ti_event_name = (
            "New Impact" if isinstance(raw_name, str) and raw_name.strip() == "Blank Event"
            else raw_name
        )

        ti_route_id = attrs.get("Route_ID")
        ti_route_name = attrs.get("Route_Name")
        ti_impact_area = geom.get("rings")  # polygon rings (lon,lat)

        # Route
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

        # Start
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

        # End
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

        rec = {
            "name": ti_event_name,
            "area": ti_impact_area,  # <-- EXISTING AREA (polygon rings)
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

    # ---------------- Stage into session ----------------
    if progress_cb:
        progress_cb(85, "Staging events…")
    st.session_state["existing_tie_records"] = records

    # Keep IDs in the working list so they flow into the selector (existing events)
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


# ---------------------------------------------------------------------
# Generic helper to deploy to AGOL for add/update/delete
# ---------------------------------------------------------------------
def _deploy_to_agol(
    package: dict,
    edit_type: str,
    *,
    progress_placeholder: Optional[st.delta_generator.DeltaGenerator] = None
) -> dict:
    """
    Build the four layer payloads from `package` and submit them to AGOL with progress.
    - Supports edit_type in {'adds','updates','deletes'}.
    - For **adds**: parent is created first (via traffic_impact_payloads with which='parent');
      capture its returned globalid and save it in st.session_state['traffic_impact_globalid'],
      then create child layers (route, start, end) using that value as `parentglobalid`.
    - For **updates/deletes**: single pass using traffic_impact_payloads with which='all'.
    - Returns a dict of results per layer.

    progress_placeholder:
      Optional placeholder created higher in the UI to control where the progress bar appears.
      If provided, the progress bar will be rendered there (full row under the buttons).
    """
    # --- loaders (one per layer) ---
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

    # -- progress factory tied to supplied placeholder (if any)
    def _progress_init(initial_frac: float, text: str):
        if progress_placeholder is not None:
            return progress_placeholder.progress(initial_frac, text=text)
        return st.progress(initial_frac, text=text)

    def _adapt_for_loader(et: str, section: dict) -> tuple[str, dict]:
        """Return (mode, adapted_payload) for AGOLDataLoader."""
        if et == "adds":
            return "adds", {"adds": section.get("adds", [])}
        if et == "updates":
            upd_items = []
            for rec in section.get("updates", []) or []:
                rec = dict(rec) if isinstance(rec, dict) else {}
                attrs = dict(rec.get("attributes", {}))
                # Normalize objectId -> OBJECTID for loader pre-check
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
        """
        Try several common shapes:
          - {"globalids": ["{GUID}", ...], "success": True}
          - {"addResults":[{"success":True, "globalId":"{GUID}"}]}
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

    # ---------- ADDS: two-phase (parent -> children) using payload builder ----------
    if edit_type == "adds":
        total = 4
        progress = _progress_init(0, text="Starting adds...")

        # Phase 1: build & add PARENT via payload builder
        st.session_state["traffic_impact_globalid"] = None  # clear stale
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

        # Capture new GUID and store in session for child payloads
        new_guid = _extract_new_globalid(res_parent)
        if not new_guid:
            st.error("Parent add did not return a globalid; cannot create child features.")
            return results
        st.session_state["traffic_impact_globalid"] = new_guid

        # Phase 2: build & add CHILDREN now that GUID is known
        children_only = manage_traffic_impact_payloads(package, edit_type="adds", which="children")
        for i, key in enumerate(("route", "start", "end"), start=2):
            progress.progress((i - 1) / total, text=f"{step_names[key]}: preparing...")
            mode, adapted = _adapt_for_loader("adds", children_only.get(key, {}))
            res = loaders[key].add_features(adapted)
            results[key] = res
            progress.progress(i / total, text=f"{step_names[key]}: {'OK' if res.get('success') else 'FAILED'}")

        # caller decides whether to clear the bar
        return results

    # ---------- UPDATES / DELETES (single-pass) ----------
    order = (["parent", "route", "start", "end"] if edit_type in ("adds", "updates")
             else ["start", "end", "route", "parent"])
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


# ---------------------------------------------------------------------
# MOVED UP to avoid "free variable ... before assignment" errors.
# These helpers were previously nested inside manage_traffic_impacts().
# Keeping behavior identical; only their location changed.
# ---------------------------------------------------------------------
def _new_event(label="New Impact", impact_area_default=None):
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


def manage_traffic_impacts():
    """
    Manage Traffic Impact Events with a segmented-control selector (no tabs).
    Each selection renders the same route/points selector in a single container,
    preventing Folium map viewport from resetting when switching selections.
    Buttons for LOAD/CLEAR (new) or UPDATE/DELETE (existing) remain below the map.
    """
    # ------------------------------------------------------------
    # Reset per-project state when GUID changes (guarded)
    # ------------------------------------------------------------
    curr_guid = st.session_state.get("guid")
    prev_guid = st.session_state.get("_ti_guid")
    if curr_guid is not None and prev_guid != curr_guid:
        st.session_state.pop("tie_events", None)
        st.session_state.pop("tie_next_id", None)
        st.session_state.pop("traffic_impacts_list", None)
        st.session_state.pop("existing_tie_records", None)
        st.session_state["_tie_loaded_for_apex_guid"] = None
        st.session_state["_ti_guid"] = curr_guid

    # ------------------------------------------------------------
    # Module persistent state
    # ------------------------------------------------------------
    st.session_state.setdefault("tie_events", [])
    st.session_state.setdefault("tie_next_id", 1)
    st.session_state.setdefault("traffic_impacts_list", [])

    # ------------------------------------------------------------
    # Header (title + Add button in the same row)
    # ------------------------------------------------------------
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


    # ------------------------------------------------------------
    # Impact buffer (cached per project geometry + params)
    # → now wrapped in the page-level progress sequence
    # ------------------------------------------------------------

    # Ephemeral progress bar (full-width just above the events area)
    progress_holder = st.empty()
    progress = progress_holder.progress(0, text="Initializing…")

    def _step(pct: int, label: str):
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
                # Fail early with a clear message; progress bar will still be cleared in finally
                raise RuntimeError("Buffering the current APEX project area produced no output.")
            st.session_state["impact_area"] = impact_area
            st.session_state["_impact_area_sig"] = impact_sig
            # propagate refreshed area into any already-initialized events
            for ev in st.session_state.get("tie_events", []):
                ev["selected_impact_area"] = impact_area

        impact_area = st.session_state.get("impact_area")

        # ------------------------------------------------------------
        # Pull and stage existing Traffic Impact Events (lazy)
        # Uses sub-step reporting from fetch_traffic_impacts
        # ------------------------------------------------------------
        _step(22, "Loading traffic impact events…")

        def _fetch_step(p: int, msg: str):
            # Clamp into [22..85] for visual continuity with the outer steps.
            p = max(22, min(85, p))
            _step(p, msg)

        pulled_records = fetch_traffic_impacts(force=False, progress_cb=_fetch_step)
        existing_records = bool(pulled_records)

        # ------------------------------------------------------------
        # Initialize event list from existing records (first-time only)
        # ------------------------------------------------------------
        _step(88, "Staging page UI…")
        if pulled_records and not st.session_state["tie_events"]:
            st.session_state["tie_events"] = [
                _event_from_record(r, impact_area_default=impact_area) for r in pulled_records
            ]

        events = st.session_state["tie_events"]

        # ------------------------------------------------------------
        # Helpers — kept here to preserve structure and behavior
        # ------------------------------------------------------------
        def _resolve_package_for_event(ev) -> dict:
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
            ti_key = f"{key_prefix}traffic_impact"
            sel_id_key = f"{key_prefix}selected_route_id"
            sel_name_key = f"{key_prefix}selected_route_name"
            sel_geom_key = f"{key_prefix}selected_route_geom"
            seg_key = f"{key_prefix}place_mode_v2"
            fit_geom_key = f"{key_prefix}fit_bounds_geom"
            map_key = f"{key_prefix}route_map"

            # 1) Clear the working TI dict (route + points)
            st.session_state.setdefault(ti_key, {})
            st.session_state[ti_key].update(
                {"route_id": None, "route_name": None, "route_geom": None, "start_point": None, "end_point": None}
            )

            # 2) Drop selector caches that re-seed the UI
            st.session_state.pop(sel_id_key, None)
            st.session_state.pop(sel_name_key, None)
            st.session_state.pop(sel_geom_key, None)
            st.session_state.pop(f"{key_prefix}selected_start_point", None)
            st.session_state.pop(f"{key_prefix}selected_end_point", None)

            # 3) Reset segmented control back to the first step
            st.session_state.pop(seg_key, None)

            # 4) Remove any sticky fit-bounds geom so viewport recalculates from project/area
            st.session_state.pop(fit_geom_key, None)

            # 5) One-shot flag so the selector knows to SKIP seeding from any passed-in package
            st.session_state[f"{key_prefix}__ti_just_cleared"] = True

            # 6) Map interaction reset
            try:
                st.session_state.setdefault(map_key, {})
                st.session_state[map_key]["last_clicked"] = None
            except Exception:
                pass

        def _render_event_panel(ev, impact_area_current):
            if ev["selected_impact_area"] is None:
                ev["selected_impact_area"] = impact_area_current

            key_prefix = f"ev{ev['event_id']}_"
            package_in = _resolve_package_for_event(ev)

            is_existing = bool(ev.get("initialized_from_record"))
            if not is_existing:
                impacts = st.session_state.get("traffic_impacts_list") or []
                is_existence_alias = any(package_in is p for p in impacts)  # back-compat
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
                            # shallow copy so we don't change the incoming dict
                            package_for_selector = dict(package_in)
                            package_for_selector["area"] = buffered_area
                except Exception:
                    # On any failure, fall back to the unbuffered package
                    package_for_selector = package_in

            # --- Second container: ONLY wrap the selector function ---
            selector_container = st.container(border=False)
            with selector_container:
                try:
                    package_out = select_route_and_points(
                        selector_container,
                        key_prefix=key_prefix,
                        is_existing=is_existing,
                        package=package_for_selector,  # <-- use buffered area when existing
                    )
                except TypeError:
                    package_out = select_route_and_points(selector_container, key_prefix=key_prefix)

            # Persist returned data to event state
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

            # --- Buttons row (directly below the selector container) ---
            btn_col1, btn_col2 = st.columns([1, 1])

            if is_existing:
                # Minimal change for your request:
                # If this existing event's label is "New Impact" (normalized from a blank name),
                # show the primary button text as "LOAD" but still perform UPDATE on click.
                primary_label = "LOAD" if (ev.get("label") == "New Impact") else "UPDATE"

                with btn_col1:
                    update_clicked = st.button(
                        primary_label,  # <-- label swap only
                        use_container_width=True,
                        type="primary",
                        key=f"{key_prefix}btn_update"
                    )
                with btn_col2:
                    delete_clicked = st.button(
                        "DELETE",
                        use_container_width=True,
                        key=f"{key_prefix}btn_delete"
                    )

                # Single full-width progress slot BELOW both buttons
                progress_row_placeholder = st.empty()

                if update_clicked:
                    if not package_out:
                        st.warning("Set a route and both Start/End points before updating.")
                    else:
                        _ = _deploy_to_agol(
                            package_out,
                            edit_type="updates",
                            progress_placeholder=progress_row_placeholder,  # shared slot under both buttons
                        )
                        # Force-refresh from AGOL so label picks up updated Event_Name
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
                                progress_placeholder=progress_row_placeholder,  # shared slot under both buttons
                            )
                        except Exception as ex:
                            st.error(f"Failed to delete on AGOL: {ex}")
                            st.stop()
                        ok_sections = [k for k, v in (delete_result or {}).items() if v.get("success")]
                        fail_sections = [k for k, v in (delete_result or {}).items() if not v.get("success")]
                        if fail_sections:
                            st.warning(f"AGOL delete completed with partial failures: {', '.join(fail_sections)}")

                        # Remove locally then rebuild from AGOL
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
                        key=f"{key_prefix}btn_load"
                    )
                with btn_col2:
                    clear_clicked = st.button(
                        "CLEAR",
                        use_container_width=True,
                        key=f"{key_prefix}btn_clear"
                    )

                # Single full-width progress slot BELOW both buttons
                progress_row_placeholder = st.empty()

                if load_clicked:
                    if not package_out:
                        st.warning("Select a route and set both Start and End points before loading.")
                    else:
                        try:
                            add_result = _deploy_to_agol(
                                package_out,
                                edit_type="adds",
                                progress_placeholder=progress_row_placeholder,  # shared slot under both buttons
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

        # ------------------------------------------------------------
        # Segmented control in place of tabs
        # ------------------------------------------------------------
        _step(94, "Preparing selector…")
        if events:
            # Build labels
            labels = [
                (ev["label"] if (isinstance(ev.get("label"), str) and ev["label"].strip()) else f"Event {i+1}")
                for i, ev in enumerate(events)
            ]

            # Keep a stable, index-based selector so switching does not reconstruct all maps
            st.session_state.setdefault("ti_event_selector", labels[0])

            # If label list changed (e.g., after update/delete/add), clamp value
            if st.session_state["ti_event_selector"] not in labels:
                st.session_state["ti_event_selector"] = labels[0]

            selection = st.segmented_control(
                "Traffic Impact Events",
                options=labels,
                key="ti_event_selector",
                width="stretch",
                label_visibility="hidden"
            )

            with st.container(border=True):
                # Render only the selected event in one container
                idx = labels.index(selection)
                _render_event_panel(events[idx], impact_area)
        else:
            if existing_records:
                st.info("Existing Traffic Impact Events were found, but could not initialize tabs.")
            else:
                st.info("No existing Traffic Impact Events were found for this project.")

        _step(100, "Done")
    finally:
        # Always clear the progress bar so it disappears when we're done (or if errors occurred)
        try:
            progress_holder.empty()
        except Exception:
            pass