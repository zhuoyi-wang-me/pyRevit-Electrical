# -*- coding: utf-8 -*-
"""Split cable trays (and conduits) in batch, with a gap.

WHAT IT DOES
    One pick cuts a whole bank of parallel trays at the same station and
    leaves a gap - for wall openings, sleeves, road crossings, expansion
    gaps. Keep picking to cut the bank at several stations, ESC to finish.

WHICH TRAYS GET CUT (target mode)
    Auto-detect at each pick  - every tray parallel to the one you clicked,
                                within a search radius, that the cut fits
                                into. Nothing to pre-select.
    Pick trays first          - rubber-band / window select the trays once,
                                then pick as many split stations as you like.
    All trays in view         - everything splittable in the active view.
    Only the tray I click     - single element, no batch.

    A pre-selection made before launching the tool always wins.

HOW THE GAP IS SET (split mode)
    Gap at picked point       - one click, opening centred on it.
    Gap between two points    - click each side of the obstruction; the gap
                                follows what you clicked.
    No gap                    - plain split, halves reconnected.

Shift+Click the button to change any of this. Settings are remembered.
Works in Revit 2021-2026, IronPython and CPython engines.
"""

__title__ = 'Split\nwith Gap'
__author__ = 'BHEEM'
__doc__ = 'Batch-splits cable trays / conduits and leaves a gap.\n' \
          'One pick cuts a whole bank. Shift+Click for settings.'

import math

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()
config = script.get_config()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Element classes this tool is allowed to split.
# Remove Conduit if you only ever want cable trays.
SPLIT_CLASSES = (DB.Electrical.CableTray, DB.Electrical.Conduit)

# Categories used when hunting for trays to batch-split.
SPLIT_CATEGORIES = (DB.BuiltInCategory.OST_CableTray,
                    DB.BuiltInCategory.OST_Conduit)

# Shortest piece we are willing to leave behind (feet). ~3 mm.
MIN_SEGMENT = 0.01

# Tolerance used when matching a connector to an end point (feet). ~1 mm.
CONNECTOR_TOL = 0.003

# How far off parallel a neighbouring tray may be and still join the batch.
PARALLEL_TOL_DEG = 1.0

# Split modes
MODE_POINT = 'Gap at picked point'
MODE_TWO_POINTS = 'Gap between two picked points'
MODE_NO_GAP = 'No gap (plain split)'

# Target modes
TARGET_AUTO = 'Auto-detect parallel trays at each pick'
TARGET_PICK = 'Pick the trays first (window select)'
TARGET_VIEW = 'All trays in the active view'
TARGET_SINGLE = 'Only the tray I click'


# ---------------------------------------------------------------------------
# UNITS
# ---------------------------------------------------------------------------

def _length_format():
    return doc.GetUnits().GetFormatOptions(DB.SpecTypeId.Length)


def to_internal(value):
    """Convert a length from the project's display units to feet."""
    return DB.UnitUtils.ConvertToInternalUnits(value, _length_format().GetUnitTypeId())


def from_internal(value):
    """Convert a length in feet to the project's display units."""
    return DB.UnitUtils.ConvertFromInternalUnits(value, _length_format().GetUnitTypeId())


def unit_label():
    try:
        return DB.LabelUtils.GetLabelForUnit(_length_format().GetUnitTypeId())
    except Exception:
        return 'project units'


# ---------------------------------------------------------------------------
# SELECTION HELPERS
# ---------------------------------------------------------------------------

class MEPCurveFilter(ISelectionFilter):
    """Only let the user click on splittable MEP curves."""

    def AllowElement(self, element):
        return isinstance(element, SPLIT_CLASSES)

    def AllowReference(self, reference, position):
        return True


def pick_point_on_tray(prompt):
    """Pick a point on a splittable element. Returns a Reference or None."""
    try:
        return uidoc.Selection.PickObject(ObjectType.PointOnElement,
                                          MEPCurveFilter(), prompt)
    except OperationCanceledException:
        return None


def pick_trays(prompt):
    """Window / rubber-band select several trays. Returns a list."""
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element,
                                           MEPCurveFilter(), prompt)
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def preselected_elements():
    """Splittable elements the user selected before launching the tool."""
    elements = []
    for eid in uidoc.Selection.GetElementIds():
        elem = doc.GetElement(eid)
        if isinstance(elem, SPLIT_CLASSES):
            elements.append(elem)
    return elements


def view_elements():
    """Every splittable element in the active view."""
    elements = []
    for bic in SPLIT_CATEGORIES:
        collector = DB.FilteredElementCollector(doc, doc.ActiveView.Id) \
            .OfCategory(bic).WhereElementIsNotElementType()
        for elem in collector:
            if isinstance(elem, SPLIT_CLASSES):
                elements.append(elem)
    return elements


def elements_near(point, radius):
    """Splittable elements whose bounding box is within radius of point."""
    offset = DB.XYZ(radius, radius, radius)
    outline = DB.Outline(point - offset, point + offset)
    bbox_filter = DB.BoundingBoxIntersectsFilter(outline)
    elements = []
    for bic in SPLIT_CATEGORIES:
        collector = DB.FilteredElementCollector(doc) \
            .OfCategory(bic).WhereElementIsNotElementType() \
            .WherePasses(bbox_filter)
        for elem in collector:
            if isinstance(elem, SPLIT_CLASSES):
                elements.append(elem)
    return elements


def straight_curve(element):
    """Return the element's straight location curve, or None."""
    location = element.Location
    if not isinstance(location, DB.LocationCurve):
        return None
    curve = location.Curve
    if not isinstance(curve, DB.Line):
        return None
    return curve


def is_parallel(curve, direction):
    other = (curve.GetEndPoint(1) - curve.GetEndPoint(0)).Normalize()
    return abs(other.DotProduct(direction)) >= \
        math.cos(math.radians(PARALLEL_TOL_DEG))


def dedupe(elements):
    seen = set()
    unique = []
    for elem in elements:
        key = str(elem.Id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(elem)
    return unique


# ---------------------------------------------------------------------------
# GEOMETRY
# ---------------------------------------------------------------------------

def cut_points_for(curve, picks, gap):
    """Work out where each half has to stop.

    Returns (first_end, second_start) on the element's own centre line, or
    None if the requested cut does not fit on this element.
    """
    start = curve.GetEndPoint(0)
    end = curve.GetEndPoint(1)
    direction = (end - start).Normalize()

    projected = []
    for pick in picks:
        result = curve.Project(pick)
        if result is None:
            return None
        projected.append(result.XYZPoint)

    if len(projected) == 1:
        half = max(gap, 0.0) / 2.0
        first_end = projected[0] - direction * half
        second_start = projected[0] + direction * half
    else:
        # Order the two picks along the element's own direction.
        a, b = projected[0], projected[1]
        if (b - a).DotProduct(direction) < 0:
            a, b = b, a
        first_end, second_start = a, b

    if start.DistanceTo(first_end) < MIN_SEGMENT:
        return None
    if second_start.DistanceTo(end) < MIN_SEGMENT:
        return None
    # The opening must sit inside the element, not run off either end.
    if (first_end - start).DotProduct(direction) < 0:
        return None
    if (end - second_start).DotProduct(direction) < 0:
        return None
    return first_end, second_start


# ---------------------------------------------------------------------------
# CONNECTORS
# ---------------------------------------------------------------------------

def end_connector_at(mep_curve, xyz):
    """Return the End connector of mep_curve located at xyz (or None)."""
    best = None
    best_dist = CONNECTOR_TOL
    try:
        connectors = mep_curve.ConnectorManager.Connectors
    except Exception:
        return None
    for conn in connectors:
        if conn.ConnectorType != DB.ConnectorType.End:
            continue
        dist = conn.Origin.DistanceTo(xyz)
        if dist < best_dist:
            best_dist = dist
            best = conn
    return best


def physical_refs(connector):
    """Connectors of *other* elements physically joined to this connector."""
    refs = []
    if connector is None:
        return refs
    for ref in connector.AllRefs:
        if ref.ConnectorType == DB.ConnectorType.Logical:
            continue
        owner = ref.Owner
        if owner is None or owner.Id == connector.Owner.Id:
            continue
        refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# THE SPLIT
# ---------------------------------------------------------------------------

def split_element(element, picks, gap):
    """Split one MEP curve, leaving a gap. Returns the new element or None."""
    curve = straight_curve(element)
    if curve is None:
        logger.debug('Skipped %s - not a straight segment.', element.Id)
        return None

    cuts = cut_points_for(curve, picks, gap)
    if cuts is None:
        logger.debug('Skipped %s - the cut does not fit.', element.Id)
        return None
    first_end, second_start = cuts

    start = curve.GetEndPoint(0)
    end = curve.GetEndPoint(1)
    opening = first_end.DistanceTo(second_start)

    # Release whatever is joined at the far end, so that shortening the
    # original does not drag the neighbours along with it.
    far_conn = end_connector_at(element, end)
    saved_refs = physical_refs(far_conn)
    for ref in saved_refs:
        try:
            far_conn.DisconnectFrom(ref)
        except Exception as err:
            logger.debug('Disconnect failed: %s', err)

    # Copy first, while the original still has its full geometry.
    new_ids = DB.ElementTransformUtils.CopyElement(doc, element.Id, DB.XYZ.Zero)
    if not new_ids:
        return None
    new_element = doc.GetElement(list(new_ids)[0])

    # Re-cut both halves.
    element.Location.Curve = DB.Line.CreateBound(start, first_end)
    new_element.Location.Curve = DB.Line.CreateBound(second_start, end)
    doc.Regenerate()

    # Hand the original's downstream connections over to the new half.
    new_far_conn = end_connector_at(new_element, end)
    if new_far_conn is not None:
        for ref in saved_refs:
            try:
                new_far_conn.ConnectTo(ref)
            except Exception as err:
                logger.debug('Reconnect to neighbour failed: %s', err)

    # Only join the halves when there is genuinely no opening between them.
    if opening <= CONNECTOR_TOL:
        conn_a = end_connector_at(element, first_end)
        conn_b = end_connector_at(new_element, second_start)
        if conn_a is not None and conn_b is not None and not conn_a.IsConnected:
            try:
                conn_a.ConnectTo(conn_b)
            except Exception as err:
                logger.debug('Could not join the two halves: %s', err)

    doc.Regenerate()
    return new_element


def auto_targets(seed_element, picks, gap, radius):
    """Every tray parallel to the clicked one that this cut fits into."""
    seed_curve = straight_curve(seed_element)
    if seed_curve is None:
        return [seed_element]
    direction = (seed_curve.GetEndPoint(1) -
                 seed_curve.GetEndPoint(0)).Normalize()

    targets = []
    for elem in elements_near(picks[0], radius):
        curve = straight_curve(elem)
        if curve is None:
            continue
        if not is_parallel(curve, direction):
            continue
        if curve.Distance(picks[0]) > radius:
            continue
        if cut_points_for(curve, picks, gap) is None:
            continue
        targets.append(elem)
    return dedupe(targets) or [seed_element]


# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------

DEFAULT_RADIUS_FT = 10.0  # ~3 m


UNSET = '__unset__'


def read_option(name, default):
    """Safe wrapper around pyRevit's get_option.

    pyRevit only honours the default value when it is not None - passing None
    makes it re-raise the missing-parameter AttributeError. It can also throw
    if the config file is missing or unreadable, so everything is guarded.
    """
    try:
        value = config.get_option(name, default)
    except Exception:
        return default
    return default if value is None else value


def load_settings():
    """Return (split mode, gap, target mode, radius, configured?)."""
    default_radius = round(from_internal(DEFAULT_RADIUS_FT), 1)

    stored_gap = read_option('gap', UNSET)
    if stored_gap == UNSET:
        # Never configured - the caller will open the settings dialog.
        return MODE_POINT, 0.0, TARGET_AUTO, default_radius, False
    try:
        gap = float(stored_gap)
    except (TypeError, ValueError):
        return MODE_POINT, 0.0, TARGET_AUTO, default_radius, False

    try:
        radius = float(read_option('radius', default_radius))
    except (TypeError, ValueError):
        radius = default_radius

    return (read_option('mode', MODE_POINT), gap,
            read_option('target', TARGET_AUTO), radius, True)


def ask_number(prompt, default):
    answer = forms.ask_for_string(default=str(default), prompt=prompt,
                                  title='Split with Gap')
    if answer is None:
        return None
    try:
        return float(answer.replace(',', '.'))
    except ValueError:
        forms.alert('"{}" is not a number.'.format(answer), exitscript=True)


def edit_settings(mode, gap, target, radius):
    """Ask for the split mode, the gap, and which trays to cut."""
    chosen_mode = forms.CommandSwitchWindow.show(
        [MODE_POINT, MODE_TWO_POINTS, MODE_NO_GAP],
        message='How should the gap be set?'
    )
    if chosen_mode is None:
        return None

    new_gap = gap
    if chosen_mode == MODE_POINT:
        new_gap = ask_number('Gap size in {}:'.format(unit_label()), gap)
        if new_gap is None:
            return None
        if new_gap <= 0:
            chosen_mode = MODE_NO_GAP
    elif chosen_mode == MODE_NO_GAP:
        new_gap = 0.0

    chosen_target = forms.CommandSwitchWindow.show(
        [TARGET_AUTO, TARGET_PICK, TARGET_VIEW, TARGET_SINGLE],
        message='Which trays should be split?'
    )
    if chosen_target is None:
        return None

    new_radius = radius
    if chosen_target == TARGET_AUTO:
        new_radius = ask_number(
            'Search radius around each pick in {}:'.format(unit_label()),
            radius)
        if new_radius is None:
            return None

    try:
        config.mode = chosen_mode
        config.gap = new_gap
        config.target = chosen_target
        config.radius = new_radius
        script.save_config()
    except Exception as err:
        logger.debug('Could not save settings: %s', err)
        forms.alert('Settings could not be saved, so they will only apply to '
                    'this run.')
    return chosen_mode, new_gap, chosen_target, new_radius


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

mode, gap_display, target, radius_display, configured = load_settings()

# Shift+Click opens the settings. So does the very first run.
if __shiftclick__ or not configured:  # noqa: F821
    result = edit_settings(mode, gap_display, target, radius_display)
    if result is None:
        script.exit()
    mode, gap_display, target, radius_display = result

gap = to_internal(gap_display) if mode == MODE_POINT else 0.0
radius = to_internal(radius_display)
picks_needed = 2 if mode == MODE_TWO_POINTS else 1

# --- work out the batch of trays ------------------------------------------

fixed_batch = None
batch_label = ''

preselection = preselected_elements()
if preselection:
    fixed_batch = dedupe(preselection)
    batch_label = '{} pre-selected tray(s)'.format(len(fixed_batch))
elif target == TARGET_PICK:
    picked = pick_trays('Select the trays to split, then click Finish')
    if not picked:
        script.exit()
    fixed_batch = dedupe(picked)
    batch_label = '{} selected tray(s)'.format(len(fixed_batch))
elif target == TARGET_VIEW:
    fixed_batch = dedupe(view_elements())
    if not fixed_batch:
        forms.alert('No cable trays or conduits found in this view.',
                    exitscript=True)
    batch_label = '{} tray(s) in this view'.format(len(fixed_batch))
elif target == TARGET_AUTO:
    batch_label = 'trays parallel to each pick, within {} {}' \
        .format(radius_display, unit_label())
else:
    batch_label = 'one tray per pick'

if mode == MODE_POINT:
    gap_label = 'gap of {} {}'.format(gap_display, unit_label())
elif mode == MODE_TWO_POINTS:
    gap_label = 'gap between the two picked points'
else:
    gap_label = 'no gap'

print('Splitting {} with a {}. Shift+Click the button to change this.'
      .format(batch_label, gap_label))

# --- pick stations and cut -------------------------------------------------

total = 0
stations = 0

while True:
    first_ref = pick_point_on_tray(
        'Pick the split location (ESC to finish)' if picks_needed == 1
        else 'Pick the FIRST side of the opening (ESC to finish)')
    if first_ref is None:
        break
    picks = [first_ref.GlobalPoint]

    if picks_needed == 2:
        second_ref = pick_point_on_tray('Pick the SECOND side of the opening')
        if second_ref is None:
            break
        picks.append(second_ref.GlobalPoint)
        if picks[0].DistanceTo(picks[1]) < MIN_SEGMENT:
            forms.alert('The two points are on top of each other.')
            continue

    seed = doc.GetElement(first_ref.ElementId)
    if fixed_batch is not None:
        targets = fixed_batch
    elif target == TARGET_AUTO:
        targets = auto_targets(seed, picks, gap, radius)
    else:
        targets = [seed]

    done = 0
    with revit.Transaction('Split cable trays with gap'):
        for element in targets:
            if split_element(element, picks, gap) is not None:
                done += 1

    stations += 1
    total += done
    print('Station {}: split {} of {} tray(s).'
          .format(stations, done, len(targets)))

if total:
    print('Done - {} tray(s) split at {} station(s).'.format(total, stations))
elif stations:
    forms.alert('Nothing was split. The cut may not fit inside the trays, '
                'or they may not be straight segments.')
