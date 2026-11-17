# -*- coding: utf-8 -*-
"""
Electrical Calculator - isolator rating -> related fixture (pure Python, unit-tested).

Which fixture belongs to a load:
    SELF  - the selected load is itself an electrical fixture carrying the rating
            parameter (an isolator that holds the equipment's load)
    TAG   - fixture tag parameter == equipment tag parameter (MEE_ToServe by default,
            type name when empty) - Equipment Power Check rules (epc_match)
    NEAR  - nearest such fixture within the plan distance / height difference
The Revit side (revit_loads.find_related_fixtures / write_ratings) collects the
elements and does the writing; this module only decides what goes where.
"""
from __future__ import division

def load_module(name, path):
    """Import a .py file by path - IronPython 2.7 (imp) and CPython 3.12+ (importlib)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except ImportError:
        import imp
        return imp.load_source(name, path)


HOW_SELF = u'self'
HOW_TAG = u'tag'
HOW_NEAR = u'nearest'


def match(loads, fixtures, epc, use_tag=True, use_distance=True, max_plan_ft=9.84, max_height_ft=13.12):
    """
    loads    : [epc.Item] - key = load element id
    fixtures : [epc.Item] - key = fixture element id (only fixtures that carry the parameter)
    epc      : the epc_match module (Equipment Power Check matching)
    Returns {load id: [(fixture id, fixture label, how, note)]}
    """
    results, _ = epc.check(loads, fixtures, use_tag, use_distance, max_plan_ft, max_height_ft)
    out = {}
    for res in results:
        found = []
        if res.tag_fixtures:
            found = [(f.key, f.label, HOW_TAG, res.note) for f in res.tag_fixtures]
        elif res.nearest is not None:
            found = [(res.nearest.key, res.nearest.label, HOW_NEAR, res.note)]
        out[res.equipment.key] = found
    return out


def plan_writes(ratings, matches):
    """
    ratings : {load id: isolator rating (A) or None}
    matches : {load id: [(fixture id, label, how, note)]}
    Returns (writes, conflicts)
        writes    {fixture id: {'rating', 'label', 'how', 'loads': [load ids]}}
        conflicts {fixture id: [ratings]} - one fixture matched by loads with
                  different ratings: the LARGEST is written (safe side)
    """
    writes, conflicts = {}, {}
    for load_id in sorted(matches):
        rating = ratings.get(load_id)
        if not rating:
            continue
        for fid, label, how, _ in matches[load_id]:
            w = writes.get(fid)
            if w is None:
                writes[fid] = {'rating': rating, 'label': label, 'how': how, 'loads': [load_id]}
                continue
            if w['rating'] != rating:
                conflicts.setdefault(fid, [w['rating']]).append(rating)
            w['rating'] = max(w['rating'], rating)
            w['loads'].append(load_id)
    return writes, conflicts


def text_value(rating, fmt=u'{0}A'):
    """Value for a TEXT parameter: 40 -> '40A' (format editable in the window)."""
    try:
        return (fmt or u'{0}A').format(int(rating) if float(rating).is_integer() else rating)
    except (IndexError, KeyError, ValueError):
        return u'{0}A'.format(rating)
