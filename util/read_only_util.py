import streamlit as st
import datetime
import html



# =============================================================================
# READ-ONLY DISPLAY LAYER (CSS)
# =============================================================================
# This CSS supports the "read-only widget" pattern:
# - ro()        : single-line boxed field
# - ro_textarea(): multi-line boxed field
# - ro_widget() : stores to session_state + renders ro/ro_textarea
#
# This keeps AASHTOWare mode visually read-only but still behaves like widgets.
# =============================================================================
_RO_CSS = """
<style>
.ro-field { margin-bottom: 0.75rem; }
.ro-label { font-size: 0.875rem; color: #6b7280; margin-bottom: 0.25rem; }
.ro-box {
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  border-radius: 0.375rem;
  padding: 0.5rem 0.75rem;
  color: #111827;
  font-size: 0.95rem;
  min-height: 38px;
  display: flex; align-items: center;
  word-break: break-word;
}
.ro-box.mono { font-variant-numeric: tabular-nums; }
.ro-box .placeholder { color: #9ca3af; }
.ro-box-textarea {
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  border-radius: 0.375rem;
  padding: 0.5rem 0.75rem;
  color: #111827;
  font-size: 0.95rem;
  min-height: 160px;
  white-space: pre-wrap;
  word-break: break-word;
  display: block;
}
.ro-box-textarea .placeholder { color: #9ca3af; }
</style>
"""

    

# =============================================================================
# READ-ONLY FIELD RENDERERS
# =============================================================================
# These helpers render read-only fields using HTML/CSS and are used in AWP mode.
# =============================================================================
def ro(label, value, mono=False):
    
    # Inject read‑only CSS globally (no extra spacing)
    st.html(_RO_CSS)

    # Render a single-line read-only field with label.
    safe_value = value if value not in (None, "") else '<span class="placeholder">—</span>'
    st.markdown(
        f"""
        <div class="ro-field">
          <div class="ro-label">{label}</div>
          <div class="ro-box{' mono' if mono else ''}">{safe_value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def ro_cols(specs):
    # Convenience renderer for column-based layouts:
    # specs = [(col, label, value, mono), ...]
    for col, label, value, mono in specs:
        with col:
            ro(label, value, mono)


def ro_textarea(label, value):
    # Render a multi-line read-only field with label.
    safe_value = value if value not in (None, "") else '<span class="placeholder">—</span>'
    st.markdown(
        f"""
        <div class="ro-field">
          <div class="ro-label">{label}</div>
          <div class="ro-box-textarea">{safe_value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def ro_widget(key, label, value, mono=False, textarea=False):
    """
    Read-only widget wrapper:
      - Writes value into st.session_state[key] (like a real widget)
      - Renders the value using ro() or ro_textarea()

    This keeps downstream logic consistent regardless of source mode.
    """
    # Persist value exactly like a widget
    st.session_state[key] = value

    # Render using your existing components
    if textarea:
        ro_textarea(label, value)
    else:
        ro(label, value, mono)


# =============================================================================
# READ-ONLY TAG LIST (PILLS)
# =============================================================================

# Local CSS for the pill styling (kept separate to avoid modifying _RO_CSS)
_RO_TAGLIST_CSS = """
<style>
  /* Wrap: lets pills flow on multiple lines inside the existing gray field box */
  .ro-taglist-wrap {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  /* Each pill */
  .ro-taglist-pill {
    background: #2e5a78;
    color: #FFFFFF;
    border-radius: 5px;
    padding: 2px 8px;
    line-height: 1.4;
    font-size: 0.9rem;
    display: inline-block;
    white-space: nowrap;
  }
</style>
"""

def _parse_to_items(values):
    """
    Normalize incoming values to a list of non-empty strings.
    Accepts comma-separated string, list/tuple/set, or None/empty.
    """
    if values is None:
        return []

    # If a string, split by comma and trim
    if isinstance(values, str):
        items = [s.strip() for s in values.split(",")]
        return [s for s in items if s]

    # If iterable (list/tuple/set), coerce to strings and trim
    if isinstance(values, (list, tuple, set)):
        items = [str(v).strip() for v in values]
        return [s for s in items if s]

    # Fallback to single string representation
    s = str(values).strip()
    return [s] if s else []


def ro_taglist(label, values):
    """
    Render a read-only field (using the same gray placeholder box as ro())
    where each item is shown as a pill with white text, #FF4B4B background,
    and 5px rounded corners.

    Parameters
    ----------
    label : str
        Field label.
    values : str | list | tuple | set | None
        Comma-separated string (e.g., "A, B, C") OR an iterable of values.
    """
    # Inject the local CSS for taglist pills without altering existing CSS
    st.html(_RO_TAGLIST_CSS)

    items = _parse_to_items(values)

    if not items:
        # Reuse your ro() to maintain the exact same field container look
        ro(label, None)
        return

    # Build pill HTML and send it to ro() so it renders inside the same box
    pills_html = "".join(
        f'<span class="ro-taglist-pill">{html.escape(item)}</span>'
        for item in items
    )
    value_html = f'<div class="ro-taglist-wrap">{pills_html}</div>'

    # Render inside your standard read-only field wrapper
    ro(label, value_html)


def ro_widget_taglist(key, label, values):
    """
    Read-only widget wrapper for taglist:
    - Writes values into st.session_state[key]
    - Renders using ro_taglist() to keep downstream logic consistent
    """
    st.session_state[key] = values
    ro_taglist(label, values)

        