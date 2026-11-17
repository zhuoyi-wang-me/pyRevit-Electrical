# -*- coding: utf-8 -*-
"""Tag Tray Runs - where the tags go (pure geometry, no Revit).

Everything is in the flat coordinates of the view: u to the right, v upwards, in feet of the model.

    candidates()  points along the pieces of one run where a tag could stand
    pick()        of those, points that are at least `spacing` apart, spread over the whole run
    place()       a free spot for the text beside the tray at such a point (or a little away, with a leader)
"""
import math


def _length(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def candidates(pieces, step, margin):
    """Points of a run where a tag could stand.

    pieces: [(index, segment)] - segment ((u, v), (u, v)) of a tray piece as seen in the view, or a single
            point ((u, v), (u, v)) for a tray seen end-on (a riser).
    step:   distance between neighbouring points along a piece.
    margin: no point nearer than this to the end of a piece (bends and tees stand there).
    Returns [(point, index, direction)] - direction is the unit vector along the piece."""
    found = []
    for index, (a, b) in pieces:
        length = _length(a, b)
        if length < 1e-9:
            found.append(((a[0], a[1]), index, (1.0, 0.0)))
            continue
        direction = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
        usable = length - 2.0 * margin
        if usable <= 0:                                     # a short piece: only its middle
            found.append((((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0), index, direction))
            continue
        count = int(usable / step) + 1
        if count % 2 == 0:
            count += 1                                      # an odd count: one point exactly in the middle
        for k in range(count):
            t = margin + (usable * (k / float(count - 1)) if count > 1 else usable / 2.0)
            found.append(((a[0] + direction[0] * t, a[1] + direction[1] * t), index, direction))
    return found


def pick(found, spacing, lengths=None, pieces=None, avoid=None, clear=0.0, taken=None):
    """As _pick, but keeping away from `avoid` (the arrows of the runs tagged before): parallel runs do not
    all put their tags at the same place. Points nearer than `clear` to an avoided point are used only
    when the run has no other points."""
    if avoid and clear > 0:
        away = [item for item in found if all(_length(item[0], other) >= clear for other in avoid)]
        if away:
            found = away
    return _pick(found, spacing, lengths, taken)


def _pick(found, spacing, lengths=None, taken=None):
    """Points at least `spacing` apart, spread over the run (farthest-point sampling).

    The first point is the middle of the longest piece; then always the point farthest from those taken,
    as long as it is at least `spacing` away. A run always gets one point.
    lengths: {index: length of that piece}, to find the longest piece."""
    if not found:
        return []
    if taken:
        # the run has tags already (from the stacks of other runs): only the gaps between them are filled
        near = [min(_length(item[0], t) for t in taken) for item in found]
        chosen = []
        while True:
            best = max(range(len(found)), key=lambda i: near[i])
            if near[best] < spacing - 1e-9:
                return chosen
            chosen.append(found[best])
            point = found[best][0]
            near = [min(near[i], _length(found[i][0], point)) for i in range(len(found))]
    if lengths:
        longest = max(lengths, key=lambda index: lengths[index])
        own = [item for item in found if item[1] == longest] or found
    else:
        own = found
    # the point nearest to the middle of that piece (of all points, when the piece is not known)
    us, vs = [item[0][0] for item in own], [item[0][1] for item in own]
    centre = ((min(us) + max(us)) / 2.0, (min(vs) + max(vs)) / 2.0)
    mid = min(own, key=lambda item: _length(item[0], centre))
    chosen = [mid]
    near = [_length(item[0], mid[0]) for item in found]
    while True:
        best = max(range(len(found)), key=lambda i: near[i])
        if near[best] < spacing - 1e-9:
            break
        chosen.append(found[best])
        point = found[best][0]
        near = [min(near[i], _length(found[i][0], point)) for i in range(len(found))]
    return chosen


def _touch(a, b, pad=0.0):
    """True when the boxes a and b (left, bottom, right, top) overlap, or come nearer than pad."""
    return a[0] < b[2] + pad and b[0] < a[2] + pad and a[1] < b[3] + pad and b[1] < a[3] + pad


def _crosses(box, line):
    """True when the line ((u, v), (u, v), half width) runs through the box (made wider by its half width)."""
    (a, b, half) = line
    l, bt, r, t = box[0] - half, box[1] - half, box[2] + half, box[3] + half
    t0, t1 = 0.0, 1.0
    du, dv = b[0] - a[0], b[1] - a[1]
    for p, q in ((-du, a[0] - l), (du, r - a[0]), (-dv, a[1] - bt), (dv, t - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        ratio = q / p
        if p < 0:
            t0 = max(t0, ratio)
        else:
            t1 = min(t1, ratio)
        if t0 > t1:
            return False
    return True


def _inside(box, limits):
    return limits is None or (box[0] >= limits[0] and box[1] >= limits[1] and box[2] <= limits[2] and box[3] <= limits[3])


# ---------------------------------------------------------------------------
# what is in the way: a grid of cells, so that a text is only checked against what is near it
# (a stadium plan has thousands of wall pieces)
# ---------------------------------------------------------------------------
class World(object):
    """The texts (boxes) and lines (trays, walls, leaders: ((u, v), (u, v), half width)) that a new text must keep off."""

    def __init__(self, cell):
        self.cell = float(cell)
        self.boxes, self.lines = [], []
        self._cells_b, self._cells_l = {}, {}

    def _keys(self, l, b, r, t):
        c = self.cell
        i0, i1, j0, j1 = int(math.floor(l / c)), int(math.floor(r / c)), int(math.floor(b / c)), int(math.floor(t / c))
        if (i1 - i0 + 1) * (j1 - j0 + 1) > 4000:            # something huge: one key for it, checked always
            return [None]
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]

    def add_box(self, box):
        self.boxes.append(box)
        for key in self._keys(*box):
            self._cells_b.setdefault(key, []).append(len(self.boxes) - 1)

    def add_line(self, line):
        (a, b, half) = line
        self.lines.append(line)
        for key in self._keys(min(a[0], b[0]) - half, min(a[1], b[1]) - half, max(a[0], b[0]) + half, max(a[1], b[1]) + half):
            self._cells_l.setdefault(key, []).append(len(self.lines) - 1)

    def _near(self, cells, items, rect, pad):
        found = set(cells.get(None, []))
        for key in self._keys(rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad):
            found.update(cells.get(key, ()))
        return [items[i] for i in sorted(found)]

    def boxes_near(self, rect, pad=0.0):
        return self._near(self._cells_b, self.boxes, rect, pad)

    def lines_near(self, rect, pad=0.0):
        return self._near(self._cells_l, self.lines, rect, pad)

    def copy(self):
        other = World(self.cell)
        other.boxes, other.lines = list(self.boxes), list(self.lines)
        other._cells_b = dict((k, list(v)) for k, v in self._cells_b.items())
        other._cells_l = dict((k, list(v)) for k, v in self._cells_l.items())
        return other


def _boxes(boxes, rect, pad=0.0):
    return boxes.boxes_near(rect, pad) if hasattr(boxes, 'boxes_near') else boxes


def _lines(lines, rect, pad=0.0):
    return lines.lines_near(rect, pad) if hasattr(lines, 'lines_near') else lines


# ---------------------------------------------------------------------------
# the leaders placed so far: a new leader may neither cross one nor lie on top of one
# ---------------------------------------------------------------------------
def _segments_cross(p1, p2, p3, p4):
    """True when p1-p2 and p3-p4 cross properly (touching ends do not count)."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2, d3, d4 = side(p3, p4, p1), side(p3, p4, p2), side(p1, p2, p3), side(p1, p2, p4)
    return ((d1 > 1e-12 and d2 < -1e-12) or (d1 < -1e-12 and d2 > 1e-12)) and \
           ((d3 > 1e-12 and d4 < -1e-12) or (d3 < -1e-12 and d4 > 1e-12))


def _too_close_parallel(p1, p2, p3, p4, distance):
    """True when the two pieces run (nearly) on one line closer than `distance` and share a stretch of it:
    one leader drawn over another, or so close beside it that they read as one line."""
    du, dv = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(du, dv)
    other = math.hypot(p4[0] - p3[0], p4[1] - p3[1])
    if length < 1e-9 or other < 1e-9:
        return False
    ux, uy = du / length, dv / length
    if abs(ux * (p4[0] - p3[0]) / other + uy * (p4[1] - p3[1]) / other) < 0.999:
        return False                                        # not parallel
    if abs((p3[0] - p1[0]) * uy - (p3[1] - p1[1]) * ux) >= distance:
        return False                                        # parallel, but far enough apart
    lo, hi = sorted(((p3[0] - p1[0]) * ux + (p3[1] - p1[1]) * uy, (p4[0] - p1[0]) * ux + (p4[1] - p1[1]) * uy))
    return min(hi, length) - max(lo, 0.0) > distance        # they share a stretch


class Leaders(object):
    """Every leader piece placed so far, in a grid of cells."""

    def __init__(self, cell, apart):
        self.world = World(cell)
        self.apart = apart                                  # two parallel leaders must be at least this far apart
        self.allow_cross = False                            # last resort: a clean crossing is accepted (never one over another)

    def clear(self, points):
        """True when the leader (its points) crosses no leader placed before and lies on none."""
        for p, q in zip(points, points[1:]):
            if _length(p, q) < 1e-9:
                continue
            for (a, b, _h) in self.world.lines_near(_seg_box(p, q), self.apart):
                if _too_close_parallel(p, q, a, b, self.apart):
                    return False
                if not self.allow_cross and _segments_cross(p, q, a, b):
                    return False
        return True

    def add(self, points):
        for p, q in zip(points, points[1:]):
            if _length(p, q) > 1e-9:
                self.world.add_line((p, q, 0.0))


# ---------------------------------------------------------------------------
# the hatched area (filled regions of the view): the texts must stand inside it
# ---------------------------------------------------------------------------
class Area(object):
    """A filled region: its loops of points (the outline and its holes)."""

    def __init__(self, loops):
        self.loops = [list(loop) for loop in loops if len(loop) >= 3]
        points = [p for loop in self.loops for p in loop]
        self.bbox = (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)) if points else (0, 0, 0, 0)
        self.edges = World(max((self.bbox[2] - self.bbox[0]) / 40.0, (self.bbox[3] - self.bbox[1]) / 40.0, 1e-3))
        for loop in self.loops:
            for a, b in zip(loop, loop[1:] + loop[:1]):
                self.edges.add_line((a, b, 0.0))

    def has_point(self, p):
        """Even-odd rule over all loops: inside the outline and not in a hole."""
        if not (self.bbox[0] <= p[0] <= self.bbox[2] and self.bbox[1] <= p[1] <= self.bbox[3]):
            return False
        inside = False
        for loop in self.loops:
            for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1]):
                if (y1 > p[1]) != (y2 > p[1]):
                    x = x1 + (p[1] - y1) * (x2 - x1) / (y2 - y1)
                    if x > p[0]:
                        inside = not inside
        return inside

    def has_box(self, box):
        if not (box[0] >= self.bbox[0] and box[1] >= self.bbox[1] and box[2] <= self.bbox[2] and box[3] <= self.bbox[3]):
            return False
        if not all(self.has_point(p) for p in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3]))):
            return False
        return not any(_crosses(box, edge) for edge in self.edges.lines_near(box))


def in_areas(point, areas):
    """The areas the point lies in ([] when none or no areas)."""
    return [area for area in (areas or []) if area.has_point(point)]


def free(box, boxes, lines, limits, pad, areas=None):
    """True when the text box covers no other text, no tray or wall, stays inside the limits - and, when
    areas are given, stands wholly inside one of them (the hatch)."""
    if not _inside(box, limits):
        return False
    if areas and not any(area.has_box(box) for area in areas):
        return False
    if any(_touch(box, other, pad) for other in _boxes(boxes, box, pad)):
        return False
    return not any(_crosses(box, line) for line in _lines(lines, box))


def place(point, direction, w, h, offset, boxes, lines, limits, pad, rings=3, slides=3, own_half=0.0, areas=None):
    """A free spot for a text w x h beside the tray at `point` (running along `direction`).

    Tried in this order: right beside the tray (offset from its edge), on either side, sliding a little along
    the tray; then farther away (those need a leader). own_half: half the width of the tray itself. Returns (u, v of the text centre, needs a leader),
    or None when nothing is free."""
    du, dv = direction
    nu, nv = -dv, du                                        # square to the tray
    half_n = (abs(nu) * w + abs(nv) * h) / 2.0              # half the size of the text across the tray
    half_d = (abs(du) * w + abs(dv) * h) / 2.0              # ... and along it
    step = max(half_d, h)                                   # sliding along the tray by about half a text
    for ring in range(rings):
        distance = own_half + offset + half_n + ring * (2.0 * half_n + 2.0 * pad)
        for k in range(slides * 2 + 1):
            slide = ((k + 1) // 2) * step * (1 if k % 2 else -1)
            for side in (1.0, -1.0):
                cu = point[0] + du * slide + nu * side * distance
                cv = point[1] + dv * slide + nv * side * distance
                box = (cu - w / 2.0, cv - h / 2.0, cu + w / 2.0, cv + h / 2.0)
                if free(box, boxes, lines, limits, pad, areas):
                    return (cu, cv), ring > 0
    return None


def place_leader(point, direction, w, h, length, shoulder, boxes, lines, limits, pad, own_half=0.0, prefer=None,
                 rings=4, slides=4, areas=None, leaders=None):
    """A spot for a text w x h AWAY from the tray, with a leader - as drawn by hand:

            +---- KSS-01 TRUNKING LV         (text starts just after the leader)
            |
        ====*===================  tray       (arrow on the tray)

    A tray running across the view gets a vertical leader, the text above or below; a tray running up the
    view gets a horizontal leader, the text to the left or right. length: from the tray edge to the middle
    of the text. shoulder: the short level piece between the corner and the text.
    prefer: +1 / -1, the side to try first (so that all tags of one run stand on the same side).
    Returns (text centre, corner, side, arrow point on the tray) or None."""
    du, dv = direction
    across = abs(du) >= abs(dv)                             # the tray runs across the view: the leader goes up / down
    sides = (prefer, -prefer) if prefer in (1, -1) else (1, -1)
    step = max(w / 2.0, h) if across else max(h, w / 4.0)
    for ring in range(rings):
        reach = own_half + length + ring * (h + 2.0 * pad)       # tray edge to the middle of the text (across trays)
        for k in range(slides * 2 + 1):
            slide = ((k + 1) // 2) * step * (1 if k % 2 else -1)
            pu, pv = point[0] + du * slide, point[1] + dv * slide
            for side in sides:
                if across:
                    tv = pv + side * reach
                    elbow = (pu, tv)
                    options = ((pu + shoulder + w / 2.0, tv), (pu - shoulder - w / 2.0, tv))    # text after the leader, else before it
                    start = (pu, pv + side * own_half)
                else:
                    tu = pu + side * (own_half + length + w / 2.0 + ring * (h + 2.0 * pad))
                    elbow = ((pu + tu) / 2.0, pv)
                    options = ((tu, pv),)
                    start = (pu + side * own_half, pv)
                for centre in options:
                    box = box_at(centre, w, h)
                    if not free(box, boxes, lines, limits, pad, areas):
                        continue
                    leg = (start, elbow, 0.0)
                    # the leader must not run through another text
                    if any(_crosses(other, leg) for other in _boxes(boxes, _seg_box(start, elbow))):
                        continue
                    # ... nor cross, or lie on, a leader placed before (both of its pieces)
                    if leaders is not None and not leaders.clear(leader_points(centre, elbow, (pu, pv), w)):
                        continue
                    return centre, elbow, side, (pu, pv)
    return None


# ---------------------------------------------------------------------------
# bundles: the trays running side by side at one place, tagged together in one stack
# ---------------------------------------------------------------------------
def runs_across(direction):
    """True for a tray running across the view (its leaders go up / down), False for one running up it."""
    return abs(direction[0]) >= abs(direction[1])


def point_at(seg, across, value):
    """The point of the segment at u = value (across=True) or at v = value; None when it does not get there."""
    (a, b) = seg
    i = 0 if across else 1
    lo, hi = sorted((a[i], b[i]))
    if value < lo - 1e-9 or value > hi + 1e-9 or abs(b[i] - a[i]) < 1e-12:
        return None
    t = (value - a[i]) / (b[i] - a[i])
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def parallel(d1, d2, degrees=25.0):
    return abs(d1[0] * d2[0] + d1[1] * d2[1]) >= math.cos(math.radians(degrees))


def _bundle_once(point, across, side, text_dir, slide, members, reach, shoulder, gap, lead_gap):
    """The stack for one try. members: [dict(seg, half, w, h)] in their order across the bundle, the one
    nearest to the texts first. Returns [(centre, corner, arrow)] in the same order, or None."""
    n = len(members)
    out = []
    if across:
        # leaders go straight up (side +1) or down from each tray; texts in rows above / below the bundle,
        # all starting on one line. The farther a row, the farther its leader from that line: nothing crosses.
        base_u = point[0] + slide
        arrows = []
        for i, m in enumerate(members):
            u = base_u + text_dir * ((n - 1 - i) - (n - 1) / 2.0) * lead_gap
            p = point_at(m['seg'], True, u)
            if p is None:
                return None
            arrows.append(p)
        edge = max(p[1] + m['half'] for p, m in zip(arrows, members)) if side > 0 else min(p[1] - m['half'] for p, m in zip(arrows, members))
        start = (max(p[0] for p in arrows) if text_dir > 0 else min(p[0] for p in arrows)) + text_dir * shoulder
        level = edge + side * reach                         # the middle of the first row
        for i, (m, p) in enumerate(zip(members, arrows)):
            if i:
                level += side * ((members[i - 1]['h'] + m['h']) / 2.0 + gap)
            centre = (start + text_dir * m['w'] / 2.0, level)
            out.append((centre, (p[0], level), p))
        return out
    # trays running up the view: one row per tray, a straight level leader out to a column of texts beside the bundle
    total = sum(m['h'] for m in members) + gap * (n - 1)
    top = point[1] + slide + total / 2.0
    rows, arrows = [], []
    for m in members:
        v = top - m['h'] / 2.0
        top -= m['h'] + gap
        p = point_at(m['seg'], False, v)
        if p is None:
            return None
        rows.append(v)
        arrows.append(p)
    if side > 0:
        column = max(p[0] + m['half'] for p, m in zip(arrows, members)) + reach
    else:
        column = min(p[0] - m['half'] for p, m in zip(arrows, members)) - reach
    for m, v, p in zip(members, rows, arrows):
        centre = (column + side * m['w'] / 2.0, v)
        out.append((centre, ((p[0] + column) / 2.0, v), p))
    return out


def place_bundle(point, direction, members, reach, shoulder, boxes, lines, limits, pad, lead_gap,
                 prefer=None, rings=4, slides=4, areas=None, leaders=None):
    """One stack of tags for the trays of a bundle (see _bundle_once). Tries both sides, the text after or
    before the leaders, sliding along the trays and moving farther out. Returns
    ([(centre, corner, arrow)], side) or None when no try is free of other texts, trays and limits."""
    across = runs_across(direction)
    sides = (prefer, -prefer) if prefer in (1, -1) else (1, -1)
    step = max(max(m['w'] for m in members) / 3.0, max(m['h'] for m in members)) if across else max(m['h'] for m in members) * 2.0
    for ring in range(rings):
        extra = ring * (max(m['h'] for m in members) + 2.0 * pad)
        for k in range(slides * 2 + 1):
            slide = ((k + 1) // 2) * step * (1 if k % 2 else -1)
            for side in sides:
                indices = list(range(len(members))) if side > 0 else list(reversed(range(len(members))))
                order = [members[i] for i in indices]
                for text_dir in ((1, -1) if across else (side,)):
                    found = _bundle_once(point, across, side, text_dir, slide, order, reach + extra, shoulder, pad, lead_gap)
                    if found is None:
                        continue
                    ok = True
                    for m, (centre, corner, arrow) in zip(order, found):
                        box = box_at(centre, m['w'], m['h'])
                        if not free(box, boxes, lines, limits, pad, areas):
                            ok = False
                            break
                        # the leader (out of the tray to the corner, then on to the text) must not run through another text
                        tip = (centre[0] - text_dir * m['w'] / 2.0, centre[1]) if across else (centre[0] - side * m['w'] / 2.0, centre[1])
                        near = _boxes(boxes, _seg_box(arrow, corner, tip))
                        if any(_crosses(other, (arrow, corner, 0.0)) or _crosses(other, (corner, tip, 0.0)) for other in near):
                            ok = False
                            break
                        # ... nor cross, or lie on, a leader of another stack / tag
                        if leaders is not None and not leaders.clear([arrow, corner, tip]):
                            ok = False
                            break
                    if ok:
                        result = [None] * len(members)
                        for i, item in zip(indices, found):
                            result[i] = item
                        return result, side
    return None


def _seg_box(*points):
    return (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))


def leader_points(centre, corner, arrow, w):
    """The leader as drawn: arrow - corner - the near end of the text (for previews)."""
    if corner is None:
        return [arrow, centre]
    tip = (centre[0] - w / 2.0, centre[1]) if corner[0] <= centre[0] else (centre[0] + w / 2.0, centre[1])
    return [arrow, corner, tip]


def box_at(centre, w, h):
    return (centre[0] - w / 2.0, centre[1] - h / 2.0, centre[0] + w / 2.0, centre[1] + h / 2.0)
