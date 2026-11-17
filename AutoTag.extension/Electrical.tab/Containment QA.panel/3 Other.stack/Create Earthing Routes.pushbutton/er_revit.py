# -*- coding: utf-8 -*-
"""Earthing Route Generator - everything that talks to Revit.

    PlanFrame            the active floor plan as a 2D (u, v) coordinate system
    connection_point     where a panel / the earth bar is connected
    collect_obstacles    obstacle shapes of the host model and of its links
    create / delete      Detail Lines in the active view (the ONLY thing this
                         tool creates), tagged so they can be found again

Routes are 2D: every position is reduced to (u, v) along the view's Right and
Up directions and Z is ignored for routing.
"""
import math

import clr
import System
from System.Collections.Generic import List

from Autodesk.Revit.DB import (Arc, BoundingBoxIntersectsFilter, BuiltInCategory, BuiltInParameter,
                               Color, CurveElement, Domain, ElementId,
                               ElementMulticategoryFilter, FilteredElementCollector, Line,
                               LocationCurve, LocationPoint, Outline, OverrideGraphicSettings,
                               RevitLinkInstance, Transaction, TransactionStatus, ViewType, XYZ)
from Autodesk.Revit.DB.ExtensibleStorage import AccessLevel, Entity, Schema, SchemaBuilder
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

import er_config as config

FT = config.FEET_PER_MM


def trace(text):
    """Replaced by the window with its run-log writer (er_window.trace)."""
    pass
KIND_ROUTE, KIND_PREVIEW = 'route', 'preview'


def id_value(element_id):
    """ElementId -> int (.Value from Revit 2024, .IntegerValue before)."""
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def make_id(value):
    try:
        return ElementId(System.Int64(value))
    except Exception:
        return ElementId(int(value))


def is_floor_plan(view):
    try:
        return view is not None and view.ViewType == ViewType.FloorPlan and not view.IsTemplate
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the plan as 2D coordinates
# ---------------------------------------------------------------------------
class PlanFrame(object):
    """(u, v) = distance along the view's Right and Up directions, turned by `angle` (radians, in the plan).

    angle 0: the lines are horizontal and vertical ON THE DRAWING.
    angle of the earth bar (see route_angle): the lines run square to the bar and to equipment set out with it.
    FIX: in a room turned on the drawing (transformers and earth bar at about 35 degrees) every route had to
    staircase across the room, and every turned transformer became a much larger upright box to route round."""

    def __init__(self, view, angle=0.0):
        self.view = view
        self.angle = float(angle or 0.0)
        right, up = view.RightDirection, view.UpDirection
        if abs(self.angle) < 1e-12:
            self.right, self.up = right, up
        else:
            c, s = math.cos(self.angle), math.sin(self.angle)
            self.right = XYZ(right.X * c + up.X * s, right.Y * c + up.Y * s, right.Z * c + up.Z * s)
            self.up = XYZ(-right.X * s + up.X * c, -right.Y * s + up.Y * c, -right.Z * s + up.Z * c)
        self.normal = view.ViewDirection
        self._z = None

    def uv(self, point):
        return point.DotProduct(self.right), point.DotProduct(self.up)

    def xyz(self, u, v, z):
        return XYZ(self.right.X * u + self.up.X * v, self.right.Y * u + self.up.Y * v, z)

    def elevations(self):
        """Heights at which a detail line may have to be drawn, best first."""
        found = []
        for getter in (lambda: self.view.SketchPlane.GetPlane().Origin.Z,
                       lambda: self.view.GenLevel.ProjectElevation,
                       lambda: self.view.GenLevel.Elevation,
                       lambda: self.view.Origin.Z,
                       lambda: 0.0):
            try:
                value = float(getter())
            except Exception:
                continue
            if not [z for z in found if abs(z - value) < 1e-6]:
                found.append(value)
        return found


# ---------------------------------------------------------------------------
# picking + names + connection points
# ---------------------------------------------------------------------------
class _Filter(ISelectionFilter):
    def __init__(self, allow):
        self._allow = allow

    def AllowElement(self, element):
        try:
            return bool(self._allow(element))
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return False


def _category_id(element):
    try:
        return id_value(element.Category.Id) if element.Category is not None else None
    except Exception:
        return None


def _bic_id(name):
    value = getattr(BuiltInCategory, name, None)
    return None if value is None else id_value(ElementId(value))


PANEL_CATEGORY = _bic_id('OST_ElectricalEquipment')


def is_panel(element):
    return _category_id(element) == PANEL_CATEGORY


def _has_position(element):
    try:
        if element.Category is None or element.ViewSpecific:
            return False
        return element.get_BoundingBox(None) is not None
    except Exception:
        return False


def pick_earth_bar(uidoc):
    """One element of any model category. Returns the element or None (Esc)."""
    try:
        reference = uidoc.Selection.PickObject(ObjectType.Element, _Filter(_has_position),
                                               'Select the EARTH BAR (Esc to cancel)')
    except Exception:
        return None
    return uidoc.Document.GetElement(reference.ElementId)


def pick_panels(uidoc):
    """Electrical equipment, as many as wanted. Returns elements or None (Esc)."""
    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element, _Filter(is_panel),
            'Select the ELECTRICAL PANELS, then click Finish (Esc to cancel)')
    except Exception:
        return None
    doc = uidoc.Document
    return [doc.GetElement(reference.ElementId) for reference in references]


def element_label(element):
    """Panel name, else Mark, else the element's name."""
    for name in ('RBS_ELEC_PANEL_NAME', 'ALL_MODEL_MARK'):
        bip = getattr(BuiltInParameter, name, None)
        if bip is None:
            continue
        try:
            parameter = element.get_Parameter(bip)
            text = (parameter.AsString() or '').strip() if parameter is not None else ''
            if text:
                return text
        except Exception:
            pass
    try:
        return u'{0} (id {1})'.format(element.Name or 'Element', id_value(element.Id))
    except Exception:
        return u'id {0}'.format(id_value(element.Id))


def connection_point(element):
    """Where the earthing line meets the element: an electrical connector,
    else the location point, else the centre of the bounding box."""
    try:
        connectors = element.MEPModel.ConnectorManager.Connectors
        fallback = None
        for connector in connectors:
            try:
                if connector.Domain == Domain.DomainElectrical:
                    return connector.Origin
                fallback = fallback or connector.Origin
            except Exception:
                continue
        if fallback is not None:
            return fallback
    except Exception:
        pass
    try:
        location = element.Location
        if isinstance(location, LocationPoint):
            return location.Point
    except Exception:
        pass
    box = element.get_BoundingBox(None)
    if box is None:
        raise ValueError('the element has no position')
    return (box.Min + box.Max) * 0.5


def _solid_points(element, limit=20000):
    """Corner points of the element's solids (for the outline of a turned panel)."""
    from Autodesk.Revit.DB import GeometryInstance, Options, Solid
    points = []

    def walk(geometry):
        for item in geometry:
            if len(points) >= limit:
                return
            if isinstance(item, GeometryInstance):
                walk(item.GetInstanceGeometry())
            elif isinstance(item, Solid):
                try:
                    if item.Faces.Size == 0 or item.Volume <= 0:
                        continue
                    for edge in item.Edges:
                        points.extend(edge.Tessellate())
                except Exception:
                    continue
    geometry = element.get_Geometry(Options())
    if geometry is not None:
        walk(geometry)
    return points


def plan_box(element, frame):
    """The outline of an element in the plan, as a rectangle that may be
    turned: (centre u, centre v, axis du, axis dv, half length, half width).

    A panel drawn square to the view gets its bounding box. A panel placed at
    an angle gets a box turned with it (from its solids), so a line can stop
    on its real edge instead of on a much larger upright box.
    None when the element has no extents."""
    box = element.get_BoundingBox(None)
    if box is None:
        return None
    u0, v0, u1, v1 = _box_uv(box, frame, None)
    upright = ((u0 + u1) / 2.0, (v0 + v1) / 2.0, 1.0, 0.0, (u1 - u0) / 2.0, (v1 - v0) / 2.0)
    try:
        hand = element.HandOrientation
        du, dv = hand.DotProduct(frame.right), hand.DotProduct(frame.up)
        size = (du * du + dv * dv) ** 0.5
        if size < 1e-6:
            return upright
        du, dv = du / size, dv / size
        turned_frame = abs(getattr(frame, 'angle', 0.0)) > 1e-9
        if (abs(du) > 0.9999 or abs(dv) > 0.9999) and not turned_frame:
            return upright                           # square to the view already
        # FIX: in a frame turned with the room (-45 degrees) the bounding box - square to the MODEL - was boxed
        # again in the frame: every 2.6 x 2.0 m transformer became a 4.6 m square, the four of them filled
        # the room from wall to wall and the lines had to go round the outside. Now the real outline.
        along, across = [], []
        for point in _solid_points(element):
            u, v = frame.uv(point)
            along.append(u * du + v * dv)
            across.append(-u * dv + v * du)
        if len(along) < 4:
            return _world_box(box, frame, None) if turned_frame else upright
        a, b = (min(along) + max(along)) / 2.0, (min(across) + max(across)) / 2.0
        return (a * du - b * dv, a * dv + b * du, du, dv,
                (max(along) - min(along)) / 2.0, (max(across) - min(across)) / 2.0)
    except Exception:
        return upright


def _world_box(box, frame, transform):
    """A bounding box (square to the model / link) as a box turned in the routing frame:
    (centre u, centre v, du, dv, half length, half width) - not the larger upright box round it."""
    centre = (box.Min + box.Max) * 0.5
    axis = XYZ(1, 0, 0)
    if transform is not None:
        centre, axis = transform.OfPoint(centre), transform.OfVector(axis)
    cu, cv = frame.uv(centre)
    du, dv = axis.DotProduct(frame.right), axis.DotProduct(frame.up)
    size = (du * du + dv * dv) ** 0.5
    if size < 1e-6:
        du, dv = 1.0, 0.0
    else:
        du, dv = du / size, dv / size
    return (cu, cv, du, dv, (box.Max.X - box.Min.X) / 2.0, (box.Max.Y - box.Min.Y) / 2.0)


def _turned_shape(outline):
    """('rect', ...) when the outline is square to the routing frame, else ('box', ...)."""
    cu, cv, du, dv, a, b = outline
    if abs(du) > 0.9999 or abs(dv) > 0.9999:
        if abs(dv) > abs(du):
            a, b = b, a
        return ('rect', cu - a, cv - b, cu + a, cv + b)
    return ('box', cu, cv, du, dv, a, b)


def _instance_outline(element, frame, transform):
    """The real outline of a turned family instance (from its solids, along its hand direction), or None."""
    try:
        hand = element.HandOrientation
        if transform is not None:
            hand = transform.OfVector(hand)
        du, dv = hand.DotProduct(frame.right), hand.DotProduct(frame.up)
        size = (du * du + dv * dv) ** 0.5
        if size < 1e-6:
            return None
        du, dv = du / size, dv / size
        along, across = [], []
        for point in _solid_points(element, 4000):
            if transform is not None:
                point = transform.OfPoint(point)
            u, v = frame.uv(point)
            along.append(u * du + v * dv)
            across.append(-u * dv + v * du)
        if len(along) < 4:
            return None
        a, b = (min(along) + max(along)) / 2.0, (min(across) + max(across)) / 2.0
        return (a * du - b * dv, a * dv + b * du, du, dv,
                (max(along) - min(along)) / 2.0, (max(across) - min(across)) / 2.0)
    except Exception:
        return None


ANGLE_MIN = math.radians(0.5)             # an earth bar turned less than this counts as square to the drawing


def _fold(angle):
    """The nearest square direction: -45 .. 45 degrees; 0 below ANGLE_MIN."""
    angle = (angle + math.pi / 4.0) % (math.pi / 2.0) - math.pi / 4.0
    if angle > math.pi / 4.0 - 1e-6:
        angle -= math.pi / 2.0                     # +45 and -45 degrees are the same square directions
    return 0.0 if abs(angle) < ANGLE_MIN else angle


def element_angles(element, view):
    """{source: angle} - every way the element tells how it is turned on the drawing (radians, folded to
    -45..45 degrees): its hand and facing directions, the rotation of its location, its turned outline."""
    found = {}
    if element is None:
        return found
    right, up = view.RightDirection, view.UpDirection
    for name in ('HandOrientation', 'FacingOrientation'):
        try:
            vector = getattr(element, name)
            du, dv = vector.DotProduct(right), vector.DotProduct(up)
            if du * du + dv * dv > 1e-6:
                found[name] = _fold(math.atan2(dv, du))
        except Exception:
            pass
    try:
        rotation = float(element.Location.Rotation)
        found['Location.Rotation'] = _fold(rotation - math.atan2(right.Y, right.X))
    except Exception:
        pass
    try:
        box = plan_box(element, PlanFrame(view))
        if box is not None and (abs(box[2]) < 0.9999 and abs(box[3]) < 0.9999):
            found['outline'] = _fold(math.atan2(box[3], box[2]))
    except Exception:
        pass
    return found


def route_angle(element, view):
    """The angle (radians, -45..45 degrees) by which the element (the earth bar) is turned on the drawing:
    the routes are laid out square to it. 0 when it is square to the drawing or has no direction.
    FIX: only the hand direction was read; for an earth bar placed with a rotation that does not show there
    (or without solids for its outline) the angle came out 0 and the routes stayed square to the drawing."""
    # FIX: the values were taken in dictionary order - an earth bar whose outline / hand read 33.7 degrees but
    # whose location rotation read 23.7 gave 23.7. The outline (what is drawn) first, then the hand direction.
    found = element_angles(element, view)
    for name in ('outline', 'HandOrientation', 'FacingOrientation', 'Location.Rotation'):
        if found.get(name):
            return found[name]
    return 0.0


def common_angle(elements, view):
    """The angle that most of the elements are turned by (the panels / transformers of a turned room), when at
    least half of them share it; else 0."""
    # FIX: only turned elements were counted - of two earth bars, one square to the drawing and one turned
    # 33.7 degrees, the turned one won 1 : 0 and every loop / pit route was laid out at 33.7 degrees. Square
    # elements now count too, and an angle needs MORE than half of the elements (a tie stays square).
    elements = list(elements)
    counts = {}
    for element in elements:
        angle = route_angle(element, view)
        key = round(math.degrees(angle) * 2.0) / 2.0                   # to half a degree
        counts.setdefault(key, []).append(angle)
    if not counts:
        return 0.0
    key = max(counts, key=lambda k: (len(counts[k]), k == 0.0))
    if key == 0.0 or len(counts[key]) * 2 <= len(elements):
        return 0.0
    return sum(counts[key]) / len(counts[key])


# ---------------------------------------------------------------------------
# obstacles
# ---------------------------------------------------------------------------
def _box_uv(box, frame, transform):
    """(u0, v0, u1, v1) of a bounding box seen in the plan."""
    us, vs = [], []
    for x in (box.Min.X, box.Max.X):
        for y in (box.Min.Y, box.Max.Y):
            for z in (box.Min.Z, box.Max.Z):
                point = XYZ(x, y, z)
                if transform is not None:
                    point = transform.OfPoint(point)
                u, v = frame.uv(point)
                us.append(u)
                vs.append(v)
    return min(us), min(vs), max(us), max(vs)


def _width_of(element):
    """Plan width (feet) of a wall / duct / pipe / tray / conduit, or None."""
    for getter in (lambda: element.Width,
                   lambda: element.get_Parameter(BuiltInParameter.RBS_PIPE_OUTER_DIAMETER).AsDouble(),
                   lambda: element.get_Parameter(BuiltInParameter.RBS_CONDUIT_OUTER_DIAM_PARAM).AsDouble(),
                   lambda: element.Diameter):
        try:
            value = float(getter())
            if value > 0:
                return value
        except Exception:
            continue
    return None


def shape_of(element, frame, transform=None):
    """The plan shape of an obstacle.

    A straight wall / duct / pipe / tray / conduit / beam is a centre line
    with a width, so a diagonal run is not turned into a huge square.
    Everything else is the rectangle of its bounding box."""
    box = element.get_BoundingBox(None)
    if box is None:
        return None
    u0, v0, u1, v1 = _box_uv(box, frame, transform)
    try:
        location = element.Location
        curve = location.Curve if isinstance(location, LocationCurve) else None
    except Exception:
        curve = None
    if curve is not None and isinstance(curve, Line) and curve.IsBound:
        a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
        if transform is not None:
            a, b = transform.OfPoint(a), transform.OfPoint(b)
        au, av = frame.uv(a)
        bu, bv = frame.uv(b)
        du, dv = abs(bu - au), abs(bv - av)
        length = (du * du + dv * dv) ** 0.5
        if length > 0.05:                             # not a riser seen from above
            width = _width_of(element)
            if width is None:
                # from the bounding box: a box L x W turned by the run's angle
                # measures L|du| + W|dv| by L|dv| + W|du|
                cu, cv = du / length, dv / length
                width = ((u1 - u0) - length * cu) / cv if cv >= cu else ((v1 - v0) - length * cv) / cu
                if width <= 0 or width > max(u1 - u0, v1 - v0):
                    width = None
            if width is not None:
                return ('seg', au, av, bu, bv, width / 2.0)
    # FIX: everything else was the upright box round its bounding box. In a routing frame turned with the room
    # an element square to the drawing (switchboards beside the TX room) became a box 1.4 times as large that
    # reached through the wall into the room. Now its outline turned as it really stands.
    turned_frame = abs(getattr(frame, 'angle', 0.0)) > 1e-9
    outline = None
    try:
        hand = element.HandOrientation
        hu, hv = hand.X, hand.Y
        turned_element = not (abs(hu) > 0.9999 or abs(hv) > 0.9999)
    except Exception:
        turned_element = False
    if turned_element or turned_frame:
        if turned_element:
            outline = _instance_outline(element, frame, transform)
        if outline is None and turned_frame:
            outline = _world_box(box, frame, transform)
    if outline is not None:
        return _turned_shape(outline)
    return ('rect', u0, v0, u1, v1)


def _door_opening(door, frame, transform):
    """(cu, cv, du, dv, half_width, wall_half_thickness) of a door, or None."""
    try:
        wall = door.Host
        location = door.Location
        if wall is None or not isinstance(location, LocationPoint):
            return None
        curve = wall.Location.Curve
        if not isinstance(curve, Line):
            return None
        width = None
        for source in (door, door.Symbol):
            for name in ('DOOR_WIDTH', 'FAMILY_WIDTH_PARAM', 'GENERIC_WIDTH'):
                bip = getattr(BuiltInParameter, name, None)
                parameter = source.get_Parameter(bip) if bip is not None else None
                if parameter is not None and parameter.HasValue and parameter.AsDouble() > 0:
                    width = parameter.AsDouble()
                    break
            if width:
                break
        if not width:
            return None
        point, direction = location.Point, curve.Direction
        if transform is not None:
            point, direction = transform.OfPoint(point), transform.OfVector(direction)
        cu, cv = frame.uv(point)
        du, dv = direction.DotProduct(frame.right), direction.DotProduct(frame.up)
        size = (du * du + dv * dv) ** 0.5
        if size < 1e-6:
            return None
        return (cu, cv, du / size, dv / size, width / 2.0, float(wall.Width) / 2.0)
    except Exception:
        return None


def _plan_heights(view):
    """(bottom, top) heights of what the plan shows, for links in Revit
    versions that cannot ask "what does this view show of that link"."""
    try:
        from Autodesk.Revit.DB import PlanViewPlane
        view_range = view.GetViewRange()
        doc = view.Document
        heights = []
        for plane in (PlanViewPlane.ViewDepthPlane, PlanViewPlane.BottomClipPlane,
                      PlanViewPlane.CutPlane, PlanViewPlane.TopClipPlane):
            level = doc.GetElement(view_range.GetLevelId(plane))
            if level is not None:
                heights.append(level.ProjectElevation + view_range.GetOffset(plane))
        if len(heights) >= 2:
            return min(heights), max(heights)
    except Exception:
        pass
    try:
        base = view.GenLevel.ProjectElevation
    except Exception:
        base = 0.0
    return base - 1.0, base + 10.0


def _outline(frame, region, low, high, transform=None):
    """A model-space box round the (u, v) region, optionally in a link's own
    coordinates."""
    xs, ys, zs = [], [], []
    for u in (region[0], region[2]):
        for v in (region[1], region[3]):
            for z in (low, high):
                point = frame.xyz(u, v, z)
                if transform is not None:
                    point = transform.Inverse.OfPoint(point)
                xs.append(point.X)
                ys.append(point.Y)
                zs.append(point.Z)
    return Outline(XYZ(min(xs), min(ys), min(zs)), XYZ(max(xs), max(ys), max(zs)))


def _category_filter(names):
    categories = List[BuiltInCategory]()
    for name in names:
        value = getattr(BuiltInCategory, name, None)
        if value is not None:
            categories.Add(value)
    return ElementMulticategoryFilter(categories) if categories.Count else None


class Obstacles(object):
    def __init__(self):
        self.walls = []        # [(key, shape)]
        self.others = []       # [(key, shape)]   key = (link id or 0, element id)
        self.doors = []        # door openings
        self.counts = {}       # where they came from -> how many
        self.notes = []        # things the user should know

    def count(self, source, number):
        if number:
            self.counts[source] = self.counts.get(source, 0) + number


def room_walls(doc, view, frame, points):
    """The boundary of the room(s) the given plan points (earth bar, panels) stand in - of this model or of a
    linked model - as wall pieces [(key, ('seg', u0, v0, u1, v1, half width))], and the room names.
    FIX: the walls of the room were in the linked architectural model ("Include linked models" was off), the
    router saw no wall at all and ran the lines out of the room, through its walls and along the corridor.
    With the room's boundary as a wall the lines stay inside the room."""
    shapes, names, seen = [], [], set()
    try:
        z = frame.elevations()[0] + 1.0                    # a foot above the floor: inside the room's volume
    except Exception:
        z = 1.0
    # FIX: this model was asked first - an MEP model with its own (larger) room "TX ROOM" kept the lines inside
    # that, not inside the architect's room. The rooms of the LINKED models come first; this model's only when
    # no link has a room there.
    sources = []
    try:
        for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                link_doc = link.GetLinkDocument()
                if link_doc is not None:
                    sources.append((link_doc, link.GetTotalTransform(), id_value(link.Id)))
            except Exception:
                pass
    except Exception:
        pass
    sources.append((doc, None, 0))
    try:
        from Autodesk.Revit.DB import SpatialElementBoundaryOptions
        options = SpatialElementBoundaryOptions()
    except Exception:
        options = None
    cu = sum(p[0] for p in points) / float(len(points)) if points else 0.0
    cv = sum(p[1] for p in points) / float(len(points)) if points else 0.0

    def room_at(u, v):
        """(room, transform, link key) at the plan point - also when the point lies just inside a wall (an earth
        bar on a wall face): then a little further in, towards the middle of all points."""
        length = ((cu - u) ** 2 + (cv - v) ** 2) ** 0.5
        for step in (0.0, 0.5, 1.0, 2.0):                 # feet towards the middle
            if step and length < 1e-6:
                break
            pu, pv = (u + (cu - u) / length * step, v + (cv - v) / length * step) if step else (u, v)
            point = frame.xyz(pu, pv, z)
            for source_doc, transform, link_key in sources:
                try:
                    local = transform.Inverse.OfPoint(point) if transform is not None else point
                    room = source_doc.GetRoomAtPoint(local)
                except Exception:
                    room = None
                if room is not None:
                    return room, transform, link_key
        return None, None, None

    found = []
    roomless = 0
    for u, v in points:
        room, transform, link_key = room_at(u, v)
        if room is None:
            roomless += 1
        elif (link_key, id_value(room.Id)) not in [(k, id_value(r.Id)) for r, t, k in found]:
            found.append((room, transform, link_key))
    if roomless and found:
        # FIX: an end in no room at all (an earth pit outside the building, a point in a shaft) and the
        # other ends in one room: that room's boundary was forced on the run and the line could not get out
        return [], [u'{0}'.format(_room_name(r)) for r, t, k in found] + [u'no room']
    if len(found) != 1:
        # no room, or the earth bar and the panels stand in different rooms: no single room to keep the lines in
        return [], [u'{0}'.format(_room_name(r)) for r, t, k in found]
    for room, transform, link_key in found:
        key = (link_key, id_value(room.Id))
        if True:
            seen.add(key)
            source = u'this model'
            for source_doc, t_, k_ in sources:
                if k_ == link_key and k_:
                    try:
                        source = u'link "{0}"'.format(source_doc.Title)
                    except Exception:
                        source = u'a linked model'
            names.append(u'{0} ({1})'.format(_room_name(room), source))
            try:
                loops = room.GetBoundarySegments(options)
            except Exception:
                loops = []
            for loop in loops or []:
                # FIX: the pieces were drawn one by one and left gaps between them (up to 800 mm, at columns
                # and wall ends) - a line could slip out of the room there. Now one closed outline.
                uvs = []
                for piece in loop:
                    try:
                        curve = piece.GetCurve()
                        pts = list(curve.Tessellate())
                    except Exception:
                        continue
                    if transform is not None:
                        pts = [transform.OfPoint(p) for p in pts]
                    uvs.extend(frame.uv(p) for p in pts)
                if len(uvs) > 2:
                    uvs.append(uvs[0])
                for (au, av), (bu, bv) in zip(uvs, uvs[1:]):
                    if abs(bu - au) + abs(bv - av) > 1e-6:
                        shapes.append(((link_key, -1), ('seg', au, av, bu, bv, 0.01)))
    return shapes, names


def room_angle(doc, view, points, radius=None):
    """The angle (radians, -45..45 degrees) the room's walls run at, when all the given plan points (XYZ:
    earth bars, panels) stand in ONE room: the direction most of its boundary length runs in. 0 when there is
    no such room or its walls are square to the drawing.
    FIX: a loop between an earth bar square to the drawing and one turned 33.7 degrees - both in the TX room,
    whose walls run at 45 degrees - was laid out square to the drawing (the bars tie) and staircased across
    the room. The room's architecture now decides."""
    # radius (feet): the walls of the room AROUND each point, each its own room, nearer walls counting more.
    # FIX: earth bars in a TX room and in a corridor, with the exit point in the corridor - all of it at
    # 45 degrees - were laid out square to the drawing: the points stand in different rooms, so no one room
    # decided, and a corridor that bends counted as square by its total length.
    frame = PlanFrame(view)
    pieces = []
    try:
        if radius is None:
            shapes, _names = room_walls(doc, view, frame, [frame.uv(p) for p in points])
            pieces = [(shape, None) for _key, shape in shapes]
        else:
            for p in points:
                here = frame.uv(p)
                shapes, _names = room_walls(doc, view, frame, [here])
                pieces.extend((shape, here) for _key, shape in shapes)
    except Exception:
        return 0.0
    totals = {}
    for shape, here in pieces:
        du, dv = shape[3] - shape[1], shape[4] - shape[2]
        length = (du * du + dv * dv) ** 0.5
        if length < 1e-6:
            continue
        if here is not None:
            t = max(0.0, min(1.0, ((here[0] - shape[1]) * du + (here[1] - shape[2]) * dv) / (length * length)))
            distance = ((here[0] - shape[1] - t * du) ** 2 + (here[1] - shape[2] - t * dv) ** 2) ** 0.5
            if distance >= radius:
                continue
            length *= 1.0 - distance / radius
        angle = _fold(math.atan2(dv, du))
        key = round(math.degrees(angle) * 2.0) / 2.0
        if key == -45.0:
            key = 45.0                                   # the same square directions
        totals.setdefault(key, [0.0, 0.0])
        totals[key][0] += length
        totals[key][1] += angle * length
    if not totals:
        return 0.0
    key = max(totals, key=lambda k: totals[k][0])
    if key == 0.0:
        return 0.0
    return _fold(math.radians(key))


def _room_name(room):
    for getter in (lambda: room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString(), lambda: room.Name):
        try:
            name = getter()
        except Exception:
            name = None
        if name:
            return u'{0}'.format(name)
    return u'room'


def collect_obstacles(doc, view, frame, category_names, region, include_links, through_doors):
    """Obstacle shapes inside `region` (u0, v0, u1, v1).

    Host model: what the active plan shows of the ticked categories, so the
    view range and the view's visibility settings decide which floor counts.
    Links: the same for each loaded link (Revit 2024+); on older versions the
    link's elements between the heights of the plan's view range."""
    result = Obstacles()
    wall_ids = set(_bic_id(name) for name in config.WALL_CATEGORIES)
    wanted = _category_filter(category_names)
    if wanted is None:
        return result
    walls_on = bool(wall_ids & set(_bic_id(name) for name in category_names))
    door_filter = _category_filter(config.DOOR_CATEGORIES) if (walls_on and through_doors) else None
    low, high = _plan_heights(view)
    everything = 1.0e5

    def take(elements, link_key, transform, source):
        number = 0
        for element in elements:
            try:
                shape = shape_of(element, frame, transform)
            except Exception:
                shape = None
            if shape is None:
                continue
            key = (link_key, id_value(element.Id))
            (result.walls if _category_id(element) in wall_ids else result.others).append((key, shape))
            number += 1
        result.count(source, number)

    def take_doors(elements, transform):
        for door in elements:
            opening = _door_opening(door, frame, transform)
            if opening is not None:
                result.doors.append(opening)

    # ---- host model
    # Every Revit call below is followed by a line in the run log, so a stop
    # inside Revit can be traced to the exact step.
    trace('host: collecting')
    host_box = BoundingBoxIntersectsFilter(_outline(frame, region, -everything, everything))
    host_elements = list(FilteredElementCollector(doc, view.Id).WherePasses(wanted).WherePasses(host_box)
                         .WhereElementIsNotElementType())
    trace('host: {0} element(s), reading shapes'.format(len(host_elements)))
    take(host_elements, 0, None, 'this model')
    if door_filter is not None:
        trace('host: doors')
        take_doors(list(FilteredElementCollector(doc, view.Id).WherePasses(door_filter).WherePasses(host_box)
                        .WhereElementIsNotElementType()), None)
    trace('host: done')

    # ---- links
    if include_links:
        for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
            try:
                link_doc = link.GetLinkDocument()
            except Exception:
                link_doc = None
            if link_doc is None:
                continue                              # not loaded
            name = link_doc.Title
            trace('link "{0}": collecting'.format(name.encode('ascii', 'replace')))
            transform = link.GetTotalTransform()
            link_key = id_value(link.Id)
            elements = None
            if config.LINK_VIEW_QUERY:
                try:
                    # Revit 2024+: the elements of this link that the plan shows
                    elements = list(FilteredElementCollector(doc, view.Id, link.Id).WherePasses(wanted)
                                    .WherePasses(BoundingBoxIntersectsFilter(
                                        _outline(frame, region, -everything, everything, transform)))
                                    .WhereElementIsNotElementType())
                    doors = (list(FilteredElementCollector(doc, view.Id, link.Id).WherePasses(door_filter)
                                  .WhereElementIsNotElementType()) if door_filter is not None else [])
                except Exception:
                    elements = None
            if elements is None:
                # the link's own elements inside the region, between the
                # heights the plan shows
                box = BoundingBoxIntersectsFilter(_outline(frame, region, low, high, transform))
                elements = list(FilteredElementCollector(link_doc).WherePasses(wanted).WherePasses(box)
                                .WhereElementIsNotElementType())
                doors = (list(FilteredElementCollector(link_doc).WherePasses(door_filter).WherePasses(box)
                              .WhereElementIsNotElementType()) if door_filter is not None else [])
            trace('link: {0} element(s), {1} door(s), reading shapes'.format(len(elements), len(doors)))
            take(elements, link_key, transform, u'link "{0}"'.format(name))
            take_doors(doors, transform)
            trace('link: done')
    return result


# ---------------------------------------------------------------------------
# line styles
# ---------------------------------------------------------------------------
def line_style_names(doc):
    """Names of the line styles a detail line can take, 'earth' ones first."""
    names = []
    try:
        lines = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        for sub in lines.SubCategories:
            if sub.Name and sub.Name not in config.LINE_STYLES_EXCLUDED:
                names.append(sub.Name)
    except Exception:
        pass

    def rank(name):
        lower = name.lower()
        hinted = [index for index, hint in enumerate(config.LINE_STYLE_HINTS) if hint in lower]
        return (hinted[0] if hinted else len(config.LINE_STYLE_HINTS), name.startswith('<'), lower)
    return sorted(set(names), key=rank)


def _style_for(doc, curve, name):
    for style_id in curve.GetLineStyleIds():
        style = doc.GetElement(style_id)
        if style is not None and style.Name == name:
            return style
    return None


# ---------------------------------------------------------------------------
# tagging the lines (so a route can be recognised, replaced and cleaned up)
# ---------------------------------------------------------------------------
_SCHEMA_GUID = System.Guid('5f0c2b6e-7d1a-4c53-9a1e-3b8e6f2a9c41')
_FIELDS = ('Panel', 'Bar', 'Kind')


def _schema():
    schema = Schema.Lookup(_SCHEMA_GUID)
    if schema is not None:
        return schema
    builder = SchemaBuilder(_SCHEMA_GUID)
    builder.SetReadAccessLevel(AccessLevel.Public)
    builder.SetWriteAccessLevel(AccessLevel.Public)
    builder.SetSchemaName('EarthingRouteGenerator')
    builder.SetDocumentation('Detail lines drawn by the Earthing Route Generator')
    for name in _FIELDS:
        builder.AddSimpleField(name, clr.GetClrType(System.String))
    return builder.Finish()


def _tag(curve, panel_id, bar_id, kind):
    schema = _schema()
    entity = Entity(schema)
    for name, value in zip(_FIELDS, (str(panel_id), str(bar_id), kind)):
        entity.Set[System.String](schema.GetField(name), value)
    curve.SetEntity(entity)


def tagged_lines(doc, view):
    """{(panel id, bar id, kind): [line ElementId, ...]} of the lines this
    tool drew in the view."""
    found = {}
    schema = Schema.Lookup(_SCHEMA_GUID)
    if schema is None:
        return found
    for curve in FilteredElementCollector(doc, view.Id).OfClass(CurveElement):
        try:
            entity = curve.GetEntity(schema)
            if entity is None or not entity.IsValid():
                continue
            values = [entity.Get[System.String](schema.GetField(name)) for name in _FIELDS]
            key = (int(values[0]), int(values[1]), values[2])
        except Exception:
            continue
        found.setdefault(key, []).append(curve.Id)
    return found


def pick_elements(uidoc, prompt):
    """Any model elements, as many as wanted, in the order they are picked.
    Returns elements or None (Esc)."""
    try:
        references = uidoc.Selection.PickObjects(ObjectType.Element, _Filter(_has_position), prompt)
    except Exception:
        return None
    doc = uidoc.Document
    return [doc.GetElement(reference.ElementId) for reference in references]


def pick_element(uidoc, prompt):
    try:
        reference = uidoc.Selection.PickObject(ObjectType.Element, _Filter(_has_position), prompt)
    except Exception:
        return None
    return uidoc.Document.GetElement(reference.ElementId)


def pick_point(uidoc, prompt):
    """A point clicked in the plan. Returns XYZ or None (Esc)."""
    try:
        return uidoc.Selection.PickPoint(prompt)
    except Exception:
        return None


def view_scale(view):
    try:
        return max(1, int(view.Scale))
    except Exception:
        return 100


def _curve_segments(curve, frame):
    """The straight pieces a drawn curve stands for: a line itself; a bend (arc) the two legs of the
    square corner it rounds off (so lines drawn with bends are still seen as horizontal / vertical runs)."""
    a, b = frame.uv(curve.GetEndPoint(0)), frame.uv(curve.GetEndPoint(1))
    if isinstance(curve, Arc):
        centre = frame.uv(curve.Center)
        corner = (a[0] + b[0] - centre[0], a[1] + b[1] - centre[1])     # 90 degree bend: the square corner
        return [(a, corner), (corner, b)]
    return [(a, b)]


def all_route_segments(doc, view, frame, skip_pairs):
    """[((u, v), (u, v)), ...] of every earthing line this tool drew in the
    view, except the connections in skip_pairs {(from id, to id)} (they are
    being drawn again). Only horizontal / vertical pieces: break marks are not
    lines to cross."""
    segments = []
    for key, ids in tagged_lines(doc, view).items():
        if key[2] != KIND_ROUTE or (key[0], key[1]) in skip_pairs:
            continue
        for element_id in ids:
            try:
                pieces = _curve_segments(doc.GetElement(element_id).GeometryCurve, frame)
            except Exception:
                continue
            for a, b in pieces:
                if abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6:
                    segments.append((a, b))
    return segments


def route_segments(doc, view, frame, bar_id, skip_panels):
    """[((u, v), (u, v)), ...] of the earthing lines that already run to this
    earth bar from panels that are NOT being routed now - new lines may tee
    into them."""
    segments = []
    for key, ids in tagged_lines(doc, view).items():
        if key[2] != KIND_ROUTE or key[1] != bar_id or key[0] in skip_panels:
            continue
        for element_id in ids:
            try:
                pieces = _curve_segments(doc.GetElement(element_id).GeometryCurve, frame)
            except Exception:
                continue
            for a, b in pieces:
                if abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6:  # horizontal / vertical only
                    segments.append((a, b))
    return segments


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------
class DrawError(Exception):
    pass


def draw_route(doc, view, frame, points, panel_id, bar_id, kind, style_name=None, bend_radius=0.0):
    """Detail lines for one route, inside an open transaction. Returns their ids.

    kind = KIND_PREVIEW: drawn in the preview colour (a view override);
    kind = KIND_ROUTE:   given the chosen line style.
    bend_radius (feet): every square corner is drawn as a detail ARC of this radius (a bend of the tape);
    0 = sharp corners."""
    import er_geometry
    ids = []
    override = None
    if kind == KIND_PREVIEW:
        override = OverrideGraphicSettings()
        colour = Color(*config.PREVIEW_COLOUR)
        override.SetProjectionLineColor(colour)
        override.SetProjectionLineWeight(config.PREVIEW_WEIGHT)
    for piece in er_geometry.bend_pieces(points, bend_radius or 0.0):
        a, b = piece[1], piece[-1]
        if abs(a[0] - b[0]) + abs(a[1] - b[1]) < 0.01:      # shorter than Revit allows
            continue
        curve = None
        problem = None
        heights = [frame._z] if frame._z is not None else frame.elevations()
        for z in heights:
            try:
                if piece[0] == 'arc':
                    middle = piece[2]
                    geometry_curve = Arc.Create(frame.xyz(a[0], a[1], z), frame.xyz(b[0], b[1], z),
                                                frame.xyz(middle[0], middle[1], z))
                else:
                    geometry_curve = Line.CreateBound(frame.xyz(a[0], a[1], z), frame.xyz(b[0], b[1], z))
                curve = doc.Create.NewDetailCurve(view, geometry_curve)
                frame._z = z                         # the height this view accepts
                break
            except Exception as error:
                problem = error
        if curve is None:
            raise DrawError(u'Revit could not draw a detail line in this view ({0})'.format(problem))
        if kind == KIND_ROUTE and style_name:
            style = _style_for(doc, curve, style_name)
            if style is None:
                raise DrawError(u'Line style "{0}" cannot be used for detail lines in this view'.format(style_name))
            curve.LineStyle = style
        if override is not None:
            view.SetElementOverrides(curve.Id, override)
        _tag(curve, panel_id, bar_id, kind)
        ids.append(curve.Id)
    return ids


def delete_ids(doc, ids):
    count = 0
    for element_id in ids:
        try:
            if doc.GetElement(element_id) is not None:
                doc.Delete(element_id)
                count += 1
        except Exception:
            pass
    return count


def run_transaction(doc, name, action):
    """action() inside one transaction; rolled back when it raises."""
    transaction = Transaction(doc, name)
    transaction.Start()
    try:
        value = action()
        # Commit() does not raise when Revit rolls the change back - it says so
        if transaction.Commit() != TransactionStatus.Committed:
            raise DrawError('Revit rolled the change back; nothing was drawn or deleted')
        return value
    except Exception:
        if transaction.HasStarted() and not transaction.HasEnded():
            transaction.RollBack()
        raise
