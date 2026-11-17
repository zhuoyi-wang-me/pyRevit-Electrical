# -*- coding: utf-8 -*-
"""Room Tag Cleanup - remove room tags that sit on top of each other.

For every view in the chosen scope:
  * DUPLICATES  - two or more tags of the SAME room that overlap. One tag is
                  kept (inside its room > without leader > oldest), the
                  others are deleted. The room keeps its tag.
  * ORPHANED    - tags whose room was deleted / is not placed (optional).
  * SHOWS "?"   - tags Revit does NOT flag as orphaned, but whose room cannot
                  be found / is not placed, or whose text is only "?" (blank
                  values) - typical after the ARC model re-created its rooms.
                  Deleted together with the orphaned tags (same option).
  * IDENTICAL   - overlapping tags with the same text (the same room reached
                  through two links): one is deleted.
  * OVERLAPS    - tags of DIFFERENT rooms that overlap. Never deleted (the
                  room would lose its only tag). Optionally moved to their
                  room's location point (tags without leader); whatever still
                  overlaps afterwards is listed with a "select them" link.

Nothing is changed until you press "Clean up" in the summary dialog; deletes
and moves are one transaction (one Undo).
"""
import datetime
import io
import os
from collections import defaultdict

from pyrevit import forms, revit, script, DB
from System.Collections.Generic import List

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# Step-by-step trace, flushed to disk after every line: if Revit itself dies
# (no Python error to show), the last line says which step was running.
TRACE_FILE = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'),
                          'pyRevit', 'RoomTagCleanup_trace.log')


def trace(text):
    try:
        with io.open(TRACE_FILE, 'a', encoding='utf-8') as fh:
            fh.write(u'{0}  {1}\n'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), text))
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        pass

TITLE = 'Room Tag Cleanup'
OVERLAP_SHARE = 0.25      # tags overlap when they share >= 25 % of the smaller tag's area
TAG_VIEW_TYPES = ('FloorPlan', 'CeilingPlan', 'AreaPlan', 'EngineeringPlan', 'Section', 'Elevation')

SCOPE_ACTIVE = 'Active view (or the views on the active sheet)'
SCOPE_SELECTED = 'Views / sheets selected in the Project Browser'
SCOPE_SHEETS = 'All views placed on sheets'
SW_ORPHANS = 'Also delete orphaned / "?" tags (room deleted, not placed, or tag shows only ?)'
SW_FAR = 'Also delete same-room duplicates that do NOT overlap'
SW_MOVE = 'Move overlapping tags of DIFFERENT rooms to their room centre (tags without leader)'
ACT_CLEAN, ACT_REPORT = 'Clean up', 'Report only'
MOVE_TOLERANCE = 0.3      # ft (~90 mm): a tag this close to its room point is already "at the centre"


# --------------------------------------------------------------------------- helpers
def eid_int(eid):
    """ElementId -> int (IntegerValue was removed in Revit 2026)."""
    try:
        return int(eid.Value)
    except AttributeError:
        return int(eid.IntegerValue)


def safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def can_hold_room_tags(view):
    return (view is not None and not view.IsTemplate
            and str(view.ViewType) in TAG_VIEW_TYPES)


def views_in_scope(scope):
    """Views to clean, never twice, never templates / schedules / 3D."""
    found = []

    def add(view):
        if can_hold_room_tags(view) and all(v.Id != view.Id for v in found):
            found.append(view)

    def add_sheet(sheet):
        for vid in sheet.GetAllPlacedViews():
            add(doc.GetElement(vid))

    if scope == SCOPE_ACTIVE:
        view = doc.ActiveView
        if isinstance(view, DB.ViewSheet):
            add_sheet(view)
        else:
            add(view)
    elif scope == SCOPE_SELECTED:
        for eid in uidoc.Selection.GetElementIds():
            el = doc.GetElement(eid)
            if isinstance(el, DB.ViewSheet):
                add_sheet(el)
            elif isinstance(el, DB.View):
                add(el)
    else:
        for sheet in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
            add_sheet(sheet)
    return found


class TagInfo(object):
    """One room tag, measured in the 2D coordinates of its view."""

    def __init__(self, tag, view):
        self.tag = tag
        self.eid = tag.Id         # kept: tag.Id cannot be read once the tag is deleted
        self.id = eid_int(tag.Id)
        self.view = view
        self.orphaned = bool(safe(lambda: tag.IsOrphaned, False))
        self.in_room = safe(lambda: tag.IsInRoom, None)          # None = unknown
        self.has_leader = bool(safe(lambda: tag.HasLeader, False))
        self.type_id = eid_int(tag.GetTypeId())
        self.label = (safe(lambda: tag.TagText, u'') or u'').replace(u'\r', u' ').replace(u'\n', u' ').strip()
        self.key = None
        room_id = safe(lambda: tag.TaggedRoomId)
        if room_id is not None and not self.orphaned:
            link = safe(lambda: eid_int(room_id.LinkInstanceId), -1)
            if link is not None and link > 0:
                self.key = (link, eid_int(room_id.LinkedElementId))
            else:
                self.key = (0, eid_int(safe(lambda: tag.TaggedLocalRoomId, room_id.HostElementId)))
        self.rect = None          # (u0, v0, u1, v1) in feet, in the view plane
        self.box_rect = None
        self.head = None

    def measure(self, right, up):
        box = safe(lambda: self.tag.get_BoundingBox(self.view))
        if box is not None:
            pts = [DB.XYZ(x, y, z) for x in (box.Min.X, box.Max.X)
                   for y in (box.Min.Y, box.Max.Y) for z in (box.Min.Z, box.Max.Z)]
            us = [p.DotProduct(right) for p in pts]
            vs = [p.DotProduct(up) for p in pts]
            self.box_rect = (min(us), min(vs), max(us), max(vs))
        head = safe(lambda: self.tag.TagHeadPosition) or safe(lambda: self.tag.Location.Point)
        if head is not None:
            self.head = (head.DotProduct(right), head.DotProduct(up))
        self.rect = self.box_rect

    def size(self):
        r = self.box_rect
        return (r[2] - r[0], r[3] - r[1]) if r else None


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None


def _overlap_share(a, b):
    """Shared area / area of the smaller rectangle (0 when apart)."""
    if a is None or b is None:
        return 0.0
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / smaller if smaller > 1e-12 else 1.0


def overlaps(a, b):
    return _overlap_share(a.rect, b.rect) >= OVERLAP_SHARE


SAME_ROOM_DISTANCE = 1.5   # ft (~450 mm): room points closer than this = the same room (two links)


def centre_of(info):
    """Room point of a tag's room, read once (room_centre is defined further down)."""
    if not hasattr(info, '_centre'):
        info._centre = safe(lambda: room_centre(info))
    return info._centre


def same_place(a, b):
    """True when the two tags' rooms have their room points at the same place
    in the model. Unknown room point -> False (never delete on a guess)."""
    pa, pb = centre_of(a), centre_of(b)
    if pa is None or pb is None:
        return False
    dx, dy = pa.X - pb.X, pa.Y - pb.Y
    return (dx * dx + dy * dy) ** 0.5 <= SAME_ROOM_DISTANCE


def keep_order(info):
    """Best tag to keep first: inside its room, no leader, oldest (lowest id)."""
    return (0 if info.in_room else 1, 1 if info.has_leader else 0, info.id)


# --------------------------------------------------------------------------- analysis
class ViewResult(object):
    def __init__(self, view):
        self.view = view
        self.tags = 0
        self.dup_overlap = []     # TagInfo to delete: same room, overlapping a kept tag
        self.dup_far = []         # TagInfo: same room, tagged again elsewhere in the view
        self.dup_text = []        # TagInfo to delete: identical text overlapping (same room via 2 links)
        self.orphans = []         # TagInfo: room deleted / not placed
        self.broken = []          # (TagInfo, reason): not orphaned, but shows "?" / room unreadable
        self.clashes = []         # (TagInfo, TagInfo): different rooms overlapping


def analyse_view(view, seen):
    res = ViewResult(view)
    tags = [t for t in DB.FilteredElementCollector(doc, view.Id)
            .OfCategory(DB.BuiltInCategory.OST_RoomTags).WhereElementIsNotElementType()
            if eid_int(t.Id) not in seen]       # dependent views show their parent's tags
    if not tags:
        return res
    for t in tags:
        seen.add(eid_int(t.Id))
    res.tags = len(tags)

    right, up = view.RightDirection, view.UpDirection
    infos = [TagInfo(t, view) for t in tags]
    for info in infos:
        info.measure(right, up)

    # A tag WITH a leader has a box that includes the leader line: use a box
    # of the usual size of its tag type (tags without leader) around its head.
    sizes = defaultdict(list)
    for info in infos:
        if not info.has_leader and info.size():
            sizes[info.type_id].append(info.size())
    for info in infos:
        known = sizes.get(info.type_id)
        if info.has_leader and known and info.head:
            w = _median([s[0] for s in known])
            h = _median([s[1] for s in known])
            u, v = info.head
            info.rect = (u - w / 2.0, v - h / 2.0, u + w / 2.0, v + h / 2.0)

    res.orphans = [i for i in infos if i.orphaned]
    live = [i for i in infos if not i.orphaned and i.key is not None]

    # Not flagged orphaned by Revit, but useless on the drawing: shows "?"
    broken_ids = set()
    for info in live:
        why = broken_reason(info)
        if why:
            res.broken.append((info, why))
            broken_ids.add(info.id)
    live = [i for i in live if i.id not in broken_ids]

    # same room tagged more than once
    by_room = defaultdict(list)
    for info in live:
        by_room[info.key].append(info)
    kept = []
    for group in by_room.values():
        group.sort(key=keep_order)
        keepers = [group[0]]
        for info in group[1:]:
            if any(overlaps(info, k) for k in keepers):
                res.dup_overlap.append(info)
            else:
                res.dup_far.append(info)
                keepers.append(info)          # stays unless the user asks otherwise
        kept.extend(keepers)

    # different rooms on top of each other (reported, never deleted)
    kept.sort(key=lambda i: i.rect[0] if i.rect else 0.0)
    for a_index, a in enumerate(kept):
        if a.rect is None:
            continue
        for b in kept[a_index + 1:]:
            if b.rect is None:
                continue
            if b.rect[0] > a.rect[2]:
                break                          # sorted by left edge: nothing further can touch a
            if a.key != b.key and overlaps(a, b):
                res.clashes.append((a, b))

    # Identical text on top of each other AND the two rooms in the same place =
    # the same room reached through two links (architecture linked twice, or
    # rooms in two models). Removing one loses nothing on the drawing.
    # The same-place test matters: a tag type that shows only the room NAME
    # gives two different neighbouring rooms ("WC", "WC") identical text -
    # deleting one of those left a room without a tag.
    removed, real = set(), []
    for a, b in res.clashes:
        if a.id in removed or b.id in removed:
            continue
        if a.label and a.label == b.label and same_place(a, b):
            worse = max(a, b, key=keep_order)
            res.dup_text.append(worse)
            removed.add(worse.id)
        else:
            real.append((a, b))
    res.clashes = [(a, b) for a, b in real if a.id not in removed and b.id not in removed]
    return res


def tagged_room(info):
    """(room, link transform or None) for a tag; room is None when it cannot be read."""
    tag = info.tag
    room_id = safe(lambda: tag.TaggedRoomId)
    if room_id is None:
        return None, None
    transform, room = None, None
    link_id = safe(lambda: room_id.LinkInstanceId)
    if link_id is not None and eid_int(link_id) > 0:
        link = doc.GetElement(link_id)
        link_doc = safe(lambda: link.GetLinkDocument())
        if link_doc is not None:
            room = safe(lambda: link_doc.GetElement(room_id.LinkedElementId))
            transform = safe(lambda: link.GetTotalTransform())
    else:
        room = safe(lambda: doc.GetElement(safe(lambda: tag.TaggedLocalRoomId, room_id.HostElementId)))
    return room, transform


def broken_reason(info):
    """Why a tag that Revit does not call orphaned still shows "?" - or None.
    Only clear-cut cases: a tag whose room exists, is placed and has a real
    value is never listed."""
    # A tag of an UNLOADED link is never "broken" - reloading the link fixes it
    room_id = safe(lambda: info.tag.TaggedRoomId)
    link_id = safe(lambda: room_id.LinkInstanceId)
    if link_id is not None and eid_int(link_id) > 0:
        link = doc.GetElement(link_id)
        if link is not None and safe(lambda: link.GetLinkDocument()) is None:
            return None
    label = info.label or u''
    if label and not label.replace(u'?', u'').strip():
        return u'tag shows only "?" (room values blank)'
    room, _t = tagged_room(info)
    if room is None:
        return u'room cannot be found (deleted in the link)'
    if safe(lambda: room.Location) is None:
        return u'room not placed'
    return None


def room_centre(info):
    """The room's location point (host coordinates) - where Revit itself places a
    room tag; always inside the room, also for L-shaped rooms. None if unknown."""
    room, transform = tagged_room(info)
    point = safe(lambda: room.Location.Point)
    if point is None:
        return None
    return transform.OfPoint(point) if transform is not None else point


def plan_moves(results, skip_ids):
    """[(TagInfo, move vector)] for the tags of the remaining overlap pairs, plus counts.
    Only the view-plane offset is applied (works in plans and sections alike)."""
    moves, done = [], set()
    stats = {'leader': 0, 'at_centre': 0, 'no_room': 0, 'locked': 0}
    for r in results:
        right, up = r.view.RightDirection, r.view.UpDirection
        for pair in r.clashes:
            for info in pair:
                if info.id in done or info.id in skip_ids:
                    continue
                done.add(info.id)
                if info.has_leader:
                    stats['leader'] += 1
                    continue
                centre = centre_of(info)
                if centre is None or info.head is None:
                    stats['no_room'] += 1
                    continue
                du = centre.DotProduct(right) - info.head[0]
                dv = centre.DotProduct(up) - info.head[1]
                if (du * du + dv * dv) ** 0.5 < MOVE_TOLERANCE:
                    stats['at_centre'] += 1
                    continue
                if owned_by_other(info.tag.Id):
                    stats['locked'] += 1
                    continue
                moves.append((info, right.Multiply(du).Add(up.Multiply(dv))))
    return moves, stats


def owned_by_other(element_id):
    if not doc.IsWorkshared:
        return False
    status = safe(lambda: DB.WorksharingUtils.GetCheckoutStatus(doc, element_id))
    return status == DB.CheckoutStatus.OwnedByOtherUser


# --------------------------------------------------------------------------- report
# pyRevit's print_table / print_md run every table through its Markdown
# converter, which under IronPython recurses so deeply on a table of a few
# hundred rows that .NET runs out of stack and REVIT CRASHES (0xc00000fd) -
# that was the 11:14 crash (309 overlap pairs). Tables and anything holding
# links are written as plain HTML, in chunks, with a row cap.
MAX_ROWS = 300
CHUNK = 100


class Raw(type(u'')):
    """Text that is already HTML (a linkify() link) - not escaped."""


def _esc(value):
    if isinstance(value, Raw):
        return value
    text = value if isinstance(value, type(u'')) else u'{0}'.format(value)
    return text.replace(u'&', u'&amp;').replace(u'<', u'&lt;').replace(u'>', u'&gt;')


def html(text):
    output.print_html(text)


def print_rows(columns, rows, limit=MAX_ROWS):
    head = u''.join(u'<th>{0}</th>'.format(_esc(c)) for c in columns)
    shown = rows[:limit]
    for start in range(0, len(shown), CHUNK):
        body = u''.join(u'<tr>{0}</tr>'.format(u''.join(u'<td>{0}</td>'.format(_esc(v)) for v in row))
                        for row in shown[start:start + CHUNK])
        html(u'<table><thead><tr>{0}</tr></thead><tbody>{1}</tbody></table>'.format(head, body))
    if len(rows) > limit:
        html(u'<p><i>... {0} more row(s) not shown - use the "select" link above.</i></p>'.format(len(rows) - limit))


def link(info, deleted=False):
    text = u'{0} {1}'.format(info.id, info.label or u'')
    if deleted:                    # no link to a deleted element
        return u'{0} (deleted)'.format(text)
    return Raw(output.linkify(info.eid, text))


def view_link(view):
    return Raw(output.linkify(view.Id, view.Name))


def write_move_report(moved, failed, stats, after):
    """after: [(ViewResult re-checked after the move)] or None when nothing moved."""
    if not (moved or failed or any(stats.values())):
        return
    html(u'<h2>Moved to room centre</h2>')
    lines = [u'<b>{0} tag(s) moved</b> to the centre of their room.'.format(moved)]
    if stats['at_centre']:
        lines.append(u'{0} tag(s) already at their room centre - not moved.'.format(stats['at_centre']))
    if stats['leader']:
        lines.append(u'{0} tag(s) with a leader left where they are (placed outside the room on purpose).'
                     .format(stats['leader']))
    if stats['no_room']:
        lines.append(u'{0} tag(s) not moved: their room (or its link) could not be read.'.format(stats['no_room']))
    if stats['locked']:
        lines.append(u'{0} tag(s) not moved: owned by another user.'.format(stats['locked']))
    if failed:
        lines.append(u'{0} tag(s) could not be moved (pinned or locked by Revit).'.format(failed))
    html(u'<p>' + u'<br>'.join(lines) + u'</p>')
    if after is not None:
        left = [(r, a, b) for r in after for a, b in r.clashes]
        if not left:
            html(u'<p><b>No overlapping room tags left</b> in the moved views.</p>')
        else:
            ids = []
            for _r, a, b in left:
                ids.extend([a.tag.Id, b.tag.Id])
            html(u'<p><b>{0} pair(s) still overlap after the move</b> (rooms too small for both tags) - '
                 u'move these by hand: {1}</p>'.format(len(left), output.linkify(ids, u'select them')))
            print_rows([u'View', u'Tag', u'Overlaps tag'], [[view_link(r.view), link(a), link(b)] for r, a, b in left])


def write_report(results, deleted_ids, locked_ids, mode):
    html(u'<h1>{0}</h1>'.format(TITLE))
    html(u'<p><b>{0}</b> - overlap = tags sharing at least {1:.0f} % of the smaller tag.</p>'.format(
        _esc(mode), OVERLAP_SHARE * 100))
    rows = []
    for r in results:
        if not (r.dup_overlap or r.dup_text or r.dup_far or r.orphans or r.broken or r.clashes):
            continue
        rows.append([view_link(r.view), r.tags, len(r.dup_overlap),
                     len(r.dup_text), len(r.dup_far), len(r.orphans), len(r.broken), len(r.clashes)])
    if not rows:
        html(u'<p>No overlapping, duplicate or orphaned room tags found.</p>')
        return
    print_rows([u'View', u'Room tags', u'Overlapping duplicates', u'Identical text overlapping (2 links)',
                u'Same room tagged again (apart)', u'Orphaned', u'Shows "?"',
                u'Different rooms overlapping'], rows)
    broken = [(r, info, why) for r in results for info, why in r.broken]
    if broken:
        gone = set(deleted_ids)
        still = [(r, info, why) for r, info, why in broken if info.id not in gone]
        html(u'<h2>Tags showing "?" (not flagged orphaned by Revit)</h2>')
        if still:
            html(u'<p>{0} still in the model - tick the orphaned option to delete them, or '
                 u'{1}</p>'.format(len(still), output.linkify([i.eid for _r, i, _w in still],
                                                               u'select them')))
        print_rows([u'View', u'Tag', u'Why'],
                   [[view_link(r.view), link(info, info.id in gone), why] for r, info, why in broken])
    if deleted_ids:
        html(u'<p><b>{0} tag(s) deleted</b> (one Undo puts them back).</p>'.format(len(deleted_ids)))
    if locked_ids:
        html(u'<p><b>{0} tag(s) not deleted: owned by another user</b> - ask them to sync, '
             u'then run again.</p>'.format(len(locked_ids)))
    clashes = [(r, a, b) for r in results for a, b in r.clashes]
    if clashes:
        html(u'<h2>Different rooms overlapping (not deleted - move one of each pair)</h2>')
        # Clicking selects them; the tool never sets the Revit selection itself.
        pair_ids = []
        for _r, a, b in clashes:
            pair_ids.extend([a.tag.Id, b.tag.Id])
        html(u'<p>Select all {0} overlapping tags: {1}</p>'.format(
            len(pair_ids), output.linkify(pair_ids, u'click here')))
        print_rows([u'View', u'Tag', u'Overlaps tag'], [[view_link(r.view), link(a), link(b)] for r, a, b in clashes])


# --------------------------------------------------------------------------- main
def main():
    if doc.IsFamilyDocument:
        forms.alert('Open a project, not a family.', title=TITLE, exitscript=True)

    scope = forms.CommandSwitchWindow.show(
        [SCOPE_ACTIVE, SCOPE_SELECTED, SCOPE_SHEETS],
        message='Where should overlapping room tags be cleaned up?')
    if not scope:
        return
    trace(u'---- start: model "{0}", active view "{1}", scope "{2}"'.format(
        safe(lambda: doc.Title, u'?'), safe(lambda: doc.ActiveView.Name, u'?'), scope))
    views = views_in_scope(scope)
    trace(u'{0} view(s) in scope'.format(len(views)))
    if not views:
        forms.alert('No plan, section or elevation view in that scope.\n\n'
                    'For "selected", select views or sheets in the Project Browser first.',
                    title=TITLE)
        return

    results, seen = [], set()
    with forms.ProgressBar(title='Checking room tags ... ({value} of {max_value} views)') as bar:
        for index, view in enumerate(views, 1):
            trace(u'analyse view {0}/{1}: {2}'.format(index, len(views), view.Name))
            try:
                results.append(analyse_view(view, seen))
            except Exception as ex:
                trace(u'  view skipped: {0}'.format(ex))
                output.print_md(u'View **{0}** skipped: {1}'.format(view.Name, ex))
            bar.update_progress(index, len(views))
    trace(u'analysis done')

    n_tags = sum(r.tags for r in results)
    n_dup = sum(len(r.dup_overlap) for r in results)
    n_text = sum(len(r.dup_text) for r in results)
    n_far = sum(len(r.dup_far) for r in results)
    n_orph = sum(len(r.orphans) for r in results)
    n_broken = sum(len(r.broken) for r in results)
    n_clash = sum(len(r.clashes) for r in results)
    trace(u'found: {0} tags, {1} overlapping duplicates, {2} identical text, {3} apart, {4} orphaned, '
          u'{5} showing "?", {6} overlap pairs'.format(n_tags, n_dup, n_text, n_far, n_orph, n_broken, n_clash))
    if not (n_dup or n_text or n_far or n_orph or n_broken or n_clash):
        trace(u'nothing to clean - finished normally')
        forms.alert(u'Checked {0} room tag(s) in {1} view(s): no overlapping, duplicate or '
                    u'orphaned room tags.'.format(n_tags, len(views)), title=TITLE)
        return

    message = (u'Checked {0} room tag(s) in {1} view(s):\n\n'
               u'  {2}  overlapping duplicates (same room)  -> deleted\n'
               u'  {3}  overlapping tags with IDENTICAL text (same room from 2 links)  -> deleted\n'
               u'  {4}  same room tagged again elsewhere in the view  -> kept unless ticked\n'
               u'  {5}  orphaned tags (room deleted / not placed)  -> kept unless ticked\n'
               u'  {7}  tags showing "?" (room missing / not placed / blank values)\n'
               u'        -> kept unless the orphaned option is ticked\n'
               u'  {6}  pairs of DIFFERENT rooms overlapping  -> never deleted; moved to their\n'
               u'        room centre if you tick that option, else listed in the report\n'
               ).format(n_tags, len(views), n_dup, n_text, n_far, n_orph, n_clash, n_broken)
    choice = forms.CommandSwitchWindow.show(
        [ACT_CLEAN, ACT_REPORT],
        switches={SW_ORPHANS: False, SW_FAR: False, SW_MOVE: False},
        message=message)
    if not choice or not choice[0]:
        return
    action, switches = choice
    switches = switches or {}

    to_delete = []
    if action == ACT_CLEAN:
        for r in results:
            to_delete.extend(r.dup_overlap)
            to_delete.extend(r.dup_text)
            if switches.get(SW_FAR):
                to_delete.extend(r.dup_far)
            if switches.get(SW_ORPHANS):
                to_delete.extend(r.orphans)
                to_delete.extend(info for info, _why in r.broken)

    deleted, locked = [], []
    ids = List[DB.ElementId]()
    for info in to_delete:
        if owned_by_other(info.tag.Id):
            locked.append(info.id)
        else:
            ids.Add(info.tag.Id)
            deleted.append(info.id)

    moves, move_stats = [], {'leader': 0, 'at_centre': 0, 'no_room': 0, 'locked': 0}
    if action == ACT_CLEAN and switches.get(SW_MOVE):
        moves, move_stats = plan_moves(results, set(deleted) | set(locked))
    trace(u'choice: {0}  switches: {1}  to delete: {2}  to move: {3}'.format(
        action, switches, ids.Count, len(moves)))
    if action == ACT_CLEAN and switches.get(SW_MOVE):
        trace(u'  not moved: {0} with leader, {1} already at room centre, {2} room not readable, '
              u'{3} owned by another user'.format(move_stats['leader'], move_stats['at_centre'],
                                                   move_stats['no_room'], move_stats['locked']))

    moved, failed = 0, 0
    if ids.Count or moves:
        with revit.Transaction(TITLE):              # one Undo for deletes and moves
            if ids.Count:
                trace(u'delete: start ({0} tags)'.format(ids.Count))
                doc.Delete(ids)
                trace(u'delete: done')
            if moves:
                trace(u'move: start ({0} tags)'.format(len(moves)))
                for info, vector in moves:
                    try:
                        DB.ElementTransformUtils.MoveElement(doc, info.tag.Id, vector)
                        moved += 1
                    except Exception as ex:
                        failed += 1
                        trace(u'  move failed for tag {0}: {1}'.format(info.id, ex))
                trace(u'move: done ({0} moved, {1} failed)'.format(moved, failed))
        trace(u'transaction committed')

    after = None
    if moved:                                   # re-check the views where tags moved
        moved_views = set(eid_int(info.view.Id) for info, _v in moves)
        after, seen_after = [], set()
        for r in results:
            if eid_int(r.view.Id) in moved_views:
                try:
                    after.append(analyse_view(r.view, seen_after))
                except Exception as ex:
                    trace(u'  re-check skipped for {0}: {1}'.format(r.view.Name, ex))
        trace(u're-check: {0} pair(s) still overlapping'.format(sum(len(r.clashes) for r in after)))

    trace(u'report: start')
    try:
        write_report(results, deleted, locked,
                     u'Cleaned up' if action == ACT_CLEAN else u'Report only - nothing changed')
        write_move_report(moved, failed, move_stats, after)
    except Exception as ex:
        # the clean-up itself is already committed - only the report failed
        trace(u'report FAILED: {0}'.format(ex))
        output.print_md(u'**Report failed** (the clean-up itself was saved): {0}'.format(ex))
        raise
    trace(u'report: done - finished normally')


main()
