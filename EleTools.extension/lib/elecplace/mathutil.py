# -*- coding: utf-8 -*-
"""
elecplace.mathutil
==================
Pure-Python helpers with NO Revit dependency, so they can be unit-tested
outside Revit (CPython 3 or IronPython 2.7).

  * subtract_intervals()  - remove exclusion zones (doors, windows) from a
                            usable stretch of wall.
  * distribute()          - spread N fixtures evenly over many pieces.
  * nudge_offsets()       - alternative positions tried when a slot clashes.
  * GridIndex             - hashed 3D grid for fast "is there already a
                            fixture here?" duplicate checks.
"""
import math


def subtract_intervals(base, exclusions, min_piece):
    """
    Subtract exclusion intervals from a base interval.

    base        -- (a, b) usable range along a wall segment (feet)
    exclusions  -- iterable of (start, end) ranges to remove
    min_piece   -- pieces shorter than this are discarded
    returns     -- sorted list of (a, b) pieces
    """
    a, b = base
    if b - a < min_piece:
        return []
    pieces = [(a, b)]
    for ex_a, ex_b in sorted(exclusions):
        if ex_b <= ex_a:
            continue
        nxt = []
        for p_a, p_b in pieces:
            if ex_b <= p_a or ex_a >= p_b:          # no overlap
                nxt.append((p_a, p_b))
                continue
            if ex_a > p_a:                            # left remainder
                nxt.append((p_a, ex_a))
            if ex_b < p_b:                            # right remainder
                nxt.append((ex_b, p_b))
        pieces = nxt
    return [(p_a, p_b) for p_a, p_b in pieces if p_b - p_a >= min_piece]


def distribute(pieces, count):
    """
    Evenly distribute `count` points over a chain of pieces.

    pieces -- list of (payload, a, b); payload is returned untouched
    count  -- number of points required
    returns list of (payload, a, b, t) where a <= t <= b

    The pieces are treated as one continuous "unrolled" perimeter of total
    length T. Point i is placed at T * (i + 0.5) / count, i.e. at the centre
    of N equal bays, which keeps fixtures away from piece ends and spreads
    them round the room rather than bunching them on the first wall.
    """
    pieces = [p for p in pieces if p[2] - p[1] > 0]
    total = sum(b - a for _, a, b in pieces)
    if count <= 0 or total <= 0:
        return []
    slots = []
    for i in range(count):
        target = total * (i + 0.5) / float(count)
        acc = 0.0
        for idx, (payload, a, b) in enumerate(pieces):
            span = b - a
            if target <= acc + span or idx == len(pieces) - 1:
                t = a + min(max(target - acc, 0.0), span)
                slots.append((payload, a, b, t))
                break
            acc += span
    return slots


def nudge_offsets(step, tries=3, prefer=1.0):
    """
    0, +s, -s, +2s, -2s ... used to slide a slot off a clash / duplicate.
    prefer = -1 tries the negative direction first at each step (e.g. keep
    moving AWAY from the unit the fixture serves).
    """
    sign = -1.0 if prefer < 0 else 1.0
    out = [0.0]
    for k in range(1, tries + 1):
        out.extend([sign * k * step, -sign * k * step])
    return out


class GridIndex(object):
    """
    Tiny spatial hash for 3D points (host model coordinates, feet).
    near() is O(1) on average instead of O(n) over every fixture.
    """

    def __init__(self, cell_size):
        self.cell = max(float(cell_size), 1e-3)
        self.grid = {}
        self.count = 0

    def _key(self, x, y, z):
        c = self.cell
        return (int(math.floor(x / c)), int(math.floor(y / c)), int(math.floor(z / c)))

    def add(self, x, y, z):
        self.grid.setdefault(self._key(x, y, z), []).append((x, y, z))
        self.count += 1

    def near(self, x, y, z, tol):
        """True if any stored point lies within `tol` of (x, y, z)."""
        kx, ky, kz = self._key(x, y, z)
        reach = int(math.ceil(tol / self.cell))
        tol2 = tol * tol
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for dz in range(-reach, reach + 1):
                    for (px, py, pz) in self.grid.get((kx + dx, ky + dy, kz + dz), ()):
                        if (px - x) ** 2 + (py - y) ** 2 + (pz - z) ** 2 <= tol2:
                            return True
        return False


# --------------------------------------------------------------------------
# Room-size driven quantities
# --------------------------------------------------------------------------
def parse_bands(text):
    """
    Parse area bands such as "10:2, 25:4, 50:6, *:8".

    Each pair is  upper_area_limit:count  in ascending order; '*' (or '>')
    is the count for anything larger. Returns [(limit or None, count)].
    Raises ValueError with a readable message on bad input.
    """
    bands = []
    for raw in (text or "").replace(";", ",").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if ":" not in raw:
            raise ValueError("'{}' must look like area:count".format(raw))
        lim_txt, cnt_txt = [p.strip() for p in raw.split(":", 1)]
        count = int(cnt_txt)
        if count < 0:
            raise ValueError("count cannot be negative in '{}'".format(raw))
        if lim_txt in ("*", ">", ""):
            bands.append((None, count))
        else:
            bands.append((float(lim_txt.replace(",", ".")), count))
    if not bands:
        raise ValueError("enter at least one band, e.g. 10:2, 25:4, *:6")
    limits = [b[0] for b in bands if b[0] is not None]
    if limits != sorted(limits):
        raise ValueError("area limits must be in ascending order")
    if any(b[0] is None for b in bands[:-1]):
        raise ValueError("'*' band must be the last one")
    return bands


def band_count(bands, value):
    """Count for `value` from bands [(limit or None, count)]."""
    for limit, count in bands:
        if limit is None or value <= limit:
            return count
    return bands[-1][1] if bands else 0


def clamp_qty(n, min_qty=0, max_qty=0):
    """Apply optional min / max (0 = no limit)."""
    n = max(int(n), 0)
    if min_qty and n < min_qty:
        n = min_qty
    if max_qty and n > max_qty:
        n = max_qty
    return n


def count_by_spacing(length, spacing):
    """One item per `spacing` of length, rounded up (10 / 3.6 -> 3)."""
    if spacing <= 0 or length <= 0:
        return 0
    return int(math.ceil(length / spacing - 1e-9))


def nearest_on_intervals(px, py, sx, sy, dx, dy, intervals):
    """
    Closest point to (px, py) on a segment line start (sx, sy) with unit
    direction (dx, dy), restricted to usable intervals [(a, b)].
    Returns (t, distance, a, b) or None.
    """
    t_raw = (px - sx) * dx + (py - sy) * dy
    best = None
    for a, b in intervals:
        t = min(max(t_raw, a), b)
        qx, qy = sx + dx * t, sy + dy * t
        dist = math.hypot(px - qx, py - qy)
        if best is None or dist < best[1]:
            best = (t, dist, a, b)
    return best



# --------------------------------------------------------------------------
# Oriented bounding boxes (clash detection)
# --------------------------------------------------------------------------
def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def obb_overlap(c1, axes1, half1, c2, axes2, half2, eps=1e-9):
    """
    Separating-axis test for two oriented boxes.

    c*     centre (x, y, z)
    axes*  three orthonormal unit axes [(x, y, z), ...]
    half*  half sizes along those axes
    Returns True when the boxes overlap (touching counts as clear).
    """
    t = (c2[0] - c1[0], c2[1] - c1[1], c2[2] - c1[2])
    cands = list(axes1) + list(axes2)
    for a in axes1:
        for b in axes2:
            cr = _cross(a, b)
            if _dot(cr, cr) > eps:
                cands.append(cr)
    for ax in cands:
        n = math.sqrt(_dot(ax, ax))
        if n < eps:
            continue
        ax = (ax[0] / n, ax[1] / n, ax[2] / n)
        r1 = sum(h * abs(_dot(a, ax)) for a, h in zip(axes1, half1))
        r2 = sum(h * abs(_dot(b, ax)) for b, h in zip(axes2, half2))
        if abs(_dot(t, ax)) >= r1 + r2 - eps:
            return False                      # separating axis found
    return True


def obb_aabb(c, axes, half):
    """Axis-aligned bounds (lo, hi) of an oriented box."""
    ext = [sum(h * abs(a[i]) for a, h in zip(axes, half)) for i in range(3)]
    return ((c[0] - ext[0], c[1] - ext[1], c[2] - ext[2]),
            (c[0] + ext[0], c[1] + ext[1], c[2] + ext[2]))
