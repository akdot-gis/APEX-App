
"""
===============================================================================
PAYLOAD BUILDERS (STREAMLIT) — APEX / AGOL APPLYEDITS PAYLOAD FACTORIES
===============================================================================

Purpose:
    Provides helper utilities and payload factory functions used to construct
    ArcGIS "applyEdits" payloads for uploading project-related data into APEX
    (AGOL-backed) feature layers.

    This module is called by the upload orchestration layer (e.g., load_project.py)
    to build per-layer payloads based on current st.session_state values.

Key behaviors:
    - Payload normalization and cleaning:
        * clean_payload(): removes attributes that are None, 0, or '' to reduce
          noise and avoid overwriting values with empty defaults.
        * clean_payloads(): removes attributes explicitly marked "REMOVE".
    - Type conversion helpers:
        * to_date_string(): normalizes date/datetime to YYYY-MM-DD string form.
        * str_to_int(): converts currency-like strings into integers.
    - Geometry center helpers:
        * get_point_center(), get_line_center(), get_polygon_center(): compute
          representative (lon, lat) centers for display/point geometry fields.
    - Payload builders (ArcGIS applyEdits schema):
        * project_payload(): main project record with a representative point.
        * geometry_payload(): child geometry records for site/route/boundary.
        * communities_payload(): optional impacted communities child records.
        * contacts_payload(): optional contacts child records.
        * geography_payload(): optional geography overlays (region/borough/senate/
          house/route) sourced by querying reference services.

Session-state dependencies (selected examples; see each builder for details):
    - Geometry selection:
        'selected_point' | 'selected_route' | 'selected_boundary'
    - Project attributes:
        'proj_name', 'proj_desc', 'phase', 'fund_type', etc.
    - Geography selections:
        '{name}_list' keys (e.g., 'region_list', 'borough_list', ...)
    - Communities:
        'impact_comm_ids'
    - Contacts:
        'project_contacts'

Notes:
    - This module intentionally raises/returns None in a few "valid empty" cases:
        * communities_payload(): returns None when nothing exists to add.
        * contacts_payload(): returns None when no contacts exist.
        * geometry_payload(): returns None when no geometry selection exists.
        * geography_payload(): returns None when no payload was assembled.
    - Shapely is used for center computation; payload geometry structures are
      formatted as ArcGIS JSON (wkid 4326).

===============================================================================
"""

import streamlit as st
import re
from shapely.geometry import LineString, Point, Polygon
from typing import Any, Dict, List, Optional, Iterable
import datetime
from agol.agol_util import (
    select_record, 
    get_objectids_by_identifier
)
from util.geospatial_util import (
    center_of_geometry, 
    create_buffers, 
    slice_and_buffer_route
)

from util.input_util import (
    fmt_date
)

# =============================================================================
# PAYLOAD CLEANING / NORMALIZATION HELPERS
# =============================================================================
# These helpers standardize outgoing payloads:
# - Remove empty values that should not be written (None/0/"")
# - Remove sentinel values used to indicate explicit removal ("REMOVE")
# - Support adds, updates, and deletes sections
# =============================================================================
def clean_payload(payload: dict, edit_type=None) -> dict:
    """
    Normalize and clean an ArcGIS applyEdits payload.

    Behavior
    --------
    - If edit_type is provided, clean only that section.
    - If edit_type is None, infer from keys present: adds > updates > deletes.
    - For adds/updates:
        - Preserve attributes where value is None so AGOL can set those fields to null.
        - Remove attributes where value is 0, "", or "REMOVE".
    - For deletes:
        - Ensure a compact list of valid objectIds.

    Parameters
    ----------
    payload : dict
        A dict shaped like an applyEdits payload containing one of:
        - {"adds":    [{"attributes": {...}, "geometry": {...}}, ...]}
        - {"updates": [{"attributes": {...}, "geometry": {...}}, ...]}
        - {"deletes": [1, 2, 3]}

    edit_type : str | None
        One of "adds", "updates", "deletes", or None to infer.

    Returns
    -------
    dict
        The cleaned payload.
    """
    if not isinstance(payload, dict):
        return payload

    # Infer edit type if not supplied.
    et = (edit_type or "").lower() if isinstance(edit_type, str) else None

    if et not in ("adds", "updates", "deletes"):
        if "adds" in payload:
            et = "adds"
        elif "updates" in payload:
            et = "updates"
        elif "deletes" in payload:
            et = "deletes"
        else:
            # Nothing recognizable; return as-is.
            return payload

    cleaned = dict(payload)  # Shallow copy only.

    def _filter_attrs(attrs: dict) -> dict:
        """
        Remove values that should not be sent to AGOL while preserving None.

        None values are intentionally kept so they can be serialized as null
        and used to clear nullable fields in AGOL.
        """
        if not isinstance(attrs, dict):
            return {}

        return {
            k: v
            for k, v in attrs.items()
            if v != "" and v != 0 and v != "REMOVE"
        }

    if et in ("adds", "updates"):
        items = []

        for rec in payload.get(et, []) or []:
            rec_clean = dict(rec) if isinstance(rec, dict) else {}

            # Clean attributes while preserving None values.
            rec_clean["attributes"] = _filter_attrs(rec_clean.get("attributes", {}))

            # Drop empty geometry dictionaries.
            geom = rec_clean.get("geometry", None)
            if isinstance(geom, dict) and not geom:
                rec_clean.pop("geometry", None)

            items.append(rec_clean)

        cleaned[et] = items

        # Preserve other sections unmodified if present.
        for other in ("adds", "updates", "deletes"):
            if other in cleaned and other != et:
                cleaned[other] = payload.get(other)

        return cleaned

    if et == "deletes":
        ids = payload.get("deletes", [])

        if not isinstance(ids, (list, tuple)):
            ids = [ids]

        # Keep only non-null, non-empty IDs.
        cleaned_ids = [oid for oid in ids if oid not in (None, "")]

        return {"deletes": cleaned_ids}

    # Fallback: return as-is if something unexpected happens.
    return payload



def to_date_string(value):
    """
    Convert a datetime.date or datetime.datetime to a string.

    Behavior:
        - If value is already a string, return it unchanged.
        - If value is None, return None.
        - If value is a date/datetime, return YYYY-MM-DD.
        - Otherwise return None.

    Rationale:
        ArcGIS services often prefer consistent date string formats when using
        attribute payloads (or when upstream sources produce mixed types).
    """
    if value is None:
        return None
    # If it's already a string, assume it's a date string and return as-is
    if isinstance(value, str):
        return value
    # If it's a date (but not datetime), promote to datetime
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    # If it's a datetime, format it
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d")
    # Anything else is invalid
    return None


def str_to_int(value):
    """
    Convert a value to an integer if it's a string.

    Behavior:
        - If value is already an int, return it unchanged.
        - If value is a string, strip $, commas, and decimals, then convert.
        - If conversion fails, return the original value.

    Notes:
        This allows number inputs to come from either numeric widgets or
        pre-formatted strings (e.g., "$12,345.00").
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        cleaned = cleaned.replace("$", "").replace(",", "")
        # Remove decimal portion if present
        if "." in cleaned:
            cleaned = cleaned.split(".")[0]
        try:
            return int(cleaned)
        except ValueError:
            return value
    return value




# =============================================================================
# PAYLOAD BUILDER: PROJECT
# =============================================================================
# project_payload():
# - Determines a representative center based on selected geometry
# - Builds the "projects" layer payload
# - Uses clean_payload() to remove empty attributes before upload
# =============================================================================
def project_payload():
    try:
        # Determine center based on selected geometry
        if st.session_state.get("selected_point"):
            proj_type = "Site"
            geoms = st.session_state['selected_point']
            geom_type = 'point'
        elif st.session_state.get("selected_route"):
            proj_type = "Route"
            geoms = st.session_state['selected_route']
            geom_type = 'line'
        elif st.session_state.get("selected_boundary"):
            proj_type = "Boundary"
            geoms = st.session_state['selected_boundary']
            geom_type = 'polygon'

        if not geoms or not isinstance(geoms, (list, tuple)):
            raise RuntimeError("No project geometries available in session.")

        # --- Create buffers (fixed distances by kind) ---
        buffer = create_buffers(geometry_list=geoms, geom_type=geom_type, distance_m=.01)
    
        if not buffer:
            raise RuntimeError("Buffering produced no output (check geometry and distances).")

        # --- ESRI Polygon geometry (multipart via rings) ---
        esri_polygon = {
            "rings": buffer,  # list of rings (each is [[lon, lat], ...])
            "spatialReference": {"wkid": 4326},
        }

        # >>> NEW: expose this polygon for Traffic Impact fallback use <<<
        st.session_state["project_esri_polygon"] = esri_polygon

        # Build payload with .get() and default None
        payload = {
            "adds": [
                {
                    "attributes": {
                        "Proj_Type": proj_type,
                        "AWP_Proj_Name": st.session_state.get("awp_proj_name", None),
                        "Proj_Name": st.session_state.get("proj_name", None),
                        "Construction_Year": st.session_state.get("construction_year", None),
                        "New_Continuing": st.session_state.get("new_continuing", None),
                        "Phase": st.session_state.get("phase", None),
                        "IRIS": st.session_state.get("iris", None),
                        "STIP": st.session_state.get("stip", None),
                        "Fed_Proj_Num": st.session_state.get("fed_proj_num", None),
                        "Fund_Type": st.session_state.get("fund_type", None),
                        "Proj_Prac": st.session_state.get("proj_prac", None),
                        "Anticipated_Start": st.session_state.get("anticipated_start", None),
                        "Anticipated_End": st.session_state.get("anticipated_end", None),
                        "Awarded": "Yes" if st.session_state.get("contractor") else "No",
                        "Award_Date": to_date_string(st.session_state.get("award_date", None)),
                        "Award_Fiscal_Year": st.session_state.get("award_fiscal_year", None),
                        "Contractor": st.session_state.get("contractor", None),
                        "Awarded_Amount": str_to_int(st.session_state.get("awarded_amount", None)),
                        "Current_Contract_Amount": str_to_int(st.session_state.get("current_contract_amount", None)),
                        "Amount_Paid_to_Date": str_to_int(st.session_state.get("amount_paid_to_date", None)),
                        "TenAdd": to_date_string(st.session_state.get("tenadd", None)),
                        "AWP_Proj_Desc": st.session_state.get("awp_proj_desc", None),
                        "AWP_Preconstruction": st.session_state.get("preconstruction", None),
                        "Proj_Desc": st.session_state.get("proj_desc", None),
                        "Contact_Name": st.session_state.get("contact_name", None),
                        "Contact_Email": st.session_state.get("contact_email", None),
                        "Contact_Phone": st.session_state.get("contact_phone", None),
                        "Impact_Comm": st.session_state.get("impact_comm_names", None),
                        "AWP_DOT_PF_Region": st.session_state.get("awp_region", None),
                        "AWP_Borough_Census_Area": st.session_state.get("awp_borough", None),
                        "AWP_Senate_District": st.session_state.get("awp_senate", None),
                        "AWP_House_District": st.session_state.get("awp_house", None),
                        "List_DOT_PF_Region": st.session_state.get("region_string", None),
                        "List_Borough_Census_Area": st.session_state.get("borough_string", None),
                        "List_Senate_District": st.session_state.get("senate_string", None),
                        "List_House_District": st.session_state.get("house_string", None),
                        "List_Route_ID": st.session_state.get("route_ids", None),
                        "List_Route_Name": st.session_state.get("route_names", None),
                        "Proj_Web": st.session_state.get("proj_web", None),
                        'Submitted_By': st.session_state.get('submitted_by', None),
                        "Database_Status": "Review: Awaiting Review",
                        "AWP_Contract_ID": st.session_state.get("awp_guid", None),
                        "AWP_Update": st.session_state.get("awp_update", None)
                    },
                    "geometry": esri_polygon
                }
            ]
        }
        return clean_payload(payload, 'adds')
        
    except Exception as e:
        # Bubble up error so caller can handle with st.error
        raise RuntimeError(f"Error building project payload: {e}")


# =============================================================================
# PAYLOAD BUILDER: GEOMETRY (SITES / ROUTES / BOUNDARIES)
# =============================================================================
# geometry_payload():
# - Builds one or more child geometry records based on the selected geometry type
# - Normalizes nesting for points, routes (paths), and boundaries (rings)
# - Returns a list of cleaned payloads (one per geometry) or None if no selection
# =============================================================================
def geometry_payload():
    try:
        payloads = []  # final list of cleaned payloads
    
        # ---------------------------------------------------------------------
        # POINT CASE
        # ---------------------------------------------------------------------
        if st.session_state.get("selected_point"):
            points = st.session_state["selected_point"]

            for lon, lat in points:
                payload = {
                    "adds": [
                        {
                            "attributes": {
                                "Site_AWP_Proj_Name": st.session_state.get("awp_proj_name"),
                                "Site_Proj_Name": st.session_state.get("proj_name"),
                                "Site_DOT_PF_Region": st.session_state.get("region_string"),
                                "Site_Borough_Census_Area": st.session_state.get("borough_string"),
                                "Site_Senate_District": st.session_state.get("senate_string"),
                                "Site_House_District": st.session_state.get("house_string"),
                                "Site_Construction_Year": st.session_state.get("construction_year", None),
                                "Site_Database_Status": "Review: Awaiting Review",
                                "parentglobalid": st.session_state.get("apex_globalid", None)
                            },
                            "geometry": {
                                "x": float(lon),
                                "y": float(lat),
                                "spatialReference": {"wkid": 4326}
                            }
                        }
                    ]
                }
                
                payloads.append(clean_payload(payload))
            return payloads

        # ---------------------------------------------------------------------
        # ROUTE CASE (POLYLINES)
        # ---------------------------------------------------------------------
        elif st.session_state.get("selected_route"):
            routes = st.session_state["selected_route"]

            for route in routes:
                payload = {
                    "adds": [
                        {
                            "attributes": {
                                "Route_AWP_Proj_Name": st.session_state.get("awp_proj_name"),
                                "Route_Proj_Name": st.session_state.get("proj_name"),
                                "Route_DOT_PF_Region": st.session_state.get("region_string"),
                                "Route_Borough_Census_Area": st.session_state.get("borough_string"),
                                "Route_Senate_District": st.session_state.get("senate_string"),
                                "Route_House_District": st.session_state.get("house_string"),
                                "Route_Construction_Year": st.session_state.get("construction_year", None),
                                "Route_Database_Status": "Review: Awaiting Review",
                                "parentglobalid": st.session_state.get("apex_globalid", None)
                            },
                            "geometry": {
                                "paths": [route],
                                "spatialReference": {"wkid": 4326}
                            }
                        }
                    ]
                }
                payloads.append(clean_payload(payload))
            return payloads

        # ---------------------------------------------------------------------
        # BOUNDARY CASE (POLYGONS)
        # ---------------------------------------------------------------------
        elif st.session_state.get("selected_boundary"):
            boundaries = st.session_state["selected_boundary"]
            
            for ring in boundaries:
                payload = {
                    "adds": [
                        {
                            "attributes": {
                                "Boundary_AWP_Proj_Name": st.session_state.get("awp_proj_name"),
                                "Boundary_Proj_Name": st.session_state.get("proj_name"),
                                "Boundary_DOT_PF_Region": st.session_state.get("region_string"),
                                "Boundary_Borough_Census_Area": st.session_state.get("borough_string"),
                                "Boundary_Senate_District": st.session_state.get("senate_string"),
                                "Boundary_House_District": st.session_state.get("house_string"),
                                "Boundary_Construction_Year": st.session_state.get("construction_year", None),
                                "Boundary_Database_Status": "Review: Awaiting Review",
                                "parentglobalid": st.session_state.get("apex_globalid", None)
                            },
                            "geometry": {
                                "rings": [ring],
                                "spatialReference": {"wkid": 4326}
                            }
                        }
                    ]
                }
                payloads.append(clean_payload(payload))
            return payloads

        # ---------------------------------------------------------------------
        # NOTHING SELECTED
        # ---------------------------------------------------------------------
        else:
            return None
    except Exception as e:
        st.error(f"Error building geometry payload: {e}")
        return None




def location_payload():
    """
    Build an AGOL-ready 'adds' payload from session-state inputs:
      - st.session_state["projects_geom"]: geometry(ies) in [lon, lat]
      - st.session_state["projects_geom_type"]: 'point' | 'line'/'linestring' | 'polygon'

    Steps:
      1) Split geoms into points/lines/polys (same pattern as impact_area).
      2) Create buffers per kind using create_buffers(...) with fixed 10 m.
      3) Combine rings into a single ESRI Polygon geometry (multipart).
      4) Build and return the applyEdits payload with Impact_Area attributes.
    """
    try:

        # Determine center based on selected geometry
        if st.session_state.get("selected_point"):
            pt = st.session_state["selected_point"]
            st.session_state['center'] = center_of_geometry(pt, "Point")
        elif st.session_state.get("selected_route"):
            route = st.session_state["selected_route"]
            st.session_state['center'] =  center_of_geometry(route, "Line")
        elif st.session_state.get("selected_boundary"):
            boundary = st.session_state["selected_boundary"]
            st.session_state['center'] = center_of_geometry(boundary, "Polygon")


        # --- Build payload with Impact_Area attribute schema ---
        payload = {
            "adds": [
                {
                    "attributes": {
                        "Location_AWP_Proj_Name": st.session_state.get("awp_proj_name", None),
                        "Location_Proj_Name": st.session_state.get("proj_name", None),
                        "Location_DOT_PF_Region": st.session_state.get("region_string", None),
                        "Location_Borough_Census_Area": st.session_state.get("borough_string", None),
                        "Location_Senate_District": st.session_state.get("senate_string", None),
                        "Location_House_District": st.session_state.get("house_string", None),
                        "Location_Construction_Year": st.session_state.get("construction_year", None),
                        "Location_Database_Status": "Review: Awaiting Review",
                        "parentglobalid": st.session_state.get("apex_globalid", None)
                    },
                    "geometry": {
                        "x": st.session_state['center'][0] if st.session_state['center'] else None,  # longitude
                        "y": st.session_state['center'][1] if st.session_state['center'] else None,  # latitude
                        "spatialReference": {"wkid": 4326}
                        
                    }
                }
            ]
        }

        return clean_payload(payload, 'adds')

    except Exception as e:
        raise RuntimeError(f"Error building buffered project polygon payload: {e}")


# =============================================================================
# PAYLOAD BUILDER: IMPACTED COMMUNITIES (OPTIONAL)
# =============================================================================
def communities_payload():
    """
    Build an ArcGIS applyEdits payload for impacted communities.

    Returns:
        dict | None:
            - dict: cleaned payload containing 'adds' for each resolved community
            - None: when there are no impacted communities, or no usable records

    Notes:
        - Communities are resolved via select_record() against a reference service.
        - Records with missing required fields are skipped rather than failing.
    """
    try:
        comm_list = st.session_state.get("impact_comm_ids", None)
        if not comm_list:
            # Valid case: nothing to add
            return None

        payload = {"adds": []}
        comms_url = st.session_state['communities']

        for comm_id in comm_list:
            comms_data = select_record(
                comms_url,
                7,
                "DCCED_CommunityId",
                str(comm_id),
                fields="OverallName,Latitude,Longitude"
            )
            if not comms_data:
                # Skip silently if no record found
                continue

            attrs = comms_data[0].get("attributes", {})
            name = attrs.get("OverallName")
            y = attrs.get("Latitude")
            x = attrs.get("Longitude")
            if name and y is not None and x is not None:
                payload["adds"].append({
                    "attributes": {
                        "Community_Name": name,
                        "parentglobalid": st.session_state.get("apex_globalid", None)
                    },
                    "geometry": {
                        "x": x,
                        "y": y,
                        "spatialReference": {"wkid": 4326}
                    }
                })
            # If required fields are missing, skip this community instead of raising

        if not payload["adds"]:
            # Valid case: no usable community records
            return None

        return clean_payload(payload, 'adds')
    except Exception as e:
        st.error(f"Error building communities payload: {e}")
        return




# =============================================================================
# PAYLOAD BUILDER: GEOGRAPHY (OPTIONAL OVERLAYS)
# =============================================================================
def geography_payload(name: str):
    """
    Build a payload containing attributes and geometry for a given geography type.

    Parameters:
        globalid: str
            The parent GlobalID to associate with the payload.
        name: str
            The geography type to process. Supported values include:
            'region', 'borough', 'senate', 'house', and 'route'.

    Returns:
        dict | None:
            - dict: cleaned payload with 'adds' entries containing attributes + geometry
            - None: when no payload could be assembled (no IDs or no results)

    Mechanism:
        - IDs are read from st.session_state[f"{name}_list"]
        - Records are fetched from an AGOL reference service via select_record()
        - The returned geometry is passed through directly into the outgoing payload
    """

    payload = {}

    # -------------------------------------------------------------------------
    # REGION
    # -------------------------------------------------------------------------
    if name == 'region':
        id_list = st.session_state.get(f"{name}_list")
    
        payload = {"adds": []}
        for item_id in id_list:
            # Query record from AGOL service
            data = select_record(
                url = st.session_state['region_intersect']['url'],
                layer = st.session_state['region_intersect']['layer'],
                id_field = "GlobalID", 
                id_value = str(item_id), 
                fields="*", 
                return_geometry=True
            )
            if not data:
                continue
            attrs = data[0].get("attributes", {})
            geom = data[0].get("geometry", {})
            region_name = attrs.get("NameAlt")
            payload["adds"].append({
                "attributes": {
                    "Region_Name": region_name,
                    "parentglobalid": st.session_state.get("apex_globalid", None),
                },
                "geometry": geom
            })

    # -------------------------------------------------------------------------
    # BOROUGH
    # -------------------------------------------------------------------------
    if name == 'borough':
        id_list = st.session_state.get(f"{name}_list")

        payload = {"adds": []}
        for item_id in id_list:
            data = select_record(
                url = st.session_state['borough_intersect']['url'],
                layer = st.session_state['borough_intersect']['layer'],
                id_field = "GlobalID", 
                id_value = str(item_id), 
                fields="*", 
                return_geometry=True
            )
            if not data:
                continue
            attrs = data[0].get("attributes", {})
            geom = data[0].get("geometry", {})
            fips = attrs.get('FIPS')
            borough_name = attrs.get("NameAlt")
            payload["adds"].append({
                "attributes": {
                    "Bor_FIPS": fips,
                    "Bor_Name": borough_name,
                    "parentglobalid": st.session_state.get("apex_globalid", None),
                },
                "geometry": geom
            })

    # -------------------------------------------------------------------------
    # SENATE
    # -------------------------------------------------------------------------
    if name == 'senate':
        id_list = st.session_state.get(f"{name}_list")
        if not id_list:
            print(None)

        payload = {"adds": []}
        for item_id in id_list:
            data = select_record(
                url = st.session_state['senate_intersect']['url'],
                layer = st.session_state['senate_intersect']['layer'],
                id_field = "GlobalID", 
                id_value = str(item_id), 
                fields="*", 
                return_geometry=True
            )
            if not data:
                continue
            attrs = data[0].get("attributes", {})
            geom = data[0].get("geometry", {})
            district = attrs.get("DISTRICT")
            payload["adds"].append({
                "attributes": {
                    "Senate_District_Name": district,
                    "parentglobalid": st.session_state.get("apex_globalid", None),
                },
                "geometry": geom
            })


    # -------------------------------------------------------------------------
    # HOUSE
    # -------------------------------------------------------------------------
    if name == 'house':
        id_list = st.session_state.get(f"{name}_list")
        if not id_list:
            print(None)

        payload = {"adds": []}
        for item_id in id_list:
            data = select_record(
                url = st.session_state['house_intersect']['url'],
                layer = st.session_state['house_intersect']['layer'],
                id_field = "GlobalID", 
                id_value = str(item_id), 
                fields="*", 
                return_geometry=True
            )
            if not data:
                continue
            attrs = data[0].get("attributes", {})
            geom = data[0].get("geometry", {})
            house_num = attrs.get("DISTRICT")
            house_name = attrs.get("HOUSE_NAME")
            senate = attrs.get("SENATE_DISTRICT")
            payload["adds"].append({
                "attributes": {
                    "House_District_Num": house_num,
                    "House_District_Name": house_name,
                    "House_Senate_District": senate,
                    "parentglobalid": st.session_state.get("apex_globalid", None),
                },
                "geometry": geom
            })

    return clean_payload(payload, 'adds')




def parent_traffic_impact_payload():
    try:
        # Use the polygon produced during project payload build
        esri_polygon_input = st.session_state.get("project_esri_polygon")
        if not isinstance(esri_polygon_input, dict) or "rings" not in esri_polygon_input:
            raise RuntimeError("Missing or invalid project_esri_polygon (expected ESRI polygon dict with 'rings').")

        # If you want a tiny expansion, buffer the rings; otherwise, you could skip buffering and use esri_polygon_input directly.
        rings = esri_polygon_input.get("rings")  # <-- list of rings (each: [[lon,lat], ...])
        buffers = create_buffers(geometry_list=rings, geom_type="polygon", distance_m=1000)
        if not buffers:
            raise RuntimeError("Buffering produced no output (check geometry and distances).")

        esri_polygon = {
            "rings": buffers,
            "spatialReference": {"wkid": 4326},
        }

        # Build a single valid feature combining attributes + geometry
        feature = {
            "attributes": {
                "Event_Name": "Blank Traffic Impact",
                "DOT_PF_Proj_Phone_COMM": "NIE",
                "Agency_Name_COMM": "NIE",
                "Agency_Phone_COMM": "NIE",
                "Contractor_Name_COMM": "NIE",
                "Contractor_Phone_COMM": "NIE",
                "Event_Type_COMM": "Roadwork / Maintenance",
                "Full_Closure_COMM": "NIE",
                "Status_COMM": "NIE",
                "Description_COMM": "NIE",
                "Broadcast_COMM": "NIE",
                "Notes_for_Approver": "No Notes",
                "Notes_for_Next_Week": "No Notes",
                "Drafter": "Unassigned",
                "Approver": "Unassigned",
                "Alaska_511_Comm": "NIE",
                "Log_Status": "Blank",
                "Agency_Name":"DOT&PF",
                "APEX_GUID": st.session_state.get("apex_globalid").strip("{}"),
                "AWP_Proj_Name": st.session_state.get("awp_proj_name"),
                "Proj_Name": st.session_state.get("proj_name"),
                "Construction_Year": st.session_state.get("construction_year", None),
                "DOT_Region": st.session_state.get("region_string", None)
            },
            "geometry": esri_polygon,
        }

        payload = {"adds": [feature]}
        return clean_payload(payload, "adds")

    except Exception as e:
        # Bubble up error so caller can handle with st.error
        raise RuntimeError(f"Error building parent traffic impact payload: {e}")
    


def child_traffic_impact_payload():

    try:
        if st.session_state.get('load_ti_guid', None):
            guid = st.session_state['load_ti_guid']

        # Build payload with attributes (required by /addFeatures on related tables)
        payload = {
            "adds": [
                {
                    "attributes": {
                        "parentglobalid": guid # e.g., d8f0951c-4259-4077-bf41-9646bc0fe2a3
                    }
                }
            ]
        }
        
        return clean_payload(payload, 'adds')
    
    except Exception as e:
        # Bubble up error so caller can handle with st.error
        raise RuntimeError(f"Error building child traffic impact payload: {e}")
    


def awp_apex_update_payload(awp_id):
    try:
        # Find year for project upload
        cy_year = st.session_state.get("construction_year", None)
        if cy_year in [None, ""]:
            raise ValueError("Session state value 'construction_year' is missing.")

        # Normalize incoming year to AGOL storage format: CY####
        cy_year_str = str(cy_year).strip()
        year_match = re.search(r"(\d{4})", cy_year_str)
        if not year_match:
            raise ValueError(f"Invalid construction_year value: {cy_year!r}")

        new_year_value = f"CY{year_match.group(1)}"

        # Locate AWP entry in existing table
        awp_apex_url = st.session_state.get("aashtoware_url")
        awp_apex_layer = st.session_state.get("awp_contracts_layer")

        if not awp_apex_url:
            raise ValueError("Session state value 'apex_contacts_url' is missing.")
        if awp_apex_layer in [None, ""]:
            raise ValueError("Session state value 'awp_contracts_layer' is missing.")

        # Find entry in table and locate existing years
        data = select_record(
            url=awp_apex_url,
            layer=awp_apex_layer,
            id_field="Id",
            id_value=awp_id,
            fields="OBJECTID,Id,ConstructionYears",
            return_geometry=False
        )

        if not data:
            raise ValueError(f"No AWP/APEX record found for Id={awp_id!r}.")

        # Handle possible select_record return shapes
        if isinstance(data, dict) and "features" in data:
            if not data["features"]:
                raise ValueError(f"No AWP/APEX record found for Id={awp_id!r}.")
            record = data["features"][0]
        elif isinstance(data, list):
            if not data:
                raise ValueError(f"No AWP/APEX record found for Id={awp_id!r}.")
            record = data[0]
        else:
            record = data

        attrs = record.get("attributes", record) if isinstance(record, dict) else {}
        if not isinstance(attrs, dict):
            raise ValueError("Returned record is not in an expected dictionary format.")

        object_id = attrs.get("OBJECTID")
        if object_id in [None, ""]:
            raise ValueError("Returned record does not include OBJECTID.")

        # Pull ConstructionYears value from data
        existing_years_raw = attrs.get("ConstructionYears", None)

        existing_years = []
        if existing_years_raw not in [None, ""]:
            existing_years = [
                item.strip()
                for item in str(existing_years_raw).split(",")
                if item and item.strip()
            ]

        # If year already exists, do not return anything
        normalized_existing = {item.upper() for item in existing_years}
        if new_year_value.upper() in normalized_existing:
            return None

        # Add new year to existing list
        updated_years = existing_years + [new_year_value]
        updated_years_str = ", ".join(updated_years)

        # Create updates payload for AGOL applyEdits
        payload = {
            'updates': [
                {
                    "attributes": {
                        "OBJECTID": object_id,
                        "ConstructionYears": updated_years_str
                    }
                }
            ]
        }

        return payload

    except ValueError as ve:
        return {
            "success": False,
            "message": f"Validation error building AWP/APEX update payload: {ve}"
        }
    except KeyError as ke:
        return {
            "success": False,
            "message": f"Missing expected key while building AWP/APEX update payload: {ke}"
        }
    except Exception as ex:
        return {
            "success": False,
            "message": f"Unexpected error building AWP/APEX update payload: {ex}"
        }


    except Exception as e:
        # Bubble up error so caller can handle with st.error
        raise RuntimeError(f"Error building AWP to APEX update payload: {e}")





def manage_traffic_impact_payloads(package: dict, edit_type=None, which: str = "all") -> dict:
    """
    Build ArcGIS applyEdits payloads for:
    - parent (Traffic Impact polygon)
    - route (polyline child)
    - start (start point child)
    - end (end point child)

    Data source:
    * Everything comes from the provided `package` (single source of truth).
    * For updates, objectids are taken from the package if present:
      - parent: package["objectid"]
      - route : package["route_objectid"]
      - start : package["start_objectid"]
      - end   : package["end_objectid"]
    * For adds to child layers, attributes MUST include:
      {"parentglobalid": st.session_state["traffic_impact_globalid"]}

    Geometry expectations (all in [lon, lat]):
    * parent polygon: sliced route between start/end point buffered 50m via slice_and_buffer_route()
    * route polyline: package["route_geom"] -> {"paths": [route_geom]}
    * start point  : package["start_point"]["lonlat"] -> {"x": lon, "y": lat}
    * end point    : package["end_point"]["lonlat"]   -> {"x": lon, "y": lat}

    Parameters
    ----------
    package   : dict
    edit_type : {'adds','updates','deletes'} | None
        If None, infer as:
        - 'deletes' if package.get('delete') or package.get('action') in ('delete','deletes')
        - 'updates' if any objectid is present in package
        - else 'adds'
    which : {'all','parent','children'}
        'all'      -> return parent + route + start + end
        'parent'   -> return parent only
        'children' -> return route + start + end only

    Returns
    -------
    dict with selected payload dicts, e.g.:
    {
        "parent": {<applyEdits section>},
        "route" : {<applyEdits section>},
        "start" : {<applyEdits section>},
        "end"   : {<applyEdits section>}
    }
    """

    if not isinstance(package, dict):
        raise ValueError("package must be a dict produced by the selector")

    which = (which or "all").strip().lower()
    if which not in ("all", "parent", "children"):
        raise ValueError("which must be one of: 'all', 'parent', 'children'")

    # ---- helpers ----
    def _et_infer():
        if package.get("delete") or package.get("action") in ("delete", "deletes"):
            return "deletes"
        if any(
            k in package and package.get(k) not in (None, "", 0)
            for k in ("objectid", "route_objectid", "start_objectid", "end_objectid")
        ):
            return "updates"
        return "adds"

    et = (edit_type or _et_infer()).strip().lower()
    if et in ("update",):
        et = "updates"
    if et in ("delete",):
        et = "deletes"
    if et not in ("adds", "updates", "deletes"):
        raise ValueError("edit_type must be 'adds', 'updates', or 'deletes'")

    # Extract core pieces once
    route_geom = package.get("route_geom")
    sp = (package.get("start_point") or {}).get("lonlat")
    ep = (package.get("end_point") or {}).get("lonlat")

    parent_oid = (
        package.get("objectid")
        or package.get("OBJECTID")
        or package.get("objectId")
        or package.get("ti_objectid")
    )
    route_oid = package.get("route_objectid") or package.get("routeObjectId") or package.get("route_OBJECTID")
    start_oid = package.get("start_objectid") or package.get("startObjectId") or package.get("start_OBJECTID")
    end_oid   = package.get("end_objectid")   or package.get("endObjectId")   or package.get("end_OBJECTID")

    # Common field extras (attributes pulled only from package)
    route_id   = package.get("route_id")
    route_name = package.get("route_name")
    event_name = package.get("name") or (f"Traffic Impact @ {route_name}" if route_name else "Traffic Impact")

    out: dict = {}

    # -------------------------
    # PARENT PAYLOAD (polygon)
    # -------------------------
    if which in ("all", "parent"):
        if et == "deletes":
            if parent_oid is None:
                raise ValueError("Parent delete requires package['objectid'].")
            parent_payload = {"deletes": [parent_oid]}
        else:
            # adds/updates: build parent geometry from sliced + buffered route segment
            if not (isinstance(route_geom, (list, tuple)) and len(route_geom) >= 2):
                raise ValueError("Parent polygon requires package['route_geom'] as a list of [lon, lat] pairs.")
            if not (isinstance(sp, (list, tuple)) and len(sp) == 2):
                raise ValueError("Parent polygon requires package['start_point']['lonlat'] as [lon, lat].")
            if not (isinstance(ep, (list, tuple)) and len(ep) == 2):
                raise ValueError("Parent polygon requires package['end_point']['lonlat'] as [lon, lat].")

            buffer_rings = slice_and_buffer_route(route_geom, sp, ep, distance_m=50)
            parent_geom  = {"rings": buffer_rings, "spatialReference": {"wkid": 4326}}

            if et == "adds":
                parent_attrs = {
                    "Event_Name":             "Blank Traffic Impact",
                    "Route_ID":               route_id,
                    "Route_Name":             route_name,
                    "Start_X":                (sp[0] if isinstance(sp, (list, tuple)) and len(sp) == 2 else None),
                    "Start_Y":                (sp[1] if isinstance(sp, (list, tuple)) and len(sp) == 2 else None),
                    "End_X":                  (ep[0] if isinstance(ep, (list, tuple)) and len(ep) == 2 else None),
                    "End_Y":                  (ep[1] if isinstance(ep, (list, tuple)) and len(ep) == 2 else None),
                    "DOT_PF_Proj_Phone_COMM": "NIE",
                    "Agency_Name_COMM":       "NIE",
                    "Agency_Phone_COMM":      "NIE",
                    "Contractor_Name_COMM":   "NIE",
                    "Contractor_Phone_COMM":  "NIE",
                    "Event_Type_COMM":        "Roadwork / Maintenance",
                    "Full_Closure_COMM":      "NIE",
                    "Status_COMM":            "NIE",
                    "Description_COMM":       "NIE",
                    "Broadcast_COMM":         "NIE",
                    "Alaska_511_Comm":        "NIE",
                    "Notes_for_Approver":     "No Notes",
                    "Notes_for_Next_Week":    "No Notes",
                    "Drafter":                "Unassigned",
                    "Approver":               "Unassigned",
                    "Log_Status":             "Inactive",
                    "APEX_GUID":              st.session_state.get("apex_guid", None),
                    "AWP_Proj_Name":          st.session_state.get("apex_awp_name", None),
                    "Proj_Name":              st.session_state.get("apex_proj_name", None),
                    "DOT_Region":             st.session_state.get("apex_region_string", None)
                }
                parent_payload = {"adds": [{"attributes": parent_attrs, "geometry": parent_geom}]}
            else:  # updates
                if parent_oid is None:
                    raise ValueError("Parent update requires package['objectid'].")
                parent_attrs = {
                    "objectId":   parent_oid,
                    "Route_ID":   route_id,
                    "Route_Name": route_name,
                    "Start_X":    sp[0],
                    "Start_Y":    sp[1],
                    "End_X":      ep[0],
                    "End_Y":      ep[1],
                }
                parent_payload = {"updates": [{"attributes": parent_attrs, "geometry": parent_geom}]}

        def _clean_parent(p):
            return clean_payload(p, et) if "clean_payload" in globals() else p
        out["parent"] = _clean_parent(parent_payload)

    # ---------------------------------
    # CHILDREN (route, start, end)
    # ---------------------------------
    if which in ("all", "children"):
        # ROUTE (polyline)
        if et == "deletes":
            if route_oid is None:
                raise ValueError("Route delete requires package['route_objectid'].")
            route_payload = {"deletes": [route_oid]}
        else:
            if not (isinstance(route_geom, (list, tuple)) and len(route_geom) >= 2):
                raise ValueError("Route geometry requires package['route_geom'] as a list of [lon, lat] pairs.")
            route_geo = {"paths": [route_geom], "spatialReference": {"wkid": 4326}}
            if et == "adds":
                route_attrs = {"parentglobalid": st.session_state.get("traffic_impact_globalid")}
                if not route_attrs["parentglobalid"]:
                    raise ValueError("Adds require st.session_state['traffic_impact_globalid'] for child layers.")
                route_payload = {"adds": [{"attributes": route_attrs, "geometry": route_geo}]}
            else:
                if route_oid is None:
                    raise ValueError("Route update requires package['route_objectid'].")
                route_attrs = {"objectId": route_oid}
                route_payload = {"updates": [{"attributes": route_attrs, "geometry": route_geo}]}

        # START (point)
        if et == "deletes":
            if start_oid is None:
                raise ValueError("Start-point delete requires package['start_objectid'].")
            start_payload = {"deletes": [start_oid]}
        else:
            if not (isinstance(sp, (list, tuple)) and len(sp) == 2):
                raise ValueError("Start-point requires package['start_point']['lonlat'] as [lon, lat].")
            start_geo = {"x": float(sp[0]), "y": float(sp[1]), "spatialReference": {"wkid": 4326}}
            if et == "adds":
                start_attrs = {"parentglobalid": st.session_state.get("traffic_impact_globalid")}
                if not start_attrs["parentglobalid"]:
                    raise ValueError("Adds require st.session_state['traffic_impact_globalid'] for child layers.")
                start_payload = {"adds": [{"attributes": start_attrs, "geometry": start_geo}]}
            else:
                if start_oid is None:
                    raise ValueError("Start-point update requires package['start_objectid'].")
                start_attrs = {"objectId": start_oid}
                start_payload = {"updates": [{"attributes": start_attrs, "geometry": start_geo}]}

        # END (point)
        if et == "deletes":
            if end_oid is None:
                raise ValueError("End-point delete requires package['end_objectid'].")
            end_payload = {"deletes": [end_oid]}
        else:
            if not (isinstance(ep, (list, tuple)) and len(ep) == 2):
                raise ValueError("End-point requires package['end_point']['lonlat'] as [lon, lat].")
            end_geo = {"x": float(ep[0]), "y": float(ep[1]), "spatialReference": {"wkid": 4326}}
            if et == "adds":
                end_attrs = {"parentglobalid": st.session_state.get("traffic_impact_globalid")}
                if not end_attrs["parentglobalid"]:
                    raise ValueError("Adds require st.session_state['traffic_impact_globalid'] for child layers.")
                end_payload = {"adds": [{"attributes": end_attrs, "geometry": end_geo}]}
            else:
                if end_oid is None:
                    raise ValueError("End-point update requires package['end_objectid'].")
                end_attrs = {"objectId": end_oid}
                end_payload = {"updates": [{"attributes": end_attrs, "geometry": end_geo}]}

        def _clean_child(p):
            return clean_payload(p, et) if "clean_payload" in globals() else p
        out["route"] = _clean_child(route_payload)
        out["start"] = _clean_child(start_payload)
        out["end"]   = _clean_child(end_payload)

    return out


# -----------------------------------------------------------------------------
# Build & deploy payloads to AGOL for add/update/delete (single point layer)
# -----------------------------------------------------------------------------
def manage_communities_payloads(package_out: dict, edit_type: str) -> dict:
    """
    Build an applyEdits payload for the Impacted Communities layer
    directly from `package_out` returned by select_community().

    Supported edit_type: 'adds', 'updates', 'deletes'
    """

    if not isinstance(package_out, dict):
        raise ValueError("package_out must be a dict")

    # --------------------------------------------------
    # Extract required inputs
    # --------------------------------------------------
    attrs_in = dict(package_out.get("attributes") or {})
    point = package_out.get("point") or {}

    apex_guid = st.session_state.get("apex_guid")
    if not apex_guid:
        raise ValueError("Missing apex_guid in session_state")

    # --------------------------------------------------
    # Geometry (POINT) — STRICT lon/lat handling
    # --------------------------------------------------
    lng = point.get("lng")
    lat = point.get("lat")

    if lng is None or lat is None:
        raise ValueError("Community payload missing valid point geometry")

    geometry = {
        "x": float(lng),
        "y": float(lat),
        "spatialReference": {"wkid": 4326},
    }

    # --------------------------------------------------
    # Attributes — explicit field mapping
    # --------------------------------------------------
    attributes = {
        "parentglobalid": apex_guid,
        "Community_Name": attrs_in.get("Community_Name"),
        "Community_Contact": attrs_in.get("Community_Contact"),
        "Community_Contact_Email": attrs_in.get("Community_Contact_Email"),
        "Community_Contact_Phone": attrs_in.get("Community_Contact_Phone"),
    }

    # --------------------------------------------------
    # ADDS
    # --------------------------------------------------
    if edit_type == "adds":
        return {
            "adds": [
                {
                    "attributes": attributes,
                    "geometry": geometry,
                }
            ]
        }

    # --------------------------------------------------
    # UPDATES
    # --------------------------------------------------
    if edit_type == "updates":
        objectid = package_out.get("objectid")
        if objectid is None:
            raise ValueError("UPDATE requires OBJECTID")

        attributes["OBJECTID"] = objectid

        return {
            "updates": [
                {
                    "attributes": attributes,
                    "geometry": geometry,
                }
            ]
        }

    # --------------------------------------------------
    # DELETES
    # --------------------------------------------------
    if edit_type == "deletes":
        objectid = package_out.get("objectid")
        if objectid is None:
            raise ValueError("DELETE requires OBJECTID")

        return {
            "deletes": [objectid]
        }

    raise ValueError(f"Unsupported edit_type '{edit_type}'")






def manage_information_payload(package_out: dict, edit_type: str) -> dict:
    """
    Build an AGOL applyEdits payload for the Project Information layer.

    - Uses fmt_date on any date/datetime fields in the package (and on known date keys).
    - Supports ONLY 'adds' and 'updates' (no deletes).
    - Normalizes integer-like fields via str_to_int.
    - Runs through clean_payload(..) before returning.
    """
    if not isinstance(package_out, dict):
        raise ValueError("package_out must be a dict")

    et = (edit_type or "").strip().lower()
    if et not in ("adds", "updates"):
        raise ValueError("manage_information supports only 'adds' or 'updates'")

    # Copy source-of-truth attributes
    attrs = dict(package_out)

    # -----------------------------
    # OBJECTID handling (updates)
    # -----------------------------
    if et == "updates":
        oid = (
            attrs.pop("objectid", None)
            or attrs.pop("OBJECTID", None)
            or attrs.pop("objectId", None)
        )
        if oid is None:
            raise ValueError("UPDATE requires a valid OBJECTID")
        attrs["OBJECTID"] = oid  # AGOL expects uppercase key

    # ------------------------------------
    # Date/time coercion using fmt_date
    # ------------------------------------
    # Known date field names from the UI/package
    known_date_keys = {
        "anticipated_start",
        "anticipated_end",
        "award_date",
        "tenadd",
    }

    # 1) Apply fmt_date to all known date keys (string or datetime inputs)
    for k in known_date_keys:
        if k in attrs:
            attrs[k] = fmt_date(attrs[k])  # -> "MM/DD/YYYY" or ""

    # 2) Safety net: if any remaining value is a date/datetime object, fmt_date it
    import datetime as _dt
    for k, v in list(attrs.items()):
        if isinstance(v, (_dt.date, _dt.datetime)):
            attrs[k] = fmt_date(v)

    # 3) Convert empty date strings to None (JSON null) so AGOL doesn't reject them
    for k in known_date_keys:
        if k in attrs and attrs[k] == "":
            attrs[k] = None

    # ------------------------------------
    # Numeric coercions for currency/int fields
    # ------------------------------------
    numeric_int_keys = (
        "awarded_amount",
        "current_contract_amount",
        "amount_paid_to_date",
        "award_fiscal_year",
    )
    for k in numeric_int_keys:
        if k in attrs:
            attrs[k] = str_to_int(attrs[k])

    # Convert empty numeric strings to None (JSON null)
    for k in numeric_int_keys:
        if k in attrs and attrs[k] == "":
            attrs[k] = None

    # -----------------------------
    # Return applyEdits payload
    # -----------------------------
    if et == "updates":
        payload = {"updates": [{"attributes": attrs}]}
        return clean_payload(payload, 'updates')

    payload = {"adds": [{"attributes": attrs}]}
    return clean_payload(payload, "adds")




def manage_project_name_update(
    url,
    layer,
    id_field,
    guid,
    package_out: dict,
    edit_type: str
):
    """
    Build an AGOL applyEdits payload.

    - Finds ALL records matching the id_field/guid
    - Dynamically resolves field names based on returned record attributes
    - Supports prefixed fields (Site_, Route_, Boundary_, etc.)
    - Builds ONE applyEdits payload with an update entry per OBJECTID
    - Supports ONLY 'adds' and 'updates'
    """

    if not isinstance(package_out, dict):
        raise ValueError("package_out must be a dict")

    et = (edit_type or "").strip().lower()
    if et not in ("adds", "updates"):
        raise ValueError("manage_project_name_update supports only 'adds' or 'updates'")

    # ---------------------------------
    # Query matching records
    # ---------------------------------
    recs = select_record(
        url=url,
        layer=layer,
        id_field=id_field,
        id_value=guid,
        return_geometry=False
    )

    if not recs:
        raise ValueError("No records found matching the provided guid")

    # ---------------------------------
    # Helper: normalize field names
    # ---------------------------------
    def _normalize(name: str) -> str:
        return name.replace("_", "").lower()

    def _logical_part(field_name: str) -> str:
        """
        Strip prefix up to the last underscore.
        Route_Proj_Name -> Proj_Name
        """
        if "_" not in field_name:
            return field_name
        return field_name.split("_", 1)[-1]

    # ---------------------------------
    # ADDs (unchanged behavior)
    # ---------------------------------
    if et == "adds":
        attrs = dict(package_out)
        payload = {"adds": [{"attributes": attrs}]}
        return clean_payload(payload, "adds")

    # ---------------------------------
    # UPDATEs
    # ---------------------------------
    updates = []

    for rec in recs:
        rec_attrs = rec.get("attributes", {})

        oid = (
            rec_attrs.get("objectid")
            or rec_attrs.get("OBJECTID")
            or rec_attrs.get("objectId")
        )

        if oid is None:
            continue

        resolved_attrs = {}

        # Resolve package fields against record schema
        for pkg_key, pkg_val in package_out.items():
            pkg_norm = _normalize(pkg_key)

            matched_field = None
            for rec_field in rec_attrs.keys():
                logical = _logical_part(rec_field)
                if _normalize(logical) == pkg_norm:
                    matched_field = rec_field
                    break

            if matched_field:
                resolved_attrs[matched_field] = pkg_val

        if not resolved_attrs:
            continue

        resolved_attrs["OBJECTID"] = oid
        updates.append({"attributes": resolved_attrs})

    if not updates:
        raise ValueError("No valid update attributes resolved for any record")

    payload = {"updates": updates}
    return clean_payload(payload, "updates")




def manage_deployment_payload(package_out: dict, edit_type: str) -> dict:
    """
    Build an AGOL applyEdits payload for deployment updates.

    Supports ONLY 'updates' (no adds / no deletes).

    Accepts either:
      1) a single record package, e.g.
         {
             "database_status": ...,
             "target_applications": ...,
             "deployment_version": ...,
             "objectid": ...
         }

      2) a nested parent package, e.g.
         {
             "apex": {...},
             "Location": {...},
             "Site": {...},
             "Route": {...},
             "Boundary": {...}
         }

    For nested packages, each child dict that contains an OBJECTID/objectid
    becomes one entry under "updates".

    Target application fields are normalized to comma+space-delimited strings.
    Payload is passed through clean_payload(..) before return.
    """

    if not isinstance(package_out, dict):
        raise ValueError("package_out must be a dict")

    et = (edit_type or "").strip().lower()
    if et != "updates":
        raise ValueError("manage_deployment_payload supports only 'updates'")

    nested_keys = ("apex", "Location", "Site", "Route", "Boundary")

    def _normalize_target_application_fields(attrs: dict) -> dict:
        """Normalize any target applications field to a comma+space string."""
        normalized = dict(attrs)

        for key in list(normalized.keys()):
            key_lower = str(key).lower()

            if key_lower == "target_applications" or key_lower.endswith("_target_applications"):
                val = normalized[key]

                if val is None:
                    normalized[key] = ""
                elif isinstance(val, (list, tuple, set)):
                    normalized[key] = ", ".join(
                        str(v).strip() for v in val if v not in (None, "")
                    )
                else:
                    normalized[key] = str(val).strip()

        return normalized

    def _build_update_attributes(record_dict: dict) -> dict:
        """Build one update attributes dict with normalized OBJECTID."""
        if not isinstance(record_dict, dict):
            raise ValueError("Each record package must be a dict")

        attrs = dict(record_dict)

        oid = (
            attrs.pop("objectid", None)
            or attrs.pop("OBJECTID", None)
            or attrs.pop("objectId", None)
        )

        if oid is None:
            raise ValueError("UPDATE requires a valid OBJECTID")

        attrs["OBJECTID"] = oid
        attrs = _normalize_target_application_fields(attrs)

        return attrs

    updates = []

    # If this is the new parent package shape, build updates for each child package present.
    if any(isinstance(package_out.get(k), dict) for k in nested_keys):
        for key in nested_keys:
            record_pkg = package_out.get(key)
            if not isinstance(record_pkg, dict):
                continue

            oid = (
                record_pkg.get("objectid")
                or record_pkg.get("OBJECTID")
                or record_pkg.get("objectId")
            )
            if oid is None:
                continue

            updates.append({"attributes": _build_update_attributes(record_pkg)})

    else:
        # Single-record package
        updates.append({"attributes": _build_update_attributes(package_out)})

    if not updates:
        raise ValueError("No valid update records were found in package_out")

    payload = {"updates": updates}
    return clean_payload(payload, "updates")





def manage_footprint_project_payload(objectid):
    try:
        # Determine center based on selected geometry
        if st.session_state.get("selected_point"):
            proj_type = "Site"
            geoms = st.session_state['selected_point']
            geom_type = 'point'
        elif st.session_state.get("selected_route"):
            proj_type = "Route"
            geoms = st.session_state['selected_route']
            geom_type = 'line'
        elif st.session_state.get("selected_boundary"):
            proj_type = "Boundary"
            geoms = st.session_state['selected_boundary']
            geom_type = 'polygon'

        if not geoms or not isinstance(geoms, (list, tuple)):
            raise RuntimeError("No project geometries available in session.")

        # --- Create buffers (fixed distances by kind) ---
        buffer = create_buffers(geometry_list=geoms, geom_type=geom_type, distance_m=.01)
    
        if not buffer:
            raise RuntimeError("Buffering produced no output (check geometry and distances).")

        # --- ESRI Polygon geometry (multipart via rings) ---
        esri_polygon = {
            "rings": buffer,  # list of rings (each is [[lon, lat], ...])
            "spatialReference": {"wkid": 4326},
        }

        # >>> NEW: expose this polygon for Traffic Impact fallback use <<<
        st.session_state["project_esri_polygon"] = esri_polygon

        # Build payload with .get() and default None
        payload = {
            "updates": [
                {
                    "attributes": {
                        'OBJECTID':objectid,
                        "Proj_Type": proj_type,
                        "List_DOT_PF_Region": st.session_state.get("region_string", None),
                        "List_Borough_Census_Area": st.session_state.get("borough_string", None),
                        "List_Senate_District": st.session_state.get("senate_string", None),
                        "List_House_District": st.session_state.get("house_string", None)
                    },
                    "geometry": esri_polygon
                }
            ]
        }
        return clean_payload(payload, 'updates')
        
    except Exception as e:
        # Bubble up error so caller can handle with st.error
        raise RuntimeError(f"Error building project payload: {e}")
    




def manage_footprint_deletes_payload(objectids: Any) -> Dict[str, List[int]]:
    """
    Craft an AGOL applyEdits deletes payload from a list (or single) OBJECTID(s).

    Returns:
        {"deletes": [<int>, <int>, ...]}

    Notes:
      - Accepts: None, int, str-int, list/tuple/set of ints/str-ints
      - Filters: None/blank/invalid values
      - Dedupes while preserving original order
    """
    if objectids is None:
        return {"deletes": []}

    # Normalize to an iterable of candidate values
    if isinstance(objectids, (int,)):
        candidates = [objectids]
    elif isinstance(objectids, str):
        s = objectids.strip()
        if not s:
            return {"deletes": []}
        # If caller passed a comma-separated string, support it
        if "," in s:
            candidates = [p.strip() for p in s.split(",")]
        else:
            candidates = [s]
    elif isinstance(objectids, (list, tuple, set)):
        candidates = list(objectids)
    else:
        # Unknown type: best effort wrap
        candidates = [objectids]

    deletes: List[int] = []
    seen = set()

    for val in candidates:
        if val is None:
            continue
        if isinstance(val, str):
            v = val.strip()
            if not v:
                continue
            # allow numeric strings
            try:
                oid = int(v)
            except Exception:
                continue
        else:
            try:
                oid = int(val)
            except Exception:
                continue

        if oid in seen:
            continue
        seen.add(oid)
        deletes.append(oid)

    return {"deletes": deletes}






