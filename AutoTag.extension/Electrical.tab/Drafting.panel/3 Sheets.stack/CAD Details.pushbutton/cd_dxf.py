# -*- coding: utf-8 -*-
"""CAD Details - reads an ASCII DXF (made from the user's DWG by cd_convert) into plain shapes.

Pure Python, no Revit: testable outside Revit. Only MODEL SPACE is read; entities on layers that are
off, frozen or not plotted (and on Defpoints) are skipped.

Every shape is in drawing units (see Drawing.units_mm for millimetres per unit):

    {'kind': 'curves', 'segs': [seg, ...], 'layer': name}           lines, polylines, arcs, circles, ellipses, splines
    {'kind': 'text', 'x', 'y', 'height', 'angle' (deg), 'text', 'h': 'left'|'center'|'right',
     'v': 'baseline'|'bottom'|'middle'|'top', 'width' (MTEXT box, 0 = none), 'layer'}
    {'kind': 'fill', 'loops': [[seg, ...], ...], 'solid': bool, 'pattern': name, 'layer'}   hatches, SOLID / TRACE

    seg = ('L', x0, y0, x1, y1)                         a straight piece
          ('A', cx, cy, r, a0, a1, ccw)                 an arc from angle a0 to a1 (radians); ccw False: clockwise
"""
import io
import math
import re

UNITS_MM = {0: None, 1: 25.4, 2: 304.8, 3: 1609344.0, 4: 1.0, 5: 10.0, 6: 1000.0, 7: 1000000.0, 8: 0.0000254,
            9: 0.0254, 10: 914.4, 14: 100.0}
TAU = 2.0 * math.pi
CURVE_STEPS = 48                       # pieces per full turn for ellipses; splines get more with more points


class Drawing(object):
    def __init__(self):
        self.shapes = []
        self.units_mm = None           # millimetres per drawing unit; None: the DWG does not say
        self.skipped = {}              # entity type -> count of what is not converted
        self.hidden_layers = set()
        self.layers = {}               # name -> colour number

    def skip(self, kind, n=1):
        self.skipped[kind] = self.skipped.get(kind, 0) + n


# ---------------------------------------------------------------------------
# reading group codes
# ---------------------------------------------------------------------------
def _pairs(path):
    """[(code, value), ...] of the whole file."""
    data = None
    for encoding in ('utf-8', 'cp1252'):
        try:
            with io.open(path, 'r', encoding=encoding) as handle:
                data = handle.read()
            break
        except UnicodeDecodeError:
            continue
    if data is None:
        raise ValueError(u'The DXF could not be read as text.')
    lines = data.splitlines()
    if lines and lines[0].startswith(u'AutoCAD Binary DXF'):
        raise ValueError(u'A binary DXF - save it as ASCII DXF.')
    out = []
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i].strip())
        except ValueError:
            continue
        out.append((code, lines[i + 1].rstrip(u'\r')))
    return out


def _sections(pairs):
    """{'HEADER': [...], 'TABLES': [...], 'BLOCKS': [...], 'ENTITIES': [...]}"""
    out, current, name_next = {}, None, False
    for code, value in pairs:
        if code == 0 and value == u'SECTION':
            current, name_next = None, True
            continue
        if name_next and code == 2:
            current, name_next = value.strip(), False
            out[current] = []
            continue
        if code == 0 and value == u'ENDSEC':
            current = None
            continue
        if current is not None:
            out[current].append((code, value))
    return out


def _entities(pairs):
    """[(type, [(code, value), ...]), ...] - one per entity (VERTEX / SEQEND kept in order)."""
    out, current = [], None
    for code, value in pairs:
        if code == 0:
            current = (value.strip(), [])
            out.append(current)
        elif current is not None:
            current[1].append((code, value))
    return out


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _first(data, code, default=None):
    for c, v in data:
        if c == code:
            return v
    return default


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------
def bulge_seg(x0, y0, x1, y1, bulge):
    """The piece of a polyline from (x0, y0) to (x1, y1) with a bulge: a line, or an arc."""
    if abs(bulge) < 1e-12:
        return ('L', x0, y0, x1, y1)
    dx, dy = x1 - x0, y1 - y0
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        return None
    theta = 4.0 * math.atan(bulge)                        # included angle, signed (+ = counterclockwise)
    radius = chord / (2.0 * abs(math.sin(theta / 2.0)))
    h = (chord / 2.0) / math.tan(theta / 2.0)             # signed distance from the chord's middle to the centre
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    nx, ny = -dy / chord, dx / chord
    cx, cy = mx + nx * h, my + ny * h
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    return ('A', cx, cy, radius, a0, a1, bulge > 0)


def mirror_seg(seg):
    """A piece seen from below (object coordinate system with Z pointing down): X is mirrored."""
    if seg[0] == 'L':
        return ('L', -seg[1], seg[2], -seg[3], seg[4])
    _k, cx, cy, r, a0, a1, ccw = seg
    return ('A', -cx, cy, r, math.pi - a0, math.pi - a1, not ccw)


def _flipped(data):
    """True when the entity's extrusion direction points down (group 230 < 0): its OCS X axis is mirrored.
    FIX: a mirrored block, once exploded, gives such arcs, polylines, hatches and leaders - they were read with
    X the wrong way and landed hundreds of km away on the other side of the origin."""
    return _f(_first(data, 230, 1.0)) < -0.5


def seg_points(seg, step=math.radians(10.0)):
    """Points along a piece (for bounding boxes and previews)."""
    if seg[0] == 'L':
        return [(seg[1], seg[2]), (seg[3], seg[4])]
    _k, cx, cy, r, a0, a1, ccw = seg
    sweep = arc_sweep(seg)
    n = max(2, int(abs(sweep) / step) + 1)
    return [(cx + r * math.cos(a0 + sweep * k / n), cy + r * math.sin(a0 + sweep * k / n)) for k in range(n + 1)]


def arc_sweep(seg):
    """Signed sweep of an arc piece from a0 to a1 (+ counterclockwise); a full circle when a1 == a0."""
    _k, cx, cy, r, a0, a1, ccw = seg
    if ccw:
        sweep = (a1 - a0) % TAU
        return sweep if sweep > 1e-12 else TAU
    sweep = (a0 - a1) % TAU
    return -(sweep if sweep > 1e-12 else TAU)


def _polyline_segs(points, bulges, closed):
    segs = []
    count = len(points)
    last = count if closed else count - 1
    for k in range(last):
        x0, y0 = points[k]
        x1, y1 = points[(k + 1) % count]
        seg = bulge_seg(x0, y0, x1, y1, bulges[k] if k < len(bulges) else 0.0)
        if seg is not None and not (seg[0] == 'L' and abs(x1 - x0) + abs(y1 - y0) < 1e-12):
            segs.append(seg)
    return segs


def _chain(points):
    return [('L', a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]) if abs(a[0] - b[0]) + abs(a[1] - b[1]) > 1e-12]


def ellipse_points(cx, cy, mx, my, ratio, t0, t1, steps=CURVE_STEPS):
    """Points of an ellipse: centre, major axis vector (mx, my), minor / major ratio, parameters t0 -> t1."""
    if t1 <= t0:
        t1 += TAU
    n = max(4, int(steps * (t1 - t0) / TAU) + 1)
    nx, ny = -my * ratio, mx * ratio
    return [(cx + mx * math.cos(t) + nx * math.sin(t), cy + my * math.cos(t) + ny * math.sin(t))
            for t in [t0 + (t1 - t0) * k / n for k in range(n + 1)]]


def spline_points(degree, knots, controls, weights=None, fits=None):
    """Points along a B-spline (de Boor); falls back to the fit points or the control polygon."""
    count = len(controls)
    if count >= 2 and len(knots) == count + degree + 1 and degree >= 1:
        weights = weights if weights and len(weights) == count else [1.0] * count
        lo, hi = knots[degree], knots[count]
        steps = max(16, 8 * count)
        out = []
        for k in range(steps + 1):
            t = lo + (hi - lo) * k / float(steps)
            # the knot span
            s = degree
            while s < count - 1 and t >= knots[s + 1]:
                s += 1
            d = [(controls[j][0] * weights[j], controls[j][1] * weights[j], weights[j]) for j in range(s - degree, s + 1)]
            for r in range(1, degree + 1):
                for j in range(degree, r - 1, -1):
                    i = s - degree + j
                    denominator = knots[i + degree - r + 1] - knots[i]
                    alpha = 0.0 if abs(denominator) < 1e-12 else (t - knots[i]) / denominator
                    d[j] = tuple((1.0 - alpha) * d[j - 1][q] + alpha * d[j][q] for q in range(3))
            x, y, w = d[degree]
            out.append((x / w, y / w) if abs(w) > 1e-12 else (x, y))
        return out
    if fits and len(fits) >= 2:
        return list(fits)
    return list(controls)


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------
_SPECIAL = [(u'%%c', u'\u00d8'), (u'%%C', u'\u00d8'), (u'%%d', u'\u00b0'), (u'%%D', u'\u00b0'),
            (u'%%p', u'\u00b1'), (u'%%P', u'\u00b1'), (u'%%u', u''), (u'%%U', u''), (u'%%o', u''), (u'%%O', u''),
            (u'%%%', u'%')]


def clean_text(text):
    """TEXT: the %% codes."""
    for code, char in _SPECIAL:
        text = text.replace(code, char)
    return re.sub(r'\\U\+([0-9A-Fa-f]{4})', lambda m: unichr_(int(m.group(1), 16)), text)


def unichr_(n):
    try:
        return unichr(n)                                  # IronPython 2
    except NameError:
        return chr(n)


def clean_mtext(text):
    """MTEXT: formatting codes off, paragraphs as new lines, stacked fractions as a/b."""
    text = text.replace(u'\\P', u'\n').replace(u'\\~', u' ')
    text = re.sub(r'\\S([^;^/#]*)[\^/#]([^;]*);', lambda m: m.group(1) + u'/' + m.group(2), text)
    text = re.sub(r'\\[fFHWCcTQAp][^;]*;', u'', text)       # font, height, width, colour, tracking, slant, align, paragraph
    text = re.sub(r'\\[LlOoKkX]', u'', text)                # underline, overline, strike, column break
    text = text.replace(u'\\{', u'\x01').replace(u'\\}', u'\x02').replace(u'\\\\', u'\x03')
    text = text.replace(u'{', u'').replace(u'}', u'')
    text = text.replace(u'\x01', u'{').replace(u'\x02', u'}').replace(u'\x03', u'\\')
    return clean_text(text).strip(u'\n')


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------
def _layer(data):
    return _first(data, 8, u'0')


def _hatch(data):
    """(loops, solid, pattern) of a HATCH entity."""
    pattern = _first(data, 2, u'')
    solid = _i(_first(data, 70, 0)) == 1
    loops = []
    k = 0
    # skip to the number of boundary paths (91)
    while k < len(data) and data[k][0] != 91:
        k += 1
    if k >= len(data):
        return loops, solid, pattern, []
    paths = _i(data[k][1])
    k += 1

    def take(code):
        """the value of `code` at the read position (and step on), else None"""
        if k_[0] < len(data) and data[k_[0]][0] == code:
            value = data[k_[0]][1]
            k_[0] += 1
            return value
        return None

    k_ = [k]
    for _p in range(paths):
        # 92: boundary path type
        while k_[0] < len(data) and data[k_[0]][0] != 92:
            k_[0] += 1
        if k_[0] >= len(data):
            break
        flags = _i(take(92))
        segs = []
        if flags & 2:                                     # a polyline path
            has_bulge = _i(take(72) or 0)
            closed = _i(take(73) or 0)
            count = _i(take(93) or 0)
            points, bulges = [], []
            for _v in range(count):
                x, y = _f(take(10)), _f(take(20))
                bulge = _f(take(42)) if has_bulge else 0.0
                points.append((x, y))
                bulges.append(bulge)
            segs = _polyline_segs(points, bulges, True)
        else:
            count = _i(take(93) or 0)
            for _e in range(count):
                kind = _i(take(72) or 0)
                if kind == 1:
                    segs.append(('L', _f(take(10)), _f(take(20)), _f(take(11)), _f(take(21))))
                elif kind == 2:
                    cx, cy, r = _f(take(10)), _f(take(20)), _f(take(40))
                    s, e = math.radians(_f(take(50))), math.radians(_f(take(51)))
                    ccw = _i(take(73) or 1) == 1
                    if not ccw:                           # stored mirrored: the real angles are 360 - the given ones
                        s, e = -s, -e
                    segs.append(('A', cx, cy, r, s, e, ccw))
                elif kind == 3:
                    cx, cy, mx, my = _f(take(10)), _f(take(20)), _f(take(11)), _f(take(21))
                    ratio = _f(take(40)) or 1.0
                    s, e = math.radians(_f(take(50))), math.radians(_f(take(51)))
                    ccw = _i(take(73) or 1) == 1
                    if not ccw:
                        s, e = -s, -e
                    pts = ellipse_points(cx, cy, mx, my, ratio, s, e) if ccw else list(reversed(ellipse_points(cx, cy, mx, my, ratio, e, s)))
                    segs.extend(_chain(pts))
                elif kind == 4:
                    degree = _i(take(94) or 3)
                    take(73); take(74)
                    knot_count = _i(take(95) or 0)
                    control_count = _i(take(96) or 0)
                    knots = [_f(take(40)) for _n in range(knot_count)]
                    controls, weights = [], []
                    for _n in range(control_count):
                        controls.append((_f(take(10)), _f(take(20))))
                        w = take(42)
                        if w is not None:
                            weights.append(_f(w))
                    fits = []
                    fit_count = take(97)
                    for _n in range(_i(fit_count or 0)):
                        fits.append((_f(take(11)), _f(take(21))))
                    segs.extend(_chain(spline_points(degree, knots, controls, weights, fits)))
                else:
                    break
        # 97: source boundary objects (handles 330) - skipped by the search for the next 92
        if segs:
            loops.append(segs)
    return loops, solid, pattern, _pattern_lines(data, k_[0])


def _pattern_lines(data, start):
    """The hatch pattern's own definition, as AutoCAD stored it with the hatch (already scaled and turned):
    [(angle deg, base x, base y, offset x, offset y, [dash, ...]), ...] - dash > 0 a line, < 0 a gap, 0 a dot."""
    k = start
    while k < len(data) and data[k][0] != 78:
        k += 1
    if k >= len(data):
        return []
    count = _i(data[k][1])
    k += 1
    lines = []
    for _n in range(count):
        values = {}
        dashes = []
        while k < len(data) and data[k][0] in (53, 43, 44, 45, 46, 79, 49):
            code, value = data[k]
            if code == 53 and 53 in values:
                break                                     # the next pattern line
            if code == 49:
                dashes.append(_f(value))
            else:
                values[code] = _f(value)
            k += 1
        if 53 in values:
            lines.append((values.get(53, 0.0), values.get(43, 0.0), values.get(44, 0.0), values.get(45, 0.0),
                          values.get(46, 0.0), dashes))
    return lines


def _text_entity(data):
    halign = {0: 'left', 1: 'center', 2: 'right', 3: 'left', 4: 'center', 5: 'left'}.get(_i(_first(data, 72, 0)), 'left')
    valign = {0: 'baseline', 1: 'bottom', 2: 'middle', 3: 'top'}.get(_i(_first(data, 73, 0)), 'baseline')
    if _i(_first(data, 72, 0)) == 4:
        valign = 'middle'
    use_second = (halign != 'left' or valign != 'baseline') and _first(data, 11) is not None
    x = _f(_first(data, 11 if use_second else 10))
    y = _f(_first(data, 21 if use_second else 20))
    return {'kind': 'text', 'x': x, 'y': y, 'height': _f(_first(data, 40, 1.0)), 'angle': _f(_first(data, 50, 0.0)),
            'text': clean_text(_first(data, 1, u'')), 'h': halign, 'v': valign, 'width': 0.0, 'layer': _layer(data)}


def _mtext_entity(data):
    # FIX: AutoCAD writes an "Embedded Object" part (from group 101 on) that repeats codes 10/20/11/21/40/41
    # with other meanings - its 11/21 were read as the text direction (a level note came out turned 32 degrees)
    for k, (code, _value) in enumerate(data):
        if code == 101:
            data = data[:k]
            break
    chunks = [v for c, v in data if c == 3] + [v for c, v in data if c == 1][:1]
    attach = _i(_first(data, 71, 1))
    halign = {1: 'left', 2: 'center', 3: 'right'}[(attach - 1) % 3 + 1]
    valign = {0: 'top', 1: 'middle', 2: 'bottom'}[min(2, (attach - 1) // 3)]
    angle = _f(_first(data, 50, 0.0))
    if _first(data, 11) is not None:
        angle = math.degrees(math.atan2(_f(_first(data, 21)), _f(_first(data, 11))))
    return {'kind': 'text', 'x': _f(_first(data, 10)), 'y': _f(_first(data, 20)), 'height': _f(_first(data, 40, 1.0)),
            'angle': angle, 'text': clean_mtext(u''.join(chunks)), 'h': halign, 'v': valign,
            'width': _f(_first(data, 41, 0.0)), 'layer': _layer(data)}


def read(path):
    """Read a DXF file. Returns a Drawing."""
    drawing = Drawing()
    sections = _sections(_pairs(path))
    header = sections.get('HEADER', [])
    for k, (code, value) in enumerate(header):
        if code == 9 and value.strip() == u'$INSUNITS' and k + 1 < len(header):
            drawing.units_mm = UNITS_MM.get(_i(header[k + 1][1]))
    # layers: off (negative colour), frozen (70 & 1), not plotted (290 = 0), Defpoints
    for kind, data in _entities(sections.get('TABLES', [])):
        if kind != 'LAYER':
            continue
        name = _first(data, 2, u'')
        colour = _i(_first(data, 62, 7))
        drawing.layers[name] = abs(colour)
        if colour < 0 or (_i(_first(data, 70, 0)) & 1) or _first(data, 290, u'1').strip() == u'0' or name.lower() == u'defpoints':
            drawing.hidden_layers.add(name)
    items = _entities(sections.get('ENTITIES', []))
    k = 0
    while k < len(items):
        kind, data = items[k]
        k += 1
        if _i(_first(data, 67, 0)) == 1:
            continue                                      # paper space
        layer = _layer(data)
        if layer in drawing.hidden_layers:
            if kind == 'POLYLINE':
                while k < len(items) and items[k][0] in ('VERTEX', 'SEQEND'):
                    k += 1
            continue
        shape = None
        try:
            if kind == 'LINE':
                x0, y0, x1, y1 = _f(_first(data, 10)), _f(_first(data, 20)), _f(_first(data, 11)), _f(_first(data, 21))
                if abs(x1 - x0) + abs(y1 - y0) > 1e-12:
                    shape = {'kind': 'curves', 'segs': [('L', x0, y0, x1, y1)]}
            elif kind == 'LWPOLYLINE':
                points, bulges = [], []
                for code, value in data:
                    if code == 10:
                        points.append([_f(value), 0.0])
                        bulges.append(0.0)
                    elif code == 20 and points:
                        points[-1][1] = _f(value)
                    elif code == 42 and bulges:
                        bulges[-1] = _f(value)
                closed = bool(_i(_first(data, 70, 0)) & 1)
                segs = _polyline_segs([tuple(p) for p in points], bulges, closed)
                if segs:
                    shape = {'kind': 'curves', 'segs': segs, 'closed': closed}
            elif kind == 'POLYLINE':
                flags = _i(_first(data, 70, 0))
                points, bulges = [], []
                while k < len(items) and items[k][0] == 'VERTEX':
                    vertex = items[k][1]
                    if not (_i(_first(vertex, 70, 0)) & 16):  # not a spline frame point
                        points.append((_f(_first(vertex, 10)), _f(_first(vertex, 20))))
                        bulges.append(_f(_first(vertex, 42, 0.0)))
                    k += 1
                if k < len(items) and items[k][0] == 'SEQEND':
                    k += 1
                if flags & (16 | 64):
                    drawing.skip('POLYLINE (3D mesh / face)')
                else:
                    segs = _polyline_segs(points, bulges, bool(flags & 1))
                    if segs:
                        shape = {'kind': 'curves', 'segs': segs, 'closed': bool(flags & 1)}
            elif kind in ('ARC', 'CIRCLE'):
                cx, cy, r = _f(_first(data, 10)), _f(_first(data, 20)), _f(_first(data, 40))
                if r > 1e-12:
                    if kind == 'CIRCLE':
                        a0, a1 = 0.0, 0.0
                    else:
                        a0, a1 = math.radians(_f(_first(data, 50))), math.radians(_f(_first(data, 51)))
                    shape = {'kind': 'curves', 'segs': [('A', cx, cy, r, a0, a1, True)], 'circle': kind == 'CIRCLE'}
            elif kind == 'ELLIPSE':
                pts = ellipse_points(_f(_first(data, 10)), _f(_first(data, 20)), _f(_first(data, 11)), _f(_first(data, 21)),
                                     _f(_first(data, 40, 1.0)), _f(_first(data, 41, 0.0)), _f(_first(data, 42, TAU)))
                shape = {'kind': 'curves', 'segs': _chain(pts)}
            elif kind == 'SPLINE':
                degree = _i(_first(data, 71, 3))
                knots = [_f(v) for c, v in data if c == 40]
                weights = [_f(v) for c, v in data if c == 41]
                xs = [_f(v) for c, v in data if c == 10]
                ys = [_f(v) for c, v in data if c == 20]
                fx = [_f(v) for c, v in data if c == 11]
                fy = [_f(v) for c, v in data if c == 21]
                pts = spline_points(degree, knots, list(zip(xs, ys)), weights, list(zip(fx, fy)))
                if len(pts) >= 2:
                    shape = {'kind': 'curves', 'segs': _chain(pts)}
            elif kind in ('TEXT', 'ATTRIB'):
                if kind == 'ATTRIB' and (_i(_first(data, 70, 0)) & 1):
                    continue                              # an invisible attribute
                shape = _text_entity(data)
                if not shape['text'].strip():
                    shape = None
            elif kind == 'MTEXT':
                shape = _mtext_entity(data)
                if not shape['text'].strip():
                    shape = None
            elif kind == 'HATCH':
                loops, solid, pattern, lines = _hatch(data)
                if loops:
                    shape = {'kind': 'fill', 'loops': loops, 'solid': solid, 'pattern': pattern, 'lines': [] if solid else lines}
                else:
                    drawing.skip('HATCH without a readable boundary')
            elif kind in ('SOLID', 'TRACE'):
                corners = [(_f(_first(data, 10 + n)), _f(_first(data, 20 + n))) for n in range(4)]
                # DXF SOLID corner order is 1, 2, 4, 3
                ordered = [corners[0], corners[1], corners[3], corners[2]]
                clean = []
                for p in ordered:
                    if not clean or abs(p[0] - clean[-1][0]) + abs(p[1] - clean[-1][1]) > 1e-12:
                        clean.append(p)
                if len(clean) > 1 and abs(clean[0][0] - clean[-1][0]) + abs(clean[0][1] - clean[-1][1]) < 1e-12:
                    clean.pop()
                if len(clean) >= 3:
                    shape = {'kind': 'fill', 'loops': [_chain(clean + [clean[0]])], 'solid': True, 'pattern': 'SOLID'}
            elif kind in ('POINT', 'VIEWPORT', 'ATTDEF', 'SEQEND', 'VERTEX', 'XLINE', 'RAY'):
                pass
            elif kind == 'INSERT':
                drawing.skip('INSERT (block not exploded)')
            else:
                drawing.skip(kind)
        except Exception:
            drawing.skip(kind + ' (unreadable)')
            shape = None
        if shape is not None and kind in ('LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE', 'HATCH', 'SOLID', 'TRACE', 'TEXT', 'ATTRIB') \
                and _flipped(data):
            if shape['kind'] == 'curves':
                shape['segs'] = [mirror_seg(s) for s in shape['segs']]
            elif shape['kind'] == 'fill':
                shape['loops'] = [[mirror_seg(s) for s in loop] for loop in shape['loops']]
                shape['lines'] = [((180.0 - a) % 360.0, -bx, by, -ox, oy, dashes)
                                  for a, bx, by, ox, oy, dashes in shape.get('lines') or []]
            elif shape['kind'] == 'text':
                shape['x'] = -shape['x']
                shape['angle'] = (180.0 - shape['angle']) % 360.0
                if shape['angle'] > 90.0 and shape['angle'] < 270.0:      # never upside down
                    shape['angle'] = (shape['angle'] + 180.0) % 360.0
                    shape['h'] = {'left': 'right', 'right': 'left'}.get(shape['h'], shape['h'])
            drawing.mirrored = getattr(drawing, 'mirrored', 0) + 1
        if shape is not None:
            shape['layer'] = layer
            drawing.shapes.append(shape)
    return drawing


# ---------------------------------------------------------------------------
# boxes
# ---------------------------------------------------------------------------
def plausible_width(shape):
    """The MTEXT box width when it fits the text in it, else 0 (no box).
    FIX: MTEXT notes with a column width of 71 km made their detail 71 km wide."""
    width = shape.get('width') or 0.0
    if width <= 0:
        return 0.0
    longest = max(len(line) for line in shape['text'].split(u'\n')) * shape['height'] * 0.6
    return width if width <= max(longest * 10.0, shape['height'] * 40.0) else 0.0


def text_box(shape):
    """(x0, y0, x1, y1) of a text: its insertion point and an estimate of its size (0.6 x height per character)."""
    h = shape['height']
    lines = shape['text'].split(u'\n')
    width = plausible_width(shape) or max(len(line) for line in lines) * h * 0.6
    height = h * (1.0 + 1.4 * (len(lines) - 1))
    x = {'left': 0.0, 'center': -width / 2.0, 'right': -width}[shape['h']]
    y = {'baseline': 0.0, 'bottom': 0.0, 'middle': -height / 2.0, 'top': -height}[shape['v']]
    corners = [(x, y), (x + width, y), (x, y + height), (x + width, y + height)]
    a = math.radians(shape['angle'])
    pts = [(shape['x'] + px * math.cos(a) - py * math.sin(a), shape['y'] + px * math.sin(a) + py * math.cos(a)) for px, py in corners]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def shape_box(shape):
    if shape['kind'] == 'text':
        return text_box(shape)
    segs = shape['segs'] if shape['kind'] == 'curves' else [s for loop in shape['loops'] for s in loop]
    pts = [p for s in segs for p in seg_points(s)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
