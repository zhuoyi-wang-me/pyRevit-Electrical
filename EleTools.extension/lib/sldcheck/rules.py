# -*- coding: utf-8 -*-
"""Pure helpers (no Revit API): name normalisation and parsing of the text
values written on single line diagrams. IronPython 2.7 and CPython 3."""
import re

try:
    text_type = unicode  # noqa: F821  (IronPython 2.7)
except NameError:
    text_type = str

_DASHES = re.compile(u"[‐-―−]")
_SPACES = re.compile(r"\s+")
_NUMBER = r"(\d+(?:[.,]\d+)?)"


def to_text(value):
    if value is None:
        return u""
    if not isinstance(value, text_type):
        try:
            value = text_type(value)
        except Exception:
            return u""
    return value


def norm(value):
    """Matching key: trim, collapse spaces, ignore case, unify dashes."""
    text = to_text(value).replace(u" ", u" ")
    text = _DASHES.sub(u"-", text)
    return _SPACES.sub(u" ", text).strip().lower()


def loose(value):
    """Letters/digits only - used ONLY for 'did you mean' hints, never to match."""
    return re.sub(r"[\W_]+", u"", norm(value), flags=re.UNICODE)


def param_key(name):
    """Parameter-name key: 'MEE-Component' == 'MEE_Component' == 'mee component'."""
    return re.sub(r"[\s_\-]+", u"", to_text(name)).lower()


def to_float(text):
    try:
        return float(to_text(text).replace(u",", u"."))
    except Exception:
        return None


def first_number(text):
    match = re.search(_NUMBER, to_text(text))
    return to_float(match.group(1)) if match else None


def fmt_num(value, digits=2):
    if value is None:
        return u""
    if abs(value - round(value)) < 1e-9:
        return u"{0:d}".format(int(round(value)))
    return (u"{0:." + str(digits) + u"f}").format(value).rstrip(u"0").rstrip(u".")


# ----------------------------------------------------------------------
# Ratings, loads, poles
# ----------------------------------------------------------------------
def parse_current(text):
    """'63A', '63 A', '4P 63A', '100AT/250AF' -> amps (first A value)."""
    text = to_text(text)
    match = re.search(_NUMBER + r"\s*(k?)a(?:t\b|mps?\b|\b|(?=[^a-z]))", text, re.I)
    if match:
        value = to_float(match.group(1))
        return value * 1000.0 if match.group(2).lower() == u"k" else value
    return first_number(text)


_POWER_UNITS = {u"va": (0.001, u"kVA"), u"kva": (1.0, u"kVA"), u"mva": (1000.0, u"kVA"),
                u"w": (0.001, u"kW"), u"kw": (1.0, u"kW"), u"mw": (1000.0, u"kW")}


def parse_power(text, default_unit=u"kVA"):
    """'250 kVA' -> (250.0, 'kVA'); '1.2MVA' -> (1200, 'kVA'); '10kW' -> (10, 'kW');
    '45' -> (45, default_unit). Returns (None, None) without a number."""
    text = to_text(text)
    match = re.search(_NUMBER + r"\s*(mva|kva|va|mw|kw|w)\b", text, re.I)
    if match:
        factor, unit = _POWER_UNITS[match.group(2).lower()]
        return to_float(match.group(1)) * factor, unit
    value = first_number(text)
    if value is None:
        return None, None
    return value, default_unit


_POLE_WORDS = [
    (r"\b(4p|4\s*pole|tpn|tp\s*\+\s*n|3p\s*\+\s*n|3pn)\b", 3, u"TPN/4P"),
    (r"\b(tp|3p|3\s*pole|triple\s*pole)\b", 3, u"TP"),
    (r"\b(dp|2p|2\s*pole|double\s*pole|spn|sp\s*\+\s*n|1p\s*\+\s*n)\b", 2, u"DP/SPN"),
    (r"\b(sp|1p|1\s*pole|single\s*pole)\b", 1, u"SP"),
]


def parse_poles(text):
    """Pole count comparable with Revit 'Number of Poles' (1..3).
    4P / TPN count as 3 (Revit models the neutral separately)."""
    text = norm(text)
    if not text:
        return None
    if re.match(r"^\d+(\.0+)?$", text):
        value = int(float(text))
        return 3 if value == 4 else value
    for pattern, poles, _label in _POLE_WORDS:
        if re.search(pattern, text):
            return poles
    return None


def is_truthy(text):
    return norm(text) in (u"1", u"yes", u"y", u"true", u"spare", u"x")


def has_keyword(text, keywords):
    value = norm(text)
    return bool(value) and any(re.search(r"\b" + re.escape(norm(k)) + r"\b", value)
                               for k in keywords)


# ----------------------------------------------------------------------
# Cables
# ----------------------------------------------------------------------
_CABLE_TYPE_TOKENS = [u"xlpe", u"pvc", u"epr", u"lszh", u"lsoh", u"lsf", u"swa", u"awa",
                      u"micc", u"fr", u"frls", u"fire resistant", u"cu", u"copper",
                      u"al", u"aluminium", u"aluminum", u"armoured", u"unarmoured",
                      u"sta", u"nyy", u"n2xy", u"busbar", u"busduct"]


def parse_cable(text):
    """Pull cores / size / type tokens out of free cable text, e.g.
    '4C x 16mm2 XLPE/SWA/PVC', '3.5C 240 sq.mm Cu', '4x16+1x16 E', '1#16'.
    Returns dict(cores, size_mm2, types) - missing parts are None / [].
    Earth / CPC conductors after '+' are not counted as cores."""
    raw = norm(text)
    out = {"cores": None, "size_mm2": None, "types": []}
    if not raw:
        return out
    groups = [g.strip() for g in re.split(r"[,;]", raw) if g.strip()]
    if len(groups) > 1 and all(re.search(r"\d\s*-?\s*[x×\*#]", g) for g in groups):
        # Revit wire-size style: '3#16, 1#16, 1#16 E' -> phases + neutral
        total, size = 0.0, None
        for group in groups:
            m = re.search(r"(\d+)\s*-?\s*[x×\*#]\s*(\d+(?:[.,]\d+)?)", group)
            if not m or re.search(r"\b(e|g|pe|cpc|earth|ground|gnd)\b|\d(e|g)\b", group):
                continue
            total += to_float(m.group(1))
            size = size if size is not None else to_float(m.group(2))
        out["cores"], out["size_mm2"] = (total or None), size
        raw_tokens = set(re.split(r"[^a-z0-9]+", raw))
        out["types"] = [t for t in _CABLE_TYPE_TOKENS if t in raw_tokens]
        return out
    main = re.split(r"\+", raw)[0]
    cores = re.search(r"(\d+(?:\.\d+)?)\s*(?:c\b|core|cores|cr\b)", main)
    multiply = re.search(r"(\d+(?:\.\d+)?)\s*[x×\*#]\s*(\d+(?:[.,]\d+)?)", main)
    size = re.search(_NUMBER + r"\s*(?:mm\s*2|mm²|sq\.?\s*mm|sqmm|mm\b)", main)
    if cores:
        out["cores"] = to_float(cores.group(1))
    elif multiply:
        out["cores"] = to_float(multiply.group(1))
    if size:
        out["size_mm2"] = to_float(size.group(1))
    elif multiply:
        out["size_mm2"] = to_float(multiply.group(2))
    tokens = set(re.split(r"[^a-z0-9]+", raw))
    for token in _CABLE_TYPE_TOKENS:
        if (u" " in token and token in raw) or token in tokens:
            out["types"].append(token)
    return out


def cable_type_key(text):
    """Order-independent token set for comparing 'XLPE/SWA/PVC' with 'PVC-SWA-XLPE'."""
    return frozenset(t for t in re.split(r"[^a-z0-9]+", norm(text)) if t)


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------
def classify(texts, rules):
    """First rule label whose regex matches any of texts (in order)."""
    for text in texts:
        value = norm(text)
        if not value:
            continue
        for label, pattern in rules:
            try:
                if re.search(pattern, value):
                    return label
            except re.error:
                continue
    return None


def detail_role(label, fields, role_rules):
    """'equipment' | 'breaker' | 'cable' | None for a detail item.
    label = 'Family : Type'; fields = {field: text}."""
    value = norm(label)
    tokens = set(re.split(r"[^a-z0-9]+", value))
    for role, keywords in role_rules:
        for keyword in keywords:
            key = norm(keyword)
            if (u" " in key and key in value) or key in tokens:
                return role
            # family names like 'S_EC_Panel' or 'MCCB_3P' -> also prefix tokens
            if u" " not in key and len(key) > 2 and any(t.startswith(key) for t in tokens):
                return role
    if any(fields.get(k) for k in ("cable_size", "cable_type", "cores", "cable_desc")):
        return "cable"
    if any(fields.get(k) for k in ("breaker_rating", "breaker_frame", "poles", "breaker_ref")):
        return "breaker"
    if fields.get("name"):
        return "equipment"
    return None


def within(a, b, abs_tol=0.0, pct_tol=0.0):
    if a is None or b is None:
        return True
    limit = max(abs_tol, abs(b) * pct_tol / 100.0)
    return abs(a - b) <= limit + 1e-9
