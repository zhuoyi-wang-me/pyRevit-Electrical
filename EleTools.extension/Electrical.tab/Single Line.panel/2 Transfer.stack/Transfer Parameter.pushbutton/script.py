# -*- coding: utf-8 -*-
"""Copy a parameter value from a SOURCE set of elements (possibly mixed
families - e.g. Isolators, MCCB blocks, schedule-header blocks) into a
parameter on a TARGET set of elements from a different family (e.g. a
Cable's MEE-Feeder).

Three pairing methods:
  - Nearest position (default): greedy nearest-neighbor matching, weighted
    heavily toward X-alignment ("same column"). Works even when
    source/target counts differ or come from several different families,
    and correctly matches a source that is far above its real target
    (across level break-lines) rather than a vertically-closer decoy.
    This assumes source and target sit in the same vertical column,
    though - it does NOT trace routed connector lines, so it will not
    correctly guess pairs where the schematic routes a connection
    sideways to an unrelated column (e.g. a panel drawn off to one side,
    fed via a bent/routed line rather than a straight drop).
  - Ordered: sort each side independently (selection order or by
    position) and pair by index - useful for simple, perfectly aligned
    rows.
  - Manual: click one source, then its matching target, one pair at a
    time. Slower, but the only option that is always correct regardless
    of how far apart elements are or how a connection is routed on the
    diagram - use this for irregular layouts the automatic modes get
    wrong.

The Source Parameter field accepts a comma-separated list of candidate
parameter names (e.g. "MEE-Component, MEE-EquipmentTag, Mark"), tried in
order per element - because different families often store the same kind
of identifying tag under different parameter names.
"""

__title__ = 'Transfer\nParameter'
__author__ = 'BHEEM'

import os
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')
clr.AddReference('PresentationFramework')

import System
from Autodesk.Revit.DB import Transaction, StorageType
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter

from pyrevit import revit, forms, script

from cb_common import (
    is_tag_or_annotation, get_writable_string_param_names, sort_elements,
    get_type_name, get_category_name, get_string_param_value_multi,
    parse_param_name_list, nearest_neighbor_pairs
)

doc = revit.doc
uidoc = revit.uidoc
logger = script.get_logger()

XAML_FILE = os.path.join(os.path.dirname(__file__), 'ui.xaml')

SOURCE_SUGGESTED_PARAMETERS = ['MEE-Component', 'MEE-EquipmentTag', 'Mark']
TARGET_SUGGESTED_PARAMETERS = ['MEE-Feeder', 'MEE-FedFrom', 'MEE-CableRef']

PAIRING_NEAREST = 0
PAIRING_ORDERED = 1
PAIRING_MANUAL = 2



def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class NoTagSelectionFilter(ISelectionFilter):
    def AllowElement(self, elem):
        return not is_tag_or_annotation(elem)

    def AllowReference(self, ref, point):
        return True


def pick_elements(prompt_text, use_current_selection=False):
    """Prompt the user to pick elements on screen. If
    use_current_selection is True and something is already selected in
    Revit, that selection is used instead of prompting. Tags/annotations
    are always excluded."""
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
            refs = uidoc.Selection.PickObjects(
                ObjectType.Element,
                NoTagSelectionFilter(),
                prompt_text
            )
            all_elements = [doc.GetElement(r.ElementId) for r in refs]
            elements = [el for el in all_elements if not is_tag_or_annotation(el)]
            excluded_count += len(all_elements) - len(elements)
        except Exception:
            return []

    if excluded_count:
        forms.alert(
            'Ignored {} tag/annotation/line element(s) from your selection.'.format(excluded_count),
            title='Transfer Parameter - Tags/Lines excluded'
        )

    return elements


class TransferRow(object):
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


class TransferWindow(forms.WPFWindow):

    def __init__(self, xaml_source, source_elements, target_elements, manual_pairs=None, pairing_mode=None):
        forms.WPFWindow.__init__(self, xaml_source)
        self.source_elements = source_elements
        self.target_elements = target_elements
        self.manual_pairs = manual_pairs if manual_pairs is not None else []
        self._populate_parameter_choices()
        self._refresh_info_labels()
        if pairing_mode is not None:
            self.cmbPairing.SelectedIndex = pairing_mode
        self.cmbPairing.SelectionChanged += self.on_pairing_changed
        self._set_mode_panels_visibility()
        self.update_preview()

    # ---------------------------------------------------------------- utils
    def _refresh_info_labels(self):
        self.txtSourceInfo.Text = 'Source: {} element(s)'.format(len(self.source_elements))
        self.txtTargetInfo.Text = 'Target: {} element(s)'.format(len(self.target_elements))
        self.txtManualCount.Text = '{} manual pair(s) added'.format(len(self.manual_pairs))

    def _populate_parameter_choices(self):
        source_found = get_writable_string_param_names(self.source_elements[0]) if self.source_elements else []
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
        # Default: try the two most common names, comma-separated fallback
        self.cmbSourceParam.Text = 'MEE-Component, MEE-EquipmentTag'

        self.cmbTargetParam.ItemsSource = target_combined
        if 'MEE-Feeder' in target_combined:
            self.cmbTargetParam.SelectedItem = 'MEE-Feeder'
        elif target_combined:
            self.cmbTargetParam.SelectedIndex = 0
        else:
            self.cmbTargetParam.Text = 'MEE-Feeder'

    def _set_mode_panels_visibility(self):
        # Resolve the Visibility enum via reflection off the control's own
        # current value instead of importing System.Windows.Visibility
        # directly - avoids fragile assembly-reference resolution across
        # different pyRevit / CLR setups.
        visibility_type = self.orderedSortPanel.Visibility.GetType()
        visible = System.Enum.Parse(visibility_type, 'Visible')
        collapsed = System.Enum.Parse(visibility_type, 'Collapsed')

        mode = self.cmbPairing.SelectedIndex
        self.orderedSortPanel.Visibility = visible if mode == PAIRING_ORDERED else collapsed
        self.manualPairPanel.Visibility = visible if mode == PAIRING_MANUAL else collapsed

    def on_pairing_changed(self, sender, args):
        self._set_mode_panels_visibility()
        self.update_preview()

    def _build_rows(self):
        source_param_names = parse_param_name_list(self.cmbSourceParam.Text)
        target_param_name = (self.cmbTargetParam.Text or '').strip()
        overwrite = bool(self.chkOverwrite.IsChecked)
        view = doc.ActiveView
        pairing_method = self.cmbPairing.SelectedIndex

        warnings = []

        if pairing_method == PAIRING_MANUAL:
            pairs = list(self.manual_pairs)
            if not pairs:
                warnings.append('No manual pairs added yet - use "Add Pair" below.')
        elif pairing_method == PAIRING_NEAREST:
            pairs, unmatched_sources, unmatched_targets, no_position = nearest_neighbor_pairs(
                self.source_elements, self.target_elements, view
            )
            if unmatched_sources:
                warnings.append('{} source element(s) had no matching target.'.format(len(unmatched_sources)))
            if unmatched_targets:
                warnings.append('{} target element(s) had no matching source.'.format(len(unmatched_targets)))
            if no_position:
                warnings.append(
                    '{} element(s) had no determinable position and were excluded '
                    'from matching (Element Ids: {}).'.format(
                        len(no_position), ', '.join(str(_id_int(e.Id)) for e in no_position)
                    )
                )
        else:
            source_sorted = sort_elements(self.source_elements, self.cmbSourceSort.SelectedIndex, view)
            target_sorted = sort_elements(self.target_elements, self.cmbTargetSort.SelectedIndex, view)
            count = min(len(source_sorted), len(target_sorted))
            pairs = list(zip(source_sorted[:count], target_sorted[:count]))
            if len(source_sorted) != len(target_sorted):
                warnings.append(
                    'Source count ({}) does not match target count ({}); '
                    'only the first {} pair(s) are shown.'.format(
                        len(source_sorted), len(target_sorted), count
                    )
                )

        rows = []
        for i, (s_el, t_el) in enumerate(pairs):
            source_value, matched_param = get_string_param_value_multi(s_el, source_param_names)
            current_target_param = t_el.LookupParameter(target_param_name) if target_param_name else None
            current_value = (
                current_target_param.AsString()
                if current_target_param and current_target_param.StorageType == StorageType.String
                else ''
            )

            if source_value is None:
                new_value = current_value  # nothing to copy - target left as-is on Apply
            elif not overwrite and current_value:
                new_value = current_value
            else:
                new_value = source_value

            rows.append(TransferRow(i + 1, s_el, source_value, matched_param, t_el, current_value, new_value))

        no_source_value_count = sum(1 for r in rows if not r.HasSourceValue)
        if no_source_value_count:
            warnings.append(
                '{} pair(s) had no matching source parameter with a value - '
                'those targets will NOT be overwritten. Check the Source '
                'Parameter list and the Matched Param column.'.format(no_source_value_count)
            )

        return source_param_names, target_param_name, rows, warnings

    # ------------------------------------------------------------- actions
    def update_preview(self):
        source_param_names, target_param_name, rows, warnings = self._build_rows()
        self.dgPreview.ItemsSource = rows

        messages = []
        if not source_param_names:
            messages.append('Enter or select at least one source parameter.')
        if not target_param_name:
            messages.append('Enter or select a target parameter.')
        messages.extend(warnings)
        self.txtStatus.Text = ' '.join(messages)

    def btnRefresh_Click(self, sender, args):
        self.update_preview()

    def btnPickSource_Click(self, sender, args):
        current_mode = self.cmbPairing.SelectedIndex
        self.Close()
        new_source = pick_elements(
            'Select SOURCE elements (the ones to copy VALUES FROM), then '
            'press Finish on the ribbon / status bar.'
        )
        if new_source:
            show_window(new_source, self.target_elements, self.manual_pairs, current_mode)
        else:
            show_window(self.source_elements, self.target_elements, self.manual_pairs, current_mode)

    def btnPickTarget_Click(self, sender, args):
        current_mode = self.cmbPairing.SelectedIndex
        self.Close()
        new_target = pick_elements(
            'Select TARGET elements (the ones to write VALUES INTO), then '
            'press Finish on the ribbon / status bar.'
        )
        if new_target:
            show_window(self.source_elements, new_target, self.manual_pairs, current_mode)
        else:
            show_window(self.source_elements, self.target_elements, self.manual_pairs, current_mode)

    def btnAddPair_Click(self, sender, args):
        self.Close()
        try:
            source_ref = uidoc.Selection.PickObject(
                ObjectType.Element,
                NoTagSelectionFilter(),
                'Pick ONE source element for this pair (Escape to cancel).'
            )
        except Exception:
            show_window(self.source_elements, self.target_elements, self.manual_pairs, PAIRING_MANUAL)
            return

        source_el = doc.GetElement(source_ref.ElementId)
        if is_tag_or_annotation(source_el):
            forms.alert('That is a tag/line/annotation - pick the actual element instead.',
                        title='Transfer Parameter')
            show_window(self.source_elements, self.target_elements, self.manual_pairs, PAIRING_MANUAL)
            return

        try:
            target_ref = uidoc.Selection.PickObject(
                ObjectType.Element,
                NoTagSelectionFilter(),
                'Now pick the matching TARGET element for this pair (Escape to cancel).'
            )
        except Exception:
            show_window(self.source_elements, self.target_elements, self.manual_pairs, PAIRING_MANUAL)
            return

        target_el = doc.GetElement(target_ref.ElementId)
        if is_tag_or_annotation(target_el):
            forms.alert('That is a tag/line/annotation - pick the actual element instead.',
                        title='Transfer Parameter')
            show_window(self.source_elements, self.target_elements, self.manual_pairs, PAIRING_MANUAL)
            return

        new_pairs = list(self.manual_pairs) + [(source_el, target_el)]
        show_window(self.source_elements, self.target_elements, new_pairs, PAIRING_MANUAL)

    def btnUndoPair_Click(self, sender, args):
        self.Close()
        new_pairs = list(self.manual_pairs)[:-1]
        show_window(self.source_elements, self.target_elements, new_pairs, PAIRING_MANUAL)

    def btnApply_Click(self, sender, args):
        source_param_names, target_param_name, rows, warnings = self._build_rows()

        if not source_param_names or not target_param_name:
            self.txtStatus.Text = 'Enter or select both a source and target parameter before applying.'
            return

        if not rows:
            self.txtStatus.Text = 'No matched pairs to transfer. Pick source and target elements first.'
            return

        skipped_no_param = []
        skipped_no_value = []

        t = Transaction(doc, 'Transfer Parameter Between Families')
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
                    # nothing changing (either overwrite disabled and value
                    # already present, or values already match)
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
        detail_lines.extend(warnings)

        forms.alert(
            '\n\n'.join(detail_lines),
            title='Transfer Parameter - Completed' if not (skipped_no_value or skipped_no_param) else
                  'Transfer Parameter - Completed with warnings'
        )

    def btnCancel_Click(self, sender, args):
        self.Close()


def show_window(source_elements, target_elements, manual_pairs=None, pairing_mode=None):
    window = TransferWindow(XAML_FILE, source_elements, target_elements, manual_pairs, pairing_mode)
    window.ShowDialog()


def main():
    source_elements = pick_elements(
        'STEP 1 of 2: Select SOURCE elements (the ones to copy VALUES '
        'FROM - can be mixed families, e.g. Isolators, MCCB blocks, '
        'schedule headers), then press Finish on the ribbon / status bar.',
        use_current_selection=True
    )
    if not source_elements:
        script.exit()
        return

    target_elements = pick_elements(
        'STEP 2 of 2: Select TARGET elements (the ones to write VALUES '
        'INTO, e.g. Cables), then press Finish on the ribbon / status bar.'
    )
    if not target_elements:
        script.exit()
        return

    show_window(source_elements, target_elements)


if __name__ == '__main__':
    main()
