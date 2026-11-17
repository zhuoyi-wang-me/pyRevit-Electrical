# -*- coding: utf-8 -*-
"""Main WPF UI controller for the pyRevit Export Manager (Revit 2025.4).

MODELESS ARCHITECTURE
---------------------
* The window is shown with window.show() (non-blocking), so the user can
  keep working in Revit -- open a sheet, zoom a view -- while it is open.
* Anything that WRITES to the model or EXPORTS (Document.Export, the
  temporary crop Transaction) is queued through revit_event.RevitDispatcher
  (an ExternalEvent) and runs inside a valid Revit API context.
* Typing in Search / changing filters never calls the Revit API: every row
  is indexed once when it is loaded (view_search.py) and search works on
  those cached strings.
* The window is bound to ONE document.  If the user switches to another
  model, "Current window" exports refuse to run instead of exporting the
  wrong model; if the bound model is closed, the window closes itself.
* "Refresh from model" re-reads sheets/views (through the ExternalEvent)
  and keeps the user's ticks.

Required XAML controls:
  rbSheets, rbViews, txtSearch, chkSelectAll, txtCount, dgItems,
  chkPDF, chkDWG, cmbDWGSetup, txtFolder, btnBrowse, btnExport,
  lstAvailableParams, lstSelectedParams, dgSeparators, txtPreview,
  txtFormatPreview, btnAddParam, btnRemoveParam, btnMoveUp, btnMoveDown,
  btnSaveFormat, btnLoadFormat.
Everything else is optional and skipped when absent.
"""

import json
import os

from pyrevit import forms
from Autodesk.Revit.DB import FilteredElementCollector, View, ViewSheet, ViewType

import System
from System import TimeSpan
from System.Windows import RoutedEventHandler, SystemParameters, Visibility
from System.Windows.Controls.Primitives import ButtonBase
from System.Windows.Input import Cursors, Key
from System.Windows.Threading import DispatcherTimer

import helpers
import cancel_watch
import filename_builder
import format_manager
import revit_event
import selection_sets
import sheet_filter_ui
import view_search
import export_dwg
import export_pdf
import export_print


WINDOW_KEY = "pyRevit.ExportManager.Window"
WINDOW_DOC_KEY = "pyRevit.ExportManager.Document"

# View types listed in Views mode.  Add ViewType.AreaPlan, ViewType.Detail,
# ViewType.ThreeD, ViewType.EngineeringPlan ... here if you need them.
VIEW_TYPES_TO_LIST = [
    ViewType.FloorPlan,
    ViewType.CeilingPlan,
    ViewType.Section,
    ViewType.Elevation,
    ViewType.DraftingView,
    ViewType.Legend
]

SEARCH_DELAY_MS = 220        # debounce: filter after the user stops typing
FILENAME_DELAY_MS = 150      # coalesce bursts of filename-sequence changes

# Wizard steps (tabMain.SelectedIndex).  A validation error names the step
# that has to be fixed, and the window jumps there.
TAB_SELECTION = 0
TAB_FORMAT = 1
TAB_CREATE = 2         # ProSheets layout: Selection / Format / Create
TAB_FILENAME = TAB_CREATE
TAB_OUTPUT = TAB_CREATE

ALL_SETS = "<All>"
ISO_SIZES = [("A0", 1189, 841), ("A1", 841, 594), ("A2", 594, 420), ("A3", 420, 297), ("A4", 297, 210),
             ("B1", 1000, 707), ("B2", 707, 500), ("ARCH E", 1219, 914), ("ARCH D", 914, 610)]

sheet_sets = {}        # View/Sheet Set name -> set of element id numbers
progress_rows = []     # ProgressRow list shown on the Create step


class ProgressRow(object):
    """One file of the Create step's list (bound to dgProgress)."""

    def __init__(self, index, fmt, label, filename, element_id=None):
        self.Index = index
        self.Format = fmt
        self.Label = label
        self.FileName = filename
        self.ElementId = element_id
        self.Status = "Waiting"
        self.State = "waiting"      # waiting | running | done | failed | skipped
        self.Message = ""

    def set(self, state, status, message=None):
        self.State = state
        self.Status = status
        if message is not None:
            self.Message = message

window = None
dispatcher = None
current_items = []
displayed_items = []   # the subset currently shown in dgItems
sheet_items = []
view_items = []
current_mode = "Sheets"

_loading = False
_busy = False
_search_timer = None
_filename_timer = None
_subscriptions = []    # (event_owner, event_name, handler) to undo on close


class _StepError(ValueError):
    """A problem the user fixes on one particular wizard step."""

    def __init__(self, message, tab=None):
        ValueError.__init__(self, message)
        self.tab = tab


# ---------------------------------------------------------------------------
# Small control helpers
# ---------------------------------------------------------------------------

def _xaml_path():
    return os.path.join(os.path.dirname(__file__), "ExportManager.xaml")


def _control(*names):
    """Return the first existing XAML control from a list of compatible names."""
    for name in names:
        try:
            control = getattr(window, name)
            if control is not None:
                return control
        except Exception:
            pass
    return None


def _is_checked(*names):
    control = _control(*names)
    try:
        return bool(control.IsChecked)
    except Exception:
        return False


def _combo_text(name, default=""):
    combo = _control(name)
    try:
        value = combo.SelectedItem
        if value is None:
            return default
        # ComboBoxItem (declared in XAML) vs plain string (ItemsSource).
        content = getattr(value, "Content", None)
        return str(content if content is not None else value)
    except Exception:
        return default


def _set_visible(control, visible):
    if control is None:
        return
    try:
        control.Visibility = Visibility.Visible if visible else Visibility.Collapsed
    except Exception:
        pass


def _alert(message, title="Export Manager", **kwargs):
    return forms.alert(message, title=title, **kwargs)


def _set_text(name, text):
    control = _control(name)
    if control is None:
        return
    try:
        control.Text = text
    except Exception:
        pass


def _count_text(count, singular):
    return "{} {}{}".format(count, singular, "" if count == 1 else "s")


# ---------------------------------------------------------------------------
# Remembered output folder (per model)
# ---------------------------------------------------------------------------

def _settings_path():
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(root, "EleTools", "ExportManager", "settings.json")


def _read_settings():
    try:
        with open(_settings_path(), "r") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _remembered_folder():
    """Output folder last used for THIS model (never another model's --
    exporting project B into project A's folder would be worse than
    starting with an empty box)."""
    try:
        folders = _read_settings().get("folders", {})
        folder = folders.get(helpers.document_title()) or ""
        return folder if folder and os.path.isdir(folder) else ""
    except Exception:
        return ""


def _remember_setting(key, value):
    """One machine-wide choice in settings.json (next to the per-model folders)."""
    try:
        data = _read_settings()
        data[key] = value
        path = _settings_path()
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as stream:
            json.dump(data, stream, indent=2)
    except Exception:
        pass


def _remember_affixes(sender, args):
    """File name prefix / suffix are remembered for the next session."""
    _remember_setting("file_prefix", filename_builder.prefix_text)
    _remember_setting("file_suffix", filename_builder.suffix_text)


def _bind_ole_Click(sender, args):
    # FIX: "Bind images as OLE" was never remembered - it came up unticked every time the window
    # opened, on a step that is easy to skip, so DWGs were exported with linked images although the
    # user had ticked it before (dwg_log.txt: "bind images as OLE: False" on every run).
    _remember_setting("bind_ole", _is_checked("chkBindOLE"))
    _update_status()


def _remember_folder(folder):
    title = helpers.document_title()
    if not title or not folder:
        return
    try:
        data = _read_settings()
        folders = data.get("folders")
        if not isinstance(folders, dict):
            folders = {}
        folders[title] = folder
        data["folders"] = folders
        path = _settings_path()
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as stream:
            json.dump(data, stream, indent=2)
    except Exception:
        pass   # a convenience only; never block an export over it


# ---------------------------------------------------------------------------
# Grid population
# ---------------------------------------------------------------------------

def _set_grid_items(items):
    global displayed_items
    displayed_items = list(items)
    try:
        window.dgItems.CommitEdit()
        window.dgItems.CommitEdit()
    except Exception:
        pass
    window.dgItems.ItemsSource = None
    window.dgItems.ItemsSource = displayed_items
    _refresh_count()


def _refresh_count():
    selected = helpers.selected_items(current_items)

    # Checks persist while the search/filter changes, so warn when some of
    # them are no longer visible -- those rows would still be exported.
    shown = set(id(item) for item in displayed_items)
    hidden = len([item for item in selected if id(item) not in shown])

    text = "Selected : {}".format(len(selected))
    if hidden:
        text += "   ({} not shown by the current search/filter)".format(hidden)

    try:
        window.txtCount.Text = text
    except Exception:
        pass
    _sync_select_all()
    _update_range_preview()
    _update_status(selected)


def _sync_select_all():
    """Make the 'Tick all shown' box describe the rows on screen:
    ticked = all of them, dash = some of them, empty = none.

    FIX: the box used to keep whatever state it was last clicked into, so
    after a search/filter change it could sit ticked above a list with no
    ticked rows (and the next click then UNticked instead of ticking).
    """
    box = _control("chkSelectAll")
    if box is None:
        return
    total = len(displayed_items)
    ticked = len([item for item in displayed_items if item.IsSelected])
    try:
        if not total or not ticked:
            box.IsChecked = False
        elif ticked == total:
            box.IsChecked = True
        else:
            box.IsChecked = None    # indeterminate
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Footer status + summary (3. Create)
# ---------------------------------------------------------------------------

def _folder_text():
    box = _control("txtFolder", "txtOutputFolder")
    try:
        return (box.Text or "").strip()
    except Exception:
        return ""


def _formats_text():
    parts = []
    if _is_checked("chkPDF"):
        combined = _is_checked("chkCombined", "chkCombineFiles")
        parts.append("PDF ({})".format(
            "one combined file" if combined else "one file per item"))
    if _is_checked("chkDWG"):
        setup = _selected_dwg_setup()
        images = "images bound as OLE" if _is_checked("chkBindOLE") else "images linked, not bound"
        parts.append("DWG ({}; {})".format(setup, images) if setup else "DWG ({})".format(images))
    return "  +  ".join(parts)


def _names_text(selected):
    """Example of the file name(s) the current settings will produce."""
    pdf = _is_checked("chkPDF")
    dwg = _is_checked("chkDWG")
    combined = pdf and _is_checked("chkCombined", "chkCombineFiles")
    if not selected or not (pdf or dwg):
        return "-"

    stem = helpers.safe_filename(selected[0].FileName, selected[0].Name)
    extensions = []
    if pdf and not combined:
        extensions.append(".pdf")
    if dwg:
        extensions.append(".dwg")

    parts = []
    if extensions:
        text = stem + " / ".join(extensions)
        if len(selected) > 1:
            text += "   (first of {})".format(len(selected))
        parts.append(text)
    if combined:
        parts.append("combined PDF: you are asked for its name on CREATE")
    return "     ".join(parts)


def _update_status(selected=None):
    """Refresh the footer line and the summary card.  Pure UI/string work:
    it only reads controls and the cached grid rows, never the Revit API."""
    if window is None:
        return
    try:
        export_range = _export_range()
        if selected is None:
            selected = helpers.selected_items(current_items)
        noun = "sheet" if current_mode == "Sheets" else "view"

        if export_range == "selected":
            if selected:
                what = "{} ticked".format(_count_text(len(selected), noun))
            else:
                what = "Nothing ticked yet - tick rows on step 1 (Selection)."
            names = _names_text(selected)
            short_what = what if selected else "Nothing ticked"
        else:
            preview = _control("txtRangePreview")
            what = getattr(preview, "Text", "") or ""
            names = "Named from the active view when CREATE is pressed."
            short_what = ("Current window" if export_range == "current"
                          else "Visible portion of current window")

        formats = _formats_text()
        folder = _folder_text()

        _set_text("txtSumItems", what)
        _set_text("txtSumFormats",
                  formats or "No format chosen - switch on PDF and / or DWG above the list on step 1.")
        _set_text("txtSumNames", names)
        _set_text("txtSumFolder", folder or "Not chosen yet - browse for a folder above.")
        _set_text("txtFooterStatus", "    |    ".join([
            short_what,
            formats or "No format chosen",
            folder or "No output folder"
        ]))
    except Exception:
        pass
    try:
        _rebuild_progress_rows(selected)
    except Exception:
        pass


def _status_changed(sender, args):
    _update_status()


# ---------------------------------------------------------------------------
# Create step: the list of files to make, with live status (as ProSheets)
# ---------------------------------------------------------------------------

def _row_label(element):
    """The label the export engines use in their progress messages / results."""
    try:
        return element.SheetNumber
    except Exception:
        try:
            return element.Name
        except Exception:
            return "Unknown View"


def _rebuild_progress_rows(selected=None):
    """Plan the files of the current settings: one row per item and format
    (one row for a combined PDF). Never while an export is running."""
    global progress_rows
    if window is None or _busy:
        return
    rows = []
    pdf, dwg = _is_checked("chkPDF"), _is_checked("chkDWG")
    combined = pdf and _is_checked("chkCombined", "chkCombineFiles")
    if _export_range() != "selected":
        label = "Active view in Revit"
        if pdf:
            rows.append(ProgressRow(len(rows) + 1, "PDF", label, "named when created"))
        if dwg:
            rows.append(ProgressRow(len(rows) + 1, "DWG", label, "named when created"))
    else:
        if selected is None:
            selected = helpers.selected_items(current_items)
        if combined and selected:
            rows.append(ProgressRow(len(rows) + 1, "PDF", "{} item(s) in one file".format(len(selected)),
                                    "combined PDF (name asked on CREATE)"))
        for fmt, wanted, extension in (("PDF", pdf and not combined, ".pdf"), ("DWG", dwg, ".dwg")):
            if not wanted:
                continue
            for item in selected:
                stem = helpers.safe_filename(item.FileName, item.Name)
                rows.append(ProgressRow(len(rows) + 1, fmt, _row_label(item.Element), stem + extension,
                                        helpers.element_id_value(item.Element.Id)))
    progress_rows = rows
    grid = _control("dgProgress")
    if grid is not None:
        try:
            grid.ItemsSource = None
            grid.ItemsSource = progress_rows
        except Exception:
            pass
    _progress_summary()


def _progress_summary():
    counts = {}
    for row in progress_rows:
        counts[row.State] = counts.get(row.State, 0) + 1
    if not progress_rows:
        text = "Nothing to create yet - tick rows on step 1 and switch on PDF and / or DWG."
    elif counts.get("waiting", 0) == len(progress_rows):
        text = "{} file(s) will be created.".format(len(progress_rows))
    else:
        parts = ["{} done".format(counts.get("done", 0))]
        for state, word in (("failed", "failed"), ("skipped", "skipped"), ("running", "in progress"),
                            ("waiting", "waiting")):
            if counts.get(state):
                parts.append("{} {}".format(counts[state], word))
        text = "{} of {}: {}".format(counts.get("done", 0), len(progress_rows), ", ".join(parts))
    _set_text("txtProgressSummary", text)


def _refresh_progress_grid():
    try:
        helpers.refresh_grid(_control("dgProgress"))
    except Exception:
        pass
    _progress_summary()


def _progress_message(message):
    """'Exporting PDF: A-101' (or a printing message) -> mark that file running, the
    previous running file of the same format done."""
    text = str(message or "")
    fmt = "DWG" if "DWG" in text.upper() else ("PDF" if "PDF" in text.upper() else None)
    if fmt is None or ":" not in text:
        return
    label = text.split(":", 1)[1].strip()
    target = None
    for row in progress_rows:
        if row.Format != fmt:
            continue
        if row.State == "running" and row.Label != label:
            row.set("done", "Done")
        if target is None and row.State in ("waiting", "running") and \
                (row.Label == label or row.Label.endswith("in one file") or row.Label == "Active view in Revit"):
            target = row
    if target is not None:
        target.set("running", "Creating...")
    _refresh_progress_grid()


def _apply_results_to_rows(results, cancelled):
    """Final status of every row from the engines' results."""
    exported = set()
    for entry in results.get("exported", []):
        if entry.get("element_id") is not None:
            exported.add(entry.get("element_id"))
    failed = {}
    for entry in results.get("errors", []):
        failed[str(entry.get("name", ""))] = entry.get("error", "")
    skipped = {}
    for entry in results.get("skipped", []):
        skipped[str(entry.get("name", ""))] = entry.get("reason", "") or entry.get("message", "") or "skipped"
    single = len(progress_rows) and progress_rows[0].ElementId is None
    for row in progress_rows:
        if row.Label in failed:
            row.set("failed", "Failed", failed[row.Label])
        elif row.Label in skipped:
            row.set("skipped", "Skipped", skipped[row.Label])
        elif (row.ElementId is not None and row.ElementId in exported) or \
                (row.ElementId is None and results.get("exported")):
            row.set("done", "Done")
        elif row.State == "running" and not cancelled:
            row.set("done", "Done")
        elif cancelled and row.State in ("waiting", "running"):
            row.set("skipped", "Cancelled")
        elif single and failed:
            row.set("failed", "Failed", "; ".join(v for v in failed.values() if v)[:300])
    _refresh_progress_grid()


def _update_nav():
    """Back is useless on the first step, Next on the last one.
    CREATE is shown only on the last step (3 Create), after the choices of the other steps."""
    tabs = _control("tabMain")
    if tabs is None:
        return
    try:
        index = tabs.SelectedIndex
        last = tabs.Items.Count - 1
        back = _control("btnBack")
        if back is not None:
            back.IsEnabled = index > 0
        forward = _control("btnNext")
        if forward is not None:
            forward.IsEnabled = index < last
        export = _control("btnExport")
        if export is not None:
            from System.Windows import Visibility
            export.Visibility = Visibility.Visible if index == last else Visibility.Collapsed
    except Exception:
        pass


def _go_to_tab(index):
    tabs = _control("tabMain")
    if tabs is None or index is None:
        return
    try:
        if 0 <= index < tabs.Items.Count:
            tabs.SelectedIndex = index
    except Exception:
        pass


def _filename_for_item(item):
    return filename_builder.build_filename(item.Element, fallback_name=item.Name)


def _refresh_custom_filenames(items=None):
    for item in (items if items is not None else current_items):
        try:
            item.FileName = _filename_for_item(item)
        except Exception:
            pass
        view_search.rebuild_search(item, current_mode)
    helpers.refresh_grid(window.dgItems)


def _sheet_revision(sheet):
    """The sheet's current revision as shown in its title block ('' when none)."""
    try:
        from Autodesk.Revit.DB import BuiltInParameter
        parameter = sheet.get_Parameter(BuiltInParameter.SHEET_CURRENT_REVISION)
        return (parameter.AsString() or "") if parameter is not None else ""
    except Exception:
        return ""


def _paper_size(width_mm, height_mm):
    """'A1' for an ISO / ARCH size (either orientation, 5 mm tolerance), else '841 x 594'."""
    long_side, short_side = max(width_mm, height_mm), min(width_mm, height_mm)
    for name, a, b in ISO_SIZES:
        if abs(long_side - a) <= 5 and abs(short_side - b) <= 5:
            return name
    if long_side <= 0:
        return ""
    return "{:.0f} x {:.0f}".format(width_mm, height_mm)


def _title_block_sizes(doc):
    """{sheet id number: paper size text} from every title block, in ONE collector."""
    sizes = {}
    try:
        from Autodesk.Revit.DB import BuiltInCategory, BuiltInParameter
        blocks = FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_TitleBlocks) \
            .WhereElementIsNotElementType().ToElements()
        for block in blocks:
            try:
                width = block.get_Parameter(BuiltInParameter.SHEET_WIDTH).AsDouble() * 304.8
                height = block.get_Parameter(BuiltInParameter.SHEET_HEIGHT).AsDouble() * 304.8
            except Exception:
                continue
            key = helpers.element_id_value(block.OwnerViewId)
            text = _paper_size(width, height)
            if key not in sizes or len(text) < len(sizes[key]):
                sizes[key] = text
    except Exception:
        pass
    return sizes


def _make_sheet_item(sheet, sizes=None):
    item = helpers.ExportItem(
        sheet,
        number=sheet.SheetNumber,
        name=sheet.Name,
        filename=sheet.SheetNumber
    )
    item.Revision = _sheet_revision(sheet)
    item.Size = (sizes or {}).get(helpers.element_id_value(sheet.Id), "")
    view_search.rebuild_sheet_search(item)
    return item


def _make_view_item(view, doc):
    item = helpers.ExportItem(view, number="", name=view.Name, filename=view.Name)
    item.Revision = ""
    item.Size = ""
    view_search.index_view_item(item, doc)   # type, level, sheet, template
    return item


def _collect_sheet_items(doc):
    sheets = FilteredElementCollector(doc).OfClass(ViewSheet).ToElements()
    sizes = _title_block_sizes(doc)
    items = [_make_sheet_item(sheet, sizes) for sheet in sheets]
    items.sort(key=lambda item: item.Number.lower())
    return items


def _collect_sheet_sets(doc):
    """{name: set of element id numbers} of the model's saved View/Sheet Sets (read once)."""
    found = {}
    try:
        from Autodesk.Revit.DB import ViewSheetSet
        for sheet_set in FilteredElementCollector(doc).OfClass(ViewSheetSet).ToElements():
            ids = set()
            try:
                for view in sheet_set.Views:
                    ids.add(helpers.element_id_value(view.Id))
            except Exception:
                pass
            found[sheet_set.Name] = ids
    except Exception:
        pass
    return found


def _fill_sheet_sets():
    combo = _control("cmbSheetSet")
    if combo is None:
        return
    previous = _combo_text("cmbSheetSet", ALL_SETS)
    names = [ALL_SETS] + sorted(sheet_sets.keys(), key=lambda n: n.lower())
    helpers.set_items_source(combo, names)
    combo.SelectedItem = previous if previous in names else ALL_SETS


def _collect_view_items(doc):
    items = []
    for view in FilteredElementCollector(doc).OfClass(View).ToElements():
        try:
            if view.IsTemplate or view.ViewType not in VIEW_TYPES_TO_LIST:
                continue
            if isinstance(view, ViewSheet) or not view.CanBePrinted:
                continue
            items.append(_make_view_item(view, doc))
        except Exception:
            pass
    items.sort(key=lambda item: (item.ViewTypeName.lower(), item.Name.lower()))
    return items


def _load_from_model(keep_checked=False):
    """(Re)read sheets and views from the bound document.

    Called at start-up (command context) and from "Refresh from model"
    (ExternalEvent context).  Existing ticks survive a refresh.
    """
    global sheet_items, view_items
    checked = set()
    if keep_checked:
        for item in helpers.selected_items(sheet_items) + helpers.selected_items(view_items):
            checked.add(helpers.element_id_value(item.Element.Id))

    global sheet_sets
    doc = helpers.doc
    sheet_items = _collect_sheet_items(doc)
    view_items = _collect_view_items(doc)
    sheet_sets = _collect_sheet_sets(doc)
    _fill_sheet_sets()

    if checked:
        for item in sheet_items + view_items:
            if helpers.element_id_value(item.Element.Id) in checked:
                item.IsSelected = True

    # Hand the sheets to the filter panel so it never re-queries the model.
    sheet_filter_ui.reload([item.Element for item in sheet_items])
    _load_view_filter_options()


def _activate_mode(mode):
    """Switch the grid between Sheets and Views (no model re-query)."""
    global current_mode, current_items, _loading
    previous_mode = current_mode
    current_mode = mode
    current_items = sheet_items if mode == "Sheets" else view_items

    _loading = True
    try:
        sample = current_items[0].Element if current_items else None
        filename_builder.load_parameters(sample_element=sample, mode=mode)
        filename_builder.restore_mode_sequence(mode, previous_mode=previous_mode)
        _refresh_custom_filenames(current_items)
        _apply_mode_ui(mode)
        _reset_select_all()
    finally:
        _loading = False

    filter_items()
    _refresh_selection_set_list()


def _apply_mode_ui(mode):
    is_sheets = mode == "Sheets"
    sheet_filter_ui.set_enabled(is_sheets)
    _set_visible(_control("expFilters"), is_sheets)
    _set_visible(_control("grpViewFilters"), not is_sheets)

    column_visibility = {
        "Level": not is_sheets,
        "On Sheet": not is_sheets,
        "Filter Values": is_sheets,
        "Revision": is_sheets,
        "Size": is_sheets,
    }
    try:
        for column in window.dgItems.Columns:
            header = str(column.Header)
            if header in ("Number / Type", "Sheet Number", "View Type"):
                column.Header = "Sheet Number" if is_sheets else "View Type"
                continue
            visible = column_visibility.get(header)
            if visible is not None:
                column.Visibility = Visibility.Visible if visible else Visibility.Collapsed
    except Exception:
        pass


def _reset_select_all():
    """Kept for the existing call sites; the box now simply mirrors the
    rows on screen (see _sync_select_all)."""
    _sync_select_all()


# ---------------------------------------------------------------------------
# Views-mode filter drop-downs
# ---------------------------------------------------------------------------

def _fill_combo(name, values, keep_selection=True):
    combo = _control(name)
    if combo is None:
        return
    previous = None
    if keep_selection:
        try:
            previous = combo.SelectedItem
        except Exception:
            previous = None
    helpers.set_items_source(combo, values)
    if previous is not None and str(previous) in values:
        combo.SelectedItem = str(previous)
    elif values:
        combo.SelectedIndex = 0


def _load_view_filter_options():
    global _loading
    was_loading = _loading
    _loading = True
    try:
        _fill_combo("cmbViewType", view_search.view_type_options(view_items))
        _fill_combo("cmbViewLevel", view_search.level_options(view_items))
        _fill_combo("cmbViewSheetState", view_search.SHEET_STATES)
    finally:
        _loading = was_loading


def _view_filter_state():
    return (
        _combo_text("cmbViewType", view_search.ALL_TYPES),
        _combo_text("cmbViewLevel", view_search.ALL_LEVELS),
        _combo_text("cmbViewSheetState", view_search.SHEET_ANY),
    )


# ---------------------------------------------------------------------------
# Filter pipeline
# ---------------------------------------------------------------------------

def _search_text():
    try:
        return (window.txtSearch.Text or "").strip()
    except Exception:
        return ""


def filter_items():
    """Two-stage filter pipeline for the selection grid.

    1. Sheets: the sheet parameter filter (AND across rules).
       Views : view type / level / sheet-placement drop-downs.
    2. The free-text Search box (view_search syntax), for both modes.

    FIX: load_views() used to push the raw list straight into the grid,
    so text already typed in Search was ignored after switching to Views.
    Both modes now always go through this pipeline.
    """
    if window is None:
        return
    items = list(current_items)

    # Stage 0: the chosen View/Sheet Set (as ProSheets)
    chosen_set = _combo_text("cmbSheetSet", ALL_SETS)
    if chosen_set and chosen_set != ALL_SETS and chosen_set in sheet_sets:
        members = sheet_sets[chosen_set]
        items = [item for item in items if helpers.element_id_value(item.Element.Id) in members]

    # Stage 1
    if current_mode == "Sheets":
        if sheet_filter_ui.is_available():
            items = sheet_filter_ui.apply_to(items)
    else:
        view_type, level, sheet_state = _view_filter_state()
        items = view_search.filter_views(items, view_type, level, sheet_state)
    stage_one_count = len(items)

    # Stage 2
    query = _search_text()
    if query:
        items = view_search.search(items, query, current_mode)

    _set_grid_items(items)

    noun = "sheet(s)" if current_mode == "Sheets" else "view(s)"
    result_box = _control("txtSearchResult")
    if result_box is not None:
        result_box.Text = "{} of {} {} shown".format(len(items), len(current_items), noun)

    extra = "Search box narrowed the list further." if query and len(items) != stage_one_count else ""
    if current_mode == "Sheets" and sheet_filter_ui.is_available():
        sheet_filter_ui.update_summary(len(items), len(current_items), extra=extra)
    else:
        summary = _control("txtViewFilterSummary")
        if summary is not None:
            if len(items) == len(current_items):
                summary.Text = "Showing all {} view(s).".format(len(current_items))
            else:
                summary.Text = "Matched {} of {} view(s).".format(len(items), len(current_items))


def _schedule_search():
    if _search_timer is None:
        filter_items()
        return
    _search_timer.Stop()
    _search_timer.Start()


def _on_search_timer(sender, args):
    _search_timer.Stop()
    filter_items()


def _on_filename_sequence_changed():
    """filename_builder callback -> refresh the Custom File Name column."""
    if _loading or window is None:
        return
    if _filename_timer is None:
        _refresh_custom_filenames(current_items)
        return
    _filename_timer.Stop()
    _filename_timer.Start()


def _on_filename_timer(sender, args):
    _filename_timer.Stop()
    _refresh_custom_filenames(current_items)
    if _search_text():
        filter_items()   # the "file:" search field may have changed
    else:
        _update_status()  # the file-name example in the summary changed


# ---------------------------------------------------------------------------
# DWG setups / selection sets
# ---------------------------------------------------------------------------

def load_dwg_setups():
    combo = _control("cmbDWGSetup")
    if combo is None:
        return
    previous = _selected_dwg_setup()
    names = export_dwg.get_setup_names(helpers.doc)
    combo.ItemsSource = names
    if previous in names:
        combo.SelectedItem = previous
    elif names:
        combo.SelectedIndex = 0


def _refresh_selection_set_list(select_name=None):
    combo = _control("cmbSelectionSets")
    if combo is None:
        return
    names = selection_sets.available_set_names(mode=current_mode)
    combo.ItemsSource = names
    if select_name and select_name in names:
        combo.SelectedItem = select_name
    elif names:
        combo.SelectedIndex = 0
    else:
        combo.SelectedIndex = -1


def _selected_dwg_setup():
    combo = _control("cmbDWGSetup")
    try:
        return str(combo.SelectedItem or "")
    except Exception:
        return ""


def _selected_selection_set_name():
    combo = _control("cmbSelectionSets")
    try:
        return str(combo.SelectedItem or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Export settings (read on the UI thread, applied inside the API context)
# ---------------------------------------------------------------------------

def _output_folder():
    """Validated, created output folder.

    FIX: every failure used to be reported as "Select an output folder",
    even when a folder WAS entered but could not be created (no access,
    unplugged drive, bad characters).  A relative path such as "Exports"
    was also accepted and silently resolved against Revit's own working
    directory, so the files landed somewhere under Program Files.
    """
    text = _folder_text()
    if not text:
        raise _StepError("Choose an output folder first.", TAB_OUTPUT)
    if not os.path.isabs(text):
        raise _StepError(
            "Enter the full path of the output folder (for example "
            "C:\\Projects\\Exports), or use Browse.\n\nYou typed: {}".format(text),
            TAB_OUTPUT
        )
    try:
        return helpers.ensure_folder(text)
    except Exception as error:
        raise _StepError(
            "The output folder could not be created:\n{}\n\n{}".format(text, error),
            TAB_OUTPUT
        )


# ---------------------------------------------------------------------------
# "Same as ProSheets": take the PDF settings of the user's ProSheets profile
# ---------------------------------------------------------------------------

def _prosheets_settings():
    """The PDF settings of the ProSheets profile in use on this machine.

    DiRoots ProSheets keeps its profiles in
    %LOCALAPPDATA%\\DiRoots\\ProSheets\\Profiles: profiles.xml (the one marked
    IsCurrent) or default_profile.xml. Returns (profile name, {tag: text}) or
    (None, None) when ProSheets has no profile here. Read-only."""
    import clr
    clr.AddReference("System.Xml")
    from System.Xml import XmlDocument

    folder = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DiRoots", "ProSheets", "Profiles")
    for filename in ("profiles.xml", "default_profile.xml"):
        path = os.path.join(folder, filename)
        if not os.path.isfile(path):
            continue
        try:
            document = XmlDocument()
            document.Load(path)
            info, name = None, None
            for profile in document.GetElementsByTagName("Profile"):
                current = profile.SelectSingleNode("IsCurrent")
                if info is None or (current is not None and current.InnerText.strip().lower() == "true"):
                    found = profile.SelectSingleNode("TemplateInfo")
                    if found is not None:
                        info = found
                        node = profile.SelectSingleNode("Name")
                        name = node.InnerText if node is not None else filename
            if info is None:
                nodes = document.GetElementsByTagName("TemplateInfo")
                if nodes.Count:
                    info, name = nodes[0], "default"
            if info is None:
                continue
            values = {}
            for child in info.ChildNodes:              # direct children only: DWF / IMG have their own
                if child.ChildNodes.Count <= 1:
                    values[child.Name] = (child.InnerText or "").strip()
            return name, values
        except Exception:
            continue
    return None, None


def _set_checked(name, state):
    control = _control(name)
    if control is not None:
        try:
            control.IsChecked = bool(state)
        except Exception:
            pass


def _select_combo(name, text):
    """Select the item whose text matches, ignoring case and spaces."""
    combo = _control(name)
    if combo is None:
        return False
    wanted = "".join(str(text).lower().split())
    try:
        for item in combo.Items:
            shown = getattr(item, "Content", item)
            if "".join(str(shown).lower().split()) == wanted:
                combo.SelectedItem = item
                return True
    except Exception:
        pass
    return False


def apply_prosheets_settings(announce=True):
    """Set the PDF controls of the Format tab to the user's ProSheets profile.
    Returns the profile name, or None when there is nothing to take over."""
    name, values = _prosheets_settings()
    if not values:
        if announce:
            _alert("No ProSheets profile was found on this computer "
                   "(%LOCALAPPDATA%\\DiRoots\\ProSheets\\Profiles).")
        return None

    def flag(tag, default=False):
        text = values.get(tag)
        return default if text is None else text.lower() == "true"

    _set_checked("rbCenter", flag("IsCenter", True))
    _set_checked("rbOffset", not flag("IsCenter", True))
    fit = flag("IsFitToPage")
    _set_checked("rbFitPage", fit)
    _set_checked("rbZoom", not fit)
    if values.get("Zoom"):
        _set_text("txtZoom", values["Zoom"])
    vector = flag("IsVectorProcessing", True)
    _set_checked("rbVectorProcessing", vector)
    _set_checked("rbRasterProcessing", not vector)
    _select_combo("cmbRasterQuality", values.get("RasterQuality", "High"))
    colour = "".join(values.get("Color", "Color").lower().split())
    _select_combo("cmbColorMode", {"blackline": "Black Lines", "blacklines": "Black Lines",
                                   "grayscale": "GrayScale", "greyscale": "GrayScale"}.get(colour, "Color"))
    # 'View links in blue' is NOT taken from ProSheets: it always starts off
    # (user's choice - tick it by hand for a colour set that needs blue links)
    _set_checked("chkViewLinksBlue", False)
    _set_checked("chkHideRefs", flag("HidePlanes", True))
    _set_checked("chkHideScope", flag("HideScopeBox", True))
    _set_checked("chkHideUnrefTags", flag("HideUnreferencedTags", True))
    _set_checked("chkHideCrop", flag("HideCropBoundaries", True))
    _set_checked("chkHalftoneThin", flag("ReplaceHalftone"))
    _set_checked("chkMaskCoincident", flag("MaskCoincidentLines"))
    separate = flag("IsSeparateFile", True)
    _set_checked("chkSeparate", separate)
    _set_checked("chkCombined", not separate)
    # ProSheets has no resolution setting of its own
    _select_combo("cmbExportQuality", "Revit default")

    note = "PDF settings taken from your ProSheets profile \"{}\".".format(name)
    printer = values.get("PrinterName", "")
    if printer and printer.lower() not in ("revit", "revit pdf", "autodesk pdf"):
        # ProSheets prints this profile through a printer: do the same
        if _select_combo("cmbPrinter", printer):
            _set_checked("rbEnginePrinter", True)
            _set_checked("rbEngineRevit", False)
            note += " Like ProSheets, the PDF is printed through \"{}\".".format(printer)
        else:
            note += (" ProSheets prints that profile through \"{}\", which is not installed here; "
                     "Revit's own PDF export is used.".format(printer))
    _set_text("txtProSheets", note)
    return name


def load_printers():
    """Fill the PDF printer list (PDF printers first; PDF24 preselected)."""
    combo = _control("cmbPrinter")
    if combo is None:
        return
    names = export_print.installed_printers()
    try:
        combo.Items.Clear()
        for name in names:
            combo.Items.Add(name)
        for wanted in (export_print.PDF24, "Microsoft Print to PDF"):
            if wanted in names:
                combo.SelectedItem = wanted
                break
        else:
            if names:
                combo.SelectedIndex = 0
    except Exception:
        pass


def btnProSheets_Click(sender, args):
    if apply_prosheets_settings(announce=True):
        _update_status()


def _collect_pdf_settings():
    """Snapshot every PDF control on the Format tab.

    FIX: the Raster quality, Colour mode, Hide crop/scope/reference planes,
    Placement and Zoom controls existed in the XAML but were never read, so
    the PDF always used Revit's defaults regardless of what the user chose.
    """
    try:
        zoom = int(float(str(_control("txtZoom").Text).strip()))
    except Exception:
        zoom = 100
    return {
        "raster": _is_checked("rbRasterProcessing"),
        "raster_quality": _combo_text("cmbRasterQuality", "High"),
        # LINE WEIGHTS: the resolution the PDF is written at, and the two
        # Revit print options that change how lines come out.
        "export_dpi": _combo_text("cmbExportQuality", "Revit default"),
        "halftone_thin": _is_checked("chkHalftoneThin"),
        "mask_coincident": _is_checked("chkMaskCoincident"),
        "links_blue": _is_checked("chkViewLinksBlue"),
        "hide_unref_tags": _is_checked("chkHideUnrefTags"),
        "color": _combo_text("cmbColorMode", "Color"),
        "hide_crop": _is_checked("chkHideCrop"),
        "hide_scope": _is_checked("chkHideScope"),
        "hide_refs": _is_checked("chkHideRefs"),
        "placement_center": not _is_checked("rbOffset"),
        "fit_to_page": _is_checked("rbFitPage"),
        "zoom": max(1, min(zoom, 1000)),
    }


def _make_pdf_options_factory(settings, warnings):
    """Return a callable creating a fresh, fully configured PDFExportOptions.

    FIX: the previous code set ``RasterQuality = PDFExportQualityType.High``.
    PDFExportQualityType is the DPI enum used by ``ExportQuality`` and has
    no ``High`` member, so it raised AttributeError, which the bare
    ``except`` swallowed: raster exports silently stayed at LOW quality.
    ``RasterQuality`` takes a RasterQualityType.
    """
    import Autodesk.Revit.DB as DB

    def _enum(type_name, member):
        enum_type = getattr(DB, type_name, None)
        return getattr(enum_type, member, None) if enum_type is not None else None

    color_map = {"Color": "Color", "Black Lines": "BlackLine", "GrayScale": "GrayScale"}
    reported = set()

    def _apply(options, attribute, value):
        if value is None:
            return
        try:
            setattr(options, attribute, value)
        except Exception as error:
            if attribute not in reported:
                reported.add(attribute)
                warnings.append({
                    "name": "PDF option",
                    "warning": "{} could not be set ({})".format(attribute, error)
                })

    def factory():
        options = DB.PDFExportOptions()
        _apply(options, "AlwaysUseRaster", bool(settings["raster"]))
        _apply(options, "RasterQuality",
               _enum("RasterQualityType", settings["raster_quality"]))
        # FIX (line weights): ExportQuality was never set, so every PDF was
        # written at whatever resolution Revit defaults to, with no way to
        # change it. Line widths are rounded to that resolution: at a low
        # DPI the thin pens all come out the same width. It is now set
        # explicitly from the "Export quality (DPI)" list.
        dpi = "".join(ch for ch in str(settings.get("export_dpi", "")) if ch.isdigit())
        if dpi:
            quality = _enum("PDFExportQualityType", "DPI" + dpi)
            if quality is None and "ExportQuality" not in reported:
                reported.add("ExportQuality")
                warnings.append({"name": "PDF option",
                                 "warning": "{} DPI is not available in this Revit version; "
                                            "Revit's default quality was used".format(dpi)})
            _apply(options, "ExportQuality", quality)
        _apply(options, "ReplaceHalftoneWithThinLines", bool(settings.get("halftone_thin")))
        _apply(options, "MaskCoincidentLines", bool(settings.get("mask_coincident")))
        _apply(options, "ViewLinksInBlue", bool(settings.get("links_blue")))
        _apply(options, "HideUnreferencedViewTags", bool(settings.get("hide_unref_tags")))
        _apply(options, "ColorDepth",
               _enum("ColorDepthType", color_map.get(settings["color"], "Color")))
        _apply(options, "HideCropBoundaries", settings["hide_crop"])
        _apply(options, "HideScopeBoxes", settings["hide_scope"])
        _apply(options, "HideReferencePlane", settings["hide_refs"])
        _apply(options, "PaperPlacement", _enum(
            "PaperPlacementType",
            "Center" if settings["placement_center"] else "LowerLeft"))
        if settings["fit_to_page"]:
            _apply(options, "ZoomType", _enum("ZoomType", "FitToPage"))
        else:
            _apply(options, "ZoomType", _enum("ZoomType", "Zoom"))
            _apply(options, "ZoomPercentage", int(settings["zoom"]))
        return options

    return factory


def _export_range():
    if _is_checked("rbRangeCurrentWindow"):
        return "current"
    if _is_checked("rbRangeVisiblePortion"):
        return "visible"
    return "selected"


def _active_view():
    uidoc = helpers.active_uidoc()
    if uidoc is None:
        return None
    try:
        return uidoc.ActiveView
    except Exception:
        return None


def _range_preview_text(export_range):
    if export_range == "selected":
        count = len(helpers.selected_items(current_items))
        return "Exports the {} checked row(s) below.".format(count)

    if not helpers.doc_is_valid():
        return "The model this window was opened for has been closed."

    if helpers.active_uidoc() is None:
        return ("Revit is showing a different model. Switch back to '{}' to "
                "use this option.".format(helpers.document_title()))

    active_view = _active_view()
    if active_view is None:
        return "No active view detected in Revit -- open a sheet or view first."

    label = helpers.element_label(active_view)
    try:
        view_name = active_view.Name or ""
    except Exception:
        view_name = ""
    title = "{} - {}".format(label, view_name) if view_name and view_name != label else label

    if not helpers.is_printable(active_view):
        return "Active view is '{}', but it can't be exported (not printable).".format(title)

    if export_range == "current":
        return "Will export: {} (full view extent).".format(title)

    if isinstance(active_view, ViewSheet):
        return "Will export: {} (sheets always export in full).".format(title)
    is_open = helpers.get_open_uiview(helpers.active_uidoc(), active_view.Id) is not None
    if is_open:
        return "Will export: {} (cropped to exactly what is on screen).".format(title)
    return ("Will export: {} -- not open in a Revit window, so the full view "
            "extent is used.".format(title))


def _update_range_preview():
    control = _control("txtRangePreview")
    if control is None:
        return
    try:
        control.Text = _range_preview_text(_export_range())
    except Exception:
        control.Text = "Exports the checked rows below."


def _pdf_mode():
    separate = _is_checked("chkSeparate", "chkSeparateFiles")
    combined = _is_checked("chkCombined", "chkCombineFiles")
    if not separate and not combined:
        separate = True
    if separate and combined:
        raise ValueError("Select either Separate PDF or Combined PDF, not both.")
    return "combined" if combined else "separate"


def _element_name_of(element):
    try:
        return element.Name or ""
    except Exception:
        return ""


def _active_view_item(active_view):
    """Wrap the active Revit view/sheet as a single-item export selection."""
    if active_view is None:
        raise ValueError(
            "Could not determine the view or sheet currently open and "
            "active in Revit."
        )
    if not helpers.is_printable(active_view):
        raise ValueError(
            "The active view/sheet ('{}') can't be exported -- switch to a "
            "printable sheet or view in Revit first.".format(
                helpers.element_label(active_view)
            )
        )

    try:
        number = active_view.SheetNumber
    except Exception:
        number = view_search.friendly_view_type(active_view)

    item = helpers.ExportItem(active_view, number=number, name=_element_name_of(active_view))
    item.IsSelected = True
    return item


class _ExportJob(object):
    """Everything the export needs, captured on the UI thread at click time."""
    pass


def _collect_export_job():
    job = _ExportJob()
    job.export_range = _export_range()
    job.pdf = _is_checked("chkPDF")
    job.dwg = _is_checked("chkDWG")
    job.items = []
    if job.export_range == "selected":
        job.items = helpers.selected_items(current_items)
        if not job.items:
            raise _StepError("Tick at least one sheet or view to export.", TAB_SELECTION)

    if not job.pdf and not job.dwg:
        raise _StepError("Switch on PDF, DWG, or both (the buttons above the list).", TAB_SELECTION)

    job.folder = _output_folder()
    _remember_folder(job.folder)
    job.pdf_mode = _pdf_mode() if job.pdf else None
    job.pdf_settings = _collect_pdf_settings()
    job.pdf_printer = None
    if job.pdf and _is_checked("rbEnginePrinter"):
        job.pdf_printer = _combo_text("cmbPrinter", "")
        if not job.pdf_printer:
            raise _StepError("Choose the PDF printer to print through.", TAB_FORMAT)
        if job.pdf_mode == "combined":
            raise _StepError(
                "Printing through a PDF printer makes one PDF per sheet / view.\n\n"
                "For one combined PDF choose \"Revit PDF export\" as the PDF engine, "
                "or choose \"One PDF per sheet / view\".", TAB_FORMAT)
    job.dwg_setup = _selected_dwg_setup()
    job.bind_ole = _is_checked("chkBindOLE")

    job.package_name = None
    if job.pdf and job.pdf_mode == "combined":
        # Asked BEFORE the progress bar opens (it used to pop up behind it).
        job.package_name = forms.ask_for_string(
            default="Combined Set",
            prompt="Enter combined PDF filename",
            title="Combined PDF"
        )
        if not job.package_name:
            return None
    return job


def _run_export_job(uiapp, job):
    """Runs inside the ExternalEvent (valid Revit API context)."""
    try:
        if not helpers.doc_is_valid():
            raise ValueError("The model this window was opened for has been closed.")
        doc = helpers.doc
        uidoc = helpers.active_uidoc()

        if job.export_range in ("current", "visible"):
            if uidoc is None:
                raise ValueError(
                    "Revit's active window is showing a different model. "
                    "Switch back to '{}' first.".format(helpers.document_title())
                )
            selected = [_active_view_item(uidoc.ActiveView)]
        else:
            # Drop rows whose element was deleted while the window was open.
            selected = [i for i in job.items if _element_alive(i.Element)]
            if not selected:
                raise ValueError(
                    "The selected sheets/views no longer exist in the model. "
                    "Click 'Refresh from model'."
                )

        _refresh_custom_filenames(selected)
        active_view = selected[0].Element if job.export_range == "visible" else None

        warnings = []
        results = [{"warnings": warnings}]
        cancelled = False
        steps = len(selected) * (int(job.pdf) + int(job.dwg)) or 1

        # FIX: Cancel did nothing while printing. While Revit prints a sheet it does not answer
        # Windows, and a click on its (then "not responding") windows is thrown away - so the
        # Cancel of the progress bar only counted in the short moment between two sheets, and
        # even then it was noticed one step late. Now: a small Cancel window on a thread of its
        # own takes the click at any time (see cancel_watch.py) - the Esc key does not cancel - and
        # the request is looked at both before and after every screen update.
        with cancel_watch.CancelWatch() as watch, forms.ProgressBar(title="Exporting", cancellable=True) as progress:
            counter = [0]
            # FIX: pyRevit's progress bar closes itself on Esc (every pyRevit window does) - Esc pressed while it had
            # the focus took the progress window away in the middle of a print run. Esc does nothing there now.
            try:
                progress.PreviewKeyDown -= progress.handle_input_key
            except Exception:
                pass

            def stop_wanted():
                if bool(progress.cancelled):
                    why = "the Cancel of the progress bar"
                elif watch.wanted():
                    why = getattr(watch, "reason", None) or "the Cancel window"
                else:
                    return False
                if not getattr(stop_wanted, "logged", False):
                    stop_wanted.logged = True
                    try:
                        export_print.log("CANCELLED by the user: {0}".format(why))
                    except Exception:
                        pass
                return True

            def progress_step(current, total, message):
                if stop_wanted():
                    raise helpers.ExportCancelled()
                # "Binding ..." and "Waiting ..." are sub-steps of one item
                if not str(message).startswith(("Binding", "Waiting", "Another Revit")):
                    counter[0] += 1
                try:
                    _progress_message(message)                 # the Create step's file list
                except Exception:
                    pass
                try:
                    progress.update_progress(min(counter[0], steps), steps)
                    progress.title = message
                except Exception:
                    pass                                       # a progress window that is gone never stops the export
                # the update above lets Windows deliver a click on Cancel: act on it now, not one step later
                if stop_wanted():
                    raise helpers.ExportCancelled()

            try:
                if job.pdf:
                    factory = _make_pdf_options_factory(job.pdf_settings, warnings)
                    results.append(_safe_engine(
                        "PDF", _export_pdf, doc, selected, job, factory,
                        progress_step, uidoc, active_view
                    ))
                if job.dwg:
                    results.append(_safe_engine(
                        "DWG", export_dwg.export_items, doc, selected, job.folder,
                        job.dwg_setup, _filename_for_item,
                        progress_callback=progress_step,
                        bind_images_as_ole=job.bind_ole,
                        export_range=job.export_range,
                        uidoc=uidoc,
                        active_view=active_view
                    ))
            except helpers.ExportCancelled as stop:
                cancelled = True
                if stop.partial:
                    results.append(stop.partial)

        merged = helpers.merge_results(*results)
        try:
            _apply_results_to_rows(merged, cancelled)
        except Exception:
            pass
        heading = "Export Cancelled" if cancelled else "Export Complete"
        text = helpers.summary_text(merged, heading=heading)
        text += "\n\nFolder: {}".format(job.folder)
        choice = _alert(text, options=["Open Folder", "Close"])
        if choice == "Open Folder":
            try:
                os.startfile(job.folder)
            except Exception:
                pass
    except Exception as error:
        _alert(str(error), title="Export Error")
    finally:
        _set_busy(False)


def _export_pdf(doc, selected, job, factory, progress_step, uidoc, active_view):
    if getattr(job, "pdf_printer", None):
        # print through a PDF printer (PDF24 ...), the way ProSheets does
        return export_print.export_separate(
            doc, selected, job.folder, _filename_for_item, job.pdf_settings, job.pdf_printer,
            progress_callback=progress_step,
            export_range=job.export_range,
            uidoc=uidoc,
            active_view=active_view
        )
    if job.pdf_mode == "combined":
        return export_pdf.export_combined(
            doc, selected, job.folder, job.package_name,
            options_factory=factory,
            progress_callback=progress_step,
            export_range=job.export_range,
            uidoc=uidoc,
            active_view=active_view
        )
    return export_pdf.export_separate(
        doc, selected, job.folder, _filename_for_item,
        options_factory=factory,
        progress_callback=progress_step,
        export_range=job.export_range,
        uidoc=uidoc,
        active_view=active_view
    )


def _safe_engine(name, function, *args, **kwargs):
    """Run one export engine; a crash in DWG no longer discards the PDF
    results of the same run (and vice versa)."""
    try:
        return function(*args, **kwargs)
    except helpers.ExportCancelled:
        raise
    except Exception as error:
        return {"errors": [{"name": "{} export".format(name), "error": str(error)}]}


def _element_alive(element):
    try:
        return element is not None and element.IsValidObject
    except Exception:
        return False


def _set_busy(busy, message=None):
    global _busy
    _busy = busy
    for name in ("btnExport", "btnRefresh"):
        control = _control(name)
        if control is not None:
            try:
                control.IsEnabled = not busy
            except Exception:
                pass
    export_button = _control("btnExport")
    if export_button is not None:
        try:
            export_button.Content = (message or "WORKING...") if busy else "CREATE"
        except Exception:
            pass
    try:
        window.Cursor = Cursors.Wait if busy else None
    except Exception:
        pass


def _on_dispatch_error(error, trace):
    _set_busy(False)
    _alert("{}\n\n{}".format(error, trace), title="Export Manager - Unexpected Error")


def _guard_document():
    """True if the bound model is still open; closes the window otherwise."""
    if helpers.doc_is_valid():
        return True
    _alert("The model this Export Manager was opened for has been closed.")
    try:
        window.Close()
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# XAML event handlers
# ---------------------------------------------------------------------------

def rbSheets_Checked(sender, args):
    if window is not None and not _loading and _guard_document():
        _activate_mode("Sheets")


def rbViews_Checked(sender, args):
    if window is not None and not _loading and _guard_document():
        _activate_mode("Views")


def txtSearch_TextChanged(sender, args):
    _schedule_search()


def txtSearch_PreviewKeyDown(sender, args):
    if args.Key == Key.Escape:
        window.txtSearch.Text = ""
        args.Handled = True
    elif args.Key == Key.Enter:
        if _search_timer is not None:
            _search_timer.Stop()
        filter_items()
        args.Handled = True


def btnClearSearch_Click(sender, args):
    window.txtSearch.Text = ""
    if _search_timer is not None:
        _search_timer.Stop()
    filter_items()
    try:
        window.txtSearch.Focus()
    except Exception:
        pass


def view_filter_SelectionChanged(sender, args):
    if not _loading and current_mode == "Views":
        filter_items()


def btnViewResetFilters_Click(sender, args):
    global _loading
    _loading = True
    try:
        for name in ("cmbViewType", "cmbViewLevel", "cmbViewSheetState"):
            combo = _control(name)
            if combo is not None:
                combo.SelectedIndex = 0
        window.txtSearch.Text = ""
    finally:
        _loading = False
    if _search_timer is not None:
        _search_timer.Stop()
    filter_items()


def _check_rows(rows, state):
    for item in rows:
        item.IsSelected = state
    helpers.refresh_grid(window.dgItems)
    _refresh_count()


def chkSelectAll_Click(sender, args):
    """Tick every row shown -- or untick them when they are all ticked.

    Decided from the rows, not from the box's own state: the box is also
    set from code (_sync_select_all) and may be showing the 'some' dash.
    """
    if not displayed_items:
        _sync_select_all()
        return
    all_ticked = all(item.IsSelected for item in displayed_items)
    _check_rows(displayed_items, not all_ticked)


def btnFilterSelectAll_Click(sender, args):
    """Tick every row the current filter/search is showing."""
    if not displayed_items:
        _alert("No rows match the current search/filter.")
        return
    _check_rows(displayed_items, True)


def btnFilterDeselectAll_Click(sender, args):
    """Clear every tick, including rows hidden by the filter -- otherwise
    hidden-but-checked rows would still be exported."""
    _reset_select_all()
    _check_rows(current_items, False)


def _highlighted_items():
    result = []
    try:
        for row in window.dgItems.SelectedItems:
            if isinstance(row, helpers.ExportItem):
                result.append(row)
    except Exception:
        pass
    return result


def dgItems_CheckBoxClick(sender, args):
    """One click on a tick box.  If several rows are highlighted and the
    clicked row is one of them, apply the same tick to all of them."""
    source = args.OriginalSource
    item = getattr(source, "DataContext", None)
    if isinstance(item, helpers.ExportItem):
        highlighted = _highlighted_items()
        if len(highlighted) > 1 and any(row is item for row in highlighted):
            state = bool(item.IsSelected)
            for row in highlighted:
                row.IsSelected = state
            helpers.refresh_grid(window.dgItems)
    _refresh_count()


def dgItems_PreviewKeyDown(sender, args):
    """Space toggles the ticks of every highlighted row."""
    if args.Key != Key.Space:
        return
    highlighted = _highlighted_items()
    if not highlighted:
        return
    state = not all(row.IsSelected for row in highlighted)
    _check_rows(highlighted, state)
    args.Handled = True


def dgItems_CurrentCellChanged(sender, args):
    _refresh_count()


def range_Checked(sender, args):
    _update_range_preview()
    _update_status()


def cmbSheetSet_SelectionChanged(sender, args):
    if not _loading and window is not None:
        filter_items()


def btnOpenFolder_Click(sender, args):
    folder = _folder_text()
    if not folder or not os.path.isdir(folder):
        _alert("Choose an existing output folder first (Browse).")
        return
    try:
        os.startfile(folder)
    except Exception as error:
        _alert("Could not open the folder:\n{}".format(error))


def btnBrowse_Click(sender, args):
    folder = forms.pick_folder()
    if folder:
        box = _control("txtFolder", "txtOutputFolder")
        if box is not None:
            box.Text = folder


def btnSaveSelectionSet_Click(sender, args):
    checked = helpers.selected_items(current_items)
    if not checked:
        _alert("Check the sheets/views you want to save first.")
        return

    name = forms.ask_for_string(
        default="My Set",
        prompt="Enter a name for this selection set",
        title="Save Selection Set"
    )
    if not name:
        return

    # Sets are stored as files: work with the name as it is stored.
    name = selection_sets.safe_name(name)
    if not name:
        _alert("Enter a valid name for the selection set.")
        return

    if selection_sets.set_exists(name):
        if not _alert("A selection set named '{}' already exists. Replace it?".format(name),
                      yes=True, no=True, title="Replace Selection Set"):
            return

    try:
        selection_sets.save_set(name, current_mode, checked)
    except Exception as error:
        _alert(str(error), title="Save Selection Set Error")
        return

    _refresh_selection_set_list(select_name=name)
    _alert("Saved '{}' with {} item(s).".format(name, len(checked)))


def btnLoadSelectionSet_Click(sender, args):
    name = _selected_selection_set_name()
    if not name:
        _alert("Choose a saved selection set first.")
        return

    try:
        data = selection_sets.load_set(name)
    except Exception as error:
        _alert(str(error), title="Load Selection Set Error")
        return

    if data.get("mode") != current_mode:
        _alert("'{}' was saved in {} mode. Switch to {} mode first, then load it."
               .format(name, data.get("mode"), data.get("mode")))
        return

    matched, missing = selection_sets.match_items(
        data.get("items", []), current_items, mode=current_mode
    )

    for item in current_items:
        item.IsSelected = False
    for item in matched:
        item.IsSelected = True

    helpers.refresh_grid(window.dgItems)
    _refresh_count()

    message = "Loaded '{}': {} item(s) selected.".format(name, len(matched))
    if missing:
        message += "\n{} saved item(s) could not be found in the model anymore.".format(missing)
    _alert(message)


def btnDeleteSelectionSet_Click(sender, args):
    name = _selected_selection_set_name()
    if not name:
        _alert("Choose a saved selection set first.")
        return
    if not _alert("Delete the saved selection set '{}'? This cannot be undone.".format(name),
                  yes=True, no=True, title="Delete Selection Set"):
        return
    selection_sets.delete_set(name)
    _refresh_selection_set_list()


def btnExport_Click(sender, args):
    if _busy or not _guard_document():
        return
    try:
        job = _collect_export_job()
    except _StepError as error:
        # EXPORT is reachable from every step, so take the user to the step
        # that needs fixing instead of only naming it.
        _go_to_tab(error.tab)
        _alert(str(error), title="Export Manager")
        return
    except Exception as error:
        _alert(str(error), title="Export Error")
        return
    if job is None:
        return   # user cancelled the combined-PDF name prompt

    _rebuild_progress_rows()          # fresh "Waiting" list for this run
    _go_to_tab(TAB_CREATE)            # the list shows the progress, file by file
    _set_busy(True, "CREATING...")
    try:
        dispatcher.run(lambda uiapp: _run_export_job(uiapp, job))
    except Exception as error:
        _set_busy(False)
        _alert(str(error), title="Export Error")


def _refresh_job(uiapp):
    try:
        if not _guard_document():
            return
        _load_from_model(keep_checked=True)
        load_dwg_setups()
        _activate_mode(current_mode)
    except Exception as error:
        _alert("Refresh failed:\n\n{}".format(error))
    finally:
        _set_busy(False)


def _browser_selection_job(uiapp):
    """Runs inside the ExternalEvent: tick the sheets / views selected in
    Revit (Project Browser, or a sheet / view selected anywhere) and untick
    the rest. Switches Sheets / Views mode when the selection only holds the
    other kind."""
    try:
        if not _guard_document():
            return
        uidoc = helpers.active_uidoc()
        if uidoc is None:
            _alert("Revit's active window is showing a different model. "
                   "Switch back to '{}' first.".format(helpers.document_title()))
            return
        ids = set()
        for element_id in uidoc.Selection.GetElementIds():
            ids.add(helpers.element_id_value(element_id))
        if not ids:
            _alert("Nothing is selected in Revit.\n\n"
                   "Select sheets or views in the Project Browser (Ctrl / Shift for several), "
                   "then click 'Tick Project Browser selection' again.")
            return

        sheet_hits = [i for i in sheet_items if helpers.element_id_value(i.Element.Id) in ids]
        view_hits = [i for i in view_items if helpers.element_id_value(i.Element.Id) in ids]
        if not sheet_hits and not view_hits:
            _alert("The Revit selection holds {} element(s), but none of them is a sheet or view "
                   "in this list (view templates, schedules, legends or elements in a view are "
                   "not exported here).\n\nIf you just created the sheets / views, click "
                   "'Refresh from model' first.".format(len(ids)))
            return

        target = current_mode
        if target == "Sheets" and not sheet_hits:
            target = "Views"
        elif target == "Views" and not view_hits:
            target = "Sheets"
        if target != current_mode:
            radio = _control("rbViews" if target == "Views" else "rbSheets")
            if radio is not None:
                radio.IsChecked = True           # rbXxx_Checked -> _activate_mode
            if current_mode != target:
                _activate_mode(target)

        hits = sheet_hits if target == "Sheets" else view_hits
        wanted = set(id(i) for i in hits)
        for item in current_items:
            item.IsSelected = id(item) in wanted
        helpers.refresh_grid(window.dgItems)
        _refresh_count()
        _update_status()

        other = view_hits if target == "Sheets" else sheet_hits
        message = "{} {} ticked from the Revit selection.".format(
            len(hits), "sheet(s)" if target == "Sheets" else "view(s)")
        if other:
            message += ("\n\nThe selection also holds {} {} - switch to {} mode and click the "
                        "button again to tick those.".format(
                            len(other), "view(s)" if target == "Sheets" else "sheet(s)",
                            "Views" if target == "Sheets" else "Sheets"))
        _set_text("txtBrowserPick", message.split("\n")[0])
        if other:
            _alert(message)
    except Exception as error:
        _alert("Could not read the Revit selection:\n\n{}".format(error))
    finally:
        _set_busy(False)


def btnFromBrowser_Click(sender, args):
    if _busy or not _guard_document():
        return
    _set_busy(True, "READING SELECTION...")
    try:
        dispatcher.run(_browser_selection_job)
    except Exception as error:
        _set_busy(False)
        _alert(str(error))


def btnRefresh_Click(sender, args):
    if _busy or not _guard_document():
        return
    _set_busy(True, "REFRESHING...")
    try:
        dispatcher.run(_refresh_job)
    except Exception as error:
        _set_busy(False)
        _alert(str(error))


def btnNext_Click(sender, args):
    tabs = _control("tabMain")
    if tabs is not None and tabs.SelectedIndex < tabs.Items.Count - 1:
        tabs.SelectedIndex += 1


def btnBack_Click(sender, args):
    tabs = _control("tabMain")
    if tabs is not None and tabs.SelectedIndex > 0:
        tabs.SelectedIndex -= 1


def tabMain_SelectionChanged(sender, args):
    # SelectionChanged bubbles: every ComboBox, ListBox and DataGrid inside
    # the tabs raises it too.  Only react to the TabControl itself.
    if not System.Object.ReferenceEquals(args.OriginalSource, sender):
        return
    _update_nav()
    _update_status()


def window_Activated(sender, args):
    """User came back from Revit (maybe after opening another view)."""
    _update_range_preview()


# ---------------------------------------------------------------------------
# Revit application events (modeless life-cycle)
# ---------------------------------------------------------------------------

def _on_view_activated(sender, args):
    try:
        _update_range_preview()
    except Exception:
        pass


def _on_document_closing(sender, args):
    try:
        if helpers.same_document(args.Document, helpers.doc) and window is not None:
            window.Close()
    except Exception:
        pass


def _subscribe(owner, event_name, handler):
    try:
        event = getattr(owner, event_name)
        event += handler
        _subscriptions.append((owner, event_name, handler))
    except Exception:
        pass


def _unsubscribe_all():
    """Detach every Revit event handler.  Returns the ones Revit refused
    to detach (see _release_revit_hooks)."""
    refused = []
    while _subscriptions:
        entry = _subscriptions.pop()
        owner, event_name, handler = entry
        try:
            event = getattr(owner, event_name)
            event -= handler
        except Exception:
            refused.append(entry)
    return refused


def _release_revit_hooks():
    """Detach the Revit events and dispose the ExternalEvent on close.

    FIX: Revit only allows events to be (un)subscribed from inside an API
    context.  The window's Closed event is a plain WPF event, so when the
    user closed the window with its X the '-=' raised "not within an API
    context", the bare except swallowed it, and ViewActivated /
    DocumentClosing stayed hooked for the rest of the Revit session -- one
    more dead pair every time the tool was opened and closed.  If the
    direct attempt is refused, the detach is now queued through the
    ExternalEvent, and the event is disposed only after that has run.
    """
    closing = dispatcher
    refused = _unsubscribe_all()
    if closing is None:
        return
    if not refused:
        closing.dispose()
        return

    def _finish(uiapp):
        _subscriptions.extend(refused)
        _unsubscribe_all()
        # Dispose after Execute() has returned, not from inside it.
        try:
            System.Windows.Threading.Dispatcher.CurrentDispatcher.BeginInvoke(
                System.Action(closing.dispose))
        except Exception:
            pass

    try:
        closing.run(_finish)
    except Exception:
        closing.dispose()


def window_Closed(sender, args):
    global window
    for timer in (_search_timer, _filename_timer):
        try:
            timer.Stop()
        except Exception:
            pass
    _release_revit_hooks()
    try:
        domain = System.AppDomain.CurrentDomain
        domain.SetData(WINDOW_KEY, None)
        domain.SetData(WINDOW_DOC_KEY, None)
    except Exception:
        pass
    window = None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def _bind(name, event_name, handler):
    control = _control(name)
    if control is None:
        return
    try:
        event = getattr(control, event_name)
        event += handler
    except Exception:
        pass


def bind_events():
    """Connect required controls and silently skip optional ones."""
    window.rbSheets.Checked += rbSheets_Checked
    window.rbViews.Checked += rbViews_Checked
    window.txtSearch.TextChanged += txtSearch_TextChanged
    window.txtSearch.PreviewKeyDown += txtSearch_PreviewKeyDown
    window.chkSelectAll.Click += chkSelectAll_Click
    window.dgItems.CurrentCellChanged += dgItems_CurrentCellChanged
    window.dgItems.PreviewKeyDown += dgItems_PreviewKeyDown
    # Tick-box clicks bubble up from the template column's CheckBox.
    window.dgItems.AddHandler(ButtonBase.ClickEvent, RoutedEventHandler(dgItems_CheckBoxClick))
    window.btnExport.Click += btnExport_Click

    _bind("btnBrowse", "Click", btnBrowse_Click)
    _bind("btnBrowseFolder", "Click", btnBrowse_Click)
    _bind("btnNext", "Click", btnNext_Click)
    _bind("btnBack", "Click", btnBack_Click)
    _bind("btnRefresh", "Click", btnRefresh_Click)
    _bind("btnClearSearch", "Click", btnClearSearch_Click)
    _bind("btnProSheets", "Click", btnProSheets_Click)

    _bind("btnFromBrowser", "Click", btnFromBrowser_Click)
    _bind("btnSaveSelectionSet", "Click", btnSaveSelectionSet_Click)
    _bind("btnLoadSelectionSet", "Click", btnLoadSelectionSet_Click)
    _bind("btnDeleteSelectionSet", "Click", btnDeleteSelectionSet_Click)

    _bind("rbRangeSelected", "Checked", range_Checked)
    _bind("rbRangeCurrentWindow", "Checked", range_Checked)
    _bind("rbRangeVisiblePortion", "Checked", range_Checked)

    _bind("btnFilterSelectAll", "Click", btnFilterSelectAll_Click)
    _bind("btnFilterDeselectAll", "Click", btnFilterDeselectAll_Click)

    # Views-mode search panel
    _bind("cmbViewType", "SelectionChanged", view_filter_SelectionChanged)
    _bind("cmbViewLevel", "SelectionChanged", view_filter_SelectionChanged)
    _bind("cmbViewSheetState", "SelectionChanged", view_filter_SelectionChanged)
    _bind("btnViewSelectMatching", "Click", btnFilterSelectAll_Click)
    _bind("btnViewDeselectAll", "Click", btnFilterDeselectAll_Click)
    _bind("btnViewResetFilters", "Click", btnViewResetFilters_Click)

    # ProSheets layout: View/Sheet Set filter, open the output folder
    _bind("cmbSheetSet", "SelectionChanged", cmbSheetSet_SelectionChanged)
    _bind("btnOpenFolder", "Click", btnOpenFolder_Click)

    # Wizard navigation state + live footer / summary
    _bind("tabMain", "SelectionChanged", tabMain_SelectionChanged)
    for name in ("chkPDF", "chkDWG", "chkSeparate", "chkCombined"):
        _bind(name, "Click", _status_changed)
    _bind("cmbDWGSetup", "SelectionChanged", _status_changed)
    _bind("chkBindOLE", "Click", _bind_ole_Click)
    _bind("txtFolder", "TextChanged", _status_changed)

    window.Activated += window_Activated
    window.Closed += window_Closed

    filename_builder.bind_events()
    format_manager.bind_events()
    sheet_filter_ui.bind_events()


def _fit_to_screen():
    """Never open taller/wider than the usable screen (small laptops)."""
    try:
        area = SystemParameters.WorkArea
        window.Width = max(window.MinWidth, min(window.Width, area.Width - 40))
        window.Height = max(window.MinHeight, min(window.Height, area.Height - 40))
    except Exception:
        pass


def _make_timer(milliseconds, handler):
    timer = DispatcherTimer()
    timer.Interval = TimeSpan.FromMilliseconds(milliseconds)
    timer.Tick += handler
    return timer


def run(doc, uiapp):
    """Build and show the modeless window.

    MUST be called from the pushbutton command (valid API context): both
    ExternalEvent.Create and the initial model read require it.
    """
    global dispatcher

    helpers.set_context(doc, uiapp)
    dispatcher = revit_event.RevitDispatcher("Export Manager", on_error=_on_dispatch_error)
    try:
        return _build_and_show(doc, uiapp)
    except Exception:
        # Don't leak the ExternalEvent / Revit event subscriptions if the
        # window failed to build (bad XAML, closed doc, ...).
        _unsubscribe_all()
        dispatcher.dispose()
        raise


def _build_and_show(doc, uiapp):
    global window, _search_timer, _filename_timer, _loading

    # handle_esc=False: Esc must clear the search box, not close a
    # long-lived modeless tool by accident.
    window = forms.WPFWindow(_xaml_path(), handle_esc=False)
    window.Title = "Export Manager - {}".format(helpers.document_title(doc))
    _set_text("txtDocName", helpers.document_title(doc))
    _fit_to_screen()

    _search_timer = _make_timer(SEARCH_DELAY_MS, _on_search_timer)
    _filename_timer = _make_timer(FILENAME_DELAY_MS, _on_filename_timer)

    filename_builder.configure(window, doc, on_change=_on_filename_sequence_changed)
    format_manager.configure(window)
    selection_sets.configure()
    sheet_filter_ui.configure(window, doc, on_change=filter_items)

    _loading = True
    try:
        bind_events()
        load_dwg_setups()
        _load_from_model()
    finally:
        _loading = False

    remembered = _remembered_folder()
    if remembered and not _folder_text():
        _set_text("txtFolder", remembered)
    try:                                                   # "Bind images as OLE" as the user left it last time
        box = _control("chkBindOLE")
        if box is not None:
            box.IsChecked = bool(_read_settings().get("bind_ole", False))
    except Exception:
        pass

    # File name prefix / suffix as the user left them last time
    try:
        saved = _read_settings()
        filename_builder.set_affixes(saved.get("file_prefix", ""), saved.get("file_suffix", ""),
                                     notify=False)
        filename_builder.show_affixes()
        for name in ("txtFilePrefix", "txtFileSuffix"):
            box = _control(name)
            if box is not None:
                box.TextChanged += _remember_affixes
    except Exception:
        pass

    # Start with the same PDF settings as the user's ProSheets profile, when
    # there is one on this machine (silent when there is not).
    try:
        load_printers()
        apply_prosheets_settings(announce=False)
    except Exception:
        pass

    _activate_mode("Sheets")
    _update_range_preview()
    _update_nav()
    _update_status()

    _subscribe(uiapp, "ViewActivated", _on_view_activated)
    _subscribe(uiapp.Application, "DocumentClosing", _on_document_closing)

    domain = System.AppDomain.CurrentDomain
    domain.SetData(WINDOW_KEY, window)
    domain.SetData(WINDOW_DOC_KEY, doc)

    window.show()   # modeless -- returns immediately
    return window
