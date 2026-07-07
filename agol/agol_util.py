
"""
===============================================================================
AGOL UTILITIES (STREAMLIT) — AUTH, QUERY, AND APPLYEDITS HELPERS
===============================================================================

Purpose:
    Centralizes ArcGIS Online (AGOL) REST API helpers used by the Streamlit app:
      - Token generation (username/password stored in st.session_state)
      - Common query helpers (query_record, select_record, get_multiple_fields,
        get_unique_field_values)
      - Delete helpers (delete_project)
      - Geometry-intersection query wrapper (AGOLQueryIntersect)
      - Feature upload wrapper using applyEdits (AGOLDataLoader)

Key behaviors:
    - Authentication:
        * get_agol_token() requests an AGOL token via generateToken endpoint
        * Uses st.session_state['AGOL_USERNAME'] and ['AGOL_PASSWORD']
    - Querying:
        * SQL-like where queries against /query endpoints
        * Optional geometry return + output spatial reference set to WKID 4326
    - Payload uploads:
        * AGOLDataLoader.add_features() sends applyEdits 'adds' as JSON
        * Parses addResults, aggregates failures, and returns success/message/globalids
    - Geometry intersections:
        * AGOLQueryIntersect supports a single geometry OR a list of geometries
        * Swaps [lat, lon] -> [lon, lat] to match ArcGIS x/y conventions
        * Executes multiple queries and merges unique results

Session-state dependencies (expected at runtime):
    - Credentials (required for all authenticated operations):
        * 'AGOL_USERNAME'
        * 'AGOL_PASSWORD'

Notes:
    - This module performs network requests via requests (HTTP).
    - Errors are surfaced as exceptions in most helpers; some functions return
      False/None to allow callers to implement best-effort cleanup.
    - Spatial reference is consistently treated as WGS84 (WKID 4326).

===============================================================================
"""

import json
import requests
import math
import streamlit as st
import logging
from shapely.geometry import LineString
from shapely.ops import unary_union, linemerge
from typing import Tuple, Optional, List, Dict, Any


# =============================================================================
# IDENTIFIER HELPERS
# =============================================================================
# format_guid():
#   - Normalizes GlobalID/GUID formatting to the ArcGIS curly-brace convention
#   - Accepts either a string or a single-element list of strings
# =============================================================================
def format_guid(value) -> str:
    """
    Ensures a GUID/GlobalID value is in the correct ArcGIS format.

    Accepted input:
        - str: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" (with or without braces)
        - list[str]: single-element list returned by some ArcGIS responses

    Returns:
        str | None:
            - A formatted string like "{xxxxxxxx-...}" when valid
            - None when value is empty/invalid
    """
    # If it's a list, take the first element
    if isinstance(value, list):
        if not value:  # empty list
            return None
        value = value[0]

    if not value or not isinstance(value, str):
        return None

    clean_value = value.strip().lstrip("{").rstrip("}")
    parts = clean_value.split("-")
    if len(parts) != 5 or not all(parts):
        return None

    return f"{{{clean_value}}}"


# =============================================================================
# AUTHENTICATION
# =============================================================================
# get_agol_token():
#   - Requests a short-lived token from AGOL using stored credentials
#   - Required by all subsequent query/edit operations
# =============================================================================
def get_agol_token() -> str:
    """
    Generates an authentication token for ArcGIS Online using a username and password.

    Session-state requirements:
        - st.session_state['AGOL_USERNAME']
        - st.session_state['AGOL_PASSWORD']

    Returns:
        str: A valid authentication token used to make authorized API requests.

    Raises:
        ValueError: If authentication fails or token missing from response.
        ConnectionError: If requests cannot reach the AGOL endpoint.
    """
    # ArcGIS Online token generation URL
    url = "https://www.arcgis.com/sharing/rest/generateToken"

    # Payload required for authentication request
    data = {
        "username": st.session_state['AGOL_USERNAME'],
        "password": st.session_state['AGOL_PASSWORD'],
        "referer": "https://www.arcgis.com",  # Required reference for token generation
        "f": "json"  # Request response format
    }

    try:
        # Send authentication request
        response = requests.post(url, data=data)

        # Validate HTTP response status
        if response.status_code != 200:
            raise Exception(f"Request failed with status code {response.status_code}: {response.text}")

        # Parse JSON response
        token_data = response.json()

        # Extract token if authentication is successful
        if "token" in token_data:
            return token_data["token"]
        elif "error" in token_data:
            raise ValueError(f"Authentication failed: {token_data['error']['message']}")
        else:
            raise ValueError("Unexpected response format: Token not found.")

    except requests.exceptions.RequestException as e:
        # Handle network-related errors
        raise ConnectionError(f"Failed to connect to ArcGIS Online: {e}")
    




# =============================================================================
# QUERY HELPERS (GENERIC)
# =============================================================================
# query_record():
#   - Executes a "where" query against an ArcGIS REST layer /query endpoint
#   - Handles URL normalization to avoid double-appending the layer
# =============================================================================
def query_record(url: str, layer: int, where: str, fields="*", return_geometry=False):
    """
    Executes an SQL-style query against an ArcGIS REST API layer and returns matching records.

    Parameters:
        url: str
            FeatureServer base URL (may or may not already include a layer segment).
        layer: int
            Layer index when url is a FeatureServer root.
        where: str
            SQL-like filter clause (e.g., "GlobalID='...'" or "1=1").
        fields: str
            outFields string. "*" requests all fields.
        return_geometry: bool
            Whether to return geometry in results.

    Returns:
        list: List of 'features' from the ArcGIS REST response.
    """
    token = get_agol_token()
    if not token:
        raise ValueError("Authentication failed: Invalid token.")

    # Normalize URL so we don't double-append the layer
    url = url.rstrip("/")

    # If the URL already ends with the layer number, don't add it again
    if url.split("/")[-1].isdigit():
        query_url = f"{url}/query"
    else:
        query_url = f"{url}/{layer}/query"

    params = {
        "where": where,
        "outFields": fields,
        "returnGeometry": str(return_geometry).lower(),
        "outSR": 4326,
        "f": "json",
        "token": token
    }

    response = requests.get(query_url, params=params)
    if response.status_code != 200:
        raise Exception(
            f"Request failed with status code {response.status_code}: {response.text}"
        )

    data = response.json()
    if "error" in data:
        raise Exception(
            f"API Error: {data['error']['message']} - {data['error'].get('details', [])}"
        )

    return data.get("features", [])




def query_geometry(url: str, layer: int):
    """
    Queries an ArcGIS REST API layer and returns ONLY geometries with inferred type.

    Returns:
        dict:
            {
                "type": "<point|multipoint|polyline|polygon|mixed|none>",
                "geometry": [list of geometry dicts in outSR 4326]
            }
            - "mixed" when multiple geometry types are present.
            - "none" when no geometries are returned.
    """
    token = get_agol_token()
    if not token:
        raise ValueError("Authentication failed: Invalid token.")

    # Normalize URL so we don't double-append the layer
    url = url.rstrip("/")

    # If the URL already ends with the layer number, don't add it again
    if url.split("/")[-1].isdigit():
        query_url = f"{url}/query"
    else:
        query_url = f"{url}/{layer}/query"

    params = {
        "where": "1=1",           # Adjust upstream if you need a specific filter
        "outFields": "OBJECTID",   # Minimal fields to reduce payload
        "returnGeometry": True,
        "outSR": 4326,
        "f": "json",
        "token": token,
    }

    response = requests.get(query_url, params=params)
    if response.status_code != 200:
        raise Exception(
            f"Request failed with status code {response.status_code}: {response.text}"
        )

    data = response.json()
    if "error" in data:
        raise Exception(
            f"API Error: {data['error']['message']} - {data['error'].get('details', [])}"
        )

    features = data.get("features", []) or []

    # Collect geometries only (skip features missing geometry)
    geometries = [f.get("geometry") for f in features if f.get("geometry") is not None]

    # Helper: infer type from an ESRI geometry dict
    def _infer_geom_type(g: dict) -> str:
        # ArcGIS JSON geometry keys: x/y, points, paths, rings
        if not isinstance(g, dict):
            return "unknown"
        if "x" in g and "y" in g:
            return "point"
        if "points" in g:
            return "multipoint"
        if "paths" in g:
            return "polyline"
        if "rings" in g:
            return "polygon"
        return "unknown"

    if not geometries:
        return {"type": "none", "geometry": []}

    inferred = {_infer_geom_type(g) for g in geometries}
    # Remove 'unknown' if there are known types present
    if len(inferred) > 1 and "unknown" in inferred:
        inferred.discard("unknown")

    if not inferred:
        geom_type = "unknown"
    elif len(inferred) == 1:
        geom_type = next(iter(inferred))
    else:
        geom_type = "mixed"

    return {
        "type": geom_type,
        "geometry": geometries
    }





# =============================================================================
# PULL AASHTOWARE GEOMETRY RECORD
# =============================================================================
# UPDATE TEXT
# =============================================================================
def aashtoware_geometry(awp_contract_id):

    points = []

    geom_sel = select_record(
        url=st.session_state['awp_url'],
        layer=st.session_state['awp_geometry_layer'],
        id_field="CONTRACT_Id",
        id_value=st.session_state['awp_id'],
        fields="*",
        return_geometry=True
    )

    st.session_state['debug'] = geom_sel

    for feat in geom_sel or []:
        a = feat.get("attributes", {})
        g = feat.get("geometry", {})
        ptype = a.get("TYPE")

        # Skip rows that don't have a TYPE
        if not ptype:
            continue

        points.append({
            "contract_id": a.get("CONTRACT_Id"),
            "type": ptype,
            "route_id": a.get("Route_Name"),
            "route_name": a.get("Route_Description"),
            "lat": g.get("y"),
            "lon": g.get("x"),
        })

    return points
    



# =============================================================================
# QUERY HELPERS (FIELD VALUE UTILS)
# =============================================================================
# get_unique_field_values():
#   - Requests distinct values for a field using returnDistinctValues=true
#   - Optionally sorts values alphabetically or numerically
# =============================================================================
def get_unique_field_values(
    url: str,
    layer: str,
    field: str,
    where: str = "1=1",
    sort_type: str = None,  # "alpha" or "numeric"
    sort_order: str = "asc"  # "asc" or "desc"
) -> list:
    """
    Queries an ArcGIS REST API layer to retrieve all unique values from a specified field,
    with optional sorting.

    Parameters:
        url: str
            Base URL of the ArcGIS REST API service.
        layer: str
            Layer ID or name to query.
        field: str
            Field name to retrieve distinct values from.
        where: str
            SQL-style filter expression. Defaults to "1=1".
        sort_type: str | None
            "alpha" for alphabetical or "numeric" for numerical sorting.
        sort_order: str
            "asc" or "desc" (default "asc").

    Returns:
        list: Unique values, optionally sorted.

    Raises:
        ValueError: If authentication fails or field does not exist.
        Exception: If request fails or the API returns an error.
    """
    try:
        # Authenticate and get API token (ensure agol_username and agol_password are defined)
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")

        # Construct query parameters
        params = {
            "where": where,
            "outFields": field,
            "returnDistinctValues": "true",  # ensures unique values
            "returnGeometry": "false",  # no geometry needed
            "f": "json",
            "token": token
        }

        # Formulate the query URL and execute the request
        query_url = f"{url}/{layer}/query"
        response = requests.get(query_url, params=params)
        if response.status_code != 200:
            raise Exception(f"Request failed with status code {response.status_code}: {response.text}")

        data = response.json()
        if "error" in data:
            raise Exception(f"API Error: {data['error']['message']} - {data['error'].get('details', [])}")

        # Validate that requested field exists
        available_fields = {field_info["name"] for field_info in data.get("fields", [])}
        if field not in available_fields:
            raise ValueError(f"Field '{field}' does not exist. Available fields: {available_fields}")

        # Extract unique values
        unique_values = []
        for feature in data.get("features", []):
            attributes = feature.get("attributes", {})
            if field in attributes and attributes[field] not in unique_values:
                unique_values.append(attributes[field])

        # Apply sorting if requested
        if sort_type:
            reverse = sort_order.lower() == "desc"
            if sort_type.lower() == "alpha":
                unique_values.sort(key=lambda x: str(x).lower(), reverse=reverse)
            elif sort_type.lower() == "numeric":
                try:
                    unique_values.sort(key=lambda x: float(x), reverse=reverse)
                except ValueError:
                    raise ValueError("Numeric sorting failed: field contains non-numeric values.")

        return unique_values

    except requests.exceptions.RequestException as req_error:
        raise Exception(f"Network error occurred: {req_error}")
    except ValueError as val_error:
        raise ValueError(val_error)
    except Exception as gen_error:
        raise Exception(gen_error)


# =============================================================================
# QUERY HELPERS (BULK FIELD RETRIEVAL)
# =============================================================================
# get_multiple_fields():
#   - Retrieves a set of attributes for all features in a layer
#   - Returns a list of dicts (attribute name -> value)
# =============================================================================
def get_multiple_fields(url: str, layer: int = 0, fields: list = None) -> list:
    """
    Queries an ArcGIS REST API table layer to retrieve records with specified fields.

    Parameters:
        url: str
            Base URL of the ArcGIS REST API service.
        layer: int
            Layer ID to query (default 0).
        fields: list[str] | None
            Field names to request. When None, requests "*".

    Returns:
        list[dict]: Attribute dictionaries for each returned feature.
    """
    try:
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")

        # If no fields provided, request all
        out_fields = ",".join(fields) if fields else "*"
        params = {
            "where": "1=1",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
            "token": token
        }

        query_url = f"{url}/{layer}/query"
        response = requests.get(query_url, params=params)
        if response.status_code != 200:
            raise Exception(f"Request failed with status code {response.status_code}: {response.text}")

        data = response.json()
        if "error" in data:
            raise Exception(f"API Error: {data['error']['message']} - {data['error'].get('details', [])}")

        results = []
        for feature in data.get("features", []):
            attributes = feature.get("attributes", {})
            # Directly use the returned attribute names as dictionary keys
            results.append({k: v for k, v in attributes.items()})

        return results

    except Exception as e:
        raise Exception(f"Error retrieving project records: {e}")


# =============================================================================
# QUERY HELPERS (SINGLE RECORD)
# =============================================================================
# select_record():
#   - Convenience wrapper for retrieving a single record by ID field/value
# =============================================================================
def select_record(url: str, layer: int, id_field: str, id_value: str, fields="*", return_geometry=False):
    """
    Queries an ArcGIS REST API table layer to retrieve a single record by ID field.

    Parameters:
        url: str
            Base URL of the ArcGIS REST API service.
        layer: int
            Layer ID to query.
        id_field: str
            Field name to filter by (e.g., 'GlobalID', 'ProposalId').
        id_value: str
            Value to match in the ID field.
        fields: str
            outFields string ("*" for all fields).
        return_geometry: bool
            Whether to include geometry in response.

    Returns:
        list: List of matching feature dictionaries (ArcGIS REST 'features').
    """
    try:
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")

        params = {
            "where": f"{id_field}='{id_value}'",
            "outFields": fields,
            "returnGeometry": str(return_geometry).lower(),
            "outSR": 4326,
            "f": "json",
            "token": token
        }

        query_url = f"{url}/{layer}/query"
        response = requests.get(query_url, params=params)
        if response.status_code != 200:
            raise Exception(f"Request failed with status code {response.status_code}: {response.text}")

        data = response.json()
        if "error" in data:
            raise Exception(f"API Error: {data['error']['message']} - {data['error'].get('details', [])}")

        return data.get("features", [])

    except Exception as e:
        raise Exception(f"Error retrieving project record: {e}")
    
    



def query_routes_within_buffer(
    buffer_geom,
    *,
    fields=("Route_ID", "Route_Name"),
    include_geometry=True,
    token=None,
):
    """
    Intersect a buffer geometry with AKDOT Roads feature layer and return results.

    Endpoint:
        https://services.arcgis.com/r4A0V7UzH9fcLVvv/arcgis/rest/services/Roads_AKDOT/FeatureServer/0/query

    Parameters
    ----------
    buffer_geom : shapely.geometry.Polygon | shapely.geometry.MultiPolygon | list
        EXPECTED IN [lon, lat] WITHOUT ANY NORMALIZATION.
        - Shapely Polygon/MultiPolygon (exterior used), OR
        - A single ring [[lon, lat], ...] (open or closed), OR
        - A list of rings [[[lon, lat], ...], ...].
        No coordinate reordering, closing, or coercion is performed.
    fields : tuple|list|str
        Fields to return (e.g., ("Route_ID","Route_Name")). '*' allowed.
    include_geometry : bool
        If True, return geometry for each matched route (as [[lon, lat], ...]).
    token : str | None
        AGOL token. If None, will call get_agol_token() if available.

    Returns
    -------
    list[dict]
        Each dict contains:
            {
              "attributes": {...requested fields...},
              "geometry": [[lon, lat], ...]  # if include_geometry and available
            }

    Notes
    -----
    - Inputs are passed through as-is (lon,lat). No normalizers are applied.
    - Output geometries are flattened to a single [[lon,lat], ...] list.
    """

    SERVICE_URL = (
        "https://services.arcgis.com/r4A0V7UzH9fcLVvv/arcgis/rest/services/"
        "Roads_AKDOT/FeatureServer/0/query"
    )

    # Resolve token only (no changes to geometry)
    if token is None:
        try:
            token = get_agol_token()  # optional helper if present
        except NameError:
            token = None

    # Normalize fields to comma-separated string (not a geometry change)
    if isinstance(fields, (list, tuple)):
        out_fields = ",".join(fields)
    else:
        out_fields = str(fields).strip() if fields else "*"

    # Build rings payload in lon,lat — WITHOUT changing coordinates
    rings_lonlat = None

    # Shapely Polygon / MultiPolygon
    is_shapely_like = hasattr(buffer_geom, "geom_type") and hasattr(buffer_geom, "exterior")
    if is_shapely_like:
        geom_type = getattr(buffer_geom, "geom_type", None)
        if geom_type == "Polygon":
            # Shapely exterior returns [(x, y)] which are (lon, lat)
            coords = list(buffer_geom.exterior.coords)
            rings_lonlat = [ [ [float(x), float(y)] for (x, y) in coords ] ]
        elif geom_type == "MultiPolygon":
            rings_lonlat = []
            for poly in buffer_geom.geoms:
                coords = list(poly.exterior.coords)
                rings_lonlat.append([ [float(x), float(y)] for (x, y) in coords ])
        else:
            raise ValueError(f"Unsupported Shapely geometry type: {geom_type}")
    else:
        # Python lists: pass through exactly as provided
        if not isinstance(buffer_geom, list) or len(buffer_geom) == 0:
            raise ValueError("buffer_geom must be a Shapely (Multi)Polygon or a non-empty list.")

        # If it's a single ring [[lon,lat], ...], wrap it in rings; if already [[[lon,lat],...],...], use as-is
        if buffer_geom and isinstance(buffer_geom[0], (list, tuple)) and len(buffer_geom[0]) == 2:
            rings_lonlat = [buffer_geom]  # single ring
        else:
            rings_lonlat = buffer_geom     # multiple rings

    arcgis_polygon = {
        "rings": rings_lonlat,
        "spatialReference": {"wkid": 4326}
    }

    params = {
        "geometry": json.dumps(arcgis_polygon),
        "geometryType": "esriGeometryPolygon",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "where": "1=1",
        "outFields": out_fields,
        "returnGeometry": bool(include_geometry),
        "outSR": 4326,
        "f": "json",
    }
    if token:
        params["token"] = token

    resp = requests.post(SERVICE_URL, data=params)
    if resp.status_code != 200:
        raise Exception(f"Request failed with status code {resp.status_code}: {resp.text}")

    data = resp.json()
    if "error" in data:
        raise Exception(f"API Error: {data['error']['message']} - {data['error'].get('details', [])}")

    features = data.get("features", []) or []

    results = []
    for feat in features:
        packet = {"attributes": feat.get("attributes", {})}

        if include_geometry:
            geom = feat.get("geometry") or {}
            paths = geom.get("paths") or []  # [[[lon,lat],...], ...]
            # Flatten multipart into a single [[lon,lat], ...] list for downstream use
            line_lonlat = []
            for part in paths:
                for xy in part:
                    if isinstance(xy, (list, tuple)) and len(xy) == 2:
                        line_lonlat.append([float(xy[0]), float(xy[1])])
            if line_lonlat:
                packet["geometry"] = line_lonlat

        results.append(packet)

    return results
    




def get_objectids_by_identifier(url: str, layer: int, id_field: str, id_value: str):
    """
    Retrieve one or more OBJECTIDs from a feature layer using an identifier field.

    Parameters:
        url: str
            Base ArcGIS REST service URL.
        layer: int
            Layer ID to query.
        id_field: str
            Attribute field to filter by (e.g. 'Identifier', 'GlobalID').
        id_value: str
            Value to match in the id_field.

    Returns:
        list[int] | int | None:
            - Single OBJECTID if only one match
            - List of OBJECTIDs if multiple matches
            - None if no results
    """
    try:
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")

        params = {
            "where": f"{id_field}='{id_value}'",
            "outFields": "OBJECTID",
            "returnGeometry": "false",
            "f": "json",
            "token": token
        }

        query_url = f"{url}/{layer}/query"
        response = requests.get(query_url, params=params)

        if response.status_code != 200:
            raise Exception(
                f"Request failed ({response.status_code}): {response.text}"
            )

        data = response.json()

        if "error" in data:
            raise Exception(
                f"API Error: {data['error']['message']} - "
                f"{data['error'].get('details', [])}"
            )

        features = data.get("features", [])

        if not features:
            return None

        # extract OBJECTIDs
        objectids = [
            feat["attributes"].get("OBJECTID")
            for feat in features
            if feat.get("attributes") and feat["attributes"].get("OBJECTID") is not None
        ]

        if not objectids:
            raise Exception("No OBJECTID field found in matching records.")

        # Return single ID instead of list if only one
        return objectids[0] if len(objectids) == 1 else objectids

    except Exception as e:
        raise Exception(f"Error retrieving OBJECTIDs: {e}")


# =============================================================================
# CASCADE DELETE BY GLOBALID (RELATED → MAIN)
# =============================================================================
# delete_cascade_by_globalid():
#   - Deletes related-layer rows (parentglobalid == <globalid_value>)
#   - Then deletes the main-layer row(s) (<globalid_field> == <globalid_value>)
#   - Uses get_agol_token() directly; no timeout parameter
#   - "No matches" is normal: no warnings; continue
#   - Returns True if all delete calls completed without API/network errors
# =============================================================================

def delete_cascade_by_globalid(
    url,
    main_layer,
    related_layers,
    globalid_field,
    globalid_value,
    parent_field="parentglobalid",
):
    """
    Delete related-layer records by parentglobalid, then delete the main-layer
    record by its GlobalID field.

    Args:
        url (str):
            Base Feature Service URL, e.g. 'https://services.arcgis.com/.../FeatureServer'
        main_layer (int|str):
            The parent (main) layer index/path containing the GlobalID field.
        related_layers (iterable of int|str):
            Layers that contain the foreign key in `parent_field`.
        globalid_field (str):
            The GlobalID field name in the main layer (often 'GlobalID').
        globalid_value (str):
            The exact GlobalID value to match. Pass it as stored in AGOL (with or without braces).
        id_field_name (str):
            The main layer's ID field name (for traceability/logging only).
        id_field_value (any):
            The value of that ID field (for traceability/logging only).
        parent_field (str):
            The FK field name in related layers referencing the parent GlobalID.
            Defaults to 'parentglobalid'.

    Returns:
        bool: True if all delete calls succeeded at the HTTP/API level; False otherwise.
              (Zero matches are considered normal and do not count as failures.)
    """
    # --- Token retrieval is fixed (no parameter) ---
    try:
        token = get_agol_token()
        if not token:
            print("delete_cascade_by_globalid: Authentication failed: Invalid token.")
            return False
    except Exception as e:
        print(f"delete_cascade_by_globalid: Error obtaining token: {e}")
        return False

    base_url = url.rstrip("/")
    all_ok = True

    # Escape any single quotes in the GUID for the where clause
    gid = str(globalid_value).replace("'", "''")

    # --------------------------------
    # Helper: POST deleteFeatures call
    # --------------------------------
    def _delete_where(layer_id_or_path, where_clause):
        nonlocal all_ok
        layer_str = str(layer_id_or_path).strip().strip("/")
        delete_url = f"{base_url}/{layer_str}/deleteFeatures"

        params = {
            "where": where_clause,
            "f": "json",
            "token": token,
            # "rollbackOnFailure": True,  # optional if desired
        }

        try:
            resp = requests.post(delete_url, data=params)
        except Exception as e:
            print(f"[Layer {layer_str}] Network error during deleteFeatures: {e}")
            all_ok = False
            return

        if resp.status_code != 200:
            print(f"[Layer {layer_str}] HTTP {resp.status_code}: {resp.text[:300]}")
            all_ok = False
            return

        try:
            payload = resp.json()
        except Exception as e:
            print(f"[Layer {layer_str}] Invalid JSON: {e}; body={resp.text[:300]}")
            all_ok = False
            return

        if "error" in payload:
            err = payload.get("error", {})
            print(f"[Layer {layer_str}] Server error: {err.get('message')} | {err.get('details')}")
            all_ok = False
            return

        # Normal: deleteResults list; empty when no matches. Both are OK.
        results = payload.get("deleteResults")

        if results is None:
            # Some services omit deleteResults when nothing matched; treat as normal.
            # Log for visibility only.
            # print(f"[Layer {layer_str}] No deleteResults (likely no matches).")
            return

        # If present, ensure all results report success=True
        for r in results:
            if not (isinstance(r, dict) and r.get("success", False)):
                print(f"[Layer {layer_str}] One or more delete results failed: {results}")
                all_ok = False
                break

    # -----------------------------------------
    # 1) Delete related-layer items (FK = GID)
    # -----------------------------------------
    where_related = f"{parent_field} = '{gid}'"
    for rl in (related_layers or []):
        _delete_where(rl, where_related)

    # ------------------------------------------------
    # 2) Delete main-layer item(s) by GlobalID field
    # ------------------------------------------------
    where_main = f"{globalid_field} = '{gid}'"
    _delete_where(main_layer, where_main)

    # Optional: trace log (quiet, but useful during dev)
    # print(
    #     f"[CascadeDelete] Main layer '{main_layer}' ({id_field_name}={id_field_value}) "
    #     f"deleted by {globalid_field}={globalid_value} (related first)."
    # )

    return all_ok


# =============================================================================
# SPATIAL INTERSECT QUERY WRAPPER
# =============================================================================
# AGOLQueryIntersect:
#   - Builds an intersects query against a layer, given point/line/polygon input
#   - Supports running against multiple input geometries and merging results
#   - Assumes incoming coordinates are already in [lon, lat] (x, y) order
# =============================================================================
import json
import requests

# Assumes you have this available in your environment
# from your_auth_module import get_agol_token

class AGOLQueryIntersect:
    def __init__(self, url, layer, geometry, fields="*", return_geometry=False,
                 list_values=None, string_values=None):
        self.url = url
        self.layer = layer

        # Accept single geometry OR list of geometries; DO NOT SWAP—input is already [lon, lat]
        if isinstance(geometry, list) and len(geometry) > 0 and all(isinstance(g, list) for g in geometry):
            # geometry is a list of geometries
            self.geometry = geometry
        else:
            # geometry is a single geometry
            self.geometry = [geometry]

        self.fields = fields
        self.return_geometry = return_geometry
        self.list_values_field = list_values
        self.string_values_field = string_values
        self.token = self._authenticate()

        # Run query for each geometry and merge results
        self.results = self._execute_query_multiple()

        # If list_values is provided, store unique values in a list
        self.list_values = []
        if self.list_values_field:
            self.list_values = self._extract_unique_values(self.list_values_field)

        # If string_values is provided, store unique values in a comma-separated string
        self.string_values = ""
        if self.string_values_field:
            unique_list = self._extract_unique_values(self.string_values_field)
            self.string_values = ",".join(map(str, unique_list))

    def _authenticate(self):
        """Authenticate with AGOL and return a valid token."""
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")
        return token

    def _build_geometry(self, geometry):
        """
        Convert input geometry list into ArcGIS JSON geometry dict and geometryType.

        Assumes input coordinates are already [lon, lat].

        Supported:
            - Point: [lon, lat]
            - Line : [[lon, lat], ...] (treated as polyline unless closed polygon)
            - Polygon: [[lon, lat], ...] closed or auto-closed

        Returns:
            (geometry_dict, geometry_type_str)
        """
        if not isinstance(geometry, list):
            raise ValueError("Invalid geometry: Geometry must be a list.")

        # POINT
        if (
            len(geometry) == 2
            and all(isinstance(coord, (int, float)) for coord in geometry)
        ):
            geometry_dict = {
                "x": geometry[0],
                "y": geometry[1],
                "spatialReference": {"wkid": 4326}
            }
            geometry_type_str = "esriGeometryPoint"
            return geometry_dict, geometry_type_str

        # LINE OR POLYGON
        if all(
            isinstance(coord, list)
            and len(coord) == 2
            and all(isinstance(val, (int, float)) for val in coord)
            for coord in geometry
        ):
            # If only 2 points → definitely a line
            if len(geometry) == 2:
                geometry_dict = {
                    "paths": [geometry],  # already [lon, lat]
                    "spatialReference": {"wkid": 4326}
                }
                geometry_type_str = "esriGeometryPolyline"
                return geometry_dict, geometry_type_str

            # POLYGON CHECK
            first = geometry[0]
            last = geometry[-1]

            # If user did NOT close the polygon, close it
            if first != last:
                ring = geometry + [first]
            else:
                ring = geometry

            # A polygon must have at least 4 points (3 unique + closure)
            if len(ring) >= 4:
                geometry_dict = {
                    "rings": [ring],  # already [lon, lat]
                    "spatialReference": {"wkid": 4326}
                }
                geometry_type_str = "esriGeometryPolygon"
                return geometry_dict, geometry_type_str

            # Fallback to polyline
            geometry_dict = {
                "paths": [geometry],
                "spatialReference": {"wkid": 4326}
            }
            geometry_type_str = "esriGeometryPolyline"
            return geometry_dict, geometry_type_str

        raise ValueError("Invalid geometry structure.")

    def _execute_query(self, geometry):
        geometry_dict, geometry_type_str = self._build_geometry(geometry)

        params = {
            "geometry": json.dumps(geometry_dict),
            "geometryType": geometry_type_str,
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "outFields": self.fields,
            "returnGeometry": self.return_geometry,
            "outSR": 4326,
            "f": "json",
            "token": self.token
        }

        query_url = f"{self.url}/{self.layer}/query"
        response = requests.get(query_url, params=params)
        if response.status_code != 200:
            raise Exception(f"Request failed with status code {response.status_code}: {response.text}")

        data = response.json()
        if "error" in data:
            raise Exception(f"API Error: {data['error']['message']} - {data['error'].get('details', [])}")

        results = []
        requested_fields = [f.strip() for f in self.fields.split(",") if f.strip()]
        for feature in data.get("features", []):
            attributes = feature.get("attributes", {})
            filtered_attrs = {f: attributes.get(f) for f in requested_fields} if self.fields != "*" else attributes
            feature_package = {"attributes": filtered_attrs}
            if self.return_geometry:
                feature_package["geometry"] = feature.get("geometry", {})
            results.append(feature_package)
        return results

    # Run query for each geometry and merge unique results
    def _execute_query_multiple(self):
        combined = []
        seen = set()
        for geom in self.geometry:
            result = self._execute_query(geom)
            for item in result:
                key = json.dumps(item["attributes"], sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    combined.append(item)
        return combined

    def _extract_unique_values(self, field_name):
        """Return a unique list of values for the specified field. Blank if no results."""
        if not self.results:
            return []  # no features returned

        available_fields = {f for feature in self.results for f in feature["attributes"].keys()}
        if field_name not in available_fields:
            return []  # gracefully return blank list if field not found

        values = [
            feature["attributes"].get(field_name)
            for feature in self.results
            if feature["attributes"].get(field_name) is not None
        ]
        return list(set(values))


# =============================================================================
# APPLYEDITS UPLOADER
# =============================================================================
# AGOLDataLoader:
#   - Wraps applyEdits adds for a specific service layer
#   - Returns a consistent {success, message, globalids} structure to callers
# =============================================================================
class AGOLDataLoader:
    def __init__(self, url: str, layer: int):
        """
        Initialize the loader with AGOL service URL and layer ID.

        Notes:
            - Token is retrieved via _authenticate().
            - Logger is configured at INFO level for visibility in app logs.
        """
        self.url = url.rstrip("/")
        self.layer = layer
        self.token = self._authenticate()
        self.success = False
        self.message = None
        self.globalids = []

        # Configure logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("AGOLDataLoader")

    def _authenticate(self):
        """Authenticate with AGOL and return a valid token."""
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")
        return token

    def add_features(self, payload: dict):
        """
        Add features to the AGOL feature layer using applyEdits.

        Behavior:
            - POSTs to /applyEdits with adds payload JSON
            - Parses addResults for success/failure
            - Aggregates error messages when failures occur
            - Returns a consistent result dictionary

        Returns:
            dict: { "success": bool, "message": str, "globalids": list }
        """
        endpoint = f"{self.url}/{self.layer}/applyEdits"
        self.logger.info("Starting add_features process...")

        try:
            # Use data= and json.dumps for adds
            resp = requests.post(
                endpoint,
                data={
                    "f": "json",
                    "token": self.token,
                    "adds": json.dumps(payload["adds"])
                }
            )

            self.logger.info("Raw response text: %s", resp.text)
            result = resp.json()


            if "addResults" in result:
                add_results = result["addResults"]
                failures = [r for r in add_results if not r.get("success")]

                if failures:
                    self.success = False
                    error_messages = []
                    for r in failures:
                        err = r.get("error")
                        if err:
                            error_messages.append(
                                f"Code {err.get('code')}: {err.get('description')}"
                            )
                    self.message = (
                        f"Failed to add {len(failures)} feature(s). "
                        f"Errors: {', '.join(error_messages)}"
                    )
                    self.logger.error(self.message)
                else:
                    self.success = True
                    self.message = "All features added successfully."
                    self.globalids = [
                        r.get("globalId") for r in add_results if r.get("success")
                    ]
                    self.logger.info(self.message)

            else:
                self.success = False
                self.message = f"Unexpected response: {result}"
                self.logger.error(self.message)

        except Exception as e:
            self.success = False
            self.message = f"Error during add_features: {str(e)}"
            self.logger.exception(self.message)

        return {
            "success": self.success,
            "message": self.message,
            "globalids": self.globalids
        }
    

    def update_features(self, payload: dict):
        """
        Update existing features in the AGOL feature layer using applyEdits.

        Requirements:
            - Each update feature must include attributes["OBJECTID"].

        Returns:
            dict: { "success": bool, "message": str, "globalids": list }
        """

        # Validate payload structure
        if "updates" not in payload or not isinstance(payload["updates"], list):
            self.success = False
            self.message = "Payload must contain a list under key 'updates'."
            self.logger.error(self.message)
            return {
                "success": self.success,
                "message": self.message,
                "globalids": self.globalids
            }

        # Enforce OBJECTID presence before posting
        missing_ids = []
        for idx, feat in enumerate(payload["updates"]):
            attrs = feat.get("attributes", {})
            if "OBJECTID" not in attrs:
                missing_ids.append(idx)

        if missing_ids:
            self.success = False
            self.message = (
                "Update failed: Missing OBJECTID for "
                f"feature indices {missing_ids}. OBJECTID is required."
            )
            self.logger.error(self.message)
            return {
                "success": self.success,
                "message": self.message,
                "globalids": self.globalids
            }

        endpoint = f"{self.url}/{self.layer}/applyEdits"
        self.logger.info("Starting update_features process...")

        try:
            resp = requests.post(
                endpoint,
                data={
                    "f": "json",
                    "token": self.token,
                    "updates": json.dumps(payload["updates"])
                }
            )

            self.logger.info("Raw response text: %s", resp.text)
            result = resp.json()

            if "updateResults" in result:
                update_results = result["updateResults"]
                failures = [r for r in update_results if not r.get("success")]

                if failures:
                    self.success = False
                    error_messages = []
                    for r in failures:
                        err = r.get("error")
                        if err:
                            error_messages.append(
                                f"Code {err.get('code')}: {err.get('description')}"
                            )
                    self.message = (
                        f"Failed to update {len(failures)} feature(s). "
                        f"Errors: {', '.join(error_messages)}"
                    )
                    self.logger.error(self.message)

                else:
                    self.success = True
                    self.message = "All features updated successfully."
                    self.globalids = [
                        r.get("globalId") for r in update_results if r.get("success")
                    ]
                    self.logger.info(self.message)

            else:
                self.success = False
                self.message = f"Unexpected response: {result}"
                self.logger.error(self.message)

        except Exception as e:
            self.success = False
            self.message = f"Error during update_features: {str(e)}"
            self.logger.exception(self.message)

        return {
            "success": self.success,
            "message": self.message,
            "globalids": self.globalids
        }
    

    def delete_features(self, payload: dict):
        """
        Delete features from the AGOL feature layer using applyEdits.

        Behavior:
            - Expects payload["updates"] in the same style as update_features()
            - Extracts all OBJECTIDs from attributes
            - Sends them as a comma-separated string to applyEdits 'deletes'
            - Returns a consistent { success, message, objectids } structure
        """

        # Validate payload shape
        if "updates" not in payload or not isinstance(payload["updates"], list):
            self.success = False
            self.message = "Payload must contain a list under key 'updates'."
            self.logger.error(self.message)
            return {
                "success": self.success,
                "message": self.message,
                "objectids": []
            }

        updates = payload["updates"]

        # Collect OBJECTIDs from all update entries
        objectids = []
        for entry in updates:
            attrs = entry.get("attributes", {})
            oid = attrs.get("OBJECTID")
            if oid is not None:
                objectids.append(str(oid))

        if not objectids:
            self.success = False
            self.message = "No OBJECTIDs found in payload['updates'] for deletion."
            self.logger.error(self.message)
            return {
                "success": self.success,
                "message": self.message,
                "objectids": []
            }

        deletes_param = ",".join(objectids)
        endpoint = f"{self.url}/{self.layer}/applyEdits"

        self.logger.info("Starting delete_features for OBJECTIDs: %s", deletes_param)

        try:
            resp = requests.post(
                endpoint,
                data={
                    "f": "json",
                    "token": self.token,
                    "deletes": deletes_param
                }
            )

            self.logger.info("Raw response text: %s", resp.text)
            result = resp.json()

            deleted_oids = []

            if "deleteResults" in result:
                delete_results = result["deleteResults"]

                failures = [r for r in delete_results if not r.get("success")]
                successes = [r for r in delete_results if r.get("success")]

                if failures:
                    self.success = False
                    error_messages = []
                    for r in failures:
                        err = r.get("error")
                        oid = r.get("objectId")
                        if err:
                            error_messages.append(
                                f"OID {oid}: Code {err.get('code')} - {err.get('description')}"
                            )
                        else:
                            error_messages.append(f"OID {oid}: Unknown error")

                    self.message = (
                        f"Failed to delete {len(failures)} feature(s). "
                        f"Errors: {', '.join(error_messages)}"
                    )
                    self.logger.error(self.message)

                else:
                    self.success = True
                    self.message = "All features deleted successfully."
                    self.logger.info(self.message)

                deleted_oids = [
                    r.get("objectId") for r in successes if r.get("objectId") is not None
                ]

            else:
                self.success = False
                self.message = f"Unexpected response: {result}"
                self.logger.error(self.message)

        except Exception as e:
            self.success = False
            self.message = f"Error during delete_features: {str(e)}"
            self.logger.exception(self.message)
            deleted_oids = []

        return {
            "success": self.success,
            "message": self.message,
            "objectids": deleted_oids
        }



# =============================================================================
# UPDATE_TITLE
# =============================================================================
# UPDATE_DESCRIP
# =============================================================================
class AGOLRecordLoader:
    """
    Loads one or more AGOL records using select_record() and stores
    all attributes + geometry into Streamlit session_state.

    If multiple records are returned:
        - Each attribute becomes a list of values
        - Geometry becomes a list of geometries

    Access values through:
        loader.attributes
        loader.geometry
        loader.<fieldname>  (dynamic attributes)
    """

    def __init__(self, url, id_field, id_value,
                 prefix="", fields="*", return_geometry=True):

        self.url = url
        self.id_field = id_field
        self.id_value = id_value
        self.fields = fields
        self.return_geometry = return_geometry

        # Normalize prefix
        self.prefix = prefix.rstrip("_") + "_" if prefix else ""

        # Fetch records (may be 1 or many)
        self.records = self._fetch_records()

        # Extract combined attributes + geometry
        self.attributes = self._combine_attributes()
        self.geometry = self._combine_geometries()

        # Store in session_state
        self._store_in_session_state()

        # Create dynamic attributes for direct access
        self._create_dynamic_attributes()

    # ---------------------------------------------------------
    # Fetch records from AGOL
    # ---------------------------------------------------------
    def _fetch_records(self):
        results = select_record(
            url=self.url,
            id_field=self.id_field,
            id_value=self.id_value,
            fields=self.fields,
            return_geometry=self.return_geometry
        )

        if not results:
            raise ValueError(f"No record found for {self.id_field} = {self.id_value}")

        return results  # <-- now returns ALL records

    # ---------------------------------------------------------
    # Combine attributes across multiple records
    # ---------------------------------------------------------
    def _combine_attributes(self):
        combined = {}

        for feature in self.records:
            attrs = feature.get("attributes", {})
            for key, value in attrs.items():
                key_lower = key.lower()
                combined.setdefault(key_lower, []).append(value)

        # If only one record, unwrap lists
        for key in combined:
            if len(combined[key]) == 1:
                combined[key] = combined[key][0]

        return combined

    # ---------------------------------------------------------
    # Combine geometries across multiple records
    # ---------------------------------------------------------
    def _combine_geometries(self):
        geoms = [f.get("geometry") for f in self.records]

        # If only one geometry, unwrap it
        if len(geoms) == 1:
            return geoms[0]

        return geoms  # list of geometries

    # ---------------------------------------------------------
    # Store values in Streamlit session_state
    # ---------------------------------------------------------
    def _store_in_session_state(self):
        for key, value in self.attributes.items():
            st.session_state[f"{self.prefix}{key}"] = value

        st.session_state[f"{self.prefix}geometry"] = self.geometry

    # ---------------------------------------------------------
    # Create dynamic attributes for direct access
    # ---------------------------------------------------------
    def _create_dynamic_attributes(self):
        for key, value in self.attributes.items():
            setattr(self, key, value)

        setattr(self, "geometry", self.geometry)




class AGOLRouteSegmentFinder:
    """
    Queries a routes FeatureServer layer once for features intersecting a WGS84 envelope,
    clips returned polylines to that envelope client-side, then (without using any route IDs)
    selects ANY clipped route segments that the BOP and EOP points lie on (or are within tolerance of),
    merges those selected segments, and returns the merged geometry.

    select_and_merge_point_routes(...) returns:
      {
        "success": bool,
        "message": str,
        "merged_geometry": dict | None,   # ESRI polyline JSON (WKID 4326)
        "selected_objectids": list,       # OBJECTIDs of all features that were merged (if present)
        "bop_matches": list,              # [{ "objectid": <int|None>, "distance_m": <float> }, ...]
        "eop_matches": list               # [{ "objectid": <int|None>, "distance_m": <float> }, ...]
      }
    """

    def __init__(self, url, layer):
        self.url = url.rstrip("/")
        self.layer = int(layer)
        self.token = self._authenticate()

        self.success = False
        self.message = None

        # Optional logger (assumes logging imported by your app)
        try:
            logging.basicConfig(level=logging.INFO)
            self.logger = logging.getLogger("AGOLRouteSegmentFinder")
        except Exception:
            self.logger = None

    # ----------------------------- AUTH -----------------------------
    def _authenticate(self):
        token = get_agol_token()
        if not token:
            raise ValueError("Authentication failed: Invalid token.")
        return token

    # --------------------------- HELPERS ----------------------------
    def _build_envelope_square_meters(self, bop_pair, eop_pair, pad_deg, square_side_m=None, margin_m=0):
        """
        Build a visually square (meter-true) envelope around the two points:
          - If square_side_m is None:
              1) Make an initial degree bbox around the points with pad_deg.
              2) Convert that bbox width/height to meters at the center latitude.
              3) Target side = max(width_m, height_m) + margin_m (ensure non-zero).
          - If square_side_m is provided: use that as the side length directly.

        Returns a WGS84 envelope dict that appears square on web maps.
        """
        pad = float(pad_deg)
        if pad < 0:
            pad = 0.0

        # Expect (lat, lon)
        lat1, lon1 = bop_pair
        lat2, lon2 = eop_pair

        # Initial bbox in degrees with symmetric pad
        xmin = min(lon1, lon2) - pad
        xmax = max(lon1, lon2) + pad
        ymin = min(lat1, lat2) - pad
        ymax = max(lat1, lat2) + pad

        # Center latitude controls meters-per-degree of longitude
        cx = (xmin + xmax) / 2.0
        cy = (ymin + ymax) / 2.0

        # Local meters-per-degree approximations
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * max(0.0, math.cos(math.radians(cy)))

        # Current bbox size in meters
        width_deg  = max(0.0, xmax - xmin)
        height_deg = max(0.0, ymax - ymin)
        width_m    = width_deg  * (m_per_deg_lon if m_per_deg_lon > 0 else 0.0)
        height_m   = height_deg * m_per_deg_lat

        # Decide target side length in meters
        if square_side_m is None:
            side_m = max(width_m, height_m) + float(margin_m)
            if side_m <= 0:
                side_m = 50.0  # minimum safety size
        else:
            side_m = max(0.0, float(square_side_m))
            if side_m == 0.0:
                side_m = 50.0

        # Convert target side back to degree offsets about the center
        half_deg_lon = (side_m / (m_per_deg_lon if m_per_deg_lon > 0 else 1e9)) / 2.0
        half_deg_lat = (side_m / m_per_deg_lat) / 2.0

        xmin_sq = cx - half_deg_lon
        xmax_sq = cx + half_deg_lon
        ymin_sq = cy - half_deg_lat
        ymax_sq = cy + half_deg_lat

        # Clamp to valid geographic ranges
        xmin_sq = max(-180.0, xmin_sq)
        xmax_sq = min(180.0,  xmax_sq)
        ymin_sq = max(-90.0,  ymin_sq)
        ymax_sq = min(90.0,   ymax_sq)

        if hasattr(self, "logger") and self.logger:
            self.logger.info(
                f"[meter-square] side_m={side_m:.2f} | width_deg={xmax_sq - xmin_sq:.8f} "
                f"height_deg={ymax_sq - ymin_sq:.8f} center=({cy:.6f},{cx:.6f})"
            )

        return {
            "xmin": xmin_sq,
            "ymin": ymin_sq,
            "xmax": xmax_sq,
            "ymax": ymax_sq,
            "spatialReference": {"wkid": 4326}
        }

    def _build_point_envelope(self, lat, lon, pad_deg):
        """
        Build a small *square* envelope around a point (in degrees).
        """
        return {
            "xmin": lon - pad_deg,
            "ymin": lat - pad_deg,
            "xmax": lon + pad_deg,
            "ymax": lat + pad_deg,
            "spatialReference": {"wkid": 4326}
        }

    def _query_intersecting_routes(self, envelope, max_page_size=2000):
        import json, requests
        endpoint = f"{self.url}/{self.layer}/query"
        params_base = {
            "f": "json",
            "where": "1=1",
            "geometryType": "esriGeometryEnvelope",
            "geometry": json.dumps(envelope),
            "inSR": 4326,
            "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "returnGeometry": "true",
            "outFields": "*",
            "token": self.token,
            "resultRecordCount": max_page_size,
        }

        features = []
        result_offset = 0
        while True:
            params = dict(params_base)
            params["resultOffset"] = result_offset

            resp = requests.get(endpoint, params=params, timeout=60)
            data = resp.json()

            if "error" in data:
                raise Exception(f"API Error: {data['error']['message']} - {data['error'].get('details', [])}")

            batch = data.get("features", []) or []
            features.extend(batch)

            if not data.get("exceededTransferLimit"):
                break

            result_offset += len(batch) if len(batch) > 0 else max_page_size

        return features

    # --------------------- CLIENT-SIDE GEOMETRY ---------------------
    def _meters_per_degree(self, lat_deg):
        R = 6371008.8
        deg2rad = math.pi / 180.0
        m_per_deg_lat = R * deg2rad
        m_per_deg_lon = R * math.cos(lat_deg * deg2rad) * deg2rad
        return m_per_deg_lat, m_per_deg_lon

    def _point_segment_distance_m(self, px, py, x1, y1, x2, y2):
        """
        Approximate point-to-segment distance (meters) using a local equirectangular projection.
        Inputs are lon/lat degrees.
        """
        lat0 = (py + y1 + y2) / 3.0
        m_lat, m_lon = self._meters_per_degree(lat0)

        P = (px * m_lon, py * m_lat)
        A = (x1 * m_lon, y1 * m_lat)
        B = (x2 * m_lon, y2 * m_lat)

        ax, ay = A
        bx, by = B
        pxm, pym = P

        abx, aby = (bx - ax), (by - ay)
        apx, apy = (pxm - ax), (pym - ay)
        ab2 = abx*abx + aby*aby

        if ab2 == 0.0:
            return math.hypot(apx, apy)

        t = (apx*abx + apy*aby) / ab2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)

        cx = ax + t * abx
        cy = ay + t * aby

        dx = pxm - cx
        dy = pym - cy
        return math.hypot(dx, dy)

    def _clip_segment_to_bbox(self, x1, y1, x2, y2, xmin, ymin, xmax, ymax):
        """
        Cohen–Sutherland clip of a single segment (x=lon,y=lat) to the bbox.
        Returns (x1c, y1c, x2c, y2c) or None if fully outside.
        """
        def code(x, y):
            c = 0
            if x < xmin: c |= 1
            elif x > xmax: c |= 2
            if y < ymin: c |= 4
            elif y > ymax: c |= 8
            return c

        c1 = code(x1, y1)
        c2 = code(x2, y2)

        while True:
            if not (c1 | c2):
                return (x1, y1, x2, y2)
            if c1 & c2:
                return None

            out = c1 or c2
            dx = x2 - x1
            dy = y2 - y1

            if out & 8:     # top
                x = x1 + dx * (ymax - y1) / dy if dy != 0 else x1
                y = ymax
            elif out & 4:   # bottom
                x = x1 + dx * (ymin - y1) / dy if dy != 0 else x1
                y = ymin
            elif out & 2:   # right
                y = y1 + dy * (xmax - x1) / dx if dx != 0 else y1
                x = xmax
            else:           # left
                y = y1 + dy * (xmin - x1) / dx if dx != 0 else y1
                x = xmin

            if out == c1:
                x1, y1 = x, y
                c1 = code(x1, y1)
            else:
                x2, y2 = x, y
                c2 = code(x2, y2)

    def _clip_polyline_to_bbox(self, polyline, envelope):
        xmin, ymin, xmax, ymax = envelope["xmin"], envelope["ymin"], envelope["xmax"], envelope["ymax"]
        out_paths = []

        for path in polyline.get("paths", []):
            current = []
            for i in range(1, len(path)):
                x1, y1 = path[i-1]
                x2, y2 = path[i]
                seg = self._clip_segment_to_bbox(x1, y1, x2, y2, xmin, ymin, xmax, ymax)
                if seg is None:
                    if current:
                        out_paths.append(current)
                        current = []
                    continue
                cx1, cy1, cx2, cy2 = seg
                if not current:
                    current.append([cx1, cy1])
                else:
                    lastx, lasty = current[-1]
                    if abs(lastx - cx1) > 1e-12 or abs(lasty - cy1) > 1e-12:
                        current.append([cx1, cy1])
                current.append([cx2, cy2])

            if current:
                out_paths.append(current)

        if not out_paths:
            return None

        return {"paths": out_paths, "spatialReference": {"wkid": 4326}}

    def _min_point_to_polyline_distance_m(self, polyline, point_pair):
        """
        Minimum point-to-polyline distance (meters) across all segments.
        """
        if not polyline or not polyline.get("paths"):
            return None
        latp, lonp = point_pair
        best = None
        for path in polyline["paths"]:
            for i in range(1, len(path)):
                lon1, lat1 = path[i-1]
                lon2, lat2 = path[i]
                d = self._point_segment_distance_m(lonp, latp, lon1, lat1, lon2, lat2)
                if (best is None) or (d < best):
                    best = d
        return best

    # --------------------------- PUBLIC API -------------------------
    def select_and_merge_point_routes(self, bop_pair, eop_pair, pad_deg=0.0005, tolerance_m=15.0):
        """
        1) Build bbox from BOP/EOP (+ padding) using the meter-true square envelope
        2) Query the layer once for routes intersecting the bbox
        3) Clip each route to the bbox (client-side)
        4) Select ANY clipped routes that the BOP/EOP are on (within tolerance_m)
        5) Merge the selected clipped routes and return the merged polyline
        """
        if self.logger: 
            self.logger.info("Selecting and merging route segments for BOP/EOP (no route IDs)...")

        try:
            # 1) Envelope: USE THE METER-TRUE METHOD YOU PROVIDED
            envelope = self._build_envelope_square_meters(bop_pair, eop_pair, pad_deg)

            # 2) Query intersecting routes once
            features = self._query_intersecting_routes(envelope)
            if not features:
                self.success = False
                self.message = "No route features found in bounding box."
                if self.logger: self.logger.info(self.message)
                return {
                    "success": self.success,
                    "message": self.message,
                    "merged_geometry": None,
                    "selected_objectids": [],
                    "bop_matches": [],
                    "eop_matches": []
                }

            # 3) Clip features to envelope and keep non-empty results
            clipped = []
            for f in features:
                g = f.get("geometry")
                if not g or not g.get("paths"):
                    continue
                cg = self._clip_polyline_to_bbox(g, envelope)
                if cg and cg.get("paths"):
                    # ensure SR
                    if "spatialReference" not in cg:
                        cg["spatialReference"] = {"wkid": 4326}
                    clipped.append({"feature": f, "clipped": cg})

            if not clipped:
                self.success = False
                self.message = "No clipped route geometry inside the bounding box."
                if self.logger: self.logger.info(self.message)
                return {
                    "success": self.success,
                    "message": self.message,
                    "merged_geometry": None,
                    "selected_objectids": [],
                    "bop_matches": [],
                    "eop_matches": []
                }

            # 4) For each point, select ANY clipped routes within tolerance (no route IDs)
            bop_matches = []
            eop_matches = []
            for rec in clipped:
                cg = rec["clipped"]
                attrs = rec["feature"].get("attributes") or {}
                oid = attrs.get("OBJECTID")

                d_bop = self._min_point_to_polyline_distance_m(cg, bop_pair)
                if d_bop is not None and d_bop <= tolerance_m:
                    bop_matches.append({"objectid": oid, "distance_m": float(d_bop)})

                d_eop = self._min_point_to_polyline_distance_m(cg, eop_pair)
                if d_eop is not None and d_eop <= tolerance_m:
                    eop_matches.append({"objectid": oid, "distance_m": float(d_eop)})

            # Anything matched?
            selected_indices = set()
            selected_objectids = []

            for idx, rec in enumerate(clipped):
                oid = (rec["feature"].get("attributes") or {}).get("OBJECTID")
                # include if it appears in either match list
                in_bop = any(m["objectid"] == oid for m in bop_matches)
                in_eop = any(m["objectid"] == oid for m in eop_matches)
                if in_bop or in_eop:
                    selected_indices.add(idx)
                    if oid is not None:
                        selected_objectids.append(oid)

            if not selected_indices:
                self.success = False
                self.message = f"No route segment within {tolerance_m} m of either point."
                if self.logger: self.logger.info(self.message)
                return {
                    "success": self.success,
                    "message": self.message,
                    "merged_geometry": None,
                    "selected_objectids": [],
                    "bop_matches": bop_matches,
                    "eop_matches": eop_matches
                }

            # 5) Merge: concatenate paths from all selected clipped routes
            merged_paths = []
            for idx in selected_indices:
                merged_paths.extend(clipped[idx]["clipped"]["paths"])

            merged = {"paths": merged_paths, "spatialReference": {"wkid": 4326}}

            # Done
            self.success = True
            self.message = "Merged route segments for points computed successfully."
            if self.logger: self.logger.info(self.message)
            return {
                "success": self.success,
                "message": self.message,
                "merged_geometry": merged,
                "selected_objectids": selected_objectids,
                "bop_matches": bop_matches,
                "eop_matches": eop_matches
            }

        except Exception as e:
            self.success = False
            self.message = f"Error during select_and_merge_point_routes: {str(e)}"
            if self.logger: self.logger.exception(self.message)
            return {
                "success": self.success,
                "message": self.message,
                "merged_geometry": None,
                "selected_objectids": [],
                "bop_matches": [],
                "eop_matches": []
            }





def get_routes_within_distance(
    geometry: Any,
    routes_url: Optional[str],
    routes_layer: Optional[int],
    geometry_type: Optional[str] = None,
    distance_miles: float = 10.0,
    fields: Tuple[str, str] = ("Route_ID", "Route_Name"),
) -> List[Dict[str, Any]]:
    """
    Buffer the provided geometry by <distance_miles> miles, then query the given
    FeatureServer layer for routes that intersect that buffer. Output is ready
    for geometry_to_folium(feature_type='line').

    Returns a list of dicts:
      [
        {"route_id": <id>, "route_name": <name>, "geom": <list or list-of-lists [lon,lat]>},
        ...
      ]
    """
    if not geometry:
        return []

    # --- Normalize input to shapely geometry in EPSG:4326 (lon/lat) ---
    from shapely.geometry import (
        Point as _Pt, LineString as _Ls, LinearRing as _Lr,
        Polygon as _Pg, MultiLineString as _MLs
    )
    from shapely.ops import unary_union as _uun

    def _to_shapely(g, gtype: Optional[str]):
        if isinstance(g, dict):
            if "x" in g and "y" in g:
                return _Pt(float(g["x"]), float(g["y"]))
            if "lonlat" in g and isinstance(g["lonlat"], (list, tuple)) and len(g["lonlat"]) == 2:
                lon, lat = g["lonlat"]
                return _Pt(float(lon), float(lat))
            if "rings" in g:
                rings = g["rings"] or []
                polys = []
                for ring in rings:
                    try:
                        polys.append(_Pg(_Lr([(float(x), float(y)) for x, y in ring])))
                    except Exception:
                        pass
                if not polys:
                    return None
                return polys[0] if len(polys) == 1 else _uun(polys)
            if "paths" in g:
                paths = g["paths"] or []
                lines = []
                for path in paths:
                    try:
                        lines.append(_Ls([(float(x), float(y)) for x, y in path]))
                    except Exception:
                        pass
                if not lines:
                    return None
                return lines[0] if len(lines) == 1 else _MLs(lines)
        # list[[lon,lat], ...] → treat as a line
        if isinstance(g, (list, tuple)) and g and isinstance(g[0], (list, tuple)) and len(g[0]) == 2:
            try:
                return _Ls([(float(x), float(y)) for x, y in g])
            except Exception:
                return None
        return None

    shp_ll = _to_shapely(geometry, geometry_type)
    if shp_ll is None:
        return []

    # --- Buffer in meters using Web Mercator (EPSG:3857) ---
    meters = float(distance_miles) * 1609.344
    from pyproj import Transformer
    from shapely.ops import transform as _xf

    to_3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform
    to_4326 = Transformer.from_crs(3857, 4326, always_xy=True).transform

    shp_3857 = _xf(to_3857, shp_ll)
    buf_3857 = shp_3857.buffer(meters)
    if buf_3857.is_empty:
        return []
    if hasattr(buf_3857, "geoms") and buf_3857.geom_type in ("MultiPolygon", "GeometryCollection"):
        polys = [g for g in buf_3857.geoms if g.geom_type == "Polygon"]
        if polys:
            from shapely.ops import unary_union
            buf_3857 = unary_union(polys)

    buf_ll = _xf(to_4326, buf_3857)

    # Convert polygon(s) → list-of-rings (each ring = [[lon,lat], ...])
    def _rings_from_polygon(p: _Pg):
        if not isinstance(p, _Pg) or p.exterior is None:
            return []
        ring = [[round(float(x), 6), round(float(y), 6)] for (x, y) in p.exterior.coords]
        return [ring]

    rings = []
    if buf_ll.geom_type == "Polygon":
        rings.extend(_rings_from_polygon(buf_ll))
    elif buf_ll.geom_type == "MultiPolygon":
        for part in buf_ll.geoms:
            rings.extend(_rings_from_polygon(part))

    if not rings:
        return []

    # --- Query the specified FeatureServer layer (via existing helper) ---
    # We pass url/layer forward when the helper supports them;
    # fallback to session-based config otherwise.
    try:
        # query_routes_within_buffer signature in your env accepts the area,
        # and typically resolves the service from session state.
        # If it supports url/layer, pass them through via kwargs.
        feats = query_routes_within_buffer(
            rings,
            fields=fields,
            include_geometry=True,
            url=routes_url,
            layer=routes_layer,
        ) or []
    except TypeError:
        # Older signature without url/layer: rely on session-configured service.
        feats = query_routes_within_buffer(
            rings,
            fields=fields,
            include_geometry=True,
        ) or []
    except Exception:
        feats = []

    # --- Package for geometry_to_folium ---
    packaged: List[Dict[str, Any]] = []
    f_id, f_name = fields
    for f in feats:
        attrs = (f.get("attributes") or {})
        rid = attrs.get(f_id)
        rname = attrs.get(f_name)
        geom = f.get("geometry") or []
        if rid and geom:
            packaged.append({"route_id": rid, "route_name": rname, "geom": geom})
    return packaged



def get_mileposts_for_route(
    route_id: str,
    *,
    # Signature kept for compatibility, but values are resolved from session state as required.
    service_url: Optional[str] = None,
    layer: Optional[int] = None,
    route_id_field: str = "Route_ID",
    mp_label_field: str = "Milepost_Number",
    mp_prefix: str = "",
    mp_suffix: str = "",
    _use_cache: bool = True
) -> List[Dict]:
    """
    Return reference milepost points for the given Route ID.

    Behavior:
    - Resolves milepost service config from:
        st.session_state['mileposts_intersect']['url']
        st.session_state['mileposts_intersect']['layer']
    - Queries by Route_ID using select_record(...) from this module.
    - Expects point geometry (x/y) and Milepost_Number (integer).
    - Packages each feature as:
        { "lon": float, "lat": float, "label": str, "mp": str }
      where 'label' is the stringified Milepost_Number (prefix/suffix optional).

    Notes:
    - Safe no-op on any error: returns [].
    - Caches results in st.session_state to avoid repeat queries per route.
    """
    # --- Required inputs ---
    if not isinstance(route_id, str) or not route_id.strip():
        return []

    # --- Resolve service config strictly from session state as requested ---
    cfg = st.session_state.get("mileposts_intersect") or {}
    service_url = cfg.get("url")
    layer = cfg.get("layer")

    if not isinstance(service_url, str) or not service_url or layer is None:
        return []

    # --- Cache guard (per-service, per-layer, per-route) ---
    cache_key = f"__mileposts_cache::{service_url}::{layer}::{route_id}"
    if _use_cache:
        cached = st.session_state.get(cache_key)
        if isinstance(cached, list):
            return cached

    # --- Query all milepost records by Route_ID, return geometry ---
    try:
        features = select_record(
            url=service_url,
            layer=int(layer),
            id_field="Route_ID",          # enforced per your instruction
            id_value=str(route_id).replace("'", "''"),
            fields="Route_ID,Milepost_Number",
            return_geometry=True,
        ) or []
    except Exception:
        st.session_state[cache_key] = []
        return []

    # --- Package results for rendering in select_route_and_points ---
    records: List[Dict] = []
    for feat in features:
        attrs = feat.get("attributes") or {}
        geom = feat.get("geometry") or {}

        x = geom.get("x")
        y = geom.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue

        mp_raw = attrs.get("Milepost_Number")
        if mp_raw is None:
            continue

        # Ensure label is a string (Milepost_Number is stored as integer)
        try:
            label_core = str(int(mp_raw))
        except Exception:
            label_core = str(mp_raw)

        label = f"{mp_prefix}{label_core}{mp_suffix}"

        try:
            records.append({
                "lon": float(x),
                "lat": float(y),
                "label": label,          # used by the DivIcon renderer
                "mp": label_core,        # raw-as-string; available as fallback
            })
        except Exception:
            # Skip malformed points but keep others
            continue

    st.session_state[cache_key] = records
    return records




def get_assignee_submitter_list():
    """
    Retrieve assignees from AGOL and build a submitter list for the loader selectbox.

    Data source:
    - URL:    st.session_state["assignees_url"]
    - Layer:  st.session_state["assignees_layer"]

    Display format:
    - "{Organization} – {Assignee Name}"

    Returns:
        list[str] suitable for st.selectbox
    """

    assignees_url = st.session_state.get("apex_contacts_url")
    assignees_layer = st.session_state.get("apex_contacts_layer")

    try:
        records = get_multiple_fields(
            url=assignees_url,
            layer=assignees_layer,
            fields=[
                "Org",
                "Assignee",
                "Role"
            ]
        )
    except Exception:
        # Fail safe: still allow upload
        return ["", "Other"]

    submitters = []

    for rec in records or []:
        org = str(rec.get("Org", "")).strip()
        name = str(rec.get("Assignee", "")).strip()
        role = str(rec.get("Role", "")).strip()

        if not name or not org:
            continue

        # ✅ NEW FILTER: Role must contain 'Loader'
        if "LOADER" not in role.upper():
            continue

        org_upper = org.upper()

        # ✅ FILTER: Only allow AK DOT&PF or MBI orgs
        if "AK DOT&PF" not in org_upper and "MBI" not in org_upper:
            continue

        submitters.append(f"{org} – {name}")

    # De-dupe
    submitters = list(set(submitters))

    # Sort: AK DOT&PF first, MBI second
    def sort_key(value: str):
        v = value.upper()
        if "AK DOT&PF" in v:
            return (0, v)
        if "MBI" in v:
            return (1, v)
        return (2, v)

    submitters = sorted(submitters, key=sort_key)

    # Always allow override
    submitters.append("Other")

    # Blank first entry so no default selection
    submitters.insert(0, "")

    return submitters

