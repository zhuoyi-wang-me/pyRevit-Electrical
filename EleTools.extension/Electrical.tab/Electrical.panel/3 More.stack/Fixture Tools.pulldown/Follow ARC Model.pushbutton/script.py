# -*- coding: utf-8 -*-
"""Follow ARC Model - move the electrical model to follow a moved
architectural (ARC) link.

WHAT IT DOES (modifies the model, one undo step):
    The architectural model has moved and/or rotated. This tool works out
    that move and applies exactly the same move + rotation to:
      - electrical elements: fixtures, devices, equipment, lighting,
        cable trays, conduits, their fittings, wires and MEP spaces
        (connected runs stay connected - everything moves together)
      - tags of those elements, tags of linked ARC elements (doors, rooms...)
        and room / space tags, including free leaders
      - text notes, annotation symbols, detail items, detail lines, filled
        regions and revision clouds placed in plan views
      - linear dimensions in plan views (rebuilt at the new position)
      - crop regions of cropped plan views (moved, and rotated if the ARC
        model rotated) and, optionally, scope boxes
      - optionally this model's own grids (e.g. copy/monitored grids)

HOW THE MOVE IS FOUND (you choose):
    A. Grids by name - this model's grids show where the ARC model WAS,
       the linked grids with the same names show where it IS now
       (typical when grids were copy/monitored). Best-fit over all grids.
    B. Pick points - click a point at the old position, then the same
       point on the moved ARC link (a second pair adds rotation).

If elements are pre-selected, only those (and their tags/dims) move;
otherwise everything electrical in the model moves.
Revit stores lengths in decimal feet; the report shows millimetres.
"""
from __future__ import division

import math

import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
import System
from System.Collections.Generic import List

from Autodesk.Revit.DB import (
    Arc, BuiltInCategory, BuiltInParameter, Dimension, ElementId,
    ElementMulticategoryFilter, ElementTransformUtils, FailureProcessingResult,
    FailureSeverity, FamilyInstance, FilteredElementCollector, Grid,
    IFailuresPreprocessor, IndependentTag, LeaderEndCondition, Line,
    Reference, ReferenceArray, RevitLinkInstance, SpotDimension,
    SubTransaction, Transaction, Transform, ViewPlan, XYZ,
)
from Autodesk.Revit.UI.Selection import ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException

from pyrevit import forms, revit, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

FT_TO_MM = 304.8          # Revit internal feet -> millimetres
EPS = 1e-9
Z_AXIS = Line.CreateBound(XYZ(0, 0, 0), XYZ(0, 0, 1))   # rotation axis (world origin)


# ---------------------------------------------------------------------------
# Version-safe helpers (Revit 2024+ uses ElementId.Value, older IntegerValue)
# ---------------------------------------------------------------------------
def eid_int(eid):
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


def bics(names):
    """BuiltInCategory names -> list of enums (missing names skipped)."""
    out = []
    for name in names:
        bic = getattr(BuiltInCategory, name, None)
        if bic is not None:
            out.append(bic)
    return out


def cat_int(element):
    cat = element.Category if element else None
    return eid_int(cat.Id) if cat else None


def flat(p):
    return XYZ(p.X, p.Y, 0)


def normalize_angle(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a <= -math.pi:
        a += 2 * math.pi
    return a


def rotate_vec(v, angle):
    c, s = math.cos(angle), math.sin(angle)
    return XYZ(v.X * c - v.Y * s, v.X * s + v.Y * c, v.Z)


def collect(categories, view_specific=None):
    """All instances in the given categories."""
    if not categories:
        return []
    col = (FilteredElementCollector(doc)
           .WherePasses(ElementMulticategoryFilter(List[BuiltInCategory](categories)))
           .WhereElementIsNotElementType())
    elements = list(col)
    if view_specific is not None:
        elements = [e for e in elements if e.ViewSpecific == view_specific]
    return elements


# Electrical model content that follows the building
MODEL_CATS = bics([
    'OST_ElectricalFixtures', 'OST_ElectricalEquipment',
    'OST_LightingFixtures', 'OST_LightingDevices',
    'OST_FireAlarmDevices', 'OST_DataDevices', 'OST_CommunicationDevices',
    'OST_SecurityDevices', 'OST_NurseCallDevices', 'OST_TelephoneDevices',
    'OST_AudioVisualDevices',
    'OST_CableTray', 'OST_CableTrayFitting', 'OST_Conduit', 'OST_ConduitFitting',
    'OST_Wire', 'OST_MEPSpaces',
])
# View-specific annotation placed in plan views
ANNOTATION_CATS = bics([
    'OST_TextNotes', 'OST_GenericAnnotation', 'OST_DetailComponents',
    'OST_Lines', 'OST_FilledRegion', 'OST_RevisionClouds',
])
MODEL_CAT_INTS = set(int(c) for c in MODEL_CATS)
ANNOTATION_CAT_INTS = set(int(c) for c in ANNOTATION_CATS)


# ---------------------------------------------------------------------------
# 1. Finding the move: grids by name, or picked points
# ---------------------------------------------------------------------------
def grid_data(document, transform=None):
    """{grid name: Line or Arc} - optionally transformed into host coordinates."""
    data = {}
    for grid in FilteredElementCollector(document).OfClass(Grid):
        curve = grid.Curve
        if transform is not None:
            curve = curve.CreateTransformed(transform)
        data[grid.Name] = curve
    return data


def intersect_2d(line_a, line_b):
    p, u = line_a.GetEndPoint(0), line_a.Direction
    q, v = line_b.GetEndPoint(0), line_b.Direction
    den = u.X * v.Y - u.Y * v.X
    if abs(den) < 1e-6:                  # parallel
        return None
    wx, wy = q.X - p.X, q.Y - p.Y
    t = (wx * v.Y - wy * v.X) / den
    return XYZ(p.X + u.X * t, p.Y + u.Y * t, 0)


def move_from_grids(link):
    """Best-fit rotation + translation that carries this model's grids onto
    the linked grids with the same names."""
    host = grid_data(doc)
    linked = grid_data(link.GetLinkDocument(), link.GetTotalTransform())
    names = sorted(set(host) & set(linked))
    if not names:
        raise ValueError('No grids with the same name exist in this model and in "{0}".'
                         .format(link.Name))

    # Rotation: average change of direction of the straight grids
    sin_sum = cos_sum = 0.0
    for n in names:
        a, b = host[n], linked[n]
        if isinstance(a, Line) and isinstance(b, Line):
            d = normalize_angle(math.atan2(b.Direction.Y, b.Direction.X)
                                - math.atan2(a.Direction.Y, a.Direction.X))
            sin_sum += math.sin(d)
            cos_sum += math.cos(d)

    # Matching points: crossings of straight grids, centres of arc grids
    old_pts, new_pts, used = [], [], set()
    arc_sin = arc_cos = 0.0
    for i, a in enumerate(names):
        if isinstance(host[a], Arc) and isinstance(linked[a], Arc):
            old_pts.append(flat(host[a].Center))
            new_pts.append(flat(linked[a].Center))
            used.add(a)
            # rotation from the arc's start point - only used when there are
            # no straight grids (arc grid ends are often stretched)
            ha, la = host[a], linked[a]
            d = normalize_angle(
                math.atan2(la.GetEndPoint(0).Y - la.Center.Y, la.GetEndPoint(0).X - la.Center.X)
                - math.atan2(ha.GetEndPoint(0).Y - ha.Center.Y, ha.GetEndPoint(0).X - ha.Center.X))
            arc_sin += math.sin(d)
            arc_cos += math.cos(d)
        for b in names[i + 1:]:
            if not all(isinstance(c, Line) for c in (host[a], host[b], linked[a], linked[b])):
                continue
            po, pn = intersect_2d(host[a], host[b]), intersect_2d(linked[a], linked[b])
            if po is not None and pn is not None:
                old_pts.append(po)
                new_pts.append(pn)
                used.update([a, b])
    if not old_pts:
        raise ValueError('Need at least two crossing grids (or one arc grid) with the same '
                         'names in both models.')

    if not (sin_sum or cos_sum):
        sin_sum, cos_sum = arc_sin, arc_cos
    angle = math.atan2(sin_sum, cos_sum) if (sin_sum or cos_sum) else 0.0
    rot = Transform.CreateRotation(XYZ.BasisZ, angle)
    n = len(old_pts)
    co = XYZ(sum(p.X for p in old_pts) / n, sum(p.Y for p in old_pts) / n, 0)
    cn = XYZ(sum(p.X for p in new_pts) / n, sum(p.Y for p in new_pts) / n, 0)
    translation = cn.Subtract(rot.OfPoint(co))
    xform = Transform.CreateTranslation(translation).Multiply(rot)
    # How well do the grids agree? (a grid moved on its own shows up here)
    residual = math.sqrt(sum(xform.OfPoint(o).DistanceTo(p) ** 2
                             for o, p in zip(old_pts, new_pts)) / n)
    return xform, angle, translation, residual, sorted(used)


def move_from_points():
    """Pick old/new point pairs. One pair = move only; two pairs = + rotation."""
    o1 = flat(uidoc.Selection.PickPoint('1/4  Pick a point at the OLD position (e.g. grid crossing in this model)'))
    n1 = flat(uidoc.Selection.PickPoint('2/4  Pick the SAME point on the moved ARC link'))
    angle = 0.0
    try:
        o2 = flat(uidoc.Selection.PickPoint('3/4  For rotation: a 2nd OLD point (Esc = no rotation)'))
        n2 = flat(uidoc.Selection.PickPoint('4/4  The SAME 2nd point on the moved ARC link'))
        vo, vn = o2.Subtract(o1), n2.Subtract(n1)
        if vo.GetLength() > 1e-3 and vn.GetLength() > 1e-3:
            angle = normalize_angle(math.atan2(vn.Y, vn.X) - math.atan2(vo.Y, vo.X))
    except OperationCanceledException:
        pass
    rot = Transform.CreateRotation(XYZ.BasisZ, angle)
    translation = n1.Subtract(rot.OfPoint(o1))
    return Transform.CreateTranslation(translation).Multiply(rot), angle, translation


# ---------------------------------------------------------------------------
# 2. Moving elements in bulk (connected runs stay connected)
# ---------------------------------------------------------------------------
def transform_ids(ids, angle, translation, errors):
    """Rotate about the world origin, then translate. Bulk first; if one bad
    element spoils the bulk call, retry element by element."""
    if not ids:
        return 0
    failed = set()

    def run(bulk, single):
        try:
            bulk(List[ElementId]([i for i in ids if eid_int(i) not in failed]))
        except Exception:
            for eid in ids:
                if eid_int(eid) in failed:
                    continue
                try:
                    single(eid)
                except Exception as err:
                    failed.add(eid_int(eid))
                    errors.append((eid, str(err)))

    if abs(angle) > EPS:
        run(lambda col: ElementTransformUtils.RotateElements(doc, col, Z_AXIS, angle),
            lambda eid: ElementTransformUtils.RotateElement(doc, eid, Z_AXIS, angle))
    if translation.GetLength() > EPS:
        run(lambda col: ElementTransformUtils.MoveElements(doc, col, translation),
            lambda eid: ElementTransformUtils.MoveElement(doc, eid, translation))
    return len(ids) - len(failed)


def top_group_id(element):
    """Outermost group that contains the element (groups move as a whole)."""
    gid = element.GroupId
    while gid != ElementId.InvalidElementId:
        group = doc.GetElement(gid)
        if group.GroupId == ElementId.InvalidElementId:
            return gid
        gid = group.GroupId
    return None


# ---------------------------------------------------------------------------
# 3. Tags
# ---------------------------------------------------------------------------
def tag_follows(tag, moved_keys, link_key, include_linked):
    """True if the tag's element moved, or it tags an element of the ARC link."""
    try:
        for ref in tag.GetTaggedReferences():                    # Revit 2022+
            key = eid_int(ref.ElementId)
            if key in moved_keys:
                return True
            if (include_linked and key == link_key
                    and ref.LinkedElementId != ElementId.InvalidElementId):
                return True
        return False
    except AttributeError:                                       # Revit 2021 and older
        try:
            lid = tag.TaggedElementId
            if eid_int(lid.HostElementId) in moved_keys:
                return True
            return include_linked and eid_int(lid.LinkInstanceId) == link_key
        except Exception:
            return False


def leader_points(tag):
    """Free leader ends and elbows of an IndependentTag."""
    points = []
    if not tag.HasLeader:
        return points
    free = tag.LeaderEndCondition == LeaderEndCondition.Free
    try:
        for ref in tag.GetTaggedReferences():                    # Revit 2022+
            try:
                if free:
                    points.append(('end', ref, tag.GetLeaderEnd(ref)))
                if tag.HasLeaderElbow(ref):
                    points.append(('elbow', ref, tag.GetLeaderElbow(ref)))
            except Exception:
                pass
    except AttributeError:
        try:
            if free:
                points.append(('end', None, tag.LeaderEnd))
            if tag.HasElbow:
                points.append(('elbow', None, tag.LeaderElbow))
        except Exception:
            pass
    return points


def write_leader_point(tag, kind, ref, point):
    try:
        if ref is None:
            setattr(tag, 'LeaderEnd' if kind == 'end' else 'LeaderElbow', point)
        elif kind == 'end':
            tag.SetLeaderEnd(ref, point)
        else:
            tag.SetLeaderElbow(ref, point)
    except Exception:
        pass


def spatial_tag_follows(tag, moved_keys, link_key, include_linked):
    """Space tags of moved spaces; room tags of rooms in the ARC link."""
    try:
        space = tag.Space                                   # SpaceTag
        return space is not None and eid_int(space.Id) in moved_keys
    except AttributeError:
        pass
    try:
        rid = tag.TaggedRoomId                              # RoomTag (LinkElementId)
        if eid_int(rid.HostElementId) in moved_keys:
            return True
        return include_linked and eid_int(rid.LinkInstanceId) == link_key
    except Exception:
        return False


def spatial_leader_points(tag):
    points = []
    try:
        if tag.HasLeader:
            points.append(('end', None, tag.LeaderEnd))
            if tag.HasElbow:
                points.append(('elbow', None, tag.LeaderElbow))
    except Exception:
        pass
    return points


# ---------------------------------------------------------------------------
# 4. Dimensions
# ---------------------------------------------------------------------------
def record_dimensions(moved_keys, link_key):
    """Linear plan dimensions touching moved elements or the ARC link."""
    records, ignored = [], []
    for dim in FilteredElementCollector(doc).OfClass(Dimension).WhereElementIsNotElementType():
        if isinstance(dim, SpotDimension) or dim.View is None:
            continue
        try:
            refs = list(dim.References)
        except Exception:
            continue
        keys = set(eid_int(r.ElementId) for r in refs)
        if not (keys & moved_keys or link_key in keys):
            continue
        if not isinstance(dim.View, ViewPlan) or not isinstance(dim.Curve, Line):
            ignored.append(dim.Id)
            continue
        try:
            origin = dim.Origin
        except Exception:
            origin = dim.Curve.Origin
        records.append({
            'old_id': dim.Id, 'view_id': dim.View.Id, 'type_id': dim.GetTypeId(),
            'refs': [r.ConvertToStableRepresentation(doc) for r in refs],
            'origin': origin, 'dir': flat(dim.Curve.Direction).Normalize(),
        })
    return records, ignored


def rebuild_dimension(rec, xform, angle):
    view = doc.GetElement(rec['view_id'])
    dim_type = doc.GetElement(rec['type_id'])
    refs = ReferenceArray()
    for stable in rec['refs']:
        refs.Append(Reference.ParseFromStableRepresentation(doc, stable))
    new_origin = xform.OfPoint(rec['origin'])
    for direction in (rotate_vec(rec['dir'], angle), rec['dir']):
        sub = SubTransaction(doc)
        sub.Start()
        try:
            line = Line.CreateBound(new_origin.Subtract(direction), new_origin.Add(direction))
            new_dim = doc.Create.NewDimension(view, line, refs, dim_type)
            doc.Regenerate()
            if new_dim is not None and new_dim.IsValidObject:
                sub.Commit()
                return new_dim
            sub.RollBack()
        except Exception:
            if sub.HasStarted() and not sub.HasEnded():
                sub.RollBack()
    return None


# ---------------------------------------------------------------------------
# 5. View crops
# ---------------------------------------------------------------------------
def crop_center(view):
    cb = view.CropBox
    return cb.Transform.OfPoint(cb.Min.Add(cb.Max).Multiply(0.5))


def is_scope_boxed(view):
    param = view.get_Parameter(BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
    return param is not None and param.AsElementId() != ElementId.InvalidElementId


def find_crop_element(view):
    """Crop region element (call while the crop region is visible)."""
    ids = list(FilteredElementCollector(doc, view.Id)
               .OfCategory(BuiltInCategory.OST_CropBoundary).ToElementIds())
    if len(ids) == 1:
        return doc.GetElement(ids[0])
    try:
        candidate = doc.GetElement(ElementId(System.Int64(eid_int(view.Id) - 1)))
    except Exception:
        candidate = doc.GetElement(ElementId(int(eid_int(view.Id) - 1)))
    if candidate is not None and cat_int(candidate) == int(BuiltInCategory.OST_CropBoundary):
        return candidate
    return doc.GetElement(ids[0]) if ids else None


def move_view_crop(view, xform, angle, rotate):
    centre = crop_center(view)
    delta = flat(xform.OfPoint(centre).Subtract(centre))
    if delta.GetLength() > EPS:                  # translate in view coordinates
        cb = view.CropBox
        dv = cb.Transform.Inverse.OfVector(delta)
        shift = XYZ(dv.X, dv.Y, 0)
        cb.Min = cb.Min.Add(shift)
        cb.Max = cb.Max.Add(shift)
        view.CropBox = cb
        doc.Regenerate()
    if not rotate or abs(angle) < EPS:
        return 'moved'
    was_visible = view.CropBoxVisible
    try:
        if not was_visible:
            view.CropBoxVisible = True
            doc.Regenerate()
        crop_el = find_crop_element(view)
        if crop_el is None:
            return 'moved (crop element not found - not rotated)'
        pivot = crop_center(view)
        ElementTransformUtils.RotateElement(
            doc, crop_el.Id, Line.CreateBound(pivot, pivot.Add(XYZ.BasisZ)), angle)
    finally:
        if not was_visible:
            view.CropBoxVisible = False
    return 'moved + rotated'


class ViewItem(forms.TemplateListItem):
    @property
    def name(self):
        return '{0}  [{1}]'.format(self.item.Name, self.item.ViewType)


class WarningSwallower(IFailuresPreprocessor):
    """Hide warnings (we report ourselves); errors still roll back."""
    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            if failure.GetSeverity() == FailureSeverity.Warning:
                accessor.DeleteWarning(failure)
        return FailureProcessingResult.Continue


# ===========================================================================
# MAIN
# ===========================================================================
# --- Which ARC link? ------------------------------------------------------
links = [l for l in FilteredElementCollector(doc).OfClass(RevitLinkInstance)
         if l.GetLinkDocument() is not None]
if not links:
    forms.alert('No loaded Revit links found. Load the ARC model first.', exitscript=True)
if len(links) == 1:
    link = links[0]
else:
    link = forms.SelectFromList.show(
        sorted(links, key=lambda l: l.Name), name_attr='Name',
        title='Which link is the ARC (architectural) model?', button_name='Use this link')
    if link is None:
        script.exit()
link_key = eid_int(link.Id)

# --- How did it move? -----------------------------------------------------
M_GRIDS = 'Grids by name (this model\'s grids = old position)'
M_POINTS = 'Pick points (old position -> moved ARC link)'
method = forms.CommandSwitchWindow.show(
    [M_GRIDS, M_POINTS], message='How should the ARC model\'s move be measured?')
if method is None:
    script.exit()

residual, used_grids = None, []
try:
    if method == M_GRIDS:
        xform, angle, translation, residual, used_grids = move_from_grids(link)
    else:
        xform, angle, translation = move_from_points()
except OperationCanceledException:
    script.exit()
except ValueError as err:
    forms.alert(str(err), exitscript=True)

move_mm = translation.GetLength() * FT_TO_MM
angle_deg = math.degrees(angle)
if move_mm < 0.5 and abs(angle_deg) < 0.001:
    forms.alert('The ARC model has not moved relative to this model - nothing to do.',
                exitscript=True)

# --- What moves? ------------------------------------------------------------
preselected = [doc.GetElement(i) for i in uidoc.Selection.GetElementIds()]
preselected = [e for e in preselected if cat_int(e) in (MODEL_CAT_INTS | ANNOTATION_CAT_INTS)]
scope_text = ('the {0} selected elements'.format(len(preselected)) if preselected
              else 'ALL electrical elements in the model')

summary = ('ARC model "{0}" moved {1:,.0f} mm  (X {2:,.0f}, Y {3:,.0f}) and rotated {4:.4f} deg.'
           .format(link.Name, move_mm, translation.X * FT_TO_MM,
                   translation.Y * FT_TO_MM, angle_deg))
if residual is not None:
    summary += '\nGrid fit error: {0:.1f} mm over {1} grids.'.format(residual * FT_TO_MM, len(used_grids))
    if residual * FT_TO_MM > 5:
        summary += '\nWARNING: the grids do not agree - some grids moved on their own.'
if not forms.alert(summary + '\n\nMove {0} to follow it?'.format(scope_text),
                   yes=True, no=True):
    script.exit()

OPT_ANNO = 'Move text / symbols / detail items / detail lines in plan views'
OPT_TAGS = 'Move tags of moved elements (all views)'
OPT_LTAGS = 'Move tags of linked ARC elements (doors, rooms...)'
OPT_DIMS = 'Rebuild plan dimensions (to moved elements or the ARC link)'
OPT_CROP = 'Move view crop regions'
OPT_CROP_ROT = 'Rotate view crop regions with the ARC model'
OPT_SCOPE = 'Move scope boxes (views using them follow)'
OPT_GRIDS = 'Move this model\'s own grids too (copy/monitored grids)'
OPT_PINNED = 'Include pinned elements (unpin, move, re-pin)'
defaults = [(OPT_ANNO, not preselected), (OPT_TAGS, True), (OPT_LTAGS, True),
            (OPT_DIMS, True), (OPT_CROP, not preselected), (OPT_CROP_ROT, True),
            (OPT_SCOPE, not preselected), (OPT_GRIDS, method == M_GRIDS and not preselected),
            (OPT_PINNED, True)]
chosen = forms.SelectFromList.show(
    [forms.TemplateListItem(o, checked=c) for o, c in defaults],
    title='Follow ARC Model - what should follow?', multiselect=True, button_name='Next')
if chosen is None:
    script.exit()
chosen = set(chosen)

# --- Gather ids -----------------------------------------------------------
if preselected:
    model_els = [e for e in preselected if not e.ViewSpecific or cat_int(e) in MODEL_CAT_INTS]
    anno_els = [e for e in preselected if e.ViewSpecific and cat_int(e) in ANNOTATION_CAT_INTS]
else:
    model_els = collect(MODEL_CATS)
    anno_els = []
    if OPT_ANNO in chosen:
        for e in collect(ANNOTATION_CATS, view_specific=True):
            owner = doc.GetElement(e.OwnerViewId)
            if isinstance(owner, ViewPlan) and not owner.IsTemplate:
                anno_els.append(e)

skipped = []
move_ids, anno_ids, seen = [], [], set()


def add(eid, bucket):
    key = eid_int(eid)
    if key not in seen:
        seen.add(key)
        bucket.append(eid)


for bucket, elements in ((move_ids, model_els), (anno_ids, anno_els)):
    for e in elements:
        if isinstance(e, FamilyInstance) and e.SuperComponent is not None:
            continue                                  # nested: moves with its parent
        gid = top_group_id(e)
        if gid is not None:
            add(gid, bucket)                          # move the whole group
            continue
        add(e.Id, bucket)

if OPT_GRIDS in chosen:
    for g in FilteredElementCollector(doc).OfClass(Grid):
        add(g.Id, move_ids)
if OPT_SCOPE in chosen:
    for sb in collect(bics(['OST_VolumeOfInterest'])):
        add(sb.Id, move_ids)

# Pinned elements: include (unpin/re-pin) or leave out
pinned = [eid for eid in move_ids + anno_ids if doc.GetElement(eid).Pinned]
if OPT_PINNED not in chosen and pinned:
    pinned_keys = set(eid_int(i) for i in pinned)
    for eid in pinned:
        skipped.append((eid, 'Pinned'))
    move_ids = [i for i in move_ids if eid_int(i) not in pinned_keys]
    anno_ids = [i for i in anno_ids if eid_int(i) not in pinned_keys]
    pinned = []

if not move_ids and not anno_ids:
    forms.alert('Nothing to move.', exitscript=True)

moved_keys = set(eid_int(i) for i in move_ids + anno_ids)
# members of moved groups count as moved too (for their tags/dims)
for gid in list(move_ids + anno_ids):
    group = doc.GetElement(gid)
    if hasattr(group, 'GetMemberIds'):
        moved_keys.update(eid_int(m) for m in group.GetMemberIds())

# --- View crops -------------------------------------------------------------
crop_views = []
if OPT_CROP in chosen:
    items = []
    for v in FilteredElementCollector(doc).OfClass(ViewPlan):
        if v.IsTemplate or not v.CropBoxActive:
            continue
        if is_scope_boxed(v) and OPT_SCOPE in chosen:
            continue                                  # follows its scope box
        items.append(ViewItem(v, checked=not is_scope_boxed(v)))
    if items:
        picked = forms.SelectFromList.show(
            sorted(items, key=lambda i: i.name), multiselect=True, button_name='Move',
            title='Views whose crop region should follow (untick overall / key plans to keep)')
        if picked is None:
            script.exit()
        crop_views = list(picked)

# --- Record tags BEFORE anything moves --------------------------------------
tag_jobs = []
if OPT_TAGS in chosen or OPT_LTAGS in chosen:
    include_linked = OPT_LTAGS in chosen
    local_keys = moved_keys if OPT_TAGS in chosen else set()
    for tag in FilteredElementCollector(doc).OfClass(IndependentTag):
        if eid_int(tag.Id) in moved_keys:
            continue                                  # (e.g. inside a moved group)
        if tag_follows(tag, local_keys, link_key, include_linked):
            tag_jobs.append((tag, tag.TagHeadPosition, leader_points(tag)))
    for tag in collect(bics(['OST_RoomTags', 'OST_MEPSpaceTags'])):
        if spatial_tag_follows(tag, local_keys, link_key, include_linked):
            tag_jobs.append((tag, tag.TagHeadPosition, spatial_leader_points(tag)))

dim_records, dims_ignored = [], []
if OPT_DIMS in chosen:
    dim_records, dims_ignored = record_dimensions(moved_keys, link_key)

# --- Do it - one transaction, one undo ---------------------------------------
t = Transaction(doc, 'Follow ARC Model')
opts = t.GetFailureHandlingOptions()
opts.SetFailuresPreprocessor(WarningSwallower())
t.SetFailureHandlingOptions(opts)

errors, tags_moved, dims_ok, dims_failed, crop_results = [], 0, 0, [], []
t.Start()
try:
    for eid in pinned:
        doc.GetElement(eid).Pinned = False
    for rec in dim_records:
        doc.Delete(rec['old_id'])

    n_model = transform_ids(move_ids, angle, translation, errors)
    n_anno = transform_ids(anno_ids, angle, translation, errors)
    doc.Regenerate()

    for tag, head, points in tag_jobs:
        try:
            if tag.Pinned:
                continue
            tag.TagHeadPosition = xform.OfPoint(head)
            for kind, ref, point in points:
                write_leader_point(tag, kind, ref, xform.OfPoint(point))
            tags_moved += 1
        except Exception as err:
            errors.append((tag.Id, 'Tag: ' + str(err)))

    for rec in dim_records:
        if rebuild_dimension(rec, xform, angle) is not None:
            dims_ok += 1
        else:
            dims_failed.append(rec)

    for view in crop_views:
        try:
            crop_results.append((view, move_view_crop(view, xform, angle, OPT_CROP_ROT in chosen)))
        except Exception as err:
            crop_results.append((view, 'FAILED: ' + str(err)))

    for eid in pinned:
        element = doc.GetElement(eid)
        if element is not None and element.IsValidObject:
            element.Pinned = True
    t.Commit()
except Exception as err:
    if t.HasStarted() and not t.HasEnded():
        t.RollBack()
    forms.alert('Follow ARC Model failed and nothing was changed:\n\n{0}'.format(err),
                exitscript=True)

# --- Report ---------------------------------------------------------------
output.print_md('## Follow ARC Model')
output.print_md(summary.replace('\n', '  \n'))
output.print_md('- Model elements / groups moved: **{0}**'.format(n_model))
output.print_md('- Annotation elements moved: **{0}**'.format(n_anno))
output.print_md('- Tags moved: **{0}**'.format(tags_moved))
if OPT_DIMS in chosen:
    output.print_md('- Dimensions rebuilt: **{0}** of {1}'.format(dims_ok, len(dim_records)))
output.print_md('- View crops moved: **{0}**'.format(
    len([r for r in crop_results if not r[1].startswith('FAILED')])))
if used_grids:
    output.print_md('- Grids used for the fit: ' + ', '.join(used_grids))

failed_crops = [(v, r) for v, r in crop_results if r.startswith('FAILED') or 'not' in r]
if failed_crops:
    output.print_md('### View crops to check')
    for view, result in failed_crops:
        output.print_md('- {0} {1}: {2}'.format(output.linkify(view.Id), view.Name, result))
if dims_failed:
    output.print_md('### Dimensions that could not be rebuilt - please re-dimension')
    for rec in dims_failed:
        view = doc.GetElement(rec['view_id'])
        output.print_md('- View **{0}**'.format(view.Name))
if dims_ignored:
    output.print_md('### Dimensions not in plan views (sections / elevations) - check them')
    output.print_md('- ' + output.linkify(dims_ignored))
if skipped:
    output.print_md('### Skipped ({0})'.format(len(skipped)))
    for eid, reason in skipped[:200]:
        output.print_md('- {0}: {1}'.format(output.linkify(eid), reason))
if errors:
    output.print_md('### Errors ({0})'.format(len(errors)))
    for eid, reason in errors[:200]:
        output.print_md('- {0}: {1}'.format(output.linkify(eid), reason))
output.print_md('*Not handled: section / elevation views and their markers, levels / '
                'elevation changes. Undo everything with one Ctrl+Z ("Follow ARC Model").*')
