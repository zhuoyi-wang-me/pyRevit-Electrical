# -*- coding: utf-8 -*-
"""Auto Dimension - which equipment forms a row, what a chain contains, which walls end it.

No Revit in this file: only numbers (feet). A "row" is equipment standing side by side,
turned the same way. Along a row runs one chain dimension:
    wall | equipment | gap | equipment | ... | wall
"""
import math

PARALLEL = math.cos(math.radians(5.0))    # directions within 5 degrees count as the same


class Part(object):
    """One piece of equipment in a row.
    s: position along the row, lat: position across it, z: height."""

    def __init__(self, key, smin, smax, lat_lo, lat_hi, zlo, zhi, front=0.0):
        self.key = key
        self.smin, self.smax, self.lat_lo, self.lat_hi, self.zlo, self.zhi = smin, smax, lat_lo, lat_hi, zlo, zhi
        self.front = front                # +1: faces towards larger lat, -1: towards smaller, 0: not known


class Row(object):
    def __init__(self, direction):
        self.direction = direction        # (dx, dy), unit
        self.parts = []

    @property
    def normal(self):
        return (-self.direction[1], self.direction[0])

    def extent(self):
        """(smin, smax, lat_lo, lat_hi, zlo, zhi) of the whole row."""
        p = self.parts
        return (min(i.smin for i in p), max(i.smax for i in p), min(i.lat_lo for i in p), max(i.lat_hi for i in p),
                min(i.zlo for i in p), max(i.zhi for i in p))

    def front(self):
        """+1 or -1: the side of the row the equipment faces (where the dimension line goes)."""
        total = sum(i.front for i in self.parts)
        return 1.0 if total >= 0 else -1.0


def canonical(dx, dy):
    """The same direction for a line and its reverse: pointing to +x (or +y when vertical). Unit length."""
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    dx, dy = dx / length, dy / length
    if dx < -1e-9 or (abs(dx) <= 1e-9 and dy < 0):
        dx, dy = -dx, -dy
    return (dx, dy)


def _part(key, direction, corners, zlo, zhi, facing):
    normal = (-direction[1], direction[0])
    along = [x * direction[0] + y * direction[1] for x, y in corners]
    across = [x * normal[0] + y * normal[1] for x, y in corners]
    front = 0.0
    if facing is not None:
        dot = facing[0] * normal[0] + facing[1] * normal[1]
        front = 1.0 if dot > 0.5 else (-1.0 if dot < -0.5 else 0.0)
    return Part(key, min(along), max(along), min(across), max(across), zlo, zhi, front)


def group_rows(raw, row_gap):
    """raw = [(key, (hx, hy) hand direction, [(x, y) corners of the footprint], zlo, zhi, (fx, fy) facing or None)].
    Returns [Row]: equipment with the same direction whose footprints are within row_gap of each other across the row."""
    clusters = []                         # [(direction, [raw item])]
    for item in raw:
        direction = canonical(item[1][0], item[1][1])
        if direction is None:
            continue
        for known, members in clusters:
            if abs(known[0] * direction[0] + known[1] * direction[1]) >= PARALLEL:
                members.append(item)
                break
        else:
            clusters.append((direction, [item]))
    rows = []
    for direction, members in clusters:
        parts = sorted((_part(m[0], direction, m[2], m[3], m[4], m[5]) for m in members), key=lambda p: p.lat_lo)
        row, reach = None, None
        for part in parts:
            if row is None or part.lat_lo > reach + row_gap:
                row = Row(direction)
                rows.append(row)
                reach = part.lat_hi
            row.parts.append(part)
            reach = max(reach, part.lat_hi)
    for row in rows:
        row.parts.sort(key=lambda p: (p.smin, p.smax))
    return rows


def one_row(raw, direction):
    """Every item in a single row along direction (a section: what stands side by side in the picture)."""
    # the direction is kept exactly as given (the right of the view): positions along the row are
    # then positions across the picture, whichever way the section looks
    length = math.hypot(direction[0], direction[1])
    direction = (direction[0] / length, direction[1] / length)
    row = Row(direction)
    row.parts = sorted((_part(m[0], direction, m[2], m[3], m[4], m[5]) for m in raw), key=lambda p: (p.smin, p.smax))
    return row


def chain(parts, tolerance=0.01):
    """(kept, hidden): the parts of a row that can be dimensioned one after the other, and those
    standing behind / in front of another one (they overlap along the row and would give nonsense)."""
    kept, hidden = [], []
    for part in sorted(parts, key=lambda p: (p.smin, p.smax)):
        if kept and part.smin < kept[-1].smax - tolerance:
            hidden.append(part)
        else:
            kept.append(part)
    return kept, hidden


class WallFace(object):
    """One side face of a wall that stands across the row."""

    def __init__(self, key, s, lat_lo, lat_hi, zlo, zhi):
        self.key, self.s, self.lat_lo, self.lat_hi, self.zlo, self.zhi = key, s, lat_lo, lat_hi, zlo, zhi


def pick_walls(faces, extent, reach, tolerance=0.02):
    """(face before the row or None, face after it or None).
    A face counts when the wall really stands at the end of the row: it crosses the row
    (across and in height) and is not farther than `reach` from it. The nearest face wins,
    which is the side of the wall that looks at the equipment."""
    smin, smax, lat_lo, lat_hi, zlo, zhi = extent
    before, after = None, None
    for face in faces:
        if face.lat_hi < lat_lo - tolerance or face.lat_lo > lat_hi + tolerance:
            continue                                        # the wall is beside the row, not at its end
        if face.zhi < zlo + tolerance or face.zlo > zhi - tolerance:
            continue                                        # another storey
        if face.s <= smin + tolerance and smin - face.s <= reach:
            if before is None or face.s > before.s:
                before = face
        elif face.s >= smax - tolerance and face.s - smax <= reach:
            if after is None or face.s < after.s:
                after = face
    return before, after


def height_groups(parts, tolerance=0.01):
    """[(representative part, [parts])]: equipment with the same bottom and top needs its height
    dimensioned only once - at the last one of them along the row."""
    groups = []
    for part in sorted(parts, key=lambda p: p.smin):
        for group in groups:
            first = group[0]
            if abs(first.zlo - part.zlo) <= tolerance and abs(first.zhi - part.zhi) <= tolerance:
                group.append(part)
                break
        else:
            groups.append([part])
    return [(group[-1], group) for group in groups]
