# -*- coding: utf-8 -*-
"""
elecplace.geometry
==================
Room boundary -> host-wall segments, "which side is the room" detection,
room-facing wall face lookup, and door/window exclusion zones.

COORDINATES: everything here is in the LINKED model's internal
coordinates (feet). Conversion to host coordinates happens in planner.py
via LinkInfo.transform.

Revit API used:
  DB.BoundarySegment (ElementId, LinkElementId, GetCurve), DB.Line,
  DB.Wall (Width, WallType.Kind), DB.WallKind, DB.HostObjectUtils.GetSideFaces,
  DB.ShellLayerType, DB.Element.GetGeometryObjectFromReference,
  DB.PlanarFace (FaceNormal, Project), DB.IntersectionResult,
  DB.ElementMulticategoryFilter, DB.FamilyInstance (Host, Location, Symbol)
"""
from System.Collections.Generic import List
from Autodesk.Revit import DB

from elecplace.compat import eid_int
from elecplace.mathutil import subtract_intervals

PROBE_DIST_FT = 0.5       # offset used to test which side of a wall is the room
FACE_NORMAL_MIN_DOT = 0.98  # face normal must be within ~11 deg of room-inward
Z_TOL = 1e-3


class WallSegment(object):
    """A straight, wall-backed stretch of a room boundary (link coords)."""

    def __init__(self, wall, p0, p1, inward):
        self.wall = wall
        self.wall_id = eid_int(wall.Id)
        self.start = DB.XYZ(p0.X, p0.Y, 0.0)
        end = DB.XYZ(p1.X, p1.Y, 0.0)
        vec = end.Subtract(self.start)
        self.length = vec.GetLength()
        self.direction = vec.Normalize()
        self.inward = inward              # unit XY vector pointing INTO the room
        self.intervals = []               # usable (a, b) distances along segment

    def point_at(self, t, z):
        """Point at distance t along the segment, at absolute link Z."""
        return DB.XYZ(self.start.X + self.direction.X * t,
                      self.start.Y + self.direction.Y * t, z)


class FaceCache(object):
    """Caches the planar side faces of each wall (expensive to compute)."""

    def __init__(self):
        self._cache = {}

    def faces(self, wall):
        key = eid_int(wall.Id)
        if key in self._cache:
            return self._cache[key]
        found = []
        for side in (DB.ShellLayerType.Interior, DB.ShellLayerType.Exterior):
            try:
                refs = DB.HostObjectUtils.GetSideFaces(wall, side)
            except Exception:
                refs = []
            for ref in refs:
                try:
                    geo = wall.GetGeometryObjectFromReference(ref)
                except Exception:
                    geo = None
                if isinstance(geo, DB.PlanarFace):
                    found.append((ref, geo))
        self._cache[key] = found
        return found


def find_room_face(face_cache, wall, probe, inward, max_dist):
    """
    Find the wall face that looks into the room and contains `probe`
    once projected.  Returns (reference, face, projected_point) or None.

    A face is accepted when:
      * its normal points into the room (dot(normal, inward) >= 0.98),
      * the probe projects perpendicularly onto it (Face.Project != None),
      * the projection keeps the same Z (i.e. we are not above wall top),
      * it is not further than `max_dist` (wall width + tolerance).
    """
    best = None
    for ref, face in face_cache.faces(wall):
        if face.FaceNormal.DotProduct(inward) < FACE_NORMAL_MIN_DOT:
            continue
        res = face.Project(probe)
        if res is None:
            continue
        pt = res.XYZPoint
        if abs(pt.Z - probe.Z) > Z_TOL or res.Distance > max_dist:
            continue
        if best is None or res.Distance < best[3]:
            best = (ref, face, pt, res.Distance)
    if best is None:
        return None
    return best[0], best[1], best[2]


SKIN_WALL_MAX_FT = 20.0 / 304.8    # walls thinner than 20 mm are finish skins, never hosts
THROUGH_SEARCH_FT = 2.0            # look ~600 mm past the room boundary for the real wall


def find_face_through(face_cache, link_doc, probe, direction, max_search, skip_wall_id=None):
    """
    The room is bounded by a thin finish / skin wall (or a wall whose back face
    is interrupted at this point): find the next REAL wall behind it and its
    face pointing `direction` (away from the room). Returns
    (reference, face, projected_point, wall) or None.
    """
    end = probe.Add(direction.Multiply(max_search))
    pad = 0.2
    lo = DB.XYZ(min(probe.X, end.X) - pad, min(probe.Y, end.Y) - pad, probe.Z - pad)
    hi = DB.XYZ(max(probe.X, end.X) + pad, max(probe.Y, end.Y) + pad, probe.Z + pad)
    flt = DB.BoundingBoxIntersectsFilter(DB.Outline(lo, hi))
    best = None
    for wall in DB.FilteredElementCollector(link_doc).OfClass(DB.Wall).WherePasses(flt):
        try:
            if skip_wall_id is not None and eid_int(wall.Id) == skip_wall_id:
                continue
            if wall.Width < SKIN_WALL_MAX_FT:
                continue
        except Exception:
            continue
        for ref, face in face_cache.faces(wall):
            if face.FaceNormal.DotProduct(direction) < FACE_NORMAL_MIN_DOT:
                continue
            res = face.Project(probe)
            if res is None:
                continue
            pt = res.XYZPoint
            if abs(pt.Z - probe.Z) > Z_TOL or res.Distance > max_search:
                continue
            if pt.Subtract(probe).DotProduct(direction) <= 0:   # must be BEHIND the boundary
                continue
            if best is None or res.Distance < best[4]:
                best = (ref, face, pt, wall, res.Distance)
    if best is None:
        return None
    return best[0], best[1], best[2], best[3]


def describe_face_miss(face_cache, wall, probe, direction, max_dist):
    """
    Why find_room_face() found nothing - one short text for the report, e.g.
    'wall 123 (Basic Wall: Block 200, 200 mm): 2 side faces, none facing that
    way' or '1 facing that way but 380 mm away (limit 352 mm)'.
    """
    try:
        wname = u"{}: {}".format(wall.WallType.FamilyName, wall.WallType.Name)
    except Exception:
        wname = u"?"
    try:
        width_mm = int(round(wall.Width * 304.8))
    except Exception:
        width_mm = 0
    faces = face_cache.faces(wall)
    head = u"wall {} ({}, {} mm)".format(eid_int(wall.Id), wname, width_mm)
    if not faces:
        return head + u": no planar side faces (curtain / stacked / curved wall?)"
    facing = [(ref, f) for ref, f in faces if f.FaceNormal.DotProduct(direction) >= FACE_NORMAL_MIN_DOT]
    if not facing:
        return head + u": {} side face(s), none facing that way".format(len(faces))
    notes = []
    for _ref, f in facing:
        res = f.Project(probe)
        if res is None:
            # face exists but does not cover this point: split by a joined wall,
            # a lining wall or an opening, or it stops below / above this height
            notes.append(u"back face does not cover this point (wall joined / lining wall / "
                         u"opening there, or wall ends here)")
            continue
        if abs(res.XYZPoint.Z - probe.Z) > Z_TOL:
            notes.append(u"face not at this height (top / bottom of wall)")
        elif res.Distance > max_dist:
            notes.append(u"face {:.0f} mm away (limit {:.0f} mm) - lining / double wall?".format(
                res.Distance * 304.8, max_dist * 304.8))
    return head + u": " + (u"; ".join(notes) if notes else u"no usable face")


# --------------------------------------------------------------------------
# Openings (doors / windows) hosted by walls in the link
# --------------------------------------------------------------------------
_WIDTH_BIPS = ["DOOR_WIDTH", "WINDOW_WIDTH", "FAMILY_WIDTH_PARAM", "FAMILY_ROUGH_WIDTH_PARAM"]


def _opening_width(inst):
    """Best-effort opening width from instance then type parameters."""
    owners = [inst]
    try:
        owners.append(inst.Symbol)
    except Exception:
        pass
    for owner in owners:
        for name in _WIDTH_BIPS:
            bip = getattr(DB.BuiltInParameter, name, None)
            if bip is None:
                continue
            try:
                prm = owner.get_Parameter(bip)
                if prm and prm.HasValue and prm.StorageType == DB.StorageType.Double \
                        and prm.AsDouble() > 0:
                    return prm.AsDouble()
            except Exception:
                continue
    return 3.0   # ~900 mm fallback


def build_opening_index(link_doc):
    """{wall_id_int: [(location XYZ, width_ft), ...]} for doors & windows."""
    index = {}
    cats = List[DB.BuiltInCategory]([DB.BuiltInCategory.OST_Doors,
                                    DB.BuiltInCategory.OST_Windows])
    collector = (DB.FilteredElementCollector(link_doc)
                 .OfClass(DB.FamilyInstance)
                 .WherePasses(DB.ElementMulticategoryFilter(cats)))
    for inst in collector:
        host = inst.Host
        loc = inst.Location
        if not isinstance(host, DB.Wall) or not isinstance(loc, DB.LocationPoint):
            continue
        index.setdefault(eid_int(host.Id), []).append((loc.Point, _opening_width(inst)))
    return index


def _opening_exclusions(seg, openings, clearance):
    """Convert openings on the segment's wall to (start, end) distances."""
    out = []
    for pt, width in openings:
        rel = DB.XYZ(pt.X - seg.start.X, pt.Y - seg.start.Y, 0.0)
        # Opening must actually lie on this stretch of wall.
        if abs(rel.DotProduct(seg.inward)) > seg.wall.Width + 1.0:
            continue
        t = rel.DotProduct(seg.direction)
        half = width / 2.0 + clearance
        out.append((t - half, t + half))
    return out


# --------------------------------------------------------------------------
# Room -> wall segments
# --------------------------------------------------------------------------
def _in_room(room, x, y, z):
    try:
        return room.IsPointInRoom(DB.XYZ(x, y, z))
    except Exception:
        return False


def build_room_segments(room_info, settings, opening_index):
    """
    Turn a room's boundary loops into usable WallSegments.

    returns (segments, notes) where notes = {reason: count} of the boundary
    pieces that were rejected (for the report).
    """
    segments, notes = [], {}
    link_doc = room_info.link.doc
    room = room_info.room
    test_z = room_info.ffl(True) + 1.0          # 1 ft above room base

    def note(reason):
        notes[reason] = notes.get(reason, 0) + 1

    for loop in room_info.loops:
        for bseg in loop:
            # Walls coming from a link nested inside the architectural link
            # cannot be referenced reliably -> skip.
            try:
                if bseg.LinkElementId != DB.ElementId.InvalidElementId:
                    note(u"wall in nested link")
                    continue
            except AttributeError:
                pass

            element = link_doc.GetElement(bseg.ElementId)
            if not isinstance(element, DB.Wall):
                note(u"non-wall boundary (separation line / column)")
                continue
            try:
                if element.WallType.Kind == DB.WallKind.Curtain:
                    note(u"curtain wall")
                    continue
            except Exception:
                pass

            curve = bseg.GetCurve()
            if not isinstance(curve, DB.Line):
                note(u"curved wall segment")
                continue
            if curve.Length < settings.min_segment_length_ft:
                note(u"segment shorter than minimum")
                continue

            p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
            d = DB.XYZ(p1.X - p0.X, p1.Y - p0.Y, 0.0).Normalize()

            # Revit returns outer loops counter-clockwise, so the LEFT normal
            # (Z x d) points into the room. We still verify with
            # Room.IsPointInRoom because loop orientation is not a contract.
            inward = DB.XYZ(-d.Y, d.X, 0.0)
            mid = curve.Evaluate(0.5, True)
            if not _in_room(room, mid.X + inward.X * PROBE_DIST_FT,
                            mid.Y + inward.Y * PROBE_DIST_FT, test_z):
                if _in_room(room, mid.X - inward.X * PROBE_DIST_FT,
                            mid.Y - inward.Y * PROBE_DIST_FT, test_z):
                    inward = inward.Negate()

            seg = WallSegment(element, p0, p1, inward)

            # Usable range: keep clear of corners/ends and of openings.
            corner = settings.corner_clearance_ft
            exclusions = []
            if settings.avoid_openings:
                exclusions = _opening_exclusions(
                    seg, opening_index.get(seg.wall_id, []), settings.opening_clearance_ft)
            seg.intervals = subtract_intervals((corner, seg.length - corner),
                                               exclusions, settings.min_piece_length_ft)
            if not seg.intervals:
                note(u"no usable length after clearances/openings")
                continue
            segments.append(seg)

    return segments, notes


def notes_to_text(notes):
    if not notes:
        return u""
    return u"; ".join(u"{} x {}".format(v, k) for k, v in sorted(notes.items()))
