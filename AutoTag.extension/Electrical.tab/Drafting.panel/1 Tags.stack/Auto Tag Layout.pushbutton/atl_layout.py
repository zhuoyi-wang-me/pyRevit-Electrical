# -*- coding: utf-8 -*-
"""Auto Tag Layout - where each tag goes. No Revit in this file.

Everything is in VIEW coordinates: u to the right of the view, v upwards, in feet.

The arrangement (as on the sample sheet):
  * tags stand in a column left of the layout and a column right of it,
    left ones ending on one line, right ones starting on one line;
  * a tag never overlaps another one: they are stacked with a gap;
  * each leader runs horizontally from its tag and then straight up or
    down to its element (or is one straight horizontal line);
  * in each column the element nearest to the column gets the tag nearest
    to the middle, the farthest element the outermost tag. With that order
    the leaders are nested inside each other and never cross;
  * the arrows keep a distance from each other: on a tray the arrow may
    slide along the tray, so parallel trays get arrows side by side;
  * a leader does not run through the symbol of a nearer element, and it
    has no tiny end piece: it is either straight or has a clear last leg.
"""
import math

LEFT, RIGHT = 'L', 'R'
BOTH, LEFT_ONLY, RIGHT_ONLY = 'both', 'left', 'right'
STRAIGHT = 1e-6                          # a leader shorter in v than this is drawn as one straight line
NEAR_COLUMN = 0.3                        # a tray is pointed at this share of its length from the end near the tags


class Item(object):
    """One tag: where its element is (u, v), how big its text is (w, h), and - after arrange() - where it goes.

    seg      ((u1, v1), (u2, v2))  the visible part of a tray: the arrow may slide along it
    body     (v low, v high)       the symbol of a device: leaders of other elements stay clear of it
    outline  [(u, v), ...]         the shape of the element in the view (convex): a straight leader
                                   ends INSIDE the shape, also when the element stands askew
    """

    def __init__(self, key, u, v, w, h, text=u'', seg=None, body=None, outline=None):
        self.key, self.u, self.v, self.w, self.h, self.text = key, float(u), float(v), float(w), float(h), text
        self.seg, self.body = seg, body
        self.outline = hull(outline) if outline else None
        if self.outline is not None and len(self.outline) < 3:
            self.outline = None
        if self.outline is not None and self.body is None:
            self.body = (min(p[1] for p in self.outline), max(p[1] for p in self.outline))
        self.side = None
        self.cu = None                   # centre of the text, u
        self.t = None                    # centre of the text, v
        self.elbow = None                # (u, v) of the corner of the leader, None for a straight leader
        self.free = False                # True: a tag beside its element or inside its room - it has no leader
        self.rect = None                 # (u low, v low, u high, v high) of the element in the view
        self.crowded = False             # True: no free place was found, the tag may overlap something

    @property
    def box(self):
        """(left, bottom, right, top) of the text."""
        return (self.cu - self.w / 2.0, self.t - self.h / 2.0, self.cu + self.w / 2.0, self.t + self.h / 2.0)

    @property
    def leader(self):
        """The points of the leader, from the tag to the element ([] for a tag without leader)."""
        if self.free:
            return []
        start = (self.cu + self.w / 2.0, self.t) if self.side == LEFT else (self.cu - self.w / 2.0, self.t)
        return [start] + ([self.elbow] if self.elbow else []) + [(self.u, self.v)]

    # ---- the tray the arrow may slide along
    @property
    def flat(self):
        """True for a tray that runs more across the view than up it: its arrow slides sideways."""
        return self.seg is not None and abs(self.seg[1][0] - self.seg[0][0]) >= abs(self.seg[1][1] - self.seg[0][1]) and abs(self.seg[1][0] - self.seg[0][0]) > 1e-9

    @property
    def steep(self):
        return self.seg is not None and not self.flat and abs(self.seg[1][1] - self.seg[0][1]) > 1e-9

    def v_at(self, u):
        (u1, v1), (u2, v2) = self.seg
        return v1 + (v2 - v1) * (u - u1) / (u2 - u1)

    def u_at(self, v):
        (u1, v1), (u2, v2) = self.seg
        return u1 + (u2 - u1) * (v - v1) / (v2 - v1)

    def landing(self):
        """(v low, v high) in which the arrow may end: the middle part of a symbol, the length of a
        tray running up the view, otherwise just the point itself."""
        if self.steep:
            return tuple(sorted((self.seg[0][1], self.seg[1][1])))
        if self.outline is not None:                        # the whole height of the shape but its very tips
            low, high = min(p[1] for p in self.outline), max(p[1] for p in self.outline)
            tip = (high - low) * 0.12
            return (low + tip, high - tip)
        if self.body is not None:
            middle, half = (self.body[0] + self.body[1]) / 2.0, (self.body[1] - self.body[0]) * 0.35
            return (min(middle - half, self.v), max(middle + half, self.v))
        return (self.v, self.v)

    def u_inside(self, v):
        """The middle of the element at the height v (for the end of a straight leader); None without an outline."""
        if self.outline is None:
            return None
        cut = chord(self.outline, v)
        return (cut[0] + cut[1]) / 2.0 if cut is not None else None

    def clearance(self):
        """(v low, v high) that leaders of other elements must stay out of."""
        return self.body if self.body is not None else (self.v, self.v)


def hull(points):
    """The convex outline of the points, counter-clockwise (Andrew's monotone chain)."""
    pts = sorted(set((float(p[0]), float(p[1])) for p in points))
    if len(pts) < 3:
        return pts

    def turn(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], p) <= 1e-12:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], p) <= 1e-12:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def chord(outline, v):
    """(u low, u high) where the horizontal line at height v runs through the outline; None when it misses it."""
    found = []
    count = len(outline)
    for index in range(count):
        (u1, v1), (u2, v2) = outline[index], outline[(index + 1) % count]
        if abs(v2 - v1) < 1e-12:
            if abs(v - v1) < 1e-9:
                found.extend([u1, u2])
            continue
        if min(v1, v2) - 1e-9 <= v <= max(v1, v2) + 1e-9:
            found.append(u1 + (u2 - u1) * (v - v1) / (v2 - v1))
    return (min(found), max(found)) if len(found) >= 2 else None


EXACT_FROM = 0.03                        # |cos2 - sin2| above this: the size can be calculated (not within ~1 degree of 45)


def size_is_estimated(angle):
    """True for a view turned (almost) exactly 45 degrees: there the text size can only be estimated."""
    c, s = abs(math.cos(angle)), abs(math.sin(angle))
    return s >= 1e-6 and c >= 1e-6 and abs(c * c - s * s) <= EXACT_FROM


def text_share(text):
    """Roughly which part of (width + height) of a text is its height: from its lines and their length."""
    lines = (text or u"").splitlines() or [u""]
    longest = max(len(line) for line in lines) or 1
    return 1.5 * len(lines) / (0.62 * longest + 1.5 * len(lines))


def rect_size(aabb_w, aabb_h, angle, text=u""):
    """Width and height of a text that is horizontal in the view, from its bounding box in MODEL axes.

    Revit gives the box of a tag along the model X and Y. In a rotated view that box is larger
    than the text. angle = rotation of the view against the model, in radians."""
    c, s = abs(math.cos(angle)), abs(math.sin(angle))
    if s < 1e-6:
        return aabb_w, aabb_h
    if c < 1e-6:
        return aabb_h, aabb_w
    det = c * c - s * s
    if abs(det) > EXACT_FROM:
        w = (aabb_w * c - aabb_h * s) / det
        h = (aabb_h * c - aabb_w * s) / det
        if w > 0 and h > 0:
            return w, h
    # at 45 degrees only width + height is known: share it by the look of the text,
    # and take the height generously so that stacked tags surely do not touch
    total = (aabb_w + aabb_h) / (c + s)
    share = text_share(text)
    return total * (1.0 - share), total * share * 1.3


def _mix(right, up):
    """How much the directions of the text (right, up) share model axes: 0 = not at all, 1 = fully."""
    return sum(abs(right[k]) * abs(up[k]) for k in range(3))


def size_needs_estimate(right, up):
    """True when the text size cannot be calculated from its model box (a plan turned 45 degrees)."""
    return 1.0 - _mix(right, up) ** 2 <= EXACT_FROM ** 2


def text_size(extents, right, up, text=u""):
    """Width and height of a tag text from the size of its bounding box along the model X, Y, Z.

    Works in every view: right / up are the directions of the view in the model. In a plan the
    text lies in X-Y; in a section its width lies in X-Y and its height along Z. A text w x h
    fills the model box by   extent[k] = w |right[k]| + h |up[k]|   for k = X, Y, Z;
    these three equations are solved for w and h (least squares)."""
    b = _mix(right, up)
    det = 1.0 - b * b
    p = sum(abs(right[k]) * extents[k] for k in range(3))
    q = sum(abs(up[k]) * extents[k] for k in range(3))
    if det > EXACT_FROM ** 2:
        w, h = (p - b * q) / det, (q - b * p) / det
        if w > 0 and h > 0:
            return w, h
    # a plan turned 45 degrees: only width + height is known. Share it by the look of the text,
    # and take the height generously so that stacked tags surely do not touch
    total = (p + q) / (1.0 + b)
    share = text_share(text)
    return total * (1.0 - share), total * share * 1.3


def clip_segment(p1, p2, box):
    """The part of the line p1-p2 inside box (umin, vmin, umax, vmax) as (a, b); None when nothing is inside."""
    u1, v1, u2, v2 = float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])
    du, dv = u2 - u1, v2 - v1
    t0, t1 = 0.0, 1.0
    for p, q in ((-du, u1 - box[0]), (du, box[2] - u1), (-dv, v1 - box[1]), (dv, box[3] - v1)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
    if t0 > t1:
        return None
    return ((u1 + t0 * du, v1 + t0 * dv), (u1 + t1 * du, v1 + t1 * dv))


def clip_midpoint(p1, p2, box):
    """Middle of the part of the line p1-p2 that lies inside box; the middle of the whole line when no part is inside."""
    part = clip_segment(p1, p2, box) or ((float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1])))
    return ((part[0][0] + part[1][0]) / 2.0, (part[0][1] + part[1][1]) / 2.0)


def spread(wishes, gap):
    """Positions on a line that are at least `gap` apart where the ranges allow it.
    wishes = [(wanted, low, high)]; returns the positions in the same order."""
    order = sorted(range(len(wishes)), key=lambda i: wishes[i][0])
    pos = [min(max(w[0], w[1]), w[2]) for w in wishes]
    for n in range(1, len(order)):                          # push to the right
        i, before = order[n], order[n - 1]
        pos[i] = min(max(pos[i], pos[before] + gap), wishes[i][2])
    for n in range(len(order) - 2, -1, -1):                 # where the right end was reached: push back to the left
        i, after = order[n], order[n + 1]
        if pos[after] - pos[i] < gap:
            pos[i] = max(min(pos[i], pos[after] - gap), wishes[i][1])
    return pos


def _slide_arrows(mine, column, leader_gap, half=None):
    """Give the arrows of one column their place along the trays, apart from each other.
    half = (u low, u high): the half of the layout this column serves; arrows stay inside it,
    so that a leader from the left never reaches among the leaders from the right."""
    wishes = []
    for item in mine:
        if item.flat:
            (u1, _), (u2, _) = item.seg
            near, far = (u1, u2) if abs(u1 - column) <= abs(u2 - column) else (u2, u1)
            inset = min(leader_gap, abs(u2 - u1) / 2.0)
            low, high = min(u1, u2) + inset, max(u1, u2) - inset
            if half is not None:
                low, high = max(low, half[0]), min(high, half[1])
                if low > high:                              # the tray only touches this half
                    low = high = min(max(item.u, half[0]), half[1])
            wishes.append((near + (far - near) * NEAR_COLUMN, low, high))
        else:
            wishes.append((item.u, item.u, item.u))         # a device, or a tray running up the view: fixed
    for item, u in zip(mine, spread(wishes, leader_gap)):
        if item.flat:
            item.u, item.v = u, item.v_at(u)


def _stack(group, column_u, start, direction, gap, min_leg):
    """Place the tags of one quarter. direction +1 = stacking upwards, -1 = downwards."""
    group.sort(key=lambda item: (abs(item.u - column_u), direction * item.v))
    previous, reach = None, None
    for item in group:
        if previous is None:
            limit = start + direction * item.h / 2.0
        else:
            limit = previous.t + direction * ((previous.h + item.h) / 2.0 + gap)
        # not nearer to the middle than the elements nearer to the column (their symbols included):
        # otherwise the horizontal line would cut through their leaders or their symbols
        wanted = [limit, item.v] + ([reach] if reach is not None else [])
        item.t = max(wanted) if direction > 0 else min(wanted)
        low, high = item.landing()
        if item.t > high + 1e-9 and item.t - high < min_leg:      # no tiny end piece: a clear last leg instead
            item.t = high + min_leg
        elif item.t < low - 1e-9 and low - item.t < min_leg:
            item.t = low - min_leg
        low, high = item.clearance()
        edge = high if direction > 0 else low
        reach = edge if reach is None else (max(reach, edge) if direction > 0 else min(reach, edge))
        previous = item


def _finish(item, min_leg):
    """Where the arrow ends, now that the height of the tag is known."""
    low, high = item.landing()
    item.v = min(max(item.t, low), high)                    # straight when the tag is level with the symbol or the tray
    if item.steep:
        item.u = item.u_at(item.v)
    else:
        inside = item.u_inside(item.v)                      # an element that stands askew: end in its middle at this height
        if inside is not None:
            item.u = inside
    item.elbow = None if abs(item.t - item.v) < STRAIGHT else (item.u, item.t)


def _level_stack(mine, gap, limits=None):
    """Tags level with their own elements, as close to them as a stack without overlaps allows.

    The tags keep the order of their elements from top to bottom. Each tag wants the height of
    its element; where several want (nearly) the same height they are stacked tightly around the
    middle of what they want. (Least-squares placement with a fixed order: pooling of neighbours.)"""
    order = sorted(mine, key=lambda item: (-item.v, item.u))
    offsets, total = [], 0.0
    for index, item in enumerate(order):
        if index:
            total += (order[index - 1].h + item.h) / 2.0 + gap
        offsets.append(total)
    # x = t + offset must not increase from one tag to the next; pool neighbours that would
    blocks = []                                             # [sum of wishes, count]
    for item, offset in zip(order, offsets):
        blocks.append([item.v + offset, 1])
        while len(blocks) > 1 and blocks[-1][0] / blocks[-1][1] > blocks[-2][0] / blocks[-2][1]:
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
    index = 0
    for total_wish, count in blocks:
        for _ in range(count):
            order[index].t = total_wish / count - offsets[index]
            index += 1
    if limits is not None and order:                        # inside the annotation crop: slide the stack, keep it tight
        top = order[0].t + order[0].h / 2.0
        if top > limits[3]:
            for item in order:
                item.t -= top - limits[3]
        bottom = order[-1].t - order[-1].h / 2.0
        if bottom < limits[1] and (order[0].t + order[0].h / 2.0) + (limits[1] - bottom) <= limits[3] + 1e-9:
            for item in order:
                item.t += limits[1] - bottom
    return order


def _squeeze(group, direction, gap, limit):
    """Pull a stack back so that its outermost text ends at `limit` (the annotation crop)."""
    cap = None
    for index in range(len(group) - 1, -1, -1):             # from the outermost tag inwards
        item = group[index]
        if cap is None:
            cap = limit - direction * item.h / 2.0
        else:
            outer = group[index + 1]
            cap = outer.t - direction * ((outer.h + item.h) / 2.0 + gap)
        item.t = min(item.t, cap) if direction > 0 else max(item.t, cap)


def outside(items, limits, tolerance=1e-6):
    """The items whose text is not wholly inside limits (u low, v low, u high, v high)."""
    return [i for i in items if i.box[0] < limits[0] - tolerance or i.box[1] < limits[1] - tolerance
            or i.box[2] > limits[2] + tolerance or i.box[3] > limits[3] + tolerance]


AUTO, STACKED = 'auto', 'stacked'        # AUTO: level with the elements where possible; STACKED: always the stack with corners
BELOW, ABOVE = 'below', 'above'          # the tags in a block under / over the layout, the leaders coming straight down / up


def _arrange_outside(items, vmin, vmax, gap, offset, side, where, leader_gap, limits):
    """The tags in one block below (or above) the layout, beside the elements:

        elements      |   |    |
                      |   |    +------ TAG
                      |   +----------- TAG
                      +--------------- TAG

    Every leader leaves its element straight down (up) and turns to its tag with one 90 degree corner.
    The element nearest to the tags gets the tag nearest to the layout, the farthest the outermost:
    the leaders are nested and never cross. side: the tags stand to the RIGHT or LEFT of the elements."""
    direction = -1.0 if where == BELOW else 1.0
    for item in items:
        item.side = side
    # the arrows apart from each other: on a tray the arrow slides along the tray, on a device across its symbol
    wishes = []
    for item in items:
        if item.flat:
            low, high = sorted((item.seg[0][0], item.seg[1][0]))
            inset = min(leader_gap, (high - low) / 2.0)
            wishes.append((item.u, low + inset, high - inset))
        else:
            cut = chord(item.outline, item.v) if item.outline is not None else None
            if cut is not None and cut[1] - cut[0] > 1e-9:
                inset = (cut[1] - cut[0]) * 0.15
                wishes.append((min(max(item.u, cut[0] + inset), cut[1] - inset), cut[0] + inset, cut[1] - inset))
            else:
                wishes.append((item.u, item.u, item.u))
    for item, u in zip(items, spread(wishes, leader_gap)):
        if item.flat:
            item.v = item.v_at(u)
        item.u = u
    widest = max(item.w for item in items)
    if side == RIGHT:
        column = max(item.u for item in items) + offset
        if limits is not None:                              # inside the annotation crop, but never back over the legs
            column = max(min(column, limits[2] - widest), max(item.u for item in items) + min(offset, leader_gap or offset))
    else:
        column = min(item.u for item in items) - offset
        if limits is not None:
            column = min(max(column, limits[0] + widest), min(item.u for item in items) - min(offset, leader_gap or offset))
    edge = (vmin - offset) if where == BELOW else (vmax + offset)
    for item in sorted(items, key=lambda i: (abs(i.u - column), -direction * i.v)):
        item.t = edge + direction * item.h / 2.0
        edge = item.t + direction * (item.h / 2.0 + gap)
        item.cu = column + item.w / 2.0 if side == RIGHT else column - item.w / 2.0
        item.elbow = (item.u, item.t)
    return items


def arrange(items, umin, umax, gap, offset, sides=BOTH, leader_gap=0.0, min_leg=0.0, limits=None, style=AUTO, vspan=None):
    """Give every item its side, text position and leader. umin / umax: the extent of the layout.
    limits = (u low, v low, u high, v high): the texts are kept inside it (the annotation crop)
    as far as that is possible without overlapping texts or crossing leaders."""
    if not items:
        return items
    if style in (BELOW, ABOVE):
        # vspan = (v low, v high) of the layout; without it, of the elements themselves
        low, high = vspan if vspan is not None else (min(i.v for i in items), max(i.v for i in items))
        return _arrange_outside(items, low, high, gap, offset, LEFT if sides == LEFT_ONLY else RIGHT, style, leader_gap, limits)
    centre = (umin + umax) / 2.0
    columns = {LEFT: umin - offset, RIGHT: umax + offset}
    count = {LEFT: 0, RIGHT: 0}
    for item in sorted(items, key=lambda i: abs(i.u - centre), reverse=True):
        if sides == LEFT_ONLY:
            item.side = LEFT
        elif sides == RIGHT_ONLY:
            item.side = RIGHT
        elif item.flat and min(item.seg[0][0], item.seg[1][0]) < centre < max(item.seg[0][0], item.seg[1][0]):
            item.side = LEFT if count[LEFT] <= count[RIGHT] else RIGHT      # a tray across the middle: the emptier side
        else:
            item.side = LEFT if item.u < centre else RIGHT
        count[item.side] += 1
    quarters = []
    for side in (LEFT, RIGHT):
        mine = [item for item in items if item.side == side]
        if not mine:
            continue
        column = columns[side]
        if limits is not None:                              # the widest text must still be inside the limit
            widest = max(item.w for item in mine)
            column = max(column, limits[0] + widest) if side == LEFT else min(column, limits[2] - widest)
            columns[side] = column
        half = None
        if sides == BOTH:
            half = (umin - abs(offset) - 1e9, centre) if side == LEFT else (centre, umax + abs(offset) + 1e9)
        _slide_arrows(mine, column, leader_gap, half)
        middle = (min(i.v for i in mine) + max(i.v for i in mine)) / 2.0
        upper = [i for i in mine if i.v >= middle]
        lower = [i for i in mine if i.v < middle]
        _stack(upper, column, middle + gap / 2.0, +1, gap, min_leg)
        _stack(lower, column, middle - gap / 2.0, -1, gap, min_leg)
        quarters.append((upper, +1))
        quarters.append((lower, -1))
        for item in mine:                                   # left tags end on the column line, right tags start on it
            item.cu = column - item.w / 2.0 if side == LEFT else column + item.w / 2.0
    plain = [(item.u, item.v) for item in items]

    def finish():
        # the ends of the leaders; if that makes two leaders cross, the plain ends are kept
        for item, (u, v) in zip(items, plain):
            item.u, item.v = u, v
            _finish(item, min_leg)
        if crossings(items):
            for item, (u, v) in zip(items, plain):
                item.u, item.v = u, v
                item.elbow = None if abs(item.t - item.v) < STRAIGHT else (item.u, item.t)

    if limits is not None:
        # pull stacks that are too high or too low back inside the limit - but only if that
        # does not make texts overlap or leaders cross; otherwise the stacks stay as they are
        free = [item.t for item in items]
        for group, direction in quarters:
            if group:
                _squeeze(group, direction, gap, limits[3] if direction > 0 else limits[1])
        finish()
        if overlaps(items) or crossings(items):
            for item, t in zip(items, free):
                item.t = t
            finish()
    else:
        finish()

    # What stands above is the "nested" arrangement: it never lets two leaders cross, but it lifts
    # tags away from their elements. Where it is possible, the simpler picture is preferred: every
    # tag level with its own element in a tight stack, the leader one straight line into the
    # element. A side keeps it only if no texts overlap and no leaders cross.
    for side in (LEFT, RIGHT):
        mine = [item for item in items if item.side == side]
        if not mine or style == STACKED:
            continue
        nested = [(item.t, item.u, item.v, item.elbow) for item in mine]
        for item in mine:
            item.u, item.v = plain[items.index(item)]
        _level_stack(mine, gap, limits)
        for item in mine:
            _finish(item, min_leg)
            leg = abs(item.t - item.v)
            if STRAIGHT < leg < min_leg:                    # no tiny end piece: let the arrow end deeper in the element
                low, high = item.landing()
                wanted = item.t - min_leg if item.t > item.v else item.t + min_leg
                item.v = min(max(wanted, low), high)
                inside = item.u_inside(item.v)
                if item.steep:
                    item.u = item.u_at(item.v)
                elif inside is not None:
                    item.u = inside
                item.elbow = (item.u, item.t)
        tiny = [item for item in mine if STRAIGHT < abs(item.t - item.v) < min_leg - 1e-9]
        if overlaps(mine) or crossings(items) or tiny:
            for item, (t, u, v, elbow) in zip(mine, nested):
                item.t, item.u, item.v, item.elbow = t, u, v, elbow
    return separate_legs(items, max(leader_gap, 1e-3))


# ---------------------------------------------------------------------------
# two columns on a side: when one column is taller than the annotation crop
# ---------------------------------------------------------------------------
def leaders_through_texts(items, pad=0.0):
    """Pairs (tag, other tag) where the leader of the first runs through the text of the second."""
    found = []
    for a in items:
        la = a.leader
        for b in items:
            if b is a:
                continue
            box = b.box
            if any(_segment_hits_box(la[m], la[m + 1], box, pad) for m in range(len(la) - 1)):
                found.append((a.key, b.key))
    return found


def arrange_double(items, umin, umax, gap, offset, sides=BOTH, leader_gap=0.0, min_leg=0.0, limits=None,
                   style=AUTO, vspan=None, between=None):
    """arrange(), and then, for a side whose column is taller than the annotation crop, a SECOND column
    further out - if the band between the crop region and the annotation crop is wide enough for both.

        layout          |  inner  outer
           ----+--------|--TAG
               |  +-----|-------- TAG        the outer leaders run through the gaps of the inner column
           ----+--+-----|--TAG
                  +-----|-------- TAG

    How: the side is stacked once more as ONE column with every tag half as high as the tallest one
    (pitch (H + gap) / 2), by the same nested stacking that keeps leaders from crossing. Its rows then go
    alternately to the inner and the outer column. Two rows of one column are then at least H + gap apart,
    so texts do not overlap, and an outer leader passes an inner text with at least gap / 2 to spare.
    Inside the layout the leaders are those of the half-height single column, which do not cross.

    The second column is kept only when it leaves fewer tags outside the annotation crop and makes no
    texts overlap, no leaders cross and no leader run through a text; otherwise the side is as before.
    Returns the number of tags put in an outer column."""
    plain = dict((id(item), (item.u, item.v)) for item in items)        # where the elements are, before sliding
    arrange(items, umin, umax, gap, offset, sides, leader_gap, min_leg, limits, style, vspan)
    if limits is None or style in (BELOW, ABOVE) or not items:
        return 0
    between = gap * 2.0 if between is None else between     # space between the inner texts and the outer column
    pad = gap * 0.25
    moved = 0

    def problems():
        return (len(overlaps(items)), len(crossings(items)), len(leaders_through_texts(items, pad)))

    for side in (LEFT, RIGHT):
        mine = [item for item in items if item.side == side]
        before_outside = len(outside(mine, limits))
        if len(mine) < 2 or not before_outside:
            continue
        tallest = max(item.h for item in mine)
        half = (tallest - gap) / 2.0                        # the height that gives a pitch of (H + gap) / 2
        if half <= 1e-9:
            continue
        # quick no: not even the two narrowest texts fit side by side in the band
        band = (limits[2] - umax) if side == RIGHT else (umin - limits[0])
        narrow = sorted(item.w for item in mine)[:2]
        if sum(narrow) + between > band + 1e-9:
            continue
        snapshot = [(item, item.side, item.cu, item.t, item.u, item.v, item.elbow, item.h) for item in items]
        base = problems()                                  # what the single column has already (not made worse)

        def restore():
            for item, s, cu, t, u, v, elbow, h in snapshot:
                item.side, item.cu, item.t, item.u, item.v, item.elbow, item.h = s, cu, t, u, v, elbow, h

        real = [item.h for item in mine]
        for item in mine:
            item.u, item.v = plain.get(id(item), (item.u, item.v))
            item.h = half
            item.cu = item.t = item.elbow = None
        # the stack is made with half-height tags: keep it that much further from the top and bottom
        # of the annotation crop, so the real (taller) texts of the end rows still fit
        trim = (tallest - half) / 2.0
        inner_limits = (limits[0], limits[1] + trim, limits[2], limits[3] - trim)
        if inner_limits[3] <= inner_limits[1]:
            inner_limits = limits
        arrange(mine, umin, umax, gap, offset, LEFT_ONLY if side == LEFT else RIGHT_ONLY,
                leader_gap, min_leg, inner_limits, style, vspan)
        for item, h in zip(mine, real):
            item.h = h
        # rows from the top: even rows stay in the inner column, odd rows go to the outer one
        rows = sorted(mine, key=lambda item: -item.t)
        inner, outer = rows[0::2], rows[1::2]
        inner_w = max(item.w for item in inner)
        outer_w = max(item.w for item in outer)
        direction = 1.0 if side == RIGHT else -1.0
        column = (umax + offset) if side == RIGHT else (umin - offset)
        # both columns inside the annotation crop: pull them in, but never over the layout
        far = column + direction * (inner_w + between + outer_w)
        if side == RIGHT and far > limits[2]:
            column -= far - limits[2]
        elif side == LEFT and far < limits[0]:
            column += limits[0] - far
        if (side == RIGHT and column < umax) or (side == LEFT and column > umin):
            restore()
            continue
        second = column + direction * (inner_w + between)
        for item in inner:
            item.cu = column + direction * item.w / 2.0
        for item in outer:
            item.cu = second + direction * item.w / 2.0
        after_outside = len(outside(mine, limits))
        worse = any(now > was for now, was in zip(problems(), base))
        if after_outside >= before_outside or worse:
            restore()
            continue
        for item in outer:
            item.outer = True
        moved += len(outer)
    return moved


# ---------------------------------------------------------------------------
# tags without leader: beside their element, or inside their room
# ---------------------------------------------------------------------------
def _touch(a, b, pad=0.0):
    """True when the boxes a and b (left, bottom, right, top) overlap, or come nearer than pad."""
    return a[0] < b[2] + pad and b[0] < a[2] + pad and a[1] < b[3] + pad and b[1] < a[3] + pad


def _free(box, blocked, pad=0.0):
    return not any(_touch(box, other, pad) for other in blocked)


def _within(box, bounds):
    return bounds is None or (box[0] >= bounds[0] and box[1] >= bounds[1] and box[2] <= bounds[2] and box[3] <= bounds[3])


def place_near(items, blocked, gap, bounds=None, rings=4):
    """Put each tag right beside its own element, where nothing else is.

    Tried in this order, first directly at the element and then farther out: right of it, above, below,
    left, then the four corners. A place is taken when the text touches neither another element, nor
    an existing tag, nor a tag placed before (`blocked`: their boxes; the new tags are added to it).
    bounds: the texts stay inside it (the crop region). Returns how many found no free place."""
    crowded = 0
    for item in sorted(items, key=lambda i: (-i.v, i.u)):
        left, bottom, right, top = item.rect if item.rect is not None else (item.u, item.v, item.u, item.v)
        cu, cv = (left + right) / 2.0, (bottom + top) / 2.0
        ex, ey = (right - left) / 2.0, (top - bottom) / 2.0
        chosen, first = None, None
        for ring in range(rings):
            away_u = ex + gap + item.w / 2.0 + ring * (item.w + gap)
            away_v = ey + gap + item.h / 2.0 + ring * (item.h + gap)
            for du, dv in ((1, 0), (0, 1), (0, -1), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                centre = (cu + du * away_u, cv + dv * away_v)
                box = (centre[0] - item.w / 2.0, centre[1] - item.h / 2.0, centre[0] + item.w / 2.0, centre[1] + item.h / 2.0)
                if first is None:
                    first = centre
                if _within(box, bounds) and _free(box, blocked, gap * 0.5):
                    chosen = centre
                    break
            if chosen is not None:
                break
        if chosen is None:                                  # nowhere free: beside the element all the same, and reported
            chosen = first
            item.crowded = True
            crowded += 1
        item.cu, item.t = chosen
        item.free, item.elbow, item.side = True, None, None
        blocked.append(item.box)
    return crowded


def place_in_room(item, inside, blocked, gap, bounds, most=1600):
    """Put a room tag inside its room, as near to the room's own point as possible, where it covers
    nothing: no element, no existing tag, no tag placed before.

    inside(u, v): True when the point is in the room. bounds: (u low, v low, u high, v high) around the
    room. The whole text has to be in the room (its four corners and its middle). The new box is added
    to `blocked`. Returns True when a free place was found; otherwise the tag stays at the room's point."""
    def fits(cu, cv):
        box = (cu - item.w / 2.0, cv - item.h / 2.0, cu + item.w / 2.0, cv + item.h / 2.0)
        if not _free(box, blocked, gap * 0.5):
            return False
        corners = ((cu, cv), (box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
        return all(inside(u, v) for u, v in corners)

    item.free, item.elbow, item.side = True, None, None
    item.cu, item.t = item.u, item.v
    found = fits(item.u, item.v)
    if not found and bounds is not None:
        span_u, span_v = max(bounds[2] - bounds[0], 1e-6), max(bounds[3] - bounds[1], 1e-6)
        step = max(item.h + gap, math.sqrt(span_u * span_v / float(most)))      # a grid of at most `most` points
        points = []
        steps_u, steps_v = int(span_u / step) + 1, int(span_v / step) + 1
        for a in range(-steps_u, steps_u + 1):
            for b in range(-steps_v, steps_v + 1):
                cu, cv = item.u + a * step, item.v + b * step
                if bounds[0] <= cu <= bounds[2] and bounds[1] <= cv <= bounds[3]:
                    points.append((a * a + b * b, cu, cv))
        for _distance, cu, cv in sorted(points):            # nearest to the room's own point first
            if fits(cu, cv):
                item.cu, item.t = cu, cv
                found = True
                break
    item.crowded = not found
    blocked.append(item.box)
    return found


# ---------------------------------------------------------------------------
# big views: each tag in the free space next to its own element, short leader
# ---------------------------------------------------------------------------
class _BoxIndex(object):
    """Boxes (left, bottom, right, top) in a grid of cells, to find the ones near a place quickly."""

    def __init__(self, cell):
        self.cell = max(cell, 1e-6)
        self.cells = {}
        self.boxes = []

    def _keys(self, box):
        c = self.cell
        for i in range(int(math.floor(box[0] / c)), int(math.floor(box[2] / c)) + 1):
            for j in range(int(math.floor(box[1] / c)), int(math.floor(box[3] / c)) + 1):
                yield (i, j)

    def add(self, box, payload=None):
        n = len(self.boxes)
        self.boxes.append((box, payload))
        for key in self._keys(box):
            self.cells.setdefault(key, []).append(n)

    def add_segment(self, p, q, payload=None):
        """A line: only the cells along it (a long diagonal grid line would fill thousands of cells)."""
        n = len(self.boxes)
        box = (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))
        self.boxes.append((box, payload))
        length = math.hypot(q[0] - p[0], q[1] - p[1])
        steps = max(1, int(length / (self.cell * 0.5)) + 1)
        keys = set()
        for k in range(steps + 1):
            u = p[0] + (q[0] - p[0]) * k / float(steps)
            v = p[1] + (q[1] - p[1]) * k / float(steps)
            i, j = int(math.floor(u / self.cell)), int(math.floor(v / self.cell))
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    keys.add((i + di, j + dj))
        for key in keys:
            self.cells.setdefault(key, []).append(n)

    def near(self, box):
        seen = set()
        for key in self._keys(box):
            for n in self.cells.get(key, ()):
                if n not in seen:
                    seen.add(n)
                    yield self.boxes[n]


def _segment_hits_box(p, q, box, pad=0.0):
    """True when the segment p-q passes through the box (grown by pad)."""
    left, bottom, right, top = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    t0, t1 = 0.0, 1.0
    du, dv = q[0] - p[0], q[1] - p[1]
    for d, lo, hi, start in ((du, left, right, p[0]), (dv, bottom, top, p[1])):
        if abs(d) < 1e-12:
            if start < lo or start > hi:
                return False
            continue
        a, b = (lo - start) / d, (hi - start) / d
        if a > b:
            a, b = b, a
        t0, t1 = max(t0, a), min(t1, b)
        if t0 > t1:
            return False
    return t1 - t0 > 1e-9


# what each kind of leader costs per unit of length: level leaders as drafted by hand first, a leader with
# one 90 degree corner (up / down from a level tray, then level into the tag) next, a slanted one last
LEVEL_COST, CORNER_COST, SLANT_COST = 1.0, 1.15, 2.2
_SLANTS = [(1, 1), (-1, 1), (1, -1), (-1, -1)]


def hits_obstacle(box, obstacles, pad=0.0):
    """True when a text box touches a grid line or a grid bubble.
    obstacles = {'lines': [((u, v), (u, v))], 'bubbles': [box]} (either may be missing)."""
    if not obstacles:
        return False
    for b in obstacles.get('bubbles') or []:
        if _touch(box, b, pad):
            return True
    for p, q in obstacles.get('lines') or []:
        if _segment_hits_box(p, q, box, pad):
            return True
    return False


def leader_through_bubble(points, obstacles):
    if not obstacles:
        return False
    for b in obstacles.get('bubbles') or []:
        if any(_segment_hits_box(p, q, b) for p, q in zip(points, points[1:])):
            return True
    return False


def clear_of_obstacles(items, obstacles, others, gap):
    """Margin tags that stand on a grid line or bubble slide along their stack (a column: up / down,
    a row block: sideways) to the nearest place that is clear, covers no other tag and makes no
    leader cross. Returns how many could not be cleared."""
    if not obstacles:
        return 0
    stuck = 0
    everyone = list(others) + list(items)
    for item in items:
        if not hits_obstacle(item.box, obstacles, gap * 0.25) and not leader_through_bubble(item.leader, obstacles):
            continue
        before = (item.cu, item.t, item.elbow)
        base_cross = len(crossings(everyone))
        step = (item.h + gap) / 2.0
        vertical = getattr(item, 'margin', None) in ('L', 'R')
        done = False
        for k in range(1, 41):
            for sign in (1, -1):
                item.cu, item.t, item.elbow = before
                if vertical:
                    item.t = before[1] + sign * k * step
                    item.elbow = None if abs(item.t - item.v) < STRAIGHT else (item.u, item.t)
                else:
                    item.cu = before[0] + sign * k * step
                if hits_obstacle(item.box, obstacles, gap * 0.25) or leader_through_bubble(item.leader, obstacles):
                    continue
                if any(_touch(item.box, o.box, gap * 0.25) for o in everyone if o is not item and o.cu is not None):
                    continue
                if len(crossings(everyone)) > base_cross:
                    continue
                done = True
                break
            if done:
                break
        if not done:
            item.cu, item.t, item.elbow = before
            stuck += 1
    return stuck


def _polyline_length(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def _candidates(item, au, av, ring, gap, step, col, row, slants=True):
    """(cost per length, side, text centre u, text centre v, elbow or None) for one anchor and one ring.
    col / row: the grids text edges / text rows snap to, so neighbouring tags line up in stacks."""
    out = []
    own = item.rect
    ex = (own[2] - own[0]) / 2.0 if (own is not None and item.seg is None) else 0.0
    ey = (own[3] - own[1]) / 2.0 if (own is not None and item.seg is None) else 0.0

    def snap(x, grid):
        return round(x / grid) * grid if grid > 0 else x

    level_tray = item.seg is not None and item.flat
    upright_tray = item.seg is not None and item.steep
    # 1. one level line (not along a level tray - that would lie on the tray)
    if not level_tray:
        for du in (1, -1):
            edge = au + du * (ex + gap * 1.5 + ring * step)
            edge = snap(edge, col) if du > 0 else -snap(-edge, col)
            if (edge - au) * du <= 0:
                edge = au + du * col
            side = RIGHT if du > 0 else LEFT
            cu = edge + du * item.w / 2.0
            out.append((LEVEL_COST, side, cu, av, None))
    # 2. up / down from the element, one 90 degree corner, then level into the tag
    if not upright_tray:
        for dv in (1, -1):
            t = av + dv * (ey + item.h / 2.0 + gap + ring * step)
            t = snap(t, row)
            if (t - av) * dv <= item.h / 2.0:
                t += dv * row
            for du in (1, -1):
                edge = au + du * gap * 2.0
                edge = snap(edge, col) if du > 0 else -snap(-edge, col)
                if (edge - au) * du < gap:
                    edge += du * col
                side = RIGHT if du > 0 else LEFT
                out.append((CORNER_COST, side, edge + du * item.w / 2.0, t, (au, t)))
    # 3. a slanted straight line, the last choice (only when no level / cornered leader fits within reach)
    for du, dv in (_SLANTS if slants else []):
        d = ex + gap * 1.5 + ring * step
        pu, pv = au + du * d, av + dv * (ey + item.h + gap + ring * step)
        side = RIGHT if du > 0 else LEFT
        out.append((SLANT_COST, side, pu + du * item.w / 2.0, snap(pv, row), None))
    return out


def align_stacks(items, blocked_boxes, gap, reach_u, reach_v, obstacles=None):
    """Line up neighbouring tags: tags on the same side whose text edges are near each other and that stand
    one above the other get one common edge (the one nearest to their elements), where nothing gets covered
    and no leader starts to cross another one. Returns how many tags were moved."""
    moved = 0
    placed = [i for i in items if i.cu is not None and not i.free]
    for side in (RIGHT, LEFT):
        mine = sorted([i for i in placed if i.side == side], key=lambda i: -i.t)
        used = set()
        for first in mine:
            if id(first) in used:
                continue
            edge_of = (lambda i: i.box[0]) if side == RIGHT else (lambda i: i.box[2])
            group = [first]
            for other in mine:
                if other is first or id(other) in used:
                    continue
                if abs(edge_of(other) - edge_of(first)) <= reach_u and \
                        min(abs(other.t - g.t) for g in group) <= reach_v:
                    group.append(other)
            if len(group) < 2:
                continue
            target = min(edge_of(i) for i in group) if side == RIGHT else max(edge_of(i) for i in group)
            # the common edge must stay clear of every element of the group (never back over a tray)
            for item in group:
                old = (item.cu, item.elbow)
                shift = target - edge_of(item)
                if abs(shift) < 1e-9:
                    used.add(id(item))
                    continue
                item.cu += shift
                others = [o.box for o in placed if o is not item]
                ok = not any(_touch(item.box, b, gap * 0.5) for b in list(blocked_boxes) + others) \
                    and not hits_obstacle(item.box, obstacles, gap * 0.5) \
                    and not leader_through_bubble(item.leader, obstacles)
                if ok and item.elbow is None and abs(item.t - item.v) > 1e-9:
                    ok = False                              # a slanted leader: keep it as it was
                if ok:
                    start = item.leader[0]
                    if (side == RIGHT and start[0] <= item.u) or (side == LEFT and start[0] >= item.u):
                        ok = False                          # the text would come back over its own element
                if ok:                                      # its leader must not run through another tag
                    lead = item.leader
                    for o in placed:
                        if o is not item and any(_segment_hits_box(p, q, o.box) for p, q in zip(lead, lead[1:])):
                            ok = False
                            break
                if ok and crossings(placed):
                    ok = False
                if ok:
                    moved += 1
                    used.add(id(item))
                else:
                    item.cu, item.elbow = old
    return moved


def place_with_leader(items, blocked, gap, bounds=None, reach=None, leader_gap=0.0, snap=None, obstacles=None):
    """Put each tag in the nearest free space around its OWN element, with a short straight leader.

    For a big layout this is what is drawn by hand: the tag sits just outside the trays it belongs to
    (or in the free space between them) instead of in a column at the edge of the whole view.
      - the text touches no element, no tag already in the view and no tag placed before (`blocked`);
      - its leader crosses no other leader and runs through no tag;
      - level leaders are preferred, then 45 degrees, then straight up / down; a leader across other
        trays costs extra;
      - the tag edge is snapped to `snap` so that neighbouring tags line up in tidy stacks;
      - on a tray the arrow may sit anywhere along the visible part (not on its very ends).
    reach: the longest leader allowed (None = no limit). Returns the items that found no place
    (they are left unplaced, for the side columns)."""
    if not items:
        return []
    size = max(max(i.h for i in items), gap)
    index = _BoxIndex(size * 4.0)
    for box in blocked:
        index.add(box, 'blocked')
    # grids: no text on a grid line or a bubble, no leader through a bubble (crossing a grid line is fine)
    for b in (obstacles or {}).get('bubbles') or []:
        index.add(b, 'bubble')
    for p, q in (obstacles or {}).get('lines') or []:
        index.add_segment(p, q, ('line', p, q))

    def covers(box, pad):
        for other, kind in index.near(box):
            if isinstance(kind, tuple):
                if _segment_hits_box(kind[1], kind[2], box, pad):
                    return True
            elif _touch(box, other, pad):
                return True
        return False
    leaders = []                                            # [(p, q)] of the leaders placed
    leader_index = _BoxIndex(size * 4.0)
    snap = snap if snap is not None else gap
    failed = []

    def snapped(x):
        return round(x / snap) * snap if snap > 0 else x

    def crosses_leader(p, q):
        box = (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))
        for (a, b), _ in leader_index.near(box):
            if _cross(p, q, a, b) or _on_top(p, q, a, b):
                return True
        return False

    def runs_through(p, q, own_rect):
        """(hits a tag: bool, number of other elements crossed)"""
        box = (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))
        elements = 0
        for other, kind in index.near(box):
            if isinstance(kind, tuple):
                continue                                    # a grid line: a leader may cross it
            if not _segment_hits_box(p, q, other, -gap * 0.25):
                continue
            if kind in ('tag', 'bubble'):
                return True, 0
            if own_rect is None or other != own_rect:
                elements += 1
        return False, elements

    col = max(snap * 2.0, 1e-6)                             # the grid text edges snap to (columns line up)

    def add_leader(points):
        for p, q in zip(points, points[1:]):
            leader_index.add((min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])), None)
            leader_index.boxes[-1] = ((p, q), None)

    # the hardest first: big texts are placed while there is still room
    order = sorted(items, key=lambda i: (-(i.h * i.w), -i.v, i.u))
    for item in order:
        if item.seg is not None:
            (u1, v1), (u2, v2) = item.seg
            anchors = [(u1 + (u2 - u1) * f, v1 + (v2 - v1) * f) for f in (0.5, 0.3, 0.7, 0.15, 0.85)]
        else:
            anchors = [(item.u, item.v)]
        own = item.rect
        best = None
        step = item.h + gap
        row = step                                          # the grid text rows snap to (rows line up)
        rings = int(math.ceil((reach if reach else 40.0 * step) / step)) + 1
        # first only level / cornered leaders over the whole reach; slanted ones only if none fits
        for ring, slants in [(r, False) for r in range(rings)] + [(r, True) for r in range(rings)]:
            if slants and best is not None:
                break
            for au, av in anchors:
                for weight, side, cu, t, elbow in _candidates(item, au, av, ring, gap, step, col, row, slants):
                    if slants and elbow is not None or (slants and weight != SLANT_COST):
                        continue                            # the level / cornered ones were tried already
                    box = (cu - item.w / 2.0, t - item.h / 2.0, cu + item.w / 2.0, t + item.h / 2.0)
                    if not _within(box, bounds):
                        continue
                    start = (box[0], t) if side == RIGHT else (box[2], t)
                    points = [start] + ([elbow] if elbow is not None else []) + [(au, av)]
                    length = _polyline_length(points)
                    if reach is not None and length > reach:
                        continue
                    cost = length * weight
                    if best is not None and cost >= best[0]:
                        continue
                    if covers(box, gap * 0.5):
                        continue
                    bad, across = False, 0
                    for p, q in zip(points, points[1:]):
                        if crosses_leader(p, q):
                            bad = True
                            break
                        hits_tag, n = runs_through(p, q, own)
                        if hits_tag:
                            bad = True
                            break
                        across += n
                    if bad:
                        continue
                    cost += across * step * 3.0             # a leader across other trays: allowed, but worse
                    if leader_gap > 0 and any(math.hypot(au - q[0], av - q[1]) < leader_gap
                                              for (p, q), _ in leader_index.near((au - leader_gap, av - leader_gap,
                                                                                  au + leader_gap, av + leader_gap))):
                        cost += step                        # arrows too close together: allowed, but worse
                    if best is None or cost < best[0]:
                        best = (cost, cu, t, side, au, av, elbow, points)
            if best is not None and ring >= 1 and best[0] <= (ring + 1) * step:
                break                                       # nothing farther out can be shorter
            if not slants and best is not None and ring == rings - 1:
                break
        if best is None:
            failed.append(item)
            continue
        _cost, item.cu, item.t, item.side, item.u, item.v, item.elbow, points = best
        item.free = False
        index.add(item.box, 'tag')
        add_leader(points)
    placed = [i for i in items if i not in failed]
    align_stacks(placed, list(blocked), gap, col * 4.0, max(i.h for i in items) * 3.0, obstacles)
    return failed


def place_in_margins(items, extent, limits, gap, offset, leader_gap=0.0, min_leg=0.0, others=None, obstacles=None,
                     allowed=('L', 'R', 'A', 'B')):
    """Like _place_in_margins, but a tag whose leader would cross a leader of `others` (the tags placed
    next to their elements) or of another margin tag, or that stands on a grid line / bubble and cannot
    slide clear of it, is tried in its next-nearest margin; the assignment with the fewest problems is kept."""
    others = list(others or [])
    if not items or extent is None or limits is None:
        return _place_in_margins(items, extent, limits, gap, offset, leader_gap, min_leg)
    # sides not allowed count as banned for every tag
    banned = dict((id(i), set(s for s in ('L', 'R', 'A', 'B') if s not in allowed)) for i in items)
    # each margin stack starts beyond the grid ends and bubbles on its side (bubbles first, then the tags)
    offsets = {'L': offset, 'R': offset, 'A': offset, 'B': offset}
    if obstacles:
        boxes = list(obstacles.get('bubbles') or [])
        for p, q in obstacles.get('lines') or []:
            boxes.append((min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])))
        for b in boxes:
            if b[0] < extent[0] and b[2] - b[0] < (extent[2] - extent[0]) * 0.5:
                offsets['L'] = max(offsets['L'], extent[0] - b[0] + gap)
            if b[2] > extent[2] and b[2] - b[0] < (extent[2] - extent[0]) * 0.5:
                offsets['R'] = max(offsets['R'], b[2] - extent[2] + gap)
            if b[1] < extent[1] and b[3] - b[1] < (extent[3] - extent[1]) * 0.5:
                offsets['B'] = max(offsets['B'], extent[1] - b[1] + gap)
            if b[3] > extent[3] and b[3] - b[1] < (extent[3] - extent[1]) * 0.5:
                offsets['A'] = max(offsets['A'], b[3] - extent[3] + gap)
    best = None
    for _round in range(5):
        counts = _place_in_margins(items, extent, limits, gap, offset, leader_gap, min_leg, banned, offsets, allowed)
        clear_of_obstacles(items, obstacles, others, gap)
        everyone = others + items
        bad = set()
        for a, b in crossings(everyone):
            bad.add(a)
            bad.add(b)
        on_grid = [i for i in items if hits_obstacle(i.box, obstacles, gap * 0.25)
                   or leader_through_bubble(i.leader, obstacles)]
        bad_items = [i for i in items if i.key in bad] + [i for i in on_grid if i.key not in bad]
        score = (len(bad_items), len(overlaps(everyone)))
        snapshot = [(i.side, i.cu, i.t, i.u, i.v, i.elbow) for i in items]
        if best is None or score < best[0]:
            best = (score, snapshot, counts)
        if not bad_items:
            break
        for i in bad_items:
            banned[id(i)].add(i.margin)
    _score, snapshot, counts = best
    for i, (side, cu, t, u, v, elbow) in zip(items, snapshot):
        i.side, i.cu, i.t, i.u, i.v, i.elbow = side, cu, t, u, v, elbow
    return counts


def _place_in_margins(items, extent, limits, gap, offset, leader_gap=0.0, min_leg=0.0, banned=None, offsets=None,
                      allowed=None):
    """The tags that found no free space next to their element go into the band OUTSIDE the crop region
    (between the crop region `extent` and the annotation crop `limits`, both (u low, v low, u high, v high)):
    each to the margin nearest to its element - left, right, above or below - if that band is wide
    enough for the text. Left / right: a column of tags; above / below: a block of tags, the leaders
    nested so they do not cross. Returns {'L': n, 'R': n, 'A': n, 'B': n}."""
    counts = {'L': 0, 'R': 0, 'A': 0, 'B': 0}
    if not items or extent is None or limits is None:
        return counts
    band = {'L': extent[0] - limits[0], 'R': limits[2] - extent[2],
            'B': extent[1] - limits[1], 'A': limits[3] - extent[3]}
    offsets = offsets or {}
    side_offset = dict((side, max(offset, offsets.get(side, offset))) for side in band)
    groups = {'L': [], 'R': [], 'A': [], 'B': []}
    for item in items:
        if not hasattr(item, 'home'):
            item.home = (item.u, item.v)                    # where the element is (arrange slides the arrow)
        item.u, item.v = item.home
        distance = {'L': item.u - extent[0], 'R': extent[2] - item.u,
                    'B': item.v - extent[1], 'A': extent[3] - item.v}
        need = {'L': item.w + side_offset['L'], 'R': item.w + side_offset['R'],
                'B': item.h * 2 + side_offset['B'], 'A': item.h * 2 + side_offset['A']}
        usable = [side for side in distance if band[side] >= need[side]]
        if not usable:
            usable = list(distance)                         # no band is wide enough: the nearest all the same
        not_banned = [side for side in usable if side not in (banned or {}).get(id(item), set())]
        if not not_banned:
            # every usable side ruled out (crossings): the nearest ALLOWED side all the same
            not_banned = [side for side in usable if allowed is None or side in allowed] or \
                         [side for side in distance if allowed is None or side in allowed] or usable
        item.margin = min(not_banned, key=lambda side: distance[side])
        groups[item.margin].append(item)
    # above / below: a band holds only so many rows - move the farthest tags of a full band to the other one
    for side, other in (('A', 'B'), ('B', 'A')):
        if allowed is not None and other not in allowed:
            continue
        rows = lambda s: int(max(0.0, band[s] - side_offset[s]) / max(1e-9, max([i.h for i in items]) + gap))
        while len(groups[side]) > rows(side) * _blocks(groups[side], offset) and \
                len(groups[other]) < rows(other) * _blocks(groups[other] or groups[side], offset):
            far = max(groups[side], key=lambda i: (i.v - extent[1]) if side == 'B' else (extent[3] - i.v))
            groups[side].remove(far)
            far.margin = other
            groups[other].append(far)
    for side, group in groups.items():
        if not group:
            continue
        for item in group:
            item.side, item.cu, item.t, item.elbow, item.free = None, None, None, None, False
        if side in ('L', 'R'):
            arrange(group, extent[0], extent[2], gap, side_offset[side], LEFT_ONLY if side == 'L' else RIGHT_ONLY,
                    leader_gap, min_leg, limits, AUTO)
        else:
            # blocks side by side along the view: tags of elements far apart share the rows of the band
            middle = (extent[0] + extent[2]) / 2.0
            for chunk in _chunks(group, side_offset[side]):
                mean_u = sum(i.u for i in chunk) / float(len(chunk))
                arrange(chunk, extent[0], extent[2], gap, side_offset[side], RIGHT_ONLY if mean_u < middle else LEFT_ONLY,
                        leader_gap, min_leg, limits, ABOVE if side == 'A' else BELOW, (extent[1], extent[3]))
        counts[side] = len(group)
    return counts


def _chunks(group, offset):
    """Tags of one band split into blocks along the view: a new block starts where the next element is
    farther from the last one than the widest text (plus the offset) - so the blocks do not run into each
    other and can use the same rows."""
    ordered = sorted(group, key=lambda i: i.u)
    out, current = [], []
    widest = max(i.w for i in group) if group else 0.0
    for item in ordered:
        if current and item.u - current[-1].u > widest + offset * 2.0:
            out.append(current)
            current = []
        current.append(item)
    if current:
        out.append(current)
    return out


def _blocks(group, offset):
    return max(1, len(_chunks(group, offset))) if group else 1


# ---------------------------------------------------------------------------
# several arrangements to choose from
# ---------------------------------------------------------------------------
VARIANTS = [
    ('level-both', u'Level with the elements - both sides', BOTH, AUTO),
    ('level-left', u'Level with the elements - left', LEFT_ONLY, AUTO),
    ('level-right', u'Level with the elements - right', RIGHT_ONLY, AUTO),
    ('stack-both', u'Stacked, with corners - both sides', BOTH, STACKED),
    ('stack-left', u'Stacked, with corners - left', LEFT_ONLY, STACKED),
    ('stack-right', u'Stacked, with corners - right', RIGHT_ONLY, STACKED),
    ('below-right', u'Below the layout - tags to the right', RIGHT_ONLY, BELOW),
    ('below-left', u'Below the layout - tags to the left', LEFT_ONLY, BELOW),
    ('above-right', u'Above the layout - tags to the right', RIGHT_ONLY, ABOVE),
    ('above-left', u'Above the layout - tags to the left', LEFT_ONLY, ABOVE),
]


class Option(object):
    """One arrangement of the tags, for the preview."""

    def __init__(self, key, title, sides, style, items):
        self.key, self.title, self.sides, self.style, self.items = key, title, sides, style, items
        self.fixed = []                   # tags beside their element / inside their room: the same in every option
        self.corners = len([i for i in items if i.elbow is not None])
        self.straight = len(items) - self.corners
        self.length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for i in items for a, b in zip(i.leader, i.leader[1:]))
        self.outside = 0                  # tags that do not fit inside the annotation crop
        self.pulled_in = False            # the annotation crop is narrow: tags stand closer than asked
        self.best = False
        self.corner_cost = 0.0            # what one corner counts for, as a length of leader

    def rank(self):
        """Smaller is better: everything inside the annotation crop first, then at the distance asked for,
        then the least leader on the drawing - each corner counting as a piece of leader as well, so that
        a straight leader is preferred but not at the price of leaders right across the layout."""
        return (self.outside, 1 if self.pulled_in else 0, self.length + self.corners * self.corner_cost)

    def summary(self):
        text = u'{0} tag(s): {1} straight, {2} with a corner'.format(len(self.items), self.straight, self.corners)
        if self.fixed:
            text = (u'{0} tag(s) beside their element or in their room'.format(len(self.fixed)) if not self.items
                    else text + u' - and {0} beside their element or in their room'.format(len(self.fixed)))
        if getattr(self, 'outer', 0):
            text += u' - {0} in a second column'.format(self.outer)
        if self.outside:
            text += u' - {0} outside the annotation crop'.format(self.outside)
        return text

    def same_picture(self, other):
        def picture(option):
            return sorted((i.text, i.side, round(i.cu, 4), round(i.t, 4), round(i.u, 4), round(i.v, 4),
                           None if i.elbow is None else (round(i.elbow[0], 4), round(i.elbow[1], 4))) for i in option.items)
        return picture(self) == picture(other)


def copy_item(item):
    """A fresh, not yet arranged copy (to arrange the same tags in another way)."""
    fresh = Item(item.key, item.u, item.v, item.w, item.h, item.text, item.seg, item.body)
    fresh.outline = item.outline
    return fresh


def options(items, umin, umax, gap, offset, leader_gap=0.0, min_leg=0.0, limits=None, vspan=None):
    """[Option]: the different arrangements of these (not yet arranged) items, the best one first
    and marked. Arrangements that give the same picture are offered only once."""
    found = []
    for key, title, sides, style in VARIANTS:
        copies = [copy_item(item) for item in items]
        doubled = arrange_double(copies, umin, umax, gap, offset, sides, leader_gap, min_leg, limits, style, vspan)
        option = Option(key, title, sides, style, copies)
        option.outer = doubled
        option.corner_cost = offset
        if limits is not None:
            option.outside = len(outside(copies, limits))
            if style not in (BELOW, ABOVE):
                option.pulled_in = any((umin - i.box[2] < offset - 1e-6) if i.side == LEFT else (i.box[0] - umax < offset - 1e-6) for i in copies)
        if overlaps(copies) or crossings(copies):
            continue                                        # never offer a picture with overlapping texts or crossing leaders
        if not any(option.same_picture(other) for other in found):
            found.append(option)
    found.sort(key=lambda option: option.rank())
    if found:
        found[0].best = True
    return found


def variant(key):
    """(sides, style) of the variant with this key; None when there is none."""
    for name, _title, sides, style in VARIANTS:
        if name == key:
            return sides, style
    return None


# ---------------------------------------------------------------------------
# the shape of the leaders
# ---------------------------------------------------------------------------
CORNER_90, CORNER_45, DIRECT = '90', '45', 'straight'
SHAPES = [(CORNER_90, u'90\u00b0 corner'), (CORNER_45, u'45\u00b0 corner'), (DIRECT, u'Straight line')]


def shape_leaders(items, shape):
    """Give the leaders the shape the user chose. arrange() makes them with a 90 degree corner:
    out of the tag horizontally, then straight up or down to the element.

      CORNER_45  out of the tag horizontally, then at 45 degrees to the element
      DIRECT     one straight line from the tag to the element

    A leader that is already one horizontal line stays as it is. Where the new shape would make
    two leaders cross, those leaders keep their 90 degree corner. Returns how many did."""
    if shape not in (CORNER_45, DIRECT):
        return 0
    before = dict((id(item), item.elbow) for item in items)
    for item in items:
        if item.elbow is None:
            continue
        if getattr(item, 'outer', False):
            continue                                        # a second-column leader must stay level through the inner column's gaps
        if shape == DIRECT:
            item.elbow = None
            continue
        rise = abs(item.t - item.v)
        start = item.leader[0][0]
        if item.side == LEFT:
            corner = item.u - rise
            item.elbow = (corner, item.t) if corner > start + 1e-6 else None      # no room for the horizontal part: one line
        else:
            corner = item.u + rise
            item.elbow = (corner, item.t) if corner < start - 1e-6 else None
    kept = set()
    for _ in range(len(items) + 1):
        pairs = crossings(items)
        if not pairs:
            break
        for a, b in pairs:
            for item in items:
                if item.key in (a, b) and before[id(item)] is not None and item.elbow != before[id(item)]:
                    item.elbow = before[id(item)]
                    kept.add(id(item))
    return len(kept)


# ---------------------------------------------------------------------------
# checks (used by the tests)
# ---------------------------------------------------------------------------
def overlaps(items, tolerance=1e-9):
    """Pairs of tags whose texts overlap."""
    found = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            A, B = a.box, b.box
            if A[0] < B[2] - tolerance and B[0] < A[2] - tolerance and A[1] < B[3] - tolerance and B[1] < A[3] - tolerance:
                found.append((a.key, b.key))
    return found


def _cross(p1, p2, p3, p4):
    """True when the segments p1-p2 and p3-p4 cross properly (touching ends do not count)."""
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2, d3, d4 = side(p3, p4, p1), side(p3, p4, p2), side(p1, p2, p3), side(p1, p2, p4)
    return ((d1 > 1e-12 and d2 < -1e-12) or (d1 < -1e-12 and d2 > 1e-12)) and ((d3 > 1e-12 and d4 < -1e-12) or (d3 < -1e-12 and d4 > 1e-12))


def _on_top(p1, p2, p3, p4, tolerance=1e-6):
    """True when the segments p1-p2 and p3-p4 lie on ONE line and share a piece of it (one leader over the other)."""
    du, dv = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(du, dv)
    if length < tolerance or math.hypot(p4[0] - p3[0], p4[1] - p3[1]) < tolerance:
        return False
    ux, uy = du / length, dv / length
    for p in (p3, p4):                                      # both ends of the second on the line of the first
        if abs((p[0] - p1[0]) * uy - (p[1] - p1[1]) * ux) > tolerance:
            return False
    a, b = sorted(((p3[0] - p1[0]) * ux + (p3[1] - p1[1]) * uy, (p4[0] - p1[0]) * ux + (p4[1] - p1[1]) * uy))
    return min(b, length) - max(a, 0.0) > tolerance


def on_top(items):
    """Pairs of tags whose leaders lie one over the other on a piece of the same line.
    FIX: crossings() only finds leaders that cross; two leaders on one line (risers of a tray grid seen end-on,
    two in one column) were not found, and the layout left them one over the other."""
    found = []
    for i, a in enumerate(items):
        la = a.leader
        for b in items[i + 1:]:
            lb = b.leader
            if any(_on_top(la[m], la[m + 1], lb[n], lb[n + 1]) for m in range(len(la) - 1) for n in range(len(lb) - 1)):
                found.append((a.key, b.key))
    return found


def separate_legs(items, gap):
    """Leaders whose last leg goes straight up or down from the same place (end-on trays of a riser one behind the
    other, devices in a column) are moved apart across their element. The leader to the row farthest out in the
    stacking direction stands farthest from the tag column, so the legs do not cross each other's horizontal piece.
    Each column of leaders is tried centred, then pushed away from the tag column, then towards it; the first that
    adds no crossing is kept (moving towards the column can cut through the straight leaders of a neighbour)."""
    if not on_top(items):
        return items
    tolerance = 1e-4
    for side in (LEFT, RIGHT):
        mine = [i for i in items if i.side == side and not i.free and i.elbow is not None and abs(i.elbow[0] - i.u) < tolerance]
        mine.sort(key=lambda i: i.u)
        clusters, current = [], []
        for item in mine:
            if current and item.u - current[-1].u > tolerance:
                clusters.append(current)
                current = []
            current.append(item)
        if current:
            clusters.append(current)

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            spans = [tuple(sorted((i.v, i.t))) for i in cluster]
            if not any(spans[x][0] < spans[y][1] and spans[y][0] < spans[x][1] for x in range(len(cluster)) for y in range(x + 1, len(cluster))):
                continue                                    # their legs do not share a piece of the line
            # room across the elements (their boxes); else the gap on both sides of where they stand
            lows, highs = [], []
            for i in cluster:
                if i.rect is not None and i.rect[2] - i.rect[0] > 1e-6:
                    inset = (i.rect[2] - i.rect[0]) * 0.15
                    lows.append(i.rect[0] + inset)
                    highs.append(i.rect[2] - inset)
            n = len(cluster)
            here = sum(i.u for i in cluster) / n
            low = max(lows) if lows else here - gap * (n - 1)
            high = min(highs) if highs else here + gap * (n - 1)
            if high - low < 1e-6:
                low, high = here - gap * (n - 1), here + gap * (n - 1)
            step = min(gap, (high - low) / (n - 1))
            span = step * (n - 1)
            away = 1.0 if side == LEFT else -1.0            # away from the tag column: larger u on the left side
            tries = [((low + high) / 2.0 - span / 2.0, step),              # centred across the elements
                     (here if away > 0 else here - span, step),            # from where they stand, away from the column
                     (here - span if away > 0 else here, step)]            # from where they stand, towards the column
            # squeezed between where they stand and the far edge of the element (a neighbour's leader may end
            # just short of this column: then none of them may move towards the column at all)
            room = (high - here) if away > 0 else (here - low)
            if room > 1e-6:
                tight = room / (n - 1)
                tries.append((here if away > 0 else here - tight * (n - 1), tight))
            # within the leaders going up, the highest row first; within those going down, the lowest row first.
            # (one column had one leader up and two down: ordering all by one direction made the two cross)
            ups = sorted([i for i in cluster if i.t > i.v], key=lambda i: -i.t)
            downs = sorted([i for i in cluster if i.t <= i.v], key=lambda i: i.t)
            orders = [ups + downs, downs + ups] if ups and downs else [ups + downs]
            before = [(i.u, i.elbow) for i in cluster]
            base_cross, base_top = len(crossings(items)), len(on_top(items))
            done = False
            for order, (first, pitch) in [(o, f) for o in orders for f in tries]:
                first = min(max(first, low), max(low, high - pitch * (n - 1)))
                places = [first + k * pitch for k in range(n)]
                if away > 0:
                    places.reverse()                        # farthest row gets the largest u (farthest from a left column)
                for item, u in zip(order, places):
                    item.u = u
                    item.elbow = (u, item.t)
                mine_pairs = [p for p in on_top(cluster)]
                if not mine_pairs and len(crossings(items)) <= base_cross and len(on_top(items)) < base_top:
                    done = True
                    break
            if not done:
                for item, (u, elbow) in zip(cluster, before):
                    item.u, item.elbow = u, elbow
    return items

def crossings(items):
    """Pairs of tags whose leaders cross."""
    found = []
    for i, a in enumerate(items):
        la = a.leader
        for b in items[i + 1:]:
            lb = b.leader
            if any(_cross(la[m], la[m + 1], lb[n], lb[n + 1]) for m in range(len(la) - 1) for n in range(len(lb) - 1)):
                found.append((a.key, b.key))
    return found
