
"""
===============================================================================
MAP UTILITIES (STREAMLIT + FOLIUM) — BOUNDS, CENTERING, UI HELPERS
===============================================================================

Purpose:
    Utility helpers for rendering Folium maps inside Streamlit and for computing
    map view parameters (bounds, center, and approximate zoom). These helpers
    are used across multiple geospatial workflows to keep map behavior consistent.

Key behaviors:
    - UI controls:
        * add_small_geocoder(): adds a compact, collapsed geocoder search box.
        * add_bottom_message(): adds a persistent message bar at the bottom.
    - View calculations:
        * set_bounds_point(): bounds for points (supports nested input shapes).
        * set_bounds_route(): bounds for routes/polylines (recursive walker).
        * set_bounds_boundary(): bounds for polygons (supports multiple shapes).
        * set_center(): center point from bounds.
        * set_zoom(): rough zoom estimation based on longitude span.

Input conventions:
    - Coordinates throughout this module are treated as [lat, lon] ordering,
      unless otherwise noted. Output bounds are always:
        [[min_lat, min_lon], [max_lat, max_lon]]

Notes:
    - These helpers are intentionally defensive: they skip invalid points and
      raise ValueError if no valid coordinates are found.
    - Zoom calculation is approximate and uses only longitude span to avoid
      overfitting and expensive computations.

===============================================================================
"""

import streamlit as st
from streamlit_folium import st_folium
import folium
from folium.plugins import Search, Draw, Geocoder
from branca.element import Element
import math

# =============================================================================
# UI ENHANCEMENTS (FOLIUM CONTROLS / OVERLAYS)
# =============================================================================
# These helpers inject Folium controls and HTML/CSS overlays to improve the user
# experience in Streamlit-hosted maps.
# =============================================================================

def add_small_geocoder(fmap, position: str = "topright", width_px: int = 120, font_px: int = 12):
    """
    Add a small, collapsed geocoder search box to a Folium map.

    Parameters
    ----------
    fmap : folium.Map
        The Folium map object to modify.
    position : str, default "topright"
        Where the geocoder control appears on the map.
    width_px : int, default 120
        Width of the input box in pixels.
    font_px : int, default 12
        Font size of the input text in pixels.

    Notes:
        - This is a UI affordance: it helps users pan/zoom to known places
          without adding permanent markers.
        - Styling is applied via injected CSS at the map HTML root.
    """
    # Add geocoder control (collapsed, no marker on search result)
    Geocoder(collapsed=True, position=position, add_marker=False).add_to(fmap)

    # Inject CSS to style the geocoder input box
    fmap.get_root().html.add_child(folium.Element(f"""
    """))


def geometry_to_folium(
    geom,
    *,
    # Line & polygon style
    color = "#3388ff",
    weight = 3,
    opacity = 1.0,
    dash_array = None,
    fill = True,
    fill_color = None,
    fill_opacity = 0.2,
    # Interactivity
    tooltip = None,
    popup = None,
    # Marker styling
    icon = None,                   # legacy behavior: folium.Marker if icon is provided
    # Point styling (no typing)
    point_shape = None,            # "marker" (default), "circle", "square"
    point_radius = 5,
    point_color = "#2e2e2e",
    point_weight = 1,
    point_fill_color = None,
    point_fill_opacity = 0.9,
    point_renderer = None,         # callable(lat, lon) -> folium.Layer (optional)
    # Explicit feature hint to disambiguate list-of-points
    feature_type = None,           # "point" | "multipoint" | "line" | "polyline" | "polygon" | None(auto)
    hightlight = False,            # kept for backward compatibility; not used
    suppress_focus_outline = True, # remove black focus box on selection
):
    """
    Convert ArcGIS-style or raw coordinate-array geometry into Folium layers.

    Supported inputs:
      - Point: {"x": lon, "y": lat}
      - Multipoint: {"points": [[lon, lat], ...]}
      - Polyline: {"paths": [ [[lon, lat], ...], ... ]}
      - Polygon: {"rings": [ [[lon, lat], ...], ... ]} (outer + holes)
      - List forms (no keys):
          * Single path: [[lon, lat], ...] -> Polyline
          * Multi-path: [ [[lon,lat],...], [[lon,lat],...] ] -> Multiple polylines
          * Single ring: [[lon, lat], ... (closed)] -> Polygon
          * Multi-ring: [ [[lon,lat],...], [[lon,lat],...] ] -> Polygon (outer + holes)
      - Collections: [ geom1, geom2, ... ] -> FeatureGroup

    Notes:
      - Assumes [lon, lat] in WGS84. Converts to [lat, lon] for Folium where required.
      - Polygons with holes are emitted as GeoJSON (Folium.Polygon can't represent holes).
      - Pass `feature_type` to explicitly interpret ambiguous lists (e.g., points-as-line).
      - Point styling:
          * `point_shape="square"` -> tiny square (RegularPolygonMarker).
          * `point_shape="circle"` -> CircleMarker.
          * Default/None -> folium.Marker using `icon` if provided.
      - If `point_renderer` is provided, it is called for every point (lat, lon)
        and its returned layer is used directly.
    """

    # ---------------- CSS Suppressor ----------------
    _FOCUS_CSS = """
    <style>
    /* Remove black focus outline for Leaflet layers and controls */
    .leaflet-container .leaflet-interactive:focus,
    .leaflet-container .leaflet-control a:focus,
    .leaflet-container .leaflet-control-layers label:focus,
    .leaflet-container .leaflet-control-layers-toggle:focus {
        outline: none !important;
        box-shadow: none !important;
    }
    </style>
    """

    def _maybe_suppress_focus(layer):
        if not suppress_focus_outline:
            return layer
        try:
            # Attach CSS to map root; safe to call multiple times
            layer.get_root().html.add_child(Element(_FOCUS_CSS))
        except Exception:
            # Non-fatal if root not available yet; once added to a Map it will apply
            pass
        return layer

    # ---------------- Helpers ----------------
    def is_num_pair(p):
        return isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)

    def to_latlon(seq):
        # Convert [[lon,lat], ...] -> [[lat,lon], ...]
        return [[p[1], p[0]] for p in seq]

    def ensure_closed(ring):
        # Ensure ring is closed for polygon consistency
        return ring if len(ring) > 0 and ring[0] == ring[-1] else ring + [ring[0]]

    def polygon_geojson_from_rings(rings):
        """Build a GeoJSON Polygon (outer + holes). Coordinates remain [lon, lat]."""
        if not rings:
            raise ValueError("Polygon requires at least one ring")
        rings_closed = [ensure_closed(r) for r in rings]
        return {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": rings_closed},
            "properties": {},
        }

    # ---- Style application helpers ----
    def _apply_common_bindings(layer):
        if tooltip is not None:
            try:
                layer.add_child(folium.Tooltip(tooltip))
            except Exception:
                pass
        if popup is not None:
            try:
                layer.add_child(folium.Popup(popup))
            except Exception:
                pass
        return _maybe_suppress_focus(layer)

    def _polyline(**kw):
        layer = folium.PolyLine(**kw)  # keep interactive=True so tooltips/popups work
        return _apply_common_bindings(layer)

    def _polygon_simple(latlon, **_):
        layer = folium.Polygon(
            locations=latlon,
            color=color,
            weight=weight,
            opacity=opacity,
            dash_array=dash_array,
            fill=fill,
            fill_color=(fill_color or color),
            fill_opacity=fill_opacity,
        )
        return _apply_common_bindings(layer)

    def _polygon_geojson(gj_feature):
        def _style_fn(_feature):
            return {
                "color": color,
                "weight": weight,
                "opacity": opacity,
                "dashArray": dash_array if isinstance(dash_array, str)
                else (",".join(map(str, dash_array)) if dash_array else None),
                "fill": fill,
                "fillColor": (fill_color or color),
                "fillOpacity": fill_opacity,
                "className": "no-focus-outline",
            }
        layer = folium.GeoJson(gj_feature, style_function=_style_fn)
        return _apply_common_bindings(layer)

    # ---- Point renderers (no typing) ----
    def _render_point(lat, lon):
        """Return a Folium layer for a single point with the requested style."""
        # Hard override if caller provides a custom renderer
        if callable(point_renderer):
            layer = point_renderer(lat, lon)
            return _apply_common_bindings(layer)

        # Default legacy behavior: folium.Marker using `icon` if caller provided one,
        # and no point_shape hint is set.
        if point_shape is None or str(point_shape).lower() == "marker":
            mk = folium.Marker([lat, lon], icon=icon if icon is not None else None)
            return _apply_common_bindings(mk)

        shape = str(point_shape).lower()

        # Circle marker
        if shape == "circle":
            cm = folium.CircleMarker(
                location=[lat, lon],
                radius=int(point_radius),
                color=point_color,
                weight=int(point_weight),
                fill=True,
                fill_color=(point_fill_color or color),
                fill_opacity=float(point_fill_opacity),
                opacity=float(opacity),
            )
            return _apply_common_bindings(cm)

        # Square marker (RegularPolygonMarker with 4 sides)
        if shape == "square":
            rpm = folium.RegularPolygonMarker(
                location=[lat, lon],
                number_of_sides=4,
                radius=int(point_radius),
                rotation=0,
                color=point_color,
                weight=int(point_weight),
                fill=True,
                fill_color=(point_fill_color or color),
                fill_opacity=float(point_fill_opacity),
                opacity=float(opacity),
            )
            return _apply_common_bindings(rpm)

        # Fallback to Marker if an unknown shape is given
        mk = folium.Marker([lat, lon], icon=icon if icon is not None else None)
        return _apply_common_bindings(mk)

    # ---------- Optional explicit interpretation using feature_type ----------
    ft = (feature_type or "").strip().lower() if isinstance(feature_type, str) else None

    # ---- COLLECTION HANDLING (top-level list) ----
    if isinstance(geom, list) and len(geom) > 0:
        first = geom[0]

        if ft in ("point", "multipoint", "line", "linestring", "polyline", "polygon"):
            if ft in ("point", "multipoint") and is_num_pair(first):
                if len(geom) == 1:
                    lon, lat = geom[0]
                    return _render_point(lat, lon)
                fg = folium.FeatureGroup()
                for lon, lat in (p for p in geom if is_num_pair(p)):
                    _render_point(lat, lon).add_to(fg)
                return _apply_common_bindings(fg)

            if ft in ("line", "linestring", "polyline"):
                if is_num_pair(first):
                    if len(geom) == 1:
                        lon, lat = geom[0]
                        return _render_point(lat, lon)
                    return _polyline(
                        locations=to_latlon(geom),
                        color=color,
                        weight=weight,
                        opacity=opacity,
                        dash_array=dash_array,
                    )
                if isinstance(first, list) and len(first) > 0 and is_num_pair(first[0]):
                    fg = folium.FeatureGroup()
                    for part in geom:
                        if not part:
                            continue
                        if len(part) == 1 and is_num_pair(part[0]):
                            lon, lat = part[0]
                            _render_point(lat, lon).add_to(fg)
                        else:
                            _polyline(
                                locations=to_latlon(part),
                                color=color,
                                weight=weight,
                                opacity=opacity,
                                dash_array=dash_array,
                            ).add_to(fg)
                    return _apply_common_bindings(fg)

            if ft == "polygon":
                if is_num_pair(first):
                    ring = ensure_closed(geom)
                    gj = polygon_geojson_from_rings([ring])
                    return _polygon_geojson(gj)
                if isinstance(first, list) and len(first) > 0 and is_num_pair(first[0]):
                    rings = [ensure_closed(r) for r in geom if r]
                    gj = polygon_geojson_from_rings(rings)
                    return _polygon_geojson(gj)

        # ---------- AUTO-DETECTION ----------
        if is_num_pair(first):
            if len(geom) == 1:
                lon, lat = geom[0]
                return _render_point(lat, lon)
            is_closed = len(geom) >= 4 and geom[0] == geom[-1]
            if is_closed:
                gj = polygon_geojson_from_rings([geom])
                return _polygon_geojson(gj)
            else:
                return _polyline(
                    locations=to_latlon(geom),
                    color=color,
                    weight=weight,
                    opacity=opacity,
                    dash_array=dash_array,
                )
        if isinstance(first, list) and len(first) > 0 and is_num_pair(first[0]):
            first_is_closed = len(first) >= 4 and first[0] == first[-1]
            if first_is_closed:
                gj = polygon_geojson_from_rings(geom)
                return _polygon_geojson(gj)
            else:
                fg = folium.FeatureGroup()
                for part in geom:
                    if not part:
                        continue
                    if len(part) == 1 and is_num_pair(part[0]):
                        lon, lat = part[0]
                        _render_point(lat, lon).add_to(fg)
                    else:
                        _polyline(
                            locations=to_latlon(part),
                            color=color,
                            weight=weight,
                            opacity=opacity,
                            dash_array=dash_array,
                        ).add_to(fg)
                return _apply_common_bindings(fg)

        grp = folium.FeatureGroup()
        for g in geom:
            layer = geometry_to_folium(
                g,
                color=color,
                weight=weight,
                opacity=opacity,
                dash_array=dash_array,
                fill=fill,
                fill_color=fill_color,
                fill_opacity=fill_opacity,
                tooltip=tooltip,
                popup=popup,
                icon=icon,
                point_shape=point_shape,
                point_radius=point_radius,
                point_color=point_color,
                point_weight=point_weight,
                point_fill_color=point_fill_color,
                point_fill_opacity=point_fill_opacity,
                point_renderer=point_renderer,
                feature_type=None,
            )
            layer.add_to(grp)
        return _apply_common_bindings(grp)

    # ---- DICT HANDLING (ArcGIS-style) ----
    if isinstance(geom, dict):
        if "x" in geom and "y" in geom:
            return _render_point(geom["y"], geom["x"])

        if "points" in geom:
            fg = folium.FeatureGroup()
            for x, y in (geom.get("points") or []):
                _render_point(y, x).add_to(fg)
            return _apply_common_bindings(fg)

        if "paths" in geom:
            paths = geom.get("paths") or []
            fg = folium.FeatureGroup()
            for path in paths:
                if not path:
                    continue
                if len(path) == 1 and is_num_pair(path[0]):
                    x, y = path[0]
                    _render_point(y, x).add_to(fg)
                else:
                    _polyline(
                        locations=to_latlon(path),
                        color=color,
                        weight=weight,
                        opacity=opacity,
                        dash_array=dash_array,
                    ).add_to(fg)
            return _apply_common_bindings(fg)

        if "rings" in geom:
            rings = geom.get("rings") or []
            if not rings:
                raise ValueError("Polygon has no rings")
            gj = polygon_geojson_from_rings(rings)
            return _polygon_geojson(gj)

    raise ValueError("Unsupported geometry type or structure")




def extract_coordinates(geom):
    """
    Extract a flat list of [lat, lon] pairs from:
      - A single ArcGIS geometry
      - A list of ArcGIS geometries
    """

    coords = []

    # If list → process each geometry
    if isinstance(geom, list):
        for g in geom:
            coords.extend(extract_coordinates(g))
        return coords

    # POINT
    if "x" in geom and "y" in geom:
        return [[geom["y"], geom["x"]]]

    # MULTIPOINT
    if "points" in geom:
        return [[y, x] for x, y in geom["points"]]

    # POLYLINE
    if "paths" in geom:
        for path in geom["paths"]:
            for x, y in path:
                coords.append([y, x])
        return coords

    # POLYGON
    if "rings" in geom:
        for ring in geom["rings"]:
            for x, y in ring:
                coords.append([y, x])
        return coords

    raise ValueError("Unsupported geometry format")



def add_bottom_message(m, message: str):
    """
    Add a persistent bottom message bar to a Folium map.

    Parameters
    ----------
    m : folium.Map
        The map object to add the message to.
    message : str
        The text to display in the bottom message bar.

    Why:
        This pattern is useful for showing user guidance or status messages
        directly on the map canvas without requiring extra Streamlit layout.
    """
    message_html = f"""
    {message}
    """
    m.get_root().html.add_child(folium.Element(message_html))


# =============================================================================
# BOUNDS CALCULATION HELPERS
# =============================================================================
# These helpers compute [[min_lat, min_lon], [max_lat, max_lon]] for different
# geometry shapes. They are designed to tolerate nested input structures.
# =============================================================================

def set_bounds_point(points):
    """
    Compute a bounding box for:
      - A single point [lat, lon]
      - A list of points [[lat, lon], ...]
      - A list of point groups [[[lat, lon], ...], ...]

    Returns:
        [[min_lat, min_lon], [max_lat, max_lon]]

    Raises:
        ValueError: if input is empty or contains no valid coordinates.

    Notes:
        - This function validates numeric types and basic lat/lon ranges.
        - Invalid entries are skipped rather than failing the whole operation.
    """
    min_lat = float('inf')
    min_lon = float('inf')
    max_lat = float('-inf')
    max_lon = float('-inf')

    def process_point(pt):
        nonlocal min_lon, min_lat, max_lon, max_lat
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
            return

        lon, lat = pt

        # Validate numeric
        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            return

        # Validate ranges
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return

        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lon)
        max_lon = max(max_lon, lon)

    def process_group(group):
        for pt in group:
            process_point(pt)

    # --- Determine input type ---
    if not points:
        raise ValueError("Empty point input.")

    # Case 1: Single point
    if isinstance(points, (list, tuple)) and len(points) == 2 and \
            all(isinstance(x, (int, float)) for x in points):
        process_point(points)

    # Case 2: Flat list of points
    elif all(isinstance(x, (list, tuple)) and len(x) == 2 for x in points):
        process_group(points)

    # Case 3: List of point groups
    else:
        for group in points:
            if isinstance(group, (list, tuple)):
                process_group(group)

    # --- Validate ---
    if min_lat == float('inf'):
        raise ValueError("No valid coordinate data found.")

    return [[min_lat, min_lon], [max_lat, max_lon]]


def set_bounds_route(route):
    """
    Compute a bounding box for:
      - A single route (list of [lat, lon] pairs)
      - A list of routes
      - Any nested structure containing coordinate pairs

    Returns:
        [[min_lat, min_lon], [max_lat, max_lon]]

    Raises:
        ValueError: if route is empty or no valid coordinate data is found.

    Notes:
        - Uses a recursive walker to support arbitrary nesting depth.
        - Treats any 2-length numeric list/tuple as a coordinate pair.
    """
    min_lat = float('inf')
    min_lon = float('inf')
    max_lat = float('-inf')
    max_lon = float('-inf')

    def process_point(pt):
        nonlocal min_lon, min_lat, max_lon, max_lat
        if (
            isinstance(pt, (list, tuple)) and len(pt) == 2
        ):
            try:
                lon = float(pt[0])
                lat = float(pt[1])
            except (TypeError, ValueError):
                return
            min_lat = min(min_lat, lat)
            max_lat = max(max_lat, lat)
            min_lon = min(min_lon, lon)
            max_lon = max(max_lon, lon)

    def walk(obj):
        """Recursively walk any nested structure."""
        if isinstance(obj, (list, tuple)):
            # If it's a coordinate pair, process it
            if len(obj) == 2 and all(isinstance(x, (int, float)) for x in obj):
                process_point(obj)
            else:
                # Otherwise, recurse into children
                for item in obj:
                    walk(item)

    if not route:
        raise ValueError("Empty route input.")

    walk(route)

    if min_lat == float('inf'):
        raise ValueError("No valid coordinate data found.")

    return [[min_lat, min_lon], [max_lat, max_lon]]


def set_bounds_boundary(boundary):
    """
    Compute a bounding box for:
      - A single polygon (list of rings)
      - A list of polygons
      - A flat list of [lon, lat] coordinate pairs

    Returns:
        [[min_lon, min_lat], [max_lon, max_lat]]

    Raises:
        ValueError: if input is empty or no valid coordinates are found.

    Notes:
        - This function assumes coordinate order is [lon, lat].
        - It supports multiple polygons and rings (outer/inner).
    """
    min_lat = float('inf')
    min_lon = float('inf')
    max_lat = float('-inf')
    max_lon = float('-inf')

    def process_point(pt):
        nonlocal min_lon, min_lat, max_lon, max_lat
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
            return

        # Input points are [lon, lat]
        lon, lat = pt

        # Validate numeric
        try:
            lon = float(lon)
            lat = float(lat)
        except Exception:
            return

        # Update bounds
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lon)
        max_lon = max(max_lon, lon)

    def process_polygon(poly):
        # poly = [ring1, ring2, ...]
        for ring in poly:
            if isinstance(ring, (list, tuple)):
                for pt in ring:
                    process_point(pt)

    # --- Determine input type ---
    if not boundary:
        raise ValueError("Empty polygon input.")

    # Case 1: Flat list of coordinate pairs
    if all(isinstance(x, (list, tuple)) and len(x) == 2 for x in boundary):
        for pt in boundary:
            process_point(pt)

    # Case 2: Single polygon (list of rings)
    elif all(isinstance(ring, (list, tuple)) for ring in boundary) and \
            any(isinstance(ring[0], (list, tuple)) for ring in boundary):
        process_polygon(boundary)

    # Case 3: List of polygons
    else:
        for poly in boundary:
            if isinstance(poly, (list, tuple)):
                process_polygon(poly)

    # --- Validate ---
    if min_lat == float('inf'):
        raise ValueError("No valid coordinate data found.")

    # Return bounds as [[min_lon, min_lat], [max_lon, max_lat]]
    return [[min_lat, min_lon], [max_lat, max_lon]]


# =============================================================================
# VIEW HELPERS (CENTER + ZOOM)
# =============================================================================
# These helpers compute a usable map center and a rough zoom level from bounds.
# They are intentionally simple and predictable for consistent UX.
# =============================================================================


def set_zoom(bounds, map_width_px=800, map_height_px=520, padding_px=40,
             tile_size=256, min_zoom=1, max_zoom=18):
    """
    Compute a Leaflet/Folium-style zoom level that fits the given bounds
    into a map viewport, using BOTH longitude and latitude spans with
    Web Mercator math (much more accurate than lon-only).

    Parameters:
        bounds: [[min_lat, min_lon], [max_lat, max_lon]]
        map_width_px:  map viewport width in pixels (approx)
        map_height_px: map viewport height in pixels (approx)
        padding_px:    padding applied on each side (pixels)
        tile_size:     tile size in pixels (Leaflet default is 256)
        min_zoom:      clamp zoom to this minimum
        max_zoom:      clamp zoom to this maximum

    Returns:
        zoom: integer zoom level suitable for folium.Map(..., zoom_start=zoom)

    Notes:
        - Uses Web Mercator projection for latitude span (critical in Alaska).
        - Uses the tighter of the lon-fit and lat-fit zooms.
        - If bounds collapse to a point, returns max_zoom (clamped).
    """
    (min_lat, min_lon), (max_lat, max_lon) = bounds

    # Normalize / sanity
    min_lat, max_lat = float(min(min_lat, max_lat)), float(max(min_lat, max_lat))
    min_lon, max_lon = float(min(min_lon, max_lon)), float(max(min_lon, max_lon))

    # Prevent invalid Mercator at the poles
    def _clamp_lat(lat):
        return max(-85.05112878, min(85.05112878, lat))

    min_lat = _clamp_lat(min_lat)
    max_lat = _clamp_lat(max_lat)

    # Dateline-safe lon span: choose the smaller arc
    delta_lon = abs(max_lon - min_lon)
    if delta_lon > 180:
        delta_lon = 360 - delta_lon

    # Available pixels after padding
    avail_w = max(1, int(map_width_px - 2 * padding_px))
    avail_h = max(1, int(map_height_px - 2 * padding_px))

    # If bounds are effectively a point, zoom way in
    if delta_lon < 1e-12 and abs(max_lat - min_lat) < 1e-12:
        return int(max(min_zoom, min(max_zoom, max_zoom)))

    # Convert latitude to normalized Web Mercator Y in [0,1]
    def _merc_y(lat_deg):
        lat_rad = math.radians(lat_deg)
        return (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi) / 2.0

    y1 = _merc_y(min_lat)
    y2 = _merc_y(max_lat)
    delta_y = abs(y2 - y1)

    # Normalized lon span in [0,1]
    delta_x = delta_lon / 360.0

    # Avoid divide-by-zero
    delta_x = max(delta_x, 1e-15)
    delta_y = max(delta_y, 1e-15)

    # At zoom z: world size in pixels = tile_size * 2^z
    # Span_px = delta * world_px <= available_px  =>  2^z <= available_px / (tile_size * delta)
    # So z <= log2(available_px / (tile_size * delta))
    zoom_x = math.log2(avail_w / (tile_size * delta_x))
    zoom_y = math.log2(avail_h / (tile_size * delta_y))

    # Choose the smaller zoom so BOTH dimensions fit
    zoom = int(math.floor(min(zoom_x, zoom_y)))

    # Clamp
    zoom = max(min_zoom, min(max_zoom, zoom))
    return zoom


def set_center(bounds):
    """
    Given bounds in the format:
        [[min_lat, min_lon], [max_lat, max_lon]]

    Return the center point as:
        [center_lat, center_lon]

    Raises:
        ValueError: if bounds is not in the expected format.
    """
    if not bounds or len(bounds) != 2:
        raise ValueError("Bounds must be [[min_lat, min_lon], [max_lat, max_lon]].")
    min_lat, min_lon = bounds[0]
    max_lat, max_lon = bounds[1]
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    return [center_lat, center_lon]




# =============================================================================
# MAP STYLES
# =============================================================================
# Preset symbology styles for geometry features deployed to maps
# =============================================================================

def loaded_project_point(lat, lon, m):
    popup_html = folium.Popup(
        """
        <div style="font-size:14px; font-weight:600; color:#1a4c7c;">
            Project Footprint (Reference Point)
        </div>
        """,
        max_width=250
    )

    folium.CircleMarker(
        location=[lat, lon],
        radius=8,               # visible but not oversized
        color="#00bcd4",        # bright cyan outline
        weight=3,               # medium border
        fill=True,
        fill_color="#00bcd4",   # same hue for consistency
        fill_opacity=0.85,      # strong visibility
        popup=popup_html
    ).add_to(m)



def loaded_project_line(coords, m):
    popup_html = folium.Popup(
        """
        <div style="font-size:14px; font-weight:600; color:#1a4c7c;">
            Project Footprint (Reference Geometry)
        </div>
        """,
        max_width=250
    )

    folium.PolyLine(
        coords,
        color="#00bcd4",      # bright cyan, highly visible on most basemaps
        weight=6,             # thicker but not overwhelming
        opacity=0.85,         # strong visibility
        dash_array="8, 6",    # dashed to indicate reference
        popup=popup_html
    ).add_to(m)


def loaded_project_polygon(coords, m):
    popup_html = folium.Popup(
        """
        <div style="font-size:14px; font-weight:600; color:#1a4c7c;">
            Project Footprint (Reference Geometry)
        </div>
        """,
        max_width=250
    )

    folium.Polygon(
        coords,
        color="#00bcd4",       # bright cyan outline for visibility
        weight=3,              # clean, medium-weight border
        dash_array="6, 4",     # dashed to indicate reference layer
        fill=True,
        fill_color="#00bcd4",  # same hue for consistency
        fill_opacity=0.25,     # translucent so it doesn't dominate
        popup=popup_html
    ).add_to(m)


def traffic_impact_area(coords, m):
    popup_html = folium.Popup(
        """
        <div style="font-size:14px; font-weight:600; color:#1a4c7c;">
            Project Impact Area (Reference Geometry)
        </div>
        """,
        max_width=250
    )

    folium.Polygon(
        coords,
        color="#00bcd4",       # bright cyan outline for visibility
        weight=3,              # clean, medium-weight border
        dash_array="6, 4",     # dashed to indicate reference layer
        fill=True,
        fill_color="#00bcd4",  # same hue for consistency
        fill_opacity=0.25,     # translucent so it doesn't dominate
        popup=popup_html
    ).add_to(m)


def traffic_impact_route(coords, m):
    popup_html = folium.Popup(
            """
            <div style="font-size:14px; font-weight:600; color:#1a4c7c;">
                Impacted Route (Reference Geometry)
            </div>
            """,
            max_width=250
    )

    folium.PolyLine(
        coords, 
        color="red", 
        weight=6
    ).add_to(m)

