# -*- coding: utf-8 -*-
"""Geometry helpers: elevation extraction and "did it move?" fingerprinting.

The safety guarantee of this tool rests entirely on :func:`capture` and
:func:`max_delta`.  Before any parameter is touched we take a geometric
fingerprint of the element; after the change we take it again.  If the two
fingerprints differ by more than the configured tolerance the change is rolled
back at sub-transaction level and reported as a failure, so a partially
corrected model can never be produced.
"""

from Autodesk.Revit import DB

from . import config


# ---------------------------------------------------------------------------
# Location accessors
# ---------------------------------------------------------------------------

def location_point(element):
    """XYZ of a LocationPoint, or None."""
    try:
        loc = element.Location
        if isinstance(loc, DB.LocationPoint):
            return loc.Point
    except Exception:
        pass
    return None


def location_curve(element):
    """Curve of a LocationCurve, or None."""
    try:
        loc = element.Location
        if isinstance(loc, DB.LocationCurve):
            return loc.Curve
    except Exception:
        pass
    return None


def bounding_box(element):
    """Model bounding box (view independent), or None."""
    try:
        return element.get_BoundingBox(None)
    except Exception:
        return None


def bbox_center(element):
    box = bounding_box(element)
    if box is None:
        return None
    try:
        return (box.Min + box.Max) / 2.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Elevation determination
# ---------------------------------------------------------------------------

def reference_elevation(element, mode=config.ELEV_AUTO, category_rule=None):
    """Return the elevation (internal feet, project coordinates) used to pick
    the target level, or None when no reliable geometry exists.

    ``mode`` is the user selected setting; when it is ``ELEV_AUTO`` the rule
    is applied, and when that is also ``ELEV_AUTO`` a generic fallback chain is
    used: location point -> curve midpoint -> bounding box centre.
    """
    effective = mode
    if effective == config.ELEV_AUTO:
        effective = category_rule or config.ELEV_AUTO

    point = location_point(element)
    curve = location_curve(element)

    if effective == config.ELEV_INSERTION:
        if point is not None:
            return point.Z
        if curve is not None:
            return curve.GetEndPoint(0).Z

    elif effective == config.ELEV_MIDPOINT:
        if curve is not None:
            return _curve_midpoint(curve).Z
        if point is not None:
            return point.Z

    elif effective == config.ELEV_BBOX:
        center = bbox_center(element)
        if center is not None:
            return center.Z

    # ELEV_AUTO / generic fallback chain.
    if point is not None:
        return point.Z
    if curve is not None:
        return _curve_midpoint(curve).Z
    center = bbox_center(element)
    if center is not None:
        return center.Z
    return None


def _curve_midpoint(curve):
    """Midpoint of a curve; robust for arcs and unbound curves."""
    try:
        if curve.IsBound:
            return curve.Evaluate(0.5, True)
        return curve.GetEndPoint(0)
    except Exception:
        try:
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            return DB.XYZ((p0.X + p1.X) / 2.0,
                          (p0.Y + p1.Y) / 2.0,
                          (p0.Z + p1.Z) / 2.0)
        except Exception:
            return DB.XYZ(0, 0, 0)


def elevation_span(element):
    """Return (z_min, z_max) of the element, used for reporting/diagnostics."""
    box = bounding_box(element)
    if box is not None:
        return box.Min.Z, box.Max.Z
    point = location_point(element)
    if point is not None:
        return point.Z, point.Z
    curve = location_curve(element)
    if curve is not None:
        z0 = curve.GetEndPoint(0).Z
        z1 = curve.GetEndPoint(1).Z
        return min(z0, z1), max(z0, z1)
    return None, None


# ---------------------------------------------------------------------------
# Geometric fingerprint
# ---------------------------------------------------------------------------

class Fingerprint(object):
    """An immutable geometric signature of an element.

    ``points``       representative XYZ values (location point, curve ends or
                     bounding box corners)
    ``orientation``  facing / hand vectors for family instances, so that an
                     accidental rotation or mirror is also detected
    ``kind``         which source produced the points, so that two fingerprints
                     are only ever compared like for like
    """

    __slots__ = ('kind', 'points', 'orientation')

    def __init__(self, kind, points, orientation=None):
        self.kind = kind
        self.points = points or []
        self.orientation = orientation

    @property
    def is_valid(self):
        return bool(self.points)

    def z(self):
        """Representative Z of the fingerprint."""
        if not self.points:
            return None
        return sum(p.Z for p in self.points) / float(len(self.points))

    def __repr__(self):
        return '<Fingerprint %s n=%d>' % (self.kind, len(self.points))


def capture(element):
    """Take a geometric fingerprint of *element*."""
    points = []
    kind = 'none'
    orientation = None

    point = location_point(element)
    curve = location_curve(element)

    if curve is not None:
        kind = 'curve'
        try:
            points = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
        except Exception:
            points = []
    elif point is not None:
        kind = 'point'
        points = [point]

    if not points:
        box = bounding_box(element)
        if box is not None:
            kind = 'bbox'
            points = [box.Min, box.Max]

    # Rotation / mirror detection for family instances.
    try:
        if isinstance(element, DB.FamilyInstance):
            orientation = (element.FacingOrientation, element.HandOrientation)
    except Exception:
        orientation = None

    return Fingerprint(kind, points, orientation)


def max_delta(before, after):
    """Largest 3D distance between two fingerprints.

    Returns ``None`` when the fingerprints cannot be compared (different kind
    or different point count) which the caller must treat as a failure.
    """
    if before is None or after is None:
        return None
    if not before.is_valid or not after.is_valid:
        return None
    if before.kind != after.kind:
        return None
    if len(before.points) != len(after.points):
        return None

    worst = 0.0
    for p0, p1 in zip(before.points, after.points):
        try:
            distance = p0.DistanceTo(p1)
        except Exception:
            return None
        if distance > worst:
            worst = distance
    return worst


def horizontal_delta(before, after):
    """Largest planar (XY) displacement between two fingerprints."""
    if before is None or after is None:
        return None
    if len(before.points) != len(after.points):
        return None
    worst = 0.0
    for p0, p1 in zip(before.points, after.points):
        dx = p0.X - p1.X
        dy = p0.Y - p1.Y
        distance = (dx * dx + dy * dy) ** 0.5
        if distance > worst:
            worst = distance
    return worst


def vertical_delta(before, after):
    """Signed Z displacement (before - after); positive means it dropped."""
    if before is None or after is None:
        return None
    z_before = before.z()
    z_after = after.z()
    if z_before is None or z_after is None:
        return None
    return z_before - z_after


def orientation_changed(before, after, tolerance=1e-6):
    """True when facing/hand orientation differs (rotation or mirroring)."""
    if before is None or after is None:
        return False
    if before.orientation is None or after.orientation is None:
        return False
    try:
        for v0, v1 in zip(before.orientation, after.orientation):
            if v0 is None or v1 is None:
                continue
            if v0.DistanceTo(v1) > tolerance:
                return True
    except Exception:
        return False
    return False
