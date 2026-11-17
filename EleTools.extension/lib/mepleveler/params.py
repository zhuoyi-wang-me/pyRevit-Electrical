# -*- coding: utf-8 -*-
"""Discovery of the level and offset parameters for any MEP element.

Different element families expose their level association through completely
different parameters:

=========================== ================================= ===============================
Element                     Level parameter                   Offset parameter
=========================== ================================= ===============================
Duct / Pipe / Conduit /     RBS_START_LEVEL_PARAM             RBS_OFFSET_PARAM
Cable Tray (MEPCurve)       ("Reference Level")               ("Offset" / "Middle Elevation")
Sloped pipe / duct          RBS_START_LEVEL_PARAM             RBS_START_OFFSET_PARAM +
                                                              RBS_END_OFFSET_PARAM
Level based family instance FAMILY_LEVEL_PARAM ("Level")      INSTANCE_ELEVATION_PARAM
                                                              ("Elevation from Level")
Unhosted work plane based   SCHEDULE_LEVEL_PARAM              INSTANCE_FREE_HOST_OFFSET_PARAM
Face hosted device          INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM  (none - schedule only)
Fitting / accessory         RBS_START_LEVEL_PARAM             RBS_OFFSET_PARAM
=========================== ================================= ===============================

Rather than hard coding per category, the module probes an ordered list of
candidates and falls back to a name based search, which also picks up shared
parameters used by some manufacturers' content.
"""

from Autodesk.Revit import DB

from .revit_compat import get_bip_param, eid_value, is_valid_id


# ---------------------------------------------------------------------------
# Candidate parameter names
# ---------------------------------------------------------------------------

#: Ordered candidates for the *level* parameter that physically drives the
#: element. The first writable one found wins.
LEVEL_BIP_CANDIDATES = [
    'RBS_START_LEVEL_PARAM',          # Reference Level (all MEP curves/fittings)
    'FAMILY_LEVEL_PARAM',             # Level (level based family instances)
    'FAMILY_BASE_LEVEL_PARAM',        # Base Level
    'SCHEDULE_LEVEL_PARAM',           # Schedule Level (work plane based)
    'INSTANCE_REFERENCE_LEVEL_PARAM',  # Reference Level (some instances)
]

#: Level parameters that are purely informational: writing them re-tags the
#: element for schedules/filters without any chance of moving geometry.
SCHEDULE_ONLY_BIP_CANDIDATES = [
    'INSTANCE_SCHEDULE_ONLY_LEVEL_PARAM',  # Schedule Level (face hosted)
    'SCHEDULE_LEVEL_PARAM',
]

#: Ordered candidates for a single offset parameter.
OFFSET_BIP_CANDIDATES = [
    'RBS_OFFSET_PARAM',                   # Offset / Middle Elevation
    'INSTANCE_ELEVATION_PARAM',           # Elevation from Level
    'INSTANCE_FREE_HOST_OFFSET_PARAM',    # Offset from Host
    'FAMILY_BASE_LEVEL_OFFSET_PARAM',     # Base Offset
    'INSTANCE_SILL_HEIGHT_PARAM',         # Sill Height
]

#: Offset parameters that come in pairs (sloped MEP curves).
OFFSET_PAIR_BIP_CANDIDATES = [
    ('RBS_START_OFFSET_PARAM', 'RBS_END_OFFSET_PARAM'),
]

#: Name based fallbacks, matched case insensitively against the parameter
#: definition name. English and the most common localisations are included.
LEVEL_NAME_CANDIDATES = [
    'reference level', 'level', 'schedule level', 'base level',
    'start level', 'end level', 'base constraint',
    'referenzebene', 'ebene', 'niveau de reference', 'niveau', 'nivel',
]

OFFSET_NAME_CANDIDATES = [
    'offset', 'offset from level', 'elevation from level', 'middle elevation',
    'offset from host', 'base offset', 'height offset from level',
    'hoehenversatz', 'decalage', 'desplazamiento',
]

PAIRED_OFFSET_NAME_CANDIDATES = [
    ('start offset', 'end offset'),
    ('start middle elevation', 'end middle elevation'),
]


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

class ParamBinding(object):
    """The parameters used to re-level a single element."""

    #: The level parameter physically drives the element position.
    MODE_PHYSICAL = 'physical'
    #: The level parameter is schedule-only; geometry is never affected.
    MODE_SCHEDULE_ONLY = 'schedule'

    def __init__(self, level_param=None, offset_params=None,
                 mode=MODE_PHYSICAL, level_param_name=u'',
                 offset_param_name=u''):
        self.level_param = level_param
        self.offset_params = offset_params or []
        self.mode = mode
        self.level_param_name = level_param_name
        self.offset_param_name = offset_param_name

    @property
    def has_level(self):
        return self.level_param is not None

    @property
    def can_compensate(self):
        return bool(self.offset_params)

    def current_level_id(self):
        if self.level_param is None:
            return None
        try:
            return self.level_param.AsElementId()
        except Exception:
            return None

    def current_offset(self):
        """Primary offset value in internal units, or None."""
        if not self.offset_params:
            return None
        try:
            return self.offset_params[0].AsDouble()
        except Exception:
            return None

    def offset_values(self):
        values = []
        for param in self.offset_params:
            try:
                values.append(param.AsDouble())
            except Exception:
                values.append(0.0)
        return values

    def __repr__(self):
        return '<ParamBinding %s level=%s offsets=%d>' % (
            self.mode, self.level_param_name, len(self.offset_params))


# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------

def _writable(param):
    """True when a parameter exists and can be written to."""
    if param is None:
        return False
    try:
        return not param.IsReadOnly
    except Exception:
        return False


def _param_name(param):
    try:
        return param.Definition.Name
    except Exception:
        return u''


def param_name(param):
    """Public alias of the parameter display name helper."""
    return _param_name(param)


def _is_level_param(doc, param):
    """True when the parameter stores an ElementId that is (or could be) a Level."""
    try:
        if param.StorageType != DB.StorageType.ElementId:
            return False
    except Exception:
        return False
    try:
        value = param.AsElementId()
    except Exception:
        return False
    if not is_valid_id(value):
        # Empty level parameters are still valid targets.
        return True
    element = doc.GetElement(value)
    return isinstance(element, DB.Level)


def _find_named_param(element, names, numeric=False, doc=None):
    """Find the first writable parameter whose name matches *names*."""
    wanted = [n.lower() for n in names]
    try:
        params = element.Parameters
    except Exception:
        return None
    best = None
    best_rank = None
    for param in params:
        if not _writable(param):
            continue
        name = _param_name(param).strip().lower()
        if name not in wanted:
            continue
        if numeric:
            try:
                if param.StorageType != DB.StorageType.Double:
                    continue
            except Exception:
                continue
        else:
            if doc is not None and not _is_level_param(doc, param):
                continue
        rank = wanted.index(name)
        if best_rank is None or rank < best_rank:
            best = param
            best_rank = rank
    return best


# ---------------------------------------------------------------------------
# Public discovery API
# ---------------------------------------------------------------------------

def find_offset_params(element):
    """Return the writable offset parameter(s) that shift the element in Z.

    A list is returned because sloped MEP curves need both the start and end
    offsets to be shifted by the same amount.
    """
    # 1. Single, primary offset parameter.
    for name in OFFSET_BIP_CANDIDATES:
        param = get_bip_param(element, name)
        if _writable(param):
            return [param], _param_name(param)

    # 2. Start/End pair (sloped pipes and ducts).
    for start_name, end_name in OFFSET_PAIR_BIP_CANDIDATES:
        start = get_bip_param(element, start_name)
        end = get_bip_param(element, end_name)
        if _writable(start) and _writable(end):
            return [start, end], u'%s + %s' % (_param_name(start),
                                               _param_name(end))

    # 3. Name based single offset.
    param = _find_named_param(element, OFFSET_NAME_CANDIDATES, numeric=True)
    if param is not None:
        return [param], _param_name(param)

    # 4. Name based pair.
    for start_name, end_name in PAIRED_OFFSET_NAME_CANDIDATES:
        start = _find_named_param(element, [start_name], numeric=True)
        end = _find_named_param(element, [end_name], numeric=True)
        if start is not None and end is not None:
            return [start, end], u'%s + %s' % (_param_name(start),
                                               _param_name(end))

    return [], u''


def find_level_param(doc, element, allow_schedule_only=True):
    """Return a :class:`ParamBinding` describing how to re-level *element*.

    The physical level parameter is preferred; when it does not exist or is
    read-only (typical for face hosted devices, elements inside groups that
    cannot be edited in place, and some nested components) the schedule-only
    level parameter is used instead, which re-tags the element for schedules,
    filters and IFC export without any possibility of moving it.
    """
    offset_params, offset_name = find_offset_params(element)

    # -- 1. Physical level parameter -------------------------------------
    for name in LEVEL_BIP_CANDIDATES:
        param = get_bip_param(element, name)
        if _writable(param) and _is_level_param(doc, param):
            return ParamBinding(level_param=param,
                                offset_params=offset_params,
                                mode=ParamBinding.MODE_PHYSICAL,
                                level_param_name=_param_name(param),
                                offset_param_name=offset_name)

    # -- 2. Name based physical level parameter --------------------------
    param = _find_named_param(element, LEVEL_NAME_CANDIDATES, doc=doc)
    if param is not None:
        schedule_only = _param_name(param).strip().lower() == 'schedule level'
        return ParamBinding(
            level_param=param,
            offset_params=[] if schedule_only else offset_params,
            mode=(ParamBinding.MODE_SCHEDULE_ONLY if schedule_only
                  else ParamBinding.MODE_PHYSICAL),
            level_param_name=_param_name(param),
            offset_param_name=u'' if schedule_only else offset_name)

    # -- 3. Schedule only fallback ---------------------------------------
    if allow_schedule_only:
        for name in SCHEDULE_ONLY_BIP_CANDIDATES:
            param = get_bip_param(element, name)
            if _writable(param) and _is_level_param(doc, param):
                return ParamBinding(level_param=param,
                                    offset_params=[],
                                    mode=ParamBinding.MODE_SCHEDULE_ONLY,
                                    level_param_name=_param_name(param),
                                    offset_param_name=u'')

    # -- 4. Nothing usable ------------------------------------------------
    # Report the read-only level parameter (if any) so the user gets a
    # meaningful message instead of a generic "not supported".
    for name in LEVEL_BIP_CANDIDATES + SCHEDULE_ONLY_BIP_CANDIDATES:
        param = get_bip_param(element, name)
        if param is not None:
            return ParamBinding(level_param=None,
                                offset_params=offset_params,
                                mode=ParamBinding.MODE_PHYSICAL,
                                level_param_name=_param_name(param),
                                offset_param_name=offset_name)

    return ParamBinding(offset_params=offset_params,
                        offset_param_name=offset_name)


def read_only_reason(doc, element):
    """Human readable explanation of why an element cannot be re-levelled."""
    for name in LEVEL_BIP_CANDIDATES + SCHEDULE_ONLY_BIP_CANDIDATES:
        param = get_bip_param(element, name)
        if param is not None:
            try:
                if param.IsReadOnly:
                    return u'Level parameter "%s" is read-only' % _param_name(param)
            except Exception:
                pass
    return u'No level parameter available for this element type'


def current_level_id(doc, element):
    """Best guess at the level the element is currently associated with."""
    binding = find_level_param(doc, element)
    level_id = binding.current_level_id()
    if is_valid_id(level_id):
        return level_id
    # Fall back to the element's own LevelId property (Revit 2022+).
    try:
        if is_valid_id(element.LevelId):
            return element.LevelId
    except Exception:
        pass
    return None


def offset_summary(binding):
    """Compact string of the current offset values, for reports."""
    values = binding.offset_values()
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return values[0]


def eid_or_none(element_id):
    return eid_value(element_id) if element_id is not None else None
