# review.py
import streamlit as st
from streamlit_folium import st_folium
import folium
from util.map_util import (
    set_bounds_point, set_bounds_route, set_bounds_boundary,
    set_zoom, set_center, geometry_to_folium
)

# ----------------------------------------------------------------------
# Navigation helpers
# ----------------------------------------------------------------------
def goto_step(target_step: int):
    """Set the wizard step (loader_step) and rerun immediately."""
    st.session_state["loader_step"] = target_step          # ✅ use the app's key
    st.session_state["scroll_to_top"] = True
    st.rerun()

def header_with_edit(title: str, target_step: int, *, help: str = None):
    """Render a left-aligned section header with a right-aligned EDIT button."""
    left, right = st.columns([1, 0.4])
    with left:
        st.markdown(f"""
##### {title}
        """, unsafe_allow_html=True)
    with right:
        is_clicked = st.button("⬅️ JUMP TO SECTION", help=help, key=f"edit_{target_step}")
        if is_clicked:
            goto_step(target_step)                         # ✅ ensure rerun + correct key

# ----------------------------------------------------------------------
# Review page
# ----------------------------------------------------------------------
def review_information():
    """
    Render the review page with section headers and edit buttons.
    """
    # --- Project Name ---
    project_name = st.session_state.get("proj_name", "")
    awp_proj_name = st.session_state.get("awp_proj_name", "—")
    display_name = project_name if project_name else awp_proj_name
    st.markdown(f"""
#### {display_name}
    """, unsafe_allow_html=True)

    # --- Map of Location ---
    header_with_edit("PROJECT FOOTPRINT", target_step=3, help="Edit Project Loaction")

    if "selected_point" in st.session_state and st.session_state["selected_point"]:
        # Points: expecting a list of [lon, lat] pairs
        coords = st.session_state["selected_point"]
        bounds = set_bounds_point(coords)
        center = set_center(bounds)
        m = folium.Map(location=[center[0], center[1]])
        geometry_to_folium(
            coords, feature_type="multipoint",
            icon=folium.Icon(color="blue"),
            tooltip="Uploaded Point"
        ).add_to(m)
        m.fit_bounds(bounds)
        st_folium(m, width=700, height=400)

    elif "selected_route" in st.session_state and st.session_state["selected_route"]:
        BLUE = "#3388ff"
        coords = st.session_state["selected_route"]
        bounds = set_bounds_route(coords)
        center = set_center(bounds)
        m = folium.Map(
            location=[center[0], center[1]],
            zoom_start=max(1, (set_zoom(bounds) or 8) - 1)
        )
        geometry_to_folium(coords, feature_type="polyline", color=BLUE, weight=5, opacity=1).add_to(m)
        m.fit_bounds(bounds)
        st_folium(m, width=700, height=400)

    elif "selected_boundary" in st.session_state and st.session_state["selected_boundary"]:
        BLUE = "#3388ff"
        coords = st.session_state["selected_boundary"]
        bounds = set_bounds_boundary(coords)
        center = set_center(bounds)
        m = folium.Map(location=[center[0], center[1]], zoom_start=set_zoom(bounds))
        geometry_to_folium(
            coords, feature_type="polygon",
            color=BLUE, weight=3, opacity=1,
            fill=True, fill_color=BLUE, fill_opacity=0.2
        ).add_to(m)
        m.fit_bounds(bounds)
        st_folium(m, width=700, height=400)
    else:
        st.info("No location data available to display a map.")

    st.write("")
    st.write("")

    # --- Project Information ---
    header_with_edit("PROJECT INFORMATION", target_step=2, help="Edit all project information")

    # Identification
    with st.expander("Identification", expanded=True):
        
        if st.session_state.get("current_option") == "AASHTOWare Database":
            st.markdown(f"**AASHTOWare Project Name:** {st.session_state.get('awp_proj_name','')}")
            st.markdown(f"**Public Project Name:** {st.session_state.get('proj_name','')}")
        
        else:
            st.markdown(f"**Public Project Name:** {st.session_state.get('proj_name','')}")
        
        
        col1, col2 = st.columns(2)
        col1.markdown(f"**Construction Year:** {st.session_state.get('construction_year','')}")
        col2.markdown(f"**Phase:** {st.session_state.get('phase','')}")
        col1.markdown(f"**IRIS:** {st.session_state.get('iris','')}")
        col2.markdown(f"**STIP:** {st.session_state.get('stip','')}")
        col1.markdown(f"**Federal Project Number:** {st.session_state.get('fed_proj_num','')}")
        col2.markdown(f"**Fund Type:** {st.session_state.get('fund_type','')}")
        col1.markdown(f"**Practice:** {st.session_state.get('proj_prac','')}")
        
    # Timeline
    with st.expander("Timeline", expanded=True):
        col1, col2 = st.columns(2)
        col1.markdown(f"**Anticipated Start:** {st.session_state.get('anticipated_start','')}")
        col2.markdown(f"**Anticipated End:** {st.session_state.get('anticipated_end','')}")

    # Funding
    with st.expander("Award Information", expanded=True):
        col1, col2 = st.columns(2)
        
        col1.markdown(f"**Award Date:** {st.session_state.get('award_date','')}")
        col2.markdown(f"**Award Fiscal Year:** {st.session_state.get('award_fiscal_year','')}")
        col1.markdown(f"**Contractor:** {st.session_state.get('contractor','')}")
        col2.markdown(
            "**Awarded Amount:** "
            + (
                # If number → currency format
                "${:,.0f}".format(st.session_state["awarded_amount"])
                if isinstance(st.session_state.get("awarded_amount"), (int, float))
                # If string → print raw
                else st.session_state.get("awarded_amount")
                if isinstance(st.session_state.get("awarded_amount"), str)
                # Otherwise → blank
                else ""
            )
        )
        col1.markdown(
            "**Current Contract Amount:** "
            + (
                "${:,.0f}".format(st.session_state["current_contract_amount"])
                if isinstance(st.session_state.get("current_contract_amount"), (int, float))
                else st.session_state.get("current_contract_amount")
                if isinstance(st.session_state.get("current_contract_amount"), str)
                else ""
            )
        )
        col2.markdown(
            "**Amount Paid to Date:** "
            + (
                "${:,.0f}".format(st.session_state["amount_paid_to_date"])
                if isinstance(st.session_state.get("amount_paid_to_date"), (int, float))
                else st.session_state.get("amount_paid_to_date")
                if isinstance(st.session_state.get("amount_paid_to_date"), str)
                else ""
            )
        )
        col1.markdown(f"**Tenative Advertise Date:** {st.session_state.get('tenadd','')}")


    # Narrative
    with st.expander("Description", expanded=True):
        
        if st.session_state.get("current_option") == "AASHTOWare Database":
            st.markdown(f"**AASHTOWare Description:**\n\n{st.session_state.get('awp_proj_desc','')}")
            st.write("")
            st.markdown(f"**Public Project Description:**\n\n{st.session_state.get('proj_desc','')}")
        else:
            st.markdown(f"**Public Project Description:**\n\n{st.session_state.get('proj_desc','')}")


    # Contact
    with st.expander("Contact", expanded=True):
        st.markdown(f"**Name:** {st.session_state.get('contact_name','—')}")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Email:** {st.session_state.get('contact_email','—')}")
        with col2:
            st.markdown(f"**Phone:** {st.session_state.get('contact_phone','—')}")

    # Web Links
    with st.expander("Web Links", expanded=True):
        st.markdown(f"**Project Website:** {st.session_state.get('proj_web','—')}")
        

    
    # Geography
    with st.expander("Geography", expanded=True):
        col1, col2 = st.columns(2)
        col1.markdown(f"**House Districts:** {st.session_state.get('house_string','')}")
        col2.markdown(f"**Senate Districts:** {st.session_state.get('senate_string','')}")
        col1.markdown(f"**Borough/Census Area:** {st.session_state.get('borough_string','')}")
        col2.markdown(f"**DOT&PF Region:** {st.session_state.get('region_string','')}")
    