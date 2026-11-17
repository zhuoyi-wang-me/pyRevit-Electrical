# -*- coding: utf-8 -*-
"""
Pure-Python 2D geometry: region containment and spatial ordering.

Filled-region boundaries are tessellated to (x, y) polylines by the collector;
everything here works on plain tuples so it can be unit-tested outside Revit.
"""
from __future__ import division

import math

GRID_ORDER_THRESHOLD = 400   # above this many items use O(n log n) serpentine ordering
GRID_CELL_FT = 10.0          # ~3 m bands for serpentine ordering


def dist2d(a, b):
    if a is None or b is None:
        return float('inf')
    return math.hypot(a[0] - b[0], a[1] - b[1])


def polygon_area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(x, y, poly):
    """Ray casting (even-odd). `poly` is a list of (x, y), not closed."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def clean_loop(points, tol=1e-6):
    out = []
    for p in points:
        p = (float(p[0]), float(p[1]))
        if out and abs(out[-1][0] - p[0]) < tol and abs(out[-1][1] - p[1]) < tol:
            continue
        out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) < tol and abs(out[0][1] - out[-1][1]) < tol:
        out.pop()
    return out


class Region(object):
    """Electrical distribution zone (one filled region; may contain holes)."""

    def __init__(self, name, loops, level_id=None, level_name='', source_id=None):
        self.name = name
        self.loops = [l for l in (clean_loop(lp) for lp in loops) if len(l) >= 3]
        self.level_id = level_id
        self.level_name = level_name
        self.source_id = source_id
        xs = [p[0] for l in self.loops for p in l]
        ys = [p[1] for l in self.loops for p in l]
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
        areas = sorted((polygon_area(l) for l in self.loops), reverse=True)
        self.area = (areas[0] - sum(areas[1:])) if areas else 0.0

    def contains(self, x, y):
        b = self.bbox
        if b is None or x < b[0] or x > b[2] or y < b[1] or y > b[3]:
            return False
        hits = sum(1 for loop in self.loops if point_in_polygon(x, y, loop))
        return hits % 2 == 1   # even-odd across all loops handles holes


class RegionIndex(object):
    """Locates the region for a point. Smallest region wins (nested zones)."""

    def __init__(self, regions, level_filter=True):
        self.regions = sorted(regions, key=lambda r: r.area)
        self.level_filter = level_filter

    def locate(self, x, y, level_id=None):
        for r in self.regions:
            if (self.level_filter and r.level_id is not None and level_id is not None
                    and r.level_id != level_id):
                continue
            if r.contains(x, y):
                return r.name
        return None

    def outline(self):
        """(minx, miny, maxx, maxy) of all regions, or None - used as a quick pre-filter."""
        boxes = [r.bbox for r in self.regions if r.bbox is not None]
        if not boxes:
            return None
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))

    def names(self):
        seen, out = set(), []
        for r in self.regions:
            if r.name not in seen:
                seen.add(r.name)
                out.append(r.name)
        return sorted(out)


def _grid_order(items, get_point):
    """Serpentine (boustrophedon) ordering in horizontal bands - O(n log n)."""
    def key(item):
        x, y = get_point(item)[0], get_point(item)[1]
        band = int(math.floor(y / GRID_CELL_FT))
        return (band, x if band % 2 == 0 else -x)
    return sorted(items, key=key)


def nearest_neighbor_order(items, get_point):
    """
    Greedy nearest-neighbour chain so consecutive devices are physically close:
    circuits cut from this sequence form compact clusters (short home runs).
    """
    remaining = list(items)
    if len(remaining) < 3:
        return remaining
    if len(remaining) > GRID_ORDER_THRESHOLD:
        return _grid_order(remaining, get_point)
    remaining.sort(key=lambda i: (get_point(i)[0], get_point(i)[1]))
    ordered = [remaining.pop(0)]
    while remaining:
        last = get_point(ordered[-1])
        idx = min(range(len(remaining)), key=lambda k: dist2d(last, get_point(remaining[k])))
        ordered.append(remaining.pop(idx))
    return ordered
