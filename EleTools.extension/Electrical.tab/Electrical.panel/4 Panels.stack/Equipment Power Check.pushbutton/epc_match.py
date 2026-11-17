# -*- coding: utf-8 -*-
"""Equipment Power Check - matching (pure Python, no Revit API, unit-tested).

Every equipment item is checked against the electrical fixtures:
  1. TAG      - a fixture whose tag parameter (e.g. MEE_ToServe) equals the
                equipment's tag parameter (spaces / case ignored)
  2. NEARBY   - a fixture within the plan distance and height difference
Coordinates are Revit internal feet, in HOST coordinates (links transformed).
"""
import math

STATUS_TAG = u'OK - tag match'
STATUS_NEAR = u'OK - fixture nearby'
STATUS_MISSING = u'MISSING'


def norm(text):
    """'  ahu-01 ' -> 'AHU-01' ; None -> ''"""
    return u' '.join(u'{0}'.format(text or u'').strip().upper().split())


class Item(object):
    """An equipment item or an electrical fixture."""

    def __init__(self, key, label, source, point, tag=u'', level=u'', has_connector=None,
                 element_id=None, tag_from=u''):
        self.key = key                    # unique: (link instance id or 0, element id)
        self.label = label                # 'Family : Type [id]'
        self.source = source              # model name
        self.point = point                # (x, y, z) feet, host coordinates
        self.tag = tag or u''
        self.tag_from = tag_from          # '' = the tag parameter, 'type name' = fallback
        self.level = level or u''
        self.has_connector = has_connector
        self.element_id = element_id      # host element id (clickable) or None for linked


class Result(object):
    def __init__(self, equipment):
        self.equipment = equipment
        self.status = STATUS_MISSING
        self.tag_fixtures = []            # fixtures with the same tag
        self.nearest = None               # nearest fixture within the limits
        self.distance = None              # plan distance to it (feet)
        self.note = u''


def plan_distance(a, b):
    if a is None or b is None:
        return float('inf')
    return math.hypot(a[0] - b[0], a[1] - b[1])


def words(text):
    """'plinth, Chamber ,,' -> ['plinth', 'chamber']"""
    return [w.strip().lower() for w in u'{0}'.format(text or u'').split(u',') if w.strip()]


def name_excluded(label, exclude_words):
    """True when the family/type label contains one of the exclude words."""
    low = (label or u'').lower()
    return any(w in low for w in exclude_words or [])


def tag_or_type(tag_value, type_name, use_fallback):
    """(tag, where it came from): the parameter value, else the type name."""
    if (tag_value or u'').strip():
        return tag_value.strip(), u''
    if use_fallback and (type_name or u'').strip():
        return type_name.strip(), u'type name'
    return u'', u''


def drop_ignored(fixtures, ignore_tags):
    """(kept fixtures, number dropped): fixtures whose tag is in ignore_tags
    (e.g. GENERAL sockets) take no part in the check at all."""
    ignore = set(norm(t) for t in ignore_tags or [] if norm(t))
    if not ignore:
        return list(fixtures), 0
    kept = [f for f in fixtures if norm(f.tag) not in ignore]
    return kept, len(fixtures) - len(kept)


class _Grid(object):
    def __init__(self, items, cell):
        self.cell = max(cell, 1.0)
        self.cells = {}
        for it in items:
            self.cells.setdefault(self._key(it.point), []).append(it)

    def _key(self, p):
        return int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell))

    def near(self, p):
        cx, cy = self._key(p)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for it in self.cells.get((cx + dx, cy + dy), ()):
                    yield it


def check(equipment, fixtures, use_tag=True, use_distance=True, max_plan_ft=10.0, max_height_ft=13.0):
    """Returns (results, fixtures whose tag matches no equipment)."""
    by_tag = {}
    for f in fixtures:
        t = norm(f.tag)
        if t:
            by_tag.setdefault(t, []).append(f)
    grid = _Grid([f for f in fixtures if f.point is not None], max_plan_ft)
    used_tags = set()

    # Tag matching. When several units share a tag (same Mark, or the same
    # type name used as tag), each fixture with that tag counts only for the
    # NEAREST of those units - one isolator tagged FCU-14A must not make ten
    # FCU-14A units OK.
    assigned, shared = {}, {}
    if use_tag:
        eq_by_tag = {}
        for eq in equipment:
            t = norm(eq.tag)
            if t:
                eq_by_tag.setdefault(t, []).append(eq)
        for t, units in eq_by_tag.items():
            fxs = by_tag.get(t, [])
            if not fxs:
                continue
            used_tags.add(t)
            if len(units) == 1:
                assigned[units[0].key] = list(fxs)
                continue
            shared[t] = (len(units), len(fxs))
            for f in fxs:
                best = min(units, key=lambda u: plan_distance(u.point, f.point))
                assigned.setdefault(best.key, []).append(f)

    results = []
    for eq in equipment:
        res = Result(eq)
        tag = norm(eq.tag)
        if use_tag and tag:
            res.tag_fixtures = assigned.get(eq.key, [])
        if use_distance and eq.point is not None:
            best = None
            for f in grid.near(eq.point):
                d = plan_distance(eq.point, f.point)
                if d > max_plan_ft or abs(f.point[2] - eq.point[2]) > max_height_ft:
                    continue
                if best is None or d < best[0]:
                    best = (d, f)
            if best is not None:
                res.distance, res.nearest = best
        short = u''
        if use_tag and tag in shared and not res.tag_fixtures:
            short = u"tag '{0}' is on {1} units but only {2} fixture(s) carry it".format(
                eq.tag, shared[tag][0], shared[tag][1])
        if res.tag_fixtures:
            res.status = STATUS_TAG
        elif res.nearest is not None:
            res.status = STATUS_NEAR
            ftag = norm(res.nearest.tag)
            if short:
                res.note = short
            elif use_tag and tag and ftag and ftag != tag:
                res.note = u"nearest fixture is tagged '{0}', equipment is '{1}'".format(
                    res.nearest.tag, eq.tag)
            elif use_tag and tag and not ftag:
                res.note = u'nearest fixture has no tag value'
        else:
            if short:
                res.note = short
            elif use_tag and not tag:
                res.note = u'equipment has no tag value'
        results.append(res)
    unused = [f for f in fixtures if norm(f.tag) and norm(f.tag) not in used_tags]
    order = {STATUS_MISSING: 0, STATUS_NEAR: 1, STATUS_TAG: 2}
    results.sort(key=lambda r: (order[r.status], r.equipment.level, r.equipment.label))
    return results, unused
