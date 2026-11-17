# -*- coding: utf-8 -*-
"""CAD Details - splits one CAD sheet into its separate details (pure Python, testable outside Revit).

1. Frames: a closed rectangle (polyline, or four lines) that holds other shapes is a detail frame. A frame
   holding two or more frames (the sheet border, a title block) is not a detail itself.
2. Everything inside a frame belongs to that detail.
3. What is outside every frame is grouped by distance: shapes closer than the gap belong together.
4. Each detail gets a name: a text with "DETAIL" / "TYPICAL" / "SECTION" in it (the tallest such), else the
   tallest text of the detail, else "Detail <n>".
"""
import math
import re

import cd_dxf

TITLE_WORDS = re.compile(r'\b(DETAIL|DETAILS|TYPICAL|SECTION|ELEVATION|INSTALLATION|ARRANGEMENT|SCHEMATIC|MOUNTING|SUPPORT)\b', re.I)


class Detail(object):
    def __init__(self, shapes, box, frame=None):
        self.shapes = shapes
        self.box = box                 # (x0, y0, x1, y1) in drawing units
        self.frame = frame             # the frame rectangle, or None
        self.name = u''
        self.title = None              # the text shape the name came from
        self.small = False             # a scrap of a few shapes outside any frame

    def counts(self):
        out = {'curves': 0, 'text': 0, 'fill': 0}
        for shape in self.shapes:
            out[shape['kind']] += 1
        return out


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _inside(inner, outer, tolerance=0.0):
    return (inner[0] >= outer[0] - tolerance and inner[1] >= outer[1] - tolerance and
            inner[2] <= outer[2] + tolerance and inner[3] <= outer[3] + tolerance)


def _area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _rectangle(shape):
    """The box of a shape that is a closed axis-aligned rectangle (4 straight pieces), else None."""
    if shape['kind'] != 'curves':
        return None
    segs = shape['segs']
    if len(segs) != 4 or any(s[0] != 'L' for s in segs):
        return None
    pts = [(s[1], s[2]) for s in segs] + [(segs[-1][3], segs[-1][4])]
    if abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) > 1e-6 * (1.0 + abs(pts[0][0]) + abs(pts[0][1])):
        return None
    for s in segs:
        if abs(s[1] - s[3]) > 1e-6 and abs(s[2] - s[4]) > 1e-6:
            return None                                  # not axis-aligned
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _line_rectangles(shapes):
    """Rectangles made of four separate LINEs (frames drawn line by line)."""
    lines = [(i, s['segs'][0]) for i, s in enumerate(shapes)
             if s['kind'] == 'curves' and len(s['segs']) == 1 and s['segs'][0][0] == 'L']
    horizontal = [(i, min(s[1], s[3]), max(s[1], s[3]), s[2]) for i, s in lines if abs(s[2] - s[4]) < 1e-6]
    vertical = [(i, min(s[2], s[4]), max(s[2], s[4]), s[1]) for i, s in lines if abs(s[1] - s[3]) < 1e-6]
    by_span = {}
    for i, x0, x1, y in horizontal:
        by_span.setdefault((round(x0, 3), round(x1, 3)), []).append((y, i))
    verticals = {}
    for i, y0, y1, x in vertical:
        verticals.setdefault((round(y0, 3), round(y1, 3)), []).append((x, i))
    found = []
    for (x0, x1), ys in by_span.items():
        if len(ys) < 2:
            continue
        ys.sort()
        for a in range(len(ys)):
            for b in range(a + 1, len(ys)):
                y0, y1 = ys[a][0], ys[b][0]
                sides = verticals.get((round(y0, 3), round(y1, 3)), [])
                left = [i for x, i in sides if abs(x - x0) < 1e-3]
                right = [i for x, i in sides if abs(x - x1) < 1e-3]
                if left and right:
                    found.append(((x0, y0, x1, y1), [ys[a][1], ys[b][1], left[0], right[0]]))
    return found


def frames(shapes, boxes, borders=None):
    """[(box, [indexes of the frame's own shapes])] - the detail frames. A frame holding two or more frames (the
    sheet border, a title block frame) is not a detail: it goes to `borders` (a list) when given."""
    candidates = []
    for i, shape in enumerate(shapes):
        box = _rectangle(shape)
        if box is not None and _area(box) > 0:
            candidates.append((box, [i]))
    candidates += _line_rectangles(shapes)
    # a frame must hold something: at least 3 other shapes
    holding = []
    own_all = set(i for _b, own in candidates for i in own)
    for box, own in candidates:
        inside = sum(1 for k, b in enumerate(boxes) if k not in own_all and _inside(b, box))
        if inside >= 3:
            holding.append((box, own, inside))
    # a frame holding two or more other frames is the sheet border / title block - not a detail
    out = []
    for box, own, _n in holding:
        children = [b for b, o, _m in holding if b != box and _inside(b, box)]
        if len(children) >= 2:
            if borders is not None:
                borders.append((box, own))
            continue
        # a frame inside another detail frame (a sub-frame, a note box) belongs to that frame
        parents = [b for b, o, _m in holding if b != box and _inside(box, b) and
                   len([c for c, _o, _x in holding if c != b and _inside(c, b)]) < 2]
        if parents:
            continue
        out.append((box, own))
    # FIX: small rectangles inside a drawing (a panel, a table cell, a note box) were taken as detail frames and
    # cut out of their detail. A frame must be detail-sized: its smaller side at least 30 % of a typical frame's.
    if out:
        sides = sorted(min(b[2] - b[0], b[3] - b[1]) for b, _o in out)
        large = [s for s in sides if s >= sides[-1] / 3.0]
        typical = large[len(large) // 2]
        out = [(b, o) for b, o in out if min(b[2] - b[0], b[3] - b[1]) >= 0.3 * typical]
    # duplicates (a polyline frame drawn over a line frame)
    unique = []
    for box, own in sorted(out, key=lambda item: -_area(item[0])):
        if not [b for b, _o in unique if all(abs(p - q) < 1e-6 for p, q in zip(b, box))]:
            unique.append((box, own))
    return unique


def _clusters(indexes, boxes, gap):
    """Groups of shapes whose boxes come closer than `gap` (union-find over a grid)."""
    parent = dict((i, i) for i in indexes)

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # FIX: every pair of shapes in a cell was compared - 27 000 shapes took 5 minutes. Now each shape marks the
    # grid cells (the size of the gap) its box covers, grown by half the gap; shapes sharing a cell are joined.
    # One pass: a few seconds for any sheet.
    cell = max(gap, 1e-9) / 4.0                          # two shapes sharing a cell are at most ~1.35 gaps apart
    owner = {}
    for i in indexes:
        b = boxes[i]
        half = gap / 2.0
        gx0, gx1 = int(math.floor((b[0] - half) / cell)), int(math.floor((b[2] + half) / cell))
        gy0, gy1 = int(math.floor((b[1] - half) / cell)), int(math.floor((b[3] + half) / cell))
        if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > 40000:      # a huge shape: only its outline cells
            cells = [(gx, gy) for gx in range(gx0, gx1 + 1) for gy in (gy0, gy1)] + \
                    [(gx, gy) for gy in range(gy0, gy1 + 1) for gx in (gx0, gx1)]
        else:
            cells = [(gx, gy) for gx in range(gx0, gx1 + 1) for gy in range(gy0, gy1 + 1)]
        for key in cells:
            first = owner.get(key)
            if first is None:
                owner[key] = i
            else:
                ra, rb = root(first), root(i)
                if ra != rb:
                    parent[ra] = rb
    groups = {}
    for i in indexes:
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def _robust_extent(boxes):
    """(x0, y0, x1, y1) of where most of the drawing is: the 1 % ... 99 % of the shapes' centres."""
    xs = sorted((b[0] + b[2]) / 2.0 for b in boxes)
    ys = sorted((b[1] + b[3]) / 2.0 for b in boxes)
    n = len(xs)
    lo, hi = n // 100, max(n // 100, n - 1 - n // 100)
    return xs[lo], ys[lo], xs[hi], ys[hi]


def auto_gap(boxes, frame_boxes=None):
    """A gap that separates details but keeps a detail together.
    FIX: it was 3 % of the whole drawing - one stray object 770 km away made it 25 km.
    Now: a sixth of the typical detail frame's smaller side; without frames 2 % of where most of the drawing is."""
    if not boxes:
        return 1.0
    if frame_boxes:
        sides = sorted(min(b[2] - b[0], b[3] - b[1]) for b in frame_boxes)
        return max(sides[len(sides) // 2] / 6.0, 1e-6)
    x0, y0, x1, y1 = _robust_extent(boxes)
    return max(max(x1 - x0, y1 - y0) * 0.02, 1e-6)


def far_away(boxes, indexes):
    """The shapes far outside where the drawing is (stray objects): more than the drawing's own size away."""
    x0, y0, x1, y1 = _robust_extent([boxes[i] for i in indexes])
    reach = max(x1 - x0, y1 - y0, 1.0)
    return set(i for i in indexes if boxes[i][2] < x0 - reach or boxes[i][0] > x1 + reach or
               boxes[i][3] < y0 - reach or boxes[i][1] > y1 + reach)


NOTE_START = re.compile(r'^\s*(\d+[\.\)]|NOTE|NOTES|-|\()', re.I)


def _letters(text):
    return len(re.findall(r'[A-Za-z]', text))


def name_of(detail):
    """The detail's name from its title text.
    FIX: names like "(TYP.)", "45 deg", "21", "-" and "2. CONTRACTOR SHALL ..." were picked. A title now has at
    least 6 letters, is not a numbered note or a bracketed remark, and scores by its height, a title word
    (DETAIL, SECTION, TYPICAL ...) and standing in the lower part of the detail (where titles are)."""
    texts = [s for s in detail.shapes if s['kind'] == 'text' and _letters(s['text']) >= 6 and not NOTE_START.match(s['text'])]
    if not texts:
        return u'', None
    low, high = detail.box[1], detail.box[3]

    def score(s):
        value = s['height']
        if TITLE_WORDS.search(s['text']):
            value *= 3.0
        if high > low and (s['y'] - low) / (high - low) < 0.3:
            value *= 1.5
        return value
    pick = max(texts, key=score)
    if not TITLE_WORDS.search(pick['text']) and pick['height'] < 1.2 * min(s['height'] for s in texts) and len(texts) > 1:
        return u'', None
    first = [line.strip() for line in pick['text'].split(u'\n') if line.strip()]
    name = u' - '.join(first[:2]) if first else u''
    name = re.sub(r'[\\:{}\[\]|;<>?`~]', u'', name)
    return re.sub(r'\s+', u' ', name).strip()[:120], pick


def split(drawing, gap=None, use_frames=True, min_shapes=1):
    """[Detail, ...] - left to right, top to bottom."""
    shapes = drawing.shapes
    if not shapes:
        return []
    boxes = []
    for shape in shapes:
        try:
            boxes.append(cd_dxf.shape_box(shape))
        except Exception:
            boxes.append(None)
    usable = [i for i, b in enumerate(boxes) if b is not None]
    stray = far_away(boxes, usable) if len(usable) > 20 else set()
    usable = [i for i in usable if i not in stray]
    drawing.stray = len(stray)
    taken = set()
    details = []
    found = []
    if use_frames:
        borders = []
        found = frames([shapes[i] if i in usable else {'kind': 'none'} for i in range(len(shapes))],
                       [boxes[i] if i in usable else (0, 0, 0, 0) for i in range(len(shapes))], borders)
        # FIX: the sheet border is no detail, but it was left among the loose shapes - its box touches everything,
        # so all titles below the frames and the details without frames ended up in one big group with it
        for _box, own in borders:
            taken.update(own)
        for box, own in found:
            members = [i for i in usable if i not in taken and (i in own or _inside(boxes[i], box, 1e-6))]
            if members:
                taken.update(members)
                details.append(Detail([shapes[i] for i in members], box, frame=box))
    rest = [i for i in usable if i not in taken]
    gap = gap if gap and gap > 0 else auto_gap([boxes[i] for i in usable], [b for b, _o in found])
    drawing.gap_used = gap
    for group in _clusters(rest, boxes, gap):
        if len(group) < min_shapes:
            continue
        box = boxes[group[0]]
        for i in group[1:]:
            box = _union(box, boxes[i])
        details.append(Detail([shapes[i] for i in sorted(group)], box))
    # FIX: the title block under a drawing (title text + underline + scale) stood further below it than the gap and
    # became a "detail" of its own. A small group with text (at most 4 lines, no hatches to speak of) that stands
    # below a drawing, within its width and not far down, joins that drawing.
    def title_like(d):
        """text with an underline or two - flat: no taller than 4 times its tallest text (a small drawing with a
        title is NOT a title block)"""
        counts = d.counts()
        if d.frame is not None or counts['text'] < 1 or counts['curves'] > 4 or counts['fill'] > 2:
            return False
        tallest = max(s['height'] for s in d.shapes if s['kind'] == 'text')
        return (d.box[3] - d.box[1]) <= 4.0 * tallest
    for d in sorted([x for x in details if title_like(x)], key=lambda x: -x.box[3]):
        if d not in details:
            continue
        best = None
        middle = (d.box[0] + d.box[2]) / 2.0
        for other in details:
            if other is d or title_like(other):
                continue
            width, height = other.box[2] - other.box[0], other.box[3] - other.box[1]
            if not (other.box[0] - width * 0.15 <= middle <= other.box[2] + width * 0.15):
                continue
            drop = other.box[1] - d.box[3]                   # how far below the drawing's bottom the title's top is
            if -height * 0.05 <= drop <= max(gap * 3.0, height * 0.35):
                if best is None or drop < best[0]:
                    best = (drop, other)
        if best is not None:
            best[1].shapes.extend(d.shapes)
            best[1].box = _union(best[1].box, d.box)
            details.remove(d)
    # reading order: rows from the top, then left to right
    if details:
        tall = max(d.box[3] - d.box[1] for d in details) or 1.0
        details.sort(key=lambda d: (-round((d.box[1] + d.box[3]) / 2.0 / (tall * 0.75)), d.box[0]))
    for n, d in enumerate(details):
        d.small = d.frame is None and len(d.shapes) < 5          # a scrap: not ticked by default
        d.name, d.title = name_of(d)
        if not d.name:
            d.name = u'Detail {0}'.format(n + 1)
    # the same name twice: numbered
    seen = {}
    for d in details:
        key = d.name.lower()
        if key in seen:
            seen[key] += 1
            d.name = u'{0} ({1})'.format(d.name, seen[key])
        else:
            seen[key] = 1
    return details
