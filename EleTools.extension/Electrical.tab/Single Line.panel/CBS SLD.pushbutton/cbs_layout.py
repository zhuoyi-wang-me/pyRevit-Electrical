# -*- coding: utf-8 -*-
"""CBS single line diagram - the drawing as plain shapes (no Revit API).

Units: "px" on paper, y DOWNWARDS. One px = (device text height / 5): the
whole diagram grows and shrinks with the text type picked for the devices,
so the boxes always fit their text.

layout(panel, options) returns a list of shapes:
    ('line', x1, y1, x2, y2, style)          style: 'thin' | 'box'
    ('text', x, y, text, size, h, v)         size: 'dev' | 'lbl' | 'title'
                                             h: 'L' | 'C' | 'R'  (anchor)
                                             v: 'T' | 'M' | 'B'
Lines inside a text are separated with '\n'.

panel = {'name': 'CBS-HP-S-B1-02-01L', 'feed': 'SMDB-...',
         'left':  [row, ...], 'right': [row, ...]}
row   = {'circuit': 'R1', 'counts': {'EM': 8, 'EXIT': 0}, 'spare': False}
"""

DEV_PX = 5.0                      # the device text is 5 px high


class Options(object):
    def __init__(self):
        self.lbl_px = 7.0                 # label text height, px
        self.title_px = 25.0              # title text height, px
        self.width_factor = 1.0           # text width factor of the types
        self.circuit_prefix = u'CKT NO. '
        self.count_suffix = u' NOS.'
        self.kinds = [                    # (key, box label, with addressable module)
            ('EXIT', u'EXIT', False),
            ('EM', u'EM', True),
        ]
        self.module_text = u'ADDRESSABLE\nMODULE'
        self.data_text = u'D +/- IN/OUT'
        self.spare_text = u'SPARE'
        self.levels = True                # circuits on other levels drawn in their level's band
        self.level_prefix = u'LEVEL '
        self.measured = {}                # {(text, height px): (width px, height px)} measured in Revit
        # building data network (combined views only): a vertical bar left of the diagram, and from every
        # panel a dashed data line down to just above its level line and along it to the bar
        # 1.0 = the normal spacing. Smaller (e.g. 0.5) shrinks the MINIMUM sizes - panel box width, circuit
        # number line, count line, gap between panel columns - to fit a sheet. Never below what the
        # (measured) text needs, so nothing overlaps.
        self.compact = 1.0
        self.network = False
        self.network_text = u'BUILDING DATA NETWORK'
        self.cable_text = u'CAT 6 CABLE\nTO BUILDING\nDATA NETWORK'
        self.net_px = 25.0                # height of the bar's (vertical) text, px
        self.left_interfaces = [u'FA INTERFACE', u'BMS HARDWARE INTERFACE']
        self.right_interfaces = [u'[DLS] LDB - SUB CIRCUIT\nMONITORING INTERFACE', u'FED FROM {feed}',
                                 u'DATA CABLE', u'DC POWER CABLE']


def text_width(text, height, factor=1.0, measured=None):
    """Width of a text of the given height: as Revit measured it (measured {(text, height): (w, h)}),
    else estimated for upper-case Arial-like text."""
    if measured:
        found = measured.get((text, round(height, 3)))
        if found:
            return found[0]
    lines = (text or u'').split('\n')
    longest = max(len(line) for line in lines) if lines else 0
    return longest * 0.68 * height * factor


def text_height(text, height, measured=None):
    if measured:
        found = measured.get((text, round(height, 3)))
        if found:
            return found[1]
    return len((text or u'').split('\n')) * height * 1.6


def _box(shapes, x0, y0, x1, y1, style='thin'):
    shapes.append(('line', x0, y0, x1, y0, style))
    shapes.append(('line', x1, y0, x1, y1, style))
    shapes.append(('line', x1, y1, x0, y1, style))
    shapes.append(('line', x0, y1, x0, y0, style))


def _kind_of(row, options):
    """(key, label, module) of the symbol drawn for a row: the kind with most devices."""
    counts = row.get('counts') or {}
    best = None
    for kind in options.kinds:
        number = counts.get(kind[0], 0)
        if number and (best is None or number > counts.get(best[0], 0)):
            best = kind
    return best


def count_text(row, options):
    counts = row.get('counts') or {}
    used = [(kind, counts.get(kind[0], 0)) for kind in options.kinds if counts.get(kind[0], 0)]
    if not used:
        return u''
    if len(used) == 1:
        return u'{0}{1}'.format(used[0][1], options.count_suffix)
    used.sort(key=lambda item: -item[1])
    return u' + '.join(u'{0} {1}'.format(number, kind[1]) for kind, number in used)


def _track_crossings(order, zone_of):
    """How many times the lines of the risers cross, for risers in this order (inner first).

    Every riser i runs from its panel row (y_p) to its row on the other level (y_r); both its horizontal
    lines run INWARD from the riser, so they cross every riser further in whose vertical span holds that
    height. Heights are abstract but in the right order: panel rows 0, 1, 2...; a band above sits above
    all of them (rows nearest its level line lowest), a band below under all of them."""
    count_in_zone = {}
    spans = []
    for index, row in order:
        zone = zone_of(row)
        rank = count_in_zone.get(zone, 0)
        count_in_zone[zone] = rank + 1
        if zone > 0:
            y_r = -100000.0 * zone - rank          # rank 0 = the row nearest the level line (lowest)
        else:
            y_r = 100000.0 * (-zone) + rank        # rank 0 = the row nearest the panel (highest)
        y_p = float(index)
        spans.append((y_p, y_r, min(y_p, y_r), max(y_p, y_r)))
    crossings = 0
    for outer in range(len(spans)):
        y_p, y_r, _a, _b = spans[outer]
        for inner in range(outer):
            a, b = spans[inner][2], spans[inner][3]
            if a < y_p < b:
                crossings += 1
            if a < y_r < b:
                crossings += 1
    return crossings


def _best_track_order(items, zone_of, passes=6):
    """The order of the risers (inner first) with the fewest crossings: moves one riser at a time to
    wherever it crosses least, until nothing gets better."""
    order = list(items)
    if len(order) < 2:
        return order
    best = _track_crossings(order, zone_of)
    for _pass in range(passes):
        improved = False
        for i in range(len(order)):
            if best == 0:
                return order
            item = order[i]
            rest = order[:i] + order[i + 1:]
            for j in range(len(order)):
                if j == i:
                    continue
                trial = rest[:j] + [item] + rest[j:]
                value = _track_crossings(trial, zone_of)
                if value < best:
                    best, order, improved = value, trial, True
                    break
        if not improved:
            break
    return order


def break_crossings(shapes, gap):
    """Where a horizontal line crosses a vertical one (a circuit line passing a riser it has no way
    round) the horizontal line is interrupted `gap` on both sides of the vertical, so the crossing reads
    as "not connected" and no line runs over another. Level lines are left whole and not counted."""
    eps = 1e-6
    tall = [s for s in shapes if s[0] == 'line' and s[5] == 'thin' and abs(s[1] - s[3]) < eps
            and abs(s[2] - s[4]) > eps]
    out, breaks = [], 0
    for s in shapes:
        if not (s[0] == 'line' and s[5] == 'thin' and abs(s[2] - s[4]) < eps and abs(s[1] - s[3]) > eps):
            out.append(s)
            continue
        x0, x1, y = min(s[1], s[3]), max(s[1], s[3]), s[2]
        cuts = sorted(v[1] for v in tall if x0 + gap < v[1] < x1 - gap
                      and min(v[2], v[4]) + eps < y < max(v[2], v[4]) - eps)
        if not cuts:
            out.append(s)
            continue
        start = x0
        for x in cuts:
            if x - gap > start + eps:
                out.append(('line', start, y, x - gap, y, s[5]))
            start = max(start, x + gap)
            breaks += 1
        if x1 > start + eps:
            out.append(('line', start, y, x1, y, s[5]))
    return out, breaks


def line_crossings(shapes):
    """How many times two drawn lines cross each other properly (through, not meeting at an end)."""
    eps = 1e-6
    flat = [s for s in shapes if s[0] == 'line' and s[5] != 'level' and abs(s[2] - s[4]) < eps]
    tall = [s for s in shapes if s[0] == 'line' and s[5] != 'level' and abs(s[1] - s[3]) < eps
            and abs(s[2] - s[4]) > eps]
    count = 0
    for h in flat:
        x0, x1, y = min(h[1], h[3]), max(h[1], h[3]), h[2]
        for v in tall:
            y0, y1, x = min(v[2], v[4]), max(v[2], v[4]), v[1]
            if x0 + eps < x < x1 - eps and y0 + eps < y < y1 - eps:
                count += 1
    return count


def layout(panel, options=None, shift=0.0, line_at=None, info=None, draw_levels=True):
    """shift: moves the panel's own band down by this much; line_at {zone: y}: where the level lines of
    the other bands must be (several panels in one view share them); info: filled with
    {'zones': {zone: (level line y, top of that band's content)}}; draw_levels False: no level lines
    (the caller draws them once for all panels)."""
    o = options or Options()
    line_at = line_at or {}
    f = o.width_factor
    W = lambda text, h: text_width(text, h, f, o.measured)
    H = lambda text, h: text_height(text, h, o.measured)
    shapes = []
    rows_l, rows_r = panel.get('left') or [], panel.get('right') or []
    all_rows = rows_l + rows_r

    # ---- symbol sizes (from the real size of their text when Revit measured it)
    labels = [kind[1] for kind in o.kinds]
    label_h = max([H(label, DEV_PX) for label in labels] or [DEV_PX * 1.6])
    em_h = max(11.0, label_h + 3.0)
    mod_w = max(40.0, W(o.module_text, DEV_PX) + 8.0)
    mod_h = max(19.0, H(o.module_text, DEV_PX) + 4.0)
    sym_w = max(26.0, max([W(label, DEV_PX) + 10.0 for label in labels] or [0.0]))
    plain_h = max(14.0, label_h + 5.0)
    col_w = max(mod_w, sym_w)                       # every device column has this width
    em_top = 0.0
    line_dy = em_h + 3.0 + mod_h / 2.0              # circuit line below the row top
    data_dy = em_h + 3.0 + mod_h + 8.0              # "D +/- IN/OUT" line below the row top
    data_w = W(o.data_text, DEV_PX)
    data_h = H(o.data_text, DEV_PX) if o.data_text else 0.0
    texts_above = [o.circuit_prefix + row['circuit'] for row in all_rows] + [count_text(row, o) for row in all_rows]
    lbl_h = max([H(t, o.lbl_px) for t in texts_above if t] or [o.lbl_px * 1.6])
    # rows far enough apart that the circuit / count text above a line clears the "D +/-" text of the row above
    row_low = data_dy + data_h / 2.0
    row_high = min(0.0, line_dy - 2.0 - lbl_h)
    pitch = max(57.0, row_low - row_high + 8.0)

    c = max(0.3, min(1.0, o.compact))
    ckt_len = max([92.0 * c] + [W(o.circuit_prefix + row['circuit'], o.lbl_px) + 18.0 for row in all_rows])
    nos_len = max([45.0 * c] + [W(count_text(row, o), o.lbl_px) + 12.0 for row in all_rows])
    if o.data_text:
        # the "D +/-" text of the device next to the panel must stop short of the far device's data line
        nos_len = max(nos_len, data_w + 16.0 - col_w)
    side_w = ckt_len + col_w + nos_len + col_w     # panel edge to the outer device
    overhang = max(0.0, data_w + 6.0 - col_w / 2.0) + 4.0

    # ---- panel box
    title = panel.get('name') or u''
    # the box also needs room for the interface lines under it (left ones from PL + 63, right ones to PR - 37)
    n_left = len([t for t in o.left_interfaces if t.strip()])
    n_right = len([t for t in o.right_interfaces if t.strip()])
    under = 63.0 + 15.0 * max(0, n_left - 1) + 37.0 + 22.0 * max(0, n_right - 1) + 30.0
    box_w = max(333.0 * c, W(title, o.title_px) + 40.0, under)
    rows = max(len(rows_l), len(rows_r), 1)
    PL = overhang + side_w
    PR = PL + box_w
    top = 4.0 + shift
    first = top - 4.0
    bottom = first + (rows - 1) * pitch + row_low + 6.0
    bottom = max(bottom, top + 2.5 * pitch)
    _box(shapes, PL, top, PR, bottom, 'box')
    title_y = top + (1.5 * pitch if rows > 2 else (bottom - top) / 2.0)
    shapes.append(('text', (PL + PR) / 2.0, title_y, title, 'title', 'C', 'M'))

    # ---- one device symbol in its column [x0, x0 + col_w]
    def device(x0, row_top, kind):
        cx = x0 + col_w / 2.0
        ly = row_top + line_dy
        if kind[2]:                                  # symbol box above an addressable module
            _box(shapes, cx - sym_w / 2.0, row_top + em_top, cx + sym_w / 2.0, row_top + em_top + em_h)
            shapes.append(('text', cx, row_top + em_top + em_h / 2.0, kind[1], 'dev', 'C', 'M'))
            shapes.append(('line', cx, row_top + em_h, cx, row_top + em_h + 3.0, 'thin'))
            m0, m1 = cx - mod_w / 2.0, cx + mod_w / 2.0
            my0 = row_top + em_h + 3.0
            _box(shapes, m0, my0, m1, my0 + mod_h)
            shapes.append(('text', cx, my0 + mod_h / 2.0, o.module_text, 'dev', 'C', 'M'))
            if o.data_text:
                dy = row_top + data_dy
                shapes.append(('line', cx, my0 + mod_h, cx, dy, 'thin'))
                shapes.append(('line', cx, dy, cx - 6.0, dy, 'thin'))
                shapes.append(('text', cx - 7.0, dy, o.data_text, 'dev', 'R', 'M'))
            return m0, m1
        w = sym_w
        _box(shapes, cx - w / 2.0, ly - plain_h / 2.0, cx + w / 2.0, ly + plain_h / 2.0)
        shapes.append(('text', cx, ly, kind[1], 'dev', 'C', 'M'))
        return cx - w / 2.0, cx + w / 2.0

    def columns(right):
        if right:
            inner_col = PR + ckt_len                        # left of the device next to the panel
            return PR, 1.0, inner_col, inner_col + col_w + nos_len
        inner_col = PL - ckt_len - col_w
        return PL, -1.0, inner_col, inner_col - nos_len - col_w

    def remote_row(row, row_top, right, track_x):
        """A circuit drawn on ANOTHER level: the line comes in from its riser (track_x), the circuit
        number next to the far device, then the two devices."""
        ly = row_top + line_dy
        label = o.circuit_prefix + row['circuit']
        kind = _kind_of(row, o)
        _edge, _out, inner_col, outer_col = columns(right)
        a0, a1 = device(inner_col, row_top, kind)
        b0, b1 = device(outer_col, row_top, kind)
        if right:
            shapes.append(('line', a1, ly, b0, ly, 'thin'))
            shapes.append(('line', b1, ly, track_x, ly, 'thin'))
            shapes.append(('text', b1 + 4.0, ly - 2.0, label, 'lbl', 'L', 'B'))
            shapes.append(('text', (a1 + b0) / 2.0, ly - 2.0, count_text(row, o), 'lbl', 'C', 'B'))
        else:
            shapes.append(('line', b1, ly, a0, ly, 'thin'))
            shapes.append(('line', track_x, ly, b0, ly, 'thin'))
            shapes.append(('text', b0 - 4.0, ly - 2.0, label, 'lbl', 'R', 'B'))
            shapes.append(('text', (b1 + a0) / 2.0, ly - 2.0, count_text(row, o), 'lbl', 'C', 'B'))
        return ly

    def row_shapes(row, row_top, right, track_x=None):
        ly = row_top + line_dy
        label = o.circuit_prefix + row['circuit']
        kind = _kind_of(row, o)
        edge, outward, inner_col, outer_col = columns(right)
        if track_x is not None:                             # on another level: out to its riser
            shapes.append(('line', edge, ly, track_x, ly, 'thin'))
            shapes.append(('text', edge + outward * 4.0, ly - 2.0, label, 'lbl', 'L' if right else 'R', 'B'))
            return ly
        if kind is None:                                    # spare / space: a short line and the word
            end = edge + outward * ckt_len
            shapes.append(('line', edge, ly, end, ly, 'thin'))
            shapes.append(('text', edge + outward * 4.0, ly - 2.0, label, 'lbl', 'L' if right else 'R', 'B'))
            if o.spare_text:
                shapes.append(('text', end + outward * 4.0, ly, o.spare_text, 'lbl', 'L' if right else 'R', 'M'))
            return
        a0, a1 = device(inner_col, row_top, kind)           # next to the panel
        b0, b1 = device(outer_col, row_top, kind)           # the far one
        if right:
            shapes.append(('line', edge, ly, a0, ly, 'thin'))
            shapes.append(('line', a1, ly, b0, ly, 'thin'))
            shapes.append(('text', edge + 4.0, ly - 2.0, label, 'lbl', 'L', 'B'))
            shapes.append(('text', (a1 + b0) / 2.0, ly - 2.0, count_text(row, o), 'lbl', 'C', 'B'))
        else:
            shapes.append(('line', a1, ly, edge, ly, 'thin'))
            shapes.append(('line', b1, ly, a0, ly, 'thin'))
            shapes.append(('text', edge - 4.0, ly - 2.0, label, 'lbl', 'R', 'B'))
            shapes.append(('text', (b1 + a0) / 2.0, ly - 2.0, count_text(row, o), 'lbl', 'C', 'B'))

    # ---- levels: a circuit whose fittings are on another level is drawn in that level's band
    def zone_of(row):
        if not o.levels or _kind_of(row, o) is None:
            return 0
        return int(row.get('zone') or 0)

    remote = {False: [], True: []}
    for right, side_rows in ((False, rows_l), (True, rows_r)):
        for index, row in enumerate(side_rows):
            if zone_of(row):
                remote[right].append((index, row))
    longest = max([W(o.circuit_prefix + row['circuit'], o.lbl_px) for row in all_rows] or [0.0])
    left_outer = PL - ckt_len - 2.0 * col_w - nos_len
    right_outer = PR + ckt_len + 2.0 * col_w + nos_len
    track_left = left_outer - max(overhang, longest + 10.0) - 10.0
    track_right = right_outer + longest + 16.0
    track_pitch = max(12.0, DEV_PX * 2.4)
    tracks = {}
    for right, items in remote.items():
        # inner riser first: going up, the circuit highest on the panel; going down, the lowest - and the
        # nearer level before the further one, so the risers nest instead of crossing
        items.sort(key=lambda item: (abs(zone_of(item[1])), item[0] if zone_of(item[1]) > 0 else -item[0]))
        # FIX: risers going UP and risers going DOWN side by side crossed each other's lines (a line out to
        # an up-riser ran across the down-riser next to it). Now the order with the fewest crossings.
        items[:] = _best_track_order(items, zone_of)
        for k, (index, row) in enumerate(items):
            tracks[id(row)] = (track_right + k * track_pitch) if right else (track_left - k * track_pitch)

    at_panel = {}
    for right, side_rows in ((False, rows_l), (True, rows_r)):
        for index, row in enumerate(side_rows):
            at_panel[id(row)] = row_shapes(row, first + index * pitch, right, tracks.get(id(row)))

    # ---- interfaces under the panel
    feed = panel.get('feed') or u'-'
    step = max(15.0, lbl_h + 7.0)
    y = bottom + 33.0
    for index, text in enumerate([t for t in o.left_interfaces if t.strip()]):
        x = PL + 63.0 + 15.0 * index
        shapes.append(('line', x, bottom, x, y, 'thin'))
        shapes.append(('line', x, y, PL - 57.0, y, 'thin'))
        shapes.append(('text', PL - 62.0, y, text.replace('{feed}', feed), 'lbl', 'R', 'M'))
        y += step + max(0.0, H(text.replace('{feed}', feed), o.lbl_px) - lbl_h)
    left_end = y
    items = [t for t in o.right_interfaces if t.strip()]
    dls_w = max(45.0, W(u'DLS', DEV_PX) + 14.0)
    x_text = PR + 60.0
    if [1 for t in items if t.startswith('[')]:
        x_text = max(x_text, PR + 5.0 + dls_w + 10.0)
    y = bottom + 38.0
    for index, text in enumerate(items):
        x = PR - 37.0 - 22.0 * index
        box = None
        if text.startswith('[') and ']' in text:
            box, text = text[1:text.index(']')].strip(), text[text.index(']') + 1:].strip()
        text = text.replace('{feed}', feed)
        extra = max(0.0, H(text, o.lbl_px) - lbl_h)
        y += extra / 2.0
        shapes.append(('line', x, bottom, x, y, 'thin'))
        if box:
            shapes.append(('line', x, y, PR + 5.0, y, 'thin'))
            _box(shapes, PR + 5.0, y - 8.0, PR + 5.0 + dls_w, y + 8.0)
            shapes.append(('text', PR + 5.0 + dls_w / 2.0, y, box, 'dev', 'C', 'M'))
        else:
            shapes.append(('line', x, y, x_text - 5.0, y, 'thin'))
        shapes.append(('text', x_text, y, text, 'lbl', 'L', 'M'))
        y += step + extra / 2.0
    interfaces_end = max(left_end, y)

    # ---- the bands of the other levels, their rows and the risers
    names = panel.get('zone_names') or {}
    level_lines = []                                  # (y, zone)
    group_h = row_low
    if o.levels and names.get(0) is not None:
        panel_line = interfaces_end + 10.0
        level_lines.append((panel_line, 0))
    else:
        panel_line = interfaces_end + 10.0
    zones = sorted(set(zone_of(row) for items in remote.values() for _i, row in items))
    bands = {0: (panel_line, first + row_high - 2.0)}
    cursor_up, cursor_down = first, panel_line
    for zone in [z for z in zones if z > 0]:
        per_side = dict((right, [row for _i, row in items if zone_of(row) == zone])
                        for right, items in remote.items())
        count = max(len(per_side[False]), len(per_side[True]))
        line_y = line_at.get(zone, cursor_up - 28.0)
        level_lines.append((line_y, zone))
        bands[zone] = (line_y, line_y - 10.0 - group_h - (count - 1) * pitch + row_high - 2.0)
        for right, rows_here in per_side.items():
            for j, row in enumerate(rows_here):           # inner riser -> the row nearest the level line
                row_top = line_y - 10.0 - group_h - j * pitch
                ly = remote_row(row, row_top, right, tracks[id(row)])
                shapes.append(('line', tracks[id(row)], at_panel[id(row)], tracks[id(row)], ly, 'thin'))
        cursor_up = line_y - 10.0 - group_h - (count - 1) * pitch - 6.0
    for zone in sorted([z for z in zones if z < 0], reverse=True):
        per_side = dict((right, [row for _i, row in items if zone_of(row) == zone])
                        for right, items in remote.items())
        count = max(len(per_side[False]), len(per_side[True]))
        line_y = line_at.get(zone, cursor_down + 16.0 + (count - 1) * pitch + group_h + 14.0)
        rows_top = line_y - 14.0 - group_h - (count - 1) * pitch
        for right, rows_here in per_side.items():
            for j, row in enumerate(rows_here):           # inner riser -> the row nearest the level above
                row_top = rows_top + j * pitch
                ly = remote_row(row, row_top, right, tracks[id(row)])
                shapes.append(('line', tracks[id(row)], at_panel[id(row)], tracks[id(row)], ly, 'thin'))
        level_lines.append((line_y, zone))
        bands[zone] = (line_y, rows_top + row_high - 2.0)
        cursor_down = line_y
    if info is not None:
        info['zones'] = bands
        info['box'] = (PL, top, PR, bottom)               # the panel box (for the data network line)
    # crossings that no order of the risers avoids: the circuit line is interrupted at the riser
    gap = max(3.0, track_pitch * 0.3)
    shapes[:], broken = break_crossings(shapes, gap)
    if info is not None:
        info['breaks'] = broken

    # ---- the level lines, across the whole drawing
    if level_lines and draw_levels:
        x0, _y0, x1, _y1 = extents(shapes, o)
        x1 += 12.0
        labels = {}
        for line_y, zone in level_lines:
            name = names.get(zone) or u''
            if name:
                labels[zone] = (name if name.upper().startswith(o.level_prefix.strip().upper() or u'\0')
                                else o.level_prefix + name)
        # the labels stand LEFT of everything (no riser runs through them); the lines start under them
        reach = max([W(text, o.lbl_px) for text in labels.values()] or [0.0])
        start = x0 - 12.0 - reach - 6.0
        for line_y, zone in level_lines:
            if zone not in labels:
                continue
            shapes.append(('line', start, line_y, x1, line_y, 'level'))
            shapes.append(('text', start + 2.0, line_y - 2.0, labels[zone], 'lbl', 'L', 'B'))
    return shapes


def _moved(shapes, dx, dy):
    out = []
    for s in shapes:
        if s[0] == 'line':
            out.append(('line', s[1] + dx, s[2] + dy, s[3] + dx, s[4] + dy, s[5]))
        else:
            out.append(('text', s[1] + dx, s[2] + dy) + tuple(s[3:]))
    return out


def combine(panels, options=None, gap=60.0, band_gap=30.0):
    """Several panels in ONE view, arranged by level.

    Every level gets one band and one level line shared by all panels; a band is as tall as the panel
    that needs most room in it. Each panel stands in the band of its own level; its circuits on other
    levels go into those bands. Panels are placed from the left; a panel moves further right only where
    another panel already uses one of the bands it spans (its own level, the levels its circuits reach
    and those in between), so panels on different levels share columns.

    panels: the dicts of read_panel (with 'zone_elev' {zone: elevation} and 'zone_names').
    Returns ([(panel, shapes), ...], level shapes, summary {'width', 'height', 'levels'})."""
    o = options or Options()
    W = lambda text, h: text_width(text, h, o.width_factor, o.measured)
    gap = max(24.0, gap * max(0.3, min(1.0, o.compact)))
    if o.network:
        # room under every level line for the "CAT 6 CABLE ..." label
        band_gap = max(band_gap, text_height(o.cable_text, o.lbl_px, o.measured) + 26.0)
    plans = []
    need = {}                                     # elevation -> room needed above its level line
    names = {}
    for panel in panels:
        info = {}
        shapes = layout(panel, o, info=info, draw_levels=False)
        elev = panel.get('zone_elev') or {}
        zones = info.get('zones') or {}
        by_elev = {}
        for zone, (line_y, content_top) in zones.items():
            e = elev.get(zone)
            if e is None:
                e = elev.get(0, -1e9)              # no level known: with the panel's own band
            by_elev[e] = (line_y, content_top)
            need[e] = max(need.get(e, 0.0), line_y - content_top)
            name = (panel.get('zone_names') or {}).get(zone)
            if name and e not in names:
                names[e] = name
        x0, _y0, x1, _y1 = extents(shapes, o)
        plans.append({'panel': panel, 'by_elev': by_elev, 'width': x1 - x0, 'x0': x0,
                      'home': elev.get(0, -1e9)})
    # ---- level lines from the top down
    order = sorted(need, reverse=True)
    line_y, cursor = {}, 0.0
    for e in order:
        line_y[e] = cursor + need[e] + band_gap
        cursor = line_y[e]
    # ---- columns
    used = dict((e, []) for e in order)          # elevation -> [(x0, x1)] taken
    result = []
    for plan in sorted(plans, key=lambda p: (-p['home'], natural_sort_key(p['panel'].get('name')))):
        es = list(plan['by_elev'])
        low, high = min(es), max(es)
        span = [e for e in order if low - 1e-6 <= e <= high + 1e-6]
        candidates = sorted(set([0.0] + [x1 + gap for e in span for _x0, x1 in used[e]]))
        place = candidates[-1]
        for x in candidates:
            if all(not (x < b and a < x + plan['width']) for e in span for a, b in used[e]):
                place = x
                break
        for e in span:
            used[e].append((place, place + plan['width']))
        plan['place'], plan['span'] = place, [names.get(e) or u'?' for e in span]
        # the same panel again, its level lines where the shared ones are
        elev = plan['panel'].get('zone_elev') or {}
        home = plan['home']
        natural = plan['by_elev'][home][0]
        shift = line_y[home] - natural
        at = {}
        for zone in (plan['panel'].get('zone_names') or {}):
            if zone and elev.get(zone) in line_y:
                at[zone] = line_y[elev[zone]]
        placed_info = {}
        shapes = layout(plan['panel'], o, shift=shift, line_at=at, info=placed_info, draw_levels=False)
        x0 = extents(shapes, o)[0]
        result.append((plan['panel'], _moved(shapes, place - x0, 0.0)))
        box = placed_info.get('box')
        if box:
            plan['data_at'] = ((box[0] + box[2]) / 2.0 + place - x0, box[3], line_y[home])
    # ---- the shared level lines
    width = max([b for e in used for _a, b in used[e]] or [0.0])
    labels = {}
    for e in order:
        name = names.get(e)
        if name and o.levels:
            labels[e] = name if name.upper().startswith(o.level_prefix.strip().upper() or u'\0') else o.level_prefix + name
    reach = max([W(t, o.lbl_px) for t in labels.values()] or [0.0])
    start = -12.0 - reach - 6.0
    levels = []
    for e in order:
        if e in labels:
            levels.append(('line', start, line_y[e], width + 12.0, line_y[e], 'level'))
            levels.append(('text', start + 2.0, line_y[e] - 2.0, labels[e], 'lbl', 'L', 'B'))
    summary = {'width': width - start + 12.0, 'height': cursor, 'levels': len(order),
               # (panel name, x from the left px, width px, levels it spans) - for the log
               'columns': [(p['panel'].get('name'), p.get('place', 0.0), p['width'], p.get('span', []))
                           for p in plans]}
    if o.network and result:
        levels += network_shapes(result, levels, [p for p in plans if p.get('data_at')], o, summary)
    return result, levels, summary


def _ellipse(cx, cy, rx, ry, style='thin', sides=16):
    import math
    pts = [(cx + rx * math.cos(2 * math.pi * k / sides), cy + ry * math.sin(2 * math.pi * k / sides))
           for k in range(sides + 1)]
    return [('line', a[0], a[1], b[0], b[1], style) for a, b in zip(pts, pts[1:])]


def network_shapes(result, level_shapes, plans, o, summary):
    """The BUILDING DATA NETWORK bar left of everything (as tall as the drawing) and, per panel, the dashed
    data line: down from the middle of the panel's bottom to just above its level line, then along it to
    the bar, with the cable symbol and "CAT 6 CABLE ..." under the level line next to the bar.
    summary gets 'network' {'x0', 'x1', 'top', 'bottom'} (px)."""
    everything = [s for _p, sh in result for s in sh] + list(level_shapes)
    X0, Y0, X1, Y1 = extents(everything, o)
    panels_x0 = extents([s for _p, sh in result for s in sh], o)[0]
    bar_w = o.net_px * 1.5 + 16.0
    # FIX: the "CAT 6 CABLE ..." label stood where the risers of the leftmost circuits run. The bar now
    # stands far enough left that the label fits between the bar and the first riser.
    label_w = text_width(o.cable_text, o.lbl_px, o.width_factor, o.measured)
    bar_x1 = min(X0 - 14.0, panels_x0 - 18.0 - label_w - 10.0)
    bar_x0 = bar_x1 - bar_w
    # the level lines and their labels start at the bar (as on the ECBS drawings)
    for k, s in enumerate(level_shapes):
        if s[0] == 'line' and s[5] == 'level':
            level_shapes[k] = ('line', bar_x1, s[2], s[3], s[4], 'level')
        elif s[0] == 'text':
            level_shapes[k] = ('text', bar_x1 + 4.0, s[2]) + tuple(s[3:])
    top, bottom = Y0 - 10.0, Y1 + 10.0
    shapes = []
    _box(shapes, bar_x0, top, bar_x1, bottom, 'box')
    shapes.append(('text', (bar_x0 + bar_x1) / 2.0, (top + bottom) / 2.0, o.network_text, 'net', 'C', 'M', 90.0))
    # the data line runs just UNDER the level line (the LEVEL label sits above it), as on the ECBS drawings
    runs = {}                                         # level line y -> furthest panel x
    for plan in plans:
        cx, box_bottom, level_y = plan['data_at']
        shapes.append(('line', cx, box_bottom, cx, level_y + 5.0, 'data'))
        runs[level_y] = max(runs.get(level_y, cx), cx)
    for level_y, far_x in sorted(runs.items()):
        y = level_y + 5.0
        shapes.append(('line', bar_x1, y, far_x, y, 'data'))
        shapes += _ellipse(bar_x1 + 12.0, y, 3.0, 8.0)
        shapes.append(('text', bar_x1 + 18.0, y + 10.0, o.cable_text, 'lbl', 'L', 'T'))
    summary['network'] = {'x0': bar_x0, 'x1': bar_x1, 'top': top, 'bottom': bottom}
    return shapes


def natural_sort_key(text):
    import re
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', text or u'')]


def text_boxes(shapes, options=None):
    """[(x0, y0, x1, y1, text)] of every text, from its anchor and its (measured) size."""
    o = options or Options()
    sizes = {'dev': DEV_PX, 'lbl': o.lbl_px, 'title': o.title_px}
    found = []
    for s in shapes:
        if s[0] != 'text' or not s[3] or s[4] not in sizes:     # 'net': the vertical bar text, inside its box
            continue
        h = sizes[s[4]]
        w, t = text_width(s[3], h, o.width_factor, o.measured), text_height(s[3], h, o.measured)
        x0 = s[1] - (w if s[5] == 'R' else (w / 2.0 if s[5] == 'C' else 0.0))
        y0 = s[2] - (t if s[6] == 'B' else (t / 2.0 if s[6] == 'M' else 0.0))
        found.append((x0, y0, x0 + w, y0 + t, s[3]))
    return found


def overlaps(shapes, options=None, shrink=0.0):
    """Pairs of texts whose outlines overlap (each outline made `shrink` px smaller on every side - the
    measured outlines include Revit's text box margin)."""
    boxes = text_boxes(shapes, options)
    found = []
    for i in range(len(boxes)):
        a = boxes[i]
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if (a[0] + shrink < b[2] - shrink and b[0] + shrink < a[2] - shrink and
                    a[1] + shrink < b[3] - shrink and b[1] + shrink < a[3] - shrink):
                found.append((a[4], b[4]))
    return found


def lines_through_text(shapes, options=None, shrink=0.0):
    """[(text, line)] where a line runs through a text's outline (made `shrink` px smaller)."""
    found = []
    lines = [s for s in shapes if s[0] == 'line']
    for x0, y0, x1, y1, text in text_boxes(shapes, options):
        x0, y0, x1, y1 = x0 + shrink, y0 + shrink, x1 - shrink, y1 - shrink
        if x0 >= x1 or y0 >= y1:
            continue
        for s in lines:
            a0, a1 = min(s[1], s[3]), max(s[1], s[3])
            b0, b1 = min(s[2], s[4]), max(s[2], s[4])
            if a1 > x0 and a0 < x1 and b1 > y0 and b0 < y1:
                found.append((text, s))
                break
    return found


def extents(shapes, options=None):
    """(x0, y0, x1, y1) of the shapes (text estimated)."""
    o = options or Options()
    xs, ys = [], []
    sizes = {'dev': DEV_PX, 'lbl': o.lbl_px, 'title': o.title_px}
    for s in shapes:
        if s[0] == 'line':
            xs += [s[1], s[3]]
            ys += [s[2], s[4]]
        elif s[4] in sizes:                           # the vertical 'net' text lies inside the bar's box
            w = text_width(s[3], sizes[s[4]], o.width_factor, o.measured)
            x0 = s[1] - (w if s[5] == 'R' else (w / 2.0 if s[5] == 'C' else 0.0))
            xs += [x0, x0 + w]
            ys += [s[2] - sizes[s[4]], s[2] + sizes[s[4]]]
    return min(xs), min(ys), max(xs), max(ys)
