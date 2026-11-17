# -*- coding: utf-8 -*-
"""Revit reads for the SLD Checker - READ-ONLY, no transactions.

Turns Detail Items, Electrical Equipment, Electrical Circuits and
Electrical Fixtures of the host document into the plain record dicts that
checks.py compares. Works on Revit 2025 and 2026 (ElementId.Value /
IntegerValue handled by id_value)."""
from pyrevit import DB, revit
from Autodesk.Revit.DB import Electrical as DBE

from sldcheck import rules


def id_value(element_id):
    """ElementId -> int (Revit 2024+ .Value, older .IntegerValue)."""
    if element_id is None:
        return None
    if hasattr(element_id, "Value"):
        return int(element_id.Value)
    return int(element_id.IntegerValue)


def safe_name(element, default=u""):
    if element is None:
        return default
    try:
        name = revit.query.get_name(element)
        if name:
            return name
    except Exception:
        pass
    return default


def element_label(doc, element):
    """'Family : Type' for instances, type name otherwise."""
    try:
        etype = doc.GetElement(element.GetTypeId())
    except Exception:
        etype = None
    fam = u""
    if etype is not None:
        try:
            fam = etype.FamilyName or u""
        except Exception:
            fam = u""
    typ = safe_name(etype) if etype is not None else safe_name(element)
    if fam and typ:
        return u"{0} : {1}".format(fam, typ)
    return typ or fam or u"<element>"


# ----------------------------------------------------------------------
# Parameter reading (mapping driven)
# ----------------------------------------------------------------------
_UNIT_IDS = {}


def _unit_type(unit):
    if unit not in _UNIT_IDS:
        name = {u"A": "Amperes", u"kVA": "KilovoltAmperes", u"kW": "Kilowatts",
                u"VA": "VoltAmperes", u"W": "Watts", u"mm": "Millimeters"}.get(unit)
        _UNIT_IDS[unit] = getattr(DB.UnitTypeId, name, None) if name else None
    return _UNIT_IDS[unit]


class ParamReader(object):
    """Reads fields through the ordered source lists in the config.
    Caches the resolved parameter names per element type for speed."""

    def __init__(self, doc, units):
        self.doc = doc
        self.units = units
        self._bips = {}
        self._type_cache = {}

    def _bip(self, name):
        if name not in self._bips:
            self._bips[name] = getattr(DB.BuiltInParameter, name, None)
        return self._bips[name]

    def _lookup(self, owner, name, cache_key):
        if owner is None:
            return None
        if name.lower().startswith("bip:"):
            bip = self._bip(name[4:].strip())
            return owner.get_Parameter(bip) if bip is not None else None
        param = owner.LookupParameter(name)
        if param is not None:
            return param
        # tolerate '-' vs '_' vs ' ' differences in the parameter name;
        # resolved once per (type, instance/type, name) - same family, same params
        key = (cache_key, name)
        if key not in self._type_cache:
            wanted, resolved = rules.param_key(name), None
            for candidate in owner.Parameters:
                try:
                    if rules.param_key(candidate.Definition.Name) == wanted:
                        resolved = candidate.Definition.Name
                        break
                except Exception:
                    continue
            self._type_cache[key] = resolved
        resolved = self._type_cache[key]
        return owner.LookupParameter(resolved) if resolved else None

    def _value(self, param, field):
        """(text, number) in the configured unit; (None, None) when empty."""
        if param is None or not param.HasValue:
            return None, None
        storage = param.StorageType
        try:
            if storage == DB.StorageType.String:
                text = param.AsString()
                return (text.strip(), None) if text and text.strip() else (None, None)
            if storage == DB.StorageType.Integer:
                value = param.AsInteger()
                shown = param.AsValueString()
                return (shown or rules.to_text(value)), float(value)
            if storage == DB.StorageType.Double:
                raw = param.AsDouble()
                unit = self.units.get(field)
                unit_id = _unit_type(unit) if unit else None
                number = None
                if unit_id is not None:
                    try:
                        number = DB.UnitUtils.ConvertFromInternalUnits(raw, unit_id)
                    except Exception:
                        number = None      # not that kind of parameter
                shown = param.AsValueString()
                if number is None:
                    number = rules.first_number(shown) if shown else raw
                text = (u"{0} {1}".format(rules.fmt_num(number), unit) if unit
                        else (shown or rules.fmt_num(raw)))
                return text, number
            if storage == DB.StorageType.ElementId:
                shown = param.AsValueString()
                if not shown:
                    target = self.doc.GetElement(param.AsElementId())
                    shown = safe_name(target) if target is not None else None
                return (shown, None) if shown else (None, None)
        except Exception:
            return None, None
        return None, None

    def read(self, element, field_map):
        """{field: text}, {field: number}, {field: parameter used}."""
        texts, numbers, used = {}, {}, {}
        etype, type_key = None, None
        try:
            type_id = element.GetTypeId()
            if type_id is not None and type_id != DB.ElementId.InvalidElementId:
                etype = self.doc.GetElement(type_id)
                type_key = id_value(type_id)
        except Exception:
            etype = None
        if type_key is None:
            try:
                type_key = u"cat{0}".format(id_value(element.Category.Id))
            except Exception:
                type_key = u"?"
        for field, sources in field_map.items():
            for source in sources:
                found = False
                for owner, level in ((element, u"i"), (etype, u"t")):
                    text, number = self._value(
                        self._lookup(owner, source, (type_key, level)), field)
                    if text:
                        texts[field], used[field] = text, source
                        if number is not None:
                            numbers[field] = number
                        found = True
                        break
                if found:
                    break
        return texts, numbers, used


# ----------------------------------------------------------------------
# Views / scope
# ----------------------------------------------------------------------
def views_with_detail_items(doc):
    """{view_id_int: (view, count)} for every non-template view owning
    Detail Items (drafting views and plans used as SLDs)."""
    counts = {}
    collector = (DB.FilteredElementCollector(doc)
                 .OfCategory(DB.BuiltInCategory.OST_DetailComponents)
                 .WhereElementIsNotElementType())
    for element in collector:
        if not isinstance(element, DB.FamilyInstance):
            continue                       # filled regions, repeating details ...
        try:
            key = id_value(element.OwnerViewId)
        except Exception:
            continue
        if key is None or key < 0:
            continue
        counts[key] = counts.get(key, 0) + 1
    out = {}
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        key = id_value(view.Id)
        if key not in counts:
            continue
        try:
            if view.IsTemplate:
                continue
        except Exception:
            pass
        out[key] = (view, counts[key])
    return out


# ----------------------------------------------------------------------
# Collectors
# ----------------------------------------------------------------------
def collect_details(doc, cfg, view_ids, reader, id_map):
    """Detail Item records in the given views (set of view id ints)."""
    records = []
    view_names, view_scales = {}, {}
    role_rules = list(cfg["sld_roles"])
    if "ignore" not in [role for role, _words in role_rules]:
        # templates saved before the ignore list existed
        from sldcheck import config as _config
        role_rules = [r for r in _config.DEFAULTS["sld_roles"] if r[0] == "ignore"] + role_rules
    collector = (DB.FilteredElementCollector(doc)
                 .OfCategory(DB.BuiltInCategory.OST_DetailComponents)
                 .WhereElementIsNotElementType())
    label_cache = {}
    for element in collector:
        # only family symbols are SLD items - filled regions (hatch zones,
        # hazard areas), repeating details etc. are Detail Items too
        if not isinstance(element, DB.FamilyInstance):
            continue
        try:
            owner = id_value(element.OwnerViewId)
        except Exception:
            continue
        if owner not in view_ids:
            continue
        try:
            # nested shared components belong to their parent symbol
            if element.SuperComponent is not None:
                continue
        except Exception:
            pass
        if owner not in view_names:
            owner_view = doc.GetElement(element.OwnerViewId)
            view_names[owner] = safe_name(owner_view, u"<view>")
            try:
                view_scales[owner] = float(owner_view.Scale) or 1.0
            except Exception:
                view_scales[owner] = 1.0
        type_key = id_value(element.GetTypeId())
        if type_key not in label_cache:
            label_cache[type_key] = element_label(doc, element)
        label = label_cache[type_key]
        texts, _numbers, used = reader.read(element, cfg["sld_fields"])
        eid = id_value(element.Id)
        id_map[eid] = element.Id
        paper_xy = None
        point = _xyz_m(element)
        if point is not None:                       # metres in model -> mm on paper
            scale = view_scales[owner]
            paper_xy = (point[0] * 1000.0 / scale, point[1] * 1000.0 / scale)
        records.append({"id": eid, "label": label, "view": view_names[owner],
                        "view_id": owner, "f": texts, "params": used, "paper_xy": paper_xy,
                        "role": rules.detail_role(label, texts, role_rules)})
    return records


def _part_type(doc, element):
    try:
        family = element.Symbol.Family
        param = family.get_Parameter(DB.BuiltInParameter.FAMILY_CONTENT_PART_TYPE)
        if param is not None:
            return param.AsValueString() or u""
    except Exception:
        pass
    return u""


_FT_TO_M = 0.3048


def _xyz_m(element):
    """Location point in metres (for the board-section distance test)."""
    try:
        point = element.Location.Point
        return (point.X * _FT_TO_M, point.Y * _FT_TO_M, point.Z * _FT_TO_M)
    except Exception:
        return None


def collect_model(doc, cfg, reader, id_map, link_info=None):
    """Electrical Equipment, Circuits and named Electrical Fixtures of the host,
    plus (cfg include_linked_models) of every loaded Revit link.

    link_info: list that receives (link name, record count) per link read."""
    records = _collect_doc(doc, cfg, reader, id_map, None)
    if not cfg.get("include_linked_models"):
        return records
    seen = set()
    for inst in DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance):
        try:
            link_doc = inst.GetLinkDocument()
        except Exception:
            link_doc = None
        if link_doc is None:
            # unloaded link: its boards cannot be matched - say so in the header
            try:
                name = safe_name(doc.GetElement(inst.GetTypeId()), u"<link>")
            except Exception:
                name = u"<link>"
            if link_info is not None and (name, None) not in link_info:
                link_info.append((name, None))
            continue
        key = link_doc.PathName or link_doc.Title
        if key in seen:
            continue                       # same link placed twice
        seen.add(key)
        prefix = u"L{0}:".format(id_value(inst.Id))
        link_reader = ParamReader(link_doc, cfg["units"])
        linked = _collect_doc(link_doc, cfg, link_reader, None, (prefix, link_doc.Title))
        records.extend(linked)
        if link_info is not None:
            link_info.append((link_doc.Title, len(linked)))
    return records


def _collect_doc(doc, cfg, reader, id_map, link):
    """link = None (host) or (id prefix, link title). Linked records get string
    ids 'L<link instance id>:<element id>' and are not selectable."""
    records = []
    cat = DB.BuiltInCategory
    equipment_cat = int(cat.OST_ElectricalEquipment)

    def make_id(element_id):
        if link is None:
            eid = id_value(element_id)
            id_map[eid] = element_id
            return eid
        return u"{0}{1}".format(link[0], id_value(element_id))

    def tag(label):
        return label if link is None else u"{0} [link: {1}]".format(label, link[1])

    for element in (DB.FilteredElementCollector(doc).OfCategory(cat.OST_ElectricalEquipment)
                    .WhereElementIsNotElementType()):
        try:
            if isinstance(element, DB.FamilyInstance) and element.SuperComponent is not None:
                continue
        except Exception:
            pass
        texts, numbers, _used = reader.read(element, cfg["model_equipment_fields"])
        records.append({"id": make_id(element.Id), "kind": "equipment",
                        "label": tag(element_label(doc, element)), "type_label": element_label(doc, element),
                        "linked": link is not None, "xyz": _xyz_m(element),
                        "part_type": _part_type(doc, element), "f": texts, "n": numbers})

    for element in (DB.FilteredElementCollector(doc).OfCategory(cat.OST_ElectricalFixtures)
                    .WhereElementIsNotElementType()):
        texts, numbers, _used = reader.read(element, cfg["model_fixture_fields"])
        if not texts.get("name"):
            continue                       # unnamed sockets etc. never join matching
        records.append({"id": make_id(element.Id), "kind": "fixture",
                        "label": tag(element_label(doc, element)), "type_label": element_label(doc, element),
                        "linked": link is not None, "xyz": _xyz_m(element),
                        "part_type": u"", "f": texts, "n": numbers})

    spare_type = getattr(DBE.CircuitType, "Spare", None)
    space_type = getattr(DBE.CircuitType, "Space", None)
    for system in (DB.FilteredElementCollector(doc).OfClass(DBE.ElectricalSystem)):
        try:
            if system.SystemType != DBE.ElectricalSystemType.PowerCircuit:
                continue
        except Exception:
            pass
        texts, numbers, _used = reader.read(system, cfg["model_circuit_fields"])
        source = u""
        try:
            base = system.BaseEquipment
            if base is not None:
                param = base.get_Parameter(DB.BuiltInParameter.RBS_ELEC_PANEL_NAME)
                source = (param.AsString() if param is not None else u"") or u""
        except Exception:
            source = u""
        downstream = []
        try:
            for member in system.Elements:
                if member.Category is not None and id_value(member.Category.Id) == equipment_cat:
                    param = member.get_Parameter(DB.BuiltInParameter.RBS_ELEC_PANEL_NAME)
                    name = param.AsString() if param is not None else None
                    if name and name not in downstream:
                        downstream.append(name)
        except Exception:
            pass
        try:
            ctype = system.CircuitType
        except Exception:
            ctype = None
        records.append({"id": make_id(system.Id), "kind": "circuit", "linked": link is not None,
                        "label": tag(u"Circuit {0} ({1})".format(
                            texts.get("circuit_number", u"?"), source or texts.get("panel", u"?"))),
                        "part_type": u"", "f": texts, "n": numbers,
                        "source_panel": source or texts.get("panel", u""),
                        "downstream": downstream,
                        "is_spare": ctype is not None and ctype == spare_type,
                        "is_space": ctype is not None and ctype == space_type})
    return records


# ----------------------------------------------------------------------
# Parameter discovery for the setup window
# ----------------------------------------------------------------------
def _param_text(param):
    try:
        if param.StorageType == DB.StorageType.String:
            value = param.AsString()
        else:
            value = param.AsValueString()
    except Exception:
        return None
    return value.strip() if value and value.strip() else None


def _scan(doc, elements, limit):
    """{param name: [items with value, sample value]} over instance + type
    parameters of up to `limit` elements."""
    found, type_values = {}, {}
    scanned = 0
    for element in elements:
        if scanned >= limit:
            break
        scanned += 1
        names_here = {}
        for param in element.Parameters:
            try:
                name = param.Definition.Name
            except Exception:
                continue
            names_here[name] = _param_text(param)
        try:
            type_id = element.GetTypeId()
            type_key = id_value(type_id)
        except Exception:
            type_key = None
        if type_key is not None and type_key >= 0:
            if type_key not in type_values:
                etype = doc.GetElement(type_id)
                values = {}
                if etype is not None:
                    for param in etype.Parameters:
                        try:
                            values[param.Definition.Name] = _param_text(param)
                        except Exception:
                            continue
                type_values[type_key] = values
            for name, value in type_values[type_key].items():
                if not names_here.get(name):
                    names_here[name] = value
        for name, value in names_here.items():
            entry = found.setdefault(name, [0, u""])
            if value:
                entry[0] += 1
                if not entry[1]:
                    entry[1] = value
    return found, scanned


def discover_parameters(doc, limit=4000):
    """Parameter names really present, per source:
    {'sld': ({name: [count, sample]}, scanned), 'model_equipment_fields': ...}"""
    cat = DB.BuiltInCategory

    def by_cat(bic):
        return (DB.FilteredElementCollector(doc).OfCategory(bic)
                .WhereElementIsNotElementType())

    power = []
    for system in DB.FilteredElementCollector(doc).OfClass(DBE.ElectricalSystem):
        try:
            if system.SystemType == DBE.ElectricalSystemType.PowerCircuit:
                power.append(system)
        except Exception:
            power.append(system)
    return {
        "sld": _scan(doc, by_cat(cat.OST_DetailComponents), limit),
        "model_equipment_fields": _scan(doc, by_cat(cat.OST_ElectricalEquipment), limit),
        "model_circuit_fields": _scan(doc, power, limit),
        "model_fixture_fields": _scan(doc, by_cat(cat.OST_ElectricalFixtures), limit),
    }


def mapping_coverage(details, cfg):
    """{field: (count with value, [parameter names used])} - shows which SLD
    parameters were actually found, so a wrong mapping is obvious."""
    out = {}
    for field in cfg["sld_fields"]:
        count, names = 0, set()
        for rec in details:
            if rec["f"].get(field):
                count += 1
                names.add(rec["params"].get(field))
        out[field] = (count, sorted(n for n in names if n))
    return out
