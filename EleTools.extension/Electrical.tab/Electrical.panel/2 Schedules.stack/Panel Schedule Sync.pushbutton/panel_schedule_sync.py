# -*- coding: utf-8 -*-
"""Modeless WPF monitor and ExternalEvent implementation for pyRevit.

Uses IronPython-compatible syntax while also supporting CPython pyRevit.
"""
from __future__ import print_function
import os
import traceback
from Autodesk.Revit.DB import (BuiltInCategory, ElementId, FilteredElementCollector,
                               Transaction, View)
from Autodesk.Revit.DB.Electrical import PanelScheduleView
from Autodesk.Revit.UI import ExternalEvent, IExternalEventHandler
import clr

# ObservableCollection<T> lives in System.dll on .NET 4.x (type-forwarded from
# WindowsBase). The pyRevit IronPython engine does not always have those
# assemblies referenced, which raises "Cannot import name ObservableCollection".
for _assembly_name in ('System', 'WindowsBase'):
    try:
        clr.AddReference(_assembly_name)
    except Exception:
        pass

try:
    from System.Collections.ObjectModel import ObservableCollection as _RowCollection
except ImportError:
    # Fallback: set_rows() reassigns ItemsSource wholesale, so change
    # notification is not actually required for the grid to update.
    from System.Collections.Generic import List as _RowCollection

from pyrevit import forms, script

LOGGER = script.get_logger()
XAML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'PanelScheduleSync.xaml')


def _id_int(element_id):
    """ElementId -> int in every Revit version: .Value (2024+) - .IntegerValue was REMOVED in Revit 2026."""
    value = getattr(element_id, 'Value', None)
    return int(element_id.IntegerValue if value is None else value)


class PanelScheduleRow(object):
    def __init__(self, panel_id, schedule_id, panel_name, schedule_name):
        self.PanelId = panel_id
        self.ScheduleId = schedule_id
        self.PanelName = panel_name
        self.ScheduleName = schedule_name
        self.Status = 'Match' if panel_name == schedule_name else 'Mismatch'

class SyncRequest(object):
    REFRESH = 'refresh'
    UPDATE_SELECTED = 'update_selected'
    UPDATE_ALL = 'update_all'

class PanelScheduleExternalEventHandler(IExternalEventHandler):
    """The only class that reads or modifies Revit document data."""
    def __init__(self, window):
        self.window = window
        self.request_type = None
        self.schedule_ids = []
    def GetName(self):
        return 'Panel Schedule Sync External Event'
    def Execute(self, uiapp):
        try:
            doc = uiapp.ActiveUIDocument.Document if uiapp.ActiveUIDocument else None
            if doc is None:
                self.window.set_message('No active document. Close and reopen this monitor.')
                return
            if self.request_type == SyncRequest.REFRESH:
                self._refresh(doc)
            elif self.request_type in (SyncRequest.UPDATE_SELECTED, SyncRequest.UPDATE_ALL):
                self._update(doc)
        except Exception as error:
            LOGGER.error('Panel Schedule Sync failed: %s\\n%s', error, traceback.format_exc())
            self.window.set_message('Operation failed: {0}. See pyRevit log.'.format(error))
        finally:
            self.request_type = None
            self.schedule_ids = []
    @staticmethod
    def _panel_name(panel):
        return panel.Name or '<Unnamed Panel>'
    def _build_association_map(self, doc):
        """Perform one schedule collector and one electrical equipment collector."""
        by_panel_id = {}
        for schedule in FilteredElementCollector(doc).OfClass(PanelScheduleView):
            try:
                panel_id = schedule.GetPanel()
                if panel_id and panel_id != ElementId.InvalidElementId:
                    by_panel_id[_id_int(panel_id)] = schedule
            except Exception as error:
                LOGGER.warning('Could not read panel for schedule %s: %s', schedule.Id, error)
        panels = (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_ElectricalEquipment)
                  .WhereElementIsNotElementType())
        return panels, by_panel_id
    def _refresh(self, doc):
        rows = _RowCollection[object]()
        panels, by_panel_id = self._build_association_map(doc)
        without_schedules = []
        for panel in panels:
            schedule = by_panel_id.get(_id_int(panel.Id))
            if schedule is None:
                without_schedules.append(self._panel_name(panel))
                continue
            rows.Add(PanelScheduleRow(panel.Id, schedule.Id, self._panel_name(panel), schedule.Name))
        self.window.set_rows(rows)
        mismatches = len([row for row in rows if row.Status == 'Mismatch'])
        message = '{0} panel schedules scanned; {1} mismatch(es).'.format(rows.Count, mismatches)
        if without_schedules:
            message += ' {0} electrical equipment item(s) without schedules skipped.'.format(len(without_schedules))
            LOGGER.info('Items without panel schedules: %s', ', '.join(without_schedules))
        self.window.set_message(message)
    def _update(self, doc):
        requested = set([_id_int(sid) for sid in self.schedule_ids])
        targets = [row for row in self.window.rows if row.Status == 'Mismatch' and
                   (self.request_type == SyncRequest.UPDATE_ALL or _id_int(row.ScheduleId) in requested)]
        if not targets:
            self.window.set_message('No mismatched schedules selected for update.')
            return
        # View names are globally unique in a document. Refuse conflicts instead
        # of adding a suffix, which would violate the required name match.
        used_names = set([view.Name for view in FilteredElementCollector(doc).OfClass(View) if not view.IsTemplate])
        renamed, skipped = [], []
        transaction = Transaction(doc, 'Synchronize Panel Schedule Names')
        try:
            transaction.Start()
            for row in targets:
                schedule = doc.GetElement(row.ScheduleId)
                if schedule is None:
                    skipped.append('{0} (schedule was deleted)'.format(row.PanelName))
                    continue
                desired = row.PanelName
                if desired in used_names and schedule.Name != desired:
                    skipped.append('{0} (view name already exists)'.format(desired))
                    continue
                old_name = schedule.Name
                try:
                    used_names.discard(old_name)
                    schedule.Name = desired
                    used_names.add(desired)
                    renamed.append(desired)
                except Exception as error:
                    used_names.add(old_name)
                    skipped.append('{0} ({1})'.format(desired, error))
                    LOGGER.warning('Could not rename schedule %s: %s', schedule.Id, error)
            transaction.Commit()
        except Exception:
            if transaction.HasStarted():
                transaction.RollBack()
            raise
        self._refresh(doc)
        message = 'Updated {0} schedule(s).'.format(len(renamed))
        if skipped:
            message += ' Skipped {0}: {1}'.format(len(skipped), '; '.join(skipped[:3]))
            LOGGER.warning('Skipped panel schedule renames: %s', '; '.join(skipped))
        self.window.set_message(message)

class PanelScheduleSyncWindow(forms.WPFWindow):
    """WPF event handlers only queue work through the Revit ExternalEvent."""
    def __init__(self):
        forms.WPFWindow.__init__(self, XAML_FILE)
        self.rows = _RowCollection[object]()
        self.PanelGrid.ItemsSource = self.rows
        self._handler = PanelScheduleExternalEventHandler(self)
        self._external_event = ExternalEvent.Create(self._handler)
        self.RefreshButton.Click += self._on_refresh
        self.UpdateSelectedButton.Click += self._on_update_selected
        self.UpdateAllButton.Click += self._on_update_all
        self.CloseButton.Click += self._on_close
        self.set_message('Ready. Scanning active project...')
    def set_rows(self, rows):
        self.rows = rows
        self.PanelGrid.ItemsSource = self.rows
    def set_message(self, message):
        self.MessageText.Text = message
    def request_refresh(self):
        self._raise(SyncRequest.REFRESH)
    def _on_refresh(self, sender, args):
        self.request_refresh()
    def _on_update_selected(self, sender, args):
        self._raise(SyncRequest.UPDATE_SELECTED, [row.ScheduleId for row in list(self.PanelGrid.SelectedItems)])
    def _on_update_all(self, sender, args):
        self._raise(SyncRequest.UPDATE_ALL)
    def _on_close(self, sender, args):
        self.Close()
    def _raise(self, request_type, schedule_ids=None):
        if self._handler.request_type is not None:
            self.set_message('A request is already queued. Please wait.')
            return
        self._handler.request_type = request_type
        self._handler.schedule_ids = schedule_ids or []
        self.set_message('Working...')
        self._external_event.Raise()
