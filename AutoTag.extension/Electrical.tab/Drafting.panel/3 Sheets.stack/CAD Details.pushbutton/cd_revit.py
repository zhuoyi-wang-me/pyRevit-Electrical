# -*- coding: utf-8 -*-
"""CAD Details - creates the drafting views in Revit (the only module that changes the model).

One drafting view per detail, all native Revit elements:
    linework   detail lines (lines and arcs) - ALL in the one line style chosen by the user
    text       text notes; a text type per paper text size ("CAD 2.5 mm"), made from the default text type
    hatches    filled regions with invisible boundaries: solid hatches / arrowheads -> a solid black type,
               patterned hatches -> the type chosen by the user; a hatch Revit refuses is drawn as its outline
Each detail's bottom-left corner is put at the view's origin. Everything is ONE Undo (a transaction group).
"""
import math

from Autodesk.Revit.DB import (Arc, BuiltInCategory, BuiltInParameter, CurveArray, CurveLoop, ElementId,
                               ElementTypeGroup, FailureProcessingResult, FailureSeverity, FilledRegion,
                               FilledRegionType, FillPatternElement, FilteredElementCollector, GraphicsStyleType,
                               IFailuresPreprocessor, Line, TextNote, TextNoteOptions, TextNoteType, Transaction,
                               TransactionGroup, ViewDrafting, ViewFamily, ViewFamilyType, XYZ)

import cd_dxf

FT_PER_MM = 1.0 / 304.8
SCALES = [1, 2, 5, 10, 20, 25, 50, 100]
TEXT_STEP_MM = 0.5                     # paper text sizes are rounded to this
MIN_TEXT_MM = 1.0
INVISIBLE = u'<Invisible lines>'


MAX_SIZE_FT = 30000.0                  # a detail larger than this (about 9 km) is refused: Revit cannot draw that far out


class Options(object):
    def __init__(self, scale=10, line_style=None, text=True, fills=True, pattern_type=None, prefix=u'',
                 units_mm=1.0, cad_factor=1.0, text_type=None, single=False, single_name=u'CAD Details', cad_patterns=True):
        self.cad_patterns = cad_patterns       # True: each CAD hatch pattern is rebuilt as a Revit drafting fill pattern
        self.cad_factor = cad_factor           # the CAD is drawn this many times larger than it is in Revit (10: 4000 -> 400)
        self.text_type = text_type             # TextNoteType id for ALL texts; None: a type per CAD text height
        self.single = single                   # True: all details in ONE drafting view (CAD layout kept)
        self.single_name = single_name
        self.scale = scale                     # drafting view scale 1:scale
        self.line_style = line_style           # GraphicsStyle for all detail lines (None: the default)
        self.text = text
        self.fills = fills
        self.pattern_type = pattern_type       # FilledRegionType id for patterned hatches (None: the first one)
        self.prefix = prefix                   # put before every view name
        self.units_mm = units_mm               # millimetres per CAD drawing unit


class Report(object):
    def __init__(self):
        self.views = []                        # (view name, lines, texts, fills, fills drawn as outline)
        self.problems = []
        self.text_types = []
        self.pattern_types = []                # filled region types made from CAD hatch patterns

    def text(self):
        if not self.views:
            return u'No drafting view created.' + (u'\n' + u'\n'.join(self.problems) if self.problems else u'')
        out = [u'{0} drafting view(s) created:'.format(len(self.views))]
        for name, lines, texts, fills, outlined in self.views:
            out.append(u'   {0}: {1} lines, {2} texts, {3} filled regions{4}'.format(
                name, lines, texts, fills, u', {0} hatch(es) drawn as outline'.format(outlined) if outlined else u''))
        if self.text_types:
            out.append(u'Text types made: ' + u', '.join(self.text_types))
        if self.pattern_types:
            out.append(u'Fill patterns made from the CAD hatches: ' + u', '.join(self.pattern_types))
        if self.problems:
            out.append(u'Notes:')
            out += [u'   ' + p for p in self.problems[:12]]
        out.append(u'Undo (Ctrl+Z) removes all of it at once.')
        return u'\n'.join(out)


class _Quiet(IFailuresPreprocessor):
    """Revit's warnings ("line slightly off axis" ...) are dropped instead of shown one by one."""

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            if failure.GetSeverity() == FailureSeverity.Warning:
                accessor.DeleteWarning(failure)
        return FailureProcessingResult.Continue


def _start(doc, name):
    transaction = Transaction(doc, name)
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(_Quiet())
    transaction.SetFailureHandlingOptions(options)
    transaction.Start()
    return transaction


# ---------------------------------------------------------------------------
# what the model offers (for the window)
# ---------------------------------------------------------------------------
def line_styles(doc):
    """[(name, GraphicsStyle)] of the line styles, sorted; <Invisible lines> left out."""
    out = []
    lines = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
    for sub in lines.SubCategories:
        try:
            style = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
        except Exception:
            style = None
        if style is not None and sub.Name != INVISIBLE:
            out.append((sub.Name, style))
    return sorted(out, key=lambda item: item[0].lower())


def type_name(element):
    """The name of a Revit TYPE.
    FIX: in pyRevit's IronPython `type.Name` cannot be read for element types - every filled region type showed
    as "?" and the CAD patterns made earlier were not found again. The type-name parameter is read instead."""
    try:
        value = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString()
        if value:
            return value
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB import Element
        value = Element.Name.GetValue(element)
        if value:
            return value
    except Exception:
        pass
    try:
        return element.Name
    except Exception:
        return u'?'


def region_types(doc):
    """[(name, FilledRegionType)] sorted."""
    return sorted([(type_name(t), t) for t in FilteredElementCollector(doc).OfClass(FilledRegionType)],
                  key=lambda item: item[0].lower())


def text_types(doc):
    """[(name, TextNoteType)] sorted by name."""
    out = []
    for t in FilteredElementCollector(doc).OfClass(TextNoteType):
        out.append((type_name(t), t))
    return sorted(out, key=lambda item: item[0].lower())


def paper_mm(cad_height, options):
    """The paper size (mm) a CAD text height gets: CAD mm / CAD factor / view scale (250 / 10 / 10 = 2.5)."""
    return cad_height * options.units_mm / (options.cad_factor or 1.0) / options.scale


def _invisible_style(doc):
    lines = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
    for sub in lines.SubCategories:
        if sub.Name == INVISIBLE:
            try:
                return sub.GetGraphicsStyle(GraphicsStyleType.Projection)
            except Exception:
                return None
    return None


def _solid_type(doc, report):
    """A filled region type with a solid foreground fill (made from another type when the model has none)."""
    solid_pattern = None
    for pattern in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            if pattern.GetFillPattern().IsSolidFill:
                solid_pattern = pattern
                break
        except Exception:
            continue
    types = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
    for t in types:
        try:
            if solid_pattern is not None and t.ForegroundPatternId == solid_pattern.Id:
                return t
        except Exception:
            continue
    if not types or solid_pattern is None:
        return types[0] if types else None
    made = types[0].Duplicate(u'CAD Solid')
    made.ForegroundPatternId = solid_pattern.Id
    report.problems.append(u'Filled region type "CAD Solid" made (the model had no solid one).')
    return made


MIN_PAPER_MM = 0.1                     # no pattern line spacing / dash on paper smaller than this


def pattern_grids(lines, k_paper):
    """Revit fill grids of a CAD pattern: [(angle rad, origin u, origin v, offset, shift, [segments])] in paper feet.
    lines: the hatch's own definition (cd_dxf), k_paper: paper feet per drawing unit.
    A DXF pattern line gives its direction, a base point and the step to the next parallel line (offset vector);
    the step splits into the spacing (square to the lines) and the shift (along them). Dashes: > 0 line, < 0 gap,
    0 dot - Revit wants line, gap, line, gap ... so the list is turned round to start with a line (the origin
    moves along with it) and neighbours of one kind are joined."""
    grids = []
    if not lines:
        return grids
    bx0, by0 = lines[0][1], lines[0][2]
    tiny = MIN_PAPER_MM / 304.8
    for angle_deg, bx, by, ox, oy, dashes in lines:
        a = math.radians(angle_deg)
        ux, uy = math.cos(a), math.sin(a)
        spacing = (-ox * uy + oy * ux) * k_paper
        shift = (ox * ux + oy * uy) * k_paper
        if spacing < 0:
            spacing, shift = -spacing, -shift
        if spacing < tiny:
            spacing = tiny
        origin_u, origin_v = (bx - bx0) * k_paper, (by - by0) * k_paper
        segments = []
        if dashes:
            parts = []                                      # (is_line, length)
            for d in dashes:
                is_line = d >= 0
                length = max(abs(d) * k_paper, tiny) if is_line else abs(d) * k_paper
                if parts and parts[-1][0] == is_line:
                    parts[-1] = (is_line, parts[-1][1] + length)
                else:
                    parts.append((is_line, length))
            if len(parts) > 1 and parts[0][0] == parts[-1][0]:  # the pattern repeats: the last and first join
                first = parts.pop(0)
                parts[-1] = (first[0], parts[-1][1] + first[1])
                # the joined piece now starts that much earlier
                origin_u -= ux * (parts[-1][1] - first[1])
                origin_v -= uy * (parts[-1][1] - first[1])
            if parts and not parts[0][0]:                   # start with a line: move the leading gap to the end
                gap = parts.pop(0)
                parts.append(gap)
                origin_u += ux * gap[1]
                origin_v += uy * gap[1]
            if len(parts) >= 2 and len(parts) % 2 == 0 and all(p[1] > 0 for p in parts):
                segments = [p[1] for p in parts]
            elif len(parts) == 1 and parts[0][0]:
                segments = []                               # one long line: continuous
        grids.append((a, origin_u, origin_v, spacing, shift, segments))
    return grids


def _cad_pattern_type(doc, shape, options, base_type, cache, report):
    """A filled region type that draws the hatch's own CAD pattern (made once per pattern and size), or None."""
    k_paper = options.units_mm / (options.cad_factor or 1.0) / options.scale * FT_PER_MM
    grids = pattern_grids(shape.get('lines') or [], k_paper)
    if not grids:
        return None
    signature = (shape.get('pattern') or u'CAD',) + tuple(tuple(round(v, 7) for v in g[:5]) + tuple(round(s, 7) for s in g[5]) for g in grids)
    if signature in cache:
        return cache[signature]
    from Autodesk.Revit.DB import Color, FillGrid, FillPattern, FillPatternHostOrientation, FillPatternTarget, UV
    from System.Collections.Generic import List
    spacing_mm = min(g[3] for g in grids) * 304.8
    base_name = u'CAD {0} {1:g}mm {2:g}deg'.format(shape.get('pattern') or u'pattern', round(spacing_mm, 2),
                                                   round(math.degrees(grids[0][0]) % 180.0, 1))
    # the same name for another pattern in this run gets a number; a name made by an earlier run is used again
    names_here = cache.setdefault('__names__', {})
    name, n = base_name, 2
    while name in names_here and names_here[name] != signature:
        name = u'{0} ({1})'.format(base_name, n)
        n += 1
    names_here[name] = signature
    try:
        element = None
        for existing in FilteredElementCollector(doc).OfClass(FillPatternElement):
            if existing.Name == name:
                element = existing
                break
        if element is None:
            pattern = FillPattern(name, FillPatternTarget.Drafting, FillPatternHostOrientation.ToView)
            fill_grids = List[FillGrid]()
            for angle, ou, ov, spacing, shift, segments in grids:
                grid = FillGrid()
                grid.Angle = angle
                grid.Origin = UV(ou, ov)
                grid.Offset = spacing
                grid.Shift = shift
                if segments:
                    grid.SetSegments(List[float](segments))
                fill_grids.Add(grid)
            pattern.SetFillGrids(fill_grids)
            element = FillPatternElement.Create(doc, pattern)
        region_type = None
        for existing in FilteredElementCollector(doc).OfClass(FilledRegionType):
            if type_name(existing) == name:
                region_type = existing
                break
        if region_type is None:
            region_type = base_type.Duplicate(name)
            region_type.ForegroundPatternId = element.Id
            try:
                region_type.ForegroundPatternColor = Color(0, 0, 0)
                region_type.BackgroundPatternId = ElementId.InvalidElementId
            except Exception:
                pass
            report.pattern_types.append(name)
        cache[signature] = region_type
        return region_type
    except Exception as error:
        report.problems.append(u'CAD pattern {0} not rebuilt ({1}) - drawn with the chosen type'.format(shape.get('pattern'), error))
        cache[signature] = None
        return None


def _text_type(doc, size_mm, cache, report):
    """The text type 'CAD <size> mm' (paper size), made from the default text type the first time."""
    key = round(size_mm, 1)
    if key in cache:
        return cache[key]
    name = u'CAD {0:g} mm'.format(key)
    for t in FilteredElementCollector(doc).OfClass(TextNoteType):
        try:
            if t.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM).AsString() == name:
                cache[key] = t.Id
                return t.Id
        except Exception:
            continue
    base = doc.GetElement(doc.GetDefaultElementTypeId(ElementTypeGroup.TextNoteType))
    made = base.Duplicate(name)
    made.get_Parameter(BuiltInParameter.TEXT_SIZE).Set(key * FT_PER_MM)
    try:
        made.get_Parameter(BuiltInParameter.TEXT_BACKGROUND).Set(1)       # transparent, as CAD text
    except Exception:
        pass
    cache[key] = made.Id
    report.text_types.append(name)
    return made.Id


def _unique_view_name(doc, name):
    taken = set()
    for view in FilteredElementCollector(doc).OfClass(ViewDrafting):
        try:
            taken.add(view.Name.lower())
        except Exception:
            pass
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate = u'{0} ({1})'.format(name, n)
        n += 1
    return candidate


# ---------------------------------------------------------------------------
# shapes -> Revit curves
# ---------------------------------------------------------------------------
class _Place(object):
    """CAD units -> feet, with the detail's bottom-left corner at the origin."""

    def __init__(self, box, units_mm, cad_factor=1.0):
        self.x0, self.y0 = box[0], box[1]
        self.k = units_mm / (cad_factor or 1.0) * FT_PER_MM

    def xyz(self, x, y):
        return XYZ((x - self.x0) * self.k, (y - self.y0) * self.k, 0.0)


def _curves(segs, place, tolerance):
    """Revit curves of the pieces; tiny line pieces (tessellated splines) merged so none is too short."""
    out = []
    run = []                                              # points of consecutive straight pieces

    def flush():
        if len(run) < 2:
            del run[:]
            return
        # points closer than Revit's shortest curve to the last kept one are dropped; the chain still ends
        # exactly at its own last point
        kept = [run[0]]
        for p in run[1:-1]:
            if math.hypot(p[0] - kept[-1][0], p[1] - kept[-1][1]) * place.k > tolerance:
                kept.append(p)
        end = run[-1]
        if math.hypot(end[0] - kept[-1][0], end[1] - kept[-1][1]) * place.k > tolerance:
            kept.append(end)
        elif len(kept) > 1:
            kept[-1] = end
        for a, b in zip(kept, kept[1:]):
            if math.hypot(b[0] - a[0], b[1] - a[1]) * place.k > tolerance:
                out.append(Line.CreateBound(place.xyz(a[0], a[1]), place.xyz(b[0], b[1])))
        del run[:]

    for seg in segs:
        if seg[0] == 'L':
            if run and abs(run[-1][0] - seg[1]) + abs(run[-1][1] - seg[2]) > 1e-9:
                flush()
            if not run:
                run.append((seg[1], seg[2]))
            run.append((seg[3], seg[4]))
            continue
        flush()
        _k, cx, cy, r, a0, a1, ccw = seg
        if r * place.k <= tolerance:
            continue
        sweep = cd_dxf.arc_sweep(seg)
        start = a0 if sweep > 0 else a0 + sweep           # counterclockwise from here
        span = abs(sweep)
        centre = place.xyz(cx, cy)
        radius = r * place.k
        if span >= 2.0 * math.pi - 1e-9:                  # a full circle: two halves
            out.append(Arc.Create(centre, radius, 0.0, math.pi, XYZ.BasisX, XYZ.BasisY))
            out.append(Arc.Create(centre, radius, math.pi, 2.0 * math.pi, XYZ.BasisX, XYZ.BasisY))
        elif span * radius > tolerance:
            arc = Arc.Create(centre, radius, start, start + span, XYZ.BasisX, XYZ.BasisY)
            out.append(arc if sweep > 0 else arc.CreateReversed())
    flush()
    return out


def _loop(segs, place, tolerance):
    """A closed CurveLoop of a hatch boundary, or None when it does not close."""
    curves = _curves(segs, place, tolerance)
    if len(curves) < 1:
        return None
    loop = CurveLoop()
    try:
        for curve in curves:
            loop.Append(curve)
    except Exception:
        return None
    try:
        if loop.IsOpen():
            # close a tiny gap left by merged pieces
            first, last = curves[0].GetEndPoint(0), curves[-1].GetEndPoint(1)
            if first.DistanceTo(last) > tolerance * 4:
                return None
            loop = CurveLoop()
            for curve in curves[:-1]:
                loop.Append(curve)
            loop.Append(Line.CreateBound(curves[-1].GetEndPoint(0), first))
    except Exception:
        return None
    return loop


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def create(doc, details, options, log=None):
    """One drafting view per detail. Returns a Report."""
    report = Report()
    log = log or (lambda text: None)
    family_type = None
    for t in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if t.ViewFamily == ViewFamily.Drafting:
            family_type = t
            break
    if family_type is None:
        report.problems.append(u'This model has no drafting view type.')
        return report
    tolerance = doc.Application.ShortCurveTolerance * 1.01
    group = TransactionGroup(doc, u'CAD Details - {0} drafting view(s)'.format(len(details)))
    group.Start()
    text_cache = {}
    pattern_cache = {}
    try:
        transaction = _start(doc, u'CAD Details - types')
        solid = _solid_type(doc, report) if options.fills else None
        pattern = doc.GetElement(options.pattern_type) if options.pattern_type is not None else None
        if options.fills and pattern is None:
            others = [t for t in FilteredElementCollector(doc).OfClass(FilledRegionType) if solid is None or t.Id != solid.Id]
            pattern = others[0] if others else solid
        invisible = _invisible_style(doc)
        transaction.Commit()
        # what goes into which view: one view per detail, or all of them in one view (their CAD layout kept)
        if options.single and details:
            box = details[0].box
            for d in details[1:]:
                box = (min(box[0], d.box[0]), min(box[1], d.box[1]), max(box[2], d.box[2]), max(box[3], d.box[3]))
            jobs = [((options.single_name or u'CAD Details').strip(), [s for d in details for s in d.shapes], box)]
        else:
            jobs = [((options.prefix or u'') + d.name, d.shapes, d.box) for d in details]
        k = options.units_mm / (options.cad_factor or 1.0) * FT_PER_MM
        for job_name, job_shapes, job_box in jobs:
            name = _unique_view_name(doc, job_name)
            size = max(job_box[2] - job_box[0], job_box[3] - job_box[1]) * k
            if size > MAX_SIZE_FT:
                # FIX: a detail hundreds of km wide (a stray object far away in the DWG) is not sent to Revit
                report.problems.append(u'{0}: not made - it would be {1:.0f} km wide in Revit (a stray object far away '
                                       u'in the DWG?)'.format(name, size * 0.3048 / 1000.0))
                log(u'view "{0}": refused, {1:.0f} ft'.format(name, size))
                continue
            log(u'view "{0}": {1} shapes, {2:.1f} x {3:.1f} m in Revit'.format(
                name, len(job_shapes), (job_box[2] - job_box[0]) * k * 0.3048, (job_box[3] - job_box[1]) * k * 0.3048))
            transaction = _start(doc, u'CAD Details - ' + name)
            try:
                view = ViewDrafting.Create(doc, family_type.Id)
                view.Name = name
                view.Scale = options.scale
                place = _Place(job_box, options.units_mm, options.cad_factor)
                lines = texts = fills = outlined = 0
                # ---- linework, in batches
                curves = CurveArray()
                pending = []
                for shape in job_shapes:
                    if shape['kind'] == 'curves':
                        for curve in _curves(shape['segs'], place, tolerance):
                            curves.Append(curve)
                            pending.append(curve)
                made = doc.Create.NewDetailCurveArray(view, curves) if pending else []
                for element in made:
                    if options.line_style is not None:
                        try:
                            element.LineStyle = options.line_style
                        except Exception:
                            pass
                    lines += 1
                # ---- hatches
                if options.fills:
                    for shape in job_shapes:
                        if shape['kind'] != 'fill':
                            continue
                        loops = [_loop(segs, place, tolerance) for segs in shape['loops']]
                        loops = [loop for loop in loops if loop is not None]
                        kind = solid if shape['solid'] else None
                        if kind is None and options.cad_patterns and pattern is not None:
                            # the hatch's own CAD pattern, rebuilt as a drafting fill pattern
                            kind = _cad_pattern_type(doc, shape, options, pattern, pattern_cache, report)
                        if kind is None:
                            kind = pattern
                        done = False
                        if loops and kind is not None:
                            try:
                                from System.Collections.Generic import List
                                region = FilledRegion.Create(doc, kind.Id, view.Id, List[CurveLoop](loops))
                                if invisible is not None:
                                    try:
                                        region.SetLineStyleId(invisible.Id)
                                    except Exception:
                                        pass
                                fills += 1
                                done = True
                            except Exception as error:
                                log(u'   filled region refused: {0}'.format(error))
                        if not done:
                            # drawn as its outline instead
                            outline = CurveArray()
                            for segs in shape['loops']:
                                for curve in _curves(segs, place, tolerance):
                                    outline.Append(curve)
                            if outline.Size:
                                for element in doc.Create.NewDetailCurveArray(view, outline):
                                    if options.line_style is not None:
                                        try:
                                            element.LineStyle = options.line_style
                                        except Exception:
                                            pass
                                    lines += 1
                            outlined += 1
                # ---- text
                if options.text:
                    for shape in job_shapes:
                        if shape['kind'] != 'text':
                            continue
                        try:
                            if options.text_type is not None:
                                type_id = options.text_type             # the user's text type for every text
                            else:
                                paper = max(MIN_TEXT_MM, round(paper_mm(shape['height'], options) / TEXT_STEP_MM) * TEXT_STEP_MM)
                                type_id = _text_type(doc, paper, text_cache, report)
                            note_options = TextNoteOptions(type_id)
                            from Autodesk.Revit.DB import HorizontalTextAlignment, VerticalTextAlignment
                            note_options.HorizontalAlignment = {'left': HorizontalTextAlignment.Left,
                                                                'center': HorizontalTextAlignment.Center,
                                                                'right': HorizontalTextAlignment.Right}[shape['h']]
                            note_options.VerticalAlignment = {'baseline': VerticalTextAlignment.Bottom,
                                                              'bottom': VerticalTextAlignment.Bottom,
                                                              'middle': VerticalTextAlignment.Middle,
                                                              'top': VerticalTextAlignment.Top}[shape['v']]
                            note_options.Rotation = math.radians(shape['angle'])
                            body = shape['text'].replace(u'\n', u'\r')
                            point = place.xyz(shape['x'], shape['y'])
                            width = cd_dxf.plausible_width(shape) * k / options.scale       # on paper, in feet
                            if width > 0:
                                low = TextNote.GetMinimumAllowedWidth(doc, note_options.TypeId)
                                high = TextNote.GetMaximumAllowedWidth(doc, note_options.TypeId)
                                TextNote.Create(doc, view.Id, point, min(max(width, low), high), body, note_options)
                            else:
                                TextNote.Create(doc, view.Id, point, body, note_options)
                            texts += 1
                        except Exception as error:
                            report.problems.append(u'{0}: a text not made ({1})'.format(name, error))
                transaction.Commit()
                report.views.append((name, lines, texts, fills, outlined))
                report.last_view = view
                if getattr(report, 'first_view', None) is None:
                    report.first_view = view
            except Exception as error:
                if transaction.HasStarted() and not transaction.HasEnded():
                    transaction.RollBack()
                report.problems.append(u'{0}: not made - {1}'.format(name, error))
                log(u'   FAILED: {0}'.format(error))
        group.Assimilate()
    except Exception as error:
        try:
            group.RollBack()
        except Exception:
            pass
        report.views = []
        report.problems.append(u'Nothing was made: {0}'.format(error))
    return report
