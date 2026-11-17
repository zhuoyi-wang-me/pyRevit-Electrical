# -*- coding: utf-8 -*-
"""Result rows, status groups, metric formatting and CSV export. PURE PYTHON."""
import io
import re

from reviztoclash import match

OPEN = u"Open"
REVIEW = u"In review"
RESOLVED = u"Resolved"
OTHER = u"Other"
GROUPS = [OPEN, REVIEW, RESOLVED, OTHER]

_RESOLVED = set([u"resolved", u"solved", u"closed", u"approved", u"fixed", u"done", u"complete",
                 u"completed", u"ignored", u"notanissue", u"accepted", u"void"])
_REVIEW = set([u"reviewed", u"review", u"inprogress", u"progress", u"assigned", u"pending",
               u"readyforreview", u"onhold", u"waiting", u"underreview", u"inreview"])
_OPEN = set([u"", u"new", u"active", u"open", u"opened", u"reopened", u"created", u"todo"])


def status_group(status, state=u""):
    """Revizto (Open / In progress / Solved / Closed), Navisworks (New / Active /
    Reviewed / Approved / Resolved) and BCF statuses -> one of GROUPS.
    state = Revizto 'Clashing status': a clash that no longer clashes counts as
    resolved whatever its issue status says."""
    if match.norm(state) in _RESOLVED or match.norm(state) in (u"notclashing", u"noclash"):
        return RESOLVED
    key = match.norm(status)
    if key in _OPEN:
        return OPEN
    if key in _RESOLVED:
        return RESOLVED
    if key in _REVIEW:
        return REVIEW
    return OTHER


# ---------------------------------------------------------------------------
# metric formatting
# ---------------------------------------------------------------------------
LENGTH_RE = re.compile(u"^\\s*([-+−]?\\s*\\d+(?:[.,]\\d+)?)\\s*(mm|cm|m|ft|feet|foot|in|inch|inches|'|\")?\\s*$",
                       re.I)
_TO_MM = {u"mm": 1.0, u"cm": 10.0, u"m": 1000.0, u"ft": 304.8, u"feet": 304.8, u"foot": 304.8,
          u"'": 304.8, u"in": 25.4, u"inch": 25.4, u"inches": 25.4, u'"': 25.4}


def length_mm(text):
    """'-0.052 m' -> -52.0, '12 mm' -> 12.0, '0,17 ft' -> 51.8; None when the
    text has no unit (the unit of a bare number is unknown) or is no length."""
    found = LENGTH_RE.match(text or u"")
    if not found or not found.group(2):
        return None
    number = found.group(1).replace(u"−", u"-").replace(u" ", u"").replace(u",", u".")
    try:
        return float(number) * _TO_MM[found.group(2).lower()]
    except Exception:
        return None


def distance_text(text):
    """Distance column for the grid: metric when the unit is known."""
    value = length_mm(text)
    if value is None:
        return text or u""
    return u"{0:.0f} mm".format(value)


FEET_TO_M = 0.3048


def point_text_m(xyz_feet):
    """(x, y, z) in Revit internal feet -> 'X 12.345  Y 6.789  Z 3.000 m'."""
    if not xyz_feet:
        return u""
    return u"X {0:.3f}  Y {1:.3f}  Z {2:.3f} m".format(*[v * FEET_TO_M for v in xyz_feet])


def size_text_mm(min_feet, max_feet):
    if not min_feet or not max_feet:
        return u""
    sizes = [(b - a) * FEET_TO_M * 1000.0 for a, b in zip(min_feet, max_feet)]
    return u"{0:.0f} x {1:.0f} x {2:.0f} mm".format(*sizes)


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def _ids_text(side):
    values = [u"{0}".format(v) for v in side.get("ids") or []]
    values += list(side.get("uids") or []) + list(side.get("ifc") or [])
    if len(values) > 3:
        return u", ".join(values[:3]) + u" +{0}".format(len(values) - 3)
    return u", ".join(values)


def side_label(side, resolved):
    """Name for the grid: what Revit says (category: name), else the export's."""
    if resolved.get("label") or resolved.get("category"):
        if resolved.get("category") and resolved.get("label"):
            return u"{0}: {1}".format(resolved["category"], resolved["label"])
        return resolved.get("category") or resolved.get("label")
    parts = [p for p in (side.get("category"), side.get("name")) if p]
    return u": ".join(parts)


def found_text(resolved):
    status = resolved.get("status")
    if status in (match.FOUND, match.AMBIGUOUS, match.PARTIAL):
        where = resolved.get("where") or u""
        text = u"Yes - {0}".format(where) if where else u"Yes"
        if status != match.FOUND:
            text += u" ({0})".format(status.lower())
        return text
    if status == match.NO_ID:
        return u"No ID"
    return u"No"


def build_row(index, clash, res_a, res_b, centre=None, box=None):
    """One grid / CSV row (all text) for a clash and its two resolved sides."""
    notes = []
    for key, res in (("A", res_a), ("B", res_b)):
        if res.get("note"):
            notes.append(u"{0}: {1}".format(key, res["note"]))
    if clash.get("extra_ids"):
        notes.append(u"{0} more element(s) in the export".format(clash["extra_ids"]))
    if clash.get("issue_id"):
        notes.insert(0, u"Issue {0}".format(clash["issue_id"]))
    status = clash.get("status") or u""
    state = clash.get("state") or u""
    group_text = u" ".join(p for p in (u"#" + clash["group_id"] if clash.get("group_id") else u"",
                                       clash.get("group") or u"") if p)
    location = u" / ".join(p for p in (clash.get("level"), clash.get("gridref")) if p)
    return {
        "idx": index,
        "clash_id": clash.get("clash_id") or u"",
        "status": u"{0} / {1}".format(status, state) if status and state else (status or state),
        "group": status_group(status, state),
        "test": clash.get("test") or u"",
        "clash_group": group_text,
        "location": location,
        "point": clash.get("point") or u"",
        "date": clash.get("date") or u"",
        "title": clash.get("title") or u"",
        "a_element": side_label(clash["a"], res_a),
        "a_id": _ids_text(clash["a"]),
        "a_found": found_text(res_a),
        "b_element": side_label(clash["b"], res_b),
        "b_id": _ids_text(clash["b"]),
        "b_found": found_text(res_b),
        "found": match.clash_found(res_a.get("status"), res_b.get("status")),
        "distance": distance_text(clash.get("distance")),
        "centre": point_text_m(centre),
        "size": size_text_mm(box[0], box[1]) if box else u"",
        "notes": u"; ".join(notes),
        "source_row": clash.get("row"),
    }


CSV_COLUMNS = [
    ("clash_id", u"Clash ID"), ("status", u"Status"), ("group", u"Status group"),
    ("test", u"Test"), ("clash_group", u"Group"), ("title", u"Title"),
    ("location", u"Level / grid"),
    ("a_element", u"Element A"), ("a_id", u"Element A ID"), ("a_found", u"Element A found"),
    ("b_element", u"Element B"), ("b_id", u"Element B ID"), ("b_found", u"Element B found"),
    ("found", u"Clash found"), ("distance", u"Distance"),
    ("point", u"Clash point from export"),
    ("centre", u"Clash centre (internal origin, m)"), ("size", u"Clash box size (mm)"),
    ("date", u"Found / created"), ("notes", u"Notes"), ("source_row", u"Source row"),
]


def csv_field(value):
    text = u"" if value is None else u"{0}".format(value)
    if any(ch in text for ch in u",;\"\r\n"):
        return u'"' + text.replace(u'"', u'""') + u'"'
    return text


def csv_text(rows):
    lines = [u",".join(csv_field(label) for _key, label in CSV_COLUMNS)]
    for row in rows:
        lines.append(u",".join(csv_field(row.get(key)) for key, _label in CSV_COLUMNS))
    return u"\r\n".join(lines) + u"\r\n"


def write_csv(path, rows):
    """UTF-8 with BOM so Excel shows accents correctly."""
    with io.open(path, "w", encoding="utf-8-sig", newline="") as stream:
        stream.write(csv_text(rows))
