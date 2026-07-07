# ===============================================================================
# DISTRICT QUERIES (STREAMLIT) — GEOGRAPHY INTERSECTS (HOUSE / SENATE / BOROUGH / REGION)
# ===============================================================================
# Updated:
# - Adaptive route chunking (polyline point chunks)
# - Adaptive polygon chunking (slice polygon into valid sub-polygons and query)
# - NEW: Optional selective sections execution when calling run_district_queries(sections=[...])
# - Messaging fix: removed info banners; use only proper spinners (context manager)
# - NEW (this commit): Per-category granular progress updates with callbacks
# ===============================================================================

import streamlit as st
import re

from agol.agol_util import AGOLQueryIntersect
from util.geospatial_util import simplify_geometry


# =============================================================================
# HELPERS: COMMON
# =============================================================================

def _is_point_pair(x) -> bool:
    """True if x looks like [lat, lon] numeric pair."""
    if not isinstance(x, (list, tuple)) or len(x) != 2:
        return False
    return isinstance(x[0], (int, float)) and isinstance(x[1], (int, float))


def _unique_preserve_order(items):
    seen = set()
    out = []
    for x in items or []:
        if x is None:
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _split_string_values(s: str):
    """
    AGOLQueryIntersect.string_values is often a single string.
    Split on common delimiters: newline, semicolon, comma.
    """
    if not s:
        return []
    # Normalize HTML line breaks we might get back
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("<br>", "\n")
    parts = re.split(r"[\n;]+", s)
    exploded = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Also split residual comma-separated items
        for q in p.split(","):
            q = q.strip()
            if q:
                exploded.append(q)
    return exploded


def _call_intersect(url, layer, geometry, fields, return_geometry, list_values, string_values):
    r = AGOLQueryIntersect(
        url=url,
        layer=layer,
        geometry=geometry,
        fields=fields,
        return_geometry=return_geometry,
        list_values=list_values,
        string_values=string_values,
    )
    return (r.list_values or [], r.string_values or "", r.results)


# =============================================================================
# ROUTE (POLYLINE) CHUNKING
# =============================================================================

def _extract_route_paths(geom):
    """
    Normalize route geometry into list of paths (each path=list of [lat,lon]).

    Supports:
    A) [[lat, lon], ...]
    B) [[[lat, lon], ...]]
    C) [[[[lat, lon], ...]], ...] (flatten)
    """
    if not isinstance(geom, list) or not geom:
        return [], False

    # A: single path directly
    if _is_point_pair(geom[0]):
        return [geom], True

    # B: list of paths
    if isinstance(geom[0], list) and geom[0] and _is_point_pair(geom[0][0]):
        return geom, True

    # C: extra nesting, flatten
    if isinstance(geom[0], list) and geom[0]:
        paths = []
        for maybe_route in geom:
            if not isinstance(maybe_route, list) or not maybe_route:
                continue
            if _is_point_pair(maybe_route[0]):
                paths.append(maybe_route)
            elif (
                isinstance(maybe_route[0], list)
                and maybe_route[0]
                and _is_point_pair(maybe_route[0][0])
            ):
                paths.extend(maybe_route)
        if paths:
            return paths, True

    return [], False


def _chunk_points(points, max_points: int, overlap: int = 1):
    """Split one path into segments."""
    if not points:
        return []
    if max_points < 2:
        max_points = 2
    if len(points) <= max_points:
        return [points]

    segs = []
    start = 0
    n = len(points)

    while start < n:
        end = min(start + max_points, n)
        seg = points[start:end]
        if len(seg) == 1 and start > 0:
            seg = points[start - 1:end]
        segs.append(seg)

        if end >= n:
            break
        start = end - overlap

    return segs


def _chunk_route_geometry(route_geom, max_points: int):
    """
    Returns list of route geometries each shaped as list-of-paths.

    Example input: [[[p1..pN]]]
    Output: [ [[seg1]], [[seg2]], ... ]
    """
    paths, is_route = _extract_route_paths(route_geom)
    if not is_route:
        return [route_geom]

    chunks = []
    for path in paths:
        for seg in _chunk_points(path, max_points=max_points, overlap=1):
            chunks.append([seg])  # keep list-of-paths structure

    return chunks or [route_geom]


# =============================================================================
# POLYGON CHUNKING (VALID SUB-POLYGONS)
# =============================================================================

def _extract_polygon_rings(geom):
    """
    Normalize boundary geometry into list of rings:
      rings = [ ring1, ring2, ... ]
      ring = [ [lat,lon], ... ]

    Returns: (rings, is_polygon_like)
    """
    if not isinstance(geom, list) or not geom:
        return [], False

    # A: ring directly
    if _is_point_pair(geom[0]):
        return [geom], True

    # B/C: list of rings
    if isinstance(geom[0], list) and geom[0] and _is_point_pair(geom[0][0]):
        return geom, True

    return [], False


def _close_ring(ring):
    """Ensure ring is closed (first==last)."""
    if not ring or len(ring) < 3:
        return ring
    if ring[0] != ring[-1]:
        return ring + [ring[0]]
    return ring


def _polygon_to_shapely(boundary_geom):
    """
    Convert boundary rings [[lat,lon]...] into shapely Polygon (lon,lat).
    Preserves holes if present.
    """
    try:
        from shapely.geometry import Polygon
    except Exception as e:
        raise RuntimeError(
            "Polygon chunking requires the 'shapely' package. "
            "Install it (e.g., pip install shapely) in your environment."
        ) from e

    rings, ok = _extract_polygon_rings(boundary_geom)
    if not ok or not rings:
        raise ValueError("Invalid/empty polygon geometry for chunking")

    outer = _close_ring(rings[0])
    holes = [_close_ring(r) for r in rings[1:]] if len(rings) > 1 else []

    # Convert [lat,lon] -> (lon,lat)
    outer_xy = [(pt[1], pt[0]) for pt in outer]
    holes_xy = [[(pt[1], pt[0]) for pt in h] for h in holes if h and len(h) >= 4]

    poly = Polygon(outer_xy, holes_xy)

    # Clean minor self-intersections
    try:
        poly = poly.buffer(0)
    except Exception:
        pass

    return poly


def _shapely_to_boundary_geom(poly):
    """Convert shapely Polygon -> boundary rings format [ [ [lat,lon]... ], [hole...], ... ]."""
    rings = []

    # Exterior
    ext = list(poly.exterior.coords)
    rings.append([[y, x] for (x, y) in ext])

    # Holes
    for interior in poly.interiors:
        coords = list(interior.coords)
        rings.append([[y, x] for (x, y) in coords])

    return rings


def _slice_polygon_into_equal_parts(boundary_geom, parts: int):
    """
    Slice polygon into 'parts' equal strips along its longest axis,
    returning a list of VALID polygon geometries (rings) for querying.
    """
    try:
        from shapely.geometry import box
        from shapely.prepared import prep
    except Exception as e:
        raise RuntimeError(
            "Polygon chunking requires the 'shapely' package. "
            "Install it (e.g., pip install shapely)."
        ) from e

    poly = _polygon_to_shapely(boundary_geom)
    if poly.is_empty:
        return []

    minx, miny, maxx, maxy = poly.bounds
    width = maxx - minx
    height = maxy - miny

    if parts < 2:
        parts = 2

    prepared = prep(poly)
    pieces = []

    # Choose slicing direction based on longest dimension
    if width >= height:
        # vertical strips
        dx = width / parts if width else 0.0
        for i in range(parts):
            x0 = minx + i * dx
            x1 = minx + (i + 1) * dx if i < parts - 1 else maxx
            strip = box(x0, miny, x1, maxy)
            if not prepared.intersects(strip):
                continue
            piece = poly.intersection(strip)
            if piece.is_empty:
                continue
            if piece.geom_type == "Polygon":
                pieces.append(_shapely_to_boundary_geom(piece))
            elif piece.geom_type == "MultiPolygon":
                for p in piece.geoms:
                    if not p.is_empty:
                        pieces.append(_shapely_to_boundary_geom(p))
    else:
        # horizontal strips
        dy = height / parts if height else 0.0
        for i in range(parts):
            y0 = miny + i * dy
            y1 = miny + (i + 1) * dy if i < parts - 1 else maxy
            strip = box(minx, y0, maxx, y1)
            if not prepared.intersects(strip):
                continue
            piece = poly.intersection(strip)
            if piece.is_empty:
                continue
            if piece.geom_type == "Polygon":
                pieces.append(_shapely_to_boundary_geom(piece))
            elif piece.geom_type == "MultiPolygon":
                for p in piece.geoms:
                    if not p.is_empty:
                        pieces.append(_shapely_to_boundary_geom(p))

    return pieces


# =============================================================================
# ADAPTIVE INTERSECT (ROUTES + POLYGONS) — with optional progress callback
# =============================================================================

def _agol_intersect_adaptive(
    url: str,
    layer: int,
    geometry,
    fields: str,
    return_geometry: bool,
    list_values: str,
    string_values: str,
    enable_route_chunking: bool = True,
    enable_polygon_chunking: bool = True,
    progress_cb=None,  # <-- NEW (optional)
):
    """
    Robust intersect wrapper:
    1) Try a single full-geometry query.
    2) If it fails:
       - If route chunking enabled and geometry route-like: chunk by points
       - Else if polygon chunking enabled and geometry polygon-like: slice into valid pieces
    3) Merge results as uniques.

    Optional progress_cb(msg: str, frac: float|None):
      - msg: human-friendly status message
      - frac: 0..1 progress within the *query phase* for a single section; can be None for message-only updates
    """

    def _notify(msg: str, frac=None):
        if progress_cb is not None:
            try:
                f = None if frac is None else max(0.0, min(1.0, float(frac)))
                progress_cb(msg, f)
            except Exception:
                # Never let UI progress errors break data work
                pass

    debug = bool(st.session_state.get("agol_debug_chunking", False))

    # Determine geometry kinds (best-effort)
    _, is_route = _extract_route_paths(geometry)
    _, is_poly = _extract_polygon_rings(geometry)

    # 1) Single call first
    _notify("Submitting full-geometry query…", 0.05)
    try:
        ids, labels, result = _call_intersect(
            url, layer, geometry, fields, return_geometry, list_values, string_values
        )
        _notify("Full-geometry query succeeded.", 1.0)
        return {"list_values": ids, "string_values": labels, "result": result}
    except Exception as e:
        if debug:
            st.write(f"[chunking] Full-geometry query failed: {e!r}")
        full_error = e
        _notify("Full-geometry query failed; evaluating chunking path…", None)

    # 2a) Route chunking path
    if enable_route_chunking and is_route:
        max_points = int(st.session_state.get("agol_max_points_per_query", 300))
        min_points = int(st.session_state.get("agol_min_points_per_query", 15))

        current = max_points
        last_error = None
        overall_max_fraction = 0.0  # ensure we never regress on retries

        while current >= min_points:
            try:
                geoms = _chunk_route_geometry(geometry, max_points=current)
                n = max(1, len(geoms))
                _notify(f"Route chunking: {n} chunk(s) @ {current} pts/chunk…", 0.0)

                merged_ids = []
                merged_labels_list = []

                for i, g in enumerate(geoms, start=1):
                    # Per-chunk fetch
                    ids, labels, result = _call_intersect(
                        url, layer, g, fields, return_geometry, list_values, string_values
                    )
                    merged_ids.extend(ids)
                    merged_labels_list.extend(_split_string_values(labels))

                    # Update fraction monotonically across retries
                    frac = i / float(n)
                    if frac > overall_max_fraction:
                        overall_max_fraction = frac
                    _notify(f"Fetched route chunk {i}/{n}…", overall_max_fraction)

                # Merge/unique finalization
                _notify(
                    "Merging and de-duplicating route results…",
                    min(0.98, overall_max_fraction),
                )
                merged_ids = _unique_preserve_order(merged_ids)
                merged_labels_list = _unique_preserve_order(merged_labels_list)

                _notify("Route query complete.", 1.0)
                return {
                    "list_values": merged_ids,
                    "string_values": ", ".join(merged_labels_list),
                    "result": result,
                }

            except Exception as e:
                last_error = e
                if debug:
                    st.write(f"[chunking][route] Failed @ {current} pts/chunk: {e!r}")
                _notify(
                    f"Retrying route chunking with smaller chunk size (≤ {current//2})…",
                    None,
                )
                current = current // 2

        raise RuntimeError(
            f"AGOL intersect failed for route even after chunking down to {min_points} pts/chunk"
        ) from last_error

    # 2b) Polygon chunking path
    if enable_polygon_chunking and is_poly:
        initial = int(st.session_state.get("agol_polygon_initial_slices", 4))
        max_slices = int(st.session_state.get("agol_polygon_max_slices", 64))

        slices = max(2, initial)
        last_error = None
        overall_max_fraction = 0.0

        while slices <= max_slices:
            try:
                pieces = _slice_polygon_into_equal_parts(geometry, parts=slices)
                n = max(1, len(pieces))
                _notify(f"Polygon slicing: {n} piece(s) @ {slices} slices…", 0.0)

                merged_ids = []
                merged_labels_list = []

                for i, piece_geom in enumerate(pieces, start=1):
                    ids, labels, result = _call_intersect(
                        url,
                        layer,
                        piece_geom,
                        fields,
                        return_geometry,
                        list_values,
                        string_values,
                    )
                    merged_ids.extend(ids)
                    merged_labels_list.extend(_split_string_values(labels))

                    frac = i / float(n)
                    if frac > overall_max_fraction:
                        overall_max_fraction = frac
                    _notify(f"Fetched polygon piece {i}/{n}…", overall_max_fraction)

                _notify(
                    "Merging and de-duplicating polygon results…",
                    min(0.98, overall_max_fraction),
                )
                merged_ids = _unique_preserve_order(merged_ids)
                merged_labels_list = _unique_preserve_order(merged_labels_list)

                _notify("Polygon query complete.", 1.0)
                return {
                    "list_values": merged_ids,
                    "string_values": ", ".join(merged_labels_list),
                    "result": result,
                }

            except Exception as e:
                last_error = e
                if debug:
                    st.write(f"[chunking][polygon] Failed @ {slices} slices: {e!r}")
                _notify("Retrying polygon slicing with more slices (×2)…", None)
                slices *= 2

        raise RuntimeError(
            f"AGOL intersect failed for polygon even after slicing up to {max_slices} parts"
        ) from last_error

    # If we couldn't chunk, re-raise the original failure
    raise full_error


# =============================================================================
# ENTRYPOINT: RUN ALL / SELECTED DISTRICT/GEOGRAPHY QUERIES — granular progress
# =============================================================================

def run_district_queries(sections=None, message=None):
    """
    Progress-bar version of district queries with *per-category granular* steps.
    Uses a borderless container inside a placeholder and clears it when done.
    """
    valid_sections = {"house", "senate", "borough", "region", "routes"}

    if sections is None:
        sections_set = valid_sections.copy()
    else:
        sections_set = {str(s).lower().strip() for s in sections if isinstance(s, str)}
        sections_set = {s for s in sections_set if s in valid_sections}
        if not sections_set:
            sections_set = valid_sections.copy()

    # Geometry precedence
    if st.session_state.get("selected_point"):
        st.session_state["project_geometry"] = st.session_state["selected_point"]
        st.session_state["project_geometry_type"] = "point"
    elif st.session_state.get("selected_route"):
        st.session_state["project_geometry"] = st.session_state["selected_route"]
        st.session_state["project_geometry_type"] = "line"
    elif st.session_state.get("selected_boundary"):
        st.session_state["project_geometry"] = st.session_state["selected_boundary"]
        st.session_state["project_geometry_type"] = "polygon"
    else:
        st.session_state["project_geometry"] = None
        st.session_state["project_geometry_type"] = None

    # ---------------------------------------------------------------------
    # FIX (needed per request):
    # Always reset simplified_geometry each run and recompute it from the
    # current project geometry, so ALL downstream querying + chunking uses
    # the simplified geometry when available.
    # ---------------------------------------------------------------------
    st.session_state["simplified_geometry"] = None

    geom_type = st.session_state.get("project_geometry_type")
    geom = st.session_state.get("project_geometry")

    # Simplify Routes or Boundaries (only when applicable)
    if geom is not None and geom_type in ("line", "polygon"):
        st.session_state["simplified_geometry"] = simplify_geometry(
            geom=geom,
            geom_type=geom_type,
            tolerance=.0001,
        )

    # Initialize defaults for selected sections
    if "house" in sections_set:
        st.session_state["house_list"] = []
        st.session_state["house_string"] = ""
    if "senate" in sections_set:
        st.session_state["senate_list"] = []
        st.session_state["senate_string"] = ""
    if "borough" in sections_set:
        st.session_state["borough_list"] = []
        st.session_state["borough_string"] = ""
    if "region" in sections_set:
        st.session_state["region_list"] = []
        st.session_state["region_string"] = ""
    if "routes" in sections_set:
        st.session_state["route_id"] = ""
        st.session_state["route_name"] = ""
        st.session_state.setdefault("route_list", [])
        st.session_state.setdefault("route_ids", "")
        st.session_state.setdefault("route_names", "")

    if geom is None:
        return

    # ---------------------------------------------------------------------
    # If simplified_geometry exists, use it as the basis for ALL AGOL queries
    # (including any chunking inside _agol_intersect_adaptive).
    # ---------------------------------------------------------------------
    geom_for_queries = st.session_state.get("simplified_geometry")
    if geom_for_queries is None:
        geom_for_queries = geom

    # Chunking enablement should follow the geometry type being queried
    enable_route_chunking = (geom_type == "line")
    enable_polygon_chunking = (geom_type == "polygon")

    ordered_sections = [s for s in ["house", "senate", "borough", "region"] if s in sections_set]
    n_sections = max(1, len(ordered_sections))
    cat_share = 1.0 / n_sections  # each category consumes equal slice of the progress bar

    # --------------------------------------------------------------
    # Use a *placeholder* that owns the render; clear it at the end.
    # --------------------------------------------------------------
    progress_block = st.empty()  # owner we can definitely clear

    try:
        with progress_block.container():
            st.write("")
            status_ph = st.empty()
            bar_ph = st.empty()
            progress_bar = bar_ph.progress(0)

            def _set_status(msg: str):
                status_ph.write(msg)

            def _set_progress(p01: float):
                progress_bar.progress(int(max(0, min(1, p01)) * 100))

            # Progress will move from 0 -> 1 across all categories
            base = 0.0

            # Helper to run a category with a live callback coming from _agol_intersect_adaptive
            def _run_category(cat_label: str, intersect_cfg, save_keys):
                nonlocal base

                # 10% for init, 80% for fetching (callback), 10% for saving
                init_w, fetch_w, save_w = 0.10, 0.80, 0.10

                # Init step
                _set_status(f"{cat_label}: Initializing…")
                _set_progress(base + cat_share * (init_w * 0.5))

                last_f = 0.0  # ensure monotone progress even if the adaptive query retries

                def _cb(msg: str, frac):
                    nonlocal last_f
                    if frac is None:
                        # Message-only (e.g., announcing retries)
                        _set_status(f"{cat_label}: {msg}")
                        return
                    if frac < last_f:
                        frac = last_f
                    last_f = frac
                    _set_status(f"{cat_label}: {msg}")
                    _set_progress(base + cat_share * (init_w + fetch_w * frac))

                # Query (with granular callback)
                _set_status(f"{cat_label}: Starting query…")
                res = _agol_intersect_adaptive(
                    url=intersect_cfg["url"],
                    layer=intersect_cfg["layer"],
                    geometry=geom_for_queries,  # <-- uses simplified geometry if present
                    fields=intersect_cfg["fields"],
                    return_geometry=False,
                    list_values=intersect_cfg["list_values"],
                    string_values=intersect_cfg["string_values"],
                    enable_route_chunking=enable_route_chunking,
                    enable_polygon_chunking=enable_polygon_chunking,
                    progress_cb=_cb,  # <-- wiring the live updates
                )

                # Saving (final 10% of the category slice)
                _set_status(f"{cat_label}: Saving results…")
                _set_progress(base + cat_share * (init_w + fetch_w + save_w * 0.5))

                st.session_state[save_keys["list_key"]] = res["list_values"] or []
                st.session_state[save_keys["string_key"]] = res["string_values"] or ""

                # Category done
                _set_status(f"{cat_label}: Complete.")
                _set_progress(base + cat_share)  # reach the end of this category slice
                base += cat_share

            # ---- Run categories in order with granular progress ----
            if "house" in ordered_sections:
                _run_category(
                    "House district(s)",
                    intersect_cfg=dict(
                        url=st.session_state["house_intersect"]["url"],
                        layer=st.session_state["house_intersect"]["layer"],
                        fields="GlobalID,DISTRICT",
                        list_values="GlobalID",
                        string_values="DISTRICT",
                    ),
                    save_keys=dict(list_key="house_list", string_key="house_string"),
                )

            if "senate" in ordered_sections:
                _run_category(
                    "Senate district(s)",
                    intersect_cfg=dict(
                        url=st.session_state["senate_intersect"]["url"],
                        layer=st.session_state["senate_intersect"]["layer"],
                        fields="GlobalID,DISTRICT",
                        list_values="GlobalID",
                        string_values="DISTRICT",
                    ),
                    save_keys=dict(list_key="senate_list", string_key="senate_string"),
                )

            if "borough" in ordered_sections:
                _run_category(
                    "Borough/Census Area(s)",
                    intersect_cfg=dict(
                        url=st.session_state["borough_intersect"]["url"],
                        layer=st.session_state["borough_intersect"]["layer"],
                        fields="GlobalID,NameAlt",
                        list_values="GlobalID",
                        string_values="NameAlt",
                    ),
                    save_keys=dict(list_key="borough_list", string_key="borough_string"),
                )

            if "region" in ordered_sections:
                _run_category(
                    "DOT&PF Region(s)",
                    intersect_cfg=dict(
                        url=st.session_state["region_intersect"]["url"],
                        layer=st.session_state["region_intersect"]["layer"],
                        fields="GlobalID,NameAlt",
                        list_values="GlobalID",
                        string_values="NameAlt",
                    ),
                    save_keys=dict(list_key="region_list", string_key="region_string"),
                )

            # Done
            _set_status("Geography queries complete.")
            _set_progress(1.0)

    finally:
        # Clear the ENTIRE block (headline + status + bar) at the very end.
        progress_block.empty()
