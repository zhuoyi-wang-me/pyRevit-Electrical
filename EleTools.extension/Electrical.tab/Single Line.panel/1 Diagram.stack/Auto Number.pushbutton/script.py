# -*- coding: utf-8 -*-
"""Select circuit breaker elements (detail items / family instances) on a
single-line diagram and automatically write sequential numbers such as
CB-01, CB-02, CB-03 ... into a chosen instance parameter (e.g. MEE-CBRef).

Elements already selected in Revit when the tool is launched are used as
the starting selection; otherwise you are prompted to pick elements.
A live preview lets you review every value before anything is written.
"""

__title__ = 'Auto Number'
__author__ = 'BHEEM'

import os
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')

from Autodesk.Revit.DB import Transaction, StorageType
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

from pyrevit import revit, forms, script

from cb_common import (
    is_tag_or_annotation, get_writable_string_param_names, sort_elements,
    get_type_name, get_category_name
)

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

XAML_FILE = os.path.join(os.path.dirname(__file__), 'ui.xaml')

# Parameters commonly used for breaker / equipment references.
# The combo box is editable, so the user can always type a different name.
SUGGESTED_PARAMETERS = [
    'MEE-CBRef',
    'MEE-EquipmentTag',
    'Mark',
    'Comments',
]



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class NoTagSelectionFilter(ISelectionFilter):
    """Used with PickObjects so tags simply can't be picked in the first
    place (Revit will show them as non-selectable while picking)."""

    def AllowElement(self, elem):
        return not is_tag_or_annotation(elem)

    def AllowReference(self, ref, point):
        return True


class PreviewRow(object):
    """Plain object bound to the DataGrid. IronPython instance attributes
    are exposed to WPF binding automatically, no extra plumbing needed."""

    def __init__(self, order, element, current_value, new_value):
        self.Order = order
        self.ElementId = _id_int(element.Id)
        self.Category = get_category_name(element)
        self.TypeName = get_type_name(doc, element)
        self.CurrentValue = current_value if current_value else ''
        self.NewValue = new_value
        self.ElementRef = element


class AutoNumberWindow(forms.WPFWindow):

    def __init__(self, xaml_source, elements):
        forms.WPFWindow.__init__(self, xaml_source)
        self.elements = elements
        self._populate_parameter_choices()
        self.txtSelectionInfo.Text = '{} element(s) selected'.format(len(self.elements))
        self.update_preview()

    # ---------------------------------------------------------------- utils
    def _populate_parameter_choices(self):
        found = []
        if self.elements:
            found = get_writable_string_param_names(self.elements[0])

        combined = []
        for name in SUGGESTED_PARAMETERS + found:
            if name not in combined:
                combined.append(name)

        self.cmbParameter.ItemsSource = combined
        if 'MEE-CBRef' in combined:
            self.cmbParameter.SelectedItem = 'MEE-CBRef'
        elif combined:
            self.cmbParameter.SelectedIndex = 0
        else:
            self.cmbParameter.Text = 'MEE-CBRef'

    def _build_values(self):
        param_name = (self.cmbParameter.Text or '').strip()
        prefix = self.txtPrefix.Text or ''

        try:
            start = int(self.txtStart.Text)
        except Exception:
            start = 1
        try:
            increment = int(self.txtIncrement.Text)
        except Exception:
            increment = 1
        try:
            padding = int(self.txtPadding.Text)
        except Exception:
            padding = 2

        ordered_elements = sort_elements(self.elements, self.cmbSort.SelectedIndex, doc.ActiveView)

        rows = []
        current = start
        for i, el in enumerate(ordered_elements):
            number_str = str(current).zfill(padding) if padding > 0 else str(current)
            new_value = '{}{}'.format(prefix, number_str)

            current_value = ''
            if param_name:
                p = el.LookupParameter(param_name)
                if p and p.StorageType == StorageType.String:
                    current_value = p.AsString()

            rows.append(PreviewRow(i + 1, el, current_value, new_value))
            current += increment

        return param_name, rows

    # ------------------------------------------------------------- actions
    def update_preview(self):
        self.txtStatus.Text = ''
        param_name, rows = self._build_values()
        self.dgPreview.ItemsSource = rows
        if not param_name:
            self.txtStatus.Text = 'Enter or select a target parameter name.'

    def btnRefresh_Click(self, sender, args):
        self.update_preview()

    def btnReselect_Click(self, sender, args):
        self.Close()
        new_elements = pick_elements()
        if new_elements:
            show_window(new_elements)

    def btnApply_Click(self, sender, args):
        param_name, rows = self._build_values()

        if not param_name:
            self.txtStatus.Text = 'Enter or select a target parameter name before applying.'
            return

        if not rows:
            self.txtStatus.Text = 'No elements to number.'
            return

        missing = []
        t = Transaction(doc, 'Auto Number Circuit Breakers')
        t.Start()
        try:
            applied = 0
            for row in rows:
                p = row.ElementRef.LookupParameter(param_name)
                if p is None or p.IsReadOnly or p.StorageType != StorageType.String:
                    missing.append(row.ElementId)
                    continue
                p.Set(row.NewValue)
                applied += 1
            t.Commit()
        except Exception as ex:
            t.RollBack()
            self.txtStatus.Text = 'Failed to apply numbering: {}'.format(ex)
            return

        self.Close()

        if missing:
            forms.alert(
                'Applied numbering to {} element(s).\n\n'
                'Skipped {} element(s) where "{}" does not exist, is read-only, '
                'or is not a text parameter (Element Ids: {}).'.format(
                    applied, len(missing), param_name,
                    ', '.join(str(m) for m in missing)
                ),
                title='Auto Number - Completed with warnings'
            )
        else:
            forms.alert(
                'Successfully applied numbering to {} element(s).'.format(applied),
                title='Auto Number - Completed'
            )

    def btnCancel_Click(self, sender, args):
        self.Close()


def pick_elements():
    """Use the current Revit selection if present, otherwise prompt the
    user to pick elements on screen (in the order they click them).
    Tags, text notes, dimensions, grids and levels are always excluded,
    even if they were caught in a box selection."""
    excluded_count = 0

    sel_ids = uidoc.Selection.GetElementIds()
    if sel_ids and len(sel_ids) > 0:
        all_elements = [doc.GetElement(eid) for eid in sel_ids]
        elements = [el for el in all_elements if not is_tag_or_annotation(el)]
        excluded_count = len(all_elements) - len(elements)
    else:
        try:
            refs = uidoc.Selection.PickObjects(
                ObjectType.Element,
                NoTagSelectionFilter(),
                'Select circuit breaker elements in the order you want them '
                'numbered (or set Sort Order to a position-based option). '
                'Tags and lines cannot be picked. Press Finish on the ribbon / status '
                'bar when done.'
            )
            elements = [doc.GetElement(r.ElementId) for r in refs]
        except Exception:
            return []

    if excluded_count:
        forms.alert(
            'Ignored {} tag/annotation/line element(s) from your selection '
            '(tags, text, dimensions, grids, levels and lines are never numbered).'.format(
                excluded_count
            ),
            title='Auto Number - Tags/Lines excluded'
        )

    return elements


def show_window(elements):
    window = AutoNumberWindow(XAML_FILE, elements)
    window.ShowDialog()


def main():
    elements = pick_elements()
    if not elements:
        script.exit()
        return
    show_window(elements)


if __name__ == '__main__':
    main()
