# -*- coding: utf-8 -*-
"""Before/after review dialog for the Load Transfer tool.

Shows one row per prospective write -- element, parameter, OLD value,
NEW value -- so the whole change set can be read before anything is
committed. The dialog is built from the same logic.build_transfer_report()
data the transfer itself runs on, and it is handed the already-filtered
match list, so what's on screen cannot drift from what gets written.

Two exit paths:
    show(rows, allow_confirm=True)  -> True if the user pressed
                                       CONFIRM TRANSFER, else False.
    show(rows, allow_confirm=False) -> always False; used for the
                                       read-only "look, don't touch"
                                       button on the main window.
"""

import os

from pyrevit import forms

import logic


def _format_value(value):
    """Display form of a parameter value.

    Anything numeric -- whether it arrived as a float from a Double
    parameter or as text from a Text parameter -- goes through
    logic.format_number, so OLD and NEW are always rendered identically.
    The previous version stripped trailing zeros from floats only, which
    put '30.00' (stored text) next to '30' (source float) and made an
    unchanged row look like a pending write.
    """
    if value is None:
        return "(empty)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = logic.format_number(value)
    if text is not None:
        return text
    text = str(value).strip()
    return text if text else "(empty)"


def _format_delta(delta):
    if delta is None:
        return ""
    formatted = _format_value(abs(delta))
    if delta > 0:
        return "+{}".format(formatted)
    if delta < 0:
        return "-{}".format(formatted)
    return ""


class ReportRow(object):
    """One prospective write, ready for the DataGrid."""

    def __init__(self, raw):
        self._raw = raw

        self.TargetName = raw["target_label"]
        self.MatchValue = raw["match_value"]
        self.SourceName = raw["source_label"]
        self.ParamPair = "{} -> {}".format(raw["source_param"], raw["target_param"])
        self.OldValue = _format_value(raw["old_value"])
        self.NewValue = "--" if raw["new_value"] is None else _format_value(raw["new_value"])

        change = raw["change"]
        delta_text = _format_delta(raw["delta"])
        if change in ("increase", "decrease") and delta_text:
            self.ChangeText = "{} ({})".format(change, delta_text)
        else:
            self.ChangeText = change

        if not raw["will_write"]:
            self.StatusText = "BLOCKED - {}".format(raw["issue"] or "cannot write")
            self.RowKind = "blocked"
        elif change == "no change":
            self.StatusText = "Already correct - value unchanged"
            self.RowKind = "unchanged"
        elif raw.get("warning"):
            self.StatusText = "Will write. WARNING: {}".format(raw["warning"])
            self.RowKind = "warning"
        else:
            self.StatusText = "Will write"
            self.RowKind = "write"

    def search_blob(self):
        return " ".join([
            self.TargetName, self.MatchValue, self.SourceName,
            self.ParamPair, self.OldValue, self.NewValue,
            self.ChangeText, self.StatusText
        ]).lower()


def _csv_cell(value):
    text = "" if value is None else str(value)
    if any(character in text for character in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def _export_csv(rows):
    """Write the visible report to CSV. Raw (unformatted) numbers are
    exported alongside the display text so the file can be used for
    checking in Excel without re-parsing '(empty)' and friends."""
    path = forms.save_file(file_ext="csv", default_name="LoadTransfer_Preview")
    if not path:
        return None

    headers = [
        "Detail Item", "Match Value", "Source", "Source Parameter",
        "Target Parameter", "Old Value", "New Value", "Old Raw", "New Raw",
        "Change", "Status"
    ]

    lines = [",".join(headers)]
    for row in rows:
        raw = row._raw
        lines.append(",".join(_csv_cell(cell) for cell in [
            row.TargetName, row.MatchValue, row.SourceName,
            raw["source_param"], raw["target_param"],
            row.OldValue, row.NewValue,
            "" if raw["old_value"] is None else raw["old_value"],
            "" if raw["new_value"] is None else raw["new_value"],
            row.ChangeText, row.StatusText
        ]))

    with open(path, "w") as stream:
        stream.write("\n".join(lines))
    return path


def _xaml_path():
    return os.path.join(os.path.dirname(__file__), "TransferPreview.xaml")


def show(raw_rows, allow_confirm=True, context_note="", owner=None):
    """Display the before/after table. Returns True only if the user
    explicitly confirmed the transfer.

    owner: the Load Transfer window. Without an owner this dialog could open BEHIND the
    (modal) main window and look like nothing happened."""
    all_rows = [ReportRow(raw) for raw in raw_rows]
    totals = logic.report_totals(raw_rows)

    window = forms.WPFWindow(_xaml_path())
    try:
        if owner is not None:
            window.Owner = owner
        from System.Windows import SystemParameters, WindowStartupLocation
        area = SystemParameters.WorkArea
        window.Height = min(window.Height, area.Height * 0.9)
        window.Width = min(window.Width, area.Width * 0.92)
        if owner is None:
            window.WindowStartupLocation = WindowStartupLocation.CenterScreen
    except Exception:
        pass
    result = {"confirmed": False}

    def visible_rows():
        query = (window.txtSearch.Text or "").strip().lower()
        changes_only = bool(window.chkChangesOnly.IsChecked)
        problems_only = bool(window.chkProblemsOnly.IsChecked)

        rows = []
        for row in all_rows:
            if problems_only and row.RowKind != "blocked":
                continue
            if changes_only and row.RowKind in ("unchanged", "blocked"):
                continue
            if query and query not in row.search_blob():
                continue
            rows.append(row)
        return rows

    def refresh(sender=None, args=None):
        rows = visible_rows()
        window.dgReport.ItemsSource = None
        window.dgReport.ItemsSource = rows
        window.txtFooter.Text = (
            "Showing {} of {} row(s). Nothing has been written yet -- "
            "Cancel leaves the model untouched.".format(len(rows), len(all_rows))
        )

    window.txtSummary.Text = (
        "{} value(s) will change across {} detail item(s)."
        .format(totals["writes"], totals["elements"])
    )

    details = []
    if totals["unchanged"]:
        details.append(
            "{} row(s) already hold the new value and will be written "
            "with no visible change".format(totals["unchanged"])
        )
    if totals["blocked"]:
        details.append(
            "{} row(s) are BLOCKED and will be skipped (highlighted red)"
            .format(totals["blocked"])
        )
    if not details:
        details.append("No blocked rows -- every listed write can be applied.")
    if context_note:
        details.append(context_note)
    window.txtSubSummary.Text = ". ".join(details) + "."

    def on_confirm(sender, args):
        if totals["writes"] == 0:
            proceed = forms.alert(
                "Nothing in this list would actually change a value. "
                "Transfer anyway?",
                yes=True, no=True, title="Nothing to Change"
            )
            if not proceed:
                return
        result["confirmed"] = True
        window.Close()

    def on_cancel(sender, args):
        result["confirmed"] = False
        window.Close()

    def on_export(sender, args):
        try:
            path = _export_csv(visible_rows())
        except Exception as error:
            forms.alert("Could not write the CSV: {}".format(error), title="Export Failed")
            return
        if path:
            forms.alert("Saved: {}".format(path), title="Export Complete")

    if not allow_confirm:
        # Read-only mode: the confirm button would be a second, easily
        # mis-clicked way to start writing, so it's removed rather than
        # just disabled.
        try:
            from System.Windows import Visibility
            window.btnConfirm.Visibility = Visibility.Collapsed
        except Exception:
            window.btnConfirm.IsEnabled = False
        window.btnCancel.Content = "Close"

    window.btnConfirm.Click += on_confirm
    window.btnCancel.Click += on_cancel
    window.btnExport.Click += on_export
    window.txtSearch.TextChanged += refresh
    window.chkChangesOnly.Checked += refresh
    window.chkChangesOnly.Unchecked += refresh
    window.chkProblemsOnly.Checked += refresh
    window.chkProblemsOnly.Unchecked += refresh

    refresh()
    window.show_dialog()
    return result["confirmed"]
