# -*- coding: utf-8 -*-
"""Adjust Grids - everything that touches the Revit model.

target_views() only reads. adjust() is the only function that changes the model: in ONE
transaction it gives the grids of the chosen views view-specific (2D) ends on a frame around
the crop region and switches their bubbles on or off. The grids themselves (their 3D extents,
their place in the model) are not touched, and other views are not affected.
"""
import io
import math
import os

from Autodesk.Revit import DB

import ga_geometry

MM = 1.0 / 304.8
# "Line up ALL": an end that stops inside the view is drawn out to the frame only when the gap is at most this
# far ON THE SHEET, measured to the edge of the crop region (15 mm = 3 m at 1:200). A grid stopping farther
# inside (e.g. W1 ... W6 at the edge of the bowl) stays as it is.
SHORT_MM = 15.0
# FIX: the first version took every grid Revit lists for the view and drew it right across the crop
# region. Revit lists a grid for a view as soon as the grid's plane passes through it - also grids whose
# 2D extents had been pulled out of the view to hide them, and grids deliberately drawn short. All of
# those were "pulled in". Now only a grid that is drawn inside the crop region is adjusted, and only
# its ends that leave the crop (or stop right at its edge).
VIEW_TYPES = ('FloorPlan', 'CeilingPlan', 'EngineeringPlan', 'AreaPlan', 'Section', 'Elevation', 'Detail')
KIND_LABELS = (('FloorPlan', u'Floor plan'), ('CeilingPlan', u'Ceiling plan'), ('EngineeringPlan', u'Structural plan'),
               ('AreaPlan', u'Area plan'), ('Section', u'Section'), ('Elevation', u'Elevation'), ('Detail', u'Detail'))

# Grids of a linked model cannot be switched off through the API the way Visibility/Graphics > Revit Links >
# Custom > Grids does it (that setting is not exposed). A view filter of this model does reach the linked
# grids, though (when the link is shown "By host view"). The filter hides every grid whose type name is
# NOT one of the grid types of this model - so this model's own grids stay, the linked ones go.
LINK_FILTER = u'EleTools - Linked grids off'
HOST_SUFFIX = u' - Host'


class Options(object):
    def __init__(self, bubble_sides=(ga_geometry.LEFT, ga_geometry.BOTTOM), bubble_mm=10.0, plain_mm=2.0, bubbles=True,
                 lengthen=False, adjust=True, linked=None, own_type=True, from_model=False, to_grids=False):
        self.to_grids = to_grids                 # True: the ends line up outside the outermost grids, not the crop edge
        self.bubble_sides = list(bubble_sides)   # the sides of the crop region where bubbles are shown
        self.bubble_mm = bubble_mm               # how far a bubble end stands outside the crop, on the sheet
        self.plain_mm = plain_mm                 # how far an end without bubble stands outside the crop, on the sheet
        self.bubbles = bubbles                   # False: only the lengths are changed, bubbles stay as they are
        self.lengthen = lengthen                 # True: also grids that stop inside the view are drawn right across it
        self.from_model = from_model             # True: start from the grid's model (3D) extents, not its 2D ends in the view
        self.adjust = adjust                     # False: the grids of this model are not touched (links only)
        self.linked = linked                     # None: linked grids as they are; 'hide': turn them off; 'show': on again
        self.own_type = own_type                 # give this model's grids their own type when a link uses the same type name


class Report(object):
    def __init__(self):
        self.adjusted = True              # False: the grids of this model were not part of this run
        self.views = 0                    # views in which at least one grid was adjusted
        self.grids = 0                    # grid ends adjusted (one count per grid and view)
        self.not_cropped = []             # view names skipped: the crop region is off
        self.outside = 0                  # grids not shown inside the crop region of their view: left as they are
        self.bubble_trouble = 0           # ends whose bubble Revit would not switch
        self.curved = 0                   # curved grids adjusted
        self.angled = 0                   # angled grids given one bubble only, at their own side
        self.segments = 0                 # end segments of multi-segment grids lined up at their free end
        self.joined = 0                   # middle segments of multi-segment grids, left as they are
        self.cut = 0                      # multi-segment grids cut at the frame where they leave the view through a joint
        self.seen = 0                     # grids of this model Revit shows in the views (one count per grid and view)
        self.skipped = []                 # (grid name, view name, reason)
        self.error = None
        self.notes = []                   # one line per view and grid, for the details file
        # linked grids
        self.linked = None                # None, 'hide' or 'show' - what was asked
        self.no_links = False             # the model has no linked models
        self.link_views = 0               # views whose linked grids were turned off / on again
        self.templates = []               # view templates changed instead of their views
        self.retyped = 0                  # grids of this model given their own type
        self.new_types = []               # the names of those types
        self.clash = []                   # type names used by this model AND a link (own_type off): those linked grids stay
        self.by_link_view = []            # links shown "By linked view": this model's filters do not reach them
        self.unloaded = 0                 # links that are not loaded
        self.link_problem = None          # why the linked grids could not be turned off

    def link_lines(self):
        out = []
        if self.linked is None:
            return out
        if self.no_links:
            return [u'This model has no linked models - no linked grids to turn off.']
        if self.link_problem:
            return [u'Linked grids: ' + self.link_problem]
        where = u''
        if self.templates:
            where = u' (through view template(s): {0})'.format(u', '.join(self.templates[:5]) + (u' ...' if len(self.templates) > 5 else u''))
        if self.linked == 'show':
            out.append(u'Grids from linked models turned on again in {0} view(s){1}.'.format(self.link_views, where))
            return out
        out.append(u'Grids from linked models turned off in {0} view(s){1} - by the view filter "{2}".'.format(self.link_views, where, LINK_FILTER))
        if self.retyped:
            out.append(u'{0} grid(s) of this model were given their own type ({1}): a linked model uses the same type name, and the '
                       u'filter can only tell the grids apart by their type.'.format(self.retyped, u', '.join(self.new_types)))
        if self.clash:
            out.append(u'Linked grids of type {0} stay visible: this model\'s grids use the same type name. Tick "Give this model\'s '
                       u'grids their own type" to turn them off too.'.format(u', '.join(self.clash)))
        if self.by_link_view:
            out.append(u'Shown "By linked view" (this model\'s filters do not reach them), their grids stay on: {0}. Set them to '
                       u'"By host view" in Visibility/Graphics > Revit Links.'.format(u', '.join(self.by_link_view[:5])))
        if self.unloaded:
            out.append(u'{0} link(s) are not loaded: their grids are turned off as well once loaded, unless they use a type name of '
                       u'this model.'.format(self.unloaded))
        return out

    def lines(self):
        if not self.adjusted:
            return self.link_lines()
        out = self.link_lines() + [u'{0} grid(s) adjusted in {1} view(s).'.format(self.grids, self.views)]
        if not self.grids:
            out.append(u'Grids of this model shown in the ticked views: {0} (grids of linked models are never moved).'.format(self.seen))
        if self.curved:
            out.append(u'{0} of them are curved grids.'.format(self.curved))
        if self.segments or self.joined:
            out.append(u'Multi-segment grids: {0} end segment(s) lined up at the free end of the grid; the joints between segments '
                       u'stay where they are ({1} middle segment(s) left as they are).'.format(self.segments, self.joined))
            if self.cut:
                out.append(u'{0} of them leave the view through a joint between segments: they are cut at the frame there '
                           u'(in this view only).'.format(self.cut))
        if self.angled:
            out.append(u'{0} angled grid(s) got their bubble at their own side only; their other end stops on the line without bubbles.'.format(self.angled))
        if self.not_cropped:
            out.append(u'{0} view(s) skipped because their crop region is switched off: {1}'.format(
                len(self.not_cropped), u', '.join(self.not_cropped[:5]) + (u' ...' if len(self.not_cropped) > 5 else u'')))
        if self.outside:
            out.append(u'{0} grid(s) are not shown inside the crop region of their view (or stop well inside it) and were left as they are.'.format(self.outside))
        if self.bubble_trouble:
            out.append(u'{0} grid end(s) were lined up, but Revit would not switch their bubble (the grid type has no bubble at that end).'.format(self.bubble_trouble))
        if self.skipped:
            out.append(u'{0} grid(s) could not be adjusted:'.format(len(self.skipped)))
            reasons = {}
            for grid, view, reason in self.skipped:
                reasons.setdefault(reason, []).append(u'{0} ({1})'.format(grid, view))
            for reason, where in sorted(reasons.items())[:5]:
                out.append(u'   {0}: {1}'.format(reason, u', '.join(where[:6]) + (u' ...' if len(where) > 6 else u'')))
        return out


# ---------------------------------------------------------------------------
# which views
# ---------------------------------------------------------------------------
def can_have_grids(view):
    try:
        return (not view.IsTemplate) and str(view.ViewType) in VIEW_TYPES
    except Exception:
        return False


def views_on_sheet(doc, sheet):
    found = []
    try:
        for view_id in sheet.GetAllPlacedViews():
            view = doc.GetElement(view_id)
            if view is not None and can_have_grids(view):
                found.append(view)
    except Exception:
        pass
    return found


def target_views(doc, uidoc):
    """(views, how they were chosen). The sheets / views selected in the Project Browser; otherwise the
    active view - and when that is a sheet, the views placed on it."""
    chosen, seen = [], set()

    def add(view):
        if view.Id.Value not in seen:
            seen.add(view.Id.Value)
            chosen.append(view)

    sheets = 0
    try:
        picked = [doc.GetElement(eid) for eid in uidoc.Selection.GetElementIds()]
    except Exception:
        picked = []
    for element in picked:
        if isinstance(element, DB.ViewSheet):
            sheets += 1
            for view in views_on_sheet(doc, element):
                add(view)
        elif isinstance(element, DB.View) and can_have_grids(element):
            add(element)
    if chosen:
        return chosen, u'{0} view(s) of the {1} sheet(s) / view(s) selected in the Project Browser'.format(len(chosen), sheets or len(chosen))
    active = uidoc.ActiveView
    if isinstance(active, DB.ViewSheet):
        for view in views_on_sheet(doc, active):
            add(view)
        return chosen, u'{0} view(s) on the active sheet {1}'.format(len(chosen), active.SheetNumber)
    if can_have_grids(active):
        return [active], u'the active view'
    return [], u'the active view cannot show grids'


def grid_count(doc, views):
    total = 0
    for view in views:
        try:
            total += len(list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid)))
        except Exception:
            pass
    return total


class ViewRow(object):
    """One line of the view list in the window."""
    def __init__(self, view, sheet):
        self.view_id = view.Id
        self.key = view.Id.Value
        self.name = view.Name
        kind = str(view.ViewType)
        self.kind = dict(KIND_LABELS).get(kind, kind)
        self.sheet = sheet                 # the number of the sheet the view is placed on, u'' when on none


def list_views(doc):
    """Every view of the model that can show grids (no templates), with the sheet it is placed on. Read only."""
    on_sheet = {}
    for sheet in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
        try:
            for view_id in sheet.GetAllPlacedViews():
                on_sheet.setdefault(view_id.Value, sheet.SheetNumber)
        except Exception:
            pass
    rows = []
    for view in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if can_have_grids(view):
            rows.append(ViewRow(view, on_sheet.get(view.Id.Value, u'')))
    order = dict((kind, n) for n, (kind, label) in enumerate(KIND_LABELS))
    labels = dict((label, kind) for kind, label in KIND_LABELS)
    rows.sort(key=lambda row: (order.get(labels.get(row.kind), 99), row.name.lower()))
    return rows


def link_count(doc):
    try:
        return len(list(DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance)))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# grids of linked models: a view filter of this model turns them off
# ---------------------------------------------------------------------------
def _type_name(doc, element):
    """The name of the element's type (u'' when unknown)."""
    try:
        element_type = doc.GetElement(element.GetTypeId())
    except Exception:
        element_type = None
    if element_type is None:
        return u''
    for name in ('SYMBOL_NAME_PARAM', 'ALL_MODEL_TYPE_NAME'):
        try:
            value = element_type.get_Parameter(getattr(DB.BuiltInParameter, name)).AsString()
            if value:
                return value
        except Exception:
            pass
    try:
        return DB.Element.Name.GetValue(element_type) or u''
    except Exception:
        try:
            return element_type.Name or u''
        except Exception:
            return u''


def _ids(items):
    from System.Collections.Generic import List
    return List[DB.ElementId](items)


def _rule_list(rules):
    from System.Collections.Generic import List
    return List[DB.FilterRule](rules)



def _usable_parameters(doc, grids_category):
    try:
        return list(DB.ParameterFilterUtilities.GetFilterableParametersInCommon(doc, _ids([grids_category])))
    except Exception:
        return []


def _passing(doc, element_filter):
    """The grids of `doc` that the filter would hide."""
    return list(DB.FilteredElementCollector(doc).OfClass(DB.Grid).WherePasses(element_filter))


def _working_filter(doc, grids_category, host_grids, host_types, links):
    """(element filter, the rule used) that hides the grids of the loaded links and none of this model's - tried on the
    grids themselves; (None, why) when no rule does that."""
    usable = _usable_parameters(doc, grids_category)
    candidates = []
    names = sorted(name for name in host_types if name)
    for bip in ('ALL_MODEL_TYPE_NAME', 'SYMBOL_NAME_PARAM'):          # the type name of the grid
        try:
            parameter = DB.ElementId(getattr(DB.BuiltInParameter, bip))
        except Exception:
            continue
        if names and any(found == parameter for found in usable):
            candidates.append((u'Type Name', lambda parameter=parameter: [
                DB.ParameterFilterRuleFactory.CreateNotEqualsRule(parameter, name) for name in names]))
    try:                                                              # the type itself (its id in this model)
        parameter = DB.ElementId(DB.BuiltInParameter.ELEM_TYPE_PARAM)
        type_ids = []
        for grid in host_grids:
            tid = grid.GetTypeId()
            if not any(tid == known for known in type_ids):
                type_ids.append(tid)
        if any(found == parameter for found in usable):
            candidates.append((u'Type', lambda parameter=parameter: [
                DB.ParameterFilterRuleFactory.CreateNotEqualsRule(parameter, tid) for tid in type_ids]))
    except Exception:
        pass
    if not candidates:
        return None, u'Revit offers neither "Type Name" nor "Type" for a filter on grids'
    linked_docs = []
    for link in links:
        try:
            linked = link.GetLinkDocument()
        except Exception:
            linked = None
        if linked is not None:
            linked_docs.append(linked)
    tried = []
    for label, make_rules in candidates:
        try:
            element_filter = DB.ElementParameterFilter(_rule_list(make_rules()))
            own_hidden = len(_passing(doc, element_filter))
            link_kept = 0
            for linked in linked_docs:
                link_kept += len(list(DB.FilteredElementCollector(linked).OfClass(DB.Grid))) - len(_passing(linked, element_filter))
        except Exception as error:
            tried.append(u'"{0}": {1}'.format(label, (u'{0}'.format(error).splitlines() or [u'error'])[0]))
            continue
        if own_hidden == 0 and link_kept == 0:
            return element_filter, label
        tried.append(u'"{0}" would hide {1} of this model\'s {2} grid(s) and keep {3} linked grid(s)'.format(
            label, own_hidden, len(host_grids), link_kept))
    return None, u'; '.join(tried)


def _filter_target(doc, view):
    """The view itself - or its view template when the template controls the filters of the view."""
    try:
        template_id = view.ViewTemplateId
        if template_id is not None and template_id != DB.ElementId.InvalidElementId:
            template = doc.GetElement(template_id)
            if template is not None:
                filters = DB.ElementId(DB.BuiltInParameter.VIS_GRAPHICS_FILTERS)
                if not any(free == filters for free in template.GetNonControlledTemplateParameterIds()):
                    return template
    except Exception:
        pass
    return view


def _existing_filter(doc):
    for element in DB.FilteredElementCollector(doc).OfClass(DB.ParameterFilterElement):
        try:
            if element.Name == LINK_FILTER:
                return element
        except Exception:
            pass
    return None


def _own_type(doc, grid, name):
    """A grid type of this model called `name`: found, or made as a copy of the grid's type."""
    for element_type in DB.FilteredElementCollector(doc).OfClass(DB.GridType):
        if _type_name_of_type(element_type) == name:
            return element_type
    return doc.GetElement(grid.GetTypeId()).Duplicate(name)


def _type_name_of_type(element_type):
    try:
        return DB.Element.Name.GetValue(element_type)
    except Exception:
        try:
            return element_type.Name
        except Exception:
            return None


def _give_own_types(doc, host_grids, shared, report):
    """Give the grids of this model whose type name a link uses as well a copy of their type ('... - Host').
    Returns the type names of this model's grids afterwards."""
    names = set()
    by_type = {}
    for grid in host_grids:
        by_type.setdefault(_type_name(doc, grid), []).append(grid)
    for name, grids in sorted(by_type.items()):
        if name not in shared:
            names.add(name)
            continue
        new_name = name + HOST_SUFFIX
        new_type = _own_type(doc, grids[0], new_name)
        names.add(new_name)
        report.new_types.append(u'"{0}"'.format(new_name))
        for grid in grids:
            try:
                grid.ChangeTypeId(new_type.Id)
                report.retyped += 1
            except Exception:
                names.add(name)                            # e.g. a grid Revit will not let change: it keeps the shared name
    names.discard(u'')
    return names


def _visible_grids(doc, views):
    """{view id: ids of this model's grids Revit shows in it} - Revit's own answer, filters included."""
    seen = {}
    for view in views:
        try:
            seen[view.Id.Value] = set(grid.Id.Value for grid in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid))
        except Exception:
            seen[view.Id.Value] = set()
    return seen


def _linked_grids(doc, views, options, report):
    """Turn the grids of the linked models off ('hide') or on again ('show') in the views. Inside the caller's transaction."""
    report.linked = options.linked
    links = list(DB.FilteredElementCollector(doc).OfClass(DB.RevitLinkInstance))
    existing = _existing_filter(doc)
    if not links and (options.linked == 'hide' or existing is None):
        report.no_links = True
        return
    targets, seen = [], set()
    for view in views:
        target = _filter_target(doc, view)
        if target.Id.Value in seen:
            continue
        seen.add(target.Id.Value)
        targets.append(target)
        if target is not view:
            report.templates.append(target.Name)

    if options.linked == 'show':
        for target in targets:
            if existing is not None and target.IsFilterApplied(existing.Id):
                target.RemoveFilter(existing.Id)
                report.link_views += 1
        return

    # the grid type names used by the links
    link_types = set()
    for link in links:
        try:
            linked_doc = link.GetLinkDocument()
        except Exception:
            linked_doc = None
        if linked_doc is None:
            report.unloaded += 1
            continue
        for grid in DB.FilteredElementCollector(linked_doc).OfClass(DB.Grid):
            link_types.add(_type_name(linked_doc, grid))

    # the filter: every grid whose type is not one of this model's grid types
    # FIX: the first version used "Type Name" without checking it, and in Revit it hid this model's grids as
    # well. Now each possible rule is TRIED first on the grids themselves: it is used only if it passes no
    # grid of this model and every grid of the loaded links. Otherwise nothing is changed for the links.
    # FIX 2: the grids of this model were given their own type BEFORE any rule was tried (713 grids in one
    # model). Now the rules are tried with the types as they are; the grids are retyped only if no rule works.
    grids_category = DB.ElementId(DB.BuiltInCategory.OST_Grids)
    host_grids = list(DB.FilteredElementCollector(doc).OfClass(DB.Grid))
    element_filter = None
    if host_grids:
        names = set(_type_name(doc, grid) for grid in host_grids)
        names.discard(u'')
        element_filter, why = _working_filter(doc, grids_category, host_grids, names, links)
        shared = sorted(names & link_types)
        if element_filter is None and options.own_type and shared:
            names = _give_own_types(doc, host_grids, shared, report)
            try:
                doc.Regenerate()                           # the new types count before the rules are tried again
            except Exception:
                pass
            element_filter, why = _working_filter(doc, grids_category, host_grids, names, links)
        if element_filter is None:
            report.link_problem = (u'no view filter could be found that hides the linked grids and keeps this model\'s grids '
                                   u'({0}). Nothing was changed for the links.'.format(why))
            return
    if existing is None:
        if element_filter is not None:
            existing = DB.ParameterFilterElement.Create(doc, LINK_FILTER, _ids([grids_category]), element_filter)
        else:
            existing = DB.ParameterFilterElement.Create(doc, LINK_FILTER, _ids([grids_category]))   # no own grids: every grid
    else:
        existing.SetCategories(_ids([grids_category]))
        if element_filter is not None:
            existing.SetElementFilter(element_filter)
        else:
            existing.ClearRules()
    for target in targets:
        if not target.IsFilterApplied(existing.Id):
            target.AddFilter(existing.Id)
        target.SetFilterVisibility(existing.Id, False)
        report.link_views += 1
    # links shown "By linked view" ignore the filters of this model
    named = set()
    for view in views:
        for link in links:
            try:
                settings = view.GetLinkOverrides(link.Id)
                if settings is None:
                    continue
                kind = settings.LinkVisibilityType
                if kind == DB.LinkVisibility.ByLinkView or (kind == DB.LinkVisibility.Custom and settings.ViewFilterType == DB.LinkVisibility.ByLinkView):
                    label = u'{0} in {1}'.format(link.Name, view.Name)
                    if label not in named:
                        named.add(label)
                        report.by_link_view.append(label)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# adjusting - the only place where the model is changed
# ---------------------------------------------------------------------------
def _curve_in_view(grid, view):
    """The one curve of the grid in the view (its 2D extent if it has one, else its 3D extent); None for a
    grid made of several segments."""
    for kind in (DB.DatumExtentType.ViewSpecific, DB.DatumExtentType.Model):
        try:
            curves = list(grid.GetCurvesInView(kind, view))
        except Exception:
            curves = []
        if len(curves) == 1:
            return curves[0]
        if len(curves) > 1:
            return None
    return None


def _model_curve(grid, view):
    """The grid as drawn in the model (its 3D extents) in this view; None for several segments."""
    try:
        curves = list(grid.GetCurvesInView(DB.DatumExtentType.Model, view))
    except Exception:
        return None
    return curves[0] if len(curves) == 1 else None


def _shown(curve, back, crop):
    """True when the curve (the grid's current ends in the view) lies at least partly inside the crop region."""
    a, b = back.OfPoint(curve.GetEndPoint(0)), back.OfPoint(curve.GetEndPoint(1))
    if not isinstance(curve, DB.Arc):
        return ga_geometry.crosses((a.X, a.Y), (b.X, b.Y), crop)
    centre, middle = back.OfPoint(curve.Center), back.OfPoint(curve.Evaluate(0.5, True))
    start_angle = math.atan2(a.Y - centre.Y, a.X - centre.X)
    to_middle = (math.atan2(middle.Y - centre.Y, middle.X - centre.X) - start_angle) % (2.0 * math.pi)
    to_end = (math.atan2(b.Y - centre.Y, b.X - centre.X) - start_angle) % (2.0 * math.pi)
    sweep = to_end if to_middle <= to_end else to_end - 2.0 * math.pi
    return ga_geometry.clip_arc((centre.X, centre.Y), curve.Radius, start_angle, sweep, crop) is not None


def _bubble(grid, end_of_grid, view, wanted):
    """Switch one bubble on or off - only when it is not in that state already. False when Revit refuses."""
    try:
        if bool(grid.IsBubbleVisibleInView(end_of_grid, view)) == bool(wanted):
            return True
    except Exception:
        pass
    try:
        if wanted:
            grid.ShowBubbleInView(end_of_grid, view)
        else:
            grid.HideBubbleInView(end_of_grid, view)
        return True
    except Exception:
        return False


ANGLED_DEGREES = 2.0                      # a grid more than this off the view's axes is an angled (radial) grid
BUBBLE_ORDER = (ga_geometry.BOTTOM, ga_geometry.LEFT, ga_geometry.RIGHT, ga_geometry.TOP)   # where an angled grid's bubble goes first


def _one_bubble(grid, view, start, end, a, b, plain_box, options):
    """An angled grid that reaches the frame at both ends on sides with bubbles gets ONE bubble, at its own side:
    the end that had the bubble before, else the first of BUBBLE_ORDER. The other end stops on the line of the
    sides without bubbles (plain distance) and gets no bubble. Returns the new (start, end)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length < 1e-9 or start[2] is None or end[2] is None:
        return start, end
    if min(abs(dx), abs(dy)) / length < math.sin(math.radians(ANGLED_DEGREES)):
        return start, end                                   # a straight grid: as it is
    if start[2] not in options.bubble_sides or end[2] not in options.bubble_sides:
        return start, end                                   # it has one bubble side already
    had = []
    for which, end_of_grid in ((0, DB.DatumEnds.End0), (1, DB.DatumEnds.End1)):
        try:
            had.append(bool(grid.IsBubbleVisibleInView(end_of_grid, view)))
        except Exception:
            had.append(False)
    if had[0] != had[1]:
        keep = 0 if had[0] else 1                           # the end the drawing had its bubble at
    else:
        rank = dict((side, n) for n, side in enumerate(BUBBLE_ORDER))
        keep = 0 if rank.get(start[2], 9) <= rank.get(end[2], 9) else 1
    plain = ga_geometry.clip_line(a, b, plain_box)
    if plain is None:
        return start, end
    if keep == 0:
        return start, (plain[1][0], plain[1][1], plain[1][2] + u'-plain')
    return (plain[0][0], plain[0][1], plain[0][2] + u'-plain'), end


JOINT = 1e-3                              # feet: two segment ends this close are one joint of a multi-segment grid


def _joints(doc):
    """{grid id: {0, 1}} - the ends of the segments of multi-segment grids that are joined to another segment.
    FIX: every segment of a multi-segment (radial) grid is a Grid of its own, and each was lined up on its own:
    inner segments were stretched across the view and got bubbles at the joints."""
    found = {}
    try:
        groups = list(DB.FilteredElementCollector(doc).OfClass(DB.MultiSegmentGrid))
    except Exception:
        return found
    found['groups'] = []
    for group in groups:
        try:
            segments = [doc.GetElement(i) for i in group.GetGridIds()]
        except Exception:
            continue
        ends = []
        for segment in segments:
            try:
                curve = segment.Curve
                ends.append((segment, [curve.GetEndPoint(0), curve.GetEndPoint(1)]))
            except Exception:
                pass
        found['groups'].append([segment for segment, points in ends])
        for segment, points in ends:
            for k, p in enumerate(points):
                for other, others in ends:
                    if other is segment:
                        continue
                    if any(abs(p.X - q.X) < JOINT and abs(p.Y - q.Y) < JOINT for q in others):
                        found.setdefault(segment.Id.Value, set()).add(k)
    return found


FOCUS = 0.05                              # feet: grid ends this close together are one point (the centre of a fan of radial grids)


def _focus_ends(grids, view, back, joints, options):
    """{grid id: {0, 1}} - the ends that two or more grids of the view share: the centre of a fan of radial grids.
    FIX: grids SE4 ... SE6 all start in one point at the bowl; "line up ALL" drew those inner ends through the centre
    of the fan up to the opposite frame. An end shared like this is never moved (unless it leaves the view)."""
    points = []
    for grid in grids:
        curve = (_model_curve(grid, view) if options.from_model else None) or _curve_in_view(grid, view)
        if curve is None:
            continue
        for k in (0, 1):
            if k in joints.get(grid.Id.Value, ()):
                continue                                    # a joint of a multi-segment grid is not a fan centre
            p, q = back.OfPoint(curve.GetEndPoint(k)), back.OfPoint(curve.GetEndPoint(1 - k))
            length = math.hypot(q.X - p.X, q.Y - p.Y) or 1.0
            points.append((grid.Id.Value, k, p.X, p.Y, (q.X - p.X) / length, (q.Y - p.Y) / length))   # the way the grid leaves this end
    found = {}
    cells = {}
    for item in points:
        cells.setdefault((int(math.floor(item[2] / FOCUS)), int(math.floor(item[3] / FOCUS))), []).append(item)
    turn = math.cos(math.radians(2.0))
    for gid, k, x, y, dx, dy in points:
        i, j = int(math.floor(x / FOCUS)), int(math.floor(y / FOCUS))
        # FIX: grids YY and N1.2 lie on one line and end in the same point: that is not a fan. A fan centre has
        # grids leaving it in DIFFERENT directions.
        others = [o for di in (-1, 0, 1) for dj in (-1, 0, 1) for o in cells.get((i + di, j + dj), ())
                  if o[0] != gid and abs(o[2] - x) < FOCUS and abs(o[3] - y) < FOCUS and o[4] * dx + o[5] * dy < turn]
        if others:
            found.setdefault(gid, set()).add(k)
    return found


def _free_ends(joints, view, back, box, crop, near, options, view_crop=None):
    """{grid id: set of its ends (0 / 1) that go out to the frame} for the segments of multi-segment grids:
    a free end of the grid that leaves the view (or stops at its edge), and a joint through which the grid leaves
    the view (the segment is cut at the frame there). An end that stops inside the view stays where it is - also
    with "line up ALL": multi-segment grids (round the bowl, radial) end inside the view on purpose."""
    out, cuts = {}, set()
    for segments in joints.get('groups', []):
        ends, total = [], 0.0
        for segment in segments:
            curve = (_model_curve(segment, view) if options.from_model else None) or _curve_in_view(segment, view)
            if curve is None:
                continue
            a, b = back.OfPoint(curve.GetEndPoint(0)), back.OfPoint(curve.GetEndPoint(1))
            total += math.hypot(b.X - a.X, b.Y - a.Y)
            on = ga_geometry.clip_line((a.X, a.Y), (b.X, b.Y), box)
            if on is None:
                continue
            for k, p in enumerate(((a.X, a.Y), (b.X, b.Y))):
                if k in joints.get(segment.Id.Value, ()):
                    # the grid leaves the view through this joint (it lies outside the crop region, or at its edge,
                    # and the segment is seen in the view): in this view the segment is CUT at the frame there
                    # FIX: with "outermost grids" the joint is tested against the REAL crop region - ring grid L lies
                    # along the outermost-grid edge, and its joints there were taken for "leaving the view"
                    edge = view_crop or crop
                    if not ga_geometry.well_inside(p, edge, near) and ga_geometry.crosses((a.X, a.Y), (b.X, b.Y), edge):
                        out.setdefault(segment.Id.Value, set()).add(k)
                        cuts.add((segment.Id.Value, k))
                    continue
                ends.append((segment.Id.Value, k, math.hypot(on[k][0] - p[0], on[k][1] - p[1]), ga_geometry.well_inside(p, crop, near),
                             math.hypot(b.X - a.X, b.Y - a.Y)))
        # the grid's outer end: a free end that leaves the view already - else the inside free end nearest to the frame
        nearest = None if any(not e[3] for e in ends) else (min([e[2] for e in ends]) if ends else None)
        for gid, k, distance, inside, own in ends:
            # the other end only when that is short compared with its own segment
            # FIX: "line up ALL" drew the end of a U-shaped grid round the bowl (stopping inside the view on purpose)
            # down to the frame. A multi-segment grid is never stretched: only an end that leaves the view (or stops
            # at its edge) is lined up.
            if not inside:
                out.setdefault(gid, set()).add(k)
    out['cuts'] = cuts
    return out


def _adjust_grid(grid, view, box, transform, back, options, crop=None, near=0.0, report=None, plain_box=None, joined=None,
                 going=None, focus=None, short=None, boundary=None, scale=1.0):
    """Give one grid its new ends in one view. Returns 'line', 'arc', 'outside' or raises with the reason.
    crop: the crop region itself (box is the frame around it); near: ends this close to the crop edge count as leaving it."""
    curve = _curve_in_view(grid, view)
    if curve is None:
        raise ValueError(u'a grid of several segments')
    crop = crop if crop is not None else box
    current = curve                                         # the grid's ends in the view as they are now
    if options.from_model:
        # FIX: a run of "line up ALL" stretched the inner ends of radial grids across the whole view; their 2D ends
        # no longer knew where the grid really starts. Start from the grid as drawn in the model (3D extents) -
        # but only for a grid that is SHOWN in the view (one hidden by dragging its 2D ends out stays hidden).
        model = _model_curve(grid, view)
        if model is not None and isinstance(model, DB.Arc) == isinstance(curve, DB.Arc):
            if not _shown(curve, back, crop):
                return 'outside'
            curve = model
    a, b = back.OfPoint(curve.GetEndPoint(0)), back.OfPoint(curve.GetEndPoint(1))
    kind = 'line'
    if isinstance(curve, DB.Arc):
        kind = 'arc'
        centre = back.OfPoint(curve.Center)
        middle = back.OfPoint(curve.Evaluate(0.5, True))
        start_angle = math.atan2(a.Y - centre.Y, a.X - centre.X)
        to_middle = (math.atan2(middle.Y - centre.Y, middle.X - centre.X) - start_angle) % (2.0 * math.pi)
        to_end = (math.atan2(b.Y - centre.Y, b.X - centre.X) - start_angle) % (2.0 * math.pi)
        # anticlockwise when the middle point is met before the end going anticlockwise
        sweep = to_end if to_middle <= to_end else to_end - 2.0 * math.pi
        # only a curved grid that is shown inside the crop region
        inside_crop = ga_geometry.clip_arc((centre.X, centre.Y), curve.Radius, start_angle, sweep, crop)
        found = ga_geometry.clip_arc((centre.X, centre.Y), curve.Radius, start_angle, sweep, box) if inside_crop is not None else None
        if found is None:
            return 'outside'
        start, end, half = found
        if True:
            # an end that stops well inside the view stays where it is (see ga_geometry.keep_ends)
            keep_start, keep_end = ga_geometry.keep_ends((a.X, a.Y), (b.X, b.Y), start, end, crop, near, options.lengthen, short)
            if focus:                                       # the centre of a fan of grids stays where it is
                keep_start = keep_start or (0 in focus and ga_geometry.well_inside((a.X, a.Y), crop, near))
                keep_end = keep_end or (1 in focus and ga_geometry.well_inside((b.X, b.Y), crop, near))
            if joined:                                      # a segment of a multi-segment grid: its joints stay
                keep_start, keep_end = 0 not in (going or ()), 1 not in (going or ())
                if keep_start and keep_end:
                    return 'joined'
            if keep_start and keep_end:
                return 'outside'
            if keep_start or keep_end:
                if keep_start:
                    start = (a.X, a.Y, None)
                if keep_end:
                    end = (b.X, b.Y, None)
                # a point of the circle between the two new ends, on the side of the old middle
                angle_a, angle_b = math.atan2(start[1] - centre.Y, start[0] - centre.X), math.atan2(end[1] - centre.Y, end[0] - centre.X)
                turn = 1.0 if sweep >= 0 else -1.0
                between = angle_a + turn * (((turn * (angle_b - angle_a)) % (2.0 * math.pi)) / 2.0)
                half = (centre.X + curve.Radius * math.cos(between), centre.Y + curve.Radius * math.sin(between))
        new_curve = DB.Arc.Create(transform.OfPoint(DB.XYZ(start[0], start[1], a.Z)), transform.OfPoint(DB.XYZ(end[0], end[1], b.Z)),
                                  transform.OfPoint(DB.XYZ(half[0], half[1], (a.Z + b.Z) / 2.0)))
    else:
        if joined:
            # a segment of a multi-segment grid: the joint end(s) stay exactly where they are (no bubble there);
            # only a free end - the end of the whole grid - is lined up on the frame
            if not ga_geometry.crosses((a.X, a.Y), (b.X, b.Y), crop):
                return 'outside'
            on = ga_geometry.clip_line((a.X, a.Y), (b.X, b.Y), box)
            if on is None:
                return 'outside'
            # which ends go out was decided for the whole grid (_free_ends): free ends, and joints where it leaves the view
            free = [k in (going or ()) for k in (0, 1)]
            start = on[0] if free[0] else (a.X, a.Y, None)
            end = on[1] if free[1] else (b.X, b.Y, None)
            if start[2] is None and end[2] is None:
                # a middle segment: left as it is - unless an earlier run stretched it in this view (its ends here
                # are not those of the model any more): then it is put back on its joints
                c0, c1 = back.OfPoint(current.GetEndPoint(0)), back.OfPoint(current.GetEndPoint(1))
                moved = max(abs(c0.X - a.X), abs(c0.Y - a.Y), abs(c1.X - b.X), abs(c1.Y - b.Y))
                if current is curve or moved < JOINT:
                    return 'joined'
            found = (start, end)
            kind = 'segment'
        else:
            found = ga_geometry.adjust_line((a.X, a.Y), (b.X, b.Y), crop, box, near, options.lengthen, short)
            if found is not None and focus:
                # the centre of a fan of radial grids stays where it is (unless it lies outside the view)
                start, end = found
                if 0 in focus and ga_geometry.well_inside((a.X, a.Y), crop, near):
                    start = (a.X, a.Y, None)
                if 1 in focus and ga_geometry.well_inside((b.X, b.Y), crop, near):
                    end = (b.X, b.Y, None)
                found = None if (start[2] is None and end[2] is None) else (start, end)
        if found is None:
            return 'outside'
        start, end = found
        if options.bubbles and plain_box is not None and not joined:
            # angled grids: a bubble only at their own side (the user's choice), the other end on the plain line
            one = _one_bubble(grid, view, start, end, (a.X, a.Y), (b.X, b.Y), plain_box, options)
            if one != (start, end):
                start, end = one
                kind = 'angled'
        if boundary and not joined:
            # an end that runs on past the outermost ring grid stops the side's distance beyond its curve
            def distance(side):
                plain = side is None or side.endswith(u'-plain') or side not in options.bubble_sides
                return (options.plain_mm if plain else options.bubble_mm) * MM * scale
            segments, ring_sides = boundary
            def on_ring_side(side):
                return side is not None and side.replace(u'-plain', u'') in ring_sides
            if on_ring_side(end[2]):
                end = ga_geometry.stop_at_boundary(start, end, segments, distance(end[2]))
            if on_ring_side(start[2]):
                start = ga_geometry.stop_at_boundary(end, start, segments, distance(start[2]))
        new_curve = DB.Line.CreateBound(transform.OfPoint(DB.XYZ(start[0], start[1], a.Z)), transform.OfPoint(DB.XYZ(end[0], end[1], b.Z)))
    # 2D (view-specific) ends: only this view changes, the grid in the model stays as it is
    for end_of_grid in (DB.DatumEnds.End0, DB.DatumEnds.End1):
        grid.SetDatumExtentType(end_of_grid, view, DB.DatumExtentType.ViewSpecific)
    grid.SetCurveInView(DB.DatumExtentType.ViewSpecific, view, new_curve)
    if options.bubbles:
        for end_of_grid, side in ((DB.DatumEnds.End0, start[2]), (DB.DatumEnds.End1, end[2])):
            if side is None:
                continue                                    # this end was not moved: its bubble stays as it is
            if not _bubble(grid, end_of_grid, view, side in options.bubble_sides) and report is not None:
                report.bubble_trouble += 1
    return kind


def _outer_grids(grids, view, back, crop_box, options):
    """(x low, y low, x high, y high) in crop coordinates: the outermost vertical grids (left / right) and the
    outermost horizontal grids (bottom / top) shown in the crop region. A side without such a grid - or whose
    outermost grid lies outside the crop - keeps the crop edge. Angled, curved and hidden grids do not count."""
    return _outer_grids_and_ids(grids, view, back, crop_box, options)[0]


def _outer_grids_and_ids(grids, view, back, crop_box, options):
    """(_outer_grids box, ids of the grids that set its edges)."""
    xs, ys = [], []
    tilt = math.sin(math.radians(ANGLED_DEGREES))
    for grid in grids:
        shown = _curve_in_view(grid, view)
        if shown is None or isinstance(shown, DB.Arc):
            continue
        a, b = back.OfPoint(shown.GetEndPoint(0)), back.OfPoint(shown.GetEndPoint(1))
        if not ga_geometry.crosses((a.X, a.Y), (b.X, b.Y), crop_box):
            continue                                       # hidden from this view (2D ends dragged out of it)
        dx, dy = b.X - a.X, b.Y - a.Y
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        if abs(dx) / length < tilt:
            xs.append(((a.X + b.X) / 2.0, grid.Id.Value))  # a vertical grid
        elif abs(dy) / length < tilt:
            ys.append(((a.Y + b.Y) / 2.0, grid.Id.Value))  # a horizontal grid
    ids = {}                                               # side -> id of the grid that sets it
    for side, found in ((ga_geometry.LEFT, min(xs) if xs else None), (ga_geometry.RIGHT, max(xs) if xs else None),
                        (ga_geometry.BOTTOM, min(ys) if ys else None), (ga_geometry.TOP, max(ys) if ys else None)):
        if found is not None:
            ids[side] = found[1]
    low_x = max(crop_box[0], min(xs)[0]) if xs else crop_box[0]
    high_x = min(crop_box[2], max(xs)[0]) if xs else crop_box[2]
    low_y = max(crop_box[1], min(ys)[0]) if ys else crop_box[1]
    high_y = min(crop_box[3], max(ys)[0]) if ys else crop_box[3]
    if high_x - low_x < 1e-6:
        low_x, high_x = crop_box[0], crop_box[2]
    if high_y - low_y < 1e-6:
        low_y, high_y = crop_box[1], crop_box[3]
    # FIX: the outermost grids themselves (YY, the straight tail of L) lie ON this edge - a rounding difference put
    # them "outside" and they were skipped. The edge is moved out by a hair (0.3 mm) so they count as inside.
    pad = 1e-3
    return (low_x - pad, low_y - pad, high_x + pad, high_y + pad), ids


def _boundary(joints, ids, back):
    """The segments ((x, y), (x, y)) of the multi-segment grids that set an edge of the outermost-grid frame (ring
    grid L: its straight tails are the outermost grids, its curve runs round the bowl between them).
    Returns (segments, the sides those grids set) - only ends on those sides are stopped at the ring."""
    segments, sides = [], set()
    for group in joints.get('groups', []):
        try:
            members = set(segment.Id.Value for segment in group)
        except Exception:
            continue
        here = [side for side, gid in ids.items() if gid in members]
        if not here:
            continue
        sides.update(here)
        for segment in group:
            try:
                curve = segment.Curve
                a, b = back.OfPoint(curve.GetEndPoint(0)), back.OfPoint(curve.GetEndPoint(1))
                segments.append(((a.X, a.Y), (b.X, b.Y)))
            except Exception:
                pass
    return segments, sides


DETAILS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'GridAdjust', 'last_run.txt')


def _ends_text(curve, back):
    """'(x, y) -> (x, y)' of a curve in the crop region's own coordinates (feet), for the details file."""
    try:
        a, b = back.OfPoint(curve.GetEndPoint(0)), back.OfPoint(curve.GetEndPoint(1))
        return u'({0:.2f}, {1:.2f}) -> ({2:.2f}, {3:.2f}){4}'.format(a.X, a.Y, b.X, b.Y, u' arc' if isinstance(curve, DB.Arc) else u'')
    except Exception:
        return u'-'


def _view_text(doc, view, crop_box):
    parts = [u'VIEW "{0}" id {1} | type {2} | scale 1:{3} | crop {4}'.format(
        view.Name, view.Id.Value, view.ViewType, view.Scale, u'({0:.2f}, {1:.2f}) - ({2:.2f}, {3:.2f})'.format(*crop_box))]
    try:
        parts.append(u'crop turned {0:.2f} deg'.format(math.degrees(math.atan2(view.CropBox.Transform.BasisX.Y, view.CropBox.Transform.BasisX.X))))
    except Exception:
        pass
    try:
        primary = view.GetPrimaryViewId()
        if primary is not None and primary != DB.ElementId.InvalidElementId:
            parts.append(u'dependent view of "{0}"'.format(doc.GetElement(primary).Name))
    except Exception:
        pass
    try:
        box = view.get_Parameter(DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP).AsElementId()
        if box is not None and box != DB.ElementId.InvalidElementId:
            parts.append(u'scope box "{0}"'.format(doc.GetElement(box).Name))
    except Exception:
        pass
    return u' | '.join(parts)


def _grid_text(doc, grid, view, back, outcome, joined, going, focus_of=None):
    parts = [u'  {0:<10} {1:<9}'.format(grid.Name, outcome)]
    if focus_of is not None and focus_of.get(grid.Id.Value):
        parts.append(u'fan centre at end {0}'.format(sorted(focus_of[grid.Id.Value])))
    try:
        box = grid.get_Parameter(DB.BuiltInParameter.DATUM_VOLUME_OF_INTEREST).AsElementId()
        if box is not None and box != DB.ElementId.InvalidElementId:
            parts.append(u'scope box "{0}"'.format(doc.GetElement(box).Name))
    except Exception:
        pass
    if joined:
        parts.append(u'joints {0} out {1}'.format(sorted(joined), sorted(going or ())))
    model = _model_curve(grid, view)
    if model is not None:
        parts.append(u'model ' + _ends_text(model, back))
    return u' | '.join(parts)


def _write_details(lines):
    try:
        folder = os.path.dirname(DETAILS)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(DETAILS, 'w', encoding='utf-8') as handle:
            handle.write(u'\n'.join(lines) + u'\n')
    except Exception:
        pass                                               # the details are only a help


def adjust(doc, views, options):
    report = _adjust(doc, views, options)
    _write_details([u'Adjust Grids - options: sides {0}, bubble {1} mm, plain {2} mm, bubbles {3}, line up ALL {4}, from model {5}, '
                    u'to outermost grids {6}'.format(options.bubble_sides, options.bubble_mm, options.plain_mm, options.bubbles,
                                                     options.lengthen, options.from_model, options.to_grids)]
        + report.lines() + [u''] + report.notes)
    return report


def _adjust(doc, views, options):
    """Adjust the grids of the views in one transaction. Returns a Report."""
    report = Report()
    report.adjusted = bool(options.adjust)
    name = u'Adjust Grids' if options.adjust else (u'Linked Grids Off' if options.linked == 'hide' else u'Linked Grids On')
    transaction = DB.Transaction(doc, name)
    transaction.Start()
    try:

        joints = _joints(doc) if options.adjust else {}     # the joints of multi-segment grids
        for view in (views if options.adjust else []):
            if not view.CropBoxActive:
                report.not_cropped.append(view.Name)
                continue
            crop = view.CropBox
            transform = crop.Transform
            back = transform.Inverse
            scale = float(view.Scale or 1)
            crop_box = (crop.Min.X, crop.Min.Y, crop.Max.X, crop.Max.Y)
            in_view = list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid))
            report.notes.append(_view_text(doc, view, crop_box))
            view_crop = crop_box
            boundary = None
            if options.to_grids:
                # NEW: the ends line up just outside the outermost grids (the last vertical grid on the left / right,
                # the last horizontal grid at the bottom / top) instead of outside the crop edge
                outer, edge_ids = _outer_grids_and_ids(in_view, view, back, crop_box, options)
                report.notes.append(u'  outermost grids: ({0:.2f}, {1:.2f}) - ({2:.2f}, {3:.2f})'.format(*outer))
                crop_box = outer
                # NEW 2: when an outermost grid is a ring (multi-segment grid L round the bowl), the grids crossing its
                # curve stop just beyond the curve, not on the straight frame
                segments, ring_sides = _boundary(joints, edge_ids, back)
                if segments:
                    boundary = (segments, ring_sides)
                    report.notes.append(u'  boundary: {0} segment(s) of the outermost ring grid, sides {1}'.format(
                        len(segments), sorted(ring_sides)))
            box = ga_geometry.frame(crop_box, options.bubble_sides, options.bubble_mm * MM * scale, options.plain_mm * MM * scale)
            # an end that stops this close to the crop edge is lined up with the ones that leave the crop
            near = max(options.bubble_mm, options.plain_mm, 1.0) * MM * scale
            plain_box = ga_geometry.frame(crop_box, [], options.plain_mm * MM * scale, options.plain_mm * MM * scale)
            going = _free_ends(joints, view, back, box, crop_box, near, options, view_crop) if joints else {}
            report.cut += len(going.get('cuts', ()))
            done = 0
            focus = _focus_ends(in_view, view, back, joints, options) if options.lengthen else {}
            for grid in in_view:
                report.seen += 1
                outcome = u'error'
                before = _curve_in_view(grid, view)
                try:
                    outcome = _adjust_grid(grid, view, box, transform, back, options, crop_box, near, report, plain_box,
                                           joints.get(grid.Id.Value), going.get(grid.Id.Value), focus.get(grid.Id.Value),
                                           SHORT_MM * MM * scale, boundary, scale)
                    try:
                        after = _curve_in_view(grid, view)
                        report.notes.append(_grid_text(doc, grid, view, back, outcome, joints.get(grid.Id.Value), going.get(grid.Id.Value), focus)
                                            + u' | before ' + (_ends_text(before, back) if before is not None else u'-')
                                            + u' | after ' + (_ends_text(after, back) if after is not None else u'-'))
                    except Exception:
                        pass
                    if outcome == 'outside':
                        report.outside += 1
                        continue
                    if outcome == 'joined':
                        report.joined += 1                  # a middle segment of a multi-segment grid: left as it is
                        continue
                    report.segments += 1 if outcome == 'segment' else 0
                    done += 1
                    report.curved += 1 if outcome == 'arc' else 0
                    report.angled += 1 if outcome == 'angled' else 0
                except Exception as error:
                    reason = (u'{0}'.format(error).splitlines() or [u'error'])[0]
                    report.skipped.append((grid.Name, view.Name, reason))
                    try:
                        report.notes.append(_grid_text(doc, grid, view, back, u'FAILED', joints.get(grid.Id.Value), going.get(grid.Id.Value)) + u' | ' + reason)
                    except Exception:
                        pass
            report.grids += done
            report.views += 1 if done else 0
        if options.linked:
            # FIX: the links were done first; with the filter on, Revit no longer listed this model's grids in
            # the view, and "0 grid(s) adjusted" was the result. The grids of this model are adjusted first now.
            # The link part runs on its own: when it fails, the new grid types are taken back as well.
            before = _visible_grids(doc, views)
            step = DB.SubTransaction(doc)
            step.Start()
            _linked_grids(doc, views, options, report)
            if not report.link_problem and options.linked == 'hide':
                # Revit's own answer: does any grid of this model disappear from a view with the filter on?
                try:
                    doc.Regenerate()
                except Exception:
                    pass
                after = _visible_grids(doc, views)
                lost = [(view.Name, len(before[view.Id.Value] - after.get(view.Id.Value, set()))) for view in views]
                lost = [(name, count) for name, count in lost if count]
                if lost:
                    report.link_problem = (u'with the filter on, Revit also hides {0} of this model\'s grids ({1}). Nothing was changed '
                                           u'for the links - use Copy/Monitor or Visibility/Graphics > Revit Links > Custom > Grids for them.'.format(
                                               sum(count for name, count in lost), u', '.join(u'{0} in {1}'.format(c, n) for n, c in lost[:3])))
            if report.link_problem:
                step.RollBack()
                report.retyped, report.new_types, report.link_views, report.templates = 0, [], 0, []
            else:
                step.Commit()
        status = transaction.Commit()
        if status != DB.TransactionStatus.Committed:
            report.error = u'Revit did not keep the changes ({0}).'.format(status)
            report.grids = report.views = report.link_views = report.retyped = 0
    except Exception as error:
        try:
            if transaction.HasStarted() and not transaction.HasEnded():
                transaction.RollBack()
        except Exception:
            pass
        report.error = u'Nothing was changed: {0}'.format(error)
        report.grids = report.views = report.link_views = report.retyped = 0
    return report

# ---------------------------------------------------------------------------
# align the grid ends to detail lines drawn by the user
# ---------------------------------------------------------------------------
PARALLEL_DEGREES = 5.0                    # a grid closer than this to the direction of a detail line is not aligned to it


ALIGN_DETAILS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'EleTools', 'GridAdjust', 'last_align.txt')


class LineReport(object):
    def __init__(self):
        self.notes = []                   # one line per guide line and grid, for last_align.txt
        self.multi = []                   # names of the multi-segment grids left as they are
        self.lines = 0                    # straight detail lines used
        self.not_straight = 0             # arcs / splines among the picked lines (not used)
        self.views = 0                    # views in which at least one grid end was aligned
        self.grids = 0                    # grids aligned (one count per grid and view)
        self.ends = 0                     # grid ends moved onto a line
        self.missed = 0                   # grids that do not meet any of the lines
        self.curved = 0                   # curved grids (not aligned)
        self.skipped = []                 # (grid name, view name, reason)
        self.bubble_trouble = 0
        self.deleted = 0
        self.other_views = []             # names of the ticked views also worked on
        self.error = None

    def text(self):
        if self.error:
            return self.error
        out = [u'{0} grid end(s) of {1} grid(s) aligned to {2} detail line(s), in {3} view(s).'.format(
            self.ends, self.grids, self.lines, self.views)]
        if self.other_views:
            out.append(u'Also in: {0}.'.format(u', '.join(self.other_views[:6]) + (u' ...' if len(self.other_views) > 6 else u'')))
        if self.multi:
            out.append(u'{0} multi-segment grid(s) left as they are ({1}) - tick "Also multi-segment grids" to align their free ends.'.format(
                len(self.multi), u', '.join(self.multi[:8]) + (u' ...' if len(self.multi) > 8 else u'')))
        if self.missed:
            out.append(u'{0} grid(s) do not meet any of the lines (or run along them) - left as they are.'.format(self.missed))
        if self.curved:
            out.append(u'{0} curved grid(s) left as they are (only straight grids are aligned to a line).'.format(self.curved))
        if self.not_straight:
            out.append(u'{0} of the picked lines are splines / ellipses - not used (straight lines, circles and arcs are).'.format(self.not_straight))
        if self.bubble_trouble:
            out.append(u'{0} end(s) aligned, but Revit would not switch their bubble on.'.format(self.bubble_trouble))
        if self.deleted:
            out.append(u'The {0} detail line(s) were deleted.'.format(self.deleted))
        if self.skipped:
            reasons = {}
            for grid, view, reason in self.skipped:
                reasons.setdefault(reason, []).append(u'{0} ({1})'.format(grid, view))
            out.append(u'{0} grid(s) could not be aligned:'.format(len(self.skipped)))
            for reason, where in sorted(reasons.items())[:5]:
                out.append(u'   {0}: {1}'.format(reason, u', '.join(where[:6]) + (u' ...' if len(where) > 6 else u'')))
        return u'\n'.join(out)


def is_detail_line(element):
    """A detail line (any style) - what the user draws to show where the grid ends go."""
    try:
        if isinstance(element, DB.DetailLine):
            return True
    except Exception:
        pass
    try:
        return isinstance(element, DB.CurveElement) and element.CurveElementType == DB.CurveElementType.DetailCurve
    except Exception:
        return False


def _axes(view):
    """The view's right / up directions (the plane the grids are drawn in)."""
    try:
        return view.RightDirection, view.UpDirection
    except Exception:
        return DB.XYZ(1, 0, 0), DB.XYZ(0, 1, 0)


def _uv(point, axes):
    right, up = axes
    return (point.X * right.X + point.Y * right.Y + point.Z * right.Z,
            point.X * up.X + point.Y * up.Y + point.Z * up.Z)


def meet(a, b, c, d):
    """Where the (endless) grid line a-b meets the detail line c-d: (t along the grid, 0 = a, 1 = b; s along the
    detail line, 0 = c, 1 = d) - or None when they (almost) run the same way."""
    a, b, c, d = [(float(p[0]), float(p[1])) for p in (a, b, c, d)]
    gx, gy = b[0] - a[0], b[1] - a[1]
    lx, ly = d[0] - c[0], d[1] - c[1]
    cross = gx * ly - gy * lx
    lengths = math.hypot(gx, gy) * math.hypot(lx, ly)
    if lengths < 1e-12 or abs(cross) / lengths < math.sin(math.radians(PARALLEL_DEGREES)):
        return None
    t = ((c[0] - a[0]) * ly - (c[1] - a[1]) * lx) / cross
    s = ((c[0] - a[0]) * gy - (c[1] - a[1]) * gx) / cross
    return t, s


def _circle_uv(item, axes):
    """A circle / arc of the picked lines in the view's plane: (cu, cv, radius, start angle, sweep) - sweep None
    for a whole circle."""
    _kind, centre, radius, start, end, middle, bound = item
    cu, cv = _uv(centre, axes)
    if not bound:
        return (cu, cv, float(radius), 0.0, None)
    a, b, m = _uv(start, axes), _uv(end, axes), _uv(middle, axes)
    start_angle = math.atan2(a[1] - cv, a[0] - cu)
    to_middle = (math.atan2(m[1] - cv, m[0] - cu) - start_angle) % (2.0 * math.pi)
    to_end = (math.atan2(b[1] - cv, b[0] - cu) - start_angle) % (2.0 * math.pi)
    sweep = to_end if to_middle <= to_end else to_end - 2.0 * math.pi
    return (cu, cv, float(radius), start_angle, sweep)


def _on_arc(point, circle):
    cu, cv, radius, start_angle, sweep = circle
    if sweep is None:
        return True
    angle = math.atan2(point[1] - cv, point[0] - cu)
    if sweep >= 0:
        return (angle - start_angle) % (2.0 * math.pi) <= sweep + 1e-9
    return (start_angle - angle) % (2.0 * math.pi) <= -sweep + 1e-9


def circle_hits(a, b, circle):
    """[(end 0/1, t along the grid)] - where the grid a-b goes onto a circle / arc the user drew.
        one end inside the circle (a radial grid from the centre of the fan): only the OUTER end moves, onto the
            circle on its own side - the inner end stays;
        both ends inside: the end farther from the centre goes out onto the circle;
        both ends outside, the grid passing through the circle: both ends onto the circle (a chord);
        both ends outside, the grid not reaching it: the nearer end goes onto it."""
    a, b = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    cu, cv, radius = circle[0], circle[1], circle[2]
    dx, dy = b[0] - a[0], b[1] - a[1]
    fx, fy = a[0] - cu, a[1] - cv
    qa = dx * dx + dy * dy
    if qa < 1e-12:
        return []
    qb = 2.0 * (fx * dx + fy * dy)
    qc = fx * fx + fy * fy - radius * radius
    disc = qb * qb - 4.0 * qa * qc
    if disc <= 0:
        return []                                       # the grid's line misses the circle (or only touches it)
    root = math.sqrt(disc)
    t1, t2 = (-qb - root) / (2.0 * qa), (-qb + root) / (2.0 * qa)
    inside0 = math.hypot(fx, fy) < radius
    inside1 = math.hypot(b[0] - cu, b[1] - cv) < radius
    if inside0 and not inside1:
        wanted = [(1, t2)]
    elif inside1 and not inside0:
        wanted = [(0, t1)]
    elif inside0 and inside1:
        wanted = [(1, t2)] if math.hypot(b[0] - cu, b[1] - cv) >= math.hypot(fx, fy) else [(0, t1)]
    elif t1 < 1.0 and t2 > 0.0:
        wanted = [(0, t1), (1, t2)]
    else:
        wanted = [(1, t1)] if t1 >= 1.0 else [(0, t2)]
    return [(end, t) for end, t in wanted if _on_arc((a[0] + dx * t, a[1] + dy * t), circle)]


def _drawing_centre(view, grids, axes):
    """(u, v) of the middle of what the view shows: the centre of its crop region - else the middle of its grids.
    The grid keeps the part on this side of a detail line; the end beyond the line is the one that moves."""
    try:
        if view.CropBoxActive:
            box = view.CropBox
            middle = DB.XYZ((box.Min.X + box.Max.X) / 2.0, (box.Min.Y + box.Max.Y) / 2.0, (box.Min.Z + box.Max.Z) / 2.0)
            return _uv(box.Transform.OfPoint(middle), axes)
    except Exception:
        pass
    points = []
    for grid in grids:
        curve = _curve_in_view(grid, view)
        if curve is not None:
            for k in (0, 1):
                points.append(_uv(curve.GetEndPoint(k), axes))
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def align_to_lines(doc, view, line_elements, only_grids=None, views=None, bubble=True, delete=False, multi=False):
    """Move grid ends onto the detail lines the user drew: every straight grid of this model in the view that the
    (extended) grid line meets ON a detail line gets the end nearer to that point moved onto it - lengthened or
    shortened. A grid that meets two lines (one at each end) gets both ends aligned.

    view: the view the lines are drawn in; views: more views to do the same in (they must look the same way -
    plans of other floors - the line's position is taken over). only_grids: ids (numbers) - only those grids.
    bubble: switch the bubble ON at an aligned end (the other end is left as it is). delete: delete the detail
    lines afterwards. Only the views' own (2D) grid ends change, in ONE transaction (one Undo)."""
    report = LineReport()
    segments = []          # ('line', start, end) | ('arc', centre, radius, start, end, middle, bound)
    for element in line_elements:
        try:
            curve = element.GeometryCurve
        except Exception:
            curve = None
        if curve is not None and isinstance(curve, DB.Line):
            segments.append(('line', curve.GetEndPoint(0), curve.GetEndPoint(1)))
        elif curve is not None and isinstance(curve, DB.Arc):
            # FIX: a circle (or arc) drawn round a bowl of radial grids was refused - "not straight"
            bound = True
            try:
                bound = bool(curve.IsBound)
            except Exception:
                pass
            if bound:
                segments.append(('arc', curve.Center, curve.Radius, curve.GetEndPoint(0), curve.GetEndPoint(1),
                                 curve.Evaluate(0.5, True), True))
            else:
                segments.append(('arc', curve.Center, curve.Radius, None, None, None, False))
        else:
            report.not_straight += 1
    report.lines = len(segments)
    for item in segments:
        if item[0] == 'line':
            report.notes.append(u'guide: line ({0:.2f}, {1:.2f}) -> ({2:.2f}, {3:.2f})'.format(item[1].X, item[1].Y, item[2].X, item[2].Y))
        else:
            report.notes.append(u'guide: {0} centre ({1:.2f}, {2:.2f}) radius {3:.2f} ft ({4:.0f} mm)'.format(
                u'arc' if item[6] else u'circle', item[1].X, item[1].Y, item[2], item[2] / MM))
    if not segments:
        report.error = u'None of the picked elements is a detail line or circle / arc. Draw one where the grid ends should be, then pick it.'
        _write_align(report)
        return report
    targets = [view]
    look = getattr(view, 'ViewDirection', None)
    for other in views or []:
        if other is None or other.Id.Value == view.Id.Value:
            continue
        there = getattr(other, 'ViewDirection', None)
        if look is not None and there is not None and abs(look.X * there.X + look.Y * there.Y + look.Z * there.Z) < 0.999:
            continue                                   # a view looking another way: the line means nothing there
        targets.append(other)
        report.other_views.append(other.Name)
    joints = _joints(doc)
    # FIX: the straight tails of multi-segment ring grids (X0 ... X7 round the bowl) were stretched along their own
    # line out to the circle - long straight lines across the radial grids. A multi-segment grid is left alone
    # unless asked for (multi=True: then only its free ends, never a joint).
    segment_ids = set()
    for group_segments in joints.get('groups', []):
        for segment in group_segments:
            try:
                segment_ids.add(segment.Id.Value)
            except Exception:
                pass
    multi_seen = set()
    transaction = DB.Transaction(doc, 'Align grids to detail lines')
    transaction.Start()
    try:
        for target in targets:
            axes = _axes(target)
            lines, circles = [], []
            for item in segments:
                if item[0] == 'line':
                    lines.append((_uv(item[1], axes), _uv(item[2], axes)))
                else:
                    circles.append(_circle_uv(item, axes))
            done_here = 0
            in_view = list(DB.FilteredElementCollector(doc, target.Id).OfClass(DB.Grid))
            centre = _drawing_centre(target, in_view, axes)
            for grid in in_view:
                if only_grids and grid.Id.Value not in only_grids:
                    continue
                name = getattr(grid, 'Name', u'?')
                if grid.Id.Value in segment_ids and not multi:
                    multi_seen.add(name)
                    continue
                curve = _curve_in_view(grid, target)
                if curve is None:
                    report.skipped.append((name, target.Name, u'a grid of several segments'))
                    continue
                if isinstance(curve, DB.Arc):
                    report.curved += 1
                    continue
                a, b = curve.GetEndPoint(0), curve.GetEndPoint(1)
                ua, ub = _uv(a, axes), _uv(b, axes)
                best = {}
                length2 = (ub[0] - ua[0]) ** 2 + (ub[1] - ua[1]) ** 2
                # where the middle of the drawing lies along the grid: the grid keeps that side of a line
                # FIX: the end NEARER to the line was moved - a grid running far past a line near its other end had
                # its wrong end cut (grid 1: y -30 .. 200, line at y 70 -> the bottom end went to the line)
                middle_t = 0.5
                if centre is not None and length2 > 1e-12:
                    middle_t = ((centre[0] - ua[0]) * (ub[0] - ua[0]) + (centre[1] - ua[1]) * (ub[1] - ua[1])) / length2
                for uc, ud in lines:
                    hit = meet(ua, ub, uc, ud)
                    if hit is None:
                        continue
                    t, s = hit
                    if not (-1e-6 <= s <= 1.0 + 1e-6):
                        continue                       # the grid passes beside the drawn line
                    end = 0 if t < middle_t else 1
                    if end in joints.get(grid.Id.Value, ()):
                        continue                       # the joint of a multi-segment grid stays where it is
                    distance = abs(t - end)
                    if end not in best or distance < best[end][0]:
                        best[end] = (distance, t)
                for circle in circles:
                    for end, t in circle_hits(ua, ub, circle):
                        if end in joints.get(grid.Id.Value, ()):
                            continue
                        distance = abs(t - end)
                        if end not in best or distance < best[end][0]:
                            best[end] = (distance, t)
                if not best:
                    report.missed += 1
                    report.notes.append(u'{0} [{1}]: ({2:.2f}, {3:.2f}) -> ({4:.2f}, {5:.2f}) meets no guide - left'.format(
                        name, target.Name, a.X, a.Y, b.X, b.Y))
                    continue
                t0 = best[0][1] if 0 in best else 0.0
                t1 = best[1][1] if 1 in best else 1.0
                length = math.hypot(ub[0] - ua[0], ub[1] - ua[1])
                if (t1 - t0) * length < 10.0 * MM:
                    report.skipped.append((name, target.Name, u'the line(s) would leave the grid shorter than 10 mm'))
                    continue
                new_a = DB.XYZ(a.X + (b.X - a.X) * t0, a.Y + (b.Y - a.Y) * t0, a.Z + (b.Z - a.Z) * t0)
                new_b = DB.XYZ(a.X + (b.X - a.X) * t1, a.Y + (b.Y - a.Y) * t1, a.Z + (b.Z - a.Z) * t1)
                try:
                    for end_of_grid in (DB.DatumEnds.End0, DB.DatumEnds.End1):
                        grid.SetDatumExtentType(end_of_grid, target, DB.DatumExtentType.ViewSpecific)
                    grid.SetCurveInView(DB.DatumExtentType.ViewSpecific, target, DB.Line.CreateBound(new_a, new_b))
                except Exception as error:
                    report.skipped.append((name, target.Name, u'{0}'.format(error).strip().split(u'\n')[0]))
                    continue
                if bubble:
                    for end, end_of_grid in ((0, DB.DatumEnds.End0), (1, DB.DatumEnds.End1)):
                        if end in best and not _bubble(grid, end_of_grid, target, True):
                            report.bubble_trouble += 1
                report.grids += 1
                report.ends += len(best)
                done_here += 1
                report.notes.append(u'{0} [{1}]: ({2:.2f}, {3:.2f}) -> ({4:.2f}, {5:.2f})  NOW  ({6:.2f}, {7:.2f}) -> ({8:.2f}, {9:.2f})  end(s) moved: {10}'.format(
                    name, target.Name, a.X, a.Y, b.X, b.Y, new_a.X, new_a.Y, new_b.X, new_b.Y, u', '.join(str(k) for k in sorted(best))))
            if done_here:
                report.views += 1
        if delete and report.grids:
            for element in line_elements:
                try:
                    doc.Delete(element.Id)
                    report.deleted += 1
                except Exception:
                    pass
        # FIX: the result of Commit was not checked - a commit Revit refused was still reported as aligned
        status = transaction.Commit()
        if status != DB.TransactionStatus.Committed:
            report.error = u'Revit did not keep the changes ({0}).'.format(status)
            report.grids = report.ends = report.views = report.deleted = 0
    except Exception as error:
        try:
            transaction.RollBack()
        except Exception:
            pass
        report.error = u'Nothing was changed: {0}'.format(error)
        import traceback
        report.notes.append(traceback.format_exc())
    report.multi = sorted(multi_seen)
    if report.multi:
        report.notes.append(u'multi-segment grids left as they are: ' + u', '.join(report.multi))
    for grid_name, view_name, reason in report.skipped:
        report.notes.append(u'{0} [{1}]: not aligned - {2}'.format(grid_name, view_name, reason))
    _write_align(report)
    return report


def _write_align(report):
    """last_align.txt: the result, then every guide line and grid (model coordinates, feet)."""
    try:
        folder = os.path.dirname(ALIGN_DETAILS)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(ALIGN_DETAILS, 'w', encoding='utf-8') as handle:
            handle.write(report.text() + u'\n\n' + u'\n'.join(report.notes) + u'\n')
    except Exception:
        pass
