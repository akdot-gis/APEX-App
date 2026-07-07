from typing import List, Sequence, Tuple, Literal
from math import hypot

from shapely.geometry import (
    Point as ShapelyPoint,
    LineString as ShapelyLineString,
    MultiLineString as ShapelyMultiLineString,
    Polygon as ShapelyPolygon,
    MultiPolygon as ShapelyMultiPolygon,
)
from shapely.ops import transform
import pyproj


from typing import List, Sequence, Tuple, Literal
from shapely.geometry import (
    Point as ShapelyPoint,
    LineString as ShapelyLineString,
    MultiLineString as ShapelyMultiLineString,
    Polygon as ShapelyPolygon,
    MultiPolygon as ShapelyMultiPolygon,
)
from shapely.ops import transform, unary_union
import pyproj


from shapely.geometry import (
    Point as ShapelyPoint,
    LineString as ShapelyLineString,
    Polygon as ShapelyPolygon,
    MultiPolygon,
)
from shapely.ops import transform, unary_union, polygonize
import pyproj


def create_buffers(
    geometry_list,
    geom_type,
    distance_m,
    *,
    crs_in="EPSG:4326",
    crs_projected="EPSG:3338",
    crs_out="EPSG:4326",
    cap_style="round",
    join_style="round",
    resolution=16,
):
    """
    Create buffers for points, lines, or polygons and return a
    unified polygon footprint consistent across geometry types.
    """
    if not geometry_list:
        raise ValueError("geometry_list must not be empty")

    # CRS transforms
    to_projected = pyproj.Transformer.from_crs(
        crs_in, crs_projected, always_xy=True
    ).transform
    to_geographic = pyproj.Transformer.from_crs(
        crs_projected, crs_out, always_xy=True
    ).transform

    # Normalize geometries
    shapely_geoms = []
    gt = (geom_type or "").lower()

    for g in geometry_list:
        if isinstance(g, (ShapelyPoint, ShapelyLineString, ShapelyPolygon)):
            shp = g
        else:
            if gt == "point":
                shp = ShapelyPoint(g)

            elif gt in ("line", "linestring"):
                shp = ShapelyLineString(g)

            elif gt == "polygon":
                # Accept either:
                #   - a single ring: [[x,y], [x,y], ...]
                #   - rings wrapper: [ [[x,y], ...] ]  (ESRI-like)
                ring = g
                if (
                    isinstance(g, (list, tuple))
                    and len(g) > 0
                    and isinstance(g[0], (list, tuple))
                    and len(g[0]) > 0
                    and isinstance(g[0][0], (list, tuple))
                ):
                    ring = g[0]

                shp = ShapelyPolygon(ring)

            else:
                raise ValueError(f"Unsupported geometry type: {geom_type}")

        # IMPORTANT: always append, including when input is already a Shapely geometry
        shapely_geoms.append(shp)

    # Buffer each geometry independently (in projected CRS)
    cap_map = {"round": 1, "flat": 2, "square": 3}
    join_map = {"round": 1, "mitre": 2, "miter": 2, "bevel": 3}

    if cap_style not in cap_map:
        raise ValueError(f"Unsupported cap_style: {cap_style}")
    if join_style not in join_map:
        raise ValueError(f"Unsupported join_style: {join_style}")

    buffered = []
    for shp in shapely_geoms:
        projected = transform(to_projected, shp)
        buf = projected.buffer(
            distance_m,
            resolution=resolution,
            cap_style=cap_map[cap_style],
            join_style=join_map[join_style],
        )
        if not buf.is_empty:
            buffered.append(buf)

    if not buffered:
        raise ValueError("Buffering produced no geometry (all buffers were empty)")

    # Dissolve buffers (unified footprint)
    merged = unary_union(buffered)

    # Reproject back to lon/lat
    merged = transform(to_geographic, merged)

    # Enforce validity AFTER reprojection
    if merged.is_empty:
        raise ValueError("Buffered geometry resulted in an empty shape after reprojection")

    if not merged.is_valid:
        # Standard Shapely fix for minor self-intersections, ring issues, etc.
        merged = merged.buffer(0)

    if merged.is_empty or not merged.is_valid:
        raise ValueError("Buffered geometry is invalid after normalization/repair")

    # Extract exterior rings ONLY (ESRI polygon rings)
    polygons = []
    if isinstance(merged, MultiPolygon):
        polygons = list(merged.geoms)
    elif isinstance(merged, ShapelyPolygon):
        polygons = [merged]
    else:
        # Handle GeometryCollection or other polygonal containers defensively
        if hasattr(merged, "geoms"):
            polygons = [g for g in merged.geoms if isinstance(g, ShapelyPolygon)]

    if not polygons:
        raise ValueError(f"Buffered result is not polygonal (type={type(merged).__name__})")

    rings = []
    for poly in polygons:
        if poly.is_empty:
            continue

        coords = list(poly.exterior.coords)
        # ESRI ring must have at least 4 points (closed ring; first==last)
        if len(coords) < 4:
            continue

        rings.append([[float(x), float(y)] for x, y in coords])

    if not rings:
        raise ValueError("No valid exterior rings could be extracted from buffered polygons")

    return rings




def center_of_geometry(
    geometry_list: List[Sequence],
    geom_type: Literal["Point", "LineString", "Polygon", "point", "line", "linestring", "polygon"],
) -> Tuple[float, float]:
    """
    Compute a representative center in [lon, lat] for the submitted geometry list, behaving
    like the old GeometryUtil.center(...) pattern:
      - Accepts a list of inputs of the specified type (Point, LineString, Polygon).
      - Each input can be a raw coordinate list OR a Shapely geometry.
      - If multiple geometries are supplied, returns the average of their per-geometry centers.
      - Coordinate order is always (lon, lat).

    Parameters
    ----------
    geometry_list : list
        List of geometries. Coordinates must be [lon, lat].
        - Points:      [lon, lat] OR [[lon, lat]]
        - LineString:  [[lon, lat], [lon, lat], ...] OR list of such lines
        - Polygon:     [[lon, lat], ..., [lon, lat]] OR list of such rings (closed or open)
    geom_type : {"Point","LineString","Polygon"} (case-insensitive; "Line" also allowed)

    Returns
    -------
    (lon, lat) : Tuple[float, float]
        A single center point representing all provided geometries.
    """

    if not geometry_list:
        raise ValueError("geometry_list is empty")

    gt = (geom_type or "").lower()
    if gt in ("line", "linestring"):
        gt = "linestring"
    elif gt in ("point", "polygon"):
        pass
    else:
        raise ValueError("geom_type must be 'Point', 'LineString', or 'Polygon'")

    def _is_lonlat_pair(v) -> bool:
        return isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) for x in v)

    # -------------------------
    # Point helpers
    # -------------------------
    def _flatten_points_like(points_input) -> List[Tuple[float, float]]:
        flat = []
        # Shapely Point
        if isinstance(points_input, ShapelyPoint):
            flat.append((float(points_input.x), float(points_input.y)))
            return flat

        # [lon, lat]
        if _is_lonlat_pair(points_input):
            lon, lat = points_input  # type: ignore
            flat.append((float(lon), float(lat)))
            return flat

        # [[lon, lat], ...] or nested list
        if isinstance(points_input, (list, tuple)):
            for item in points_input:
                if _is_lonlat_pair(item):
                    lon, lat = item  # type: ignore
                    flat.append((float(lon), float(lat)))
                elif isinstance(item, (list, tuple)):
                    for pt in item:
                        if _is_lonlat_pair(pt):
                            lon, lat = pt  # type: ignore
                            flat.append((float(lon), float(lat)))
        return flat

    def _point_center(points_any) -> Tuple[float, float]:
        pts = _flatten_points_like(points_any)
        if not pts:
            raise ValueError("No valid point data found.")
        if len(pts) == 1:
            return pts[0]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    # -------------------------
    # Line helpers
    # -------------------------
    def _is_shapely_linestring(obj) -> bool:
        return isinstance(obj, ShapelyLineString)

    def _is_shapely_multiline(obj) -> bool:
        return isinstance(obj, ShapelyMultiLineString)

    def _center_single_line_coords(coords: Sequence[Sequence[float]]) -> Tuple[float, float]:
        if not isinstance(coords, (list, tuple)) or len(coords) == 0:
            raise ValueError("Invalid line geometry.")
        if len(coords) == 1:
            lon, lat = coords[0]
            return (float(lon), float(lat))

        total = 0.0
        for i in range(len(coords) - 1):
            a = coords[i]; b = coords[i + 1]
            total += hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))

        target = total / 2.0
        d = 0.0
        for i in range(len(coords) - 1):
            a = coords[i]; b = coords[i + 1]
            seg = hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))
            if seg > 0.0 and d + seg >= target:
                t = (target - d) / seg
                lon = float(a[0]) + t * (float(b[0]) - float(a[0]))
                lat = float(a[1]) + t * (float(b[1]) - float(a[1]))
                return (lon, lat)
            d += seg
        lon, lat = coords[-1]
        return (float(lon), float(lat))

    def _line_center(line_any) -> Tuple[float, float]:
        # Shapely MultiLineString -> average of per-line centers
        if _is_shapely_multiline(line_any):
            centers = [_line_center(ls) for ls in line_any.geoms]
            return _average_centers(centers)

        # Shapely LineString
        if _is_shapely_linestring(line_any):
            mid = line_any.interpolate(line_any.length / 2.0)
            return (float(mid.x), float(mid.y))

        # List-like:
        # - Single line: [[lon, lat], ...]
        # - Multi lines: [[[lon,lat],...], [[lon,lat],...], ...]
        if isinstance(line_any, (list, tuple)) and len(line_any) > 0:
            first = line_any[0]
            if _is_lonlat_pair(first):
                return _center_single_line_coords(line_any)  # type: ignore
            if isinstance(first, (list, tuple)) and len(first) > 0 and _is_lonlat_pair(first[0]):
                centers = [_center_single_line_coords(line) for line in line_any]  # type: ignore
                return _average_centers(centers)

        # Fallback attempt
        return _center_single_line_coords(line_any)

    # -------------------------
    # Polygon helpers
    # -------------------------
    def _is_shapely_polygon(obj) -> bool:
        return isinstance(obj, ShapelyPolygon)

    def _is_shapely_multipolygon(obj) -> bool:
        return isinstance(obj, ShapelyMultiPolygon)

    def _center_single_polygon_coords(ring: Sequence[Sequence[float]]) -> Tuple[float, float]:
        if len(ring) < 1:
            raise ValueError("Empty polygon ring")

        if len(ring) < 3:
            # Handle degenerate inputs gracefully (same as previous behavior)
            if len(ring) == 1:
                lon, lat = ring[0]
                return (float(lon), float(lat))
            if len(ring) == 2:
                (lon1, lat1), (lon2, lat2) = ring
                return ((float(lon1) + float(lon2)) / 2.0, (float(lat1) + float(lat2)) / 2.0)

        # Ensure closed
        closed = list(ring)
        if closed[0] != closed[-1]:
            closed = closed + [closed[0]]

        xs = [float(p[0]) for p in closed]
        ys = [float(p[1]) for p in closed]

        # Shoelace centroid
        A = 0.0
        Cx = 0.0
        Cy = 0.0
        for i in range(len(closed) - 1):
            cross = xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
            A += cross
            Cx += (xs[i] + xs[i + 1]) * cross
            Cy += (ys[i] + ys[i + 1]) * cross
        A *= 0.5

        if A == 0.0:
            # Degenerate: average vertices (excluding duplicate last)
            lon_avg = sum(xs[:-1]) / (len(xs) - 1)
            lat_avg = sum(ys[:-1]) / (len(ys) - 1)
            return (lon_avg, lat_avg)

        Cx /= (6.0 * A)
        Cy /= (6.0 * A)
        return (Cx, Cy)

    def _polygon_center(poly_any) -> Tuple[float, float]:
        # Shapely MultiPolygon -> average of per-polygon centroids
        if _is_shapely_multipolygon(poly_any):
            centers = [_polygon_center(pg) for pg in poly_any.geoms]
            return _average_centers(centers)

        # Shapely Polygon
        if _is_shapely_polygon(poly_any):
            c = poly_any.centroid
            return (float(c.x), float(c.y))

        # List-like:
        # - Single polygon ring: [[lon, lat], ...]
        # - Multiple polygons: [[[lon,lat],...], [[lon,lat],...], ...]
        if isinstance(poly_any, (list, tuple)) and len(poly_any) > 0:
            first = poly_any[0]
            if _is_lonlat_pair(first):
                return _center_single_polygon_coords(poly_any)  # type: ignore
            if isinstance(first, (list, tuple)) and len(first) > 0 and _is_lonlat_pair(first[0]):
                centers = [_center_single_polygon_coords(pg) for pg in poly_any]  # type: ignore
                return _average_centers(centers)

        # Fallback attempt
        return _center_single_polygon_coords(poly_any)

    # -------------------------
    # General helpers
    # -------------------------
    def _average_centers(centers: List[Tuple[float, float]]) -> Tuple[float, float]:
        xs = [c[0] for c in centers]
        ys = [c[1] for c in centers]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    # -------------------------
    # Dispatch over the list
    # -------------------------
    per_item_centers: List[Tuple[float, float]] = []
    if gt == "point":
        # All provided items are considered parts of the same point collection,
        # consistent with prior behavior -> compute one center from ALL points.
        all_points: List[Tuple[float, float]] = []
        for item in geometry_list:
            all_points.extend(_flatten_points_like(item))
        if not all_points:
            raise ValueError("No valid point data found.")
        if len(all_points) == 1:
            return all_points[0]
        return _average_centers(all_points)

    elif gt == "linestring":
        # Compute center per item (line or list-of-lines), then average
        for item in geometry_list:
            per_item_centers.append(_line_center(item))
        return _average_centers(per_item_centers)

    elif gt == "polygon":
        # Compute center per item (polygon or list-of-polygons), then average
        for item in geometry_list:
            per_item_centers.append(_polygon_center(item))
        return _average_centers(per_item_centers)

    # Unreachable
    raise ValueError("Unsupported geometry type")



def snap_bop_eop_to_route(route_geom, bop_lonlat, eop_lonlat):
    """
    Snap BOP/EOP to the closest segment of the given AGOL route geometry.

    Inputs:
      - route_geom: ArcGIS polyline geometry; supports:
          * {"paths": [[[lon, lat], ...], ...]}
          * [[lon, lat], ...]  (single part)
          * [ [[lon,lat],...], [[lon,lat],...] ]  (multi-part)
      - bop_lonlat: [lon, lat]
      - eop_lonlat: [lon, lat]

    Returns:
      (snapped_bop_lonlat, snapped_eop_lonlat, chosen_part_coords)
      chosen_part_coords is a single polyline part as [[lon,lat], ...]
    """
    # ---- normalize into list of parts ----
    parts = []
    if isinstance(route_geom, dict) and "paths" in route_geom:
        for p in route_geom.get("paths") or []:
            coords = []
            for xy in p or []:
                try:
                    coords.append([float(xy[0]), float(xy[1])])
                except Exception:
                    continue
            if len(coords) >= 2:
                parts.append(coords)
    elif isinstance(route_geom, (list, tuple)):
        # single path [[lon,lat], ...] or multi-part [ [[lon,lat],...], ... ]
        if all(isinstance(v, (list, tuple)) and len(v) == 2 for v in route_geom):
            coords = []
            for xy in route_geom:
                try:
                    coords.append([float(xy[0]), float(xy[1])])
                except Exception:
                    continue
            if len(coords) >= 2:
                parts.append(coords)
        else:
            for p in route_geom:
                if not isinstance(p, (list, tuple)):
                    continue
                coords = []
                for xy in p:
                    try:
                        coords.append([float(xy[0]), float(xy[1])])
                    except Exception:
                        continue
                if len(coords) >= 2:
                    parts.append(coords)

    if not parts:
        return bop_lonlat, eop_lonlat, []

    # ---- choose the best part minimizing sum of distances to bop/eop ----
    from shapely.geometry import LineString, Point  # you said you'll handle imports
    from shapely.ops import nearest_points

    try:
        bpt = Point(float(bop_lonlat[0]), float(bop_lonlat[1]))
        ept = Point(float(eop_lonlat[0]), float(eop_lonlat[1]))
    except Exception:
        return bop_lonlat, eop_lonlat, (parts[0] if parts else [])

    best_idx, best_sum = None, float("inf")
    for i, p in enumerate(parts):
        ln = LineString([(c[0], c[1]) for c in p])
        try:
            nb = nearest_points(ln, bpt)[0]
            ne = nearest_points(ln, ept)[0]
            dsum = nb.distance(bpt) + ne.distance(ept)
            if dsum < best_sum:
                best_sum, best_idx = dsum, i
        except Exception:
            continue

    if best_idx is None:
        return bop_lonlat, eop_lonlat, (parts[0] if parts else [])

    chosen = parts[best_idx]
    ln = LineString([(c[0], c[1]) for c in chosen])

    try:
        nb = nearest_points(ln, bpt)[0]
        ne = nearest_points(ln, ept)[0]
        snapped_bop = [float(nb.x), float(nb.y)]
        snapped_eop = [float(ne.x), float(ne.y)]
    except Exception:
        snapped_bop, snapped_eop = bop_lonlat, eop_lonlat

    return snapped_bop, snapped_eop, chosen




def slice_route_between_points(route_geom: list, start_point: list, end_point: list) -> list:
    """
    Slice a single-part route (LineString) between two points.

    Parameters
    ----------
    route_geom : list[[lon, lat], ...]
        Vertices of a route linestring in [lon, lat] order.
    start_point : [lon, lat]
        Begin point (not required to lie exactly on the route).
    end_point : [lon, lat]
        End point (not required to lie exactly on the route).

    Returns
    -------
    list[[lon, lat], ...]
        The sliced line segment as a list of [lon, lat] coordinates.
        Returns an empty list if the slice cannot be computed.
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    try:
        line = LineString([(float(x), float(y)) for x, y in route_geom])
        sp = Point(float(start_point[0]), float(start_point[1]))
        ep = Point(float(end_point[0]), float(end_point[1]))

        s_dist = line.project(sp)
        e_dist = line.project(ep)
        d0, d1 = (s_dist, e_dist) if s_dist <= e_dist else (e_dist, s_dist)

        seg = substring(line, d0, d1)
        if not isinstance(seg, LineString) or len(seg.coords) < 2:
            return []
        return [[float(x), float(y)] for (x, y) in seg.coords]
    except Exception:
        return []



def slice_and_buffer_route(route_geom: list, start_point: list, end_point: list, distance_m: int = 50) -> list:
    """
    Slices a route between a start and end point and returns a buffered polygon
    as a list of rings suitable for an ESRI polygon geometry.

    Parameters
    ----------
    route_geom  : list of [lon, lat] pairs representing the full route
    start_point : [lon, lat] snapped to the route line
    end_point   : [lon, lat] snapped to the route line
    distance_m  : buffer distance in meters (default 50)

    Returns
    -------
    list of rings ([[lon, lat], ...]) for use in {"rings": ..., "spatialReference": {"wkid": 4326}}
    """
    from shapely.geometry import LineString, Point
    from shapely.ops import substring

    route_line = LineString(route_geom)

    sp_point = Point(start_point[0], start_point[1])
    ep_point = Point(end_point[0], end_point[1])

    sp_dist = route_line.project(sp_point)
    ep_dist = route_line.project(ep_point)

    start_dist = min(sp_dist, ep_dist)
    end_dist   = max(sp_dist, ep_dist)

    sliced_segment = substring(route_line, start_dist, end_dist)
    sliced_coords  = list(sliced_segment.coords)

    buffer_rings = create_buffers(geometry_list=[sliced_coords], geom_type="line", distance_m=distance_m)
    if not buffer_rings:
        raise RuntimeError("create_buffers produced no output for sliced segment.")

    return buffer_rings




def simplify_geometry(geom, geom_type, tolerance):
    """
    Simplify line or polygon geometry.
    tolerance is in degrees (lat/lon).
    """
    from shapely.geometry import LineString, Polygon

    if geom_type == "line":
        # Expect list-of-paths [[ [lat, lon], ... ]]
        simplified_paths = []

        for path in geom:
            if len(path) < 3:
                simplified_paths.append(path)
                continue

            line = LineString([(pt[1], pt[0]) for pt in path])
            simple = line.simplify(tolerance, preserve_topology=True)

            simplified_paths.append([[y, x] for x, y in simple.coords])

        return simplified_paths

    if geom_type == "polygon":
        # Rings -> Polygon
        outer = [(pt[1], pt[0]) for pt in geom[0]]
        holes = [
            [(pt[1], pt[0]) for pt in ring]
            for ring in geom[1:]
        ] if len(geom) > 1 else []

        poly = Polygon(outer, holes)
        simple = poly.simplify(tolerance, preserve_topology=True)

        return [
            [[y, x] for x, y in simple.exterior.coords]
        ] + [
            [[y, x] for x, y in ring.coords]
            for ring in simple.interiors
        ]

    return geom  # points untouched