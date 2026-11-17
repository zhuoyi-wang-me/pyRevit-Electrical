# -*- coding: utf-8 -*-
"""Shared helpers for the Circuit Breaker Tools extension.
Placed in /lib so pyRevit automatically adds it to sys.path and both
pushbuttons (Auto Number, Transfer Parameter) can import it."""

from Autodesk.Revit.DB import (
    StorageType, BuiltInParameter, IndependentTag, TextNote, Dimension,
    Grid, Level, SpatialElementTag, LocationPoint, LocationCurve, CurveElement
)

ANNOTATION_TYPES = (
    IndependentTag, TextNote, Dimension, Grid, Level, SpatialElementTag,
    CurveElement  # detail lines, model lines, symbolic lines
)


def is_tag_or_annotation(element):
    """Return True for tags / text / dimensions / grids / levels / lines
    so they never get swept into a selection, even if caught in a box
    select. Covers detail lines, model lines, and any other line-like
    element that isn't the actual equipment/cable being numbered or
    transferred."""
    try:
        if isinstance(element, ANNOTATION_TYPES):
            return True
    except Exception:
        pass

    try:
        cat = element.Category
        if cat is not None and cat.Name:
            name_lower = cat.Name.lower()
            if 'tag' in name_lower or 'line' in name_lower or 'wire' in name_lower:
                return True
    except Exception:
        pass

    return False


def get_writable_string_param_names(element):
    """Names of instance parameters on `element` that are text-like and
    not read-only, used to populate parameter combo boxes intelligently."""
    names = []
    try:
        for p in element.Parameters:
            try:
                if p.IsReadOnly:
                    continue
                if p.StorageType != StorageType.String:
                    continue
                if p.Definition is None:
                    continue
                names.append(p.Definition.Name)
            except Exception:
                continue
    except Exception:
        pass
    return names


def get_string_param_value(element, param_name):
    """Returns the string value, '' if the parameter exists but is empty,
    or None if the parameter does not exist at all on this element (so
    callers can tell 'blank' apart from 'not found' - important once
    elements come from several different families)."""
    if not param_name:
        return None
    p = element.LookupParameter(param_name)
    if p is None:
        return None
    if p.StorageType != StorageType.String:
        return None
    return p.AsString() or ''


def get_string_param_value_multi(element, param_names):
    """Tries each parameter name in `param_names` (in order) on `element`
    and returns the first one that exists AND has a non-empty value.
    Returns (value, matched_param_name) or (None, None) if none matched.
    This lets one Source Parameter field work across mixed families that
    store the same kind of tag under different parameter names."""
    for name in param_names:
        name = name.strip()
        if not name:
            continue
        value = get_string_param_value(element, name)
        if value:
            return value, name
    return None, None


def parse_param_name_list(text):
    """Splits a comma-separated 'Source Parameter' field like
    'MEE-Component, MEE-EquipmentTag, Mark' into a clean list."""
    if not text:
        return []
    return [p.strip() for p in text.split(',') if p.strip()]


def get_element_position(element, view):
    """Best-effort 2D position for an element, tried in order of
    reliability:
      1. Bounding box midpoint in the given view (works for most
         detail items / family instances shown on the diagram).
      2. Bounding box midpoint with no view (model-space bbox).
      3. LocationPoint / LocationCurve midpoint.
    Returns (x, y) or None if no position could be determined at all -
    callers should treat None as 'exclude from position-based matching'
    rather than silently defaulting to (0, 0), which used to cause
    unrelated elements to bunch together and pair up wrongly.
    """
    try:
        bbox = element.get_BoundingBox(view)
        if bbox:
            return (bbox.Min.X + bbox.Max.X) / 2.0, (bbox.Min.Y + bbox.Max.Y) / 2.0
    except Exception:
        pass

    try:
        bbox = element.get_BoundingBox(None)
        if bbox:
            return (bbox.Min.X + bbox.Max.X) / 2.0, (bbox.Min.Y + bbox.Max.Y) / 2.0
    except Exception:
        pass

    try:
        loc = element.Location
        if isinstance(loc, LocationPoint) and loc.Point:
            return loc.Point.X, loc.Point.Y
        if isinstance(loc, LocationCurve) and loc.Curve:
            mid = loc.Curve.Evaluate(0.5, True)
            return mid.X, mid.Y
    except Exception:
        pass

    return None


def sort_elements(elements, sort_choice, view):
    """sort_choice: 0=selection order, 1=left-right, 2=right-left,
    3=top-bottom, 4=bottom-top (matches the Sort Order combo boxes).
    Elements with no determinable position are kept at the end, in their
    original order, rather than silently collapsing to (0, 0)."""
    if sort_choice == 0:
        return list(elements)

    positioned = []
    unpositioned = []
    for el in elements:
        pos = get_element_position(el, view)
        if pos is None:
            unpositioned.append(el)
        else:
            positioned.append((pos[0], pos[1], el))

    if sort_choice == 1:
        positioned.sort(key=lambda t: t[0])
    elif sort_choice == 2:
        positioned.sort(key=lambda t: -t[0])
    elif sort_choice == 3:
        positioned.sort(key=lambda t: -t[1])
    elif sort_choice == 4:
        positioned.sort(key=lambda t: t[1])

    return [t[2] for t in positioned] + unpositioned


def nearest_neighbor_pairs(source_elements, target_elements, view):
    """Greedy nearest-position matching between two element lists, robust
    to mixed families and mismatched counts (unlike index-based pairing
    after independently sorting each side).

    Matching prioritizes X-alignment ("same column") over raw distance,
    because on single-line diagrams the vertical distance between a
    source (breaker/lift box near the top) and its real target (the cable
    far below it, often crossing several level break-lines) can be much
    larger than the gap to an unrelated element sitting right next to it.
    A source and target that share a column should match even if they are
    physically far apart vertically, so long as nothing else in that same
    column is a better fit.

    Returns:
      pairs: list of (source_element, target_element) ordered by target's
             position (top-to-bottom, then left-to-right) for a readable
             preview
      unmatched_sources: source elements left over (more sources than
             targets)
      unmatched_targets: target elements left over
      no_position: elements (from either list) that had no determinable
             position at all and were excluded from matching entirely
    """
    no_position = []

    def collect(elements):
        good = []
        for el in elements:
            pos = get_element_position(el, view)
            if pos is None:
                no_position.append(el)
            else:
                good.append((pos[0], pos[1], el))
        return good

    sources = collect(source_elements)
    targets = collect(target_elements)

    # X differences dominate the match; Y is only a tiebreaker between
    # elements that are already in (approximately) the same column. This
    # is what lets a source near the top of the diagram correctly match a
    # target far below it, instead of losing out to an unrelated element
    # that merely happens to be vertically closer. The weight is large
    # enough that even a small X offset always outweighs a large Y gap.
    X_WEIGHT = 1000000.0

    candidates = []
    for si, (sx, sy, s_el) in enumerate(sources):
        for ti, (tx, ty, t_el) in enumerate(targets):
            dist = (abs(sx - tx) * X_WEIGHT) + abs(sy - ty)
            candidates.append((dist, si, ti))
    candidates.sort(key=lambda c: c[0])

    used_source = set()
    used_target = set()
    matched = []  # (source_element, target_element, sort_y, sort_x)
    for dist, si, ti in candidates:
        if si in used_source or ti in used_target:
            continue
        used_source.add(si)
        used_target.add(ti)
        s_el = sources[si][2]
        t_el = targets[ti][2]
        matched.append((s_el, t_el, -targets[ti][1], targets[ti][0]))

    # Order for preview: top-to-bottom, then left-to-right
    matched.sort(key=lambda m: (m[2], m[3]))
    pairs = [(m[0], m[1]) for m in matched]

    unmatched_sources = [sources[i][2] for i in range(len(sources)) if i not in used_source]
    unmatched_targets = [targets[i][2] for i in range(len(targets)) if i not in used_target]

    return pairs, unmatched_sources, unmatched_targets, no_position


def get_type_name(doc, element):
    try:
        et = doc.GetElement(element.GetTypeId())
        if et is not None:
            fam_name = getattr(et, 'FamilyName', None)
            type_name_param = et.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
            type_name = type_name_param.AsString() if type_name_param else et.Name
            if fam_name:
                return '{} : {}'.format(fam_name, type_name)
            return type_name or et.Name
    except Exception:
        pass
    try:
        return element.Name
    except Exception:
        return '<unknown>'


def get_category_name(element):
    try:
        if element.Category:
            return element.Category.Name
    except Exception:
        pass
    return '<none>'


def filter_out_tags(elements):
    """Returns (kept_elements, excluded_count)."""
    kept = [el for el in elements if not is_tag_or_annotation(el)]
    return kept, len(elements) - len(kept)
