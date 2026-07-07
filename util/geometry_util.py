
"""
===============================================================================
GEOSPATIAL UPLOAD TOOLS (STREAMLIT) — DRAW / MANUAL ENTRY / SHAPEFILE
===============================================================================

Purpose:
    Consolidates Streamlit geospatial upload utilities used to capture project
    geometry in multiple ways:

      1) Draw geometries on a map (points, routes, boundaries)
      2) Enter values (lat/lon point entry; milepoint entry placeholder)
      3) Upload zipped shapefiles (points, polylines, polygons)
      4) Review AASHTOWare-provided coordinates (point)

Key behaviors:
    - Canonical geometry keys:
        All tools write to the same session_state keys expected downstream:
          * st.session_state['selected_point']    -> list[[lat, lon], ...]
          * st.session_state['selected_route']    -> list[list[[lat, lon], ...], ...]
          * st.session_state['selected_boundary'] -> list[list[[lat, lon], ...], ...]

    - “LOAD vs CLEAR” pattern:
        Drawing/manual-entry tools buffer user interactions and only persist to
        the canonical keys when the user presses LOAD. CLEAR wipes selection and
        forces a rerun.

    - Shapefile upload behavior:
        Reads zipped shapefiles with GeoPandas, validates geometry types per mode,
        converts coordinates into app-standard [lat, lon], and persists to the
        canonical session keys.

Session-state dependencies (expected at runtime):
    - Draw / manual entry:
        * 'selected_point', 'selected_route', 'selected_boundary'
        * 'map_reset_counter', 'route_reset_counter' (used to force rerender)
        * 'manual_points_buffer' (manual entry staging)

    - Milepoint entry:
        * 'milepoint' (FeatureServer URL or service reference)

    - Shapefile uploads:
        * 'point_shapefile_uploaded', 'route_shapefile_uploaded',
          'boundary_shapefile_uploaded' flags

Notes:
    - This module is a direct consolidation of earlier feature modules.
      Function logic is preserved; only organization and documentation are applied.
    - Coordinate conventions:
        * ArcGIS/Folium typically use [lat, lon] for location inputs
        * GeoJSON drawn output is [lon, lat] and is converted as needed

===============================================================================
"""

# =============================================================================
# IMPORTS
# =============================================================================
import tempfile
import zipfile
import math
import hashlib
import json
from typing import Optional  # (ensure present at top of file)

import streamlit as st
from streamlit_folium import st_folium
import folium
from shapely.geometry import LineString, Point

# Draw tools
from folium.plugins import Draw, Geocoder

# Shapefile tools
import geopandas as gpd

# RO Helper
from util.read_only_util import ro_widget
from util.input_util import fmt_string

# Map Tools
from util.map_util import (
    add_small_geocoder,
    set_bounds_point,
    set_bounds_route,
    set_bounds_boundary,
    set_zoom,
    geometry_to_folium
)

# Data helpers (milepoint entry)
from agol.agol_util import (
    get_multiple_fields,
    select_record,
    get_routes_within_distance, 
    query_routes_within_buffer,
    get_mileposts_for_route
    )

# Data Helpers Create Buffer
from util.geospatial_util import (
    slice_route_between_points,
    snap_bop_eop_to_route
)



# =============================================================================
# SECTION 1: DRAW-ON-MAP UPLOAD TOOLS (POINT / ROUTE / BOUNDARY)
# =============================================================================
# These functions allow users to interactively draw features on a Folium map.
# The drawn features are captured from st_folium output, but are only persisted
# to session_state when the user clicks LOAD. CLEAR wipes existing selections.
# =============================================================================

def draw_point(container):
    """
    Interactive point drawing tool.

    UI behavior:
        - Displays a Folium map where users can drop one or more points.
        - Previously saved points (session_state['selected_point']) are rendered.
        - Drawn points are captured from st_folium output but are only saved to
          session_state on LOAD.

    Side effects:
        - Updates st.session_state['selected_point'] on LOAD.
        - Clears st.session_state['selected_point'] on CLEAR.
        - Uses st.session_state['map_reset_counter'] to force rerender on CLEAR.
    """

    if "map_reset_counter" not in st.session_state:
        st.session_state.map_reset_counter = 0

    st.markdown("<h6>DROP POINT(S) ON MAP</h6>", unsafe_allow_html=True)
    st.caption(
        "Use the map to drop pins for your project. Select the pin icon on the left, "
        "then click on the map to place points. The points will only be saved when you press **LOAD**."
        " Hit the **CLEAR** button to clear any input points and add new ones"
    )

    # Create map centered on Alaska
    m = folium.Map(location=[64.0000, -152.0000], zoom_start=4)

    # Show previously saved points
    if st.session_state.get("project_geometry") and st.session_state.get("selected_point"):
        layer = geometry_to_folium(
            st.session_state['selected_point'],
            icon=folium.Icon(color="blue"),
            feature_type = 'point'
        )
        layer.add_to(m)

        bounds = set_bounds_point(st.session_state["selected_point"])
        m.fit_bounds(bounds)

    # Draw control
    draw = Draw(
        draw_options={
            "polyline": False,
            "polygon": False,
            "circle": False,
            "rectangle": False,
            "circlemarker": False,
            "marker": True,
        },
        edit_options={"edit": True, "remove": True},
    )
    draw.add_to(m)

    # Add geocoder
    add_small_geocoder(m)

    # Render map
    map_key = f"point_draw_map_{st.session_state.get('map_reset_counter', 0)}"

    # ⛔️ Prevent reruns on pan/zoom:
    # Only return drawings; exclude bounds/zoom/center so panning/zooming doesn't trigger a rerun.
    # (You can include "last_clicked" if needed without re-enabling pan/zoom triggers.)
    output = st_folium(
        m,
        use_container_width=True,
        height=500,
        key=map_key,
        returned_objects=["all_drawings"],  # <--- this prevents pan/zoom-triggered reruns
    )

    # Extract ALL drawn points (but DO NOT save yet)
    latest_points = []
    if output and "all_drawings" in output and output["all_drawings"]:
        for f in output["all_drawings"]:
            if f.get("geometry", {}).get("type") == "Point":
                lon, lat = f["geometry"]["coordinates"]
                latest_points.append([round(lon, 6), round(lat, 6)])

    # 50% width container for buttons
    button_container = st.container()
    with button_container:
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("LOAD", use_container_width=True):
                if latest_points:
                    st.session_state["selected_point"] = latest_points
        with col2:
            if st.button("CLEAR", use_container_width=True):
                st.session_state["selected_point"] = None
                st.session_state.map_reset_counter += 1
                st.rerun()



def draw_line(container):
    """
    Interactive route drawing tool (polylines).

    UI behavior:
        - Displays a Folium map where users can draw one or more routes.
        - Previously saved routes (session_state['selected_route']) are rendered.
        - Drawn lines are captured from st_folium output but only saved on LOAD.

    Side effects:
        - Updates st.session_state['selected_route'] on LOAD.
        - Clears st.session_state['selected_route'] on CLEAR.
        - Uses st.session_state['route_reset_counter'] to force rerender on CLEAR.
    """

    # Ensure a reset counter exists (consistent key)
    if "route_reset_counter" not in st.session_state:
        st.session_state.route_reset_counter = 0

    st.markdown("<h6>DRAW ROUTE(S) ON MAP</h6>", unsafe_allow_html=True)
    st.caption(
        "Use the map to sketch your project route. Select the line tool on the left, "
        "then click on the map to trace your path. You can draw as many lines as you need. "
        "Your routes are saved only when you press **LOAD**. Press **CLEAR** to remove all saved routes."
    )

    # Create map centered on Alaska
    m = folium.Map(location=[64.2008, -149.4937], zoom_start=4)

    # Restore previously saved routes (each route is a list of [lon, lat] points)
    if st.session_state.get("footprint_submitted") and st.session_state.get("selected_route"):
        layer = geometry_to_folium(
            st.session_state['selected_route'],
            weight=4,
            feature_type = 'line'
        )
        layer.add_to(m)

        bounds = set_bounds_route(st.session_state["selected_route"])
        m.fit_bounds(bounds)

    # Draw control (polyline only)
    draw = Draw(
        draw_options={
            "polyline": True,
            "polygon": False,
            "circle": False,
            "rectangle": False,
            "circlemarker": False,
            "marker": False,
        },
        edit_options={"edit": True, "remove": True},
    )
    draw.add_to(m)

    # Add geocoder
    add_small_geocoder(m)

    # Render map (use reset counter in key to force rerender after CLEAR)
    map_key = f"line_draw_map_{st.session_state.get('route_reset_counter', 0)}"

    # Prevent reruns on pan/zoom: only return drawings; exclude bounds/zoom/center
    output = st_folium(
        m,
        use_container_width=True,
        height=500,
        key=map_key,
        returned_objects=["all_drawings"],  # <-- pan/zoom won't trigger reruns
    )

    # Extract ALL drawn lines (but DO NOT save yet)
    latest_routes = []
    if output and "all_drawings" in output and output["all_drawings"]:
        for f in output["all_drawings"]:
            geom = f.get("geometry", {})
            gtype = geom.get("type")
            if gtype == "LineString":
                # GeoJSON coordinates are [lon, lat]; keep storage as [lon, lat]
                coords = geom.get("coordinates", [])
                line_lonlat = [[round(lon, 6), round(lat, 6)] for lon, lat in coords]
                if line_lonlat:
                    latest_routes.append(line_lonlat)
            elif gtype == "MultiLineString":
                # GeoJSON: list of LineStrings; each is list of [lon, lat]
                for line in geom.get("coordinates", []):
                    line_lonlat = [[round(lon, 6), round(lat, 6)] for lon, lat in line]
                    if line_lonlat:
                        latest_routes.append(line_lonlat)

    # 50% width container for buttons
    button_container = st.container()
    with button_container:
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("LOAD", use_container_width=True):
                if latest_routes:
                    st.session_state["selected_route"] = latest_routes
        with col2:
            if st.button("CLEAR", use_container_width=True):
                st.session_state["selected_route"] = None
                st.session_state.route_reset_counter += 1
                st.rerun()



def draw_boundary(container):
    """
    Interactive boundary drawing tool (polygons).

    UI behavior:
        - Displays a Folium map where users can draw one or more polygons.
        - Previously saved boundaries (session_state['selected_boundary']) are rendered.
        - Drawn polygons are captured from st_folium output but only saved on LOAD.

    Side effects:
        - Updates st.session_state['selected_boundary'] on LOAD.
        - Clears st.session_state['selected_boundary'] on CLEAR.
        - Uses st.session_state['route_reset_counter'] (existing pattern) for reset.
    """

    # Ensure a reset counter exists (mirrors other tools)
    if "route_reset_counter" not in st.session_state:
        st.session_state.route_reset_counter = 0

    st.markdown("<h6>DROP BOUNDARY(IES) ON MAP</h6>", unsafe_allow_html=True)
    st.caption(
        "Use the map to outline your project boundary. Select the polygon tool on the left, "
        "then click around the map to define your boundaries. You can draw multiple boundaries on the map. "
        "Your polygons are saved only when you press **LOAD**. Press **CLEAR** to remove all saved polygons."
    )

    # Create map centered on Alaska
    m = folium.Map(location=[64.2008, -149.4937], zoom_start=4)

    # Restore previously saved polygons (each polygon is a list of [lon, lat] points)
    if st.session_state.get("footprint_submitted") and st.session_state.get("selected_boundary"):
        layer = geometry_to_folium(
            st.session_state['selected_boundary'],
            weight=4,
            fill=True,
            feature_type = 'polygon'
        )
        layer.add_to(m)

        bounds = set_bounds_boundary(st.session_state["selected_boundary"])
        m.fit_bounds(bounds)

    # Draw control (polygon only)
    draw = Draw(
        draw_options={
            "polyline": False,
            "polygon": True,
            "circle": False,
            "rectangle": False,
            "circlemarker": False,
            "marker": False,
        },
        edit_options={"edit": True, "remove": True},
    )
    draw.add_to(m)

    # Add geocoder control
    add_small_geocoder(m)

    # Render map in Streamlit
    # Use the reset counter in the key to force rerender after CLEAR
    map_key = f"polygon_draw_map_{st.session_state.get('route_reset_counter', 0)}"

    # Prevent reruns on pan/zoom:
    # Only return drawings; exclude bounds/zoom/center so panning/zooming doesn't trigger a rerun.
    output = st_folium(
        m,
        use_container_width=True,
        height=500,
        key=map_key,
        returned_objects=["all_drawings"],  # <-- stops reruns on pan/zoom while keeping drawing events
    )

    # Extract ALL drawn polygons (but DO NOT save yet)
    latest_boundaries = []
    if output and "all_drawings" in output and output["all_drawings"]:
        for f in output["all_drawings"]:
            geom = f.get("geometry", {})
            gtype = geom.get("type")
            if gtype == "Polygon":
                # GeoJSON polygon: coordinates[0] is outer ring -> list of [lon, lat]
                outer = geom.get("coordinates", [[]])[0]
                poly_lonlat = [[round(lon, 6), round(lat, 6)] for lon, lat in outer]
                if poly_lonlat:
                    latest_boundaries.append(poly_lonlat)
            elif gtype == "MultiPolygon":
                # Each polygon: first ring is outer -> list of [lon, lat]
                for rings in geom.get("coordinates", []):
                    if rings and rings[0]:
                        outer = rings[0]
                        poly_lonlat = [[round(lon, 6), round(lat, 6)] for lon, lat in outer]
                        if poly_lonlat:
                            latest_boundaries.append(poly_lonlat)

    # 50% width container for buttons
    button_container = st.container()
    with button_container:
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("LOAD", use_container_width=True):
                if latest_boundaries:
                    st.session_state["selected_boundary"] = latest_boundaries
        with col2:
            if st.button("CLEAR", use_container_width=True):
                st.session_state["selected_boundary"] = None
                st.session_state.route_reset_counter += 1
                st.rerun()




# =============================================================================
# SECTION 2: MANUAL ENTRY UPLOAD TOOLS (LAT/LON, MILEPOINTS)
# =============================================================================
# These functions provide form-based alternatives to map drawing.
# NOTE: enter_milepoints() is currently a placeholder and depends on the presence
# of a milepoints layer reference in session_state.
# =============================================================================

def enter_latlng(container):
    """
    Manual point entry tool (lat/lon).

    Storage format:
        - Points are stored as [lon, lat]
        - Folium markers are displayed as [lat, lon]

    Behavior:
        - ADD POINT saves to manual_points_buffer as [lon, lat]
        - LOAD saves buffer into selected_point as [lon, lat]
        - CLEAR wipes everything
    """

    # -------------------------------------------------------------------------
    # Init state
    # -------------------------------------------------------------------------
    if "manual_points_buffer" not in st.session_state:
        st.session_state.manual_points_buffer = []
    if "map_reset_counter" not in st.session_state:
        st.session_state.map_reset_counter = 0

    st.markdown("<h6>Enter Latitude & Longitude Coordinates\n</h6>", unsafe_allow_html=True)
    st.caption(
        "Enter coordinates and press **Add point**. Repeat as needed. "
        "Press **LOAD** to save your points. Press **CLEAR** to start over."
    )

    # -------------------------------------------------------------------------
    # Existing saved points (ALWAYS [lon, lat])
    # -------------------------------------------------------------------------
    existing_points = st.session_state.get("selected_point") or []

    # Default inputs based on last saved point (convert [lon, lat] -> lat, lon)
    if existing_points:
        last_lon, last_lat = existing_points[-1]
        default_lat, default_lon = last_lat, last_lon
    else:
        default_lat, default_lon = 0.0, 0.0

    # -------------------------------------------------------------------------
    # Input fields
    # -------------------------------------------------------------------------
    cols = st.columns(2)
    with cols[0]:
        lat = st.number_input("Latitude", value=float(default_lat), format="%.6f")
    with cols[1]:
        lon = st.number_input("Longitude", value=float(default_lon), format="%.6f")

    # -------------------------------------------------------------------------
    # ADD POINT
    # -------------------------------------------------------------------------
    if st.button("ADD POINT", use_container_width=True):
        if not -90 <= lat <= 90:
            st.error("Latitude must be between -90 and 90.")
        elif not -180 <= lon <= 180:
            st.error("Longitude must be between -180 and 180.")
        else:
            # STORE AS [lon, lat]
            st.session_state.manual_points_buffer.append(
                [round(float(lon), 6), round(float(lat), 6)]
            )
            st.rerun()

    # -------------------------------------------------------------------------
    # Build map
    # -------------------------------------------------------------------------
    m = folium.Map(location=[64.0, -152.0], zoom_start=4)
    saved_fg = folium.FeatureGroup(name="Saved Points").add_to(m)
    buffer_fg = folium.FeatureGroup(name="Buffered Points").add_to(m)

    # -------------------------------------------------------------------------
    # Display saved points (green)
    # stored is [lon, lat] -> [lat, lon]
    # -------------------------------------------------------------------------
    for lo, la in existing_points:
        folium.Marker(
            [la, lo],
            icon=folium.Icon(color="green")
        ).add_to(saved_fg)

    # -------------------------------------------------------------------------
    # Display buffered points (blue)
    # stored is [lon, lat] -> [lat, lon]
    # -------------------------------------------------------------------------
    for lo, la in st.session_state.manual_points_buffer:
        folium.Marker(
            [la, lo],
            icon=folium.Icon(color="blue")
        ).add_to(buffer_fg)

    # -------------------------------------------------------------------------
    # Preview marker (not stored yet)
    # -------------------------------------------------------------------------
    preview_lonlat = [round(lon, 6), round(lat, 6)]
    if preview_lonlat not in st.session_state.manual_points_buffer and (lat != 0.0 or lon != 0.0):
        folium.CircleMarker(
            [lat, lon],   # Folium wants [lat, lon]
            radius=5,
            color="orange",
            fill=True,
            fill_opacity=0.7
        ).add_to(m)

    # -------------------------------------------------------------------------
    # Fit to bounds (storage is [lon, lat])
    # -------------------------------------------------------------------------
    all_pts = existing_points + st.session_state.manual_points_buffer
    if all_pts:
        min_lon = min(p[0] for p in all_pts)
        max_lon = max(p[0] for p in all_pts)
        min_lat = min(p[1] for p in all_pts)
        max_lat = max(p[1] for p in all_pts)
        m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

    add_small_geocoder(m)
    st_folium(
        m,
        use_container_width=True,
        height=500,
        key=f"latlng_map_{st.session_state.map_reset_counter}",
    )

    # -------------------------------------------------------------------------
    # LOAD and CLEAR
    # -------------------------------------------------------------------------
    bottom = st.container()
    with bottom:
        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("LOAD", use_container_width=True):
                if st.session_state.manual_points_buffer:
                    # Save buffer as [lon, lat]
                    st.session_state["selected_point"] = list(st.session_state.manual_points_buffer)
                else:
                    st.info("No points to load.")
        with c2:
            if st.button("CLEAR", use_container_width=True):
                st.session_state.manual_points_buffer = []
                st.session_state["selected_point"] = None
                st.session_state.map_reset_counter += 1
                st.rerun()

    st.markdown("", unsafe_allow_html=True)




# =============================================================================
# SECTION 3: SHAPEFILE UPLOAD TOOLS (ZIP)
# =============================================================================
# These functions accept zipped shapefiles and read them using GeoPandas.
# Each upload mode validates expected geometry types and persists into the
# canonical session_state geometry keys.
# =============================================================================

def point_shapefile(container):
    """
    Upload and review a zipped point shapefile.

    Behavior:
        - Accepts a .zip containing required shapefile components (.shp, .shx, .dbf, .prj).
        - Reads features with GeoPandas.
        - Validates that only Point geometries are present.
        - Stores coordinates as [[lon, lat], ...] in:
            * st.session_state.selected_point
            * st.session_state.point_shapefile_uploaded = True

    Review:
        - If a shapefile was uploaded previously, renders a Folium map by passing the
          stored points through geometry_to_folium (as an ArcGIS-style Multipoint).
    """
    st.markdown("<h6>UPLOAD A POINT SHAPEFILE (ZIP)</h6>", unsafe_allow_html=True)
    st.caption(
        "Upload a zipped point shapefile (.zip) containing all required components "
        "(.shp, .shx, .dbf, and .prj). The file must contain Point geometry."
    )

    uploaded = st.file_uploader(
        "Upload shapefile containing all required files (.shp, .shx, .dbf, .prj).",
        type=["zip"],
    )

    # --- If a new file is uploaded, process and store it ---
    if uploaded:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = f"{tmpdir}/shapefile.zip"
            with open(zip_path, "wb") as f:
                f.write(uploaded.getbuffer())
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)

            gdf = gpd.read_file(tmpdir)

            # Reproject to WGS84 if possible (so we can safely store lon/lat)
            try:
                if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs(4326)
            except Exception:
                # If CRS handling fails, proceed; coordinates assumed already lon/lat
                pass

            # Validate geometry type is strictly Point
            geom_types = set(gdf.geom_type.unique())
            if not geom_types.issubset({"Point"}):
                st.warning(f"Uploaded shapefile contains non-point geometries: {geom_types}.")
                st.session_state.point_shapefile_uploaded = False
            else:
                # Store ALL points as [lon, lat]
                all_points = []
                for geom in gdf.geometry:
                    # shapely Point -> (x, y) == (lon, lat)
                    x, y = float(geom.x), float(geom.y)
                    all_points.append([round(x, 6), round(y, 6)])

                st.session_state.selected_point = all_points
                st.session_state.point_shapefile_uploaded = True

    # --- If a point shapefile was uploaded earlier, display it via geometry_to_folium ---
    if st.session_state.get("point_shapefile_uploaded") and st.session_state.get("selected_point"):
        st.write("")
        st.markdown("###### Review Mapped Point(s)", unsafe_allow_html=True)

        points_lonlat = st.session_state.selected_point  # [[lon, lat], ...]

        # Build the Folium map, centered roughly on the first point
        if points_lonlat:
            first_lon, first_lat = points_lonlat[0]
            m = folium.Map(location=[first_lat, first_lon], zoom_start=11)
        else:
            m = folium.Map(location=[64.0, -152.0], zoom_start=4)

        # 🔹 Display using geometry_to_folium as ArcGIS-style Multipoint
        #     {"points": [[lon, lat], ...]} guarantees markers (not a polyline)
        try:
            multipoint_geom = {"points": points_lonlat}
            layer = geometry_to_folium(
                multipoint_geom, 
                icon=folium.Icon(color="blue"),
                feature_type = 'point')
            layer.add_to(m)
        except Exception as e:
            st.error(f"Failed to render uploaded points: {e}")
            return

        # Fit bounds to all points (stored in [lon, lat]; Folium expects [lat, lon])
        if points_lonlat:
            min_lon = min(p[0] for p in points_lonlat)
            max_lon = max(p[0] for p in points_lonlat)
            min_lat = min(p[1] for p in points_lonlat)
            max_lat = max(p[1] for p in points_lonlat)
            m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

        add_small_geocoder(m)

        # Render the map as display-only (no reruns on pan/zoom)
        st_folium(
            m,
            use_container_width=True,
            height=500,
            returned_objects=[],  # don't send bounds/zoom/center back; avoids reruns on pan/zoom
        )


def polyline_shapefile(container):
    """
    Upload and review a zipped polyline shapefile.

    Behavior:
        - Accepts a .zip with LineString / MultiLineString geometries (including Z/M variants).
        - Flattens MultiLineString into individual LineString parts.
        - Stores coordinates in [lon, lat] ordering:
            * st.session_state.selected_route (list of polylines, each a list of [lon, lat])
            * st.session_state.route_shapefile_uploaded = True

    Review:
        - Renders a Folium map drawing all stored routes by passing them through
          geometry_to_folium using the ArcGIS-style {"paths": [...]} form.
    """
    st.markdown("<h6>UPLOAD A POLYLINE SHAPEFILE (ZIP)</h6>", unsafe_allow_html=True)

    st.caption(
        "Upload a zipped polyline shapefile (.zip) containing all required components "
        "(.shp, .shx, .dbf, and .prj). The file should represent one or more routes."
    )

    uploaded = st.file_uploader(
        "Upload shapefile containing all required files (.shp, .shx, .dbf, .prj).",
        type=["zip"],
    )

    # --- If a new file is uploaded, process and store it ---
    if uploaded:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = f"{tmpdir}/shapefile.zip"

            # Save uploaded zip
            with open(zip_path, "wb") as f:
                f.write(uploaded.getbuffer())

            # Extract contents
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)

            # Read shapefile
            gdf = gpd.read_file(tmpdir)

            # Reproject to WGS84 if needed (so we can safely store lon/lat)
            try:
                if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs(4326)
            except Exception:
                # If CRS handling fails, proceed; assume coordinates are already lon/lat
                pass

            # ---- FIX: Accept LineString/MultiLineString, including Z/M variants ----
            # Normalize geometry types to their base (strip " Z", " M", etc.)
            geom_types_raw = set(gdf.geom_type.unique())
            geom_types = {str(t).split()[0] for t in geom_types_raw}  # e.g., "LineString Z" -> "LineString"

            valid_line_types = {"LineString", "MultiLineString"}
            if not geom_types.issubset(valid_line_types):
                st.warning(f"Uploaded shapefile contains non-line geometries: {geom_types_raw}.")
                st.session_state.route_shapefile_uploaded = False
            else:
                all_lines = []
                for geom in gdf.geometry:
                    # Normalize to a list of LineString parts
                    if geom.geom_type.startswith("MultiLineString"):
                        parts = list(geom.geoms)
                    else:
                        parts = [geom]

                    # Each part -> list of [lon, lat] (ignore Z/M if present)
                    for line in parts:
                        # shapely coords can be (x,y) or (x,y,z); use only x,y
                        line_lonlat = []
                        for c in line.coords:
                            x = float(c[0])
                            y = float(c[1])
                            line_lonlat.append([round(x, 6), round(y, 6)])
                        if line_lonlat:
                            all_lines.append(line_lonlat)

                # Store all polylines (each as list of [lon, lat])
                st.session_state.selected_route = all_lines
                st.session_state.route_shapefile_uploaded = True

    # --- If a polyline shapefile was uploaded earlier, display it ---
    if st.session_state.get("route_shapefile_uploaded") and st.session_state.get("selected_route"):
        st.write("")
        st.markdown("###### Review Mapped Route(s)", unsafe_allow_html=True)

        routes = st.session_state["selected_route"]  # list[list[[lon, lat], ...]]

        # Build map
        if routes and routes[0] and routes[0][0]:
            first_lon, first_lat = routes[0][0]
            m = folium.Map(location=[first_lat, first_lon], zoom_start=8)
        else:
            m = folium.Map(location=[64.0, -152.0], zoom_start=4)

        # Display via geometry_to_folium using ArcGIS-style {"paths": [...]}
        try:
            paths_geom = {"paths": routes}  # [lon, lat] arrays; geometry_to_folium swaps as needed for Folium
            layer = geometry_to_folium(
                paths_geom, 
                color="#3388ff", 
                weight=8, 
                opacity=1.0,
                feature_type = 'line')
            layer.add_to(m)
        except Exception as e:
            st.error(f"Failed to render uploaded polylines: {e}")
            return

        # Compute bounds over all vertices (stored as [lon, lat]) and fit map
        if routes:
            all_pts = [pt for line in routes for pt in line]  # flatten
            min_lon = min(p[0] for p in all_pts)
            max_lon = max(p[0] for p in all_pts)
            min_lat = min(p[1] for p in all_pts)
            max_lat = max(p[1] for p in all_pts)
            m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

        add_small_geocoder(m)

        # Display-only review map: don't return bounds/zoom/center so pan/zoom doesn't rerun
        st_folium(
            m,
            use_container_width=True,
            height=500,
            returned_objects=[],  # prevents reruns on pan/zoom
        )


def polygon_shapefile(container):
    """
    Upload and review a zipped polygon shapefile.

    Behavior:
        - Accepts a .zip with Polygon / MultiPolygon geometries (including Z/M variants).
        - Flattens MultiPolygon into individual Polygon parts.
        - Stores ONLY exterior ring coordinates in [lon, lat] order:
            * st.session_state.selected_boundary (list of polygons, each a list[[lon, lat], ...])
            * st.session_state.boundary_shapefile_uploaded = True

    Review:
        - Renders a Folium map drawing all stored polygons by passing each through
          geometry_to_folium using the ArcGIS-style {"rings": [...]} form.
    """
    st.markdown("<h6>UPLOAD A POLYGON SHAPEFILE (ZIP)</h6>", unsafe_allow_html=True)

    st.caption(
        "Upload a zipped polygon shapefile (.zip) containing all required components "
        "(.shp, .shx, .dbf, and .prj). The file should represent the project boundary(ies)."
    )

    uploaded = st.file_uploader(
        "Upload shapefile containing all required files (.shp, .shx, .dbf, .prj).",
        type=["zip"],
    )

    # --- If a new file is uploaded, process and store it ---
    if uploaded:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = f"{tmpdir}/shapefile.zip"

            # Save uploaded zip
            with open(zip_path, "wb") as f:
                f.write(uploaded.getbuffer())

            # Extract contents
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(tmpdir)

            # Read shapefile
            gdf = gpd.read_file(tmpdir)

            # Reproject to WGS84 if needed (so we can safely store lon/lat)
            try:
                if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                    gdf = gdf.to_crs(4326)
            except Exception:
                # If CRS handling fails, proceed; assume coordinates are already lon/lat
                pass

            # ---- Validate geometry types (accept Polygon/MultiPolygon & Z/M variants) ----
            geom_types_raw = set(gdf.geom_type.unique())
            geom_types = {str(t).split()[0] for t in geom_types_raw}  # "Polygon Z" -> "Polygon"
            valid_poly_types = {"Polygon", "MultiPolygon"}

            if not geom_types.issubset(valid_poly_types):
                st.warning(f"Uploaded shapefile contains non-polygon geometries: {geom_types_raw}.")
                st.session_state.boundary_shapefile_uploaded = False
            else:
                all_polygons = []
                for geom in gdf.geometry:
                    # Normalize to list of Polygon parts
                    parts = list(geom.geoms) if geom.geom_type.startswith("MultiPolygon") else [geom]

                    for poly in parts:
                        # Use EXTERIOR ring only; coords may be (x,y) or (x,y,z) -> store [lon, lat]
                        ext = poly.exterior
                        if ext is None:
                            continue
                        ring_lonlat = []
                        for c in ext.coords:
                            x = float(c[0])
                            y = float(c[1])
                            ring_lonlat.append([round(x, 6), round(y, 6)])
                        if ring_lonlat:
                            all_polygons.append(ring_lonlat)

                # Store all polygons in session_state (each polygon is a list of [lon, lat])
                st.session_state.selected_boundary = all_polygons
                st.session_state.boundary_shapefile_uploaded = True

    # --- If a polygon shapefile was uploaded earlier, display it via geometry_to_folium ---
    if st.session_state.get("boundary_shapefile_uploaded") and st.session_state.get("selected_boundary"):
        st.write("")
        st.markdown("###### Review Mapped Boundary(ies)", unsafe_allow_html=True)

        polygons = st.session_state["selected_boundary"]  # list of polygons; each polygon is [[lon, lat], ...]

        # Build map (center on first polygon's first vertex if available)
        if polygons and polygons[0]:
            first_lon, first_lat = polygons[0][0]
            m = folium.Map(location=[first_lat, first_lon], zoom_start=9)
        else:
            m = folium.Map(location=[64.0, -152.0], zoom_start=4)

        # 🔹 Display polygons using geometry_to_folium
        # We’ll add one layer per polygon using ArcGIS-style {"rings": [ring]}
        try:
            fg = folium.FeatureGroup(name="Uploaded Polygons").add_to(m)
            for ring_lonlat in polygons:
                # Ensure ring is closed for proper polygon rendering (geometry_to_folium will also ensure closure)
                if ring_lonlat and ring_lonlat[0] != ring_lonlat[-1]:
                    ring_lonlat = ring_lonlat + [ring_lonlat[0]]
                gj_poly = {"rings": [ring_lonlat]}  # [lon, lat] as required for GeoJSON/ArcGIS
                layer = geometry_to_folium(
                    gj_poly,
                    color="#3388ff",
                    weight=4, 
                    fill=True, 
                    fill_opacity=0.3,
                    feature_type = 'polygon')
                layer.add_to(fg)
        except Exception as e:
            st.error(f"Failed to render uploaded polygons: {e}")
            return

        # Fit bounds from stored [lon, lat] coordinates
        if polygons:
            all_pts = [pt for poly in polygons for pt in poly]
            min_lon = min(p[0] for p in all_pts)
            max_lon = max(p[0] for p in all_pts)
            min_lat = min(p[1] for p in all_pts)
            max_lat = max(p[1] for p in all_pts)
            m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

        add_small_geocoder(m)

        # Display-only review map: don't return bounds/zoom/center so pan/zoom doesn't rerun
        st_folium(
            m,
            use_container_width=True,
            height=500,
            returned_objects=[],  # prevents reruns on pan/zoom
        )


# =============================================================================
# SECTION 4: AASHTOWARE COORDINATES REVIEW (POINT)
# =============================================================================
# This helper displays and confirms AASHTOWare-provided coordinates. It also
# writes a canonical selected_point into session_state for downstream flows.
# =============================================================================
def aashtoware_point(points, container):
    """
    Display AASHTOWare-provided midpoint(s) with tabs. Tabs use the route_name when
    present; otherwise they are lettered as "MIDPOINT A", "MIDPOINT B", ... (no numbers).

    INPUT FORMAT (new):
    points: list[dict] where each dict represents a point with fields like:
    {
        'contract_id': '1058',
        'type': 'Midpoint' \ 'BOP' \ 'EOP',
        'route_id': '...',
        'route_name': '...',
        'lat': 64.84923889,
        'lon': -147.85586389
    }

    Back-compat (legacy):
    points: dict with optional key 'Midpoint' as dict or list of dicts.

    Behavior:
    - Extract ALL items with type == 'Midpoint' (case-insensitive).
    - Tabs ABOVE the map:
      * Label = route_name (if present/trimmed)
      * Else label = "MIDPOINT <letters>" (A, B, C ... AA, AB ...).
      * Inside each tab: read-only Latitude and Longitude.
    - The map (below tabs) shows ALL midpoint markers.
    - Fit map bounds to encompass **all** midpoint markers.

    IMPORTANT (updated):
    - Do NOT write to st.session_state["selected_point"] automatically.
      Only write when the user presses **LOAD**.
    """
    target = container if container is not None else st
    with target:
        st.markdown("<h6>AASHTOWARE COORDINATES\n</h6>", unsafe_allow_html=True)
        st.caption(
            "The coordinates below reflect the project's midpoint(s) from AASHTOWare. "
            "If they are correct, continue. Otherwise update AASHTOWare or select another upload option."
        )

        # -----------------------------
        # Extract midpoint records
        # -----------------------------
        mid_records = []
        if isinstance(points, list):
            for p in points:
                if isinstance(p, dict) and str(p.get("type", "")).strip().upper() == "MIDPOINT":
                    mid_records.append(p)
        elif isinstance(points, dict):
            mid_raw = points.get("Midpoint") or points.get("MIDPOINT") or points.get("midpoint")
            if isinstance(mid_raw, list):
                mid_records = [m for m in mid_raw if isinstance(m, dict)]
            elif isinstance(mid_raw, dict):
                mid_records = [mid_raw]
            else:
                mid_records = []
        else:
            mid_records = []

        # -----------------------------
        # Helper: index -> letters (A, B, ..., Z, AA, AB, ...)
        # -----------------------------
        def _letters(idx_zero_based: int) -> str:
            s = ""
            n = idx_zero_based
            while True:
                n, r = divmod(n, 26)
                s = chr(65 + r) + s
                if n == 0:
                    break
                n -= 1  # Excel-style sequence
            return s

        # -----------------------------
        # Prepare coords + tab labels
        # -----------------------------
        midpoints_lonlat = []  # [[lon, lat], ...] as floats when possible
        tab_labels = []
        unnamed_counter = 0
        for mp in mid_records:
            # Tab label: route_name or "MIDPOINT <letters>"
            rn = mp.get("route_name")
            if isinstance(rn, str) and rn.strip():
                label = rn.strip()
            else:
                label = f"MIDPOINT {_letters(unnamed_counter)}"
                unnamed_counter += 1
            tab_labels.append(label)

            # Coordinates for map (skip only if not castable to float)
            lon = mp.get("lon")
            lat = mp.get("lat")
            try:
                lon_f = float(lon)
                lat_f = float(lat)
                midpoints_lonlat.append([lon_f, lat_f])  # store as [lon, lat]
            except Exception:
                # OK for display in tabs; skip mapping if not castable
                pass

        # -----------------------------
        # Tabs: one per midpoint (read-only lat/lon)
        # -----------------------------
        if mid_records:
            tabs = st.tabs(tab_labels)
            for idx, (tab, mp) in enumerate(zip(tabs, mid_records)):
                with tab:
                    c1, c2 = st.columns(2)
                    with c1:
                        ro_widget(key=f"awp_mid_lat_{idx}", label="Latitude", value=mp.get("lat"))
                    with c2:
                        ro_widget(key=f"awp_mid_lon_{idx}", label="Longitude", value=mp.get("lon"))
        else:
            st.info("No AASHTOWare midpoint found.")

        # -----------------------------
        # Map: render ALL midpoints (blue markers) and fit to ALL
        # -----------------------------
        if midpoints_lonlat:
            first_latlon = [float(midpoints_lonlat[0][1]), float(midpoints_lonlat[0][0])]
        else:
            first_latlon = [0.0, 0.0]
        m = folium.Map(location=first_latlon, zoom_start=12)

        def _as_float_lonlat(pair):
            # pair is [lon, lat]; ensure float; preserve order
            return [float(pair[0]), float(pair[1])]

        for coords in midpoints_lonlat:
            geometry_to_folium(
                geom=[_as_float_lonlat(coords)],
                feature_type="point",
                icon=folium.Icon(color="blue"),
            ).add_to(m)

        # Fit bounds to encompass **all** midpoint markers.
        if midpoints_lonlat:
            m.fit_bounds(set_bounds_point(midpoints_lonlat))

        st_folium(m, use_container_width=True, height=500)

        # -----------------------------
        # NEW: Full-width primary LOAD button (only writer)
        # -----------------------------
        if st.button(
            "LOAD",
            use_container_width=True,
            key="awp_load_all_points",
        ):
            if midpoints_lonlat:
                # Store ALL points as [[lon, lat], ...] (floats)
                st.session_state["selected_point"] = [[float(lon), float(lat)] for lon, lat in midpoints_lonlat]
            else:
                st.info("No points to load.")



def aashtoware_path(points, container):
    """
    Build per-route *entries* from AASHTOWare BOP/EOP points (multiple pairs per route_id allowed),
    snap each pair to its exact AGOL route geometry, slice between snapped endpoints, and render.

    For each entry (per BOP/EOP pair):
    {
      "route_id": str,
      "route_name": str,
      # Originals from AASHTOWare (preserved)
      "bop_orig": [lon, lat],
      "eop_orig": [lon, lat],
      # Snapped results (preferred for display and slicing)
      "bop_snapped": [lon, lat] \ None,
      "eop_snapped": [lon, lat] \ None,
      # Sliced segment (single or multi-part) in [lon,lat] order
      "route_geom": [[lon,lat], ...] or [[[lon,lat],...], ...]
    }

    HARD REQUIREMENTS:
    - BOP and EOP for an entry MUST come from the SAME route_id in the points list.
    - The geometry used MUST be fetched by exact attribute match on that route_id.
    - Coordinate order remains [lon, lat] (floats).
    Writes nothing automatically to session_state["selected_route"]; only on LOAD.

    UPDATED (targeted fix):
    - When multiple BOP/EOP pairs exist on the SAME route_id, pair BOPs to EOPs by
      along-route position (chainage) so each “group” is snipped independently.
      This prevents snipping between two separate groups on the same route.
    """
    target = container if container is not None else st
    with target:
        st.markdown("###### AASHTOWARE COORDINATES\n", unsafe_allow_html=True)
        st.caption(
            "Begin (BOP) and End (EOP) points from AASHTOWare are snapped to the AGOL route, "
            "and the route segment between them is displayed. Tabs show the snapped coordinates "
            "for each route. If the footprint appears incorrect or misaligned, verify the BOP and "
            "EOP values in the **AASHTOWare to APEX** table and update them in AASHTOWare if errors are found."
        )

        # ─────────────────────────────────────────────────────────────────────
        # 0) Normalize incoming 'points' → flat list[dict]
        # ─────────────────────────────────────────────────────────────────────
        flat = []
        if isinstance(points, list):
            flat = [p for p in points if isinstance(p, dict)]
        elif isinstance(points, dict):
            def _as_list(obj):
                if isinstance(obj, list): return [x for x in obj if isinstance(x, dict)]
                if isinstance(obj, dict): return [obj]
                return []
            flat.extend(_as_list(points.get("BOP") or points.get("bop")))
            flat.extend(_as_list(points.get("EOP") or points.get("eop")))
        else:
            flat = []

        if not flat:
            st.info("No AASHTOWare BOP/EOP points were provided.")
            return {}

        # ─────────────────────────────────────────────────────────────────────
        # 1) STRICT grouping by route_id; collect BOPs and EOPs (do NOT pair by index)
        #    Pairing is computed later using along-route chainage to prevent cross-group snips.
        # ─────────────────────────────────────────────────────────────────────
        grouped = {}  # rid -> {"name": str, "bops": [ {"lonlat":[lon,lat], "order":int}, ... ], "eops":[...]}
        order_counter = 0
        for rec in flat:
            t = str(rec.get("type", "")).strip().upper()
            if t not in ("BOP", "EOP"):
                continue

            rid = rec.get("route_id") or rec.get("routeID") or rec.get("Route_ID")
            if rid is None:
                continue

            rname = rec.get("route_name") or rec.get("routeName") or rec.get("Route_Name") or ""
            lon, lat = rec.get("lon"), rec.get("lat")
            try:
                lon_f, lat_f = float(lon), float(lat)
            except Exception:
                continue  # skip invalid coords

            bucket = grouped.setdefault(str(rid), {"name": "", "bops": [], "eops": []})
            if not bucket["name"] and isinstance(rname, str):
                bucket["name"] = rname.strip()

            order_counter += 1
            payload = {"lonlat": [lon_f, lat_f], "order": order_counter}

            if t == "BOP":
                bucket["bops"].append(payload)
            elif t == "EOP":
                bucket["eops"].append(payload)

        # Require at least one route with both BOP and EOP present
        has_any_complete = any((len(v.get("bops") or []) > 0 and len(v.get("eops") or []) > 0) for v in grouped.values())
        if not has_any_complete:
            st.info("No complete BOP/EOP sets found per route_id.")
            return {}

        # ─────────────────────────────────────────────────────────────────────
        # 2) Cache guard signature (based on raw originals + service)
        # ─────────────────────────────────────────────────────────────────────
        def _fingerprint(obj) -> str:
            try:
                return hashlib.md5(
                    json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
            except Exception:
                return f"{type(obj).__name__}:{str(obj)[:200]}"

        ri = st.session_state.get("route_intersect") or {}
        sig_payload = {
            "grouped": {
                rid: {
                    "route_name": data.get("name") or "",
                    "bops": [p.get("lonlat") for p in (data.get("bops") or [])],
                    "eops": [p.get("lonlat") for p in (data.get("eops") or [])],
                }
                for rid, data in grouped.items()
            },
            "service": {
                "url": ri.get("url"),
                "layer": int(ri.get("layer", 0)),
                "id_field": (ri.get("id_field") or "Route_ID"),
            },
        }
        sig = _fingerprint(sig_payload)

        # Bump cache version because pairing logic changed (prevents stale wrong-pair cache reuse)
        cache_key = "awp_paths_cache_v3"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = {}
        cached = st.session_state[cache_key].get(sig)

        # ─────────────────────────────────────────────────────────────────────
        # Helper functions: extract parts, snap-to-line with chainage, and pair by chainage
        # ─────────────────────────────────────────────────────────────────────
        def _extract_parts(route_geom):
            """
            Returns list of parts where each part is a list of [lon,lat].
            Handles:
              - Esri Polyline dict: {"paths":[ [...], [...], ... ]}
              - Plain list: [[lon,lat], ...] (single part)
              - Already list-of-parts: [[[lon,lat],...], ...]
            """
            if route_geom is None:
                return []
            if isinstance(route_geom, dict):
                paths = route_geom.get("paths")
                if isinstance(paths, list) and paths:
                    # Ensure proper shape
                    out = []
                    for p in paths:
                        if isinstance(p, list) and p and isinstance(p[0], (list, tuple)) and len(p[0]) == 2:
                            out.append([[float(x), float(y)] for x, y in p])
                    return out
                # Fallback: try common Esri GeoJSON-ish shape
                coords = route_geom.get("coordinates")
                if isinstance(coords, list) and coords:
                    # Could be LineString or MultiLineString-like
                    if coords and isinstance(coords[0], (list, tuple)) and len(coords[0]) == 2:
                        return [[[float(x), float(y)] for x, y in coords]]
                    if coords and isinstance(coords[0], list) and coords[0] and isinstance(coords[0][0], (list, tuple)) and len(coords[0][0]) == 2:
                        return [[[float(x), float(y)] for x, y in part] for part in coords]
                return []
            if isinstance(route_geom, list) and route_geom:
                # single part: [[lon,lat], ...]
                if isinstance(route_geom[0], (list, tuple)) and len(route_geom[0]) == 2 and all(isinstance(v, (int, float)) for v in route_geom[0]):
                    return [[[float(x), float(y)] for x, y in route_geom]]
                # multi-part: [[[lon,lat],...], ...]
                if isinstance(route_geom[0], list) and route_geom[0] and isinstance(route_geom[0][0], (list, tuple)) and len(route_geom[0][0]) == 2:
                    out = []
                    for part in route_geom:
                        if isinstance(part, list) and part and isinstance(part[0], (list, tuple)) and len(part[0]) == 2:
                            out.append([[float(x), float(y)] for x, y in part])
                    return out
            return []

        def _haversine(lon1, lat1, lon2, lat2):
            R = 6371000.0
            phi1, phi2 = math.radians(lat1), math.radians(lat2)
            dphi = math.radians(lat2 - lat1)
            dlmb = math.radians(lon2 - lon1)
            a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
            return 2 * R * math.asin(math.sqrt(a))

        def _precompute_metrics(line_lonlat):
            lengths = []
            for i in range(len(line_lonlat) - 1):
                lon1, lat1 = line_lonlat[i]
                lon2, lat2 = line_lonlat[i + 1]
                lengths.append(_haversine(lon1, lat1, lon2, lat2))
            cum = [0.0]
            for L in lengths:
                cum.append(cum[-1] + L)
            return {"lengths": lengths, "cum": cum}

        def _snap_pt_to_line(pt_lonlat, line_lonlat, metrics=None):
            """
            Snap a [lon,lat] point to a single polyline part.
            Returns dict: {"snapped":[lon,lat], "dist_m":float, "chainage_m":float, "seg_idx":int}
            """
            if metrics is None:
                metrics = _precompute_metrics(line_lonlat)

            cx, cy = float(pt_lonlat[0]), float(pt_lonlat[1])

            # meters-per-degree at this latitude for distance approximation
            deg_to_m_lat = 111320.0
            deg_to_m_lon = 111320.0 * math.cos(math.radians(cy))

            best = (float("inf"), None, None, None, None)  # (dist_m, px, py, seg_idx, chainage_m)

            for i in range(len(line_lonlat) - 1):
                ax, ay = line_lonlat[i]
                bx, by = line_lonlat[i + 1]
                dx, dy = (bx - ax), (by - ay)
                if dx == 0 and dy == 0:
                    continue

                # projection parameter t in lon/lat plane
                t = ((cx - ax) * dx + (cy - ay) * dy) / (dx * dx + dy * dy)
                t = max(0.0, min(1.0, t))
                px, py = ax + t * dx, ay + t * dy

                # approximate perpendicular distance in meters
                dist_m = math.hypot((cx - px) * deg_to_m_lon, (cy - py) * deg_to_m_lat)

                if dist_m < best[0]:
                    seg_len_m = metrics["lengths"][i] if i < len(metrics["lengths"]) else 0.0
                    chain_m = metrics["cum"][i] + seg_len_m * t
                    best = (dist_m, px, py, i, chain_m)

            dist_m, px, py, seg_idx, chain_m = best
            if px is None or py is None:
                return None
            return {
                "snapped": [float(px), float(py)],
                "dist_m": float(dist_m),
                "chainage_m": float(chain_m),
                "seg_idx": int(seg_idx) if seg_idx is not None else None,
            }

        def _pair_bops_eops_by_chainage(route_geom, bops, eops):
            """
            Pair BOPs to EOPs for a single route_id using along-route chainage.

            Strategy:
              1) Snap every BOP and EOP to the best route part (multipart-safe) and compute chainage.
              2) Group by snapped part index.
              3) Within each part, pair each BOP to the nearest unmatched EOP by absolute chainage difference.
              4) If pairing fails (edge cases), fall back to encounter-order index pairing.
            """
            parts = _extract_parts(route_geom)
            if not parts:
                return []

            # snap all points to their best part
            snapped_bops = []
            snapped_eops = []

            # precompute metrics per part once
            part_metrics = [(_precompute_metrics(p) if p and len(p) > 1 else None) for p in parts]

            def _best_part_snap(p_lonlat):
                best = None  # (dist_m, part_idx, snap_dict)
                for pi, part in enumerate(parts):
                    if not part or len(part) < 2:
                        continue
                    snap = _snap_pt_to_line(p_lonlat, part, metrics=part_metrics[pi])
                    if snap is None:
                        continue
                    cand = (snap["dist_m"], pi, snap)
                    if best is None or cand[0] < best[0]:
                        best = cand
                if best is None:
                    return None
                return {"part_idx": best[1], **best[2]}

            for rec in bops or []:
                ll = rec.get("lonlat")
                if not (isinstance(ll, (list, tuple)) and len(ll) == 2):
                    continue
                snap = _best_part_snap(ll)
                if snap:
                    snapped_bops.append({**rec, **snap})

            for rec in eops or []:
                ll = rec.get("lonlat")
                if not (isinstance(ll, (list, tuple)) and len(ll) == 2):
                    continue
                snap = _best_part_snap(ll)
                if snap:
                    snapped_eops.append({**rec, **snap})

            if not snapped_bops or not snapped_eops:
                return []

            # group by part
            bops_by_part = {}
            eops_by_part = {}
            for b in snapped_bops:
                bops_by_part.setdefault(b["part_idx"], []).append(b)
            for e in snapped_eops:
                eops_by_part.setdefault(e["part_idx"], []).append(e)

            pairs = []
            for part_idx in sorted(set(bops_by_part.keys()) & set(eops_by_part.keys())):
                b_list = sorted(bops_by_part[part_idx], key=lambda r: (r.get("chainage_m", 0.0), r.get("order", 0)))
                e_list = sorted(eops_by_part[part_idx], key=lambda r: (r.get("chainage_m", 0.0), r.get("order", 0)))

                used = set()
                for b in b_list:
                    # find nearest unmatched EOP by chainage distance
                    best_j = None
                    best_d = None
                    for j, e in enumerate(e_list):
                        if j in used:
                            continue
                        d = abs(float(b.get("chainage_m", 0.0)) - float(e.get("chainage_m", 0.0)))
                        if best_d is None or d < best_d:
                            best_d = d
                            best_j = j
                    if best_j is not None:
                        used.add(best_j)
                        pairs.append((b, e_list[best_j]))

            # If nothing paired (rare), fall back to encounter-order pairing by original lists
            if not pairs:
                n = min(len(bops or []), len(eops or []))
                for i in range(n):
                    pairs.append((bops[i], eops[i]))

            # sort pairs for stable rendering: by route-part then bop chainage then original order
            def _pair_sort_key(pair):
                b, e = pair
                return (
                    b.get("part_idx", 0),
                    float(b.get("chainage_m", 0.0)),
                    b.get("order", 0),
                    float(e.get("chainage_m", 0.0)),
                    e.get("order", 0),
                )

            return sorted(pairs, key=_pair_sort_key)

        # ─────────────────────────────────────────────────────────────────────
        # 3) For each route_id → fetch exact route geometry, compute correct pairs, snap, slice
        # ─────────────────────────────────────────────────────────────────────
        if cached is None:
            progress_box = st.empty()
            prog = progress_box.progress(0, text="Preparing route geometry…")

            def _update_progress(i: int, total: int, label: str):
                total = max(1, total)
                pct = int(round((i / total) * 100))
                try:
                    prog.progress(pct, text=label)
                except Exception:
                    try:
                        prog.progress(pct)
                    except Exception:
                        pass

            routes_url = ri.get("url")
            routes_layer = ri.get("layer", 0)
            id_field = ri.get("id_field") or "Route_ID"

            computed_entries = []
            route_ids = list(grouped.keys())
            total_routes = len(route_ids)

            for r_idx, rid in enumerate(route_ids, start=1):
                data = grouped.get(rid) or {}
                bops = data.get("bops") or []
                eops = data.get("eops") or []

                if not bops or not eops:
                    _update_progress(r_idx, total_routes, f"{rid}: missing BOP/EOP")
                    continue

                if not routes_url:
                    _update_progress(r_idx, total_routes, f"{rid}: no route service configured")
                    continue

                # 3.1 Fetch geometry for THIS exact route_id
                try:
                    _update_progress(r_idx - 1, total_routes, f"{rid}: fetching route geometry…")
                    features = select_record(
                        url=str(routes_url),
                        layer=int(routes_layer),
                        id_field=str(id_field),
                        id_value=str(rid),
                        fields=f"{id_field},Route_Name",
                        return_geometry=True,
                    ) or []
                except Exception:
                    features = []

                geom = None
                if features:
                    def _attr_id(f):
                        attrs = (f.get("attributes") or {})
                        return (
                            attrs.get(id_field)
                            or attrs.get(str(id_field).upper())
                            or attrs.get(str(id_field).lower())
                        )
                    exact = next((f for f in features if str(_attr_id(f)).strip() == str(rid).strip()), None)
                    geom = exact.get("geometry") if exact is not None else None

                if not geom:
                    _update_progress(r_idx, total_routes, f"{rid}: route geometry not found for exact Route_ID")
                    continue

                # 3.2 Compute correct pairings for this route_id (prevents cross-group snips)
                pairs = _pair_bops_eops_by_chainage(geom, bops, eops)
                if not pairs:
                    _update_progress(r_idx, total_routes, f"{rid}: could not pair BOP/EOP for this route")
                    continue

                # 3.3 For each paired BOP/EOP → snap (existing helper) + slice
                for (b_rec, e_rec) in pairs:
                    bop = b_rec.get("lonlat")
                    eop = e_rec.get("lonlat")
                    entry = {
                        "route_id": rid,
                        "route_name": data.get("name") or "",
                        "bop_orig": bop,
                        "eop_orig": eop,
                        "bop_snapped": None,
                        "eop_snapped": None,
                        "route_geom": None,
                    }

                    # Snap THIS entry's BOP/EOP to THIS geometry (preserves prior snapping behavior)
                    try:
                        snapped_bop, snapped_eop, chosen_part = snap_bop_eop_to_route(
                            route_geom=geom,
                            bop_lonlat=bop,
                            eop_lonlat=eop,
                        )
                        entry["bop_snapped"] = snapped_bop
                        entry["eop_snapped"] = snapped_eop
                    except Exception:
                        chosen_part = None

                    def _valid_pt(pt):
                        return (
                            isinstance(pt, (list, tuple))
                            and len(pt) == 2
                            and all(isinstance(v, (int, float)) for v in pt)
                        )

                    if not chosen_part or not _valid_pt(entry.get("bop_snapped")) or not _valid_pt(entry.get("eop_snapped")):
                        entry["route_geom"] = None
                        computed_entries.append(entry)
                        continue

                    # Slice THIS route between THESE snapped points
                    try:
                        seg = slice_route_between_points(
                            route_geom=chosen_part,  # [[lon,lat], ...]
                            start_point=entry.get("bop_snapped"),
                            end_point=entry.get("eop_snapped"),
                        )
                    except Exception:
                        seg = None

                    entry["route_geom"] = seg
                    computed_entries.append(entry)

                _update_progress(r_idx, total_routes, f"{rid}: ready")

            try:
                progress_box.empty()
            except Exception:
                pass

            if not computed_entries:
                st.info("No routes could be created from the provided BOP/EOP points.")
                return {}

            # Cache the full computed entries (with originals + snapped + geometry)
            st.session_state[cache_key][sig] = computed_entries
            computed = computed_entries
        else:
            computed = cached

        # Optional seed (now includes originals; snapped slots preserved)
        st.session_state["awp_route_entries"] = [
            {
                "route_id": e.get("route_id"),
                "route_name": e.get("route_name"),
                "bop_orig": e.get("bop_orig"),
                "eop_orig": e.get("eop_orig"),
                "bop_snapped": e.get("bop_snapped"),
                "eop_snapped": e.get("eop_snapped"),
            }
            for e in computed
        ]

        # ─────────────────────────────────────────────────────────────────────
        # 4) Render: tabs + map (all segments + BOP/EOP markers; fit to all)
        # Tabs and map ALWAYS prefer snapped values; fall back to originals.
        # ─────────────────────────────────────────────────────────────────────
        # Build tab labels (ensure uniqueness if names repeat)
        def _unique_labels(bases):
            seen = {}
            labels = []
            for b in bases:
                base = (b or "").strip()
                base = base if base else "—"
                cnt = seen.get(base, 0) + 1
                seen[base] = cnt
                labels.append(base if cnt == 1 else f"{base} ({cnt})")
            return labels

        bases = [(e.get("route_name") or "").strip() or str(e.get("route_id")) for e in computed]
        tab_labels = _unique_labels(bases)
        tabs = st.tabs(tab_labels)

        for idx, (tab, e) in enumerate(zip(tabs, computed)):
            with tab:
                # Prefer snapped values in tabs
                bop_ll = e.get("bop_snapped") or e.get("bop_orig")
                eop_ll = e.get("eop_snapped") or e.get("eop_orig")

                colA, colB = st.columns(2)
                with colA:
                    ro_widget(key=f"awp_bop_lat_{idx}", label="BEGIN Latitude",
                              value=(bop_ll[1] if isinstance(bop_ll, (list, tuple)) else None))
                with colB:
                    ro_widget(key=f"awp_bop_lon_{idx}", label="BEGIN Longitude",
                              value=(bop_ll[0] if isinstance(bop_ll, (list, tuple)) else None))

                colC, colD = st.columns(2)
                with colC:
                    ro_widget(key=f"awp_eop_lat_{idx}", label="END Latitude",
                              value=(eop_ll[1] if isinstance(eop_ll, (list, tuple)) else None))
                with colD:
                    ro_widget(key=f"awp_eop_lon_{idx}", label="END Longitude",
                              value=(eop_ll[0] if isinstance(eop_ll, (list, tuple)) else None))

        # Map start location: use first available snapped (else original)
        first_pt = None
        for e in computed:
            pt = e.get("bop_snapped") or e.get("bop_orig")
            if pt:
                first_pt = pt
                break
            pt = e.get("eop_snapped") or e.get("eop_orig")
            if pt:
                first_pt = pt
                break

        start_latlon = [first_pt[1], first_pt[0]] if first_pt else [64.0, -152.0]
        m = folium.Map(location=start_latlon, zoom_start=10)

        # Draw sliced route segments first (so markers sit on top)
        for e in computed:
            seg = e.get("route_geom")
            if not seg:
                continue
            try:
                geometry_to_folium(
                    geom=seg,
                    color="#3388ff",
                    weight=6,
                    opacity=1.0,
                    tooltip=f"{e.get('route_id')}: {e.get('route_name') or ''}",
                    feature_type="line",
                ).add_to(m)
            except Exception:
                pass

        # BOP/EOP markers — prefer snapped values for display
        all_points_lonlat = []
        for e in computed:
            name = (e.get("route_name") or "").strip()
            bop = e.get("bop_snapped") or e.get("bop_orig")
            eop = e.get("eop_snapped") or e.get("eop_orig")

            if isinstance(bop, (list, tuple)) and len(bop) == 2:
                all_points_lonlat.append(bop)
                try:
                    folium.Marker(
                        [bop[1], bop[0]],
                        tooltip=f"BEGIN: {name}" if name else "BEGIN",
                        icon=folium.Icon(color="green"),
                    ).add_to(m)
                except Exception:
                    pass

            if isinstance(eop, (list, tuple)) and len(eop) == 2:
                all_points_lonlat.append(eop)
                try:
                    folium.Marker(
                        [eop[1], eop[0]],
                        tooltip=f"END: {name}" if name else "END",
                        icon=folium.Icon(color="red"),
                    ).add_to(m)
                except Exception:
                    pass

        # Fit bounds to all points
        if all_points_lonlat:
            try:
                bounds = set_bounds_point(all_points_lonlat)
                m.fit_bounds(bounds)
            except Exception:
                pass


        _ = st_folium(
            m,
            use_container_width=True,
            height=520,
            zoom = set_zoom(bounds),
            returned_objects=["last_clicked"],  # no pan/zoom reruns
        )

        # -----------------------------
        # LOAD button → selected_route
        # -----------------------------
        def _as_list_of_lines(seg):
            """
            Normalize route_geom into a list of polylines.
            - If seg is a single polyline: [[lon,lat], ...] -> [seg]
            - If seg is multi-part: [[[lon,lat],...], [[lon,lat],...]] -> seg
            - Otherwise: return []
            """
            if not isinstance(seg, list) or not seg:
                return []
            # single part: [[lon,lat], ...]
            if (isinstance(seg[0], (list, tuple)) and len(seg[0]) == 2
                    and all(isinstance(v, (int, float)) for v in seg[0])):
                return [seg]
            # multi-part: [[[lon,lat],...], ...]
            if (isinstance(seg[0], (list, tuple)) and seg
                    and isinstance(seg[0][0], (list, tuple)) and len(seg[0][0]) == 2):
                return seg
            return []

        if st.button("LOAD", use_container_width=True, key="awp_load_all_routes_v2"):
            all_lines = []
            for e in computed:
                parts = _as_list_of_lines(e.get("route_geom"))
                if parts:
                    all_lines.extend(parts)
            if all_lines:
                st.session_state["selected_route"] = [
                    [[float(x), float(y)] for (x, y) in line] for line in all_lines
                ]
            else:
                st.info("No routes to load.")




def select_route_and_points(container, key_prefix: str = "", is_existing: bool = False, package=None):
    """
    One-step UI (pure selector):
    - Segmented control at top: 1. Select Route (default), 2. Set Start, 3. Set End
    - Start/End disabled until a route is selected (soft-lock before widget render)
    - When Select Route is active → map clicks select a route
    - When Start/End active → map clicks snap endpoints
    - Draw order: Project Geometry (footprint) → Routes → Markers (Start/End)
    VIEWPORT POLICY (fit_bounds every render):
    - The map always calls `fit_bounds(bounds)` to the current project/area on render.
    - Segmented-control changes do not change which area we fit to; they only change click behavior.
    """
    # --- Headings (contextual) ---
    if not is_existing:
        st.markdown("###### ADD ROUTE'S TRAFFIC IMPACT WORK EXTENT", unsafe_allow_html=True)
        st.caption(
            "Create the work extent for a route’s traffic impact. First, select the impacted route, then place the "
            "start and end points to define the extent of work along the route. Once the route and both points are set, "
            "you can load the impacted route into the project using the page controls."
        )
    else:
        st.markdown("###### MANAGE ROUTE'S TRAFFIC IMPACT WORK EXTENT", unsafe_allow_html=True)
        st.caption(
            "Manage the work extent for an existing impacted route. Select the route and adjust the start and end "
            "points to update the extent of work along the route. Once changes are made, use the page controls to "
            "update or delete the impacted route from the project."
        )

    # ---------------------------
    # Helpers
    # ---------------------------
    def _get_last_click():
        try:
            return (st.session_state.get(map_key) or {}).get("last_clicked")
        except Exception:
            return None

    def _ensure_ti_dict():
        ti_key_local = f"{key_prefix}traffic_impact"
        if ti_key_local not in st.session_state or not isinstance(st.session_state[ti_key_local], dict):
            st.session_state[ti_key_local] = {
                "route_id": None,
                "route_name": None,
                "route_geom": None,
                "start_point": None,
                "end_point": None,
            }
        return ti_key_local

    def _line_distance_meters(click_lonlat, line_lonlat):
        try:
            from shapely.geometry import LineString, Point
            from shapely.ops import transform as shp_transform
            from pyproj import Transformer
            to_merc = Transformer.from_crs(4326, 3857, always_xy=True).transform
            ln = shp_transform(to_merc, LineString(line_lonlat))
            pt = shp_transform(to_merc, Point(click_lonlat))
            return pt.distance(ln)
        except Exception:
            lon, lat = click_lonlat
            deg_to_m_lat = 111_320.0
            deg_to_m_lon = 111_320.0 * math.cos(math.radians(lat))

            def _segdist(p, a, b):
                (x, y), (x1, y1), (x2, y2) = p, a, b
                dx, dy = (x2 - x1), (y2 - y1)
                if dx == 0 and dy == 0:
                    return math.hypot((x - x1) * deg_to_m_lon, (y - y1) * deg_to_m_lat)
                t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
                px, py = x1 + t * dx, y1 + t * dy
                return math.hypot((x - px) * deg_to_m_lon, (y - py) * deg_to_m_lat)

            best = float("inf")
            for i in range(len(line_lonlat) - 1):
                best = min(best, _segdist((lon, lat), line_lonlat[i], line_lonlat[i + 1]))
            return best

    def _haversine(lon1, lat1, lon2, lat2):
        R = 6371000.0
        import math as _m
        phi1, phi2 = _m.radians(lat1), _m.radians(lat2)
        dphi = _m.radians(lat2 - lat1)
        dlmb = _m.radians(lon2 - lon1)
        a = _m.sin(dphi / 2) ** 2 + _m.cos(phi1) * _m.cos(phi2) * _m.sin(dlmb / 2) ** 2
        return 2 * R * _m.asin(_m.sqrt(a))

    def _precompute_metrics(line_lonlat):
        lengths = []
        for i in range(len(line_lonlat) - 1):
            lon1, lat1 = line_lonlat[i]
            lon2, lat2 = line_lonlat[i + 1]
            lengths.append(_haversine(lon1, lat1, lon2, lat2))
        cum = [0.0]
        for L in lengths:
            cum.append(cum[-1] + L)
        return {"lengths": lengths, "cum": cum}

    def _snap(clicked, line_lonlat, metrics):
        cx, cy = float(clicked["lng"]), float(clicked["lat"])
        best = (float("inf"), None, None, None, None)
        for i in range(len(line_lonlat) - 1):
            ax, ay = line_lonlat[i]
            bx, by = line_lonlat[i + 1]
            dx, dy = (bx - ax), (by - ay)
            if dx == 0 and dy == 0:
                continue
            t = ((cx - ax) * dx + (cy - ay) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
            px, py = ax + t * dx, ay + t * dy
            dist = math.hypot(cx - px, cy - py)
            if dist < best[0]:
                chain = metrics["cum"][i] + metrics["lengths"][i] * t
                best = (dist, px, py, i, chain)
        _, px, py, seg, chain = best
        return {"lat": py, "lng": px, "lonlat": [px, py], "seg_idx": seg, "chainage_m": chain}

    def _fingerprint(obj) -> str:
        try:
            return hashlib.md5(
                json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        except Exception:
            return f"{type(obj).__name__}:{str(obj)[:200]}"

    # ---------------------------
    # Session keys
    # ---------------------------
    cand_key = f"{key_prefix}impact_route_candidates"
    cand_sig_key = f"{key_prefix}impact_route_candidates_sig"  # cache guard
    map_key = f"{key_prefix}route_map"
    seg_key_legacy = f"{key_prefix}place_mode"
    seg_key = f"{key_prefix}place_mode_v2"
    sel_id_key = f"{key_prefix}selected_route_id"
    sel_name_key = f"{key_prefix}selected_route_name"
    sel_geom_key = f"{key_prefix}selected_route_geom"
    tol_key = f"{key_prefix}route_click_out_of_tolerance"
    reset_notice_key = f"{key_prefix}__route_reset_notice"
    ti_key = _ensure_ti_dict()

    # ----------------------------------------------------------------
    # --- OID persistence: normalize & cache IDs for existing items ---
    # ----------------------------------------------------------------
    oid_parent_key = f"{key_prefix}ti_parent_oid"
    oid_route_key = f"{key_prefix}ti_route_oid"
    oid_start_key = f"{key_prefix}ti_start_oid"
    oid_end_key = f"{key_prefix}ti_end_oid"

    def _norm_parent_oid(src: dict):
        if not isinstance(src, dict): return None
        return src.get("objectid") or src.get("OBJECTID") or src.get("objectId") or src.get("ti_objectid")

    def _norm_child_oids(src: dict):
        if not isinstance(src, dict): return (None, None, None)
        return (
            src.get("route_objectid") or src.get("routeObjectId") or src.get("route_OBJECTID"),
            src.get("start_objectid") or src.get("startObjectId") or src.get("start_OBJECTID"),
            src.get("end_objectid") or src.get("endObjectId") or src.get("end_OBJECTID"),
        )

    if is_existing and isinstance(package, dict):
        # Capture once (do not overwrite non-None values).
        p_oid = _norm_parent_oid(package)
        r_oid, s_oid, e_oid = _norm_child_oids(package)
        if p_oid is not None and st.session_state.get(oid_parent_key) is None:
            st.session_state[oid_parent_key] = p_oid
        if r_oid is not None and st.session_state.get(oid_route_key) is None:
            st.session_state[oid_route_key] = r_oid
        if s_oid is not None and st.session_state.get(oid_start_key) is None:
            st.session_state[oid_start_key] = s_oid
        if e_oid is not None and st.session_state.get(oid_end_key) is None:
            st.session_state[oid_end_key] = e_oid

        # Rehydrate IDs into the working package if missing on re-entry.
        if "objectid" not in package and st.session_state.get(oid_parent_key) is not None:
            package["objectid"] = st.session_state[oid_parent_key]
        if "route_objectid" not in package and st.session_state.get(oid_route_key) is not None:
            package["route_objectid"] = st.session_state[oid_route_key]
        if "start_objectid" not in package and st.session_state.get(oid_start_key) is not None:
            package["start_objectid"] = st.session_state[oid_start_key]
        if "end_objectid" not in package and st.session_state.get(oid_end_key) is not None:
            package["end_objectid"] = st.session_state[oid_end_key]

    # ------------------------------------------------------
    # Decide which area polygon to display (visual only)
    # ------------------------------------------------------
    package_area = (package or {}).get("area") if isinstance(package, dict) else None
    buffered_area = st.session_state.get("impact_area")  # from Manage
    fit_geom_key = f"{key_prefix}fit_bounds_geom"
    parent_fit_geom = st.session_state.get(fit_geom_key)

    if is_existing and package_area:
        area_for_display = package_area
    else:
        area_for_display = parent_fit_geom or buffered_area or []

    # ---------------------------
    # Project geometry change-detection (run BEFORE seeding)
    # ---------------------------
    proj_geom_sig_key = f"{key_prefix}__proj_geom_sig"
    project_changed = False
    project_geom = st.session_state.get("apex_proj_area")
    curr_proj_sig = _fingerprint(project_geom)
    prev_proj_sig = st.session_state.get(proj_geom_sig_key)
    if prev_proj_sig is None:
        st.session_state[proj_geom_sig_key] = curr_proj_sig
    elif curr_proj_sig != prev_proj_sig:
        # Clear previous selection — we'll reseed from `package` below
        st.session_state.pop(sel_id_key, None)
        st.session_state.pop(sel_name_key, None)
        st.session_state.pop(sel_geom_key, None)
        st.session_state.pop(f"{key_prefix}selected_start_point", None)
        st.session_state.pop(f"{key_prefix}selected_end_point", None)
        st.session_state[ti_key].update(
            {"route_id": None, "route_name": None, "route_geom": None, "start_point": None, "end_point": None}
        )
        st.session_state.pop(tol_key, None)
        try:
            st.session_state.setdefault(map_key, {})
            st.session_state[map_key]["last_clicked"] = None
        except Exception:
            pass
        st.session_state[proj_geom_sig_key] = curr_proj_sig
        st.session_state[reset_notice_key] = True
        project_changed = True

    # ------------------------------------------
    # Seed from package (AFTER change-detection)
    # ------------------------------------------
    if isinstance(package, dict):
        ti_dict = st.session_state.get(ti_key, {})
        needs_seed = project_changed or not any(
            [
                ti_dict.get("route_id"),
                ti_dict.get("route_geom"),
                ti_dict.get("start_point"),
                ti_dict.get("end_point"),
            ]
        )
        if needs_seed:
            for k in ("route_id", "route_name", "route_geom", "start_point", "end_point"):
                if package.get(k) is not None:
                    ti_dict[k] = package.get(k)
            if package.get("route_id") is not None:
                st.session_state[sel_id_key] = package.get("route_id")
            if package.get("route_name") is not None:
                st.session_state[sel_name_key] = package.get("route_name")
            if package.get("route_geom") is not None:
                st.session_state[sel_geom_key] = package.get("route_geom")
            if package.get("start_point") is not None:
                st.session_state[f"{key_prefix}selected_start_point"] = package.get("start_point")
            if package.get("end_point") is not None:
                st.session_state[f"{key_prefix}selected_end_point"] = package.get("end_point")

    # ---------------------------
    # Candidate routes (cached per area signature)
    # ---------------------------
    results = []

    def _fingerprint_area(geom):
        return _fingerprint(geom)

    buffers_sig = _fingerprint_area(area_for_display)
    try:
        if st.session_state.get(cand_sig_key) != buffers_sig:
            results = query_routes_within_buffer(
                area_for_display,
                fields=("Route_ID", "Route_Name"),
                include_geometry=True,
            ) or []
            st.session_state[cand_key] = results
            st.session_state[cand_sig_key] = buffers_sig
        else:
            results = st.session_state.get(cand_key, []) or []
    except Exception:
        results = st.session_state.get(cand_key, []) or []

    id_to_name, id_to_geom = {}, {}
    for r in results:
        attrs = r.get("attributes") or {}
        rid = attrs.get("Route_ID")
        geom = r.get("geometry") or []
        if rid and geom:
            id_to_name[rid] = attrs.get("Route_Name")
            id_to_geom[rid] = geom

    # ---------- RESOLVE selection robustly (supports is_existing) ----------
    # Prefer explicit session keys, then TI dict, then incoming package
    ti_dict_now = st.session_state.get(ti_key, {}) or {}
    pkg_dict = package if isinstance(package, dict) else {}

    selected_id = (
        st.session_state.get(sel_id_key)
        or ti_dict_now.get("route_id")
        or pkg_dict.get("route_id")
    )
    selected_geom = (
        st.session_state.get(sel_geom_key)
        or ti_dict_now.get("route_geom")
        or pkg_dict.get("route_geom")
    )

    # =======================================================
    # Segmented Control (numbered; stable)
    # =======================================================
    if seg_key_legacy in st.session_state:
        st.session_state.pop(seg_key_legacy, None)

    OPTIONS = ["1. Select Route", "2. Set Start", "3. Set End"]
    disabled_start_end = selected_id is None
    curr = st.session_state.get(seg_key)
    if curr not in OPTIONS:
        st.session_state[seg_key] = "1. Select Route"
        curr = "1. Select Route"
    if disabled_start_end and curr in ("2. Set Start", "3. Set End"):
        st.session_state[seg_key] = "1. Select Route"
        curr = "1. Select Route"

    place_mode = st.segmented_control(
        "Complete Steps",
        options=OPTIONS,
        key=seg_key,
        width="stretch",
    )

    # ---------------------------
    # Route fields (READ-ONLY)
    # ---------------------------
    col1, col2 = st.columns(2)
    with col1:
        ro_widget(f"{key_prefix}route_id_ro", "Route ID", fmt_string(selected_id or ""))
    with col2:
        ro_widget(
            f"{key_prefix}route_name_ro",
            "Route Name",
            fmt_string(st.session_state.get(sel_name_key, "")),
        )

    # ---------------------------
    # Map click for ROUTE SELECTION
    # ---------------------------
    last_click = _get_last_click()
    if last_click and place_mode == "1. Select Route" and id_to_geom:
        try:
            click_lon, click_lat = float(last_click["lng"]), float(last_click["lat"])
            nearest_id, nearest_dist = None, float("inf")
            for rid, geom in id_to_geom.items():
                d = _line_distance_meters((click_lon, click_lat), geom)
                if d < nearest_dist:
                    nearest_dist, nearest_id = d, rid
            if nearest_id and nearest_dist <= 100:
                prev_id = st.session_state.get(sel_id_key)
                if prev_id != nearest_id:
                    # Route changed → clear points (IDs preserved)
                    st.session_state.pop(f"{key_prefix}selected_start_point", None)
                    st.session_state.pop(f"{key_prefix}selected_end_point", None)
                    st.session_state[ti_key]["start_point"] = None
                    st.session_state[ti_key]["end_point"] = None
                    if isinstance(package, dict):
                        package["start_point"] = None
                        package["end_point"] = None

                st.session_state[sel_id_key] = nearest_id
                st.session_state[sel_name_key] = id_to_name.get(nearest_id)
                st.session_state[sel_geom_key] = id_to_geom.get(nearest_id)
                st.session_state[ti_key].update(
                    {
                        "route_id": nearest_id,
                        "route_name": st.session_state[sel_name_key],
                        "route_geom": st.session_state[sel_geom_key],
                    }
                )
                if isinstance(package, dict):
                    package["route_id"] = st.session_state[ti_key]["route_id"]
                    package["route_name"] = st.session_state[ti_key]["route_name"]
                    package["route_geom"] = st.session_state[ti_key]["route_geom"]

                # --- NAME from route_name (existing only) ---
                if is_existing and package.get("route_name"):
                    package["name"] = f"Traffic Impact @ {package['route_name']}"

                # Re-attach IDs after route change if needed
                if "objectid" not in package and st.session_state.get(oid_parent_key) is not None:
                    package["objectid"] = st.session_state[oid_parent_key]
                if "route_objectid" not in package and st.session_state.get(oid_route_key) is not None:
                    package["route_objectid"] = st.session_state[oid_route_key]
                if "start_objectid" not in package and st.session_state.get(oid_start_key) is not None:
                    package["start_objectid"] = st.session_state[oid_start_key]
                if "end_objectid" not in package and st.session_state.get(oid_end_key) is not None:
                    package["end_objectid"] = st.session_state[oid_end_key]

                # Reset click
                try:
                    st.session_state.setdefault(map_key, {})
                    st.session_state[map_key]["last_clicked"] = None
                except Exception:
                    pass
                st.rerun()
            else:
                st.session_state[tol_key] = True
        finally:
            try:
                st.session_state.setdefault(map_key, {})
                st.session_state[map_key]["last_clicked"] = None
            except Exception:
                pass

    # ---------------------------
    # Map click for ENDPOINT SNAPPING
    # ---------------------------
    last_click = _get_last_click()
    # Use the resolved selected_geom
    if last_click and selected_geom and place_mode in ("2. Set Start", "3. Set End"):
        try:
            metrics = _precompute_metrics(selected_geom)
            snapped = _snap(last_click, selected_geom, metrics)
            if place_mode == "2. Set Start":
                st.session_state[ti_key]["start_point"] = snapped
                st.session_state[f"{key_prefix}selected_start_point"] = snapped
                if isinstance(package, dict):
                    package["start_point"] = snapped
            elif place_mode == "3. Set End":
                st.session_state[ti_key]["end_point"] = snapped
                st.session_state[f"{key_prefix}selected_end_point"] = snapped
                if isinstance(package, dict):
                    package["end_point"] = snapped
        finally:
            try:
                st.session_state.setdefault(map_key, {})
                st.session_state[map_key]["last_clicked"] = None
            except Exception:
                pass

    # ------------------------------------------------------
    # Build map (always fit to area)
    # ------------------------------------------------------
    m = folium.Map(control_scale=True)

    # ─────────────────────────────────────────────────────────────
    # PROJECT GEOMETRY — render FIRST so it sits UNDER routes & markers
    # (Wrap in a non-controllable FeatureGroup to keep it out of LayerControl)
    # ─────────────────────────────────────────────────────────────
    FOOTPRINT_COLOR = "#3388ff"
    FOOTPRINT_LINE_WEIGHT = 15
    FOOTPRINT_LINE_OPACITY = 0.70
    FOOTPRINT_POLY_WEIGHT = 4
    FOOTPRINT_POLY_FILL_OPACITY = 0.30
    FOOTPRINT_TOOLTIP = "PROJECT FOOTPRINT"

    footprint_fg = folium.FeatureGroup(
        name="(internal) Footprint",
        show=True,
        control=False
    ).add_to(m)

    try:
        apex_geom_ctx = st.session_state.get("apex_geom") or {}
        geom_type_raw = (
            apex_geom_ctx.get("type")
            or st.session_state.get("geom_type")
            or st.session_state.get("apex_proj_type")
            or ""
        )
        geom_type = str(geom_type_raw).strip().lower()
        apex_parts = apex_geom_ctx.get("geoms") or []
        proj_area_fallback = st.session_state.get("apex_proj_area")  # rings list if available

        def _is_pair(x):
            return isinstance(x, (list, tuple)) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x)

        if geom_type in ("route", "line", "linestring"):
            if apex_parts:
                geometry_to_folium(
                    {"paths": apex_parts},
                    color=FOOTPRINT_COLOR,
                    weight=FOOTPRINT_LINE_WEIGHT,
                    opacity=FOOTPRINT_LINE_OPACITY,
                    tooltip=FOOTPRINT_TOOLTIP,
                    feature_type="line",
                ).add_to(footprint_fg)
        elif geom_type in ("boundary", "polygon", "area"):
            rings = apex_parts if apex_parts else (proj_area_fallback or [])
            if rings:
                geometry_to_folium(
                    {"rings": rings},
                    color=FOOTPRINT_COLOR,
                    weight=FOOTPRINT_POLY_WEIGHT,
                    fill=True,
                    fill_opacity=FOOTPRINT_POLY_FILL_OPACITY,
                    tooltip=FOOTPRINT_TOOLTIP,
                    feature_type="polygon",
                ).add_to(footprint_fg)
        elif geom_type in ("site", "point"):
            pts = [p for p in apex_parts if _is_pair(p)]
            if pts:
                geometry_to_folium(
                    pts,
                    feature_type="point",
                    icon=folium.Icon(color="blue"),
                    tooltip=FOOTPRINT_TOOLTIP,
                ).add_to(footprint_fg)
        else:
            # Smart fallback: use project area if present; else try lines; else points
            if proj_area_fallback:
                geometry_to_folium(
                    {"rings": proj_area_fallback},
                    color=FOOTPRINT_COLOR,
                    weight=FOOTPRINT_POLY_WEIGHT,
                    fill=True,
                    fill_opacity=FOOTPRINT_POLY_FILL_OPACITY,
                    tooltip=FOOTPRINT_TOOLTIP,
                    feature_type="polygon",
                ).add_to(footprint_fg)
            elif apex_parts:
                if isinstance(apex_parts[0], (list, tuple)) and apex_parts and apex_parts[0] and isinstance(apex_parts[0][0], (list, tuple)):
                    geometry_to_folium(
                        {"paths": apex_parts},
                        color=FOOTPRINT_COLOR,
                        weight=FOOTPRINT_LINE_WEIGHT,
                        opacity=FOOTPRINT_LINE_OPACITY,
                        tooltip=FOOTPRINT_TOOLTIP,
                        feature_type="line",
                    ).add_to(footprint_fg)
                else:
                    pts = [p for p in apex_parts if _is_pair(p)]
                    if pts:
                        geometry_to_folium(
                            pts,
                            feature_type="point",
                            icon=folium.Icon(color="blue"),
                            tooltip=FOOTPRINT_TOOLTIP,
                        ).add_to(footprint_fg)
    except Exception:
        # Legacy keys fallback (also CAPITALIZED tooltip and corrected opacity=0.70)
        project_geom_display = st.session_state.get("project_geometry")
        project_geom_type = (st.session_state.get("project_geometry_type") or "").lower()
        if project_geom_display and project_geom_type:
            if project_geom_type in ("point",):
                geometry_to_folium(
                    project_geom_display,
                    feature_type="point",
                    icon=folium.Icon(color="blue"),
                    tooltip=FOOTPRINT_TOOLTIP
                ).add_to(footprint_fg)
            elif project_geom_type in ("line", "linestring"):
                geometry_to_folium(
                    project_geom_display,
                    feature_type="line",
                    color=FOOTPRINT_COLOR,
                    weight=FOOTPRINT_LINE_WEIGHT,
                    opacity=FOOTPRINT_LINE_OPACITY,  # was 0.07 → now 0.70
                    tooltip=FOOTPRINT_TOOLTIP
                ).add_to(footprint_fg)
            elif project_geom_type in ("polygon",):
                geometry_to_folium(
                    project_geom_display,
                    feature_type="polygon",
                    color=FOOTPRINT_COLOR,
                    weight=FOOTPRINT_POLY_WEIGHT,
                    opacity=0.8,
                    fill=True,
                    fill_opacity=FOOTPRINT_POLY_FILL_OPACITY,
                    tooltip=FOOTPRINT_TOOLTIP
                ).add_to(footprint_fg)
        elif project_geom_display:
            geometry_to_folium(
                project_geom_display,
                feature_type="line",
                color=FOOTPRINT_COLOR,
                weight=FOOTPRINT_LINE_WEIGHT,
                opacity=FOOTPRINT_LINE_OPACITY,
                tooltip=FOOTPRINT_TOOLTIP
            ).add_to(footprint_fg)

    # ---------------------------
    # Routes (when selecting) OR the selected route (other modes)
    # (drawn AFTER project footprint so they appear above it)
    # ---------------------------
    if place_mode == "1. Select Route":
        for r in results:
            attrs = r.get("attributes") or {}
            rid = attrs.get("Route_ID")
            geom = r.get("geometry") or []
            if not rid or not geom:
                continue
            feature = {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": geom},
                "properties": {"Route_ID": rid, "Route_Name": attrs.get("Route_Name")},
            }
            base_color = "#e53935" if rid == selected_id else "#6c757d"
            base_weight = 6 if rid == selected_id else 3
            # non-selected gray routes “transparent” but still clickable
            base_opacity = 1.0 if rid == selected_id else 0.20

            def _style_factory(color, weight, opacity):
                return {"color": color, "weight": weight, "opacity": opacity}

            # Keep strong red highlight on hover so gray routes light up
            highlight = lambda f: {"color": "#e53935", "weight": 6, "opacity": 1.0}
            folium.GeoJson(
                data=feature,
                style_function=lambda f, c=base_color, w=base_weight, o=base_opacity: _style_factory(c, w, o),
                highlight_function=highlight,
                tooltip=folium.Tooltip(f"ROUTE {rid}: {attrs.get('Route_Name')}"),  # CAPITALIZED
                name=f"route_{rid}",
            ).add_to(m)
    else:
        if selected_id and selected_geom:
            # Keep the selected route OUT of the LayerControl by drawing it inside a non-controllable FeatureGroup
            route_fg = folium.FeatureGroup(
                name="(internal) Selected Route",
                show=True,
                control=False
            ).add_to(m)
            # Draw the selected route using PolyLine(s) (avoid anonymous GeoJson layers)
            try:
                # selected_geom can be:
                # • list of [lon, lat] -> single path
                # • dict with {"paths": [...]} -> one or more paths
                if isinstance(selected_geom, dict) and isinstance(selected_geom.get("paths"), list):
                    paths = selected_geom["paths"]
                elif isinstance(selected_geom, list) and selected_geom and isinstance(selected_geom[0], (list, tuple)):
                    paths = [selected_geom]  # wrap single path
                else:
                    paths = []
                drew_any = False
                for path in paths:
                    if not path or not isinstance(path[0], (list, tuple)) or len(path[0]) != 2:
                        continue
                    latlon = [[p[1], p[0]] for p in path if isinstance(p, (list, tuple)) and len(p) == 2]
                    if not latlon:
                        continue
                    folium.PolyLine(
                        locations=latlon,
                        color="#e53935",
                        weight=6,
                        opacity=1.0,
                    ).add_to(route_fg)
                    drew_any = True

                # Fallback: if path parsing failed, try geometry_to_folium inside the same non-controllable group
                if not drew_any:
                    try:
                        geometry_to_folium(
                            selected_geom,
                            color="#e53935",
                            weight=6,
                            opacity=1.0,
                            tooltip=(
                                f"ROUTE {fmt_string(selected_id)}: "
                                f"{fmt_string(st.session_state.get(sel_name_key, '') or id_to_name.get(selected_id, ''))}"
                            ),
                            feature_type="line",
                        ).add_to(route_fg)
                    except Exception:
                        pass
            except Exception:
                # As a last resort, do nothing (never break core flow)
                pass

    # ---------------------------
    # Markers: Start/End points (always on top) — CAPITALIZED tooltips
    # ---------------------------
    start_pt = (
        st.session_state.get(f"{key_prefix}selected_start_point")
        or st.session_state.get(ti_key, {}).get("start_point")
    )
    end_pt = (
        st.session_state.get(f"{key_prefix}selected_end_point")
        or st.session_state.get(ti_key, {}).get("end_point")
    )

    if isinstance(start_pt, dict) and start_pt.get("lonlat"):
        geometry_to_folium(
            [start_pt["lonlat"]],
            feature_type="point",
            icon=folium.Icon(color="green", icon="play", prefix="fa"),
            tooltip="START"
        ).add_to(m)

    if isinstance(end_pt, dict) and end_pt.get("lonlat"):
        geometry_to_folium(
            [end_pt["lonlat"]],
            feature_type="point",
            icon=folium.Icon(color="red", icon="stop", prefix="fa"),
            tooltip="END"
        ).add_to(m)

    # ─────────────────────────────────────────────────────────────
    # MILEPOSTS (REFERENCE ONLY) — Start/End tabs with ANY route selected
    # (Always create the layer + LayerControl on Start/End; populate if records exist)
    # ─────────────────────────────────────────────────────────────
    if place_mode in ("2. Set Start", "3. Set End") and selected_id and selected_geom:
        try:
            # Create a togglable overlay layer; default OFF in the layer control
            mp_group = folium.FeatureGroup(name="Mileposts", show=False).add_to(m)

            # Attempt to populate from service
            mp_records = []
            try:
                mp_records = get_mileposts_for_route(str(selected_id)) or []
            except Exception:
                mp_records = []

            # Optional developer/testing injection:
            if not mp_records:
                dev_key = f"{key_prefix}__mileposts_debug"
                test = st.session_state.get(dev_key)
                if isinstance(test, list):
                    mp_records = test

            # Render labels if any
            for rec in mp_records or []:
                lon = rec.get("lon") or rec.get("x")
                lat = rec.get("lat") or rec.get("y")
                lab = rec.get("label") or rec.get("mp") or rec.get("milepost") or rec.get("text")
                if isinstance(lon, (int, float)) and isinstance(lat, (int, float)) and lab is not None:
                    html = f"""
                    <div style="
                        pointer-events:none;
                        display:inline-flex;
                        align-items:center;
                        justify-content:center;
                        background:#ffffff;
                        border:1px solid #000;
                        border-radius:3px;
                        padding:1px 4px;
                        min-width:14px;
                        max-width:40px;
                        font-size:10px;
                        font-weight:700;
                        color:#000;
                        line-height:1.1;
                        white-space:nowrap;
                        overflow:hidden;
                        text-overflow:ellipsis;
                    ">
                        {lab}
                    </div>
                    """
                    try:
                        folium.Marker(
                            location=[float(lat), float(lon)],
                            icon=folium.DivIcon(
                                html=html,
                                icon_anchor=(0, 0),
                            ),
                            tooltip=None
                        ).add_to(mp_group)
                    except Exception:
                        pass

            # Always show the LayerControl on Start/End (even if the layer is empty)
            folium.LayerControl(position="topright", collapsed=False).add_to(m)
        except Exception:
            # Never break core flow because of milepost overlay
            pass

    # ---- FIT-BOUNDS ----
    # New priority:
    # 1) Start + End
    # 2) Only Start or Only End
    # 3) Selected Route
    # 4) Project Area (fallback)
    def _valid_point_obj(pt: dict) -> bool:
        return (
            isinstance(pt, dict)
            and isinstance(pt.get("lonlat"), (list, tuple))
            and len(pt["lonlat"]) == 2
            and all(isinstance(v, (int, float)) for v in pt["lonlat"])
        )

    def _bounds_from_points(points_lonlat):
        if not points_lonlat:
            return None
        xs = [p[0] for p in points_lonlat]
        ys = [p[1] for p in points_lonlat]
        west, east = min(xs), max(xs)
        south, north = min(ys), max(ys)
        return [[south, west], [north, east]]

    def _compute_bounds(geom):
        if not geom:
            return None
        try:
            # Flatten rings/parts into a single list of [lon, lat]
            def _iter_coords(g):
                if isinstance(g, dict):
                    g = g.get("coordinates") or g.get("lonlat") or []
                if isinstance(g, (list, tuple)):
                    for item in g:
                        if (
                            isinstance(item, (list, tuple))
                            and len(item) == 2
                            and all(isinstance(v, (int, float)) for v in item)
                        ):
                            yield item
                        else:
                            for sub in _iter_coords(item):
                                yield sub

            xs, ys = [], []
            for lon, lat in _iter_coords(geom):
                xs.append(lon)
                ys.append(lat)
            if not xs or not ys:
                return None
            west, east = min(xs), max(xs)
            south, north = min(ys), max(ys)
            return [[south, west], [north, east]]
        except Exception:
            return None

    bounds = None
    PAD = 1e-5  # tiny padding to avoid too-tight boxes

    # 1) Start + End
    if _valid_point_obj(start_pt) and _valid_point_obj(end_pt):
        s_lon, s_lat = start_pt["lonlat"]
        e_lon, e_lat = end_pt["lonlat"]
        south, north = min(s_lat, e_lat), max(s_lat, e_lat)
        west, east = min(s_lon, e_lon), max(s_lon, e_lon)
        bounds = [[south - PAD, west - PAD], [north + PAD, east + PAD]]
    # 2) Only Start or Only End
    elif _valid_point_obj(start_pt):
        lon, lat = start_pt["lonlat"]
        bounds = [[lat - PAD, lon - PAD], [lat + PAD, lon + PAD]]
    elif _valid_point_obj(end_pt):
        lon, lat = end_pt["lonlat"]
        bounds = [[lat - PAD, lon - PAD], [lat + PAD, lon + PAD]]
    # 3) Selected Route
    elif selected_geom:
        bounds = _compute_bounds(selected_geom)
    # 4) Project Area (fallback)
    if not bounds:
        preferred_fit_geom = (
            st.session_state.get("apex_proj_area")
            or st.session_state.get(fit_geom_key)
            or area_for_display
            or st.session_state.get("impact_area")  # ← guaranteed fallback
        )
        bounds = _compute_bounds(preferred_fit_geom)

    if bounds:
        m.fit_bounds(bounds)

    # Render (only last_clicked is returned; dragging the map won't trigger reruns)
    _ = st_folium(
        m,
        use_container_width=True,
        height=520,
        key=map_key,
        returned_objects=["last_clicked"],  # only clicks; no bounds/zoom subscription
    )

    # Clear tolerance message & geometry-change notice if set
    if st.session_state.get(tol_key):
        st.info("Click closer to a route (within ~100m).")
        st.session_state.pop(tol_key, None)

    # -----------------------------------------------------
    # Return the package (ensure IDs are preserved; name from route_name)
    # -----------------------------------------------------
    ti_final = st.session_state.get(ti_key, {}) or {}
    if isinstance(package, dict):
        # Update only the managed fields
        for k in ("route_id", "route_name", "route_geom", "start_point", "end_point"):
            if ti_final.get(k) is not None:
                package[k] = ti_final.get(k)

        # Area: buffered for new; keep passed-in area for existing
        if not is_existing:
            if "area" not in package or package.get("area") is None:
                package["area"] = buffered_area or area_for_display
        else:
            if package_area is not None:
                package["area"] = package_area

        # Ensure IDs present for existing (fallback to cache)
        if is_existing:
            parent_oid = _norm_parent_oid(package) or st.session_state.get(oid_parent_key)
            route_oid, start_oid, end_oid = _norm_child_oids(package)
            route_oid = route_oid or st.session_state.get(oid_route_key)
            start_oid = start_oid or st.session_state.get(oid_start_key)
            end_oid = end_oid or st.session_state.get(oid_end_key)

            if parent_oid is not None:
                package["objectid"] = parent_oid
                for alt in ("OBJECTID", "objectId", "ti_objectid"):
                    package.pop(alt, None)
            if route_oid is not None:
                package["route_objectid"] = route_oid
                for alt in ("routeObjectId", "route_OBJECTID"):
                    package.pop(alt, None)
            if start_oid is not None:
                package["start_objectid"] = start_oid
                for alt in ("startObjectId", "start_OBJECTID"):
                    package.pop(alt, None)
            if end_oid is not None:
                package["end_objectid"] = end_oid
                for alt in ("endObjectId", "end_OBJECTID"):
                    package.pop(alt, None)

        # --- NAME from route_name (existing only) ---
        rn = package.get("route_name")
        if isinstance(rn, str) and rn.strip():
            package["name"] = f"Traffic Impact @ {rn.strip()}"

        # Require route + both points before returning a package
        has_route = bool(ti_final.get("route_id") and ti_final.get("route_geom"))
        has_both_pts = bool(ti_final.get("start_point") and ti_final.get("end_point"))
        if not (has_route and has_both_pts):
            return None

        # Drop disallowed keys
        for drop_key in ("key_prefix", "project_geometry", "project_geometry_type"):
            if drop_key in package:
                package.pop(drop_key, None)

        return package

    # If no incoming dict was provided, produce a minimal output (no ID inference for new)
    out_pkg = {
        "route_id": ti_final.get("route_id"),
        "route_name": ti_final.get("route_name"),
        "route_geom": ti_final.get("route_geom"),
        "start_point": ti_final.get("start_point"),
        "end_point": ti_final.get("end_point"),
        "area": (buffered_area or area_for_display) if not is_existing else package_area,
    }
    return out_pkg






# assuming these helpers exist in your environment
# from your_module import get_multiple_fields, select_record, geometry_to_folium

def select_community(container, key_prefix: str = "", is_existing: bool = False, package=None):
    """
    Updated behavior:
    - Existing communities immediately show their stored point on the map
    - Text input changes do NOT reset or re-render the map
    - Map only responds to clicks (last_clicked subscription only)
    """
    COMMUNITY_ZOOM = 15
    ALASKA_CENTER = [64.5, -152.0]
    ALASKA_ZOOM = 3
    PLACEHOLDER = "— Select a community —"

    pkg = dict(package or {})
    fields = dict(pkg.get("fields") or pkg.get("attributes") or {})
    pkg["fields"] = fields
    pkg["attributes"] = fields
    pkg["point"] = dict(pkg.get("point") or {})

    root = container if container is not None else st.container()
    with root:

        # ========================================================
        # COMMUNITY SELECTION
        # ========================================================
        selected_name = None

        if is_existing:
            selected_name = fields.get("Community_Name")
        else:
            comms_url = (
                st.session_state.get("communities_url")
                or st.session_state.get("dcced_communities_url")
            )
            lyr_idx = int(
                st.session_state.get("communities_layer")
                or st.session_state.get("dcced_communities_layer")
                or 7
            )
            id_field = (
                st.session_state.get("communities_id_field")
                or st.session_state.get("dcced_communities_id_field")
                or "DCCED_CommunityId"
            )

            options = []
            if isinstance(comms_url, str) and comms_url:
                try:
                    rows = get_multiple_fields(
                        comms_url, lyr_idx, ["OverallName", id_field]
                    ) or []
                    for r in rows:
                        if r.get("OverallName") and r.get(id_field) is not None:
                            options.append((r["OverallName"], r[id_field]))
                except Exception:
                    pass

            names = sorted([n for n, _ in options])
            name_to_id = dict(options)

            sel_key = f"{key_prefix}_community_select"
            st.markdown("<h6>SELECT COMMUNITY</h6>", unsafe_allow_html=True)
            selected_display = st.selectbox(
                "Impacted community",
                [PLACEHOLDER] + names,
                key=sel_key,
            )
            st.write("")

            if selected_display != PLACEHOLDER:
                selected_name = selected_display
                fields["Community_Name"] = selected_name
                cid = name_to_id.get(selected_name)

                if comms_url and cid is not None:
                    feats = select_record(
                        url=comms_url,
                        layer=lyr_idx,
                        id_field=id_field,
                        id_value=str(cid),
                        fields="*",
                        return_geometry=True,
                    ) or []

                    if feats:
                        geom = feats[0].get("geometry")
                        if geom and "x" in geom and "y" in geom:
                            pkg["point"] = {
                                "lonlat": [geom["x"], geom["y"]],
                                "lng": geom["x"],
                                "lat": geom["y"],
                            }

        # ========================================================
        # CONTACT INFORMATION
        # ========================================================
        
        st.markdown("<h6>CONTACT INFORMATION</h6>", unsafe_allow_html=True)

        fields["Community_Contact"] = st.text_input(
            "Contact Name",
            value=fields.get("Community_Contact") or "",
            key=f"{key_prefix}Community_Contact",
        )

        c1, c2 = st.columns(2)
        with c1:
            fields["Community_Contact_Phone"] = st.text_input(
                "Phone",
                value=fields.get("Community_Contact_Phone") or "",
                key=f"{key_prefix}Community_Contact_Phone",
            )
        with c2:
            fields["Community_Contact_Email"] = st.text_input(
                "Email",
                value=fields.get("Community_Contact_Email") or "",
                key=f"{key_prefix}Community_Contact_Email",
            )

        # ========================================================
        # MAP
        # ========================================================
        if selected_name or is_existing:
            lat = pkg.get("point", {}).get("lat")
            lng = pkg.get("point", {}).get("lng")

            if isinstance(lat, (float, int)) and isinstance(lng, (float, int)):
                start_location = [lat, lng]
                start_zoom = COMMUNITY_ZOOM
            else:
                start_location = ALASKA_CENTER
                start_zoom = ALASKA_ZOOM

            m = folium.Map(
                location=start_location,
                zoom_start=start_zoom,
                control_scale=True,
            )

            # Existing point
            if isinstance(lat, (float, int)) and isinstance(lng, (float, int)):
                geometry_to_folium(
                    {"x": lng, "y": lat},
                    icon=folium.Icon(color="pink", icon="home", prefix="fa"),
                ).add_to(m)

            st.write("")
            st.markdown("<h6>COMMUNITY LOCATION</h6>", unsafe_allow_html=True)
            result = st_folium(
                m,
                use_container_width=True,
                height=500,
                key=f"{key_prefix}community_map",
                returned_objects=["last_clicked"],
            )

            # Only update on map click
            if result and result.get("last_clicked"):
                pt = result["last_clicked"]
                if "lat" in pt and "lng" in pt:
                    pkg["point"] = {
                        "lonlat": [pt["lng"], pt["lat"]],
                        "lng": pt["lng"],
                        "lat": pt["lat"],
                    }

        pkg["fields"] = fields
        pkg["attributes"] = fields
        return pkg
