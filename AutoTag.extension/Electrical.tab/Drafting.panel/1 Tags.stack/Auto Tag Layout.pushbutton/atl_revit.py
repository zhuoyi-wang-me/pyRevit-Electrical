# -*- coding: utf-8 -*-
"""Auto Tag Layout - everything that touches the Revit model.

find_groups() and tag_types() only read. create_tags() is the only function that
changes the model: it creates tags in the active view, in one transaction.
"""
import io
import math
import os

from Autodesk.Revit import DB

import atl_layout

MM = 1.0 / 304.8                          # one millimetre in feet
MULTI = 'OST_MultiCategoryTags'
# where the tags of a category go
SIDES, NEAR, IN_ROOM, NEARBY = 'sides', 'near', 'room', 'nearby'
MODES = [(SIDES, u'At the sides, with a leader'), (NEAR, u'Beside the element'),
         (NEARBY, u'Nearby free space, short leader (big views)')]
NEARBY_REACH_MM = 60.0          # the longest leader (on the sheet) before a tag goes to the side columns instead
ROOMS = 'OST_Rooms'
NEAR_BY_DEFAULT = ('OST_LightingFixtures', 'OST_LightingDevices')     # lighting tags belong beside their fixture
END_ON = 0.05                             # a tray shorter than this in the view (feet) is seen end-on


# ---------------------------------------------------------------------------
# the view as a flat sheet: u to the right, v upwards
# ---------------------------------------------------------------------------
class Frame(object):
    def __init__(self, view):
        self.o, self.r, self.up = view.Origin, view.RightDirection, view.UpDirection
        self.right = (self.r.X, self.r.Y, self.r.Z)        # the directions of the view in the model,
        self.upward = (self.up.X, self.up.Y, self.up.Z)    # for the size of the tag texts
        self.is_plan = abs(self.up.Z) < 0.5 and abs(self.r.Z) < 0.5      # looked at from above (or below)

    def uv(self, p):
        dx, dy, dz = p.X - self.o.X, p.Y - self.o.Y, p.Z - self.o.Z
        return (dx * self.r.X + dy * self.r.Y + dz * self.r.Z, dx * self.up.X + dy * self.up.Y + dz * self.up.Z)

    def xyz(self, u, v):
        return DB.XYZ(self.o.X + u * self.r.X + v * self.up.X, self.o.Y + u * self.r.Y + v * self.up.Y,
                      self.o.Z + u * self.r.Z + v * self.up.Z)


def view_problem(view):
    """Why tags cannot be placed in this view, or None."""
    if isinstance(view, DB.View3D):
        return u'Open a plan, section or detail view. Tags are not placed in 3D views.'
    if isinstance(view, (DB.ViewSheet, DB.ViewSchedule)) or view.IsTemplate:
        return u'Open the enlarged layout view itself (not a sheet, schedule or template).'
    return None


def crop_extent(view, frame):
    """(umin, vmin, umax, vmax) of the crop region, or None when the view is not cropped."""
    if not view.CropBoxActive:
        return None
    box = view.CropBox
    points = []
    for x in (box.Min.X, box.Max.X):
        for y in (box.Min.Y, box.Max.Y):
            points.append(frame.uv(box.Transform.OfPoint(DB.XYZ(x, y, box.Min.Z))))
    us, vs = [p[0] for p in points], [p[1] for p in points]
    return (min(us), min(vs), max(us), max(vs))


def annotation_extent(view, frame, extent, scale):
    """(umin, vmin, umax, vmax) of the annotation crop, or None when the view has none.
    Revit keeps the four offsets of the annotation crop as distances on the sheet,
    so in the model they are that distance times the view scale."""
    if extent is None:
        return None
    try:
        active = view.get_Parameter(DB.BuiltInParameter.VIEWER_ANNOTATION_CROP_ACTIVE)
        if active is None or active.AsInteger() != 1:
            return None
        manager = view.GetCropRegionShapeManager()
        return (extent[0] - manager.LeftAnnotationCropOffset * scale, extent[1] - manager.BottomAnnotationCropOffset * scale,
                extent[2] + manager.RightAnnotationCropOffset * scale, extent[3] + manager.TopAnnotationCropOffset * scale)
    except Exception:
        return None


GRID_BUBBLE_MM = 12.0           # diameter of a grid bubble on the sheet (kept clear of tags, with a small margin)


def grid_obstacles(doc, view, frame, scale, area=None):
    """{'lines': [((u, v), (u, v))], 'bubbles': [box]} of the grids seen in this view - this model's grids
    (their 2D extents in the view and the bubbles shown at their ends) and the grids of loaded links
    (their lines, with a bubble at each end). Empty when grids are hidden in the view."""
    out = {'lines': [], 'bubbles': []}
    try:
        if view.GetCategoryHidden(DB.ElementId(DB.BuiltInCategory.OST_Grids)):
            return out
    except Exception:
        pass
    radius = GRID_BUBBLE_MM / 2.0 * MM * scale

    def bubble(end, other):
        du, dv = end[0] - other[0], end[1] - other[1]
        length = math.hypot(du, dv) or 1.0
        cu, cv = end[0] + du / length * radius, end[1] + dv / length * radius     # the circle sits beyond the end
        out['bubbles'].append((cu - radius, cv - radius, cu + radius, cv + radius))

    def add(curve, transform=None, ends=(True, True)):
        try:
            p0, p1 = curve.GetEndPoint(0), curve.GetEndPoint(1)
        except Exception:
            return
        if transform is not None:
            p0, p1 = transform.OfPoint(p0), transform.OfPoint(p1)
        a, b = frame.uv(p0), frame.uv(p1)
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
            return                                          # a grid seen end-on (in a section): no line
        if ends[0]:
            bubble(a, b)
        if ends[1]:
            bubble(b, a)
        if area is not None:
            # only the part that can meet a tag (a linked site grid can be hundreds of metres long)
            part = atl_layout.clip_segment(a, b, area)
            if part is None:
                return
            a, b = part
        out['lines'].append((a, b))

    for grid in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid):
        try:
            curves = list(grid.GetCurvesInView(DB.DatumExtentType.ViewSpecific, view))
        except Exception:
            curves = [grid.Curve]
        for curve in curves:
            ends = []
            for end in (DB.DatumEnds.End0, DB.DatumEnds.End1):
                try:
                    ends.append(bool(grid.IsBubbleVisibleInView(end, view)))
                except Exception:
                    ends.append(True)
            add(curve, None, tuple(ends))
    try:
        links = list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.RevitLinkInstance))
    except Exception:
        links = []
    for link in links:
        try:
            link_doc = link.GetLinkDocument()
            if link_doc is None:
                continue
            transform = link.GetTotalTransform()
            for grid in DB.FilteredElementCollector(link_doc).OfClass(DB.Grid):
                add(grid.Curve, transform)
        except Exception:
            continue
    if area is not None:
        out['bubbles'] = [b for b in out['bubbles']
                          if b[2] >= area[0] and b[0] <= area[2] and b[3] >= area[1] and b[1] <= area[3]]
    return out


# ---------------------------------------------------------------------------
# what can be tagged, and with what
# ---------------------------------------------------------------------------
def _bic_name(category):
    try:
        return str(category.BuiltInCategory)
    except Exception:
        return None


def _tag_names(name):
    """Possible names of the tag category of an element category: OST_CableTray -> OST_CableTrayTags."""
    names = [name + 'Tags']
    if name.endswith('s'):
        names.append(name[:-1] + 'Tags')                    # OST_ElectricalFixtures -> OST_ElectricalFixtureTags
    if name.endswith('Curves'):
        names.append(name[:-6] + 'Tags')                    # OST_DuctCurves -> OST_DuctTags
    return names


def tag_types(doc):
    """{tag category name: [(label, symbol)]} of every loaded tag type, labels sorted."""
    found = {}
    for symbol in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol):
        name = _bic_name(symbol.Category) if symbol.Category is not None else None
        if not name or not name.endswith('Tags'):
            continue
        type_name = symbol.get_Parameter(DB.BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        found.setdefault(name, []).append((u'{0} : {1}'.format(symbol.FamilyName, type_name), symbol))
    for name in found:
        found[name].sort(key=lambda pair: pair[0].lower())
    return found


RUN_LIMIT = 3000                          # pieces looked at when following one run


def _connectors(element):
    """The connectors of a tray, conduit, duct, pipe or fitting; [] for anything else."""
    for owner in (element, getattr(element, 'MEPModel', None)):
        try:
            return list(owner.ConnectorManager.Connectors)
        except Exception:
            continue
    return []


def run_of(element, cache):
    """A number that is the same for every piece of one connected run (trays and the fittings
    between them), and different for a tray that is not connected to it - e.g. the next riser.
    cache: {element id: run} for the model the element is in."""
    start = element.Id.Value
    if start in cache:
        return cache[start]
    kind = _bic_name(element.Category) if element.Category is not None else None
    seen, queue = set([start]), [element]
    while queue and len(seen) < RUN_LIMIT:
        current = queue.pop()
        for connector in _connectors(current):
            try:
                partners = list(connector.AllRefs)
            except Exception:
                continue
            for partner in partners:
                try:
                    owner = partner.Owner
                    name = _bic_name(owner.Category)
                    # along the run only: the same kind of element, and the fittings that join them
                    if name is None or not (name == kind or name.endswith('Fitting')):
                        continue
                    if owner.Id.Value not in seen:
                        seen.add(owner.Id.Value)
                        queue.append(owner)
                except Exception:
                    continue
    run = min(seen)
    for one in seen:
        cache[one] = run
    return run


def _solid_points(items, points, depth=0):
    """The corner points of every solid of an element, as it stands in its model."""
    for item in items:
        if isinstance(item, DB.GeometryInstance):
            if depth < 4:
                _solid_points(item.GetInstanceGeometry(), points, depth + 1)
            continue
        edges = getattr(item, 'Edges', None)
        if edges is None:
            continue
        for edge in edges:
            for point in edge.Tessellate():
                points.append(point)


class Target(object):
    """One element that can get a tag."""

    def __init__(self, element, link=None):
        self.element, self.link = element, link
        self.transform = link.GetTotalTransform() if link is not None else None

    @property
    def key(self):
        return (self.link.Id.Value if self.link is not None else 0, self.element.Id.Value)

    def reference(self):
        reference = DB.Reference(self.element)
        return reference.CreateLinkReference(self.link) if self.link is not None else reference

    def _model(self, point):
        return self.transform.OfPoint(point) if self.transform is not None else point

    def _box_uv(self, view, frame):
        """(umin, vmin, umax, vmax) of the element in the view, or None."""
        try:
            box = self.element.get_BoundingBox(view if self.link is None else None)
        except Exception:
            box = None
        if box is None:
            return None
        points = [frame.uv(self._model(DB.XYZ(x, y, z))) for x in (box.Min.X, box.Max.X) for y in (box.Min.Y, box.Max.Y)
                  for z in (box.Min.Z, box.Max.Z)]
        return (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))

    def outline(self, view, frame):
        """The corner points of the element as it is seen in the view, [(u, v)]; None when it has no solids.
        With it a leader can end inside the element even when the element stands askew in the view."""
        try:
            options = DB.Options()
            if self.link is None:
                options.View = view
            points = []
            _solid_points(self.element.get_Geometry(options), points)
            if len(points) < 3:
                return None
            if len(points) > 400:                           # plenty for an outline
                points = points[::len(points) // 400 + 1]
            return [frame.uv(self._model(p)) for p in points]
        except Exception:
            return None

    def geometry(self, view, frame, extent):
        """What the layout needs to know: (anchor (u, v), seg, body) or None.
        anchor: the middle of the visible part of a tray, the insertion point of a device,
                otherwise the middle of the element;
        seg:    the visible part of a tray, along which the arrow may slide;
        body:   (v low, v high) of the symbol of a device."""
        location = self.element.Location
        curve = getattr(location, 'Curve', None)
        if curve is not None:
            a, b = frame.uv(self._model(curve.GetEndPoint(0))), frame.uv(self._model(curve.GetEndPoint(1)))
            seg = (atl_layout.clip_segment(a, b, extent) if extent is not None else None) or (a, b)
            if abs(seg[1][0] - seg[0][0]) + abs(seg[1][1] - seg[0][1]) > END_ON:
                return ((seg[0][0] + seg[1][0]) / 2.0, (seg[0][1] + seg[1][1]) / 2.0), seg, None
            curve = None                                    # a tray seen end-on (in a section): treated as a device
        box = self._box_uv(view, frame)
        point = getattr(location, 'Point', None)
        if point is not None and frame.is_plan:
            anchor = frame.uv(self._model(point))
        elif box is not None:                               # sections and elevations: the middle of what is seen
            anchor = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        elif point is not None:
            anchor = frame.uv(self._model(point))
        else:
            return None
        body = None
        if box is not None and box[1] <= anchor[1] <= box[3] and box[3] - box[1] > 1e-6:
            body = (box[1], box[3])
        return anchor, None, body

    def anchor(self, view, frame, extent):
        found = self.geometry(view, frame, extent)
        return found[0] if found else None

    def outside_crop(self, view, frame, extent):
        """True when nothing of the element is inside the crop region (u low, v low, u high, v high).

        A view collector - for linked models especially - also returns elements whose 3D box only
        touches the crop box: a diagonal tray that passes by a corner of the crop region. Such a tag
        is cut away by the crop, Revit gives it no size, and it used to be reported as "the tag has
        no value to show" although its text was there."""
        if extent is None:
            return False
        location = self.element.Location
        curve = getattr(location, 'Curve', None)
        if curve is not None:
            try:
                a = frame.uv(self._model(curve.GetEndPoint(0)))
                b = frame.uv(self._model(curve.GetEndPoint(1)))
            except Exception:
                a = b = None
            if a is not None and abs(b[0] - a[0]) + abs(b[1] - a[1]) > END_ON:
                return atl_layout.clip_segment(a, b, extent) is None
        box = self._box_uv(view, frame)
        if box is not None:
            return box[2] < extent[0] or box[0] > extent[2] or box[3] < extent[1] or box[1] > extent[3]
        point = getattr(location, 'Point', None)
        if point is not None:
            u, v = frame.uv(self._model(point))
            return not (extent[0] <= u <= extent[2] and extent[1] <= v <= extent[3])
        return False


class Group(object):
    """The elements of one category in the view, with the tag types that fit them."""

    def __init__(self, name):
        self.name = name                  # category name as the user knows it
        self.targets = []
        self.types = []                   # [(label, symbol)]
        self.rooms = False                # True: rooms - their tags go inside the room
        self.mode = SIDES                 # where the tags go unless the user chooses otherwise


def tagged_keys(doc, view):
    """Keys of the elements that already have a tag in this view."""
    keys = set()
    for tag in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.IndependentTag):
        try:
            for item in tag.GetTaggedElementIds():
                if item.LinkInstanceId is not None and item.LinkInstanceId.Value > 0:
                    keys.add((item.LinkInstanceId.Value, item.LinkedElementId.Value))
                else:
                    keys.add((0, item.HostElementId.Value))
        except Exception:
            pass
    try:                                                    # room tags
        for tag in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.SpatialElementTag):
            try:
                item = tag.TaggedRoomId
                if item.LinkInstanceId is not None and item.LinkInstanceId.Value > 0:
                    keys.add((item.LinkInstanceId.Value, item.LinkedElementId.Value))
                else:
                    keys.add((0, item.HostElementId.Value))
            except Exception:
                pass
    except Exception:
        pass
    return keys


def _uv_rect(box, frame, transform=None):
    """(u low, v low, u high, v high) of a model box in the view."""
    points = [DB.XYZ(x, y, z) for x in (box.Min.X, box.Max.X) for y in (box.Min.Y, box.Max.Y) for z in (box.Min.Z, box.Max.Z)]
    if transform is not None:
        points = [transform.OfPoint(p) for p in points]
    uvs = [frame.uv(p) for p in points]
    return (min(p[0] for p in uvs), min(p[1] for p in uvs), max(p[0] for p in uvs), max(p[1] for p in uvs))


def existing_boxes(doc, view, frame):
    """The boxes of the tags that are in the view already: new tags without leader keep off them."""
    found = []
    for kind in ('IndependentTag', 'SpatialElementTag'):
        try:
            for tag in DB.FilteredElementCollector(doc, view.Id).OfClass(getattr(DB, kind)):
                try:
                    box = tag.get_BoundingBox(view)
                    if box is not None:
                        found.append(_uv_rect(box, frame))
                except Exception:
                    pass
        except Exception:
            pass
    return found


def _room_tag(doc, view, target, symbol, point):
    """A room tag at the point (model coordinates), of the chosen tag type."""
    room = target.element
    room_id = DB.LinkElementId(target.link.Id, room.Id) if target.link is not None else DB.LinkElementId(room.Id)
    tag = doc.Create.NewRoomTag(room_id, DB.UV(point.X, point.Y), view.Id)
    try:
        if tag.GetTypeId() != symbol.Id:
            tag.ChangeTypeId(symbol.Id)
    except Exception:
        pass
    return tag


def _room_inside(target, frame):
    """inside(u, v): True when that point of the view is in the room (also for a room of a linked model)."""
    room = target.element
    try:
        height = room.Location.Point.Z + 1.0               # a little above the floor of the room
    except Exception:
        height = None
    back = target.transform.Inverse if target.transform is not None else None

    def inside(u, v):
        try:
            p = frame.xyz(u, v)
            if back is not None:
                p = back.OfPoint(p)
            return bool(room.IsPointInRoom(DB.XYZ(p.X, p.Y, height if height is not None else p.Z)))
        except Exception:
            return False
    return inside


def _add(groups, types, element, link):
    category = element.Category
    if category is None:
        return
    name = _bic_name(category)
    if not name or name.endswith('Tags'):
        return
    label = category.Name + (u'  (linked)' if link is not None else u'')
    group = groups.get(label)
    if group is None:
        fitting = []
        for tag_name in _tag_names(name):
            fitting.extend(types.get(tag_name, []))
        if not fitting and MULTI not in types:
            groups[label] = False                           # nothing can tag this category: remembered, not shown
            return
        group = groups[label] = Group(label)
        group.rooms = name == ROOMS
        group.mode = IN_ROOM if group.rooms else (NEAR if name in NEAR_BY_DEFAULT else SIDES)
        seen = set()
        for pair in fitting + types.get(MULTI, []):
            if pair[0] not in seen:
                seen.add(pair[0])
                group.types.append(pair)
    if group:
        group.targets.append(Target(element, link))


def find_groups(doc, view, selected_ids=None, include_links=False):
    """[Group] of the elements seen in the view that a loaded tag type fits, by category name.
    selected_ids: when given, only these elements of the model."""
    types = tag_types(doc)
    groups = {}
    for element in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
        if selected_ids is not None and element.Id.Value not in selected_ids:
            continue
        try:
            _add(groups, types, element, None)
        except Exception:
            pass
    if include_links and selected_ids is None:
        for link in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.RevitLinkInstance):
            try:
                if link.GetLinkDocument() is None:
                    continue
                for element in DB.FilteredElementCollector(doc, view.Id, link.Id).WhereElementIsNotElementType():
                    _add(groups, types, element, link)
            except Exception:
                pass                                        # link not loaded, or a Revit without this collector
    return sorted([g for g in groups.values() if g], key=lambda g: g.name.lower())


# ---------------------------------------------------------------------------
# creating the tags - the only place where the model is changed
# ---------------------------------------------------------------------------
class Report(object):
    def __init__(self):
        self.created = 0
        self.already = 0                  # skipped: tagged before
        self.same_text = 0                # removed: another element gives the same tag
        self.stacked = 0                  # removed: a piece right above / below another one with the same text (a riser)
        self.same_room = 0                # removed: the same room (name and place) from a second linked model
        self.rooms_outside = 0            # rooms whose tag point is outside the view (only partly seen in it)
        self.notes = []                   # one line per element, for the details file
        self.empty = 0                    # removed: the tag has nothing to show
        self.outside_crop = 0             # not tagged: the element is in the view's range but outside its crop region
        self.not_visible = 0              # removed: the tag has a text, but Revit does not show it in this view
        self.failed = []                  # (category, reason)
        self.unaligned = 0                # tags Revit did not let us place exactly
        self.estimated = False            # view turned 45 degrees: text sizes are estimates
        self.kept_square = 0              # leaders that kept their 90 degree corner: the chosen shape would have crossed another leader
        self.beside = 0                   # tags placed beside their element, without leader
        self.in_rooms = 0                 # room tags placed inside their room
        self.crowded = 0                  # of those: no free place was found, they may cover something
        self.rooms_skipped = 0            # rooms not tagged: this is not a plan view
        self.previewed = 0                # tags in a preview (nothing was created)
        self.view_box = None              # (u low, v low, u high, v high) of the crop region, for drawing a preview
        self.limits = None                # the annotation crop the tags are kept in, for drawing a preview
        self.annotation = None            # (left, right) room of the annotation crop in mm on the sheet, when it was used
        self.pulled_in = False            # the annotation crop is narrow: tags stand closer than asked, or over the layout
        self.outside = 0                  # tags that could not be kept inside the annotation crop
        self.nearby = 0                   # tags placed in the free space next to their element, short leader
        self.nearby_to_sides = 0          # of the 'nearby' tags: no free place within reach - put in the side columns
        self.margins = None               # {'L','R','A','B': n} 'nearby' tags put in the band outside the crop region
        self.grid_lines = 0               # grid lines seen in the view (kept clear of tags)
        self.outer_column = 0             # side-column tags put in a second column further out (the view was too short)
        self.overflow_above_below = 0     # side-column tags that did not fit the view's height: put above / below
        self.on_grid = 0                  # margin tags that could not be moved off a grid line / bubble
        self.error = None

    def lines(self):
        out = [u'{0} tag(s) created.'.format(self.created)]
        if self.already:
            out.append(u'{0} element(s) were tagged already and were left as they are.'.format(self.already))
        if self.same_text:
            out.append(u'{0} piece(s) not tagged: another piece of the same connected run shows the same tag text.'.format(self.same_text))
        if self.rooms_outside:
            out.append(u'{0} room(s) only partly in this view were not tagged: their tag point lies outside the view.'.format(self.rooms_outside))
        if self.same_room:
            out.append(u'{0} room(s) not tagged twice: the same room is in a second linked model.'.format(self.same_room))
        if self.stacked:
            out.append(u'{0} piece(s) not tagged: they stand right above / below another piece with the same tag text '
                       u'(e.g. a riser cut into pieces) - one tag is enough there.'.format(self.stacked))
        if self.empty:
            out.append(u'{0} element(s) not tagged: the tag has no value to show.'.format(self.empty))
        if self.outside_crop:
            out.append(u'{0} element(s) not tagged: they are outside the crop region of this view.'.format(self.outside_crop))
        if self.not_visible:
            out.append(u'{0} element(s) not tagged: Revit does not show their tag in this view '
                       u'(hidden by the crop, a filter or the view range).'.format(self.not_visible))
        if self.beside:
            out.append(u'{0} tag(s) stand beside their element, without leader.'.format(self.beside))
        if self.in_rooms:
            out.append(u'{0} room tag(s) stand inside their room.'.format(self.in_rooms))
        if self.nearby:
            out.append(u'{0} tag(s) stand in the free space next to their element, with a short leader.'.format(self.nearby))
        if self.margins and sum(self.margins.values()):
            m = self.margins
            out.append(u'{0} tag(s) found no free space next to their element and stand outside the crop region, in the '
                       u'nearest margin: {1} left, {2} right, {3} above, {4} below.'.format(
                           sum(m.values()), m['L'], m['R'], m['A'], m['B']))
        if self.outer_column:
            out.append(u'{0} tag(s) stand in a second column further out (one column was taller than the view); '
                       u'their leaders run through the gaps of the inner column.'.format(self.outer_column))
        if self.overflow_above_below:
            out.append(u'{0} tag(s) did not fit in the side columns (taller than the view) and stand in the band '
                       u'above / below the crop region instead.'.format(self.overflow_above_below))
        if self.nearby and self.grid_lines:
            out.append(u'{0} grid line(s) in the view: tags are kept off grid lines and grid bubbles.'.format(self.grid_lines))
        if self.on_grid:
            out.append(u'{0} margin tag(s) could not be moved off a grid line or bubble - move those by hand.'.format(self.on_grid))
        if self.nearby_to_sides:
            out.append(u'{0} tag(s) found no free space within {1:.0f} mm of their element and went to the side columns.'
                       .format(self.nearby_to_sides, NEARBY_REACH_MM))
        if self.crowded:
            out.append(u'{0} of them found no free place and may cover something - move those by hand.'.format(self.crowded))
        if self.rooms_skipped:
            out.append(u'{0} room(s) not tagged: room tags are placed in plan views only.'.format(self.rooms_skipped))
        if self.annotation is not None and self.created:
            out.append(u'Annotation crop: {0:.0f} mm left and {1:.0f} mm right of the crop region (on the sheet).'.format(*self.annotation))
            if getattr(self, 'annotation_vertical', None):
                out.append(u'Annotation crop: {0:.0f} mm below and {1:.0f} mm above the crop region.'.format(*self.annotation_vertical))
            if self.pulled_in:
                out.append(u'That is less than the tags need at the distance asked for, so they were moved closer to (or over) the layout. '
                           u'Make the annotation crop wider for a cleaner result.')
            if self.outside:
                out.append(u'{0} tag(s) do not fit inside the annotation crop and are hidden by it - make the annotation crop larger.'.format(self.outside))
        if self.kept_square and self.created:
            out.append(u'{0} leader(s) kept a 90\u00b0 corner: with the shape you chose they would have crossed another leader.'.format(self.kept_square))
        if self.estimated and self.created:
            out.append(u'This view is turned 45 degrees: the text sizes are estimated, so the tags may not end exactly on one line.')
        if self.unaligned:
            out.append(u'{0} tag(s) could not be given a leader exactly as planned - check them.'.format(self.unaligned))
        if self.failed:
            reasons = sorted(set(u'{0}: {1}'.format(c, r) for c, r in self.failed))
            out.append(u'{0} element(s) could not be tagged:'.format(len(self.failed)))
            out.extend(u'   ' + reason for reason in reasons[:6])
        return out


class _Made(object):
    def __init__(self, tag, target, group, start, seg=None, body=None, outline=None):
        self.tag, self.target, self.group, self.start = tag, target, group, start
        self.seg, self.body, self.outline = seg, body, outline
        self.mode = SIDES                 # SIDES, NEAR or IN_ROOM
        self.rect = None                  # (u low, v low, u high, v high) of the element in the view
        self.centre = None                # centre of the text when the head is at start, (u, v)
        self.item = None


def _measure(made, view, frame, text):
    """Text size and where its centre is, from the box Revit gives (along the model axes)."""
    box = made.tag.get_BoundingBox(view)
    if box is None:
        return None
    # the middle of the box is the middle of the text, in plans, sections and elevations alike
    made.centre = frame.uv(DB.XYZ((box.Min.X + box.Max.X) / 2.0, (box.Min.Y + box.Max.Y) / 2.0, (box.Min.Z + box.Max.Z) / 2.0))
    extents = (box.Max.X - box.Min.X, box.Max.Y - box.Min.Y, box.Max.Z - box.Min.Z)
    return atl_layout.text_size(extents, frame.right, frame.upward, text)


SPOT_PAD = 0.01                           # feet: a point-like element counts as a tiny square of this half size
SAME_SPOT = 0.6                           # two pieces cover the same spot when they share this part of the smaller one


def _spot(made):
    """(u low, v low, u high, v high) the element covers in the view; around its anchor when it has no box."""
    rect = made.rect
    if rect is None:
        if made.start is None:
            return None
        rect = (made.start[0], made.start[1], made.start[0], made.start[1])
    return (rect[0] - SPOT_PAD, rect[1] - SPOT_PAD, rect[2] + SPOT_PAD, rect[3] + SPOT_PAD)


def _same_spot(a, b):
    """True when the two rectangles are (nearly) one on top of the other in the view."""
    wide = min(a[2], b[2]) - max(a[0], b[0])
    high = min(a[3], b[3]) - max(a[1], b[1])
    if wide <= 0 or high <= 0:
        return False
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return smaller > 0 and wide * high >= SAME_SPOT * smaller


MIN_LEG_MM = 3.0                          # the last leg of a leader is never shorter than this on the sheet
BEST = 'best'                             # create_tags(variant=BEST): the arrangement that ranks first


DETAILS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'AutoTagLayout', 'last_run.txt')
VERSION = 'two-columns 1'


def _write_details(view, report, preview):
    """What happened to every element of the last run, for checking a result that looks wrong."""
    try:
        folder = os.path.dirname(DETAILS)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        lines = [u'Auto Tag Layout {0} - {1} - view "{2}" (scale 1:{3})'.format(
            VERSION, u'preview' if preview is not None else u'create', view.Name, view.Scale)]
        lines.extend(report.lines())
        lines.append(u'')
        lines.append(u'decision | category | text | link, element | run | anchor (u, v) | box in the view | tray part seen')
        lines.extend(report.notes)
        with io.open(DETAILS, 'w', encoding='utf-8') as handle:
            handle.write(u'\n'.join(lines) + u'\n')
    except Exception:
        pass                                               # the details are only a help


def _fmt(values):
    if values is None:
        return u'-'
    try:
        return u'(' + u', '.join(u'{0:.3f}'.format(float(x)) for x in values) + u')'
    except Exception:
        try:
            return u'(' + u', '.join(_fmt(x) for x in values) + u')'
        except Exception:
            return u'?'


def create_tags(doc, view, choices, gap_mm=1.0, offset_mm=10.0, sides=atl_layout.BOTH,
                skip_tagged=True, one_per_text=True, leader_gap_mm=3.0, inside_annotation=True,
                shape=atl_layout.CORNER_90, variant=None, preview=None):
    report = _create_tags(doc, view, choices, gap_mm, offset_mm, sides, skip_tagged, one_per_text, leader_gap_mm,
                          inside_annotation, shape, variant, preview)
    _write_details(view, report, preview)
    return report


def _create_tags(doc, view, choices, gap_mm=1.0, offset_mm=10.0, sides=atl_layout.BOTH,
                 skip_tagged=True, one_per_text=True, leader_gap_mm=3.0, inside_annotation=True,
                 shape=atl_layout.CORNER_90, variant=None, preview=None):
    """choices = [(Group, tag symbol)]. Creates and arranges the tags in one transaction. Returns a Report.

    variant: the key of one of atl_layout.VARIANTS, or BEST for the arrangement that ranks first;
             None = as given by `sides`.
    preview: a list. When given, NOTHING is kept in the model: the tags are made only to learn their
             texts and sizes, the different arrangements (atl_layout.Option) are put into the list,
             and the transaction is rolled back."""
    report = Report()
    frame = Frame(view)
    report.estimated = atl_layout.size_needs_estimate(frame.right, frame.upward)
    scale = float(view.Scale or 1)
    extent = crop_extent(view, frame)
    existing = tagged_keys(doc, view) if skip_tagged else set()
    taken = existing_boxes(doc, view, frame)               # where tags stand already
    transaction = DB.Transaction(doc, 'Auto Tag Layout')
    transaction.Start()
    try:
        made = []
        for choice in choices:
            # (group, tag type) or (group, tag type, where the tags go)
            group, symbol = choice[0], choice[1]
            mode = IN_ROOM if group.rooms else (choice[2] if len(choice) > 2 and choice[2] in (SIDES, NEAR, NEARBY) else SIDES)
            if not symbol.IsActive:
                symbol.Activate()
            for target in group.targets:
                if not getattr(target.element, 'IsValidObject', True):    # deleted in Revit while the window was open
                    continue
                if target.key in existing:
                    report.already += 1
                    continue
                if mode == IN_ROOM and not frame.is_plan:
                    report.rooms_skipped += 1
                    continue
                try:
                    # rooms partly in the view keep their own handling (ROOM OUTSIDE below)
                    if mode != IN_ROOM and target.outside_crop(view, frame, extent):
                        report.outside_crop += 1
                        continue
                except Exception:
                    pass
                try:
                    found = target.geometry(view, frame, extent)
                    anchor = found[0] if found else None
                    if anchor is None:
                        report.failed.append((group.name, u'the element has no position in this view'))
                        continue
                    start = frame.xyz(anchor[0], anchor[1])
                    if mode == IN_ROOM:
                        tag = _room_tag(doc, view, target, symbol, start)
                        one = _Made(tag, target, group, anchor)
                    else:
                        tag = DB.IndependentTag.Create(doc, symbol.Id, view.Id, target.reference(), False,
                                                       DB.TagOrientation.Horizontal, start)
                        # the shape of a device (or of a tray seen end-on): where a straight leader may end
                        seen_as = target.outline(view, frame) if (found[1] is None and mode in (SIDES, NEARBY)) else None
                        one = _Made(tag, target, group, anchor, found[1], found[2], seen_as)
                    one.mode = mode
                    one.rect = target._box_uv(view, frame)
                    made.append(one)
                except Exception as error:
                    report.failed.append((group.name, u'{0}'.format(error).splitlines()[0]))
        doc.Regenerate()                                    # the tags get their text and size

        items, seen, runs, spots, rooms_seen = [], set(), {}, {}, set()

        def note(one, text, decision, run=None):
            try:
                report.notes.append(u'{0} | {1} | {2} | {3} | {4} | {5} | {6} | {7}'.format(
                    decision, one.group.name, text.replace(u'\n', u' / '), one.target.key, run if run is not None else u'-',
                    _fmt(one.start), _fmt(one.rect), _fmt(one.seg)))
            except Exception:
                pass

        for one in made:
            try:
                text = (one.tag.TagText or u'').strip()
            except Exception:
                text = u''
            size = _measure(one, view, frame, text)
            if one.mode == IN_ROOM and size is not None:
                text = text or u'room'                      # a room tag always shows something of its room
            if not text or text == u'?' or size is None:
                doc.Delete(one.tag.Id)
                if one.mode == IN_ROOM:
                    # FIX: these were reported as "the tag has no value to show" - they are rooms (LOBBY, FAN ROOM ...)
                    # whose tag point lies outside this view, so Revit gives the tag no size here
                    note(one, text, u'ROOM OUTSIDE')
                    report.rooms_outside += 1
                elif text and text != u'?':
                    # the tag HAS a text - Revit just does not draw it here (no box): not an empty tag
                    note(one, text, u'NOT VISIBLE')
                    report.not_visible += 1
                else:
                    note(one, text, u'EMPTY')
                    report.empty += 1
                continue
            run = None
            if one.mode == IN_ROOM:
                # FIX: the same room in two linked models (rooms ELEC and CRAC UNIT came from links 1905646 AND 5280388)
                # was tagged twice, one tag on top of the other. One room tag per room name and place.
                spot = _spot(one)
                where = ('room', text, tuple(round(x, 1) for x in spot) if spot else None)
                if where in rooms_seen:
                    note(one, text, u'SAME ROOM')
                    doc.Delete(one.tag.Id)
                    report.same_room += 1
                    continue
                rooms_seen.add(where)
            if one_per_text and one.mode in (SIDES, NEARBY):
                # one tag for the pieces of ONE run that show the same text. A separate tray with the
                # same name and size (the next riser, a parallel tray) is another run and gets its own tag.
                link_key = one.target.key[0]
                run = run_of(one.target.element, runs.setdefault(link_key, {}))
                same = (one.group.name, text, link_key, run)
                if same in seen:
                    note(one, text, u'SAME RUN', run)
                    doc.Delete(one.tag.Id)
                    report.same_text += 1
                    continue
                # FIX: a riser is often made of several tray pieces one above the other (one per floor, or cut
                # at a level). Seen from above they stand on the same spot, are not connected to each other,
                # and each got its own tag - two leaders ending on the same point, one over the other.
                # Pieces that show the same text and cover the same spot in the view get one tag.
                spot = _spot(one)
                where = (one.group.name, text, link_key)
                if spot is not None and any(_same_spot(spot, other) for other in spots.get(where, [])):
                    note(one, text, u'STACKED', run)
                    doc.Delete(one.tag.Id)
                    report.stacked += 1
                    continue
                if spot is not None:
                    spots.setdefault(where, []).append(spot)
                seen.add(same)
            note(one, text, u'TAGGED', run)
            one.item = atl_layout.Item(one, one.start[0], one.start[1], size[0], size[1], text, one.seg, one.body, one.outline)
            one.item.rect = one.rect
            items.append(one.item)

        # ---- tags without leader first: beside their element, or inside their room. They do not
        #      depend on the arrangement of the others and are the same in every preview.
        fixed = [item for item in items if item.key.mode in (NEAR, IN_ROOM)]
        nearby = [item for item in items if item.key.mode == NEARBY]
        items = [item for item in items if item.key.mode == SIDES]
        small = gap_mm * MM * scale
        # what a tag must not cover: the tags already in the view, and every element that is being tagged
        blocked = list(taken) + [one.rect for one in made if one.rect is not None and one.mode != IN_ROOM and one.item is not None]
        if fixed:
            beside = [item for item in fixed if item.key.mode == NEAR]
            report.crowded += atl_layout.place_near(beside, blocked, small, extent)
            report.beside = len(beside)
            for item in fixed:
                if item.key.mode != IN_ROOM:
                    continue
                around = item.key.rect or (item.u - 30.0, item.v - 30.0, item.u + 30.0, item.v + 30.0)
                if not atl_layout.place_in_room(item, _room_inside(item.key.target, frame), blocked, small, around):
                    report.crowded += 1
                report.in_rooms += 1
        # ---- big views: each tag in the free space next to its own element, short straight leader.
        #      Those that find no free place within reach go to the side columns with the others.
        placed_nearby = []
        if nearby:
            margin_limits = annotation_extent(view, frame, extent, scale)
            # with a band outside the crop region, the tags next to their elements stay inside the crop:
            # the band is kept for the stacks of tags that find no room inside (left, right, above, below)
            limits_near = extent if (extent is not None and margin_limits is not None) else \
                (annotation_extent(view, frame, extent, scale) if inside_annotation else extent)
            try:
                grids = grid_obstacles(doc, view, frame, scale, margin_limits or extent)
            except Exception:
                grids = None
            report.grid_lines = len(grids['lines']) if grids else 0
            no_place = atl_layout.place_with_leader(
                nearby, blocked + [item.box for item in fixed], small, limits_near or extent,
                reach=NEARBY_REACH_MM * MM * scale, leader_gap=leader_gap_mm * MM * scale, snap=small,
                obstacles=grids)
            placed_nearby = [item for item in nearby if item not in no_place]
            report.nearby = len(placed_nearby)
            if no_place and extent is not None and margin_limits is not None:
                # the free band outside the crop region: each to the margin nearest to its element
                edge = 1.0 * MM * scale
                margin_limits = (margin_limits[0] + edge, margin_limits[1] + edge,
                                 margin_limits[2] - edge, margin_limits[3] - edge)
                report.margins = atl_layout.place_in_margins(
                    no_place, extent, margin_limits, small, offset_mm * MM * scale,
                    leader_gap_mm * MM * scale, MIN_LEG_MM * MM * scale, others=list(placed_nearby),
                    obstacles=grids)
                report.on_grid = len([i for i in no_place if atl_layout.hits_obstacle(i.box, grids, small * 0.25)])
                placed_nearby += no_place
                report.outside += len(atl_layout.outside(no_place, margin_limits))
            else:
                report.nearby_to_sides = len(no_place)
                for item in no_place:                       # a fresh item for the side columns
                    item.side, item.cu, item.t, item.elbow = None, None, None, None
                    items.append(item)
        if preview is not None and placed_nearby and not items:
            only = atl_layout.Option('nearby', u'Nearby free space, short leaders', atl_layout.BOTH, atl_layout.AUTO,
                                     placed_nearby)
            only.fixed, only.best = fixed, True
            preview.append(only)
            report.previewed = len(fixed) + len(placed_nearby)
            report.view_box = extent
            transaction.RollBack()
            return report
        if preview is not None and fixed and not items:
            # nothing goes to the sides: one picture to look at
            only = atl_layout.Option('fixed', u'Beside the elements / inside the rooms', atl_layout.BOTH, atl_layout.AUTO, [])
            only.fixed, only.best = fixed, True
            preview.append(only)
            report.previewed = len(fixed)
            report.view_box = extent
            transaction.RollBack()
            return report

        for item in fixed:                                  # move each of them to its place
            one = item.key
            head = frame.xyz(one.start[0] + item.cu - one.centre[0], one.start[1] + item.t - one.centre[1])
            try:
                if one.mode == NEAR:
                    one.tag.HasLeader = False
                one.tag.TagHeadPosition = head
            except Exception:
                try:
                    old = one.tag.TagHeadPosition
                    one.tag.Location.Move(DB.XYZ(head.X - old.X, head.Y - old.Y, head.Z - old.Z))
                except Exception:
                    report.unaligned += 1
            report.created += 1

        if items:
            if extent is not None:
                umin, umax = extent[0], extent[2]
            else:
                umin, umax = min(i.u for i in items), max(i.u for i in items)
            limits = annotation_extent(view, frame, extent, scale) if inside_annotation else None
            if limits is not None:                          # 1 mm (on the sheet) clear of the annotation crop itself
                edge = 1.0 * MM * scale
                limits = (limits[0] + edge, limits[1] + edge, limits[2] - edge, limits[3] - edge)
                report.annotation = ((umin - limits[0]) / (MM * scale) + 1.0, (limits[2] - umax) / (MM * scale) + 1.0)
                if extent is not None:
                    report.annotation_vertical = ((extent[1] - limits[1]) / (MM * scale) + 1.0,
                                                  (limits[3] - extent[3]) / (MM * scale) + 1.0)
            style = atl_layout.AUTO
            measures = (umin, umax, gap_mm * MM * scale, offset_mm * MM * scale, leader_gap_mm * MM * scale, MIN_LEG_MM * MM * scale, limits)
            # the height of the layout, for the arrangements below / above it
            vspan = (extent[1], extent[3]) if extent is not None else (min(i.v for i in items), max(i.v for i in items))
            wanted = offset_mm * MM * scale
            grids_cache = []

            def overflow_to_bands(column_items, column_style):
                """A side column taller than the view: the tags that do not fit go to the free band ABOVE and
                BELOW the crop region (beyond the grid bubbles) instead of being hidden by the annotation crop.
                Returns how many of them really ended INSIDE the annotation crop - the band is often full
                (grid bubbles), and they used to be counted as moved although they were still hidden."""
                if limits is None or extent is None or column_style in (atl_layout.BELOW, atl_layout.ABOVE):
                    return 0
                overflow = atl_layout.outside(column_items, limits)
                if not overflow:
                    return 0
                if not grids_cache:
                    try:
                        grids_cache.append(grid_obstacles(doc, view, frame, scale, limits))
                    except Exception:
                        grids_cache.append(None)
                rest = [i for i in column_items if i not in overflow]
                atl_layout.place_in_margins(
                    overflow, extent, limits, gap_mm * MM * scale, offset_mm * MM * scale,
                    leader_gap_mm * MM * scale, MIN_LEG_MM * MM * scale, others=rest,
                    obstacles=grids_cache[0], allowed=('A', 'B'))
                still = atl_layout.outside(overflow, limits)
                return len([i for i in overflow if i not in still])

            def in_outer_column(column_items):
                """Tags in a second (outer) side column - not those that went on to the band above / below."""
                return len([i for i in column_items if getattr(i, 'outer', False)
                            and getattr(i, 'margin', None) not in ('A', 'B')])

            def pulled(column_items, column_style):
                """True when a side-column tag stands nearer to the layout than asked. Tags moved to the band
                above / below stand over the layout on purpose and do not count (they made this always True)."""
                if column_style in (atl_layout.BELOW, atl_layout.ABOVE):
                    return False
                side_items = [i for i in column_items if getattr(i, 'margin', None) not in ('A', 'B')]
                return any((umin - i.box[2] < wanted - 1e-6) if i.side == atl_layout.LEFT else (i.box[0] - umax < wanted - 1e-6)
                           for i in side_items)

            if preview is not None or variant == BEST:
                found = atl_layout.options(items, *measures, vspan=vspan)
                if preview is not None:
                    # the same overflow step as on Create, so that the picture is what will be created
                    for option in found:
                        overflow_to_bands(option.items, option.style)
                        option.outer = in_outer_column(option.items)
                        if limits is not None:
                            option.outside = len(atl_layout.outside(option.items, limits))
                            option.pulled_in = pulled(option.items, option.style)
                        option.corners = len([i for i in option.items if i.elbow is not None])
                        option.straight = len(option.items) - option.corners
                        option.length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                                            for i in option.items for a, b in zip(i.leader, i.leader[1:]))
                    found.sort(key=lambda option: option.rank())
                    for position, option in enumerate(found):
                        option.best = position == 0
                    # only looking: the tags that were made for measuring are taken out again
                    for option in found:
                        option.fixed = fixed
                        option.items = option.items + placed_nearby      # the same in every option
                    preview.extend(found)
                    report.previewed = len(items) + len(fixed)
                    report.created = 0
                    report.view_box = (umin, extent[1] if extent is not None else None, umax, extent[3] if extent is not None else None)
                    report.limits = limits
                    transaction.RollBack()
                    return report
                if found:
                    sides, style = found[0].sides, found[0].style
            elif variant is not None and atl_layout.variant(variant) is not None:
                sides, style = atl_layout.variant(variant)
            # a side column taller than the view gets a second column further out, when the band is wide enough
            atl_layout.arrange_double(items, umin, umax, gap_mm * MM * scale, offset_mm * MM * scale, sides,
                                      leader_gap_mm * MM * scale, MIN_LEG_MM * MM * scale, limits, style, vspan)
            # what still does not fit goes to the band above / below
            report.overflow_above_below = overflow_to_bands(items, style)
            report.outer_column = in_outer_column(items)
            if limits is not None:
                report.outside = len(atl_layout.outside(items, limits))
                report.pulled_in = pulled(items, style)
            report.kept_square = atl_layout.shape_leaders(items, shape)
        if items or placed_nearby:
            for item in items + placed_nearby:
                one = item.key
                report.notes.append(u'ARROW | {0} | {1} | side {2} | arrow {3} | corner {4} | text centre {5}'.format(
                    item.text.replace(u'\n', u' / '), one.target.key, item.side, _fmt((item.u, item.v)), _fmt(item.elbow), _fmt((item.cu, item.t))))
                tag = one.tag
                # move the head by as much as the text has to move
                head = frame.xyz(one.start[0] + item.cu - one.centre[0], one.start[1] + item.t - one.centre[1])
                try:
                    tag.HasLeader = True
                    tag.LeaderEndCondition = DB.LeaderEndCondition.Free
                    tag.TagHeadPosition = head
                    reference = list(tag.GetTaggedReferences())[0]
                    tag.SetLeaderEnd(reference, frame.xyz(item.u, item.v))
                    if item.elbow is not None:
                        elbow = item.elbow
                    else:                                   # one straight line (level or slanted): the corner lies on it, half way
                        edge = item.leader[0]
                        elbow = ((edge[0] + item.u) / 2.0, (edge[1] + item.v) / 2.0)
                    tag.SetLeaderElbow(reference, frame.xyz(elbow[0], elbow[1]))
                except Exception:
                    report.unaligned += 1
                report.created += 1
        if preview is not None:
            # a preview in which every tag dropped out (all tagged, empty or outside): it must not
            # commit - the measuring tags were deleted, but Activate() of a tag type would be kept
            transaction.RollBack()
            report.created = 0
            return report
        status = transaction.Commit()
        if status != DB.TransactionStatus.Committed:
            report.error = u'Revit did not keep the tags ({0}).'.format(status)
            report.created = 0
    except Exception as error:
        try:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
        except Exception:
            pass
        report.error = u'Nothing was created: {0}'.format(error)
        report.created = 0
    return report
