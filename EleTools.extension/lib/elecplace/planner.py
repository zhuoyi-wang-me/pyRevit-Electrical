# -*- coding: utf-8 -*-
"""
elecplace.planner
=================
Builds a complete placement PLAN without touching the model (read-only,
no transaction). The plan feeds the Preview tab and then executor.py.

Rule kinds
  KIND_WALLS  place a family type on the walls of each target room; the
              quantity can be FIXED or driven by ROOM SIZE (wall length,
              floor area, or area bands) with optional min / max.
  KIND_EQUIP  place a family type (e.g. socket, isolator, fused spur, MSFD
              control/junction box) on the NEAREST WALL to every selected
              Mechanical Equipment / Plumbing Fixture / Plumbing Equipment /
              Duct Accessory / Pipe Accessory instance (host and/or links).
              Height is above FFL or relative to the equipment elevation.
  KIND_ON_EQUIP host the family type ON the equipment itself (e.g. an
              isolator on an FCU / BCU casing) on a chosen side with two
              in-face offsets. Face-based types are hosted on the equipment
              face (linked or host) through a ReferenceIntersector hit;
              level-based types are placed at the same point, rotated.

Placeable categories: electrical fixtures and devices, lighting,
electrical/mechanical equipment, plumbing fixtures and equipment.

Revit API used:
  DB.FamilySymbol, DB.Family.FamilyPlacementType, DB.Category
  (IsBuiltInCategoryValid), DB.ElementMulticategoryFilter,
  DB.Transform (OfPoint, OfVector, Inverse), DB.Reference.CreateLinkReference,
  DB.Level.ProjectElevation, DB.FamilyInstance (Location, SuperComponent,
  Symbol), DB.BoundingBoxXYZ, Room.IsPointInRoom
"""
from collections import OrderedDict

from System.Collections.Generic import List
from Autodesk.Revit import DB

from elecplace.compat import (eid_int, element_name, format_area, format_length,
                              format_length_u, symbol_label, to_text)
from elecplace.geometry import (FaceCache, build_opening_index, build_room_segments,
                                find_room_face, notes_to_text, describe_face_miss,
                                find_face_through, THROUGH_SEARCH_FT)
from elecplace.equipgeom import edge_shift
from elecplace.mathutil import (GridIndex, band_count, clamp_qty, count_by_spacing,
                                distribute, nearest_on_intervals, nudge_offsets)

MODE_FACE = "face-hosted (linked wall face)"
MODE_UNHOSTED = "level-based (unhosted, rotated)"

KIND_WALLS = "walls"
KIND_EQUIP = "equipment"
KIND_ON_EQUIP = "on_equipment"

HREF_FFL = "ffl"
HREF_EQUIP = "equipment"

# X / Y offset directions for 'wall next to equipment' rules
AXES_WALL = "wall"        # X = along the wall, to the right as seen from the room
AXES_EQUIP = "equipment"  # X = unit's right (hand), Y = unit's front (facing)
AXES_PROJECT = "project"  # X = project east, Y = project north (host model axes)

# Where X / Y / Z offsets are measured from (wall-next-to-equipment rules)
FROM_CENTRE = "centre"    # the unit's insertion point
FROM_EDGE = "edge"        # the unit's outer edge, read from its geometry (auto size)
# Wall next to equipment: which face of the wall gets the fixture
FACE_ROOM = "room"        # the face inside the equipment's room (default)
FACE_BACK = "back"        # the opposite face of the wall BEHIND the unit (duct / IPS / chase side)
# On the equipment: what Offset Z is measured from
ZREF_CENTRE = "centre"    # centre of the chosen side (original behaviour)
ZREF_FFL = "ffl"          # floor level of the unit's room -> Offset Z = height above floor
BACK_WALL_MIN_DOT = 0.7   # wall counts as front/back (not a side wall) when its normal is within ~45 deg of the unit's facing axis

QTY_FIXED = "fixed"
QTY_SPACING = "spacing"
QTY_AREA = "area"
QTY_BANDS = "bands"

SCOPE_ALL = "all"
SCOPE_LEVELS = "levels"
SCOPE_NAME = "name"
SCOPE_NUMBER = "number"
SCOPE_PICKED = "picked"

# Where equipment is searched. In some projects the MEP equipment and
# accessories are modelled in THIS project, in others they sit in a LINKED
# MEP model - every source below works the same way (host coordinates).
SRC_HOST = "host"                 # this project only
SRC_ARCH = "arch"                 # the architectural link chosen in the header
SRC_ALL = "all"                   # this project + every loaded link (default)
SRC_LINKS = "links"               # every loaded link, not this project
SRC_LINK_PREFIX = "link:"         # "link:<model title>" - one specific linked model


def source_description(source):
    names = {SRC_HOST: u"this project", SRC_ARCH: u"architectural link",
             SRC_ALL: u"this project + all links", SRC_LINKS: u"all linked models"}
    if source.startswith(SRC_LINK_PREFIX):
        return u"link '{}'".format(source[len(SRC_LINK_PREFIX):])
    return names.get(source, source)

# (display name, BuiltInCategory member name) - members missing in older
# Revit versions are skipped automatically.
PLACEABLE_CATEGORIES = [
    ("Electrical Fixtures", "OST_ElectricalFixtures"),
    ("Lighting Devices", "OST_LightingDevices"),
    ("Lighting Fixtures", "OST_LightingFixtures"),
    ("Communication Devices", "OST_CommunicationDevices"),
    ("Data Devices", "OST_DataDevices"),
    ("Fire Alarm Devices", "OST_FireAlarmDevices"),
    ("Security Devices", "OST_SecurityDevices"),
    ("Nurse Call Devices", "OST_NurseCallDevices"),
    ("Telephone Devices", "OST_TelephoneDevices"),
    ("Electrical Equipment", "OST_ElectricalEquipment"),
    ("Mechanical Equipment", "OST_MechanicalEquipment"),
    ("Mechanical Control Devices", "OST_MechanicalControlDevices"),
    ("Plumbing Fixtures", "OST_PlumbingFixtures"),
    ("Plumbing Equipment", "OST_PlumbingEquipment"),
]

# Equipment that an equipment rule can serve: (key, display, BIC name)
EQUIPMENT_CATEGORIES = [
    ("mech", "Mechanical Equipment", "OST_MechanicalEquipment"),
    ("plumbfix", "Plumbing Fixtures", "OST_PlumbingFixtures"),
    ("plumbeq", "Plumbing Equipment", "OST_PlumbingEquipment"),
    ("ductacc", "Duct Accessories", "OST_DuctAccessory"),     # enum name is singular
    ("pipeacc", "Pipe Accessories", "OST_PipeAccessory"),     # enum name is singular
    # Lifts / escalators / travelators: Revit 2022+ category, plus Specialty
    # Equipment where older lift & escalator families were modelled.
    ("vcirc", "Vertical Circulation", "OST_VerticalCirculation"),
    ("specialty", "Specialty Equipment", "OST_SpecialityEquipment"),   # enum spelling 'Speciality'
]


def get_bic(name):
    """
    BuiltInCategory by member name, or None if absent in this Revit.
    `name` may list alternatives separated by '|' (first one found wins).
    """
    bic = None
    for alt in name.split("|"):
        bic = getattr(DB.BuiltInCategory, alt.strip(), None)
        if bic is not None:
            break
    if bic is None:
        return None
    try:
        if not DB.Category.IsBuiltInCategoryValid(bic):
            return None
    except Exception:
        pass
    return bic


def equipment_category_available(key):
    for k, _disp, name in EQUIPMENT_CATEGORIES:
        if k == key:
            return get_bic(name) is not None
    return False


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------
class Rule(object):
    """
    One placement rule. `values` keeps the raw editor/template text so the
    rule can be re-edited and saved; parsed values live in attributes and
    are filled by ui.MainWindow._build_rule().
    """

    def __init__(self, symbol, values):
        self.values = dict(values)
        self.symbol = symbol
        self.symbol_id = eid_int(symbol.Id)
        self.label = symbol_label(symbol)
        self.category_name = to_text(values.get("category", u""))
        self.enabled = bool(values.get("enabled", True))
        self.kind = values.get("kind", KIND_WALLS)
        self.height_ft = 0.0
        self.height_text = u""
        self.wall_offset_ft = 0.0
        # quantity (KIND_WALLS)
        self.qty_method = values.get("qty_method", QTY_FIXED)
        self.qty_count = 0
        self.qty_value = 0.0          # spacing ft or area ft2 per fixture
        self.bands = []               # [(limit ft2 or None, count)]
        self.min_qty = 0
        self.max_qty = 0
        # rooms
        self.room_scope = values.get("room_scope", SCOPE_ALL)
        self.room_filter = to_text(values.get("room_filter", u""))
        self.picked_uids = list(values.get("picked_uids", []) or [])
        # equipment (KIND_EQUIP)
        self.equip_cats = list(values.get("equip_cats", []) or [])
        self.equip_source = values.get("equip_source", SRC_ALL)
        self.equip_keywords = [k.strip().lower() for k in
                               to_text(values.get("equip_keywords", u"")).split(u",") if k.strip()]
        # exact 'Family : Type' labels picked from the list (empty = all types);
        # when set they replace the name filter above
        self.equip_types = [to_text(t) for t in (values.get("equip_types", []) or []) if to_text(t)]
        self.height_ref = values.get("height_ref", HREF_FFL)
        self.max_dist_ft = 0.0
        self.per_equipment = 1
        # X / Y offsets for wall-next-to-equipment (Z = height field)
        self.near_axes = values.get("near_axes", AXES_WALL)
        self.near_x_ft = 0.0
        self.near_y_ft = 0.0
        self.near_from = values.get("near_from", FROM_CENTRE)
        self.wall_face = values.get("wall_face", FACE_ROOM)
        # on-equipment (KIND_ON_EQUIP): X / Y / Z in the unit's own axes
        self.side = values.get("side", "front")
        self.z_ref = values.get("z_ref", ZREF_CENTRE)
        self.off_x_ft = 0.0
        self.off_y_ft = 0.0
        self.off_z_ft = 0.0
        # data written to placed fixtures: [(parameter name, value template)]
        self.mappings = []
        # derived
        self.qty_desc = u""
        self.room_uids = []
        self.mode, self.mode_reason = None, u""

    def refresh_mode(self, settings):
        self.mode, self.mode_reason = classify_symbol(self.symbol, settings)

    # kept for the report / preview
    @property
    def quantity(self):
        return self.qty_desc


class Planned(object):
    """One fixture that will be created (all *_host values in host coords)."""

    def __init__(self, rule, room, seg):
        self.rule = rule
        self.room = room
        self.seg = seg
        self.point_host = None      # insertion point (host coords)
        self.normal_host = None     # unit vector pointing into the room
        self.ref_dir_host = None    # family X axis for face-based placement
        self.link_ref = None        # Reference to linked wall face
        self.host_level = None
        self.level_offset = 0.0
        self.served = u""           # equipment label for equipment rules
        self.host_desc = (u"wall {}".format(seg.wall_id) if seg is not None else u"-")
        self.rotate_dir = None      # horizontal facing for level-based placement
        self.adjust_note = u""      # e.g. "moved 150 mm to avoid <obstacle>"
        self.equipment = None       # served Equipment (equipment rules)
        self.param_values = []      # [(parameter, text)] resolved from rule.mappings


class Plan(object):
    def __init__(self):
        self.placements = []
        self.issues = []            # (room/equipment, level, rule label, reason)
        self.info = []              # informational lines (equipment found per source)
        self.missing_tokens = {}    # {token: fixtures where it had no value}
        self.segment_stats = {}     # room uid -> (usable segments, notes text)
        self.rows = []              # preview rows (dicts)

    def add_issue(self, room, rule_label, reason, subject=None):
        if subject is not None:
            self.issues.append((subject, room.level_name if room else u"-", rule_label, reason))
        elif room is None:
            self.issues.append((u"-", u"-", rule_label, reason))
        else:
            self.issues.append((room.label, room.level_name, rule_label, reason))

    def add_row(self, rule, room, basis, target, planned, note=u"", served=u""):
        self.rows.append({"rule": rule, "room": room, "basis": basis, "served": served,
                          "target": target, "planned": planned, "note": note, "data": u""})

    def count_for_rule(self, rule):
        return sum(1 for p in self.placements if p.rule is rule)


# --------------------------------------------------------------------------
# Family types
# --------------------------------------------------------------------------
def collect_placeable_symbols(doc):
    """OrderedDict {category display name: [FamilySymbol, ...]} (non-empty only)."""
    valid = [(disp, get_bic(name)) for disp, name in PLACEABLE_CATEGORIES]
    valid = [(d, b) for d, b in valid if b is not None]
    id_to_disp = dict((int(b), d) for d, b in valid)
    grouped = OrderedDict((d, []) for d, _b in valid)
    collector = (DB.FilteredElementCollector(doc)
                 .OfClass(DB.FamilySymbol)
                 .WherePasses(DB.ElementMulticategoryFilter(
                     List[DB.BuiltInCategory]([b for _d, b in valid]))))
    for sym in collector:
        try:
            disp = id_to_disp.get(eid_int(sym.Category.Id))
        except Exception:
            disp = None
        if disp:
            grouped[disp].append(sym)
    out = OrderedDict()
    for disp, syms in grouped.items():
        if syms:
            out[disp] = sorted(syms, key=lambda s: symbol_label(s).lower())
    return out


def classify_symbol(symbol, settings):
    """
    Decide HOW a family can be placed against a LINKED wall.
    returns (mode, reason); mode None = rejected.

    * WorkPlaneBased (face-based / work-plane-based) -> hosted on the linked
      wall face via Reference.CreateLinkReference (best practice).
    * OneLevelBasedHosted (legacy wall/ceiling-hosted template) -> Revit
      cannot host these on elements inside a link -> rejected.
    * OneLevelBased (non-hosted: most mechanical/plumbing equipment) ->
      placed on the host level at the wall, rotated to face the room, when
      'Allow level-based families' is enabled.
    """
    try:
        if not symbol.IsValidObject:
            return None, u"Family type is no longer loaded in the project"
        ptype = symbol.Family.FamilyPlacementType
    except Exception as ex:
        return None, u"Cannot read family placement type: {}".format(to_text(ex))

    if ptype == DB.FamilyPlacementType.WorkPlaneBased:
        return MODE_FACE, u""
    if ptype == DB.FamilyPlacementType.OneLevelBasedHosted:
        return None, (u"Host-based family (wall/ceiling template). Revit cannot host these "
                      u"on elements of a linked model - use a FACE-BASED or level-based version.")
    if ptype == DB.FamilyPlacementType.OneLevelBased:
        if settings.allow_unhosted_fallback:
            return MODE_UNHOSTED, u""
        return None, (u"Level-based family. Tick 'Allow level-based families' on the "
                      u"Options tab to place it against walls.")
    return None, u"Unsupported placement type: {}".format(to_text(ptype))


def hosting_tag(symbol, settings):
    mode, _ = classify_symbol(symbol, settings)
    if mode == MODE_FACE:
        return u"[FACE]"
    if mode == MODE_UNHOSTED:
        return u"[LEVEL]"
    try:
        if symbol.Family.FamilyPlacementType == DB.FamilyPlacementType.OneLevelBased:
            return u"[LEVEL - enable option]"
    except Exception:
        pass
    return u"[NOT SUPPORTED]"


# --------------------------------------------------------------------------
# Rooms and quantities
# --------------------------------------------------------------------------
def resolve_room_uids(scope, room_filter, picked_uids, rooms):
    """Target rooms of a rule -> list of RoomInfo.uid."""
    keys = [k.strip().lower() for k in to_text(room_filter).split(u",") if k.strip()]
    if scope == SCOPE_ALL:
        return [r.uid for r in rooms]
    if scope == SCOPE_LEVELS:
        return [r.uid for r in rooms if r.level_name.lower() in keys]
    if scope == SCOPE_NAME:
        return [r.uid for r in rooms if any(k in r.name.lower() for k in keys)]
    if scope == SCOPE_NUMBER:
        return [r.uid for r in rooms if any(r.number.lower().startswith(k) for k in keys)]
    if scope == SCOPE_PICKED:
        picked = set(picked_uids or [])
        return [r.uid for r in rooms if r.uid in picked]
    return []


def compute_target_qty(rule, room, wall_len_ft):
    """How many fixtures this room needs under the rule's quantity method."""
    if rule.qty_method == QTY_SPACING:
        n = count_by_spacing(wall_len_ft, rule.qty_value)
    elif rule.qty_method == QTY_AREA:
        n = count_by_spacing(room.area, rule.qty_value)
    elif rule.qty_method == QTY_BANDS:
        n = band_count(rule.bands, room.area)
    else:
        n = rule.qty_count
    return clamp_qty(n, rule.min_qty, rule.max_qty)


# --------------------------------------------------------------------------
# Duplicate index
# --------------------------------------------------------------------------
def build_duplicate_index(doc, settings, rules):
    """Existing instances of the rules' categories in the HOST (host coords)."""
    tol = settings.duplicate_tolerance_ft
    index = GridIndex(max(tol, 0.1))
    cat_ids = OrderedDict()
    for r in rules:
        try:
            cat_ids[eid_int(r.symbol.Category.Id)] = r.symbol.Category.Id
        except Exception:
            pass
    if not cat_ids:
        return index
    collector = (DB.FilteredElementCollector(doc)
                 .OfClass(DB.FamilyInstance)
                 .WherePasses(DB.ElementMulticategoryFilter(
                     List[DB.ElementId](list(cat_ids.values())))))
    for inst in collector:
        loc = inst.Location
        if isinstance(loc, DB.LocationPoint):
            p = loc.Point
            index.add(p.X, p.Y, p.Z)
    return index


# --------------------------------------------------------------------------
# Equipment (for KIND_EQUIP rules)
# --------------------------------------------------------------------------
class Equipment(object):
    """An equipment instance from the host or a link (host coordinates)."""

    def __init__(self, inst, label, host_point, to_host, link_instance_id, source=u""):
        self.inst = inst
        self.source = source
        self.elem_id = eid_int(inst.Id)
        self.label = label
        self.host_point = host_point
        self.to_host = to_host
        self.link_instance_id = link_instance_id     # None = host model
        self._frame = None

    def extent(self, axis):
        """
        (min, max) of the unit's geometry along a HOST axis, relative to its
        insertion point - i.e. its real size in that direction (MSFD width,
        heater depth...). Geometry is read once and cached.
        """
        if not hasattr(self, "_pts_host"):
            from elecplace.equipgeom import equipment_points
            self._pts_host = [self.to_host.OfPoint(p) for p in equipment_points(self.inst)]
        if not self._pts_host:
            return 0.0, 0.0
        vals = [p.Subtract(self.host_point).DotProduct(axis) for p in self._pts_host]
        return min(vals), max(vals)

    def axes(self):
        """(right/hand, front/facing) of the unit in host coords, horizontal."""
        hand = self.to_host.OfVector(self.inst.HandOrientation)
        facing = self.to_host.OfVector(self.inst.FacingOrientation)
        return _flat(hand), _flat(facing)

    def frame(self):
        if self._frame is None:
            from elecplace.equipgeom import equipment_frame
            self._frame = equipment_frame(self.inst, self.to_host)
        return self._frame


def _flat(v):
    """Horizontal unit vector (falls back to X if v is vertical)."""
    h = DB.XYZ(v.X, v.Y, 0.0)
    return h.Normalize() if h.GetLength() > 1e-9 else DB.XYZ.BasisX


def equipment_bics(cat_keys):
    names = [n for k, _d, n in EQUIPMENT_CATEGORIES if k in cat_keys]
    return [b for b in (get_bic(n) for n in names) if b is not None]


def _instance_point(inst):
    loc = inst.Location
    if isinstance(loc, DB.LocationPoint):
        return loc.Point
    try:
        bb = inst.get_BoundingBox(None)
        if bb:
            return bb.Min.Add(bb.Max).Multiply(0.5)
    except Exception:
        pass
    return None


def collect_equipment(doc, arch_link, all_links, cat_keys, source, keywords=None, types=None):
    """
    Equipment instances (host coordinates) from the chosen source models.

    keywords: optional list; family or type name must contain one of them
    (case-insensitive), e.g. ["msfd"] or ["fcu", "bcu"].
    types: optional list of exact 'Family : Type' labels (picked from the list
    in the window). When given it replaces `keywords`.

    returns (items, stats) where stats = OrderedDict {source name: count}
    (including sources where nothing was found, so the preview can show it).
    Items at the same position with the same type coming from two sources
    (e.g. a model that is both open and linked) are kept only once.
    """
    stats = OrderedDict()
    bics = equipment_bics(cat_keys)
    if not bics:
        return [], stats
    sources = []
    if source in (SRC_HOST, SRC_ALL):
        sources.append((u"this project", doc, DB.Transform.Identity, None))
    if source == SRC_ARCH:
        sources.append((arch_link.title, arch_link.doc, arch_link.transform, arch_link.id))
    if source in (SRC_ALL, SRC_LINKS):
        for lk in all_links:
            sources.append((lk.name, lk.doc, lk.transform, lk.id))
    if source.startswith(SRC_LINK_PREFIX):
        title = source[len(SRC_LINK_PREFIX):]
        for lk in all_links:                    # every placed instance of that model
            if lk.title == title:
                sources.append((lk.name, lk.doc, lk.transform, lk.id))

    type_set = set(to_text(t) for t in (types or []))
    if type_set:
        keywords = None
    found, seen = [], set()
    cat_filter = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory](bics))
    for src_name, sdoc, xf, link_id in sources:
        stats[src_name] = 0
        for inst in DB.FilteredElementCollector(sdoc).OfClass(DB.FamilyInstance).WherePasses(cat_filter):
            if inst.SuperComponent is not None:      # skip nested sub-components
                continue
            type_label = symbol_label(inst.Symbol)
            if type_set and type_label not in type_set:
                continue
            if keywords and not any(k in type_label.lower() for k in keywords):
                continue
            p = _instance_point(inst)
            if p is None:
                continue
            hp = xf.OfPoint(p)
            key = (type_label, round(hp.X, 2), round(hp.Y, 2), round(hp.Z, 2))
            if key in seen:
                continue                        # same unit reached through two sources
            seen.add(key)
            label = u"{} [{}] ({})".format(type_label, eid_int(inst.Id), src_name)
            found.append(Equipment(inst, label, hp, xf, link_id, src_name))
            stats[src_name] += 1
    return found, stats


def list_equipment_types(doc, arch_link, all_links, cat_keys, source):
    """
    Every equipment type found in the chosen source models, for the
    'Pick types...' list: [(type label, category name, count)] sorted by
    category then name. Same collection (and duplicate handling) as the plan.
    """
    items, _stats = collect_equipment(doc, arch_link, all_links, cat_keys, source)
    counts, cats = OrderedDict(), {}
    for eq in items:
        label = symbol_label(eq.inst.Symbol)
        counts[label] = counts.get(label, 0) + 1
        if label not in cats:
            try:
                cats[label] = to_text(eq.inst.Category.Name)
            except Exception:
                cats[label] = u""
    rows = [(label, cats.get(label, u""), n) for label, n in counts.items()]
    rows.sort(key=lambda r: (r[1].lower(), r[0].lower()))
    return rows


def _shift_along(seg, t_new):
    """
    Move to distance t_new along the segment, staying inside a usable
    interval (corner / opening clearances). Returns (t, a, b, note).
    """
    best = None
    for a, b in seg.intervals:
        t = min(max(t_new, a), b)
        if best is None or abs(t - t_new) < abs(best[0] - t_new):
            best = (t, a, b)
    t, a, b = best
    note = u"" if abs(t - t_new) < 1e-6 else u"X offset shortened to stay clear of corner/opening"
    return t, a, b, note


def _suggest_distance(ctx, plan, rule, too_far):
    """
    Explain 'too far' skips and propose a Max. distance that would cover 90 %
    of them (rounded up to 100 mm), so a units slip (10 instead of 1500) is
    obvious in the preview.
    """
    vals = sorted(too_far)
    p90 = vals[min(len(vals) - 1, int(0.9 * len(vals)))]
    step = 100.0 / 304.8                                   # 100 mm in feet
    suggest = (int(p90 / step) + 1) * step
    plan.info.append(
        u"{}: {} item(s) skipped because the nearest wall is further than the Max. distance "
        u"({}). Half of them are within {}, 90 % within {} - try Max. distance = {}.".format(
            rule.label, len(vals), format_length_u(ctx.doc, rule.max_dist_ft),
            format_length_u(ctx.doc, vals[len(vals) // 2]), format_length_u(ctx.doc, p90),
            format_length_u(ctx.doc, suggest)))


def _record_sources(plan, rule, stats):
    """Preview/report line: where the equipment of this rule was found."""
    if not stats:
        return
    parts = [u"{} in {}".format(n, name) for name, n in stats.items()]
    plan.info.append(u"{}: {} item(s) found - {}".format(
        rule.label, sum(stats.values()), u"; ".join(parts)))


_PROBE_DIRS = [(1, 0), (-1, 0), (0, 1), (0, -1), (0.7071, 0.7071),
               (-0.7071, 0.7071), (0.7071, -0.7071), (-0.7071, -0.7071)]


def _in_room(room, x, y):
    try:
        return room.room.IsPointInRoom(DB.XYZ(x, y, room.ffl(True) + 1.0))
    except Exception:
        return False


def _room_containing(rooms, p):
    """
    Room (link coords) that an equipment point belongs to.

    * Plan position is tested 1 ft above the room floor, so equipment in a
      ceiling void (FCUs, dampers) still matches the room below.
    * The room with the HIGHEST floor at or below the point wins.
    * Items sitting inside a wall (fire / smoke dampers) are matched by
      probing 1.5 ft around the point.
    """
    cands = []
    for room in rooms:
        if room.ffl(True) > p.Z + 1.0:
            continue
        if room.bbox:
            lo, hi = room.bbox
            if not (lo.X - 2.0 <= p.X <= hi.X + 2.0 and lo.Y - 2.0 <= p.Y <= hi.Y + 2.0):
                continue
        cands.append(room)
    cands.sort(key=lambda r: -r.ffl(True))
    for room in cands:
        if _in_room(room, p.X, p.Y):
            return room
    for room in cands:                       # point inside a wall
        for dx, dy in _PROBE_DIRS:
            if _in_room(room, p.X + dx * 1.5, p.Y + dy * 1.5):
                return room
    return None


class NoRoom(object):
    """Stand-in 'room' for on-equipment placements outside any room."""

    def __init__(self, link, level_name):
        self.uid = u"__noroom__" + to_text(level_name)
        self.id = -1
        self.number = u""
        self.name = u"(outside rooms)"
        self.level_name = to_text(level_name)
        self.link = link
        self.area = 0.0
        self.bounding_wall_ids = set()
        self.label = u"(outside rooms)"
        self.full_label = u"(outside rooms) [{}]".format(self.level_name)


# --------------------------------------------------------------------------
# Plan builder
# --------------------------------------------------------------------------
class _Ctx(object):
    """Shared caches for one planning run."""

    def __init__(self, doc, link, settings, dup_index, logger, plan, all_links=None):
        self.doc = doc
        self.link = link
        self.settings = settings
        self.dup = dup_index
        self.logger = logger
        self.plan = plan
        self.faces = FaceCache()
        self.openings = build_opening_index(link.doc) if settings.avoid_openings else {}
        self.levels = sorted(DB.FilteredElementCollector(doc).OfClass(DB.Level),
                             key=lambda l: l.ProjectElevation)
        self._segs = {}
        self.clash = None
        if settings.avoid_clashes:
            from elecplace.clash import build_clash_index
            self.clash = build_clash_index(doc, all_links or [], settings, logger)
            plan.info.append(u"Clash check: {} obstacle(s) indexed (equipment & accessories{}) "
                             u"in this project and {} link(s)".format(
                                 self.clash.count,
                                 u", ducts / pipes / trays / conduits"
                                 if settings.clash_include_mep_curves else u"",
                                 len(all_links or [])))

    def search_offsets(self, prefer=1.0):
        """Slide positions tried when a spot is taken (clash / duplicate)."""
        if self.clash is not None:
            step = self.settings.clash_step_ft
            tries = max(1, int(self.settings.clash_max_slide_ft / step))
        else:
            step, tries = max(self.settings.duplicate_tolerance_ft * 2.1, 0.2), 3
        return nudge_offsets(step, tries, prefer)

    def check_clash(self, rule, point, normal, ref_dir, ignore_key=None, ignore_cats=None):
        """(fixture box, clashing obstacle label or None)."""
        if self.clash is None:
            return None, None
        from elecplace.clash import fixture_box
        box = fixture_box(rule.symbol, point, normal, ref_dir, rule.mode == MODE_FACE,
                          self.settings.clash_clearance_ft, self.settings.fixture_default_size_ft)
        return box, self.clash.find(box, ignore_key, ignore_cats)

    def register(self, box, rule):
        if self.clash is not None and box is not None:
            self.clash.add_fixture(box, u"new " + rule.label)

    def segments(self, room):
        if room.uid not in self._segs:
            try:
                segs, notes = build_room_segments(room, self.settings, self.openings)
            except Exception as ex:
                segs, notes = [], {u"error: " + to_text(ex): 1}
            self._segs[room.uid] = (segs, notes)
            self.plan.segment_stats[room.uid] = (len(segs), notes_to_text(notes))
        return self._segs[room.uid]

    def pick_level(self, z):
        chosen = None
        for lvl in self.levels:
            if lvl.ProjectElevation <= z + 1e-3:
                chosen = lvl
        return chosen or (self.levels[0] if self.levels else None)


def _resolve_slot(ctx, slot, rule, room, z_place=None, prefer=1.0, ignore_key=None, back=False):
    """Slot -> Planned. Slides along the piece on clashes. (Planned, reason).
    ignore_key: (link instance id, element id) of the equipment being served -
    its own box must not count as a clash (a WC / cubicle family's bounding box
    usually covers the wall where its spur goes).
    back: use the OPPOSITE face of the wall (behind the unit: duct / IPS side);
    the fixture then faces away from the room."""
    seg, a, b, t0 = slot
    tol = ctx.settings.duplicate_tolerance_ft
    xf = ctx.link.transform
    ffl = room.ffl(ctx.settings.include_room_base_offset)
    if z_place is None:
        z_place = ffl + rule.height_ft
    z_probe = max(z_place, ffl + 0.05)           # floor-standing: probe just above floor
    reason = u"no valid position"
    # room side: face whose normal points into the room; back side: the wall's
    # other face, normal pointing away from the room (one wall width further)
    face_dir = seg.inward.Negate() if back else seg.inward

    blocked_by = None
    miss_text = None                             # back face diagnosis, worked out once per slot
    for off in ctx.search_offsets(prefer):
        t = t0 + off
        if t < a - 1e-6 or t > b + 1e-6:
            continue
        probe = seg.point_at(t, z_probe)
        hit = find_room_face(ctx.faces, seg.wall, probe, face_dir, seg.wall.Width + 0.5)
        through_wall = None
        if hit is None and back:
            # thin finish / skin wall as room boundary: use the real wall behind it
            try:
                th = find_face_through(ctx.faces, ctx.link.doc, probe, face_dir,
                                       seg.wall.Width + THROUGH_SEARCH_FT, seg.wall_id)
            except Exception:
                th = None
            if th is not None:
                hit, through_wall = th[:3], th[3]
        if hit is None:
            if back:
                if miss_text is None:
                    try:
                        miss_text = describe_face_miss(ctx.faces, seg.wall, probe, face_dir,
                                                       seg.wall.Width + 0.5)
                    except Exception as ex:
                        miss_text = u"wall {}: {}".format(seg.wall_id, to_text(ex))
                reason = u"no back face - " + miss_text
            else:
                reason = u"no room-facing wall face at this height (above wall top?)"
            continue
        ref, face, proj = hit
        face_pt = DB.XYZ(proj.X, proj.Y, z_place)
        if rule.mode == MODE_UNHOSTED and rule.wall_offset_ft > 0:
            face_pt = face_pt.Add(face_dir.Multiply(rule.wall_offset_ft))

        # ---- link -> host transformation ---------------------------------
        p_host = xf.OfPoint(face_pt)
        if ctx.dup.near(p_host.X, p_host.Y, p_host.Z, tol):
            reason = u"duplicate - an element already exists within tolerance"
            continue
        n_host = xf.OfVector(face.FaceNormal).Normalize()
        # BasisZ x normal keeps face-based families upright on vertical faces.
        ref_dir = DB.XYZ.BasisZ.CrossProduct(n_host).Normalize()
        # Back side: the served units (WCs, cubicle / accessible-toilet assemblies...)
        # stand in the ROOM; their big family boxes reach through the wall but
        # cannot block a fixture on the chase face. Other categories still count.
        ignore_cats = _served_category_ids(rule) if back else None
        box, hit_label = ctx.check_clash(rule, p_host, n_host, ref_dir, ignore_key=ignore_key,
                                         ignore_cats=ignore_cats)
        if hit_label:
            blocked_by = hit_label
            reason = u"clash with {} (no clear spot within {})".format(
                hit_label, format_length_u(ctx.doc, ctx.settings.clash_max_slide_ft))
            continue

        pp = Planned(rule, room, seg)
        pp.point_host = p_host
        pp.normal_host = n_host
        pp.ref_dir_host = ref_dir
        if abs(off) > 1e-9:
            pp.adjust_note = (u"moved {} along wall to avoid {}".format(
                format_length_u(ctx.doc, abs(off)), blocked_by) if blocked_by else
                u"moved {} along wall (position taken)".format(format_length_u(ctx.doc, abs(off))))
        if through_wall is not None:
            pp.host_wall_id = eid_int(through_wall.Id)
            note = u"room is bounded by thin wall {} - placed on wall {} behind it".format(
                seg.wall_id, pp.host_wall_id)
            pp.adjust_note = (pp.adjust_note + u"; " + note) if pp.adjust_note else note
        if rule.mode == MODE_FACE:
            pp.link_ref = ref.CreateLinkReference(ctx.link.instance)
        pp.host_level = ctx.pick_level(p_host.Z)
        if pp.host_level is not None:
            pp.level_offset = p_host.Z - pp.host_level.ProjectElevation
        ctx.dup.add(p_host.X, p_host.Y, p_host.Z)
        ctx.register(box, rule)
        return pp, u""
    return None, reason


def _plan_walls(ctx, rule, rooms):
    plan = ctx.plan
    for room in rooms:
        segs, notes = ctx.segments(room)
        wall_len = sum(s.length for s in segs)
        basis = u"area {} | wall {}".format(format_area(ctx.doc, room.area),
                                          format_length(ctx.doc, wall_len))
        if not segs:
            detail = notes_to_text(notes)
            plan.add_issue(room, rule.label, u"Room skipped - no usable host wall"
                           + (u" ({})".format(detail) if detail else u""))
            plan.add_row(rule, room, basis, 0, 0, u"no usable wall")
            continue

        target = compute_target_qty(rule, room, wall_len)
        if target <= 0:
            plan.add_row(rule, room, basis, 0, 0, u"quantity rule gives 0")
            continue

        pieces = [(seg, a, b) for seg in segs for a, b in seg.intervals]
        placed, reasons = 0, {}
        for slot in distribute(pieces, target):
            try:
                pp, why = _resolve_slot(ctx, slot, rule, room)
            except Exception as ex:
                pp, why = None, u"error: " + to_text(ex)
            if pp is not None:
                plan.placements.append(pp)
                placed += 1
                if pp.adjust_note:
                    reasons[pp.adjust_note] = reasons.get(pp.adjust_note, 0) + 1
            else:
                reasons[why] = reasons.get(why, 0) + 1

        detail = notes_to_text(reasons)
        plan.add_row(rule, room, basis, target, placed, detail)
        if placed < target:
            plan.add_issue(room, rule.label, u"{} - planned {}/{} ({})".format(
                u"Room skipped" if placed == 0 else u"Partial", placed, target,
                detail or u"not enough usable wall"))


def _floor_z_host(ctx, room, eq):
    """Floor level (host Z) under a unit: its room's FFL, else the host level below it."""
    try:
        ffl = room.ffl(ctx.settings.include_room_base_offset)          # arch-link Z
        p = ctx.link.transform.Inverse.OfPoint(eq.host_point)
        return ctx.link.transform.OfPoint(DB.XYZ(p.X, p.Y, ffl)).Z
    except Exception:
        pass
    lvl = ctx.pick_level(eq.host_point.Z)
    return lvl.ProjectElevation if lvl is not None else None


def _served_category_ids(rule):
    """Category ids (ints) of the categories a rule serves, e.g. Plumbing Fixtures."""
    ids = set()
    for bic in equipment_bics(rule.equip_cats):
        try:
            ids.add(int(bic))
        except Exception:
            pass
    return ids


def _facing_in_link(eq, inverse):
    """Horizontal facing direction of the unit in arch-link coordinates, or None."""
    try:
        f = eq.inst.FacingOrientation               # in the unit's own model
        if eq.to_host is not None:
            f = eq.to_host.OfVector(f)
        f = inverse.OfVector(f)
        h = DB.XYZ(f.X, f.Y, 0.0)
        return h.Normalize() if h.GetLength() > 1e-6 else None
    except Exception:
        return None


def _plan_equipment(ctx, rule, rooms, all_links):
    plan = ctx.plan
    items, stats = collect_equipment(ctx.doc, ctx.link, all_links, rule.equip_cats,
                                     rule.equip_source, rule.equip_keywords,
                                     types=rule.equip_types)
    _record_sources(plan, rule, stats)
    if not items:
        plan.add_issue(None, rule.label, u"No equipment of the selected categories / names found "
                       u"in {} - check 'Equipment from'".format(source_description(rule.equip_source)))
        return
    inverse = ctx.link.transform.Inverse
    outside = 0
    too_far = []                 # nearest-wall distances beyond the limit
    spacing = ctx.settings.equipment_spacing_ft
    for eq in items:
        p_eq_link = inverse.OfPoint(eq.host_point)       # host -> arch link coords
        # ---- X / Y offset of the target point (before snapping to a wall) --
        target_host = eq.host_point
        edge = rule.near_from == FROM_EDGE
        size_note = u""
        if rule.near_axes in (AXES_EQUIP, AXES_PROJECT):
            if rule.near_axes == AXES_EQUIP:
                ax_x, ax_y = eq.axes()
            else:
                ax_x, ax_y = DB.XYZ.BasisX, DB.XYZ.BasisY
            dx, dy = rule.near_x_ft, rule.near_y_ft
            if edge:                              # measure from the unit's edges
                if dx:
                    dx = edge_shift(dx, *eq.extent(ax_x))
                if dy:
                    dy = edge_shift(dy, *eq.extent(ax_y))
                size_note = u"from unit edge: X {} / Y {} from centre".format(
                    format_length_u(ctx.doc, dx), format_length_u(ctx.doc, dy))
            target_host = target_host.Add(ax_x.Multiply(dx)).Add(ax_y.Multiply(dy))
        p_link = inverse.OfPoint(target_host)
        # room of the offset point first (it may be the next room), else the unit's
        room = _room_containing(rooms, p_link) or _room_containing(rooms, p_eq_link)
        if room is None:
            outside += 1
            continue
        all_segs, _notes = ctx.segments(room)
        segs = all_segs
        back = rule.wall_face == FACE_BACK
        behind_note = u""
        if back:
            # only walls square to the unit's facing axis (in front of / behind it),
            # never the side walls; the nearest of those is the wall it stands against.
            # Either sign: families differ - some face away from their back wall,
            # the QIC cubicle families face TOWARDS it.
            facing = _facing_in_link(eq, inverse)
            if facing is not None:
                behind = [s for s in segs if abs(s.inward.DotProduct(facing)) >= BACK_WALL_MIN_DOT]
                if behind:
                    segs = behind
                else:
                    behind_note = u"no wall behind the unit (by its facing) - nearest wall used"
            else:
                behind_note = u"unit facing unknown - nearest wall used"

        def _nearest(candidates):
            found = None
            for s in candidates:
                h = nearest_on_intervals(p_link.X, p_link.Y, s.start.X, s.start.Y,
                                         s.direction.X, s.direction.Y, s.intervals)
                if h and (found is None or h[1] < found[1][1]):
                    found = (s, h)
            return found

        best = _nearest(segs)
        if back and segs is not all_segs and (best is None or best[1][1] > rule.max_dist_ft):
            # facing-based pick failed (some families' facing runs ALONG their back
            # wall, e.g. TypicalToiletCubicleGA): fall back to the plain nearest wall
            near = _nearest(all_segs)
            if near is not None and near[1][1] <= rule.max_dist_ft:
                far_text = format_length_u(ctx.doc, best[1][1]) if best is not None else u"-"
                best = near
                behind_note = (u"wall in line with the unit's facing is {} away - nearest wall "
                               u"used instead".format(far_text))
        if best is None or best[1][1] > rule.max_dist_ft:
            if best is not None:
                too_far.append(best[1][1])
            why = (u"no usable wall in room" if best is None else
                   u"nearest usable {}wall is {} away (max. distance is {})".format(
                       u"back " if back else u"",
                       format_length_u(ctx.doc, best[1][1]), format_length_u(ctx.doc, rule.max_dist_ft)))
            plan.add_issue(room, rule.label, why, subject=eq.label)
            plan.add_row(rule, room, u"serves " + eq.label, rule.per_equipment, 0, why,
                         served=eq.label)
            continue

        seg, (t, dist, a, b) = best
        # 'Along the wall' X offset: to the right as seen from inside the room.
        # Viewer looks at the wall along -inward; right = Z x inward.
        adjusted = u""
        prefer = 1.0
        if rule.near_axes == AXES_WALL and abs(rule.near_x_ft) > 1e-9:
            right_xy = DB.XYZ(-seg.inward.Y, seg.inward.X, 0.0)
            sign = 1.0 if right_xy.DotProduct(seg.direction) >= 0 else -1.0
            if edge:
                # Unit size along THIS wall, e.g. MSFD 826 mm wide -> -413 / +413
                lo, hi = eq.extent(ctx.link.transform.OfVector(right_xy))
                shift = edge_shift(rule.near_x_ft, lo, hi)
                t_try, a2, b2, note = _shift_along(seg, t + sign * shift)
                if note:
                    # side blocked (corner / door) -> same clearance on the other end
                    alt = edge_shift(-rule.near_x_ft, lo, hi)
                    t_alt, a3, b3, note_alt = _shift_along(seg, t + sign * alt)
                    if not note_alt:
                        t_try, a2, b2, shift = t_alt, a3, b3, alt
                        note = u"placed at the other end of the unit (first side blocked)"
                t, a, b, adjusted = t_try, a2, b2, note
                prefer = sign * (1.0 if shift >= 0 else -1.0)   # keep sliding AWAY from the unit
                size_note = u"unit {} long on wall, fixture {} from its centre".format(
                    format_length_u(ctx.doc, hi - lo), format_length_u(ctx.doc, abs(shift)))
            else:
                t, a, b, adjusted = _shift_along(seg, t + sign * rule.near_x_ft)
                prefer = sign * (1.0 if rule.near_x_ft >= 0 else -1.0)
        k = rule.per_equipment
        z_place = None
        if rule.height_ref == HREF_EQUIP:
            dz = rule.height_ft
            if edge and dz:                       # above the top / below the bottom
                dz = edge_shift(dz, *eq.extent(DB.XYZ.BasisZ))
            z_place = p_eq_link.Z + dz
        placed, reasons = 0, {}
        if adjusted:
            reasons[adjusted] = 1
        if behind_note:
            reasons[behind_note] = 1
        for i in range(k):
            ti = min(max(t + (i - (k - 1) / 2.0) * spacing, a), b)
            pp, why = _resolve_slot(ctx, (seg, a, b, ti), rule, room, z_place, prefer,
                                    ignore_key=(eq.link_instance_id, eq.elem_id), back=back)
            if pp is not None:
                pp.served = eq.label
                pp.equipment = eq
                if back:
                    pp.host_desc = u"wall {} (back face)".format(
                        getattr(pp, "host_wall_id", None) or seg.wall_id)
                plan.placements.append(pp)
                placed += 1
                if pp.adjust_note:
                    reasons[pp.adjust_note] = 1
            else:
                reasons[why] = reasons.get(why, 0) + 1
        detail = notes_to_text(reasons)
        if size_note:
            detail = size_note + (u"; " + detail if detail else u"")
        plan.add_row(rule, room, u"serves {} ({} from wall)".format(
            eq.label, format_length_u(ctx.doc, dist)), k, placed, detail, served=eq.label)
        if placed < k:
            plan.add_issue(room, rule.label, u"planned {}/{} ({})".format(placed, k, detail),
                           subject=eq.label)
    if outside:
        plan.add_issue(None, rule.label,
                       u"{} equipment item(s) are not inside any target room - ignored".format(outside))
    if too_far:
        _suggest_distance(ctx, plan, rule, too_far)


def _plan_on_equipment(ctx, rule, rooms, all_links, include_outside):
    """Host fixtures ON each equipment item (side + offsets)."""
    from elecplace.equipgeom import FaceFinder, find_3d_view
    plan = ctx.plan
    items, stats = collect_equipment(ctx.doc, ctx.link, all_links, rule.equip_cats,
                                     rule.equip_source, rule.equip_keywords,
                                     types=rule.equip_types)
    _record_sources(plan, rule, stats)
    if not items:
        plan.add_issue(None, rule.label, u"No equipment of the selected categories / names found "
                       u"in {} - check 'Equipment from'".format(source_description(rule.equip_source)))
        return
    finder = None
    if rule.mode == MODE_FACE:
        view = find_3d_view(ctx.doc)
        if view is None:
            plan.add_issue(None, rule.label, u"Face hosting on equipment needs a 3D view without a "
                           u"section box where the equipment (and its link) is visible - open the "
                           u"default {3D} view once and retry")
            return
        finder = FaceFinder(view, equipment_bics(rule.equip_cats))

    inverse = ctx.link.transform.Inverse
    tol = ctx.settings.duplicate_tolerance_ft
    spacing = ctx.settings.equipment_spacing_ft
    outside, no_rooms = 0, {}
    for eq in items:
        room = _room_containing(rooms, inverse.OfPoint(eq.host_point))
        if room is None:
            if not include_outside:
                outside += 1
                continue
            lvl = ctx.pick_level(eq.host_point.Z)
            key = element_name(lvl) if lvl else u"-"
            if key not in no_rooms:
                no_rooms[key] = NoRoom(ctx.link, key)
            room = no_rooms[key]
        try:
            frame = eq.frame()
        except Exception as ex:
            plan.add_issue(room, rule.label, u"cannot read equipment geometry: " + to_text(ex),
                           subject=eq.label)
            continue

        k = rule.per_equipment
        placed, reasons = 0, {}
        own_key = (eq.link_instance_id, eq.elem_id)    # the unit itself is never a clash
        prefer = -1.0 if rule.off_x_ft < 0 else 1.0
        # Offset Z from floor level: shift Z so the fixture ends up at FFL + Offset Z
        off_z = rule.off_z_ft
        if rule.z_ref == ZREF_FFL and rule.side not in ("top", "bottom"):
            floor_z = _floor_z_host(ctx, room, eq)
            if floor_z is None:
                plan.add_issue(room, rule.label, u"floor level unknown - Offset Z measured from the "
                               u"centre of the side", subject=eq.label)
            else:
                centre_z = frame.face_point_xyz(rule.side, 0.0, 0.0, 0.0)[0].Z
                off_z = (floor_z + rule.off_z_ft) - centre_z
        # back panel: neighbouring units of the served category (cubicle rows)
        # overlap this one's box but are not in the way of its back face
        ignore_cats = _served_category_ids(rule) if rule.side == "back" else None
        for i in range(k):
            shift = (i - (k - 1) / 2.0) * spacing
            pp, why, blocked_by = None, u"no valid position", None
            # Slide along the unit's X axis until clear of other equipment
            for slide in ctx.search_offsets(prefer):
                # X / Y / Z in the unit's own axes; several fixtures step along X
                p, normal, ax1, standoff = frame.face_point_xyz(
                    rule.side, rule.off_x_ft + shift + slide, rule.off_y_ft, off_z)
                if rule.mode == MODE_UNHOSTED and abs(standoff) > 1e-9:
                    p = p.Add(normal.Multiply(standoff))  # level-based: honour stand-off
                if ctx.dup.near(p.X, p.Y, p.Z, tol):
                    why = u"duplicate - an element already exists within tolerance"
                    continue
                box, hit_label = ctx.check_clash(rule, p, normal, ax1, ignore_key=own_key,
                                                 ignore_cats=ignore_cats)
                if hit_label:
                    blocked_by = hit_label
                    why = u"clash with {} (no clear spot within {})".format(
                        hit_label, format_length_u(ctx.doc, ctx.settings.clash_max_slide_ft))
                    continue
                cand = Planned(rule, room, None)
                cand.served = eq.label
                cand.equipment = eq
                cand.host_desc = u"equipment {} ({})".format(eq.elem_id, rule.side)
                cand.normal_host = normal
                cand.ref_dir_host = ax1
                horiz = DB.XYZ(normal.X, normal.Y, 0.0)
                cand.rotate_dir = horiz.Normalize() if horiz.GetLength() > 1e-6 else frame.F
                if rule.mode == MODE_FACE:
                    hit = finder.hit(eq, p, normal)
                    if hit is None:
                        why = u"no face of the equipment at this offset (or hidden in the 3D view)"
                        continue
                    cand.link_ref, cand.point_host = hit
                else:
                    cand.point_host = p
                if abs(slide) > 1e-9:
                    cand.adjust_note = u"moved {} along the unit to avoid {}".format(
                        format_length_u(ctx.doc, abs(slide)), blocked_by or u"a taken position")
                pp = cand
                ctx.register(box, rule)
                break
            if pp is None:
                reasons[why] = reasons.get(why, 0) + 1
                continue
            pp.host_level = ctx.pick_level(pp.point_host.Z)
            if pp.host_level is not None:
                pp.level_offset = pp.point_host.Z - pp.host_level.ProjectElevation
            ctx.dup.add(pp.point_host.X, pp.point_host.Y, pp.point_host.Z)
            plan.placements.append(pp)
            placed += 1
            if pp.adjust_note:
                reasons[pp.adjust_note] = 1

        detail = notes_to_text(reasons)
        plan.add_row(rule, room, u"on {} of {}".format(rule.side, eq.label), k, placed, detail,
                     served=eq.label)
        if placed < k:
            plan.add_issue(room, rule.label, u"planned {}/{} ({})".format(placed, k, detail),
                           subject=eq.label)
    if outside:
        plan.add_issue(None, rule.label,
                       u"{} equipment item(s) are not inside any target room - ignored".format(outside))


def build_plan(doc, link, all_links, rooms, rules, settings, dup_index, logger=None):
    """Compute every placement. Read-only - safe outside a transaction."""
    plan = Plan()
    ctx = _Ctx(doc, link, settings, dup_index, logger, plan, all_links)
    by_uid = dict((r.uid, r) for r in rooms)

    for rule in rules:
        if rule.mode is None:
            plan.add_issue(None, rule.label, u"Type rejected: " + rule.mode_reason)
            continue
        targets = [by_uid[u] for u in rule.room_uids if u in by_uid]
        if not targets and not (rule.kind == KIND_ON_EQUIP and rule.room_scope == SCOPE_ALL):
            plan.add_issue(None, rule.label, u"No target rooms match the room filter")
            continue
        try:
            if rule.kind == KIND_ON_EQUIP:
                # 'All rooms' also covers equipment outside any room (plant, voids)
                _plan_on_equipment(ctx, rule, targets, all_links,
                                   include_outside=(rule.room_scope == SCOPE_ALL))
            elif rule.kind == KIND_EQUIP:
                _plan_equipment(ctx, rule, targets, all_links)
            else:
                _plan_walls(ctx, rule, targets)
        except Exception as ex:
            plan.add_issue(None, rule.label, u"Planning error: " + to_text(ex))
            if logger:
                logger.debug(u"planning error for {}: {}".format(rule.label, ex))
    _resolve_data(plan)
    return plan


def _resolve_data(plan):
    """
    Fill Planned.param_values from each rule's mappings (room / equipment
    data read now, written after placement) and show a sample per preview
    row so the values can be checked before anything is placed.
    """
    from elecplace.datamap import render
    sample = {}
    for pp in plan.placements:
        if not pp.rule.mappings:
            continue
        pp.param_values = [(name, render(tpl, pp, plan.missing_tokens))
                           for name, tpl in pp.rule.mappings]
        key = (id(pp.rule), pp.room.uid, pp.served)
        if key not in sample:
            sample[key] = u"; ".join(u"{}={}".format(n, v) for n, v in pp.param_values)
    for row in plan.rows:                   # first fixture of each row as a sample
        room = row["room"]
        if room is not None:
            row["data"] = sample.get((id(row["rule"]), room.uid, row["served"]), u"")
    for token, n in sorted(plan.missing_tokens.items()):
        plan.info.append(u"Data: {} had no value for {} fixture(s) - written as empty text".format(token, n))
