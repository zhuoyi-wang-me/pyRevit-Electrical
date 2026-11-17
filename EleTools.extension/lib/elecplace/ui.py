# -*- coding: utf-8 -*-
"""
elecplace.ui
============
Single-window interface (WPF, XAML in ui_main.xaml) built on
pyrevit.forms.WPFWindow. Everything happens in one place:

  Header      link picker + live count of eligible rooms
  1. Rules    rule list (left) + rule editor (right)
  2. Preview  per-room / per-equipment target vs planned + issues list
  3. Options  clearances, tolerances and behaviour switches
  Footer      Load/Save template, Preview, Place fixtures, Cancel

Design notes
  * Grids are bound to System.Data.DataTable views (rock-solid binding
    under IronPython); the editor is read/written explicitly, no two-way
    binding to Python objects.
  * The editor state is a plain dict ("values"). The same dict is stored in
    Rule.values, saved to JSON templates and restored on the next run, so
    one parser (_build_rule) validates editor input, templates and the
    auto-restored last session alike.
  * The window never modifies the model. On "Place fixtures" it stores the
    final plan in self.result and closes; script.py runs the transactions.
"""
import io
import json
import os

import clr
try:
    clr.AddReference("System.Data")
except Exception:
    pass
from System.Data import DataTable                      # noqa: E402
from System.Windows import Visibility                  # noqa: E402
from System.Windows.Media import Brushes               # noqa: E402

from pyrevit import forms                               # noqa: E402

from elecplace import linkdata, planner                 # noqa: E402
from elecplace.compat import (area_number_to_internal, area_unit_label,   # noqa: E402
                              area_uses_feet, format_area, format_length, length_unit_label,
                              format_length_u, mm_to_ft, parse_area, parse_length, sqm_to_ft2,
                              symbol_label, to_text)
from elecplace.mathutil import parse_bands              # noqa: E402
from elecplace import datamap                            # noqa: E402

XAML_PATH = os.path.join(os.path.dirname(__file__), "ui_main.xaml")
LAST_RULES_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                               "pyRevit", "ElecAutoPlace", "last_rules.json")
TEMPLATE_VERSION = 2

# (key, display text) for every combo box with fixed options
KIND_ITEMS = [(planner.KIND_WALLS, u"On room walls"),
              (planner.KIND_EQUIP, u"On wall next to equipment (plumbing, MSFD...)"),
              (planner.KIND_ON_EQUIP, u"On the equipment itself, with offset (FCU / BCU...)")]
HREF_ITEMS = [(planner.HREF_FFL, u"Finished floor level (FFL)"),
              (planner.HREF_EQUIP, u"Equipment elevation (+/- offset)")]
NEAR_AXES_ITEMS = [(planner.AXES_WALL, u"Along the wall (X = to the right, seen from the room)"),
                   (planner.AXES_EQUIP, u"Equipment axes (X = unit's right, Y = unit's front)"),
                   (planner.AXES_PROJECT, u"Project axes (X = east, Y = north)")]
NEAR_FROM_ITEMS = [(planner.FROM_CENTRE, u"Unit centre (insertion point)"),
                   (planner.FROM_EDGE, u"Unit edge - automatic from each unit's size")]
ZREF_ITEMS = [(planner.ZREF_CENTRE, u"Centre of the chosen side"),
              (planner.ZREF_FFL, u"Floor level (FFL) - Offset Z = height above floor, e.g. 1200")]
WALL_FACE_ITEMS = [(planner.FACE_ROOM, u"Room side - in front of the wall, same room as the unit"),
                   (planner.FACE_BACK, u"Back side - other face of the wall BEHIND the unit (duct / IPS)")]
from elecplace.equipgeom import SIDES as SIDE_ITEMS                  # noqa: E402
from elecplace.equipgeom import legacy_offsets_to_xyz                # noqa: E402
QTY_ITEMS = [(planner.QTY_FIXED, u"Fixed number per room"),
             (planner.QTY_SPACING, u"By room size - 1 per length of wall"),
             (planner.QTY_AREA, u"By room size - 1 per floor area"),
             (planner.QTY_BANDS, u"By room size - area bands")]
SCOPE_ITEMS = [(planner.SCOPE_ALL, u"All eligible rooms"),
               (planner.SCOPE_LEVELS, u"Rooms on levels (Pick...)"),
               (planner.SCOPE_NAME, u"Room name contains (comma separated)"),
               (planner.SCOPE_NUMBER, u"Room number starts with (comma separated)"),
               (planner.SCOPE_PICKED, u"Picked rooms (Pick...)")]
def build_source_items(links):
    """
    'Equipment from' options. Equipment and accessories may be modelled in
    this project on some jobs and in a linked MEP model on others, so every
    loaded link is offered by name as well as the combined options.
    """
    items = [(planner.SRC_ALL, u"Auto - this project + all loaded links"),
             (planner.SRC_HOST, u"This project only (equipment modelled here)"),
             (planner.SRC_LINKS, u"All linked models only")]
    seen = set()
    for lk in links:
        if lk.title not in seen:
            seen.add(lk.title)
            items.append((planner.SRC_LINK_PREFIX + lk.title, u"Linked model: " + lk.title))
    return items

# Smart default heights (mm) by keyword in the family/type name
HEIGHT_HINTS = [("switch", 1200.0), ("thermostat", 1500.0), ("isolator", 1200.0),
                ("spur", 1200.0), ("card", 1200.0), ("data", 450.0), ("tel", 450.0),
                ("socket", 450.0), ("receptacle", 450.0), ("outlet", 450.0)]


def _keys(items):
    return [k for k, _t in items]


def _texts(items):
    return [t for _k, t in items]


def _new_table(columns):
    table = DataTable()
    for col in columns:
        table.Columns.Add(col)
    return table


def _add_row(table, values):
    row = table.NewRow()
    for i, val in enumerate(values):
        row[i] = to_text(val)
    table.Rows.Add(row)


class MainWindow(forms.WPFWindow):
    """Main tool window. After ShowDialog(), read .result (None = cancelled)."""

    def __init__(self, doc, links, settings, symbols_by_cat, logger=None):
        forms.WPFWindow.__init__(self, XAML_PATH)
        self.doc = doc
        self.links = links
        self.settings = settings
        self.symbols_by_cat = symbols_by_cat
        self.logger = logger
        self.link = None
        self.rooms, self.skipped_rooms = [], []
        self.rules = []
        self.result = None
        self._busy = False
        self._picked_uids = []
        self._equip_types = []          # exact 'Family : Type' labels to serve ([] = all)
        self._type_lookup = {}          # combo text -> (label, symbol)
        self._boundary_mode = None
        self._can_save = False          # don't overwrite last session before restoring it

        self._init_static_controls()
        self._settings_to_ui()
        self._set_editor(self._default_values())
        self._busy = True
        self.cbLink.ItemsSource = [l.name for l in links]
        self.cbLink.SelectedIndex = 0
        self._busy = False
        self._load_link(0)
        self._restore_last_rules()
        self._can_save = True
        self._refresh_rules_grid()

    # ==================================================================
    # Initialisation
    # ==================================================================
    def _init_static_controls(self):
        self._busy = True
        self.cbKind.ItemsSource = _texts(KIND_ITEMS)
        self.cbQtyMethod.ItemsSource = _texts(QTY_ITEMS)
        self.cbScope.ItemsSource = _texts(SCOPE_ITEMS)
        self._src_items = build_source_items(self.links)
        self.cbEquipSource.ItemsSource = _texts(self._src_items)
        self.cbHeightRef.ItemsSource = _texts(HREF_ITEMS)
        self.cbSide.ItemsSource = _texts(SIDE_ITEMS)
        self.cbNearAxes.ItemsSource = _texts(NEAR_AXES_ITEMS)
        self.cbNearFrom.ItemsSource = _texts(NEAR_FROM_ITEMS)
        self.cbWallFace.ItemsSource = _texts(WALL_FACE_ITEMS)
        self.cbZRef.ItemsSource = _texts(ZREF_ITEMS)
        self.cbToken.ItemsSource = list(datamap.TOKENS)
        self._param_cache = {}
        # Grey out only categories that really don't exist in this Revit
        # version, and say so on the checkbox instead of disabling silently.
        for key, box in self._equipment_boxes():
            ok = planner.equipment_category_available(key)
            box.IsEnabled = ok
            if not ok:
                box.Content = u"{}  (not available in this Revit version)".format(box.Content)
                box.IsChecked = False
        self.cbCategory.ItemsSource = list(self.symbols_by_cat.keys())
        self._busy = False

    def _equipment_boxes(self):
        """(category key, checkbox) for every 'Serve categories' checkbox."""
        return (("mech", self.chkEqMech), ("plumbfix", self.chkEqPlumbFix),
                ("plumbeq", self.chkEqPlumbEq), ("ductacc", self.chkEqDuctAcc),
                ("pipeacc", self.chkEqPipeAcc), ("vcirc", self.chkEqVertCirc),
                ("specialty", self.chkEqSpecialty))

    def _status(self, text):
        self.txtStatus.Text = to_text(text)

    def _load_link(self, index):
        """Read rooms of the selected link (re-run when boundary option changes)."""
        if index < 0 or index >= len(self.links):
            return
        self.link = self.links[index]
        self._boundary_mode = self.settings.use_finish_boundary
        self.rooms, self.skipped_rooms = linkdata.collect_rooms(self.link, self.settings, self.logger)
        self.txtRoomsInfo.Text = u"{} eligible rooms  |  {} skipped".format(
            len(self.rooms), len(self.skipped_rooms))
        self._update_room_match()
        self._refresh_rules_grid()
        self._status(u"Loaded {} rooms from {}".format(len(self.rooms), self.link.title))

    # ==================================================================
    # Options tab <-> Settings
    # ==================================================================
    def _settings_to_ui(self):
        s = self.settings
        self.txtCornerClr.Text = u"{:g}".format(s.corner_clearance_mm)
        self.txtOpeningClr.Text = u"{:g}".format(s.opening_clearance_mm)
        self.txtMinSeg.Text = u"{:g}".format(s.min_segment_length_mm)
        self.txtDupTol.Text = u"{:g}".format(s.duplicate_tolerance_mm)
        self.txtEquipSpacing.Text = u"{:g}".format(s.equipment_spacing_mm)
        self.txtMaxHeight.Text = u"{:g}".format(s.max_height_mm)
        self.chkAvoidOpenings.IsChecked = s.avoid_openings
        self.chkFinishBoundary.IsChecked = s.use_finish_boundary
        self.chkBaseOffset.IsChecked = s.include_room_base_offset
        self.chkAllowLevelBased.IsChecked = s.allow_unhosted_fallback
        self.chkScheduleLevel.IsChecked = s.assign_schedule_level
        self.chkCsv.IsChecked = s.export_csv
        self.txtCommentTag.Text = to_text(s.comment_tag)
        self.chkWriteComments.IsChecked = bool(s.write_comments)
        self.chkAvoidClashes.IsChecked = s.avoid_clashes
        self.chkClashCurves.IsChecked = s.clash_include_mep_curves
        self.txtClashClearance.Text = u"{:g}".format(s.clash_clearance_mm)
        self.txtClashSlide.Text = u"{:g}".format(s.clash_max_slide_mm)
        self.txtClashStep.Text = u"{:g}".format(s.clash_step_mm)

    def _ui_to_settings(self):
        """Copy the Options tab into settings. False (with alert) if invalid."""
        s = self.settings
        fields = [("corner_clearance_mm", self.txtCornerClr, u"Corner clearance"),
                  ("opening_clearance_mm", self.txtOpeningClr, u"Door / window clearance"),
                  ("min_segment_length_mm", self.txtMinSeg, u"Minimum segment length"),
                  ("duplicate_tolerance_mm", self.txtDupTol, u"Duplicate radius"),
                  ("equipment_spacing_mm", self.txtEquipSpacing, u"Equipment spacing"),
                  ("max_height_mm", self.txtMaxHeight, u"Maximum height"),
                  ("clash_clearance_mm", self.txtClashClearance, u"Clash clear gap"),
                  ("clash_max_slide_mm", self.txtClashSlide, u"Max. slide"),
                  ("clash_step_mm", self.txtClashStep, u"Slide step")]
        for key, box, label in fields:
            try:
                val = float(box.Text.strip().replace(u",", u"."))
                if val < 0:
                    raise ValueError()
                setattr(s, key, val)
            except Exception:
                forms.alert(u"Options: '{}' must be a positive number of millimetres.".format(label))
                self.tabs.SelectedIndex = 2
                return False
        s.avoid_openings = bool(self.chkAvoidOpenings.IsChecked)
        s.use_finish_boundary = bool(self.chkFinishBoundary.IsChecked)
        s.include_room_base_offset = bool(self.chkBaseOffset.IsChecked)
        s.allow_unhosted_fallback = bool(self.chkAllowLevelBased.IsChecked)
        s.assign_schedule_level = bool(self.chkScheduleLevel.IsChecked)
        s.export_csv = bool(self.chkCsv.IsChecked)
        s.avoid_clashes = bool(self.chkAvoidClashes.IsChecked)
        s.clash_include_mep_curves = bool(self.chkClashCurves.IsChecked)
        s.comment_tag = self.txtCommentTag.Text.strip() or u"ElecAutoPlace"
        s.write_comments = bool(self.chkWriteComments.IsChecked)
        if self._boundary_mode != s.use_finish_boundary:
            self._load_link(self.cbLink.SelectedIndex)      # boundary type changed
        return True

    # ==================================================================
    # Editor <-> values dict
    # ==================================================================
    def _default_values(self):
        cats = list(self.symbols_by_cat.keys())
        cat = u"Electrical Fixtures" if u"Electrical Fixtures" in cats else (cats[0] if cats else u"")
        syms = self.symbols_by_cat.get(cat, [])
        sym = syms[0] if syms else None
        return {
            "enabled": True, "kind": planner.KIND_WALLS, "category": cat,
            "type_label": symbol_label(sym) if sym else u"",
            "height": format_length(self.doc, self._default_height_ft(sym)),
            "wall_offset": u"0",
            "qty_method": planner.QTY_FIXED, "qty_value": u"2",
            "min_qty": u"0", "max_qty": u"0",
            "room_scope": planner.SCOPE_ALL, "room_filter": u"", "picked_uids": [],
            "equip_cats": ["mech", "plumbfix"], "equip_source": planner.SRC_ALL,
            "equip_keywords": u"", "equip_types": [], "height_ref": planner.HREF_FFL,
            "near_axes": planner.AXES_WALL, "near_x": u"0", "near_y": u"0",
            "near_from": planner.FROM_CENTRE, "wall_face": planner.FACE_ROOM,
            "mappings": u"",
            "side": "front", "off_x": u"0", "off_y": u"0", "off_z": u"0",
            "z_ref": planner.ZREF_CENTRE,
            "max_dist": format_length(self.doc, self.settings.equipment_max_distance_ft),
            "per_equipment": u"1",
        }

    def _default_height_ft(self, symbol):
        if symbol is None:
            return self.settings.default_height_ft
        text = symbol_label(symbol).lower()
        for key, mm in HEIGHT_HINTS:
            if key in text:
                return mm_to_ft(mm)
        return self.settings.default_height_ft

    def _default_qty_value(self, method):
        if method == planner.QTY_SPACING:
            return format_length(self.doc, mm_to_ft(3600))
        if method == planner.QTY_AREA:
            return format_area(self.doc, sqm_to_ft2(10))
        if method == planner.QTY_BANDS:
            return u"100:2, 250:4, 500:6, *:8" if area_uses_feet(self.doc) else u"10:2, 25:4, 50:6, *:8"
        return u"2"

    @staticmethod
    def _sel_key(combo, items, default):
        idx = combo.SelectedIndex
        return items[idx][0] if 0 <= idx < len(items) else default

    @staticmethod
    def _select_key(combo, items, key):
        keys = _keys(items)
        combo.SelectedIndex = keys.index(key) if key in keys else 0

    def _current_type(self):
        text = self.cbType.SelectedItem
        return self._type_lookup.get(to_text(text)) if text is not None else None

    def _editor_values(self):
        cur = self._current_type()
        cats = [key for key, box in self._equipment_boxes() if box.IsChecked and box.IsEnabled]
        return {
            "enabled": bool(self.chkEnabled.IsChecked),
            "kind": self._sel_key(self.cbKind, KIND_ITEMS, planner.KIND_WALLS),
            "category": to_text(self.cbCategory.SelectedItem or u""),
            "type_label": cur[0] if cur else u"",
            "height": self.txtHeight.Text,
            "wall_offset": self.txtWallOffset.Text,
            "qty_method": self._sel_key(self.cbQtyMethod, QTY_ITEMS, planner.QTY_FIXED),
            "qty_value": self.txtQtyValue.Text,
            "min_qty": self.txtMinQty.Text, "max_qty": self.txtMaxQty.Text,
            "room_scope": self._sel_key(self.cbScope, SCOPE_ITEMS, planner.SCOPE_ALL),
            "room_filter": self.txtRoomFilter.Text,
            "picked_uids": list(self._picked_uids),
            "equip_cats": cats,
            "equip_source": self._sel_key(self.cbEquipSource, self._src_items, planner.SRC_ALL),
            "max_dist": self.txtMaxDist.Text,
            "per_equipment": self.txtPerEquip.Text,
            "equip_keywords": self.txtEquipKeywords.Text,
            "equip_types": list(self._equip_types),
            "height_ref": self._sel_key(self.cbHeightRef, HREF_ITEMS, planner.HREF_FFL),
            "side": self._sel_key(self.cbSide, SIDE_ITEMS, "front"),
            "off_x": self.txtOffX.Text, "off_y": self.txtOffY.Text, "off_z": self.txtOffZ.Text,
            "z_ref": self._sel_key(self.cbZRef, ZREF_ITEMS, planner.ZREF_CENTRE),
            "near_axes": self._sel_key(self.cbNearAxes, NEAR_AXES_ITEMS, planner.AXES_WALL),
            "near_x": self.txtNearX.Text, "near_y": self.txtNearY.Text,
            "near_from": self._sel_key(self.cbNearFrom, NEAR_FROM_ITEMS, planner.FROM_CENTRE),
            "wall_face": self._sel_key(self.cbWallFace, WALL_FACE_ITEMS, planner.FACE_ROOM),
            "mappings": self.txtMappings.Text,
        }

    def _set_editor(self, v):
        self._busy = True
        try:
            self.chkEnabled.IsChecked = bool(v.get("enabled", True))
            self._select_key(self.cbKind, KIND_ITEMS, v.get("kind"))
            cats = list(self.symbols_by_cat.keys())
            cat = v.get("category")
            self.cbCategory.SelectedIndex = cats.index(cat) if cat in cats else (0 if cats else -1)
            self._fill_types(v.get("type_label"))
            self.txtHeight.Text = to_text(v.get("height", u""))
            self.txtWallOffset.Text = to_text(v.get("wall_offset", u"0"))
            self._select_key(self.cbQtyMethod, QTY_ITEMS, v.get("qty_method"))
            self.txtQtyValue.Text = to_text(v.get("qty_value", u""))
            self.txtMinQty.Text = to_text(v.get("min_qty", u"0"))
            self.txtMaxQty.Text = to_text(v.get("max_qty", u"0"))
            self._select_key(self.cbScope, SCOPE_ITEMS, v.get("room_scope"))
            self.txtRoomFilter.Text = to_text(v.get("room_filter", u""))
            self._picked_uids = list(v.get("picked_uids", []) or [])
            eq = v.get("equip_cats", []) or []
            for key, box in self._equipment_boxes():
                box.IsChecked = key in eq and box.IsEnabled
            self.txtEquipKeywords.Text = to_text(v.get("equip_keywords", u""))
            self._equip_types = [to_text(t) for t in (v.get("equip_types", []) or []) if to_text(t)]
            self._update_equip_types_text()
            self._select_key(self.cbHeightRef, HREF_ITEMS, v.get("height_ref"))
            self._select_key(self.cbSide, SIDE_ITEMS, v.get("side", "front"))
            v = self._upgrade_values(v)
            self.txtOffX.Text = to_text(v.get("off_x", u"0"))
            self.txtOffY.Text = to_text(v.get("off_y", u"0"))
            self.txtOffZ.Text = to_text(v.get("off_z", u"0"))
            self._select_key(self.cbZRef, ZREF_ITEMS, v.get("z_ref", planner.ZREF_CENTRE))
            self._select_key(self.cbNearAxes, NEAR_AXES_ITEMS, v.get("near_axes", planner.AXES_WALL))
            self._select_key(self.cbNearFrom, NEAR_FROM_ITEMS, v.get("near_from", planner.FROM_CENTRE))
            self._select_key(self.cbWallFace, WALL_FACE_ITEMS, v.get("wall_face", planner.FACE_ROOM))
            self.txtNearX.Text = to_text(v.get("near_x", u"0"))
            self.txtMappings.Text = to_text(v.get("mappings", u""))
            self.txtNearY.Text = to_text(v.get("near_y", u"0"))
            self._select_key(self.cbEquipSource, self._src_items, v.get("equip_source"))
            self.txtMaxDist.Text = to_text(v.get("max_dist", u""))
            self.txtPerEquip.Text = to_text(v.get("per_equipment", u"1"))
        finally:
            self._busy = False
        self._update_kind_panels()
        self._update_side_labels()
        self._update_qty_hint()
        self._update_host_info()
        self._update_room_match()

    def _fill_types(self, select_label=None):
        """Populate the type combo for the current category (tagged by hosting)."""
        cat = to_text(self.cbCategory.SelectedItem or u"")
        self._type_lookup = {}
        texts = []
        for sym in self.symbols_by_cat.get(cat, []):
            label = symbol_label(sym)
            text = u"{}  {}".format(label, planner.hosting_tag(sym, self.settings))
            self._type_lookup[text] = (label, sym)
            texts.append(text)
        self.cbType.ItemsSource = texts
        idx = 0
        if select_label:
            for i, t in enumerate(texts):
                if self._type_lookup[t][0] == select_label:
                    idx = i
                    break
        self.cbType.SelectedIndex = idx if texts else -1

    # ==================================================================
    # Editor helpers (visibility, hints)
    # ==================================================================
    def _update_kind_panels(self):
        kind = self._sel_key(self.cbKind, KIND_ITEMS, planner.KIND_WALLS)
        show = lambda ok: Visibility.Visible if ok else Visibility.Collapsed    # noqa: E731
        near, on = kind == planner.KIND_EQUIP, kind == planner.KIND_ON_EQUIP
        self.grpQuantity.Visibility = show(kind == planner.KIND_WALLS)
        self.grpEquipment.Visibility = show(near or on)
        self.grpOnEquip.Visibility = show(on)
        self.pnlHeight.Visibility = show(not on)          # on-equipment uses offsets
        self.pnlWallOffset.Visibility = show(not on)
        self.pnlHeightRef.Visibility = show(near)
        self.pnlMaxDist.Visibility = show(near)
        self.pnlWallFace.Visibility = show(near)
        self.pnlNearOffsets.Visibility = show(near)
        self._update_near_axes()
        self.txtEquipHint.Text = (
            u"Each item gets its fixtures on the nearest usable wall of its room, e.g. a fused "
            u"spur beside a water heater or a control box on the wall beside an MSFD. Items "
            u"inside a wall (fire dampers) or in a ceiling void are matched to the room below."
            if near else
            u"Fixtures per equipment are spaced along the offset-1 direction. With 'All eligible "
            u"rooms', equipment outside any room (plant, voids) is included too.")
        self._update_height_labels()

    def _update_height_labels(self):
        kind = self._sel_key(self.cbKind, KIND_ITEMS, planner.KIND_WALLS)
        equip_ref = (kind == planner.KIND_EQUIP and
                     self._sel_key(self.cbHeightRef, HREF_ITEMS, planner.HREF_FFL) == planner.HREF_EQUIP)
        self.lblHeight.Text = u"Offset from equipment Z" if equip_ref else u"Height above FFL"
        unit = length_unit_label(self.doc)
        self.txtHeightHint.Text = (
            u"Project unit: {}. Relative to the equipment insertion point; negative = below "
            u"(e.g. 0 = level with an MSFD).".format(unit) if equip_ref else
            u"Project unit: {}. You may also type 450mm, 1.2m or 1'-6\". Use 0 for "
            u"floor-standing equipment.".format(unit))

    def _update_side_labels(self):
        """Explain which offset moves across the face and which is a stand-off."""
        side = self._sel_key(self.cbSide, SIDE_ITEMS, "front")
        out = {"front": u"+Y", "back": u"-Y", "right": u"+X", "left": u"-X",
               "top": u"+Z", "bottom": u"-Z"}[side]
        across = {"front": u"X and Z", "back": u"X and Z", "right": u"Y and Z",
                  "left": u"Y and Z", "top": u"X and Y", "bottom": u"X and Y"}[side]
        self.txtSideHint.Text = (u"On the {} side, {} move the fixture across the face; the {} "
                                 u"component is a stand-off (level-based types only)."
                                 .format(side, across, out[1]))

    def _update_near_axes(self):
        axes = self._sel_key(self.cbNearAxes, NEAR_AXES_ITEMS, planner.AXES_WALL)
        edge = self._sel_key(self.cbNearFrom, NEAR_FROM_ITEMS, planner.FROM_CENTRE) == planner.FROM_EDGE
        wall = axes == planner.AXES_WALL
        self.txtNearY.IsEnabled = not wall
        self.lblNearY.Opacity = 0.4 if wall else 1.0
        self.txtNearHint.Text = {
            planner.AXES_WALL: u"X slides the fixture along the wall from the point nearest to the "
                               u"unit (negative = to the left). Z = the height field above.",
            planner.AXES_EQUIP: u"The unit position is shifted by X (its right) and Y (its front), "
                                u"then snapped to the nearest wall - follows rotated units. Z = height field.",
            planner.AXES_PROJECT: u"The unit position is shifted by X (east) and Y (north) of this "
                                  u"project, then snapped to the nearest wall. Z = height field.",
        }[axes]
        if edge:
            self.txtNearHint.Text += (
                u"  EDGE MODE: each unit's size is read from its geometry, so X = 100 means 100 "
                u"clear of the unit's end (e.g. MSFD 826 wide -> 513 from its centre); + = right "
                u"end, - = left end. If that end is blocked by a corner or door, the other end is "
                u"used. With height from equipment, Z is measured from its top (+) / bottom (-).")

    @staticmethod
    def _upgrade_values(v):
        """v3 rules stored viewer-relative off_1 / off_2 - convert to X / Y / Z."""
        if "off_x" in v or ("off_1" not in v and "off_2" not in v):
            return v
        v = dict(v)
        try:
            x, y, z = legacy_offsets_to_xyz(v.get("side", "front"),
                                            float(v.get("off_1") or 0), float(v.get("off_2") or 0))
            v["off_x"], v["off_y"], v["off_z"] = [u"{:g}".format(n) for n in (x, y, z)]
        except Exception:
            v["off_x"], v["off_y"], v["off_z"] = v.get("off_1", u"0"), u"0", v.get("off_2", u"0")
        return v

    def _update_qty_hint(self):
        method = self._sel_key(self.cbQtyMethod, QTY_ITEMS, planner.QTY_FIXED)
        a_unit, l_unit = area_unit_label(self.doc), length_unit_label(self.doc)
        labels = {
            planner.QTY_FIXED: (u"Fixtures per room", u"The same number in every target room."),
            planner.QTY_SPACING: (u"1 fixture per", u"Wall length ({}). Example: 3600 mm on a room "
                                  u"with 14 m of wall gives 4. Bigger rooms get more.".format(l_unit)),
            planner.QTY_AREA: (u"1 fixture per", u"Floor area ({}). Example: 10 m2 on a 45 m2 room "
                               u"gives 5.".format(a_unit)),
            planner.QTY_BANDS: (u"Area bands", u"area:count pairs in {}, ascending; * = anything "
                                u"larger. Example 10:2, 25:4, 50:6, *:8 -> a 30 m2 room gets 6."
                                .format(a_unit)),
        }
        lbl, hint = labels[method]
        self.lblQtyValue.Text = lbl
        self.txtQtyHint.Text = hint

    def _refresh_param_list(self):
        """Target parameter suggestions for the selected fixture type."""
        cur = self._current_type()
        if not cur:
            self.cbParam.ItemsSource = [u"Comments", u"Mark"]
            return
        key = cur[0]
        if key not in self._param_cache:
            self._param_cache[key] = datamap.fixture_param_names(self.doc, cur[1])
        self.cbParam.ItemsSource = self._param_cache[key]

    def _update_host_info(self):
        self._refresh_param_list()
        cur = self._current_type()
        if not cur:
            self.txtHostInfo.Text = u"No family types loaded in this category."
            self.txtHostInfo.Foreground = Brushes.Firebrick
            return
        mode, reason = planner.classify_symbol(cur[1], self.settings)
        if mode == planner.MODE_FACE:
            self.txtHostInfo.Text = u"Face-based: hosted on the linked wall face, facing into the room."
            self.txtHostInfo.Foreground = Brushes.SeaGreen
        elif mode == planner.MODE_UNHOSTED:
            self.txtHostInfo.Text = u"Level-based: placed at the wall on the host level and rotated to face the room."
            self.txtHostInfo.Foreground = Brushes.SteelBlue
        else:
            self.txtHostInfo.Text = reason
            self.txtHostInfo.Foreground = Brushes.Firebrick

    def _update_room_match(self):
        if self._busy or not hasattr(self, "rooms"):
            return
        scope = self._sel_key(self.cbScope, SCOPE_ITEMS, planner.SCOPE_ALL)
        uids = planner.resolve_room_uids(scope, self.txtRoomFilter.Text, self._picked_uids, self.rooms)
        self.txtRoomMatch.Text = u"Matches {} of {} rooms".format(len(uids), len(self.rooms))
        self.txtRoomMatch.Foreground = Brushes.SeaGreen if uids else Brushes.Firebrick

    # ==================================================================
    # Rule parsing / validation (editor, templates and restore)
    # ==================================================================
    def _build_rule(self, v):
        """values dict -> planner.Rule. Raises ValueError with a readable message."""
        cat = v.get("category")
        sym = None
        for s in self.symbols_by_cat.get(cat, []):
            if symbol_label(s) == v.get("type_label"):
                sym = s
                break
        if sym is None:
            raise ValueError(u"Family type '{}' ({}) is not loaded in this project."
                             .format(v.get("type_label"), cat))
        v = self._upgrade_values(v)
        rule = planner.Rule(sym, v)
        rule.refresh_mode(self.settings)

        def as_len(key, name):
            val = parse_length(self.doc, v.get(key) or u"0")
            if val is None:
                raise ValueError(u"{} '{}' is not a valid length.".format(name, v.get(key)))
            return val

        on_equip = rule.kind == planner.KIND_ON_EQUIP
        rel = rule.kind == planner.KIND_EQUIP and rule.height_ref == planner.HREF_EQUIP
        height = 0.0 if on_equip else parse_length(self.doc, v.get("height"))
        if height is None or (height < 0 and not rel):
            raise ValueError(u"Height '{}' is not a valid length.".format(v.get("height")))
        if abs(height) > self.settings.max_height_ft:
            raise ValueError(u"Height {} exceeds the maximum ({}). Check units.".format(
                format_length(self.doc, height), format_length(self.doc, self.settings.max_height_ft)))
        rule.height_ft = height
        rule.height_text = (u"on equipment" if on_equip else
                            (u"equip. Z {}{}".format(u"+" if height >= 0 else u"",
                                                     format_length(self.doc, height)) if rel
                             else format_length(self.doc, height)))

        offset = parse_length(self.doc, v.get("wall_offset") or u"0")
        if offset is None or offset < 0:
            raise ValueError(u"Offset from wall must be zero or a positive length.")
        rule.wall_offset_ft = offset

        def as_int(key, name, lo, hi):
            try:
                n = int(to_text(v.get(key, u"0")).strip() or 0)
            except Exception:
                raise ValueError(u"{} must be a whole number.".format(name))
            if n < lo or n > hi:
                raise ValueError(u"{} must be between {} and {}.".format(name, lo, hi))
            return n

        if rule.kind in (planner.KIND_EQUIP, planner.KIND_ON_EQUIP):
            if not rule.equip_cats:
                raise ValueError(u"Tick at least one equipment category to serve.")
            rule.per_equipment = as_int("per_equipment", u"Fixtures per equipment", 1, 10)
            valid_sources = _keys(self._src_items) + [planner.SRC_ARCH]
            if rule.equip_source not in valid_sources:
                raise ValueError(u"Equipment source '{}' is not loaded in this project. Load that "
                                 u"link or choose another 'Equipment from' option."
                                 .format(planner.source_description(rule.equip_source)))
            names = [d for k, d, _n in planner.EQUIPMENT_CATEGORIES if k in rule.equip_cats]
            what = u", ".join(names)
            if rule.equip_types:
                what += u" of {} picked type(s)".format(len(rule.equip_types))
            elif rule.equip_keywords:
                what += u" named '{}'".format(u", ".join(rule.equip_keywords))
            if on_equip:
                rule.off_x_ft = as_len("off_x", u"Offset X")
                rule.off_y_ft = as_len("off_y", u"Offset Y")
                rule.off_z_ft = as_len("off_z", u"Offset Z")
                rule.qty_desc = u"{} on {} of each {} (X {} / Y {} / Z {}{}) - from {}".format(
                    rule.per_equipment, rule.side, what,
                    format_length_u(self.doc, rule.off_x_ft), format_length_u(self.doc, rule.off_y_ft),
                    format_length_u(self.doc, rule.off_z_ft),
                    u" above floor" if rule.z_ref == planner.ZREF_FFL else u"",
                    planner.source_description(rule.equip_source))
            else:
                dist = parse_length(self.doc, v.get("max_dist"))
                if dist is None or dist <= 0:
                    raise ValueError(u"Max. distance to wall must be a positive length.")
                rule.max_dist_ft = dist
                rule.near_x_ft = as_len("near_x", u"Offset X")
                rule.near_y_ft = (0.0 if rule.near_axes == planner.AXES_WALL
                                  else as_len("near_y", u"Offset Y"))
                offs = u""
                if rule.near_x_ft or rule.near_y_ft:
                    offs = u", offset X {}{} ({})".format(
                        format_length_u(self.doc, rule.near_x_ft),
                        u"" if rule.near_axes == planner.AXES_WALL
                        else u" / Y " + format_length_u(self.doc, rule.near_y_ft),
                        {planner.AXES_WALL: u"along wall", planner.AXES_EQUIP: u"unit axes",
                         planner.AXES_PROJECT: u"project axes"}[rule.near_axes])
                if rule.near_from == planner.FROM_EDGE and (rule.near_x_ft or rule.near_y_ft):
                    offs += u" from unit edge"
                if rule.wall_face == planner.FACE_BACK:
                    offs += u", BACK face of the wall behind the unit"
                rule.qty_desc = u"{} per {} (wall within {}{}) - from {}".format(
                    rule.per_equipment, what, format_length_u(self.doc, dist), offs,
                    planner.source_description(rule.equip_source))
        else:
            method, raw = rule.qty_method, to_text(v.get("qty_value", u"")).strip()
            if method == planner.QTY_FIXED:
                rule.qty_count = as_int("qty_value", u"Fixtures per room", 1, 200)
                desc = u"{} per room".format(rule.qty_count)
            elif method == planner.QTY_SPACING:
                val = parse_length(self.doc, raw)
                if val is None or val <= 0:
                    raise ValueError(u"Wall length per fixture must be a positive length.")
                rule.qty_value = val
                desc = u"1 per {} of wall".format(format_length(self.doc, val))
            elif method == planner.QTY_AREA:
                val = parse_area(self.doc, raw)
                if val is None or val <= 0:
                    raise ValueError(u"Floor area per fixture must be a positive area.")
                rule.qty_value = val
                desc = u"1 per {} floor area".format(format_area(self.doc, val))
            else:
                try:
                    bands = parse_bands(raw)
                except ValueError as ex:
                    raise ValueError(u"Area bands: {}".format(ex))
                rule.bands = [(None if lim is None else area_number_to_internal(self.doc, lim), cnt)
                              for lim, cnt in bands]
                desc = u"area bands {}".format(raw)
            rule.min_qty = as_int("min_qty", u"Min per room", 0, 200)
            rule.max_qty = as_int("max_qty", u"Max per room", 0, 200)
            if rule.max_qty and rule.min_qty > rule.max_qty:
                raise ValueError(u"Min per room cannot be larger than max.")
            if rule.min_qty or rule.max_qty:
                desc += u" (min {}, max {})".format(rule.min_qty, rule.max_qty or u"-")
            rule.qty_desc = desc

        try:
            rule.mappings = datamap.parse_mappings(v.get("mappings", u""))
        except ValueError as ex:
            raise ValueError(to_text(ex))
        equip_tokens = [t for _p, tpl in rule.mappings for t in datamap.TOKEN_RE.findall(tpl)
                        if t.strip().lower().startswith(u"equip")]
        if equip_tokens and rule.kind == planner.KIND_WALLS:
            raise ValueError(u"{{{}}} needs an equipment rule - room-wall rules have no served "
                             u"equipment. Use room tokens or fixed text.".format(equip_tokens[0]))
        if rule.room_scope == planner.SCOPE_PICKED and not rule.picked_uids:
            raise ValueError(u"Pick at least one room (Target rooms > Pick...).")
        if rule.room_scope in (planner.SCOPE_NAME, planner.SCOPE_NUMBER, planner.SCOPE_LEVELS) \
                and not rule.room_filter.strip():
            raise ValueError(u"Enter a room filter or choose 'All eligible rooms'.")
        return rule

    def _rooms_desc(self, rule):
        uids = planner.resolve_room_uids(rule.room_scope, rule.room_filter, rule.picked_uids, self.rooms)
        rule.room_uids = uids
        names = {planner.SCOPE_ALL: u"All", planner.SCOPE_LEVELS: u"Levels: " + rule.room_filter,
                 planner.SCOPE_NAME: u"Name: " + rule.room_filter,
                 planner.SCOPE_NUMBER: u"Number: " + rule.room_filter,
                 planner.SCOPE_PICKED: u"Picked"}
        return u"{} ({})".format(names.get(rule.room_scope, u"?"), len(uids))

    # ==================================================================
    # Rules grid
    # ==================================================================
    def _refresh_rules_grid(self, select=None):
        if not hasattr(self, "rules"):
            return
        table = _new_table([u"No", u"On", u"Placement", u"Category", u"Family type",
                            u"Height AFF", u"Quantity", u"Rooms", u"Hosting", u"Data"])
        for i, r in enumerate(self.rules, 1):
            r.refresh_mode(self.settings)
            _add_row(table, [i, u"Yes" if r.enabled else u"No",
                             {planner.KIND_WALLS: u"Room walls",
                              planner.KIND_EQUIP: u"Wall near equip.",
                              planner.KIND_ON_EQUIP: u"On equipment"}.get(r.kind, r.kind),
                             r.category_name, r.label, r.height_text, r.qty_desc,
                             self._rooms_desc(r),
                             planner.hosting_tag(r.symbol, self.settings),
                             datamap.describe(r.mappings)])
        self._busy = True
        self.rulesGrid.ItemsSource = table.DefaultView
        if select is not None and 0 <= select < len(self.rules):
            self.rulesGrid.SelectedIndex = select
        self._busy = False
        self._save_last_rules()

    def _selected_index(self):
        idx = self.rulesGrid.SelectedIndex
        return idx if 0 <= idx < len(self.rules) else -1

    def _try_build_from_editor(self):
        try:
            rule = self._build_rule(self._editor_values())
        except ValueError as ex:
            forms.alert(to_text(ex), title=u"Rule not valid")
            return None
        # Units slip guard: equipment is rarely within 100 mm of a wall face
        # (its insertion point is usually at the centre of the unit).
        if rule.kind == planner.KIND_EQUIP and rule.max_dist_ft < mm_to_ft(100):
            ok = forms.alert(
                u"Max. distance to wall is only {}.".format(format_length_u(self.doc, rule.max_dist_ft)),
                sub_msg=u"Equipment insertion points are usually 100-500 mm from the wall face, so "
                        u"almost every item would be skipped. Typical value: 1500 mm.\n\n"
                        u"Keep this value anyway?", yes=True, no=True)
            if not ok:
                self.txtMaxDist.Focus()
                return None
        return rule

    # ==================================================================
    # Event handlers - header / editor
    # ==================================================================
    def on_link_changed(self, sender, args):
        if not self._busy:
            self._load_link(self.cbLink.SelectedIndex)

    def on_kind_changed(self, sender, args):
        if self._busy:
            return
        self._update_kind_panels()
        kind = self._sel_key(self.cbKind, KIND_ITEMS, planner.KIND_WALLS)
        if kind != planner.KIND_WALLS and not (self.chkEqMech.IsChecked or self.chkEqPlumbFix.IsChecked
                                               or self.chkEqPlumbEq.IsChecked or self.chkEqDuctAcc.IsChecked
                                               or self.chkEqPipeAcc.IsChecked):
            self.chkEqMech.IsChecked = True

    def on_height_ref_changed(self, sender, args):
        if self._busy:
            return
        self._update_height_labels()
        if self._sel_key(self.cbHeightRef, HREF_ITEMS, planner.HREF_FFL) == planner.HREF_EQUIP:
            self.txtHeight.Text = u"0"

    def on_add_data_line(self, sender, args):
        param = to_text(self.cbParam.Text).strip()
        value = to_text(self.cbToken.Text).strip()
        if not param:
            forms.alert(u"Choose or type the fixture parameter to fill.")
            return
        if u"=" in param:
            forms.alert(u"A parameter name cannot contain '='.")
            return
        text = to_text(self.txtMappings.Text).rstrip()
        self.txtMappings.Text = (text + u"\n" if text else u"") + u"{} = {}".format(param, value)
        self.cbToken.Text = u""

    def on_near_axes_changed(self, sender, args):
        if not self._busy:
            self._update_near_axes()

    def on_side_changed(self, sender, args):
        if not self._busy:
            self._update_side_labels()

    def on_category_changed(self, sender, args):
        if self._busy:
            return
        self._busy = True
        self._fill_types()
        self._busy = False
        self.on_type_changed(None, None)

    def on_type_changed(self, sender, args):
        if self._busy:
            return
        cur = self._current_type()
        if cur:
            self.txtHeight.Text = format_length(self.doc, self._default_height_ft(cur[1]))
        self._update_host_info()

    def on_qty_method_changed(self, sender, args):
        if self._busy:
            return
        method = self._sel_key(self.cbQtyMethod, QTY_ITEMS, planner.QTY_FIXED)
        self.txtQtyValue.Text = self._default_qty_value(method)
        self._update_qty_hint()

    def on_scope_changed(self, sender, args):
        self._update_room_match()

    def on_room_filter_changed(self, sender, args):
        self._update_room_match()

    # ---- equipment types to serve --------------------------------------
    def _update_equip_types_text(self):
        n = len(self._equip_types)
        if not n:
            self.txtEquipTypes.Text = u"All types (name filter applies)"
            self.txtEquipTypes.ToolTip = None
        else:
            self.txtEquipTypes.Text = u"{} type(s): {}".format(n, u", ".join(self._equip_types))
            self.txtEquipTypes.ToolTip = u"\n".join(self._equip_types)

    def on_pick_equip_types(self, sender, args):
        """List every type of the ticked categories in 'Equipment from', let the user tick some."""
        cats = [key for key, box in self._equipment_boxes() if box.IsChecked and box.IsEnabled]
        if not cats:
            forms.alert(u"Tick at least one 'Serve categories' box first (e.g. Plumbing Fixtures).")
            return
        source = self._sel_key(self.cbEquipSource, self._src_items, planner.SRC_ALL)
        self._status(u"Reading equipment types from {}...".format(planner.source_description(source)))
        try:
            rows = planner.list_equipment_types(self.doc, self.link, self.links, cats, source)
        except Exception as ex:
            forms.alert(u"Could not read the equipment: {}".format(to_text(ex)))
            return
        if not rows:
            self._status(u"No equipment found")
            forms.alert(u"No items of the ticked categories were found in {}.".format(
                planner.source_description(source)),
                sub_msg=u"Check 'Equipment from' - plumbing fixtures are often in the ARC or MEP link.")
            return
        lookup, options = {}, []
        for label, cat, n in rows:
            text = u"{}   ({} x {})".format(label, n, cat) if cat else u"{}   ({})".format(label, n)
            lookup[text] = label
            options.append(text)
        total = sum(n for _l, _c, n in rows)
        self._status(u"{} type(s), {} item(s) found".format(len(rows), total))
        picked = forms.SelectFromList.show(
            options, multiselect=True, button_name=u"Use selected types",
            title=u"Types to serve - {} type(s), {} item(s) in {}".format(
                len(rows), total, planner.source_description(source)))
        if not picked:
            return                                   # cancelled - keep the current choice
        self._equip_types = [lookup[p] for p in picked if p in lookup]
        self._update_equip_types_text()
        n_items = sum(n for label, _c, n in rows if label in set(self._equip_types))
        self._status(u"{} type(s) picked - {} item(s) will be served. Press Update selected / "
                     u"Add rule, then Preview.".format(len(self._equip_types), n_items))

    def on_clear_equip_types(self, sender, args):
        self._equip_types = []
        self._update_equip_types_text()
        self._status(u"Type filter cleared - every type of the ticked categories (name filter applies)")

    def on_pick_rooms(self, sender, args):
        scope = self._sel_key(self.cbScope, SCOPE_ITEMS, planner.SCOPE_ALL)
        if scope == planner.SCOPE_LEVELS:
            levels = sorted(set(r.level_name for r in self.rooms))
            picked = forms.SelectFromList.show(levels, multiselect=True, title=u"Select levels",
                                               button_name=u"Use levels")
            if picked:
                self.txtRoomFilter.Text = u", ".join(picked)
        else:
            lookup = {}
            for r in self.rooms:
                key = r.full_label if r.full_label not in lookup else u"{} #{}".format(r.full_label, r.id)
                lookup[key] = r.uid
            picked = forms.SelectFromList.show(sorted(lookup.keys()), multiselect=True,
                                               title=u"Pick target rooms",
                                               button_name=u"Use selected rooms")
            if picked:
                self._picked_uids = [lookup[p] for p in picked]
                self._busy = True
                self._select_key(self.cbScope, SCOPE_ITEMS, planner.SCOPE_PICKED)
                self.txtRoomFilter.Text = u"{} room(s) picked".format(len(picked))
                self._busy = False
        self._update_room_match()

    # ==================================================================
    # Event handlers - rule list
    # ==================================================================
    def on_rule_selected(self, sender, args):
        if self._busy:
            return
        idx = self._selected_index()
        if idx >= 0:
            self._set_editor(self.rules[idx].values)
            self.txtEditorTitle.Text = u"Editing rule {}".format(idx + 1)

    def on_new_rule(self, sender, args):
        self._busy = True
        self.rulesGrid.SelectedIndex = -1
        self._busy = False
        self._set_editor(self._default_values())
        self.txtEditorTitle.Text = u"New rule"

    def on_add_rule(self, sender, args):
        rule = self._try_build_from_editor()
        if rule:
            self.rules.append(rule)
            self._refresh_rules_grid(select=len(self.rules) - 1)
            self.txtEditorTitle.Text = u"Editing rule {}".format(len(self.rules))
            self._status(u"Rule added: {}".format(rule.label))

    def on_update_rule(self, sender, args):
        idx = self._selected_index()
        if idx < 0:
            forms.alert(u"Select a rule in the list first (or use 'Add rule').")
            return
        rule = self._try_build_from_editor()
        if rule:
            self.rules[idx] = rule
            self._refresh_rules_grid(select=idx)
            self._status(u"Rule {} updated".format(idx + 1))

    def on_duplicate_rule(self, sender, args):
        idx = self._selected_index()
        if idx >= 0:
            try:
                self.rules.insert(idx + 1, self._build_rule(self.rules[idx].values))
            except ValueError as ex:
                forms.alert(to_text(ex))
                return
            self._refresh_rules_grid(select=idx + 1)

    def on_delete_rule(self, sender, args):
        idx = self._selected_index()
        if idx >= 0:
            del self.rules[idx]
            self._refresh_rules_grid(select=min(idx, len(self.rules) - 1))
            self.on_new_rule(None, None)

    def _move(self, step):
        idx = self._selected_index()
        new = idx + step
        if idx >= 0 and 0 <= new < len(self.rules):
            self.rules[idx], self.rules[new] = self.rules[new], self.rules[idx]
            self._refresh_rules_grid(select=new)

    def on_move_up(self, sender, args):
        self._move(-1)

    def on_move_down(self, sender, args):
        self._move(1)

    # ==================================================================
    # Templates and session restore
    # ==================================================================
    def _dump(self, path):
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        data = {"version": TEMPLATE_VERSION, "rules": [r.values for r in self.rules]}
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(to_text(json.dumps(data, indent=2)))

    def _load(self, path):
        """Load rules from JSON. Returns list of problems (strings)."""
        with io.open(path, "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
        loaded, problems = [], []
        for i, values in enumerate(data.get("rules", []), 1):
            try:
                loaded.append(self._build_rule(values))
            except ValueError as ex:
                problems.append(u"Rule {}: {}".format(i, ex))
        self.rules = loaded
        return problems

    def _save_last_rules(self):
        if not self._can_save:
            return
        try:
            self._dump(LAST_RULES_PATH)
        except Exception:
            pass

    def _restore_last_rules(self):
        if not os.path.isfile(LAST_RULES_PATH):
            return
        try:
            problems = self._load(LAST_RULES_PATH)
            msg = u"Restored {} rule(s) from your last session".format(len(self.rules))
            if problems:
                msg += u" - {} could not be restored (type not loaded?)".format(len(problems))
            self._status(msg)
        except Exception:
            self.rules = []

    def on_save_template(self, sender, args):
        if not self.rules:
            forms.alert(u"There are no rules to save.")
            return
        path = forms.save_file(file_ext="json", default_name="fixture_rules")
        if path:
            self._dump(path)
            self._status(u"Template saved: {}".format(path))

    def on_load_template(self, sender, args):
        path = forms.pick_file(file_ext="json")
        if not path:
            return
        try:
            problems = self._load(path)
        except Exception as ex:
            forms.alert(u"Could not read template:\n{}".format(ex))
            return
        self._refresh_rules_grid(select=0 if self.rules else None)
        if problems:
            forms.alert(u"Some rules were skipped:", expanded=u"\n".join(problems))
        self._status(u"Loaded {} rule(s) from {}".format(len(self.rules), os.path.basename(path)))

    def on_save_options(self, sender, args):
        if self._ui_to_settings():
            self.settings.save()
            self._refresh_rules_grid(select=self._selected_index())
            self._status(u"Options saved as your defaults")

    # ==================================================================
    # Preview / Place
    # ==================================================================
    def _make_plan(self):
        if not self._ui_to_settings():
            return None, None
        active = [r for r in self.rules if r.enabled]
        if not active:
            forms.alert(u"Add at least one enabled rule (Rules tab > Add rule).")
            return None, None
        for r in active:
            r.refresh_mode(self.settings)
            self._rooms_desc(r)                      # resolves r.room_uids
        self._status(u"Calculating placements{}...".format(
            u" and checking clashes" if self.settings.avoid_clashes else u""))
        dup = planner.build_duplicate_index(self.doc, self.settings, active)
        plan = planner.build_plan(self.doc, self.link, self.links, self.rooms, active,
                                  self.settings, dup, self.logger)
        self._show_preview(plan, active)
        return plan, active

    def _show_preview(self, plan, active):
        table = _new_table([u"Rule", u"Family type", u"Room", u"Level", u"Basis",
                            u"Target", u"Planned", u"Note", u"Data (first fixture)"])
        index = dict((id(r), i) for i, r in enumerate(active, 1))
        for row in plan.rows:
            room = row["room"]
            _add_row(table, [index.get(id(row["rule"]), u"?"), row["rule"].label,
                             room.label if room else u"-", room.level_name if room else u"-",
                             row["basis"], row["target"], row["planned"], row["note"],
                             row.get("data", u"")])
        self.previewGrid.ItemsSource = table.DefaultView

        issues = _new_table([u"Room or equipment", u"Level", u"Family type", u"Reason"])
        for subject, lvl, rule_lbl, why in plan.issues:
            _add_row(issues, [subject, lvl, rule_lbl, why])
        self.issuesGrid.ItemsSource = issues.DefaultView

        rooms_hit = len(set(p.room.uid for p in plan.placements))
        target = sum(int(r["target"]) for r in plan.rows)
        summary = (u"{} fixture(s) planned in {} room(s) from {} rule(s)  |  target {}  |  "
                   u"{} issue(s). Nothing has been changed yet."
                   .format(len(plan.placements), rooms_hit, len(active), target, len(plan.issues)))
        if plan.info:
            summary += u"\n" + u"\n".join(u"- " + line for line in plan.info)
        self.txtPreviewSummary.Text = summary
        self._status(u"Preview ready - {} fixture(s) planned".format(len(plan.placements)))

    def on_preview(self, sender, args):
        plan, active = self._make_plan()
        if plan is not None:
            self.tabs.SelectedIndex = 1
            self._trace_preview(plan, active)

    def _trace_preview(self, plan, active):
        """Log every Preview too, so a run that is cancelled afterwards can still be checked."""
        try:
            from elecplace import report
            ctx = report.RunContext()
            ctx.link, ctx.settings, ctx.rules = self.link, self.settings, active
            ctx.rooms, ctx.skipped_rooms, ctx.plan = self.rooms, self.skipped_rooms, plan
            try:
                title = self.doc.Title
            except Exception:
                title = u""
            report.write_trace(ctx, title, stage=u"preview (nothing placed yet)")
        except Exception:
            pass

    def on_place(self, sender, args):
        plan, active = self._make_plan()
        if plan is None:
            return
        self.tabs.SelectedIndex = 1
        if not plan.placements:
            forms.alert(u"Nothing can be placed with these rules - see the issues list.")
            return
        ok = forms.alert(u"Place {} fixture(s) now?".format(len(plan.placements)),
                         sub_msg=u"{} issue(s) are listed in the Preview tab. The whole run can be "
                                 u"undone with a single Ctrl+Z.".format(len(plan.issues)),
                         yes=True, no=True)
        if not ok:
            return
        self.settings.save()
        self._save_last_rules()
        self.result = {"link": self.link, "rooms": self.rooms,
                       "skipped_rooms": self.skipped_rooms, "rules": active, "plan": plan}
        self.Close()

    def on_cancel(self, sender, args):
        self.result = None
        self.Close()
