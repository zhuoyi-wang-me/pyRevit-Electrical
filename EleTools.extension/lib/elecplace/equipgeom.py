# -*- coding: utf-8 -*-
"""
elecplace.equipgeom
===================
Geometry for placing fixtures ON an equipment instance (FCU, BCU, AHU,
water heater, damper...) with an offset.

1. equipment_frame()  - builds the equipment's local frame in HOST
   coordinates: origin, Facing (front), Hand (right) and Up axes, plus the
   real extents of its solid geometry along those axes. This works for
   rotated and mirrored instances, and for instances inside linked models.
2. EquipFrame.face_point() - centre of the chosen side (front / back /
   left / right / top / bottom) shifted by two in-face offsets.
3. FaceFinder - casts a ray from just outside that point back onto the
   equipment with a ReferenceIntersector (FindReferencesInRevitLinks=True)
   and returns the hit face Reference. For linked equipment this is a link
   reference, so face-based fixtures can be hosted directly on it.

Offsets use the UNIT'S OWN AXES, whatever side is chosen, so they
follow rotated / mirrored equipment and accessories:
  X = hand direction (unit's right)   Y = facing direction (unit's front)
  Z = unit's up
measured from the centre of the chosen side. The part of the offset that
points out of the face is a stand-off: applied to level-based fixtures,
ignored for face-hosted ones (they must sit on the face).

Revit API used:
  FamilyInstance (GetTransform, FacingOrientation, HandOrientation,
  get_Geometry), Options, ViewDetailLevel, GeometryInstance
  (GetInstanceGeometry), Solid, Edge.AsCurve, View3D (IsTemplate,
  IsSectionBoxActive), ReferenceIntersector, FindReferenceTarget,
  ReferenceWithContext (GetReference, Proximity), Reference
  (ElementId, LinkedElementId), ElementMulticategoryFilter
"""
from System.Collections.Generic import List
from Autodesk.Revit import DB

from elecplace.compat import eid_int, element_name

SIDES = [("front", u"Front (facing side)"), ("back", u"Back"),
         ("right", u"Right (hand side)"), ("left", u"Left"),
         ("top", u"Top"), ("bottom", u"Bottom (underside)")]

STANDOFF_FT = 2.0      # ray starts this far outside the face
DEPTH_TOL_FT = 1.0     # accept faces recessed up to this much


# --------------------------------------------------------------------------
# Local frame + extents
# --------------------------------------------------------------------------
def _collect_points(geo, pts, depth=0):
    """End points of solid edges (recurses into nested families)."""
    if geo is None or depth > 3:
        return
    for obj in geo:
        if isinstance(obj, DB.GeometryInstance):
            _collect_points(obj.GetInstanceGeometry(), pts, depth + 1)
        elif isinstance(obj, DB.Solid) and obj.Volume > 1e-9:
            for edge in obj.Edges:
                crv = edge.AsCurve()
                pts.append(crv.GetEndPoint(0))
                pts.append(crv.GetEndPoint(1))


def _bbox_points(inst):
    bb = inst.get_BoundingBox(None)
    if bb is None:
        return []
    lo, hi = bb.Min, bb.Max
    return [DB.XYZ(x, y, z) for x in (lo.X, hi.X) for y in (lo.Y, hi.Y) for z in (lo.Z, hi.Z)]


class EquipFrame(object):
    """Equipment local frame in HOST coordinates with geometric extents."""

    def __init__(self, origin, facing, hand, up, ext):
        self.origin = origin
        self.F = facing
        self.H = hand
        self.Z = up
        self.ext = ext            # {"F": (min, max), "H": (...), "Z": (...)}

    def _side(self, side):
        """(outward normal, axis key, use max extent)."""
        return {"front": (self.F, "F", True), "back": (self.F.Negate(), "F", False),
                "right": (self.H, "H", True), "left": (self.H.Negate(), "H", False),
                "top": (self.Z, "Z", True), "bottom": (self.Z.Negate(), "Z", False)}[side]

    def in_face_axes(self, side, normal):
        """(offset-1 axis, offset-2 axis) lying in the face plane."""
        if side == "top":
            return self.H, self.F
        if side == "bottom":
            return self.H, self.F.Negate()
        right = self.Z.CrossProduct(normal).Normalize()   # viewer's right
        return right, self.Z

    def face_point_xyz(self, side, dx, dy, dz):
        """
        Centre of `side` shifted by (dx, dy, dz) in the unit's X/Y/Z axes.
        Returns (point on the face plane, outward normal, in-face X axis for
        family orientation, stand-off distance along the normal).
        """
        centre, normal, ax1, _ax2 = self.face_point(side, 0.0, 0.0)
        shift = self.H.Multiply(dx).Add(self.F.Multiply(dy)).Add(self.Z.Multiply(dz))
        standoff = shift.DotProduct(normal)
        on_face = centre.Add(shift).Subtract(normal.Multiply(standoff))
        return on_face, normal, ax1, standoff

    def face_point(self, side, off_1, off_2):
        """Centre of `side` + in-face offsets. Returns (point, normal, axis1, axis2)."""
        normal, key, use_max = self._side(side)
        coords = {}
        for k in ("F", "H", "Z"):
            lo, hi = self.ext[k]
            coords[k] = (lo + hi) / 2.0
        lo, hi = self.ext[key]
        coords[key] = hi if use_max else lo
        p = (self.origin.Add(self.F.Multiply(coords["F"]))
             .Add(self.H.Multiply(coords["H"])).Add(self.Z.Multiply(coords["Z"])))
        ax1, ax2 = self.in_face_axes(side, normal)
        p = p.Add(ax1.Multiply(off_1)).Add(ax2.Multiply(off_2))
        return p, normal, ax1, ax2


def equipment_points(inst):
    """Solid-edge end points of an instance in ITS OWN document coordinates."""
    opts = DB.Options()
    opts.DetailLevel = DB.ViewDetailLevel.Fine
    pts = []
    _collect_points(inst.get_Geometry(opts), pts)
    return pts or _bbox_points(inst)


def edge_shift(offset, lo, hi):
    """
    Offset measured from the unit EDGE instead of its centre.
      offset > 0 : beyond the positive edge   -> hi + offset
      offset < 0 : beyond the negative edge   -> lo + offset
      offset = 0 : centre (no shift)
    lo / hi are the unit's extents along that axis relative to its
    insertion point (lo <= 0 <= hi for normal families).
    """
    if offset > 0:
        return hi + offset
    if offset < 0:
        return lo + offset
    return 0.0


def equipment_frame(inst, to_host):
    """Build the EquipFrame of `inst` (in its own doc) mapped by `to_host`."""
    t = inst.GetTransform()
    origin = t.Origin
    facing = inst.FacingOrientation
    hand = inst.HandOrientation
    up = t.BasisZ

    pts = equipment_points(inst)
    if not pts:
        raise ValueError("equipment has no geometry")

    ext = {}
    for key, axis in (("F", facing), ("H", hand), ("Z", up)):
        vals = [p.Subtract(origin).DotProduct(axis) for p in pts]
        ext[key] = (min(vals), max(vals))

    return EquipFrame(to_host.OfPoint(origin), to_host.OfVector(facing).Normalize(),
                      to_host.OfVector(hand).Normalize(), to_host.OfVector(up).Normalize(), ext)


# --------------------------------------------------------------------------
# Face hosting via ReferenceIntersector
# --------------------------------------------------------------------------
def find_3d_view(doc):
    """
    A non-template 3D view without an active section box, used for ray
    casting ('{3D}' preferred). Elements hidden in that view cannot be hit.
    """
    candidates = []
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View3D):
        try:
            if v.IsTemplate or v.IsSectionBoxActive:
                continue
        except Exception:
            continue
        candidates.append(v)
    candidates.sort(key=lambda v: (0 if element_name(v).startswith(u"{3D") else 1, element_name(v)))
    return candidates[0] if candidates else None


class FaceFinder(object):
    """Ray-casts onto a specific equipment instance (host or linked)."""

    def __init__(self, view3d, bics):
        self.view = view3d
        flt = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](bics))
        self.ri = DB.ReferenceIntersector(flt, DB.FindReferenceTarget.Face, view3d)
        self.ri.FindReferencesInRevitLinks = True
        self._ri_all = None

    def _unfiltered(self):
        """Fallback intersector without a category filter (matched by ids)."""
        if self._ri_all is None:
            self._ri_all = DB.ReferenceIntersector(self.view)
            self._ri_all.TargetType = DB.FindReferenceTarget.Face
            self._ri_all.FindReferencesInRevitLinks = True
        return self._ri_all

    def hit(self, equipment, point, normal):
        """
        Returns (reference, host point on the face) or None.
        Works for equipment in THIS project (plain reference) and in a
        LINKED model (link reference: ElementId = link instance,
        LinkedElementId = equipment). The hit must belong to `equipment` and
        lie close to the expected face plane. If the category-filtered cast
        finds nothing, an unfiltered cast is tried (some Revit versions do
        not apply category filters to elements inside links).
        """
        res = self._cast(self.ri, equipment, point, normal)
        if res is None:
            res = self._cast(self._unfiltered(), equipment, point, normal)
        return res

    def _cast(self, intersector, equipment, point, normal):
        origin = point.Add(normal.Multiply(STANDOFF_FT))
        direction = normal.Negate()
        best = None
        for rwc in intersector.Find(origin, direction):
            ref = rwc.GetReference()
            if equipment.link_instance_id is None:
                ok = (eid_int(ref.ElementId) == equipment.elem_id and
                      ref.LinkedElementId == DB.ElementId.InvalidElementId)
            else:
                ok = (eid_int(ref.ElementId) == equipment.link_instance_id and
                      eid_int(ref.LinkedElementId) == equipment.elem_id)
            if ok and (best is None or rwc.Proximity < best[1]):
                best = (ref, rwc.Proximity)
        if best is None or best[1] > STANDOFF_FT + DEPTH_TOL_FT:
            return None
        return best[0], origin.Add(direction.Multiply(best[1]))


def legacy_offsets_to_xyz(side, off_1, off_2):
    """
    Convert v3 templates (viewer-relative 'right' / 'up' offsets) to the
    unit-axis X / Y / Z offsets used from v4 on.
    """
    if side == "front":
        return -off_1, 0.0, off_2
    if side == "back":
        return off_1, 0.0, off_2
    if side == "right":
        return 0.0, off_1, off_2
    if side == "left":
        return 0.0, -off_1, off_2
    if side == "top":
        return off_1, off_2, 0.0
    return off_1, -off_2, 0.0          # bottom
