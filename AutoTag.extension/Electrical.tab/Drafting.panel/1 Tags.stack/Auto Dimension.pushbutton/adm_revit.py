# -*- coding: utf-8 -*-
"""Auto Dimension - everything that touches the Revit model.

counts(), dimension_types() only read. create() is the only function that changes the
model: it creates dimensions in the active view (one step for Undo).

What is dimensioned
  along a row of equipment (plan and section):   wall | equipment | gap | equipment | ... | wall
  in sections and elevations also:               the height of the equipment, and level -> top
The dimensions are attached to the Left / Right / Front / Back / Top / Bottom reference planes
of the families; where a family has none, to its outer faces.

Order of work (the sure things first, so that an attempt Revit rejects cannot spoil them):
  1. the chain along each row, equipment only
  2. the heights
  3. last, the walls: each wall is first tried with one small dimension; a wall that Revit
     accepts is then added to the chain.
"""
import codecs
import math
import os

from Autodesk.Revit import DB

import adm_layout

MM = 1.0 / 304.8
DETAILS_PATH = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'AutoDimension', 'last_run.txt')
DETAILS = []                              # what the last run did, line by line (written to DETAILS_PATH)


def note(text):
    try:
        DETAILS.append(u'{0}'.format(text))
    except Exception:
        pass


def _stable(doc, reference):
    """How Revit writes the reference, for the details file."""
    try:
        return reference.ConvertToStableRepresentation(doc)
    except Exception:
        return u'{0}'.format(reference)


def write_details():
    """Save the details of the run; returns the path, or None when that failed."""
    try:
        folder = os.path.dirname(DETAILS_PATH)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with codecs.open(DETAILS_PATH, 'w', 'utf-8-sig') as handle:
            handle.write(u'\r\n'.join(DETAILS))
        return DETAILS_PATH
    except Exception:
        return None
CATEGORIES = [('OST_ElectricalEquipment', u'Electrical Equipment'), ('OST_ElectricalFixtures', u'Electrical Fixtures')]
ROW_GAP = 150.0 * MM                      # equipment this far apart across the row still belongs to it
WALL_REACH = 15000.0 * MM                 # a wall farther than this from the row does not end its chain
ALIGNED = 0.985                           # cosine: a reference plane within ~10 degrees of square to the dimension
WALL_SQUARE = 0.02                        # a wall may be this much (sine, ~1 degree) off square to the row
STEP_MM = 8.0                             # distance between two dimension lines, on the sheet


class Frame(object):
    """The view as a flat sheet: u to the right, v upwards."""

    def __init__(self, view):
        self.o, self.r, self.up = view.Origin, view.RightDirection, view.UpDirection
        self.is_plan = abs(self.up.Z) < 0.5 and abs(self.r.Z) < 0.5
        self.plan_z = self.o.Z
        try:
            self.plan_z = view.GenLevel.Elevation
        except Exception:
            pass

    def uv(self, p):
        dx, dy, dz = p.X - self.o.X, p.Y - self.o.Y, p.Z - self.o.Z
        return (dx * self.r.X + dy * self.r.Y + dz * self.r.Z, dx * self.up.X + dy * self.up.Y + dz * self.up.Z)

    def xyz(self, u, v):
        return DB.XYZ(self.o.X + u * self.r.X + v * self.up.X, self.o.Y + u * self.r.Y + v * self.up.Y,
                      self.o.Z + u * self.r.Z + v * self.up.Z)


def view_problem(view):
    """Why dimensions cannot be placed in this view, or None."""
    if isinstance(view, DB.View3D):
        return u'Open a plan, section or elevation view. Dimensions are not placed in 3D views.'
    if isinstance(view, (DB.ViewSheet, DB.ViewSchedule)) or view.IsTemplate:
        return u'Open the layout view itself (not a sheet, schedule or template).'
    frame = Frame(view)
    if not frame.is_plan and abs(view.RightDirection.Z) > 0.1:
        return u'This view is tilted. Open a plan, or a vertical section or elevation.'
    return None


def dimension_types(doc):
    """[(name, DimensionType)] of the linear dimension types of the project, by name."""
    found = []
    for item in DB.FilteredElementCollector(doc).OfClass(DB.DimensionType):
        try:
            if item.StyleType != DB.DimensionStyleType.Linear:
                continue
            name = item.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME).AsString()
        except Exception:
            continue
        if name:
            found.append((name, item))
    return sorted(found, key=lambda pair: pair[0].lower())


def instances(doc, view, category, selected_ids=None):
    """The family instances of the category that are seen in the view (only the selected ones, when given)."""
    bic = getattr(DB.BuiltInCategory, category)
    found = []
    for element in DB.FilteredElementCollector(doc, view.Id).OfCategory(bic).WhereElementIsNotElementType():
        if not isinstance(element, DB.FamilyInstance):
            continue
        if selected_ids is not None and element.Id.Value not in selected_ids:
            continue
        found.append(element)
    return found


def counts(doc, view, selected_ids=None):
    """{category: number of instances in the view}"""
    return dict((category, len(instances(doc, view, category, selected_ids))) for category, _ in CATEGORIES)


# ---------------------------------------------------------------------------
# geometry of one piece of equipment
# ---------------------------------------------------------------------------
def _corners(box, transform=None):
    points = [DB.XYZ(x, y, z) for x in (box.Min.X, box.Max.X) for y in (box.Min.Y, box.Max.Y) for z in (box.Min.Z, box.Max.Z)]
    return [transform.OfPoint(p) for p in points] if transform is not None else points


def _solid_points(items, points, depth=0):
    """The corner points of every solid, as it stands in the model."""
    for item in items:
        if isinstance(item, DB.GeometryInstance) and depth < 4:
            _solid_points(item.GetInstanceGeometry(), points, depth + 1)
            continue
        edges = getattr(item, 'Edges', None)
        if edges is None:
            continue
        for edge in edges:
            for point in edge.Tessellate():
                points.append(point)


def footprint(instance, view):
    """Points that span the equipment as it stands in the model (the corners of its solids);
    None when it has no geometry. Their extent along any direction is the extent of the equipment."""
    try:
        options = DB.Options()
        options.View = view
        points = []
        _solid_points(instance.get_Geometry(options), points)
        if len(points) >= 4:
            return points
    except Exception:
        pass
    try:                                                    # no solids: the box Revit keeps for the element
        box = instance.get_BoundingBox(None)
        return _corners(box) if box is not None else None
    except Exception:
        return None


def _dot(vector, d):
    return vector.X * d[0] + vector.Y * d[1] + vector.Z * d[2]


def _named(instance, name):
    try:
        found = list(instance.GetReferences(getattr(DB.FamilyInstanceReferenceType, name)))
    except Exception:
        found = []
    return found[0] if found else None


def _flat_faces(element, d, transform=None):
    """[(position along d, reference)] of the flat faces of the element that are square to d."""
    found = []
    options = DB.Options()
    options.ComputeReferences = True

    def walk(items, moved):
        for item in items:
            if isinstance(item, DB.GeometryInstance):
                step = item.Transform if moved is None else moved.Multiply(item.Transform)
                walk(item.GetSymbolGeometry(), step)
                continue
            faces = getattr(item, 'Faces', None)
            if faces is None:
                continue
            for face in faces:
                normal = getattr(face, 'FaceNormal', None)
                if normal is None or face.Reference is None:
                    continue
                origin = face.Origin
                if moved is not None:
                    normal, origin = moved.OfVector(normal), moved.OfPoint(origin)
                if abs(_dot(normal, d)) >= ALIGNED:
                    found.append((_dot(origin, d), face.Reference))
    walk(element.get_Geometry(options), transform)
    return found


def instance_reference(instance, reference):
    """The reference to a face of THIS instance.

    The faces of a family instance are read from its family type, and Revit hands out their
    references without saying which instance is meant ("type:face:SURFACE"). For one piece alone
    that is enough, but two pieces of the same type then have the very same references, and a
    dimension through both is refused ("references are or have become invalid"). Written as
    "instance:0:INSTANCE:type:face:SURFACE" the reference names the face of this one instance.
    Returns the reference unchanged when that cannot be done."""
    try:
        doc = instance.Document
        stable = reference.ConvertToStableRepresentation(doc)
        if ':INSTANCE:' in stable:
            return reference
        own = DB.Reference.ParseFromStableRepresentation(doc, u'{0}:0:INSTANCE:{1}'.format(instance.UniqueId, stable))
        return own if own is not None else reference
    except Exception:
        return reference


def _face_references(instance, d, view):
    """The two outermost flat faces square to d, as (reference, reference, plain pair or None);
    None when there are none. The references name this instance; the plain pair is what Revit
    handed out, kept as a second choice."""
    try:
        faces = sorted(_flat_faces(instance, d), key=lambda pair: pair[0])
    except Exception:
        return None
    if len(faces) >= 2 and faces[-1][0] - faces[0][0] > 1e-6:
        low, high = faces[0][1], faces[-1][1]
        own_low, own_high = instance_reference(instance, low), instance_reference(instance, high)
        changed = own_low is not low or own_high is not high
        return own_low, own_high, ((low, high) if changed else None)
    return None


CENTRE = u'centre'                        # how: the piece is dimensioned from its centre plane (one reference)


def _flat(pairs):
    """The references of the pairs in order; a piece dimensioned from its centre gives one reference, not two."""
    out = []
    for pair in pairs:
        out.append(pair[0])
        if pair[1] is not pair[0]:
            out.append(pair[1])
    return out


def centre_reference(instance, d):
    """The centre reference plane of the family that is square to d = (x, y, z), or None:
    Center (Left/Right), Center (Front/Back) or Center (Elevation)."""
    try:
        axes = [(instance.HandOrientation, 'CenterLeftRight'), (instance.FacingOrientation, 'CenterFrontBack'),
                (DB.XYZ(0, 0, 1), 'CenterElevation')]
    except Exception:
        axes = [(DB.XYZ(0, 0, 1), 'CenterElevation')]
    for axis, name in axes:
        if abs(_dot(axis, d)) >= ALIGNED:
            return _named(instance, name)
    return None


def side_references(instance, d, view, centre=False):
    """(reference, reference, how, second choice) of the two sides of the equipment measured along
    d = (x, y, z); None when it has none. First choice: the reference planes of the family; otherwise
    its outer faces. second choice: another pair of references for the same two sides, or None.
    centre=True (fixtures): the centre plane of the family instead - the same reference twice, how = CENTRE;
    when the family has no centre plane, its sides as for equipment."""
    if centre:
        middle = centre_reference(instance, d)
        if middle is not None:
            return middle, middle, CENTRE, None
    try:
        axes = [(instance.HandOrientation, 'Left', 'Right'), (instance.FacingOrientation, 'Front', 'Back'),
                (DB.XYZ(0, 0, 1), 'Bottom', 'Top')]
    except Exception:
        axes = [(DB.XYZ(0, 0, 1), 'Bottom', 'Top')]
    for axis, first, second in axes:
        if abs(_dot(axis, d)) >= ALIGNED:
            a, b = _named(instance, first), _named(instance, second)
            if a is not None and b is not None:
                return a, b, u'reference planes', None
            break
    faces = _face_references(instance, d, view)
    if faces is not None:
        return faces[0], faces[1], u'faces', faces[2]
    return None


# ---------------------------------------------------------------------------
# walls that stand across a row
# ---------------------------------------------------------------------------
def _walls(doc, view, include_links):
    """[(wall, link instance or None)] seen in the view."""
    found = [(wall, None) for wall in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Wall)]
    if include_links:
        for link in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.RevitLinkInstance):
            try:
                if link.GetLinkDocument() is None:
                    continue
                try:
                    walls = list(DB.FilteredElementCollector(doc, view.Id, link.Id).OfClass(DB.Wall))
                except Exception:                           # an older Revit: every wall of the link
                    walls = list(DB.FilteredElementCollector(link.GetLinkDocument()).OfClass(DB.Wall))
                found.extend((wall, link) for wall in walls)
            except Exception:
                pass
    return found


def link_reference(doc, reference, link):
    """(first choice, second choice) of a reference to a face in a linked model, for a dimension in this model.

    Revit hands out link references in a form that dimensions often refuse
    ("uid:RVTLINK/uid:element:index:SURFACE"). Written as "uid:0:RVTLINK:element:index:SURFACE"
    the same face is accepted. That form is tried first; the plain one is kept as second choice."""
    plain = reference.CreateLinkReference(link)
    try:
        tokens = plain.ConvertToStableRepresentation(doc).split(':')
        if len(tokens) >= 5 and tokens[1] != '0':
            rewritten = u'{0}:0:RVTLINK:{1}'.format(tokens[0], u':'.join(tokens[2:]))
            better = DB.Reference.ParseFromStableRepresentation(doc, rewritten)
            if better is not None:
                return better, plain
    except Exception:
        pass
    return plain, None


def wall_faces(doc, view, direction, include_links=True):
    """[adm_layout.WallFace] of the walls standing across `direction` (dx, dy).
    Each face remembers its wall, so that wall_candidates() can give the references to try."""
    d = (direction[0], direction[1], 0.0)
    n = (-direction[1], direction[0], 0.0)
    out = []
    for wall, link in _walls(doc, view, include_links):
        try:
            curve = wall.Location.Curve
            transform = link.GetTotalTransform() if link is not None else None
            a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
            if transform is not None:
                a, b = transform.OfPoint(a), transform.OfPoint(b)
            length = math.hypot(b.X - a.X, b.Y - a.Y)
            if length < 1e-6:
                continue
            if abs(((b.X - a.X) * d[0] + (b.Y - a.Y) * d[1]) / length) > WALL_SQUARE:
                continue                                    # the wall runs along the row, or askew: it cannot end the chain
            lats = sorted((_dot(a, n), _dot(b, n)))
            box = wall.get_BoundingBox(None)
            zs = [p.Z for p in _corners(box, transform)] if box is not None else [-1e9, 1e9]
            for shell in (DB.ShellLayerType.Interior, DB.ShellLayerType.Exterior):
                for reference in DB.HostObjectUtils.GetSideFaces(wall, shell):
                    face = wall.GetGeometryObjectFromReference(reference)
                    origin = face.Origin
                    if transform is not None:
                        origin = transform.OfPoint(origin)
                    found = adm_layout.WallFace(reference, _dot(origin, d), lats[0], lats[1], min(zs), max(zs))
                    found.wall, found.link, found.transform = wall, link, transform
                    found.linked = link is not None
                    out.append(found)
        except Exception:
            pass                                            # curved or curtain wall, or a wall without side faces
    return out


def wall_candidates(doc, face, direction):
    """[(how, reference)]: the ways to refer to this wall face in a dimension, the likeliest first.
    Revit does not accept every form for every wall (above all for walls of linked models),
    so create() tries them one after the other."""
    d = (direction[0], direction[1], 0.0)
    bases = [(u'side face', face.key)]
    try:                                                    # the same face, taken from the solid of the wall
        near = [pair for pair in _flat_faces(face.wall, d, face.transform) if abs(pair[0] - face.s) < 0.005]
        if near:
            bases.append((u'face of the solid', near[0][1]))
    except Exception:
        pass
    out = []
    for how, reference in bases:
        if face.link is None:
            out.append((how, reference))
            continue
        try:
            first, second = link_reference(doc, reference, face.link)
        except Exception:
            continue
        out.append((how + u', link form 1', first))
        if second is not None:
            out.append((how + u', link form 2', second))
    return out


# ---------------------------------------------------------------------------
# creating the dimensions - the only place where the model is changed
# ---------------------------------------------------------------------------
class Options(object):
    def __init__(self, chain=True, walls=True, heights=True, above_level=True, links=True, offset_mm=10.0, fixture_centres=True):
        self.chain, self.walls, self.heights, self.above_level, self.links, self.offset_mm = chain, walls, heights, above_level, links, offset_mm
        self.fixture_centres = fixture_centres            # electrical fixtures: from their centre, not from their sides


class Report(object):
    def __init__(self):
        self.dimensions = 0
        self.rows = 0
        self.wall_ends = 0                # chain ends that reach a wall
        self.no_wall = 0                  # chain ends without a wall across the row
        self.linked_walls = 0             # walls found in linked models
        self.wall_refused = []            # (wall is linked, Revit's reason) for walls Revit did not accept
        self.chain_broken = None          # Revit's reason when a row could not be one chain
        self.centres = 0                  # fixtures dimensioned from their centre
        self.no_centre = 0                # fixtures whose family has no centre reference plane: from their sides
        self.gaps = 0                     # gaps between neighbours dimensioned on their own
        self.gaps_missing = 0             # gaps Revit did not take either
        self.details = None               # path of the file with the details of the run
        self.hidden = 0                   # equipment behind another one: left out of the chain
        self.no_reference = []            # names of equipment that could not be referenced
        self.by_faces = 0                 # equipment dimensioned to faces (the family has no reference planes)
        self.failed = []                  # what Revit refused, with its reason
        self.error = None

    def lines(self):
        out = [u'{0} dimension(s) created for {1} row(s) of equipment.'.format(self.dimensions, self.rows)]
        if self.wall_ends or self.no_wall or self.wall_refused:
            out.append(u'Walls: {0} chain end(s) dimensioned to a wall; {1} have no wall square across the row within 15 m.'.format(self.wall_ends, self.no_wall))
            if self.linked_walls:
                out.append(u'{0} of the walls found are in linked models.'.format(self.linked_walls))
        if self.wall_refused:
            linked = len([1 for is_linked, _ in self.wall_refused if is_linked])
            out.append(u'{0} wall(s) were found but Revit did not accept a dimension to them ({1} in linked models), in any of the forms tried. '
                       u'The chain stops at the equipment there. Revit said: {2}'.format(len(self.wall_refused), linked, self.wall_refused[0][1]))
        if self.chain_broken:
            out.append(u'A row could not be dimensioned as one chain, so it was built up piece by piece, with {0} gap(s) between pieces dimensioned separately '
                       u'on the same line. Revit said: {1}'.format(self.gaps, self.chain_broken))
            if self.gaps_missing:
                out.append(u'{0} gap(s) between pieces could not be dimensioned at all.'.format(self.gaps_missing))
        if self.centres:
            out.append(u'{0} fixture(s) are dimensioned from their centre.'.format(self.centres))
        if self.no_centre:
            out.append(u'{0} fixture(s) have no centre reference plane in their family (Center Left/Right, Front/Back or Elevation) '
                       u'and were dimensioned from their sides instead.'.format(self.no_centre))
        if self.hidden:
            out.append(u'{0} piece(s) of equipment stand behind another one in this view and were left out.'.format(self.hidden))
        if self.by_faces:
            out.append(u'{0} piece(s) were dimensioned to their outer faces (their families have no Left/Right reference planes). That is normal and needs no action.'.format(self.by_faces))
        if self.no_reference:
            names = sorted(set(self.no_reference))
            out.append(u'{0} piece(s) could not be dimensioned (turned against the row, or no flat faces): {1}'.format(
                len(self.no_reference), u', '.join(names[:6]) + (u' ...' if len(names) > 6 else u'')))
        if self.failed:
            out.append(u'Revit refused {0} dimension(s):'.format(len(self.failed)))
            out.extend(u'   ' + reason for reason in sorted(set(self.failed))[:5])
        return out


def _name(instance):
    try:
        return u'{0} (id {1})'.format(instance.Name, instance.Id.Value)
    except Exception:
        return u'id {0}'.format(instance.Id.Value)


class _Catcher(DB.IFailuresPreprocessor):
    """Listens to what Revit objects to when a dimension is committed.
    Warnings are dismissed; an error undoes this one dimension only, and its text is kept for the report."""

    def __init__(self):
        self.errors = []

    def PreprocessFailures(self, accessor):
        result = DB.FailureProcessingResult.Continue
        for failure in accessor.GetFailureMessages():
            try:
                if failure.GetSeverity() == DB.FailureSeverity.Warning:
                    accessor.DeleteWarning(failure)
                    continue
                self.errors.append(failure.GetDescriptionText())
            except Exception:
                self.errors.append(u'Revit reported an error')
            result = DB.FailureProcessingResult.ProceedWithRollBack
        return result


def _try(doc, view, dim_type, references, p1, p2, what=u'dimension'):
    """One dimension through the references, along the line p1-p2, in a transaction of its own:
    if Revit rejects it - when it is made or when it is committed - only this dimension is undone.
    Returns (id of the dimension, None) when it is in the model, else (None, Revit's reason)."""
    made, reason = _try_quiet(doc, view, dim_type, references, p1, p2)
    note(u'  try {0}: {1} reference(s) -> {2}'.format(what, len(references), u'OK' if made is not None else u'REFUSED: {0}'.format(reason)))
    return made, reason


def _try_quiet(doc, view, dim_type, references, p1, p2):
    if len(references) < 2:
        return None, u'fewer than two references'
    array = DB.ReferenceArray()
    for reference in references:
        array.Append(reference)
    catcher = _Catcher()
    transaction = DB.Transaction(doc, 'Auto Dimension')
    transaction.Start()
    try:
        handling = transaction.GetFailureHandlingOptions()
        handling.SetFailuresPreprocessor(catcher)
        handling.SetClearAfterRollback(True)
        handling.SetForcedModalHandling(False)
        transaction.SetFailureHandlingOptions(handling)
        line = DB.Line.CreateBound(p1, p2)
        made = doc.Create.NewDimension(view, line, array, dim_type) if dim_type is not None else doc.Create.NewDimension(view, line, array)
        if made is None:
            transaction.RollBack()
            return None, u'no dimension was made'
        made_id = made.Id
        status = transaction.Commit()
        if status == DB.TransactionStatus.Committed:
            return made_id, None
        return None, (catcher.errors[0] if catcher.errors else u'Revit undid the dimension ({0})'.format(status))
    except Exception as error:
        try:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
        except Exception:
            pass
        return None, (u'{0}'.format(error).splitlines() or [u'error'])[0]


def _remove(doc, element_id):
    """Take a dimension out again (a trial, or a chain that was replaced by a longer one)."""
    transaction = DB.Transaction(doc, 'Auto Dimension')
    transaction.Start()
    try:
        doc.Delete(element_id)
        transaction.Commit()
    except Exception:
        try:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
        except Exception:
            pass


def _dimension(doc, view, dim_type, references, p1, p2, report, what):
    """As _try, and counted in the report. True when Revit made the dimension."""
    made, reason = _try(doc, view, dim_type, references, p1, p2)
    if made is not None:
        report.dimensions += 1
        return True
    report.failed.append(u'{0}: {1}'.format(what, reason))
    return False


class _Group(object):
    """One dimension of a row: the pieces Revit accepted together."""

    def __init__(self, dim_id, members):
        self.dim_id = dim_id
        self.members = members            # [(part, (reference, reference))]

    def references(self):
        return _flat(pair for _, pair in self.members)


class _Chain(object):
    """The dimensions along one row as they stand in the model, for adding the walls at the end.
    Normally ONE group (one chain). Where Revit does not take all pieces together: several."""

    def __init__(self):
        self.groups = []
        self.row = None
        self.point = None                 # point(s, away) of its dimension line
        self.extent = None                # (smin, smax, lat_lo, lat_hi, zlo, zhi)

    @property
    def members(self):
        return sorted((m for g in self.groups for m in g.members), key=lambda m: m[0].smin)

    def group_of(self, part):
        for group in self.groups:
            if any(member[0] is part for member in group.members):
                return group
        return None


def _build_chain(doc, view, dim_type, chain, usable, pairs, offset, report, by_id, seconds=None):
    """The chain of the equipment of one row. If Revit does not take all pieces in one chain,
    the chain is built up piece by piece and a piece that does not fit gets its own dimension.
    seconds: {part key: second-choice pair of references}."""
    seconds = seconds or {}
    everything = _flat(pairs)
    smin, smax = min(p.smin for p in usable), max(p.smax for p in usable)
    if len(everything) < 2:                                 # one fixture alone: nothing to dimension yet - the walls may still be
        chain.groups = [_Group(None, list(zip(usable, pairs)))]
        return
    made, reason = _try(doc, view, dim_type, everything, chain.point(smin, offset), chain.point(smax, offset))
    if made is None and seconds:                            # the same chain with the second-choice references
        other = [seconds.get(part.key, pair) for part, pair in zip(usable, pairs)]
        made, _ = _try(doc, view, dim_type, _flat(other), chain.point(smin, offset), chain.point(smax, offset))
        if made is not None:
            pairs = other
    if made is not None:
        chain.groups = [_Group(made, list(zip(usable, pairs)))]
        report.dimensions += 1
        return
    if len(pairs) == 1:
        report.failed.append(u'{0}: {1}'.format(_name(by_id[usable[0].key]), reason))
        return
    report.chain_broken = reason
    # Revit does not take the whole row in one dimension. Each piece joins a chain that takes it,
    # or starts a new one; the result is as few dimensions as Revit allows, all on the same line.
    for part, pair in zip(usable, pairs):
        choices = [pair] + ([seconds[part.key]] if part.key in seconds else [])
        placed = False
        for group in reversed(chain.groups):
            for choice in choices:
                trial = group.members + [(part, choice)]
                references = _flat(one for _, one in trial)
                made, _ = _try(doc, view, dim_type, references, chain.point(trial[0][0].smin, offset), chain.point(part.smax, offset),
                               u'{0} joins a chain of {1}'.format(_name(by_id[part.key]), len(group.members)))
                if made is not None:
                    if group.dim_id is not None:
                        _remove(doc, group.dim_id)          # replaced by the longer chain
                    else:
                        report.dimensions += 1              # the group was a lone centre until now
                    group.dim_id, group.members, placed = made, trial, True
                    break
            if placed:
                break
        if placed:
            continue
        said = None
        if pair[0] is pair[1]:                              # a centre has no width: it waits for a neighbour or a wall
            chain.groups.append(_Group(None, [(part, pair)]))
            continue
        for choice in choices:                              # its own width, the start of a new chain
            own, said = _try(doc, view, dim_type, list(choice), chain.point(part.smin, offset), chain.point(part.smax, offset),
                             u'width of {0}'.format(_name(by_id[part.key])))
            if own is not None:
                chain.groups.append(_Group(own, [(part, choice)]))
                report.dimensions += 1
                placed = True
                break
        if not placed:
            report.failed.append(u'{0}: {1}'.format(_name(by_id[part.key]), said))
    # the gaps between neighbours that are not in the same dimension: one small dimension each,
    # from the facing sides of the two pieces, on the same line - it reads like a chain
    for left, right in zip(usable, usable[1:]):
        a, b = chain.group_of(left), chain.group_of(right)
        if a is None or b is None or a is b:
            continue
        if right.smin - left.smax < 0.003:
            continue                                        # the two pieces touch: no gap to dimension
        sides = []
        for part in (left, right):
            own = [pair for member, pair in chain.group_of(part).members if member is part][0]
            if own[0] is own[1]:                            # dimensioned from its centre
                sides.append((own[0], own[0], None))
                continue
            found = _face_references(by_id[part.key], (chain.row.direction[0], chain.row.direction[1], 0.0), view)
            sides.append(found)
        if sides[0] is None or sides[1] is None:
            report.gaps_missing += 1
            continue
        trials = [[sides[0][1], sides[1][0]]]
        if sides[0][2] is not None or sides[1][2] is not None:
            trials.append([(sides[0][2] or sides[0])[1], (sides[1][2] or sides[1])[0]])
        for references in trials:
            made, _ = _try(doc, view, dim_type, references, chain.point(left.smax, offset), chain.point(right.smin, offset),
                           u'gap {0} | {1}'.format(_name(by_id[left.key]), _name(by_id[right.key])))
            if made is not None:
                report.dimensions += 1
                report.gaps += 1
                break
        else:
            report.gaps_missing += 1


def _add_walls(doc, view, dim_type, chain, options, offset, report):
    """Last step: extend the chain of a row to the walls at its ends, where Revit accepts them."""
    direction = chain.row.direction
    before, after = adm_layout.pick_walls(wall_faces(doc, view, direction, options.links), chain.extent, WALL_REACH)
    report.no_wall += len([face for face in (before, after) if face is None])
    report.linked_walls += len([face for face in (before, after) if face is not None and face.linked])
    members = chain.members
    proven = {}
    for which, face, neighbour in (('before', before, members[0][1][0]), ('after', after, members[-1][1][1])):
        if face is None:
            continue
        reason = u'no usable reference'
        for how, reference in wall_candidates(doc, face, direction):
            # one small dimension from the wall to the nearest piece shows whether Revit takes this form
            middle = (chain.extent[0] + chain.extent[1]) / 2.0
            trial, said = _try(doc, view, dim_type, [reference, neighbour], chain.point(face.s, offset), chain.point(middle, offset))
            if trial is not None:
                _remove(doc, trial)
                proven[which] = (reference, face)
                break
            reason = said or reason
        if which not in proven:
            report.wall_refused.append((face.linked, reason))
    if not proven:
        return
    first, last = chain.group_of(members[0][0]), chain.group_of(members[-1][0])
    jobs = [(first, [w for w in ('before', 'after') if w in proven])] if first is last else \
           [(first, [w for w in ('before',) if w in proven]), (last, [w for w in ('after',) if w in proven])]
    for group, ends in jobs:
        if not ends:
            continue
        references = group.references()
        start, end = group.members[0][0].smin, group.members[-1][0].smax
        if 'before' in ends:
            references, start = [proven['before'][0]] + references, proven['before'][1].s
        if 'after' in ends:
            references, end = references + [proven['after'][0]], proven['after'][1].s
        made, reason = _try(doc, view, dim_type, references, chain.point(start, offset), chain.point(end, offset), u'chain with wall(s)')
        if made is not None:
            if group.dim_id is not None:
                _remove(doc, group.dim_id)                  # the dimension without the wall is replaced
            else:
                report.dimensions += 1
            group.dim_id = made
            report.wall_ends += len(ends)
        else:
            for which in ends:
                report.wall_refused.append((proven[which][1].linked, reason))


def _line_point(doc, frame, row, usable, by_id):
    """point(s, away): a point of the dimension line of a row - s along the row, `away` from the equipment.
    In a plan the line runs in front of the row; in a section below the floor level."""
    d2, n2 = row.direction, row.normal
    lat_lo, lat_hi = min(p.lat_lo for p in usable), max(p.lat_hi for p in usable)
    front = row.front()
    base = min(p.zlo for p in usable)
    if not frame.is_plan:
        for part in usable:
            try:
                base = min(base, doc.GetElement(by_id[part.key].LevelId).Elevation)
            except Exception:
                pass
    along = frame.o.X * d2[0] + frame.o.Y * d2[1]

    def point(s, away):
        if frame.is_plan:
            lat = (lat_hi + away) if front > 0 else (lat_lo - away)
            return DB.XYZ(d2[0] * s + n2[0] * lat, d2[1] * s + n2[1] * lat, frame.plan_z)
        return frame.xyz(s - along, (base - frame.o.Z) - away)
    return point


def create(doc, view, categories, dim_type, options, selected_ids=None):
    """Creates the dimensions as one step for Undo. categories: names from CATEGORIES. Returns a Report."""
    report = Report()
    frame = Frame(view)
    scale = float(view.Scale or 1)
    DETAILS[:] = []
    try:
        note(u'Auto Dimension - details of the last run')
        note(u'view: {0} | {1} | scale 1:{2}'.format(view.Name, u'plan' if frame.is_plan else u'section / elevation', view.Scale))
        note(u'view right ({0:.6f}, {1:.6f}, {2:.6f})  up ({3:.6f}, {4:.6f}, {5:.6f})'.format(frame.r.X, frame.r.Y, frame.r.Z, frame.up.X, frame.up.Y, frame.up.Z))
    except Exception:
        pass
    offset = options.offset_mm * MM * scale
    step = STEP_MM * MM * scale
    # every dimension has a small transaction of its own (see _try), so that one dimension
    # Revit rejects does not take the others with it; the group makes them ONE step for Undo
    undo_step = DB.TransactionGroup(doc, 'Auto Dimension')
    undo_step.Start()
    try:
        raw, by_id, fixtures = [], {}, set()               # fixtures: the pieces to dimension from their centre
        for category in categories:
            for instance in instances(doc, view, category, selected_ids):
                corners = footprint(instance, view)
                if not corners:
                    report.no_reference.append(_name(instance))
                    continue
                try:
                    hand, facing = instance.HandOrientation, instance.FacingOrientation
                    hand, facing = (hand.X, hand.Y), (facing.X, facing.Y)
                except Exception:
                    hand, facing = (1.0, 0.0), None
                zs = [p.Z for p in corners]
                by_id[instance.Id.Value] = instance
                if category == 'OST_ElectricalFixtures' and options.fixture_centres:
                    fixtures.add(instance.Id.Value)
                raw.append((instance.Id.Value, hand, [(p.X, p.Y) for p in corners], min(zs), max(zs), facing))

        if frame.is_plan:
            rows = adm_layout.group_rows(raw, ROW_GAP)
        else:                                               # a section: what stands side by side in the picture
            rows = [adm_layout.one_row(raw, (frame.r.X, frame.r.Y))] if raw else []

        chains = []
        for row in rows:
            kept, hidden = adm_layout.chain(row.parts)
            report.hidden += len(hidden)
            if not kept:
                continue
            report.rows += 1
            d2 = row.direction
            d3 = (d2[0], d2[1], 0.0)
            usable, pairs, seconds = [], [], {}
            for part in kept:
                found = side_references(by_id[part.key], d3, view, part.key in fixtures)
                if found is None:
                    report.no_reference.append(_name(by_id[part.key]))
                    continue
                report.by_faces += 1 if found[2] == u'faces' else 0
                if part.key in fixtures:
                    report.centres += 1 if found[2] == CENTRE else 0
                    report.no_centre += 0 if found[2] == CENTRE else 1
                usable.append(part)
                pairs.append((found[0], found[1]))
                try:
                    one = by_id[part.key]
                    note(u'piece {0} | along the row {1:.4f} .. {2:.4f} ft | height {3:.4f} .. {4:.4f} ft | {5}'.format(
                        _name(one), part.smin, part.smax, part.zlo, part.zhi, found[2]))
                    note(u'    hand ({0:.6f}, {1:.6f}, {2:.6f})  unique id {3}'.format(one.HandOrientation.X, one.HandOrientation.Y, one.HandOrientation.Z, one.UniqueId))
                    note(u'    side 1: {0}'.format(_stable(doc, found[0])))
                    note(u'    side 2: {0}'.format(_stable(doc, found[1])))
                except Exception:
                    pass
                if found[3] is not None:
                    seconds[part.key] = found[3]
            if not usable:
                continue

            chain = _Chain()
            chain.row = row
            chain.point = _line_point(doc, frame, row, usable, by_id)
            chain.extent = (min(p.smin for p in usable), max(p.smax for p in usable), min(p.lat_lo for p in usable),
                            max(p.lat_hi for p in usable), min(p.zlo for p in usable), max(p.zhi for p in usable))

            # ---- 1. the chain along the row: equipment | gap | equipment | ...
            if options.chain:
                _build_chain(doc, view, dim_type, chain, usable, pairs, offset, report, by_id, seconds)
                if chain.groups:
                    chain.extent = (min(p.smin for p, _ in chain.members), max(p.smax for p, _ in chain.members)) + chain.extent[2:]
                    chains.append(chain)

            # ---- 2. sections and elevations: the height of the equipment, and level -> top
            if not frame.is_plan and (options.heights or options.above_level):
                up = (0.0, 0.0, 1.0)
                along = frame.o.X * d2[0] + frame.o.Y * d2[1]
                for part, same in adm_layout.height_groups(usable):
                    instance = by_id[part.key]
                    found = side_references(instance, up, view, part.key in fixtures)
                    if found is None:
                        report.no_reference.append(_name(instance))
                        continue
                    u = part.smax - along + step * 0.6      # just right of the piece
                    v1, v2 = part.zlo - frame.o.Z, part.zhi - frame.o.Z
                    if found[2] == CENTRE:
                        # a fixture: its mounting height, from the level to its centre - it has no "height" to show
                        if options.above_level:
                            try:
                                level = doc.GetElement(instance.LevelId)
                                middle = (part.zlo + part.zhi) / 2.0
                                if level is not None and abs(level.Elevation - middle) > 1e-6:
                                    _dimension(doc, view, dim_type, [DB.Reference(level), found[0]],
                                               frame.xyz(u, level.Elevation - frame.o.Z), frame.xyz(u, middle - frame.o.Z), report,
                                               u'level to centre of ' + _name(instance))
                            except Exception as error:
                                report.failed.append(u'level to centre of {0}: {1}'.format(_name(instance), error))
                        continue
                    if options.heights:
                        made, said = _try(doc, view, dim_type, [found[0], found[1]], frame.xyz(u, v1), frame.xyz(u, v2))
                        if made is None and found[3] is not None:
                            made, _ = _try(doc, view, dim_type, list(found[3]), frame.xyz(u, v1), frame.xyz(u, v2))
                            if made is not None:
                                found = (found[3][0], found[3][1], found[2], None)
                        if made is not None:
                            report.dimensions += 1
                        else:
                            report.failed.append(u'height of {0}: {1}'.format(_name(instance), said))
                        u += step
                    if options.above_level:
                        try:
                            level = doc.GetElement(instance.LevelId)
                            if level is not None and abs(level.Elevation - part.zhi) > 1e-6:
                                _dimension(doc, view, dim_type, [DB.Reference(level), found[1]],
                                           frame.xyz(u, level.Elevation - frame.o.Z), frame.xyz(u, v2), report,
                                           u'level to top of ' + _name(instance))
                        except Exception as error:
                            report.failed.append(u'level to top of {0}: {1}'.format(_name(instance), error))

        # ---- 3. last: the walls at the ends of the chains
        if options.chain and options.walls:
            for chain in chains:
                try:
                    _add_walls(doc, view, dim_type, chain, options, offset, report)
                except Exception as error:
                    report.wall_refused.append((False, u'{0}'.format(error)))

        if report.dimensions:
            undo_step.Assimilate()
        else:
            undo_step.RollBack()
        for line in report.lines():
            note(line)
        report.details = write_details()
    except Exception as error:
        try:
            if undo_step.HasStarted() and not undo_step.HasEnded():
                undo_step.RollBack()
        except Exception:
            pass
        report.error = u'Nothing was created: {0}'.format(error)
        report.dimensions = 0
    return report
