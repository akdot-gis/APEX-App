import streamlit as st
import re
from agol.agol_district_queries import run_district_queries
from util.read_only_util import ro_widget, ro_widget_taglist  # <- added ro_widget_taglist
from util.input_util import fmt_string
from agol.agol_util import (
    get_multiple_fields,
    select_record
)
from util.input_util import (
    fmt_currency, 
    fmt_date, 
    fmt_date_or_none, 
    fmt_int, 
    fmt_int_or_none, 
    fmt_string,
    fmt_agol_date,
    widget_key
)


def aashtoware_project():
    # ---------------------------------------------------------------------
    # Helpers for construction years (unchanged logic)
    # ---------------------------------------------------------------------
    def _format_construction_years(cy):
        if not cy:
            return ""
        if isinstance(cy, (list, tuple, set)):
            parts = [str(x).strip() for x in cy if x and str(x).strip()]
        else:
            parts = [p.strip() for p in str(cy).split(",") if p.strip()]
        return f"{', '.join(parts)}" if parts else ""

    def _parse_years_to_set(cy):
        if not cy:
            return set()
        if isinstance(cy, (list, tuple, set)):
            return {str(x).strip().upper() for x in cy if x and str(x).strip()}
        return {p.strip().upper() for p in str(cy).split(",") if p.strip()}

    aashtoware = st.session_state["awp_url"]

    # ---------------------------------------------------------------------
    # Pull projects + prep lookups
    # ---------------------------------------------------------------------
    projects = get_multiple_fields(
        aashtoware,
        st.session_state["awp_contracts_layer"],
        ["ProjectName", "IRIS", "ConstructionYears", "Id"]
    ) or []

    gid_to_cy = {
        p.get("Id"): _format_construction_years(p.get("ConstructionYears"))
        for p in projects
        if p.get("Id")
    }

    # Optional filter by URL/session param set_year (e.g., "CY2026")
    set_year_raw = st.session_state.get("set_year", None)
    set_year = str(set_year_raw).strip().upper() if set_year_raw else None

    def _passes_set_year_filter(p):
        if not set_year:
            return True
        years = _parse_years_to_set(p.get("ConstructionYears"))
        return set_year not in years

    selector_exclude_ids = {
        str(x) for x in (st.session_state.get("awp_selector_exclude_ids") or [])
        if x not in (None, "")
    }
    allow_saved_when_filtered = st.session_state.get("awp_selector_allow_saved_when_filtered", True)

    projects_all_sorted = sorted(
        (p for p in projects if p.get("Id") and _passes_set_year_filter(p)),
        key=lambda p: (
            (p.get("ProjectName") or "").strip().lower() == "",
            (p.get("ProjectName") or "").strip().lower()
        )
    )

    all_label_to_gid = {
        f"{p.get('Id', '')} – {p.get('ProjectName', '')}": p.get("Id")
        for p in projects_all_sorted
    }
    all_gid_to_label = {gid: label for label, gid in all_label_to_gid.items()}

    projects_sorted = [
        p for p in projects_all_sorted
        if str(p.get("Id")) not in selector_exclude_ids
    ]

    label_to_gid = {
        f"{p.get('Id', '')} – {p.get('ProjectName', '')}": p.get("Id")
        for p in projects_sorted
    }
    gid_to_label = {gid: label for label, gid in label_to_gid.items()}

    placeholder_label = "— Select a project —"
    labels = [placeholder_label] + list(label_to_gid.keys())

    # ---------------------------------------------------------------------
    # MINIMAL SAFE FIX: restore the dropdown by saved GUID/ID from last submit
    # ---------------------------------------------------------------------
    version = st.session_state.get("form_version", 0)
    widget_key_select = f"awp_project_select_{version}"

    saved_gid = (
        st.session_state.get("awp_id")
        or st.session_state.get("awp_guid")
        or st.session_state.get("aashto_id")
    )
    saved_label = all_gid_to_label.get(saved_gid) if saved_gid else None

    if saved_label and saved_label not in labels and allow_saved_when_filtered:
        labels = [placeholder_label, saved_label] + [
            lab for lab in labels if lab != placeholder_label
        ]

    if saved_label and (saved_label in labels or allow_saved_when_filtered):
        if st.session_state.get(widget_key_select) != saved_label:
            st.session_state[widget_key_select] = saved_label
    else:
        if st.session_state.get(widget_key_select) not in labels:
            st.session_state[widget_key_select] = placeholder_label

    active_gid = st.session_state.get("awp_guid") or st.session_state.get("aashto_id")
    active_label = all_gid_to_label.get(active_gid) if active_gid else None
    if active_gid and active_label and active_label in labels:
        st.session_state["aashto_id"] = active_gid
        st.session_state["aashto_label"] = active_label
        st.session_state["aashto_selected_project"] = active_label
        st.session_state["awp_selected_construction_years"] = gid_to_cy.get(active_gid, "")

    desired_label = st.session_state.get(widget_key_select)
    if desired_label not in labels:
        desired_label = st.session_state.get("aashto_label")
    if desired_label not in labels:
        desired_label = active_label
    if desired_label not in labels:
        desired_label = placeholder_label
    if st.session_state.get(widget_key_select) not in labels:
        st.session_state[widget_key_select] = desired_label

    def _on_project_change():
        selected_label = st.session_state[widget_key_select]

        for k in [k for k in st.session_state.keys()
                  if k.startswith("awp_") and k not in ("awp_fields", "awp_contracts_layer")]:
            try:
                st.session_state.pop(k)
            except Exception:
                pass

        if selected_label == placeholder_label:
            st.session_state["aashto_label"] = None
            st.session_state["aashto_id"] = None
            st.session_state["aashto_selected_project"] = None
            st.session_state["awp_guid"] = None
            st.session_state["awp_update"] = "No"
            st.session_state["awp_selected_construction_years"] = ""
            st.session_state["awp_last_loaded_gid"] = None
            return

        selected_gid = label_to_gid.get(selected_label) or all_label_to_gid.get(selected_label)

        st.session_state["aashto_label"] = selected_label
        st.session_state["aashto_id"] = selected_gid
        st.session_state["aashto_selected_project"] = selected_label
        st.session_state["awp_guid"] = selected_gid
        st.session_state["awp_id"] = selected_gid
        st.session_state["awp_update"] = "Yes"
        st.session_state["awp_selected_construction_years"] = gid_to_cy.get(selected_gid, "")
        st.session_state["awp_last_loaded_gid"] = None

    st.selectbox(
        "AASHTOWare Project List",
        labels,
        key=widget_key_select,
        on_change=_on_project_change,
    )

    selected_gid = st.session_state.get("aashto_id")
    if selected_gid and not st.session_state.get("awp_selected_construction_years"):
        st.session_state["awp_selected_construction_years"] = gid_to_cy.get(selected_gid, "")

    ro_widget_taglist(
        key="awp_selected_construction_years",
        label="Existing Construction Year(s) in APEX",
        values=st.session_state.get("awp_selected_construction_years", ""),
    )
    st.write("")  # spacer

    # ---------------------------------------------------------------------
    # Load form values when the GUID changes
    # ---------------------------------------------------------------------
    last_loaded = st.session_state.get("awp_last_loaded_gid")
    if selected_gid and selected_gid != last_loaded:
        user_keys = [
            "construction_year", "phase", "proj_name", "iris", "stip", "fed_proj_num",
            "fund_type", "proj_prac", "anticipated_start", "anticipated_end",
            "award_date", "award_fiscal_year", "contractor", "awarded_amount",
            "current_contract_amount", "amount_paid_to_date", "tenadd", "proj_desc",
            "awp_contact_name", "awp_contact_role", "awp_contact_email", "awp_contact_phone",
            "proj_web",
            "impact_comm",
        ]
        date_like = {"award_date", "anticipated_start", "anticipated_end", "tenadd"}
        for k in user_keys:
            st.session_state[k] = None if k in date_like else ""

        record = select_record(
            url=st.session_state['awp_url'],
            layer=st.session_state['awp_contracts_layer'],
            id_field="Id",
            id_value=selected_gid,
            return_geometry=False
        )

        if record and "attributes" in record[0]:
            attrs = record[0]["attributes"]
            for k, v in attrs.items():
                st.session_state[f"awp_{k}".lower()] = v

            _awp_to_friendly = {
                "ProjectName": "awp_proj_name",
                "Description": "awp_proj_desc",
                "Phase": "awp_phase",
                "FundingType": "awp_fund_type",
                "ProjectPractice": "awp_proj_prac",
                "IRIS": "awp_iris",
                "STIP": "awp_stip",
                "FederalProjectNumber": "awp_fed_proj_num",
                "AnticipatedStart": "awp_anticipated_start",
                "AnticipatedEnd": "awp_anticipated_end",
                "AwardDate": "awp_award_date",
                "AwardFiscalYear": "awp_award_fiscal_year",
                "TentativeAdvertiseDate": "awp_tenadd",
                "AwardedAmount": "awp_awarded_amount",
                "CurrentContractAmount": "awp_current_contract_amount",
                "AmountPaidToDate": "awp_amount_paid_to_date",
                "ContactName": "awp_contact_name",
                "ContactRole": "awp_contact_role",
                "ContactEmail": "awp_contact_email",
                "ContactPhone": "awp_contact_phone",
                "ProjectWebsite": "awp_proj_web",
                "RouteId": "awp_route_id",
                "RouteName": "awp_route_name",
            }
            for awp_attr, friendly_key in _awp_to_friendly.items():
                if awp_attr in attrs:
                    st.session_state[friendly_key] = attrs[awp_attr]

            st.session_state.setdefault("awp_id", selected_gid)
            st.session_state["awp_last_loaded_gid"] = selected_gid
            st.session_state["awp_selection_changed"] = True


def manage_aashtoware_connection():
    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    def _format_construction_years(cy):
        if not cy:
            return ""
        if isinstance(cy, (list, tuple, set)):
            parts = [str(x).strip() for x in cy if x and str(x).strip()]
        else:
            parts = [p.strip() for p in str(cy).split(",") if p.strip()]
        return f"{', '.join(parts)}" if parts else ""

    def _parse_years_to_set(cy):
        if not cy:
            return set()
        if isinstance(cy, (list, tuple, set)):
            return {str(x).strip().upper() for x in cy if x and str(x).strip()}
        return {p.strip().upper() for p in str(cy).split(",") if p.strip()}

    def _awp_manage_value(key, default=""):
        return st.session_state.get(key, default)

    aashtoware = st.session_state["awp_url"]

    # ---------------------------------------------------------------------
    # Pull projects + prep lookups
    # ---------------------------------------------------------------------
    projects = get_multiple_fields(
        aashtoware,
        st.session_state["awp_contracts_layer"],
        ["ProjectName", "IRIS", "ConstructionYears", "Id"]
    ) or []

    set_year_raw = st.session_state.get("set_year", None)
    set_year = str(set_year_raw).strip().upper() if set_year_raw else None

    def _passes_set_year_filter(p):
        if not set_year:
            return True
        years = _parse_years_to_set(p.get("ConstructionYears"))
        return set_year not in years

    selector_exclude_ids = {
        str(x) for x in (st.session_state.get("awp_selector_exclude_ids") or [])
        if x not in (None, "")
    }
    allow_saved_when_filtered = st.session_state.get("awp_selector_allow_saved_when_filtered", True)

    projects_all_sorted = sorted(
        (p for p in projects if p.get("Id") and _passes_set_year_filter(p)),
        key=lambda p: (
            (p.get("ProjectName") or "").strip().lower() == "",
            (p.get("ProjectName") or "").strip().lower()
        )
    )

    all_label_to_gid = {
        f"{p.get('Id', '')} – {p.get('ProjectName', '')}": p.get("Id")
        for p in projects_all_sorted
    }
    all_gid_to_label = {gid: label for label, gid in all_label_to_gid.items()}

    projects_sorted = [
        p for p in projects_all_sorted
        if str(p.get("Id")) not in selector_exclude_ids
    ]

    label_to_gid = {
        f"{p.get('Id', '')} – {p.get('ProjectName', '')}": p.get("Id")
        for p in projects_sorted
    }
    gid_to_label = {gid: label for label, gid in label_to_gid.items()}

    placeholder_label = "— Select a project —"
    current_tag_prefix = "✅ "
    current_connected_gid = (
        st.session_state.get("awp_manage_existing_id")
        or st.session_state.get("apex_awp_id")
    )

    def _display_label(base_label, gid=None):
        if not base_label:
            return base_label
        if gid and current_connected_gid and str(gid) == str(current_connected_gid):
            return f"{current_tag_prefix}{base_label}"
        return base_label

    def _normalize_label(display_label):
        if isinstance(display_label, str) and display_label.startswith(current_tag_prefix):
            return display_label[len(current_tag_prefix):]
        return display_label

    current_base_label = all_gid_to_label.get(current_connected_gid) if current_connected_gid else None

    display_label_to_gid = {
        _display_label(base_label, gid): gid
        for base_label, gid in label_to_gid.items()
    }
    all_display_label_to_gid = {
        _display_label(base_label, gid): gid
        for base_label, gid in all_label_to_gid.items()
    }
    gid_to_display_label = {
        gid: _display_label(base_label, gid)
        for gid, base_label in all_gid_to_label.items()
    }

    labels = [_display_label(base_label, gid) for base_label, gid in label_to_gid.items()]

    if current_base_label:
        current_display_label = _display_label(current_base_label, current_connected_gid)
        labels = [lab for lab in labels if lab != current_display_label]
        labels = [current_display_label, placeholder_label] + labels
    else:
        current_display_label = None
        labels = [placeholder_label] + labels

    # ---------------------------------------------------------------------
    # Restore dropdown state
    # ---------------------------------------------------------------------
    version = st.session_state.get("form_version", 0)
    widget_key_select = f"awp_manage_project_select_{version}"

    saved_gid = (
        st.session_state.get("awp_manage_id")
        or st.session_state.get("awp_manage_guid")
    )
    saved_label = gid_to_display_label.get(saved_gid) if saved_gid else None

    if saved_label and saved_label not in labels and allow_saved_when_filtered:
        insert_at = 2 if current_display_label else 1
        labels = labels[:insert_at] + [saved_label] + [
            lab for lab in labels[insert_at:] if lab != saved_label
        ]

    if saved_label and (saved_label in labels or allow_saved_when_filtered):
        if st.session_state.get(widget_key_select) != saved_label:
            st.session_state[widget_key_select] = saved_label
    else:
        if st.session_state.get(widget_key_select) not in labels:
            if current_display_label and current_display_label in labels:
                st.session_state[widget_key_select] = current_display_label
            else:
                st.session_state[widget_key_select] = placeholder_label

    active_gid = st.session_state.get("awp_manage_guid") or st.session_state.get("awp_manage_id")
    active_label = gid_to_display_label.get(active_gid) if active_gid else None
    if active_gid and active_label and active_label in labels:
        st.session_state["awp_manage_id"] = active_gid
        st.session_state["awp_manage_label"] = _normalize_label(active_label)
        st.session_state["awp_manage_selected_project"] = _normalize_label(active_label)
        st.session_state["awp_manage_guid"] = active_gid

    desired_label = st.session_state.get(widget_key_select)
    if desired_label not in labels:
        desired_label = st.session_state.get("awp_manage_label")
        if desired_label:
            desired_label = gid_to_display_label.get(
                st.session_state.get("awp_manage_guid") or st.session_state.get("awp_manage_id"),
                _display_label(desired_label),
            )
    if desired_label not in labels:
        desired_label = active_label
    if desired_label not in labels and current_display_label in labels:
        desired_label = current_display_label
    if desired_label not in labels:
        desired_label = placeholder_label
    if st.session_state.get(widget_key_select) not in labels:
        st.session_state[widget_key_select] = desired_label

    def _on_project_change():
        selected_label = st.session_state[widget_key_select]
        selected_base_label = _normalize_label(selected_label)

        # Clear all awp_manage_* keys except trigger/display-control keys
        keep_keys = {
            "awp_manage_show_details",
            "awp_manage_mode",
        }
        for k in [k for k in list(st.session_state.keys()) if k.startswith("awp_manage_") and k not in keep_keys]:
            try:
                st.session_state.pop(k)
            except Exception:
                pass

        if selected_label == placeholder_label:
            st.session_state["awp_manage_label"] = None
            st.session_state["awp_manage_id"] = None
            st.session_state["awp_manage_selected_project"] = None
            st.session_state["awp_manage_guid"] = None
            st.session_state["awp_manage_update"] = "No"
            st.session_state["awp_manage_last_loaded_gid"] = None
            st.session_state["awp_manage_selection_changed"] = False
            return

        selected_gid = (
            display_label_to_gid.get(selected_label)
            or all_display_label_to_gid.get(selected_label)
            or label_to_gid.get(selected_base_label)
            or all_label_to_gid.get(selected_base_label)
        )

        st.session_state["awp_manage_label"] = selected_base_label
        st.session_state["awp_manage_id"] = selected_gid
        st.session_state["awp_manage_selected_project"] = selected_base_label
        st.session_state["awp_manage_guid"] = selected_gid
        st.session_state["awp_manage_update"] = "Yes"
        st.session_state["awp_manage_last_loaded_gid"] = None
        st.session_state["awp_manage_selection_changed"] = True
        st.session_state['awp_selection_made'] = True

    st.selectbox(
        "AASHTOWare Project List",
        labels,
        key=widget_key_select,
        on_change=_on_project_change,
    )

    selected_gid = st.session_state.get("awp_manage_id")

    st.write("")

    # ---------------------------------------------------------------------
    # Load selected AASHTOWare record into awp_manage_* session state
    # ---------------------------------------------------------------------
    last_loaded = st.session_state.get("awp_manage_last_loaded_gid")
    if selected_gid and selected_gid != last_loaded:
        user_keys = [
            "phase", "proj_name", "iris", "stip", "fed_proj_num",
            "fund_type", "proj_prac", "anticipated_start", "anticipated_end",
            "award_date", "award_fiscal_year", "contractor", "awarded_amount",
            "current_contract_amount", "amount_paid_to_date", "tenadd", "proj_desc",
            "contact_name", "contact_role", "contact_email", "contact_phone",
            "proj_web", "impact_comm", "route_id", "route_name", "preconstruction",
        ]
        date_like = {"award_date", "anticipated_start", "anticipated_end", "tenadd"}

        for k in user_keys:
            st.session_state[f"awp_manage_{k}"] = None if k in date_like else ""

        record = select_record(
            url=st.session_state["awp_url"],
            layer=st.session_state["awp_contracts_layer"],
            id_field="Id",
            id_value=selected_gid,
            return_geometry=False
        )

        if record and "attributes" in record[0]:
            attrs = record[0]["attributes"]

            # raw attribute mirrors -> awp_manage_<attribute_lower>
            for k, v in attrs.items():
                st.session_state[f"awp_manage_{k}".lower()] = v

            # friendly mirrors
            _awp_to_friendly = {
                "ProjectName": "awp_manage_proj_name",
                "Description": "awp_manage_proj_desc",
                "Phase": "awp_manage_phase",
                "FundingType": "awp_manage_fund_type",
                "ProjectPractice": "awp_manage_proj_prac",
                "IRIS": "awp_manage_iris",
                "STIP": "awp_manage_stip",
                "FederalProjectNumber": "awp_manage_fed_proj_num",
                "AnticipatedStart": "awp_manage_anticipated_start",
                "AnticipatedEnd": "awp_manage_anticipated_end",
                "AwardDate": "awp_manage_award_date",
                "AwardFiscalYear": "awp_manage_award_fiscal_year",
                "TentativeAdvertiseDate": "awp_manage_tenadd",
                "AwardedAmount": "awp_manage_awarded_amount",
                "CurrentContractAmount": "awp_manage_current_contract_amount",
                "AmountPaidToDate": "awp_manage_amount_paid_to_date",
                "Contractor": "awp_manage_contractor",
                "ContactName": "awp_manage_contact_name",
                "ContactRole": "awp_manage_contact_role",
                "ContactEmail": "awp_manage_contact_email",
                "ContactPhone": "awp_manage_contact_phone",
                "ProjectWebsite": "awp_manage_proj_web",
                "RouteId": "awp_manage_route_id",
                "RouteName": "awp_manage_route_name",
                "Preconstruction": "awp_manage_preconstruction",
            }
            for awp_attr, friendly_key in _awp_to_friendly.items():
                if awp_attr in attrs:
                    st.session_state[friendly_key] = attrs[awp_attr]

            st.session_state.setdefault("awp_manage_id", selected_gid)
            st.session_state["awp_manage_last_loaded_gid"] = selected_gid
            st.session_state["awp_manage_selection_changed"] = True

    # ---------------------------------------------------------------------
    # Read-only manage form
    #
    # IMPORTANT:
    # This only displays after:
    #   1. the reconnect/update flow has been activated
    #   2. a project is selected
    #
    # Your reconnect/update button callback should set:
    #   st.session_state["awp_manage_show_details"] = True
    # and optionally:
    #   st.session_state["awp_manage_mode"] = "reconnect"
    # ---------------------------------------------------------------------
    show_manage_details = (
        bool(st.session_state.get("awp_manage_show_details", False))
        and bool(selected_gid)
        and st.session_state.get("awp_manage_update") == "Yes"
    )
    if show_manage_details:
        st.markdown("###### AASHTOWARE PROJECT INFORMATION")
        with st.container(border=True):
            # -----------------------------------------------------------------
            # 1. PROJECT NAME
            # -----------------------------------------------------------------
            st.markdown("<h6>1. PROJECT NAME</h6>", unsafe_allow_html=True)
            c1, c2 = st.columns(2)
            with c1:
                ro_widget(
                    "awp_manage_proj_name",
                    "AASHTOWare Project Name",
                    fmt_string(_awp_manage_value("awp_manage_proj_name"))
                )
            with c2:
                ro_widget(
                    "awp_manage_public_proj_name",
                    "Public Project Name",
                    fmt_string(_awp_manage_value("awp_manage_proj_name"))
                )
            st.write("")

            # -----------------------------------------------------------------
            # 2. PHASE & IDS
            # -----------------------------------------------------------------
            st.markdown("<h6>2. PHASE & IDS</h6>", unsafe_allow_html=True)
            col1, col2, col3 = st.columns(3)
            with col1:
                ro_widget(
                    "awp_manage_phase",
                    "Phase",
                    fmt_string(_awp_manage_value("awp_manage_phase"))
                )
            with col2:
                ro_widget(
                    "awp_manage_iris",
                    "IRIS",
                    fmt_string(_awp_manage_value("awp_manage_iris"))
                )
            with col3:
                ro_widget(
                    "awp_manage_stip",
                    "STIP",
                    fmt_string(_awp_manage_value("awp_manage_stip"))
                )

            ro_widget(
                "awp_manage_fed_proj_num",
                "Federal Project Number",
                fmt_string(_awp_manage_value("awp_manage_fed_proj_num"))
            )
            st.write("")
            st.write("")

            # -----------------------------------------------------------------
            # 3. FUNDING TYPE & PRACTICE
            # -----------------------------------------------------------------
            st.markdown("<h6>3. FUNDING TYPE & PRACTICE</h6>", unsafe_allow_html=True)
            col13, col14 = st.columns(2)
            with col13:
                ro_widget(
                    "awp_manage_fund_type",
                    "Funding Type",
                    fmt_string(_awp_manage_value("awp_manage_fund_type"))
                )
            with col14:
                ro_widget(
                    "awp_manage_proj_prac",
                    "Project Practice",
                    fmt_string(_awp_manage_value("awp_manage_proj_prac"))
                )
            st.write("")
            st.write("")

            # -----------------------------------------------------------------
            # 4. START & END DATE
            # -----------------------------------------------------------------
            st.markdown("<h6>4. START & END DATE</h6>", unsafe_allow_html=True)
            col10, col11 = st.columns(2)
            with col10:
                ro_widget(
                    "awp_manage_anticipated_start",
                    "Anticipated Start",
                    fmt_date(_awp_manage_value("awp_manage_anticipated_start"))
                )
            with col11:
                ro_widget(
                    "awp_manage_anticipated_end",
                    "Anticipated End",
                    fmt_date(_awp_manage_value("awp_manage_anticipated_end"))
                )
            st.write("")
            st.write("")

            # -----------------------------------------------------------------
            # 5. AWARD INFORMATION
            # -----------------------------------------------------------------
            st.markdown("<h6>5. AWARD INFORMATION</h6>", unsafe_allow_html=True)
            col12, col13 = st.columns(2)
            with col12:
                ro_widget(
                    "awp_manage_award_date",
                    "Award Date",
                    fmt_agol_date(_awp_manage_value("awp_manage_award_date"))
                )
            with col13:
                ro_widget(
                    "awp_manage_award_fiscal_year",
                    "Awarded Fiscal Year",
                    fmt_int(_awp_manage_value("awp_manage_award_fiscal_year"), year=True)
                )
            ro_widget(
                "awp_manage_contractor",
                "Awarded Contractor",
                fmt_string(_awp_manage_value("awp_manage_contractor"))
            )
            col15, col16, col17 = st.columns(3)
            with col15:
                ro_widget(
                    "awp_manage_awarded_amount",
                    "Awarded Amount",
                    fmt_currency(_awp_manage_value("awp_manage_awarded_amount"))
                )
            with col16:
                ro_widget(
                    "awp_manage_current_contract_amount",
                    "Current Contract Amount",
                    fmt_currency(_awp_manage_value("awp_manage_current_contract_amount"))
                )
            with col17:
                ro_widget(
                    "awp_manage_amount_paid_to_date",
                    "Amount Paid to Date",
                    fmt_currency(_awp_manage_value("awp_manage_amount_paid_to_date"))
                )
            ro_widget(
                "awp_manage_tenadd",
                "Tentative Advertise Date",
                fmt_date(_awp_manage_value("awp_manage_tenadd"))
            )
            st.write("")
            st.write("")

            # -----------------------------------------------------------------
            # 6. DESCRIPTION
            # -----------------------------------------------------------------
            st.markdown("<h6>6. DESCRIPTION</h6>", unsafe_allow_html=True)
            ro_widget(
                "awp_manage_proj_desc_awp",
                "AASHTOWare Description",
                fmt_string(_awp_manage_value("awp_manage_proj_desc")),
                textarea=True,
            )
            ro_widget(
                "awp_manage_proj_desc_public",
                "Public Description",
                fmt_string(_awp_manage_value("awp_manage_proj_desc")),
                textarea=True,
            )
            st.write("")
            st.write("")

            # -----------------------------------------------------------------
            # 7. CONTACT
            # -----------------------------------------------------------------
            st.markdown("<h6>7. CONTACT</h6>", unsafe_allow_html=True)
            ro_widget(
                "awp_manage_contact_name",
                "Contact",
                fmt_string(_awp_manage_value("awp_manage_contact_name"))
            )
            col18, col19 = st.columns(2)
            with col18:
                ro_widget(
                    "awp_manage_contact_email",
                    "Email",
                    fmt_string(_awp_manage_value("awp_manage_contact_email"))
                )
            with col19:
                ro_widget(
                    "awp_manage_contact_phone",
                    "Phone",
                    fmt_string(_awp_manage_value("awp_manage_contact_phone"))
                )
            st.write("")
            st.write("")

            # -----------------------------------------------------------------
            # 8. WEB LINK
            # -----------------------------------------------------------------
            st.markdown("<h6>8. WEB LINK</h6>", unsafe_allow_html=True)
            ro_widget(
                "awp_manage_proj_web",
                "Project Website",
                fmt_string(_awp_manage_value("awp_manage_proj_web"))
            )