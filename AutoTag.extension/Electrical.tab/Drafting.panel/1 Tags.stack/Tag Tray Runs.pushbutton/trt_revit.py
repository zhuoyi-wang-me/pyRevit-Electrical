# -*- coding: utf-8 -*-
"""Tag Tray Runs - everything that touches the Revit model.

find_targets() only reads. create_tags() is the only function that changes the model: in ONE
transaction it tags the cable tray (and conduit) runs of the active plan, repeating the tag
every N metres along each run, each tag beside its tray where nothing else is.

The view helpers (the flat view frame, crop and annotation crop, tag types, which pieces form one
run, linked elements) are those of Auto Tag Layout, next to this button.
"""
import io
import os
import sys

from Autodesk.Revit import DB

_HERE = os.path.dirname(os.path.abspath(__file__))
_ATL = os.path.join(os.path.dirname(_HERE), 'Auto Tag Layout.pushbutton')
if _ATL not in sys.path:
    sys.path.append(_ATL)

import atl_revit as base                                  # noqa: E402  (Auto Tag Layout)
import trt_layout                                         # noqa: E402

MM = 1.0 / 304.8                                          # one millimetre in feet
M = 1000.0 * MM                                           # one metre in feet
# the categories that can be tagged along their runs: (category, label in the window)
CATEGORIES = (('OST_CableTray', u'Cable Trays'), ('OST_Conduit', u'Conduits'))


class Choice(object):
    """What the user asked for one category."""
    def __init__(self, category, symbol):
        self.category, self.symbol = category, symbol


class Report(object):
    def __init__(self):
        self.created = 0                  # tags placed
        self.runs = 0                     # runs (with one tag text) that got tags
        self.repeated = 0                 # of the tags: the 2nd, 3rd ... of a run
        self.leaders = 0                  # tags that had to stand away from the tray, with a leader
        self.bundles = 0                  # stacks that tag several trays running side by side
        self.already = 0                  # runs left alone: they had a tag in this view already
        self.empty = 0                    # pieces whose tag shows nothing
        self.no_room = 0                  # places along a run where no free spot was found (skipped)
        self.crowded = 0                  # runs whose only tag may cover something (no free spot at all)
        self.failed = []                  # (category, reason)
        self.previewed = 0                # tags worked out for the preview (nothing created)
        self.error = None

    def lines(self):
        out = [u'{0} tag(s) created on {1} run(s).'.format(self.created, self.runs)]
        live = getattr(self, 'live', None)
        if live is not None and live > self.runs:
            out.append(u'{0} run(s) of {1} got NO tag - check them.'.format(live - self.runs, live))
        if self.bundles:
            out.append(u'{0} stack(s) tag the trays running side by side together.'.format(self.bundles))
        if self.repeated:
            out.append(u'{0} of them repeat the tag of a long run.'.format(self.repeated))
        if self.leaders:
            out.append(u'{0} tag(s) stand a little away from their tray, with a leader: there was no room right beside it.'.format(self.leaders))
        if self.already:
            out.append(u'{0} run(s) had a tag in this view already and were left as they are.'.format(self.already))
        if self.empty:
            out.append(u'{0} piece(s) not tagged: the tag has no value to show.'.format(self.empty))
        if self.no_room:
            out.append(u'{0} repeat(s) left out: no free place beside the tray there.'.format(self.no_room))
        if getattr(self, 'crossing', 0):
            out.append(u'{0} run(s) had room only with a leader crossing another leader (never lying on one).'.format(self.crossing))
        if self.crowded:
            out.append(u'{0} run(s) found no free place at all: their tag may cover something - check them.'.format(self.crowded))
            for text in sorted(set(getattr(self, 'crowded_runs', []) or []))[:15]:
                out.append(u'   ' + text.replace(u'\n', u' / '))
        if self.failed:
            reasons = sorted(set(u'{0}: {1}'.format(c, r) for c, r in self.failed))
            out.append(u'{0} piece(s) could not be tagged:'.format(len(self.failed)))
            out.extend(u'   ' + reason for reason in reasons[:6])
        return out


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def view_problem(view):
    problem = base.view_problem(view)
    if problem:
        return problem
    if not base.Frame(view).is_plan:
        return u'Open a plan view: tags along the tray runs are placed in plans (for sections use Auto Tag Layout).'
    return None


def tag_types(doc):
    """{category: [(label, symbol)]} of the tag types that fit each category (multi-category tags too)."""
    types = base.tag_types(doc)
    found = {}
    for category, label in CATEGORIES:
        fitting, seen = [], set()
        for tag_name in base._tag_names(category):
            fitting.extend(types.get(tag_name, []))
        for pair in fitting + types.get(base.MULTI, []):
            if pair[0] not in seen:
                seen.add(pair[0])
                found.setdefault(category, []).append(pair)
    return found


def _category_of(element):
    try:
        return base._bic_name(element.Category)
    except Exception:
        return None


def find_targets(doc, view, include_links=False, selected_ids=None):
    """{category: [Target]} of the trays / conduits seen in the view."""
    wanted = set(category for category, label in CATEGORIES)
    found = dict((category, []) for category in wanted)
    for element in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
        if selected_ids is not None and element.Id.Value not in selected_ids:
            continue
        category = _category_of(element)
        if category in wanted:
            found[category].append(base.Target(element, None))
    if include_links and selected_ids is None:
        for link in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.RevitLinkInstance):
            try:
                if link.GetLinkDocument() is None:
                    continue
                for element in DB.FilteredElementCollector(doc, view.Id, link.Id).WhereElementIsNotElementType():
                    category = _category_of(element)
                    if category in wanted:
                        found[category].append(base.Target(element, link))
            except Exception:
                pass                                        # link not loaded, or a Revit without this collector
    return found


def _half_width(element):
    """Half the width of a tray (half the diameter of a conduit), in feet; a small value when unknown."""
    for name in ('Width', 'Diameter'):
        try:
            value = float(getattr(element, name))
            if value > 0:
                return value / 2.0
        except Exception:
            pass
    return 0.1


# annotation that spans (parts of) the view and must not keep the tags away
_NOT_IN_THE_WAY = ('OST_Grids', 'OST_Levels', 'OST_Viewers', 'OST_SectionBox', 'OST_Matchline', 'OST_VolumeOfInterest',
                   'OST_CropBoundary', 'OST_ReferenceViewer', 'OST_Elev', 'OST_Sections', 'OST_Callouts', 'OST_IOSModelGroups')


def _annotation_boxes(doc, view, frame, extent=None):
    """The boxes of the annotation in the view already: tags, text notes, and every other annotation
    (generic annotation, keynotes, symbols, dimensions ...) - except grids, levels, section marks and the like."""
    boxes = list(base.existing_boxes(doc, view, frame))
    # FIX: only tags and text notes were avoided, and a tag was put over a "FOR DETAILS REFER TO ..." note
    try:
        big = ((extent[2] - extent[0]) * 0.5, (extent[3] - extent[1]) * 0.5) if extent is not None else None
        for element in DB.FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType():
            try:
                category = element.Category
                if category is None or category.CategoryType != DB.CategoryType.Annotation:
                    continue
                if isinstance(element, (DB.IndependentTag, DB.TextNote)) or base._bic_name(category) in _NOT_IN_THE_WAY:
                    continue                                # tags and notes are in already; the others span the view
                box = element.get_BoundingBox(view)
                if box is None:
                    continue
                rect = base._uv_rect(box, frame)
                if big is not None and (rect[2] - rect[0] > big[0] or rect[3] - rect[1] > big[1]):
                    continue                                # something drawn across the view (a match line ...)
                boxes.append(rect)
            except Exception:
                pass
    except Exception:
        pass
    try:
        for note in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.TextNote):
            try:
                box = note.get_BoundingBox(view)
                if box is not None:
                    boxes.append(base._uv_rect(box, frame))
            except Exception:
                pass
    except Exception:
        pass
    return boxes


# ---------------------------------------------------------------------------
# creating the tags - the only place where the model is changed
# ---------------------------------------------------------------------------
class _Piece(object):
    """One tray piece in the view, with the tag made on it to learn the text and its size."""
    def __init__(self, target, category, symbol, anchor, seg, half):
        self.target, self.category, self.symbol = target, category, symbol
        self.anchor, self.seg, self.half = anchor, seg, half
        self.made = None                  # atl_revit._Made of the first tag (to measure the text)
        self.text = u''
        self.size = None                  # (w, h) of the text in the view
        self.used = False                 # the first tag has been placed (is kept)
        self.rect = None                  # (u low, v low, u high, v high) of the piece in the view


def _new_tag(doc, view, frame, piece, point):
    return DB.IndependentTag.Create(doc, piece.symbol.Id, view.Id, piece.target.reference(), False,
                                    DB.TagOrientation.Horizontal, frame.xyz(point[0], point[1]))


def _put(tag, frame, piece, centre, leader_end, elbow=None):
    """Move the tag so that the middle of its text is at `centre`.
    leader_end: (u, v) on the tray for a leader, or None for no leader; elbow: the corner of the leader."""
    made = piece.made
    # the middle of the text relative to the head, as measured on the first tag of this piece
    shift = (made.centre[0] - made.start[0], made.centre[1] - made.start[1])
    head = frame.xyz(centre[0] - shift[0], centre[1] - shift[1])
    if leader_end is not None:
        tag.HasLeader = True
        tag.LeaderEndCondition = DB.LeaderEndCondition.Free
    tag.TagHeadPosition = head
    if leader_end is not None:
        reference = list(tag.GetTaggedReferences())[0]
        tag.SetLeaderEnd(reference, frame.xyz(leader_end[0], leader_end[1]))
        if elbow is None:                                   # one straight line: the corner lies on it, half way
            elbow = ((leader_end[0] + centre[0]) / 2.0, (leader_end[1] + centre[1]) / 2.0)
        tag.SetLeaderElbow(reference, frame.xyz(elbow[0], elbow[1]))


# ---------------------------------------------------------------------------
# walls and the hatched area
# ---------------------------------------------------------------------------
def _curve_points(curve, transform=None):
    """The points of a curve (an arc as many short pieces) as it stands in the host model."""
    try:
        points = list(curve.Tessellate())
    except Exception:
        try:
            points = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
        except Exception:
            return []
    if transform is not None:
        points = [transform.OfPoint(p) for p in points]
    return points


def find_walls(doc, view, frame):
    """[(a, b, half width)] of the walls seen in the view - this model's and those of the linked models -
    curved walls as many short pieces."""
    found = []

    def add(element, transform):
        try:
            curve = element.Location.Curve
        except Exception:
            return
        try:
            half = float(element.Width) / 2.0
        except Exception:
            half = 0.2
        points = [frame.uv(p) for p in _curve_points(curve, transform)]
        for a, b in zip(points, points[1:]):
            found.append((a, b, half))

    try:
        for wall in DB.FilteredElementCollector(doc, view.Id).OfCategory(DB.BuiltInCategory.OST_Walls).WhereElementIsNotElementType():
            add(wall, None)
    except Exception:
        pass
    try:
        for link in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.RevitLinkInstance):
            try:
                if link.GetLinkDocument() is None:
                    continue
                transform = link.GetTotalTransform()
                for wall in DB.FilteredElementCollector(doc, view.Id, link.Id).OfCategory(DB.BuiltInCategory.OST_Walls).WhereElementIsNotElementType():
                    add(wall, transform)
            except Exception:
                pass
    except Exception:
        pass
    return found


def _type_name(doc, element):
    try:
        element_type = doc.GetElement(element.GetTypeId())
        return DB.Element.Name.GetValue(element_type)
    except Exception:
        try:
            return doc.GetElement(element.GetTypeId()).Name
        except Exception:
            return u'?'


def region_types(doc, view):
    """{filled region type name: count} of the filled regions (hatches) drawn in the view."""
    found = {}
    try:
        for region in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.FilledRegion):
            name = _type_name(doc, region)
            found[name] = found.get(name, 0) + 1
    except Exception:
        pass
    return found


def find_areas(doc, view, frame, names=None):
    """[trt_layout.Area] of the filled regions in the view (only of the type names given, when given)."""
    areas = []
    try:
        for region in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.FilledRegion):
            if names and _type_name(doc, region) not in names:
                continue
            loops = []
            for loop in region.GetBoundaries():
                points = []
                for curve in loop:
                    pts = [frame.uv(p) for p in _curve_points(curve)]
                    points.extend(pts[:-1] if len(pts) > 1 else pts)
                if len(points) >= 3:
                    loops.append(points)
            if loops:
                areas.append(trt_layout.Area(loops))
    except Exception:
        pass
    return areas


# ---------------------------------------------------------------------------
# the arrangements offered in the preview
# ---------------------------------------------------------------------------
LEADER, BESIDE = 'leader', 'beside'
STYLES = ((LEADER, u'Away from the tray, with a leader'), (BESIDE, u'Right beside the tray, no leader'))
MAX_BUNDLE = 12                           # trays in one stack at most


class Variant(object):
    def __init__(self, key, title, style, bundle, prefer):
        self.key, self.title, self.style, self.bundle, self.prefer = key, title, style, bundle, prefer


VARIANTS = (Variant('stack_up', u'Stacks above / right of the trays', LEADER, True, 1),
            Variant('stack_down', u'Stacks below / left of the trays', LEADER, True, -1),
            Variant('single', u'One by one, with a leader', LEADER, False, None),
            Variant('beside', u'Right beside the trays, no leader', BESIDE, False, None))


def variant(key):
    for one in VARIANTS:
        if one.key == key:
            return one
    return None


class Placement(object):
    """Where one tag goes: the piece it tags, the middle of its text, its arrow and corner (leader)."""
    def __init__(self, piece, centre, arrow=None, corner=None):
        self.piece, self.centre, self.arrow, self.corner = piece, centre, arrow, corner


class Option(object):
    """One arrangement worked out for the preview: what it places, and what the drawing needs."""
    def __init__(self, var, placements, counts):
        self.key, self.title = var.key, var.title
        self.placements, self.counts = placements, counts
        self.best = False
        self.drawing = None               # filled in by create_tags: trays, walls, areas, crop

    def rank(self):
        left_out = self.counts['no_room'] + self.counts['crowded'] * 3
        return (left_out, -len(self.placements))

    def summary(self):
        c = self.counts
        text = u'{0} tag(s) on {1} run(s)'.format(len(self.placements), c['runs'])
        if c['bundles']:
            text += u', {0} stack(s)'.format(c['bundles'])
        if c['no_room']:
            text += u', {0} left out (no room)'.format(c['no_room'])
        if c['crowded']:
            text += u', {0} crowded'.format(c['crowded'])
        return text


# ---------------------------------------------------------------------------
# working out the places (no change to the model)
# ---------------------------------------------------------------------------
def _spots(members, spacing, extent, scale):
    w, h = members[0].size
    spots = trt_layout.candidates([(i, m.seg) for i, m in enumerate(members)], max(spacing / 8.0, 2.0 * h),
                                  max(w / 2.0, 3.0 * MM * scale))
    if extent is not None:
        inside = [s for s in spots if extent[0] <= s[0][0] <= extent[2] and extent[1] <= s[0][1] <= extent[3]]
        spots = inside or spots
    return spots


def _new_leaders(world, s):
    """The store of placed leader pieces: a new leader may not cross one or lie on / right beside one."""
    return trt_layout.Leaders(world.cell, 1.2 * MM * s['scale'])


def _record_leader(world, leaders, s, centre, corner, arrow, w):
    """A placed leader: both of its pieces (tray to corner, corner to the text) are kept clear of later texts,
    and later leaders may not cross or cover them. FIX: only the first piece used to be recorded."""
    points = trt_layout.leader_points(centre, corner, arrow, w)
    for p, q in zip(points, points[1:]):
        world.add_line((p, q, s['pad'] / 2.0))
    leaders.add(points)


def _plan_single(live, runs, var, world, s):
    """Each run on its own (leader style without stacks, or the no-leader style)."""
    plan, counts = [], dict(runs=0, bundles=0, no_room=0, crowded=0, leaders=0, crowded_runs=[])
    arrows, legs = [], []
    leaders = _new_leaders(world, s)
    for key in live:
        members = runs[key]
        w, h = members[0].size
        lengths = dict((i, trt_layout._length(*m.seg)) for i, m in enumerate(members))
        chosen = trt_layout.pick(_spots(members, s['spacing'], s['extent'], s['scale']), s['spacing'], lengths,
                                 avoid=arrows, clear=w + 2.0 * s['pad'])
        placed, run_side = 0, var.prefer
        for n, (point, index, direction) in enumerate(chosen):
            member = members[index]
            areas = trt_layout.in_areas(point, s['areas'])
            spot, corner, arrow = None, None, point
            if var.style == LEADER:
                if trt_layout._length(*member.seg) < 1e-9:
                    # a riser seen end-on: moved across the riser when a riser beside it has a leader on that line
                    found = _riser_leader(member, point, var, world, s, legs, prefer=run_side, leaders=leaders)
                else:
                    found = trt_layout.place_leader(point, direction, w, h, s['reach'], s['shoulder'], world, world, s['limits'], s['pad'],
                                                    own_half=member.half, prefer=run_side, areas=areas, leaders=leaders)
                if found is not None:
                    centre, corner, run_side, arrow = found
                    spot = (centre, True)
            if spot is None:
                spot = trt_layout.place(point, direction, w, h, s['offset'], world, world, s['limits'], s['pad'],
                                        own_half=member.half, areas=areas)
                if spot is not None and var.style == LEADER:
                    spot = (spot[0], True)
            if spot is None:
                if placed or n < len(chosen) - 1:
                    counts['no_room'] += 1
                    continue
                # the run has no tag yet and this is the last chance: beside the tray, even if crowded
                normal = (-direction[1], direction[0])
                distance = member.half + s['offset'] + (abs(normal[0]) * w + abs(normal[1]) * h) / 2.0
                spot = ((point[0] + normal[0] * distance, point[1] + normal[1] * distance), False)
                counts['crowded'] += 1
                counts['crowded_runs'].append(member.text)
            centre, leader = spot
            plan.append(Placement(member, centre, arrow if leader else None, corner))
            world.add_box(trt_layout.box_at(centre, w, h))
            if leader:
                _record_leader(world, leaders, s, centre, corner, arrow, w)
                legs.append((arrow, corner if corner is not None else centre))
                counts['leaders'] += 1 if var.style == BESIDE else 0
            arrows.append(arrow)
            placed += 1
        if placed:
            counts['runs'] += 1
    counts['live'] = len(live)
    return plan, counts


def _on_one_line(leg, legs, tolerance=1e-4):
    """True when the leader (arrow, corner) lies on a piece of the line of a leader placed before."""
    (a, b) = leg
    du, dv = b[0] - a[0], b[1] - a[1]
    length = (du * du + dv * dv) ** 0.5
    if length < tolerance:
        return False
    ux, uy = du / length, dv / length
    for c, d in legs:
        if abs((c[0] - a[0]) * uy - (c[1] - a[1]) * ux) > tolerance or abs((d[0] - a[0]) * uy - (d[1] - a[1]) * ux) > tolerance:
            continue
        lo, hi = sorted(((c[0] - a[0]) * ux + (c[1] - a[1]) * uy, (d[0] - a[0]) * ux + (d[1] - a[1]) * uy))
        if min(hi, length) - max(lo, 0.0) > tolerance:
            return True
    return False


def _riser_leader(piece, point, var, world, s, legs, prefer=None, leaders=None):
    """A leader for a riser seen end-on: straight up or down out of it to its text. When that leader would lie on
    the leader of a riser beside it, the arrow moves across the riser (its box in the view) until it is free.
    Returns (text centre, corner, side, arrow) or None."""
    rect = piece.rect
    half_w = (rect[2] - rect[0]) / 2.0 * 0.7 if rect is not None else 0.0
    step = 1.5 * MM * s['scale']
    offsets = [0.0]
    k = 1
    while k * step <= half_w + 1e-9 and k <= 8:
        offsets.extend([k * step, -k * step])
        k += 1
    w, h = piece.size
    for off in offsets:
        spot = (point[0] + off, point[1])
        found = trt_layout.place_leader(spot, (1.0, 0.0), w, h, s['reach'], s['shoulder'], world, world, s['limits'], s['pad'],
                                        own_half=piece.half, prefer=prefer, slides=0, areas=trt_layout.in_areas(point, s['areas']),
                                        leaders=leaders)
        if found is not None and not _on_one_line((found[3], found[1]), legs):
            return found
    return None


def _plan_bundles(live, runs, var, world, s):
    """The leader style with stacks: every N metres along the runs, the trays running side by side there are
    tagged together - leaders next to each other, the texts in one stack (as on a containment drawing).
    A run that got its tags from the stacks of other runs is only tagged where it still has a gap."""
    plan, counts = [], dict(runs=0, bundles=0, no_room=0, crowded=0, leaders=0, crowded_runs=[])
    lead_gap = 1.5 * MM * s['scale']                        # between the leaders of one stack
    leaders = _new_leaders(world, s)
    spacing = s['spacing']
    total = dict((key, sum(trt_layout._length(*m.seg) for m in runs[key])) for key in live)
    taken = dict((key, []) for key in live)                 # where each run's tags point at it
    legs = []                                               # (arrow, corner) of every leader placed: none may lie on another
    sides = {}                                              # the side of the first stack of a run: the others follow
    for key in sorted(live, key=lambda k: -total[k]):       # the longest runs set the stations
        members = runs[key]
        lengths = dict((i, trt_layout._length(*m.seg)) for i, m in enumerate(members))
        for point, index, direction in trt_layout.pick(_spots(members, spacing, s['extent'], s['scale']), spacing, lengths, taken=taken[key]):
            if any(trt_layout._length(point, t) < spacing * 0.5 for t in taken[key]):
                continue
            if trt_layout._length(*members[index].seg) < 1e-9:
                # FIX: a riser seen end-on (one point in the plan) was never tagged in this mode - a stack needs the
                # line of each tray, and a point has none. It gets a leader of its own, moved across its tray when
                # the place is taken by the leader of a riser next to it.
                m = members[index]
                found = _riser_leader(m, point, var, world, s, legs, prefer=sides.get(key, var.prefer), leaders=leaders)
                if found is None:
                    counts['no_room'] += 1
                    continue
                centre, corner, side, arrow = found
                plan.append(Placement(m, centre, arrow, corner))
                world.add_box(trt_layout.box_at(centre, m.size[0], m.size[1]))
                _record_leader(world, leaders, s, centre, corner, arrow, m.size[0])
                legs.append((arrow, corner))
                taken[key].append(arrow)
                sides.setdefault(key, side)
                continue
            across = trt_layout.runs_across(direction)
            bundle = [(key, members[index], point)]
            cut = point[0] if across else point[1]
            for other in live:
                if other == key or len(bundle) >= MAX_BUNDLE:
                    continue
                best = None
                for m in runs[other]:
                    a, b = m.seg
                    length = trt_layout._length(a, b)
                    if length < 1e-9 or not trt_layout.parallel(direction, ((b[0] - a[0]) / length, (b[1] - a[1]) / length)):
                        continue
                    p = trt_layout.point_at(m.seg, across, cut)
                    if p is None:
                        continue
                    distance = abs(p[1] - point[1]) if across else abs(p[0] - point[0])
                    if distance <= s['bundle'] and (best is None or distance < best[0]):
                        best = (distance, m, p)
                if best is None or any(trt_layout._length(best[2], t) < spacing * 0.5 for t in taken[other]):
                    continue                                # not here, or tagged nearby already
                bundle.append((other, best[1], best[2]))
            bundle.sort(key=lambda item: -(item[2][1] if across else item[2][0]))      # the highest (rightmost) tray first
            areas = trt_layout.in_areas(point, s['areas'])
            found = None
            while bundle and found is None:
                spec = [dict(seg=m.seg, half=m.half, w=m.size[0], h=m.size[1]) for k, m, p in bundle]
                found = trt_layout.place_bundle(point, direction, spec, s['reach'], s['shoulder'], world, world, s['limits'], s['pad'],
                                                lead_gap, prefer=sides.get(key, var.prefer), areas=areas, leaders=leaders)
                if found is None:
                    if len(bundle) == 1:
                        break
                    # no room for the whole stack: drop the tray farthest from this run, try again
                    bundle.sort(key=lambda item: abs((item[2][1] - point[1]) if across else (item[2][0] - point[0])))
                    bundle = bundle[:-1]
                    bundle.sort(key=lambda item: -(item[2][1] if across else item[2][0]))
            if found is None:
                counts['no_room'] += 1
                continue
            items, side = found
            for (k, m, p), (centre, corner, arrow) in zip(bundle, items):
                plan.append(Placement(m, centre, arrow, corner))
                legs.append((arrow, corner))
                world.add_box(trt_layout.box_at(centre, m.size[0], m.size[1]))
                _record_leader(world, leaders, s, centre, corner, arrow, m.size[0])     # later texts / leaders keep off
                taken[k].append(arrow)
                sides.setdefault(k, side)
            if len(bundle) > 1:
                counts['bundles'] += 1

    # FIX: a run whose every stack try failed got NO tag at all (the one-by-one style always gives each run
    # one). Every run without a tag now gets one of its own: a leader anywhere along the run (searching
    # farther out than a stack does), else right beside the tray, else - reported - beside it all the same.
    for key in sorted(live, key=lambda k: -total[k]):
        if taken[key]:
            continue
        members = runs[key]
        w, h = members[0].size
        lengths = dict((i, trt_layout._length(*m.seg)) for i, m in enumerate(members))
        spots = _spots(members, spacing, s['extent'], s['scale'])
        # the middle of the longest piece first, then the rest of the run
        order = trt_layout.pick(spots, 1e12, lengths)[:1] + spots        # (a huge spacing: only the middle point)
        done = False
        # 1. a clean leader near the tray, 2. right beside the tray, 3. a clean leader twice as far out,
        # 4. a leader that may CROSS another one (never lie on one) - before a tag is squeezed in anywhere
        for reach_factor, rings, slides, cross in ((1.0, 8, 8, False), (2.0, 12, 12, False), (2.0, 12, 12, True)):
            leaders.allow_cross = cross
            for point, index, direction in order:
                member = members[index]
                areas = trt_layout.in_areas(point, s['areas'])
                if trt_layout._length(*member.seg) < 1e-9:
                    found = _riser_leader(member, point, var, world, s, legs, prefer=sides.get(key, var.prefer), leaders=leaders)
                else:
                    found = trt_layout.place_leader(point, direction, w, h, s['reach'] * reach_factor, s['shoulder'], world, world,
                                                    s['limits'], s['pad'], own_half=member.half, prefer=sides.get(key, var.prefer),
                                                    rings=rings, slides=slides, areas=areas, leaders=leaders)
                if found is not None:
                    centre, corner, side, arrow = found
                    plan.append(Placement(member, centre, arrow, corner))
                    world.add_box(trt_layout.box_at(centre, w, h))
                    _record_leader(world, leaders, s, centre, corner, arrow, w)
                    legs.append((arrow, corner))
                    taken[key].append(arrow)
                    if cross:
                        counts['crossing'] = counts.get('crossing', 0) + 1
                    done = True
                    break
                if reach_factor == 1.0:
                    spot = trt_layout.place(point, direction, w, h, s['offset'], world, world, s['limits'], s['pad'],
                                            own_half=member.half, areas=areas)
                    if spot is not None:
                        plan.append(Placement(member, spot[0], None, None))
                        world.add_box(trt_layout.box_at(spot[0], w, h))
                        taken[key].append(point)
                        done = True
                        break
            if done:
                break
        leaders.allow_cross = False
        if not done and spots:
            point, index, direction = order[0]
            member = members[index]
            normal = (-direction[1], direction[0])
            distance = member.half + s['offset'] + (abs(normal[0]) * w + abs(normal[1]) * h) / 2.0
            centre = (point[0] + normal[0] * distance, point[1] + normal[1] * distance)
            plan.append(Placement(member, centre, None, None))
            world.add_box(trt_layout.box_at(centre, w, h))
            taken[key].append(point)
            counts['crowded'] += 1
            counts['crowded_runs'].append(member.text)
    counts['runs'] = len([key for key in live if taken[key]])
    counts['live'] = len(live)
    return plan, counts


def _plan(live, runs, var, base_world, s):
    world = base_world.copy()
    if var.style == LEADER and var.bundle:
        return _plan_bundles(live, runs, var, world, s)
    return _plan_single(live, runs, var, world, s)


# ---------------------------------------------------------------------------
# creating the tags - the only place where the model is changed
# ---------------------------------------------------------------------------
DETAILS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'TagTrayRuns', 'last_run.txt')


def _write_details(view, report, plan, preview):
    """What the last run did, one line per tag: for checking a result that looks wrong in Revit."""
    try:
        lines = [u'Tag Tray Runs - {0} - view "{1}" (scale 1:{2})'.format(u'preview' if preview else u'create', view.Name, view.Scale)]
        lines.extend(report.lines())
        lines.append(u'')
        lines.append(u'text | link, element | arrow (u, v) | corner | text centre')
        for p in plan or []:
            lines.append(u'{0} | {1} | {2} | {3} | {4}'.format(
                p.piece.text.replace(u'\n', u' / '), p.piece.target.key, _fmt(p.arrow), _fmt(p.corner), _fmt(p.centre)))
        folder = os.path.dirname(DETAILS)
        if not os.path.isdir(folder):
            os.makedirs(folder)
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
        return u'?'


def create_tags(doc, view, choices, spacing_m=15.0, offset_mm=2.0, gap_mm=1.0, include_links=False,
                skip_tagged=True, inside_annotation=True, selected_ids=None, style=LEADER, leader_mm=8.0,
                bundle=True, bundle_mm=15.0, hatch=True, hatch_names=None, walls=True, variant_key=None, preview=None):
    """choices = [Choice]. Tags every run, again every spacing_m metres along it. Returns a Report.

    style LEADER: as drawn by hand - the text away from the tray (leader_mm on the sheet), a leader going straight
          out of the tray and turning to the start of the text. With `bundle` the trays running side by side
          (within bundle_mm on the sheet) are tagged together at each place, in one stack.
    style BESIDE: the text right beside the tray (offset_mm from its edge), without leader.
    hatch: where a tray runs through a filled region of the view (of the type names hatch_names, or any), its
          texts stand inside that region. walls: texts keep off the walls (also curved and linked ones).
    variant_key: one of VARIANTS (overrides style / bundle). preview: a list - NOTHING is kept in the model:
          every variant is worked out, an Option for each is put into the list, and the transaction is rolled back."""
    report = Report()
    problem = view_problem(view)
    if problem:
        report.error = problem
        return report
    frame = base.Frame(view)
    scale = float(view.Scale or 1)
    extent = base.crop_extent(view, frame)
    limits = None
    if inside_annotation:
        limits = base.annotation_extent(view, frame, extent, scale) or extent
        if limits is not None:
            edge = 1.0 * MM * scale
            limits = (limits[0] + edge, limits[1] + edge, limits[2] - edge, limits[3] - edge)
    s = dict(spacing=spacing_m * M, offset=offset_mm * MM * scale, pad=gap_mm * MM * scale, reach=leader_mm * MM * scale,
             shoulder=1.0 * MM * scale, bundle=bundle_mm * MM * scale, extent=extent, limits=limits, scale=scale, areas=None)
    existing = base.tagged_keys(doc, view) if skip_tagged else set()
    world = trt_layout.World(20.0 * MM * scale)
    for box in _annotation_boxes(doc, view, frame, extent):
        world.add_box(box)
    wall_lines = find_walls(doc, view, frame) if walls else []
    for line in wall_lines:
        world.add_line(line)
    s['areas'] = find_areas(doc, view, frame, hatch_names) if hatch else []
    targets = find_targets(doc, view, include_links, selected_ids)
    chosen_variant = variant(variant_key) if variant_key else Variant('asked', u'', style, bundle, None)

    transaction = DB.Transaction(doc, 'Tag Tray Runs')
    transaction.Start()
    try:
        pieces, tray_lines = [], []
        for choice in choices:
            if not choice.symbol.IsActive:
                choice.symbol.Activate()
            for target in targets.get(choice.category, []):
                try:
                    found = target.geometry(view, frame, extent)
                except Exception:
                    found = None
                if not found:
                    continue
                anchor, seg = found[0], found[1]
                if extent is not None:
                    # only what is seen inside the crop region (the clipped piece has its middle inside)
                    look = ((seg[0][0] + seg[1][0]) / 2.0, (seg[0][1] + seg[1][1]) / 2.0) if seg is not None else anchor
                    if not (extent[0] - 1e-6 <= look[0] <= extent[2] + 1e-6 and extent[1] - 1e-6 <= look[1] <= extent[3] + 1e-6):
                        continue
                half = _half_width(target.element)
                piece = _Piece(target, choice.category, choice.symbol, anchor, seg or (anchor, anchor), half)
                try:
                    piece.rect = target._box_uv(view, frame)
                except Exception:
                    piece.rect = None
                if seg is not None:
                    world.add_line((seg[0], seg[1], half))  # texts keep off every tray
                    tray_lines.append((seg[0], seg[1], half))
                pieces.append(piece)
        # one tag on every piece, to learn what it shows and how big that is
        for piece in pieces:
            try:
                tag = _new_tag(doc, view, frame, piece, piece.anchor)
                piece.made = base._Made(tag, piece.target, None, piece.anchor)
            except Exception as error:
                report.failed.append((piece.category, (u'{0}'.format(error).splitlines() or [u'error'])[0]))
        doc.Regenerate()
        runs, caches = {}, {}
        for piece in pieces:
            if piece.made is None:
                continue
            try:
                piece.text = (piece.made.tag.TagText or u'').strip()
            except Exception:
                piece.text = u''
            piece.size = base._measure(piece.made, view, frame, piece.text)
            if not piece.text or piece.text == u'?' or piece.size is None:
                report.empty += 1
                continue
            link_key = piece.target.key[0]
            run = base.run_of(piece.target.element, caches.setdefault(link_key, {}))
            runs.setdefault((piece.category, link_key, run, piece.text), []).append(piece)
        live = []
        for key in sorted(runs, key=lambda k: (k[0], k[3], k[1], k[2])):
            if any(member.target.key in existing for member in runs[key]):
                report.already += 1
            else:
                live.append(key)

        if preview is not None:
            # only looking: every arrangement is worked out, nothing is kept
            options = []
            for var in VARIANTS:
                plan, counts = _plan(live, runs, var, world, s)
                option = Option(var, plan, counts)
                option.drawing = dict(trays=tray_lines, walls=wall_lines, areas=s['areas'], crop=extent, limits=limits)
                options.append(option)
            if options:
                min(options, key=lambda o: o.rank()).best = True
            preview.extend(options)
            report.previewed = sum(len(o.placements) for o in options)
            transaction.RollBack()
            return report

        plan, counts = _plan(live, runs, chosen_variant, world, s)
        for placement in plan:
            piece = placement.piece
            try:
                if not piece.used:
                    tag = piece.made.tag
                    piece.used = True
                else:
                    tag = _new_tag(doc, view, frame, piece, placement.arrow or placement.centre)
                _put(tag, frame, piece, placement.centre, placement.arrow, placement.corner)
                report.created += 1
            except Exception as error:
                report.failed.append((piece.category, (u'{0}'.format(error).splitlines() or [u'error'])[0]))
        report.runs, report.bundles = counts['runs'], counts['bundles']
        report.no_room, report.crowded, report.leaders = counts['no_room'], counts['crowded'], counts['leaders']
        report.crowded_runs = counts.get('crowded_runs', [])
        report.crossing = counts.get('crossing', 0)
        report.live = counts.get('live', len(live))
        report.repeated = max(report.created - report.runs, 0)
        _write_details(view, report, plan, False)
        for piece in pieces:                                # the measuring tags that are not needed
            if piece.made is not None and not piece.used:
                try:
                    doc.Delete(piece.made.tag.Id)
                except Exception:
                    pass
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
