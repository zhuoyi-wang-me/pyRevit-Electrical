# -*- coding: utf-8 -*-
"""Create Filled Regions from Rooms that live in a linked model.

Workflow:
1. Pick the linked model (if more than one is loaded).
2. Pick where to create the Filled Regions:
   - Active View only, or
   - One or more Floor Plan views (multiple levels at once).
3. Rooms are automatically filtered to only the ones whose level matches
   one of the chosen views, so you're never shown rooms from other floors.
4. Pick which of those (already filtered) rooms to convert.
5. Pick the Filled Region Type to use.
6. Each room's boundary is transformed from link coordinates into host
   coordinates and a Filled Region is created in that room's matching view.
"""

from pyrevit import revit, DB, forms, script

doc = revit.doc
uidoc = revit.uidoc

output = script.get_output()
logger = script.get_logger()

LEVEL_MATCH_TOLERANCE_FT = 1.0  # how close (in feet) a room's level must be
                                 # to a target view's level to count as a match


# ------------------------------------------------------------------
# 0. Choose target view(s): active view, or pick from Floor Plans
# ------------------------------------------------------------------
view_choice = forms.CommandSwitchWindow.show(
    ['Use Active View', 'Select Floor Plan View(s)'],
    message='Where should the Filled Regions be created?'
)
if not view_choice:
    script.exit()

target_views = []

if view_choice == 'Use Active View':
    active_view = doc.ActiveView
    # A room boundary is a horizontal outline, so it can only be drawn in a
    # plan or drafting view - never in a 3D view, section or elevation.
    if not isinstance(active_view, (DB.ViewPlan, DB.ViewDrafting)) or active_view.IsTemplate:
        forms.alert(
            "The active view is not a plan or drafting view. Room outlines are "
            "horizontal, so Filled Regions from rooms can only be created in a "
            "floor / ceiling / area plan or a drafting view.\n\n"
            "Open a plan view, or re-run and choose 'Select Floor Plan View(s)'.",
            exitscript=True
        )
    target_views = [active_view]

else:
    plan_views = DB.FilteredElementCollector(doc)\
        .OfClass(DB.ViewPlan)\
        .WhereElementIsNotElementType()\
        .ToElements()

    plan_views = [v for v in plan_views if not v.IsTemplate]

    view_dict = {}
    for v in plan_views:
        # The view type is part of the key: a floor plan and a ceiling plan
        # can share a name, and one would silently overwrite the other.
        key = "{}  ({})  [{}]".format(
            revit.query.get_name(v),
            v.ViewType,
            revit.query.get_name(v.GenLevel) if v.GenLevel else "no level"
        )
        view_dict[key] = v

    if not view_dict:
        forms.alert("No Floor Plan views were found in this project.", exitscript=True)

    selected_view_keys = forms.SelectFromList.show(
        sorted(view_dict.keys()),
        title="Select Floor Plan View(s)  (multiple levels allowed)",
        button_name="Next >",
        multiselect=True
    )
    if not selected_view_keys:
        script.exit()

    target_views = [view_dict[k] for k in selected_view_keys]


# ------------------------------------------------------------------
# 1. Collect linked model instances that are actually loaded
# ------------------------------------------------------------------
link_instances = DB.FilteredElementCollector(doc)\
    .OfClass(DB.RevitLinkInstance)\
    .ToElements()

link_dict = {}
for li in link_instances:
    link_doc = li.GetLinkDocument()
    if link_doc:
        # revit.query.get_name() avoids the IronPython "Name" binding issue
        # that several Revit API classes trigger via plain attribute access.
        link_dict[revit.query.get_name(li)] = li

if not link_dict:
    forms.alert(
        "No loaded linked models were found in this project.\n"
        "Make sure the link containing your rooms is loaded (not unloaded).",
        exitscript=True
    )

if len(link_dict) == 1:
    selected_link_name = list(link_dict.keys())[0]
else:
    selected_link_name = forms.SelectFromList.show(
        sorted(link_dict.keys()),
        title="Select Linked Model",
        button_name="Select",
        multiselect=False
    )
    if not selected_link_name:
        script.exit()

link_instance = link_dict[selected_link_name]
link_doc = link_instance.GetLinkDocument()
link_transform = link_instance.GetTotalTransform()


# ------------------------------------------------------------------
# 2. Collect placed rooms from the linked document
# ------------------------------------------------------------------
all_rooms = DB.FilteredElementCollector(link_doc)\
    .OfCategory(DB.BuiltInCategory.OST_Rooms)\
    .WhereElementIsNotElementType()\
    .ToElements()

all_rooms = [r for r in all_rooms if r.Area > 0]

if not all_rooms:
    forms.alert(
        "No placed rooms (with area > 0) were found in the linked model '{}'."
        .format(selected_link_name),
        exitscript=True
    )



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


def room_name_only(room):
    """The room's name without its number (Room.Name returns 'Name Number',
    so labels showed the number twice: '101 - Office 101')."""
    p = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
    value = p.AsString() if p is not None else None
    return value or revit.query.get_name(room)


def get_room_host_elevation(room, transform):
    """Elevation (Z) of the room's level, translated into host coordinates.

    ProjectElevation, not Elevation: Level.Elevation is measured from the
    level's 'Elevation Base' (project base point OR survey point), so host
    and link values can differ by the survey elevation and nothing would
    match. ProjectElevation is always from the internal origin, which is
    what the link transform maps between."""
    if not room.Level:
        return None
    pt = DB.XYZ(0, 0, room.Level.ProjectElevation)
    return transform.OfPoint(pt).Z


def view_plane_z(view):
    """Z (host internal coordinates) of the plane a view draws on: the
    view's level for plans, 0 for drafting views (which have no level)."""
    lvl = getattr(view, "GenLevel", None)
    return lvl.ProjectElevation if lvl else 0.0


# Build elevation lookup for each target view's associated Level (host coords)
view_level_elev = {}
for v in target_views:
    lvl = getattr(v, "GenLevel", None)
    view_level_elev[v] = lvl.ProjectElevation if lvl else None

has_any_level = any(elev is not None for elev in view_level_elev.values())

if not has_any_level:
    forms.alert(
        "None of the selected view(s) have an associated Level (e.g. a "
        "drafting view), so rooms can't be matched automatically.\n"
        "All rooms from the link will be listed instead - pick manually.",
        warn_icon=False
    )

# ------------------------------------------------------------------
# 2b. Filter rooms to only those matching one of the target views' levels,
#     and remember which view each matched room belongs to.
# ------------------------------------------------------------------
room_to_view = {}   # room.Id.IntegerValue -> matching target view
filtered_rooms = []

for r in all_rooms:
    if not has_any_level:
        # No level info available on any target view - keep all rooms,
        # default them to the first target view.
        filtered_rooms.append(r)
        room_to_view[_id_int(r.Id)] = target_views[0]
        continue

    room_elev = get_room_host_elevation(r, link_transform)
    if room_elev is None:
        continue

    best_view = None
    best_diff = None
    for v, lvl_elev in view_level_elev.items():
        if lvl_elev is None:
            continue
        diff = abs(room_elev - lvl_elev)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_view = v

    if best_view is not None and best_diff <= LEVEL_MATCH_TOLERANCE_FT:
        filtered_rooms.append(r)
        room_to_view[_id_int(r.Id)] = best_view

if not filtered_rooms:
    forms.alert(
        "No rooms in the linked model matched the level(s) of the "
        "selected view(s). Nothing to convert.",
        exitscript=True
    )


# ------------------------------------------------------------------
# 3. Let the user pick which of the (already level-filtered) rooms to convert
# ------------------------------------------------------------------
room_lookup = {}
for r in filtered_rooms:
    room_name = room_name_only(r)
    level_name = revit.query.get_name(r.Level) if r.Level else "?"
    matched_view_name = revit.query.get_name(room_to_view[_id_int(r.Id)])
    key = "{}  |  {} - {}  [{} -> {}]".format(
        _id_int(r.Id),
        r.Number,
        room_name,
        level_name,
        matched_view_name
    )
    room_lookup[key] = r

selected_keys = forms.SelectFromList.show(
    sorted(room_lookup.keys()),
    title="Select Rooms to Convert to Filled Regions",
    button_name="Next >",
    multiselect=True
)
if not selected_keys:
    script.exit()

selected_rooms = [room_lookup[k] for k in selected_keys]


# ------------------------------------------------------------------
# 4. Pick the Filled Region Type
# ------------------------------------------------------------------
fr_types = DB.FilteredElementCollector(doc)\
    .OfClass(DB.FilledRegionType)\
    .ToElements()

fr_type_dict = {revit.query.get_name(t): t for t in fr_types}

selected_fr_type_name = forms.SelectFromList.show(
    sorted(fr_type_dict.keys()),
    title="Select Filled Region Type",
    button_name="Create Filled Regions",
    multiselect=False
)
if not selected_fr_type_name:
    script.exit()

fr_type = fr_type_dict[selected_fr_type_name]


# ------------------------------------------------------------------
# 5. Build boundary loops from each room, transformed to host coords
# ------------------------------------------------------------------
def get_room_curve_loops(room, transform, plane_z):
    """Return a list of DB.CurveLoop built from the room's finish boundary,
    transformed from link coordinates into host coordinates and dropped
    onto the target view's plane (Z = plane_z).

    Rooms are matched to a view within LEVEL_MATCH_TOLERANCE_FT, so the
    room's level can sit a little above / below the view's level (or the
    whole link can be offset vertically). The outline is moved onto the
    view's plane so FilledRegion.Create gets curves lying in that view."""
    loops = []
    bridged = []
    boundary_options = DB.SpatialElementBoundaryOptions()
    boundary_options.SpatialElementBoundaryLocation = \
        DB.SpatialElementBoundaryLocation.Finish

    segment_loops = room.GetBoundarySegments(boundary_options)
    for segment_loop in segment_loops:
        curves = []
        drop = None
        for seg in segment_loop:
            curve = seg.GetCurve().CreateTransformed(transform)
            if drop is None:
                # Room boundaries are horizontal: one vertical move per loop.
                dz = plane_z - curve.GetEndPoint(0).Z
                drop = DB.Transform.CreateTranslation(DB.XYZ(0, 0, dz))
            curves.append(curve.CreateTransformed(drop))
        if curves:
            curve_loop, gaps = stitch_loop(curves)
            if curve_loop is not None:
                loops.append(curve_loop)
                bridged.extend(gaps)
    return loops, bridged


# ------------------------------------------------------------------
# Boundary stitching. A room boundary read from a link is often not
# perfectly continuous - walls that don't quite meet, room separation
# lines a hair off, segments a fraction of a millimetre long. CurveLoop
# refuses any gap ("This curve will make the loop discontinuous"), so the
# segments are made to join exactly before the loop is built.
# ------------------------------------------------------------------
SHORT_CURVE_TOL = doc.Application.ShortCurveTolerance   # ~0.8 mm: Revit's minimum curve length


def refit_curve(curve, start, end):
    """The curve re-made to run exactly from `start` to `end` (its own end
    points moved by a tiny amount). Returns a list of curves (empty when it
    would be too short to exist)."""
    if start.DistanceTo(end) < SHORT_CURVE_TOL:
        return []
    if isinstance(curve, DB.Line):
        return [DB.Line.CreateBound(start, end)]
    if isinstance(curve, DB.Arc):
        try:
            return [DB.Arc.Create(start, end, curve.Evaluate(0.5, True))]
        except Exception:
            return [DB.Line.CreateBound(start, end)]
    # Splines / ellipses: follow them with short straight pieces.
    points = list(curve.Tessellate())
    points[0], points[-1] = start, end
    pieces = []
    for a, b in zip(points[:-1], points[1:]):
        if a.DistanceTo(b) >= SHORT_CURVE_TOL:
            pieces.append(DB.Line.CreateBound(a, b))
    return pieces


def stitch_loop(curves):
    """Build a closed CurveLoop from boundary curves that may not join
    exactly. Each curve is turned round if it runs the wrong way, curves too
    short for Revit are dropped, a tiny mismatch is absorbed by re-fitting
    the curve, and a real gap is closed with a short straight line.
    Returns (CurveLoop or None, [gap lengths bridged, in feet])."""
    pieces = []
    gaps = []
    end = None
    for curve in curves:
        if curve.Length < SHORT_CURVE_TOL:
            continue                         # its neighbours are joined instead
        if end is not None:
            # Running backwards? Turn it round.
            if curve.GetEndPoint(1).DistanceTo(end) < curve.GetEndPoint(0).DistanceTo(end):
                curve = curve.CreateReversed()
            gap = curve.GetEndPoint(0).DistanceTo(end)
            if gap >= SHORT_CURVE_TOL:
                pieces.append(DB.Line.CreateBound(end, curve.GetEndPoint(0)))
                gaps.append(gap)
            elif gap > 1e-9:
                refit = refit_curve(curve, end, curve.GetEndPoint(1))
                if not refit:
                    continue
                pieces.extend(refit)
                end = refit[-1].GetEndPoint(1)
                continue
        pieces.append(curve)
        end = curve.GetEndPoint(1)

    if len(pieces) < 2:
        return None, gaps

    # Close the loop back to its first point.
    first = pieces[0].GetEndPoint(0)
    gap = end.DistanceTo(first)
    if gap >= SHORT_CURVE_TOL:
        pieces.append(DB.Line.CreateBound(end, first))
        gaps.append(gap)
    elif gap > 1e-9:
        last = pieces.pop()
        refit = refit_curve(last, last.GetEndPoint(0), first)
        if refit:
            pieces.extend(refit)
        elif pieces and pieces[-1].GetEndPoint(1).DistanceTo(first) >= SHORT_CURVE_TOL:
            # The last curve was too short to keep: close straight instead.
            pieces.append(DB.Line.CreateBound(pieces[-1].GetEndPoint(1), first))
        if len(pieces) < 2:
            return None, gaps

    loop = DB.CurveLoop()
    for piece in pieces:
        loop.Append(piece)
    return loop, gaps


# ------------------------------------------------------------------
# Fallback outline: the bottom face of the room's 3D volume.
# A closed face is closed by definition, so this never has gaps. It is used
# when the boundary segments give no outline, or one with a gap so large
# (SUSPECT_GAP_MM) that closing it with a straight line would cut across
# the room and draw the wrong shape.
# ------------------------------------------------------------------
SUSPECT_GAP_MM = 50.0
SUSPECT_GAP_FT = DB.UnitUtils.ConvertToInternalUnits(SUSPECT_GAP_MM, DB.UnitTypeId.Millimeters)


def drop_loop_to_plane(loop, plane_z):
    """The loop moved straight up/down so it lies at Z = plane_z."""
    for curve in loop:
        dz = plane_z - curve.GetEndPoint(0).Z
        return DB.CurveLoop.CreateViaTransform(
            loop, DB.Transform.CreateTranslation(DB.XYZ(0, 0, dz)))
    return loop


def get_room_shell_loops(room, transform, plane_z):
    """Outline loops (outer + any holes) from the downward-facing face of
    the room's ClosedShell, moved into host coordinates and onto the view's
    plane. Empty list when the room has no volume (not enclosed, redundant)."""
    try:
        shell = room.ClosedShell
    except Exception:
        shell = None
    if shell is None:
        return []

    bottom_faces = []
    for geo in shell:
        if isinstance(geo, DB.Solid) and geo.Volume > 0:
            for face in geo.Faces:
                if isinstance(face, DB.PlanarFace) and face.FaceNormal.Z < -0.99:
                    bottom_faces.append(face)
    if not bottom_faces:
        return []

    # Only the lowest floor face(s) - the room's footprint at its level.
    low_z = min(f.Origin.Z for f in bottom_faces)
    loops = []
    for face in bottom_faces:
        if abs(face.Origin.Z - low_z) > 0.01:
            continue
        for loop in face.GetEdgesAsCurveLoops():
            host_loop = DB.CurveLoop.CreateViaTransform(loop, transform)
            loops.append(drop_loop_to_plane(host_loop, plane_z))
    return loops


def no_boundary_reason(room):
    """Plain-English reason a room gave no outline at all."""
    try:
        opts = DB.SpatialElementBoundaryOptions()
        segments = room.GetBoundarySegments(opts)
        has_segments = segments is not None and any(len(s) > 0 for s in segments)
    except Exception:
        has_segments = False
    if not has_segments:
        return ("no boundary in the linked model - the room is probably "
                "'Not Enclosed' or 'Redundant' (sharing a space with another "
                "room). Fix it in the architectural model")
    return "boundary exists but could not be made into a closed outline"


created_count = 0
failed_rooms = []
bridged_rooms = []
shell_rooms = []
views_used = set()

with revit.Transaction("Create Filled Regions from Linked Rooms"):
    for room in selected_rooms:
        target_view = room_to_view[_id_int(room.Id)]
        room_label = "{} - {}  [{}]".format(
            room.Number, room_name_only(room), revit.query.get_name(target_view)
        )
        try:
            plane_z = view_plane_z(target_view)
            loops, gaps = get_room_curve_loops(room, link_transform, plane_z)

            # No outline, or a gap too big to close with a straight line:
            # use the room's 3D volume footprint instead.
            used_shell = False
            if not loops or (gaps and max(gaps) > SUSPECT_GAP_FT):
                shell_loops = get_room_shell_loops(room, link_transform, plane_z)
                if shell_loops:
                    largest = max(gaps) if gaps else 0.0
                    loops, gaps, used_shell = shell_loops, [], True

            if not loops:
                failed_rooms.append("{} ({})".format(room_label, no_boundary_reason(room)))
                continue

            DB.FilledRegion.Create(doc, fr_type.Id, target_view.Id, loops)
            created_count += 1
            views_used.add(revit.query.get_name(target_view))
            if used_shell:
                if largest:
                    shell_rooms.append("{} (boundary had a {:.0f} mm gap)".format(
                        room_label,
                        DB.UnitUtils.ConvertFromInternalUnits(largest, DB.UnitTypeId.Millimeters)))
                else:
                    shell_rooms.append("{} (boundary segments gave no outline)".format(room_label))
            elif gaps:
                # Created, but its outline had holes that were closed with
                # straight lines - worth a look if a gap was large.
                bridged_rooms.append("{} ({} gap(s) closed, largest {:.0f} mm)".format(
                    room_label, len(gaps),
                    DB.UnitUtils.ConvertFromInternalUnits(max(gaps), DB.UnitTypeId.Millimeters)))

        except Exception as ex:
            # First line only: Revit adds 'Parameter name: ...' on the next.
            reason = str(ex).strip().splitlines()[0] if str(ex).strip() else type(ex).__name__
            logger.error("Failed for room {}: {}".format(room_label, ex))
            failed_rooms.append("{} - {}".format(room_label, reason))


# ------------------------------------------------------------------
# 6. Report results
# ------------------------------------------------------------------
output.print_md("### Filled Region Creation Complete")
output.print_md("- Linked model: **{}**".format(selected_link_name))
output.print_md("- Filled Region Type: **{}**".format(selected_fr_type_name))
output.print_md("- View(s) used: **{}**".format(", ".join(sorted(views_used)) if views_used else "none"))
output.print_md("- Created: **{}**".format(created_count))

if shell_rooms:
    output.print_md("- Created from the room's 3D volume, because its boundary was broken "
                    "(shape follows the link's Area/Volume boundary setting):")
    for sr in shell_rooms:
        output.print_md("  - {}".format(sr))

if bridged_rooms:
    output.print_md("- Created, but the room outline had gaps that were closed (check these):")
    for br in bridged_rooms:
        output.print_md("  - {}".format(br))

if failed_rooms:
    output.print_md("- Failed / skipped rooms:")
    for fr in failed_rooms:
        output.print_md("  - {}".format(fr))
