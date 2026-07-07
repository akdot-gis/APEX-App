import html
import streamlit as st
from typing import Optional, Tuple, Dict, Any


def _box_markdown(
    text: str,
    *,
    bg_color: str,
    text_color: str = "#ffffff",
    border_bottom: Optional[str] = None,
    border_radius: str = "0.35rem",
    padding: str = "0.6rem 0.9rem",
    margin: str = "0.5rem 0 0.75rem 0",
    font_size: str = "1rem",
    font_weight: str = "700",
) -> None:
    border_css = f"border-bottom: {border_bottom};" if border_bottom else ""

    st.markdown(
        f'<div style="width:100%; background:{bg_color}; color:{text_color}; border-radius:{border_radius}; padding:{padding}; margin:{margin}; font-size:{font_size}; font-weight:{font_weight}; box-sizing:border-box; {border_css}"><span>{text}</span></div>',
        unsafe_allow_html=True,
    )




def header_markdown(text: str) -> None:
    """Main header style: dark blue with a yellow banner on the bottom."""
    _box_markdown(
        text,
        bg_color="#0d3a6d",
        border_bottom="4px solid #e6a32c",
        border_radius="0.35rem 0.35rem 0 0",
        padding="0.6rem 0.9rem",
        margin="0.5rem 0 0.75rem 0",
        font_size="1.15rem",
        font_weight="500",
    )


def subheader_markdown(text: str) -> None:
    """Secondary header style: lighter blue block."""
    _box_markdown(
        text,
        bg_color="#194a6b",
        #border_bottom="4px solid #194a6b",
        padding="0.65rem 0.95rem",
        margin="0.2rem 0 0.75rem 0",
        font_size="1rem",
        font_weight="600",
    )


def section_markdown(text: str) -> None:
    """Optional section label style for smaller grouped content."""
    _box_markdown(
        text,
        bg_color="#dfe8f3",
        text_color="#0d3a6d",
        padding="0.55rem 0.85rem",
        margin="0.15rem 0 0.5rem 0",
        font_size="0.95rem",
        font_weight="700",
    )



def title_markdown(
    text: str,
    *,
    bg_color: str = "#0d3a6d",
    text_color: str = "#ffffff",
    border_bottom: Optional[str] = "4px solid #e6a32c",
    border_radius: str = "0.35rem 0.35rem 0 0",
    padding: str = "0.25rem 0.25rem",
    margin: str = "0.10rem 0 0.45rem 0",
    subtitle_size: str = ".8rem",
    subtitle_weight: str = "500",
    subtitle_spacing: str = "0.06em",
    title_size: str = "1.8rem",
    title_weight: str = "500",
    title_spacing: str = "0.09em",
    image_size: str = "65px",
    gap: str = "0.4rem",
) -> None:
    image_url = "https://akdot.maps.arcgis.com/sharing/rest/content/items/6050c8888d1b45dbbe586217c7cd8e04/data"
    border_css = f"border-bottom: {border_bottom};" if border_bottom else ""

    st.markdown(
        f'<div style="width:100%; background:{bg_color}; color:{text_color}; border-radius:{border_radius}; padding:{padding}; margin:{margin}; box-sizing:border-box; {border_css}">'
        f'<div style="display:flex; align-items:center; gap:{gap}; line-height:1.0;">'
        f'<img src="{image_url}" style="width:{image_size}; height:{image_size}; object-fit:contain; display:block; margin:0; padding:0;" />'
        f'<div style="display:flex; flex-direction:column; justify-content:center; margin:0; padding:0;">'
        f'<div style="font-size:{subtitle_size}; font-weight:{subtitle_weight}; font-style:italic; letter-spacing:{subtitle_spacing}; margin:0 0 0.15rem 0; padding:0;">ALASKA DEPARTMENT OF TRANSPORTATION AND PUBLIC FACILITIES</div>'
        f'<div style="font-size:{title_size}; font-weight:{title_weight}; letter-spacing:{title_spacing}; margin:0; padding:0; line-height:1.0;">{text}</div>'
        f'</div>'
        f'</div>'
        f'</div>',
        unsafe_allow_html=True,
    )