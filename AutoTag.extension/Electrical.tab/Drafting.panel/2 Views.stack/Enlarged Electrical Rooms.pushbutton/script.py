# -*- coding: utf-8 -*-
"""Enlarged Electrical Rooms - Revit 2025 / pyRevit.

Creates (or updates) one enlarged FLOOR PLAN view per electrical room of an architectural link:

    Linked room -> room boundary (SpatialElementBoundaryOptions) -> link transform (translation, rotation,
    shared coordinates: RevitLinkInstance.GetTotalTransform) -> boundary offset outward by the crop offset
    (+ door swings joined in) -> crop SHAPE of the new view that follows the room boundary.
    Fallback (option off, or the outline cannot be made): a rectangle in the view's axes / turned to the walls.

Order per view: create the floor plan at the room's (host) level -> set scale -> apply view template ->
set crop. A view that already exists (same name) is never created twice: it is updated (option) or skipped.

WHAT THIS CHANGES IN THE MODEL: floor plan views only (new views, or the crop / scale / template of the
views this tool made before). Everything is ONE Undo. The linked model is only read.
"""
import math
import re
import time

from Autodesk.Revit.DB import (BoundingBoxXYZ, BuiltInCategory, BuiltInParameter, CurveLoop, ElementId,
                               FilteredElementCollector, Level, Line, RevitLinkInstance,
                               SpatialElementBoundaryLocation, SpatialElementBoundaryOptions, Transaction,
                               TransactionGroup, Transform, ViewFamily, ViewFamilyType, ViewPlan, ViewType, XYZ)


# ---- step trace for "check logs": %APPDATA%\pyRevit\EnlargedRooms_trace.log (flushed after every line, so it
# survives a hang / crash and shows the last step reached)
_T0 = [time.time()]


def _trace_path():
    import os
    folder = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'pyRevit')
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return os.path.join(folder, 'EnlargedRooms_trace.log')


def trace(text, new=False):
    import io, os
    try:
        if new:
            _T0[0] = time.time()
        with io.open(_trace_path(), 'w' if new else 'a', encoding='utf-8') as handle:
            handle.write(u'{0} +{1:7.1f}s  {2}\n'.format(time.strftime('%H:%M:%S'), time.time() - _T0[0], text))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass

# ======================================================================= settings - edit here
# The window lists EVERY room of the chosen link - the user ticks the rooms (names vary per project).
# The window opens with NOTHING ticked. The names below are only SUGGESTIONS: the "Suggest" button ticks the
# rooms whose name matches one marked True. A room matches when its name is the suggestion, or starts with it
# followed by a space, "-", "_" or a digit: "LV ROOM 01" is an LV ROOM, "LVSG ROOM" is not.
# The rooms of each GENERATE are remembered per project and link: the "Last run" button ticks them again.
TARGET_ROOMS = [
    (u'MVSG ROOM', True),
    (u'MV ROOM', False),
    (u'LV ROOM', True),
    (u'LVSG ROOM', True),
    (u'TX ROOM', True),
    (u'UPS ROOM', True),
    (u'ELECTRICAL ROOM', False),
    (u'MDB ROOM', False),
    (u'SMDB ROOM', False),
    (u'DB ROOM', False),
]
VIEW_PREFIX = u'EL'                                  # EL-{ROOM NAME}-{ROOM NUMBER}
SCALES = [20, 25, 50, 75, 100]
DEFAULT_SCALE = 50
CROP_OFFSETS_MM = [300, 500, 750, 1000]
DEFAULT_OFFSET_MM = 500
DEFAULT_TEMPLATE_HINT = u'enlarged'                  # a view template whose name contains this is preselected
LEVEL_WARNING_MM = 300.0                             # report when the nearest host level is further than this

# Substation views: ticked rooms on the same level whose boundaries are at most this far apart (a wall
# between them) are merged into ONE view, named EL-SUBSTATION-{lowest room number}. A room with no
# neighbour keeps its own EL-{ROOM}-{NUMBER} view.
SUBSTATION_NAME = u'SUBSTATION'
MERGE_GAPS_MM = [500, 1000, 1500, 2000, 3000]
DEFAULT_MERGE_GAP_MM = 1000

FT_PER_MM = 1.0 / 304.8
INVALID_NAME_CHARS = re.compile(r'[\\:{}\[\]|;<>?`~,"\'/*]')
NO_TEMPLATE = u'<None>'


# ======================================================================= Revit links and rooms
def get_revit_links(doc):
    """[(display name, RevitLinkInstance, link document)] of the LOADED links, sorted by name."""
    out = []
    for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_doc = link.GetLinkDocument()
        except Exception:
            link_doc = None
        if link_doc is None:
            continue                                 # not loaded: nothing to read
        out.append((u'{0}'.format(link.Name), link, link_doc))
    return sorted(out, key=lambda item: item[0].lower())


def room_name(room):
    """The room's NAME (Room.Name in the API also carries the number)."""
    try:
        value = room.get_Parameter(BuiltInParameter.ROOM_NAME).AsString()
        if value:
            return value.strip()
    except Exception:
        pass
    return u''


def room_number(room):
    try:
        value = room.Number
        return value.strip() if value else u''
    except Exception:
        return u''


def get_rooms_from_link(link_doc):
    """Every room element of the linked model (placed or not - filtered later with a reason)."""
    return list(FilteredElementCollector(link_doc).OfCategory(BuiltInCategory.OST_Rooms)
                .WhereElementIsNotElementType())


def _normal(text):
    return re.sub(r'\s+', u' ', (text or u'').upper()).strip()


def match_target(name, targets):
    """The target a room name belongs to, or None. The LONGEST matching target wins."""
    name = _normal(name)
    best = None
    for target in targets:
        t = _normal(target)
        if name == t or (name.startswith(t) and len(name) > len(t) and (name[len(t)] in u' -_' or name[len(t)].isdigit())):
            if best is None or len(t) > len(_normal(best)):
                best = target
    return best


def filter_target_rooms(rooms, targets):
    """[(room, target)] of the rooms whose name matches one of the targets, sorted by target, level, number."""
    out = []
    for room in rooms:
        target = match_target(room_name(room), targets)
        if target is not None:
            out.append((room, target))
    return sort_rooms(out)


def _elevation(room):
    try:
        return room.Level.ProjectElevation if room.Level is not None else 0.0
    except Exception:
        return 0.0


def _level_name(room):
    try:
        return room.Level.Name if room.Level is not None else u''
    except Exception:
        return u''


def sort_rooms(items):
    """[(room, group name)] sorted by group name, level, number."""
    return sorted(items, key=lambda item: (_normal(item[1]), _elevation(item[0]), room_number(item[0]),
                                           getattr(item[0].Id, 'Value', 0)))


def is_placed(room):
    try:
        return room.Area > 0 and room.Location is not None
    except Exception:
        return False


def list_link_rooms(link_doc):
    """[room] - the PLACED rooms of the link, by level, number, name (for the window's list)."""
    rooms = [r for r in get_rooms_from_link(link_doc) if is_placed(r)]
    return sorted(rooms, key=lambda r: (_elevation(r), _level_name(r), room_number(r), _normal(room_name(r))))


def room_list_label(room):
    """'L01   |   S-003   |   MVSG ROOM'"""
    return u'{0}   |   {1}   |   {2}'.format(_level_name(room) or u'-', room_number(room) or u'-', room_name(room) or u'(no name)')


def suggested(room):
    """True when the room's name matches a suggestion that is marked True in TARGET_ROOMS."""
    return match_target(room_name(room), [t for t, on in TARGET_ROOMS if on]) is not None


# ---- the ticked rooms are remembered per project + link (room UniqueIds), in %APPDATA%\EleTools\EnlargedRooms
def _memory_file():
    import os
    folder = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'EnlargedRooms')
    if not os.path.isdir(folder):
        os.makedirs(folder)
    return os.path.join(folder, 'ticked_rooms.json')


def _memory_key(doc, link_name):
    try:
        title = doc.Title
    except Exception:
        title = u''
    # "ARC.rvt : 28 : location Internal" -> "ARC.rvt" (the instance number / location may change)
    return u'{0} || {1}'.format(title, (link_name or u'').split(u' : ')[0].strip())


def _old_memory_key(doc, link_name):
    try:
        title = doc.Title
    except Exception:
        title = u''
    return u'{0} || {1}'.format(title, re.sub(r'\s*:\s*\d+\s*$', u'', link_name or u''))


def load_ticked(doc, link_name):
    """The UniqueIds ticked last time for this project + link, or None (never run)."""
    import json, io, os
    try:
        path = _memory_file()
        if not os.path.isfile(path):
            return None
        with io.open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
        value = data.get(_memory_key(doc, link_name))
        if value is None:
            value = data.get(_old_memory_key(doc, link_name))         # saved by the previous version
        return set(value) if value is not None else None
    except Exception:
        return None


def save_ticked(doc, link_name, unique_ids):
    import json, io, os
    try:
        path = _memory_file()
        data = {}
        if os.path.isfile(path):
            with io.open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        data[_memory_key(doc, link_name)] = sorted(unique_ids)
        text = json.dumps(data, indent=1, ensure_ascii=False)
        with io.open(path, 'w', encoding='utf-8') as handle:
            handle.write(text if isinstance(text, unicode) else text.decode('utf-8'))
    except Exception:
        pass


# ======================================================================= boundary and coordinates
def _boundary_loops(room, location):
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = location
    try:
        loops = room.GetBoundarySegments(options)
    except Exception:
        return []
    out = []
    for loop in loops or []:
        points = []
        for segment in loop:
            try:
                points.extend(segment.GetCurve().Tessellate())
            except Exception:
                continue
        if len(points) >= 3:
            out.append(points)
    return out


def _shell_loop(room):
    """The outline of the room's 3D volume (Room.ClosedShell) as one loop - its plan convex hull. Follows the
    real walls (also turned ones), unlike the bounding box. [] when Revit gives no volume."""
    try:
        from Autodesk.Revit.DB import Solid
        shell = room.ClosedShell
        points = []
        for obj in shell or []:
            if isinstance(obj, Solid):
                for edge in obj.Edges:
                    try:
                        points.extend(edge.Tessellate())
                    except Exception:
                        continue
        hull = convex_hull(points)
        return [hull] if len(hull) >= 3 else []
    except Exception:
        return []


def _box_loop(room):
    """The room's bounding box as one rectangular loop (last resort) - [] when Revit has no box."""
    try:
        box = room.get_BoundingBox(None)
    except Exception:
        box = None
    if box is None:
        return []
    z = box.Min.Z
    return [[XYZ(box.Min.X, box.Min.Y, z), XYZ(box.Max.X, box.Min.Y, z),
             XYZ(box.Max.X, box.Max.Y, z), XYZ(box.Min.X, box.Max.Y, z)]]


def get_room_boundary(room):
    """([[XYZ, ...], ...], how) - the room's boundary loops in the LINK's coordinates, curves tessellated so arcs
    count with their full bulge. Tries the finish faces, then the wall centres, then the wall cores, then the
    room's bounding box (some rooms - e.g. bounded by elements of a nested link, or with a broken boundary -
    give no segments at all although they have an area). ([], '') when nothing works."""
    try:
        if room.Area <= 0:
            return [], u''
    except Exception:
        pass
    for location, how in ((SpatialElementBoundaryLocation.Finish, u'finish'),
                          (SpatialElementBoundaryLocation.Center, u'wall centre'),
                          (SpatialElementBoundaryLocation.CoreBoundary, u'wall core')):
        loops = _boundary_loops(room, location)
        if loops:
            return loops, how
    loops = _shell_loop(room)
    if loops:
        return loops, u'room volume'
    loops = _box_loop(room)
    return (loops, u'bounding box') if loops else ([], u'')


# ======================================================================= doors of the room
DOOR_WALL_DISTANCE_MM = 600.0                        # a door this close to the room boundary is in its wall


def get_link_doors(link_doc):
    """[(door, location XYZ)] - every placed door of the link (link coordinates)."""
    out = []
    for door in FilteredElementCollector(link_doc).OfCategory(BuiltInCategory.OST_Doors).WhereElementIsNotElementType():
        try:
            point = door.Location.Point
        except Exception:
            continue
        out.append((door, point))
    return out


def _door_width(door):
    """The door's width in feet (instance, then type parameter, then its bounding box)."""
    for owner in (door, getattr(door, 'Symbol', None)):
        if owner is None:
            continue
        for bip in (BuiltInParameter.DOOR_WIDTH, BuiltInParameter.FAMILY_WIDTH_PARAM):
            try:
                value = owner.get_Parameter(bip).AsDouble()
                if value > 0:
                    return value
            except Exception:
                continue
    try:
        box = door.get_BoundingBox(None)
        return max(box.Max.X - box.Min.X, box.Max.Y - box.Min.Y)
    except Exception:
        return 3.0


def _room_of(door, phase, which):
    try:
        room = door.get_FromRoom(phase) if which == 'from' else door.get_ToRoom(phase)
        if room is not None:
            return room
    except Exception:
        pass
    try:
        return door.FromRoom if which == 'from' else door.ToRoom
    except Exception:
        return None


def _boundary_gap(point, loops):
    """The plan distance (feet) from the point to the nearest boundary edge; 0 when the point is INSIDE a loop
    (a door on an inner / recessed wall of an L-shaped room lies inside the room outline)."""
    best = None
    for loop in loops:
        pts = [(p.X, p.Y) for p in loop]
        inside = False
        for a, b in zip(pts, pts[1:] + pts[:1]):
            d = _point_segment(point.X, point.Y, a[0], a[1], b[0], b[1])
            best = d if best is None or d < best else best
            if (a[1] > point.Y) != (b[1] > point.Y):
                x_cross = a[0] + (point.Y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                if point.X < x_cross:
                    inside = not inside
        if inside:
            return 0.0
    return best if best is not None else float('inf')


def _near_boundary(point, loops, limit_ft):
    """True when the point is inside the room outline or within limit_ft of it (plan)."""
    return _boundary_gap(point, loops) <= limit_ft


class DoorRecord(object):
    """A door of ANY loaded model, in HOST coordinates."""

    def __init__(self, door, point, hand, facing, source, same_model):
        self.door = door
        self.point = point
        self.hand = hand
        self.facing = facing
        self.source = source                          # model name (for the log)
        self.same_model = same_model                  # True: the rooms' own link - From / To Room can be asked
        self.Id = getattr(door, 'Id', None)


def get_all_doors(doc, room_link):
    """[DoorRecord] - the doors of the host model and of EVERY loaded link (doors are often modelled in another
    model than the rooms: an interiors / doors link, or the host itself), all in host coordinates."""
    sources = [(u'host model', doc, None, False)]
    for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
        try:
            link_doc = link.GetLinkDocument()
        except Exception:
            link_doc = None
        if link_doc is None:
            continue
        same = room_link is not None and link.Id == room_link.Id
        sources.append((u'{0}'.format(link.Name).split(u' : ')[0], link_doc, link.GetTotalTransform(), same))
    out = []
    for name, source_doc, transform, same in sources:
        try:
            doors = FilteredElementCollector(source_doc).OfCategory(BuiltInCategory.OST_Doors) \
                .WhereElementIsNotElementType()
        except Exception:
            continue
        for door in doors:
            try:
                point = door.Location.Point
            except Exception:
                continue
            try:
                hand, facing = door.HandOrientation, door.FacingOrientation
            except Exception:
                hand, facing = XYZ(1, 0, 0), XYZ(0, 1, 0)
            if transform is not None:
                point, hand, facing = transform.OfPoint(point), transform.OfVector(hand), transform.OfVector(facing)
            out.append(DoorRecord(door, point, hand, facing, name, same))
    return out


def room_door_points(room, raw_loops, doors, link_doc):
    """(points, count) - the plan area every door of the room needs, incl. its swing.
    raw_loops and the doors must be in the SAME coordinates (host: DoorRecords + transformed loops; the tests
    also pass (door, point) pairs in link coordinates).
    A door belongs to the room when Revit says so (From / To Room in the room's phase - only for doors of the
    rooms' own model) or when it sits in the room's wall (location inside the room outline or within
    DOOR_WALL_DISTANCE_MM of it, at the room's height).
    Per door: a rectangle along the wall of +-0.6 x width and +-width across it (swing either side)."""
    doors = [d if isinstance(d, DoorRecord) else
             DoorRecord(d[0], d[1], getattr(d[0], 'HandOrientation', XYZ(1, 0, 0)),
                        getattr(d[0], 'FacingOrientation', XYZ(0, 1, 0)), u'link', True) for d in doors]
    phase = None
    try:
        phase = link_doc.GetElement(room.get_Parameter(BuiltInParameter.ROOM_PHASE).AsElementId())
    except Exception:
        pass
    if phase is None:
        try:
            phase = link_doc.Phases[link_doc.Phases.Size - 1]           # the link's last phase
        except Exception:
            phase = None
    room_id = room.Id
    all_pts = [p for loop in raw_loops for p in loop]
    if not all_pts:
        return [], 0
    z0 = min(p.Z for p in all_pts)
    limit = DOOR_WALL_DISTANCE_MM * FT_PER_MM
    # quick plan box around the room (+ 1 m): only doors inside it are looked at - the link has thousands
    margin = 1000.0 * FT_PER_MM
    x0, x1 = min(p.X for p in all_pts) - margin, max(p.X for p in all_pts) + margin
    y0, y1 = min(p.Y for p in all_pts) - margin, max(p.Y for p in all_pts) + margin
    points, count = [], 0
    nearby = []                                      # (gap mm, dz mm, id, source, why) - for the trace log
    for rec in doors:
        door, loc = rec.door, rec.point
        if not (x0 <= loc.X <= x1 and y0 <= loc.Y <= y1):
            continue
        dz = loc.Z - z0
        mine, why = False, u''
        if rec.same_model:
            for which in ('from', 'to'):
                other = _room_of(door, phase, which)
                if other is not None and other.Id == room_id:
                    mine, why = True, which + u' room'
                    break
        gap = _boundary_gap(loc, raw_loops)
        if not mine and -1.0 <= dz <= 10.0 and gap <= limit:
            mine, why = True, u'in the wall'
        nearby.append((gap / FT_PER_MM, dz / FT_PER_MM, getattr(rec.Id, 'Value', rec.Id), rec.source, why or u'not used'))
        if not mine:
            continue
        count += 1
        width = _door_width(door)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            # along the wall: the opening (+ a little); across: the full leaf swing to either side
            points.append(loc.Add(rec.hand.Multiply(su * 0.6 * width)).Add(rec.facing.Multiply(sv * width)))
    # doors on the room's own floor first (a stack of the same door on every floor is otherwise all you see)
    nearby.sort(key=lambda item: (not (-305.0 <= item[1] <= 3050.0), item[0]))
    trace(u'    doors near the room: {0} ({1} on this floor)'.format(
        len(nearby), sum(1 for item in nearby if -305.0 <= item[1] <= 3050.0)))
    for gap_mm, dz_mm, door_id, source, why in nearby[:8]:
        trace(u'      door {0} ({1}): {2:.0f} mm from the outline, {3:.0f} mm above the room floor - {4}'.format(
            door_id, source, gap_mm, dz_mm, why))
    if not nearby and doors:
        # nothing near: show where the closest doors are, so a coordinate / model problem can be seen
        cx = sum(p.X for p in all_pts) / len(all_pts)
        cy = sum(p.Y for p in all_pts) / len(all_pts)
        trace(u'    room outline centre x {0:.1f} m, y {1:.1f} m, floor z {2:.1f} m'.format(
            cx * 0.3048, cy * 0.3048, z0 * 0.3048))
        closest = sorted(doors, key=lambda r: math.hypot(r.point.X - cx, r.point.Y - cy))[:3]
        for rec in closest:
            trace(u'      nearest door {0} ({1}): {2:.1f} m away, z {3:.1f} m'.format(
                getattr(rec.Id, 'Value', rec.Id), rec.source,
                math.hypot(rec.point.X - cx, rec.point.Y - cy) * 0.3048, rec.point.Z * 0.3048))
    return points, count


def transform_boundary(loops, transform):
    """The boundary in HOST coordinates: every point through the link instance's total transform
    (translation, rotation, shared coordinates)."""
    if transform is None:
        return [list(loop) for loop in loops]
    return [[transform.OfPoint(p) for p in loop] for loop in loops]


def calculate_crop(points, right, up, offset_ft):
    """(u0, v0, u1, v1) - the extents of the points along the view's right / up directions, grown by the offset.
    Measured in the view's own axes, so the crop fits the room also in a view turned to True North."""
    us = [p.X * right.X + p.Y * right.Y + p.Z * right.Z for p in points]
    vs = [p.X * up.X + p.Y * up.Y + p.Z * up.Z for p in points]
    return (min(us) - offset_ft, min(vs) - offset_ft, max(us) + offset_ft, max(vs) + offset_ft)


def crop_loop(extents, right, up, z):
    """The crop rectangle as a CurveLoop in model coordinates, on the height z."""
    u0, v0, u1, v1 = extents

    def at(u, v):
        p = right.Multiply(u).Add(up.Multiply(v))
        return XYZ(p.X, p.Y, z)
    corners = [at(u0, v0), at(u1, v0), at(u1, v1), at(u0, v1)]
    loop = CurveLoop()
    for a, b in zip(corners, corners[1:] + corners[:1]):
        loop.Append(Line.CreateBound(a, b))
    return loop


# ======================================================================= levels
def host_level(doc, room, transform):
    """(host Level, distance in mm) - the host level nearest to the room's level height after the link
    transform; a level of the same name wins when it is as near."""
    try:
        linked_level = room.Level
        height = linked_level.ProjectElevation
        name = linked_level.Name
    except Exception:
        linked_level, height, name = None, 0.0, u''
    z = transform.OfPoint(XYZ(0, 0, height)).Z if transform is not None else height
    best = None
    for level in FilteredElementCollector(doc).OfClass(Level):
        distance = abs(level.ProjectElevation - z)
        same_name = 0 if (name and level.Name == name) else 1
        key = (round(distance, 3), same_name)
        if best is None or key < best[0]:
            best = (key, level, distance)
    if best is None:
        return None, None
    return best[1], best[2] / FT_PER_MM


# ======================================================================= views
def clean_name(text):
    """A view name part Revit accepts: upper case, no invalid characters, spaces as '-'."""
    text = INVALID_NAME_CHARS.sub(u'', _normal(text))
    text = re.sub(r'[\s_]+', u'-', text)
    return re.sub(r'-{2,}', u'-', text).strip(u'-')


def view_name(room, index):
    """EL-{ROOM NAME}-{ROOM NUMBER} (the automatic index when the room has no number)."""
    number = clean_name(room_number(room)) or u'{0:02d}'.format(index)
    return u'{0}-{1}-{2}'.format(VIEW_PREFIX, clean_name(room_name(room)), number)


def floor_plan_type(doc):
    for view_type in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if view_type.ViewFamily == ViewFamily.FloorPlan:
            return view_type
    return None


def plan_templates(doc):
    """[(name, view)] of the view templates for floor plans."""
    out = []
    for view in FilteredElementCollector(doc).OfClass(ViewPlan):
        try:
            if view.IsTemplate and view.ViewType == ViewType.FloorPlan:
                out.append((view.Name, view))
        except Exception:
            continue
    return sorted(out, key=lambda item: item[0].lower())


def find_existing_view(doc, name):
    """The floor plan (not a template) of that name, or None. Case does not matter (as in Revit)."""
    wanted = name.lower()
    for view in FilteredElementCollector(doc).OfClass(ViewPlan):
        try:
            if not view.IsTemplate and view.Name.lower() == wanted:
                return view
        except Exception:
            continue
    return None


def create_floor_plan(doc, level, name, plan_type):
    view = ViewPlan.Create(doc, plan_type.Id, level.Id)
    view.Name = name
    return view


def apply_view_template(view, template):
    """The template (or none). Done after the scale, so a template that controls the scale wins."""
    if template is not None:
        view.ViewTemplateId = template.Id


def _set_scale(view, scale):
    try:
        if view.Scale != scale:
            view.Scale = scale
        return True
    except Exception:
        return False                                  # the view template controls the scale


def crop_box(extents, right, up, points):
    """The crop as a BoundingBoxXYZ in the view's own axes (origin 0, X = right, Y = up, Z = towards the viewer),
    the same (u, v) frame as calculate_crop."""
    u0, v0, u1, v1 = extents
    t = Transform.Identity
    t.Origin = XYZ.Zero
    t.BasisX = right
    t.BasisY = up
    t.BasisZ = right.CrossProduct(up)
    zs = [p.Z for p in points]
    box = BoundingBoxXYZ()
    box.Transform = t
    box.Min = XYZ(u0, v0, min(zs) - 30.0)
    box.Max = XYZ(u1, v1, max(zs) + 30.0)
    return box


def wall_angle(loops, right, up, min_edge_ft=0.3):
    """The direction of the room's walls, as an angle (radians, -45..45 deg) from the view's right direction.
    Every boundary edge votes with its length for its angle modulo 90 deg (so the walls of a rectangular room
    at 0 / 90 / 180 / 270 deg all agree); the strongest half-degree bucket wins, refined by its weighted mean.
    0.0 when the walls are already square to the view (within 0.5 deg) or there are no usable edges."""
    buckets = {}
    for loop in loops:
        pts = [(p.X * right.X + p.Y * right.Y + p.Z * right.Z, p.X * up.X + p.Y * up.Y + p.Z * up.Z) for p in loop]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length < min_edge_ft:
                continue
            deg = math.degrees(math.atan2(dy, dx)) % 90.0
            if deg >= 45.0:
                deg -= 90.0                              # -45 .. 45
            key = int(round(deg * 2.0))
            if key >= 90:                                 # 44.75 .. 45 deg is the same wall as -45
                key -= 180
                deg -= 90.0
            total, weighted = buckets.get(key, (0.0, 0.0))
            buckets[key] = (total + length, weighted + length * deg)
    if not buckets:
        return 0.0
    key = max(buckets, key=lambda k: buckets[k][0])
    # neighbour buckets belong to the same wall direction (tessellated / slightly off walls)
    total = sum(buckets.get(k, (0.0, 0.0))[0] for k in (key - 1, key, key + 1))
    weighted = sum(buckets.get(k, (0.0, 0.0))[1] for k in (key - 1, key, key + 1))
    deg = weighted / total if total else 0.0
    return 0.0 if abs(deg) < 0.5 else math.radians(deg)


def convex_hull(points):
    """The convex hull (XY, counter-clockwise) of XYZ points, as a list of XYZ on the lowest Z."""
    if not points:
        return []
    z = min(p.Z for p in points)
    pts = sorted(set((round(p.X, 6), round(p.Y, 6)) for p in points))
    if len(pts) < 3:
        return [XYZ(x, y, z) for x, y in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [XYZ(x, y, z) for x, y in lower[:-1] + upper[:-1]]


def _turn(right, up, angle):
    """The pair of plan directions turned by the angle inside the (right, up) frame."""
    c, s = math.cos(angle), math.sin(angle)
    d1 = right.Multiply(c).Add(up.Multiply(s))
    d2 = up.Multiply(c).Subtract(right.Multiply(s))
    return d1, d2


def _hide_links(view):
    """Hide every Revit link in the NEW view while its crop is set (Revit then does not draw ~all links of the
    whole floor first). Returns the hidden ids, to show them again right after."""
    from System.Collections.Generic import List
    ids = List[ElementId]()
    try:
        for link in FilteredElementCollector(view.Document).OfClass(RevitLinkInstance):
            try:
                if link.CanBeHidden(view) and not link.IsHidden(view):
                    ids.Add(link.Id)
            except Exception:
                continue
        if ids.Count:
            view.HideElements(ids)
    except Exception as error:
        trace(u'    could not hide links ({0})'.format(error))
        return None
    return ids


def _unhide(view, ids):
    if ids is not None and ids.Count:
        try:
            view.UnhideElements(ids)
        except Exception as error:
            trace(u'    could not show links again ({0})'.format(error))


# ======================================================================= crop that follows the room boundary
MIN_EDGE_FT = 2.0 * FT_PER_MM                        # crop edges shorter than this are merged away
COLLINEAR_FT = 1.0 * FT_PER_MM                       # a corner this close to the line through its neighbours goes


def _area(pts):
    """Signed plan area (shoelace) - positive when the points run counter-clockwise."""
    total = 0.0
    for a, b in zip(pts, pts[1:] + pts[:1]):
        total += a.X * b.Y - b.X * a.Y
    return total / 2.0


def _inside(point, pts):
    inside = False
    for a, b in zip(pts, pts[1:] + pts[:1]):
        if (a.Y > point.Y) != (b.Y > point.Y):
            if point.X < a.X + (point.Y - a.Y) * (b.X - a.X) / (b.Y - a.Y):
                inside = not inside
    return inside


def _clean_points(points, z):
    """The points flat on height z, without duplicates, tiny edges and straight-line corners, counter-clockwise."""
    pts = []
    for p in points:
        q = XYZ(p.X, p.Y, z)
        if pts and q.DistanceTo(pts[-1]) < MIN_EDGE_FT:
            continue
        pts.append(q)
    while len(pts) > 2 and pts[0].DistanceTo(pts[-1]) < MIN_EDGE_FT:
        pts.pop()
    changed = True
    while changed and len(pts) > 3:
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            if _point_segment(b.X, b.Y, a.X, a.Y, c.X, c.Y) < COLLINEAR_FT:
                del pts[i]
                changed = True
                break
    if len(pts) >= 3 and _area(pts) < 0:
        pts.reverse()
    return pts


def _loop_from_points(pts):
    loop = CurveLoop()
    for a, b in zip(pts, pts[1:] + pts[:1]):
        loop.Append(Line.CreateBound(a, b))
    return loop


def _loop_points(loop):
    """The corner points of a CurveLoop (arcs tessellated)."""
    pts = []
    for curve in loop:
        if isinstance(curve, Line):
            pts.append(curve.GetEndPoint(0))
        else:
            pts.extend(list(curve.Tessellate())[:-1])
    return pts


def _offset_outward(pts, offset_ft):
    """The (counter-clockwise) outline grown by the offset on every side, or None when Revit cannot offset it."""
    if offset_ft <= 1e-6:
        return pts
    loop = _loop_from_points(pts)
    base = abs(_area(pts))
    for sign in (1.0, -1.0):                          # Revit's offset side depends on the loop direction
        try:
            grown = CurveLoop.CreateViaOffset(loop, sign * offset_ft, XYZ.BasisZ)
        except Exception:
            continue
        out = _loop_points(grown)
        if len(out) >= 3 and abs(_area(out)) > base:
            return out
    return None


def _prism(pts):
    from Autodesk.Revit.DB import GeometryCreationUtilities
    from System.Collections.Generic import List
    loops = List[CurveLoop]()
    loops.Add(_loop_from_points(pts))
    return GeometryCreationUtilities.CreateExtrusionGeometry(loops, XYZ.BasisZ, 1.0)


def boundary_crop_points(loops, offset_ft, z, extra=None):
    """(outline points, note) - ONE outline that follows the room boundary at the crop offset: every room's outer
    loop grown by the offset, joined (substation rooms) with the door-swing rectangles (extra: 4 corners per door).
    Holes (columns, shafts inside the room) are ignored. (None, reason) when it cannot be made."""
    from Autodesk.Revit.DB import BooleanOperationsType, BooleanOperationsUtils, PlanarFace
    flat = [p for p in (_clean_points(loop, z) for loop in loops) if len(p) >= 3 and abs(_area(p)) > 1e-4]
    # keep the OUTER loops only: a loop inside a bigger loop is a hole of that room
    flat.sort(key=lambda p: -abs(_area(p)))
    outer = []
    for pts in flat:
        if not any(_inside(pts[0], big) for big in outer):
            outer.append(pts)
    if not outer:
        return None, u'no usable boundary loop'
    solid = None
    for pts in outer:
        grown = _offset_outward(pts, offset_ft)
        if grown is None:
            return None, u'the room outline could not be offset by the crop offset'
        grown = _clean_points(grown, z)
        try:
            piece = _prism(grown)
            solid = piece if solid is None else BooleanOperationsUtils.ExecuteBooleanOperation(
                solid, piece, BooleanOperationsType.Union)
        except Exception as error:
            return None, u'the room outlines could not be joined ({0})'.format(error)
    doors_added = 0
    for k in range(0, len(extra or []) - 3, 4):
        rect = _clean_points(extra[k:k + 4], z)
        if len(rect) < 3:
            continue
        try:
            solid = BooleanOperationsUtils.ExecuteBooleanOperation(solid, _prism(rect), BooleanOperationsType.Union)
            doors_added += 1
        except Exception as error:
            trace(u'    door swing {0} not joined to the crop ({1})'.format(k // 4 + 1, error))
    bottoms = [f for f in solid.Faces if isinstance(f, PlanarFace) and f.FaceNormal.Z < -0.999]
    if len(bottoms) != 1:
        return None, u'the rooms are {0} separate areas - one crop cannot follow them all'.format(len(bottoms))
    best = None
    for loop in bottoms[0].GetEdgesAsCurveLoops():
        pts = _clean_points(_loop_points(loop), z)
        if len(pts) >= 3 and (best is None or abs(_area(pts)) > abs(_area(best))):
            best = pts
    if best is None:
        return None, u'no outline found'
    trace(u'    boundary crop: {0} corner(s), {1} room loop(s), {2} door swing(s)'.format(
        len(best), len(outer), doors_added))
    return best, u''


def set_view_crop(view, loops, offset_ft, align=True, extra=None, follow=False, notes=None):
    """The crop region around the room boundary loops (host coordinates) + offset.

    follow=True: the crop SHAPE follows the room boundary (L-shaped rooms, substation groups), offset on every
    side by the crop offset, with the door swings joined in. When that is not possible the rectangle below is
    used and the reason goes into notes.

    align=True: the crop rectangle is turned to the room's walls (wall_angle), so a room that is not square to
    Project North gets a crop parallel to its walls instead of a larger, skewed box.

    SPEED: a NEW plan has never had a crop. Turning CropBoxActive on first makes Revit work out the default crop
    from the extents of EVERYTHING visible - the whole host model and every link. So the crop box is SET FIRST
    (and the links hidden meanwhile), then switched on, then the turned shape is applied."""
    points = [p for loop in loops for p in loop]
    right, up = view.RightDirection, view.UpDirection
    angle = wall_angle(loops, right, up) if align else 0.0      # from the walls only, not the doors
    d1, d2 = _turn(right, up, angle) if angle else (right, up)
    extents = calculate_crop(points, d1, d2, offset_ft)
    if extra:
        # the doors and their swings: the crop grows to include them (+ a small margin), never shrinks
        u0, v0, u1, v1 = extents
        e0, f0, e1, f1 = calculate_crop(extra, d1, d2, 100.0 * FT_PER_MM)
        extents = (min(u0, e0), min(v0, f0), max(u1, e1), max(v1, f1))
    z = view.GenLevel.ProjectElevation if view.GenLevel is not None else points[0].Z
    shape = crop_loop(extents, d1, d2, z)
    trace(u'    wall angle {0:.2f} deg'.format(math.degrees(angle)))
    custom = bool(angle)                              # the shape is not the plain view-aligned box
    if follow:
        outline, why = None, u''
        try:
            outline, why = boundary_crop_points(loops, offset_ft, z, extra)
        except Exception as error:
            why = u'{0}'.format(error)
        if outline is not None:
            candidate = _loop_from_points(outline)
            try:
                valid = view.GetCropRegionShapeManager().IsCropRegionShapeValid(candidate)
            except Exception:
                valid = True
            if valid:
                shape, custom = candidate, True
            else:
                why = u'Revit does not accept the room outline as a crop shape'
                outline = None
        if outline is None:
            trace(u'    boundary crop not possible: {0} - rectangle instead'.format(why))
            if notes is not None:
                notes.append(u'crop could not follow the room boundary ({0}) - a rectangle was used'.format(why))
    if not view.CropBoxActive:
        hidden = _hide_links(view)
        trace(u'    {0} link(s) hidden while cropping'.format(hidden.Count if hidden is not None else 0))
        try:
            # the axis-aligned box around the crop shape - fast first crop
            corners = [c.GetEndPoint(0) for c in shape]
            box_extents = calculate_crop(corners, right, up, 0.0)
            view.CropBox = crop_box(box_extents, right, up, points)
            trace(u'    crop box assigned')
            view.CropBoxActive = True
            trace(u'    crop switched on')
        except Exception as error:
            trace(u'    crop box failed ({0}) - crop shape instead'.format(error))
            view.CropBoxActive = True
        if custom:
            view.GetCropRegionShapeManager().SetCropShape(shape)
            trace(u'    crop shape set')
        _unhide(view, hidden)
        trace(u'    links shown again')
        return extents
    view.GetCropRegionShapeManager().SetCropShape(shape)
    return extents


def update_existing_view(view, template, scale, loops, offset_ft, align=True, extra=None, follow=False, notes=None):
    extents = set_view_crop(view, loops, offset_ft, align, extra, follow, notes)
    _set_scale(view, scale)
    apply_view_template(view, template)
    return extents


# ======================================================================= substation: neighbouring rooms
def room_label(room, target=u''):
    return u'{0} - {1}'.format(room_name(room) or target, room_number(room) or u'(no number)')


def _level_key(room):
    """Rooms are merged only on the same (linked) level."""
    try:
        level = room.Level
        return (getattr(level.Id, 'Value', None), round(level.ProjectElevation, 3))
    except Exception:
        return (None, 0.0)


def _point_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    cx, cy = ax + t * dx - px, ay + t * dy - py
    return math.sqrt(cx * cx + cy * cy)


def _segments_cross(a, b, c, d):
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    s1, s2, s3, s4 = side(a, b, c), side(a, b, d), side(c, d, a), side(c, d, b)
    return ((s1 > 0) != (s2 > 0)) and ((s3 > 0) != (s4 > 0)) and s1 * s2 != 0 and s3 * s4 != 0


def _segments(loops):
    out = []
    for loop in loops:
        pts = [(float(p.X), float(p.Y)) for p in loop]
        for a, b in zip(pts, pts[1:] + pts[:1]):
            if a != b:
                out.append((a, b))
    return out


def _box(loops):
    xs = [p.X for loop in loops for p in loop]
    ys = [p.Y for loop in loops for p in loop]
    return min(xs), min(ys), max(xs), max(ys)


def boundary_distance(loops_a, loops_b, limit=None):
    """The shortest plan distance (feet) between two room boundaries; 0 when they touch or cross.
    With a limit: returns early as soon as the boxes are further apart than it."""
    a0, b0 = _box(loops_a), _box(loops_b)
    gap_x = max(0.0, max(a0[0], b0[0]) - min(a0[2], b0[2]))
    gap_y = max(0.0, max(a0[1], b0[1]) - min(a0[3], b0[3]))
    box_gap = math.sqrt(gap_x * gap_x + gap_y * gap_y)
    if limit is not None and box_gap > limit:
        return box_gap
    best = None
    seg_b = _segments(loops_b)
    for a, b in _segments(loops_a):
        for c, d in seg_b:
            if _segments_cross(a, b, c, d):
                return 0.0
            for value in (_point_segment(a[0], a[1], c[0], c[1], d[0], d[1]), _point_segment(b[0], b[1], c[0], c[1], d[0], d[1]),
                          _point_segment(c[0], c[1], a[0], a[1], b[0], b[1]), _point_segment(d[0], d[1], a[0], a[1], b[0], b[1])):
                if best is None or value < best:
                    best = value
    return best if best is not None else box_gap


def group_nearby_rooms(placed, gap_ft):
    """[[item, ...], ...] - the rooms (items: (room, target, label, index, loops)) joined in groups: two rooms on
    the same level whose boundaries are at most gap_ft apart are in one group (also through a third room:
    MVSG - TX - LV in a row is one substation). Groups keep the order of the first room."""
    count = len(placed)
    parent = list(range(count))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(count):
        for j in range(i + 1, count):
            if _level_key(placed[i][0]) != _level_key(placed[j][0]):
                continue
            if root(i) == root(j):
                continue
            if boundary_distance(placed[i][4], placed[j][4], gap_ft) <= gap_ft + 1e-9:
                parent[root(j)] = root(i)
    groups, order = {}, []
    for i in range(count):
        r = root(i)
        if r not in groups:
            groups[r] = []
            order.append(r)
        groups[r].append(placed[i])
    return [groups[r] for r in order]


def substation_name(rooms, index):
    """EL-SUBSTATION-{lowest room number of the group} - the same rooms give the same name next time
    (so the view is updated, not made twice). The automatic index when no room has a number."""
    numbers = sorted(clean_name(room_number(r)) for r in rooms if clean_name(room_number(r)))
    return u'{0}-{1}-{2}'.format(VIEW_PREFIX, SUBSTATION_NAME, numbers[0] if numbers else u'{0:02d}'.format(index))


# ======================================================================= the run
class Result(object):
    def __init__(self):
        self.found = 0
        self.created = []                             # view names
        self.updated = []
        self.skipped = []                             # (room label, reason)
        self.errors = []                              # (room label, error)
        self.warnings = []                            # (room label, warning)
        self.merged = []                              # (substation view name, [room labels])


def generate(doc, link, link_doc, selection, scale, offset_mm, template, create_missing, update_existing,
             merge=False, merge_gap_mm=DEFAULT_MERGE_GAP_MM, align=True, include_doors=True, follow=True):
    """Create / update the views. Returns a Result. One transaction per view inside one group (one Undo):
    a view that fails is rolled back on its own and the others go on.
    selection: the rooms of the link the user ticked (room elements) - or room-name suggestions (text).
    merge=True: neighbouring rooms on the same level (gap <= merge_gap_mm) share one SUBSTATION view."""
    result = Result()
    trace(u'Enlarged Electrical Rooms: {0} room(s), scale 1:{1}, offset {2} mm, template {3}, merge {4}'.format(
        len(selection or []), scale, offset_mm, template.Name if template is not None else u'-', merge), new=True)
    transform = link.GetTotalTransform()
    plan_type = floor_plan_type(doc)
    if plan_type is None:
        result.errors.append((u'-', u'This model has no floor plan view type.'))
        return result
    selection = list(selection or [])
    if selection and isinstance(selection[0], basestring):
        rooms = filter_target_rooms(get_rooms_from_link(link_doc), selection)
    else:
        rooms = sort_rooms([(room, room_name(room) or u'ROOM') for room in selection])
    result.found = len(rooms)
    offset_ft = offset_mm * FT_PER_MM
    used_names = set()
    index_of = {}

    def unique(name):
        # the same name twice in this run (two rooms with the same name and number): numbered
        base, n = name, 2
        while name.lower() in used_names:
            name = u'{0}-{1}'.format(base, n)
            n += 1
        used_names.add(name.lower())
        return name

    # ---- 1. every room with its boundary in host coordinates
    placed = []                                       # (room, target, label, index, loops, door points)
    doors = None                                      # the link's doors, read once when first needed
    for room, target in rooms:
        label = room_label(room, target)
        index_of[target] = index_of.get(target, 0) + 1
        raw, how = get_room_boundary(room)
        loops = transform_boundary(raw, transform)
        trace(u'room {0}: boundary from {1}'.format(label, how or u'NOTHING'))
        door_pts = []
        if include_doors and raw:
            try:
                if doors is None:
                    doors = get_all_doors(doc, link)
                    per_model = {}
                    for rec in doors:
                        per_model[rec.source] = per_model.get(rec.source, 0) + 1
                    trace(u'{0} door(s) in all models: {1}'.format(len(doors), u', '.join(
                        u'{0} {1}'.format(n, k) for k, n in sorted(per_model.items()))))
                # host coordinates: the doors may come from any model
                door_pts, n_doors = room_door_points(room, loops, doors, link_doc)
                trace(u'    {0} door(s) of this room kept inside the crop'.format(n_doors))
                if not n_doors:
                    result.warnings.append((label, u'no door found for this room - check the crop shows its entrance'))
            except Exception as error:
                # a door problem must never stop the view: crop without the doors, and say why
                import traceback
                trace(u'    DOOR STEP FAILED: {0}\n{1}'.format(error, traceback.format_exc()))
                result.warnings.append((label, u'doors could not be read ({0}) - crop made without them'.format(error)))
                door_pts = []
        if how and how != u'finish':
            result.warnings.append((label, u'no finish-face boundary - crop taken from the {0}; check the view'.format(how)))
        if not any(loops):
            result.warnings.append((label, u'WARNING: Room boundary could not be determined (not placed or not enclosed).'))
            result.skipped.append((label, u'no room boundary'))
            continue
        placed.append((room, target, label, index_of[target], loops, door_pts))

    # ---- 2. the views to make: one per room, or one per group of neighbouring rooms (substation)
    jobs = []                                         # (label, view name, room for the level, boundary loops)
    groups = group_nearby_rooms(placed, merge_gap_mm * FT_PER_MM) if merge else [[item] for item in placed]
    for members in groups:
        loops = [loop for item in members for loop in item[4]]
        extra = [p for item in members for p in item[5]]
        if len(members) == 1:
            room, target, label, index = members[0][:4]
            jobs.append((label, unique(view_name(room, index)), room, loops, extra))
        else:
            name = unique(substation_name([m[0] for m in members], len(result.merged) + 1))
            labels = [m[2] for m in members]
            result.merged.append((name, labels))
            jobs.append((u'{0} ({1} rooms)'.format(name, len(members)), name, members[0][0], loops, extra))

    group = TransactionGroup(doc, u'Enlarged Electrical Rooms')
    group.Start()
    try:
        for label, name, room, loops, extra in jobs:
            existing = find_existing_view(doc, name)
            if existing is not None and not update_existing:
                result.skipped.append((label, u'Existing view found ({0})'.format(name)))
                continue
            if existing is None and not create_missing:
                result.skipped.append((label, u'No view yet and "Create Missing Views" is off ({0})'.format(name)))
                continue
            started = time.time()
            trace(u'{0}: {1}'.format(u'update' if existing is not None else u'create', name))
            transaction = Transaction(doc, u'Enlarged room view ' + name)
            transaction.Start()
            notes = []
            try:
                if existing is None:
                    level, off_mm = host_level(doc, room, transform)
                    if level is None:
                        raise ValueError(u'this model has no levels')
                    if off_mm > LEVEL_WARNING_MM:
                        result.warnings.append((label, u'nearest level "{0}" is {1:.0f} mm from the room\'s level'.format(level.Name, off_mm)))
                    view = create_floor_plan(doc, level, name, plan_type)
                    trace(u'    view created on level {0}'.format(level.Name))
                    # crop FIRST (before the template, which may control the crop, and before anything makes
                    # Revit draw the uncropped plan of the whole building)
                    set_view_crop(view, loops, offset_ft, align, extra, follow, notes)
                    _set_scale(view, scale)
                    apply_view_template(view, template)
                    trace(u'    scale / template set')
                    result.created.append(name)
                else:
                    view = existing
                    update_existing_view(existing, template, scale, loops, offset_ft, align, extra, follow, notes)
                    result.updated.append(name)
                for note in notes:
                    result.warnings.append((label, note))
                if not view.CropBoxActive:
                    result.warnings.append((label, u'the view template "{0}" switches Crop View OFF - the view shows the '
                                                   u'whole floor (slow). In that template untick "Include" for Crop View, '
                                                   u'or pick another template.'.format(template.Name if template else u'-')))
                transaction.Commit()
                trace(u'    committed ({0:.1f} s)'.format(time.time() - started))
            except Exception as error:
                if transaction.HasStarted() and not transaction.HasEnded():
                    transaction.RollBack()
                trace(u'    ERROR {0}'.format(error))
                result.errors.append((label, u'{0}'.format(error).strip().split(u'\n')[0]))
        group.Assimilate()
        trace(u'done: {0} created, {1} updated, {2} skipped, {3} errors'.format(
            len(result.created), len(result.updated), len(result.skipped), len(result.errors)))
    except Exception as error:
        group.RollBack()
        trace(u'ROLLED BACK {0}'.format(error))
        result.errors.append((u'-', u'Nothing was changed: {0}'.format(error)))
    return result


def generate_report(result):
    line = u'=' * 40
    out = [line, u'Electrical Enlarged Room Generator', line, u'',
           u'Rooms Found:     {0:>5}'.format(result.found),
           u'Views Created:   {0:>5}'.format(len(result.created)),
           u'Views Updated:   {0:>5}'.format(len(result.updated)),
           u'Views Skipped:   {0:>5}'.format(len(result.skipped)),
           u'Errors:          {0:>5}'.format(len(result.errors)), u'', line]
    if result.merged:
        out += [u'', u'Substation views (merged rooms):']
        for name, labels in result.merged:
            out += [u'  ' + name] + [u'    + ' + l for l in labels]
    if result.created:
        out += [u'', u'Created:'] + [u'  ' + n for n in result.created]
    if result.updated:
        out += [u'', u'Updated:'] + [u'  ' + n for n in result.updated]
    if result.skipped:
        out += [u'', u'Skipped:'] + [u'  {0}\n    {1}'.format(label, reason) for label, reason in result.skipped]
    if result.warnings:
        out += [u'', u'Warnings:'] + [u'  {0}\n    {1}'.format(label, text) for label, text in result.warnings]
    if result.errors:
        out += [u'', u'Errors:'] + [u'  {0}\n    {1}'.format(label, text) for label, text in result.errors]
    out += [u'', line]
    return u'\n'.join(out)


# ======================================================================= the window
XAML = u'''<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Enlarged Electrical Rooms" Width="640" SizeToContent="Height" ResizeMode="NoResize"
        WindowStartupLocation="CenterScreen" FontFamily="Segoe UI" FontSize="12" Background="#F4F6F8">
    <StackPanel Margin="16">
        <TextBlock Text="Architectural Link" FontWeight="Bold" Margin="0,0,0,4"/>
        <ComboBox x:Name="link_cb" Margin="0,0,0,12"/>
        <TextBlock Text="Rooms (from the link)   Level  |  Number  |  Name" FontWeight="Bold" Margin="0,0,0,4"/>
        <Grid Margin="0,0,0,6">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <TextBox x:Name="search_tb" Grid.Column="0" Height="24" VerticalContentAlignment="Center"
                     ToolTip="Search level, number or name - e.g. TX, LV, MVSG, L01"/>
            <ComboBox x:Name="level_cb" Grid.Column="1" Width="110" Margin="6,0,0,0" ToolTip="Show the rooms of one level"/>
            <Button x:Name="all_btn" Grid.Column="2" Content="Tick shown" Width="72" Margin="6,0,0,0"/>
            <Button x:Name="none_btn" Grid.Column="3" Content="Untick shown" Width="80" Margin="6,0,0,0"/>
            <Button x:Name="suggest_btn" Grid.Column="4" Content="Suggest" Width="60" Margin="6,0,0,0"
                    ToolTip="Tick the rooms whose name looks electrical (TARGET_ROOMS at the top of the script)"/>
            <Button x:Name="last_btn" Grid.Column="5" Content="Last run" Width="60" Margin="6,0,0,0"
                    ToolTip="Tick the rooms of the last GENERATE in this project"/>
        </Grid>
        <Border BorderBrush="#C9D1DA" BorderThickness="1" Background="White" Margin="0,0,0,4" Height="240">
            <ScrollViewer VerticalScrollBarVisibility="Auto">
                <StackPanel x:Name="rooms_sp" Margin="8,6"/>
            </ScrollViewer>
        </Border>
        <CheckBox x:Name="ticked_only_chk" Content="Show ticked rooms only" Margin="0,0,0,12"/>
        <Grid Margin="0,0,0,12">
            <Grid.ColumnDefinitions>
                <ColumnDefinition Width="*"/>
                <ColumnDefinition Width="12"/>
                <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <StackPanel Grid.Column="0">
                <TextBlock Text="View Scale" FontWeight="Bold" Margin="0,0,0,4"/>
                <ComboBox x:Name="scale_cb"/>
            </StackPanel>
            <StackPanel Grid.Column="2">
                <TextBlock Text="Crop Offset (mm)" FontWeight="Bold" Margin="0,0,0,4"/>
                <ComboBox x:Name="offset_cb" IsEditable="True"/>
            </StackPanel>
        </Grid>
        <TextBlock Text="View Template" FontWeight="Bold" Margin="0,0,0,4"/>
        <ComboBox x:Name="template_cb" Margin="0,0,0,12"/>
        <TextBlock Text="Options" FontWeight="Bold" Margin="0,0,0,4"/>
        <CheckBox x:Name="create_chk" Content="Create Missing Views" IsChecked="True" Margin="0,0,0,4"/>
        <CheckBox x:Name="update_chk" Content="Update Existing Views" IsChecked="True" Margin="0,0,0,4"/>
        <CheckBox x:Name="follow_chk" Content="Crop follows the room boundary (L-shapes, substations) at the crop offset" IsChecked="True" Margin="0,0,0,4"/>
        <CheckBox x:Name="align_chk" Content="Rectangle crop: align to the room walls (rooms not square to North)" IsChecked="True" Margin="0,0,0,4"/>
        <CheckBox x:Name="doors_chk" Content="Show the room's doors fully (door swings outside the room)" IsChecked="True" Margin="0,0,0,10"/>
        <Border BorderBrush="#C9D1DA" BorderThickness="1" Background="White" Padding="8,6" Margin="0,0,0,14">
            <StackPanel>
                <CheckBox x:Name="merge_chk" Content="Substation view: merge nearby rooms into one view" FontWeight="Bold"/>
                <TextBlock Foreground="#5F5F5F" TextWrapping="Wrap" Margin="20,2,0,6"
                           Text="Ticked rooms on the same level that are next to each other (e.g. MVSG + TX + LV) get ONE view: EL-SUBSTATION-{lowest room number}. A room with no neighbour keeps its own view."/>
                <StackPanel Orientation="Horizontal" Margin="20,0,0,0">
                    <TextBlock Text="Max gap between rooms (mm)" VerticalAlignment="Center" Margin="0,0,8,0"/>
                    <ComboBox x:Name="gap_cb" IsEditable="True" Width="90"/>
                </StackPanel>
            </StackPanel>
        </Border>
        <Button x:Name="generate_btn" Content="GENERATE" Height="34" FontWeight="Bold" Background="#2E7D32" Foreground="White"/>
        <TextBlock x:Name="status_tb" Foreground="#5F5F5F" TextWrapping="Wrap" Margin="0,8,0,0"/>
    </StackPanel>
</Window>'''


def _window_class():
    from pyrevit import forms

    class EnlargedRoomsWindow(forms.WPFWindow):
        def __init__(self, doc):
            forms.WPFWindow.__init__(self, XAML, literal_string=True)
            from System.Windows import Thickness
            from System.Windows.Controls import CheckBox
            self.doc = doc
            self.choice = None
            self.links = get_revit_links(doc)
            for name, _link, _ldoc in self.links:
                self.link_cb.Items.Add(name)
            self.boxes = []                                # (room, CheckBox) of the chosen link
            self._filling = False
            self.search_tb.TextChanged += self._filter
            self.level_cb.SelectionChanged += self._filter
            self.ticked_only_chk.Checked += self._filter
            self.ticked_only_chk.Unchecked += self._filter
            self.all_btn.Click += lambda s, a: self._tick_shown(True)
            self.none_btn.Click += lambda s, a: self._tick_shown(False)
            self.suggest_btn.Click += self._suggest
            self.last_btn.Click += self._last_run
            self.remembered = None
            for scale in SCALES:
                self.scale_cb.Items.Add(u'1:{0}'.format(scale))
            self.scale_cb.SelectedIndex = SCALES.index(DEFAULT_SCALE)
            for offset in CROP_OFFSETS_MM:
                self.offset_cb.Items.Add(u'{0}'.format(offset))
            self.offset_cb.Text = u'{0}'.format(DEFAULT_OFFSET_MM)
            for gap in MERGE_GAPS_MM:
                self.gap_cb.Items.Add(u'{0}'.format(gap))
            self.gap_cb.Text = u'{0}'.format(DEFAULT_MERGE_GAP_MM)
            self.gap_cb.IsEnabled = False
            self.merge_chk.Checked += lambda s, a: setattr(self.gap_cb, 'IsEnabled', True)
            self.merge_chk.Unchecked += lambda s, a: setattr(self.gap_cb, 'IsEnabled', False)
            self.templates = plan_templates(doc)
            self.template_cb.Items.Add(NO_TEMPLATE)
            for name, _view in self.templates:
                self.template_cb.Items.Add(name)
            hint = [k for k, (n, _v) in enumerate(self.templates) if DEFAULT_TEMPLATE_HINT in n.lower()]
            self.template_cb.SelectedIndex = (hint[0] + 1) if hint else 0
            self.link_cb.SelectionChanged += self._link_changed
            if self.links:
                arc = [k for k, (n, _l, _d) in enumerate(self.links) if u'arc' in n.lower() or u'arch' in n.lower()]
                self.link_cb.SelectedIndex = arc[0] if arc else 0
            else:
                self.status_tb.Text = u'No loaded Revit link in this model - load the architectural model first.'
                self.generate_btn.IsEnabled = False
            self.generate_btn.Click += self._generate

        def _link_changed(self, sender, args):
            """List every placed room of the chosen link - NOTHING ticked. The user ticks the rooms, or uses
            "Suggest" (electrical-looking names) or "Last run" (the rooms of the last GENERATE)."""
            from System.Windows import Thickness
            from System.Windows.Controls import CheckBox
            index = self.link_cb.SelectedIndex
            if index < 0:
                return
            link_name = self.links[index][0]
            rooms = list_link_rooms(self.links[index][2])
            self.remembered = load_ticked(self.doc, link_name)
            self.last_btn.IsEnabled = bool(self.remembered)
            self._filling = True
            self.rooms_sp.Children.Clear()
            self.boxes = []
            for room in rooms:
                box = CheckBox()
                box.Content = room_list_label(room)
                box.Margin = Thickness(0, 2, 0, 2)
                box.IsChecked = False
                box.Checked += self._count
                box.Unchecked += self._count
                self.rooms_sp.Children.Add(box)
                self.boxes.append((room, box))
            levels = []
            for room in rooms:
                name = _level_name(room)
                if name and name not in levels:
                    levels.append(name)
            self.level_cb.Items.Clear()
            self.level_cb.Items.Add(u'<All levels>')
            for name in levels:
                self.level_cb.Items.Add(name)
            self.level_cb.SelectedIndex = 0
            self._filling = False
            self._filter(None, None)
            if not rooms:
                self.status_tb.Text = u'No placed room in this link.'

        def _hidden_ticked(self):
            """The ticked rooms the search / level filter hides - GENERATE makes views for them too."""
            from System.Windows import Visibility
            return [room for room, box in self.boxes if box.IsChecked and box.Visibility != Visibility.Visible]

        def _status(self):
            ticked = sum(1 for _r, box in self.boxes if box.IsChecked)
            hidden = len(self._hidden_ticked())
            text = u'{0} of {1} room(s) ticked.'.format(ticked, len(self.boxes))
            if hidden:
                text += u'  {0} of them hidden by the search / level filter - tick "Show ticked rooms only" to see them.'.format(hidden)
            self.status_tb.Text = text

        def _count(self, sender, args):
            """A room was ticked / unticked."""
            if self._filling:
                return
            if self.ticked_only_chk.IsChecked:
                self._filter(None, None)
            else:
                self._status()

        def _filter(self, sender, args):
            from System.Windows import Visibility
            if self._filling:
                return
            words = _normal(self.search_tb.Text).split()
            level = self.level_cb.SelectedItem if self.level_cb.SelectedIndex > 0 else None
            only_ticked = bool(self.ticked_only_chk.IsChecked)
            for room, box in self.boxes:
                text = _normal(room_list_label(room))
                show = all(w in text for w in words) and (level is None or _level_name(room) == level) \
                    and (not only_ticked or bool(box.IsChecked))
                box.Visibility = Visibility.Visible if show else Visibility.Collapsed
            self._status()

        def _tick_shown(self, value):
            from System.Windows import Visibility
            self._filling = True
            for _room, box in self.boxes:
                if box.Visibility == Visibility.Visible:
                    box.IsChecked = value
            self._filling = False
            self._filter(None, None)

        def _suggest(self, sender, args):
            self._filling = True
            for room, box in self.boxes:
                box.IsChecked = suggested(room)
            self._filling = False
            self._filter(None, None)

        def _last_run(self, sender, args):
            remembered = self.remembered or set()
            self._filling = True
            for room, box in self.boxes:
                box.IsChecked = getattr(room, 'UniqueId', None) in remembered
            self._filling = False
            self._filter(None, None)

        def _generate(self, sender, args):
            try:
                offset = float((self.offset_cb.Text or u'').replace(u',', u'.'))
                if offset < 0:
                    raise ValueError()
            except ValueError:
                forms.alert(u'Crop Offset must be a number of millimetres (0 or more).', title='Enlarged Electrical Rooms')
                return
            gap = DEFAULT_MERGE_GAP_MM
            if self.merge_chk.IsChecked:
                try:
                    gap = float((self.gap_cb.Text or u'').replace(u',', u'.'))
                    if gap < 0:
                        raise ValueError()
                except ValueError:
                    forms.alert(u'Max gap between rooms must be a number of millimetres (0 or more).', title='Enlarged Electrical Rooms')
                    return
            rooms = [room for room, box in self.boxes if box.IsChecked]
            if not rooms:
                forms.alert(u'Tick at least one room.', title='Enlarged Electrical Rooms')
                return
            # always show WHICH rooms get a view - ticked rooms hidden by the filter are easy to miss
            hidden = set(id(r) for r in self._hidden_ticked())
            lines = [u'{0}{1}'.format(room_list_label(r), u'   (hidden by filter)' if id(r) in hidden else u'')
                     for r in rooms]
            shown = lines[:25] + ([u'... and {0} more'.format(len(lines) - 25)] if len(lines) > 25 else [])
            if not forms.alert(u'Make / update views for these {0} room(s)?'.format(len(rooms)),
                               sub_msg=u'\n'.join(shown), title='Enlarged Electrical Rooms', yes=True, no=True):
                return
            if not (self.create_chk.IsChecked or self.update_chk.IsChecked):
                forms.alert(u'Tick "Create Missing Views", "Update Existing Views" or both.', title='Enlarged Electrical Rooms')
                return
            template_index = self.template_cb.SelectedIndex
            self.choice = {
                'link': self.links[self.link_cb.SelectedIndex],
                'rooms': rooms,
                'scale': SCALES[self.scale_cb.SelectedIndex],
                'offset_mm': offset,
                'template': self.templates[template_index - 1][1] if template_index > 0 else None,
                'create': bool(self.create_chk.IsChecked),
                'update': bool(self.update_chk.IsChecked),
                'merge': bool(self.merge_chk.IsChecked),
                'gap_mm': gap,
                'align': bool(self.align_chk.IsChecked),
                'doors': bool(self.doors_chk.IsChecked),
                'follow': bool(self.follow_chk.IsChecked),
            }
            self.Close()

    return EnlargedRoomsWindow


def main():
    from pyrevit import forms, revit, script
    doc = revit.doc
    if doc is None or doc.IsFamilyDocument:
        forms.alert(u'Open a project first.', title='Enlarged Electrical Rooms')
        return
    window = _window_class()(doc)
    window.ShowDialog()
    choice = window.choice
    if not choice:
        return                                        # closed without GENERATE
    link_name, link, link_doc = choice['link']
    save_ticked(doc, link_name, [getattr(r, 'UniqueId', u'') for r in choice['rooms']])
    try:
        result = _run(doc, link, link_doc, choice)
    except Exception as error:
        import traceback
        trace(u'STOPPED BY ERROR: {0}\n{1}'.format(error, traceback.format_exc()))
        forms.alert(u'Enlarged Electrical Rooms stopped: {0}'.format(error),
                    sub_msg=u'Details in %APPDATA%\\pyRevit\\EnlargedRooms_trace.log', title='Enlarged Electrical Rooms')
        return
    report = generate_report(result)
    output = script.get_output()
    output.print_code(report)
    forms.alert(report, title='Enlarged Electrical Rooms')


def _run(doc, link, link_doc, choice):
    return generate(doc, link, link_doc, choice['rooms'], choice['scale'], choice['offset_mm'], choice['template'],
                      choice['create'], choice['update'], choice['merge'], choice['gap_mm'], choice['align'],
                      choice['doors'], choice.get('follow', True))


if __name__ == '__main__':
    main()
