# -*- coding: utf-8 -*-
"""Earthing Route Generator - 2D routing (pure Python, no Revit API).

Everything here works in the plan of the active view: a point is (u, v) in
feet along the view's Right and Up directions. Z does not exist in this file.

    Grid          a raster of the routing region; every cell is FREE, NEAR,
                  SOFT or HARD
    find_path     A* over the grid: orthogonal steps only, a penalty per turn
    route_points  the cells of a path turned into the few corner points of a
                  clean orthogonal polyline that starts and ends exactly on
                  the two connection points

Obstacle shapes (made by er_revit.py):
    ('rect', u0, v0, u1, v1)              an axis-aligned box
    ('seg', au, av, bu, bv, half_width)   a centre line with a width (a wall,
                                          duct, pipe, tray, conduit, beam)
    ('box', cu, cv, du, dv, half_a, half_b)  a box turned on the routing frame:
                                          centre, unit direction of its first
                                          side, half sizes along / across it
"""
import heapq
from math import ceil, floor, sqrt

FREE = 0      # route freely
NEAR = 1      # free, but close to an obstacle: costs a little, so routes keep away
SOFT = 2      # inside the clearance of an obstacle: never entered ...
HARD = 3      # inside the obstacle itself:          ... except next to the two ends

_EPS = 1e-9


class Grid(object):
    """The routing region as cells of `step` feet. Cell (i, j) stands for the
    point (u0 + i * step, v0 + j * step); routes run from point to point."""

    def __init__(self, u0, v0, u1, v1, step):
        self.step = float(step)
        self.u0, self.v0 = float(u0), float(v0)
        self.w = int(ceil((u1 - u0) / self.step)) + 1
        self.h = int(ceil((v1 - v0) / self.step)) + 1
        self.cells = bytearray(self.w * self.h)

    # ---- coordinates
    def cell_of(self, u, v):
        i = int(round((u - self.u0) / self.step))
        j = int(round((v - self.v0) / self.step))
        return min(max(i, 0), self.w - 1), min(max(j, 0), self.h - 1)

    def point_of(self, i, j):
        return self.u0 + i * self.step, self.v0 + j * self.step

    def value(self, i, j):
        return self.cells[j * self.w + i]

    def _span(self, low, high, origin, count):
        a = int(ceil((low - origin) / self.step - _EPS))
        b = int(floor((high - origin) / self.step + _EPS))
        return max(a, 0), min(b, count - 1)

    # ---- drawing
    def fill_rect(self, u0, v0, u1, v1, value):
        i0, i1 = self._span(u0, u1, self.u0, self.w)
        j0, j1 = self._span(v0, v1, self.v0, self.h)
        if i0 > i1 or j0 > j1:
            return
        row = bytearray([value]) * (i1 - i0 + 1)
        w, cells = self.w, self.cells
        for j in range(j0, j1 + 1):
            base = j * w
            cells[base + i0: base + i1 + 1] = row

    def fill_seg(self, au, av, bu, bv, half_width, inflate, value):
        """A centre line with a width, grown by `inflate` on every side."""
        across = half_width + inflate
        if abs(au - bu) < 1e-6:                       # runs along v
            self.fill_rect(au - across, min(av, bv) - inflate, au + across, max(av, bv) + inflate, value)
            return
        if abs(av - bv) < 1e-6:                       # runs along u
            self.fill_rect(min(au, bu) - inflate, av - across, max(au, bu) + inflate, av + across, value)
            return
        i0, i1 = self._span(min(au, bu) - across, max(au, bu) + across, self.u0, self.w)
        j0, j1 = self._span(min(av, bv) - across, max(av, bv) + across, self.v0, self.h)
        du, dv = bu - au, bv - av
        length2 = du * du + dv * dv
        limit2 = across * across
        w, cells, step = self.w, self.cells, self.step
        for j in range(j0, j1 + 1):
            v = self.v0 + j * step
            base = j * w
            for i in range(i0, i1 + 1):
                u = self.u0 + i * step
                t = ((u - au) * du + (v - av) * dv) / length2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                ru, rv = u - (au + t * du), v - (av + t * dv)
                if ru * ru + rv * rv <= limit2:
                    cells[base + i] = value

    def carve_box(self, cu, cv, du, dv, half_along, half_across):
        """Free the cells of a box centred on (cu, cv) whose long side runs
        along the unit direction (du, dv): a door opening in a wall."""
        if half_along <= 0 or half_across <= 0:
            return
        reach = sqrt(half_along * half_along + half_across * half_across)
        i0, i1 = self._span(cu - reach, cu + reach, self.u0, self.w)
        j0, j1 = self._span(cv - reach, cv + reach, self.v0, self.h)
        w, cells, step = self.w, self.cells, self.step
        for j in range(j0, j1 + 1):
            v = self.v0 + j * step - cv
            base = j * w
            for i in range(i0, i1 + 1):
                u = self.u0 + i * step - cu
                if abs(u * du + v * dv) <= half_along and abs(-u * dv + v * du) <= half_across:
                    cells[base + i] = FREE

    def fill_box(self, cu, cv, du, dv, half_a, half_b, grow, value):
        """A turned box, grown by `grow` on every side (square corners).
        FIX: an element square to the DRAWING in a room turned on it (routes laid out at 45 degrees) was its
        bounding box in the routing frame - a box 1.4 times as large, which filled the space between the
        transformers and the room's wall and sent every line round the outside of the room."""
        a, b = half_a + grow, half_b + grow
        reach = sqrt(a * a + b * b)
        i0, i1 = self._span(cu - reach, cu + reach, self.u0, self.w)
        j0, j1 = self._span(cv - reach, cv + reach, self.v0, self.h)
        w, cells, step = self.w, self.cells, self.step
        for j in range(j0, j1 + 1):
            v = self.v0 + j * step - cv
            base = j * w
            for i in range(i0, i1 + 1):
                u = self.u0 + i * step - cu
                if abs(u * du + v * dv) <= a and abs(-u * dv + v * du) <= b:
                    if cells[base + i] < value or value == FREE:
                        cells[base + i] = value

    def stamp(self, shapes, clearance):
        """Draw obstacles with their clearance.

        Every band is grown by half a cell: a cell is a point, and the line
        between two free points must not slip past a thin obstacle or come
        closer than the clearance when its ends are moved onto the real
        connection points (at most half a cell)."""
        half = self.step / 2.0
        for value, grow in ((NEAR, clearance + half + 2.0 * self.step),
                            (SOFT, clearance + half),
                            (HARD, half)):
            for shape in shapes:
                if shape[0] == 'rect':
                    self.fill_rect(shape[1] - grow, shape[2] - grow, shape[3] + grow, shape[4] + grow, value)
                elif shape[0] == 'box':
                    self.fill_box(shape[1], shape[2], shape[3], shape[4], shape[5], shape[6], grow, value)
                else:
                    self.fill_seg(shape[1], shape[2], shape[3], shape[4], shape[5], grow, value)

    def merge(self, other):
        """Keep, cell by cell, the stronger of this grid and another one of
        the same size."""
        a, b = self.cells, other.cells
        self.cells = bytearray(x if x >= y else y for x, y in zip(a, b))


def shape_bounds(shape):
    """(u0, v0, u1, v1) of a shape."""
    if shape[0] == 'rect':
        return shape[1], shape[2], shape[3], shape[4]
    if shape[0] == 'box':
        cu, cv, du, dv, a, b = shape[1:7]
        ru, rv = abs(du) * a + abs(dv) * b, abs(dv) * a + abs(du) * b
        return cu - ru, cv - rv, cu + ru, cv + rv
    hw = shape[5]
    return (min(shape[1], shape[3]) - hw, min(shape[2], shape[4]) - hw,
            max(shape[1], shape[3]) + hw, max(shape[2], shape[4]) + hw)


def shape_touches(shape, u0, v0, u1, v1, margin=0.0):
    a0, b0, a1, b1 = shape_bounds(shape)
    return not (a1 + margin < u0 or a0 - margin > u1 or b1 + margin < v0 or b0 - margin > v1)


# ---------------------------------------------------------------------------
# A*
# ---------------------------------------------------------------------------
_DX = (1, 0, -1, 0)
_DY = (0, 1, 0, -1)


def find_path(grid, start, goal, turn_penalty=6.0, near_cost=0.6, escape=6,
              soft_cost=25.0, hard_cost=2500.0, max_expand=600000, targets=None,
              crowd=None, crowd_cost=8.0, cross_cost=80.0, pull_axis=None, pull=0.01,
              walls=False):
    """Cheapest orthogonal path between two cells.

    Cost = cells walked + turn_penalty per 90 degree turn + near_cost per cell
    close to an obstacle. SOFT / HARD cells cannot be entered, except within
    `escape` cells of the start or the goal: a panel on a wall sits inside the
    wall's clearance and has to be allowed to leave it. Even there they cost a
    lot, so the route leaves by the shortest way and never crosses an obstacle
    when there is any way round.

    targets: [(i0, j0, i1, j1), ...] runs of cells of lines that are already
    routed. The search then ends on the goal OR on the first of those cells
    it reaches, so the new line tees into the existing one instead of running
    on top of it all the way to the earth bar.

    crowd: a mask (bytearray, one per cell) round the earlier lines:
        1  closer to an earlier line than the wanted spacing: costs
           crowd_cost per cell, so a second line settles beside the first
        2  ON an earlier line: costs cross_cost, as much as a long detour.
           A line therefore crosses another one only when going round would
           be far longer, or when there is no other way.

    walls: True = the earlier lines are WALLS. A cell on an earlier line can
    not be entered at all, and a cell closer to one than the spacing only
    right next to the two ends. The new line then cannot cross an earlier
    one; it either finds a way beside them or there is no route (the caller
    then tries again with walls=False, which crosses at a cost).

    pull_axis: 'u' or 'v' = the direction the earth bar's long side runs in.
    A step in that direction costs a little more the further it is from the
    row (column) the line ends in. A line therefore first comes towards the
    bar and then runs along it close by, on the nearest free track - which
    is what lets several lines nest without crossing. Without it a line may
    just as well run along first and come down right at the bar, cutting off
    every line that has to pass that point.

    Returns (cells, info). cells is [(i, j), ...] or None; info has
    'hard' / 'soft' (blocked cells used near the ends), 'expanded', and
    'reason' when there is no path."""
    w, h, cells = grid.w, grid.h, grid.cells
    si, sj = start
    gi, gj = goal
    info = {'hard': 0, 'soft': 0, 'expanded': 0, 'reason': ''}
    boxes = [(gi, gj, gi, gj)]
    finish = None
    if targets:
        finish = bytearray(w * h)
        finish[gj * w + gi] = 1
        for i0, j0, i1, j1 in targets:
            i0, i1 = max(0, min(i0, i1)), min(w - 1, max(i0, i1))
            j0, j1 = max(0, min(j0, j1)), min(h - 1, max(j0, j1))
            if i0 > i1 or j0 > j1:
                continue
            boxes.append((i0, j0, i1, j1))
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    finish[j * w + i] = 1
    if start == goal or (finish is not None and finish[sj * w + si]):
        return [start], info

    if finish is None:
        def cost_estimate(i, j):
            di, dj = abs(i - gi), abs(j - gj)
            return di + dj + (turn_penalty if di and dj else 0.0)
    else:
        def cost_estimate(i, j):
            # distance to the nearest place the line may end
            best_distance = 1e18
            for i0, j0, i1, j1 in boxes:
                distance = ((i0 - i if i < i0 else (i - i1 if i > i1 else 0)) +
                            (j0 - j if j < j0 else (j - j1 if j > j1 else 0)))
                if distance < best_distance:
                    best_distance = distance
            return best_distance

    start_state = (sj * w + si) * 5 + 4               # 4 = no direction yet
    best = {start_state: 0.0}
    came = {}
    heap = [(cost_estimate(si, sj), 0.0, start_state)]
    push, pop = heapq.heappush, heapq.heappop
    found = None
    expanded = 0
    while heap:
        _f, g, state = pop(heap)
        if g > best.get(state, 1e18):
            continue
        cell, d = divmod(state, 5)
        j, i = divmod(cell, w)
        if (i == gi and j == gj) if finish is None else finish[cell]:
            found = state
            break
        expanded += 1
        if expanded > max_expand:
            info['reason'] = 'the search area is too large'
            break
        for nd in range(4):
            if d != 4 and (nd - d) % 4 == 2:
                continue                              # no U-turn
            ni, nj = i + _DX[nd], j + _DY[nd]
            if ni < 0 or nj < 0 or ni >= w or nj >= h:
                continue
            kind = cells[nj * w + ni]
            step = 1.0
            if kind == NEAR:
                step += near_cost
            elif kind >= SOFT:
                if not ((abs(ni - si) <= escape and abs(nj - sj) <= escape) or
                        (abs(ni - gi) <= escape and abs(nj - gj) <= escape)):
                    continue
                step += soft_cost if kind == SOFT else hard_cost
            if d != 4 and nd != d:
                step += turn_penalty
            if pull_axis is not None:
                if pull_axis == 'u':
                    if nd == 0 or nd == 2:
                        step += pull * abs(nj - gj)
                elif nd == 1 or nd == 3:
                    step += pull * abs(ni - gi)
            if crowd is not None:
                near_line = crowd[nj * w + ni]
                if near_line:
                    if walls:
                        if near_line == 2:
                            continue
                        if not ((abs(ni - si) <= escape and abs(nj - sj) <= escape) or
                                (abs(ni - gi) <= escape and abs(nj - gj) <= escape)):
                            continue
                    step += cross_cost if near_line == 2 else crowd_cost
            ng = g + step
            ns = (nj * w + ni) * 5 + nd
            if ng < best.get(ns, 1e18):
                best[ns] = ng
                came[ns] = state
                push(heap, (ng + cost_estimate(ni, nj), ng, ns))
    info['expanded'] = expanded
    if found is None:
        info['reason'] = info['reason'] or 'every way is blocked'
        return None, info
    path = []
    state = found
    while True:
        cell = state // 5
        j, i = divmod(cell, w)
        path.append((i, j))
        if state == start_state:
            break
        state = came[state]
    path.reverse()
    for i, j in path[1:-1]:
        kind = cells[j * w + i]
        if kind == HARD:
            info['hard'] += 1
        elif kind == SOFT:
            info['soft'] += 1
    return path, info


# ---------------------------------------------------------------------------
# from cells to a clean polyline
# ---------------------------------------------------------------------------
def simplify(cells):
    """Only the first cell, the last cell and the cells where the path turns."""
    if len(cells) <= 2:
        return list(cells)
    kept = [cells[0]]
    for index in range(1, len(cells) - 1):
        a, b, c = cells[index - 1], cells[index], cells[index + 1]
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            kept.append(b)
    kept.append(cells[-1])
    return kept


def clean(points, tolerance=1e-6):
    """Drop repeated points and points in the middle of a straight run."""
    out = []
    for point in points:
        if out and abs(point[0] - out[-1][0]) < tolerance and abs(point[1] - out[-1][1]) < tolerance:
            continue
        out.append((point[0], point[1]))
    index = 1
    while index < len(out) - 1:
        a, b, c = out[index - 1], out[index], out[index + 1]
        same_u = abs(a[0] - b[0]) < tolerance and abs(b[0] - c[0]) < tolerance
        same_v = abs(a[1] - b[1]) < tolerance and abs(b[1] - c[1]) < tolerance
        if same_u or same_v:
            del out[index]
        else:
            index += 1
    return out


def route_points(grid, cells, start_point, end_point):
    """The corner points of the route, starting exactly on start_point and
    ending exactly on end_point.

    Path cells are grid points, the two connection points are not. The first
    and the last leg are slid sideways (by less than half a cell) onto the
    connection points, so the result stays strictly horizontal / vertical."""
    su, sv = start_point
    eu, ev = end_point
    tolerance = 1e-6
    corners = [grid.point_of(i, j) for i, j in simplify(cells)]
    if len(corners) < 2:                              # both ends in one cell
        return clean([(su, sv), (eu, sv), (eu, ev)])
    if len(corners) == 2:                             # one straight run
        horizontal = abs(corners[0][1] - corners[1][1]) < tolerance
        if horizontal:
            if abs(sv - ev) < tolerance:
                return clean([(su, sv), (eu, ev)])
            middle = (su + eu) / 2.0
            return clean([(su, sv), (middle, sv), (middle, ev), (eu, ev)])
        if abs(su - eu) < tolerance:
            return clean([(su, sv), (eu, ev)])
        middle = (sv + ev) / 2.0
        return clean([(su, sv), (su, middle), (eu, middle), (eu, ev)])
    points = [list(p) for p in corners]
    if abs(points[0][1] - points[1][1]) < tolerance:  # first leg horizontal
        points[1][1] = sv
    else:
        points[1][0] = su
    points[0] = [su, sv]
    if abs(points[-1][1] - points[-2][1]) < tolerance:  # last leg horizontal
        points[-2][1] = ev
    else:
        points[-2][0] = eu
    points[-1] = [eu, ev]
    return clean([(p[0], p[1]) for p in points])


def inside_box(box, point):
    """box = (centre u, centre v, axis du, axis dv, half length along the
    axis, half length across it): a rectangle that may be turned."""
    cu, cv, du, dv, half_a, half_b = box
    x, y = point[0] - cu, point[1] - cv
    return abs(x * du + y * dv) <= half_a + 1e-9 and abs(-x * dv + y * du) <= half_b + 1e-9


def _trim_start(points, box):
    """Cut off the part of the route that lies inside the outline of the
    element it starts in, so the line begins on the element's EDGE."""
    if box is None or len(points) < 2 or not inside_box(box, points[0]):
        return points
    for index in range(len(points) - 1):
        a, b = points[index], points[index + 1]
        if inside_box(box, b):
            continue
        low, high = 0.0, 1.0                       # a is inside, b is outside
        for _ in range(48):
            middle = (low + high) / 2.0
            if inside_box(box, (a[0] + (b[0] - a[0]) * middle, a[1] + (b[1] - a[1]) * middle)):
                low = middle
            else:
                high = middle
        edge = (a[0] + (b[0] - a[0]) * low, a[1] + (b[1] - a[1]) * low)
        return [edge] + list(points[index + 1:])
    return points                                  # the whole route is inside the outline


def trim_ends(points, start_box, end_box):
    """The route from the edge of the first element to the edge of the last
    one (instead of centre to centre). A box of None leaves that end alone."""
    trimmed = _trim_start(list(points), start_box)
    trimmed = _trim_start(trimmed[::-1], end_box)[::-1]
    return clean(trimmed)


def spread_along(box, count, spacing):
    """`count` points spread along the long side of an outline, `spacing`
    apart and centred (closer together when the outline is too short), so
    lines that end on the same earth bar do not all end on one spot.
    box = (centre u, centre v, axis du, axis dv, half length, half width)."""
    cu, cv, du, dv, half_a, half_b = box
    if half_b > half_a:                               # the long side is the other axis
        du, dv, half_a = -dv, du, half_b
    if count <= 1:
        return [(cu, cv)]
    room = max(0.0, 2.0 * half_a - spacing)           # keep half a spacing free at both ends
    gap = min(spacing, room / (count - 1))
    first = -gap * (count - 1) / 2.0
    return [(cu + du * (first + gap * index), cv + dv * (first + gap * index)) for index in range(count)]


def bar_entries(box, starts, spacing, lead, to_edge=True, keys=None):
    """How the lines enter the earth bar: square to its long side, each on its
    own spot, all from the side the panels are on - like terminals on a bar.

    box     outline of the earth bar (centre u, centre v, axis du, axis dv,
            half length, half width)
    starts  the panels' points, one per line
    spacing distance between two spots (less when the bar is too short)
    lead    how far in front of the bar a line turns in

    Returns [(approach point, point on the bar), ...] in the order of
    `starts`, or None when the bar is not square to the view (a slanted last
    leg is not wanted; the caller then routes to the bar as before).
    The spots are handed out in the order the panels lie along the bar, so
    lines from the left take the left spots and do not have to cross."""
    cu, cv, du, dv, half_a, half_b = box
    if half_b > half_a:                               # the long side is the other axis
        du, dv, half_a, half_b = -dv, du, half_b, half_a
    if not (abs(du) > 0.999 or abs(dv) > 0.999):
        return None
    nu, nv = -dv, du                                  # across the bar
    count = len(starts)
    if not count:
        return []
    # the side most panels are on
    balance = sum((1 if ((s[0] - cu) * nu + (s[1] - cv) * nv) >= 0 else -1) for s in starts)
    side = 1.0 if balance >= 0 else -1.0
    spots = spread_along((cu, cv, du, dv, half_a, half_b), count, spacing)
    # keys: one sort key per line that says from where it really arrives
    # (found by routing it once on its own); without them the panels'
    # positions along the bar are used
    if keys is not None and len(keys) == count:
        order = sorted(range(count), key=lambda index: keys[index])
    else:
        order = sorted(range(count), key=lambda index: (starts[index][0] - cu) * du + (starts[index][1] - cv) * dv)
    result = [None] * count
    for spot, index in zip(spots, order):
        reach = half_b if to_edge else 0.0
        touch = (spot[0] + nu * side * reach, spot[1] + nv * side * reach)
        approach = (spot[0] + nu * side * (half_b + lead), spot[1] + nv * side * (half_b + lead))
        result[index] = (approach, touch)
    return result


def is_turned(box):
    """True when an outline is not square to the routing frame."""
    if box is None:
        return False
    du, dv = box[2], box[3]
    return not (abs(du) > 0.9999 or abs(dv) > 0.9999)


def turned_entries(box, starts, step, spacing, lead, to_edge=True, stagger=0, shift=0):
    """Square entry into an earth bar that is TURNED in the routing frame (a bar square to the drawing in a
    room laid out at 45 degrees): every line runs on the frame to a point straight in front of its own spot
    and its last leg goes square into the bar's long side - slanted on the frame, square to the bar.

    The spots are spread so that their approach points lie whole grid cells apart on BOTH frame axes (the
    lines that run to them stay on separate tracks), centred on the bar, and handed out in the order the
    panels lie along the bar so the lines nest instead of crossing.
    Returns ([(point to route to, [the points after it, ending on the bar]), ...] in the order of `starts`,
    note) or (None, '')."""
    if box is None or not starts:
        return None, u''
    cu, cv, du, dv, half_a, half_b = _bar_axes(box)
    nu, nv = -dv, du
    mu = sum(p[0] for p in starts) / float(len(starts))
    mv = sum(p[1] for p in starts) / float(len(starts))
    side = 1.0 if (mu - cu) * nu + (mv - cv) * nv >= 0 else -1.0
    count = len(starts)
    note = u''
    gap = 0.0
    # the frame axis the bar's normal mostly runs along: the lines come in along it
    normal_u = abs(nu) >= abs(nv)
    lined_up = max(abs(nu), abs(nv)) > 0.8          # bar within ~37 degrees of the frame: line the lines up
    if count > 1:
        cells = max(1, int(ceil(spacing / step - 1e-6)))
        if lined_up:
            # FIX: a bar only ~11 degrees off the frame had its spots 60 mm apart across the lines' way in:
            # one line's last leg cut across the next line. Now the spots are whole grid cells apart along the
            # bar - as far apart as the bar allows, up to 3 times the spacing - and every line comes in on its
            # own row (see `via` below).
            unit = step / max(abs(du), abs(dv))
            fits = int(floor(2.0 * half_a * 0.9 / (count - 1) / unit + 1e-9))
            gap = (cells if shift else max(cells, min(fits, 3 * cells))) * unit
        else:
            gap = cells * step / max(min(abs(du), abs(dv)), 0.25)
        room = 2.0 * half_a * 0.9
        if gap * (count - 1) > room:
            gap = room / (count - 1)
            if gap < spacing * 0.999:
                note = u'the earth bar is short for {0} lines: their last legs are {1:.0f} mm apart'.format(
                    count, gap * 304.8)
    along = sorted(range(count), key=lambda index: (starts[index][0] - cu) * du + (starts[index][1] - cv) * dv)
    result = [None] * count
    end = half_a * 0.9
    for rank, index in enumerate(along):
        position = (rank - (count - 1) / 2.0) * gap
        if shift > 0 and count > 1 and gap * (count - 1) < 2.0 * end:
            # shift: the spots packed at one end of the bar (the lines that come in past a panel standing
            # beside the bar then need not squeeze through the gap between them)
            position = end - (count - 1 - rank) * gap
        elif shift < 0 and count > 1 and gap * (count - 1) < 2.0 * end:
            position = -end + rank * gap
        su, sv = cu + du * position, cv + dv * position
        reach = half_b if to_edge else 0.0
        touch = (su + nu * side * reach, sv + nv * side * reach)
        approach = (su + nu * side * (half_b + lead), sv + nv * side * (half_b + lead))
        result[index] = (approach, touch)
    if lined_up and count > 1:
        # every line first reaches its row on ONE common line in front of all spots (the spot furthest out),
        # then runs straight along its row to its approach point and square into the bar.
        # stagger +1 / -1: the rows are reached on separate lines instead, one grid step further out per
        # spot from one end of the bar - lines that climb past each other into their rows then nest (the
        # caller tries all three and keeps the one without crossings).
        sign = 1.0 if (nu if normal_u else nv) * side > 0 else -1.0
        outward = [(a[0] if normal_u else a[1]) * sign for a, _t in result]
        front = max(outward)
        rank = dict((index, position) for position, index in enumerate(along if stagger >= 0 else list(reversed(along))))
        lined = []
        for index, (approach, touch) in enumerate(result):
            out = (front + (rank[index] * max(1, int(ceil(spacing / step - 1e-6))) * step if stagger else 0.0)) * sign
            via = (out, approach[1]) if normal_u else (approach[0], out)
            lined.append((via, [approach, touch]))
        return lined, note
    return [(approach, [touch]) for approach, touch in result], note


def long_axis(box):
    """'u' or 'v': the direction the long side of an outline runs in."""
    cu, cv, du, dv, half_a, half_b = box
    if half_b > half_a:
        du, dv = -dv, du
    return 'u' if abs(du) >= abs(dv) else 'v'


def entry_order(touches, box):
    """The order in which lines that enter one earth bar should be routed so
    that they do not cross: the OUTERMOST spots first, working inwards.

    A line to an outer spot is routed first and takes the track nearest to
    the bar; the next one, to the spot beside it, has to pass that spot and
    so runs one track further out - the lines nest instead of crossing.
    touches: the point on the bar of each line. Returns indexes into it."""
    cu, cv, du, dv, half_a, half_b = box
    if half_b > half_a:
        du, dv = -dv, du
    along = sorted(range(len(touches)),
                   key=lambda index: (touches[index][0] - cu) * du + (touches[index][1] - cv) * dv)
    order = []
    low, high = 0, len(along) - 1
    while low <= high:
        order.append(along[low])
        if high != low:
            order.append(along[high])
        low += 1
        high -= 1
    return order


def break_at_crossings(points, others, margin, half_width, height):
    """Where a line has to cross other lines it is INTERRUPTED: the line stops
    short of them with a break mark, and starts again behind them with a
    second one - the lines it crosses stay whole.

    points  the line, horizontal / vertical pieces
    others  [((u, v), (u, v)), ...] pieces of the lines that stay whole
    margin  how far from the crossed lines the line stops
    half_width, height  size of the break mark (a small zigzag across the
            end of the line)

    Lines crossed close together (a bundle of parallel lines) get ONE gap.
    A crossing too near a corner or an end of the line to fit a gap is left
    as a plain crossing.
    Returns (pieces, marks, count): the parts of the line, the zigzags, and
    the number of gaps made."""
    eps = 1e-6
    pieces, marks, count = [], [], 0
    current = [points[0]]
    for a, b in zip(points, points[1:]):
        length = abs(b[0] - a[0]) + abs(b[1] - a[1])
        if length < eps:
            continue
        flat = abs(a[1] - b[1]) < eps
        du, dv = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        hits = []
        for p, q in others:
            if flat:
                # a vertical piece that passes through this horizontal one
                if (abs(p[0] - q[0]) < eps and min(a[0], b[0]) + eps < p[0] < max(a[0], b[0]) - eps
                        and min(p[1], q[1]) + eps < a[1] < max(p[1], q[1]) - eps):
                    hits.append(abs(p[0] - a[0]))
            elif (abs(p[1] - q[1]) < eps and min(a[1], b[1]) + eps < p[1] < max(a[1], b[1]) - eps
                  and min(p[0], q[0]) + eps < a[0] < max(p[0], q[0]) - eps):
                hits.append(abs(p[1] - a[1]))
        hits.sort()
        groups = []
        for hit in hits:
            if groups and hit - groups[-1][1] <= 3.0 * margin:
                groups[-1][1] = hit
            else:
                groups.append([hit, hit])
        for first, last in groups:
            begin, end = first - margin, last + margin
            if begin < half_width or end > length - half_width:
                continue                              # no room for a gap here
            nu, nv = -dv, du                          # across the line
            stop = (a[0] + du * begin, a[1] + dv * begin)
            start = (a[0] + du * end, a[1] + dv * end)
            current.append(stop)
            pieces.append(current)
            current = [start]
            for point, sign in ((stop, 1.0), (start, -1.0)):      # sign: towards the gap
                gu, gv = du * sign, dv * sign
                marks.append([
                    (point[0] - nu * half_width - gu * height / 2.0, point[1] - nv * half_width - gv * height / 2.0),
                    (point[0] - nu * half_width / 3.0 + gu * height / 2.0, point[1] - nv * half_width / 3.0 + gv * height / 2.0),
                    (point[0] + nu * half_width / 3.0 - gu * height / 2.0, point[1] + nv * half_width / 3.0 - gv * height / 2.0),
                    (point[0] + nu * half_width + gu * height / 2.0, point[1] + nv * half_width + gv * height / 2.0)])
            count += 1
        current.append(b)
    pieces.append(current)
    return [piece for piece in pieces if length_of(piece) > eps], marks, count


def tour(points):
    """The order in which to visit the points for a loop: start at the first
    and always go on to the nearest one not yet visited. Returns indexes."""
    left = list(range(1, len(points)))
    order = [0] if points else []
    while left:
        here = points[order[-1]]
        nearest = min(left, key=lambda index: abs(points[index][0] - here[0]) + abs(points[index][1] - here[1]))
        left.remove(nearest)
        order.append(nearest)
    return order


def nest_order(keys, middle):
    """The order in which the lines of one earth bar must be routed so that
    they can nest without crossing.

    keys[i]: where line i arrives from - the coordinates (along the bar) of
    the corners of its own best route, counted back from the bar. middle:
    that coordinate of the point in front of the bar.

    Lines that arrive from the left are routed first, the one that turns in
    furthest from the bar first: it takes the track nearest to the bar and
    the next one runs just outside it. Then the same for the lines from the
    right. Lines that come straight at the bar are last. (Taking the two
    sides in turn by spot number is wrong as soon as one side has more lines
    than the other: an inner line would then be routed before the lines it
    has to run outside of.)"""
    def side(key):
        for value in key:
            if value < middle - 1e-6:
                return -1
            if value > middle + 1e-6:
                return 1
        return 0
    left = sorted([i for i in range(len(keys)) if side(keys[i]) < 0], key=lambda i: keys[i])
    right = sorted([i for i in range(len(keys)) if side(keys[i]) > 0], key=lambda i: keys[i], reverse=True)
    straight = sorted([i for i in range(len(keys)) if side(keys[i]) == 0], key=lambda i: keys[i])
    return left + right + straight


def _cross(a0, a1, b0, b1, eps=1e-6):
    """True when two pieces cross each other properly - through each other, not just touching (a tee, a
    shared corner) and not lying along each other. Any angle.
    FIX: every piece that was not horizontal was taken as vertical: the slanted last leg into a turned
    earth bar was counted (and broken) as a crossing where there was none, and real ones were missed."""
    def side(p, q, r):
        value = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        length = ((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2) ** 0.5
        value = value / length if length > 0 else 0.0
        return 0 if abs(value) < eps else (1 if value > 0 else -1)
    s1, s2 = side(a0, a1, b0), side(a0, a1, b1)
    s3, s4 = side(b0, b1, a0), side(b0, b1, a1)
    return s1 * s2 < 0 and s3 * s4 < 0


def crossings(routes):
    """How many times the given routes ([points, ...]) cross each other."""
    count = 0
    for first in range(len(routes)):
        for second in range(first + 1, len(routes)):
            for a0, a1 in zip(routes[first], routes[first][1:]):
                for b0, b1 in zip(routes[second], routes[second][1:]):
                    if _cross(a0, a1, b0, b1):
                        count += 1
    return count


def crossings_of(points, segments):
    """How many times one line crosses the given pieces of other lines."""
    count = 0
    for a, b in zip(points, points[1:]):
        for p, q in segments:
            if _cross(a, b, p, q):
                count += 1
    return count


def _bar_axes(box):
    """(centre u, centre v, axis du, axis dv, half length, half width) with
    the axis along the LONG side."""
    cu, cv, du, dv, half_a, half_b = box
    if half_b > half_a:
        du, dv, half_a, half_b = -dv, du, half_b, half_a
    return cu, cv, du, dv, half_a, half_b


def occupied(box, segments, tolerance):
    """Where earthing lines already meet an earth bar: [(position along the
    bar, side +1 / -1), ...] - one per line end that lies on the bar."""
    cu, cv, du, dv, half_a, half_b = _bar_axes(box)
    nu, nv = -dv, du
    found = []
    for a, b in segments:
        for end, other in ((a, b), (b, a)):
            x, y = end[0] - cu, end[1] - cv
            along, across = x * du + y * dv, x * nu + y * nv
            if abs(along) <= half_a + tolerance and abs(across) <= half_b + tolerance:
                far = (other[0] - cu) * nu + (other[1] - cv) * nv
                found.append((along, 1.0 if (far if abs(far) > abs(across) else across) >= 0 else -1.0))
    return found


def bar_slot(box, toward, taken, spacing, lead, to_edge=True, side=None, turned=False):
    """One free spot on an earth bar for one more line, entered square to the
    bar's long side - the same way the panel lines enter it.

    toward  the point the line goes to: the spot is taken at the END of the
            bar that faces it, so the line does not have to pass the others
    taken   [(position along the bar, side), ...] already in use (occupied())
    side    +1 / -1 to force a side; None = the side the lines already on the
            bar use, else the side that faces `toward`

    Returns (approach point, point on the bar, position, side), or None when
    the bar is not square to the view."""
    cu, cv, du, dv, half_a, half_b = _bar_axes(box)
    if not (abs(du) > 0.999 or abs(dv) > 0.999) and not turned:
        return None
    # turned=True: a bar at an angle to the routing frame gets the same square leg - slanted on the frame,
    # square to the bar (the loop and pit lines; FIX: they left / entered a turned bar at an angle)
    nu, nv = -dv, du
    if side is None:
        balance = sum(s for _position, s in taken)
        if balance:
            side = 1.0 if balance > 0 else -1.0
        else:
            across = (toward[0] - cu) * nu + (toward[1] - cv) * nv
            side = 1.0 if across >= 0 else -1.0
    along = (toward[0] - cu) * du + (toward[1] - cv) * dv
    end = 1.0 if along > 1e-6 else (-1.0 if along < -1e-6 else 0.0)
    room = half_a - spacing / 2.0
    candidates = []
    if room <= 0:
        candidates = [0.0]
    elif end:
        position = end * room                          # from the end that faces `toward`, inwards
        while -room - 1e-9 <= position <= room + 1e-9:
            candidates.append(position)
            position -= end * spacing
    else:
        candidates = [0.0]
        k = 1
        while k * spacing <= room + 1e-9:
            candidates += [k * spacing, -k * spacing]
            k += 1
    used = [position for position, s in taken if s == side]
    choice = None
    for position in candidates:
        if not [1 for other in used if abs(other - position) < spacing * 0.9]:
            choice = position
            break
    if choice is None:
        choice = candidates[0]                         # the bar is full: share the end spot
    spot = (cu + du * choice, cv + dv * choice)
    reach = half_b if to_edge else 0.0
    touch = (spot[0] + nu * side * reach, spot[1] + nv * side * reach)
    approach = (spot[0] + nu * side * (half_b + lead), spot[1] + nv * side * (half_b + lead))
    return approach, touch, choice, side


def box_rect(box):
    """Obstacle of an outline: ('rect', ...) when it is square to the routing frame, else ('box', ...).
    FIX: a turned outline (an earth bar square to the drawing in a room turned 45 degrees) was made a rect of
    its half sizes, ignoring the turn."""
    cu, cv, du, dv, half_a, half_b = box
    if not (abs(du) > 0.9999 or abs(dv) > 0.9999):
        return ('box', cu, cv, du, dv, half_a, half_b)
    if abs(dv) > abs(du):
        half_a, half_b = half_b, half_a
    return ('rect', cu - half_a, cv - half_b, cu + half_a, cv + half_b)


def bend_pieces(points, radius, min_radius=0.02):
    """The route as drawing pieces, with every square (90 degree) corner rounded off by an arc - a bend of
    the earthing tape instead of a sharp corner.

    radius     wanted bend radius (feet); 0 = sharp corners (plain lines)
    Returns [('line', a, b) | ('arc', a, middle, b), ...].
    A bend never takes more than half of a leg between two corners (the whole leg at the two ends), so two
    corners close together each get a smaller bend instead of overlapping. Corners that are not square
    (a break mark's zigzag, a slanted leg into a turned earth bar) stay sharp."""
    points = [(float(p[0]), float(p[1])) for p in points]
    count = len(points)
    radii = list(radius) if isinstance(radius, (list, tuple)) else [radius] * count
    if count < 3 or max(radii[1:-1] or [0.0]) <= 0:
        return [('line', a, b) for a, b in zip(points, points[1:])]
    lengths, units = [], []
    for a, b in zip(points, points[1:]):
        length = sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)
        lengths.append(length)
        units.append(((b[0] - a[0]) / length, (b[1] - a[1]) / length) if length > 1e-9 else (0.0, 0.0))
    # how far each corner may cut back into its two legs
    cut = [0.0] * count
    for index in range(1, count - 1):
        d0, d1 = units[index - 1], units[index]
        if abs(d0[0] * d1[0] + d0[1] * d1[1]) > 0.035:      # not within ~2 degrees of square
            continue
        if lengths[index - 1] < 1e-9 or lengths[index] < 1e-9:
            continue
        before = lengths[index - 1] if index - 1 == 0 else lengths[index - 1] / 2.0
        after = lengths[index] if index + 1 == count - 1 else lengths[index] / 2.0
        size = min(radii[index], before, after)
        if size >= min_radius:
            cut[index] = size
    pieces = []
    start = points[0]
    for index in range(1, count):
        corner = points[index]
        size = cut[index] if index < count - 1 else 0.0
        if size <= 0:
            end = corner
            if sqrt((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) > 1e-9:
                pieces.append(('line', start, end))
            start = end
            continue
        d0, d1 = units[index - 1], units[index]
        t1 = (corner[0] - d0[0] * size, corner[1] - d0[1] * size)
        t2 = (corner[0] + d1[0] * size, corner[1] + d1[1] * size)
        if sqrt((t1[0] - start[0]) ** 2 + (t1[1] - start[1]) ** 2) > 1e-9:
            pieces.append(('line', start, t1))
        centre = (t1[0] + d1[0] * size, t1[1] + d1[1] * size)
        cu, cv = corner[0] - centre[0], corner[1] - centre[1]
        reach = sqrt(cu * cu + cv * cv)
        middle = (centre[0] + cu / reach * size, centre[1] + cv / reach * size)
        pieces.append(('arc', t1, middle, t2))
        start = t2
    return pieces


def _square_corners(points):
    """[(vertex index, corner, incoming unit direction, outgoing unit direction), ...] of the square corners."""
    found = []
    for index in range(1, len(points) - 1):
        a, c, b = points[index - 1], points[index], points[index + 1]
        l0 = sqrt((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2)
        l1 = sqrt((b[0] - c[0]) ** 2 + (b[1] - c[1]) ** 2)
        if l0 < 1e-9 or l1 < 1e-9:
            continue
        d0 = ((c[0] - a[0]) / l0, (c[1] - a[1]) / l0)
        d1 = ((b[0] - c[0]) / l1, (b[1] - c[1]) / l1)
        if abs(d0[0] * d1[0] + d0[1] * d1[1]) <= 0.035:
            found.append((index, c, d0, d1))
    return found


def bend_radii(polylines, radius, reach):
    """One radius per vertex of every polyline, so that the bends of lines running side by side are
    CONCENTRIC: the innermost line of a group of parallel corners bends with `radius`, every line outside it
    with `radius` plus its distance from that inner line - the gap between the lines stays the same all
    round the bend, as tape laid side by side does.

    Two corners belong together when they turn the same way and one lies straight out from the other along
    the corner's diagonal (their bends share a centre), at most `reach` (feet) apart."""
    radii = [[radius] * len(points) for points in polylines]
    if radius <= 0:
        return radii
    corners = []
    for line, points in enumerate(polylines):
        for index, c, d0, d1 in _square_corners([(float(p[0]), float(p[1])) for p in points]):
            corners.append((line, index, c, d0, d1))
    for line, index, c, d0, d1 in corners:
        inner = 0.0                                   # how far out this corner lies from the innermost one
        for other_line, _i, c2, e0, e1 in corners:
            if other_line == line:
                continue
            if abs(e0[0] - d0[0]) > 1e-3 or abs(e0[1] - d0[1]) > 1e-3 or \
                    abs(e1[0] - d1[0]) > 1e-3 or abs(e1[1] - d1[1]) > 1e-3:
                continue                              # turns another way
            du, dv = c[0] - c2[0], c[1] - c2[1]
            out = du * d0[0] + dv * d0[1]             # how far this corner lies outside the other one
            if abs(out + (du * d1[0] + dv * d1[1])) > 1e-3 * max(1.0, abs(out)):
                continue                              # not on the same diagonal: the bends do not share a centre
            if 1e-6 < out <= reach and out > inner:
                inner = out
        radii[line][index] = radius + inner
    return radii


def is_orthogonal(points, tolerance=1e-6):
    for a, b in zip(points, points[1:]):
        if abs(a[0] - b[0]) > tolerance and abs(a[1] - b[1]) > tolerance:
            return False
    return True


def length_of(points):
    return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:]))


def bends_of(points):
    return max(0, len(points) - 2)


# ---------------------------------------------------------------------------
# one route, start to finish
# ---------------------------------------------------------------------------
class Route(object):
    def __init__(self):
        self.points = None          # [(u, v), ...] or None when it failed
        self.reason = ''
        self.length = 0.0
        self.bends = 0
        self.crossed = 0            # obstacle cells crossed next to the ends
        self.buffer = 0.0
        self.step = 0.0
        self.cells = 0
        self.joined = False         # ends on another panel's line (a tee)
        self.forced = False         # had to cross another line: there was no way beside them
        self.pieces = None          # what is drawn when the line is broken at crossings
        self.breaks = 0             # number of such breaks


def _nearest_on(segments, point):
    """(distance, point on the segment) for the closest of some
    horizontal / vertical segments."""
    best = None
    for a, b in segments:
        u = min(max(point[0], min(a[0], b[0])), max(a[0], b[0]))
        v = min(max(point[1], min(a[1], b[1])), max(a[1], b[1]))
        distance = abs(point[0] - u) + abs(point[1] - v)
        if best is None or distance < best[0]:
            best = (distance, (u, v))
    return best


def plan_route(start_point, end_point, walls, doors, others, clearance, step, buffers,
               turn_penalty=6.0, escape_distance=2.0, max_cells=1500000, join=None,
               beside=None, spacing=0.0, pull_axis=None, anchor=None, strict=False):
    """Route between two points.

    walls / others: obstacle shapes; doors: (cu, cv, du, dv, half_width,
    wall_half_thickness) openings cut out of the walls.
    buffers: how far (feet) the search region reaches beyond the two points;
    the next, larger one is tried when a route is not found.
    The grid covers only that region - never the whole building.

    join: [((u, v), (u, v)), ...] horizontal / vertical pieces of earthing
    lines that already run to the same earth bar. The route ends where it
    first meets one of them (a tee) - route.joined is then True - so shared
    stretches are drawn once instead of several lines on top of each other.

    beside + spacing: pieces of earlier lines this one must not lie on. Where
    it would run the same way it runs next to them, `spacing` feet apart
    (rounded up to whole grid cells).
    strict: True = it may not cross them either. Only when no such route
    exists it is routed again with crossing allowed; route.forced is then
    True."""
    route = Route()
    route.joined = False
    route.forced = False
    if strict and beside:
        attempt = plan_route(start_point, end_point, walls, doors, others, clearance, step, buffers,
                             turn_penalty, escape_distance, max_cells, join, beside, spacing,
                             pull_axis, anchor, strict=None)
        if attempt.points is not None:
            return attempt
        route.forced = True
    reason = 'every way is blocked'
    for buffer in buffers:
        u0 = min(start_point[0], end_point[0]) - buffer
        u1 = max(start_point[0], end_point[0]) + buffer
        v0 = min(start_point[1], end_point[1]) - buffer
        v1 = max(start_point[1], end_point[1]) + buffer
        used_step = step
        count = ((u1 - u0) / used_step + 1) * ((v1 - v0) / used_step + 1)
        if count > max_cells:
            # a coarser grid rather than a search that never ends
            used_step = step * sqrt(count / float(max_cells))
        reach = clearance + 4.0 * used_step
        # every route's grid sits on the same lattice, so the cells of one
        # route are also cells of the next
        # (anchor: a point the grid lines must pass through - a spot in front
        # of the earth bar, so that every such spot is a grid point)
        base_u, base_v = anchor if anchor is not None else (0.0, 0.0)
        u0 = base_u + floor((u0 - base_u) / used_step) * used_step
        v0 = base_v + floor((v0 - base_v) / used_step) * used_step
        grid = Grid(u0, v0, u1, v1, used_step)
        near_walls = [s for s in walls if shape_touches(s, u0, v0, u1, v1, reach)]
        if near_walls:
            grid.stamp(near_walls, clearance)
            for cu, cv, du, dv, half_width, half_wall in doors:
                grid.carve_box(cu, cv, du, dv, half_width - clearance - used_step / 2.0,
                               half_wall + clearance + 3.0 * used_step)
        near_others = [s for s in others if shape_touches(s, u0, v0, u1, v1, reach)]
        if near_others:
            if near_walls:
                second = Grid(u0, v0, u1, v1, used_step)
                second.stamp(near_others, clearance)
                grid.merge(second)
            else:
                grid.stamp(near_others, clearance)
        start, goal = grid.cell_of(*start_point), grid.cell_of(*end_point)
        targets = []
        for a, b in join or []:
            if max(a[0], b[0]) < u0 or min(a[0], b[0]) > u1 or max(a[1], b[1]) < v0 or min(a[1], b[1]) > v1:
                continue
            ia, ja = grid.cell_of(*a)
            ib, jb = grid.cell_of(*b)
            targets.append((min(ia, ib), min(ja, jb), max(ia, ib), max(ja, jb)))
        crowd = None
        if beside and spacing > 0:
            # cells nearer to an earlier line than the spacing
            # every cell closer than the spacing; an earlier line whose first or
            # last leg sits between two cell rows still gets its full distance
            keep_off = spacing - used_step * 0.01
            mask = Grid(u0, v0, u1, v1, used_step)
            for a, b in beside:
                mask.fill_rect(min(a[0], b[0]) - keep_off, min(a[1], b[1]) - keep_off,
                               max(a[0], b[0]) + keep_off, max(a[1], b[1]) + keep_off, 1)
            on_line = used_step * 0.51                # the cells the line itself runs through
            for a, b in beside:
                mask.fill_rect(min(a[0], b[0]) - on_line, min(a[1], b[1]) - on_line,
                               max(a[0], b[0]) + on_line, max(a[1], b[1]) + on_line, 2)
            # the line may start and end where it has to, whatever is there
            for point in (start_point, end_point):
                i, j = grid.cell_of(*point)
                mask.cells[j * mask.w + i] = 0
            crowd = mask.cells
        cells, info = find_path(grid, start, goal, turn_penalty,
                                escape=max(1, int(ceil(escape_distance / used_step))),
                                targets=targets or None, crowd=crowd, pull_axis=pull_axis,
                                walls=(strict is None))
        route.buffer, route.step, route.cells = buffer, used_step, grid.w * grid.h
        if cells is not None:
            finish_point = end_point
            if targets and cells[-1] != goal:
                # ended on an existing line: finish exactly ON it
                nearest = _nearest_on(join, grid.point_of(*cells[-1]))
                if nearest is not None and nearest[0] <= used_step * 1.5:
                    finish_point = nearest[1]
                    route.joined = True
            route.points = route_points(grid, cells, start_point, finish_point)
            route.length = length_of(route.points)
            route.bends = bends_of(route.points)
            route.crossed = info['hard']
            return route
        reason = info['reason']
    route.reason = reason
    return route
