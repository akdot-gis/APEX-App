import datetime
import re

# =============================================================================
# FORMATTERS (DISPLAY / INPUT NORMALIZATION)
# =============================================================================
# These helpers sanitize or format values coming from:
#   - AASHTOWare-populated st.session_state (strings, ints, ISO-like date strings)
#   - User inputs (Streamlit widgets)
# =============================================================================
def fmt_string(value):
    # Normalizes string display values:
    # - None => ""
    # - "none"/"" => ""
    # - otherwise stripped string
    if value is None:
        return ""
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned == "" or cleaned.lower() == "none":
            return ""
        return cleaned
    return value


def fmt_int(val, year=False):
    """Return an integer formatted with commas, or return the original value.

    - year=False: apply commas (10,000)
    - year=True : return plain int (years shouldn't be comma formatted)
    """

    # --- NEW: coerce numeric strings (and strings with commas) to int ---
    if isinstance(val, str):
        s = val.strip()
        if s:
            # allow "10,000" style strings
            s2 = s.replace(",", "")
            if s2.isdigit():
                val = int(s2)

    if year is False:
        if isinstance(val, int):
            return f"{val:,}"
    else:
        if isinstance(val, int):
            return val

    return val


def fmt_date(val):
    # Display helper:
    # Accepts datetime/date objects or ISO-like strings.
    if not val:
        return ""
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val.strftime("%m/%d/%Y")
    try:
        d = datetime.datetime.fromisoformat(val).date()
        return d.strftime("%m/%d/%Y")
    except Exception:
        return str(val)
    

def fmt_agol_date(val):
    """
    Convert AGOL millisecond timestamp (e.g., 1685404800000)
    into a date string MM/DD/YYYY.
    
    Returns "" for None/invalid inputs.
    """
    if val is None:
        return ""
    
    try:
        # Accept strings or ints
        ms = int(str(val).strip())

        # Convert milliseconds → seconds
        dt = datetime.datetime.fromtimestamp(ms / 1000)

        return dt.strftime("%m/%d/%Y")
    except Exception:
        return ""


def year_to_mmddyyyy(val):
    """
    Returns a date string formatted as MM/DD/YYYY.

    - int year -> "01/01/YYYY"
    - datetime/date -> formatted "MM/DD/YYYY"
    - otherwise -> None
    """
    if isinstance(val, datetime.datetime):
        d = val.date()
    elif isinstance(val, datetime.date):
        d = val
    elif isinstance(val, int) and 1 <= val <= 9999:
        d = datetime.date(val, 1, 1)
    else:
        return None

    return d.strftime("%m/%d/%Y")


def fmt_date_or_none(val):
    """
    Input helper:
      Returns a datetime.date (for Streamlit date_input) or None.

    Accepted formats:
      - datetime.date / datetime.datetime
      - Strings:
          MM/DD/YYYY
          YYYY-MM-DD
          MM-DD-YYYY
          ISO-ish: YYYY-MM-DDTHH:MM:SS(.fff)(Z|+00:00)

    Anything else => None (safe for date_input).
    """
    # datetime.datetime is also a datetime.date, so check it first
    if isinstance(val, datetime.datetime):
        return val.date()

    if isinstance(val, datetime.date):
        return val

    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None

        # Treat common "not a date" placeholders as empty
        if s.lower() in ("none", "null", "nan", "n/a", "na", "tbd"):
            return None

        # Strip time if ISO-like
        if "T" in s:
            s = s.split("T", 1)[0]
        elif " " in s:
            left = s.split(" ", 1)[0]
            if len(left) == 10 and left[4] == "-" and left[7] == "-":
                s = left

        # Try common date formats
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
            try:
                return datetime.datetime.strptime(s, fmt).date()
            except ValueError:
                pass

    return None


def fmt_int_or_none(val):
    # Streamlit number_input "value" must be numeric or None.
    # Reject bool because bool is subclass of int.
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        # Accept floats (e.g., 678.0) and coerce to int
        try:
            return int(val)
        except Exception:
            return None
    if isinstance(val, str):
        s = val.strip()
        if not s or s.lower() in {"none", "null", "nan", "n/a", "na"}:
            return None
        # Remove grouping commas and currency symbols, keep digits, '.', and sign
        s2 = s.replace(",", "").replace("$", "").strip()
        try:
            f = float(s2)
            return int(f)
        except Exception:
            return None
    return None


import re

def fmt_currency(val, mode: str = "format", *, default=""):
    """
    Currency helper.

    mode:
      - "format" (default): convert val -> "$#,##0.00" (string)
      - "float": convert currency-like string -> float
      - "passthrough": return float if numeric else original string

    default:
      - what to return when val is empty/None and mode="format"
    """
    if val is None or val == "":
        return default if mode == "format" else None

    # ---------- parse helpers ----------
    def _to_float(x):
        if isinstance(x, (int, float)):
            return float(x)

        s = str(x).strip()
        if s == "":
            return None

        # Negative in parentheses: (1,234.56) -> -1234.56
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]

        # Remove common currency chars and grouping
        s = s.replace("$", "").replace(",", "").strip()

        # Keep only digits, decimal, sign
        s = re.sub(r"[^0-9.\-+]", "", s)

        try:
            f = float(s)
            return -f if neg else f
        except Exception:
            return None

    # ---------- modes ----------
    if mode == "float":
        return _to_float(val)

    if mode == "passthrough":
        f = _to_float(val)
        return f if f is not None else (str(val) if val else "")

    # default: "format"
    f = _to_float(val)
    if f is None:
        # if it can't parse, keep old behavior: return string/empty
        return str(val) if val else default
    return f"${f:,.2f}"



def fmt_double(val, mode: str = "float", *, decimals: int = 2, thousands: bool = False, default=None):
    """
    Double (float) helper.

    mode:
      - "float"  (default): convert val -> float (or None if not parseable)
      - "format"          : convert val -> formatted string (keeps decimals/thousands options)
      - "passthrough"     : return float if parseable else original string (or "")

    decimals:
      - number of digits after the decimal point when mode="format"

    thousands:
      - when mode="format", if True uses grouping separators (e.g., 12,345.67)

    default:
      - returned when val is empty/None in mode="format"
      - returned when val is empty/None in mode="float" (defaults to None)
    """
    if val is None or val == "":
        return default if mode == "format" else default  # typically None unless caller sets it

    def _to_float(x):
        # Already numeric (reject bool explicitly if you want)
        if isinstance(x, bool):
            return None
        if isinstance(x, (int, float)):
            return float(x)

        s = str(x).strip()
        if s == "":
            return None

        # Handle negative in parentheses: (1234.5) -> -1234.5
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1].strip()

        # Remove grouping commas and spaces
        s = s.replace(",", "").replace(" ", "")

        # Keep only digits, decimal point, and sign



def fmt_phone(input_value, default_area=907):
    """
    Format a phone-like input into "(AAA) BBB-CCCC".

    Rules:
    - Remove all non-digits
    - If 11 digits and starts with '1', drop the leading '1' (US/Canada)
    - If exactly 7 digits, prepend default_area (default: 907)
    - If not exactly 10 digits after normalization, return None
    """
    if input_value is None:
        return None

    digits = re.sub(r"\D+", "", str(input_value))

    # Handle leading country code '1'
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    # Local 7-digit -> add area code
    if len(digits) == 7:
        digits = f"{default_area}{digits}"

    # Must be 10 digits now
    if len(digits) != 10:
        return None

    area, prefix, line = digits[:3], digits[3:6], digits[6:]
    return f"({area}) {prefix}-{line}"








# =============================================================================
# WIDGET KEY MANAGEMENT (PREVENTS MODE BLEED)
# =============================================================================
# Streamlit retains widget values by key. When switching AWP <-> UI, we need
# distinct widget keys (and a version bump) to force clean widget instantiation.
# =============================================================================
def widget_key(name: str, version: int, is_awp: bool) -> str:
    """
    Build a per-source, per-version widget key so Streamlit treats AWP and UI
    as distinct controls and doesn't retain values across source switches.

    Result format:
      - AASHTOWare: awp_widget_key_<name>_<version>
      - User Input : ui_widget_key_<name>_<version>
    """
    prefix = "awp_widget_key" if is_awp else "ui_widget_key"
    return f"{prefix}_{name}_{version}"