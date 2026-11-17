# -*- coding: utf-8 -*-
"""Pick the Revit element(s) for each side of a clash. PURE PYTHON.

The Revit side (revit_side.py) wraps the host model and every loaded link
instance in a "source" object with:
    source.key       unique per source (host = 'host', link = instance id)
    source.doc_key   same for every instance of one linked file
    source.name      model name ('ARC_Building.rvt' / doc title)
    source.is_link   bool
    source.find(kind, value) -> (element id int, category, name) or None
                     kind = 'ids' (int element id), 'uids' (UniqueId), 'ifc'
The unit tests use simple fake sources with the same shape.

Element ids are only unique INSIDE one model, so one id can exist in the host
and in several links. The model name from the export ('Model 1', 'Item 1
File' ...) and the category narrow it down; what is left is reported as
AMBIGUOUS (host preferred) so the user can see it.
"""
import os
import re

FOUND = u"Found"
AMBIGUOUS = u"Ambiguous"
PARTIAL = u"Partly found"
NOT_FOUND = u"Not found"
NO_ID = u"No ID"

MODEL_SUFFIXES = (u"_detached", u"-detached", u"_central", u"-central", u"_local", u"-local")
MODEL_EXTS = (u".rvt", u".nwc", u".nwd", u".ifc", u".rvz", u".dwg")


def norm(text):
    return re.sub(u"[^0-9a-z]+", u"", (text or u"").lower())


def norm_model(name):
    """'C:\\..\\ARC_Building_detached.rvt' -> 'arcbuilding'."""
    text = (name or u"").strip().replace(u"\\", u"/").rsplit(u"/", 1)[-1].lower()
    for ext in MODEL_EXTS:
        if text.endswith(ext):
            text = text[:-len(ext)]
    for suffix in MODEL_SUFFIXES:
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return norm(text)


def model_matches(hint, name):
    a, b = norm_model(hint), norm_model(name)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def hint_matches(hints, category, name):
    """True when a hint and the element's category or 'Family : Type' name
    contain one another (ignoring case, spaces and punctuation)."""
    for hint in hints:
        h = norm(hint)
        if len(h) < 3:
            continue
        for text in (category, name):
            t = norm(text)
            if len(t) >= 3 and (t in h or h in t):
                return True
    return False


def _lookups(side):
    for kind in ("uids", "ifc", "ids"):          # globally unique ids first
        for value in side.get(kind) or []:
            yield kind, value


def resolve_side(side, sources):
    """-> {'status', 'refs': [(source key, element id int)], 'where', 'label',
           'category', 'note'}"""
    result = {"status": NO_ID, "refs": [], "where": u"", "label": u"", "category": u"",
              "note": u""}
    lookups = list(_lookups(side))
    if not lookups:
        result["note"] = u"no element id in the export"
        return result
    hint = side.get("model") or u""
    hint_known = bool(hint) and any(model_matches(hint, s.name) for s in sources)
    category = side.get("category") or u""
    found, ambiguous, missing = 0, 0, 0
    wheres, labels, categories, notes = [], [], [], []
    seen_ids = set()
    for kind, value in lookups:
        candidates = []
        for source in sources:
            hit = source.find(kind, value)
            if hit is not None:
                candidates.append((source, hit))
        if not candidates:
            missing += 1
            continue
        if hint_known:
            same = [c for c in candidates if model_matches(hint, c[0].name)]
            candidates = same or candidates
        if category and len(candidates) > 1:
            same = [c for c in candidates if norm(c[1][1]) == norm(category)]
            candidates = same or candidates
        hints = side.get("hints") or []
        if hints and len(set(c[0].doc_key for c in candidates)) > 1:
            # Revizto: clash 'STR-BEAM vs MEE_ELQ-SMDB', test '306.STR-Structural
            # Trusses Vs 211.MEP-Electrical Equipment' -> name / category of A and B
            same = [c for c in candidates if hint_matches(hints, c[1][1], c[1][2])]
            candidates = same or candidates
        docs = []
        for source, _hit in candidates:
            if source.doc_key not in docs:
                docs.append(source.doc_key)
        # host first, then links in the order Revit lists them
        candidates.sort(key=lambda c: (1 if c[0].is_link else 0))
        source, (element_id, cat, name) = candidates[0]
        ref = (source.key, element_id)
        if ref in seen_ids:
            continue                        # same element through id and GUID
        seen_ids.add(ref)
        result["refs"].append(ref)
        if len(docs) > 1:
            ambiguous += 1
            others = [c[0].name for c in candidates[1:] if c[0].doc_key != source.doc_key]
            notes.append(u"id {0} also in {1}".format(value, u", ".join(sorted(set(others)))))
        else:
            found += 1
        _add(wheres, source.name)
        _add(labels, name)
        _add(categories, cat)
    if hint and not hint_known:
        notes.append(u"model '{0}' is not open or loaded as a link".format(hint))
    if missing and (found or ambiguous):
        notes.append(u"{0} of {1} id(s) not found".format(missing, len(lookups)))
    if found + ambiguous == 0:
        result["status"] = NOT_FOUND
    elif ambiguous:
        result["status"] = AMBIGUOUS
    elif missing:
        result["status"] = PARTIAL
    else:
        result["status"] = FOUND
    result["where"] = u", ".join(wheres)
    result["label"] = u", ".join(l for l in labels if l)
    result["category"] = u", ".join(c for c in categories if c)
    result["note"] = u"; ".join(notes)
    return result


def _add(values, value):
    if value and value not in values:
        values.append(value)


def is_found(status):
    return status in (FOUND, AMBIGUOUS, PARTIAL)


def clash_found(status_a, status_b):
    """'Yes' both sides located, 'Partly' one side, 'No' none."""
    a, b = is_found(status_a), is_found(status_b)
    if a and b:
        return u"Yes"
    if a or b:
        return u"Partly"
    return u"No"


def model_label(path_or_title):
    """Short model name for the grid."""
    text = (path_or_title or u"").replace(u"\\", u"/").rsplit(u"/", 1)[-1]
    root, ext = os.path.splitext(text)
    return root if ext.lower() in MODEL_EXTS else text
