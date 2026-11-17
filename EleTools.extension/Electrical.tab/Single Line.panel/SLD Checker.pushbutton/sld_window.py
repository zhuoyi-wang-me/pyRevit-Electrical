# -*- coding: utf-8 -*-
"""SLD Checker - setup window: scope, checks, parameter mapping, templates.
Modal; no Revit API calls here (discovery is done before it opens)."""
import copy
import os

from pyrevit import forms
from System.Windows import FontWeights, GridLength, GridUnitType, Thickness, TextWrapping, \
    VerticalAlignment, Visibility
from System.Windows.Controls import (CheckBox, ColumnDefinition, ComboBox, Grid,
                                     RowDefinition, StackPanel, TextBlock,
                                     TextChangedEventHandler)
from System.Windows.Controls.Primitives import TextBoxBase
from System.Windows.Media import ColorConverter, SolidColorBrush

from sldcheck import checks, config, rules

BUNDLE_DIR = os.path.dirname(__file__)
LOAD_UNITS = [u"kVA", u"kW"]


def brush(hex_color):
    return SolidColorBrush(ColorConverter.ConvertFromString(hex_color))


GREEN, RED, GREY, BLUE, AMBER = (brush(c) for c in
                                 (u"#0F7B0F", u"#C42B1C", u"#7A7A7A", u"#005FB8", u"#9D5D00"))


def _text(value, size=None, bold=False, color=None, wrap=True, margin=None):
    block = TextBlock()
    block.Text = value
    if size:
        block.FontSize = size
    if bold:
        block.FontWeight = FontWeights.SemiBold
    if color is not None:
        block.Foreground = color
    if wrap:
        block.TextWrapping = TextWrapping.Wrap
    if margin is not None:
        block.Margin = margin
    return block


class Discovered(object):
    """Parameters found in the model for one source (SLD, equipment, ...)."""

    def __init__(self, scan):
        found, self.scanned = scan if scan else ({}, 0)
        self.by_key = {}
        for name, (count, sample) in found.items():
            key = rules.param_key(name)
            old = self.by_key.get(key)
            if old is None or count > old[1]:
                self.by_key[key] = (name, count, sample)
        self.names = sorted((v[0] for v in self.by_key.values()), key=lambda n: n.lower())
        self.with_values = [v[0] for v in self.by_key.values() if v[1] > 0]


class MappingRow(object):
    """One field: label | editable parameter combo | live status."""

    def __init__(self, grid, row, field, label, hint, discovered, on_change):
        self.field = field
        self.discovered = discovered
        grid.RowDefinitions.Add(RowDefinition())
        left = StackPanel()
        left.Margin = Thickness(0, 6, 14, 6)
        left.Children.Add(_text(label, bold=True))
        if hint:
            left.Children.Add(_text(hint, size=11.5, color=GREY))
        self.combo = ComboBox()
        self.combo.IsEditable = True
        self.combo.IsTextSearchEnabled = False
        self.combo.Margin = Thickness(0, 6, 14, 6)
        self.combo.ItemsSource = discovered.names
        self.combo.ToolTip = u"Pick or type; separate fallbacks with commas"
        self.status = _text(u"", size=12, margin=Thickness(0, 6, 0, 6))
        self.status.VerticalAlignment = VerticalAlignment.Center
        for col, child in enumerate((left, self.combo, self.status)):
            Grid.SetRow(child, row)
            Grid.SetColumn(child, col)
            grid.Children.Add(child)
        self._on_change = on_change
        self.combo.AddHandler(TextBoxBase.TextChangedEvent,
                              TextChangedEventHandler(self._changed))

    def _changed(self, sender, args):
        self.refresh()
        if self._on_change:
            self._on_change()

    @property
    def names(self):
        return config.text_to_names(self.combo.Text)

    def set_names(self, names):
        self.combo.Text = config.names_to_text(names)
        self.refresh()

    def found_count(self):
        best = 0
        for name in self.names:
            if name.lower().startswith(u"bip:"):
                return -1
            hit = self.discovered.by_key.get(rules.param_key(name))
            if hit:
                best = max(best, hit[1])
        return best

    def refresh(self):
        names = self.names
        if not names:
            self.status.Text, self.status.Foreground = u"– not mapped - not compared", GREY
            return
        empty = None
        for name in names:
            if name.lower().startswith(u"bip:"):
                self.status.Text = u"● Revit built-in parameter {0}".format(name[4:])
                self.status.Foreground = BLUE
                return
            hit = self.discovered.by_key.get(rules.param_key(name))
            if hit and hit[1] > 0:
                sample = hit[2] if len(hit[2]) <= 40 else hit[2][:37] + u"..."
                self.status.Text = u"✓ {0}: value on {1} of {2} elements  ·  e.g. '{3}'".format(
                    hit[0], hit[1], self.discovered.scanned, sample)
                self.status.Foreground = GREEN
                return
            if hit and empty is None:
                empty = hit[0]
        if empty:
            self.status.Text = u"⚠ '{0}' exists but is empty everywhere".format(empty)
            self.status.Foreground = AMBER
        else:
            self.status.Text = u"✗ not found on any scanned element"
            self.status.Foreground = RED


def _setup_columns(grid):
    grid.Children.Clear()
    grid.RowDefinitions.Clear()
    grid.ColumnDefinitions.Clear()
    for width in (GridLength(250), GridLength(1, GridUnitType.Star), GridLength(1.1, GridUnitType.Star)):
        column = ColumnDefinition()
        column.Width = width
        grid.ColumnDefinitions.Add(column)


class ViewItem(object):
    def __init__(self, view_id, name, view_type, count):
        self.view_id = view_id
        self.search = (u"{0} {1}".format(name, view_type)).lower()
        self.box = CheckBox()
        self.box.Content = u"{0}   [{1}]  ·  {2} items".format(name, view_type, count)


class SetupWindow(forms.WPFWindow):
    # class defaults: WPF raises events while the XAML loads, before __init__
    _ready = False
    result = None

    def __init__(self, views, active_view_id, discovery, template_name):
        forms.WPFWindow.__init__(self, os.path.join(BUNDLE_DIR, "ui.xaml"))
        self._ready = False
        self.result = None
        self._active_view_id = active_view_id
        self._cfg = config.load_template(template_name)
        self._template = template_name
        self._sld_disc = Discovered(discovery.get("sld"))
        self._model_disc = dict((key, Discovered(discovery.get(key)))
                                for key, _title in config.MODEL_SECTIONS)

        self._view_items = [ViewItem(*v) for v in views]
        for item in self._view_items:
            self.views_panel.Children.Add(item.box)
        self._active_ok = any(v.view_id == active_view_id for v in self._view_items)
        self.rb_active.IsEnabled = self._active_ok
        self.load_unit.ItemsSource = LOAD_UNITS

        self._check_boxes = {}
        self._build_checks()
        self.sld_info.Text = (u"{0} Detail Items scanned, {1} parameter names found.".format(
            self._sld_disc.scanned, len(self._sld_disc.names)))
        self._fill_template_combo(template_name)
        self._load_into_ui(self._cfg)
        self._ready = True
        self._status(u"Template '{0}' loaded.".format(template_name))

    # ------------------------------------------------------------------
    # building blocks
    # ------------------------------------------------------------------
    def _status(self, text, error=False):
        self.status_text.Text = text
        self.status_text.Foreground = RED if error else GREY

    def _build_checks(self):
        group = None
        for key, check_group, label, locked in checks.CHECK_ITEMS:
            if check_group != group:
                group = check_group
                self.checks_panel.Children.Add(
                    _text(check_group, bold=True, margin=Thickness(0, 10 if self._check_boxes else 0, 0, 2)))
            box = CheckBox()
            box.Content = label
            box.Margin = Thickness(12, 3, 0, 3)
            if locked:
                box.IsChecked = True
                box.IsEnabled = False
            self._check_boxes[key] = box
            self.checks_panel.Children.Add(box)

    def _build_sld_rows(self, sld_fields):
        _setup_columns(self.sld_grid)
        self._sld_rows = []
        for index, (field, label, hint, _kw) in enumerate(config.FIELD_INFO):
            row = MappingRow(self.sld_grid, index, field, label, hint, self._sld_disc, None)
            row.set_names(sld_fields.get(field, []))
            self._sld_rows.append(row)

    def _build_model_rows(self, cfg):
        self.model_panel.Children.Clear()
        self._model_rows = {}
        for section, title in config.MODEL_SECTIONS:
            disc = self._model_disc[section]
            header = _text(u"{0}   ({1} scanned)".format(title, disc.scanned), size=14, bold=True,
                           margin=Thickness(0, 12 if self._model_rows else 0, 0, 4))
            self.model_panel.Children.Add(header)
            grid = Grid()
            _setup_columns(grid)
            rows = []
            for index, (field, names) in enumerate(sorted(
                    cfg[section].items(), key=lambda kv: self._field_order(kv[0]))):
                row = MappingRow(grid, index, field, config.FIELD_LABELS.get(field, field),
                                 u"", disc, None)
                row.set_names(names)
                rows.append(row)
            self._model_rows[section] = rows
            self.model_panel.Children.Add(grid)

    @staticmethod
    def _field_order(field):
        order = [f[0] for f in config.FIELD_INFO] + ["load_name", "panel", "connected_load_kw",
                                                     "design_current"]
        return order.index(field) if field in order else len(order)

    def _fill_template_combo(self, select):
        was = self._ready
        self._ready = False
        names = config.list_templates()
        self.template_combo.ItemsSource = names
        self.template_combo.SelectedItem = select if select in names else config.DEFAULT_TEMPLATE
        self._ready = was

    # ------------------------------------------------------------------
    # cfg <-> UI
    # ------------------------------------------------------------------
    def _load_into_ui(self, cfg):
        was = self._ready
        self._ready = False
        mode = cfg.get("scope_mode", "all")
        if mode == "active" and not self._active_ok:
            mode = "all"
        {"all": self.rb_all, "active": self.rb_active, "pick": self.rb_pick}.get(
            mode, self.rb_all).IsChecked = True
        self._update_scope_ui()

        enabled = checks.enabled_checks(cfg)
        for key, box in self._check_boxes.items():
            box.IsChecked = enabled.get(key, True)
        self.chk_show_info.IsChecked = bool(cfg.get("show_info", True))

        self._build_sld_rows(cfg["sld_fields"])
        self._build_model_rows(cfg)

        roles = dict((role, words) for role, words in cfg["sld_roles"])
        if "ignore" not in roles:          # templates saved before the ignore list existed
            roles["ignore"] = dict(config.DEFAULTS["sld_roles"]).get("ignore", [])
        self.role_ignore.Text = u", ".join(roles.get("ignore", []))
        self.suffix_patterns.Text = u"\n".join(cfg.get("name_suffix_patterns") or [])
        self.role_cable.Text = u", ".join(roles.get("cable", []))
        self.role_breaker.Text = u", ".join(roles.get("breaker", []))
        self.role_equipment.Text = u", ".join(roles.get("equipment", []))
        self.spare_keywords.Text = u", ".join(cfg["spare_keywords"])
        self.class_rules.Text = config.rules_to_text(cfg["classification_rules"])

        self.tol_load.Text = rules.fmt_num(cfg["load_tolerance_pct"], 3)
        self.tol_rating.Text = rules.fmt_num(cfg["rating_tolerance_a"], 3)
        self.tol_cable.Text = rules.fmt_num(cfg["cable_size_tolerance_mm2"], 3)
        self.max_rows.Text = rules.to_text(cfg["max_rows_per_section"])
        unit = cfg.get("default_load_unit", u"kVA")
        self.load_unit.SelectedItem = unit if unit in LOAD_UNITS else u"kVA"
        self.chk_multi_view.IsChecked = bool(cfg["allow_same_name_in_different_views"])
        self.chk_expect_all.IsChecked = bool(cfg["expect_all_circuits_on_sld"])
        self.chk_sections.IsChecked = bool(cfg["merge_board_sections"])
        self.chk_links.IsChecked = bool(cfg["include_linked_models"])
        self._ready = was

    def _number(self, box, label, minimum=0.0):
        value = rules.to_float(box.Text)
        if value is None or value < minimum:
            raise ValueError(u"'{0}' must be a number >= {1}.".format(label, rules.fmt_num(minimum)))
        return value

    def _read_from_ui(self):
        """Current window state as a complete cfg; ValueError on bad input."""
        cfg = copy.deepcopy(self._cfg)
        cfg["scope_mode"] = self._scope_mode()
        cfg["checks"] = dict((key, bool(box.IsChecked)) for key, box in self._check_boxes.items())
        cfg["show_info"] = bool(self.chk_show_info.IsChecked)
        cfg["report_spare_circuits_missing_on_sld"] = True       # switched by the check toggle
        for row in self._sld_rows:
            cfg["sld_fields"][row.field] = row.names
        if not cfg["sld_fields"].get("name"):
            raise ValueError(u"Map 'Panel / equipment name' on the SLD Parameters tab - it is the "
                             u"matching key.")
        for section, rows in self._model_rows.items():
            for row in rows:
                cfg[section][row.field] = row.names
        if not cfg["model_equipment_fields"].get("name"):
            raise ValueError(u"Map the model equipment 'Panel / equipment name' (Model Parameters).")
        cfg["name_suffix_patterns"] = config.text_to_patterns(self.suffix_patterns.Text)
        cfg["sld_roles"] = [["ignore", config.text_to_names(self.role_ignore.Text)],
                            ["cable", config.text_to_names(self.role_cable.Text)],
                            ["breaker", config.text_to_names(self.role_breaker.Text)],
                            ["equipment", config.text_to_names(self.role_equipment.Text)]]
        cfg["spare_keywords"] = config.text_to_names(self.spare_keywords.Text)
        cfg["classification_rules"] = config.text_to_rules(self.class_rules.Text)
        cfg["load_tolerance_pct"] = self._number(self.tol_load, u"Load tolerance")
        cfg["rating_tolerance_a"] = self._number(self.tol_rating, u"Breaker rating tolerance")
        cfg["cable_size_tolerance_mm2"] = self._number(self.tol_cable, u"Cable size tolerance")
        cfg["max_rows_per_section"] = int(self._number(self.max_rows, u"Max rows", 10))
        cfg["default_load_unit"] = self.load_unit.SelectedItem or u"kVA"
        cfg["allow_same_name_in_different_views"] = bool(self.chk_multi_view.IsChecked)
        cfg["expect_all_circuits_on_sld"] = bool(self.chk_expect_all.IsChecked)
        cfg["merge_board_sections"] = bool(self.chk_sections.IsChecked)
        cfg["include_linked_models"] = bool(self.chk_links.IsChecked)
        cfg.pop("_template", None)
        cfg.pop("_readme", None)
        return cfg

    # ------------------------------------------------------------------
    # scope
    # ------------------------------------------------------------------
    def _scope_mode(self):
        if self.rb_active.IsChecked:
            return "active"
        if self.rb_pick.IsChecked:
            return "pick"
        return "all"

    def _update_scope_ui(self):
        mode = self._scope_mode()
        picking = mode == "pick"
        for item in self._view_items:
            item.box.IsEnabled = picking
        self.view_filter.IsEnabled = picking
        if mode == "all":
            self.scope_hint.Text = (u"{0} views. Every model board not drawn anywhere is reported "
                                    u"as missing.".format(len(self._view_items)))
        elif mode == "active":
            self.scope_hint.Text = (u"Only the active view. Missing boards / feeders are reported "
                                    u"only below boards drawn in it.")
        else:
            self.scope_hint.Text = (u"Tick the views below. Missing boards / feeders are reported "
                                    u"only below boards drawn in them.")
        if not self._active_ok:
            self.rb_active.Content = u"Active view only (it has no Detail Items)"

    def on_scope_changed(self, sender, args):
        if self._ready:
            self._update_scope_ui()

    def on_view_filter(self, sender, args):
        if not self._ready:
            return
        query = (self.view_filter.Text or u"").strip().lower()
        self.view_filter_hint.Visibility = Visibility.Collapsed if query else Visibility.Visible
        for item in self._view_items:
            item.box.Visibility = (Visibility.Visible if not query or query in item.search
                                   else Visibility.Collapsed)

    def _tick_views(self, value):
        for item in self._view_items:
            if item.box.Visibility == Visibility.Visible:
                item.box.IsChecked = value
        if value and not self.rb_pick.IsChecked:
            self.rb_pick.IsChecked = True

    def on_views_all(self, sender, args):
        self._tick_views(True)

    def on_views_none(self, sender, args):
        self._tick_views(False)

    def on_checks_all(self, sender, args):
        for box in self._check_boxes.values():
            if box.IsEnabled:
                box.IsChecked = True

    def on_checks_none(self, sender, args):
        for box in self._check_boxes.values():
            if box.IsEnabled:
                box.IsChecked = False

    # ------------------------------------------------------------------
    # mapping helpers
    # ------------------------------------------------------------------
    def on_auto_detect(self, sender, args):
        changed = []
        for row in self._sld_rows:
            if row.found_count() != 0:
                continue                       # already resolves to a parameter with values
            best = config.auto_detect(row.field, self._sld_disc.with_values)
            if best:
                row.set_names([best] + [n for n in row.names if n != best])
                changed.append(u"{0} → {1}".format(config.FIELD_LABELS[row.field], best))
        if changed:
            self._status(u"Auto-detected: " + u";  ".join(changed))
        else:
            self._status(u"Nothing to auto-detect - every field already resolves, or no "
                         u"similar parameter name was found.")

    def on_reset_sld(self, sender, args):
        self._build_sld_rows(config.DEFAULTS["sld_fields"])
        self._status(u"SLD parameters reset to the defaults (not saved yet).")

    def on_reset_model(self, sender, args):
        self._build_model_rows(config.DEFAULTS)
        self._status(u"Model parameters reset to the defaults (not saved yet).")

    # ------------------------------------------------------------------
    # templates
    # ------------------------------------------------------------------
    def _current_or_error(self):
        try:
            return self._read_from_ui()
        except ValueError as error:
            self._status(rules.to_text(error), error=True)
            return None

    def on_template_selected(self, sender, args):
        if not self._ready:
            return
        name = self.template_combo.SelectedItem
        if not name or name == self._template:
            return
        try:
            cfg = config.load_template(name)
        except config.TemplateError as error:
            self._status(rules.to_text(error), error=True)
            return
        self._cfg, self._template = cfg, name
        self._load_into_ui(cfg)
        config.set_last_template(name)
        self._status(u"Template '{0}' loaded.".format(name))

    def _save_as(self, cfg, suggested=u""):
        name = forms.ask_for_string(default=suggested or u"", prompt=u"Template name:",
                                    title=u"Save SLD Checker template")
        if not name:
            return
        try:
            name = config.clean_name(name)
            if name in config.list_templates() and not forms.alert(
                    u"Template '{0}' exists. Overwrite it?".format(name), yes=True, no=True):
                return
            config.save_template(name, cfg)
        except Exception as error:
            self._status(rules.to_text(error), error=True)
            return
        self._cfg, self._template = cfg, name
        self._fill_template_combo(name)
        self._status(u"Saved template '{0}'.".format(name))

    def on_save(self, sender, args):
        cfg = self._current_or_error()
        if cfg is None:
            return
        if self._template == config.DEFAULT_TEMPLATE:
            self._save_as(cfg)
            return
        try:
            config.save_template(self._template, cfg)
            self._cfg = cfg
            self._status(u"Saved template '{0}'.".format(self._template))
        except Exception as error:
            self._status(rules.to_text(error), error=True)

    def on_save_as(self, sender, args):
        cfg = self._current_or_error()
        if cfg is not None:
            self._save_as(cfg, u"" if self._template == config.DEFAULT_TEMPLATE else self._template)

    def on_delete(self, sender, args):
        if self._template == config.DEFAULT_TEMPLATE:
            self._status(u"The built-in <Default> template cannot be deleted.", error=True)
            return
        if not forms.alert(u"Delete template '{0}'?".format(self._template), yes=True, no=True):
            return
        config.delete_template(self._template)
        deleted = self._template
        self._template = None
        self._fill_template_combo(config.DEFAULT_TEMPLATE)
        self.on_template_selected(None, None)
        self._status(u"Deleted template '{0}'.".format(deleted))

    def on_import(self, sender, args):
        path = forms.pick_file(file_ext="json", title=u"Import SLD Checker template")
        if not path:
            return
        try:
            cfg = config.import_template(path)
        except config.TemplateError as error:
            self._status(rules.to_text(error), error=True)
            return
        suggested = os.path.splitext(os.path.basename(path))[0]
        self._cfg = cfg
        self._load_into_ui(cfg)
        self._status(u"Imported '{0}' - give it a name to keep it as a template.".format(path))
        self._save_as(cfg, suggested)

    def on_export(self, sender, args):
        cfg = self._current_or_error()
        if cfg is None:
            return
        name = self._template if self._template != config.DEFAULT_TEMPLATE else u"SLD_Checker"
        path = forms.save_file(file_ext="json", default_name=name)
        if not path:
            return
        try:
            config.export_template(cfg, path)
            self._status(u"Exported to {0}".format(path))
        except Exception as error:
            self._status(rules.to_text(error), error=True)

    # ------------------------------------------------------------------
    # run / cancel
    # ------------------------------------------------------------------
    def on_run(self, sender, args):
        cfg = self._current_or_error()
        if cfg is None:
            return
        mode = cfg["scope_mode"]
        if mode == "all":
            view_ids = set(item.view_id for item in self._view_items)
        elif mode == "active":
            view_ids = set([self._active_view_id])
        else:
            view_ids = set(item.view_id for item in self._view_items if item.box.IsChecked)
            if not view_ids:
                self._status(u"Tick at least one view, or choose 'All views'.", error=True)
                self.tabs.SelectedIndex = 0
                return
        if not any(cfg["checks"].get(k, True) for k, _g, _l, locked in checks.CHECK_ITEMS
                   if not locked):
            self._status(u"All optional checks are off - switch at least one on.", error=True)
            return
        if self._template != config.DEFAULT_TEMPLATE:
            config.set_last_template(self._template)
        self.result = {"cfg": cfg, "template": self._template, "mode": mode,
                       "view_ids": view_ids, "view_count": len(view_ids)}
        self.Close()

    def on_cancel(self, sender, args):
        self.result = None
        self.Close()
