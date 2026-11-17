# -*- coding: utf-8 -*-
__persistentengine__ = True

import clr
import os
import shutil
import time
import subprocess
import xml.etree.ElementTree as ET
import System

clr.AddReference("System.Windows.Forms")
clr.AddReference("System.Drawing")
clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import *
import System.Windows.Forms as WF
import System.Drawing as SD
from System.Collections.Generic import List
from dwg_ole_binder import process_dwg_batch, shutdown_autocad

__author__ = 'BHEEM'
__doc__ = 'Export PDF & DWG'
__title__ = 'Exporter'

uidoc = __revit__.ActiveUIDocument
doc = uidoc.Document

try:
    PDFExportOptions
    HAS_NATIVE_PDF = True
except:
    HAS_NATIVE_PDF = False

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
# Default settings location — can be overridden by user browsing a folder
_DEFAULT_SETTINGS_DIR   = os.path.join(APPDATA, "RevitExportTool")
_LOCATION_POINTER_FILE  = os.path.join(_DEFAULT_SETTINGS_DIR, "settings_location.txt")


def _resolve_settings_dir():
    """Return the user-chosen settings folder, or the default."""
    try:
        if os.path.exists(_LOCATION_POINTER_FILE):
            with open(_LOCATION_POINTER_FILE, "r") as _f:
                chosen = _f.read().strip()
            if chosen and os.path.isdir(chosen):
                return chosen
    except:
        pass
    return _DEFAULT_SETTINGS_DIR


# These are resolved dynamically so changing location mid-session takes effect
def _get_settings_dir():      return _resolve_settings_dir()
def _get_settings_file():     return os.path.join(_get_settings_dir(), "export_settings.xml")
def _get_naming_preset_dir(): return os.path.join(_get_settings_dir(), "naming_presets")
def _get_export_preset_dir(): return os.path.join(_get_settings_dir(), "export_presets")

# Keep module-level names for backward compat — resolved at call time
SETTINGS_DIR      = _get_settings_dir()
SETTINGS_FILE     = _get_settings_file()
NAMING_PRESET_DIR = _get_naming_preset_dir()
EXPORT_PRESET_DIR = _get_export_preset_dir()


def ensure_dir(path):
    if path and not os.path.exists(path):
        os.makedirs(path)


def ensure_settings_folder():
    ensure_dir(_get_settings_dir())


def default_settings():
    return {
        "version": "1.0",   # bump this if default_settings keys change
        "parameter_type": "Sheets",
        "file_name": "",
        "combine": False,
        "zoom_type": "FitToPage",
        "zoom_percentage": 100,
        "color_depth": "Color",
        "raster_quality": "High",
        "paper_size": "<Use Sheet Size>",
        "paper_placement": "Center",
        "orientation": "Auto",
        "dwg_setup": "<In-session>",
        "last_folder": "",
        "create_output_mode": "Vector Processing",
        "rows": [
            {"name": "Sheet Number", "prefix": "", "sample": "DR-001", "suffix": "", "separator": "-"},
            {"name": "Sheet Name",   "prefix": "", "sample": "Floor Plan", "suffix": "", "separator": ""}
        ]
    }


def load_settings():
    try:
        if os.path.exists(_get_settings_file()):
            data = xml_load_file(_get_settings_file())
            if isinstance(data, dict):
                base = default_settings()
                saved_ver   = safe_str(data.get("version", ""))
                current_ver = safe_str(base.get("version", "1.0"))
                if saved_ver and saved_ver != current_ver:
                    # Version mismatch — use defaults, keep saved folder
                    base["last_folder"] = safe_str(data.get("last_folder", ""))
                    return base
                base.update(data)
                return base
    except:
        pass
    return default_settings()


def save_settings(data):
    try:
        ensure_settings_folder()
        xml_save_file(_get_settings_file(), data, root_name='settings')
    except:
        pass


def safe_str(v):
    if v is None:
        return ""
    try:
        return str(v)
    except:
        return ""


def get_id_value(eid):
    try:
        return eid.Value
    except:
        try:
            return eid.IntegerValue
        except:
            try:
                return int(str(eid))
            except:
                return None


def safe_filename(name):
    name = safe_str(name)
    for c in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']:
        name = name.replace(c, "_")
    return name.strip()


def is_file_locked(path):
    """Return True if `path` exists and appears to be locked by another
    program (e.g. open in a PDF/DWG viewer), False otherwise — including
    when the file doesn't exist yet, since there's nothing to conflict with.

    Revit's PDF/DWG export can fail with no catchable .NET exception when
    the destination file is locked — the export just quietly does nothing.
    Checking up front lets us warn the user instead of a silent no-op."""
    if not path or not os.path.exists(path):
        return False
    try:
        f = open(path, 'r+b')  # exclusive-enough to fail if another
        f.close()              # process holds a write lock on it
        return False
    except (IOError, OSError):
        return True


def safe_separator(sep):
    for c in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']:
        sep = sep.replace(c, "")
    return sep


def unique_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def get_selected_exportables():
    selected = []
    for eid in uidoc.Selection.GetElementIds():
        el = doc.GetElement(eid)
        if isinstance(el, ViewSheet):
            selected.append(el)
        elif isinstance(el, View):
            try:
                if not el.IsTemplate:
                    selected.append(el)
            except:
                pass
    return selected


def get_all_sheets():
    try:
        return list(FilteredElementCollector(doc).OfClass(ViewSheet).ToElements())
    except:
        return []


def is_printable_view(v):
    try:
        if v.IsTemplate:
            return False
    except:
        pass
    try:
        vt = v.ViewType
        excluded = [
            ViewType.ProjectBrowser,
            ViewType.SystemBrowser,
            ViewType.Undefined,
            ViewType.DrawingSheet,
            ViewType.Internal,
            ViewType.Schedule,
            ViewType.Legend,        # excluded: Legend views
            #ViewType.DraftingView,  # excluded: Drafting views
        ]
        if vt in excluded:
            return False
    except:
        pass
    return True


def get_all_printable_views():
    views = []
    try:
        for v in FilteredElementCollector(doc).OfClass(View).ToElements():
            if is_printable_view(v):
                views.append(v)
    except:
        pass
    return views


def get_params(category):
    params = []
    try:
        if category == "Views":
            view = doc.ActiveView
            if view and not getattr(view, "IsTemplate", False):
                params = ["View Name"] + [p.Definition.Name for p in view.Parameters]
            else:
                v = next(iter(get_all_printable_views()), None)
                if v:
                    params = ["View Name"] + [p.Definition.Name for p in v.Parameters]
        elif category == "Project Information":
            pi = doc.ProjectInformation
            if pi:
                params = ["Project Name"] + [p.Definition.Name for p in pi.Parameters]
        else:
            first = next(iter(get_all_sheets()), None)
            if first:
                params = ["Sheet Number", "Sheet Name"] + [p.Definition.Name for p in first.Parameters]
    except:
        pass
    return unique_keep_order(params)


def get_element_value(element, param_name):
    try:
        if element is None:
            return ""

        if param_name == "Sheet Number" and hasattr(element, "SheetNumber"):
            return safe_str(element.SheetNumber)
        if param_name in ("Sheet Name", "View Name", "Project Name") and hasattr(element, "Name"):
            return safe_str(element.Name)

        p = element.LookupParameter(param_name)
        if p:
            try:
                if p.StorageType == StorageType.String:
                    v = p.AsString()
                    if v:
                        return v
                else:
                    v = p.AsValueString()
                    if v:
                        return v
                    v = p.AsString()
                    if v:
                        return v
            except:
                try:
                    v = p.AsString() or p.AsValueString()
                    if v:
                        return v
                except:
                    pass
    except:
        pass
    return ""


def get_export_source(element, parameter_type):
    try:
        if parameter_type == "Views":
            if isinstance(element, ViewSheet):
                viewports = FilteredElementCollector(doc, element.Id).OfClass(Viewport).ToElements()
                if viewports:
                    v = doc.GetElement(viewports[0].ViewId)
                    if v:
                        return v
            return element
        elif parameter_type == "Project Information":
            return doc.ProjectInformation
        else:
            return element
    except:
        pass
    return element


def build_filename(element, rows, parameter_type="Sheets"):
    source = get_export_source(element, parameter_type)
    segments = []
    for row in (rows or []):
        name = safe_str(row.get("name", ""))
        prefix = safe_str(row.get("prefix", ""))
        suffix = safe_str(row.get("suffix", ""))
        sep = safe_separator(safe_str(row.get("separator", "")))
        sample = safe_str(row.get("sample", ""))

        val = get_element_value(source, name)
        if not val:
            val = sample

        part = safe_filename(prefix + val + suffix)
        segments.append((part, sep))

    result = ""
    for i, (part, sep) in enumerate(segments):
        result += part
        if i < len(segments) - 1:
            result += sep
    return result.strip()


def get_dwg_dxf_export_setups():
    setups = ["<In-session>"]
    try:
        for setup in FilteredElementCollector(doc).OfClass(ExportDWGSettings).ToElements():
            nm = safe_str(setup.Name)
            if nm and nm not in setups:
                setups.append(nm)
    except:
        pass
    return setups


def get_selected_dwg_options(setup_name):
    try:
        if not setup_name or setup_name == "<In-session>":
            return DWGExportOptions()
        # Collect all setups, find by name
        all_setups = list(FilteredElementCollector(doc).OfClass(ExportDWGSettings).ToElements())
        if not any(safe_str(s.Name) == setup_name for s in all_setups):
            return DWGExportOptions()  # setup not found — use defaults
        for setup in FilteredElementCollector(doc).OfClass(ExportDWGSettings).ToElements():
            if safe_str(setup.Name) == setup_name:
                try:
                    return setup.GetDWGExportOptions()
                except:
                    break
    except:
        pass
    return DWGExportOptions()


def get_paper_sizes():
    return [
        "<Use Sheet Size>",
        "ISO A0 : 841 x 1189 mm", "ISO A1 : 594 x 841 mm",
        "ISO A2 : 420 x 594 mm",  "ISO A3 : 297 x 420 mm",
        "ISO A4 : 210 x 297 mm",  "ISO A5 : 148 x 210 mm",
        "ANSI A : 8.5 x 11 in",   "ANSI B : 11 x 17 in",
        "ANSI C : 17 x 22 in",    "ANSI D : 22 x 34 in",
        "ANSI E : 34 x 44 in",    "ARCH C : 18 x 24 in",
        "ARCH D : 24 x 36 in",    "ARCH E : 36 x 48 in",
        "Letter : 8.5 x 11 in",   "Legal : 8.5 x 14 in",
        "Ledger : 11 x 17 in",    "Tabloid : 11 x 17 in",
    ]


COLOR_ACCENT    = SD.Color.FromArgb(0, 120, 215)
COLOR_WHITE     = SD.Color.White
COLOR_PANEL_BG  = SD.Color.FromArgb(245, 245, 248)
COLOR_HEADER    = SD.Color.FromArgb(30, 30, 40)
COLOR_CANCEL    = SD.Color.FromArgb(220, 220, 220)
COLOR_CANCEL_FG = SD.Color.FromArgb(60, 60, 60)

FONT_TITLE  = SD.Font("Segoe UI", 11, SD.FontStyle.Bold)
FONT_BOLD   = SD.Font("Segoe UI", 9,  SD.FontStyle.Bold)
FONT_NORMAL = SD.Font("Segoe UI", 9,  SD.FontStyle.Regular)
FONT_SMALL  = SD.Font("Segoe UI", 8,  SD.FontStyle.Regular)
FONT_MEDIUM  = SD.Font("Segoe UI", 11,  SD.FontStyle.Bold)

TOP_LEFT         = WF.AnchorStyles.Top  | WF.AnchorStyles.Left
TOP_RIGHT        = WF.AnchorStyles.Top  | WF.AnchorStyles.Right
TOP_LEFT_RIGHT   = WF.AnchorStyles.Top  | WF.AnchorStyles.Left | WF.AnchorStyles.Right
BOTTOM_RIGHT     = WF.AnchorStyles.Bottom | WF.AnchorStyles.Right
ALL_SIDES        = (WF.AnchorStyles.Top | WF.AnchorStyles.Bottom |
                    WF.AnchorStyles.Left | WF.AnchorStyles.Right)


def make_primary_button(text, w=110, h=34):
    btn = WF.Button()
    btn.Text = text
    btn.Size = SD.Size(w, h)
    btn.BackColor = COLOR_ACCENT
    btn.ForeColor = COLOR_WHITE
    btn.FlatStyle = WF.FlatStyle.Flat
    btn.FlatAppearance.BorderSize = 0
    btn.Font = FONT_BOLD
    return btn


def make_cancel_button(text, w=100, h=34):
    btn = WF.Button()
    btn.Text = text
    btn.Size = SD.Size(w, h)
    btn.BackColor = COLOR_CANCEL
    btn.ForeColor = COLOR_CANCEL_FG
    btn.FlatStyle = WF.FlatStyle.Flat
    btn.FlatAppearance.BorderSize = 0
    btn.Font = FONT_NORMAL
    return btn


def _xml_bool_text(v):
    return "true" if bool(v) else "false"


def _safe_xml_tag(name):
    name = safe_str(name).strip()
    if not name:
        return "item"
    cleaned = []
    for ch in name:
        if ch.isalnum() or ch in ['_', '-']:
            cleaned.append(ch)
        else:
            cleaned.append('_')
    if cleaned and cleaned[0].isdigit():
        cleaned.insert(0, '_')
    return ''.join(cleaned) or 'item'


def _value_to_xml(parent, key, value):
    tag = _safe_xml_tag(key)
    node = ET.SubElement(parent, tag)

    if isinstance(value, dict):
        node.set('type', 'dict')
        for k, v in value.items():
            _value_to_xml(node, k, v)
    elif isinstance(value, list):
        node.set('type', 'list')
        for item in value:
            item_node = ET.SubElement(node, 'item')
            if isinstance(item, dict):
                item_node.set('type', 'dict')
                for k, v in item.items():
                    _value_to_xml(item_node, k, v)
            elif isinstance(item, list):
                item_node.set('type', 'list')
                for sub in item:
                    _value_to_xml(item_node, 'item', sub)
            elif isinstance(item, bool):
                item_node.set('type', 'bool')
                item_node.text = _xml_bool_text(item)
            elif item is None:
                item_node.set('type', 'none')
                item_node.text = ''
            elif isinstance(item, (int, float)):
                item_node.set('type', 'number')
                item_node.text = safe_str(item)
            else:
                item_node.set('type', 'str')
                item_node.text = safe_str(item)
    elif isinstance(value, bool):
        node.set('type', 'bool')
        node.text = _xml_bool_text(value)
    elif value is None:
        node.set('type', 'none')
        node.text = ''
    elif isinstance(value, (int, float)):
        node.set('type', 'number')
        node.text = safe_str(value)
    else:
        node.set('type', 'str')
        node.text = safe_str(value)
    return node


def dict_to_xml_file(path, root_name, data):
    ensure_settings_folder()
    root = ET.Element(root_name)
    if isinstance(data, dict):
        for k, v in data.items():
            _value_to_xml(root, k, v)
    else:
        _value_to_xml(root, 'value', data)
    tree = ET.ElementTree(root)
    try:
        tree.write(path, encoding='utf-8', xml_declaration=True)
    except TypeError:
        tree.write(path)


def _xml_text_value(node, kind=None):
    txt = node.text or ''
    if kind == 'bool':
        return txt.strip().lower() in ('1', 'true', 'yes', 'on')
    if kind == 'number':
        txt = txt.strip()
        if txt == '':
            return ''
        try:
            if '.' in txt:
                return float(txt)
            return int(txt)
        except:
            return txt
    if kind == 'none':
        return None
    return txt


def _xml_node_to_value(node):
    kind = node.get('type', 'str')
    children = list(node)

    if kind == 'dict' or (children and kind not in ('list',) and node.tag != 'item'):
        result = {}
        for child in children:
            key = child.tag
            value = _xml_node_to_value(child)
            if key in result:
                existing = result[key]
                if not isinstance(existing, list):
                    result[key] = [existing]
                result[key].append(value)
            else:
                result[key] = value
        return result

    if kind == 'list':
        return [_xml_node_to_value(child) for child in children]

    if kind == 'dict' and not children:
        return {}

    return _xml_text_value(node, kind)


def xml_load_file(path):
    tree = ET.parse(path)
    root = tree.getroot()
    data = {}
    for child in list(root):
        key = child.tag
        value = _xml_node_to_value(child)
        if key in data:
            existing = data[key]
            if not isinstance(existing, list):
                data[key] = [existing]
            data[key].append(value)
        else:
            data[key] = value
    return data


def xml_save_file(path, data, root_name='preset'):
    ensure_settings_folder()
    root = ET.Element(root_name)
    if isinstance(data, dict):
        for k, v in data.items():
            _value_to_xml(root, k, v)
    else:
        _value_to_xml(root, 'value', data)
    tree = ET.ElementTree(root)
    try:
        tree.write(path, encoding='utf-8', xml_declaration=True)
    except TypeError:
        tree.write(path)


def pick_open_xml(title, folder, owner=None):
    dlg = WF.OpenFileDialog()
    dlg.Title = title
    dlg.Filter = "XML files (*.xml)|*.xml|All files (*.*)|*.*"
    if folder and os.path.exists(folder):
        dlg.InitialDirectory = folder
    try:
        result = dlg.ShowDialog(owner) if owner is not None else dlg.ShowDialog()
    except:
        result = dlg.ShowDialog()
    if result == WF.DialogResult.OK:
        return dlg.FileName
    return None


def pick_save_xml(title, folder, default_name, owner=None):
    dlg = WF.SaveFileDialog()
    dlg.Title = title
    dlg.Filter = "XML files (*.xml)|*.xml|All files (*.*)|*.*"
    dlg.FileName = default_name
    if folder and os.path.exists(folder):
        dlg.InitialDirectory = folder
    try:
        result = dlg.ShowDialog(owner) if owner is not None else dlg.ShowDialog()
    except:
        result = dlg.ShowDialog()
    if result == WF.DialogResult.OK:
        return dlg.FileName
    return None


_pending_back_timer = None  # keeps the "Back" deferred-navigation Timer
                            # alive between when it's started and when it
                            # fires — see _show_export_done_dialog

def _show_export_done_dialog(folder_path, errors=None, total=0, items=None):
    """Post-export dialog: shows success count, failures list, and
    Open Folder / Back / Close buttons.

    items: the sheets/views that were selected for this export — passed
    through so "Back" can reopen the selector pre-checked with the same
    items, then reopen MainExportDialog with the new selection."""
    errors = errors or []
    ok_count = total - len(errors)

    try:
        has_errors = len(errors) > 0
        dlg_h = 220 if has_errors else 165
        dlg = WF.Form()
        dlg.Text = "Export Complete"
        dlg.Size = SD.Size(420, dlg_h)
        dlg.StartPosition = WF.FormStartPosition.CenterScreen
        dlg.FormBorderStyle = WF.FormBorderStyle.FixedDialog
        dlg.MaximizeBox = False
        dlg.MinimizeBox = False
        dlg.BackColor = COLOR_WHITE

        # Status bar — green if all OK, amber if some failed
        bar = WF.Panel()
        bar.Location = SD.Point(0, 0)
        bar.Size = SD.Size(480, 5)
        bar.BackColor = (SD.Color.FromArgb(255, 193, 7) if has_errors
                         else SD.Color.FromArgb(40, 167, 69))
        dlg.Controls.Add(bar)

        # Main message
        if total > 0:
            if has_errors:
                msg = "{}/{} exported successfully.  {} failed.".format(
                    ok_count, total, len(errors))
            else:
                msg = "All {} file(s) exported successfully.".format(total)
        else:
            msg = "Export finished successfully."

        lbl = WF.Label()
        lbl.Text = msg
        lbl.Font = FONT_BOLD
        lbl.ForeColor = (SD.Color.FromArgb(180, 80, 0) if has_errors
                         else SD.Color.FromArgb(20, 120, 50))
        lbl.Location = SD.Point(20, 20)
        lbl.Size = SD.Size(440, 22)
        lbl.AutoSize = False
        dlg.Controls.Add(lbl)

        # Error list (scrollable) — shown only when failures exist
        if has_errors:
            txt_errors = WF.TextBox()
            txt_errors.Multiline = True
            txt_errors.ReadOnly = True
            txt_errors.ScrollBars = WF.ScrollBars.Vertical
            txt_errors.Location = SD.Point(20, 50)
            txt_errors.Size = SD.Size(440, 100)
            txt_errors.Font = FONT_SMALL
            txt_errors.BackColor = SD.Color.FromArgb(255, 248, 240)
            txt_errors.ForeColor = SD.Color.FromArgb(180, 40, 0)
            txt_errors.Text = "".join(errors)
            dlg.Controls.Add(txt_errors)

        btn_y = dlg_h - 100
        btn_open = make_primary_button("Open Folder", 115, 36)
        btn_open.Location = SD.Point(15, btn_y)
        def _open_folder(s, e):
            try:
                subprocess.Popen(["explorer", os.path.normpath(folder_path)])
            except:
                pass
            dlg.Close()
        btn_open.Click += _open_folder
        dlg.Controls.Add(btn_open)

        btn_back = make_cancel_button("Back", 85, 36)
        btn_back.Location = SD.Point(175, btn_y)
        def _back_to_selection(s, e):
            # dlg.Close() takes effect once this click handler returns —
            # ShowDialog() then unblocks and _do_export_work()/Execute()
            # finish unwinding. We must NOT open the selector or a new
            # MainExportDialog (which creates its own ExternalEvent) right
            # here — we're still nested inside the export's own Execute()
            # callback at this point, and building a whole new modeless
            # form + ExternalEvent from inside another event's Execute()
            # is what was causing everything to exit after "Next". A short
            # one-shot Timer defers that work until Revit is back in its
            # normal idle loop.
            dlg.Close()
            global _pending_back_timer
            timer = WF.Timer()
            timer.Interval = 150
            def _reopen(sender, args):
                global _pending_back_timer
                timer.Stop()
                timer.Dispose()
                _pending_back_timer = None
                try:
                    step_c = SheetViewSelectorDialog(preselected=items or [])
                    if step_c.ShowDialog() != WF.DialogResult.OK:
                        return
                    new_selection = step_c.SelectedElements
                    if not new_selection:
                        return
                    global __export_form__
                    step_d = MainExportDialog(new_selection)
                    __export_form__ = step_d
                    step_d.Show()
                except Exception as ex:
                    WF.MessageBox.Show(str(ex), "Back Error")
            timer.Tick += _reopen
            _pending_back_timer = timer   # keep a strong reference alive
            timer.Start()
        btn_back.Click += _back_to_selection
        dlg.Controls.Add(btn_back)

        btn_close = make_cancel_button("Close", 85, 36)
        btn_close.Location = SD.Point(300, btn_y)
        btn_close.Click += lambda s, e: dlg.Close()
        dlg.Controls.Add(btn_close)

        dlg.ShowDialog()
    except:
        msg = "Export complete."
        if errors:
            msg += "\n\nFailed ({}):\n".format(len(errors))
            msg += "\n".join(errors[:10])
        TaskDialog.Show("Export Complete", msg)


def _run_ole_bind_batch(dwg_paths, errors):
    """Run the raster->OLE bind pass for the whole batch of DWGs collected
    during export, SYNCHRONOUSLY - same execution model as the original
    per-file version (runs inline, blocks until done, before the Export
    Complete dialog is shown). Batched here only means "one AutoCAD
    session, title-block name learned once and reused" - not "runs in the
    background." AutoCAD needs to stay in the foreground/same thread for
    PASTECLIP's insertion-point prompt to be answered reliably; running
    this off-thread was tried and broke that (plus a separate, real risk:
    the cached AutoCAD COM object is tied to the STA thread that created
    it, and pyRevit's persistent engine keeps this module alive between
    button presses, so a NEW background thread on the next run would try
    to reuse a COM reference from a DIFFERENT, no-longer-current thread -
    a cross-apartment violation). Appends any failures into `errors` (the
    same list already shown in the Export Complete dialog) so they're not
    silently lost the way the background/print-only version left them."""
    if not dwg_paths:
        return

    results = process_dwg_batch(dwg_paths)
    for r in results:
        if not r["success"]:
            errors.append(
                "DWG OLE bind [{}]: {}".format(
                    os.path.basename(r["dwg"]),
                    "; ".join(r["warnings"]) or "unknown error"
                )
            )


# ── IronPython scope fix ────────────────────────────────────────────────────
# pyRevit's persistent engine can hide module globals from class method
# callbacks. Re-export every module-level name into builtins so they
# are always visible regardless of call scope.
try:
    import __builtin__ as _builtins_mod
except ImportError:
    import builtins as _builtins_mod

_this_module_globals = globals().copy()
for _k, _v in _this_module_globals.items():
    if not _k.startswith('__'):
        try:
            setattr(_builtins_mod, _k, _v)
        except:
            pass
del _this_module_globals, _k, _v, _builtins_mod
# ────────────────────────────────────────────────────────────────────────────



class NoSelectionDialog(WF.Form):
    def __init__(self):
        import System.Windows.Forms as WF
        self.Text = "No Sheet or View Selected"
        self.Size = SD.Size(450, 250)
        self.StartPosition = WF.FormStartPosition.CenterScreen
        self.FormBorderStyle = WF.FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.BackColor = COLOR_WHITE
        self.Result = None

        warn_bar = WF.Panel()
        warn_bar.BackColor = SD.Color.FromArgb(255, 193, 7)
        warn_bar.Location = SD.Point(0, 0)
        warn_bar.Size = SD.Size(420, 6)
        self.Controls.Add(warn_bar)

        icon_lbl = WF.Label()
        icon_lbl.Text = "!"
        icon_lbl.Font = SD.Font("Segoe UI", 22, SD.FontStyle.Regular)
        icon_lbl.ForeColor = SD.Color.FromArgb(255, 160, 0)
        icon_lbl.Location = SD.Point(25, 22)
        icon_lbl.AutoSize = True
        self.Controls.Add(icon_lbl)

        lbl_title = WF.Label()
        lbl_title.Text = "No Sheet or View Selected"
        lbl_title.Font = FONT_TITLE
        lbl_title.ForeColor = COLOR_HEADER
        lbl_title.Location = SD.Point(70, 25)
        lbl_title.AutoSize = True
        self.Controls.Add(lbl_title)

        lbl_msg = WF.Label()
        lbl_msg.Text = (
            "No sheets or views were pre-selected in Revit.\n"
            "You can manually choose them in the next step,\n"
            "or cancel to go back and select them first."
        )
        lbl_msg.Font = FONT_NORMAL
        lbl_msg.ForeColor = SD.Color.FromArgb(60, 60, 60)
        lbl_msg.Location = SD.Point(70, 60)
        lbl_msg.Size = SD.Size(330, 60)
        self.Controls.Add(lbl_msg)

        btn_cancel = make_cancel_button("Cancel", 90, 34)
        btn_cancel.Location = SD.Point(35, 145)
        btn_cancel.Click += self._cancel
        self.Controls.Add(btn_cancel)

        btn_continue = make_primary_button("Continue", 95, 34)
        btn_continue.Location = SD.Point(305, 145)
        btn_continue.Click += self._continue
        self.Controls.Add(btn_continue)

    def _select_all_sheets(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, sh in enumerate(self._sheet_visible):
                self._sheet_selected_ids.add(get_id_value(sh.Id))
                self.lst_sheets.SetItemChecked(i, True)
        finally:
            self._populating = False

    def _deselect_all_sheets(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, sh in enumerate(self._sheet_visible):
                self._sheet_selected_ids.discard(get_id_value(sh.Id))
                self.lst_sheets.SetItemChecked(i, False)
        finally:
            self._populating = False

    def _select_all_views(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, v in enumerate(self._view_visible):
                self._view_selected_ids.add(get_id_value(v.Id))
                self.lst_views.SetItemChecked(i, True)
        finally:
            self._populating = False

    def _deselect_all_views(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, v in enumerate(self._view_visible):
                self._view_selected_ids.discard(get_id_value(v.Id))
                self.lst_views.SetItemChecked(i, False)
        finally:
            self._populating = False

    def _cancel(self, s, e):
        import System.Windows.Forms as WF
        self.Result = None
        self.DialogResult = WF.DialogResult.Cancel
        self.Close()

    def _continue(self, s, e):
        import System.Windows.Forms as WF
        self.Result = "continue"
        self.DialogResult = WF.DialogResult.OK
        self.Close()

class PromptDialog(WF.Form):
    def __init__(self, title, label_text, default_text=""):
        import System.Windows.Forms as WF
        self.Text = title
        self.Size = SD.Size(360, 170)
        self.StartPosition = WF.FormStartPosition.CenterParent
        self.FormBorderStyle = WF.FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.BackColor = COLOR_WHITE
        self.Value = None

        lbl = WF.Label()
        lbl.Text = label_text
        lbl.Font = FONT_NORMAL
        lbl.Location = SD.Point(15, 15)
        lbl.AutoSize = True
        self.Controls.Add(lbl)

        self.txt_input = WF.TextBox()
        self.txt_input.Location = SD.Point(15, 40)
        self.txt_input.Size = SD.Size(310, 24)
        self.txt_input.Text = default_text
        self.Controls.Add(self.txt_input)

        btn_ok = make_primary_button("OK", 90, 32)
        btn_ok.Location = SD.Point(15, 80)
        btn_ok.Click += self._ok
        self.Controls.Add(btn_ok)

        btn_cancel = make_cancel_button("Cancel", 90, 32)
        btn_cancel.Location = SD.Point(235, 80)
        btn_cancel.Click += self._cancel
        self.Controls.Add(btn_cancel)

        self.AcceptButton = btn_ok
        self.CancelButton = btn_cancel

    def _ok(self, s, e):
        import System.Windows.Forms as WF
        self.Value = safe_str(self.txt_input.Text).strip()
        self.DialogResult = WF.DialogResult.OK
        self.Close()

    def _cancel(self, s, e):
        import System.Windows.Forms as WF
        self.Value = None
        self.DialogResult = WF.DialogResult.Cancel
        self.Close()

def _in_valid_api_context():
    """True if we can start a Transaction right now (i.e. this call is
    still nested inside Revit's original synchronous execution context),
    False if we're outside it (e.g. rebuilt from a modeless Back-flow
    button click) and must defer through ExternalEvent instead.
    Probes by actually starting+immediately-rolling-back a no-op
    Transaction — the only reliable way to test this, since the API
    exposes no direct 'am I in context' flag."""
    try:
        t = Transaction(doc, "context probe")
        t.Start()
        t.RollBack()
        return True
    except:
        return False


class SheetViewSelectorDialog(WF.Form):
    def __init__(self, preselected=None):
        import System.Windows.Forms as WF  # ensure WF in local scope
        self.Text = "Select Sheets / Views to Export"
        self.Size = SD.Size(980, 900)
        self.StartPosition = WF.FormStartPosition.CenterScreen
        self.FormBorderStyle = WF.FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.BackColor = COLOR_WHITE
        self.SelectedElements = []

        self._populating = False
        self._all_sheets = get_all_sheets()
        self._all_views = get_all_printable_views()
        self._sheet_visible = []
        self._view_visible = []
        self._sheet_selected_ids = set()
        self._view_selected_ids = set()
        self._last_sheet_idx = -1   # for shift-click range
        self._last_view_idx  = -1   # for shift-click range

        if preselected:
            for el in preselected:
                try:
                    vid = get_id_value(el.Id)
                    if isinstance(el, ViewSheet):
                        self._sheet_selected_ids.add(vid)
                    elif isinstance(el, View):
                        self._view_selected_ids.add(vid)
                except:
                    pass

        header = WF.Panel()
        header.BackColor = COLOR_ACCENT
        header.Location = SD.Point(0, 0)
        header.Size = SD.Size(1000, 6)
        self.Controls.Add(header)

        lbl_title = WF.Label()
        lbl_title.Text = "Select Sheets & Views"
        lbl_title.Font = FONT_TITLE
        lbl_title.ForeColor = COLOR_HEADER
        lbl_title.Location = SD.Point(20, 20)
        lbl_title.AutoSize = True
        self.Controls.Add(lbl_title)

        lbl_sub = WF.Label()
        lbl_sub.Text = "Check the items you want to include in the export."
        lbl_sub.Font = FONT_NORMAL
        lbl_sub.ForeColor = SD.Color.Gray
        lbl_sub.Location = SD.Point(20, 48)
        lbl_sub.AutoSize = True
        self.Controls.Add(lbl_sub)

        grp_sheets = WF.GroupBox()
        grp_sheets.Text = "Sheets"
        grp_sheets.Font = FONT_BOLD
        grp_sheets.Location = SD.Point(15, 75)
        grp_sheets.Size = SD.Size(935, 350)
        grp_sheets.ForeColor = COLOR_ACCENT
        self.Controls.Add(grp_sheets)

        # ── View/Sheet Set Controls ──────────────────────────────────────────
        lbl_vs_set = WF.Label()
        lbl_vs_set.Text = "View/Sheet Set"
        lbl_vs_set.Font = FONT_BOLD
        lbl_vs_set.Location = SD.Point(460, 20)
        lbl_vs_set.AutoSize = True
        self.Controls.Add(lbl_vs_set)

        self.cmb_vs_set = WF.ComboBox()
        self.cmb_vs_set.DropDownStyle = WF.ComboBoxStyle.DropDownList
        self.cmb_vs_set.Font = FONT_NORMAL
        self.cmb_vs_set.Location = SD.Point(585, 17)
        self.cmb_vs_set.Size = SD.Size(220, 25)
        self.cmb_vs_set.SelectedIndexChanged += self._on_vs_set_changed
        self.Controls.Add(self.cmb_vs_set)

        self.cmb_save_set = WF.ComboBox()
        self.cmb_save_set.DropDownStyle = WF.ComboBoxStyle.DropDownList
        self.cmb_save_set.Font = FONT_NORMAL
        self.cmb_save_set.Location = SD.Point(815, 17)
        self.cmb_save_set.Size = SD.Size(130, 25)
        self.cmb_save_set.Items.AddRange(System.Array[System.Object](["Save V/S Set", "New Set", "Add to Existing", "Delete Set"]))
        self.cmb_save_set.SelectedIndex = 0
        self.cmb_save_set.SelectedIndexChanged += self._on_save_set_action
        self.Controls.Add(self.cmb_save_set)

        self._load_view_sheet_sets()
        # ──────────────────────────────────────────────────────────────────

        self.txt_sheet_search = WF.TextBox()
        self.txt_sheet_search.Location = SD.Point(10, 22)
        self.txt_sheet_search.Size = SD.Size(720, 24)
        self.txt_sheet_search.Font = FONT_NORMAL
        self.txt_sheet_search.TextChanged += self._filter_sheets
        grp_sheets.Controls.Add(self.txt_sheet_search)

        # Select All / None buttons for sheets
        btn_sel_all_s = WF.Button()
        btn_sel_all_s.Text = "Select All"
        btn_sel_all_s.Size = SD.Size(80, 22)
        btn_sel_all_s.Location = SD.Point(750, 22)
        btn_sel_all_s.FlatStyle = WF.FlatStyle.Flat
        btn_sel_all_s.FlatAppearance.BorderSize = 1
        btn_sel_all_s.Font = FONT_SMALL
        btn_sel_all_s.Click += lambda s, e: self._select_all_sheets(s, e)
        grp_sheets.Controls.Add(btn_sel_all_s)

        btn_sel_none_s = WF.Button()
        btn_sel_none_s.Text = "Deselect All"
        btn_sel_none_s.Size = SD.Size(87, 22)
        btn_sel_none_s.Location = SD.Point(836, 22)
        btn_sel_none_s.FlatStyle = WF.FlatStyle.Flat
        btn_sel_none_s.FlatAppearance.BorderSize = 1
        btn_sel_none_s.Font = FONT_SMALL
        btn_sel_none_s.Click += lambda s, e: self._deselect_all_sheets(s, e)
        grp_sheets.Controls.Add(btn_sel_none_s)

        self.lst_sheets = WF.CheckedListBox()
        self.lst_sheets.Location = SD.Point(10, 48)
        self.lst_sheets.Size = SD.Size(920, 296)
        self.lst_sheets.Font = FONT_NORMAL
        self.lst_sheets.CheckOnClick = True
        self.lst_sheets.ItemCheck += self._sheet_item_check
        grp_sheets.Controls.Add(self.lst_sheets)

        grp_views = WF.GroupBox()
        grp_views.Text = "Views"
        grp_views.Font = FONT_BOLD
        grp_views.Location = SD.Point(15, 425)
        grp_views.Size = SD.Size(935, 350)
        grp_views.ForeColor = COLOR_ACCENT
        self.Controls.Add(grp_views)

        self.txt_view_search = WF.TextBox()
        self.txt_view_search.Location = SD.Point(10, 22)
        self.txt_view_search.Size = SD.Size(720, 24)
        self.txt_view_search.Font = FONT_NORMAL
        self.txt_view_search.TextChanged += self._filter_views
        grp_views.Controls.Add(self.txt_view_search)

        # Select All / None buttons for views
        btn_sel_all_v = WF.Button()
        btn_sel_all_v.Text = "Select All"
        btn_sel_all_v.Size = SD.Size(80, 22)
        btn_sel_all_v.Location = SD.Point(750, 22)
        btn_sel_all_v.FlatStyle = WF.FlatStyle.Flat
        btn_sel_all_v.FlatAppearance.BorderSize = 1
        btn_sel_all_v.Font = FONT_SMALL
        btn_sel_all_v.Click += lambda s, e: self._select_all_views(s, e)
        grp_views.Controls.Add(btn_sel_all_v)

        btn_sel_none_v = WF.Button()
        btn_sel_none_v.Text = "Deselect All"
        btn_sel_none_v.Size = SD.Size(87, 22)
        btn_sel_none_v.Location = SD.Point(836, 22)
        btn_sel_none_v.FlatStyle = WF.FlatStyle.Flat
        btn_sel_none_v.FlatAppearance.BorderSize = 1
        btn_sel_none_v.Font = FONT_SMALL
        btn_sel_none_v.Click += lambda s, e: self._deselect_all_views(s, e)
        grp_views.Controls.Add(btn_sel_none_v)

        self.lst_views = WF.CheckedListBox()
        self.lst_views.Location = SD.Point(10, 48)
        self.lst_views.Size = SD.Size(920, 296)
        self.lst_views.Font = FONT_NORMAL
        self.lst_views.CheckOnClick = True
        self.lst_views.ItemCheck += self._view_item_check
        grp_views.Controls.Add(self.lst_views)

        btn_cancel = make_cancel_button("Cancel", 90, 34)
        btn_cancel.Location = SD.Point(740, 795)
        btn_cancel.Click += self._cancel
        self.Controls.Add(btn_cancel)

        btn_next = make_primary_button("Next >", 100, 34)
        btn_next.Location = SD.Point(845, 795)
        btn_next.Click += self._next
        self.Controls.Add(btn_next)

        self._populate_sheets(self._all_sheets)
        self._populate_views(self._all_views)


    # ── View / Sheet Set Helper Methods ──────────────────────────────────────
    def _load_view_sheet_sets(self):
        """Populate the View/Sheet Set combo box from Revit document sets."""
        self.cmb_vs_set.Items.Clear()
        self.cmb_vs_set.Items.Add("<In-session Set / Selection>")
        try:
            sets = FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements()
            for s in sets:
                nm = safe_str(s.Name)
                if nm:
                    self.cmb_vs_set.Items.Add(nm)
        except:
            pass
        self.cmb_vs_set.SelectedIndex = 0

    def _on_vs_set_changed(self, s, e):
        """Select elements belonging to the active Revit ViewSheetSet."""
        if self._populating or self.cmb_vs_set.SelectedIndex <= 0:
            return
        set_name = safe_str(self.cmb_vs_set.SelectedItem)
        target_set = None
        try:
            for vs in FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements():
                if safe_str(vs.Name) == set_name:
                    target_set = vs
                    break
        except:
            pass

        if not target_set:
            return

        self._populating = True
        try:
            self._sheet_selected_ids.clear()
            self._view_selected_ids.clear()

            set_view_ids = set([get_id_value(v.Id) for v in target_set.Views])

            for sh in self._all_sheets:
                if get_id_value(sh.Id) in set_view_ids:
                    self._sheet_selected_ids.add(get_id_value(sh.Id))

            for v in self._all_views:
                if get_id_value(v.Id) in set_view_ids:
                    self._view_selected_ids.add(get_id_value(v.Id))

            self._refresh_sheet_checks()
            self._refresh_view_checks()
        finally:
            self._populating = False

    def _on_save_set_action(self, s, e):
        if getattr(self, '_processing_save_action', False):
            return
        idx = self.cmb_save_set.SelectedIndex
        if idx == 0:
            return
        self._processing_save_action = True
        try:
            action = safe_str(self.cmb_save_set.SelectedItem)
            self.cmb_save_set.SelectedIndex = 0
            if action == "New Set":
                self._create_new_set()
            elif action == "Add to Existing":
                self._add_to_existing_set()
            elif action == "Delete Set":
                self._delete_current_set()
        finally:
            self._processing_save_action = False

    def _get_current_checked_views(self):
        """Returns List[View] of all checked sheets and views."""
        views_list = List[View]()
        selected_ids = self._sheet_selected_ids.union(self._view_selected_ids)
        for sh in self._all_sheets:
            if get_id_value(sh.Id) in selected_ids:
                views_list.Add(sh)
        for v in self._all_views:
            if get_id_value(v.Id) in selected_ids:
                views_list.Add(v)
        return views_list

    def _create_new_set(self):
        """Create a new Revit ViewSheetSet with checked elements."""
        views_to_save = self._get_current_checked_views()
        if views_to_save.Count == 0:
            WF.MessageBox.Show("Please select at least one Sheet or View to save to a set.", "No Selection")
            return

        dlg = PromptDialog("New View/Sheet Set", "Enter Set Name:")
        if dlg.ShowDialog() != WF.DialogResult.OK or not dlg.Value:
            return
        set_name = dlg.Value

        def _do():
            t = Transaction(doc, "Create View Sheet Set")
            try:
                t.Start()
                view_set = ViewSet()
                for v in views_to_save:
                    view_set.Insert(v)
                pm = doc.PrintManager
                pm.PrintRange = PrintRange.Select
                vss_setting = pm.ViewSheetSetting
                vss_setting.CurrentViewSheetSet.Views = view_set
                vss_setting.SaveAs(set_name)
                t.Commit()
                WF.MessageBox.Show("View/Sheet Set '{}' created successfully.".format(set_name), "Success")
                self._load_view_sheet_sets()
                idx = self.cmb_vs_set.FindStringExact(set_name)
                if idx >= 0:
                    self.cmb_vs_set.SelectedIndex = idx
            except Exception as ex:
                if t.HasStarted():
                    t.RollBack()
                WF.MessageBox.Show("Failed to save set: " + str(ex), "Error")

        # Run instantly if we're already in valid API context (the normal
        # path — nested inside the original ShowDialog() call chain, still
        # inside Revit's synchronous script execution). Only defer through
        # the ExternalEvent if we're not (rebuilt via Back-flow, which
        # already handed control back to Revit's idle loop).
        if _in_valid_api_context():
            _do()
        else:
            _generic_handler.action = _do
            _generic_event.Raise()

    def _add_to_existing_set(self):
        """Append checked elements to an existing Revit ViewSheetSet."""
        views_to_save = self._get_current_checked_views()
        if views_to_save.Count == 0:
            WF.MessageBox.Show("Please select at least one Sheet or View to add.", "No Selection")
            return

        current_sel = safe_str(self.cmb_vs_set.SelectedItem)
        if not current_sel or current_sel.startswith("<"):
            WF.MessageBox.Show("Please select an existing View/Sheet Set from the dropdown first.", "Select Existing Set")
            return

        def _do():
            target_set = None
            for vs in FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements():
                if safe_str(vs.Name) == current_sel:
                    target_set = vs
                    break
            if not target_set:
                WF.MessageBox.Show("Set '{}' no longer exists.".format(current_sel), "Error")
                return

            t = Transaction(doc, "Add to View Sheet Set")
            try:
                t.Start()
                pm = doc.PrintManager
                pm.PrintRange = PrintRange.Select
                vss_setting = pm.ViewSheetSetting
                vss_setting.CurrentViewSheetSet = target_set

                # target_set.Views is a READ-ONLY ViewSet snapshot —
                # .Insert() on it throws "Collection is read-only".
                # Build a brand-new ViewSet with existing + new views
                # combined, then assign it wholesale (same pattern
                # _create_new_set already uses).
                combined = ViewSet()
                existing_ids = set()
                for v in target_set.Views:
                    combined.Insert(v)
                    existing_ids.add(get_id_value(v.Id))
                for v in views_to_save:
                    if get_id_value(v.Id) not in existing_ids:
                        combined.Insert(v)

                vss_setting.CurrentViewSheetSet.Views = combined
                vss_setting.Save()
                t.Commit()
                WF.MessageBox.Show("Items added to '{}' successfully.".format(current_sel), "Success")
            except Exception as ex:
                if t.HasStarted():
                    t.RollBack()
                WF.MessageBox.Show("Failed to update set: " + str(ex), "Error")

        if _in_valid_api_context():
            _do()
        else:
            _generic_handler.action = _do
            _generic_event.Raise()

    def _delete_current_set(self):
        """Delete currently selected ViewSheetSet in Revit."""
        current_sel = safe_str(self.cmb_vs_set.SelectedItem)
        if not current_sel or current_sel.startswith("<"):
            WF.MessageBox.Show("Please select a valid View/Sheet Set to delete.", "Delete Set")
            return

        res = WF.MessageBox.Show(
            "Are you sure you want to delete set '{}'?".format(current_sel),
            "Confirm Delete", WF.MessageBoxButtons.YesNo)
        if res != WF.DialogResult.Yes:
            return

        def _do():
            target_set = None
            for vs in FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements():
                if safe_str(vs.Name) == current_sel:
                    target_set = vs
                    break
            if not target_set:
                return
            t = Transaction(doc, "Delete View Sheet Set")
            try:
                t.Start()
                doc.Delete(target_set.Id)
                t.Commit()
                WF.MessageBox.Show("Set deleted.", "Success")
                self._load_view_sheet_sets()
            except Exception as ex:
                if t.HasStarted():
                    t.RollBack()
                WF.MessageBox.Show("Failed to delete set: " + str(ex), "Error")

        if _in_valid_api_context():
            _do()
        else:
            _generic_handler.action = _do
            _generic_event.Raise()

    # ── Select All / Deselect All ────────────────────────────────────────
    def _select_all_sheets(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, sh in enumerate(self._sheet_visible):
                self._sheet_selected_ids.add(get_id_value(sh.Id))
                self.lst_sheets.SetItemChecked(i, True)
        finally:
            self._populating = False

    def _deselect_all_sheets(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, sh in enumerate(self._sheet_visible):
                self._sheet_selected_ids.discard(get_id_value(sh.Id))
                self.lst_sheets.SetItemChecked(i, False)
        finally:
            self._populating = False

    def _select_all_views(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, v in enumerate(self._view_visible):
                self._view_selected_ids.add(get_id_value(v.Id))
                self.lst_views.SetItemChecked(i, True)
        finally:
            self._populating = False

    def _deselect_all_views(self, s, e):
        import System.Windows.Forms as WF
        self._populating = True
        try:
            for i, v in enumerate(self._view_visible):
                self._view_selected_ids.discard(get_id_value(v.Id))
                self.lst_views.SetItemChecked(i, False)
        finally:
            self._populating = False

    def _refresh_sheet_checks(self):
        for i, sh in enumerate(self._sheet_visible):
            isChecked = get_id_value(sh.Id) in self._sheet_selected_ids
            self.lst_sheets.SetItemChecked(i, isChecked)

    def _refresh_view_checks(self):
        for i, v in enumerate(self._view_visible):
            isChecked = get_id_value(v.Id) in self._view_selected_ids
            self.lst_views.SetItemChecked(i, isChecked)

    def _populate_sheets(self, sheets):
        self._populating = True
        try:
            self.lst_sheets.Items.Clear()
            self._sheet_visible = list(sheets)
            for sheet in sheets:
                try:
                    label = u"{} - {}".format(sheet.SheetNumber, sheet.Name)
                except:
                    label = safe_str(sheet.Name)
                checked = get_id_value(sheet.Id) in self._sheet_selected_ids
                self.lst_sheets.Items.Add(label, checked)
        finally:
            self._populating = False

    def _populate_views(self, views):
        self._populating = True
        try:
            self.lst_views.Items.Clear()
            self._view_visible = list(views)
            for v in views:
                try:
                    label = u"[{}] {}".format(str(v.ViewType).replace("ViewType.", ""), v.Name)
                except:
                    label = safe_str(v.Name)
                checked = get_id_value(v.Id) in self._view_selected_ids
                self.lst_views.Items.Add(label, checked)
        finally:
            self._populating = False

    def _filter_sheets(self, s, e):
        kw = self.txt_sheet_search.Text.strip().lower()
        if not kw:
            filtered = self._all_sheets
        else:
            filtered = [sh for sh in self._all_sheets if kw in safe_str(sh.SheetNumber).lower() or kw in safe_str(sh.Name).lower()]
        self._populate_sheets(filtered)

    def _filter_views(self, s, e):
        kw = self.txt_view_search.Text.strip().lower()
        if not kw:
            filtered = self._all_views
        else:
            filtered = [v for v in self._all_views if kw in safe_str(v.Name).lower()]
        self._populate_views(filtered)

    def _sheet_item_check(self, sender, e):
        import System.Windows.Forms as WF
        if self._populating:
            return
        try:
            if e.Index < 0 or e.Index >= len(self._sheet_visible):
                return
            new_state = (int(e.NewValue) == 1)  # 1 = CheckState.Checked
            # Shift held → range select from last clicked to current
            try:
                from System.Windows.Forms import Control as _Ctrl, Keys as _Keys
                shift = bool(int(_Ctrl.ModifierKeys) & int(_Keys.Shift))
            except:
                shift = False
            if shift and self._last_sheet_idx >= 0:
                lo = min(self._last_sheet_idx, e.Index)
                hi = max(self._last_sheet_idx, e.Index)
                self._populating = True
                try:
                    for i in range(lo, hi + 1):
                        sh = self._sheet_visible[i]
                        sid2 = get_id_value(sh.Id)
                        if new_state:
                            self._sheet_selected_ids.add(sid2)
                        else:
                            self._sheet_selected_ids.discard(sid2)
                        self.lst_sheets.SetItemChecked(i, new_state)
                finally:
                    self._populating = False
            else:
                sheet = self._sheet_visible[e.Index]
                sid = get_id_value(sheet.Id)
                if new_state:
                    self._sheet_selected_ids.add(sid)
                else:
                    self._sheet_selected_ids.discard(sid)
            self._last_sheet_idx = e.Index
        except:
            pass

    def _view_item_check(self, sender, e):
        import System.Windows.Forms as WF  # ensure WF in local scope
        if self._populating:
            return
        try:
            if e.Index < 0 or e.Index >= len(self._view_visible):
                return
            new_state = (int(e.NewValue) == 1)  # 1 = CheckState.Checked
            try:
                from System.Windows.Forms import Control as _Ctrl, Keys as _Keys
                shift = bool(int(_Ctrl.ModifierKeys) & int(_Keys.Shift))
            except:
                shift = False
            if shift and self._last_view_idx >= 0:
                lo = min(self._last_view_idx, e.Index)
                hi = max(self._last_view_idx, e.Index)
                self._populating = True
                try:
                    for i in range(lo, hi + 1):
                        v2 = self._view_visible[i]
                        vid2 = get_id_value(v2.Id)
                        if new_state:
                            self._view_selected_ids.add(vid2)
                        else:
                            self._view_selected_ids.discard(vid2)
                        self.lst_views.SetItemChecked(i, new_state)
                finally:
                    self._populating = False
            else:
                view = self._view_visible[e.Index]
                vid = get_id_value(view.Id)
                if new_state:
                    self._view_selected_ids.add(vid)
                else:
                    self._view_selected_ids.discard(vid)
            self._last_view_idx = e.Index
        except:
            pass

    def _cancel(self, s, e):
        import System.Windows.Forms as WF
        self.SelectedElements = []
        self.DialogResult = WF.DialogResult.Cancel
        self.Close()

    def _next(self, s, e):
        import System.Windows.Forms as WF
        selected = []
        try:
            for sh in self._all_sheets:
                if get_id_value(sh.Id) in self._sheet_selected_ids:
                    selected.append(sh)
            for v in self._all_views:
                if get_id_value(v.Id) in self._view_selected_ids:
                    selected.append(v)
        except:
            pass

        if not selected:
            WF.MessageBox.Show("Please select at least one sheet or view before proceeding.", "Nothing Selected", WF.MessageBoxButtons.OK, WF.MessageBoxIcon.Warning)
            return

        self.SelectedElements = selected
        self.DialogResult = WF.DialogResult.OK
        self.Close()

class DWGDXFExportSetupDialog(WF.Form):
    def __init__(self, current_setup):
        import System.Windows.Forms as WF
        self.Text = "DWG/DXF Export Setup"
        self.Size = SD.Size(430, 360)
        self.MinimumSize = SD.Size(430, 360)
        self.StartPosition = WF.FormStartPosition.CenterParent
        self.FormBorderStyle = WF.FormBorderStyle.FixedDialog
        self.MaximizeBox = False
        self.MinimizeBox = False
        self.BackColor = SD.Color.White
        self.SelectedSetup = safe_str(current_setup) if current_setup else "<In-session>"

        lbl = WF.Label()
        lbl.Text = "Select DWG/DXF Export Setup"
        lbl.Location = SD.Point(15, 15)
        lbl.AutoSize = True
        lbl.Font = FONT_BOLD
        self.Controls.Add(lbl)

        self.lst = WF.ListBox()
        self.lst.Location = SD.Point(15, 40)
        self.lst.Size = SD.Size(390, 190)
        self.lst.Anchor = TOP_LEFT_RIGHT
        self.lst.Items.AddRange(System.Array[System.Object](get_dwg_dxf_export_setups()))
        self.lst.DoubleClick += self._ok
        self.Controls.Add(self.lst)

        try:
            idx = self.lst.Items.IndexOf(self.SelectedSetup)
            self.lst.SelectedIndex = idx if idx >= 0 else 0
        except:
            pass

        self.lbl_status = WF.Label()
        self.lbl_status.Text = "Import or export the selected setup name as an XML preset."
        self.lbl_status.Location = SD.Point(15, 238)
        self.lbl_status.Size = SD.Size(390, 32)
        self.lbl_status.AutoEllipsis = True
        self.lbl_status.ForeColor = SD.Color.FromArgb(80, 80, 80)
        self.lbl_status.Font = FONT_SMALL
        self.lbl_status.Anchor = TOP_LEFT_RIGHT
        self.Controls.Add(self.lbl_status)

        btn_import = WF.Button()
        btn_import.Text = "Import..."
        btn_import.Location = SD.Point(15, 280)
        btn_import.Size = SD.Size(80, 30)
        btn_import.Anchor = TOP_LEFT
        btn_import.Click += self._import_preset
        self.Controls.Add(btn_import)

        btn_export = WF.Button()
        btn_export.Text = "Export..."
        btn_export.Location = SD.Point(105, 280)
        btn_export.Size = SD.Size(80, 30)
        btn_export.Anchor = TOP_LEFT
        btn_export.Click += self._export_preset
        self.Controls.Add(btn_export)

        btn_ok = WF.Button()
        btn_ok.Text = "OK"
        btn_ok.Location = SD.Point(250, 280)
        btn_ok.Size = SD.Size(80, 30)
        btn_ok.Anchor = TOP_RIGHT
        btn_ok.Click += self._ok
        self.Controls.Add(btn_ok)

        btn_cancel = WF.Button()
        btn_cancel.Text = "Cancel"
        btn_cancel.Location = SD.Point(335, 280)
        btn_cancel.Size = SD.Size(80, 30)
        btn_cancel.Anchor = TOP_RIGHT
        btn_cancel.Click += self._cancel
        self.Controls.Add(btn_cancel)

    def _import_preset(self, s, e):
        import System.Windows.Forms as WF
        path = pick_open_xml("Import export setup preset", _get_export_preset_dir(), self)
        if not path:
            return
        try:
            data = xml_load_file(path)
            if isinstance(data, dict):
                setup_name = safe_str(data.get("dwg_setup", "")).strip()
                if setup_name:
                    self.SelectedSetup = setup_name
                    try:
                        idx = self.lst.Items.IndexOf(self.SelectedSetup)
                        self.lst.SelectedIndex = idx if idx >= 0 else 0
                    except:
                        pass
                msg = safe_str(data.get("preset_name", "")).strip()
                if msg:
                    self.lbl_status.Text = "Loaded preset: {}".format(msg)
                else:
                    self.lbl_status.Text = "Preset imported."
        except Exception as ex:
            WF.MessageBox.Show(str(ex), "Import Error")

    def _export_preset(self, s, e):
        import System.Windows.Forms as WF
        path = pick_save_xml("Export export setup preset", EXPORT_PRESET_DIR, "export_setup_preset.xml", self)
        if not path:
            return
        try:
            data = {
                "preset_name": "DWG Setup Preset",
                "dwg_setup": self.SelectedSetup,
                "created_by": "Revit Export Tool"
            }
            xml_save_file(path, data, root_name="preset")
            self.lbl_status.Text = "Preset exported to XML."
            WF.MessageBox.Show("Preset exported.", "Export Setup")
        except Exception as ex:
            WF.MessageBox.Show(str(ex), "Export Error")

    def _ok(self, s, e):
        import System.Windows.Forms as WF
        try:
            if self.lst.SelectedItem is not None:
                self.SelectedSetup = safe_str(self.lst.SelectedItem)
        except:
            pass
        self.DialogResult = WF.DialogResult.OK
        self.Close()

    def _cancel(self, s, e):
        import System.Windows.Forms as WF
        self.DialogResult = WF.DialogResult.Cancel
        self.Close()


class _ExportEventHandler(IExternalEventHandler):
    """Runs the ENTIRE export routine (temp crop -> export -> restore crop)
    inside a valid Revit API context.

    MainExportDialog is shown modelessly (.Show()), so its button-click
    handlers run OUTSIDE the API call stack and cannot start a Transaction
    directly — Revit raises 'Starting a transaction from an external
    application running outside of API context is not allowed.'

    A single ExternalEvent.Raise() -> Execute() call is used (rather than
    raising an event and blocking/polling for it from the click handler)
    because polling with Application.DoEvents() from inside a click handler
    that Revit's own message loop already invoked is unreliable and can
    time out. Doing all the work in one Execute() call sidesteps that."""
    def __init__(self):
        self.dialog = None

    def Execute(self, uiapp):
        if self.dialog is None:
            return
        try:
            self.dialog._do_export_work()
        except Exception as ex:
            try:
                import System.Windows.Forms as WF
                WF.MessageBox.Show(str(ex), "Export Error")
            except:
                pass

    def GetName(self):
        return "Export Tool Export Handler"


# Created exactly ONCE here, during this script's own top-level execution.
# That's the only point guaranteed to be a valid Revit API execution context
# for THIS whole session — ExternalEvent.Create() fails with "Attempting to
# create an ExternalEvent outside of a standard API execution" when called
# from a button-click handler on a modeless form, and (confirmed) also when
# called from inside a WF.Timer.Tick callback — neither is a genuine API
# callback, just an ordinary Windows message. Every MainExportDialog
# instance — including ones rebuilt later via any "Back" flow — must reuse
# this single event/handler instead of creating its own.
_export_handler = _ExportEventHandler()
_export_event = ExternalEvent.Create(_export_handler)


class _GenericActionHandler(IExternalEventHandler):
    """One-shot handler: runs whatever no-arg callable is set on .action
    inside a valid Revit API context, then clears it. Used for Transaction
    work triggered from controls on SheetViewSelectorDialog, which can be
    opened either from valid API context (first run) or from a modeless
    Back-flow (outside API context) — Transactions must always go through
    here so both paths behave identically."""
    def __init__(self):
        self.action = None

    def Execute(self, uiapp):
        action = self.action
        self.action = None
        if action is None:
            return
        try:
            action()
        except Exception as ex:
            try:
                WF.MessageBox.Show(str(ex), "Error")
            except:
                pass

    def GetName(self):
        return "Export Tool Generic Action Handler"


_generic_handler = _GenericActionHandler()
_generic_event = ExternalEvent.Create(_generic_handler)

# In-memory settings cache — replaces the old export_settings.xml file.
#
# IMPORTANT: __persistentengine__ keeps the IronPython AppDomain warm for
# speed, but the script's own top-level code (including any plain module
# variable declared here, like "_x = None") re-executes FRESH every time
# the tool is run from the ribbon — so a plain module-level variable gets
# reset on every single invocation and can never survive across them.
#
# System.AppDomain.CurrentDomain, however, IS the same object across
# invocations (that's what __persistentengine__ actually keeps alive), so
# storing data there via SetData/GetData genuinely persists for as long
# as Revit itself stays open — without ever touching disk.
_SETTINGS_DOMAIN_KEY = "RevitExportTool_SettingsCache_v1"

def _get_cached_settings():
    try:
        return System.AppDomain.CurrentDomain.GetData(_SETTINGS_DOMAIN_KEY)
    except:
        return None

def _set_cached_settings(data):
    try:
        System.AppDomain.CurrentDomain.SetData(_SETTINGS_DOMAIN_KEY, data)
    except:
        pass

class MainExportDialog(WF.Form):
    def __init__(self, items):
        import System.Windows.Forms as WF
        self.items = items
        _cached = _get_cached_settings()
        self.settings = _cached if _cached is not None else default_settings()
        self.export_path = self.settings.get("last_folder", "")
        self._restore_state = None  # set externally by _go_back before Show()

        # Reuse the single ExternalEvent created at script top-level (see
        # comment above _export_handler/_export_event) — do NOT create a new
        # one here, it would fail whenever this dialog is rebuilt from a
        # button click (e.g. any "Back" flow).
        self._export_handler = _export_handler
        self._export_handler.dialog = self
        self._export_event = _export_event

        self.Text = "Export PDF & DWG Setting"
        self.Size = SD.Size(980, 980)
        self.MinimumSize = SD.Size(960, 960)
        self.StartPosition = WF.FormStartPosition.CenterScreen
        self.BackColor = COLOR_WHITE
        self.AutoScaleMode = WF.AutoScaleMode.Dpi
        self.FormBorderStyle = WF.FormBorderStyle.Sizable

        header = WF.Panel()
        header.BackColor = COLOR_ACCENT
        header.Location = SD.Point(0, 0)
        header.Size = SD.Size(980, 5)
        header.Anchor = TOP_LEFT_RIGHT
        self.Controls.Add(header)

        title = WF.Label()
        title.Text = "PDF / DWG  —  {} Item(s) Selected".format(len(items))
        title.Font = FONT_TITLE
        title.ForeColor = COLOR_HEADER
        title.Location = SD.Point(20, 16)
        title.AutoSize = False
        title.Size = SD.Size(480, 32)
        title.Anchor = TOP_LEFT
        self.Controls.Add(title)

        lbl_fmt = WF.Label()
        lbl_fmt.Text = "Export Format"
        lbl_fmt.Font = FONT_BOLD
        lbl_fmt.Location = SD.Point(20, 52)
        lbl_fmt.AutoSize = True
        lbl_fmt.Anchor = TOP_LEFT
        self.Controls.Add(lbl_fmt)

        # Now checkboxes (not radio buttons) — both PDF and CAD can be
        # checked at once, exporting both formats in a single run.
        # Attribute names kept as rad_pdf/rad_cad to avoid touching every
        # other reference to .Checked across the class.
        self.rad_pdf = WF.CheckBox()
        self.rad_pdf.Text = "PDF"
        self.rad_pdf.Font = FONT_BOLD
        self.rad_pdf.Location = SD.Point(140, 49)
        self.rad_pdf.Size = SD.Size(70, 24)
        self.rad_pdf.Checked = True
        self.rad_pdf.ForeColor = SD.Color.FromArgb(255, 0, 0)
        self.rad_pdf.Anchor = TOP_LEFT
        self.Controls.Add(self.rad_pdf)

        self.rad_cad = WF.CheckBox()
        self.rad_cad.Text = "DWG"
        self.rad_cad.Font = FONT_BOLD
        self.rad_cad.Location = SD.Point(220, 49)
        self.rad_cad.Size = SD.Size(110, 24)
        self.rad_cad.ForeColor = SD.Color.FromArgb(255, 0, 0)
        self.rad_cad.Anchor = TOP_LEFT
        self.Controls.Add(self.rad_cad)

        lbl_type = WF.Label()
        lbl_type.Text = "Parameter Type"
        lbl_type.Font = FONT_BOLD
        lbl_type.Location = SD.Point(20, 88)
        lbl_type.AutoSize = True
        lbl_type.Anchor = TOP_LEFT
        self.Controls.Add(lbl_type)

        self.cmb_type = WF.ComboBox()
        self.cmb_type.Location = SD.Point(140, 83)
        self.cmb_type.Width = 160
        self.cmb_type.DropDownStyle = WF.ComboBoxStyle.DropDownList
        self.cmb_type.Items.AddRange(System.Array[System.Object](["Sheets", "Views", "Project Information"]))
        saved_type = safe_str(self.settings.get("parameter_type", "Sheets"))
        if saved_type in ["Sheets", "Views", "Project Information"]:
            self.cmb_type.SelectedItem = saved_type
        if self.cmb_type.SelectedIndex < 0:
            self.cmb_type.SelectedIndex = 0
        self.cmb_type.Anchor = TOP_LEFT
        self.Controls.Add(self.cmb_type)

        self.grid = WF.DataGridView()
        self.grid.Location = SD.Point(20, 120)
        self.grid.Size = SD.Size(920, 225)
        self.grid.Anchor = TOP_LEFT_RIGHT
        self.grid.AllowUserToAddRows = False
        self.grid.RowHeadersVisible = False
        self.grid.AutoSizeColumnsMode = WF.DataGridViewAutoSizeColumnsMode.Fill
        self.grid.EnableHeadersVisualStyles = False
        self.grid.ColumnHeadersDefaultCellStyle.BackColor = COLOR_ACCENT
        self.grid.ColumnHeadersDefaultCellStyle.ForeColor = COLOR_WHITE
        self.grid.SelectionMode = WF.DataGridViewSelectionMode.FullRowSelect
        self.grid.MultiSelect = False
        self.grid.DataError += self._grid_data_error
        self.Controls.Add(self.grid)

        self.name_col = WF.DataGridViewComboBoxColumn()
        self.name_col.HeaderText = "Name"
        self.name_col.Name = "Name"
        self.name_col.FlatStyle = WF.FlatStyle.Flat
        self.name_col.DataSource = get_params(self.cmb_type.Text)
        self.grid.Columns.Add(self.name_col)
        self.grid.Columns.Add("Prefix", "Prefix")
        self.grid.Columns.Add("Sample", "Sample Value")
        self.grid.Columns.Add("Suffix", "Suffix")
        self.grid.Columns.Add("Separator", "Separator")

        for row in self.settings.get("rows", []):
            self.grid.Rows.Add(
                row.get("name", ""),
                row.get("prefix", ""),
                row.get("sample", ""),
                row.get("suffix", ""),
                row.get("separator", "")
            )

        self.cmb_type.SelectedIndexChanged += self._refresh_name_column
        self.cmb_type.SelectedIndexChanged += self._update_preview
        self.grid.CellEndEdit += self._update_preview
        self.grid.CellValueChanged += self._update_preview
        self.grid.RowsAdded += self._update_preview
        self.grid.RowsRemoved += self._update_preview
        self.grid.CurrentCellDirtyStateChanged += self._on_dirty_state

        btn_y = 357
        self.btn_add = WF.Button()
        self.btn_add.Text = "+ Add Row"
        self.btn_add.Location = SD.Point(20, btn_y)
        self.btn_add.Size = SD.Size(90, 27)
        self.btn_add.Anchor = TOP_LEFT
        self.btn_add.Click += self._add_row
        self.Controls.Add(self.btn_add)

        self.btn_remove = WF.Button()
        self.btn_remove.Text = "- Remove"
        self.btn_remove.Location = SD.Point(118, btn_y)
        self.btn_remove.Size = SD.Size(85, 27)
        self.btn_remove.Anchor = TOP_LEFT
        self.btn_remove.Click += self._remove_row
        self.Controls.Add(self.btn_remove)

        self.btn_up = WF.Button()
        self.btn_up.Text = "Move Up"
        self.btn_up.Location = SD.Point(211, btn_y)
        self.btn_up.Size = SD.Size(80, 27)
        self.btn_up.Anchor = TOP_LEFT
        self.btn_up.Click += self._move_up
        self.Controls.Add(self.btn_up)

        self.btn_down = WF.Button()
        self.btn_down.Text = "Move Down"
        self.btn_down.Location = SD.Point(299, btn_y)
        self.btn_down.Size = SD.Size(90, 27)
        self.btn_down.Anchor = TOP_LEFT
        self.btn_down.Click += self._move_down
        self.Controls.Add(self.btn_down)

        pnl_preview = WF.Panel()
        pnl_preview.Location = SD.Point(10, 394)
        pnl_preview.Size = SD.Size(940, 44)
        pnl_preview.BackColor = SD.Color.FromArgb(230, 240, 255)
        pnl_preview.Anchor = TOP_LEFT_RIGHT
        self.Controls.Add(pnl_preview)

        lbl_prev_title = WF.Label()
        lbl_prev_title.Text = "Preview:"
        lbl_prev_title.Font = FONT_BOLD
        lbl_prev_title.ForeColor = COLOR_ACCENT
        lbl_prev_title.Location = SD.Point(10, 12)
        lbl_prev_title.AutoSize = True
        lbl_prev_title.Anchor = TOP_LEFT
        pnl_preview.Controls.Add(lbl_prev_title)

        self.lbl_preview = WF.Label()
        self.lbl_preview.Text = ""
        self.lbl_preview.Font = FONT_NORMAL
        self.lbl_preview.ForeColor = SD.Color.FromArgb(30, 30, 30)
        self.lbl_preview.Location = SD.Point(75, 12)
        self.lbl_preview.Size = SD.Size(848, 20)
        self.lbl_preview.AutoEllipsis = True
        self.lbl_preview.Anchor = TOP_LEFT_RIGHT
        pnl_preview.Controls.Add(self.lbl_preview)

        self.pnl = WF.Panel()
        self.pnl.Location = SD.Point(10, 447)
        self.pnl.Size = SD.Size(940, 415)
        self.pnl.BackColor = COLOR_PANEL_BG
        self.pnl.Anchor = ALL_SIDES
        self.Controls.Add(self.pnl)

        def add_lbl(text, x, y):
            lbl = WF.Label()
            lbl.Text = text
            lbl.Font = FONT_BOLD
            lbl.Location = SD.Point(x, y)
            lbl.AutoSize = True
            lbl.Anchor = TOP_LEFT
            self.pnl.Controls.Add(lbl)
            return lbl

        def add_cmb(items, x, y, w=160):
            cmb = WF.ComboBox()
            cmb.Location = SD.Point(x, y)
            cmb.Size = SD.Size(w, 24)
            cmb.DropDownStyle = WF.ComboBoxStyle.DropDownList
            cmb.Items.AddRange(System.Array[System.Object](items))
            if cmb.Items.Count > 0:
                cmb.SelectedIndex = 0
            cmb.Anchor = TOP_LEFT
            self.pnl.Controls.Add(cmb)
            return cmb

        def add_chk(text, x, y, w=270, checked=False):
            chk = WF.CheckBox()
            chk.Text = text
            chk.Location = SD.Point(x, y)
            chk.Width = w
            chk.Checked = checked
            chk.Anchor = TOP_LEFT
            self.pnl.Controls.Add(chk)
            return chk

        add_lbl("Select Export Setup", 20, 15)
        self.cmb_setup = add_cmb(get_dwg_dxf_export_setups(), 20, 38, 250)

        self.btn_setup_more = WF.Button()
        self.btn_setup_more.Text = "..."
        self.btn_setup_more.Location = SD.Point(275, 37)
        self.btn_setup_more.Size = SD.Size(28, 26)
        self.btn_setup_more.Anchor = TOP_LEFT
        self.btn_setup_more.Click += self._open_export_setup_dialog
        self.pnl.Controls.Add(self.btn_setup_more)

        add_lbl("File Name", 20, 74)
        self.txt_file = WF.TextBox()
        self.txt_file.Location = SD.Point(20, 97)
        self.txt_file.Size = SD.Size(250, 24)
        self.txt_file.Text = self.settings.get("file_name", "")
        self.txt_file.Anchor = TOP_LEFT
        self.pnl.Controls.Add(self.txt_file)

        self.chk_combine = WF.CheckBox()
        self.chk_combine.Text = "Combine into a single PDF file"
        self.chk_combine.Location = SD.Point(20, 130)
        self.chk_combine.Width = 310
        self.chk_combine.Checked = self.settings.get("combine", False)
        self.chk_combine.Anchor = TOP_LEFT
        self.pnl.Controls.Add(self.chk_combine)

        self.btn_folder = WF.Button()
        self.btn_folder.Text = "Choose Folder"
        self.btn_folder.Location = SD.Point(20, 160)
        self.btn_folder.Size = SD.Size(120, 30)
        self.btn_folder.Anchor = TOP_LEFT
        self.btn_folder.Click += self._choose_folder
        self.pnl.Controls.Add(self.btn_folder)

        self.btn_save = WF.Button()
        self.btn_save.Text = "Save Settings"
        self.btn_save.Location = SD.Point(150, 160)
        self.btn_save.Size = SD.Size(120, 30)
        self.btn_save.Anchor = TOP_LEFT
        self.btn_save.Click += self._save_settings
        self.pnl.Controls.Add(self.btn_save)

        self.lbl_folder = WF.Label()
        self.lbl_folder.Text = self.export_path
        self.lbl_folder.Location = SD.Point(20, 198)
        self.lbl_folder.Size = SD.Size(390, 36)
        self.lbl_folder.Anchor = TOP_LEFT
        self.pnl.Controls.Add(self.lbl_folder)

        add_lbl("Preset Tools", 430, 15)
        self.btn_load_preset = WF.Button()
        self.btn_load_preset.Text = "Load Preset"
        self.btn_load_preset.Location = SD.Point(430, 38)
        self.btn_load_preset.Size = SD.Size(150, 28)
        self.btn_load_preset.Click += self._load_export_preset
        self.pnl.Controls.Add(self.btn_load_preset)

        self.btn_save_preset = WF.Button()
        self.btn_save_preset.Text = "Save Preset"
        self.btn_save_preset.Location = SD.Point(590, 38)
        self.btn_save_preset.Size = SD.Size(150, 28)
        self.btn_save_preset.Click += self._save_export_preset
        self.pnl.Controls.Add(self.btn_save_preset)

        grp_range = WF.GroupBox()
        grp_range.Text = "Export Range"
        grp_range.Font = FONT_BOLD
        grp_range.ForeColor = COLOR_ACCENT
        grp_range.Location = SD.Point(15, 272)
        grp_range.Size = SD.Size(390, 108)
        grp_range.Anchor = TOP_LEFT
        self.pnl.Controls.Add(grp_range)

        self.rad_current_window = WF.RadioButton()
        self.rad_current_window.Text = "Current window"
        self.rad_current_window.Font = FONT_NORMAL
        self.rad_current_window.Location = SD.Point(12, 22)
        self.rad_current_window.AutoSize = True
        self.rad_current_window.Enabled = True
        grp_range.Controls.Add(self.rad_current_window)

        self.rad_visible_portion = WF.RadioButton()
        self.rad_visible_portion.Text = "Visible portion of current window"
        self.rad_visible_portion.Font = FONT_NORMAL
        self.rad_visible_portion.Location = SD.Point(12, 46)
        self.rad_visible_portion.AutoSize = True
        self.rad_visible_portion.Enabled = True
        grp_range.Controls.Add(self.rad_visible_portion)

        self.rad_selected_views = WF.RadioButton()
        self.rad_selected_views.Text = "Selected views/sheets"
        self.rad_selected_views.Font = FONT_NORMAL
        self.rad_selected_views.Location = SD.Point(12, 70)
        self.rad_selected_views.AutoSize = True
        self.rad_selected_views.Checked = True
        grp_range.Controls.Add(self.rad_selected_views)

        self.lbl_viewset = WF.Label()
        self.lbl_viewset.Text = "  ({} item(s))".format(len(self.items))
        self.lbl_viewset.Font = FONT_SMALL
        self.lbl_viewset.ForeColor = SD.Color.Gray
        self.lbl_viewset.Location = SD.Point(178, 73)
        self.lbl_viewset.AutoSize = True
        grp_range.Controls.Add(self.lbl_viewset)

        add_lbl("Page Size", 430, 75)
        self.cmb_pagesize = add_cmb(get_paper_sizes(), 430, 98, 190)
        add_lbl("Paper Placement", 430, 132)
        self.cmb_place = add_cmb(["Center", "LowerLeft"], 430, 155, 150)
        add_lbl("Orientation", 430, 189)
        self.cmb_orientation = add_cmb(["Auto", "Portrait", "Landscape"], 430, 212, 150)

        add_lbl("Zoom", 660, 75)
        self.cmb_zoom = add_cmb(["FitToPage", "Zoom"], 660, 98, 115)
        self.txt_zoom = WF.TextBox()
        self.txt_zoom.Location = SD.Point(782, 98)
        self.txt_zoom.Size = SD.Size(55, 24)
        self.txt_zoom.Text = safe_str(self.settings.get("zoom_percentage", 100))
        self.txt_zoom.Anchor = TOP_LEFT
        self.pnl.Controls.Add(self.txt_zoom)


        add_lbl("Color Depth", 660, 132)
        self.cmb_color = add_cmb(["Color", "Gray Scale", "Black Line"], 660, 155, 130)
        add_lbl("Raster Quality", 660, 189)
        self.cmb_raster = add_cmb(["Low", "Medium", "High", "Presentation"], 660, 212, 130)
        self.chk_bind_raster = add_chk("Bind images as OLE", 660, 273, 220, False)
        # Default False (opt-in) - unchecked exports DWGs exactly as
        # before (raster images stay separate external references).
        # Checked runs the OLE-bind pass on that DWG after export.

        # Tooltip is optional but worth it since the effect isn't obvious
        # from the label alone:
        try:
            tip = WF.ToolTip()
            tip.SetToolTip(
                self.chk_bind_raster,
                "When checked, raster images (e.g. logos in the titleblock) are "
                "converted to embedded OLE objects and the external image "
                "references are removed after DWG export. When unchecked, DWGs "
                "export with raster images as normal separate references."
            )
        except:
            pass        

        add_lbl("Create Output Using", 430, 250)
        self.cmb_output = add_cmb(["Vector Processing", "Raster Processing"], 430, 273, 190)

        self.chk_links = add_chk("View links in blue", 430, 308, 220)
        self.chk_ref = add_chk("Hide ref/work planes", 430, 333, 220, True)
        self.chk_unref = add_chk("Hide unreferenced view tags", 660, 333, 240)
        self.chk_scope = add_chk("Hide scope boxes", 430, 358, 220, True)
        self.chk_crop = add_chk("Hide crop boundaries", 660, 358, 240, True)
        self.chk_mask = add_chk("Region edges mask coincident lines", 430, 383, 390)

        # Button container panel — anchored Bottom+Right so all 3 buttons
        # move together as one unit when form is resized.
        btn_panel = WF.Panel()
        btn_panel.Size = SD.Size(330, 50)
        btn_panel.BackColor = COLOR_WHITE
        btn_panel.Anchor = BOTTOM_RIGHT
        btn_panel.Location = SD.Point(970 - 330 - 10, 980 - 50 - 55)
        self.Controls.Add(btn_panel)

        self.btn_back_main = make_cancel_button("Back", 90, 50)
        self.btn_back_main.Location = SD.Point(0, 0)
        self.btn_back_main.Click += self._go_back
        btn_panel.Controls.Add(self.btn_back_main)

        self.btn_cancel_main = make_cancel_button("Cancel", 100, 50)
        self.btn_cancel_main.Location = SD.Point(100, 0)
        self.btn_cancel_main.Click += self._close_form
        btn_panel.Controls.Add(self.btn_cancel_main)

        self.btn_export_main = make_primary_button("EXPORT", 110, 50)
        self.btn_export_main.Location = SD.Point(210, 0)
        self.btn_export_main.Click += lambda s, e: self._export(s, e)
        btn_panel.Controls.Add(self.btn_export_main)

        self._apply_settings_to_ui(self.settings)
        # If Back was pressed, restore the state the user had before
        if self._restore_state:
            try:
                self._apply_ui_state(self._restore_state)
            except:
                pass
        self.Resize += self._on_resize

        self.txt_file.TextChanged += self._update_preview
        self.rad_pdf.CheckedChanged += self._update_preview
        self.rad_cad.CheckedChanged += self._update_preview
        self.rad_pdf.CheckedChanged += self._update_format_controls_enabled
        self.rad_cad.CheckedChanged += self._update_format_controls_enabled
        self.cmb_type.SelectedIndexChanged += self._update_preview
        self._update_preview(None, None)
        self._update_format_controls_enabled(None, None)

    def _reposition_buttons(self):
        # Buttons are inside a BOTTOM_RIGHT anchored panel — no manual
        # repositioning needed. Method kept for compatibility.
        pass

    def _on_resize(self, s, e):
        self._reposition_buttons()

    def _grid_data_error(self, sender, e):
        try:
            e.ThrowException = False
        except:
            pass

    def _on_dirty_state(self, s, e):
        import System.Windows.Forms as WF
        try:
            if self.grid.IsCurrentCellDirty:
                self.grid.CommitEdit(WF.DataGridViewDataErrorContexts.Commit)
        except:
            pass

    def _refresh_name_column(self, sender, e):
        try:
            cur = self.cmb_type.Text
            self.name_col.DataSource = None
            self.name_col.DataSource = get_params(cur)
        except:
            pass


    def _read_rows_for_preview(self):
        return self._collect_rows()

    def _compose_export_base_name(self, element, naming_source=None):
        """naming_source: if given, filenames are built from THIS element's
        parameters instead of `element`'s. Used when exporting from a
        throwaway duplicate view (see the crop logic in _do_export_work) —
        the duplicate's mangled temp name/parameters shouldn't leak into
        the output filename; the original view's identity should."""
        rows = self._collect_rows()
        if not rows:
            return "Project1"

        input_name = safe_separator(self.txt_file.Text)
        row_name = build_filename(naming_source or element, rows, self.cmb_type.Text)
        return safe_separator(input_name + row_name) if (input_name or row_name) else "Project1"

    def _build_preview_name(self):
        try:
            rows = self._collect_rows()
            if not rows:
                return "(no rows defined)"

            input_name = safe_separator(self.txt_file.Text)
            exts = []
            if self.rad_pdf.Checked:
                exts.append(".pdf")
            if self.rad_cad.Checked:
                exts.append(".dwg")
            ext = " / ".join(exts) if exts else ""

            sample_item = None
            for el in self.items:
                if isinstance(el, ViewSheet) or isinstance(el, View):
                    sample_item = el
                    break

            if sample_item:
                row_name = build_filename(sample_item, rows, self.cmb_type.Text)
            else:
                segments = []
                for row in rows:
                    part = row["prefix"] + (row["sample"] or row["name"]) + row["suffix"]
                    segments.append((part, safe_separator(row["separator"])))
                row_name = ""
                for i, (part, sep) in enumerate(segments):
                    row_name += part
                    if i < len(segments) - 1:
                        row_name += sep

            filename = safe_separator(input_name + row_name) if (input_name or row_name) else "Project1"
            return filename + ext
        except Exception as ex:
            return "(preview error: {})".format(str(ex))


    def _unique_export_base_name(self, folder, base_name, taken=None):
        base = safe_filename(safe_str(base_name)) or "Project1"
        taken = taken if taken is not None else set()
        taken.add(base.lower())
        existing = os.path.join(folder, base + ".pdf")
        try:
            if os.path.exists(existing):
                os.remove(existing)
        except:
            pass
        return base


    def _export_pdf_single_item(self, element, base_name, opts, used_names):
        final_base = self._unique_export_base_name(self.export_path, base_name, used_names)
        final_path = os.path.join(self.export_path, final_base + ".pdf")
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
        except:
            pass
        ids = List[ElementId]()
        ids.Add(element.Id)
        if hasattr(opts, "Combine"):
            opts.Combine = True
        if hasattr(opts, "FileName"):
            opts.FileName = final_base
        try:
            if hasattr(opts, "SetExportInBackground"):
                opts.SetExportInBackground(False)
        except:
            pass
        try:
            if hasattr(opts, "StopOnError"):
                opts.StopOnError = False
        except:
            pass
        doc.Export(self.export_path, ids, opts)

        # Give Revit's writer a brief moment before falling back to polling —
        # avoids hammering os.listdir() on the very first check.
        if os.path.exists(final_path):
            return

        before = set(
            os.path.normcase(os.path.join(self.export_path, f))
            for f in os.listdir(self.export_path)
            if f.lower().endswith(".pdf")
        )
        new_pdf = None
        for _ in range(20):          # was 60 — cut max wait from 30s to 10s
            if os.path.exists(final_path):
                return
            time.sleep(0.5)
            after = set(
                os.path.normcase(os.path.join(self.export_path, f))
                for f in os.listdir(self.export_path)
                if f.lower().endswith(".pdf")
            )
            diff = list(after - before)
            if diff:
                diff.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                new_pdf = diff[0]
                break
        if not new_pdf or not os.path.exists(new_pdf):
            raise Exception("PDF not found after export: {}".format(final_base))
        if os.path.normcase(os.path.abspath(new_pdf)) != os.path.normcase(os.path.abspath(final_path)):
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
            except:
                pass
            shutil.move(new_pdf, final_path)
            
    def _update_preview(self, s, e):

        try:
            self.lbl_preview.Text = self._build_preview_name()
        except:
            pass

    def _update_format_controls_enabled(self, s=None, e=None):
        """Grey out settings that don't apply to the currently selected
        export format(s):
        - DWG only checked → freeze all PDF-only plot settings (page size,
          zoom, orientation, color/raster quality, visibility toggles,
          Preset Tools), EXCEPT "Bind images as OLE" which is a DWG-only
          setting and must stay usable.
        - PDF only checked → freeze the DWG-only settings (Export Setup
          dropdown, its "..." button, and "Bind images as OLE").
        - Both or neither checked → nothing frozen."""
        is_pdf = self.rad_pdf.Checked
        is_cad = self.rad_cad.Checked

        pdf_only_controls = [
            #self.btn_load_preset, self.btn_save_preset,
            self.cmb_pagesize, self.cmb_place, self.cmb_orientation,
            self.cmb_zoom, self.txt_zoom,
            self.cmb_color, self.cmb_raster, self.cmb_output,
            self.chk_links, self.chk_ref, self.chk_unref,
            self.chk_scope, self.chk_crop, self.chk_mask,
        ]
        dwg_only_controls = [
            self.cmb_setup, self.btn_setup_more, self.chk_bind_raster,
        ]

        if is_cad and not is_pdf:
            for c in pdf_only_controls:
                c.Enabled = False
            for c in dwg_only_controls:
                c.Enabled = True
        elif is_pdf and not is_cad:
            for c in dwg_only_controls:
                c.Enabled = False
            for c in pdf_only_controls:
                c.Enabled = True
        else:
            for c in (pdf_only_controls + dwg_only_controls):
                c.Enabled = True

    def _add_row(self, s, e):
        self.grid.Rows.Add("", "", "", "", "")
        self._update_preview(None, None)

    def _remove_row(self, s, e):
        if self.grid.SelectedRows.Count > 0:
            self.grid.Rows.RemoveAt(self.grid.SelectedRows[0].Index)
        self._update_preview(None, None)

    def _move_up(self, s, e):
        if self.grid.SelectedRows.Count == 0:
            return
        idx = self.grid.SelectedRows[0].Index
        if idx == 0:
            return
        v1 = [self.grid.Rows[idx].Cells[i].Value for i in range(5)]
        v2 = [self.grid.Rows[idx-1].Cells[i].Value for i in range(5)]
        for i in range(5):
            self.grid.Rows[idx-1].Cells[i].Value = v1[i]
            self.grid.Rows[idx].Cells[i].Value = v2[i]
        self._update_preview(None, None)

    def _move_down(self, s, e):
        if self.grid.SelectedRows.Count == 0:
            return
        idx = self.grid.SelectedRows[0].Index
        if idx >= self.grid.Rows.Count - 1:
            return
        v1 = [self.grid.Rows[idx].Cells[i].Value for i in range(5)]
        v2 = [self.grid.Rows[idx+1].Cells[i].Value for i in range(5)]
        for i in range(5):
            self.grid.Rows[idx+1].Cells[i].Value = v1[i]
            self.grid.Rows[idx].Cells[i].Value = v2[i]
        self._update_preview(None, None)

    def _apply_settings_to_ui(self, settings):
        try:
            # Compatibility alias. The real implementation is _apply_ui_state.
            self._apply_ui_state(settings or {})
        except:
            pass

    def _set_combo_value(self, combo, value):
        try:
            value = safe_str(value)
            if not value:
                return
            if not any(safe_str(item) == value for item in combo.Items):
                combo.Items.Add(value)
            combo.SelectedItem = value
        except:
            try:
                combo.Text = value
            except:
                pass

    def _open_export_setup_dialog(self, s, e):
        import System.Windows.Forms as WF
        try:
            dlg = DWGDXFExportSetupDialog(self.cmb_setup.Text)
            if dlg.ShowDialog(self) == WF.DialogResult.OK:
                self._set_combo_value(self.cmb_setup, dlg.SelectedSetup)
        except:
            pass

    def _collect_rows(self):
        rows = []
        for r in self.grid.Rows:
            name_val = safe_str(r.Cells[0].Value).strip()
            if not name_val:
                continue
            rows.append({
                "name": name_val,
                "prefix": safe_str(r.Cells[1].Value).strip(),
                "sample": safe_str(r.Cells[2].Value).strip(),
                "suffix": safe_str(r.Cells[3].Value).strip(),
                "separator": safe_str(r.Cells[4].Value)
            })
        return rows

    def _capture_ui_state(self):
        return {
            "parameter_type": self.cmb_type.Text,
            "file_name": self.txt_file.Text,
            "combine": self.chk_combine.Checked,
            "zoom_type": self.cmb_zoom.Text,
            "zoom_percentage": self.txt_zoom.Text,
            "color_depth": self.cmb_color.Text,
            "raster_quality": self.cmb_raster.Text,
            "paper_size": self.cmb_pagesize.Text,
            "paper_placement": self.cmb_place.Text,
            "orientation": self.cmb_orientation.Text,
            "dwg_setup": self.cmb_setup.Text,
            "last_folder": self.export_path,
            "rad_pdf": self.rad_pdf.Checked,
            "rad_cad": self.rad_cad.Checked,
            "export_range": ("current_window" if self.rad_current_window.Checked else
                             "visible_portion" if self.rad_visible_portion.Checked else
                             "selected_views"),
            "rows": self._collect_rows(),
            "create_output_mode": self.cmb_output.Text,
            "chk_raster": self.cmb_output.Text == "Raster Processing",
            "chk_links": self.chk_links.Checked,
            "chk_ref": self.chk_ref.Checked,
            "chk_unref": self.chk_unref.Checked,
            "chk_scope": self.chk_scope.Checked,
            "chk_crop": self.chk_crop.Checked,
            "chk_mask": self.chk_mask.Checked,
            "chk_bind_raster": self.chk_bind_raster.Checked,
        }

    def _apply_ui_state(self, state):
        if not isinstance(state, dict):
            return

        self._set_combo_value(self.cmb_type, state.get("parameter_type", self.cmb_type.Text))
        self.txt_file.Text = safe_str(state.get("file_name", self.txt_file.Text))
        self.chk_combine.Checked = bool(state.get("combine", self.chk_combine.Checked))
        self._set_combo_value(self.cmb_zoom, state.get("zoom_type", self.cmb_zoom.Text))
        self.txt_zoom.Text = safe_str(state.get("zoom_percentage", self.txt_zoom.Text))
        self._set_combo_value(self.cmb_color, state.get("color_depth", self.cmb_color.Text))
        self._set_combo_value(self.cmb_raster, state.get("raster_quality", self.cmb_raster.Text))
        self._set_combo_value(self.cmb_pagesize, state.get("paper_size", self.cmb_pagesize.Text))
        self._set_combo_value(self.cmb_place, state.get("paper_placement", self.cmb_place.Text))
        self._set_combo_value(self.cmb_orientation, state.get("orientation", self.cmb_orientation.Text))
        self._set_combo_value(self.cmb_setup, state.get("dwg_setup", self.cmb_setup.Text))
        self.export_path = safe_str(state.get("last_folder", self.export_path))
        self.lbl_folder.Text = self.export_path
        self.rad_pdf.Checked = bool(state.get("rad_pdf", self.rad_pdf.Checked))
        self.rad_cad.Checked = bool(state.get("rad_cad", self.rad_cad.Checked))

        export_range = safe_str(state.get("export_range", "")).strip()
        if export_range == "current_window":
            self.rad_current_window.Checked = True
        elif export_range == "visible_portion":
            self.rad_visible_portion.Checked = True
        elif export_range == "selected_views":
            self.rad_selected_views.Checked = True
        # else: no saved value — leave whatever was already checked
        # (the hardcoded "Selected views/sheets" default from __init__)

        create_output_mode = safe_str(state.get("create_output_mode", ""))
        if not create_output_mode:
            create_output_mode = "Raster Processing" if bool(state.get("chk_raster", False)) else "Vector Processing"
        self._set_combo_value(self.cmb_output, create_output_mode)

        self.chk_links.Checked = bool(state.get("chk_links", self.chk_links.Checked))
        self.chk_ref.Checked = bool(state.get("chk_ref", self.chk_ref.Checked))
        self.chk_unref.Checked = bool(state.get("chk_unref", self.chk_unref.Checked))
        self.chk_scope.Checked = bool(state.get("chk_scope", self.chk_scope.Checked))
        self.chk_crop.Checked = bool(state.get("chk_crop", self.chk_crop.Checked))
        self.chk_mask.Checked = bool(state.get("chk_mask", self.chk_mask.Checked))
        self.chk_bind_raster.Checked = bool(state.get("chk_bind_raster", self.chk_bind_raster.Checked))

        rows = state.get("rows", None)
        if isinstance(rows, list):
            self.grid.Rows.Clear()
            for row in rows:
                self.grid.Rows.Add(
                    safe_str(row.get("name", "")),
                    safe_str(row.get("prefix", "")),
                    safe_str(row.get("sample", "")),
                    safe_str(row.get("suffix", "")),
                    safe_str(row.get("separator", ""))
                )


    def _save_settings(self, s, e):
        """Commits any pending grid edits and stores the current UI state
        via AppDomain storage (survives closing/reopening the tool for the
        rest of this Revit session) — without writing export_settings.xml
        to disk. Save Preset / Load Preset remain the way to persist
        settings permanently across Revit restarts."""
        import System.Windows.Forms as WF
        self.grid.EndEdit()
        self.grid.CurrentCell = None
        data = self._capture_ui_state()
        data["rows"] = self._collect_rows()
        _set_cached_settings(data)
        WF.MessageBox.Show("Settings Saved", "Export Tool")

    def _save_export_preset(self, s, e):
        import System.Windows.Forms as WF
        path = pick_save_xml("Save export preset", _get_export_preset_dir(), "export_preset.xml", self)
        if not path:
            return
        try:
            data = self._capture_ui_state()
            xml_save_file(path, data, root_name="preset")
            WF.MessageBox.Show("Export preset saved.", "Preset")
        except Exception as ex:
            WF.MessageBox.Show(str(ex), "Save Error")

    def _load_export_preset(self, s, e):
        import System.Windows.Forms as WF
        path = pick_open_xml("Load export preset", _get_export_preset_dir(), self)
        if not path:
            return
        try:
            data = xml_load_file(path)
            if isinstance(data, dict):
                self._apply_ui_state(data)
                self._update_preview(None, None)
        except Exception as ex:
            WF.MessageBox.Show(str(ex), "Load Error")

    def _choose_folder(self, s, e):
        import System.Windows.Forms as WF
        dlg = WF.FolderBrowserDialog()
        if dlg.ShowDialog() == WF.DialogResult.OK:
            self.export_path = dlg.SelectedPath
            self.lbl_folder.Text = self.export_path

    def _go_back(self, s, e):
        import System.Windows.Forms as WF
        try:
            # Capture current UI state BEFORE closing so nothing is lost
            self.grid.EndEdit()
            self.grid.CurrentCell = None
            saved_state = self._capture_ui_state()
            current_items = list(self.items)
            self.Close()
            dlg = SheetViewSelectorDialog(preselected=current_items)
            if dlg.ShowDialog() != WF.DialogResult.OK:
                return
            new_selection = dlg.SelectedElements
            if not new_selection:
                TaskDialog.Show("Export", "No sheets or views selected.")
                return
            global __export_form__
            step_d = MainExportDialog(new_selection)
            # Restore state so user sees exactly what they had before
            step_d._restore_state = saved_state
            __export_form__ = step_d
            step_d.Show()
        except Exception as ex:
            WF.MessageBox.Show(str(ex), "Back Error")

    def _close_form(self, s, e):
        self.Close()

    def _get_visible_extents(self, view):
        """Return (local_extents, world_extents) for the current zoom window
        of the open UIView showing `view`, or (None, None) if it can't be
        determined.

        - local_extents: (minX,minY,maxX,maxY) in the view's own
          CropBox.Transform coordinate system. Used directly for
          CropBox.Min/Max.
        - world_extents: (minX,minY,minZ,maxX,maxY,maxZ) — a WORLD-space
          axis-aligned box that safely encloses the local rectangle,
          extruded through view depth. Used to build the Outline for
          BoundingBoxIntersectsFilter when testing element positions.

        Earlier this took the two GetZoomCorners() world points and used
        their raw world X/Y directly as "world_extents". That only happens
        to work for plan views, where the view's horizontal axes roughly
        align with world X/Y and depth is world Z. For a SECTION or
        ELEVATION, the view's "up" is world Z, not X/Y — the two zoom
        corners can share the same world X (or Y), making that rectangle
        degenerate (zero width) and triggering "Could not determine the
        visible extents." Building world_extents by transforming the
        already-correct local rectangle through the view's own transform
        works for any view orientation, since it goes through the same
        Transform.Inverse/Transform pair consistently rather than assuming
        which world axes vary.
        """
        try:
            for uiview in uidoc.GetOpenUIViews():
                if uiview.ViewId == view.Id:
                    corners = uiview.GetZoomCorners()
                    if not corners or len(corners) != 2:
                        return None, None
                    p0, p1 = corners[0], corners[1]

                    try:
                        crop_box = view.CropBox
                        transform = crop_box.Transform
                        inv = transform.Inverse
                        lp0 = inv.OfPoint(p0)
                        lp1 = inv.OfPoint(p1)
                    except:
                        return None, None

                    lminX, lmaxX = min(lp0.X, lp1.X), max(lp0.X, lp1.X)
                    lminY, lmaxY = min(lp0.Y, lp1.Y), max(lp0.Y, lp1.Y)
                    if lmaxX <= lminX or lmaxY <= lminY:
                        return None, None
                    local_extents = (lminX, lminY, lmaxX, lmaxY)

                    # Enclose the local rectangle in WORLD space by
                    # transforming its 4 XY corners at both a near and far
                    # local Z (covering any view depth) through the view's
                    # transform, then taking the world axis-aligned min/max
                    # over all 8 resulting points.
                    BIG = 1000000.0  # feet — effectively "no depth limit"
                    world_pts = []
                    for lx in (lminX, lmaxX):
                        for ly in (lminY, lmaxY):
                            for lz in (-BIG, BIG):
                                world_pts.append(transform.OfPoint(XYZ(lx, ly, lz)))
                    wxs = [pt.X for pt in world_pts]
                    wys = [pt.Y for pt in world_pts]
                    wzs = [pt.Z for pt in world_pts]
                    world_extents = (min(wxs), min(wys), min(wzs),
                                      max(wxs), max(wys), max(wzs))

                    return local_extents, world_extents
        except:
            pass
        return None, None

    def _configure_pdf_options(self, opts, sheet=None):
        # ── Visibility flags ─────────────────────────────────────────────────
        try:
            if hasattr(opts, "HideCropBoundaries"):
                opts.HideCropBoundaries = self.chk_crop.Checked
            if hasattr(opts, "HideReferencePlane"):
                opts.HideReferencePlane = self.chk_ref.Checked
            if hasattr(opts, "HideUnreferencedViewTags"):
                opts.HideUnreferencedViewTags = self.chk_unref.Checked
            if hasattr(opts, "HideScopeBoxes"):
                opts.HideScopeBoxes = self.chk_scope.Checked
            if hasattr(opts, "MaskCoincidentLines"):
                opts.MaskCoincidentLines = self.chk_mask.Checked
            if hasattr(opts, "ViewLinksInBlue"):
                opts.ViewLinksInBlue = self.chk_links.Checked
            if hasattr(opts, "AlwaysUseRaster"):
                opts.AlwaysUseRaster = (safe_str(self.cmb_output.Text) == "Raster Processing")
        except:
            pass

        # ── PaperPlacement (PaperPlacementType: Center=0, LowerLeft=1) ───────
        try:
            if hasattr(opts, "PaperPlacement"):
                from Autodesk.Revit.DB import PaperPlacementType
                opts.PaperPlacement = (PaperPlacementType.Center
                                       if self.cmb_place.Text == "Center"
                                       else PaperPlacementType.LowerLeft)
        except:
            try:
                if hasattr(opts, "PaperPlacement"):
                    v = 0 if self.cmb_place.Text == "Center" else 1
                    opts.PaperPlacement = System.Enum.ToObject(type(opts.PaperPlacement), v)
            except:
                pass

        # ── ColorDepth (ColorDepthType: BlackLine=0, GrayScale=1, Color=2) ───
        try:
            if hasattr(opts, "ColorDepth"):
                from Autodesk.Revit.DB import ColorDepthType
                c = safe_str(self.cmb_color.Text).strip()
                if c == "Gray Scale":
                    opts.ColorDepth = ColorDepthType.GrayScale
                elif c == "Black Line":
                    opts.ColorDepth = ColorDepthType.BlackLine
                else:
                    opts.ColorDepth = ColorDepthType.Color
        except:
            try:
                if hasattr(opts, "ColorDepth"):
                    c = safe_str(self.cmb_color.Text).strip()
                    v = 1 if c == "Gray Scale" else 0 if c == "Black Line" else 2
                    opts.ColorDepth = System.Enum.ToObject(type(opts.ColorDepth), v)
            except:
                pass

        # ── RasterQuality (RasterQualityType: Low=0,Medium=1,High=2,Pres=3) ──
        try:
            if hasattr(opts, "RasterQuality"):
                from Autodesk.Revit.DB import RasterQualityType
                q = safe_str(self.cmb_raster.Text)
                opts.RasterQuality = {"Low": RasterQualityType.Low,
                    "Medium": RasterQualityType.Medium,
                    "Presentation": RasterQualityType.Presentation
                }.get(q, RasterQualityType.High)
        except:
            try:
                if hasattr(opts, "RasterQuality"):
                    q = safe_str(self.cmb_raster.Text)
                    v = {"Low":0,"Medium":1,"High":2,"Presentation":3}.get(q, 2)
                    opts.RasterQuality = System.Enum.ToObject(type(opts.RasterQuality), v)
            except:
                pass

        # ── PaperFormat (MUST be non-Default for Zoom/Orient to work) ────────
        # ExportPaperFormat uses UNDERSCORES: ISO_A0 not ISOA0
        # Ref: revitapidocs.com/2025 ISO_A0=10,ISO_A1=9,...ANSI_A=1,ARCH_E=19
        paper_fmt_set = False
        try:
            if hasattr(opts, "PaperFormat"):
                from Autodesk.Revit.DB import ExportPaperFormat
                st = safe_str(self.cmb_pagesize.Text).strip()
                for prefix, member, ival in [
                    ("ISO A0","ISO_A0",10),("ISO A1","ISO_A1",9),
                    ("ISO A2","ISO_A2", 8),("ISO A3","ISO_A3",7),
                    ("ISO A4","ISO_A4", 6),("ISO B4","ISO_B4",11),
                    ("ISO B3","ISO_B3",12),("ISO B2","ISO_B2",13),
                    ("ISO B1","ISO_B1",14),("ANSI A","ANSI_A", 1),
                    ("ANSI B","ANSI_B", 2),("ANSI C","ANSI_C", 3),
                    ("ANSI D","ANSI_D", 4),("ANSI E","ANSI_E", 5),
                    ("ARCH A","ARCH_A",15),("ARCH B","ARCH_B",16),
                    ("ARCH C","ARCH_C",17),("ARCH D","ARCH_D",18),
                    ("ARCH E1","ARCH_E1",20),("ARCH E2","ARCH_E2",21),
                    ("ARCH E3","ARCH_E3",22),("ARCH E","ARCH_E",19),
                ]:
                    if st.startswith(prefix):
                        try:
                            opts.PaperFormat = getattr(ExportPaperFormat, member)
                            paper_fmt_set = True
                        except:
                            try:
                                opts.PaperFormat = System.Enum.ToObject(
                                    type(opts.PaperFormat), ival)
                                paper_fmt_set = True
                            except:
                                pass
                        break
        except:
            pass

        # ── ZoomType + ZoomPercentage (IGNORED when PaperFormat=Default) ─────
        # ZoomType: FitToPage=0, Zoom=1
        # NOTE: Zoom=100% does NOT mean "fill the page" — it means draw the
        # sheet at its original scale on the chosen paper. A3 sheet on A0 paper
        # at 100% will appear small. Use FitToPage to fill the page correctly.
        try:
            if hasattr(opts, "ZoomType") and paper_fmt_set:
                zi = 0 if safe_str(self.cmb_zoom.Text).strip() == "FitToPage" else 1
                opts.ZoomType = System.Enum.ToObject(type(opts.ZoomType), zi)
                if zi == 1 and hasattr(opts, "ZoomPercentage"):
                    try:
                        opts.ZoomPercentage = max(10, min(int(float(self.txt_zoom.Text)), 100))
                    except:
                        opts.ZoomPercentage = 100
        except:
            pass

        # ── PaperOrientation (IGNORED when PaperFormat=Default) ──────────────
        # PageOrientationType: Portrait=1, Landscape=2
        try:
            if hasattr(opts, "PaperOrientation") and paper_fmt_set:
                from Autodesk.Revit.DB import PageOrientationType
                u = safe_str(self.cmb_orientation.Text).strip()
                if u == "Portrait":
                    opts.PaperOrientation = PageOrientationType.Portrait
                elif u == "Landscape":
                    opts.PaperOrientation = PageOrientationType.Landscape
        except:
            try:
                if hasattr(opts, "PaperOrientation") and paper_fmt_set:
                    u = safe_str(self.cmb_orientation.Text).strip()
                    v = 1 if u == "Portrait" else 2 if u == "Landscape" else None
                    if v:
                        opts.PaperOrientation = System.Enum.ToObject(
                            type(opts.PaperOrientation), v)
            except:
                pass


    def _export(self, s, e):
        """Click handler — runs OUTSIDE Revit's API context (modeless form),
        so it must not touch the model directly. It only validates UI state
        and commits pending grid edits, then hands off to _do_export_work()
        via the ExternalEvent, which Revit calls back into a valid context."""
        import System.Windows.Forms as WF
        if not self.export_path:
            WF.MessageBox.Show("Please choose an export folder first.")
            return
        if not self.rad_pdf.Checked and not self.rad_cad.Checked:
            WF.MessageBox.Show(
                "Please select at least one export format (PDF and/or DWG).",
                "Export Format")
            return
        try:
            self.grid.EndEdit()
            self.grid.CurrentCell = None
        except:
            pass
        self._export_event.Raise()

    def _do_export_work(self):
        """The actual export routine. Only ever called from
        _ExportEventHandler.Execute(), i.e. inside a valid Revit API
        context — so Transaction() (needed for the temporary 'visible
        portion' crop) is legal here."""
        import System.Windows.Forms as WF
        try:
            rows = self._collect_rows()
            param_type = self.cmb_type.Text
            is_pdf = self.rad_pdf.Checked
            is_cad = self.rad_cad.Checked

            if not is_pdf and not is_cad:
                WF.MessageBox.Show(
                    "Please select at least one export format (PDF and/or DWG).",
                    "Export Format")
                return

            # ── Build export list based on Export Range selection ─────────
            use_visible_crop = False
            if self.rad_current_window.Checked or self.rad_visible_portion.Checked:
                # Current window / Visible portion: bypass sheet selection,
                # use the active view in Revit
                try:
                    active_view = uidoc.ActiveView
                    if active_view is None:
                        WF.MessageBox.Show(
                            "No active view found in Revit.\n"
                            "Please open a sheet or view and try again.", "Export")
                        return
                    # Validate: Revit only exports printable views and sheets
                    # Non-exportable types: templates, schedules, system browser, etc.
                    not_exportable = False
                    try:
                        if getattr(active_view, "IsTemplate", False):
                            not_exportable = True
                        elif hasattr(active_view, "ViewType"):
                            bad_types = [
                                ViewType.Schedule,
                                ViewType.ProjectBrowser,
                                ViewType.SystemBrowser,
                                ViewType.Undefined,
                                ViewType.Internal,
                            ]
                            if active_view.ViewType in bad_types:
                                not_exportable = True
                    except:
                        pass
                    if not_exportable:
                        WF.MessageBox.Show(
                            "The current active view cannot be exported.\n"
                            "Please open a Sheet, Plan, Section, Elevation, or 3D View in Revit first.",
                            "Export")
                        return
                    selected = [active_view]
                    use_visible_crop = self.rad_visible_portion.Checked

                    # PDFExportOptions/doc.Export do NOT honor CropBox on a
                    # ViewSheet — only on model views (plan/section/elev/3D).
                    # Setting it anyway silently exports the full sheet, so
                    # warn up front instead of surprising the user.
                    if use_visible_crop and isinstance(active_view, ViewSheet):
                        WF.MessageBox.Show(
                            "\"Visible portion of current window\" only works on "
                            "model views (Floor Plan, Section, Elevation, 3D) — "
                            "Revit does not support cropping a Sheet on export.\n\n"
                            "The full sheet will be exported instead.\n\n"
                            "To export just a portion, open the underlying view "
                            "(not the sheet) and try again from there.",
                            "Export")
                        use_visible_crop = False
                except Exception as ex:
                    WF.MessageBox.Show(
                        "Could not get active view:\n{}".format(str(ex)), "Export")
                    return
            else:
                # Selected views/sheets: use the items passed from the selector
                selected = []
                for element in self.items:
                    if isinstance(element, ViewSheet) or isinstance(element, View):
                        selected.append(element)

            if not selected:
                WF.MessageBox.Show("No valid sheets or views found to export.", "Export")
                return

            # ── Pre-check: warn if any target output file is already open
            # elsewhere (e.g. a PDF viewer or DWG program has it locked).
            # Checks both formats independently now — if only PDF is
            # checked, only .pdf targets are checked, and vice versa.
            locked_files = []
            try:
                if is_pdf:
                    if self.chk_combine.Checked:
                        base_name = self._compose_export_base_name(
                            selected[0]) if selected else "Project1"
                        target = os.path.join(
                            self.export_path, safe_filename(base_name) + ".pdf")
                        if is_file_locked(target):
                            locked_files.append(target)
                    else:
                        for element in selected:
                            base_name = self._compose_export_base_name(element)
                            target = os.path.join(
                                self.export_path, safe_filename(base_name) + ".pdf")
                            if is_file_locked(target):
                                locked_files.append(target)
                if is_cad:
                    for element in selected:
                        row_name = build_filename(element, rows, param_type)
                        filename = safe_separator(
                            safe_separator(self.txt_file.Text) + row_name
                        ) if (self.txt_file.Text or row_name) else "Project1"
                        target = os.path.join(self.export_path, filename + ".dwg")
                        if is_file_locked(target):
                            locked_files.append(target)
            except:
                locked_files = []  # never let the pre-check itself block export

            if locked_files:
                preview = "\n".join(locked_files[:10])
                more = ("" if len(locked_files) <= 10 else
                        "\n...and {} more".format(len(locked_files) - 10))
                WF.MessageBox.Show(
                    "The following file(s) appear to be open in another "
                    "program and can't be overwritten:\n\n{}{}\n\n"
                    "Please close them, then try exporting again.".format(
                        preview, more),
                    "File In Use")
                return

            # ── Temporary crop for "Visible portion of current window" ────────
            # (unchanged from before)
            temp_crop_view = None      # the throwaway duplicate, if created
            crop_naming_source = None  # original view — used for filenames
            if use_visible_crop:
                candidate_view = selected[0]
                local_extents, world_extents = self._get_visible_extents(candidate_view)
                if local_extents and world_extents:
                    lminX, lminY, lmaxX, lmaxY = local_extents
                    wminX, wminY, wminZ, wmaxX, wmaxY, wmaxZ = world_extents

                    t_crop = None
                    try:
                        t_crop = Transaction(doc, "Export Tool - Temp Crop View")
                        t_crop.Start()

                        new_view_id = candidate_view.Duplicate(ViewDuplicateOption.WithDetailing)
                        dup_view = doc.GetElement(new_view_id)
                        try:
                            dup_view.Name = "{}_TEMP_EXPORT_{}".format(
                                safe_filename(candidate_view.Name)[:180], int(time.time()))
                        except:
                            pass

                        bbox = dup_view.CropBox
                        bbox.Min = XYZ(lminX, lminY, bbox.Min.Z)
                        bbox.Max = XYZ(lmaxX, lmaxY, bbox.Max.Z)
                        dup_view.CropBox = bbox
                        dup_view.CropBoxActive = True
                        try:
                            dup_view.CropBoxVisible = False
                        except:
                            pass

                        margin = 0.05  # feet
                        to_hide = List[ElementId]()
                        outline = Outline(
                            XYZ(wminX - margin, wminY - margin, wminZ - margin),
                            XYZ(wmaxX + margin, wmaxY + margin, wmaxZ + margin))
                        bbox_filter = BoundingBoxIntersectsFilter(outline)

                        all_ids_full = FilteredElementCollector(doc, dup_view.Id) \
                            .WhereElementIsNotElementType().ToElementIds()
                        all_ids = List[ElementId]()
                        for eid in all_ids_full:
                            el = doc.GetElement(eid)
                            if el is None:
                                continue
                            if isinstance(el, RevitLinkInstance):
                                continue
                            try:
                                cat = el.Category
                                if cat is not None and cat.CategoryType == CategoryType.Model:
                                    continue
                            except:
                                pass
                            all_ids.Add(eid)

                        visible_ids = FilteredElementCollector(doc, dup_view.Id) \
                            .WhereElementIsNotElementType() \
                            .WherePasses(bbox_filter).ToElementIds()
                        visible_id_set = set(get_id_value(i) for i in visible_ids)

                        for eid in all_ids:
                            if get_id_value(eid) in visible_id_set:
                                continue
                            el = doc.GetElement(eid)
                            if el is None:
                                continue
                            try:
                                if not el.CanBeHidden(dup_view):
                                    continue
                                ebbox = el.get_BoundingBox(dup_view)
                                if ebbox is None:
                                    continue
                                to_hide.Add(eid)
                            except:
                                continue
                        if to_hide.Count > 0:
                            dup_view.HideElements(to_hide)

                        t_crop.Commit()
                        temp_crop_view = dup_view
                        crop_naming_source = candidate_view
                        selected = [dup_view]
                    except Exception as crop_ex:
                        try:
                            if t_crop is not None:
                                t_crop.RollBack()
                        except:
                            pass
                        temp_crop_view = None
                        crop_naming_source = None
                        WF.MessageBox.Show(
                            "Could not apply the visible-portion crop:\n{}\n\n"
                            "Exporting the full view instead.".format(str(crop_ex)),
                            "Export")
                else:
                    WF.MessageBox.Show(
                        "Could not determine the visible extents of the current window "
                        "(view may not be open, or is a schedule/legend).\n"
                        "Exporting the full view instead.", "Export")

            total   = len(selected)
            errors  = []   # collect failures — never abort the whole batch
            dwg_paths_to_bind = []   # collected here, bound as ONE batch
                                     # after every export finishes - see
                                     # the OLE-bind section near the end
                                     # of this method for why

            # ── Progress label shown on the form during export ────────────────
            lbl_progress = WF.Label()
            lbl_progress.Font = FONT_MEDIUM
            lbl_progress.ForeColor = SD.Color.FromArgb(255, 0, 0)
            lbl_progress.AutoSize = False
            lbl_progress.Size = SD.Size(600, 50)
            lbl_progress.Location = SD.Point(20, self.ClientSize.Height - 55)
            lbl_progress.Anchor = BOTTOM_RIGHT
            lbl_progress.Text = "Preparing export..."
            self.Controls.Add(lbl_progress)

            def _progress(current, name):
                try:
                    lbl_progress.Text = "Exporting {}/{}: {}".format(
                        current, total, name[:60])
                    lbl_progress.Refresh()
                    WF.Application.DoEvents()   # keep UI responsive
                except:
                    pass

            try:
                # ── PDF phase — runs if PDF is checked, independent of DWG ──
                if is_pdf:
                    if not HAS_NATIVE_PDF:
                        WF.MessageBox.Show(
                            "PDFExportOptions is not available in this Revit context.",
                            "PDF Export")
                    elif self.chk_combine.Checked:
                        # ── Combined: all sheets → one PDF ────────────────────────
                        _progress(1, "PDF: Combined PDF")
                        try:
                            opts = PDFExportOptions()
                            _fs = next((x for x in selected if isinstance(x, ViewSheet)),
                                       selected[0])
                            self._configure_pdf_options(opts, sheet=_fs)
                            ids = List[ElementId]()
                            for element in selected:
                                ids.Add(element.Id)
                            base_name = self._compose_export_base_name(
                                selected[0], naming_source=crop_naming_source) if selected else "Project1"
                            if hasattr(opts, "Combine"):
                                opts.Combine = True
                            if hasattr(opts, "FileName"):
                                opts.FileName = base_name
                            try:
                                if hasattr(opts, "SetExportInBackground"):
                                    opts.SetExportInBackground(False)
                            except:
                                pass
                            try:
                                if hasattr(opts, "StopOnError"):
                                    opts.StopOnError = False
                            except:
                                pass
                            doc.Export(self.export_path, ids, opts)
                        except Exception as ex:
                            errors.append("Combined PDF: {}".format(str(ex)))
                    else:
                        # ── Individual: one PDF per sheet, continue on failure ─────
                        used_names = set()
                        opts = PDFExportOptions()
                        self._configure_pdf_options(opts)
                        for idx_e, element in enumerate(selected, 1):
                            _label_src = crop_naming_source or element
                            sheet_label = safe_str(getattr(_label_src, "SheetNumber",
                                                   getattr(_label_src, "Name", "?")))
                            _progress(idx_e, "PDF: " + sheet_label)
                            try:
                                base_name = self._compose_export_base_name(
                                    element, naming_source=crop_naming_source)
                                self._export_pdf_single_item(
                                    element, base_name, opts, used_names)
                            except Exception as ex:
                                errors.append("PDF [{}]: {}".format(sheet_label, str(ex)))
                                continue   # keep going — do not abort batch

                # ── DWG phase — runs if DWG is checked, independent of PDF ──
                if is_cad:
                    dwg_options = get_selected_dwg_options(self.cmb_setup.Text)
                    try:
                        if hasattr(dwg_options, "MergedViews"):
                            dwg_options.MergedViews = True
                    except:
                        pass

                    dwg_targets = selected
                    if not dwg_targets:
                        WF.MessageBox.Show("No sheets or views found to export as DWG.", "Export")
                    else:
                        for idx_e, element in enumerate(dwg_targets, 1):
                            _label_src = crop_naming_source or element
                            sheet_label = safe_str(getattr(_label_src, "SheetNumber",
                                                   getattr(_label_src, "Name", "?")))
                            _progress(idx_e, "DWG: " + sheet_label)
                            row_name = build_filename(crop_naming_source or element, rows, param_type)
                            filename = safe_separator(
                                safe_separator(self.txt_file.Text) + row_name
                            ) if (self.txt_file.Text or row_name) else "Project1"
                            ids = List[ElementId]()
                            ids.Add(element.Id)
                            try:
                                doc.Export(self.export_path, filename, ids, dwg_options)
                            except Exception as ex:
                                errors.append("DWG [{}]: {}".format(filename, str(ex)))
                                continue   # keep going — do not abort batch

                            if self.chk_bind_raster.Checked:
                                dwg_full_path = os.path.join(self.export_path, filename + ".dwg")
                                if os.path.exists(dwg_full_path):
                                    # Collected only - NOT processed here. Binding
                                    # every file inline was what made each Revit
                                    # export wait on a full AutoCAD open/scan/
                                    # save/close cycle. All paths are now bound
                                    # together as one batch after the export loop
                                    # finishes (see near the end of this method) -
                                    # AutoCAD opens once, reuses the title block
                                    # name it learns from the first file that has
                                    # one, and runs in the background while the
                                    # Export Complete dialog is already showing.
                                    dwg_paths_to_bind.append(dwg_full_path)

                            pcp = os.path.join(self.export_path, filename + ".pcp")
                            try:
                                if os.path.exists(pcp):
                                    os.remove(pcp)
                            except:
                                pass
            finally:
                # ── Clean up the throwaway duplicate view, if one was
                # created — even if export failed. ───────────────────────
                if temp_crop_view is not None:
                    t_cleanup = None
                    try:
                        t_cleanup = Transaction(doc, "Export Tool - Remove Temp View")
                        t_cleanup.Start()
                        doc.Delete(temp_crop_view.Id)
                        t_cleanup.Commit()
                    except:
                        try:
                            if t_cleanup is not None:
                                t_cleanup.RollBack()
                        except:
                            pass


            try:
                data = self._capture_ui_state()
                data["rows"] = rows
                _set_cached_settings(data)
            except:
                pass

            # Bind raster->OLE for the WHOLE batch at once, AFTER every
            # Revit export above has finished - one AutoCAD session, one
            # title-block name learned and reused across files, instead
            # of a separate open/scan/save/close cycle per file. Runs
            # synchronously here (same as the original per-file version
            # did) - AutoCAD needs to stay on this thread for PASTECLIP's
            # insertion-point prompt to be answered reliably; a background
            # thread was tried and broke that.
            _run_ole_bind_batch(dwg_paths_to_bind, errors)
            shutdown_autocad()   # closes the shared AutoCAD session once

            export_path_done = self.export_path
            prior_items = list(self.items)
            self.Close()
            _show_export_done_dialog(export_path_done, errors, total, prior_items)

        except Exception as ex:
            WF.MessageBox.Show(str(ex), "Export Error")


# ── IronPython scope fix (2nd pass) — captures class definitions ─────────────
# First injection ran before classes existed. This one runs after all classes
# are defined, ensuring they are also visible in all callback scopes.
try:
    import __builtin__ as _builtins_mod2
except ImportError:
    import builtins as _builtins_mod2

_this_module_globals2 = globals().copy()
for _k2, _v2 in _this_module_globals2.items():
    if not _k2.startswith('__'):
        try:
            setattr(_builtins_mod2, _k2, _v2)
        except:
            pass
del _this_module_globals2, _k2, _v2, _builtins_mod2
# ─────────────────────────────────────────────────────────────────────────────



def run_export_workflow():
    preselected = get_selected_exportables()

    step_c = SheetViewSelectorDialog(preselected=preselected)
    if step_c.ShowDialog() != WF.DialogResult.OK:
        return
    final_selection = step_c.SelectedElements

    if not final_selection:
        TaskDialog.Show("Export", "No sheets or views selected. Export cancelled.")
        return

    global __export_form__
    step_d = MainExportDialog(final_selection)
    __export_form__ = step_d
    step_d.Show()

# =========================================================


run_export_workflow()