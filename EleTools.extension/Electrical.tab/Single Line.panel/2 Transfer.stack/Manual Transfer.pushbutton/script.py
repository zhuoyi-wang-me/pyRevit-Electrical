# -*- coding: utf-8 -*-
"""Manual, pair-at-a-time version of Transfer Parameter: click one source
element, then its matching target, and copy a value between them. Repeat
for each pair. This is the reliable option for far-apart or
irregularly-routed elements (e.g. a panel fed via a bent/routed line
rather than a straight drop) where automatic position-based matching
can't guess the connection correctly.

Unlike the main Transfer Parameter tool, this one launches straight into
the picking workflow - no mandatory bulk source/target selection step,
since manual pairing doesn't need it.
"""

__title__ = 'Manual\nTransfer'
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
    is_tag_or_annotation, get_type_name, get_string_param_value_multi,
    parse_param_name_list
)

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

XAML_FILE = os.path.join(os.path.dirname(__file__), 'ui.xaml')

SOURCE_SUGGESTED_PARAMETERS = ['MEE-Component', 'MEE-EquipmentTag', 'Mark']
TARGET_SUGGESTED_PARAMETERS = ['MEE-Feeder', 'MEE-FedFrom', 'MEE-CableRef']



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class NoTagSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return not is_tag_or_annotation(elem)

    def AllowReference(self, ref, point):
        return True


class PairRow(object):
    def __init__(self, order, source_el, source_value, matched_param,
                 target_el, current_value, new_value):
        self.Order = order
        self.SourceId = _id_int(source_el.Id)
        self.SourceType = get_type_name(doc, source_el)
        self.MatchedParam = matched_param if matched_param else '<none found>'
        self.SourceValue = source_value if source_value is not None else '<not found>'
        self.TargetId = _id_int(target_el.Id)
        self.TargetType = get_type_name(doc, target_el)
        self.CurrentTargetValue = current_value if current_value else ''
        self.NewTargetValue = new_value
        self.SourceRef = source_el
        self.TargetRef = target_el
        self.HasSourceValue = bool(source_value)


class ManualTransferWindow(forms.WPFWindow):

    def __init__(self, xaml_source, pairs):
        forms.WPFWindow.__init__(self, xaml_source)
        self.pairs = pairs  # list of (source_element, target_element)
        self._populate_parameter_choices()
        self.update_preview()

    # ---------------------------------------------------------------- utils
    def _populate_parameter_choices(self):
        self.cmbSourceParam.ItemsSource = SOURCE_SUGGESTED_PARAMETERS
        self.cmbSourceParam.Text = 'MEE-Component, MEE-EquipmentTag'

        self.cmbTargetParam.ItemsSource = TARGET_SUGGESTED_PARAMETERS
        self.cmbTargetParam.Text = 'MEE-Feeder'

    def _build_rows(self):
        source_param_names = parse_param_name_list(self.cmbSourceParam.Text)
        target_param_name = (self.cmbTargetParam.Text or '').strip()
        overwrite = bool(self.chkOverwrite.IsChecked)

        rows = []
        for i, (s_el, t_el) in enumerate(self.pairs):
            source_value, matched_param = get_string_param_value_multi(s_el, source_param_names)

            current_target_param = t_el.LookupParameter(target_param_name) if target_param_name else None
            current_value = (
                current_target_param.AsString()
                if current_target_param and current_target_param.StorageType == StorageType.String
                else ''
            )

            if source_value is None:
                new_value = current_value
            elif not overwrite and current_value:
                new_value = current_value
            else:
                new_value = source_value

            rows.append(PairRow(i + 1, s_el, source_value, matched_param, t_el, current_value, new_value))

        return source_param_names, target_param_name, rows

    # ------------------------------------------------------------- actions
    def update_preview(self):
        source_param_names, target_param_name, rows = self._build_rows()
        self.dgPreview.ItemsSource = rows
        self.txtPairCount.Text = '{} pair(s) added'.format(len(self.pairs))

        messages = []
        if not self.pairs:
            messages.append('No pairs added yet - use "+ Add Pair" above.')
        if not source_param_names:
            messages.append('Enter or select at least one source parameter.')
        if not target_param_name:
            messages.append('Enter or select a target parameter.')

        no_source_value_count = sum(1 for r in rows if not r.HasSourceValue)
        if no_source_value_count:
            messages.append(
                '{} pair(s) had no matching source parameter with a value - '
                'those targets will NOT be overwritten.'.format(no_source_value_count)
            )

        self.txtStatus.Text = ' '.join(messages)

    def btnAddPair_Click(self, sender, args):
        self.Close()
        try:
            source_ref = uidoc.Selection.PickObject(
                ObjectType.Element,
                NoTagSelectionFilter(),
                'Pick ONE source element for this pair (Escape to cancel).'
            )
        except Exception:
            show_window(self.pairs)
            return

        source_el = doc.GetElement(source_ref.ElementId)

        try:
            target_ref = uidoc.Selection.PickObject(
                ObjectType.Element,
                NoTagSelectionFilter(),
                'Now pick the matching TARGET element for this pair (Escape to cancel).'
            )
        except Exception:
            show_window(self.pairs)
            return

        target_el = doc.GetElement(target_ref.ElementId)

        new_pairs = list(self.pairs) + [(source_el, target_el)]
        show_window(new_pairs)

    def btnUndoPair_Click(self, sender, args):
        self.Close()
        show_window(list(self.pairs)[:-1])

    def btnClearPairs_Click(self, sender, args):
        self.Close()
        show_window([])

    def btnApply_Click(self, sender, args):
        source_param_names, target_param_name, rows = self._build_rows()

        if not source_param_names or not target_param_name:
            self.txtStatus.Text = 'Enter or select both a source and target parameter before applying.'
            return

        if not rows:
            self.txtStatus.Text = 'No pairs to transfer. Use "+ Add Pair" first.'
            return

        skipped_no_param = []
        skipped_no_value = []

        t = Transaction(doc, 'Manual Transfer Parameter')
        t.Start()
        try:
            applied = 0
            for row in rows:
                if not row.HasSourceValue:
                    skipped_no_value.append(row.SourceId)
                    continue
                target_param = row.TargetRef.LookupParameter(target_param_name)
                if target_param is None or target_param.IsReadOnly or target_param.StorageType != StorageType.String:
                    skipped_no_param.append(row.TargetId)
                    continue
                if row.NewTargetValue == row.CurrentTargetValue:
                    continue
                target_param.Set(row.NewTargetValue)
                applied += 1
            t.Commit()
        except Exception as ex:
            t.RollBack()
            self.txtStatus.Text = 'Failed to apply transfer: {}'.format(ex)
            return

        self.Close()

        detail_lines = ['Transferred values to {} element(s).'.format(applied)]
        if skipped_no_value:
            detail_lines.append(
                'Skipped {} pair(s) with no matching source value (Source Element Ids: {}).'.format(
                    len(skipped_no_value), ', '.join(str(m) for m in skipped_no_value)
                )
            )
        if skipped_no_param:
            detail_lines.append(
                'Skipped {} element(s) where "{}" does not exist, is read-only, '
                'or is not a text parameter (Target Element Ids: {}).'.format(
                    len(skipped_no_param), target_param_name,
                    ', '.join(str(m) for m in skipped_no_param)
                )
            )

        forms.alert(
            '\n\n'.join(detail_lines),
            title='Manual Transfer - Completed' if not (skipped_no_value or skipped_no_param) else
                  'Manual Transfer - Completed with warnings'
        )

    def btnCancel_Click(self, sender, args):
        self.Close()


def show_window(pairs):
    window = ManualTransferWindow(XAML_FILE, pairs)
    window.ShowDialog()


def main():
    show_window([])


if __name__ == '__main__':
    main()
