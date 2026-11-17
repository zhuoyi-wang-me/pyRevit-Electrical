# -*- coding: utf-8 -*-
"""
elecplace.datamap
=================
Writes data into parameters of the fixtures that are placed.

Each rule has mapping lines, one per target parameter:

    Parameter name = value template

The template mixes fixed text with {tokens}:

    Comments          = Serves {Equip:Mark}
    EL_Room Number    = {Room Number}
    EL_Room Name      = {Room Name}
    EL_Description    = Fused spur for {Equip Type} ({Equip:System Name})
    EL_Status         = NEW

Tokens
  Room         {Room Number} {Room Name} {Room Level} {Room:<any room parameter>}
  Equipment    {Equip:<any instance or type parameter>} {Equip Family} {Equip Type}
               {Equip Id} {Equip Model}      (served equipment - equipment rules)
  Other        {Fixture Type} {Rule Category} {Date}

Rooms and equipment may live in linked models - their parameters are read
from the linked element. Values are resolved during planning (visible in
the Preview) and written by executor.py after the fixture is created.

Revit API used:
  Element.LookupParameter, Parameter (StorageType, AsString, AsValueString,
  AsInteger, AsDouble, Set, SetValueString, IsReadOnly), StorageType,
  Document.ParameterBindings, InstanceBinding, CategorySet.Contains
"""
import re
import time

from Autodesk.Revit import DB

from elecplace.compat import eid_int, symbol_family_name, symbol_type_name, to_text

TOKEN_RE = re.compile(r"\{([^{}]+)\}")

# Offered in the editor's token list (any parameter name works after ':')
TOKENS = [
    u"{Room Number}", u"{Room Name}", u"{Room Level}", u"{Room:Department}",
    u"{Equip:Mark}", u"{Equip:Type Mark}", u"{Equip:Comments}", u"{Equip:System Name}",
    u"{Equip:Size}", u"{Equip Family}", u"{Equip Type}", u"{Equip Id}", u"{Equip Model}",
    u"{Fixture Type}", u"{Date}",
]


def parse_mappings(text):
    """
    'Param = template' lines -> [(param, template)]. Blank lines and lines
    starting with # are ignored. Raises ValueError on malformed lines.
    """
    out = []
    for n, raw in enumerate(to_text(text).splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(u"#"):
            continue
        if u"=" not in line:
            raise ValueError(u"Data line {}: '{}' must look like  Parameter = value".format(n, line))
        name, template = line.split(u"=", 1)
        name, template = name.strip(), template.strip()
        if not name:
            raise ValueError(u"Data line {}: parameter name is missing".format(n))
        for tok in TOKEN_RE.findall(template):
            if not _known_token(tok):
                raise ValueError(u"Data line {}: unknown token {{{}}}".format(n, tok))
        out.append((name, template))
    return out


def _known_token(tok):
    low = tok.strip().lower()
    return (low in (u"room number", u"room name", u"room level", u"equip family", u"equip type",
                    u"equip id", u"equip model", u"fixture type", u"rule category", u"date")
            or low.startswith(u"room:") or low.startswith(u"equip:"))


def param_text(element, name):
    """Display text of a parameter (instance first, then type). None if absent."""
    if element is None:
        return None
    owners = [element]
    try:
        tid = element.GetTypeId()
        if tid != DB.ElementId.InvalidElementId:
            owners.append(element.Document.GetElement(tid))
    except Exception:
        pass
    for owner in owners:
        if owner is None:
            continue
        prm = owner.LookupParameter(name)
        if prm is None or not prm.HasValue:
            continue
        st = prm.StorageType
        if st == DB.StorageType.String:
            return to_text(prm.AsString() or u"")
        text = prm.AsValueString()
        if text:
            return to_text(text)
        if st == DB.StorageType.Integer:
            return to_text(prm.AsInteger())
        if st == DB.StorageType.Double:
            return u"{:g}".format(prm.AsDouble())
        if st == DB.StorageType.ElementId:
            el = owner.Document.GetElement(prm.AsElementId())
            return to_text(el.Name) if el is not None else u""
    return None


def token_value(tok, pp):
    """Value of one token for a Planned fixture (None = unavailable)."""
    t = tok.strip()
    low = t.lower()
    room = pp.room
    eq = getattr(pp, "equipment", None)
    real_room = getattr(room, "room", None)          # None for the 'outside rooms' stand-in

    if low == u"room number":
        return room.number if real_room is not None else None
    if low == u"room name":
        return room.name if real_room is not None else None
    if low == u"room level":
        return room.level_name
    if low.startswith(u"room:"):
        return param_text(real_room, t[5:].strip())
    if low == u"fixture type":
        return pp.rule.label
    if low == u"rule category":
        return pp.rule.category_name
    if low == u"date":
        return to_text(time.strftime("%Y-%m-%d"))
    if eq is None:
        return None                                   # equipment tokens on a room-walls rule
    if low.startswith(u"equip:"):
        return param_text(eq.inst, t[6:].strip())
    if low == u"equip family":
        return symbol_family_name(eq.inst.Symbol)
    if low == u"equip type":
        return symbol_type_name(eq.inst.Symbol)
    if low == u"equip id":
        return to_text(eq.elem_id)
    if low == u"equip model":
        return eq.source
    return None


def render(template, pp, missing):
    """Fill a template; tokens without a value become '' and are counted in `missing`."""
    def repl(match):
        val = token_value(match.group(1), pp)
        if val is None or val == u"":
            key = u"{" + match.group(1) + u"}"
            missing[key] = missing.get(key, 0) + 1
            return u""
        return val
    return TOKEN_RE.sub(repl, template).strip()


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------
_YES = (u"1", u"yes", u"y", u"true", u"on")


def write_param(inst, name, text):
    """Set an INSTANCE parameter from text. Returns None or an error reason."""
    prm = inst.LookupParameter(name)
    if prm is None:
        return u"parameter not found on the placed family (instance parameter needed)"
    if prm.IsReadOnly:
        return u"parameter is read-only"
    st = prm.StorageType
    try:
        if st == DB.StorageType.String:
            prm.Set(text)
        elif st == DB.StorageType.Integer:
            low = text.strip().lower()
            if low in _YES or low in (u"0", u"no", u"n", u"false", u"off"):
                prm.Set(1 if low in _YES else 0)
            else:
                prm.Set(int(float(text)))
        elif st == DB.StorageType.Double:
            if not prm.SetValueString(text):
                return u"'{}' is not a valid value for this parameter".format(text)
        else:
            return u"parameter type (element id) cannot be written from text"
    except Exception as ex:
        return u"could not set: {}".format(to_text(ex))
    return None


def fixture_param_names(doc, symbol):
    """
    Candidate target parameters for a fixture type: instance parameters of
    an existing instance of that family, project / shared parameters bound
    to its category as instance parameters, plus Comments and Mark.
    """
    names = set([u"Comments", u"Mark"])
    try:
        fam_id = eid_int(symbol.Family.Id)
        col = (DB.FilteredElementCollector(doc).OfCategoryId(symbol.Category.Id)
               .OfClass(DB.FamilyInstance))
        for inst in col:
            if eid_int(inst.Symbol.Family.Id) != fam_id:
                continue
            for prm in inst.Parameters:
                if not prm.IsReadOnly and prm.StorageType != DB.StorageType.ElementId:
                    names.add(to_text(prm.Definition.Name))
            break
    except Exception:
        pass
    try:
        it = doc.ParameterBindings.ForwardIterator()
        while it.MoveNext():
            binding = it.Current
            if isinstance(binding, DB.InstanceBinding) and binding.Categories.Contains(symbol.Category):
                names.add(to_text(it.Key.Name))
    except Exception:
        pass
    return sorted(names, key=lambda n: n.lower())


def describe(mappings):
    """Short text for the rules grid."""
    if not mappings:
        return u"-"
    return u"; ".join(u"{} = {}".format(p, t) for p, t in mappings)

