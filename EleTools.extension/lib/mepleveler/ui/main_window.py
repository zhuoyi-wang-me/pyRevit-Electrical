# -*- coding: utf-8 -*-
"""Main dialog: processing scope, level matching options and category picker."""

import os
import clr

# pyRevit already loads the WPF stack, but referencing it explicitly keeps the
# module importable from a plain python engine and on .NET 8 (Revit 2025/2026).
for _assembly in ('PresentationFramework', 'PresentationCore', 'WindowsBase',
                  'System.Xaml'):
    try:
        clr.AddReference(_assembly)
    except Exception:
        pass

from System import Predicate
from System.Collections.ObjectModel import ObservableCollection
from System.ComponentModel import INotifyPropertyChanged, PropertyChangedEventArgs
from System.Windows.Data import CollectionViewSource, PropertyGroupDescription
from System.Windows.Input import Cursors

from pyrevit import forms

from .. import config
from ..revit_compat import to_display_length, from_display_length, \
    length_unit_label, format_length

XAML_FILE = os.path.join(os.path.dirname(__file__), 'MainWindow.xaml')


# ---------------------------------------------------------------------------
# Bindable view models
# ---------------------------------------------------------------------------

class NotifyBase(INotifyPropertyChanged):
    """Minimal INotifyPropertyChanged implementation for Python objects."""

    def __init__(self):
        self._handlers = []

    def add_PropertyChanged(self, handler):
        self._handlers.append(handler)

    def remove_PropertyChanged(self, handler):
        if handler in self._handlers:
            self._handlers.remove(handler)

    def raise_property_changed(self, name):
        args = PropertyChangedEventArgs(name)
        for handler in list(self._handlers):
            try:
                handler(self, args)
            except Exception:
                pass


class CategoryItem(NotifyBase):
    """One row of the category checklist."""

    def __init__(self, category_def, checked=False, count=None):
        NotifyBase.__init__(self)
        self._def = category_def
        self._checked = bool(checked)
        self._count = count

    # -- bound properties -------------------------------------------------
    @property
    def IsChecked(self):
        return self._checked

    @IsChecked.setter
    def IsChecked(self, value):
        value = bool(value)
        if value != self._checked:
            self._checked = value
            self.raise_property_changed('IsChecked')

    @property
    def DisplayName(self):
        return self._def.name

    @property
    def Discipline(self):
        return self._def.discipline

    @property
    def BicName(self):
        return self._def.bic_name

    @property
    def Count(self):
        return self._count

    @property
    def CountLabel(self):
        if self._count is None:
            return u''
        return u'{:,}'.format(self._count)

    # -- helpers ----------------------------------------------------------
    def set_count(self, value):
        self._count = value
        self.raise_property_changed('Count')
        self.raise_property_changed('CountLabel')

    def matches(self, needle):
        if not needle:
            return True
        needle = needle.lower()
        return (needle in self._def.name.lower()
                or needle in self._def.discipline.lower()
                or needle in self._def.bic_name.lower())


class LevelItem(NotifyBase):
    """One row of the target level checklist."""

    def __init__(self, doc, level_info, checked=True, usage=None):
        NotifyBase.__init__(self)
        self._doc = doc
        self._info = level_info
        self._checked = bool(checked)
        self._usage = usage

    # -- bound properties -------------------------------------------------
    @property
    def IsChecked(self):
        return self._checked

    @IsChecked.setter
    def IsChecked(self, value):
        value = bool(value)
        if value != self._checked:
            self._checked = value
            self.raise_property_changed('IsChecked')

    @property
    def DisplayName(self):
        return self._info.name

    @property
    def ElevationLabel(self):
        return format_length(self._doc, self._info.elevation, 1)

    @property
    def Usage(self):
        return self._usage

    @property
    def UsageLabel(self):
        if not self._usage:
            return u''
        return u'{:,} in use'.format(self._usage)

    @property
    def ToolTipText(self):
        return u'Level id %s - elevation %s' % (self._info.id_value,
                                                self.ElevationLabel)

    # -- helpers ----------------------------------------------------------
    @property
    def info(self):
        return self._info

    def set_usage(self, value):
        self._usage = value
        self.raise_property_changed('Usage')
        self.raise_property_changed('UsageLabel')

    def matches(self, needle):
        if not needle:
            return True
        return needle.lower() in (self._info.name or u'').lower()


class ComboItem(object):
    """Key/label pair for the option combo boxes."""

    def __init__(self, key, label):
        self.Key = key
        self.Label = label

    def __str__(self):
        return self.Label


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class MainWindowResult(object):
    """What the user asked for when the dialog closed."""

    CANCEL = 'cancel'
    PREVIEW = 'preview'
    EXECUTE = 'execute'

    def __init__(self, action=CANCEL, settings=None):
        self.action = action
        self.settings = settings

    @property
    def cancelled(self):
        return self.action == self.CANCEL


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class MainWindow(forms.WPFWindow):
    """The main tool dialog."""

    def __init__(self, doc, uidoc, settings, engine, logger):
        forms.WPFWindow.__init__(self, XAML_FILE)
        self.doc = doc
        self.uidoc = uidoc
        self.settings = settings
        self.engine = engine
        self.logger = logger
        self.result = MainWindowResult()

        self._loading = True
        self._items = ObservableCollection[object]()
        self._levels = ObservableCollection[object]()
        self._defs = config.available_categories()

        self._build_categories()
        self._build_levels()
        self._build_combos()
        self._load_settings()
        self._loading = False

        self._update_model_label()
        self._refresh_counts()
        self._update_summary()
        self._update_level_summary()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_categories(self):
        selected = set(self.settings.categories or [])
        for cat_def in self._defs:
            item = CategoryItem(cat_def, checked=cat_def.bic_name in selected)
            # Keep the "n of m categories selected" footer in sync with the
            # checkboxes without polling.
            item.add_PropertyChanged(self._on_item_changed)
            self._items.Add(item)

        source = CollectionViewSource()
        source.Source = self._items
        source.GroupDescriptions.Add(PropertyGroupDescription('Discipline'))
        self._view = source.View
        self._view.Filter = Predicate[object](self._filter_item)
        self.lst_categories.ItemsSource = self._view

    def _build_levels(self):
        """Populate the target level checklist, highest level first."""
        remembered = self.settings.levels_for(self.doc)
        remembered_set = set(n.strip() for n in remembered if n)

        infos = sorted(self.engine.resolver.all_levels,
                       key=lambda lv: lv.elevation, reverse=True)
        for info in infos:
            # First run on a model: everything is a valid target. After that,
            # the remembered selection wins - including levels that were
            # unticked, which is the whole point of the feature.
            checked = (True if not remembered_set
                       else (info.name or u'').strip() in remembered_set)
            item = LevelItem(self.doc, info, checked=checked)
            item.add_PropertyChanged(self._on_level_changed)
            self._levels.Add(item)

        source = CollectionViewSource()
        source.Source = self._levels
        self._level_view = source.View
        self._level_view.Filter = Predicate[object](self._filter_level)
        self.lst_levels.ItemsSource = self._level_view

    def _filter_level(self, item):
        try:
            return item.matches(self.txt_level_search.Text)
        except Exception:
            return True

    def _visible_levels(self):
        return [item for item in self._levels if self._filter_level(item)]

    def _on_level_changed(self, sender, args):
        if self._loading:
            return
        try:
            if args.PropertyName == 'IsChecked':
                self._update_level_summary()
        except Exception:
            pass

    def _on_item_changed(self, sender, args):
        if self._loading:
            return
        try:
            if args.PropertyName == 'IsChecked':
                self._update_summary()
        except Exception:
            pass

    def _filter_item(self, item):
        try:
            return item.matches(self.txt_search.Text)
        except Exception:
            return True

    def _build_combos(self):
        strategies = [ComboItem(key, config.STRATEGY_LABELS[key])
                      for key in (config.STRATEGY_NEAREST,
                                  config.STRATEGY_BELOW,
                                  config.STRATEGY_BELOW_SNAP)]
        self.cmb_strategy.ItemsSource = strategies

        elevations = [ComboItem(key, config.ELEV_LABELS[key])
                      for key in (config.ELEV_AUTO, config.ELEV_INSERTION,
                                  config.ELEV_MIDPOINT, config.ELEV_BBOX)]
        self.cmb_elevation.ItemsSource = elevations

    def _select_combo(self, combo, key):
        for item in combo.ItemsSource:
            if item.Key == key:
                combo.SelectedItem = item
                return
        if combo.ItemsSource:
            combo.SelectedIndex = 0

    # ------------------------------------------------------------------
    # Settings <-> UI
    # ------------------------------------------------------------------
    def _load_settings(self):
        settings = self.settings
        unit = length_unit_label(self.doc)

        self.rb_selection.IsChecked = settings.scope == config.SCOPE_SELECTION
        self.rb_view.IsChecked = settings.scope == config.SCOPE_VIEW
        self.rb_model.IsChecked = settings.scope == config.SCOPE_MODEL
        if not (self.rb_selection.IsChecked or self.rb_view.IsChecked
                or self.rb_model.IsChecked):
            self.rb_view.IsChecked = True

        self._select_combo(self.cmb_strategy, settings.strategy)
        self._select_combo(self.cmb_elevation, settings.elevation_source)

        self.lbl_snap.Text = 'Snap tolerance (%s)' % unit
        self.lbl_tolerance.Text = 'Movement tolerance (%s)' % unit
        self.txt_snap.Text = '%.2f' % to_display_length(
            self.doc, settings.snap_tolerance)
        self.txt_tolerance.Text = '%.3f' % to_display_length(
            self.doc, settings.move_tolerance)

        self.chk_skip_grouped.IsChecked = settings.skip_grouped
        self.chk_skip_design_options.IsChecked = \
            settings.skip_design_option_elements
        self.chk_skip_pinned.IsChecked = settings.skip_pinned
        self.chk_skip_nested.IsChecked = settings.skip_nested_components
        self.chk_schedule_fallback.IsChecked = \
            settings.use_schedule_level_fallback
        self.chk_verify_connected.IsChecked = settings.verify_connected_elements
        self.chk_debug.IsChecked = settings.debug

        self._update_strategy_state()
        self._update_scope_hint()

    def _harvest_settings(self):
        """Copy the UI state back into the settings object."""
        settings = self.settings
        settings.scope = self.current_scope
        settings.categories = self.selected_categories

        if self.cmb_strategy.SelectedItem is not None:
            settings.strategy = self.cmb_strategy.SelectedItem.Key
        if self.cmb_elevation.SelectedItem is not None:
            settings.elevation_source = self.cmb_elevation.SelectedItem.Key

        settings.snap_tolerance = self._read_length(
            self.txt_snap, settings.snap_tolerance)
        settings.move_tolerance = max(1e-6, self._read_length(
            self.txt_tolerance, settings.move_tolerance))

        settings.set_levels_for(self.doc, self.selected_levels)

        settings.skip_grouped = bool(self.chk_skip_grouped.IsChecked)
        settings.skip_design_option_elements = \
            bool(self.chk_skip_design_options.IsChecked)
        settings.skip_pinned = bool(self.chk_skip_pinned.IsChecked)
        settings.skip_nested_components = bool(self.chk_skip_nested.IsChecked)
        settings.use_schedule_level_fallback = \
            bool(self.chk_schedule_fallback.IsChecked)
        settings.verify_connected_elements = \
            bool(self.chk_verify_connected.IsChecked)
        settings.debug = bool(self.chk_debug.IsChecked)
        settings.save()
        return settings

    def _read_length(self, textbox, fallback):
        """Read a display-unit length from a textbox, in internal units."""
        try:
            return from_display_length(self.doc, float(textbox.Text))
        except Exception:
            return fallback

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------
    @property
    def current_scope(self):
        if self.rb_selection.IsChecked:
            return config.SCOPE_SELECTION
        if self.rb_model.IsChecked:
            return config.SCOPE_MODEL
        return config.SCOPE_VIEW

    @property
    def selected_categories(self):
        return [item.BicName for item in self._items if item.IsChecked]

    @property
    def selected_levels(self):
        """Names of the levels ticked as valid targets."""
        return [item.DisplayName for item in self._levels if item.IsChecked]

    # ------------------------------------------------------------------
    # Counts and labels
    # ------------------------------------------------------------------
    def _update_model_label(self):
        try:
            title = self.doc.Title
        except Exception:
            title = ''
        try:
            view_name = self.uidoc.ActiveView.Name
        except Exception:
            view_name = ''
        self.lbl_model.Text = 'Model: %s     Active view: %s' % (title,
                                                                 view_name)

    def _update_scope_hint(self):
        scope = self.current_scope
        if scope == config.SCOPE_SELECTION:
            try:
                count = self.uidoc.Selection.GetElementIds().Count
            except Exception:
                count = 0
            self.lbl_scope_hint.Text = (
                '%d element(s) currently selected.' % count
                if count else
                'Nothing is selected - select elements in Revit first, or '
                'choose another scope.')
        elif scope == config.SCOPE_VIEW:
            self.lbl_scope_hint.Text = (
                'Only elements visible in the active view are processed. '
                'View filters and crop regions apply.')
        else:
            self.lbl_scope_hint.Text = (
                'Every element of the selected categories in the model is '
                'processed. This can take several minutes on large models.')

    def _refresh_counts(self):
        """Recount elements per category for the current scope."""
        try:
            self.Cursor = Cursors.Wait
            self.lbl_status.Text = 'Counting elements...'
            names = [item.BicName for item in self._items]
            counts = self.engine.count_by_category(self.current_scope,
                                                   self.uidoc, names)
            for item in self._items:
                item.set_count(counts.get(item.BicName, 0))
            self.lbl_status.Text = ''
        except Exception as err:
            self.logger.warning('Count failed: %s', err)
            self.lbl_status.Text = 'Element counts unavailable.'
        finally:
            self.Cursor = Cursors.Arrow
            self._update_summary()

    def _update_summary(self):
        chosen = [item for item in self._items if item.IsChecked]
        total = 0
        known = False
        for item in chosen:
            if item.Count is not None:
                total += item.Count
                known = True
        if known:
            self.lbl_cat_summary.Text = (
                '%d of %d categories selected - approximately %s element(s) '
                'in scope.' % (len(chosen), self._items.Count,
                               '{:,}'.format(total)))
        else:
            self.lbl_cat_summary.Text = '%d of %d categories selected.' % (
                len(chosen), self._items.Count)

    def _update_level_summary(self):
        ticked = [item for item in self._levels if item.IsChecked]
        total = self._levels.Count
        if not ticked:
            self.lbl_level_summary.Text = (
                'No target level is ticked - tick at least one level before '
                'running.')
            return
        excluded = total - len(ticked)
        extremes = ''
        if len(ticked) > 1:
            try:
                lowest = min(ticked, key=lambda i: i.info.elevation)
                highest = max(ticked, key=lambda i: i.info.elevation)
                extremes = ' (%s to %s)' % (lowest.DisplayName,
                                            highest.DisplayName)
            except Exception:
                extremes = ''
        self.lbl_level_summary.Text = (
            '%d of %d levels are valid targets%s. %d level(s) will never be '
            'assigned.' % (len(ticked), total, extremes, excluded))

    def _update_strategy_state(self):
        try:
            key = self.cmb_strategy.SelectedItem.Key
        except Exception:
            key = config.STRATEGY_NEAREST
        enabled = key == config.STRATEGY_BELOW_SNAP
        self.txt_snap.IsEnabled = enabled
        self.lbl_snap.Opacity = 1.0 if enabled else 0.5

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def on_scope_changed(self, sender, args):
        if self._loading:
            return
        self._update_scope_hint()
        self._refresh_counts()

    def on_strategy_changed(self, sender, args):
        if self._loading:
            return
        self._update_strategy_state()

    def on_search_changed(self, sender, args):
        if self._loading:
            return
        try:
            self._view.Refresh()
        except Exception:
            pass

    def on_clear_search(self, sender, args):
        self.txt_search.Text = ''

    def on_select_all(self, sender, args):
        for item in self._items:
            if self._filter_item(item):
                item.IsChecked = True
        self._update_summary()

    def on_select_none(self, sender, args):
        for item in self._items:
            if self._filter_item(item):
                item.IsChecked = False
        self._update_summary()

    def on_select_nonempty(self, sender, args):
        for item in self._items:
            item.IsChecked = bool(item.Count)
        self._update_summary()

    def on_refresh_counts(self, sender, args):
        self._refresh_counts()

    # -- level list -------------------------------------------------------
    def on_level_search_changed(self, sender, args):
        if self._loading:
            return
        try:
            self._level_view.Refresh()
        except Exception:
            pass

    def on_clear_level_search(self, sender, args):
        self.txt_level_search.Text = ''

    def on_levels_all(self, sender, args):
        for item in self._levels:
            item.IsChecked = True
        self._update_level_summary()

    def on_levels_none(self, sender, args):
        for item in self._levels:
            item.IsChecked = False
        self._update_level_summary()

    def on_levels_tick_shown(self, sender, args):
        for item in self._visible_levels():
            item.IsChecked = True
        self._update_level_summary()

    def on_levels_untick_shown(self, sender, args):
        for item in self._visible_levels():
            item.IsChecked = False
        self._update_level_summary()

    def on_levels_invert(self, sender, args):
        for item in self._levels:
            item.IsChecked = not item.IsChecked
        self._update_level_summary()

    def on_levels_in_use(self, sender, args):
        """Tick only the levels currently referenced by elements in scope."""
        categories = self.selected_categories
        if not categories:
            forms.alert('Select at least one category first - the usage count '
                        'is based on the categories in scope.',
                        title='MEP Level Fixer')
            return
        try:
            self.Cursor = Cursors.Wait
            self.lbl_status.Text = 'Counting level usage...'
            usage = self.engine.count_by_level(self.current_scope, self.uidoc,
                                               categories)
            for item in self._levels:
                count = usage.get(item.info.id_value, 0)
                item.set_usage(count)
                item.IsChecked = count > 0
            self.lbl_status.Text = ''
        except Exception as err:
            self.logger.warning('Level usage count failed: %s', err)
            self.lbl_status.Text = 'Level usage counts unavailable.'
        finally:
            self.Cursor = Cursors.Arrow
            self._update_level_summary()

    def on_cancel(self, sender, args):
        self.result = MainWindowResult(MainWindowResult.CANCEL)
        self.Close()

    def on_preview(self, sender, args):
        self._finish(MainWindowResult.PREVIEW)

    def on_execute(self, sender, args):
        self._finish(MainWindowResult.EXECUTE)

    def _finish(self, action):
        if not self.selected_categories:
            forms.alert('Select at least one category before continuing.',
                        title='MEP Level Fixer')
            return
        if not self.selected_levels:
            forms.alert('No target level is ticked.\n\nTick the levels that '
                        'elements are allowed to be allocated to, then try '
                        'again.', title='MEP Level Fixer')
            return
        if self.current_scope == config.SCOPE_SELECTION:
            try:
                count = self.uidoc.Selection.GetElementIds().Count
            except Exception:
                count = 0
            if not count:
                forms.alert('Nothing is selected in Revit. Select elements '
                            'first or choose another processing scope.',
                            title='MEP Level Fixer')
                return
        settings = self._harvest_settings()
        self.result = MainWindowResult(action, settings)
        self.Close()
