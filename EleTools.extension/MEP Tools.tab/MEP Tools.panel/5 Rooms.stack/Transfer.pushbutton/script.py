# -*- coding: utf-8 -*-
"""Transfer mapped parameters from Mechanical Equipment through physical MEP connectors."""
import clr
import os
from collections import deque

clr.AddReference('RevitAPI')
clr.AddReference('PresentationCore')
from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, ElementId,
    FilteredElementCollector, FamilyInstance, StorageType, Transaction,
    SubTransaction, ConnectorType, Domain, XYZ)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from pyrevit import forms, revit, script
from System.Windows.Controls import StackPanel, ComboBox, Button, Orientation
from System.Windows import Thickness
from System.Windows.Media import Brushes
import safetable  # crash-safe output tables (lib/safetable.py)

__title__ = 'Transfer Equipment Data'
__author__ = 'BHEEM'

DOC = revit.doc
UIDOC = revit.uidoc
OUTPUT = script.get_output()

DEFAULT_MAPS = [
    {'source': 'EQUIPMENT_TAG', 'target': 'SERVING_EQUIPMENT', 'categories': ['Air Terminals', 'Duct Accessories']},
    {'source': 'AHU_ID', 'target': 'Serving_AHU', 'categories': ['Air Terminals']},
    {'source': 'Level_Zone', 'target': 'Zone', 'categories': ['Duct Accessories']}
]

CATEGORY_NAMES = {
    'Duct Accessories': BuiltInCategory.OST_DuctAccessory,
    'Air Terminals': BuiltInCategory.OST_DuctTerminal
}


def element_id_value(element_id):
    try:
        return element_id.Value
    except Exception:
        try:
            return element_id.IntegerValue
        except Exception:
            return -1


def category_name(element):
    try:
        return element.Category.Name if element.Category else ''
    except Exception:
        return ''


def unique_id(element):
    try:
        return element.UniqueId
    except Exception:
        return str(element_id_value(element.Id))


def mep_connectors(element):
    """Return connectors for MEPCurve or FamilyInstance owners, or [] when absent."""
    try:
        manager = None
        if hasattr(element, 'ConnectorManager'):
            manager = element.ConnectorManager
        elif isinstance(element, FamilyInstance) and element.MEPModel:
            manager = element.MEPModel.ConnectorManager
        if manager is None:
            return []
        return [c for c in manager.Connectors if c and c.IsValidObject]
    except Exception:
        return []


def physical_refs(connector):
    """Only follow connected physical references; AllRefs also contains logical/self refs."""
    refs = []
    try:
        if not connector.IsConnected:
            return refs
        for ref in connector.AllRefs:
            try:
                if ref is None or not ref.IsValidObject:
                    continue
                if ref.Owner is None or ref.Owner.Id == connector.Owner.Id:
                    continue
                if ref.ConnectorType == ConnectorType.Logical:
                    continue
                # Duct-network traversal is limited to HVAC connectors. A null domain
                # can occur on some fitting families, so allow it and rely on physicality.
                if ref.Domain not in (Domain.DomainHvac, Domain.DomainUndefined):
                    continue
                refs.append(ref)
            except Exception:
                continue
    except Exception:
        pass
    return refs


def connector_system_id(connector):
    try:
        system = connector.MEPSystem
        if system:
            return element_id_value(system.Id)
    except Exception:
        pass
    return None


def is_equipment(element):
    try:
        return element.Category and element_id_value(element.Category.Id) == int(BuiltInCategory.OST_MechanicalEquipment)
    except Exception:
        return False


def is_target(element):
    try:
        cid = element_id_value(element.Category.Id)
        return cid in (int(BuiltInCategory.OST_DuctAccessory), int(BuiltInCategory.OST_DuctTerminal))
    except Exception:
        return False


def target_category_key(element):
    try:
        cid = element_id_value(element.Category.Id)
        if cid == int(BuiltInCategory.OST_DuctAccessory):
            return 'Duct Accessories'
        if cid == int(BuiltInCategory.OST_DuctTerminal):
            return 'Air Terminals'
    except Exception:
        pass
    return None


def traverse_equipment(equipment, options, log):
    """Breadth-first walk over physical HVAC connector adjacency; each element is visited once.

    At a connector's junction, Include branches follows every unseen adjacent owner.
    When disabled, only the first eligible adjacent owner (stable ElementId order) is
    enqueued. This is a deterministic topology rule; Revit connectors do not reliably
    encode a universal downstream direction for every family/network.
    """
    found = {}
    queue = deque()
    visited = set([element_id_value(equipment.Id)])
    connectors = mep_connectors(equipment)
    if not connectors:
        log('SKIPPED', equipment, 'Mechanical Equipment has no accessible connectors')
        return found
    for connector in connectors:
        try:
            if connector.Domain in (Domain.DomainHvac, Domain.DomainUndefined):
                queue.append((connector, 0, None))
        except Exception:
            continue
    if not queue:
        log('SKIPPED', equipment, 'No HVAC connectors available')
        return found

    max_depth = options['max_depth']
    while queue:
        current, depth, baseline_system = queue.popleft()
        if max_depth >= 0 and depth >= max_depth:
            continue
        refs = physical_refs(current)
        candidates = []
        for ref in refs:
            owner = ref.Owner
            oid = element_id_value(owner.Id)
            if oid in visited:
                continue
            if options['stop_at_system_boundaries']:
                from_system = connector_system_id(current)
                to_system = connector_system_id(ref)
                if from_system is not None and to_system is not None and from_system != to_system:
                    log('SKIPPED', owner, 'Connector crosses system boundary')
                    continue
            if is_equipment(owner):
                visited.add(oid)
                if options['stop_at_other_equipment']:
                    log('STOPPED', owner, 'Traversal stopped at another Mechanical Equipment')
                    continue
            candidates.append((oid, ref))
        candidates.sort(key=lambda item: item[0])
        if not options['include_branches'] and len(candidates) > 1:
            for oid, ref in candidates[1:]:
                log('SKIPPED', ref.Owner, 'Additional branch skipped (Include branches is off)')
            candidates = candidates[:1]
        for oid, ref in candidates:
            owner = ref.Owner
            visited.add(oid)
            next_depth = depth + 1
            if is_target(owner):
                found[oid] = (owner, next_depth)
            if not is_equipment(owner) or not options['stop_at_other_equipment']:
                exits = []
                for next_connector in mep_connectors(owner):
                    try:
                        if next_connector.Domain not in (Domain.DomainHvac, Domain.DomainUndefined):
                            continue
                        # Ignore the connector by which this owner was reached. At
                        # tees/fittings the remaining connected ports represent exits.
                        if next_connector.Id == ref.Id:
                            continue
                        if physical_refs(next_connector):
                            exits.append(next_connector)
                    except Exception:
                        continue
                if not options['include_branches'] and len(exits) > 1:
                    def exit_key(connector):
                        ids = [element_id_value(r.Owner.Id) for r in physical_refs(connector)]
                        return min(ids) if ids else 2147483647
                    exits = [sorted(exits, key=exit_key)[0]]
                for next_connector in exits:
                    queue.append((next_connector, next_depth, baseline_system))
    return found


def resolve_parameter(element, name):
    name = (name or '').strip()
    if name.upper().startswith('GUID:'):
        import System
        try:
            return element.get_Parameter(System.Guid(name[5:].strip()))
        except Exception:
            return None
    return element.LookupParameter(name)


def parameter_value(parameter):
    st = parameter.StorageType
    if st == StorageType.String:
        return parameter.AsString() or ''
    if st == StorageType.Integer:
        return parameter.AsInteger()
    if st == StorageType.Double:
        return parameter.AsDouble()
    if st == StorageType.ElementId:
        return parameter.AsElementId()
    raise Exception('Unsupported parameter storage type')


def set_parameter(target_parameter, source_parameter):
    if target_parameter.StorageType != source_parameter.StorageType:
        return False, 'Storage type mismatch: source {0}, target {1}'.format(
            source_parameter.StorageType, target_parameter.StorageType)
    if target_parameter.IsReadOnly:
        return False, 'Target parameter is read-only'
    try:
        value = parameter_value(source_parameter)
        if target_parameter.StorageType == StorageType.String:
            ok = target_parameter.Set(value)
        elif target_parameter.StorageType == StorageType.Integer:
            ok = target_parameter.Set(value)
        elif target_parameter.StorageType == StorageType.Double:
            ok = target_parameter.Set(value)
        elif target_parameter.StorageType == StorageType.ElementId:
            ok = target_parameter.Set(value)
        else:
            return False, 'Unsupported storage type'
        return (True, '') if ok else (False, 'Revit rejected parameter value')
    except Exception as ex:
        return False, str(ex)


class EquipmentFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_equipment(element)
    def AllowReference(self, reference, point):
        return False


def parameter_names_for_category(bic):
    """Collect distinct names as editable suggestions; custom input remains available."""
    names = set()
    try:
        elements = FilteredElementCollector(DOC).OfCategory(bic).WhereElementIsNotElementType()
        for element in elements:
            try:
                for parameter in element.Parameters:
                    if parameter and parameter.Definition and parameter.Definition.Name:
                        names.add(parameter.Definition.Name)
            except Exception:
                continue
    except Exception:
        pass
    return sorted(list(names), key=lambda value: value.lower())


class TransferWindow(forms.WPFWindow):
    def __init__(self, xaml_file, source_names, target_names):
        forms.WPFWindow.__init__(self, xaml_file)
        self.result = None
        self.source_names = source_names
        self.target_names = target_names
        self.mapping_rows = []
        for mapping in DEFAULT_MAPS:
            self.add_mapping_row(mapping['source'], mapping['target'], mapping['categories'])
        self.maxDepthBox.Text = '100'
        self.includeBranchesBox.IsChecked = True
        self.stopEquipmentBox.IsChecked = True
        self.stopSystemsBox.IsChecked = False
        self.processLinksBox.IsChecked = False
        self.categoryAccessoriesBox.IsChecked = True
        self.categoryTerminalsBox.IsChecked = True
        self.dryRunBox.IsChecked = True
        self.dryrun_changed(None, None)

    def add_mapping_row(self, source='', target='', categories=None):
        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        row.Margin = Thickness(0, 2, 0, 2)

        source_box = ComboBox()
        source_box.Width = 250
        source_box.IsEditable = True
        source_box.Height = 30
        source_box.IsTextSearchEnabled = True
        source_box.ItemsSource = self.source_names
        source_box.Text = source
        source_box.ToolTip = 'Source parameter on Mechanical Equipment'
        source_box.Margin = Thickness(0, 0, 6, 0)

        target_box = ComboBox()
        target_box.Width = 250
        target_box.IsEditable = True
        target_box.Height = 30
        target_box.IsTextSearchEnabled = True
        target_box.Text = target
        target_box.ToolTip = 'Target parameter on the selected category'
        target_box.Margin = Thickness(0, 0, 6, 0)

        category_box = ComboBox()
        category_box.Width = 150
        category_box.Height = 30
        category_box.Items.Add('Both categories')
        category_box.Items.Add('Duct Accessories')
        category_box.Items.Add('Air Terminals')
        category_box.ToolTip = 'Target category for this mapping'
        categories = categories or ['Duct Accessories', 'Air Terminals']
        if len(categories) == 1 and categories[0] == 'Duct Accessories':
            category_box.SelectedIndex = 1
        elif len(categories) == 1 and categories[0] == 'Air Terminals':
            category_box.SelectedIndex = 2
        else:
            category_box.SelectedIndex = 0
        category_box.Margin = Thickness(0, 0, 6, 0)
        self.update_target_suggestions(category_box, target_box)
        target_box.Text = target
        category_box.SelectionChanged += lambda sender, args: self.update_target_suggestions(category_box, target_box)

        remove_button = Button()
        remove_button.Content = '×'
        remove_button.Width = 28
        remove_button.Height = 28
        remove_button.ToolTip = 'Remove mapping'
        remove_button.Click += lambda sender, args: self.remove_mapping_row(row)

        row.Children.Add(source_box)
        row.Children.Add(target_box)
        row.Children.Add(category_box)
        row.Children.Add(remove_button)
        self.mappingPanel.Children.Add(row)
        self.mapping_rows.append({'panel': row, 'source': source_box,
                                  'target': target_box, 'category': category_box})

    def update_target_suggestions(self, category_box, target_box):
        choice = str(category_box.SelectedItem) if category_box.SelectedItem else 'Both categories'
        target_box.ItemsSource = self.target_names.get(choice, self.target_names.get('Both categories', []))
    def remove_mapping_row(self, row):
        if len(self.mapping_rows) <= 1:
            forms.alert('Keep at least one parameter mapping row.', title=__title__)
            return
        self.mappingPanel.Children.Remove(row)
        self.mapping_rows = [item for item in self.mapping_rows if item['panel'] != row]

    def add_mapping_click(self, sender, args):
        self.add_mapping_row()

    def execute_click(self, sender, args):
        try:
            depth = int(self.maxDepthBox.Text.strip())
            if depth < 0:
                depth = -1
            allowed_categories = {
                'Duct Accessories': bool(self.categoryAccessoriesBox.IsChecked),
                'Air Terminals': bool(self.categoryTerminalsBox.IsChecked)
            }
            if not any(allowed_categories.values()):
                forms.alert('Allow at least one target category.', title=__title__)
                return
            maps = []
            for index, row in enumerate(self.mapping_rows, 1):
                source = row['source'].Text.strip()
                target = row['target'].Text.strip()
                if not source or not target:
                    forms.alert('Mapping row {0} needs both a source and target parameter.'.format(index), title='Incomplete mapping')
                    return
                choice = row['category'].SelectedItem
                requested = ['Duct Accessories', 'Air Terminals'] if choice == 'Both categories' else [str(choice)]
                active = [name for name in requested if allowed_categories.get(name, False)]
                if not active:
                    forms.alert('Mapping row {0} is excluded by the global category filters.'.format(index), title='Category mismatch')
                    return
                maps.append({'source': source, 'target': target, 'categories': active})
            self.result = {
                'maps': maps,
                'max_depth': depth,
                'include_branches': bool(self.includeBranchesBox.IsChecked),
                'stop_at_other_equipment': bool(self.stopEquipmentBox.IsChecked),
                'stop_at_system_boundaries': bool(self.stopSystemsBox.IsChecked),
                'process_links': bool(self.processLinksBox.IsChecked),
                'dry_run': bool(self.dryRunBox.IsChecked)
            }
            self.Close()
        except Exception as ex:
            forms.alert(str(ex), title='Invalid settings')

    def dryrun_changed(self, sender, args):
        if bool(self.dryRunBox.IsChecked):
            self.executeButton.Content = 'Preview only'
            self.executionModeText.Text = 'PREVIEW ONLY — no model changes will be made'
            self.executionModeText.Foreground = Brushes.DarkGreen
        else:
            self.executeButton.Content = 'Write values'
            self.executionModeText.Text = 'WRITE MODE — connected target parameters will be changed'
            self.executionModeText.Foreground = Brushes.DarkRed
    def cancel_click(self, sender, args):
        self.Close()


def main():
    xaml_path = os.path.join(os.path.dirname(__file__), 'ui.xaml')
    source_names = parameter_names_for_category(BuiltInCategory.OST_MechanicalEquipment)
    accessory_names = parameter_names_for_category(BuiltInCategory.OST_DuctAccessory)
    terminal_names = parameter_names_for_category(BuiltInCategory.OST_DuctTerminal)
    target_names = {
        'Duct Accessories': accessory_names,
        'Air Terminals': terminal_names,
        'Both categories': sorted(set(accessory_names + terminal_names), key=lambda value: value.lower())
    }
    window = TransferWindow(xaml_path, source_names, target_names)
    window.show_dialog()
    options = window.result
    if not options:
        return

    mode = forms.alert('Choose Mechanical Equipment input.', title=__title__,
                       options=['Use current selection', 'Select all in model', 'Cancel'])
    if mode == 'Cancel' or mode is None:
        return
    if mode == 'Use current selection':
        equipment = []
        for eid in UIDOC.Selection.GetElementIds():
            element = DOC.GetElement(eid)
            if element and is_equipment(element):
                equipment.append(element)
        if not equipment:
            try:
                refs = UIDOC.Selection.PickObjects(ObjectType.Element, EquipmentFilter(),
                    'Select one or more Mechanical Equipment elements')
                equipment = [DOC.GetElement(r.ElementId) for r in refs]
            except Exception:
                return
    else:
        equipment = list(FilteredElementCollector(DOC).OfCategory(
            BuiltInCategory.OST_MechanicalEquipment).WhereElementIsNotElementType())
    if not equipment:
        forms.alert('No Mechanical Equipment elements were selected or found.', title=__title__)
        return

    log_entries = []
    counts = {'processed': 0, 'accessories': 0, 'terminals': 0, 'failed': 0,
              'missing': 0, 'skipped': 0, 'planned': 0}
    def log(status, element, reason):
        try:
            desc = '{0} [{1}]'.format(element.Name, element_id_value(element.Id))
        except Exception:
            desc = 'Element'
        log_entries.append((status, desc, reason))
        if status in ('SKIPPED', 'STOPPED'):
            counts['skipped'] += 1

    if options['process_links']:
        log_entries.append(('NOTICE', 'Linked models', 'Linked documents are read-only in this host workflow; linked targets are not modified.'))

    work = []
    for source in equipment:
        counts['processed'] += 1
        targets = traverse_equipment(source, options, log)
        log_entries.append(('INFO', '{0} [{1}]'.format(source.Name, element_id_value(source.Id)), 'Found {0} eligible connected target(s).'.format(len(targets))))
        for target, depth in targets.values():
            category = target_category_key(target)
            target_maps = [m for m in options['maps'] if category in m['categories']]
            if target_maps:
                work.append((source, target, target_maps, depth))

    if not options['dry_run'] and work:
        tx = Transaction(DOC, 'Transfer Mechanical Equipment Parameters')
        tx.Start()
        try:
            for source, target, maps, depth in work:
                sub = SubTransaction(DOC)
                sub.Start()
                target_failed = False
                target_succeeded = False
                try:
                    for mapping in maps:
                        sp = resolve_parameter(source, mapping['source'])
                        tp = resolve_parameter(target, mapping['target'])
                        if sp is None:
                            log_entries.append(('MISSING', target, 'Source parameter not found: {0}'.format(mapping['source'])))
                            counts['missing'] += 1
                            continue
                        if tp is None:
                            log_entries.append(('MISSING', target, 'Target parameter not found: {0}'.format(mapping['target'])))
                            counts['missing'] += 1
                            continue
                        ok, reason = set_parameter(tp, sp)
                        if ok:
                            target_succeeded = True
                            log_entries.append(('UPDATED', target, '{0} -> {1}'.format(mapping['source'], mapping['target'])))
                        if not ok:
                            counts['failed'] += 1
                            target_failed = True
                            log_entries.append(('FAILED', target, '{0} -> {1}: {2}'.format(mapping['source'], mapping['target'], reason)))
                    sub.Commit()
                except Exception as ex:
                    try: sub.RollBack()
                    except Exception: pass
                    counts['failed'] += 1
                    log_entries.append(('FAILED', target, 'Unexpected target error: {0}'.format(ex)))
                if target_succeeded:
                    if target_category_key(target) == 'Duct Accessories':
                        counts['accessories'] += 1
                    else:
                        counts['terminals'] += 1
            tx.Commit()
        except Exception:
            try: tx.RollBack()
            except Exception: pass
            raise
    else:
        counts['planned'] = len(work)
        for source, target, maps, depth in work:
            category = target_category_key(target)
            for mapping in maps:
                sp = resolve_parameter(source, mapping['source'])
                tp = resolve_parameter(target, mapping['target'])
                if sp is None or tp is None:
                    counts['missing'] += 1
                    absent = mapping['source'] if sp is None else mapping['target']
                    log_entries.append(('MISSING', target, 'Parameter not found: {0}'.format(absent)))
                elif tp.IsReadOnly:
                    counts['failed'] += 1
                    log_entries.append(('FAILED', target, 'Target parameter is read-only: {0}'.format(mapping['target'])))
                elif tp.StorageType != sp.StorageType:
                    counts['failed'] += 1
                    log_entries.append(('FAILED', target, 'Storage type mismatch: {0} -> {1}'.format(mapping['source'], mapping['target'])))
                else:
                    log_entries.append(('VALID', target, 'Ready: {0} -> {1}'.format(mapping['source'], mapping['target'])))
            if category == 'Duct Accessories': counts['accessories'] += 1
            elif category == 'Air Terminals': counts['terminals'] += 1

    OUTPUT.print_md('# Equipment Parameter Transfer Report')
    OUTPUT.print_md('- **Mode:** {0}'.format('Dry run' if options['dry_run'] else 'Executed'))
    OUTPUT.print_md('- **Mechanical Equipment processed:** {0}'.format(counts['processed']))
    OUTPUT.print_md('- **Duct Accessories {0}:** {1}'.format('planned' if options['dry_run'] else 'updated', counts['accessories']))
    OUTPUT.print_md('- **Air Terminals {0}:** {1}'.format('planned' if options['dry_run'] else 'updated', counts['terminals']))
    OUTPUT.print_md('- **Failed updates:** {0}'.format(counts['failed']))
    OUTPUT.print_md('- **Missing parameter instances:** {0}'.format(counts['missing']))
    OUTPUT.print_md('- **Skipped traversal elements:** {0}'.format(counts['skipped']))
    OUTPUT.print_md('- **Traversal limit:** {0}'.format('unlimited' if options['max_depth'] < 0 else options['max_depth']))
    if log_entries:
        OUTPUT.print_md('## Details')
        safetable.print_table(OUTPUT, [[a, b if isinstance(b, str) else unique_id(b), c] for a, b, c in log_entries],
                           ['Status', 'Element', 'Reason'])
    summary = 'Processed {0} Mechanical Equipment; {1} accessories and {2} terminals {3}; {4} failed, {5} missing parameter instances.'.format(
        counts['processed'], counts['accessories'], counts['terminals'],
        'planned' if options['dry_run'] else 'updated', counts['failed'], counts['missing'])
    forms.alert(summary + '\nDetailed results are in the pyRevit output panel.', title='Transfer complete')


if __name__ == '__main__':
    main()




















