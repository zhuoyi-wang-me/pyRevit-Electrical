# -*- coding: utf-8 -*-
"""
Export Load Breakdown to Excel.

Reads every Electrical Circuit in the active model, groups it by the
distribution board it belongs to ("From") and by Load Classification
(A/C, LTG, SP.L, STANDBY, ...), sums the Apparent Load per group, and
writes a Load-Breakdown-style workbook (one row per feeder/board, one
column per load classification).

Engine: pyRevit's default IronPython engine (no "#! python3" shebang -
this file intentionally runs on IronPython).

Requires: Microsoft Excel installed on this machine (COM automation -
no pip packages needed).
"""

import clr
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import (
    FilteredElementCollector,
    BuiltInParameter,
    UnitUtils,
)
from Autodesk.Revit.DB.Electrical import ElectricalSystem

from pyrevit import revit, forms, script

try:
    from Autodesk.Revit.DB import UnitTypeId
    VA_UNIT = UnitTypeId.VoltAmperes
except ImportError:
    from Autodesk.Revit.DB import DisplayUnitType
    VA_UNIT = DisplayUnitType.DUT_VOLT_AMPERES

from System import Activator, Type, Array
from System.Reflection import BindingFlags
from System.Runtime.InteropServices import Marshal
from collections import OrderedDict
import os
import time
import System
import safetable  # crash-safe output tables (lib/safetable.py)

doc = revit.doc
output = script.get_output()

# Panel types whose own outgoing (final/branch) circuits should NOT be
# broken out in the export - these are terminal distribution boards
# (Power Panel, Lighting Panel, AC Panel, etc.) that can have dozens or
# hundreds of individual branch circuits each. They still appear as a
# normal row wherever something upstream feeds them, so the chain stays
# intact - they just don't get their own expanded panel block.
EXCLUDED_FINAL_PANEL_PREFIXES = set(["PP", "LP", "ACP", "CBS", "UDB"])


def is_excluded_final_panel(panel_name):
    """True if panel_name's leading hyphen-delimited code (e.g. "PP" in
    "PP-ST-NE-B2-08-01N") is one of the excluded final panel types."""
    if not panel_name:
        return False
    code = panel_name.split("-")[0].strip().upper()
    return code in EXCLUDED_FINAL_PANEL_PREFIXES


def va_to_kva(value_internal):
    if value_internal is None:
        return 0.0
    va = UnitUtils.ConvertFromInternalUnits(value_internal, VA_UNIT)
    return va / 1000.0



def get_bip(name):
    """
    Safely resolve a BuiltInParameter enum member by name.

    Accessing a nonexistent member (e.g. BuiltInParameter.SOME_NAME) raises
    AttributeError. Previously that AttributeError was thrown at the
    call site - as an argument expression - before get_param_value's own
    try/except could ever run, and it then propagated all the way up to
    the bare "except Exception: continue" in the main collection loop.
    That silently skipped EVERY circuit, which is why the export showed
    "0 circuits across 0 panels" even though circuits existed.

    Returns None if the name doesn't exist in this Revit API version, so
    callers can fall back gracefully instead of crashing.
    """
    return getattr(BuiltInParameter, name, None)


def get_param_value(element, bip):
    if bip is None:
        return None
    param = element.get_Parameter(bip)
    if param is None:
        return None
    return param


def split_load_classification_names(raw_name):
    """
    Feeder/riser circuits that feed another distribution board report a
    combined, semicolon-separated list of every classification present
    downstream (e.g. "LTG - GENERAL LIGHTING; SK - SOCKETS; AC - HVAC"),
    rather than a single category. Left as one string, every unique
    combination becomes its own column - with a large model that
    explodes into 100+ near-duplicate columns instead of ~20 real
    categories.

    Returns the individual category names as a list (stripped,
    deduplicated, order preserved) so the circuit's load can be divided
    evenly across each real category it actually belongs to, instead of
    either duplicating it under every combination or arbitrarily
    crediting only one.
    """
    if not raw_name:
        return []
    seen = []
    for part in raw_name.split(";"):
        name = part.strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def get_load_classification_names(circuit):
    # LoadClassification is exposed as a direct property on
    # ElectricalSystem in the Revit API (returns an ElementId), not
    # reliably as a retrievable Parameter in every Revit version - which
    # is why the BuiltInParameter route below was returning "Unclassified"
    # for every single circuit. Try the property first.
    try:
        lc_id = circuit.LoadClassification
        if lc_id is not None:
            lc_element = doc.GetElement(lc_id)
            if lc_element is not None:
                names = split_load_classification_names(lc_element.Name)
                if names:
                    return names
    except Exception:
        pass

    param = get_param_value(circuit, get_bip("RBS_ELEC_CIRCUIT_LOAD_CLASSIFICATION_PARAM"))
    if param is None:
        # Last resort: look the parameter up by its visible name instead
        # of an internal enum. Works regardless of which BuiltInParameter
        # name (if any) this Revit build uses internally.
        try:
            param = circuit.LookupParameter("Load Classification")
        except Exception:
            param = None
    if param is not None:
        try:
            el_id = param.AsElementId()
            lc_element = doc.GetElement(el_id)
            if lc_element is not None:
                names = split_load_classification_names(lc_element.Name)
                if names:
                    return names
        except Exception:
            pass
        try:
            val = param.AsValueString()
            names = split_load_classification_names(val)
            if names:
                return names
        except Exception:
            pass
    return ["Unclassified"]


def get_panel_name(circuit):
    param = get_param_value(circuit, get_bip("RBS_ELEC_CIRCUIT_PANEL_PARAM"))
    if param is not None:
        try:
            val = param.AsString()
            if val:
                return val
        except Exception:
            pass
    try:
        base_eq = circuit.BaseEquipment
        if base_eq is not None:
            return base_eq.Name
    except Exception:
        pass
    return "(Unassigned)"


_feeder_tag_source_counts = {
    "element_load_name": 0,
    "circuit_load_name": 0,
    "circuit_name": 0,
    "circuit_default_name": 0,
}


def get_feeder_tag(circuit):
    # "Load Name" is the descriptive text of what's actually connected
    # (e.g. "WASTE SEPARATOR", "DISHWASHER, HOOD TYPE, PASS-THRU") and is
    # far more useful in the report than the generic auto-numbered
    # "Circuit Name"/"Circuit Number". Revit's native panel schedules
    # source this from a "Load Name" parameter on the CONNECTED
    # ELEMENT(S) themselves (the fixture/equipment family instance) -
    # not from the circuit object - so that's checked first. Only if
    # nothing is set there does this fall back to Load Name at the
    # circuit level, then Circuit Name, then circuit.Name.
    try:
        elements = list(circuit.Elements)
        names = []
        for el in elements:
            try:
                p = el.LookupParameter("Load Name")
                if p is not None:
                    val = p.AsString()
                    if val:
                        names.append(val)
            except Exception:
                pass
        if names:
            _feeder_tag_source_counts["element_load_name"] += 1
            return ", ".join(sorted(set(names)))
    except Exception:
        pass

    param = get_param_value(circuit, get_bip("RBS_ELEC_CIRCUIT_LOAD_NAME_PARAM"))
    if param is None:
        param = get_param_value(circuit, get_bip("RBS_ELEC_CIRCUIT_LOAD_NAME"))
    if param is None:
        try:
            param = circuit.LookupParameter("Load Name")
        except Exception:
            param = None
    if param is not None:
        try:
            val = param.AsString()
            if val:
                _feeder_tag_source_counts["circuit_load_name"] += 1
                return val
        except Exception:
            pass

    param = get_param_value(circuit, get_bip("RBS_ELEC_CIRCUIT_NAME"))
    if param is not None:
        try:
            val = param.AsString()
            if val:
                _feeder_tag_source_counts["circuit_name"] += 1
                return val
        except Exception:
            pass
    _feeder_tag_source_counts["circuit_default_name"] += 1
    return circuit.Name if hasattr(circuit, "Name") else "Circuit"


def get_to_description(circuit):
    # Prefer each connected element's own "Load Name" parameter (the
    # same field that makes Feeder Tag descriptive, e.g. "WASTE
    # SEPARATOR" instead of a generic isolator type name) over its raw
    # family/type Name. Panels/distribution boards being fed by a
    # riser/feeder circuit normally won't have a Load Name set, so this
    # naturally falls back to the board's own .Name in that case -
    # which is exactly what's needed to keep the "To" value matching a
    # downstream panel's name for chain-hierarchy detection.
    try:
        elements = list(circuit.Elements)
        names = []
        for el in elements:
            label = None
            try:
                p = el.LookupParameter("Load Name")
                if p is not None:
                    val = p.AsString()
                    if val:
                        label = val
            except Exception:
                pass
            if not label:
                try:
                    label = el.Name
                except Exception:
                    pass
            if label:
                names.append(label)
        if names:
            return ", ".join(sorted(set(names)))
    except Exception:
        pass
    return ""


def get_apparent_load_kva(circuit):
    # Same situation as LoadClassification: ApparentLoad is a direct
    # ElectricalSystem property (internal units, VA) - try that first,
    # fall back to the parameter lookup for older/different API surfaces.
    try:
        return va_to_kva(circuit.ApparentLoad)
    except Exception:
        pass

    param = get_param_value(circuit, get_bip("RBS_ELEC_APPARENT_LOAD_PARAM"))
    if param is not None:
        try:
            return va_to_kva(param.AsDouble())
        except Exception:
            pass
    return 0.0


# --------------------------------------------------------------------------
# 1. Collect circuits
# --------------------------------------------------------------------------
all_circuits = (
    FilteredElementCollector(doc)
    .OfClass(ElectricalSystem)
    .ToElements()
)

if not all_circuits:
    forms.alert("No electrical circuits were found in this model.", exitscript=True)

output.print_md("Found **{}** electrical circuit(s) in the model.".format(len(all_circuits)))

_missing_bips = [
    name for name in (
        "RBS_ELEC_CIRCUIT_LOAD_CLASSIFICATION_PARAM",
        "RBS_ELEC_CIRCUIT_PANEL_PARAM",
        "RBS_ELEC_CIRCUIT_NAME",
        "RBS_ELEC_APPARENT_LOAD_PARAM",
        "RBS_ELEC_CIRCUIT_LOAD_NAME_PARAM",
        "RBS_ELEC_CIRCUIT_LOAD_NAME",
    )
    if get_bip(name) is None
]
if _missing_bips:
    output.print_md(
        "*Note: these BuiltInParameter names don't exist in this Revit "
        "version and were skipped in favor of fallbacks: {}*".format(
            ", ".join(_missing_bips)
        )
    )

# --------------------------------------------------------------------------
# 2. Build rows
# --------------------------------------------------------------------------
rows = []
skip_errors = []
excluded_final_panel_circuits = 0
excluded_final_panel_kva = 0.0
excluded_final_panel_names = set()

for c in all_circuits:
    try:
        panel = get_panel_name(c)
        tag = get_feeder_tag(c)
        to_desc = get_to_description(c)
        classes = get_load_classification_names(c)
        kva = get_apparent_load_kva(c)
    except Exception as ex:
        skip_errors.append("{}: {}".format(type(ex).__name__, ex))
        continue

    if is_excluded_final_panel(panel):
        # This circuit is a branch/final circuit originating from a
        # terminal board (PP/LP/ACP/CBS) - skip it entirely. The board
        # itself still shows up normally as a row wherever it's fed
        # from, since that's a different circuit (originating from the
        # upstream panel) and isn't touched by this check.
        excluded_final_panel_circuits += 1
        excluded_final_panel_kva += kva
        excluded_final_panel_names.add(panel)
        continue

    # If a circuit lists multiple classifications (typically a feeder
    # that reports every classification present on the board it feeds),
    # divide its apparent load evenly across each one instead of
    # crediting the full amount to just one column or duplicating it
    # under every column. Every classification actually used in the
    # model is kept at this stage - nothing is restricted to a fixed
    # list, since a hardcoded list can miss real, load-bearing
    # categories (e.g. "FI - FIRE (NORMAL)" was excluded that way and
    # turned out to carry over 3,400 kVA).
    names = classes if classes else ["Unclassified"]
    split_kva = kva / float(len(names))
    for lc in names:
        rows.append({
            "panel": panel,
            "tag": tag,
            "to": to_desc,
            "load_class": lc,
            "kva": split_kva,
        })

if rows:
    _sample = rows[0]
    output.print_md(
        "*Sample row check — first circuit `{}`: load classification = "
        "`{}`, apparent load = `{}` kVA*".format(
            _sample["tag"], _sample["load_class"], _sample["kva"]
        )
    )

output.print_md(
    "*Feeder Tag source breakdown across all circuits: "
    "**{}** from connected element's Load Name, "
    "**{}** from circuit-level Load Name, "
    "**{}** from Circuit Name, "
    "**{}** fell back to the circuit's default/auto name "
    "(e.g. equipment type text) because no Load Name was set anywhere.*".format(
        _feeder_tag_source_counts["element_load_name"],
        _feeder_tag_source_counts["circuit_load_name"],
        _feeder_tag_source_counts["circuit_name"],
        _feeder_tag_source_counts["circuit_default_name"],
    )
)

if excluded_final_panel_circuits:
    output.print_md(
        "*Excluded **{}** branch circuit(s) totaling **{:.2f} kVA** "
        "originating from {} terminal panel(s) ({}) - only PP/LP/ACP/CBS "
        "themselves are shown as rows, their own outgoings are omitted.*".format(
            excluded_final_panel_circuits,
            excluded_final_panel_kva,
            len(excluded_final_panel_names),
            ", ".join(sorted(excluded_final_panel_names)[:8])
            + (", ..." if len(excluded_final_panel_names) > 8 else ""),
        )
    )

if not rows:
    # Every circuit that was found still ended up producing zero usable
    # rows. Surface the real reason instead of silently exporting an
    # empty "0 circuits across 0 panels" workbook.
    if skip_errors:
        detail = "First error ({} total):\n{}".format(len(skip_errors), skip_errors[0])
    else:
        detail = "No specific error was captured."
    forms.alert(
        "Found {} electrical circuit(s) in the model, but none of them "
        "could be processed.\n\n{}".format(len(all_circuits), detail),
        title="No usable circuit data",
        exitscript=True,
    )

# Only keep classifications that actually carry load anywhere in the
# model. A classification whose total across every circuit is exactly
# zero (e.g. genuine unused "Spare" positions) is dropped as a column -
# but anything with real, non-zero load is kept, regardless of whether
# it appears on a "standard" list.
_class_totals = {}
for r in rows:
    _class_totals[r["load_class"]] = _class_totals.get(r["load_class"], 0.0) + r["kva"]

_ZERO_EPSILON = 0.005
load_classes = sorted(lc for lc, total in _class_totals.items() if total > _ZERO_EPSILON)
_dropped_zero_classes = sorted(lc for lc, total in _class_totals.items() if total <= _ZERO_EPSILON)
_load_classes_set = set(load_classes)

if _dropped_zero_classes:
    output.print_md(
        "*Note: these classifications had zero total load across the "
        "whole model and were dropped as columns: {}*".format(
            ", ".join(_dropped_zero_classes)
        )
    )

output.print_md(
    "*Using **{}** load classification column(s): {}*".format(
        len(load_classes), ", ".join(load_classes)
    )
)

# --------------------------------------------------------------------------
# 3. Pivot into: panel -> feeder tag -> {load_class: kva}
# --------------------------------------------------------------------------
panels = OrderedDict()
for r in rows:
    panel_bucket = panels.setdefault(r["panel"], OrderedDict())
    feeder_bucket = panel_bucket.setdefault(r["tag"], {
        "to": r["to"],
        "loads": dict((lc, 0.0) for lc in load_classes),
    })
    if r["load_class"] in _load_classes_set:
        feeder_bucket["loads"][r["load_class"]] += r["kva"]
    if r["to"] and not feeder_bucket["to"]:
        feeder_bucket["to"] = r["to"]

# --------------------------------------------------------------------------
# 3b. Build the distribution chain: main panel(s) -> sub-boards -> ... -> end
#
# Each feeder's "To" field is the board/equipment it terminates at. When
# that value happens to match another panel's own name (i.e. the feeder
# actually lands on another distribution board rather than a terminal
# device like an isolator), that's a real upstream->downstream link. A
# panel that is never anyone else's "To" is a main/incoming panel (a
# root of the chain); everything else nests under whichever panel feeds
# it, as deep as the model's feeders actually go.
# --------------------------------------------------------------------------
fed_by = {}  # child panel name -> parent panel name (first match wins)
for parent_name, feeders in panels.items():
    for tag, data in feeders.items():
        to_name = data["to"]
        if to_name and to_name in panels and to_name != parent_name:
            fed_by.setdefault(to_name, parent_name)

children = OrderedDict()
for child, parent in fed_by.items():
    children.setdefault(parent, []).append(child)

all_panel_names = list(panels.keys())
root_panels = [p for p in all_panel_names if p not in fed_by]

ordered_panels = []  # list of (panel_name, depth)
_visited = set()


def _visit(name, depth):
    if name in _visited:
        return
    _visited.add(name)
    ordered_panels.append((name, depth))
    for child in children.get(name, []):
        _visit(child, depth + 1)


for root in root_panels:
    _visit(root, 0)

# Any panel never reached from a root (e.g. genuinely isolated, or part
# of a feedback loop in the data) is still included so nothing silently
# disappears from the export - just appended at the top level.
for name in all_panel_names:
    if name not in _visited:
        _visit(name, 0)

output.print_md(
    "*Distribution chain: **{}** main panel(s) detected ({}), {} sub-board "
    "link(s) resolved from feeder \"To\" fields.*".format(
        len(root_panels), ", ".join(root_panels) if root_panels else "none", len(fed_by)
    )
)

# --------------------------------------------------------------------------
# 3c. Build the sheet matrix - pure Python, no Excel/COM involved yet, so
# it can be previewed before anything is written or even asked to save.
# --------------------------------------------------------------------------
headers = ["Feeder Tag", "From", "To"] + load_classes + ["Total (kVA)"]
num_cols = len(headers)
root_panels_set = set(root_panels)

# --- Pass 1: build the per-panel breakdown body. Each panel gets a
# full-width "<Panel> OUTGOINGS" banner row, then one real data row
# per feeder with Feeder Tag / From / To all populated explicitly
# (From = this panel, so the source is never ambiguous or implied
# by indentation alone). Only root/main panels get a Subtotal row.
body_rows = []
banner_rows = []       # row numbers (relative to body_rows, 1-based)
subtotal_rows = []     # relative row numbers
root_panel_totals = OrderedDict()  # panel_name -> {lc: total}, root order

for panel_name, depth in ordered_panels:
    feeders = panels[panel_name]

    # full-width "<Panel> OUTGOINGS" banner row
    banner_row = [""] * num_cols
    banner_row[0] = "{} OUTGOINGS".format(panel_name)
    body_rows.append(banner_row)
    banner_rows.append(len(body_rows))

    panel_total_by_lc = dict((lc, 0.0) for lc in load_classes)

    for tag, data in feeders.items():
        row = [""] * num_cols
        row[0] = tag           # Feeder Tag
        row[1] = panel_name    # From
        row[2] = data["to"]    # To
        row_total = 0.0
        for ci, lc in enumerate(load_classes):
            val = round(data["loads"].get(lc, 0.0), 2)
            row[3 + ci] = val
            panel_total_by_lc[lc] += val
            row_total += val
        row[3 + len(load_classes)] = round(row_total, 2)
        body_rows.append(row)

    is_root = panel_name in root_panels_set
    if is_root:
        root_panel_totals[panel_name] = panel_total_by_lc

        sub_row = [""] * num_cols
        sub_row[0] = "Subtotal"
        subtotal = 0.0
        for ci, lc in enumerate(load_classes):
            val = round(panel_total_by_lc[lc], 2)
            sub_row[3 + ci] = val
            subtotal += val
        sub_row[3 + len(load_classes)] = round(subtotal, 2)
        body_rows.append(sub_row)
        subtotal_rows.append(len(body_rows))

    body_rows.append([""] * num_cols)  # blank spacer row

# --- Pass 2: build the main-panel summary block that goes at the
# very top of the sheet, one row per main panel, in the same order
# they were detected as roots. Kept in the same Feeder Tag/From/To
# shape as the detail rows below (From left blank - the true
# incoming source above these main panels isn't present as circuit
# data in the model) so the whole sheet reads consistently.
summary_rows = []
for panel_name in root_panels:
    totals = root_panel_totals.get(panel_name, {})
    row = [""] * num_cols
    row[2] = panel_name  # To
    row_total = 0.0
    for ci, lc in enumerate(load_classes):
        val = round(totals.get(lc, 0.0), 2)
        row[3 + ci] = val
        row_total += val
    row[3 + len(load_classes)] = round(row_total, 2)
    summary_rows.append(row)

# --- Assemble the full sheet: header, "MAIN PANEL SUMMARY" banner,
# summary rows, spacer, then the full detailed chain.
matrix = [list(headers)]
summary_banner_row = None
if summary_rows:
    banner = [""] * num_cols
    banner[0] = "MAIN PANEL SUMMARY"
    matrix.append(banner)
    summary_banner_row = len(matrix)
matrix.extend(summary_rows)
summary_row_numbers = list(range(
    (summary_banner_row + 1) if summary_banner_row else 2,
    (summary_banner_row + 1 if summary_banner_row else 2) + len(summary_rows)
))
matrix.append([""] * num_cols)  # spacer after the summary block

offset = len(matrix)  # rows already used; body rows start right after
matrix.extend(body_rows)
banner_rows = [offset + r for r in banner_rows]
subtotal_rows = [offset + r for r in subtotal_rows]

total_rows = len(matrix)

# --------------------------------------------------------------------------
# 3d. Preview before doing anything with Excel - main panel summary in
# full, plus a sample of the detailed chain, rendered right in the
# pyRevit output window. Nothing is saved or opened until confirmed.
# --------------------------------------------------------------------------
if summary_rows:
    output.print_md("### Preview — Main Panel Summary")
    safetable.print_table(output, 
        table_data=[headers] + summary_rows,
        title="Main Panel Summary",
    )

_PREVIEW_SAMPLE_ROWS = 40
output.print_md(
    "### Preview — Detailed Chain (first {} of {} rows)".format(
        min(_PREVIEW_SAMPLE_ROWS, len(body_rows)), len(body_rows)
    )
)
safetable.print_table(output, 
    table_data=[headers] + body_rows[:_PREVIEW_SAMPLE_ROWS],
    title="Detailed Chain (sample)",
)
if len(body_rows) > _PREVIEW_SAMPLE_ROWS:
    output.print_md(
        "*...and **{}** more row(s) not shown here - the full detail will "
        "be in the exported Excel file.*".format(len(body_rows) - _PREVIEW_SAMPLE_ROWS)
    )

proceed_choice = forms.CommandSwitchWindow.show(
    ["Export to Excel", "Cancel"],
    message="Review the preview above. Proceed with export?",
)
if proceed_choice != "Export to Excel":
    script.exit()

# --------------------------------------------------------------------------
# 4. Choose Excel format, then ask where to save
# --------------------------------------------------------------------------

# label -> (file extension, xlFileFormat SaveAs code)
EXCEL_FORMAT_OPTIONS = OrderedDict([
    ("Excel Workbook (.xlsx) - recommended", ("xlsx", 51)),   # xlOpenXMLWorkbook
    ("Excel Macro-Enabled Workbook (.xlsm)", ("xlsm", 52)),   # xlOpenXMLWorkbookMacroEnabled
    ("Excel 97-2003 Workbook (.xls) - legacy", ("xls", 56)),  # xlExcel8
])

selected_format_label = forms.CommandSwitchWindow.show(
    list(EXCEL_FORMAT_OPTIONS.keys()),
    message="Choose the Excel format to export Load Breakdown as:",
)
if not selected_format_label:
    script.exit()

file_ext, xl_save_format = EXCEL_FORMAT_OPTIONS[selected_format_label]

save_path = forms.save_file(file_ext=file_ext, default_name="Load Breakdown.{}".format(file_ext))
if not save_path:
    script.exit()

open_after_export = forms.CommandSwitchWindow.show(
    ["Yes", "No"],
    message="Open the workbook automatically once it's exported?",
) == "Yes"

# --------------------------------------------------------------------------
# 5. Write Excel via COM automation (IronPython late-bound COM - no PIA
#    reference or pip packages needed, just Excel installed).
#
#    NOTE: In this environment IronPython's dynamic COM binder cannot
#    resolve ANY member (get, set, or method call) on the generic
#    System.__ComObject returned by Activator.CreateInstance - not just
#    Quit(), but even the very first "excel.Visible = False". So every
#    single interaction with the Excel object model below goes through
#    .NET reflection (Type.InvokeMember) via the com_get/com_set/com_call
#    helpers instead of normal dot notation.
# --------------------------------------------------------------------------

_GET_FLAGS = BindingFlags.GetProperty | BindingFlags.InvokeMethod
_SET_FLAGS = BindingFlags.SetProperty
_CALL_FLAGS = BindingFlags.InvokeMethod | BindingFlags.GetProperty


def com_get(obj, name):
    """Read a COM property, e.g. workbook.ActiveSheet."""
    return obj.GetType().InvokeMember(name, _GET_FLAGS, None, obj, Array[object]([]))


def com_set(obj, name, value):
    """Set a COM property, e.g. sheet.Name = 'x'."""
    obj.GetType().InvokeMember(name, _SET_FLAGS, None, obj, Array[object]([value]))


def com_call(obj, name, *args):
    """Call a COM method or parameterized/indexed property,
    e.g. sheet.Cells(1, 1), workbook.SaveAs(path, 51), excel.Quit()."""
    return obj.GetType().InvokeMember(name, _CALL_FLAGS, None, obj, Array[object](list(args)))


def build_com_array(matrix):
    """
    Convert a Python list-of-lists into a rectangular System.Object[,]
    2D array suitable for a single bulk Range.Value2 assignment.

    Writing one row/one cell at a time via reflection (com_set in a
    loop) is the dominant cost at scale - with thousands of circuits
    that's tens of thousands of individual COM calls. A single Value2
    assignment covering the whole block is dramatically faster because
    it's one COM call total, regardless of how many rows/cols it holds.
    """
    n_rows = len(matrix)
    n_cols = len(matrix[0]) if n_rows else 0
    arr = System.Array.CreateInstance(System.Object, n_rows, n_cols)
    for i in range(n_rows):
        row = matrix[i]
        for j in range(n_cols):
            v = row[j]
            arr[i, j] = v if v is not None else ""
    return arr


excel = None
workbook = None
try:
    excel_type = Type.GetTypeFromProgID("Excel.Application")
    excel = Activator.CreateInstance(excel_type)
    com_set(excel, "Visible", False)
    com_set(excel, "DisplayAlerts", False)

    workbooks = com_get(excel, "Workbooks")
    workbook = com_call(workbooks, "Add")
    sheet = com_get(workbook, "ActiveSheet")
    com_set(sheet, "Name", "Load Breakdown")

    # --- Single bulk write of every value in one COM call ---------------
    top_left = com_call(sheet, "Cells", 1, 1)
    bottom_right = com_call(sheet, "Cells", total_rows, num_cols)
    data_range = com_call(sheet, "Range", top_left, bottom_right)
    com_set(data_range, "Value2", build_com_array(matrix))

    # --- Formatting: proportional to panel count, not circuit count -----
    GREEN_FILL = 0x50D092  # bright green banner (BGR), matches reference layout

    header_range = com_call(
        sheet, "Range", com_call(sheet, "Cells", 1, 1), com_call(sheet, "Cells", 1, num_cols)
    )
    com_set(com_get(header_range, "Font"), "Bold", True)
    com_set(com_get(header_range, "Interior"), "Color", GREEN_FILL)
    com_set(header_range, "HorizontalAlignment", -4108)  # xlCenter

    if summary_banner_row:
        rng = com_call(
            sheet, "Range",
            com_call(sheet, "Cells", summary_banner_row, 1),
            com_call(sheet, "Cells", summary_banner_row, num_cols),
        )
        com_set(com_get(rng, "Font"), "Bold", True)
        com_set(com_get(rng, "Interior"), "Color", GREEN_FILL)

    # Main panel summary rows - bold, distinct shading so they read as
    # the "executive summary" before the detailed chain below.
    for r in summary_row_numbers:
        rng = com_call(
            sheet, "Range", com_call(sheet, "Cells", r, 1), com_call(sheet, "Cells", r, num_cols)
        )
        com_set(com_get(rng, "Font"), "Bold", True)
        com_set(com_get(rng, "Interior"), "Color", 0xDDCBA6)  # gold/tan accent (BGR)

    # "<Panel> OUTGOINGS" banner rows - same bright green as the column
    # header, so every section start reads the same way at a glance.
    for r in banner_rows:
        rng = com_call(
            sheet, "Range", com_call(sheet, "Cells", r, 1), com_call(sheet, "Cells", r, num_cols)
        )
        com_set(com_get(rng, "Font"), "Bold", True)
        com_set(com_get(rng, "Interior"), "Color", GREEN_FILL)

    for r in subtotal_rows:
        rng = com_call(
            sheet, "Range", com_call(sheet, "Cells", r, 1), com_call(sheet, "Cells", r, num_cols)
        )
        com_set(com_get(rng, "Font"), "Bold", True)

    com_call(com_get(sheet, "Columns"), "AutoFit")
    active_window = com_get(com_get(sheet, "Application"), "ActiveWindow")
    # Freeze through the header row and the whole main-panel summary
    # block (plus its spacer row) so both stay visible while scrolling
    # through the detailed chain below.
    freeze_row = offset
    com_set(active_window, "SplitRow", freeze_row)
    com_set(active_window, "FreezePanes", True)

    # Save in whichever format the user chose in the earlier prompt.
    try:
        com_call(workbook, "SaveAs", save_path, xl_save_format)
    except Exception:
        # SaveAs typically fails here because the target file is locked -
        # most commonly because it's still open in the user's own Excel
        # window from viewing a previous export. Rather than losing the
        # whole export, retry once with a timestamped filename.
        base, ext = os.path.splitext(save_path)
        alt_path = "{}_{}{}".format(base, time.strftime("%Y%m%d-%H%M%S"), ext)
        com_call(workbook, "SaveAs", alt_path, xl_save_format)
        save_path = alt_path

finally:
    # Each cleanup step is isolated in its own try/except so that a
    # failure in one can't skip the remaining steps and leave an
    # orphaned EXCEL.EXE process running in the background.
    if workbook is not None:
        try:
            com_call(workbook, "Close", False)
        except Exception:
            pass
    if excel is not None:
        try:
            com_call(excel, "Quit")
        except Exception:
            pass
        try:
            Marshal.ReleaseComObject(excel)
        except Exception:
            pass

if open_after_export:
    try:
        os.startfile(save_path)
    except Exception:
        pass

forms.alert(
    "Load Breakdown exported.\n\n"
    "File: {}\n"
    "Format: {}\n\n"
    "{} circuits across {} panels ({} main panel(s)).".format(
        save_path,
        selected_format_label,
        len(rows),
        len(panels),
        len(root_panels),
    ),
    title="Load Breakdown - Done",
)
output.print_md("**Exported:** `{}` ({})".format(save_path, selected_format_label))
