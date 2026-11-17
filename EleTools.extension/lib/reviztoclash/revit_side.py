# -*- coding: utf-8 -*-
"""Revit side of Revizto Clashes: the host model and every loaded link as
"sources", element lookup by id / UniqueId / IFC GUID, and clash boxes.
READ-ONLY - nothing here changes the model.

Revit 2025 / 2026 (ElementId.Value, Int64) with a fallback to IntegerValue.
"""
from Autodesk.Revit.DB import (BuiltInParameter, ElementId, FilteredElementCollector,
                               RevitLinkInstance, XYZ)
from pyrevit import revit

from reviztoclash import match, parse, report


def id_value(element_id):
    """ElementId -> int (Revit 2024+ .Value, older .IntegerValue)."""
    if element_id is None:
        return None
    if hasattr(element_id, "Value"):
        return int(element_id.Value)
    return int(element_id.IntegerValue)


def make_id(value):
    """int -> ElementId (Int64 constructor in Revit 2024+, Int32 before)."""
    try:
        from System import Int64
        return ElementId(Int64(int(value)))
    except Exception:
        return ElementId(int(value))


def safe_name(element, default=u""):
    if element is None:
        return default
    try:
        name = revit.query.get_name(element)
        if name:
            return parse.to_text(name)
    except Exception:
        pass
    try:
        return parse.to_text(element.Name) or default
    except Exception:
        return default


def element_label(element):
    """'Family : Type' for family instances (what Revizto shows), else the name."""
    name = safe_name(element)
    try:
        family = parse.to_text(element.Symbol.FamilyName)
    except Exception:
        family = u""
    if family and family != name:
        return u"{0} : {1}".format(family, name) if name else family
    return name


def category_name(element):
    try:
        return parse.to_text(element.Category.Name) if element.Category is not None else u""
    except Exception:
        return u""


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------
class Source(object):
    """The host model or one link INSTANCE (each instance has its own place)."""

    def __init__(self, doc, key, name, is_link, doc_key, transform=None, instance_id=None):
        self.doc = doc
        self.key = key
        self.name = name
        self.is_link = is_link
        self.doc_key = doc_key
        self.transform = transform          # link -> host coordinates, None for host
        self.instance_id = instance_id      # RevitLinkInstance id (int) for links
        self._ifc = None

    def element(self, element_id):
        try:
            return self.doc.GetElement(make_id(element_id))
        except Exception:
            return None

    def find(self, kind, value):
        """-> (element id int, category, name) or None."""
        element = None
        try:
            if kind == "ids":
                element = self.doc.GetElement(make_id(value))
            elif kind == "uids":
                element = self.doc.GetElement(value)
            elif kind == "ifc":
                found = self._ifc_map().get(value)
                element = self.doc.GetElement(make_id(found)) if found is not None else None
        except Exception:
            element = None
        if element is None:
            return None
        return id_value(element.Id), category_name(element), element_label(element)

    def _ifc_map(self):
        """IFC GUID -> element id. Built once, only when an export has IFC
        GUIDs and no element ids (BCF from IFC based tools)."""
        if self._ifc is None:
            self._ifc = {}
            try:
                from Autodesk.Revit.DB import ExportUtils
            except Exception:
                ExportUtils = None
            collector = FilteredElementCollector(self.doc).WhereElementIsNotElementType()
            for element in collector:
                value = id_value(element.Id)
                try:
                    stored = element.get_Parameter(BuiltInParameter.IFC_GUID)
                    if stored is not None and stored.HasValue:
                        self._ifc[parse.to_text(stored.AsString())] = value
                except Exception:
                    pass
                if ExportUtils is not None:
                    try:
                        guid = parse.to_text(ExportUtils.GetExportId(self.doc, element.Id))
                        self._ifc.setdefault(parse.guid_to_ifc(guid), value)
                    except Exception:
                        pass
        return self._ifc


def build_sources(doc):
    """(sources, unloaded link names). Host first, then each loaded link instance."""
    sources = [Source(doc, "host", parse.to_text(doc.Title), False, "host")]
    unloaded = []
    for instance in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        link_doc = None
        try:
            link_doc = instance.GetLinkDocument()
        except Exception:
            pass
        type_name = u""
        try:
            type_name = safe_name(doc.GetElement(instance.GetTypeId()))
        except Exception:
            pass
        if link_doc is None:
            name = type_name or safe_name(instance, u"<link>")
            if name not in unloaded:
                unloaded.append(name)
            continue
        try:
            doc_key = parse.to_text(link_doc.PathName) or parse.to_text(link_doc.Title)
        except Exception:
            doc_key = type_name
        sources.append(Source(link_doc, id_value(instance.Id),
                              type_name or parse.to_text(link_doc.Title), True, doc_key,
                              transform=instance.GetTotalTransform(),
                              instance_id=id_value(instance.Id)))
    return sources, unloaded


# ---------------------------------------------------------------------------
# boxes (host coordinates, internal feet, as tuples)
# ---------------------------------------------------------------------------
def element_box(source, element_id):
    element = source.element(element_id)
    if element is None:
        return None
    try:
        box = element.get_BoundingBox(None)
    except Exception:
        box = None
    if box is None:
        return None
    lo, hi = box.Min, box.Max
    corners = [XYZ(x, y, z) for x in (lo.X, hi.X) for y in (lo.Y, hi.Y) for z in (lo.Z, hi.Z)]
    if source.transform is not None:
        corners = [source.transform.OfPoint(p) for p in corners]
    return ((min(p.X for p in corners), min(p.Y for p in corners), min(p.Z for p in corners)),
            (max(p.X for p in corners), max(p.Y for p in corners), max(p.Z for p in corners)))


def union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (tuple(min(b[0][i] for b in boxes) for i in range(3)),
            tuple(max(b[1][i] for b in boxes) for i in range(3)))


def intersection(a, b):
    if not a or not b:
        return None
    lo = tuple(max(a[0][i], b[0][i]) for i in range(3))
    hi = tuple(min(a[1][i], b[1][i]) for i in range(3))
    if any(lo[i] > hi[i] for i in range(3)):
        return None
    return lo, hi


def centre(box):
    if not box:
        return None
    return tuple((box[0][i] + box[1][i]) / 2.0 for i in range(3))


# ---------------------------------------------------------------------------
# resolve every clash
# ---------------------------------------------------------------------------
def resolve_all(clashes, sources):
    """-> list of entries, one per clash:
        {'clash', 'res_a', 'res_b', 'box' (both elements), 'focus' (where they
         overlap, else 'box'), 'row' (report.build_row)}"""
    by_key = dict((s.key, s) for s in sources)
    entries = []
    for index, clash in enumerate(clashes):
        res_a = match.resolve_side(clash["a"], sources)
        res_b = match.resolve_side(clash["b"], sources)
        box_a = union([element_box(by_key[k], v) for k, v in res_a["refs"]])
        box_b = union([element_box(by_key[k], v) for k, v in res_b["refs"]])
        box = union([box_a, box_b])
        focus = intersection(box_a, box_b) or box
        entries.append({"clash": clash, "res_a": res_a, "res_b": res_b, "box": box,
                        "focus": focus,
                        "row": report.build_row(index, clash, res_a, res_b, centre(focus), focus)})
    return entries
