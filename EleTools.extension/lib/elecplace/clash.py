# -*- coding: utf-8 -*-
"""
elecplace.clash
===============
Keeps new fixtures clear of MSFDs, other accessories, equipment and
(optionally) ducts / pipes / cable trays / conduits - in THIS project and in
every loaded link.

How it works
  * Obstacles are collected once per plan with their cheap axis-aligned
    bounding box (host coordinates) into a spatial hash.
  * A candidate fixture is modelled as an ORIENTED box from the family
    type's real geometry (+ clearance), aligned with how it will be placed.
  * Only obstacles whose bounding box touches the fixture are examined
    in detail: family instances get an oriented box from their own
    geometry and axes (so a rotated MSFD is not treated as a huge square);
    ducts / pipes / trays / conduits become a box along their centre line
    with their real size. The test is a separating-axis test
    (mathutil.obb_overlap).
  * planner.py slides a clashing fixture along the wall (or along the unit)
    step by step until it is clear, and reports what it avoided.

Revit API used:
  FilteredElementCollector, ElementMulticategoryFilter, FamilyInstance,
  MEPCurve (Diameter, Width, Height), LocationCurve, Line,
  Element.get_BoundingBox, FamilySymbol.get_Geometry, Options,
  BuiltInParameter.RBS_PIPE_OUTER_DIAMETER / RBS_CONDUIT_OUTER_DIAM_PARAM
"""
import math

from System.Collections.Generic import List
from Autodesk.Revit import DB

from elecplace.compat import eid_int, symbol_label, to_text
from elecplace.equipgeom import _collect_points, equipment_frame
from elecplace.mathutil import obb_aabb, obb_overlap

# Family categories treated as obstacles (missing ones are skipped)
EQUIPMENT_OBSTACLES = [
    "OST_MechanicalEquipment", "OST_PlumbingFixtures", "OST_PlumbingEquipment",
    "OST_DuctAccessory", "OST_PipeAccessory", "OST_DuctTerminal",
    "OST_ElectricalEquipment", "OST_ElectricalFixtures", "OST_LightingDevices",
    "OST_CommunicationDevices", "OST_DataDevices", "OST_FireAlarmDevices",
    "OST_SecurityDevices", "OST_NurseCallDevices", "OST_TelephoneDevices",
    "OST_MechanicalControlDevices", "OST_SpecialityEquipment",
    "OST_VerticalCirculation",        # lifts / escalators (Revit 2022+)
]
# Linear MEP elements (optional - Options tab)
CURVE_OBSTACLES = ["OST_DuctCurves", "OST_PipeCurves", "OST_CableTray", "OST_Conduit"]

CELL_FT = 5.0
MAX_CELLS = 400            # larger obstacles go to a short linear list


def _t(v):
    return (v.X, v.Y, v.Z)


def _bics(names):
    from elecplace.planner import get_bic
    return [b for b in (get_bic(n) for n in names) if b is not None]


class Box(object):
    """Oriented box in host coordinates (tuples for fast pure-python maths)."""

    def __init__(self, center, axes, half):
        self.c = center
        self.axes = axes
        self.half = half
        self.lo, self.hi = obb_aabb(center, axes, half)

    def overlaps(self, other):
        return obb_overlap(self.c, self.axes, self.half, other.c, other.axes, other.half)


class Obstacle(object):
    def __init__(self, label, key, lo, hi, provider=None, box=None, cat=None):
        self.label = label
        self.key = key              # (link instance id or None, element id)
        self.cat = cat              # category id (int) or None (new fixtures)
        self.lo, self.hi = lo, hi
        self._provider = provider
        self._box = box

    def box(self):
        """Precise oriented box, computed on first use (falls back to AABB)."""
        if self._box is None:
            try:
                self._box = self._provider()
            except Exception:
                self._box = None
            if self._box is None:
                c = tuple((a + b) / 2.0 for a, b in zip(self.lo, self.hi))
                h = tuple((b - a) / 2.0 for a, b in zip(self.lo, self.hi))
                self._box = Box(c, [(1, 0, 0), (0, 1, 0), (0, 0, 1)], h)
        return self._box


def _instance_box(inst, xf):
    """Oriented box of a family instance from its own axes and geometry."""
    fr = equipment_frame(inst, xf)
    mids = dict((k, (lo + hi) / 2.0) for k, (lo, hi) in fr.ext.items())
    c = (fr.origin.Add(fr.F.Multiply(mids["F"])).Add(fr.H.Multiply(mids["H"]))
         .Add(fr.Z.Multiply(mids["Z"])))
    half = tuple(max((fr.ext[k][1] - fr.ext[k][0]) / 2.0, 0.01) for k in ("F", "H", "Z"))
    return Box(_t(c), [_t(fr.F), _t(fr.H), _t(fr.Z)], half)


def _curve_size(el):
    """(width, height) of a duct / pipe / tray / conduit in feet."""
    for bip_name in ("RBS_PIPE_OUTER_DIAMETER", "RBS_CONDUIT_OUTER_DIAM_PARAM"):
        bip = getattr(DB.BuiltInParameter, bip_name, None)
        if bip is not None:
            prm = el.get_Parameter(bip)
            if prm is not None and prm.HasValue and prm.AsDouble() > 0:
                return prm.AsDouble(), prm.AsDouble()
    try:
        d = el.Diameter
        if d > 0:
            return d, d
    except Exception:
        pass
    try:
        return el.Width, el.Height
    except Exception:
        return 0.3, 0.3


def _curve_box(el, xf):
    loc = el.Location
    if not isinstance(loc, DB.LocationCurve) or not isinstance(loc.Curve, DB.Line):
        return None
    p0, p1 = xf.OfPoint(loc.Curve.GetEndPoint(0)), xf.OfPoint(loc.Curve.GetEndPoint(1))
    v = p1.Subtract(p0)
    length = v.GetLength()
    if length < 1e-6:
        return None
    a = v.Normalize()
    b = a.CrossProduct(DB.XYZ.BasisZ)
    b = b.Normalize() if b.GetLength() > 1e-6 else DB.XYZ.BasisX
    c = a.CrossProduct(b).Normalize()
    w, h = _curve_size(el)
    mid = p0.Add(v.Multiply(0.5))
    return Box(_t(mid), [_t(a), _t(b), _t(c)], (length / 2.0, w / 2.0, h / 2.0))


class ClashIndex(object):
    """Spatial hash of obstacles; find() returns the first clashing label."""

    def __init__(self):
        self.grid = {}
        self.large = []
        self.count = 0

    def _cells(self, lo, hi):
        ix = range(int(math.floor(lo[0] / CELL_FT)), int(math.floor(hi[0] / CELL_FT)) + 1)
        iy = range(int(math.floor(lo[1] / CELL_FT)), int(math.floor(hi[1] / CELL_FT)) + 1)
        iz = range(int(math.floor(lo[2] / CELL_FT)), int(math.floor(hi[2] / CELL_FT)) + 1)
        return ix, iy, iz

    def add(self, ob):
        ix, iy, iz = self._cells(ob.lo, ob.hi)
        self.count += 1
        if len(ix) * len(iy) * len(iz) > MAX_CELLS:
            self.large.append(ob)
            return
        for x in ix:
            for y in iy:
                for z in iz:
                    self.grid.setdefault((x, y, z), []).append(ob)

    def add_fixture(self, box, label):
        """Register a newly planned fixture so later fixtures avoid it too."""
        self.add(Obstacle(label, None, box.lo, box.hi, box=box))

    def find(self, box, ignore_key=None, ignore_cats=None):
        """First obstacle overlapping `box`. ignore_key: one element to skip;
        ignore_cats: category ids (ints) whose elements are not obstacles."""
        seen = set()
        ix, iy, iz = self._cells(box.lo, box.hi)
        cands = list(self.large)
        for x in ix:
            for y in iy:
                for z in iz:
                    cands.extend(self.grid.get((x, y, z), ()))
        for ob in cands:
            if id(ob) in seen:
                continue
            seen.add(id(ob))
            if ignore_key is not None and ob.key == ignore_key:
                continue
            if ignore_cats and ob.cat is not None and ob.cat in ignore_cats:
                continue
            if (ob.hi[0] <= box.lo[0] or ob.lo[0] >= box.hi[0] or ob.hi[1] <= box.lo[1] or
                    ob.lo[1] >= box.hi[1] or ob.hi[2] <= box.lo[2] or ob.lo[2] >= box.hi[2]):
                continue                                    # bounding boxes apart
            if box.overlaps(ob.box()):
                return ob.label
        return None


def build_clash_index(doc, links, settings, logger=None):
    """Collect obstacles from this project and every loaded link."""
    index = ClashIndex()
    sources = [(u"this project", doc, DB.Transform.Identity, None)]
    sources += [(lk.title, lk.doc, lk.transform, lk.id) for lk in links]
    eq_bics = _bics(EQUIPMENT_OBSTACLES)
    cv_bics = _bics(CURVE_OBSTACLES) if settings.clash_include_mep_curves else []

    for src, sdoc, xf, link_id in sources:
        for bics, is_curve in ((eq_bics, False), (cv_bics, True)):
            if not bics:
                continue
            flt = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](bics))
            col = DB.FilteredElementCollector(sdoc).WherePasses(flt).WhereElementIsNotElementType()
            for el in col:
                try:
                    try:
                        cat = eid_int(el.Category.Id)
                    except Exception:
                        cat = None
                    if is_curve:
                        box = _curve_box(el, xf)
                        if box is None:
                            continue
                        label = u"{} [{}] ({})".format(el.Category.Name, eid_int(el.Id), src)
                        index.add(Obstacle(label, (link_id, eid_int(el.Id)), box.lo, box.hi, box=box,
                                           cat=cat))
                        continue
                    if not isinstance(el, DB.FamilyInstance) or el.SuperComponent is not None:
                        continue
                    bb = el.get_BoundingBox(None)
                    if bb is None:
                        continue
                    corners = [xf.OfPoint(DB.XYZ(x, y, z)) for x in (bb.Min.X, bb.Max.X)
                               for y in (bb.Min.Y, bb.Max.Y) for z in (bb.Min.Z, bb.Max.Z)]
                    lo = tuple(min(getattr(p, a) for p in corners) for a in "XYZ")
                    hi = tuple(max(getattr(p, a) for p in corners) for a in "XYZ")
                    label = u"{} [{}] ({})".format(symbol_label(el.Symbol), eid_int(el.Id), src)
                    index.add(Obstacle(label, (link_id, eid_int(el.Id)), lo, hi,
                                       provider=(lambda e=el, t=xf: _instance_box(e, t)), cat=cat))
                except Exception as ex:
                    if logger:
                        logger.debug(u"obstacle skipped: {}".format(to_text(ex)))
    return index


# --------------------------------------------------------------------------
# Fixture size
# --------------------------------------------------------------------------
_DIMS = {}


def fixture_dims(symbol, default_ft):
    """
    (min, max) extents of the family type along its own X, Y, Z, from the
    loaded type's geometry. Falls back to a default cube if unreadable.
    """
    key = eid_int(symbol.Id)
    if key not in _DIMS:
        pts = []
        try:
            opts = DB.Options()
            opts.DetailLevel = DB.ViewDetailLevel.Fine
            _collect_points(symbol.get_Geometry(opts), pts)
        except Exception:
            pts = []
        if pts:
            _DIMS[key] = tuple((min(getattr(p, a) for p in pts), max(getattr(p, a) for p in pts))
                               for a in "XYZ")
        else:
            h = default_ft / 2.0
            _DIMS[key] = ((-h, h), (-h, h), (0.0, default_ft / 2.0))
    return _DIMS[key]


def fixture_box(symbol, point, normal, ref_dir, face_hosted, clearance, default_ft):
    """
    Oriented box a fixture will occupy once placed.
      face-hosted : family X = ref_dir, Z = face normal, Y = Z x X (upright)
      level-based : plan footprint taken square (rotation-safe), Z = up
    """
    (x0, x1), (y0, y1), (z0, z1) = fixture_dims(symbol, default_ft)
    if face_hosted:
        ax_x, ax_z = ref_dir, normal
        ax_y = ax_z.CrossProduct(ax_x).Normalize()
        c = (point.Add(ax_x.Multiply((x0 + x1) / 2.0)).Add(ax_y.Multiply((y0 + y1) / 2.0))
             .Add(ax_z.Multiply((z0 + z1) / 2.0)))
        half = ((x1 - x0) / 2.0, (y1 - y0) / 2.0, (z1 - z0) / 2.0)
    else:
        ax_x = DB.XYZ(ref_dir.X, ref_dir.Y, 0.0)
        ax_x = ax_x.Normalize() if ax_x.GetLength() > 1e-9 else DB.XYZ.BasisX
        ax_z = DB.XYZ.BasisZ
        ax_y = ax_z.CrossProduct(ax_x)
        r = max(abs(x0), abs(x1), abs(y0), abs(y1))
        c = point.Add(ax_z.Multiply((z0 + z1) / 2.0))
        half = (r, r, (z1 - z0) / 2.0)
    half = tuple(max(h, 0.01) + clearance for h in half)
    return Box(_t(c), [_t(ax_x), _t(ax_y), _t(ax_z)], half)
