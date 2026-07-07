import streamlit as st
import requests
from typing import Optional, Tuple, Dict, Any
from util.container_util import (
    header_markdown,
    subheader_markdown,
    section_markdown,
)

AGOL_REST_BASE = "https://www.arcgis.com/sharing/rest"
AGOL_REFERER = "https://www.arcgis.com"
VALID_GROUP_MEMBER_TYPES = {"owner", "admin", "member"}

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False


def _clear_login_state() -> None:
    st.session_state["logged_in"] = False
    st.session_state.pop("agol_group_authorized", None)
    st.session_state.pop("agol_group_member_type", None)


def _generate_agol_token(username: str, password: str) -> Optional[str]:
    response = requests.post(
        f"{AGOL_REST_BASE}/generateToken",
        data={
            "f": "json",
            "username": username,
            "password": password,
            "client": "referer",
            "referer": AGOL_REFERER,
            "expiration": "60",
        },
        headers={"Referer": AGOL_REFERER},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    if data.get("error"):
        return None

    token = data.get("token")
    if not token:
        return None

    return str(token).strip() or None


def _check_group_membership(group_id: str, token: str) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    response = requests.get(
        f"{AGOL_REST_BASE}/community/groups/{group_id}",
        params={
            "f": "json",
            "token": token,
        },
        headers={"Referer": AGOL_REFERER},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    if data.get("error"):
        error_message = data["error"].get("message") or "Unable to verify group membership."
        return False, error_message, data

    user_membership = data.get("userMembership") or {}
    member_type = str(user_membership.get("memberType") or "").lower()
    is_member = member_type in VALID_GROUP_MEMBER_TYPES

    return is_member, member_type, data


def login() -> bool:
    if st.session_state.get("logged_in") and st.session_state.get("agol_group_authorized"):
        return True

    group_id = st.session_state.get("group_id")
    prefilled_username = str(st.session_state.get("app_username") or "").strip()
    if prefilled_username and not st.session_state.get("login_agol_username"):
        st.session_state["login_agol_username"] = prefilled_username

    with st.container(border=True):
        header_markdown("  SIGN IN WITH AGOL CREDENTIALS")
        #st.markdown('<div style="height: 1px;"\></div>', unsafe_allow_html=True)
       
        username = st.text_input(
            "AGOL Username",
            key="login_agol_username",
        )

        password = st.text_input(
            "AGOL Password",
            type="password",
            key="login_agol_password",
        )

        if st.button("SIGN IN", use_container_width=True, type="primary"):
            if not username or not password:
                _clear_login_state()
                st.error("Enter your AGOL username and password.")
                return False

            if not group_id:
                _clear_login_state()
                st.error("A required group_id value is missing from session state.")
                return False

            try:
                token = _generate_agol_token(username.strip(), password)
            except requests.RequestException:
                token = None

            if not token:
                _clear_login_state()
                st.error("The credentials are not valid and a token cannot be generated.")
                return False

            try:
                is_member, member_type, _group_response = _check_group_membership(
                    group_id=str(group_id),
                    token=token,
                )
            except requests.RequestException:
                _clear_login_state()
                st.warning("The user is not part of the group and is not authorized to access the application.")
                return False

            if not is_member:
                _clear_login_state()
                st.warning("The user is not part of the group and is not authorized to access the application.")
                return False

            st.session_state["AGOL_USERNAME"] = username.strip()
            st.session_state["AGOL_PASSWORD"] = password
            st.session_state["logged_in"] = True
            st.session_state["agol_group_authorized"] = True
            st.session_state["agol_group_member_type"] = member_type
            st.session_state.pop("login_agol_password", None)
            st.rerun()

    return False
