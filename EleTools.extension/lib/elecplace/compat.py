# -*- coding: utf-8 -*-
"""
elecplace.compat
================
Version-compatibility and small utility helpers shared by every module.

Revit API differences handled here (Revit 2023 - 2026):
  * ElementId.IntegerValue was deprecated in Revit 2024 (64-bit ids) and
    removed in Revit 2026  -> eid_int() uses ElementId.Value when present.
  * Units use ForgeTypeId (UnitTypeId / SpecTypeId), valid for 2021+.
  * FamilySymbol.Name is unreliable under IronPython (the setter-only
    override on ElementType hides the getter), so names are read through
    Element.Name.GetValue() or built-in parameters instead.

Revit API used:
  DB.ElementId, DB.Element.Name, DB.BuiltInParameter, DB.UnitUtils,
  DB.UnitTypeId, DB.SpecTypeId, DB.UnitFormatUtils, DB.LabelUtils
"""
from Autodesk.Revit import DB

try:                      # IronPython 2.7 has 'unicode'; CPython 3 does not
    text_type = unicode   # noqa: F821
except NameError:         # pragma: no cover
    text_type = str


# --------------------------------------------------------------------------
# Element ids and names
# --------------------------------------------------------------------------
def eid_int(element_id):
    """Return the numeric value of an ElementId on any Revit version."""
    if element_id is None:
        return -1
    try:
        return element_id.Value            # Revit 2024+ (Int64)
    except AttributeError:
        return element_id.IntegerValue     # Revit 2023


def to_text(value):
    """Safe conversion of anything (incl. .NET strings / None) to text."""
    if value is None:
        return u""
    try:
        return text_type(value)
    except Exception:
        return u"<?>"


def element_name(element):
    """Robust element name getter (works for ElementTypes in IronPython)."""
    if element is None:
        return u""
    try:
        return DB.Element.Name.GetValue(element)
    except Exception:
        try:
            return element.Name
        except Exception:
            return u"<unnamed>"


def param_as_string(element, bip):
    """Read a built-in parameter as display text; '' when missing."""
    try:
        prm = element.get_Parameter(bip)
        if prm is not None and prm.HasValue:
            return prm.AsString() or prm.AsValueString() or u""
    except Exception:
        pass
    return u""


def symbol_type_name(symbol):
    """Type name of a FamilySymbol."""
    name = param_as_string(symbol, DB.BuiltInParameter.SYMBOL_NAME_PARAM)
    return name or element_name(symbol)


def symbol_family_name(symbol):
    """Family name of a FamilySymbol."""
    try:
        return symbol.FamilyName
    except Exception:
        try:
            return symbol.Family.Name
        except Exception:
            return u"<family>"


def symbol_label(symbol):
    """'Family : Type' label used in UI and reports."""
    return u"{} : {}".format(symbol_family_name(symbol), symbol_type_name(symbol))


# --------------------------------------------------------------------------
# Units (Revit internal length unit = decimal feet)
# --------------------------------------------------------------------------
def mm_to_ft(value_mm):
    return DB.UnitUtils.ConvertToInternalUnits(float(value_mm), DB.UnitTypeId.Millimeters)


def ft_to_mm(value_ft):
    return DB.UnitUtils.ConvertFromInternalUnits(float(value_ft), DB.UnitTypeId.Millimeters)


def _unit_id(doc, spec):
    return doc.GetUnits().GetFormatOptions(spec).GetUnitTypeId()


def _unit_label(doc, spec, fallback):
    try:
        return DB.LabelUtils.GetLabelForUnit(_unit_id(doc, spec))
    except Exception:
        return fallback


def _format(doc, spec, value):
    return DB.UnitFormatUtils.Format(doc.GetUnits(), spec, float(value), False)


def _parse(doc, spec, text):
    """
    Parse user text for a spec (length / area) into internal units.
    Revit's own parser handles suffixes ("450mm", "1'-6\"", "10 m2");
    a bare number is taken in the project's display unit.
    """
    text = to_text(text).strip()
    if not text:
        return None
    units = doc.GetUnits()
    try:
        res = DB.UnitFormatUtils.TryParse(units, spec, text)
        # IronPython returns out-params as a tuple: (bool, value[, msg])
        if isinstance(res, tuple) and res[0]:
            return float(res[1])
    except Exception:
        pass
    try:
        number = float(text.replace(u",", u"."))
        return DB.UnitUtils.ConvertToInternalUnits(number, _unit_id(doc, spec))
    except Exception:
        return None


# ---- length -------------------------------------------------------------
def length_unit_label(doc):
    """Human readable project length unit, e.g. 'Millimeters'."""
    return _unit_label(doc, DB.SpecTypeId.Length, u"project units")


def format_length(doc, value_ft):
    """Format an internal length using the project's display units."""
    try:
        return _format(doc, DB.SpecTypeId.Length, value_ft)
    except Exception:
        return u"{:.0f} mm".format(ft_to_mm(value_ft))


_UNIT_SUFFIX = [("Millimeters", u"mm"), ("Centimeters", u"cm"), ("Meters", u"m"),
                ("Feet", u"ft"), ("Inches", u"in")]


def format_length_u(doc, value_ft):
    """
    Like format_length but guarantees a unit suffix. Many templates format
    lengths without a symbol ("174"), which is ambiguous in messages.
    """
    text = format_length(doc, value_ft)
    if any(ch.isalpha() for ch in text) or u"'" in text or u'"' in text:
        return text
    try:
        uid = _unit_id(doc, DB.SpecTypeId.Length)
        for name, suffix in _UNIT_SUFFIX:
            if uid == getattr(DB.UnitTypeId, name):
                return u"{} {}".format(text, suffix)
    except Exception:
        pass
    return u"{:.0f} mm".format(ft_to_mm(value_ft))


def parse_length(doc, text):
    """User text -> internal feet (None if invalid)."""
    return _parse(doc, DB.SpecTypeId.Length, text)


# ---- area ---------------------------------------------------------------
def sqm_to_ft2(value_m2):
    return DB.UnitUtils.ConvertToInternalUnits(float(value_m2), DB.UnitTypeId.SquareMeters)


def area_unit_label(doc):
    return _unit_label(doc, DB.SpecTypeId.Area, u"project area units")


def area_uses_feet(doc):
    """True when the project displays areas in square feet / inches."""
    lbl = area_unit_label(doc).lower()
    return u"feet" in lbl or u"inch" in lbl


def format_area(doc, value_ft2):
    try:
        return _format(doc, DB.SpecTypeId.Area, value_ft2)
    except Exception:
        return u"{:.2f} ft2".format(value_ft2)


def parse_area(doc, text):
    """User text -> internal square feet (None if invalid)."""
    return _parse(doc, DB.SpecTypeId.Area, text)


def area_number_to_internal(doc, number):
    """A bare number in project area display units -> internal ft2."""
    return DB.UnitUtils.ConvertToInternalUnits(float(number), _unit_id(doc, DB.SpecTypeId.Area))
