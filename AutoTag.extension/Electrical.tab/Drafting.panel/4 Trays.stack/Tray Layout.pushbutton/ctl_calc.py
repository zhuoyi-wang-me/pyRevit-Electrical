# -*- coding: utf-8 -*-
"""Cable Tray Layout - the calculation. No Revit in this file.

All values are millimetres and are NOT rounded here; rounding happens only
when a number is shown.

One schedule row = one cable type, drawn "quantity" times.
Required width = layout width x 1.20 (20 % future space).

In EVERY method only the phase (main) cables count in the width. The earth
cable is attached to its cable: it lies on the tray bottom, touching the cable
on the right. No space is kept for it and it is never part of the width.

In EVERY method half the first cable's diameter is left free at the left tray
side and half the last cable's diameter after the last cable.

Methods (they differ only in the space between two neighbouring cables):
  1A (default, from the sample drawing "New Calc Method-1A")
      * space = the BIGGER of the two diameters
  half + half
      * space = diameter_left / 2 + diameter_right / 2
"""
import math

FUTURE_SHARE = 0.20                      # future space, as a share of the layout width
SPREAD_OVER_TRAY = True                  # draw the cables over the given tray width, edge to edge

METHOD_1A = 'phase_max'
METHOD_HALF = 'main_ecc'                 # the key is kept so a remembered choice still works
METHODS = [
    (METHOD_1A, 'Method 1A - space between cables = the bigger diameter'),
    (METHOD_HALF, 'Space between cables = D left/2 + D right/2'),
]


class Row(object):
    """One line of the cable schedule."""

    def __init__(self, item, cable_type, main_od, ecc_od, quantity, line, fed_from=u'', fed_to=u''):
        self.item = item                 # 1, 2, 3 ... inside its tray
        self.cable_type = cable_type
        self.fed_from = fed_from         # texts for the table only
        self.fed_to = fed_to
        self.size_index = None           # which of the tray's sizes this row was written for, if the file says
        self.earth_quantity = None       # number of earth cables of the row; None = one per main cable
        self.quantity_in_text = False    # the cable text itself says "2x...": the table does not repeat it
        self.main_od = main_od           # mm
        self.ecc_od = ecc_od             # mm, or None when the run has no ECC
        self.quantity = quantity
        self.line = line                 # line number in the file, for messages


class Tray(object):
    """One tray and its schedule rows, as read from the file."""

    def __init__(self, tray_id):
        self.id = tray_id
        self.part_label = None           # set when the id stands for several trays: "<id>  (tray 1 of 2)"
        self.sizes = []                # [(width, height, layers)]: more than one = several trays side by side
        self.width = None                # mm
        self.height = None               # mm
        self.layers = 1
        self.rows = []
        self.errors = []                 # the tray cannot be drawn while this is not empty
        self.warnings = []


def _tray_label(tray):
    return tray.part_label or tray.id


Tray.label = property(_tray_label, doc='The name shown for the tray: its id, or "<id>  (tray 1 of 2)".')


class Cable(object):
    """One circle of the drawing."""

    def __init__(self, row, kind, diameter):
        self.row = row
        self.kind = kind                 # 'main' or 'ecc'
        self.diameter = diameter
        self.earth = None                # for a main cable: the earth cable attached to it
        self.x = 0.0                   # centre, from the left inside face of the tray
        self.y = 0.0                     # centre, from the tray bottom

    @property
    def label(self):
        return str(self.row.item) if self.kind == 'main' else 'E'


class Gap(object):
    """The clear space between two neighbouring cables of one layer."""

    def __init__(self, left, right, start, end):
        self.left = left
        self.right = right
        self.start = start               # x where the gap starts (right edge of the left cable)
        self.end = end

    @property
    def value(self):
        return self.end - self.start


class Layer(object):
    def __init__(self, number):
        self.number = number             # 1 = bottom layer
        self.cables = []                 # the cables of the row, left to right (they make the width)
        self.tucked = []                 # earth cables lying beside their cable (method 1A), not in the width
        self.gaps = []
        # the dimension chain: (start x, end x, kind); kind is 'edge' (half a diameter at a tray side),
        # 'cable', 'gap' (space between two cables), 'spare' (the 20%) or 'free' (unused tray width)
        self.segments = []
        self.centred = False             # a cable alone in its layer, moved to the centre of the tray
        self.width = 0.0
        self.height = 0.0                # the largest diameter of the layer
        self.base = 0.0                  # y of the underside of the layer


class Layout(object):
    """Everything the drawing and the window show for one tray."""

    def __init__(self, tray, method):
        self.tray = tray
        self.method = method
        self.layers = []
        self.layout_width = 0.0
        self.allowance = 0.0
        self.required_width = 0.0
        self.stack_height = 0.0
        self.main_count = 0
        self.ecc_count = 0
        self.spare_zone = None           # (x1, x2): where the 20% spare is shown in the tray
        self.notes = []                # things worth telling the user (not errors)

    @property
    def cable_count(self):
        return self.main_count + self.ecc_count

    @property
    def exceeds_width(self):
        """The required width (with future space) does not fit the tray."""
        return self.required_width > self.tray.width + 1e-9

    @property
    def cables_exceed_width(self):
        """Even without future space the cables do not fit."""
        return self.layout_width > self.tray.width + 1e-9

    @property
    def exceeds_height(self):
        return self.stack_height > self.tray.height + 1e-9

    def warnings(self):
        """Texts for the view and the window; empty when the tray is fine."""
        out = []
        if self.cables_exceed_width:
            out.append('WARNING: the cable layout width {0} mm exceeds the tray width {1} mm.'.format(
                fmt(self.layout_width), fmt(self.tray.width)))
        elif self.exceeds_width:
            out.append('WARNING: the required width {0} mm (with 20% future space) exceeds the tray width '
                       '{1} mm.'.format(fmt(self.required_width), fmt(self.tray.width)))
        if self.exceeds_height:
            out.append('WARNING: the {0} layer(s) are {1} mm high, more than the tray height {2} mm.'.format(
                len(self.layers), fmt(self.stack_height), fmt(self.tray.height)))
        return out


def fmt(value, places=1):
    """A number for display. One decimal place by default."""
    return '{0:.{1}f}'.format(float(value), places)


def gap_between(left_diameter, right_diameter, method=METHOD_HALF):
    """The clear space between two neighbouring cables."""
    if method == METHOD_1A:
        return max(left_diameter, right_diameter)
    return left_diameter / 2.0 + right_diameter / 2.0


def _units(tray, method):
    """The runs in schedule order. A unit is a main cable with its earth
    cable (when it has one); a unit is never split over two layers."""
    units = []
    for row in tray.rows:
        # "2x4C... + 1x1C... (ECC)": two main cables but ONE earth cable, which
        # goes with the first of them. Not stated = one earth cable per main cable.
        earths = getattr(row, 'earth_quantity', None)
        for number in range(row.quantity):
            unit = [Cable(row, 'main', row.main_od)]
            if row.ecc_od and (earths is None or number < earths):
                unit.append(Cable(row, 'ecc', row.ecc_od))
            units.append(unit)
    return units


def _split(units, layer_count):
    """Units shared out over the layers in order, as evenly as possible; the
    lower layers take the extra ones."""
    count = max(1, min(layer_count, len(units)))
    base, extra = divmod(len(units), count)
    parts, start = [], 0
    for index in range(count):
        size = base + (1 if index < extra else 0)
        parts.append(units[start:start + size])
        start += size
    return parts


def _part(tray, number, count, size, rows):
    """One physical tray of a Tray ID that stands for several."""
    part = Tray(tray.id)
    part.part_label = u'{0}  (tray {1} of {2})'.format(tray.id, number, count)
    part.width, part.height, part.layers = size
    part.sizes = [size]
    part.warnings = list(tray.warnings)
    for row in rows:
        copy = Row(len(part.rows) + 1, row.cable_type, row.main_od, row.ecc_od, row.quantity, row.line,
                   row.fed_from, row.fed_to)
        copy.earth_quantity = row.earth_quantity
        copy.quantity_in_text = row.quantity_in_text
        part.rows.append(copy)
    return part


def split_tray(tray, method=METHOD_1A):
    """The physical trays behind one Tray ID, as Tray objects ready for build().

    One size: the tray itself. Several sizes (e.g. 900 and 750 mm):
      * if the file says which rows belong to which tray, that is followed
      * otherwise the cables are shared out in schedule order: a tray takes
        cables as long as its REQUIRED width (with the 20% future space) fits;
        the next cable starts the next tray and the last tray takes the rest.
        If a tray is then over-filled, cables are shifted between the trays
        until every tray fits (see _rearrange).
    Returns (parts, notes)."""
    if len(tray.sizes) <= 1 or not tray.rows:
        return [tray], []
    count = len(tray.sizes)
    groups = [[] for _ in tray.sizes]
    moved = False
    if all(row.size_index is not None for row in tray.rows):
        for row in tray.rows:
            groups[row.size_index].append(row)
    else:
        index = 0
        for row in tray.rows:
            if index < count - 1 and groups[index]:
                trial = _part(tray, index + 1, count, tray.sizes[index], groups[index] + [row])
                if build(trial, method).exceeds_width:
                    index += 1                             # this tray is full: the cable starts the next one
            groups[index].append(row)
        groups, moved = _rearrange(groups, tray.sizes, method)
    parts, notes = [], []
    for index, rows in enumerate(groups):
        if rows:
            parts.append(_part(tray, index + 1, count, tray.sizes[index], rows))
        else:
            notes.append(u'{0}: tray {1} of {2} ({3} mm) gets no cables and is not drawn.'.format(
                tray.id, index + 1, count, fmt(tray.sizes[index][0])))
    notes.insert(0, u'{0}: {1} trays ({2} mm) - {3} cable row(s).'.format(
        tray.id, count, ' / '.join(fmt(size[0]) for size in tray.sizes),
        ' + '.join(str(len(rows)) for rows in groups)))
    if moved:
        still = [index + 1 for index, rows in enumerate(groups)
                 if rows and required_width(rows, tray.sizes[index], method) > tray.sizes[index][0] + 1e-9]
        notes.append(u'{0}: the cables were rearranged between the trays so that {1}.'.format(
            tray.id, u'every tray has its 20% spare' if not still else
            u'the trays are filled as evenly as possible - together they are too narrow for all cables with the 20% spare'))
    return parts, notes


def required_width(rows, size, method):
    """The required width (layout + 20%) of these schedule rows in a tray of
    this size (width, height, layers). The same number build() gives, without
    building the drawing - the rearranging below asks for it thousands of times."""
    diameters = [row.main_od for row in rows for _ in range(row.quantity)]
    if not diameters:
        return 0.0
    widest = 0.0
    for part in _split(diameters, size[2]):
        width = part[0] / 2.0 + part[-1] / 2.0 + sum(part)
        for left, right in zip(part, part[1:]):
            width += gap_between(left, right, method)
        widest = max(widest, width)
    return widest * (1.0 + FUTURE_SHARE)


def _rearrange(groups, sizes, method):
    """Shift cables between the trays of one Tray ID until every tray holds its
    cables with the 20% spare - or, if that cannot be done, until the fullest
    tray is as little over-filled as possible.

    Nothing is changed when the schedule-order split already fits. Otherwise
    the best single step is taken again and again: move one row to another
    tray, or exchange two rows (then two rows for one). A step is only taken
    when it makes the fullest tray less full (or, equally full, the trays more even).
    Inside a tray the rows keep their schedule order. Returns (groups, changed)."""
    def fill(rows, index):
        return required_width(rows, sizes[index], method) / sizes[index][0]

    def score(state):
        # (how full the fullest tray is, how uneven the trays are): lower is better
        fills = [fill(rows, index) for index, rows in enumerate(state)]
        return (round(max(fills), 9), round(sum(value * value for value in fills), 9))

    def fits(value):
        return value[0] <= 1.0 + 1e-9

    def ordered(rows):
        return sorted(rows, key=lambda row: row.item)

    state = [list(rows) for rows in groups]
    best = score(state)
    if fits(best):
        return groups, False                               # the schedule order already fits
    changed = False
    for _ in range(200):
        found = None
        count = len(state)
        for a in range(count):
            for b in range(count):
                if a == b:
                    continue
                for row in state[a]:                       # move one row from tray a to tray b
                    trial = [list(rows) for rows in state]
                    trial[a].remove(row)
                    trial[b] = ordered(trial[b] + [row])
                    value = score(trial)
                    if value < best and (found is None or value < found[0]):
                        found = (value, trial)
                if a < b:
                    for row in state[a]:                   # exchange one row for one row
                        for other in state[b]:
                            trial = [list(rows) for rows in state]
                            trial[a].remove(row)
                            trial[b].remove(other)
                            trial[a] = ordered(trial[a] + [other])
                            trial[b] = ordered(trial[b] + [row])
                            value = score(trial)
                            if value < best and (found is None or value < found[0]):
                                found = (value, trial)
        if found is None and not fits(best):
            for a in range(count):                         # last resort: two rows of tray a for one of tray b
                for b in range(count):
                    if a == b:
                        continue
                    rows_a = state[a]
                    for i in range(len(rows_a)):
                        for j in range(i + 1, len(rows_a)):
                            for other in state[b]:
                                trial = [list(rows) for rows in state]
                                trial[a].remove(rows_a[i])
                                trial[a].remove(rows_a[j])
                                trial[b].remove(other)
                                trial[a] = ordered(trial[a] + [other])
                                trial[b] = ordered(trial[b] + [rows_a[i], rows_a[j]])
                                value = score(trial)
                                if value < best and (found is None or value < found[0]):
                                    found = (value, trial)
        if found is None:
            break
        best, state = found
        changed = True
        if fits(best):
            break                                          # every tray fits: stop, move no more than needed
    return state, changed


def build(tray, method=METHOD_1A, spread=None):
    """The Layout of a tray that has no errors.

    spread: relate the drawing to the GIVEN tray width - a cable alone in its
    layer is centred, the 20% spare is shown in the tray and the dimension
    chain runs on to the right tray side. None = SPREAD_OVER_TRAY. The spaces
    between cables and the calculated widths are the same either way."""
    if spread is None:
        spread = SPREAD_OVER_TRAY
    layout = Layout(tray, method)
    units = _units(tray, method)
    if not units:
        return layout
    if tray.layers > len(units):
        layout.notes.append('{0} layers were asked for but there are only {1} run(s); {1} layer(s) are used.'.format(
            tray.layers, len(units)))
    # In every method the first cable stands half its diameter away from the
    # left tray side, and half the last cable's diameter is kept free after it.
    side_space = True
    base = 0.0
    for number, part in enumerate(_split(units, tray.layers), 1):
        layer = Layer(number)
        layer.base = base
        layer.cables = [unit[0] for unit in part]              # only the phase cables make the width
        edge = 0.0                                             # right edge of what is placed so far
        if side_space:
            edge = layer.cables[0].diameter / 2.0              # free space at the left tray side
            layer.segments.append((0.0, edge, 'edge'))
        previous = None
        for cable in layer.cables:
            if previous is not None:
                gap = gap_between(previous.diameter, cable.diameter, method)
                layer.gaps.append(Gap(previous, cable, edge, edge + gap))
                layer.segments.append((edge, edge + gap, 'gap'))
                edge += gap
            cable.x = edge + cable.diameter / 2.0
            cable.y = layer.base + cable.diameter / 2.0        # every cable rests on the layer's underside
            layer.segments.append((edge, edge + cable.diameter, 'cable'))
            edge += cable.diameter
            previous = cable
        if side_space:
            end = edge + layer.cables[-1].diameter / 2.0       # free space at the right tray side
            layer.segments.append((edge, end, 'edge'))
            edge = end
        # The spaces between the cables are ALWAYS the calculated ones (e.g.
        # 29 + 29 = 58); spare width of the tray is never shared out between
        # the cables. Only a cable that is alone in its layer is moved: to the
        # centre of the given tray.
        only = layer.cables[0]
        layer.centred = bool(spread and len(layer.cables) == 1 and tray.width - only.diameter > 1e-9)
        if layer.centred:
            only.x = tray.width / 2.0
            left = only.x - only.diameter / 2.0
            layer.segments = [(0.0, left, 'free'), (left, left + only.diameter, 'cable'),
                              (left + only.diameter, tray.width, 'free')]
        for unit in part:
            if len(unit) > 1:
                main, earth = unit
                big, small = main.diameter / 2.0, earth.diameter / 2.0
                # attached: on the bottom and touching its cable, so the centres
                # are 2 * sqrt(R * r) apart horizontally. No space is kept for it.
                earth.x = main.x + 2.0 * math.sqrt(big * small)
                earth.y = layer.base + small
                main.earth = earth                         # the label of the main cable names it too
                layer.tucked.append(earth)
        layer.width = edge
        layer.height = max(cable.diameter for cable in layer.cables + layer.tucked)
        base += layer.height
        layout.layers.append(layer)
        everything = layer.cables + layer.tucked
        layout.main_count += len([c for c in everything if c.kind == 'main'])
        layout.ecc_count += len([c for c in everything if c.kind == 'ecc'])
    layout.stack_height = base
    layout.layout_width = max(layer.width for layer in layout.layers)
    layout.allowance = layout.layout_width * FUTURE_SHARE
    layout.required_width = layout.layout_width + layout.allowance
    if layout.ecc_count:
        layout.notes.append('Earth cables are attached to their cable and are not counted in the width.')
    if spread:
        _chain_to_tray_side(layout)
    return layout


def _chain_to_tray_side(layout):
    """The rest of each layer's dimension chain, up to the right tray side:

        edge | cable | space | ... | cable | edge | free

    'free' is the tray width left after the cable layout. The 20% spare is
    NOT drawn in the tray and is not a segment of the chain; it is part of the
    required width, which is dimensioned below the tray and listed in the
    summary. (A centred single cable already has its chain: free | cable | free.)"""
    width = layout.tray.width
    for layer in layout.layers:
        if not layer.centred and width - layer.width > 1e-9:
            layer.segments.append((layer.width, width, 'free'))


def summary_lines(layout):
    """The summary block, as (label, value) pairs."""
    tray = layout.tray
    if layout.ecc_count:
        cables = '{0}  ({1} main + {2} ECC)'.format(layout.cable_count, layout.main_count, layout.ecc_count)
    else:
        cables = str(layout.cable_count)
    spare = tray.width - layout.required_width
    return [
        ('Cables drawn', cables),
        ('Layers', str(len(layout.layers))),
        ('Cable layout width', fmt(layout.layout_width) + ' mm'),
        ('Future space 20%', fmt(layout.allowance) + ' mm'),
        ('Required width', fmt(layout.required_width) + ' mm'),
        ('Tray width', fmt(tray.width) + ' mm'),
        ('Tray height', fmt(tray.height) + ' mm'),
        ('Width left over' if spare >= 0 else 'Width short by', fmt(abs(spare)) + ' mm'),
    ]
