# -*- coding: utf-8 -*-
"""Connect cable trays that sit at different levels - a whole bank at once.

WHAT IT DOES
    Give it a group of trays (or conduits) that run in line but at different
    elevations, and it pairs them up automatically and builds every
    transition in one go:

        - works out the horizontal run needed for the angle you choose,
        - extends or trims both trays back to the kink points,
        - drops in a sloped segment between them,
        - and creates the two elbow fittings.

    THE ANGLE
    Choose "Auto" and each pair gets the first angle from the preference
    list that its own space allows - 45 by default, stepping to 60 or 90
    where it is tight, or down to 30 / 20 if 45 will not fit. Reorder the
    list in the settings to change what is tried first. Or pick a fixed
    angle and it is forced on every pair.

PAIRING MODES (Shift+Click to change - remembered between runs)
    Select a group and auto-pair   - window select the trays on both levels,
                                     the tool works out which goes with which.
    Auto-pair every tray in view   - same, for the whole active view.
    Pick each pair myself          - two picks per transition, ESC to stop.

    A pre-selection of two or more trays made before launching always wins.

Works in Revit 2021-2026, IronPython and CPython engines.
"""

__title__ = 'Connect\nOffset Trays'
__author__ = 'BHEEM'
__doc__ = 'Connects trays at different levels with sloped segments and\n' \
          'elbows, a whole bank at a time. Shift+Click for settings.'

import math

from pyrevit import revit, DB, forms, script
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from Autodesk.Revit.Exceptions import OperationCanceledException

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()
output = script.get_output()
config = script.get_config()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

CONNECT_CLASSES = (DB.Electrical.CableTray, DB.Electrical.Conduit)

CONNECT_CATEGORIES = (DB.BuiltInCategory.OST_CableTray,
                      DB.BuiltInCategory.OST_Conduit)

# Shortest piece we are willing to leave behind (feet). ~3 mm.
MIN_SEGMENT = 0.01

# Tolerance used when matching a connector to an end point (feet). ~1 mm.
CONNECTOR_TOL = 0.003

# How far off parallel the two trays may be (degrees).
PARALLEL_TOL_DEG = 1.0

# When auto-pairing, trays that share a plan alignment are strongly
# preferred over ones shifted sideways. This is that weighting.
LATERAL_PENALTY = 10.0

# Default gap within which the tool looks for a partner tray.
DEFAULT_SEARCH_FT = 30.0  # ~9 m

ANGLE_AUTO = 'Auto - best angle for the space'
ANGLE_OTHER = 'Other angle...'
ANGLE_CHOICES = [ANGLE_AUTO, '20', '30', '45', '60', '90', ANGLE_OTHER]

# Angles the automatic mode may use, in preference order: the first one
# that fits the space is used. Editable from the Shift+Click settings.
DEFAULT_ANGLES = '45, 30, 20, 60, 90'

PAIR_GROUP = 'Select a group of trays and auto-pair them'
PAIR_VIEW = 'Auto-pair every tray in the active view'
PAIR_MANUAL = 'Pick each pair myself'

# Safety factor on the estimated elbow size. The real fitting family may be
# a little bigger than the bend radius suggests.
FITTING_CLEARANCE = 1.15

# When an elbow cannot be placed, undo that pair completely rather than
# leaving trimmed trays with open ends. Set False to keep the geometry.
ROLLBACK_IF_NO_FITTING = True

TRIM_YES = 'May shorten the trays to reach a flatter angle'
TRIM_NO = 'Only extend the trays, never shorten them'

# Shortest straight piece to leave on a tray when trimming it back.
DEFAULT_MIN_LEG_FT = 0.328  # ~100 mm

PLACE_CENTRE = 'Centre the offset between the two trays'
PLACE_FIRST = 'Against the first tray'
PLACE_SECOND = 'Against the second tray'


# ---------------------------------------------------------------------------
# UNITS AND CONFIG
# ---------------------------------------------------------------------------

def _length_format():
    return doc.GetUnits().GetFormatOptions(DB.SpecTypeId.Length)


def to_internal(value):
    return DB.UnitUtils.ConvertToInternalUnits(
        value, _length_format().GetUnitTypeId())


def from_internal(value):
    return DB.UnitUtils.ConvertFromInternalUnits(
        value, _length_format().GetUnitTypeId())


def unit_label():
    try:
        return DB.LabelUtils.GetLabelForUnit(_length_format().GetUnitTypeId())
    except Exception:
        return 'project units'


def fmt(length_ft):
    return '{0:.1f} {1}'.format(from_internal(length_ft), unit_label())


def read_option(name, default):
    """pyRevit's get_option re-raises when the default is None - guard it."""
    try:
        value = config.get_option(name, default)
    except Exception:
        return default
    return default if value is None else value


def save_options(**values):
    try:
        for key, value in values.items():
            setattr(config, key, value)
        script.save_config()
    except Exception as err:
        logger.debug('Could not save settings: %s', err)


# ---------------------------------------------------------------------------
# SELECTION
# ---------------------------------------------------------------------------

class MEPCurveFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, CONNECT_CLASSES)

    def AllowReference(self, reference, position):
        return True


def pick_tray(prompt):
    try:
        ref = uidoc.Selection.PickObject(ObjectType.Element,
                                         MEPCurveFilter(), prompt)
    except OperationCanceledException:
        return None
    return doc.GetElement(ref.ElementId)


def pick_trays(prompt):
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element,
                                           MEPCurveFilter(), prompt)
    except OperationCanceledException:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def preselected_elements():
    elements = []
    for eid in uidoc.Selection.GetElementIds():
        elem = doc.GetElement(eid)
        if isinstance(elem, CONNECT_CLASSES):
            elements.append(elem)
    return elements


def view_elements():
    elements = []
    for bic in CONNECT_CATEGORIES:
        collector = DB.FilteredElementCollector(doc, doc.ActiveView.Id) \
            .OfCategory(bic).WhereElementIsNotElementType()
        for elem in collector:
            if isinstance(elem, CONNECT_CLASSES):
                elements.append(elem)
    return elements


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


class FittingError(Exception):
    """Raised so the pair's transaction rolls back instead of committing."""


# ---------------------------------------------------------------------------
# GEOMETRY
# ---------------------------------------------------------------------------

def straight_curve(element):
    location = element.Location
    if not isinstance(location, DB.LocationCurve):
        return None
    curve = location.Curve
    if not isinstance(curve, DB.Line):
        return None
    return curve


def nearest_end_pair(curve_a, curve_b):
    best = None
    best_dist = None
    for i in (0, 1):
        for j in (0, 1):
            dist = curve_a.GetEndPoint(i).DistanceTo(curve_b.GetEndPoint(j))
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best = (i, j)
    return best


def pair_geometry(curve_a, curve_b):
    """Facing ends, axis and offset between two parallel curves.

    'along' is the clear gap between the two facing ends. 'reach_a' and
    'reach_b' are how much further each tray body extends behind its facing
    end, which is room the offset can also use if the trays may be trimmed.

    Returns a dict, or None when the curves are not parallel.
    """
    dir_a = (curve_a.GetEndPoint(1) - curve_a.GetEndPoint(0)).Normalize()
    dir_b = (curve_b.GetEndPoint(1) - curve_b.GetEndPoint(0)).Normalize()
    if abs(dir_a.DotProduct(dir_b)) < math.cos(math.radians(PARALLEL_TOL_DEG)):
        return None

    i, j = nearest_end_pair(curve_a, curve_b)
    point_a = curve_a.GetEndPoint(i)
    fixed_a = curve_a.GetEndPoint(1 - i)
    point_b = curve_b.GetEndPoint(j)
    fixed_b = curve_b.GetEndPoint(1 - j)

    axis = dir_a
    if (point_a - fixed_a).DotProduct(axis) < 0:
        axis = axis.Negate()

    between = point_b - point_a
    along = between.DotProduct(axis)
    perp = between - axis * along

    return {
        'point_a': point_a, 'fixed_a': fixed_a,
        'point_b': point_b, 'fixed_b': fixed_b,
        'axis': axis, 'along': along, 'perp': perp,
        'offset': perp.GetLength(),
        'reach_a': max((point_a - fixed_a).DotProduct(axis), 0.0),
        'reach_b': max((fixed_b - point_b).DotProduct(axis), 0.0),
    }


def room_for_offset(geo, trim, min_leg):
    """How much horizontal run the offset can actually use.

    Returns (lowest kink_a station, highest kink_b station), both measured
    along the axis from tray A's facing end. Without trimming the offset has
    to live in the clear gap; with trimming it can eat into either tray,
    which is what you do by hand when you drag an end back.
    """
    if not trim:
        return 0.0, geo['along']
    return (-max(geo['reach_a'] - min_leg, 0.0),
            geo['along'] + max(geo['reach_b'] - min_leg, 0.0))


def run_for(offset, angle_deg):
    """Horizontal run this angle needs to cover the offset."""
    if angle_deg >= 89.999:
        return 0.0
    return offset / math.tan(math.radians(angle_deg))


def parse_angles(text):
    """Turn '45, 30, 60' into a list of usable angles, order preserved.

    The order is the preference order: the first angle that fits the space
    is the one used, so put your house standard first.
    """
    angles = []
    for chunk in str(text).replace(';', ',').split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            value = float(chunk)
        except ValueError:
            continue
        if 0 < value <= 90 and value not in angles:
            angles.append(value)
    return angles


def size_of(element):
    """Tray width or conduit diameter, in feet. 0 when it cannot be read."""
    for name in ('Width', 'Diameter', 'Nominal Diameter', 'Outside Diameter'):
        try:
            param = element.LookupParameter(name)
        except Exception:
            param = None
        if param is not None and param.StorageType == DB.StorageType.Double:
            value = param.AsDouble()
            if value > 0:
                return value
    return 0.0


def bend_radius_of(element):
    """Best guess at the bend radius the elbow family will use, in feet."""
    try:
        etype = doc.GetElement(element.GetTypeId())
    except Exception:
        etype = None

    size = size_of(element)
    if etype is not None:
        for name in ('Bend Radius', 'Minimum Bend Radius'):
            try:
                param = etype.LookupParameter(name)
            except Exception:
                param = None
            if param is not None and \
                    param.StorageType == DB.StorageType.Double:
                value = param.AsDouble()
                if value > 0:
                    return value
        for name in ('Bend Radius Multiplier',):
            try:
                param = etype.LookupParameter(name)
            except Exception:
                param = None
            if param is not None and \
                    param.StorageType == DB.StorageType.Double:
                multiplier = param.AsDouble()
                if multiplier > 0 and size > 0:
                    return size * multiplier
    return size


def elbow_tangent(radius, angle_deg):
    """Straight length an elbow of this deflection eats on each leg."""
    if radius <= 0:
        return 0.0
    return radius * math.tan(math.radians(angle_deg) / 2.0)


def fit_angle(geo, angle_deg, trim, min_leg, radius, clearance):
    """Can this angle be built here? Returns a dict or a short reason."""
    offset = geo['offset']
    tangent = elbow_tangent(radius, angle_deg) * clearance

    # Each straight leg has to keep at least the elbow's tangent length.
    leg = max(min_leg, tangent)
    low, high = room_for_offset(geo, trim, leg)
    room = high - low
    run = run_for(offset, angle_deg)
    if run > room:
        return 'needs {0} of run but only {1} is available' \
            .format(fmt(run), fmt(room))

    # The sloped piece carries an elbow at both of its own ends.
    slope_length = offset / math.sin(math.radians(angle_deg))
    if slope_length < 2.0 * tangent:
        return 'the sloped piece would be {0} but the two elbows need {1}' \
            .format(fmt(slope_length), fmt(2.0 * tangent))

    return {'angle': angle_deg, 'run': run, 'low': low, 'high': high,
            'room': room, 'tangent': tangent}


def solve_transition(curve_a, curve_b, angle_deg, placement, allowed=None,
                     trim=True, min_leg=MIN_SEGMENT, radius=0.0,
                     clearance=FITTING_CLEARANCE):
    """Kink points of the offset, or a string saying why it cannot be built.

    Pass angle_deg=None to let the tool work down the preference list in
    'allowed'. Stations are measured along the axis from tray A's facing
    end: negative means the offset starts inside tray A.
    """
    geo = pair_geometry(curve_a, curve_b)
    if geo is None:
        return 'the two trays are not parallel'
    if geo['offset'] < CONNECTOR_TOL:
        return 'the two trays are already in line - use trim / extend instead'

    along = geo['along']
    offset = geo['offset']
    normal = geo['perp'].Normalize()
    axis = geo['axis']

    candidates = [angle_deg] if angle_deg is not None \
        else list(allowed or [90.0])

    fit = None
    reasons = []
    for candidate in candidates:
        attempt = fit_angle(geo, candidate, trim, min_leg, radius, clearance)
        if isinstance(attempt, str):
            reasons.append('{0:.0f} deg {1}'.format(candidate, attempt))
            continue
        fit = attempt
        break

    if fit is None:
        if len(candidates) == 1:
            return reasons[0]
        return 'no angle fits here - ' + '; '.join(reasons)

    angle_deg = fit['angle']
    run = fit['run']
    low, high = fit['low'], fit['high']

    # Where the offset would ideally start, then clamped into the room.
    if placement == PLACE_FIRST:
        station = 0.0
    elif placement == PLACE_SECOND:
        station = along - run
    else:
        station = (along - run) / 2.0
    station = max(low, min(station, high - run))

    kink_a = geo['point_a'] + axis * station
    kink_b = kink_a + axis * run + normal * offset

    if geo['fixed_a'].DistanceTo(kink_a) < MIN_SEGMENT:
        return 'the first tray would be left too short'
    if geo['fixed_b'].DistanceTo(kink_b) < MIN_SEGMENT:
        return 'the second tray would be left too short'
    if (kink_a - geo['fixed_a']).DotProduct(axis) <= 0:
        return 'the first tray would have to be flipped to reach the offset'
    if (geo['fixed_b'] - kink_b).DotProduct(axis) <= 0:
        return 'the second tray would have to be flipped to reach the offset'

    result = dict(geo)
    result['kink_a'] = kink_a
    result['kink_b'] = kink_b
    result['run'] = run
    result['angle'] = angle_deg
    result['room'] = fit['room']
    result['tangent'] = fit['tangent']
    return result


# ---------------------------------------------------------------------------
# AUTOMATIC PAIRING
# ---------------------------------------------------------------------------

def pair_score(curve_a, curve_b, max_search, trim=True, min_leg=MIN_SEGMENT):
    """Lower is a better match. None means these two cannot be paired."""
    geo = pair_geometry(curve_a, curve_b)
    if geo is None:
        return None
    along = geo['along']
    offset = geo['offset']
    if along > max_search:
        return None
    if offset < CONNECTOR_TOL or offset > max_search:
        return None

    # With trimming allowed the trays may already overlap and still be
    # connectable, so judge on the room the offset would actually get.
    low, high = room_for_offset(geo, trim, min_leg)
    if high - low <= MIN_SEGMENT:
        return None
    if along < -max_search:
        return None

    # How far the partner sits sideways in plan, as opposed to above/below.
    perp = geo['perp']
    lateral = DB.XYZ(perp.X, perp.Y, 0).GetLength()
    return abs(along) + LATERAL_PENALTY * lateral


def auto_pair(elements, max_search, trim=True, min_leg=MIN_SEGMENT):
    """Greedily match each tray with its best partner. Returns (pairs, left)."""
    curves = []
    usable = []
    for elem in elements:
        curve = straight_curve(elem)
        if curve is None:
            continue
        usable.append(elem)
        curves.append(curve)

    candidates = []
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            score = pair_score(curves[i], curves[j], max_search, trim, min_leg)
            if score is not None:
                candidates.append((score, i, j))
    candidates.sort()

    taken = set()
    pairs = []
    for _, i, j in candidates:
        if i in taken or j in taken:
            continue
        taken.add(i)
        taken.add(j)
        pairs.append((usable[i], usable[j]))

    unmatched = [usable[k] for k in range(len(usable)) if k not in taken]
    return pairs, unmatched


# ---------------------------------------------------------------------------
# CONNECTORS
# ---------------------------------------------------------------------------

def end_connector_at(mep_curve, xyz):
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


def release_end(element, xyz):
    conn = end_connector_at(element, xyz)
    if conn is None:
        return
    for ref in list(conn.AllRefs):
        if ref.ConnectorType == DB.ConnectorType.Logical:
            continue
        owner = ref.Owner
        if owner is None or owner.Id == element.Id:
            continue
        try:
            conn.DisconnectFrom(ref)
        except Exception as err:
            logger.debug('Disconnect failed: %s', err)


# ---------------------------------------------------------------------------
# THE CONNECTION
# ---------------------------------------------------------------------------

def build_transition(tray_a, tray_b, angle_deg, placement, allowed=None,
                     trim=True, min_leg=MIN_SEGMENT):
    """Build one transition. Returns (ok, message, angle_used)."""
    curve_a = straight_curve(tray_a)
    curve_b = straight_curve(tray_b)
    if curve_a is None or curve_b is None:
        return False, 'both elements must be straight segments', None

    radius = bend_radius_of(tray_a)
    solved = solve_transition(curve_a, curve_b, angle_deg, placement,
                              allowed, trim, min_leg, radius,
                              FITTING_CLEARANCE)
    if isinstance(solved, str):
        return False, solved, None

    release_end(tray_a, solved['point_a'])
    release_end(tray_b, solved['point_b'])

    # The sloped piece is a copy of the first tray, so it keeps the type,
    # width, height and all the other instance parameters.
    new_ids = DB.ElementTransformUtils.CopyElement(doc, tray_a.Id, DB.XYZ.Zero)
    if not new_ids:
        return False, 'could not create the sloped segment', None
    ramp = doc.GetElement(list(new_ids)[0])

    tray_a.Location.Curve = DB.Line.CreateBound(solved['fixed_a'],
                                                solved['kink_a'])
    tray_b.Location.Curve = DB.Line.CreateBound(solved['kink_b'],
                                                solved['fixed_b'])
    ramp.Location.Curve = DB.Line.CreateBound(solved['kink_a'],
                                              solved['kink_b'])
    doc.Regenerate()

    made = 0
    problems = []
    for element, kink in ((tray_a, solved['kink_a']),
                          (tray_b, solved['kink_b'])):
        conn_one = end_connector_at(element, kink)
        conn_two = end_connector_at(ramp, kink)
        if conn_one is None or conn_two is None:
            problems.append('no connector at a kink point')
            continue
        try:
            doc.Create.NewElbowFitting(conn_one, conn_two)
            made += 1
        except Exception as err:
            logger.debug('Elbow failed: %s', err)
            problems.append(str(err).split('\n')[0])

    doc.Regenerate()

    message = '{0:.0f} deg, offset {1}, run {2} of {3} available'.format(
        solved['angle'], fmt(solved['offset']), fmt(solved['run']),
        fmt(solved['room']))
    if made == 2:
        return True, message, solved['angle']

    detail = '{0:.0f} deg would fit geometrically but Revit could not place ' \
             '{1} of the 2 elbows ({2})' \
        .format(solved['angle'], 2 - made,
                '; '.join(problems) or 'unknown reason')
    if ROLLBACK_IF_NO_FITTING:
        # Roll the whole pair back rather than leave open tray ends behind.
        raise FittingError(detail)
    return True, message + ' - WARNING, only {0} of 2 elbows created' \
        .format(made), solved['angle']


def build_many(pairs, angle_deg, placement, allowed=None, trim=True,
               min_leg=MIN_SEGMENT):
    """Build every pair under one undo step.

    Returns (built, failures, angle_tally).
    """
    built = 0
    failures = []
    tally = {}
    group = DB.TransactionGroup(doc, 'Connect offset trays')
    group.Start()
    try:
        for tray_a, tray_b in pairs:
            # The try has to sit OUTSIDE the transaction: catching the error
            # inside would let the transaction commit the half-built joint.
            # Letting it escape makes pyRevit roll that transaction back.
            try:
                with revit.Transaction('Connect offset trays'):
                    ok, message, used = build_transition(
                        tray_a, tray_b, angle_deg, placement, allowed,
                        trim, min_leg)
            except FittingError as err:
                ok = False
                used = None
                message = str(err) + ' - pair rolled back, nothing changed'
            except Exception as err:
                ok = False
                used = None
                message = str(err).split('\n')[0]
            label = '{0} + {1}'.format(output.linkify(tray_a.Id),
                                       output.linkify(tray_b.Id))
            if ok:
                built += 1
                if used is not None:
                    key = '{0:.0f}'.format(used)
                    tally[key] = tally.get(key, 0) + 1
                print('{0}: {1}'.format(label, message))
            else:
                failures.append((tray_a, tray_b, message))
                print('{0}: skipped - {1}'.format(label, message))
        group.Assimilate()
    except Exception:
        group.RollBack()
        raise
    return built, failures, tally


# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------

def ask_angle(default_angle):
    """Returns None (cancelled), the string 'auto', or a fixed angle."""
    choice = forms.CommandSwitchWindow.show(
        ANGLE_CHOICES, message='Angle of the offset?')
    if choice is None:
        return None
    if choice == ANGLE_AUTO:
        return 'auto'
    if choice != ANGLE_OTHER:
        return float(choice)

    answer = forms.ask_for_string(
        default=str(default_angle),
        prompt='Angle in degrees (above 0, up to 90):',
        title='Connect Offset Trays')
    if answer is None:
        return None
    try:
        angle = float(answer.replace(',', '.'))
    except ValueError:
        forms.alert('"{}" is not a number.'.format(answer))
        return None
    if angle <= 0 or angle > 90:
        forms.alert('The angle has to be above 0 and no more than 90.')
        return None
    return angle


def edit_settings(pairing, placement, search, angles_text, trim_text, leg):
    chosen_pairing = forms.CommandSwitchWindow.show(
        [PAIR_GROUP, PAIR_VIEW, PAIR_MANUAL],
        message='How should the trays be paired up?')
    if chosen_pairing is None:
        return None

    chosen_trim = forms.CommandSwitchWindow.show(
        [TRIM_YES, TRIM_NO],
        message='Can the offset eat into the trays themselves?')
    if chosen_trim is None:
        return None

    new_leg = leg
    if chosen_trim == TRIM_YES:
        leg_answer = forms.ask_for_string(
            default=str(leg),
            prompt='Shortest straight piece to leave on each tray, in {}:'
                   .format(unit_label()),
            title='Connect Offset Trays')
        if leg_answer is None:
            return None
        try:
            new_leg = float(leg_answer.replace(',', '.'))
        except ValueError:
            forms.alert('"{}" is not a number.'.format(leg_answer))
            return None

    chosen_placement = forms.CommandSwitchWindow.show(
        [PLACE_CENTRE, PLACE_FIRST, PLACE_SECOND],
        message='Where should each offset sit along the run?')
    if chosen_placement is None:
        return None

    answer = forms.ask_for_string(
        default=str(angles_text),
        prompt='Angles the automatic mode may use, best first - the first '
               'one that fits the space is the one used:',
        title='Connect Offset Trays')
    if answer is None:
        return None
    if not parse_angles(answer):
        forms.alert('That is not a usable list of angles - keeping "{}".'
                    .format(angles_text))
        answer = angles_text

    new_search = search
    if chosen_pairing != PAIR_MANUAL:
        gap_answer = forms.ask_for_string(
            default=str(search),
            prompt='Largest gap between two trays that should still be '
                   'paired, in {}:'.format(unit_label()),
            title='Connect Offset Trays')
        if gap_answer is None:
            return None
        try:
            new_search = float(gap_answer.replace(',', '.'))
        except ValueError:
            forms.alert('"{}" is not a number.'.format(gap_answer))
            return None

    save_options(pairing=chosen_pairing, placement=chosen_placement,
                 search=new_search, angles=answer, trim=chosen_trim,
                 leg=new_leg)
    return (chosen_pairing, chosen_placement, new_search, answer,
            chosen_trim, new_leg)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

pairing = read_option('pairing', PAIR_GROUP)
if pairing not in (PAIR_GROUP, PAIR_VIEW, PAIR_MANUAL):
    pairing = PAIR_GROUP

placement = read_option('placement', PLACE_CENTRE)
if placement not in (PLACE_CENTRE, PLACE_FIRST, PLACE_SECOND):
    placement = PLACE_CENTRE

trim_text = read_option('trim', TRIM_YES)
if trim_text not in (TRIM_YES, TRIM_NO):
    trim_text = TRIM_YES

default_leg = round(from_internal(DEFAULT_MIN_LEG_FT), 1)
try:
    leg_display = float(read_option('leg', default_leg))
except (TypeError, ValueError):
    leg_display = default_leg

default_search = round(from_internal(DEFAULT_SEARCH_FT), 1)
try:
    search_display = float(read_option('search', default_search))
except (TypeError, ValueError):
    search_display = default_search

angles_text = read_option('angles', DEFAULT_ANGLES)
if not parse_angles(angles_text):
    angles_text = DEFAULT_ANGLES

if __shiftclick__:  # noqa: F821
    result = edit_settings(pairing, placement, search_display, angles_text,
                           trim_text, leg_display)
    if result is None:
        script.exit()
    (pairing, placement, search_display, angles_text,
     trim_text, leg_display) = result

max_search = to_internal(search_display)
allowed = parse_angles(angles_text)
trim = (trim_text == TRIM_YES)
min_leg = max(to_internal(leg_display), MIN_SEGMENT)

try:
    last_angle = float(read_option('angle', 45.0))
except (TypeError, ValueError):
    last_angle = 45.0

choice = ask_angle(last_angle)
if choice is None:
    script.exit()

automatic = (choice == 'auto')
angle = None if automatic else choice
if not automatic:
    save_options(angle=angle)

if automatic:
    angle_label = 'the best fit from {} degrees' \
        .format(', '.join(['{0:g}'.format(a) for a in allowed]))
else:
    angle_label = '{0:.0f} degrees'.format(angle)


def report_angles(tally):
    if not tally:
        return
    parts = ['{0} x {1} deg'.format(count, key)
             for key, count in sorted(tally.items(), key=lambda kv: float(kv[0]))]
    print('Angles used: ' + ', '.join(parts))


# --- manual mode -----------------------------------------------------------

preselection = dedupe(preselected_elements())

if pairing == PAIR_MANUAL and len(preselection) < 2:
    print('Connecting picked pairs using {0}. {1}.'
          .format(angle_label, placement))
    total = 0
    all_tally = {}
    while True:
        first = pick_tray('Pick the FIRST tray (ESC to finish)')
        if first is None:
            break
        second = pick_tray('Pick the SECOND tray, at the other level')
        if second is None:
            break
        if first.Id == second.Id:
            forms.alert('Those are the same tray.')
            continue
        done, failures, tally = build_many([(first, second)], angle,
                                           placement, allowed, trim, min_leg)
        total += done
        for key, count in tally.items():
            all_tally[key] = all_tally.get(key, 0) + count
        for _, _, message in failures:
            forms.alert('Could not connect these two: ' + message)
    if total:
        print('Done - {} connection(s) built.'.format(total))
        report_angles(all_tally)
    script.exit()

# --- batch modes -----------------------------------------------------------

if len(preselection) >= 2:
    elements = preselection
    source = '{} pre-selected tray(s)'.format(len(elements))
elif pairing == PAIR_VIEW:
    elements = dedupe(view_elements())
    source = '{} tray(s) in this view'.format(len(elements))
else:
    elements = dedupe(pick_trays(
        'Select the trays on both levels, then click Finish'))
    source = '{} selected tray(s)'.format(len(elements))

if len(elements) < 2:
    forms.alert('Pick at least two trays.', exitscript=True)

pairs, unmatched = auto_pair(elements, max_search, trim, min_leg)
if not pairs:
    forms.alert('No trays could be paired up. They may not be parallel, may '
                'already be in line, or may be further apart than the {} '
                'search distance (Shift+Click to change it).'
                .format(fmt(max_search)), exitscript=True)

question = 'Found {0} pair(s) from {1}, using {2}.' \
    .format(len(pairs), source, angle_label)
if unmatched:
    question += '\n{} tray(s) had no partner and will be left alone.' \
        .format(len(unmatched))
question += '\n\nBuild them?'

if not forms.alert(question, title='Connect Offset Trays', yes=True, no=True):
    script.exit()

print('Connecting {0} pair(s) using {1}. {2}.'
      .format(len(pairs), angle_label, placement))

built, failures, tally = build_many(pairs, angle, placement, allowed,
                                    trim, min_leg)

print('Done - {0} of {1} pair(s) connected.'.format(built, len(pairs)))
report_angles(tally)
if failures:
    print('{} pair(s) were skipped, see the reasons above.'
          .format(len(failures)))
if unmatched:
    print('{0} tray(s) had no partner: {1}'
          .format(len(unmatched),
                  ', '.join([output.linkify(e.Id) for e in unmatched])))
