# -*- coding: utf-8 -*-
"""SLD Checker - QA/QC results preview (modeless, same look as Cable Tray QA/QC).

WHY THIS IS NOT IN script.py
The window stays open after the button script has finished. pyRevit clears the
global names of a button script when it ends, so anything the window needs
later must live in an imported module - like this one.

READ-ONLY: the window only selects elements, opens views and zooms. Revit is
touched through an ExternalEvent (RevitRunner), never directly from a click.
"""
import os
import time

from pyrevit import forms

from sldcheck import checks, config, report, rules

XAML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results.xaml")
_OPEN_WINDOWS = []              # keeps the modeless window (and its ExternalEvent) alive

ALL_SEV = u"All severities"
ERR_WARN = u"Errors + warnings"
SEVERITIES = [ALL_SEV, ERR_WARN, checks.ERROR, checks.WARNING, checks.INFO]
ALL_ISSUES = u"All issue types"
# summary tiles that filter on a group of issues
SPECIAL = {
    u"ISSUE:Missing": (u"Missing", u"Issue LIKE 'Missing model element%' OR Issue LIKE 'Missing Detail Item%'"),
    u"ISSUE:Dup": (u"Duplicate / ambiguous", u"Issue LIKE 'Duplicate%' OR Issue LIKE 'Ambiguous%'"),
}
MAX_SELECT = 5000


# ---------------------------------------------------------------------------
# ExternalEvent runner - the only way a modeless window may call the Revit API
# ---------------------------------------------------------------------------
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler   # noqa: E402


class _Handler(IExternalEventHandler):
    def __init__(self):
        self.action = None
        self.on_error = None

    def Execute(self, uiapp):
        action, self.action = self.action, None
        if action is None:
            return
        try:
            action(uiapp)
        except Exception as error:
            if self.on_error is not None:
                try:
                    self.on_error(error)
                except Exception:
                    pass

    def GetName(self):
        return "SLD Checker QA/QC"


class RevitRunner(object):
    def __init__(self, on_error=None):
        self._handler = _Handler()
        self._handler.on_error = on_error
        self._event = ExternalEvent.Create(self._handler)

    def run(self, action):
        self._handler.action = action
        self._event.Raise()

    def dispose(self):
        try:
            self._event.Dispose()
        except Exception:
            pass


def _like(text):
    """Escape user text for a DataView RowFilter LIKE expression."""
    out = []
    for ch in text:
        if ch in u"[]*%":
            out.append(u"[" + ch + u"]")
        elif ch == u"'":
            out.append(u"''")
        else:
            out.append(ch)
    return u"".join(out)


def _ids_text(ids):
    shown = [rules.to_text(i) for i in ids[:3]]
    return u", ".join(shown) + (u" +{0}".format(len(ids) - 3) if len(ids) > 3 else u"")


# ---------------------------------------------------------------------------
class ResultsWindow(forms.WPFWindow):
    def __init__(self, doc, findings, stats, coverage, cfg, header_lines, id_map, log_path=None):
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.doc = doc
        self.findings = findings
        self.stats = stats
        self.coverage = coverage or {}
        self.cfg = cfg
        self.id_map = id_map
        self.log_path = log_path
        self.check_filter = None          # one check group, or None
        self.special = None               # key of SPECIAL, or None
        self.runner = RevitRunner(on_error=self._revit_error)
        self._building = False
        self._build_table()
        self._fill_header(header_lines)
        self._fill_tiles()
        self._fill_filters()
        self._wire()
        self._apply_filter()
        self._set_status(u"Double-click a row to open its SLD view and select the symbols. "
                         u"Click a tile or a check chip to filter.")

    # ------------------------------------------------------------------ setup
    def _build_table(self):
        import clr
        clr.AddReference("System.Data")
        import System
        from System.Data import DataTable
        table = DataTable("results")
        for name, kind in (("Idx", System.Int32), ("Severity", System.String),
                           ("Check", System.String), ("Issue", System.String),
                           ("Panel", System.String), ("Details", System.String),
                           ("Sld", System.String), ("Model", System.String),
                           ("View", System.String), ("SldIds", System.String),
                           ("ModelIds", System.String)):
            table.Columns.Add(name, clr.GetClrType(kind))
        table.BeginLoadData()
        for idx, item in enumerate(self.findings):
            row = table.NewRow()
            row["Idx"] = idx
            row["Severity"] = item["severity"]
            row["Check"] = item["check"]
            row["Issue"] = rules.to_text(item["issue"])
            row["Panel"] = rules.to_text(item["key"])
            row["Details"] = rules.to_text(item["message"])
            row["Sld"] = rules.to_text(item["sld"])
            row["Model"] = rules.to_text(item["model"])
            row["View"] = rules.to_text(item["view"])
            row["SldIds"] = _ids_text(item["sld_ids"])
            row["ModelIds"] = _ids_text(item["model_ids"])
            table.Rows.Add(row)
        table.EndLoadData()
        self.table = table
        self.grid.ItemsSource = table.DefaultView

    def _fill_header(self, header_lines):
        from System.Windows import Visibility
        self.model_tb.Text = u"{0}\n{1}".format(self.doc.Title, time.strftime("%d %b %Y  %H:%M"))
        plain = [report._plain(line) for line in header_lines or []]
        notes = [p for p in plain if p.lower().startswith(u"warning") or u"partial scope" in p.lower()]
        run = [p for p in plain if p not in notes and not p.lower().startswith(u"model:")]
        self.run_tb.Text = u"   |   ".join(run)
        self.warn_tb.Text = u"  ".join(notes)
        self.warn_panel.Visibility = Visibility.Visible if notes else Visibility.Collapsed
        found, missing = [], []
        for field in sorted(self.coverage):
            count, names = self.coverage[field]
            label = config.FIELD_LABELS.get(field, field)
            if count:
                found.append(u"{0} ({1:,} via {2})".format(label, count, u", ".join(names)))
            else:
                missing.append(label)
        text = u"{0} of {1} SLD fields found. ".format(len(found), len(self.coverage))
        if missing:
            text += u"NOT FOUND (not compared): {0}. Map them on the SLD Parameters tab. ".format(
                u", ".join(missing))
        text += u"Found: {0}.".format(u"; ".join(found) or u"none")
        self.mapping_tb.Text = text

    def _count(self, test):
        return len([f for f in self.findings if test(f)])

    def _fill_tiles(self):
        from System.Windows.Media import BrushConverter
        s = self.stats
        for name, key in (("t_sld", "sld_equipment"), ("t_feed", "sld_feeders"),
                          ("t_model", "model_equipment"), ("t_cct", "model_circuits"),
                          ("t_matched", "matched_equipment"), ("t_fmatched", "matched_feeders")):
            getattr(self, name).Text = u"{0:,}".format(s.get(key, 0))
        self.t_missing.Text = u"{0:,}".format(self._count(
            lambda f: f["issue"].startswith(u"Missing model element")
            or f["issue"].startswith(u"Missing Detail Item")))
        self.t_dup.Text = u"{0:,}".format(self._count(
            lambda f: f["issue"].startswith(u"Duplicate") or f["issue"].startswith(u"Ambiguous")))
        errors = self._count(lambda f: f["severity"] == checks.ERROR)
        self.t_warn.Text = u"{0:,}".format(self._count(lambda f: f["severity"] == checks.WARNING))
        self.t_info.Text = u"{0:,}".format(self._count(lambda f: f["severity"] == checks.INFO))
        self.t_errors.Text = u"{0:,}".format(errors)
        brush = BrushConverter().ConvertFromString(u"#00B85C" if errors == 0 else u"#E53935")
        self.errors_tile.Background = brush
        self.errors_tile.BorderBrush = brush

    def _fill_filters(self):
        self._building = True
        for label in SEVERITIES:
            self.sev_cb.Items.Add(label)
        self.sev_cb.SelectedIndex = 0
        counts = {}
        for item in self.findings:
            counts[item["issue"]] = counts.get(item["issue"], 0) + 1
        self.issue_cb.Items.Add(ALL_ISSUES)
        self._issue_names = {}
        for issue in sorted(counts, key=lambda k: (-counts[k], k)):
            label = u"{0}  ({1:,})".format(issue, counts[issue])
            self._issue_names[label] = issue
            self.issue_cb.Items.Add(label)
        self.issue_cb.SelectedIndex = 0
        self._build_chips()
        self._building = False

    def _build_chips(self):
        """One chip per check group: red = errors, orange = warnings only,
        green = clean. Click = show only that group (click again = all)."""
        from System.Windows import CornerRadius, FontWeights, Thickness
        from System.Windows.Controls import Border, TextBlock
        from System.Windows.Input import Cursors
        from System.Windows.Media import BrushConverter, Brushes
        conv = BrushConverter()
        self.group_panel.Children.Clear()
        self._chips = {}
        for check in [None] + list(checks.CHECK_ORDER):
            rows = [f for f in self.findings if check is None or f["check"] == check]
            err = len([f for f in rows if f["severity"] == checks.ERROR])
            warn = len([f for f in rows if f["severity"] == checks.WARNING])
            colour = u"#1F3A5F" if check is None else (
                u"#E53935" if err else (u"#FB8C00" if warn else u"#00B85C"))
            chip = Border()
            chip.Tag = check
            chip.CornerRadius = CornerRadius(11)
            chip.Padding = Thickness(10, 3, 10, 3)
            chip.Margin = Thickness(0, 0, 6, 0)
            chip.Cursor = Cursors.Hand
            chip.Background = conv.ConvertFromString(colour)
            chip.BorderBrush = Brushes.Transparent
            chip.BorderThickness = Thickness(2)
            chip.ToolTip = (u"Show every check" if check is None else
                            u"{0}: {1} error(s), {2} warning(s), {3} row(s)".format(
                                check, err, warn, len(rows)))
            text = TextBlock()
            text.Text = (u"All  {0:,}".format(len(rows)) if check is None else
                         u"{0}  {1:,}".format(check, len(rows)))
            text.FontSize = 11
            text.FontWeight = FontWeights.SemiBold
            text.Foreground = Brushes.White
            chip.Child = text
            chip.MouseLeftButtonUp += self._chip_clicked
            self.group_panel.Children.Add(chip)
            self._chips[check] = chip
        self._mark_chip()

    def _mark_chip(self):
        from System.Windows.Media import Brushes
        for check, chip in self._chips.items():
            chip.BorderBrush = Brushes.Black if check == self.check_filter else Brushes.Transparent

    def _wire(self):
        self.sev_cb.SelectionChanged += self._apply_filter
        self.issue_cb.SelectionChanged += self._issue_changed
        self.search_tb.TextChanged += self._apply_filter
        self.btn_reset.Click += self._reset
        for name in ("tile_sld", "tile_feed", "tile_model", "tile_cct", "tile_matched",
                     "tile_fmatched", "tile_missing", "tile_dup", "tile_warn", "tile_info",
                     "errors_tile"):
            getattr(self, name).MouseLeftButtonUp += self._tile_clicked
        self.grid.MouseDoubleClick += self._show_sld
        self.grid.SelectionChanged += self._row_selected
        self.btn_highlight.Click += self._highlight
        self.btn_clear.Click += self._clear
        self.btn_sld.Click += self._show_sld
        self.btn_model.Click += self._show_model
        self.btn_export.Click += self._export
        self.btn_log.Click += self._open_log
        self.btn_close.Click += lambda sender, args: self.Close()
        self.Closed += self._closed

    # ------------------------------------------------------------------ filtering
    def _chip_clicked(self, sender, args):
        check = sender.Tag
        self.check_filter = None if check == self.check_filter else check
        self._mark_chip()
        self._apply_filter()

    def _tile_clicked(self, sender, args):
        tag = sender.Tag
        self._building = True
        self.special = None
        self.issue_cb.SelectedIndex = 0
        if tag in SPECIAL:
            self.special = tag
            self.sev_cb.SelectedIndex = 0
        elif tag in (checks.ERROR, checks.WARNING, checks.INFO):
            self.sev_cb.SelectedItem = tag
        else:
            self.sev_cb.SelectedIndex = 0
            self.check_filter = None
            self._mark_chip()
        self._building = False
        self._apply_filter()

    def _issue_changed(self, sender, args):
        if not self._building:
            self.special = None
        self._apply_filter()

    def _reset(self, sender, args):
        self._building = True
        self.sev_cb.SelectedIndex = 0
        self.issue_cb.SelectedIndex = 0
        self.search_tb.Text = u""
        self.special = None
        self.check_filter = None
        self._mark_chip()
        self._building = False
        self._apply_filter()

    def _apply_filter(self, sender=None, args=None):
        if self._building:
            return
        from System.Windows import Visibility
        text = (self.search_tb.Text or u"").strip()
        self.search_hint.Visibility = Visibility.Collapsed if text else Visibility.Visible
        parts = []
        sev = self.sev_cb.SelectedItem
        if sev == ERR_WARN:
            parts.append(u"Severity <> 'INFO'")
        elif sev in (checks.ERROR, checks.WARNING, checks.INFO):
            parts.append(u"Severity = '{0}'".format(sev))
        issue = self._issue_names.get(self.issue_cb.SelectedItem)
        if issue:
            parts.append(u"Issue = '{0}'".format(issue.replace(u"'", u"''")))
        if self.special:
            parts.append(u"(" + SPECIAL[self.special][1] + u")")
        if self.check_filter:
            parts.append(u"Check = '{0}'".format(self.check_filter.replace(u"'", u"''")))
        for word in text.split():
            w = _like(word)
            parts.append(u"(Panel LIKE '%{0}%' OR Details LIKE '%{0}%' OR Sld LIKE '%{0}%' "
                         u"OR Model LIKE '%{0}%' OR View LIKE '%{0}%' OR Issue LIKE '%{0}%' "
                         u"OR SldIds LIKE '%{0}%' OR ModelIds LIKE '%{0}%')".format(w))
        try:
            self.table.DefaultView.RowFilter = u" AND ".join(parts)
        except Exception:
            self.table.DefaultView.RowFilter = u""
        label = u""
        if self.special:
            label = u"  ({0})".format(SPECIAL[self.special][0])
        self.shown_tb.Text = u"{0:,} of {1:,} rows{2}".format(
            self.table.DefaultView.Count, self.table.Rows.Count, label)
        self._update_buttons()

    def _shown(self):
        return [self.findings[int(view_row.Row["Idx"])] for view_row in self.table.DefaultView]

    def _selected(self):
        out = []
        for item in self.grid.SelectedItems:
            try:
                out.append(self.findings[int(item.Row["Idx"])])
            except Exception:
                continue
        return out

    def _row_selected(self, sender, args):
        rows = self._selected()
        if len(rows) == 1:
            f = rows[0]
            text = u"{0} | {1} | {2} | {3}".format(f["severity"], f["issue"], f["key"], f["message"])
            if f["sld"] or f["model"]:
                text += u"  SLD: {0}  Model: {1}".format(f["sld"] or u"-", f["model"] or u"-")
            self._set_status(text)
        self._update_buttons()

    def _update_buttons(self):
        rows = self._selected()
        self.btn_sld.IsEnabled = any(self._host_ids(r["sld_ids"]) for r in rows)
        self.btn_model.IsEnabled = any(self._host_ids(r["model_ids"]) for r in rows)
        self.btn_export.IsEnabled = self.table.DefaultView.Count > 0

    # ------------------------------------------------------------------ Revit actions
    def _set_status(self, text):
        self.status_tb.Text = text

    def _revit_error(self, error):
        self._set_status(u"Revit could not do that: {0}".format(error))

    def _closed(self, sender, args):
        self.runner.dispose()
        if self in _OPEN_WINDOWS:
            _OPEN_WINDOWS.remove(self)

    def _host_ids(self, ids):
        """ElementIds of the host model (linked ids are text and cannot be selected)."""
        return [self.id_map[i] for i in ids if i in self.id_map]

    def _in_revit(self, action):
        def run(uiapp):
            uidoc = uiapp.ActiveUIDocument
            try:
                same = uidoc is not None and uidoc.Document.Equals(self.doc)
            except Exception:
                same = False
            if not same:
                self._set_status(u"These results belong to '{0}'. Activate that model first."
                                 .format(self.doc.Title))
                return
            message = action(uidoc)
            if message:
                self._set_status(message)
        self.runner.run(run)

    @staticmethod
    def _id_list(doc, ids):
        from System.Collections.Generic import List
        from Autodesk.Revit.DB import ElementId
        out = List[ElementId]()
        for eid in ids:
            if doc.GetElement(eid) is not None:
                out.Add(eid)
        return out

    def _show_sld(self, sender=None, args=None):
        rows = self._selected()
        ids = []
        for r in rows:
            ids.extend(self._host_ids(r["sld_ids"]))
        if not ids:
            self._set_status(u"The selected row has no SLD item (it is a model-side issue) - "
                             u"use 'Show in model'.")
            return

        def action(uidoc):
            doc = uidoc.Document
            first = doc.GetElement(ids[0])
            if first is None:
                return u"The SLD item no longer exists - run the check again."
            view = doc.GetElement(first.OwnerViewId)
            # only the items of that one view can be selected and zoomed there
            same_view = [i for i in ids if doc.GetElement(i) is not None
                         and doc.GetElement(i).OwnerViewId == first.OwnerViewId]
            if view is not None and uidoc.ActiveView.Id != view.Id:
                uidoc.ActiveView = view
            selection = self._id_list(doc, same_view)
            uidoc.Selection.SetElementIds(selection)
            try:
                uidoc.ShowElements(selection)
            except Exception:
                pass
            other = len(ids) - len(same_view)
            return u"View '{0}': {1} SLD item(s) selected{2}.".format(
                view.Name if view is not None else u"?", selection.Count,
                u" ({0} more in other views)".format(other) if other else u"")
        self._in_revit(action)

    def _show_model(self, sender, args):
        rows = self._selected()
        ids, linked = [], 0
        for r in rows:
            ids.extend(self._host_ids(r["model_ids"]))
            linked += len([i for i in r["model_ids"] if i not in self.id_map])
        if not ids:
            self._set_status(u"No host-model element on this row{0}.".format(
                u" - it is in a linked model" if linked else u""))
            return

        def action(uidoc):
            selection = self._id_list(uidoc.Document, ids)
            uidoc.Selection.SetElementIds(selection)
            try:
                uidoc.ShowElements(selection)
            except Exception:
                pass
            return u"{0} model element(s) selected and shown{1}.".format(
                selection.Count, u"; {0} linked element(s) cannot be selected".format(linked)
                if linked else u"")
        self._in_revit(action)

    def _highlight(self, sender, args):
        rows = [r for r in self._shown() if r["severity"] == checks.ERROR]
        ids = []
        for r in rows:
            ids.extend(self._host_ids(r["sld_ids"]))
            ids.extend(self._host_ids(r["model_ids"]))
        ids = list(dict((rules.to_text(i), i) for i in ids).values())[:MAX_SELECT]
        if not ids:
            self._set_status(u"No error row in the list has a host element to select.")
            return

        def action(uidoc):
            selection = self._id_list(uidoc.Document, ids)
            uidoc.Selection.SetElementIds(selection)
            uidoc.RefreshActiveView()
            return (u"{0:,} element(s) of {1:,} error row(s) selected - they light up in every "
                    u"view you open. Selection only, nothing changed.".format(selection.Count, len(rows)))
        self._in_revit(action)

    def _clear(self, sender, args):
        def action(uidoc):
            from System.Collections.Generic import List
            from Autodesk.Revit.DB import ElementId
            uidoc.Selection.SetElementIds(List[ElementId]())
            uidoc.RefreshActiveView()
            return u"Selection cleared."
        self._in_revit(action)

    # ------------------------------------------------------------------ export
    def _export(self, sender, args):
        rows = self._shown()
        if not rows:
            return
        default = u"SLD_Check_{0}_{1}".format(
            (self.doc.Title or u"model").replace(u" ", u"_"), time.strftime("%Y%m%d_%H%M"))
        path = forms.save_file(file_ext="csv", default_name=default)
        if not path:
            return
        try:
            report.write_csv(path, rows)
        except Exception as error:
            forms.alert(u"Could not write the CSV:\n{0}".format(error))
            return
        self._set_status(u"{0:,} row(s) exported: {1}".format(len(rows), path))
        if forms.alert(u"{0:,} rows written to:\n{1}\n\nOpen it now?".format(len(rows), path),
                       yes=True, no=True):
            try:
                os.startfile(path)
            except Exception:
                pass

    def _open_log(self, sender, args):
        path = self.log_path or config.LOG_PATH
        if path and os.path.isfile(path):
            os.startfile(path)
        else:
            self._set_status(u"No run log found.")


def show(doc, findings, stats, coverage, cfg, header_lines, id_map, log_path=None):
    """Open the modeless results window and keep it alive."""
    window = ResultsWindow(doc, findings, stats, coverage, cfg, header_lines, id_map, log_path)
    _OPEN_WINDOWS.append(window)
    window.show()
    return window
