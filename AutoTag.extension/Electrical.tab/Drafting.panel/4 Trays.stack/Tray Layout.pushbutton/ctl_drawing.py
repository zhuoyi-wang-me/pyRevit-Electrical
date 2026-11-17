# -*- coding: utf-8 -*-
"""Cable Tray Layout - what the drawing consists of. No Revit in this file.

compose() turns a Layout into lines, circles and texts by calling a "canvas".
In Revit the canvas is ctl_revit.RevitCanvas, which makes detail lines and
text notes. Everything is in millimetres; the origin is the inside
bottom-left corner of the tray, x to the right, y up.

The annotation follows the sample drawing "New Calc Method-1A":
    * a centre cross in every cable
    * the diameter of every cable on a leader ("O33.1")
    * one dimension chain under the tray: edge space, cable, space, cable ...
    * the tray height dimensioned at the left, the tray width under the chain

Canvas methods used:
    line(x1, y1, x2, y2, style)       style: 'tray', 'cable', 'thin'
    circle(x, y, radius, style)
    text(x, y, text, align)           y is the TOP of the text; align 'left', 'center' or 'right'
"""
import math

from ctl_calc import fmt, summary_lines

DIAMETER = u'Ø'
SCALES = (1, 2, 5, 10, 20, 50)
CHAR_WIDTH = 0.8                          # letter width as a share of the text height (measured ~0.74 in the user's views)
TRAY_THICKNESS = 5.0                      # mm between the inside and the outside line of the tray; 0 = one line


def choose_scale(layout, text_mm):
    """The view scale (1 : n): small enough for a diameter label to fit above
    the smallest main cable, and for the tray to be at least ~150 mm on paper."""
    mains = [cable.diameter for layer in layout.layers for cable in layer.cables if cable.kind == 'main']
    if not mains:
        return 5
    extent = max(layout.tray.width, layout.required_width)
    limit = min(min(mains) / (2.75 * text_mm), extent / 150.0)
    fitting = [scale for scale in SCALES if scale <= limit]
    return fitting[-1] if fitting else 1


def _text_width(text, h):
    return CHAR_WIDTH * h * len(text)


def _real(canvas, handles, line, label=None):
    """A real Revit dimension between the lines `handles` (what canvas.line
    returned), placed on `line` = (x1, y1, x2, y2). False when the canvas
    cannot make it; the caller then draws the dimension with lines and text."""
    make = getattr(canvas, 'dimension', None)
    if make is None or len(handles) < 2 or any(handle is None for handle in handles):
        return False
    return bool(make(handles, line, label))


def _dimension(canvas, left, right, x1, x2, y, label, h):
    """One horizontal dimension from x1 to x2 at height y, between the
    vertical lines `left` and `right`. A real dimension with the label under
    its value; if that is not possible, lines and text."""
    if _real(canvas, [left, right], (x1, y, x2, y), label):
        return
    canvas.line(x1, y, x2, y, 'thin')
    for x in (x1, x2):
        canvas.line(x, y - 0.6 * h, x, y + 0.6 * h, 'thin')
    canvas.text((x1 + x2) / 2.0, y - 0.5 * h, u'{0} {1}'.format(label, fmt(x2 - x1)), 'center')


def chain_points(layer, width):
    """Where the dimension chain of a layer is attached: [(x, what)], left to
    right. `what` is 'left' / 'right' (a tray side) or (cable, 'L' / 'R') (the
    left / right edge of a main cable). Only things that EXIST are used:

        tray side | cable | cable | ... | cable | tray side

    so the chain reads: space to the first cable, cable, space, cable, ...,
    and the rest of the tray after the last cable."""
    points = []
    cables = layer.cables
    if cables[0].x - cables[0].diameter / 2.0 > 1e-6:
        points.append((0.0, 'left'))
    for cable in cables:
        points.append((cable.x - cable.diameter / 2.0, (cable, 'L')))
        points.append((cable.x + cable.diameter / 2.0, (cable, 'R')))
    if width - points[-1][0] > 1e-6:
        points.append((width, 'right'))
    return points


def _chain(canvas, layer, edges, sides, width, y, h):
    """The dimension chain of a layer, as ONE real dimension ATTACHED TO THE
    CABLES: its witness lines stand on the left and right edge of every main
    cable and on the two tray sides. No extra lines are drawn for it.

    edges: {cable: (left handle, right handle)} from canvas.cable().
    sides: (left tray side, right tray side) from canvas.line().
    If Revit refuses to attach a dimension to the cables, short lines are
    drawn at the same places and the dimension is put on those (as before);
    if that fails too, the values are written as text."""
    points = chain_points(layer, width)
    line = (points[0][0], y, points[-1][0], y)
    handles = []
    for x, what in points:
        if what == 'left':
            handles.append(sides[0])
        elif what == 'right':
            handles.append(sides[1])
        else:
            pair = edges.get(id(what[0]), (None, None))
            handles.append(pair[0] if what[1] == 'L' else pair[1])
    if _real(canvas, handles, line):
        return
    marks = [canvas.line(x, y - 0.6 * h, x, y + 0.6 * h, 'thin') for x, _ in points]
    if _real(canvas, marks, line):
        if hasattr(canvas, 'unattached'):
            canvas.unattached += 1                         # reported: short lines had to be used
        return
    canvas.line(line[0], y, line[2], y, 'thin')
    lowered = False
    for (start, _), (end, _) in zip(points, points[1:]):
        value = fmt(end - start)
        tight = _text_width(value, h) + 0.4 * h > (end - start)
        lowered = tight and not lowered                    # every second tight value goes lower
        canvas.text((start + end) / 2.0, y - (2.2 * h if lowered else 0.5 * h), value, 'center')


def _cross(canvas, x, y, diameter, h):
    """The centre mark of a cable."""
    arm = min(0.22 * diameter, 0.7 * h)
    canvas.line(x - arm, y, x + arm, y, 'thin')
    canvas.line(x, y - arm, x, y + arm, 'thin')


def label_levels(positions, widths):
    """On which level (0 = lowest) each diameter label goes.

    positions: the x of every leader, left to right. widths: how far each
    label reaches to the right of its leader. A label is put one level above
    the highest label whose leader stands under its text. So every leader
    ends BELOW any text that passes over it: no leader crosses a text or
    another leader, and no two texts touch."""
    count = len(positions)
    levels = [0] * count
    for index in range(count - 1, -1, -1):                 # from the right: those are needed first
        reach = positions[index] + widths[index]
        under = [levels[other] for other in range(index + 1, count) if positions[other] < reach]
        levels[index] = max(under) + 1 if under else 0
    return levels


def centred_levels(positions, widths):
    """On which level (0 = lowest) each label goes, for labels CENTRED above
    their leader. A level is free for a label when
      * no label of that level overlaps it,
      * no leader of a higher label runs through it, and
      * its own leader does not run through a lower label.
    Neighbours therefore alternate between two levels in the usual case."""
    return block_levels([(x - w / 2.0, x + w / 2.0) for x, w in zip(positions, widths)],
                        [[x] for x in positions])


def block_levels(spans, leaders):
    """The same for text blocks that have ONE OR MORE leaders each.

    spans: (left, right) of every text block. leaders: the x of each of its
    leaders. A level is free for a block when no block of that level overlaps
    it, no leader of a higher block runs through it, and none of its own
    leaders runs through a lower block."""
    levels = []
    for index, (low, high) in enumerate(spans):
        chosen = None
        for level in range(len(spans) + 1):
            free = True
            for other in range(index):
                o_low, o_high = spans[other]
                if levels[other] == level and low < o_high and o_low < high:
                    free = False                           # two texts on one level
                elif levels[other] > level and any(low < x < high for x in leaders[other]):
                    free = False                           # a higher block's leader through this text
                elif levels[other] < level and any(o_low < x < o_high for x in leaders[index]):
                    free = False                           # one of these leaders through a lower text
                if not free:
                    break
            if free:
                chosen = level
                break
        levels.append(max(levels) + 1 if chosen is None else chosen)
    return levels


def _labels(canvas, cables, base, h, tray_height=None):
    """The diameter of every cable on its own leader, as in the user's sketch:

          +-------- O48.4        <- main cable: up, then across to its text
          |    +--- O12.6        <- earth cable: up to the line below, then across
          |    |
          v    v
    Each leader goes straight up from its cable (the main cable's top; the
    earth cable's upper right, clear of the main cable beside it), turns right
    and ends at its text. The two texts of a cable and its earth cable start
    at the same x, one above the other, to the right of both leaders. The main
    cable's line is the upper one, so it passes over the top of the earth
    leader without touching it.

    A text reaches sideways over the leaders of the next cables, so a label
    must stand one level above every label whose leader is under it
    (label_levels): closely spaced cables step like stairs. To keep the stairs
    LOW the cables are divided into two groups:

        the left group has its texts on the LEFT of the leaders  (stairs up),
        the right group has its texts on the RIGHT               (stairs down)

    which makes one roof-shaped block, about half as high as stairs to one
    side only. The dividing point is the one that gives the lowest block. In
    the left group everything is mirrored, so there the EARTH line is the
    upper one and passes over the top of the main leader.

    No leader ever crosses a text or another leader. tray_height: the labels
    start above the tray when a text of the left group reaches out over the
    tray side. Returns the top of the labels."""
    mains = sorted([cable for cable in cables if cable.kind == 'main'], key=lambda cable: cable.x)
    if not mains:
        return base
    pitch = 1.8 * h                                        # from the lower text line up to the upper one
    gap = 0.3 * h                                          # between the end of a leader and its text
    out = 1.2 * h                                          # from the outer leader to the text
    blocks = []                                            # (main foot, earth foot or None, texts, text width)
    for cable in mains:
        texts = [DIAMETER + fmt(cable.diameter)]
        main_foot = (cable.x, cable.y + cable.diameter / 2.0)          # the top of the main cable
        earth_foot = None
        earth = getattr(cable, 'earth', None)
        if earth is not None:
            texts.append(DIAMETER + fmt(earth.diameter))
            reach = 0.7071 * earth.diameter / 2.0                      # the point at 45 degrees, upper right
            earth_foot = (earth.x + reach, earth.y + reach)
        blocks.append((main_foot, earth_foot, texts, max(_text_width(text, h) for text in texts) + 0.5 * h))
    count = len(blocks)
    firsts = [block[0][0] for block in blocks]                         # the leftmost leader of each block
    lasts = [block[1][0] if block[1] else block[0][0] for block in blocks]   # its rightmost leader

    def right_levels(group):
        """Texts on the right: a block is above every later block whose first leader is under its text."""
        return label_levels([firsts[i] for i in group], [lasts[i] + out + blocks[i][3] - firsts[i] for i in group])

    def left_levels(group):
        """The mirror image: a block is above every EARLIER block whose last leader is under its text."""
        mirrored = list(reversed(group))
        levels = label_levels([-lasts[i] for i in mirrored], [lasts[i] - (firsts[i] - out - blocks[i][3]) for i in mirrored])
        return list(reversed(levels))

    best = None
    for split in range(count + 1):                         # blocks[:split] to the left, the rest to the right
        left = left_levels(range(split)) if split else []
        right = right_levels(range(split, count)) if split < count else []
        rank = (max(left + right), abs(split - count / 2.0), split)
        if best is None or rank < best[0]:
            best = (rank, left + right, split)
    levels, split = best[1], best[2]
    if split and firsts[0] - out - blocks[0][3] < 0.0 and tray_height is not None:
        base = max(base, tray_height + 1.2 * h)            # the first text reaches out over the tray side: go above it
    two_lines = any(block[1] is not None for block in blocks)
    step = (pitch if two_lines else 0.0) + 2.0 * h         # from one level to the next
    top = base
    for index, ((main_foot, earth_foot, texts, _), level) in enumerate(zip(blocks, levels)):
        low = base + level * step                          # height of the lower text line of this block
        on_left = index < split
        # the leader nearer to the text ends on the lower line; the other one passes over its top
        if on_left:
            text_x = main_foot[0] - out
            rows = [(main_foot, low, texts[0])]
            if earth_foot:
                rows.append((earth_foot, low + pitch, texts[1]))
        else:
            text_x = (earth_foot[0] if earth_foot else main_foot[0]) + out
            rows = [(main_foot, low + (pitch if earth_foot else 0.0), texts[0])]
            if earth_foot:
                rows.append((earth_foot, low, texts[1]))
        for (x, foot), y, text in rows:
            canvas.line(x, foot, x, y, 'thin')             # up from the cable
            canvas.line(x, y, text_x + (gap if on_left else -gap), y, 'thin')     # across to the text
            # arrow head on the cable: two short lines opening upwards
            for side in (-1.0, 1.0):
                canvas.line(x, foot, x + side * 0.25 * h, foot + 0.65 * h, 'thin')
            canvas.text(text_x, y + 0.65 * h, text, 'right' if on_left else 'left')
            top = max(top, y + 0.8 * h)
    return top


class _Measure(object):
    """A canvas that draws nothing and only notes how high the drawing reaches."""

    def __init__(self, dimension_text=None):
        self.top = 0.0
        self.dimension_text = dimension_text               # as on the real canvas, so the spacing is the same

    def line(self, x1, y1, x2, y2, style='thin'):
        self.top = max(self.top, y1, y2)
        return None

    def circle(self, x, y, radius, style='cable'):
        self.top = max(self.top, y + radius)

    def text(self, x, y, text, align='left'):
        self.top = max(self.top, y)


class _Shifted(object):
    """A canvas that draws on another canvas, moved down or up by dy."""

    def __init__(self, canvas, dy):
        self._canvas = canvas
        self._dy = dy
        self.dimension_text = getattr(canvas, 'dimension_text', None)
        if hasattr(canvas, 'dimension'):
            self.dimension = self._dimension
        if hasattr(canvas, 'cable'):
            self.cable = self._cable

    def _cable(self, x, y, radius, style='cable'):
        return self._canvas.cable(x, y + self._dy, radius, style)

    @property
    def unattached(self):
        return getattr(self._canvas, 'unattached', 0)

    @unattached.setter
    def unattached(self, value):
        if hasattr(self._canvas, 'unattached'):
            self._canvas.unattached = value

    def line(self, x1, y1, x2, y2, style='thin'):
        return self._canvas.line(x1, y1 + self._dy, x2, y2 + self._dy, style)

    def circle(self, x, y, radius, style='cable'):
        return self._canvas.circle(x, y + self._dy, radius, style)

    def text(self, x, y, text, align='left'):
        return self._canvas.text(x, y + self._dy, text, align)

    def _dimension(self, lines, placement, label=None):
        moved = (placement[0], placement[1] + self._dy, placement[2], placement[3] + self._dy)
        return self._canvas.dimension(lines, moved, label)


def compose_stack(canvas, layouts, h):
    """Several trays in one drawing, one below the other, each complete with
    its dimensions, summary and table. Returns the y of the bottom."""
    top = 0.0
    for layout in layouts:
        measure = _Measure(getattr(canvas, 'dimension_text', None))
        bottom = compose(measure, layout, h)               # a dry run: how tall is this tray's drawing?
        dy = top - measure.top
        compose(_Shifted(canvas, dy), layout, h)
        top = dy + bottom - 8.0 * h                        # free space before the next tray
    return top


TABLE_HEADINGS = (u'CABLE TAG', u'FED FROM', u'FED TO', u'CABLE')
TABLE_CHAR_WIDTH = 0.9                    # on the wide side: capitals, and a cell must never be too narrow


def _table(canvas, tray, top, h):
    """The cable table: CABLE TAG | FED FROM | FED TO | CABLE, with grid lines.

    One line per schedule row. The first column holds the tray ID once, as a
    merged cell beside all the lines. Returns the y of the bottom of the table."""
    lines = []
    for row in tray.rows:
        cable = row.cable_type or u'-'
        if row.quantity > 1 and not getattr(row, 'quantity_in_text', False):
            cable = u'{0} x  {1}'.format(row.quantity, cable)
        lines.append((row.fed_from or u'-', row.fed_to or u'-', cable))
    columns = [[u'{0}'.format(tray.id)]] + [[line[index] for line in lines] for index in range(3)]
    widths = [h * (TABLE_CHAR_WIDTH * max(len(text) for text in [heading] + column) + 2.0)
              for heading, column in zip(TABLE_HEADINGS, columns)]
    edges = [0.0]
    for width in widths:
        edges.append(edges[-1] + width)
    row_height = 2.0 * h
    head_bottom = top - row_height
    bottom = head_bottom - row_height * max(1, len(lines))

    # frame, heading line and column lines
    for y in (top, head_bottom, bottom):
        canvas.line(edges[0], y, edges[-1], y, 'thin')
    for x in edges:
        canvas.line(x, top, x, bottom, 'thin')
    # lines between the rows - not through the merged first column
    for index in range(1, len(lines)):
        y = head_bottom - row_height * index
        canvas.line(edges[1], y, edges[-1], y, 'thin')

    for index, heading in enumerate(TABLE_HEADINGS):
        canvas.text((edges[index] + edges[index + 1]) / 2.0, top - 0.35 * h, heading, 'center')
    # the tray ID in the middle of its merged cell
    canvas.text((edges[0] + edges[1]) / 2.0, (head_bottom + bottom) / 2.0 + 0.65 * h, u'{0}'.format(tray.id), 'center')
    for number, line in enumerate(lines):
        y = head_bottom - row_height * number - 0.35 * h
        for index, text in enumerate(line):
            canvas.text(edges[index + 1] + 0.8 * h, y, text, 'left')
    return bottom


def compose(canvas, layout, h):
    """Draw the tray. h is the text height in model millimetres (paper text
    size x view scale); every spacing of the texts follows from it."""
    tray = layout.tray
    width, height = tray.width, tray.height
    pitch = 1.8 * h                                        # distance from one text line to the next
    # The text of a real dimension has the size of the DIMENSION style, which
    # can be much bigger than the text style. The rows of dimensions are spaced
    # by that size, so a value never lies over the name of the dimension above.
    d = max(h, getattr(canvas, 'dimension_text', None) or h)
    t = TRAY_THICKNESS

    # --- tray: inside line (bottom and two sides, open at the top) ----------
    bottom_line = canvas.line(0.0, 0.0, width, 0.0, 'tray')
    left_side = canvas.line(0.0, 0.0, 0.0, height, 'tray')
    right_side = canvas.line(width, 0.0, width, height, 'tray')
    # --- tray: outside line, 5 mm out, closed to the inside line at the top --
    top_edge = None
    if t > 0:
        canvas.line(-t, -t, width + t, -t, 'tray')
        canvas.line(-t, -t, -t, height, 'tray')
        canvas.line(width + t, -t, width + t, height, 'tray')
        top_edge = canvas.line(-t, height, 0.0, height, 'tray')
        canvas.line(width, height, width + t, height, 'tray')

    # --- tray height, dimensioned at the left -----------------------------
    # attached to the tray itself: its inside bottom and its top edge
    x = -(t + 2.5 * d + 1.0 * h)
    if top_edge is None:                                   # a tray drawn as one line has no top edge to hold on to
        top_edge = canvas.line(x - 0.6 * h, height, -0.2 * h, height, 'thin')
    if not _real(canvas, [bottom_line, top_edge], (x, 0.0, x, height)):
        canvas.line(x, 0.0, x, height, 'thin')
        canvas.line(x - 0.6 * h, 0.0, -t, 0.0, 'thin')
        canvas.line(x - 0.6 * h, height, -t, height, 'thin')
        canvas.text(x - 0.8 * h, height / 2.0 + 0.65 * h, fmt(height), 'right')

    # --- cables, centre marks and diameter labels ---------------------------
    everything = [cable for layer in layout.layers for cable in layer.cables + layer.tucked]
    edges = {}                                             # main cable -> what a dimension can hold on to
    draw_cable = getattr(canvas, 'cable', None)
    for cable in everything:                               # main cables and the earth cables attached to them
        if cable.kind == 'main' and draw_cable is not None:
            edges[id(cable)] = draw_cable(cable.x, cable.y, cable.diameter / 2.0, 'cable')
        else:
            canvas.circle(cable.x, cable.y, cable.diameter / 2.0, 'cable')
        _cross(canvas, cable.x, cable.y, cable.diameter, h)
    # the labels start above the highest cable, clear of every circle
    top = max(height, _labels(canvas, everything, layout.stack_height + 1.6 * h, h, height))

    # --- title, above everything -------------------------------------------
    canvas.text(0.0, top + 4.5 * h, u'CABLE TRAY  {0}'.format(getattr(tray, 'label', tray.id)), 'left')
    canvas.text(0.0, top + 2.7 * h, u'{0} x {1} mm, {2} layer(s)'.format(fmt(width), fmt(height), len(layout.layers)), 'left')

    # --- dimension chain, one per layer -----------------------------------
    row = 5.5 * d + 1.0 * h                                # from one dimension line to the next: value above, name below
    y = -(t + 2.2 * d + 1.0 * h)
    for layer in layout.layers:
        if len(layout.layers) > 1:
            canvas.text(-(t + 1.5 * h), y + 0.65 * h, u'Layer {0}'.format(layer.number), 'right')
        _chain(canvas, layer, edges, (left_side, right_side), width, y, h)
        y -= row

    # --- tray width, attached to the two tray sides -------------------------
    # (The calculated cable layout width and the required width have nothing
    # in the drawing to be attached to; they are in the summary below.)
    _dimension(canvas, left_side, right_side, 0.0, width, y, u'Tray width', h)
    y -= row

    # --- warning, in a box --------------------------------------------------
    warnings = layout.warnings()
    if warnings:
        longest = max(len(text) for text in warnings)
        box_width = h * (CHAR_WIDTH * longest + 2.0)
        box_top, box_bottom = y, y - pitch * len(warnings) - 1.0 * h
        for index, text in enumerate(warnings):
            canvas.text(1.0 * h, y - 0.6 * h - pitch * index, text, 'left')
        canvas.line(0.0, box_top, box_width, box_top, 'tray')
        canvas.line(box_width, box_top, box_width, box_bottom, 'tray')
        canvas.line(box_width, box_bottom, 0.0, box_bottom, 'tray')
        canvas.line(0.0, box_bottom, 0.0, box_top, 'tray')
        y = box_bottom - 2.0 * h

    # --- summary ----------------------------------------------------------
    canvas.text(0.0, y, u'SUMMARY', 'left')
    y -= pitch
    for label, value in summary_lines(layout):
        canvas.text(0.0, y, label, 'left')
        canvas.text(16.0 * h, y, value, 'left')
        y -= pitch
    canvas.text(0.0, y, u'Result', 'left')
    canvas.text(16.0 * h, y, u'NOT OK - see warning' if warnings else u'OK', 'left')
    y -= pitch + 1.5 * h

    # --- cable table --------------------------------------------------------
    y = _table(canvas, tray, y, h) - 1.5 * h
    for note in layout.notes:
        canvas.text(0.0, y, u'Note: ' + note, 'left')
        y -= pitch
    return y
