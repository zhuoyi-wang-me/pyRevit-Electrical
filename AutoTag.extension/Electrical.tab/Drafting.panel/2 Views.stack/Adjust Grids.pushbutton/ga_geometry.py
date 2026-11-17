# -*- coding: utf-8 -*-
"""Adjust Grids - where a grid line has to end. No Revit in this file.

Everything is in the coordinates of the crop region of a view: x to the right, y upwards, feet.
A grid is cut (or lengthened) so that it ends on the frame around the crop region:

        frame = crop region, moved outwards on each side by the distance for that side
                (the bubble distance where bubbles are wanted, the plain distance elsewhere)

All ends on one side then stand on one line, parallel to the crop edge - the bubbles line up.
"""
import math

LEFT, RIGHT, BOTTOM, TOP = 'left', 'right', 'bottom', 'top'
SIDES = (LEFT, RIGHT, BOTTOM, TOP)


def frame(crop, bubble_sides, bubble_distance, plain_distance):
    """(x low, y low, x high, y high) of the frame the grids end on.
    crop = (x low, y low, x high, y high); bubble_sides: the sides where bubbles are shown."""
    def pad(side):
        return bubble_distance if side in bubble_sides else plain_distance
    return (crop[0] - pad(LEFT), crop[1] - pad(BOTTOM), crop[2] + pad(RIGHT), crop[3] + pad(TOP))


def _side_of(point, box, tolerance=1e-6):
    """The side of the box the point lies on (the nearest one)."""
    x, y = point
    distances = [(abs(x - box[0]), LEFT), (abs(x - box[2]), RIGHT), (abs(y - box[1]), BOTTOM), (abs(y - box[3]), TOP)]
    return min(distances)[1]


def clip_line(start, end, box):
    """Where the endless line through start -> end crosses the box.

    Returns ((x, y, side), (x, y, side)): the new start and the new end, in the SAME direction as
    start -> end, each with the side of the box it lies on. None when the line does not cross the box
    (the grid is not in this view's crop region)."""
    x1, y1, x2, y2 = float(start[0]), float(start[1]), float(end[0]), float(end[1])
    dx, dy = x2 - x1, y2 - y1
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return None
    t_in, t_out = -1e30, 1e30
    for p, q in ((-dx, x1 - box[0]), (dx, box[2] - x1), (-dy, y1 - box[1]), (dy, box[3] - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return None                                 # parallel to this edge and outside it
            continue
        t = q / p
        if p < 0:
            t_in = max(t_in, t)
        else:
            t_out = min(t_out, t)
    if t_in >= t_out - 1e-12:
        return None
    a = (x1 + t_in * dx, y1 + t_in * dy)
    b = (x1 + t_out * dx, y1 + t_out * dy)
    return (a[0], a[1], _side_of(a, box)), (b[0], b[1], _side_of(b, box))


def crosses(start, end, box):
    """True when the grid as it is DRAWN (the piece from start to end, not its endless line) lies at
    least partly inside the box."""
    x1, y1, x2, y2 = float(start[0]), float(start[1]), float(end[0]), float(end[1])
    dx, dy = x2 - x1, y2 - y1
    t_in, t_out = 0.0, 1.0
    for p, q in ((-dx, x1 - box[0]), (dx, box[2] - x1), (-dy, y1 - box[1]), (dy, box[3] - y1)):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t_in = max(t_in, t)
        else:
            t_out = min(t_out, t)
    return t_in <= t_out


def well_inside(point, box, margin):
    """True when the point is inside the box and farther than `margin` from each of its edges."""
    return box[0] + margin < point[0] < box[2] - margin and box[1] + margin < point[1] < box[3] - margin


SHORT = 0.5                               # an end inside the view is drawn out when that adds at most half the grid's length


def keep_ends(a, b, new_a, new_b, crop, near, lengthen, short=None):
    """Which ends stay where they are: (keep a, keep b).

    An end that leaves the crop region, or stops within `near` of its edge, is always lined up.
    An end that stops well inside the view stays - unless `lengthen` ("line up ALL grid ends"), and then:
      * the OUTER end of a grid (the one nearer to the frame, on the grid's own side) goes out to the frame;
      * the other (inner) end goes out too only when that is short (at most half the grid's length) - a grid
        drawn a little short. It does NOT cross the whole view: a radial grid drawn from the stadium bowl
        outwards keeps its inner end, and its bubble stays on its own side.
    FIX: "line up ALL" drew every inner end right through the view to the opposite side.
    FIX 2: "short" was only "at most half the grid's length": grids W1 ... W6 (341 ft long, starting at the bowl)
    were drawn 145 ft up to the top of the view. `short` (feet, from a distance on the sheet) caps it as well:
    an inner end is drawn out only when the gap to the frame is at most that."""
    in_a, in_b = well_inside(a, crop, near), well_inside(b, crop, near)
    if not lengthen:
        return in_a, in_b
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    out_a = math.hypot(new_a[0] - a[0], new_a[1] - a[1])
    out_b = math.hypot(new_b[0] - b[0], new_b[1] - b[1])
    far_a = out_a > SHORT * length
    far_b = out_b > SHORT * length
    if short is not None:
        # FIX 3: measured to the CROP EDGE, not to the frame - the frame lies the bubble distance further out, and
        # with bubbles on every side grid XX (2.7 m inside the crop) was left short of the others
        edge = clip_line(a, b, crop)
        if edge is not None:
            far_a = far_a or math.hypot(edge[0][0] - a[0], edge[0][1] - a[1]) > short
            far_b = far_b or math.hypot(edge[1][0] - b[0], edge[1][1] - b[1]) > short
    keep_a = in_a and far_a and not (in_b and out_a <= out_b)
    keep_b = in_b and far_b and not (in_a and out_b < out_a)
    return keep_a, keep_b


def adjust_line(start, end, crop, frame_box, near, lengthen=False, short=None):
    """The new ends of a straight grid in a view.

    Only a grid that is shown inside the crop region is adjusted (a grid whose 2D extents were pulled
    out of the view to hide it stays hidden). Each end is treated on its own:
      * an end that leaves the crop region - or stops within `near` of its edge - is moved onto the
        frame: these are the ends that get lined up;
      * an end that stops well inside the view (a grid deliberately drawn short) stays where it is,
        unless lengthen is True.

    Returns ((x, y, side or None), (x, y, side or None)) - side None = this end was not moved - or
    None when the grid is not shown in the crop region, or when neither end has to move."""
    if not crosses(start, end, crop):
        return None
    on_frame = clip_line(start, end, frame_box)
    if on_frame is None:
        return None
    new_start, new_end = on_frame
    keep_start, keep_end = keep_ends(start, end, new_start, new_end, crop, near, lengthen, short)
    if keep_start:
        new_start = (float(start[0]), float(start[1]), None)
    if keep_end:
        new_end = (float(end[0]), float(end[1]), None)
    if new_start[2] is None and new_end[2] is None:
        return None
    return new_start, new_end


def last_crossing(start, end, segments):
    """Where the piece start -> end last crosses one of the segments ((x, y), (x, y)) - the crossing nearest to
    `end`: (t along start -> end, x, y), or None when it crosses none of them."""
    x1, y1 = float(start[0]), float(start[1])
    dx, dy = float(end[0]) - x1, float(end[1]) - y1
    best = None
    for c, d in segments:
        lx, ly = d[0] - c[0], d[1] - c[1]
        cross = dx * ly - dy * lx
        if abs(cross) < 1e-12:
            continue                                        # parallel
        t = ((c[0] - x1) * ly - (c[1] - y1) * lx) / cross
        s = ((c[0] - x1) * dy - (c[1] - y1) * dx) / cross
        if 1e-9 < t <= 1.0 + 1e-9 and -1e-9 <= s <= 1.0 + 1e-9 and (best is None or t > best[0]):
            best = (t, x1 + t * dx, y1 + t * dy)
    return best


def stop_at_boundary(start, end, segments, distance):
    """A grid end that runs on past the boundary (the outermost ring grid) is pulled back to `distance` beyond
    the point where it crosses that boundary. start: the other end; end: (x, y, side). Returns the new end."""
    hit = last_crossing(start, end, segments)
    if hit is None:
        return end
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    if length < 1e-9:
        return end
    along = hit[0] * length + distance
    if along >= length - 1e-6:
        return end                                          # the end is no farther out than that already
    ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
    return (hit[1] + ux * distance, hit[2] + uy * distance, end[2])


def _inside(point, box, tolerance=1e-7):
    return box[0] - tolerance <= point[0] <= box[2] + tolerance and box[1] - tolerance <= point[1] <= box[3] + tolerance


def clip_arc(centre, radius, start_angle, sweep, box):
    """The part of a curved grid that lies inside the box.

    The arc starts at start_angle (radians) and turns by `sweep` (positive = anticlockwise,
    negative = clockwise). The circle is cut where it crosses the box; of the pieces inside the box,
    the one that shares most with the original arc is taken (so an arc may also get longer, up to the box).

    Returns ((x, y, side), (x, y, side), (x, y) of a point half way) in the direction of the original
    arc; None when no part of it is inside the box, or when the whole circle is inside (nothing to cut)."""
    cx, cy = float(centre[0]), float(centre[1])
    turn = 1.0 if sweep >= 0 else -1.0
    span = abs(sweep)

    def at(tau):                                            # tau: how far along the circle from the start, in the arc's direction
        angle = start_angle + turn * tau
        return (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

    cuts = []                                               # tau of every crossing with an edge of the box, in 0 .. 2 pi
    def crossing(x, y):
        angle = math.atan2(y - cy, x - cx)
        cuts.append((turn * (angle - start_angle)) % (2.0 * math.pi))
    for x in (box[0], box[2]):
        inner = radius * radius - (x - cx) ** 2
        if inner >= 0:
            for y in (cy + math.sqrt(inner), cy - math.sqrt(inner)):
                if box[1] - 1e-9 <= y <= box[3] + 1e-9:
                    crossing(x, y)
    for y in (box[1], box[3]):
        inner = radius * radius - (y - cy) ** 2
        if inner >= 0:
            for x in (cx + math.sqrt(inner), cx - math.sqrt(inner)):
                if box[0] - 1e-9 <= x <= box[2] + 1e-9:
                    crossing(x, y)
    cuts = sorted(set(round(c, 9) for c in cuts))
    if len(cuts) < 2:
        return None
    best = None
    for index, low in enumerate(cuts):
        high = cuts[(index + 1) % len(cuts)]
        if high <= low:
            high += 2.0 * math.pi
        if high - low < 1e-7 or not _inside(at((low + high) / 2.0), box):
            continue
        # how much of the original arc (tau 0 .. span) lies in this piece - also one turn earlier
        shared = max(0.0, min(high, span) - max(low, 0.0)) + max(0.0, min(high - 2.0 * math.pi, span) - max(low - 2.0 * math.pi, 0.0))
        if shared > 1e-9 and (best is None or shared > best[0]):
            if low > span:                                  # the piece is reached going backwards from the start
                low, high = low - 2.0 * math.pi, high - 2.0 * math.pi
            best = (shared, low, high)
    if best is None:
        return None
    _shared, low, high = best
    a, b, middle = at(low), at(high), at((low + high) / 2.0)
    return (a[0], a[1], _side_of(a, box)), (b[0], b[1], _side_of(b, box)), middle
