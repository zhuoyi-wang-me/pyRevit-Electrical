# -*- coding: utf-8 -*-
"""Project level cache and level resolution strategies.

``Level.ProjectElevation`` is used everywhere rather than ``Level.Elevation``
because it is expressed in the same coordinate system as ``XYZ.Z`` regardless
of the level's "Elevation Base" setting (Project Base Point vs Survey Point).
Offsets are deltas, so using ProjectElevation consistently on both sides of the
comparison keeps the arithmetic correct.
"""

from Autodesk.Revit import DB

from .revit_compat import eid_value


class LevelInfo(object):
    """Lightweight wrapper around a Revit Level."""

    __slots__ = ('element', 'id', 'id_value', 'name', 'elevation')

    def __init__(self, level):
        self.element = level
        self.id = level.Id
        self.id_value = eid_value(level.Id)
        try:
            self.name = level.Name
        except Exception:
            self.name = DB.Element.Name.GetValue(level)
        self.elevation = _project_elevation(level)

    def __repr__(self):
        return '<LevelInfo %s @ %.4f>' % (self.name, self.elevation)


def _project_elevation(level):
    """Elevation of *level* in project internal coordinates."""
    try:
        return level.ProjectElevation
    except Exception:
        return level.Elevation


class LevelResolver(object):
    """Caches the project levels and maps elevations onto them.

    ``allowed_names`` is the explicit list of target levels chosen by the user.
    When it is empty every level is a candidate; otherwise only the named
    levels can ever be assigned to an element, which is how non-storey levels
    (T.O.S., ceiling references, datum planes, levels imported from links) are
    kept out of the result.

    Excluded levels remain in the cache so that an element's *current* level
    can still be reported by name even when it is not a valid target.
    """

    def __init__(self, doc, strategy='nearest', excluded_keywords=None,
                 snap_tolerance=1.0, allowed_names=None):
        self.doc = doc
        self.strategy = strategy
        self.snap_tolerance = snap_tolerance
        self.excluded_keywords = [k.strip().lower()
                                  for k in (excluded_keywords or [])
                                  if k and k.strip()]
        self.allowed_names = set(n.strip() for n in (allowed_names or [])
                                 if n and n.strip())

        self._all = []       # every level in the document
        self._valid = []     # levels usable as a target, sorted by elevation
        self._by_id = {}
        self._load()

    # -- construction ------------------------------------------------------
    def _load(self):
        collector = DB.FilteredElementCollector(self.doc) \
            .OfClass(DB.Level) \
            .WhereElementIsNotElementType()

        for level in collector:
            info = LevelInfo(level)
            self._all.append(info)
            self._by_id[info.id_value] = info

        self._all.sort(key=lambda lv: lv.elevation)
        self._valid = [lv for lv in self._all if self.is_allowed(lv)]

    def is_allowed(self, level_info):
        """True when *level_info* may be assigned to an element."""
        if self._is_excluded(level_info):
            return False
        if self.allowed_names:
            return (level_info.name or u'').strip() in self.allowed_names
        return True

    def _is_excluded(self, level_info):
        if not self.excluded_keywords:
            return False
        name = (level_info.name or '').lower()
        return any(keyword in name for keyword in self.excluded_keywords)

    # -- public API --------------------------------------------------------
    @property
    def levels(self):
        """All valid target levels, sorted from bottom to top."""
        return list(self._valid)

    @property
    def all_levels(self):
        return list(self._all)

    @property
    def excluded_levels(self):
        """Levels present in the model but not usable as a target."""
        return [lv for lv in self._all if not self.is_allowed(lv)]

    def by_id(self, element_id):
        """LevelInfo for an ElementId (valid or excluded), or None."""
        if element_id is None:
            return None
        return self._by_id.get(eid_value(element_id))

    def resolve(self, elevation):
        """Return the LevelInfo that *elevation* belongs to, or None."""
        if elevation is None or not self._valid:
            return None

        if self.strategy == 'below':
            return self._resolve_below(elevation, 0.0)
        if self.strategy == 'below_snap':
            return self._resolve_below(elevation, self.snap_tolerance)
        return self._resolve_nearest(elevation)

    # -- strategies --------------------------------------------------------
    def _resolve_nearest(self, elevation):
        best = None
        best_distance = None
        for level in self._valid:
            distance = abs(level.elevation - elevation)
            # Strict "<" keeps the lower level when two are equidistant, which
            # matches the way schedules are normally expected to read.
            if best_distance is None or distance < best_distance:
                best = level
                best_distance = distance
        return best

    def _resolve_below(self, elevation, snap):
        # Highest level whose elevation is at or below (elevation + snap).
        threshold = elevation + snap
        candidate = None
        for level in self._valid:
            if level.elevation <= threshold:
                candidate = level
            else:
                break
        if candidate is None:
            # Element sits below the lowest level: use the lowest one.
            candidate = self._valid[0]
        return candidate

    # -- diagnostics -------------------------------------------------------
    def distance_to(self, level_info, elevation):
        if level_info is None or elevation is None:
            return None
        return elevation - level_info.elevation
