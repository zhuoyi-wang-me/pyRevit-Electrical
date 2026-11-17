# -*- coding: utf-8 -*-
"""Preview dialog: review, filter and de-select proposed changes."""

import os

from System import Predicate
from System.Collections.ObjectModel import ObservableCollection
from System.Collections.Generic import List

from Autodesk.Revit import DB

from pyrevit import forms

from .. import reporting
from ..engine import Status
from ..revit_compat import format_length, make_eid
from .main_window import NotifyBase

XAML_FILE = os.path.join(os.path.dirname(__file__), 'PreviewWindow.xaml')

ALL_ITEM = '<All>'


class PreviewRow(NotifyBase):
    """Bindable wrapper around an :class:`~mepleveler.engine.ElementPlan`."""

    def __init__(self, doc, plan):
        NotifyBase.__init__(self)
        self.plan = plan
        self._doc = doc
        self._selected = bool(plan.selected and plan.is_actionable)

    # -- bound properties -------------------------------------------------
    @property
    def Selected(self):
        return self._selected

    @Selected.setter
    def Selected(self, value):
        value = bool(value)
        if value != self._selected:
            self._selected = value
            self.plan.selected = value
            self.raise_property_changed('Selected')

    @property
    def ElementId(self):
        return self.plan.element_id

    @property
    def Category(self):
        return self.plan.category

    @property
    def Family(self):
        return self.plan.family

    @property
    def TypeName(self):
        return self.plan.type_name

    @property
    def CurrentLevel(self):
        return self.plan.current_level_name

    @property
    def ProposedLevel(self):
        return self.plan.proposed_level_name

    @property
    def CurrentOffset(self):
        if self.plan.current_offset is None:
            return u''
        return format_length(self._doc, self.plan.current_offset, 1)

    @property
    def ProposedOffset(self):
        if self.plan.proposed_offset is None:
            return u''
        return format_length(self._doc, self.plan.proposed_offset, 1)

    @property
    def Status(self):
        return self.plan.status

    @property
    def Message(self):
        return self.plan.message

    # -- helpers ----------------------------------------------------------
    def matches(self, needle):
        if not needle:
            return True
        needle = needle.lower()
        haystack = u' '.join([
            u'%s' % self.plan.element_id,
            self.plan.family or u'',
            self.plan.type_name or u'',
            self.plan.category or u'',
            self.plan.current_level_name or u'',
            self.plan.proposed_level_name or u'',
            self.plan.message or u'',
        ]).lower()
        return needle in haystack

    def refresh(self):
        for name in ('Selected', 'CurrentLevel', 'ProposedLevel',
                     'CurrentOffset', 'ProposedOffset', 'Status', 'Message'):
            self.raise_property_changed(name)


class PreviewWindow(forms.WPFWindow):
    """Lets the user review and curate the proposed changes."""

    def __init__(self, doc, uidoc, plans, settings, logger,
                 title='Preview of proposed changes'):
        forms.WPFWindow.__init__(self, XAML_FILE)
        self.doc = doc
        self.uidoc = uidoc
        self.settings = settings
        self.logger = logger
        self.plans = plans
        self.confirmed = False

        self._loading = True
        self._rows = ObservableCollection[object]()
        for plan in plans:
            row = PreviewRow(doc, plan)
            row.add_PropertyChanged(self._on_row_changed)
            self._rows.Add(row)

        self.grid.ItemsSource = self._rows
        self._view = self.grid.Items      # ItemCollection supports Filter
        self._view.Filter = Predicate[object](self._filter_row)

        self.lbl_title.Text = title
        self._build_filters()
        self._loading = False
        self._update_counts()
        self._update_subtitle()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_filters(self):
        categories = sorted(set(p.category for p in self.plans if p.category))
        self.cmb_category.ItemsSource = [ALL_ITEM] + categories
        self.cmb_category.SelectedIndex = 0

        statuses = sorted(set(p.status for p in self.plans))
        self.cmb_status.ItemsSource = [ALL_ITEM] + statuses
        self.cmb_status.SelectedIndex = 0

    def _update_subtitle(self):
        actionable = len([p for p in self.plans if p.is_actionable])
        correct = len([p for p in self.plans
                       if p.status == Status.ALREADY_CORRECT])
        other = len(self.plans) - actionable - correct
        self.lbl_subtitle.Text = (
            '%d element(s) analysed - %d change(s) proposed, %d already '
            'correct, %d skipped or unsupported. Untick any row you do not '
            'want to change.' % (len(self.plans), actionable, correct, other))

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------
    def _filter_row(self, row):
        try:
            category = self.cmb_category.SelectedItem
            if category and category != ALL_ITEM and row.Category != category:
                return False
            status = self.cmb_status.SelectedItem
            if status and status != ALL_ITEM and row.Status != status:
                return False
            return row.matches(self.txt_search.Text)
        except Exception:
            return True

    def _visible_rows(self):
        return [row for row in self._rows if self._filter_row(row)]

    def _refresh_view(self):
        try:
            self._view.Refresh()
        except Exception:
            pass
        self._update_counts()

    def _update_counts(self):
        ticked = len([r for r in self._rows if r.Selected])
        shown = len(self._visible_rows())
        self.lbl_counts.Text = ('%d row(s) shown - %d ticked for update'
                                % (shown, ticked))
        self.btn_apply.IsEnabled = ticked > 0

    def _on_row_changed(self, sender, args):
        if not self._loading:
            self._update_counts()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def on_filter_changed(self, sender, args):
        if self._loading:
            return
        self._refresh_view()

    def on_clear_filters(self, sender, args):
        self._loading = True
        self.cmb_category.SelectedIndex = 0
        self.cmb_status.SelectedIndex = 0
        self.txt_search.Text = ''
        self._loading = False
        self._refresh_view()

    def on_check_shown(self, sender, args):
        for row in self._visible_rows():
            if row.plan.is_actionable:
                row.Selected = True
        self._update_counts()

    def on_uncheck_shown(self, sender, args):
        for row in self._visible_rows():
            row.Selected = False
        self._update_counts()

    def on_uncheck_selected(self, sender, args):
        for row in list(self.grid.SelectedItems):
            try:
                row.Selected = False
            except Exception:
                pass
        self._update_counts()

    def on_row_selection_changed(self, sender, args):
        try:
            count = self.grid.SelectedItems.Count
        except Exception:
            count = 0
        self.lbl_status.Text = ('%d row(s) highlighted' % count) if count else ''

    def on_row_double_click(self, sender, args):
        self.on_zoom(sender, args)

    # -- Revit interaction ------------------------------------------------
    def _highlighted_ids(self):
        ids = List[DB.ElementId]()
        try:
            for row in self.grid.SelectedItems:
                ids.Add(make_eid(row.ElementId))
        except Exception:
            pass
        return ids

    def on_select_in_revit(self, sender, args):
        ids = self._highlighted_ids()
        if ids.Count == 0:
            return
        try:
            self.uidoc.Selection.SetElementIds(ids)
            self.lbl_status.Text = '%d element(s) selected in Revit.' % ids.Count
        except Exception as err:
            self.logger.warning('Selection failed: %s', err)

    def on_zoom(self, sender, args):
        ids = self._highlighted_ids()
        if ids.Count == 0:
            return
        try:
            self.uidoc.Selection.SetElementIds(ids)
            self.uidoc.ShowElements(ids)
            self.lbl_status.Text = 'Zoomed to %d element(s).' % ids.Count
        except Exception as err:
            self.logger.warning('Zoom failed: %s', err)

    # -- export -----------------------------------------------------------
    def on_export(self, sender, args):
        try:
            path = forms.save_file(
                file_ext='csv',
                default_name=os.path.basename(
                    reporting.default_report_path(self.doc,
                                                  prefix='MEPLevelPreview')))
            if not path:
                return
            reporting.write_csv(
                self.doc, self.plans, path=path,
                title='MEP Level Fixer - PREVIEW (no changes applied)',
                extra_info=[('Report type', 'Preview')])
            self.lbl_status.Text = 'Preview exported to %s' % path
            self.logger.info('Preview exported to %s', path)
        except Exception as err:
            self.logger.error('Preview export failed: %s', err)
            forms.alert('Could not write the preview file:\n%s' % err,
                        title='MEP Level Fixer')

    # -- close ------------------------------------------------------------
    def on_cancel(self, sender, args):
        self.confirmed = False
        self.Close()

    def on_apply(self, sender, args):
        ticked = len([r for r in self._rows if r.Selected])
        if not ticked:
            forms.alert('No rows are ticked, so there is nothing to apply.',
                        title='MEP Level Fixer')
            return
        if not forms.alert('Apply the level correction to %d element(s)?\n\n'
                           'Elements that cannot be updated without moving '
                           'will be rolled back automatically and listed in '
                           'the report.' % ticked,
                           title='Confirm', yes=True, no=True):
            return
        # Push the final tick state back into the plans.
        for row in self._rows:
            row.plan.selected = row.Selected
            if not row.Selected and row.plan.is_actionable:
                row.plan.status = Status.DESELECTED
                row.plan.message = 'Deselected by the user in the preview'
        self.confirmed = True
        self.Close()
