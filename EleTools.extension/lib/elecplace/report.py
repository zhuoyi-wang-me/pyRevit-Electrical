# -*- coding: utf-8 -*-
"""
elecplace.report
================
Renders the run report in the pyRevit output window (markdown tables with
clickable element links) and optionally exports a CSV copy.

Sections: Execution summary | Rules & heights | Rooms processed |
          Fixtures placed | Skipped rooms & reasons | Errors
"""
import io
import os
import time

from elecplace.compat import eid_int, to_text
import safetable  # crash-safe output tables (lib/safetable.py)

MAX_LINK_ROWS = 1000   # linkify() is slow on huge tables; cap clickable rows


class RunContext(object):
    """Everything the report needs, collected by script.py."""

    def __init__(self):
        self.link = None
        self.settings = None
        self.rules = []
        self.rooms = []
        self.skipped_rooms = []
        self.plan = None
        self.result = None
        self.t_start = time.time()
        self.t_end = None
        self.cancelled = False
        self.fatal_error = None


def _csv_cell(value):
    text = to_text(value).replace(u'"', u'""')
    return u'"{}"'.format(text)


def export_csv(ctx, rows):
    folder = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "ElecAutoPlace")
    if not os.path.isdir(folder):
        os.makedirs(folder)
    path = os.path.join(folder, "ElecAutoPlace_{}.csv".format(time.strftime("%Y%m%d_%H%M%S")))
    header = [u"Record", u"Room Number", u"Room Name", u"Level", u"Fixture Type",
              u"Height AFF", u"Element Id", u"Host", u"Detail"]
    with io.open(path, "w", encoding="utf-8-sig") as fh:
        fh.write(u",".join(_csv_cell(h) for h in header) + u"\n")
        for row in rows:
            fh.write(u",".join(_csv_cell(c) for c in row) + u"\n")
    return path


TRACE_FILE = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                          "pyRevit", "AutoPlaceFixtures_trace.log")
TRACE_MAX_BYTES = 2 * 1024 * 1024     # start a fresh file above 2 MB


def _trace_reason_key(text):
    """Group similar reasons: '1234 mm away' and '987 mm away' -> '# mm away'."""
    import re
    return re.sub(r"\d+(\.\d+)?", u"#", to_text(text))


def write_trace(ctx, doc_title=u"", stage=u"done", error_text=None):
    """
    Append a plain-text summary of this run to TRACE_FILE so a run can be
    checked afterwards (the output window is gone once closed). Never raises.
    """
    try:
        folder = os.path.dirname(TRACE_FILE)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        if os.path.exists(TRACE_FILE) and os.path.getsize(TRACE_FILE) > TRACE_MAX_BYTES:
            os.remove(TRACE_FILE)
        plan, res = ctx.plan, ctx.result
        placed = res.placed if res else []
        errors = res.errors if res else []
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [u"{}  ---- run: model \"{}\", link \"{}\", stage {}".format(
            now, to_text(doc_title), ctx.link.name if ctx.link else u"-", stage)]
        for r in ctx.rules or []:
            lines.append(u"{}  rule: {} | kind {} | mode {} {} | height {} | qty {} | "
                         u"equipment from {} | names {} | max dist {:.0f} mm | rooms {}".format(
                             now, to_text(getattr(r, "label", u"?")), getattr(r, "kind", u"?"),
                             to_text(getattr(r, "mode", None) or u"REJECTED"),
                             u"(" + to_text(r.mode_reason) + u")" if getattr(r, "mode_reason", u"") else u"",
                             to_text(getattr(r, "height_text", u"")), to_text(getattr(r, "quantity", u"")),
                             to_text(getattr(r, "equip_source", u"-")),
                             (u"{} picked type(s): {}".format(len(r.equip_types), u"; ".join(r.equip_types))
                              if getattr(r, "equip_types", None) else
                              u",".join(getattr(r, "equip_keywords", []) or []) or u"(any)"),
                             (getattr(r, "max_dist_ft", 0.0) or 0.0) * 304.8,
                             len(getattr(r, "room_uids", []) or [])))
        lines.append(u"{}  counts: rooms {} (skipped reading link {}), planned {}, placed {}, "
                     u"plan issues {}, placement errors {}, flipped {}".format(
                         now, len(ctx.rooms or []), len(ctx.skipped_rooms or []),
                         len(plan.placements) if plan else 0, len(placed),
                         len(plan.issues) if plan else 0, len(errors), res.flipped if res else 0))
        if plan:
            for line in plan.info:
                lines.append(u"{}  info: {}".format(now, to_text(line)))
            groups = {}
            for _room, _lvl, rule_lbl, why in plan.issues:
                key = (to_text(rule_lbl), _trace_reason_key(why))
                groups[key] = groups.get(key, 0) + 1
            for (rule_lbl, why), n in sorted(groups.items(), key=lambda kv: -kv[1])[:15]:
                lines.append(u"{}  skip x{}: [{}] {}".format(now, n, rule_lbl, why))
            # up to 3 examples of EVERY kind of skip (not just the first 10 issues,
            # which are often all the same reason)
            shown = {}
            for room_lbl, lvl, rule_lbl, why in plan.issues:
                key = _trace_reason_key(why)
                if shown.get(key, 0) >= 3:
                    continue
                shown[key] = shown.get(key, 0) + 1
                lines.append(u"{}  issue: {} ({}) [{}] {}".format(now, to_text(room_lbl), to_text(lvl),
                                                                  to_text(rule_lbl), to_text(why)))
        egroups = {}
        for _pp, msg in errors:
            k = _trace_reason_key(msg)
            egroups[k] = egroups.get(k, 0) + 1
        for msg, n in sorted(egroups.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(u"{}  error x{}: {}".format(now, n, msg))
        if placed:
            ids = u", ".join(to_text(eid_int(eid)) for _pp, eid in placed[:20])
            lines.append(u"{}  placed ids: {}{}".format(now, ids, u" ..." if len(placed) > 20 else u""))
        if ctx.fatal_error or error_text:
            lines.append(u"{}  FATAL: {}".format(now, to_text(error_text or ctx.fatal_error)))
        with io.open(TRACE_FILE, "a", encoding="utf-8") as fh:
            fh.write(u"\n".join(lines) + u"\n")
    except Exception:
        pass


def render(output, ctx):
    plan, res = ctx.plan, ctx.result
    placed = res.placed if res else []
    errors = res.errors if res else []
    ctx.t_end = ctx.t_end or time.time()
    csv_rows = []

    output.print_md(u"# Auto-Place Electrical Fixtures - Report")

    # ---- Execution summary ------------------------------------------------
    rooms_touched = set(pp.room.uid for pp, _ in placed)
    summary = [
        [u"Linked model", ctx.link.name if ctx.link else u"-"],
        [u"Eligible rooms (placed & enclosed)", len(ctx.rooms)],
        [u"Rooms skipped while reading link", len(ctx.skipped_rooms)],
        [u"Rules (fixture types)", len(ctx.rules)],
        [u"Fixtures planned", len(plan.placements) if plan else 0],
        [u"Fixtures placed", len(placed)],
        [u"Rooms receiving fixtures", len(rooms_touched)],
        [u"Planning skips / shortfalls", len(plan.issues) if plan else 0],
        [u"Placement errors", len(errors)],
        [u"Orientation corrections (flipped)", res.flipped if res else 0],
        [u"Status", u"CANCELLED" if ctx.cancelled else
         (u"FAILED: " + to_text(ctx.fatal_error) if ctx.fatal_error else u"Completed")],
        [u"Duration", u"{:.1f} s".format(ctx.t_end - ctx.t_start)],
    ]
    safetable.print_table(output, table_data=summary, columns=[u"Item", u"Value"], title=u"Execution summary")
    if plan and plan.info:
        output.print_md(u"**Equipment found per source model**")
        for line in plan.info:
            output.print_md(u"- " + line)

    # ---- Rules / heights used --------------------------------------------
    if ctx.rules:
        rows = []
        for r in ctx.rules:
            n = sum(1 for pp, _ in placed if pp.rule is r)
            rows.append([r.category_name, r.label,
                         {"walls": u"Room walls", "equipment": u"Wall next to equipment",
                          "on_equipment": u"On equipment"}.get(r.kind, r.kind),
                         r.height_text, r.quantity, len(r.room_uids),
                         r.mode or u"REJECTED", n, r.mode_reason or u""])
        safetable.print_table(output, table_data=rows, title=u"Rules and heights used",
                           columns=[u"Category", u"Family type", u"Placement", u"Height AFF",
                                    u"Quantity rule", u"Target rooms", u"Mode", u"Placed", u"Note"])

    # ---- Rooms processed --------------------------------------------------
    if plan and ctx.rooms:
        targeted = set()
        for r in ctx.rules:
            targeted.update(r.room_uids)
        rows = []
        for room in ctx.rooms:
            if room.uid not in targeted:
                continue
            here = [pp for pp, _ in placed if pp.room.uid == room.uid]
            segs, notes = plan.segment_stats.get(room.uid, (0, u""))
            rows.append([room.number, room.name, room.level_name,
                         u"{:.1f}".format(room.area * 0.09290304), len(room.bounding_wall_ids),
                         segs, len(here), notes or u"-"])
        if rows:
            safetable.print_table(output, table_data=rows, title=u"Rooms processed",
                               columns=[u"Number", u"Name", u"Level", u"Area (m2)", u"Bounding walls",
                                        u"Usable wall segments", u"Fixtures placed",
                                        u"Rejected boundary pieces"])

    # ---- Fixtures placed --------------------------------------------------
    if placed:
        rows = []
        for i, (pp, eid) in enumerate(placed):
            link_cell = output.linkify(eid) if i < MAX_LINK_ROWS else to_text(eid_int(eid))
            rows.append([link_cell, pp.rule.label, pp.room.number, pp.room.name,
                         pp.room.level_name, pp.rule.height_text, pp.host_desc,
                         pp.served or u"-",
                         u"; ".join(u"{}={}".format(n, v) for n, v in pp.param_values) or u"-"])
            csv_rows.append([u"PLACED", pp.room.number, pp.room.name, pp.room.level_name,
                             pp.rule.label, pp.rule.height_text, eid_int(eid), pp.host_desc,
                             (u"serves " + pp.served + u" | " if pp.served else u"") + pp.rule.mode])
        safetable.print_table(output, table_data=rows, title=u"Fixtures placed ({})".format(len(placed)),
                           columns=[u"Element", u"Type", u"Room No.", u"Room name", u"Level",
                                    u"Height AFF", u"Host", u"Serves", u"Data written"])
        if len(placed) > 1:
            safetable.print_line(output, u"Select all placed fixtures: {}".format(
                output.linkify([eid for _, eid in placed[:MAX_LINK_ROWS]], title=u"Select all")))

    # ---- Skipped rooms ----------------------------------------------------
    rows = []
    for room, why in ctx.skipped_rooms:
        rows.append([room.number, room.name, room.level_name, u"(link data)", why])
        csv_rows.append([u"SKIPPED ROOM", room.number, room.name, room.level_name, u"",
                         u"", u"", u"", why])
    if plan:
        for room_lbl, lvl, rule_lbl, why in plan.issues:
            rows.append([room_lbl, u"", lvl, rule_lbl, why])
            csv_rows.append([u"PLAN ISSUE", room_lbl, u"", lvl, rule_lbl, u"", u"", u"", why])
    if rows:
        safetable.print_table(output, table_data=rows, title=u"Skipped rooms and reasons ({})".format(len(rows)),
                           columns=[u"Room", u"Name", u"Level", u"Fixture type", u"Reason"])

    # ---- Data written to parameters ----------------------------------------
    if res and (res.data_written or res.data_failed):
        rows = [[name, n, u"written"] for name, n in sorted(res.data_written.items())]
        rows += [[name, n, u"FAILED: " + why] for (name, why), n in sorted(res.data_failed.items())]
        safetable.print_table(output, table_data=rows, title=u"Data written to placed fixtures",
                           columns=[u"Parameter", u"Fixtures", u"Result"])
    if plan and plan.missing_tokens:
        for token, n in sorted(plan.missing_tokens.items()):
            output.print_md(u"- Token `{}` had no value for {} fixture(s) (written empty)".format(token, n))

    # ---- Errors -----------------------------------------------------------
    if errors or ctx.fatal_error:
        rows = []
        for pp, msg in errors:
            if pp is None:
                rows.append([u"-", u"-", msg])
                continue
            rows.append([pp.room.label, pp.rule.label, msg])
            csv_rows.append([u"ERROR", pp.room.number, pp.room.name, pp.room.level_name,
                             pp.rule.label, pp.rule.height_text, u"", pp.host_desc, msg])
        if ctx.fatal_error:
            rows.append([u"-", u"-", to_text(ctx.fatal_error)])
        safetable.print_table(output, table_data=rows, title=u"Errors encountered",
                           columns=[u"Room", u"Fixture type", u"Message"])

    # ---- CSV --------------------------------------------------------------
    if ctx.settings and ctx.settings.export_csv and csv_rows:
        try:
            path = export_csv(ctx, csv_rows)
            output.print_md(u"**CSV report saved:** `{}`".format(path))
        except Exception as ex:
            output.print_md(u"CSV export failed: {}".format(to_text(ex)))
