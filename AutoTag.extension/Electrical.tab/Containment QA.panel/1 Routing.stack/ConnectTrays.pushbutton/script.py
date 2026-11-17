# -*- coding: utf-8 -*-
"""
Auto-Connect Cable Trays
=========================
pyRevit tool that connects two selected groups of cable trays automatically,
matching them by Family Type / Service Type, pairing the closest trays
between the two groups, and generating the correct fitting (elbow,
transition or union) at whatever horizontal angle is required.

Tested against: Revit 2021 - 2024 API (Autodesk.Revit.DB / DB.Electrical)
Engine: works under both IronPython2.7 and pyRevit's CPython3 engine.

Author: pyRevit Community Tool
"""

import math
import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    XYZ,
    Transaction,
    TransactionGroup,
    SubTransaction,
    BuiltInCategory,
    ElementId,
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


def safe_table(rows, columns, limit=500, chunk=100):
    """output.print_table without pyRevit's Markdown converter: on a table of a
    few hundred rows that converter recurses so deeply under IronPython that
    Revit crashes (0xc00000fd). Plain HTML, in chunks, at most `limit` rows."""
    def cell(value):
        try:
            text = value if isinstance(value, unicode) else unicode(value)  # noqa: F821
        except NameError:
            text = str(value)
        return text.replace(u'<', u'&lt;').replace(u'>', u'&gt;')
    head = u'<thead><tr>{0}</tr></thead>'.format(u''.join(u'<th>{0}</th>'.format(cell(c)) for c in columns))
    shown = list(rows)[:limit]
    for start in range(0, len(shown), chunk):
        body = u''.join(u'<tr>{0}</tr>'.format(u''.join(u'<td>{0}</td>'.format(cell(v)) for v in row))
                        for row in shown[start:start + chunk])
        output.print_html(u'<table>{0}<tbody>{1}</tbody></table>'.format(head if start == 0 else u'', body))
    if len(rows) > limit:
        output.print_html(u'<p><i>... {0} more row(s) not shown.</i></p>'.format(len(rows) - limit))

# --------------------------------------------------------------------------
# CONFIG - adjust to match your project standards
# --------------------------------------------------------------------------

# Name of the (optional) shared/project parameter used to classify cable
# trays by discipline / service (e.g. "Power", "Data", "Fire Alarm").
# If the parameter doesn't exist on an element, matching falls back to
# Family Type only.
SERVICE_TYPE_PARAM = "Service Type"

# Angle tolerance (radians) used to decide whether two facing connectors
# are "straight" (union/transition) or require an elbow. ~1 degree.
STRAIGHT_ANGLE_TOLERANCE = math.radians(1.0)

# Distance tolerance (feet) - connector endpoints closer than this are
# treated as already coincident.
DISTANCE_TOLERANCE = 0.01

# Size tolerance (feet) for deciding whether two rectangular connector
# profiles count as "equal size".
SIZE_TOLERANCE = 0.001


# --------------------------------------------------------------------------
# Selection filter - cable trays only
# --------------------------------------------------------------------------

class CableTraySelectionFilter(ISelectionFilter):
    """Restricts picking to Cable Tray elements only."""

    def AllowElement(self, elem):
        try:
            return (
                elem.Category is not None
                and _id_int(elem.Category.Id)
                == int(BuiltInCategory.OST_CableTray)
            )
        except Exception:
            return False

    def AllowReference(self, reference, point):
        return True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def pick_group(prompt):
    """Interactively pick a group of cable trays. Returns list of Element."""
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element, CableTraySelectionFilter(), prompt
        )
    except OperationCanceledException:
        return []
    return [doc.GetElement(r) for r in refs]


def get_family_type_name(tray):
    try:
        sym = tray.Symbol if hasattr(tray, "Symbol") else doc.GetElement(tray.GetTypeId())
        return sym.Family.Name + " : " + sym.Name
    except Exception:
        return "Unknown Type"


def get_service_type(tray):
    p = tray.LookupParameter(SERVICE_TYPE_PARAM)
    if p is None:
        return None
    try:
        if p.StorageType == DB.StorageType.String:
            return p.AsString()
        elif p.StorageType == DB.StorageType.ElementId:
            eid = p.AsElementId()
            if eid != ElementId.InvalidElementId:
                return doc.GetElement(eid).Name
        return p.AsValueString()
    except Exception:
        return None


def match_key(tray):
    """Key used to decide whether two trays are allowed to connect."""
    return (get_family_type_name(tray), get_service_type(tray))


def get_connectors(tray):
    try:
        return list(tray.ConnectorManager.Connectors)
    except Exception:
        return []


def get_free_connectors(tray):
    return [c for c in get_connectors(tray) if not c.IsConnected]


def tray_endpoints(tray):
    loc = tray.Location
    if hasattr(loc, "Curve") and loc.Curve is not None:
        crv = loc.Curve
        return crv.GetEndPoint(0), crv.GetEndPoint(1)
    return None, None


def closest_free_connector_pair(tray_a, tray_b):
    """Return (conn_a, conn_b, distance) for the closest pair of free
    (unconnected) connectors between two trays, or None if either tray
    has no free connectors left."""
    best = None
    for ca in get_free_connectors(tray_a):
        for cb in get_free_connectors(tray_b):
            d = ca.Origin.DistanceTo(cb.Origin)
            if best is None or d < best[2]:
                best = (ca, cb, d)
    return best


def connector_size(c):
    """Returns a comparable (width, height) tuple for rectangular / oval
    cable tray connectors, or (radius,) for round."""
    try:
        if c.Shape == DB.ConnectorProfileType.Rectangular or c.Shape == DB.ConnectorProfileType.Oval:
            return (round(c.Width, 4), round(c.Height, 4))
        elif c.Shape == DB.ConnectorProfileType.Round:
            return (round(c.Radius, 4),)
    except Exception:
        pass
    return None


def sizes_equal(ca, cb):
    sa, sb = connector_size(ca), connector_size(cb)
    if sa is None or sb is None:
        return False
    if len(sa) != len(sb):
        return False
    return all(abs(x - y) <= SIZE_TOLERANCE for x, y in zip(sa, sb))


def connector_direction(c):
    """Outward direction of a connector (points away from the tray body)."""
    return c.CoordinateSystem.BasisZ


def is_straight(ca, cb):
    """True if the two facing connectors are collinear/opposed (a plain
    butt joint), False if an elbow is needed to bridge an angle. Works
    for ANY horizontal angle - not limited to 45/90 - because the
    decision only distinguishes 'straight' vs 'not straight'; the actual
    angle geometry is resolved by Revit's NewElbowFitting call itself."""
    da = connector_direction(ca)
    db = connector_direction(cb)
    angle = da.AngleTo(db)
    # Facing connectors on a straight run point in opposite directions,
    # i.e. angle between them ~= pi (180 deg).
    return abs(angle - math.pi) <= STRAIGHT_ANGLE_TOLERANCE


def create_fitting(ca, cb):
    """Creates the correct Revit fitting between two connectors and
    returns the created Element. Raises on failure."""
    if ca.IsConnected or cb.IsConnected:
        raise Exception("One or both connectors are already connected - skipped to avoid a duplicate fitting.")

    if ca.Origin.DistanceTo(cb.Origin) <= DISTANCE_TOLERANCE and is_straight(ca, cb):
        # Already touching / effectively coincident and collinear -
        # a plain connector-to-connector join is enough, no fitting needed.
        ca.ConnectTo(cb)
        return None

    if is_straight(ca, cb):
        if sizes_equal(ca, cb):
            fitting = doc.Create.NewUnionFitting(ca, cb)
        else:
            fitting = doc.Create.NewTransitionFitting(ca, cb)
    else:
        # Any horizontal angle - Revit computes the exact elbow geometry
        # from the two connectors' positions/directions, so this is not
        # restricted to 45 or 90 degrees.
        try:
            fitting = doc.Create.NewElbowFitting(ca, cb)
        except Exception:
            # Some tray families need an explicit transition before an
            # elbow when sizes differ - fall back to that combination.
            if not sizes_equal(ca, cb):
                fitting = doc.Create.NewTransitionFitting(ca, cb)
            else:
                raise

    return fitting


def validate_connection(ca, cb):
    """Re-checks the two connectors after fitting creation to confirm the
    network is actually connected (per requirement #10 / #12)."""
    try:
        return ca.IsConnected and cb.IsConnected
    except Exception:
        return False


def pair_up(group_a, group_b):
    """Greedy nearest-neighbour matching of trays in group_a to group_b
    that share the same match_key(). Returns:
        pairs           -> list of (tray_a, tray_b, distance)
        skipped_a        -> trays in A with no compatible match in B
        skipped_b        -> trays in B with no compatible match in A
    """
    candidates = []
    for a in group_a:
        for b in group_b:
            if match_key(a) != match_key(b):
                continue
            pa0, pa1 = tray_endpoints(a)
            pb0, pb1 = tray_endpoints(b)
            if pa0 is None or pb0 is None:
                continue
            d = min(
                pa0.DistanceTo(pb0), pa0.DistanceTo(pb1),
                pa1.DistanceTo(pb0), pa1.DistanceTo(pb1),
            )
            candidates.append((d, a, b))

    candidates.sort(key=lambda t: t[0])

    used_a, used_b = set(), set()
    pairs = []
    for d, a, b in candidates:
        if a.Id in used_a or b.Id in used_b:
            continue
        used_a.add(a.Id)
        used_b.add(b.Id)
        pairs.append((a, b, d))

    skipped_a = [a for a in group_a if a.Id not in used_a]
    skipped_b = [b for b in group_b if b.Id not in used_b]
    return pairs, skipped_a, skipped_b


def elem_desc(e):
    try:
        return "Id {} ({})".format(_id_int(e.Id), get_family_type_name(e))
    except Exception:
        return "Id {}".format(e.Id.IntegerValue)


# --------------------------------------------------------------------------
# Main workflow
# --------------------------------------------------------------------------

def main():
    forms.alert(
        "Step 1 of 2: Select the FIRST group of cable trays "
        "(e.g. the 12 trays on side A), then click Finish.",
        title="Auto-Connect Cable Trays",
        warn_icon=False,
    )
    group_a = pick_group("Select GROUP A cable trays, then click Finish")
    if not group_a:
        forms.alert("No trays selected for Group A. Aborted.", exitscript=True)

    forms.alert(
        "Step 2 of 2: Select the SECOND group of cable trays "
        "(e.g. the 12 trays on side B), then click Finish.",
        title="Auto-Connect Cable Trays",
        warn_icon=False,
    )
    group_b = pick_group("Select GROUP B cable trays, then click Finish")
    if not group_b:
        forms.alert("No trays selected for Group B. Aborted.", exitscript=True)

    pairs, skipped_a, skipped_b = pair_up(group_a, group_b)

    connected_rows = []
    skipped_rows = []
    error_rows = []

    for a in skipped_a:
        skipped_rows.append((elem_desc(a), "No matching Family Type / Service Type in Group B"))
    for b in skipped_b:
        skipped_rows.append((elem_desc(b), "No matching Family Type / Service Type in Group A"))

    # A SubTransaction can only live inside an *open* Transaction (a
    # TransactionGroup on its own does not count as one), so we wrap the
    # whole batch in a single Transaction and use a SubTransaction per
    # pair purely so one failed pair can be rolled back without losing
    # the pairs already completed.
    tg = TransactionGroup(doc, "Auto-Connect Cable Trays")
    tg.Start()
    t = Transaction(doc, "Connect Cable Trays")
    t.Start()

    for a, b, dist in pairs:
        sub = SubTransaction(doc)
        sub.Start()
        try:
            best = closest_free_connector_pair(a, b)
            if best is None:
                raise Exception("No free connector ends available on one or both trays.")
            ca, cb, cdist = best

            fitting = create_fitting(ca, cb)

            # refresh connector state and validate
            ok = validate_connection(ca, cb)
            if not ok:
                raise Exception("Fitting created but connectors did not report as connected.")

            sub.Commit()
            fitting_desc = "direct join" if fitting is None else fitting.Category.Name
            connected_rows.append((elem_desc(a), elem_desc(b), fitting_desc))
        except Exception as ex:
            sub.RollBack()
            error_rows.append((elem_desc(a), elem_desc(b), str(ex)))

    t.Commit()
    tg.Assimilate()

    # ----------------------------------------------------------------
    # Report
    # ----------------------------------------------------------------
    total = len(group_a) + len(group_b)
    output.print_md("# Auto-Connect Cable Trays - Summary Report")
    output.print_md(
        "**Total trays processed:** {}  \n"
        "**Pairs attempted:** {}  \n"
        "**Successfully connected:** {}  \n"
        "**Skipped (unmatched):** {}  \n"
        "**Errors:** {}".format(
            total, len(pairs), len(connected_rows), len(skipped_rows), len(error_rows)
        )
    )

    if connected_rows:
        output.print_md("## Connected Trays")
        safe_table(connected_rows, ["Tray A", "Tray B", "Fitting Used"])

    if skipped_rows:
        output.print_md("## Skipped Trays")
        safe_table(skipped_rows, ["Tray", "Reason"])

    if error_rows:
        output.print_md("## Errors")
        safe_table(error_rows, ["Tray A", "Tray B", "Error"])

    forms.alert(
        "Done.\n\nConnected: {}\nSkipped: {}\nErrors: {}\n\n"
        "See the pyRevit output window for the full report.".format(
            len(connected_rows), len(skipped_rows), len(error_rows)
        ),
        title="Auto-Connect Cable Trays",
    )


if __name__ == "__main__":
    main()
