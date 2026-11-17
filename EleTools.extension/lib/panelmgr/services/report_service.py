# -*- coding: utf-8 -*-
"""
report_service.py
===================
Builds the human-readable balancing / spare-space report (requirement
6) from the results produced by `schedule_service.execute_batch` (or
`run_preview`), and can export it to HTML/CSV. No Revit API
dependency - operates entirely on `models.py` result objects.
"""

import csv
import io
import re
import time

try:                      # Python 3
    from html import escape as _html_escape
except ImportError:       # IronPython 2.7
    from cgi import escape as _html_escape

from panelmgr.logger import get_logger

logger = get_logger(__name__)


def result_property_name(header):
    """
    Turns a report column header into the property name a XAML
    `{Binding ...}` can reach.

    FORTY-SIXTH CORRECTION: the viewmodel used to mangle headers with
    an inline expression that stripped only spaces, parentheses and
    "%". That left the DOT in "Imbalance Incl. Spares (%)", producing
    `ImbalanceIncl.SparesPct` - which Python's `setattr` accepts
    happily, but WPF parses as a nested property PATH
    (`ImbalanceIncl` -> `SparesPct`). The binding could therefore never
    resolve, and WPF renders an unresolvable binding as a silent blank
    cell rather than an error. Anything that is not a letter or digit
    is now removed, and a leading digit is prefixed, so every header
    yields a legal identifier.

    This lives here, next to the headers it has to stay in step with,
    rather than in the viewmodel - the viewmodel package imports `clr`
    and so cannot be loaded (or tested) outside Revit.
    """
    name = "{}".format(header).replace("%", "Pct")
    name = re.sub(r"[^0-9A-Za-z]", "", name)
    if not name:
        name = "Column"
    if name[0].isdigit():
        name = "_" + name
    return name


def build_report_rows(balancing_results, spare_space_reports_by_panel):
    """
    Returns a list of dict rows, one per panel, suitable for both the
    WPF results grid and CSV/HTML export:

        Panel | Status | Total Load (W) | R (W) | Y (W) | B (W) |
        Total Load (VA) | Imbalance Before % | Imbalance After % |
        3-Pole R-Y-B | Spares | Spaces

    TWENTIETH CORRECTION: Watts columns are now primary (matching what
    a project's own printed schedule labels as "CONN./DEMAND WATTS",
    and what a user reasonably expects to see), computed via
    `CircuitInfo.true_load_w` (VA x Power Factor, approximated - see
    that field's docstring for the caveat). VA remains available as a
    secondary column since it's the actual, electrically-correct basis
    the balancing/imbalance-percentage math itself uses.

    TWENTY-FOURTH CORRECTION: "3-Pole R-Y-B" surfaces the post-move
    verification result (execute_batch's TWENTY-FIRST CORRECTION)
    directly in this grid - "OK" if every 3-pole circuit landed on a
    genuine R-Y-B sequence, or the specific circuit name(s) and actual
    phase order otherwise - so confirming this no longer requires
    digging through pyRevit's log panel.
    """
    rows = []
    for bal in balancing_results:
        ss = spare_space_reports_by_panel.get(bal.panel_name)
        if bal.misaligned_3pole_circuits:
            ryb_status = "; ".join(
                "{} ({})".format(name, phases)
                for name, phases in bal.misaligned_3pole_circuits
            )
        else:
            ryb_status = "OK"
        row = {
            "Panel": bal.panel_name,
            "Status": "OK",
            "Total Connected Load (W)": round(bal.total_connected_load_w, 1),
            "R Load (W)": round(_phase_load(bal.after_phase_watts, "R"), 1),
            "Y Load (W)": round(_phase_load(bal.after_phase_watts, "Y"), 1),
            "B Load (W)": round(_phase_load(bal.after_phase_watts, "B"), 1),
            "Total Connected Load (VA)": round(bal.total_connected_load_va, 1),
            "Imbalance Before (%)": round(bal.before_imbalance_pct, 2),
            "Imbalance After (%)": round(bal.after_imbalance_pct, 2),
            "RYB Status": ryb_status,
            "Circuits Moved": len(bal.moved_circuits),
            "Spares Added": ss.spares_added if ss else 0,
            "Spaces Added": ss.spaces_added if ss else 0,
            "Reserved Slots": ss.reserved_slots if ss else 0,
            # TWENTY-SIXTH / TWENTY-SEVENTH CORRECTIONS. On a preserved
            # panel "Imbalance After" is necessarily identical to
            # "Imbalance Before" - nothing was moved, and nothing could
            # be without invalidating the issued schedule. The number
            # that actually changed is the PROJECTED imbalance once the
            # new spare capacity is counted, so it is reported
            # alongside rather than instead of the connected-only
            # figure. Reporting only the connected figure would make a
            # preserved panel look like the tool did nothing.
            "Schedule": "Preserved" if bal.schedule_preserved else "Rearranged",
            "Spare Load Booked (VA)": round(
                sum((bal.spare_load_written_va or {}).values()), 1
            ),
            "Spare Load by Phase (VA)": ", ".join(
                "{} {:.0f}".format(p, v)
                for p, v in sorted((bal.spare_load_written_va or {}).items())
            ) or "-",
            "Imbalance Incl. Spares (%)": round(
                _projected_imbalance(bal), 2
            ),
        }
        rows.append(row)
    return rows


def _projected_imbalance(bal):
    """
    Imbalance once the booked spare load is added to each leg. Falls
    back to the connected-only figure when no spare load was written.
    """
    written = bal.spare_load_written_va or {}
    if not written:
        return bal.after_imbalance_pct
    loads = []
    for phase, pl in (bal.after_phase_loads or {}).items():
        loads.append(pl.load_va + written.get(phase, 0.0))
    if not loads:
        return bal.after_imbalance_pct
    avg = sum(loads) / float(len(loads))
    if avg <= 0:
        return 0.0
    return (max(loads) - min(loads)) / avg * 100.0


def _phase_load(phase_loads, phase):
    pl = phase_loads.get(phase)
    return pl.load_va if pl else 0.0


def _esc(value):
    """
    FORTY-FIFTH CORRECTION: report values went into the HTML export
    raw. Panel names routinely contain "&" ("LP-1 & 2"), and the
    FAILED rows embed an arbitrary Revit exception message, which can
    easily contain "<" or ">". Either one silently corrupts the
    surrounding table - and an exception text containing markup would
    be injected straight into a file the user then opens in a browser.
    """
    return _html_escape(u"{}".format(value), quote=True)


def export_csv(rows, filepath):
    if not rows:
        return None
    fieldnames = list(rows[0].keys())
    # `csv` writes BYTES on Python 2 / IronPython and TEXT on Python 3,
    # so the file has to be opened the way the running interpreter's
    # csv module expects or every write raises TypeError. This export
    # had never been exercised (only export_html is wired to the UI),
    # which is why the mismatch went unnoticed.
    if str is bytes:                       # IronPython 2.7
        f = open(filepath, "wb")
    else:                                  # CPython 3
        f = io.open(filepath, "w", newline="", encoding="utf-8")
    with f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    logger.info("Wrote report CSV to %s", filepath)
    return filepath


def export_html(rows, filepath, title="Panel Schedule Manager - Balancing Report"):
    if not rows:
        return None
    fieldnames = list(rows[0].keys())
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    html = []
    html.append("<html><head><meta charset='utf-8'><title>{}</title>".format(_esc(title)))
    html.append(
        "<style>"
        "body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222;}"
        "h1{font-size:20px;} .meta{color:#666;font-size:12px;margin-bottom:16px;}"
        "table{border-collapse:collapse;width:100%;}"
        "th,td{border:1px solid #ddd;padding:6px 10px;font-size:13px;text-align:right;}"
        "th{background:#1f2937;color:#fff;text-align:center;}"
        "td:first-child,th:first-child{text-align:left;}"
        "tr:nth-child(even){background:#f7f7f9;}"
        "</style></head><body>"
    )
    html.append("<h1>{}</h1>".format(_esc(title)))
    html.append("<div class='meta'>Generated {}</div>".format(ts))
    html.append("<table><thead><tr>")
    for fn in fieldnames:
        html.append("<th>{}</th>".format(_esc(fn)))
    html.append("</tr></thead><tbody>")
    for row in rows:
        html.append("<tr>")
        for fn in fieldnames:
            html.append("<td>{}</td>".format(_esc(row[fn])))
        html.append("</tr>")
    html.append("</tbody></table></body></html>")

    with io.open(filepath, "w", encoding="utf-8") as f:
        f.write(u"".join(html))
    logger.info("Wrote report HTML to %s", filepath)
    return filepath
