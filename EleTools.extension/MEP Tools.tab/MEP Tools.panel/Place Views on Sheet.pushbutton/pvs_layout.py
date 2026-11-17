# -*- coding: utf-8 -*-
"""Place Views on Sheet - the packing algorithm. No Revit in this file.

Everything is in SHEET coordinates (feet, x to the right, y upwards), as boxes
(x low, y low, x high, y high).

The algorithm is a "shelf" (row-by-row) packing:

    +--------------------------------------------- usable area ---+
    | [view 1]   [view 2]        [view 3]   <- row 1 (top-aligned)  |
    |                                                               |
    | [view 4]  [view 5]   [obstacle]  [view 6]   <- row 2          |
    |                                                               |
    +---------------------------------------------------------------+

  1. The cursor starts at the TOP-LEFT corner of the usable area.
  2. Each view is put at the cursor, its top on the top line of the row.
  3. If it would touch something already on the sheet (an existing viewport or
     schedule) it slides right, past that obstacle.
  4. If the remaining width of the row is not enough, a new row starts below the
     tallest view of the row just finished, one spacing lower.
  5. A view that no longer fits below the last row is left for the next sheet;
     the views after it are still tried (a smaller one may fit at the end of a row).
  6. Finally the whole block of placed views is moved to the CENTRE of the usable
     area - if that does not bring it onto an obstacle (then it tries centring
     sideways only, then up/down only, then leaves it at the top-left).

Every gap - view to view, view to obstacle - is at least `spacing`.
"""

EPS = 1e-9


def overlaps(a, b, pad=0.0):
    """True when the boxes a and b overlap, or come closer than pad."""
    return (a[0] < b[2] + pad - EPS and b[0] < a[2] + pad - EPS and
            a[1] < b[3] + pad - EPS and b[1] < a[3] + pad - EPS)


def width(box):
    return box[2] - box[0]


def height(box):
    return box[3] - box[1]


def union(boxes):
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def shift(box, dx, dy):
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


class Result(object):
    """What pack() decided.

    placed:    {key: box}  where each view goes (its full extent, title included)
    left_over: [key]       did not fit on this sheet - for the next one
    too_big:   [key]       larger than the usable area itself: no sheet of this size takes it
    centred:   'both', 'horizontal', 'vertical' or 'no'"""

    def __init__(self):
        self.placed = {}
        self.order = []
        self.left_over = []
        self.too_big = []
        self.centred = 'no'


def _first_free_x(x, top, w, h, area, spacing, blocked):
    """Slide a w x h box (top edge at `top`) right from x until it is clear of every blocked box.
    Returns x, or None when it runs out of the area."""
    while True:
        if x + w > area[2] + EPS:
            return None
        box = (x, top - h, x + w, top)
        hit = [b for b in blocked if overlaps(box, b, spacing)]
        if not hit:
            return x
        x = max(b[2] for b in hit) + spacing         # just past the obstacle(s) in the way


def pack(sizes, area, spacing, obstacles=(), tallest_first=True, centre=True):
    """sizes:     [(key, w, h)] of the views, in the order the user listed them
    area:      (x low, y low, x high, y high) the views must stay inside
    spacing:   minimum gap between any two boxes
    obstacles: boxes already on the sheet (viewports, schedules) to keep clear of
    Returns a Result."""
    result = Result()
    obstacles = [tuple(b) for b in obstacles or []]
    area_w, area_h = width(area), height(area)

    items = []
    for index, (key, w, h) in enumerate(sizes):
        if w > area_w + EPS or h > area_h + EPS:
            result.too_big.append(key)
        else:
            items.append((index, key, float(w), float(h)))
    if tallest_first:
        # the classic shelf heuristic: tall views first, so each row is about as high as its views
        items.sort(key=lambda item: (-item[3], item[0]))

    placed_boxes = []
    row_top = area[3]                  # top line of the current row
    row_bottom = area[3]               # lowest bottom edge of the current row so far
    cursor = area[0]
    row_used = False

    for _index, key, w, h in items:
        blocked = obstacles + placed_boxes
        spot = None
        top, x = row_top, cursor
        started_new_row = False
        while spot is None:
            if top - h < area[1] - EPS:
                break                                    # below the area: does not fit on this sheet
            found = _first_free_x(x, top, w, h, area, spacing, blocked)
            if found is not None:
                spot = (found, top - h, found + w, top)
                break
            if not started_new_row:
                # the remaining width of this row is not enough: a new row below it
                started_new_row = True
                top = (row_bottom - spacing) if row_used else row_top
                x = area[0]
                continue
            # even a fresh row is blocked by obstacles at this height: go below the lowest
            # obstacle that is in the way and try again
            in_band = [b for b in blocked if b[1] < top + spacing and b[3] > top - h - spacing]
            if not in_band:
                break
            top = min(b[1] for b in in_band) - spacing
            x = area[0]
        if spot is None:
            result.left_over.append(key)
            continue
        if started_new_row:
            row_top, row_bottom, row_used = top, top, False
        result.placed[key] = spot
        result.order.append(key)
        placed_boxes.append(spot)
        cursor = spot[2] + spacing
        row_bottom = min(row_bottom, spot[1])
        row_used = True

    if centre and placed_boxes:
        _centre(result, area, obstacles, spacing)
    return result


def _centre(result, area, obstacles, spacing):
    """Move the whole block of placed views to the middle of the area, if nothing is in the way."""
    block = union(result.placed.values())
    dx = ((area[0] + area[2]) - (block[0] + block[2])) / 2.0
    dy = ((area[1] + area[3]) - (block[1] + block[3])) / 2.0
    for mode, (mx, my) in (('both', (dx, dy)), ('horizontal', (dx, 0.0)), ('vertical', (0.0, dy))):
        moved = dict((key, shift(box, mx, my)) for key, box in result.placed.items())
        clear = all(not overlaps(box, b, spacing) for box in moved.values() for b in obstacles)
        if clear:
            result.placed = moved
            result.centred = mode
            return
    result.centred = 'no'


def check(result, obstacles, spacing, area):
    """Problems of a result, for tests and for a last safety check before Revit is touched:
    [(key, key or 'obstacle' or 'area', text)]."""
    problems = []
    keys = list(result.placed)
    for i, a in enumerate(keys):
        box = result.placed[a]
        if box[0] < area[0] - 1e-6 or box[1] < area[1] - 1e-6 or box[2] > area[2] + 1e-6 or box[3] > area[3] + 1e-6:
            problems.append((a, 'area', 'outside the usable area'))
        for b in keys[i + 1:]:
            if overlaps(box, result.placed[b], spacing - 1e-6):
                problems.append((a, b, 'closer than the spacing'))
        for other in obstacles:
            if overlaps(box, other, spacing - 1e-6):
                problems.append((a, 'obstacle', 'closer than the spacing to something on the sheet'))
    return problems
