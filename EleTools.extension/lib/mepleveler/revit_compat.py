# -*- coding: utf-8 -*-
"""Compatibility helpers for Revit 2022 - 2026.

The Revit API changed in a number of places between 2022 and 2026:

*   ``ElementId.IntegerValue`` (int32) was deprecated in 2024 in favour of
    ``ElementId.Value`` (int64) and produces obsolete warnings afterwards.
*   ``ElementId(int)`` still works, but ``ElementId(long)`` is required on
    2024+ when round tripping large ids.
*   ``DisplayUnitType`` was replaced by ``ForgeTypeId`` / ``UnitTypeId``.
*   A handful of ``BuiltInCategory`` / ``BuiltInParameter`` members only exist
    in newer releases (e.g. ``OST_PlumbingEquipment`` in 2024+).

Every helper in this module degrades gracefully so that a single code base can
be deployed to all supported releases.
"""

from Autodesk.Revit import DB

# ---------------------------------------------------------------------------
# Revit version
# ---------------------------------------------------------------------------

def revit_version(doc):
    """Return the host Revit version as an int (e.g. 2024)."""
    try:
        return int(doc.Application.VersionNumber)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# ElementId helpers
# ---------------------------------------------------------------------------

def eid_value(element_id):
    """Return the numeric value of an ElementId on any supported release."""
    if element_id is None:
        return -1
    try:
        return element_id.Value          # Revit 2024+
    except AttributeError:
        return element_id.IntegerValue   # Revit 2022 / 2023


def make_eid(value):
    """Build an ElementId from a numeric value on any supported release."""
    try:
        return DB.ElementId(value)
    except Exception:
        return DB.ElementId(int(value))


INVALID_ID = DB.ElementId.InvalidElementId


def is_valid_id(element_id):
    return element_id is not None and element_id != INVALID_ID


# ---------------------------------------------------------------------------
# Enum lookups that may not exist in every release
# ---------------------------------------------------------------------------

def bic(name):
    """Safely resolve a BuiltInCategory by name, returning None if absent."""
    return getattr(DB.BuiltInCategory, name, None)


def bip(name):
    """Safely resolve a BuiltInParameter by name, returning None if absent."""
    return getattr(DB.BuiltInParameter, name, None)


def get_bip_param(element, name):
    """Return the parameter for a BuiltInParameter *name*, or None."""
    enum_member = bip(name)
    if enum_member is None:
        return None
    try:
        return element.get_Parameter(enum_member)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def _length_unit(doc):
    """Return the document's display unit (ForgeTypeId) for lengths."""
    try:
        fo = doc.GetUnits().GetFormatOptions(DB.SpecTypeId.Length)
        return fo.GetUnitTypeId()
    except Exception:
        return DB.UnitTypeId.Feet


def to_display_length(doc, internal_value):
    """Convert an internal length (decimal feet) to the project display units."""
    try:
        return DB.UnitUtils.ConvertFromInternalUnits(internal_value,
                                                     _length_unit(doc))
    except Exception:
        return internal_value


def length_unit_label(doc):
    """Human readable label of the project length unit, e.g. 'mm'."""
    try:
        return DB.LabelUtils.GetLabelForUnit(_length_unit(doc))
    except Exception:
        return 'ft'


def format_length(doc, internal_value, digits=1):
    """Format an internal length for display, including the unit label."""
    if internal_value is None:
        return ''
    try:
        value = to_display_length(doc, internal_value)
        return u'{0:.{1}f} {2}'.format(value, digits, length_unit_label(doc))
    except Exception:
        return u'{0:.4f} ft'.format(internal_value)


def from_display_length(doc, display_value):
    """Convert a value expressed in project display units to internal feet."""
    try:
        return DB.UnitUtils.ConvertToInternalUnits(display_value,
                                                   _length_unit(doc))
    except Exception:
        return display_value


# ---------------------------------------------------------------------------
# Misc element helpers
# ---------------------------------------------------------------------------

def element_name(element):
    """Best effort element/type name that never raises."""
    try:
        return element.Name
    except Exception:
        try:
            return DB.Element.Name.GetValue(element)
        except Exception:
            return u''


def family_and_type(doc, element):
    """Return (family_name, type_name) for any element."""
    family_name = u''
    type_name = u''
    try:
        type_id = element.GetTypeId()
        if is_valid_id(type_id):
            etype = doc.GetElement(type_id)
            if etype is not None:
                type_name = element_name(etype)
                fam_param = get_bip_param(etype, 'ALL_MODEL_FAMILY_NAME')
                if fam_param is not None:
                    family_name = fam_param.AsString() or u''
                if not family_name:
                    try:
                        family_name = etype.FamilyName
                    except Exception:
                        family_name = u''
    except Exception:
        pass
    if not type_name:
        type_name = element_name(element)
    return family_name, type_name
