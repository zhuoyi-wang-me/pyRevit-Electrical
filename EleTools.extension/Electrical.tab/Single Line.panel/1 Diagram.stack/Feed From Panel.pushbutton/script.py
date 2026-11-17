# -*- coding: utf-8 -*-
"""One-to-many broadcast: pick ONE source panel/board, then pick ALL the
elements it feeds (cables with breakers, isolators, sub-panels, etc.) and
write the source's value (e.g. MEE-Component) into every target's
parameter (e.g. MEE-FedFrom) in one transaction.

This is for the common case of a panel schedule where a single panel
feeds a whole row of downstream devices, as opposed to Transfer
Parameter's pair-by-pair matching (one source -> one target each).
"""

__title__ = 'Feed From\nPanel'
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
    is_tag_or_annotation, get_writable_string_param_names, get_type_name,
    get_category_name, get_string_param_value_multi, parse_param_name_list
)

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

XAML_FILE = os.path.join(os.path.dirname(__file__), 'ui.xaml')

SOURCE_SUGGESTED_PARAMETERS = ['MEE-Component', 'MEE-EquipmentTag', 'Mark']
TARGET_SUGGESTED_PARAMETERS = ['MEE-FedFrom', 'MEE-Feeder', 'MEE-CableRef']



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class NoTagSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return not is_tag_or_annotation(elem)

    def AllowReference(self, ref, point):
        return True


def pick_one_element(prompt_text):
    try:
        ref = uidoc.Selection.PickObject(ObjectType.Element, NoTagSelectionFilter(), prompt_text)
        el = doc.GetElement(ref.ElementId)
        if is_tag_or_annotation(el):
            forms.alert('That is a tag/line/annotation - pick the actual element instead.',
                        title='Feed From Panel')
            return None
        return el
    except Exception:
        return None


def pick_many_elements(prompt_text, use_current_selection=False):
    """Box-select / ctrl-click any number of elements. If
    use_current_selection is True and something is already selected in
    Revit, that selection is used instead of prompting. Tags/lines are
    always excluded."""
    excluded_count = 0
    elements = []

    if use_current_selection:
        sel_ids = uidoc.Selection.GetElementIds()
        if sel_ids and len(sel_ids) > 0:
            all_elements = [doc.GetElement(eid) for eid in sel_ids]
            elements = [el for el in all_elements if not is_tag_or_annotation(el)]
            excluded_count = len(all_elements) - len(elements)

    if not elements:
        try:
            refs = uidoc.Selection.PickObjects(ObjectType.Element, NoTagSelectionFilter(), prompt_text)
            all_elements = [doc.GetElement(r.ElementId) for r in refs]
            elements = [el for el in all_elements if not is_tag_or_annotation(el)]
            excluded_count += len(all_elements) - len(elements)
        except Exception:
            return []

    if excluded_count:
        forms.alert(
            'Ignored {} tag/annotation/line element(s) from your selection.'.format(excluded_count),
            title='Feed From Panel - Tags/Lines excluded'
        )

    return elements


def get_string_value_from_source(source_element, param_names):
    """Returns (value, matched_param) or (None, None)."""
    if not source_element:
        return None, None
    return get_string_param_value_multi(source_element, param_names)


class BroadcastRow(object):
    def __init__(self, order, target_el, current_value, new_value):
        self.Order = order
        self.TargetId = _id_int(target_el.Id)
        self.TargetCategory = get_category_name(target_el)
        self.TargetType = get_type_name(doc, target_el)
        self.CurrentValue = current_value if current_value else ''
        self.NewValue = new_value
        self.TargetRef = target_el


class FeedFromPanelWindow(forms.WPFWindow):

    def __init__(self, xaml_source, source_element, target_elements):
        forms.WPFWindow.__init__(self, xaml_source)
        self.source_element = source_element
        self.target_elements = target_elements
        self._populate_parameter_choices()
        self._refresh_info_labels()
        self.update_preview()

    # ---------------------------------------------------------------- utils
    def _refresh_info_labels(self):
        if self.source_element:
            self.txtSourceInfo.Text = 'Element Id {} ({})'.format(
                _id_int(self.source_element.Id), get_type_name(doc, self.source_element)
            )
        else:
            self.txtSourceInfo.Text = 'No source selected yet'
        self.txtTargetInfo.Text = '{} target(s) selected'.format(len(self.target_elements))

    def _populate_parameter_choices(self):
        source_found = get_writable_string_param_names(self.source_element) if self.source_element else []
        target_found = get_writable_string_param_names(self.target_elements[0]) if self.target_elements else []

        source_combined = []
        for name in SOURCE_SUGGESTED_PARAMETERS + source_found:
            if name not in source_combined:
                source_combined.append(name)

        target_combined = []
        for name in TARGET_SUGGESTED_PARAMETERS + target_found:
            if name not in target_combined:
                target_combined.append(name)

        self.cmbSourceParam.ItemsSource = source_combined
        self.cmbSourceParam.Text = 'MEE-Component, MEE-EquipmentTag'

        self.cmbTargetParam.ItemsSource = target_combined
        if 'MEE-FedFrom' in target_combined:
            self.cmbTargetParam.SelectedItem = 'MEE-FedFrom'
        elif target_combined:
            self.cmbTargetParam.SelectedIndex = 0
        else:
            self.cmbTargetParam.Text = 'MEE-FedFrom'

    def _get_source_value(self):
        source_param_names = parse_param_name_list(self.cmbSourceParam.Text)
        return get_string_value_from_source(self.source_element, source_param_names)

    def _build_rows(self):
        target_param_name = (self.cmbTargetParam.Text or '').strip()
        overwrite = bool(self.chkOverwrite.IsChecked)
        source_value, matched_param = self._get_source_value()

        rows = []
        for i, t_el in enumerate(self.target_elements):
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

            rows.append(BroadcastRow(i + 1, t_el, current_value, new_value))

        return target_param_name, source_value, matched_param, rows

    # ------------------------------------------------------------- actions
    def update_preview(self):
        target_param_name, source_value, matched_param, rows = self._build_rows()
        self.dgPreview.ItemsSource = rows

        messages = []
        if not self.source_element:
            messages.append('Pick a source element first.')
        elif source_value is None:
            messages.append(
                'No value found on the source using parameter(s) "{}". Check the Source '
                'Parameter field.'.format(self.cmbSourceParam.Text)
            )
        else:
            messages.append('Broadcasting "{}" (from {}) to all targets.'.format(source_value, matched_param))
        if not target_param_name:
            messages.append('Enter or select a target parameter.')
        if not self.target_elements:
            messages.append('Pick at least one target element.')

        self.txtStatus.Text = ' '.join(messages)

    def btnRefresh_Click(self, sender, args):
        self.update_preview()

    def btnPickSource_Click(self, sender, args):
        self.Close()
        new_source = pick_one_element('Pick the SOURCE element (the one to copy the value FROM, e.g. the panel).')
        show_window(new_source if new_source else self.source_element, self.target_elements)

    def btnPickTargets_Click(self, sender, args):
        self.Close()
        new_targets = pick_many_elements(
            'Select ALL the TARGET elements this panel feeds (cables, breakers, '
            'isolators, sub-panels...), then press Finish on the ribbon / status bar.',
            use_current_selection=True
        )
        if new_targets:
            show_window(self.source_element, new_targets)
        else:
            show_window(self.source_element, self.target_elements)

    def btnApply_Click(self, sender, args):
        target_param_name, source_value, matched_param, rows = self._build_rows()

        if not self.source_element:
            self.txtStatus.Text = 'Pick a source element first.'
            return
        if source_value is None:
            self.txtStatus.Text = 'No value found on the source - nothing to broadcast.'
            return
        if not target_param_name:
            self.txtStatus.Text = 'Enter or select a target parameter before applying.'
            return
        if not rows:
            self.txtStatus.Text = 'No target elements. Pick targets first.'
            return

        skipped_no_param = []

        t = Transaction(doc, 'Feed From Panel - Broadcast Parameter')
        t.Start()
        try:
            applied = 0
            for row in rows:
                target_param = row.TargetRef.LookupParameter(target_param_name)
                if target_param is None or target_param.IsReadOnly or target_param.StorageType != StorageType.String:
                    skipped_no_param.append(row.TargetId)
                    continue
                if row.NewValue == row.CurrentValue:
                    continue
                target_param.Set(row.NewValue)
                applied += 1
            t.Commit()
        except Exception as ex:
            t.RollBack()
            self.txtStatus.Text = 'Failed to apply: {}'.format(ex)
            return

        self.Close()

        detail_lines = [
            'Set "{}" = "{}" on {} target element(s).'.format(target_param_name, source_value, applied)
        ]
        if skipped_no_param:
            detail_lines.append(
                'Skipped {} element(s) where "{}" does not exist, is read-only, '
                'or is not a text parameter (Element Ids: {}).'.format(
                    len(skipped_no_param), target_param_name,
                    ', '.join(str(m) for m in skipped_no_param)
                )
            )

        forms.alert(
            '\n\n'.join(detail_lines),
            title='Feed From Panel - Completed' if not skipped_no_param else
                  'Feed From Panel - Completed with warnings'
        )

    def btnCancel_Click(self, sender, args):
        self.Close()


def show_window(source_element, target_elements):
    window = FeedFromPanelWindow(XAML_FILE, source_element, target_elements)
    window.ShowDialog()


def main():
    # Capture any pre-existing selection FIRST - picking the single source
    # element below will overwrite Revit's active selection, so if the
    # user already box-selected the target cables/breakers/isolators
    # (as in the typical workflow: select the row, then run this tool),
    # that selection needs to be grabbed before it's lost.
    initial_sel_ids = uidoc.Selection.GetElementIds()
    initial_target_candidates = []
    excluded_count = 0
    if initial_sel_ids and len(initial_sel_ids) > 0:
        all_elements = [doc.GetElement(eid) for eid in initial_sel_ids]
        initial_target_candidates = [el for el in all_elements if not is_tag_or_annotation(el)]
        excluded_count = len(all_elements) - len(initial_target_candidates)

    if excluded_count:
        forms.alert(
            'Ignored {} tag/annotation/line element(s) from your selection.'.format(excluded_count),
            title='Feed From Panel - Tags/Lines excluded'
        )

    source_element = pick_one_element(
        'STEP 1 of 2: Pick the SOURCE element (the panel/board to copy '
        'the value FROM, e.g. its MEE-Component).'
    )
    if not source_element:
        script.exit()
        return

    if initial_target_candidates:
        target_elements = initial_target_candidates
    else:
        target_elements = pick_many_elements(
            'STEP 2 of 2: Select ALL the TARGET elements this panel feeds '
            '(cables, breakers, isolators, sub-panels...), then press Finish '
            'on the ribbon / status bar.'
        )
    if not target_elements:
        script.exit()
        return

    show_window(source_element, target_elements)


if __name__ == '__main__':
    main()
